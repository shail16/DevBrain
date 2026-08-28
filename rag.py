"""
DevBrain — the RAG pipeline itself.

This module is shared by `ingest.py` (write path) and `app.py` (read path).
Keeping the factory functions in one place is what guarantees both sides use
the *same* embedding model and the *same* collection — if they ever disagreed,
ingestion would appear to succeed while every search returned nothing.

Read this file top to bottom to understand the whole system:

    build_embeddings()   text  -> vector          (Cohere)
    build_vector_store() vector store handle      (Postgres + pgvector)
    build_llm()          prompt -> written answer (OpenAI)
    build_rag_chain()    question -> answer + sources
"""

from __future__ import annotations

from langchain_cohere import CohereEmbeddings
from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.retrievers import BaseRetriever
from langchain_core.runnables import Runnable, RunnableParallel, RunnablePassthrough
from langchain_openai import ChatOpenAI
from langchain_postgres import PGVector

from config import Settings

# Cohere's embed endpoint accepts at most 96 texts per request.
COHERE_MAX_BATCH_SIZE = 96


# --------------------------------------------------------------------------- #
#  1. Embeddings — the bridge between text and maths
# --------------------------------------------------------------------------- #


def build_embeddings(settings: Settings) -> CohereEmbeddings:
    """
    Create the Cohere embedding client.

    An "embedding" is a list of ~1024 numbers that represents the *meaning* of
    a piece of text. Texts about similar topics end up close together in that
    1024-dimensional space, which is what makes semantic search possible.

    Cohere's v3 models are asymmetric: they embed a stored document and a
    user's search query slightly differently (`search_document` vs
    `search_query`) for better relevance. `langchain-cohere` sets that flag for
    us depending on whether it is called from `embed_documents` or
    `embed_query`, so there is nothing to configure here.
    """
    return CohereEmbeddings(
        model=settings.cohere_embed_model,
        cohere_api_key=settings.cohere_api_key,
        # Retry transient rate limits instead of failing a long ingestion run.
        max_retries=3,
    )


# --------------------------------------------------------------------------- #
#  2. Vector store — Postgres with the pgvector extension
# --------------------------------------------------------------------------- #


def build_vector_store(
    settings: Settings,
    embeddings: CohereEmbeddings | None = None,
    pre_delete_collection: bool = False,
) -> PGVector:
    """
    Open (or create) the pgvector-backed collection.

    On first use this creates the `vector` extension plus two tables:
      * langchain_pg_collection — one row per collection (a named index)
      * langchain_pg_embedding  — one row per chunk: text, metadata, vector

    Set `pre_delete_collection=True` to drop and recreate the collection, which
    is how `ingest.py --reset` gives you a clean rebuild.
    """
    embeddings = embeddings or build_embeddings(settings)

    return PGVector(
        embeddings=embeddings,
        collection_name=settings.collection_name,
        connection=settings.database_url,
        # Cosine distance is the right default for Cohere's normalised vectors.
        distance_strategy="cosine",
        # Store metadata as JSONB so we can filter on it efficiently later.
        use_jsonb=True,
        # Issues `CREATE EXTENSION IF NOT EXISTS vector` on connect.
        create_extension=True,
        pre_delete_collection=pre_delete_collection,
        engine_args={
            # Recycle connections before Postgres times them out, and check
            # liveness before handing one out — this prevents the
            # "server closed the connection unexpectedly" error that otherwise
            # appears after the API has been idle for a while.
            "pool_pre_ping": True,
            "pool_recycle": 1800,
            "pool_size": 5,
            "max_overflow": 5,
        },
    )


# --------------------------------------------------------------------------- #
#  3. LLM — OpenAI writes the final answer
# --------------------------------------------------------------------------- #


def build_llm(settings: Settings) -> ChatOpenAI:
    return ChatOpenAI(
        model=settings.openai_model,
        temperature=settings.openai_temperature,
        api_key=settings.openai_api_key,
        timeout=60,
        max_retries=2,
    )


# --------------------------------------------------------------------------- #
#  4. The prompt — where "don't hallucinate" is actually enforced
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """\
You are DevBrain, an engineering knowledge assistant. You answer questions \
using ONLY the reference material provided below, which was retrieved from the \
user's own private notes.

Rules you must follow:

1. Ground every claim in the reference material. Never use outside knowledge to
   add facts, versions, numbers, or recommendations.
2. If the reference material does not contain the answer, say exactly this and
   nothing more:
   "I don't have that in the knowledge base. Try rephrasing, or add a document
   covering it to the /data folder and re-run ingestion."
3. If the material only partially answers the question, answer the part you can
   and state plainly which part is missing.
4. Cite the source file after each claim using square brackets, e.g. [redis-notes.md].
   The file name is given in the "Source:" line of each excerpt.
5. Preserve code snippets, commands, and config exactly as written, inside
   fenced markdown code blocks.
6. Be concise and practical. Prefer short paragraphs and bullet lists. Do not
   restate the question or pad the answer with filler.

Reference material:
---------------------
{context}
---------------------
"""

ANSWER_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", SYSTEM_PROMPT),
        ("human", "{question}"),
    ]
)

# Shown when retrieval comes back empty, so the LLM has something concrete to
# refuse against rather than inventing an answer from a blank context.
NO_CONTEXT_PLACEHOLDER = "(No matching documents were found in the knowledge base.)"


def format_documents(documents: list[Document]) -> str:
    """
    Flatten retrieved chunks into the single `{context}` string the prompt asks
    for.

    Each excerpt is labelled with its source (and page/sheet where relevant).
    That label is not decoration: it is what lets the LLM produce real
    citations, and what lets you verify an answer instead of trusting it.
    """
    if not documents:
        return NO_CONTEXT_PLACEHOLDER

    blocks: list[str] = []
    for index, document in enumerate(documents, start=1):
        metadata = document.metadata or {}
        source = metadata.get("source") or metadata.get("file_name") or "unknown"

        locator_parts = []
        if metadata.get("page"):
            locator_parts.append(f"page {metadata['page']}")
        if metadata.get("sheet"):
            locator_parts.append(f"sheet '{metadata['sheet']}'")
        locator = f" ({', '.join(locator_parts)})" if locator_parts else ""

        blocks.append(
            f"[Excerpt {index}]\n"
            f"Source: {source}{locator}\n"
            f"Content:\n{document.page_content.strip()}"
        )

    return "\n\n".join(blocks)


# --------------------------------------------------------------------------- #
#  5. Wiring it all together with LCEL
# --------------------------------------------------------------------------- #


def build_retriever(settings: Settings, vector_store: PGVector) -> BaseRetriever:
    """
    Wrap the vector store in a retriever: `question -> list[Document]`.

    With a score threshold configured we use `similarity_score_threshold`,
    which discards weak matches even if that means returning fewer than `k`
    documents. That trades recall for precision — useful once your knowledge
    base is large enough that irrelevant chunks start creeping in.
    """
    if settings.retriever_score_threshold > 0:
        return vector_store.as_retriever(
            search_type="similarity_score_threshold",
            search_kwargs={
                "k": settings.retriever_k,
                "score_threshold": settings.retriever_score_threshold,
            },
        )

    return vector_store.as_retriever(search_kwargs={"k": settings.retriever_k})


def build_rag_chain(settings: Settings, retriever: BaseRetriever, llm: ChatOpenAI) -> Runnable:
    """
    Assemble the full Retrieval-Augmented Generation chain.

    Input:  a question string
    Output: {"question": str, "docs": list[Document], "context": str, "answer": str}

    Reading the pipe operators in order:

      1. RunnableParallel        run the retriever AND keep the raw question
      2. .assign(context=...)    render those documents into prompt text
      3. .assign(answer=...)     fill the prompt, call OpenAI, take the string

    Because each step is a LangChain Runnable, LangSmith records it as its own
    span — so a trace shows you exactly which chunks were retrieved and the
    fully-rendered prompt that OpenAI actually received.

    We deliberately keep `docs` in the output so the API can return real
    citations to the browser instead of trusting the model's own footnotes.
    """
    generate_answer = ANSWER_PROMPT | llm | StrOutputParser()

    return (
        RunnableParallel(question=RunnablePassthrough(), docs=retriever)
        | RunnablePassthrough.assign(context=lambda payload: format_documents(payload["docs"]))
        | RunnablePassthrough.assign(answer=generate_answer)
    ).with_config({"run_name": "DevBrainRAG"})


def documents_to_citations(documents: list[Document], snippet_chars: int = 320) -> list[dict]:
    """
    Convert retrieved Documents into small JSON-friendly dicts for the UI.

    Chunks from the same file are merged so the sources list stays readable
    when three chunks all came out of the same document.
    """
    citations: dict[str, dict] = {}

    for document in documents:
        metadata = document.metadata or {}
        source = metadata.get("source") or metadata.get("file_name") or "unknown"
        entry = citations.setdefault(
            source,
            {
                "source": source,
                "file_type": metadata.get("file_type", ""),
                "locations": [],
                "snippet": "",
            },
        )

        if metadata.get("page"):
            location = f"page {metadata['page']}"
        elif metadata.get("sheet"):
            location = f"sheet {metadata['sheet']}"
        else:
            location = ""
        if location and location not in entry["locations"]:
            entry["locations"].append(location)

        if not entry["snippet"]:
            text = " ".join(document.page_content.split())
            entry["snippet"] = text[:snippet_chars] + ("…" if len(text) > snippet_chars else "")

    return list(citations.values())

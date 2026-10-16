# Chapter 11 — The RAG Pipeline

*© 2026 Shailendra Srivastava. All rights reserved.*

---

## 11.1 The smallest important file

`rag.py` is 298 lines and contains the entire RAG implementation: embeddings, the
vector store, the LLM, the prompt, the chain, and citation formatting.

That it is this small is the point. Strip away configuration, error handling and
web plumbing, and retrieval-augmented generation is a modest amount of code. The
chain itself is **eight lines**.

`rag.py` is also the module that makes the two pipelines agree. `ingest.py` and
`app.py` both import their embedding client from here, so they cannot drift apart —
the failure Chapter 1 warned about is structurally prevented.

---

## 11.2 Embeddings

```python
COHERE_MAX_BATCH_SIZE = 96

def build_embeddings(settings: Settings) -> CohereEmbeddings:
    return CohereEmbeddings(
        model=settings.cohere_embed_model,
        cohere_api_key=settings.cohere_api_key,
        # Retry transient rate limits instead of failing a long ingestion run.
        max_retries=3,
    )
```

Three lines of configuration, and each earns its place.

**`model` is explicit, never defaulted.** Chapter 3 explained why: change the model
and every stored vector becomes incomparable. Making it explicit means the value
lives in one place and is visible in logs.

**`max_retries=3`** absorbs transient rate limits. A long ingestion run should not
die because one request hit a momentary limit. (Note that this is also what
produced the noisy 401 tracebacks Chapter 10's preflight check solves — retries are
right for *transient* errors and pointless for *permanent* ones.)

**The asymmetric input types are handled for you.** `langchain-cohere` sends
`search_document` from `embed_documents()` and `search_query` from `embed_query()`.
This is the strongest practical argument for using the integration rather than the
raw Cohere SDK — forgetting the input type degrades retrieval in a way that is very
hard to notice.

---

## 11.3 The vector store

```python
def build_vector_store(
    settings: Settings,
    embeddings: CohereEmbeddings | None = None,
    pre_delete_collection: bool = False,
) -> PGVector:
    embeddings = embeddings or build_embeddings(settings)

    return PGVector(
        embeddings=embeddings,
        collection_name=settings.collection_name,
        connection=settings.database_url,
        distance_strategy="cosine",
        use_jsonb=True,
        create_extension=True,
        pre_delete_collection=pre_delete_collection,
        engine_args={
            "pool_pre_ping": True,
            "pool_recycle": 1800,
            "pool_size": 5,
            "max_overflow": 5,
        },
    )
```

### The parameters that matter

**`distance_strategy="cosine"`** — Chapter 3 explained why angle beats distance for
text. This maps to pgvector's `<=>` operator.

**`use_jsonb=True`** stores metadata as JSONB rather than JSON, so it is indexed
and filterable. Chapter 4 covered the difference.

**`create_extension=True`** issues `CREATE EXTENSION IF NOT EXISTS vector` on
connect. This is why you never ran that statement yourself — and why using the
wrong Docker image fails here rather than earlier.

**`pre_delete_collection`** is how `ingest.py --reset` gets a clean rebuild. It
drops the collection row, and `ON DELETE CASCADE` takes the chunks with it.

### The connection pool

```python
"pool_pre_ping": True,
"pool_recycle": 1800,
```

These two prevent a genuinely annoying class of bug.

A connection pool hands out reused connections. If Postgres closed one while it sat
idle — a timeout, a restart, a network blip — the pool does not know, and the next
request fails with *"server closed the connection unexpectedly"* mid-transaction.

**`pool_pre_ping`** sends a cheap `SELECT 1` before handing out a connection. Dead
ones are discarded and replaced transparently.

**`pool_recycle: 1800`** proactively retires connections after 30 minutes, before
Postgres' own idle timeout can act.

Without these, DevBrain works perfectly until it sits idle for a while — then the
first question after the pause fails. That is a horrible bug to diagnose, because
it never reproduces when you are actively testing.

---

## 11.4 The LLM

```python
def build_llm(settings: Settings) -> ChatOpenAI:
    return ChatOpenAI(
        model=settings.openai_model,
        temperature=settings.openai_temperature,
        api_key=settings.openai_api_key,
        timeout=60,
        max_retries=2,
    )
```

**`temperature=0.0` by default.** Temperature controls randomness in token
selection. For factual retrieval you want the same question to give the same
answer — creativity here is a bug, not a feature.

**`timeout=60`** matters more than it looks. Without a timeout, a hung request
holds a worker thread indefinitely. Under load that exhausts your pool and the
whole service stops responding. *Every outbound network call needs a timeout.*

---

## 11.5 The prompt — where grounding is enforced

This is where "don't hallucinate" actually happens. Not by magic — by explicit
instruction.

```python
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
```

### Why each rule exists

**Rule 1** is the core constraint. Note it forbids outside knowledge for *"facts,
versions, numbers, or recommendations"* specifically — those are exactly what a
model tends to helpfully invent.

**Rule 2 prescribes exact wording.** This matters more than it appears. A
consistent refusal string is recognisable to users, testable in code, and cannot be
mistaken for a real answer. Verified in practice: asked *"What is the capital of
France?"*, the model returned that sentence verbatim.

**Rule 3** prevents all-or-nothing behaviour. Partial answers are useful if their
limits are stated.

**Rule 4** produces citations, and points at the `Source:` line so the model knows
where to find the name.

**Rule 5** protects code. Without it, models paraphrase snippets — which is
catastrophic for a developer knowledge base.

**Rule 6** fights padding. Left alone, models restate the question and add a
summary paragraph.

### The delimiters

```
Reference material:
---------------------
{context}
---------------------
```

The dashes are a boundary marker. They help the model distinguish *"material to
reason over"* from *"instructions to follow"* — a small but real defence against
retrieved text being misread as a command.

---

## 11.6 Formatting the context

```python
NO_CONTEXT_PLACEHOLDER = "(No matching documents were found in the knowledge base.)"

def format_documents(documents: list[Document]) -> str:
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

        uploader = metadata.get("uploaded_by")
        credit = f"\nUploaded by: {uploader}" if uploader else ""

        blocks.append(
            f"[Excerpt {index}]\n"
            f"Source: {source}{locator}\n"
            f"Content:\n{document.page_content.strip()}"
        )

    return "\n\n".join(blocks)
```

Retrieved chunks become:

```
[Excerpt 1]
Source: postgres-best-practices.md
Content:
## Connection pooling
Never let an application open one connection per request...

[Excerpt 2]
Source: incident-response-runbook.pdf (page 1)
Content:
SEV1 — complete outage or data loss...
```

**The `Source:` lines are not decoration.** They are the only way the model can
produce a real citation — and therefore the only way you can verify an answer
rather than trusting it. All that Chapter 9 metadata work pays off here.

**The empty-list placeholder matters too.** If retrieval finds nothing, handing the
model a blank context invites improvisation. An explicit *"no matching documents"*
gives it something concrete to refuse against.

---

## 11.7 The retriever

```python
def build_retriever(settings: Settings, vector_store: PGVector) -> BaseRetriever:
    if settings.retriever_score_threshold > 0:
        return vector_store.as_retriever(
            search_type="similarity_score_threshold",
            search_kwargs={
                "k": settings.retriever_k,
                "score_threshold": settings.retriever_score_threshold,
            },
        )

    return vector_store.as_retriever(search_kwargs={"k": settings.retriever_k})
```

A **retriever** wraps the vector store as `question → list[Document]`.

Two modes:

**Plain similarity (default).** Always returns exactly `k` documents — the nearest
`k`, however poor. Good while experimenting: you always see something.

**Score threshold.** Discards weak matches even if that means returning fewer than
`k`, or none. This is where Chapter 3's score table becomes actionable — real
matches scored 0.38–0.55, nonsense scored 0.11. Set the threshold to `0.3` and an
off-topic question retrieves nothing at all, so the model refuses without ever
being tempted.

Precision versus recall, exposed as one number.

---

## 11.8 The chain — eight lines

```python
def build_rag_chain(settings: Settings, retriever: BaseRetriever, llm: ChatOpenAI) -> Runnable:
    generate_answer = ANSWER_PROMPT | llm | StrOutputParser()

    return (
        RunnableParallel(question=RunnablePassthrough(), docs=retriever)
        | RunnablePassthrough.assign(context=lambda payload: format_documents(payload["docs"]))
        | RunnablePassthrough.assign(answer=generate_answer)
    ).with_config({"run_name": "DevBrainRAG"})
```

That is the whole of RAG. Read the pipes left to right.

### Step by step

**Input:** a question string.

**Step 1 — `RunnableParallel`**

```python
RunnableParallel(question=RunnablePassthrough(), docs=retriever)
```

Runs two things on the same input: pass the question through unchanged, *and* send
it to the retriever.

Output: `{"question": "How do I...", "docs": [Document, Document, ...]}`

**Step 2 — add the context**

```python
| RunnablePassthrough.assign(context=lambda payload: format_documents(payload["docs"]))
```

`RunnablePassthrough.assign` keeps everything already in the dict and *adds* a key.

Output: `{"question": ..., "docs": [...], "context": "[Excerpt 1]\nSource: ..."}`

**Step 3 — generate**

```python
| RunnablePassthrough.assign(answer=generate_answer)
```

where `generate_answer = ANSWER_PROMPT | llm | StrOutputParser()`. The prompt needs
`{context}` and `{question}` — both are in the dict.

Final output: `{"question", "docs", "context", "answer"}`

### Why `docs` is kept

This is deliberate and important. The chain could return just the answer. It keeps
the retrieved documents so the API can return **real** citations — built from what
retrieval actually returned, not from what the model claimed.

Chapter 1 called this the difference between a demo and a system you can rely on.

### Why LCEL rather than four function calls

You could write this as plain Python:

```python
docs = retriever.invoke(question)
context = format_documents(docs)
prompt = ANSWER_PROMPT.format(context=context, question=question)
answer = llm.invoke(prompt).content
```

Functionally identical. So why the pipes?

**Because every step is a `Runnable`, LangSmith records each as its own span.** You
get, for free:

```
DevBrainRAG                        1.96s  1229 tokens
├─ RunnableParallel<question,docs> 0.50s
│  └─ VectorStoreRetriever         0.49s  ← which chunks, and their scores
├─ RunnableAssign<context>         0.00s
└─ RunnableAssign<answer>          1.39s
   └─ ChatOpenAI                   1.39s  ← the full rendered prompt
```

The plain version gives you none of that without manual instrumentation.

**This is the payoff for using LangChain at all.** Composition with `|` is not just
tidier syntax — it is what makes the pipeline *observable*. Chapter 17 shows why
that matters more than anything else when an answer is wrong.

`.with_config({"run_name": "DevBrainRAG"})` names the root span so it is findable
among thousands of runs.

---

## 11.9 Citations for the UI

```python
def documents_to_citations(documents: list[Document], snippet_chars: int = 320) -> list[dict]:
    citations: dict[str, dict] = {}

    for document in documents:
        metadata = document.metadata or {}
        source = metadata.get("source") or metadata.get("file_name") or "unknown"
        entry = citations.setdefault(source, {
            "source": source,
            "file_type": metadata.get("file_type", ""),
            "locations": [],
            "snippet": "",
            "uploaded_by": metadata.get("uploaded_by", "") or "",
            "uploaded_at": metadata.get("uploaded_at", "") or "",
        })

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
```

**Deduplication by source** is the main job. If three chunks came from the same
file, showing it three times is noise. They collapse to one entry with merged
locations: *"page 2, page 5"*.

**`" ".join(text.split())`** collapses all whitespace — newlines and runs of spaces
become single spaces — so a snippet renders cleanly in one line.

---

## 11.10 Checkpoint

Test the chain without spending API calls, using a fake LLM:

```python
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from config import load_settings
from rag import build_vector_store, build_retriever, build_rag_chain, format_documents

settings = load_settings()
vs = build_vector_store(settings)
retriever = build_retriever(settings, vs)
chain = build_rag_chain(settings, retriever, FakeListChatModel(responses=["TEST"]))

result = chain.invoke("How do I configure connection pooling?")
print("keys   :", sorted(result))
print("docs   :", len(result["docs"]))
print("context:", result["context"][:150])
```

Expected:

```
keys   : ['answer', 'context', 'docs', 'question']
docs   : 4
context: [Excerpt 1]
Source: postgres-best-practices.md
Content:
## Connection pooling...
```

**Then test retrieval quality for real** (embeddings only — no OpenAI cost):

```python
for q in ["How do I stop connections going stale?",
          "Which datastore for caching sessions?",
          "What is the capital of France?"]:
    hits = vs.similarity_search_with_relevance_scores(q, k=3)
    print(f"\n{q}")
    for doc, score in hits:
        print(f"   {score:.3f}  {doc.metadata['source']}")
```

You should see real questions scoring 0.35–0.55 and the France question collapsing
to ~0.11. That gap is the signal Chapter 3 described, measured on *your* corpus.

---

## 11.11 Mistakes to avoid

**Building embeddings separately in each module.** The whole reason `rag.py` exists.
Import from here.

**Omitting the connection-pool settings.** Works fine in testing; fails after the
first idle period in real use.

**Softening the prompt.** *"Try to use mainly the provided context"* is not a
constraint. Be absolute, and prescribe the refusal wording.

**Returning only the answer.** You lose verifiable citations.

**Raising temperature to make answers "better".** It makes them more varied, which
for factual retrieval means less reliable.

**Setting the score threshold before measuring.** Run the score test above on your
own corpus first — thresholds do not transfer between models or document sets.

---

## 11.12 What you have

The complete RAG implementation in under 300 lines. Shared factories that keep both
pipelines consistent, a prompt that enforces grounding, an eight-line chain that is
automatically traceable, and citations built from ground truth.

Next: wrapping it in a web API that handles failure gracefully.

*Next: [Chapter 12 — The FastAPI Application](ch12-fastapi-application.md)*

# Chapter 21 — Extending DevBrain

*© 2026 Shailendra Srivastava. All rights reserved.*

---

## 21.1 Where the design deliberately stops

DevBrain is complete for what it is: a private knowledge base for one person or a
small team. Several things were left out **on purpose**, and knowing which is part
of understanding the system.

| Left out | Why | When you'd add it |
|---|---|---|
| Approximate vector index | Exact scan is faster *and* accurate under ~100k chunks | Past 100k chunks |
| Reranking | Adds a vendor and latency for a corpus this size | Retrieval brings back near-misses |
| Hybrid search | pgvector alone handles semantic queries well | Exact terms, IDs, error codes fail |
| Streaming | Answers arrive in 1.5–5s | Answers regularly exceed 5s |
| Background jobs | Uploads are small and fast | Large batch uploads time out |
| OCR | Text-based PDFs extract cleanly | You have scanned documents |
| Conversation memory | Each question is independent | Users expect follow-ups |

This chapter covers each: what it is, when it earns its complexity, and how it fits
the existing design.

> **The recurring theme: do not add these speculatively.** Every one costs
> dependencies, latency or money. Measure the problem first.

---

## 21.2 Reranking — the highest-value addition

If you add one thing, add this.

### The problem it solves

Embedding search is fast but coarse. It compares two vectors that were computed
*independently* — the model never sees the question and the document together.

A **reranker** does. It is a cross-encoder: it reads the query and a candidate
passage as one input and scores their actual relevance. Far more accurate, far
slower — which is why it cannot replace the first stage.

### The two-stage pattern

```
Question
   │
   ▼  Stage 1: embedding search, k=20      fast, coarse — narrow the field
   ▼  Stage 2: rerank those 20, keep 4     slow, precise — pick the winners
   ▼  Generation
```

You retrieve *more* than you need, then let a better model choose.

### Fitting it into DevBrain

`build_retriever()` in `rag.py` is the seam. It returns a `BaseRetriever`, and
nothing downstream cares how that retriever works.

```python
from langchain.retrievers import ContextualCompressionRetriever
from langchain_cohere import CohereRerank

def build_retriever(settings, vector_store) -> BaseRetriever:
    base = vector_store.as_retriever(search_kwargs={"k": 20})   # widen stage 1
    reranker = CohereRerank(
        model="rerank-english-v3.0",
        top_n=settings.retriever_k,
        cohere_api_key=settings.cohere_api_key,
    )
    return ContextualCompressionRetriever(
        base_compressor=reranker,
        base_retriever=base,
    )
```

**No other file changes.** The chain, the API, the frontend are untouched — because
Chapter 11 made the retriever an interface rather than a concrete implementation.

### The trade

| | Cost | Benefit |
|---|---|---|
| Latency | +200–400ms | Noticeably better ordering |
| Money | One extra API call per question | — |
| Complexity | One dependency, one function | — |

**Add it when** answers cite documents that are *nearly* right — the topic matches
but the specific passage does not. That is the signature of coarse first-stage
retrieval.

---

## 21.3 Hybrid search — for the queries embeddings are bad at

### The problem

Chapter 3 warned that embeddings capture *topic* far better than *exact tokens*.
They struggle with:

- Error codes — `ORA-01555`
- Version numbers — `pgvector 0.8.6`
- Identifiers — ticket numbers, function names
- Rare proper nouns the model never saw

A question containing `ORA-01555` embeds as "something about database errors", and
you get generic database chunks rather than the one document mentioning that exact
code.

### The fix

Run both searches and merge:

```
Question ──┬──► semantic search (pgvector <=>)     topic matches
           └──► full-text search (Postgres tsvector) exact-token matches
                          │
                          ▼
                 merge and rank (RRF)
```

Postgres already has excellent full-text search, so this needs **no new
infrastructure**:

```sql
ALTER TABLE langchain_pg_embedding
  ADD COLUMN document_tsv tsvector
  GENERATED ALWAYS AS (to_tsvector('english', document)) STORED;

CREATE INDEX ON langchain_pg_embedding USING gin (document_tsv);
```

A generated column stays in sync automatically — no trigger to maintain.

### Merging results

**Reciprocal Rank Fusion** is the standard approach and is pleasantly simple:

```python
def rrf(rankings: list[list[str]], k: int = 60) -> dict[str, float]:
    """Score each id by 1/(k + rank) across every ranking it appears in."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0) + 1 / (k + rank)
    return scores
```

RRF needs no score normalisation, which matters because cosine similarity and
`ts_rank` are on completely different scales.

**Add it when** users search for exact strings — codes, IDs, versions — and get
topically-related but wrong results.

---

## 21.4 Streaming — the cheapest perceived-latency win

### The problem

A question takes 1.5–5 seconds, and almost all of it is generation. The user stares
at a skeleton loader.

### The fix

Stream tokens as they are produced. Time-to-first-token drops to under a second,
and the answer appears progressively.

The chain already supports it — `Runnable` provides `.astream()` for free.

```python
from fastapi.responses import StreamingResponse

@app.post("/api/ask/stream")
async def ask_stream(request: AskRequest, user: auth.User = Depends(current_user)):
    pipeline = get_pipeline(app)

    async def generate():
        async for chunk in pipeline.chain.astream(request.question):
            if "answer" in chunk:
                yield f"data: {json.dumps({'token': chunk['answer']})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
```

### The catch

**This requires solving Chapter 20's Bug 2 properly.** `astream` is async, and
`PGVector` is synchronous. You would need either:

- A second `PGVector` built with `async_mode=True` for the read path, or
- Retrieval run in a threadpool, with only generation streamed

The second is simpler: retrieval is 0.5s of a 2s request, so streaming only the
generation captures most of the benefit.

**Add it when** answers regularly exceed 5 seconds, or users are on slow
connections.

---

## 21.5 OCR — for scanned documents

### The gap

Chapter 9 was honest about this: if a PDF is photographs of pages, there is no text
layer. Ingestion reports it:

```
!  empty   scan.pdf: no extractable text (a scanned/image-only PDF is the usual cause)
```

### Two routes

**Tesseract** — local, free, ~100 MB, no torch. Good for clean scans, weaker on
complex layouts.

**A document-understanding model** (MinerU, Azure Document Intelligence, AWS
Textract) — far better on tables, multi-column layouts and formulas. Costs
gigabytes locally or money per page.

For reference, MinerU 3.x supports Python 3.10–3.13 and its base wheel is small —
but the `[pipeline]` extra pulls torch, torchvision, transformers and onnxruntime,
taking a ~600 MB image to several gigabytes plus ~1 GB of model weights. It also
ships under a **custom licence**, not a standard OSI one, which matters if you
redistribute.

### The design that keeps it cheap

Do not replace `pypdf`. Use a **smart fallback**:

```
PDF → pypdf (milliseconds)
        ├─ got good text?  → use it              ← ~95% of cases
        └─ near-empty?     → OCR                 ← only the hard ones
```

```python
def load_pdf_file(path: Path) -> list[Document]:
    documents = _extract_with_pypdf(path)

    chars = sum(len(d.page_content) for d in documents)
    if chars < 50 * _page_count(path):        # suspiciously little text
        return _extract_with_ocr(path)

    return documents
```

Chapter 9's dispatch table makes this contained: the change lives entirely in
`loaders.py`, and nothing downstream knows.

For the heavier engines, run them as a **sidecar container** so torch never enters
the DevBrain image.

**Add it when** ingestion actually reports files as `empty`. Not before.

---

## 21.6 Conversation memory

### The problem

Every question is independent. Ask *"What is the deploy window?"* then *"Who
approves it?"* and the second question retrieves nothing — "it" is meaningless in
isolation.

### The standard fix: query rewriting

Do **not** simply concatenate history into the retrieval query; that muddies the
embedding. Instead, use the LLM to rewrite the follow-up as a standalone question:

```python
CONDENSE_PROMPT = ChatPromptTemplate.from_messages([
    ("system",
     "Given the conversation so far and a follow-up question, rewrite the "
     "follow-up as a standalone question that makes sense on its own. "
     "Do not answer it. If it is already standalone, return it unchanged."),
    ("human", "Conversation:\n{history}\n\nFollow-up: {question}"),
])

condense = CONDENSE_PROMPT | llm | StrOutputParser()
```

*"Who approves it?"* becomes *"Who approves the deployment window?"* — which
retrieves correctly.

### What else it requires

- A `devbrain_conversations` table (you already have the auth schema pattern)
- Session-scoped history in the API
- A chat-style UI instead of single question/answer
- An extra LLM call per follow-up

**This is the largest item in the chapter.** It changes the data model, the API and
the UI. Add it only if users genuinely expect conversation — many knowledge-base
users are happy with one-shot questions.

---

## 21.7 Smaller improvements

### Vector index (past ~100k chunks)

```sql
CREATE INDEX ON langchain_pg_embedding
USING hnsw (embedding vector_cosine_ops);
```

The operator class must match your distance operator (Chapter 4). Measure before
and after — under 100k chunks this makes things *worse*, not better.

### Metadata filtering

`cmetadata` is JSONB with a GIN index, so filtering is already fast:

```python
retriever = vector_store.as_retriever(
    search_kwargs={"k": 4, "filter": {"file_type": "pdf"}}
)
```

Useful for *"search only the runbooks"* or scoping to one user's documents.

### Evaluation

The most valuable engineering addition after reranking.

Build a set of question/expected-source pairs and run it after every change:

```python
CASES = [
    ("How do I stop stale connections?", "postgres-best-practices.md"),
    ("Which datastore for caching sessions?", "tech-stack-decisions.xlsx"),
    ("What is the capital of France?", None),   # must refuse
]
```

Without this, "did that change help?" is a matter of opinion. With it, it is a
number. LangSmith has built-in dataset and evaluation support that plugs into the
tracing you already have.

### Rate limiting

Chapter 19 flagged that DevBrain has none. `slowapi` adds per-user limits in a few
lines — worth it before any shared deployment.

---

## 21.8 How to decide

A rough order of value for most teams:

| Priority | Addition | Trigger |
|---|---|---|
| 1 | **Evaluation set** | Immediately — you cannot improve what you cannot measure |
| 2 | **Reranking** | Answers cite nearly-right documents |
| 3 | **Rate limiting** | Before any shared deployment |
| 4 | **Hybrid search** | Exact-term queries fail |
| 5 | **Streaming** | Answers exceed ~5 seconds |
| 6 | **OCR** | Ingestion reports `empty` files |
| 7 | **Conversation memory** | Users expect follow-ups |
| 8 | **Vector index** | Past ~100k chunks |

Note that **evaluation comes first**, before any of the improvements. Every other
item on this list is a hypothesis about what will help. Without a measurement, you
are guessing — and RAG systems are unusually good at *seeming* better after a change
that made them worse.

---

## 21.9 What makes this extensible

The additions above are cheap because of decisions made early:

**`build_retriever()` returns an interface.** Reranking and hybrid search slot in
without touching the chain, API or frontend.

**`_LOADER_BY_EXTENSION` is a dictionary.** New formats and OCR fallbacks are one
entry plus a function.

**Configuration is environment-driven.** New settings follow the existing pattern
and inherit validation and provenance reporting.

**The chain is Runnables.** Anything you add is automatically traced.

**`db.py` uses plain SQL.** Nothing hides the schema from you.

> **This is what "extensible" actually means** — not that everything is
> configurable, but that the seams are in the places where change is likely.

---

## 21.10 The end of the book

You have built a complete RAG system:

**Part I** gave you the concepts — retrieval, embeddings, vector search, chunking.

**Part II** set up the environment — Docker, dependencies, configuration.

**Part III** built the write path — loaders that extract cleanly, ingestion that is
idempotent.

**Part IV** built the read path — a grounded prompt, an eight-line chain, an API
that degrades gracefully, a frontend with no dependencies.

**Part V** made it multi-user — bcrypt, revocable sessions, ownership, an admin
console, safe uploads.

**Part VI** made it operable — tracing, containers, deployment, and the six bugs
that shaped it.

### The three things worth carrying to your next project

**Make the dangerous mistake structurally impossible.** One shared `rag.py` means
the two pipelines cannot drift to different embedding models. That is worth more
than any test.

**Assume failures will be silent.** Four of six real bugs reported success while
doing the wrong thing. Assert on outcomes, not on the absence of exceptions.

**Error messages are a feature.** *"OPENAI_API_KEY is missing. Get one at
https://platform.openai.com/api-keys"* costs thirty seconds.
`KeyError: 'OPENAI_API_KEY'` costs an afternoon. The difference is one line of code
and a moment's empathy.

### And the one about AI specifically

`rag.py` — the entire AI implementation — is under 300 lines. `app.py` and
`index.html` are half the project.

**The hard part of an AI application is rarely the AI.** It is configuration, error
handling, authentication, file parsing, and telling the user clearly what went
wrong. Budget accordingly.

---

*Appendices follow: [A — File Listing](appendix-a-file-listing.md) ·
[B — Commands](appendix-b-commands.md) ·
[C — Glossary](appendix-c-glossary.md) ·
[D — Troubleshooting](appendix-d-troubleshooting.md)*

# Appendix C — Glossary

*© 2026 Shailendra Srivastava. All rights reserved.*

Terms as they are used in this book, with the chapter that introduces each.

---

### ASGI
The async server interface FastAPI implements; `uvicorn` is the ASGI server that
runs it. The successor to WSGI, adding async and WebSocket support. *(Ch 6)*

### Asymmetric embedding model
An embedding model that encodes a stored document differently from a search query,
so a question lands near the passages that *answer* it rather than near passages
that merely resemble it. Cohere v3 uses `search_document` and `search_query` input
types. *(Ch 3)*

### bcrypt
A deliberately slow password-hashing function with a tunable cost factor and
built-in salting. Slowness is the feature: it makes brute-forcing a stolen hash
impractical. DevBrain uses 12 rounds (~250ms). *(Ch 14)*

### Chunk
A small piece of a document — roughly 1000 characters here — and the unit that
gets embedded, stored and retrieved. *(Ch 5)*

### Chunk overlap
Characters shared between consecutive chunks (150 in DevBrain) so an idea straddling
a boundary survives intact in at least one chunk. *(Ch 5)*

### Collection
A named group of chunks inside the vector store, roughly analogous to a table name.
Two collections can coexist in one database. *(Ch 4)*

### Context window
The maximum amount of text a language model can consider at once. *(Ch 2)*

### Cosine similarity
Closeness measured as the angle between two vectors, ignoring length. 1.0 =
identical direction, 0.0 = unrelated. Chosen over Euclidean distance because
direction encodes meaning while length mostly encodes verbosity. *(Ch 3)*

### Cross-encoder
A model that reads a query and a candidate passage *together* and scores their
relevance. More accurate than comparing independently-computed embeddings, and much
slower — hence its use as a second-stage reranker. *(Ch 21)*

### Deterministic chunk ID
A content-derived primary key — `sha256(collection|source|index|text)` — that makes
re-ingestion idempotent, because identical content produces an identical ID and the
store upserts on it. *(Ch 10)*

### Embedding
A list of numbers (1024 here) representing the *meaning* of a piece of text. Texts
about similar topics land close together in that space. *(Ch 3)*

### Grounded
An answer is grounded when every claim can be traced to a specific passage in a
specific source document. DevBrain enforces this through retrieval limits, prompt
rules and separately-returned sources. *(Ch 1)*

### Hallucination
Fluent, confident, factually wrong model output. Not a malfunction — it is a
next-token predictor working as designed on a question it cannot answer. The core
problem RAG exists to solve. *(Ch 2)*

### HNSW
*Hierarchical Navigable Small World* — an approximate nearest-neighbour index.
Trades a little recall for large speed gains at scale. DevBrain omits it deliberately
below ~100k chunks. *(Ch 4)*

### HttpOnly cookie
A cookie JavaScript cannot read, so an XSS bug cannot steal it. The main reason to
prefer a cookie over `localStorage` for session tokens. *(Ch 14)*

### Hybrid search
Combining semantic (vector) search with keyword (full-text) search and merging the
rankings — usually with Reciprocal Rank Fusion. Fixes queries containing exact
tokens like error codes. *(Ch 21)*

### Idempotent
An operation that produces the same result whether run once or many times. DevBrain's
ingestion is idempotent thanks to deterministic chunk IDs. *(Ch 10)*

### JSONB
PostgreSQL's binary, indexable JSON type. Used for `cmetadata` so metadata filtering
is fast; plain `json` would store raw text. *(Ch 4)*

### LCEL
*LangChain Expression Language* — composing steps with the `|` operator. Not just
tidier syntax: because every step is a `Runnable`, the pipeline becomes
automatically traceable. *(Ch 11)*

### Lost in the middle
The documented effect where language models reason measurably worse when the
relevant text is buried in a long context. Undermines the "just paste everything"
approach even when the corpus fits. *(Ch 2)*

### Multipart form data
The HTTP encoding used for file uploads. FastAPI needs `python-multipart` to parse
it, and the browser must set the `Content-Type` header itself because it contains a
generated boundary. *(Ch 16)*

### pgvector
A PostgreSQL extension adding a `vector` column type and distance operators
(`<=>` cosine, `<->` Euclidean, `<#>` inner product). Requires the
`pgvector/pgvector` Docker image, not stock `postgres`. *(Ch 4)*

### Preflight check
Validating credentials with one cheap call *before* doing expensive work. DevBrain
embeds one short string before a batch ingestion, turning a wall of retry tracebacks
into one clean line. *(Ch 10)*

### Provenance (configuration)
Reporting *where* each setting came from — `environment`, `.env` or `default` —
without revealing values. Turns "which key is it using?" into a one-line answer.
*(Ch 8)*

### RAG
*Retrieval-Augmented Generation* — retrieve relevant passages, then have a model
answer using only those. Stops using the model as a source of facts and starts using
it as a reading-comprehension engine over facts you supply. *(Ch 2)*

### Reranking
A second retrieval stage: fetch ~20 candidates by embedding search, then use a
cross-encoder to pick the best 4. The highest-value single improvement to retrieval
quality. *(Ch 21)*

### Retriever
A component taking a question and returning relevant `Document` objects. In DevBrain
it is an *interface*, which is why reranking and hybrid search can slot in without
touching anything downstream. *(Ch 11)*

### RRF
*Reciprocal Rank Fusion* — merging multiple rankings by scoring each item
`1/(k + rank)` across the rankings it appears in. Needs no score normalisation,
which matters when combining incomparable scales. *(Ch 21)*

### Runnable
LangChain's standard interface (`invoke`, `stream`, `batch`). Anything that is a
Runnable can be piped into anything else and is traced automatically. *(Ch 11)*

### Semantic search
Searching by meaning rather than keyword. Finds *"reusing database handles"* for the
query *"stale connections"* despite zero shared words. *(Ch 3)*

### Session (server-side)
A row in the database representing an active login, checked on every request. Chosen
over JWTs because it supports **instant revocation** — which the requirement
"a deleted user loses access immediately" demands. *(Ch 14)*

### Span
One step within a trace, with its own inputs, outputs, duration and token usage.
DevBrain's `VectorStoreRetriever` and `ChatOpenAI` spans are the two you read most.
*(Ch 17)*

### Trace
A recorded tree of every step in one run. The primary tool for telling a retrieval
failure apart from a generation failure. *(Ch 17)*

### Upsert
Insert a row, or update it if the key already exists. **Only updates the columns you
name** — the subtlety behind Chapter 20's first bug. *(Ch 4, 20)*

### Vector database
A store optimised for "find the vectors nearest to this one". DevBrain uses
PostgreSQL + pgvector rather than a dedicated one, trading ultimate scale for
operational simplicity. *(Ch 4)*

### XSS
*Cross-site scripting* — injecting executable script through data. DevBrain defends
by escaping **before** adding any markup, so a document containing `<script>` renders
as visible text. *(Ch 13)*

---

## Model and dimension reference

| Model | Provider | Dimensions | Notes |
|---|---|---|---|
| `embed-english-v3.0` | Cohere | 1024 | DevBrain's default; asymmetric |
| `embed-multilingual-v3.0` | Cohere | 1024 | 100+ languages |
| `text-embedding-3-small` | OpenAI | 1536 | Supports dimension truncation |
| `text-embedding-3-large` | OpenAI | 3072 | Highest quality, ~5× cost |

> Dimensions are **fixed per model**. Changing `COHERE_EMBED_MODEL` requires
> `python ingest.py --reset` — and note that switching between two *1024-dimensional*
> models will not error, it will just quietly return wrong results.

---

## Score interpretation

Real cosine similarity scores from the project's corpus:

| Range | Meaning |
|---|---|
| 0.50 – 0.60 | Strong match — the chunk directly answers the question |
| 0.35 – 0.50 | Good match — relevant, typical for a real answer |
| 0.20 – 0.35 | Weak — related topic, probably not the answer |
| below 0.20 | Nothing relevant exists |

**Do not expect 0.9 for good matches.** A question and the passage answering it are
related but not identical. Judge scores relative to each other within your own
corpus, and note that thresholds do not transfer between models or document sets.

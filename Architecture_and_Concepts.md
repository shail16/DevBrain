# DevBrain — Architecture and Concepts

A beginner-friendly deep dive. No prior AI experience assumed. By the end you
should be able to explain every box in the diagram, and to open any file in this
project and know why each line is there.

---

## Contents

1. [The problem RAG solves](#1-the-problem-rag-solves)
2. [The big picture](#2-the-big-picture)
3. [Embeddings: turning meaning into numbers](#3-embeddings-turning-meaning-into-numbers)
4. [Why a vector database, and why pgvector](#4-why-a-vector-database-and-why-pgvector)
5. [Chunking: why we cut documents up](#5-chunking-why-we-cut-documents-up)
6. [The two pipelines](#6-the-two-pipelines)
7. [What each technology actually does](#7-what-each-technology-actually-does)
8. [Code walkthrough](#8-code-walkthrough)
9. [How LangSmith tracks all of this](#9-how-langsmith-tracks-all-of-this)
10. [Design decisions and trade-offs](#10-design-decisions-and-trade-offs)
11. [Two bugs this design had to solve](#11-two-bugs-this-design-had-to-solve)
12. [Deployment architecture](#12-deployment-architecture)
13. [Glossary](#13-glossary)

---

## 1. The problem RAG solves

A large language model like GPT-4o knows an enormous amount about the public
internet up to its training cutoff. It knows **nothing** about:

- your team's internal notes
- your architecture decisions
- anything written after its training cutoff

Worse, when a model does not know something, its default behaviour is not to say
*"I don't know"* — it is to produce fluent, confident, wrong text. This is called
**hallucination**, and it is what makes a raw chatbot dangerous as a knowledge
base.

There are three ways to fix this:

| Approach | How it works | Cost | Updating it |
|---|---|---|---|
| **Fine-tuning** | Retrain the model's weights on your data | Thousands of dollars, hours of GPU time | Retrain from scratch |
| **Long context** | Paste all your documents into every prompt | Enormous per question; hits token limits fast | Free |
| **RAG** ← *this project* | Find the few relevant paragraphs, paste only those | Fractions of a cent per question | Re-run one script |

**Retrieval-Augmented Generation** splits the job in two:

1. **Retrieval** — a search engine finds the handful of paragraphs in *your*
   documents that are most relevant to the question.
2. **Generation** — the LLM is handed those paragraphs and instructed to answer
   *using only them*.

The model stops being a source of facts and becomes a **reading-comprehension
and writing engine** over facts you supply. That is the key mental shift.

This is why DevBrain refuses to answer "What is the capital of France?" even
though GPT-4o obviously knows the answer. Nothing in your documents mentions
Paris, so nothing relevant is retrieved, and the prompt forbids answering from
outside knowledge. **That refusal is a feature.** A system that answers
questions your documents do not cover is a system you cannot trust on the
questions they *do* cover.

---

## 2. The big picture

```mermaid
flowchart TB
    subgraph ingest["INGESTION — run once per document change (ingest.py)"]
        direction LR
        A["📁 data/<br/>txt · pdf · docx · xlsx"] --> B["Loaders<br/>extract raw text"]
        B --> C["Splitter<br/>~1000-char chunks"]
        C --> D["Cohere<br/>text → 1024 numbers"]
        D --> E[("PostgreSQL<br/>+ pgvector")]
    end

    subgraph query["QUERY — runs on every question (app.py)"]
        direction LR
        F["🌐 index.html<br/>user types a question"] --> G["FastAPI<br/>/api/ask"]
        G --> H["Cohere<br/>question → 1024 numbers"]
        H --> I[("pgvector<br/>find 4 nearest chunks")]
        I --> J["Prompt<br/>chunks + question + rules"]
        J --> K["OpenAI<br/>writes the answer"]
        K --> L["Answer + sources<br/>back to the browser"]
    end

    E -.->|"stored vectors"| I
    query -.->|"every step reported"| M["🔍 LangSmith<br/>trace"]
```

The single most important idea: **both pipelines use the same embedding model.**
Ingestion converts documents to vectors; querying converts the question to a
vector using the identical model. Only then are the numbers comparable. Using a
different model on each side produces vectors in two unrelated coordinate
systems, and search results become noise.

That is exactly why `rag.py` exists as a shared module — `ingest.py` and
`app.py` both call the same `build_embeddings()` function, so they cannot drift
apart.

---

## 3. Embeddings: turning meaning into numbers

### The core idea

An **embedding** is a list of numbers that represents the *meaning* of a piece
of text. Cohere's `embed-english-v3.0` produces exactly **1024 numbers** for any
input, whether it is one word or three paragraphs.

```
"database connection pooling"  →  [0.021, -0.114, 0.087, ..., 0.043]   (1024 numbers)
"how to reuse DB connections"  →  [0.019, -0.108, 0.091, ..., 0.041]   (very similar!)
"chocolate cake recipe"        →  [-0.201, 0.334, -0.076, ..., 0.512]  (totally different)
```

Think of each number as a coordinate. Two numbers give a point on a map; three
give a point in a room; 1024 give a point in a space we cannot picture but
mathematics handles perfectly well. **Texts about similar topics land close
together in that space.**

This is what makes DevBrain *semantic*. Ordinary keyword search for "connection
pooling" would miss a note titled "reusing database handles" because they share
no words. Embedding search finds it, because the two phrases mean nearly the
same thing and therefore sit near each other.

### How closeness is measured

DevBrain uses **cosine similarity**: the angle between two vectors, ignoring
their length.

- Angle near 0° → score near **1.0** → nearly identical meaning
- Angle near 90° → score near **0.0** → unrelated

Angle rather than distance matters because a long document and a short question
about it have very different vector *lengths* but point in the same *direction*.
Direction is meaning; length is mostly verbosity.

Here are real scores from this project's sample documents:

| Question | Best-matching document | Score |
|---|---|---|
| "How many dimensions do Cohere embeddings have?" | `tech-stack-decisions.xlsx` | **0.544** |
| "How big should a pull request be?" | `code-review-standards.docx` | **0.540** |
| "Which datastore for caching sessions?" | `tech-stack-decisions.xlsx` | **0.487** |
| "Why avoid alpine images for Python?" | `docker-tips.md` | **0.469** |
| "What is the capital of France?" | *(best of a bad lot)* | **0.112** |

Notice the last row. Nothing relevant exists, so every score collapses. That gap
between ~0.4 and ~0.1 is what `RETRIEVER_SCORE_THRESHOLD` lets you exploit: set
it to `0.3` and off-topic questions retrieve nothing at all.

### One subtlety: asymmetric embeddings

Cohere's v3 models embed a stored document slightly differently from a search
query (`search_document` vs `search_query` mode). A question and its answer are
not phrased alike — "How do I stop stale connections?" versus "Enable
`pool_pre_ping` before handing out a connection" — so the model learns to place
a *question* near the documents that *answer* it, rather than near documents
that merely repeat it.

You do not have to configure this. `langchain-cohere` sets the correct mode
depending on whether it is embedding documents or a query.

---

## 4. Why a vector database, and why pgvector

### The problem

After ingestion you have thousands of vectors. For each question you must find
the handful closest to the question's vector. Comparing against every stored
vector one at a time works for a hundred documents and collapses at a million.

A **vector database** stores vectors and answers *"which stored vectors are
nearest to this one?"* efficiently, using specialised indexes.

### Why PostgreSQL with pgvector

`pgvector` is an extension that teaches ordinary PostgreSQL to store a `vector`
column type and to compare vectors with new operators:

| Operator | Distance |
|---|---|
| `<=>` | cosine (what DevBrain uses) |
| `<->` | Euclidean (L2) |
| `<#>` | negative inner product |

That means a nearest-neighbour search is just SQL:

```sql
SELECT document, cmetadata
FROM langchain_pg_embedding
ORDER BY embedding <=> '[0.021, -0.114, ...]'   -- the question's vector
LIMIT 4;
```

The advantage over a dedicated vector database (Pinecone, Weaviate, Chroma) is
that there is **one** database. Your vectors sit beside your relational data, in
the same transaction, with the same backups, the same access control, and the
same tooling you already know. You can inspect everything in pgAdmin. You can
`JOIN` a vector search against a `users` table. Nothing is a black box.

The trade-off: purpose-built vector databases scale further. Past roughly 10–50
million vectors, or when you need sub-10ms p99 latency at high concurrency, a
specialised store starts to win. For a personal or team knowledge base —
thousands to low millions of chunks — pgvector is comfortably the right choice,
and it is what this project uses.

### The two tables

`langchain-postgres` creates these automatically on first use:

**`langchain_pg_collection`** — one row per named knowledge base

| Column | Meaning |
|---|---|
| `uuid` | Primary key |
| `name` | Your `COLLECTION_NAME`, e.g. `devbrain_docs` |
| `cmetadata` | Optional JSON about the collection |

**`langchain_pg_embedding`** — one row per chunk

| Column | Meaning |
|---|---|
| `id` | Text primary key — DevBrain supplies a deterministic one (see §11) |
| `collection_id` | Foreign key to the collection, `ON DELETE CASCADE` |
| `document` | The chunk's raw text |
| `cmetadata` | JSONB: `source`, `page`, `sheet`, `chunk_index`, `file_type` |
| `embedding` | The `vector(1024)` itself |

`cmetadata` is JSONB rather than plain JSON because JSONB is indexed (there is a
GIN index on it), which makes metadata filtering fast.

### A note on indexes

DevBrain does not create an HNSW or IVFFlat index. Below roughly 100,000 chunks,
PostgreSQL's exact sequential scan is fast enough (single-digit milliseconds)
and is *perfectly accurate*. Approximate indexes trade a little recall for
speed, and that trade only pays off at scale. When you outgrow it:

```sql
CREATE INDEX ON langchain_pg_embedding
USING hnsw (embedding vector_cosine_ops);
```

---

## 5. Chunking: why we cut documents up

You cannot embed a whole 40-page PDF as one vector, for two independent reasons.

**1. Meaning gets averaged away.** A single 1024-number vector representing an
entire document is a blurry average of every topic in it. A question about one
paragraph will not match it strongly, because that paragraph is 2% of the
average.

**2. Prompts have limits.** Even with a large context window, pasting whole
documents into every prompt is slow and expensive, and models genuinely reason
worse when the relevant sentence is buried in tens of thousands of tokens
("lost in the middle").

So we cut documents into **chunks** of about 1000 characters. Each chunk gets
its own vector, so retrieval can pinpoint the exact paragraph that answers the
question.

### Why chunks overlap

DevBrain uses `CHUNK_OVERLAP=150`, meaning consecutive chunks share their last
and first 150 characters. Without overlap, a hard cut can orphan an idea:

```
...always enable pool_pre_ping, which sends a cheap SELECT 1     ← chunk 1 ends
before handing the connection to the caller...                   ← chunk 2 begins
```

Now neither chunk explains what `pool_pre_ping` does. The overlap keeps the
sentence intact in at least one chunk. It costs a little duplicated storage and
is almost always worth it.

### Why splitting is "recursive"

`RecursiveCharacterTextSplitter` is given a list of separators in priority
order, and only escalates to a more aggressive one when a piece is still too
big. DevBrain's list is tuned for technical notes:

```python
separators=[
    "\n## ",    # markdown h2   — a section boundary is the best place to cut
    "\n### ",   # markdown h3
    "\n```",    # fenced code block boundary
    "\n\n",     # paragraph
    "\n",       # line
    ". ",       # sentence
    " ",        # word
    "",         # single character — last resort
]
```

It tries to split on section headings first. Only if a section is still over
1000 characters does it split on paragraphs, then lines, and so on. The result
is chunks that are **semantically whole** rather than arbitrary 1000-character
slabs — and, thanks to the ` ``` ` entry, code snippets that are far less likely
to be cut in half.

### Chunk size is a real trade-off

| | Small chunks (~400) | Large chunks (~2000) |
|---|---|---|
| Retrieval precision | High — pinpoints exact facts | Low — vectors are blurry averages |
| Context per hit | Little; answers may lack background | Plenty |
| Best for | Reference tables, FAQs, short notes | Essays, long-form reasoning |

1000 with 150 overlap is a good general default. Change it in `.env` and re-run
`python ingest.py --reset`.

---

## 6. The two pipelines

### Ingestion (write path) — `ingest.py`

Runs when you change documents. Steps:

```
1. DISCOVER   walk data/, keep supported extensions, skip ~$ and dotfiles
2. LOAD       parse each file into LangChain Documents          (loaders.py)
3. SPLIT      cut into ~1000-char overlapping chunks
4. EMBED      Cohere: 96 chunks per API call
5. STORE      INSERT ... ON CONFLICT DO UPDATE into pgvector
```

Two details worth knowing:

- **Batching at 96.** Cohere accepts at most 96 texts per request. Batching also
  means one failure costs you one batch rather than the whole run, and you get
  progress output instead of a silent multi-minute wait.
- **Deterministic IDs.** Each chunk's primary key is a SHA-256 hash of
  *(collection, source, chunk index, text)*. Because the store upserts on that
  key, re-running ingestion overwrites unchanged chunks instead of duplicating
  them. Without this, running `python ingest.py` twice would double every
  answer's context.

### Query (read path) — `app.py`

Runs on every question:

```
1. The browser POSTs {"question": "..."} to /api/ask
2. Short-circuit if the knowledge base is empty (saves a pointless API call)
3. Cohere embeds the question                          → 1024 numbers
4. pgvector finds the k nearest chunks                 → SELECT ... ORDER BY <=>
5. format_documents() labels each chunk with its source
6. The prompt template pastes them in alongside the rules
7. OpenAI writes the answer
8. Return answer + the REAL retrieved sources + latency + trace ID
```

Step 8 matters more than it looks. The answer's own bracketed citations are
generated *by the model* and could in principle be wrong. The `sources` list in
the JSON response is built from the documents the retriever actually returned,
so it is ground truth.

---

## 7. What each technology actually does

| Technology | Role in DevBrain | What breaks without it |
|---|---|---|
| **FastAPI** | HTTP server; validates requests, serves the UI, auto-generates `/docs` | No web interface |
| **LangChain** | The glue: a common `Document` type, text splitters, and the LCEL syntax that wires steps together | You would hand-write the plumbing between Cohere, Postgres and OpenAI |
| **Cohere** | Embeddings — converts text to 1024-number vectors, on both sides of the pipeline | No semantic search |
| **PostgreSQL + pgvector** | Stores chunks, metadata and vectors; performs nearest-neighbour search | Nowhere to keep or search the knowledge |
| **OpenAI** | Reads the retrieved chunks and writes a fluent answer | You would get raw chunks, not prose |
| **LangSmith** | Records every step of every question for debugging | The pipeline is a black box when answers go wrong |
| **pgAdmin** | Web UI to inspect the database directly | You would need a SQL client |
| **Docker** | Runs Postgres and pgAdmin identically on any machine | Manual database installation |

### Why two different AI providers?

It looks odd to use Cohere *and* OpenAI, but they do genuinely different jobs:

- **Embedding models** are small, fast, and cheap. They only produce coordinates.
  You call one thousands of times during ingestion.
- **Generative models** are large and comparatively expensive. They produce text.
  You call one once per question.

Using the best tool for each is normal in production RAG. Cohere's v3 embeddings
are strong, generous on the free tier, and the asymmetric query/document modes
suit search work particularly well.

### A word on LangChain

LangChain gets criticised for being a heavy abstraction, and sometimes fairly.
This project uses it for the parts that genuinely pay for themselves:

- a single `Document` type shared by every loader, splitter and store
- `RecursiveCharacterTextSplitter`, which is fiddly to write well
- the `PGVector` store, which manages the schema and SQL for you
- **LCEL**, which gives automatic LangSmith tracing for free

It deliberately avoids the heavier abstractions. There are no agents, no memory
objects, and no `langchain.chains` helpers — just explicit, readable composition
you can follow line by line.

---

## 8. Code walkthrough

The project is split into small modules so each has one job:

```
config.py     read and validate .env, configure LangSmith
loaders.py    file on disk       → list[Document]
rag.py        the shared AI components and the chain
db.py         plain SQL for health checks and stats
ingest.py     the write path (CLI)
app.py        the read path (web server)
index.html    the browser UI
```

### `config.py` — one source of truth

Reads `.env` exactly once into a frozen dataclass. Both scripts import it, which
guarantees they agree on the embedding model and collection name — a mismatch
there is the number-one cause of *"ingestion worked but search returns
nothing"*.

It fails **early and readably**. Rather than a traceback halfway through a
five-minute ingestion run, you get:

```
[X] Configuration error
DevBrain is not configured yet:
  - OPENAI_API_KEY is missing. Get one at https://platform.openai.com/api-keys
```

Two touches worth copying into your own projects:

```python
def _normalise_db_url(url: str) -> str:
    # Beginners paste `postgresql://...` (the format every other tool uses),
    # which raises a confusing SQLAlchemy driver error. Upgrade it silently.
    for legacy in ("postgresql+psycopg2://", "postgres://", "postgresql://"):
        if url.startswith(legacy):
            return "postgresql+psycopg://" + url[len(legacy):]
    return url
```

```python
@property
def safe_database_url(self) -> str:
    """The connection string with the password masked, safe to log."""
```

Every log line prints `devbrain:****@localhost:5433` — you can paste terminal
output into a bug report without leaking your password.

### `loaders.py` — files to Documents

One function per file type, each returning `list[Document]`. Notable choices:

**PDF → one Document per page**, so a citation can say *page 3*. Pages that
extract no text (scans) are skipped rather than stored as empty chunks.

**Text files try four encodings** (`utf-8-sig`, `utf-8`, `cp1252`, `latin-1`)
before giving up. Notes exported from Windows tools are frequently not clean
UTF-8, and one bad byte should not abort a whole ingestion run.

**Excel gets the most care.** A bare grid of values embeds terribly, because a
number loses all meaning once separated from its column heading. So when the
first row looks like a header, DevBrain repeats the column name on every row:

```
Worksheet: Datastores
Technology: Redis | Category: Cache / KV store | Use Case: Session storage, ...
Technology: Kafka | Category: Event streaming | Use Case: Durable event log, ...
```

Now a chunk pulled from the middle of a 500-row sheet still carries enough
context for the LLM to interpret it. This is why the question *"Which datastore
should I use for caching sessions?"* correctly retrieves the spreadsheet row
about Redis.

### `rag.py` — the AI components and the chain

The heart of the project. `build_embeddings`, `build_vector_store`, `build_llm`
and `build_retriever` are small factory functions; the interesting parts are the
prompt and the chain.

**The prompt is where "don't hallucinate" is actually enforced.** Not by magic —
by explicit instruction:

```python
SYSTEM_PROMPT = """\
You are DevBrain... You answer questions using ONLY the reference material
provided below, which was retrieved from the user's own private notes.

1. Ground every claim in the reference material. Never use outside knowledge...
2. If the reference material does not contain the answer, say exactly this...
4. Cite the source file after each claim using square brackets...

Reference material:
---------------------
{context}
---------------------
"""
```

`{context}` is filled by `format_documents()`, which labels each excerpt:

```
[Excerpt 1]
Source: postgres-best-practices.md
Content:
## Connection pooling
Never let an application open one connection per request...
```

Those `Source:` lines are not decoration — they are what allow the model to
produce real citations, and therefore what lets you verify an answer rather than
trusting it.

**The chain** uses LCEL (LangChain Expression Language), where `|` pipes one
step into the next:

```python
return (
    RunnableParallel(question=RunnablePassthrough(), docs=retriever)
    | RunnablePassthrough.assign(context=lambda payload: format_documents(payload["docs"]))
    | RunnablePassthrough.assign(answer=ANSWER_PROMPT | llm | StrOutputParser())
).with_config({"run_name": "DevBrainRAG"})
```

Read it as three steps:

1. `RunnableParallel` runs the retriever **and** keeps the original question.
   Output: `{"question": ..., "docs": [...]}`
2. `.assign(context=...)` adds a `context` key by formatting those documents.
3. `.assign(answer=...)` adds an `answer` key by filling the prompt, calling
   OpenAI, and taking the string.

Final output: `{"question", "docs", "context", "answer"}`.

Keeping `docs` in the output is deliberate — that is how the API returns real
citations instead of trusting the model's own footnotes.

Because every step is a Runnable, LangSmith records each as its own span. That
is the whole reason to use LCEL rather than four plain function calls.

### `ingest.py` — the write path

A normal CLI with `--reset`, `--dry-run`, `--stats` and `--data-dir`.

The most important function is small:

```python
def chunk_id(chunk: Document, collection_name: str) -> str:
    source = str(chunk.metadata.get("source", "unknown"))
    index  = str(chunk.metadata.get("chunk_index", 0))
    digest = hashlib.sha256(
        f"{collection_name}|{source}|{index}|{chunk.page_content}".encode("utf-8")
    ).hexdigest()
    return f"{collection_name}:{source}:{index}:{digest[:16]}"
```

Same content → same ID → the database upserts instead of duplicating. Edit a
document and the text changes, so the hash changes, so you get a genuinely new
row. (Why `collection_name` is in there is explained in §11 — it fixes a real
bug.)

There is also a **preflight check** before the batch loop:

```python
info("  checking Cohere credentials...")
preflight_check_embeddings(embeddings)   # embeds one short string
info("  Cohere OK")
```

Without it, a wrong API key is discovered *inside* the batch loop, after
LangChain's retry logic has dumped three enormous 401 tracebacks. One tiny call
upfront turns that into one clean line.

### `app.py` — the read path

FastAPI with four endpoints: `/`, `/api/health`, `/api/config`, `/api/ask`.

**Expensive objects are built once and cached.** The embedding client, the
connection pool, the OpenAI client and the assembled chain live in a `Pipeline`
object created on first use, not per request.

**Construction failures do not kill the server.** If Postgres is not up yet,
startup still succeeds and `/api/health` *explains the problem*. The failure is
not cached, so the next request retries — start your containers and hit refresh.
A process that dies instantly with a traceback is far harder for a newcomer to
debug than a page that says "Cannot reach PostgreSQL — try `docker compose up -d`".

**`_explain_failure()` translates upstream errors into actions.** Rather than
surfacing a raw SDK exception, it maps common cases:

| Upstream error contains | What the user sees |
|---|---|
| `insufficient_quota` | "Your OpenAI account has no remaining quota. Add credit at…" |
| `invalid_api_key` | "OpenAI rejected the API key. Check OPENAI_API_KEY in your .env" |
| `expected … dimensions` | "Vector size mismatch… re-ingest with `python ingest.py --reset`" |
| `connection refused` | "Lost the connection to Postgres. Check `docker compose ps`" |

### `db.py` — plain SQL on purpose

Health checks and stats use raw SQL instead of going through LangChain. That is
deliberate: it demonstrates the most reassuring fact about the whole project —
your embeddings are not locked inside a black box. They are ordinary rows you
can query yourself in pgAdmin.

The engine is `lru_cache`d because creating one sets up a connection pool;
building a new engine on every health check would leak connections until
Postgres refused new ones.

### `index.html` — dependency-free frontend

No framework, no build step, no CDN. Three jobs: poll `/api/health`, POST to
`/api/ask`, and render the answer.

The rendering is worth a look, because it is where an XSS bug would live. The
order is deliberate:

```javascript
function renderMarkdown(raw) {
  let text = esc(raw);          // 1. escape EVERYTHING first
  // 2. pull fenced code blocks out into placeholders
  // 3. apply inline formatting to the prose
  // 4. put the code blocks back
}
```

Because escaping happens before any markup is added, a document containing
`<script>alert(1)</script>` renders as visible text and can never execute — even
inside a code fence.

---

## 9. How LangSmith tracks all of this

### What it does

LangSmith is an observability platform for LLM applications. It records the
inputs, outputs, latency, token counts and cost of every step of every run.

### How it hooks in — there is almost no code

This is the part that surprises people. Look at all the tracing code in DevBrain:

```python
os.environ["LANGSMITH_TRACING"] = "true"
os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
```

That is it. Once those variables are set, every LangChain component checks for
them at import time and attaches a **callback handler** to itself. Every
Runnable — retriever, prompt, chat model — already emits `on_start` / `on_end` /
`on_error` events as it executes. The tracer listens to those events and ships
them to LangSmith in a background thread, so it does not slow your response.

That is the real payoff of building the chain from Runnables rather than plain
functions. Composition with `|` is not just tidier syntax; it is what makes the
pipeline *observable*.

DevBrain adds one small extra:

```python
with collect_runs() as collector:
    result = chain.invoke(question)
    if collector.traced_runs:
        run_id = str(collector.traced_runs[0].id)
```

This captures the run's ID so the API can return it to the browser. Every answer
in the UI shows a short trace ID, so you can find the exact run that produced it.

### What a trace looks like

```
DevBrainRAG                                    2.41s   $0.0003
├── RunnableParallel                           0.28s
│   ├── RunnablePassthrough (question)         0.00s
│   └── VectorStoreRetriever                   0.28s
│       ├── input:  "How do I configure connection pooling?"
│       └── output: 4 documents  [postgres-best-practices.md × 2, docker-tips.md × 2]
├── RunnableAssign (context)                   0.00s
└── RunnableAssign (answer)                    2.13s
    ├── ChatPromptTemplate                     0.00s
    │   └── output: the FULL prompt, with your documents pasted in
    └── ChatOpenAI                             2.13s
        ├── tokens: 1,204 in / 187 out
        └── output: "Enable `pool_pre_ping` so a stale connection..."
```

### Why this matters for debugging

When an answer is wrong, there are only two possibilities, and the trace tells
you which:

1. **The retriever pulled the wrong chunks.** A retrieval problem. Fix it with
   `RETRIEVER_K`, `CHUNK_SIZE`, or better source documents.
2. **The retriever pulled the right chunks and the answer is still poor.** A
   generation problem. Fix it with the prompt or a stronger model.

Without a trace you are guessing. With one you can read the exact text that was
sent to OpenAI. That single capability is worth more than every other debugging
tool in this project combined.

---

## 10. Design decisions and trade-offs

**Cosine distance, not Euclidean.** Cohere's vectors are normalised, and angle
captures meaning while length mostly captures verbosity.

**No approximate index (HNSW/IVFFlat).** Below ~100k chunks an exact scan is
both fast enough and perfectly accurate. Approximate indexes trade recall for
speed, and that trade does not pay off yet.

**`temperature=0.0` by default.** For factual retrieval you want the same
question to give the same answer. Creativity is a bug here, not a feature.

**Sources returned separately from the answer.** The model's own bracketed
citations are generated text and could be wrong. The `sources` array is built
from what the retriever actually returned, so it is ground truth.

**`/api/ask` is a plain `def`, not `async def`.** Explained in §11 — it fixes a
real crash.

**Custom loaders instead of `langchain_community`.** That package is being
sunset and emits deprecation warnings, and its Excel loader needs the heavy
`unstructured` dependency. Writing three small loaders directly against `pypdf`,
`docx2txt` and `openpyxl` means fewer dependencies, no warnings, better error
messages, and full control over the spreadsheet flattening that turned out to
matter so much for answer quality.

**Ports 5433 / 5051 / 8001 instead of 5432 / 5050 / 8000.** So DevBrain runs
alongside other Postgres instances on the same machine without a fight. All
three are configurable in `.env`.

### What you would add for production

DevBrain is a solid local single-user system. To take it further:

| Need | Approach |
|---|---|
| Faster search at scale | `CREATE INDEX ... USING hnsw (embedding vector_cosine_ops)` |
| Better retrieval quality | Add a **reranker** (Cohere Rerank) over the top 20 hits |
| Keyword + semantic together | **Hybrid search**: combine pgvector with Postgres full-text search |
| Answers appearing word by word | Stream tokens with `chain.astream()` and Server-Sent Events |
| Multiple users | Add auth, and a `user_id` filter on the metadata |
| Knowing if changes helped | Build a LangSmith evaluation dataset of question/answer pairs |

---

## 11. Two bugs this design had to solve

Both were caught by testing before you ever ran the project. They are worth
understanding because both are easy to hit in your own RAG work.

### Bug 1 — the silent collection thief

`langchain_pg_embedding.id` is a **single global primary key**, shared by every
collection. The library's upsert clause is:

```sql
INSERT INTO langchain_pg_embedding (...) VALUES (...)
ON CONFLICT (id) DO UPDATE
SET embedding = excluded.embedding,
    document  = excluded.document,
    cmetadata = excluded.cmetadata;
```

Look at what is missing: **`collection_id` is never updated.**

The original `chunk_id()` hashed only *(source, chunk index, text)*. So
ingesting the same `data/` folder into a second collection produced identical
IDs, hit the primary-key conflict, and updated rows **still owned by the first
collection**. The second collection ended up empty — while ingestion cheerfully
reported *"18/18 chunks stored"*.

The fix is one line: include the collection name in the hash, so IDs are unique
per collection. There is now a regression test that ingests into two collections
and asserts both keep their own chunks.

**The lesson:** an upsert only updates the columns you list. If a row's identity
spans more than the primary key, put all of it in the key.

### Bug 2 — sync store, async call

`PGVector` is created in synchronous mode, which is what `ingest.py` needs. But
the endpoint originally called `await chain.ainvoke(...)`. The async path asks
the store for an async engine, which a sync store does not have:

```
AssertionError: This method must be called with async_mode
```

Every single question would have failed with a 502.

The fix is to declare the handler as a plain `def`. FastAPI automatically runs
synchronous handlers in a worker thread, so the blocking database and OpenAI
calls never stall the event loop and requests are still served concurrently.
Keeping the whole call on one thread also makes the `collect_runs` context
variable behave predictably.

**The lesson:** in FastAPI, `async def` is not "the fast option". It is a
promise that everything inside is non-blocking. When your libraries are
synchronous, plain `def` is both correct *and* faster.

---

## 12. Deployment architecture

Part 12 of the User Guide gives the commands. This section explains *why* the
deployment is shaped the way it is.

### Two topologies, one codebase

```mermaid
flowchart TB
    subgraph dev["MODE A — Development"]
        direction TB
        D1["uvicorn --reload<br/><b>on your host</b>"]
        D2[("devbrain-postgres<br/>container")]
        D1 -->|"localhost:5433<br/>(published port)"| D2
    end

    subgraph prod["MODE B/C — Containerised"]
        direction TB
        subgraph net["Docker network 'devbrain_default'"]
            P1["devbrain-app<br/>uvicorn --workers 2"]
            P2[("devbrain-postgres")]
            P1 -->|"postgres:5432<br/>(service name, internal port)"| P2
        end
        BROWSER["browser"] -->|"localhost:8001<br/>(published port)"| P1
    end
```

The application code is **byte-identical** in both. Only two environment
variables differ: the database host and the bind address. That is not an
accident — it is the whole point of reading configuration from the environment.

### Why `localhost` breaks inside a container

Every container gets its own network namespace, which means its own loopback
interface. When code inside `devbrain-app` connects to `localhost:5433`, it is
asking *itself* for port 5433, not your machine. Nothing is listening, so the
connection is refused.

Two distinct address spaces exist:

| Address | Meaning | Valid from |
|---|---|---|
| `localhost:5433` | The **published** port, mapped by `ports:` onto your host | Your host only |
| `postgres:5432` | The **service name** on the Compose network, at the container's real port | Other containers only |

Compose runs an embedded DNS server that resolves service names to container
IPs, so `postgres` is a real hostname inside that network. Note the port also
changes: the `5433` is purely a host-side mapping, invented to avoid clashing
with other Postgres instances on your machine. Inside the network, Postgres
listens on its normal `5432`.

`docker-compose.yml` handles this by overriding `DATABASE_URL` for the app
service only:

```yaml
environment:
  DATABASE_URL: postgresql+psycopg://devbrain:devbrain_pass@postgres:5432/devbrain
```

Compose gives `environment:` precedence over `env_file:`, so your `.env` stays
correct for host-based development while the container gets the value it needs.

### Why the app sits behind a Compose profile

```yaml
app:
  profiles: ["full"]
```

A service with a profile is skipped unless that profile is requested. So:

- `docker compose up -d` → database only, and you run Python on your host with
  hot reload
- `docker compose --profile full up -d` → the complete containerised stack

One file describes both topologies, with no duplication and no second compose
file to keep in sync. The default remains the fast development loop, because
that is what you use ninety-nine times out of a hundred.

### Why the image is built in two stages

```dockerfile
FROM python:3.13-slim AS builder     # has compilers, build headers
RUN /opt/venv/bin/pip install -r requirements.txt

FROM python:3.13-slim AS runtime     # clean slate
COPY --from=builder /opt/venv /opt/venv
```

Only the finished virtualenv crosses the boundary. Everything used to *build*
it — compilers, headers, pip's cache — is discarded with the builder stage. The
runtime image ends up around 600 MB rather than well over a gigabyte, and it
carries a smaller attack surface because there is no toolchain inside it to
abuse.

### Why `requirements.txt` is copied before the source code

```dockerfile
COPY requirements.txt .              # changes rarely
RUN pip install -r requirements.txt  # slow, gets cached
COPY *.py index.html ./              # changes constantly
```

Docker caches each instruction as a layer and invalidates every layer after the
first change it detects. Copying source code first would invalidate the
dependency layer on every single edit, forcing a full reinstall each rebuild.
Ordered this way, editing `app.py` rebuilds in seconds.

The general rule: **order instructions from least- to most-frequently-changed.**

### Why the container runs as a non-root user

```dockerfile
RUN groupadd --system devbrain && useradd --system --gid devbrain ... devbrain
USER devbrain
```

By default a container process runs as root, and container root maps to
meaningful privilege on the host in several escape scenarios. Running as an
unprivileged user means a compromise through the application lands somewhere
that cannot write to system paths or install anything.

### Why configuration is injected, never baked in

`.dockerignore` excludes `.env` from the build context on purpose. Secrets in an
image are permanent: they live in a layer, survive being "deleted" by a later
instruction, and travel to every registry the image is pushed to.

Instead, `env_file: .env` supplies configuration at *run* time. The same image
runs in development, staging and production; only the environment differs. This
is the **config** principle from the twelve-factor app methodology, and it is
what makes an image promotable rather than rebuilt per environment.

Two consequences worth noticing:

- `config.py` had to stop treating a missing `.env` as an error. In a container
  there is no `.env` file — the values arrive as real environment variables. It
  now only mentions `.env` when something is genuinely unset.
- `load_dotenv(..., override=False)` means real environment variables win over
  the file. That ordering is what lets Compose override `DATABASE_URL`.

### Why the healthcheck matters

```dockerfile
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/api/health', timeout=4)"
```

`depends_on` alone only waits for a container to **start**, not to become
usable. Postgres accepts TCP connections several seconds before it can serve
queries. That gap is why the database has its own healthcheck and the app
declares:

```yaml
depends_on:
  postgres:
    condition: service_healthy
```

The app's own healthcheck is what an orchestrator uses to decide whether to send
traffic to it. Because `/api/health` returns `503` when Postgres is unreachable,
`urlopen` raises and the container is correctly marked unhealthy — the signal is
honest rather than merely "the process is alive".

### The security model, stated plainly

DevBrain has **no authentication**. Every design decision assumes it is running
on `localhost` for one person:

- CORS is `allow_origins=["*"]`, so opening `index.html` from disk works
- The database password is committed to the repo
- pgAdmin runs in desktop mode with no login

None of that is wrong for a personal tool, and all of it is wrong the moment the
service is reachable from a network. The boundary is deliberate: DevBrain
handles *retrieval*, and access control belongs in a reverse proxy in front of
it. The production checklist in the User Guide exists because these defaults are
safe only under an assumption that deployment breaks.

### Where the state actually lives

| What | Where | If you lose it |
|---|---|---|
| Vectors and chunks | Docker volume `devbrain_pgdata` | Rebuild: `ingest.py --reset` |
| Source documents | `./data` on the host | **Gone** — back this up |
| Secrets | `.env` on the host | **Gone** — back this up |
| Application code | The image, rebuilt from git | Rebuild |

Only two of those four are irreplaceable, and the database is not one of them.
That is a deliberate property: as long as you hold `data/` and your API keys,
the entire knowledge base is reproducible with one command. It makes the
deployment far less precious than it first appears.

---

## 13. Glossary

**Chunk** — a small piece of a document (~1000 characters here), the unit that
gets embedded and retrieved.

**Collection** — a named set of chunks inside the vector store. Like a table
name; lets you keep several knowledge bases in one database.

**Context window** — the maximum amount of text an LLM can consider at once.

**Cosine similarity** — closeness measured as the angle between two vectors.
1.0 = identical direction, 0.0 = unrelated.

**Embedding** — a list of numbers (1024 here) representing the meaning of a
piece of text.

**Grounding** — requiring the model to answer only from supplied source
material.

**Hallucination** — fluent, confident, factually wrong output. The core problem
RAG exists to solve.

**LCEL** — LangChain Expression Language; composing steps with the `|` operator
so the pipeline is declarative and automatically traceable.

**Retriever** — a component that takes a question and returns relevant
Documents.

**Runnable** — LangChain's standard interface (`invoke`, `stream`, `batch`).
Anything that is a Runnable can be piped into anything else, and is traced
automatically.

**Semantic search** — searching by meaning rather than by keyword.

**Trace** — a recorded tree of every step in one run, with inputs, outputs,
timings and costs.

**Upsert** — insert a row, or update it if the key already exists.

**Vector database** — a store optimised for "find the vectors nearest to this
one".

---

## Suggested reading order for the code

1. `config.py` — what the system needs to know
2. `loaders.py` — files become Documents
3. `ingest.py` — the write path, top to bottom
4. `rag.py` — the prompt and the chain (the conceptual heart)
5. `app.py` — the read path
6. `index.html` — the browser side

Then open a LangSmith trace beside `rag.py` and match each span to the line that
produced it. That is the moment RAG stops feeling like magic.

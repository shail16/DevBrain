# Chapter 1 — What DevBrain Is, and Why It Exists

*© 2026 Shailendra Srivastava. All rights reserved.*

---

## 1.1 The problem, stated plainly

You have knowledge scattered across files. Architecture decisions in a Word
document. SQL snippets in a Markdown file. An incident runbook in a PDF. A
spreadsheet comparing datastores. Every one of those answers a question somebody
on your team asks every few weeks.

Finding those answers is miserable. Full-text search fails you constantly: you
search for *"connection pooling"* and miss the note titled *"reusing database
handles"* because it shares no words with your query. You know the answer exists.
You just cannot find it.

Meanwhile, ChatGPT will answer any question you ask — confidently, fluently, and
with no idea whatsoever what your team decided last March.

**DevBrain sits between those two failures.** It reads your documents, and answers
questions about them in plain language — but it will refuse to answer anything
your documents do not cover.

That refusal is the entire point, and it is worth dwelling on before we write a
line of code.

---

## 1.2 Why the refusal matters

Here is a real exchange with the finished system:

> **Q:** How do I stop database connections from going stale?
>
> **A:** Always enable `pool_pre_ping` in SQLAlchemy. This sends a cheap
> `SELECT 1` before handing a connection to the caller, which avoids the "server
> closed the connection unexpectedly" error mid-transaction.
> `[postgres-best-practices.md]`

And here is the same system asked something outside its knowledge:

> **Q:** What is the capital of France?
>
> **A:** I don't have that in the knowledge base. Try rephrasing, or add a
> document covering it to the /data folder and re-run ingestion.

GPT-4o obviously knows that Paris is the capital of France. DevBrain refuses
anyway.

**This is not a limitation. It is the feature.** A system that will answer
questions your documents do not cover is a system you cannot trust on the
questions they *do* cover — because you can never tell which mode it is in. Once
the model is willing to draw on outside knowledge, every answer becomes suspect,
and you are back to verifying everything by hand.

By making refusal the default, every answer DevBrain *does* give carries an
implicit guarantee: **this came from your documents, and here is which one.**

---

## 1.3 The two pipelines

DevBrain has exactly two moving parts. Almost every design decision in this book
follows from keeping them consistent with each other.

### Pipeline 1 — Ingestion (the write path)

Runs when documents change. Turns files into searchable vectors.

```
data/*.pdf .docx .xlsx .md .txt
        │
        ▼  1. DISCOVER   walk the folder, keep supported types
        ▼  2. LOAD       parse each file into text + metadata
        ▼  3. SPLIT      cut into ~1000-character overlapping chunks
        ▼  4. EMBED      Cohere: each chunk → 1024 numbers
        ▼  5. STORE      write text + metadata + vector to Postgres
   PostgreSQL + pgvector
```

### Pipeline 2 — Query (the read path)

Runs on every question. Finds relevant chunks and writes an answer.

```
   "How do I stop stale connections?"
        │
        ▼  1. EMBED      Cohere: the question → 1024 numbers
        ▼  2. SEARCH     pgvector: find the 4 nearest chunks
        ▼  3. ASSEMBLE   paste those chunks into a prompt with rules
        ▼  4. GENERATE   OpenAI writes the answer
        ▼  5. RETURN     answer + the real sources it used
   Browser
```

### The rule that ties them together

> **Both pipelines must use the same embedding model.**

Ingestion converts documents to vectors. Querying converts the question to a
vector. Those numbers are only comparable if the same model produced them. Use a
different model on each side and you get vectors in two unrelated coordinate
systems — search returns noise, and nothing about the failure will look like a
bug. Ingestion succeeds. Queries succeed. The answers are just quietly wrong.

This is why the project has a shared module, `rag.py`, that both sides import.
Neither `ingest.py` nor `app.py` is allowed to construct its own embedding
client. We will build that in Chapter 11, but the constraint is worth carrying
from the very beginning.

---

## 1.4 The technology map

Nine pieces. Here is what each does and — more usefully — what breaks without it.

| Technology | Role | Without it |
|---|---|---|
| **FastAPI** | HTTP server, request validation, auto-generated docs | No web interface |
| **LangChain** | Common `Document` type, text splitters, LCEL composition | You hand-wire the plumbing between three vendors |
| **Cohere** | Embeddings — text → 1024 numbers, on both pipelines | No semantic search |
| **PostgreSQL + pgvector** | Stores chunks, metadata and vectors; nearest-neighbour search | Nowhere to keep or search the knowledge |
| **OpenAI** | Reads retrieved chunks, writes fluent prose | You get raw chunks, not answers |
| **LangSmith** | Records every step of every question | The pipeline is a black box when answers go wrong |
| **pgAdmin** | Web UI to inspect the database | You need a separate SQL client |
| **Docker** | Runs Postgres and pgAdmin identically anywhere | Manual database installation |
| **bcrypt** | Password hashing | Plaintext passwords, which is indefensible |

### Why two different AI vendors?

Using Cohere *and* OpenAI looks like over-engineering. It is not — they do
genuinely different jobs:

**Embedding models** are small, fast and cheap. They only produce coordinates.
You call one *thousands* of times during ingestion. Cohere's `embed-english-v3.0`
is strong, generous on its free tier, and has a feature that matters for search
(covered in Chapter 3).

**Generative models** are large and comparatively expensive. They produce text.
You call one *once per question*.

Picking the best tool for each job is normal in production RAG. It also means a
bad month from one vendor does not take down the whole system.

### A word about LangChain

LangChain attracts criticism for being a heavy abstraction over things you could
write yourself. Often that criticism is fair. This project uses it deliberately
and narrowly, for the four things that genuinely pay for themselves:

1. A single `Document` type shared by every loader, splitter and store
2. `RecursiveCharacterTextSplitter`, which is fiddly to write well
3. `PGVector`, which manages the schema and SQL for you
4. **LCEL** — which gives automatic LangSmith tracing for free

It deliberately avoids the heavier machinery. There are no agents, no memory
objects, no `langchain.chains` helpers. Just explicit composition you can follow
line by line. Chapter 11 shows the entire chain in eight lines of code.

---

## 1.5 What "grounded" actually means

You will meet the word *grounded* throughout this book. It has a precise meaning
here:

> An answer is **grounded** when every claim in it can be traced to a specific
> passage in a specific source document.

DevBrain enforces grounding in three places, and it takes all three:

**1. Retrieval limits what the model can see.** The model is never shown your
whole corpus — only the handful of chunks that matched the question. It cannot
cite what it was not given.

**2. The prompt forbids outside knowledge.** Explicitly, in words, with a
prescribed refusal message. Chapter 11 shows the exact text.

**3. The API returns the real sources separately.** This is the subtle one. The
bracketed citations in the answer text are *generated by the model* and could in
principle be wrong. So the JSON response carries a `sources` array built from
what the retriever *actually returned* — ground truth, independent of anything the
model said.

That third point is the difference between a demo and a system you can rely on.

---

## 1.6 What you will actually build

The finished project is roughly 5,700 lines across twelve files. Here is the map,
with the chapter that builds each one:

| File | Lines | Role | Chapter |
|---|---|---|---|
| `config.py` | 360 | Settings, validation, source provenance | 8 |
| `loaders.py` | 292 | Files → `Document` objects | 9 |
| `ingest.py` | 583 | The write path, as a CLI | 10 |
| `rag.py` | 298 | Prompt, LCEL chain, citations | 11 |
| `app.py` | 1,090 | The read path, as a web API | 12, 15, 16 |
| `index.html` | 1,960 | The entire frontend, no framework | 13 |
| `db.py` | 149 | Plain SQL for health and stats | 12 |
| `auth.py` | 577 | Users, sessions, document ownership | 14 |
| `docker-compose.yml` | 158 | Postgres, pgAdmin, the app | 7, 18 |
| `Dockerfile` | 82 | Multi-stage production image | 18 |
| `requirements.txt` | 30 | Pinned dependencies | 6 |
| `set-env.ps1` | 165 | Secret management | 8 |

Note the shape of it: **`app.py` and `index.html` are half the project.** The AI
parts — embeddings, retrieval, the chain — are small. `rag.py` is under 300
lines and contains the entire RAG implementation.

This surprises most people. It should tell you something useful: **the hard part
of an AI application is rarely the AI.** It is configuration, error handling,
authentication, file parsing, and telling the user clearly what went wrong. Budget
your learning accordingly.

---

## 1.7 Design principles

Five decisions shape everything that follows. Each is revisited where it applies.

### One source of truth for configuration

Both pipelines read from `config.py`, which reads from environment variables. They
cannot drift apart. A mismatch in embedding model between ingestion and query is
the single most damaging bug in a RAG system, and it is silent — so we make it
structurally impossible.

### Fail loudly, early, and with instructions

An error message that says `KeyError: 'OPENAI_API_KEY'` wastes your afternoon. One
that says *"OPENAI_API_KEY is missing. Get one at https://platform.openai.com/api-keys"*
costs you thirty seconds. Every failure path in this project is written for
somebody who does not already know the answer.

### Degrade gracefully

If Postgres is down, the server still starts — and `/api/health` explains exactly
what is wrong and how to fix it. A process that dies instantly with a traceback is
much harder for a newcomer to debug than a page that says *"Cannot reach
PostgreSQL — try `docker compose up -d`."*

### Nothing is a black box

The embeddings are ordinary rows in ordinary Postgres tables. You can open pgAdmin
and look at them. `db.py` deliberately uses plain SQL rather than going through
LangChain, partly to prove that point. Chapter 4 has you run the queries yourself.

### Verify, don't trust

Every claim in this book was tested. When Chapter 20 says an upsert silently stole
rows between collections, that is because a test caught it doing exactly that. When
retrieval scores are quoted, they came from a real run.

---

## 1.8 Checkpoint

You should now be able to answer these without looking back. If any is unclear,
re-read the relevant section before continuing — Chapter 2 builds directly on this.

1. Why is DevBrain refusing to answer a question a *feature* rather than a bug?
2. What are the two pipelines, and what is the one rule that must hold between them?
3. Why does the API return a `sources` array instead of relying on the citations
   inside the answer text?
4. Why does the project use Cohere *and* OpenAI rather than one vendor?
5. Which two files make up half the codebase, and what does that tell you about
   where the difficulty in AI applications actually lies?

---

## What's next

Chapter 1 gave you the map. Chapter 2 goes down one level: *why* retrieval is the
right answer to hallucination, what the alternatives cost, and the mental shift
that makes RAG click.

*Next: [Chapter 2 — The Problem RAG Solves](ch02-the-problem-rag-solves.md)*

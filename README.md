# DevBrain

A private, local AI knowledge base over your own engineering documents.

Drop notes, runbooks, specs and spreadsheets into `data/` — or upload them
straight from the browser — and ask questions. Answers come **only** from your
documents, with the source files listed underneath so you can verify them.

Uploaded files are saved to `data/` **and ingested in the same request**, so
they are searchable immediately: no CLI step, no restart.

```
data/*.pdf .docx .xlsx .md .txt
        │
        ▼  ingest.py — chunk, embed (Cohere), store
  PostgreSQL + pgvector
        │
        ▼  app.py — retrieve, prompt, generate (OpenAI)
  http://localhost:8001
```

## Quick start

```bash
# 1. Configure — paste your OpenAI, Cohere and LangSmith keys
copy .env.example .env

# 2. Install
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

# 3. Start the database
docker compose up -d

# 4. Load your documents
python ingest.py

# 5. Run
uvicorn app:app --reload --port 8001
```

Then open <http://localhost:8001>.

## Run it fully containerised

The steps above run Postgres in Docker and Python on your host (best for
development). To run everything in Docker instead:

```bash
docker compose --profile full up -d --build
docker compose --profile full run --rm app python ingest.py
```

Same URL. See [User_Guide.md → Deployment](User_Guide.md#12-deployment) for
server deployment, HTTPS, backups and the production checklist.

## Documentation

| Document | What's in it |
|---|---|
| **[User_Guide.md](User_Guide.md)** | Full setup walkthrough, command reference, tuning, troubleshooting |
| **[Architecture_and_Concepts.md](Architecture_and_Concepts.md)** | How it works — embeddings, pgvector, chunking, and a walkthrough of every file |

## Stack

| Layer | Technology |
|---|---|
| Frontend | Plain HTML/CSS/JS — no build step |
| API | FastAPI + Uvicorn |
| Orchestration | LangChain (LCEL) |
| Embeddings | Cohere `embed-english-v3.0` (1024-dim) |
| Generation | OpenAI `gpt-4o-mini` |
| Vector store | PostgreSQL 17 + pgvector (Docker) |
| Database UI | pgAdmin 4 (Docker) |
| Tracing | LangSmith |
| Auth | bcrypt password hashing + server-side sessions |

## Files

```
app.py          FastAPI server, RAG + auth + admin    (read path)
auth.py         users, sessions, document registry
ingest.py       document ingestion CLI                (write path)
rag.py          shared AI components and the chain
loaders.py      pdf / docx / xlsx / txt → Documents
config.py       .env loading and validation
db.py           plain SQL for health checks and stats
index.html      the browser UI

docker-compose.yml   PostgreSQL + pgAdmin (+ app, behind the "full" profile)
Dockerfile           multi-stage build of the app image
.dockerignore        keeps secrets and cruft out of the image
```

## Ports

DevBrain uses non-default ports so it coexists with other local Postgres
instances. All configurable in `.env`.

| Service | Port |
|---|---|
| Web UI / API | 8001 |
| PostgreSQL | 5433 |
| pgAdmin | 5051 |

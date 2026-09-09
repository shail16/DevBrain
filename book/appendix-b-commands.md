# Appendix B — Command Reference

Every command in the book, in one place.

All Python commands assume `(.venv)` is active and you are in the project folder.

---

## B.1 Environment

```powershell
python -m venv .venv                 # create
.\.venv\Scripts\Activate.ps1         # activate (Windows)
source .venv/bin/activate            # activate (macOS/Linux)
pip install -r requirements.txt      # install
pip check                            # verify nothing conflicts
deactivate                           # leave
```

**PowerShell blocks the activate script?**

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

**Confirm the venv is active:**

```bash
python -c "import sys; print(sys.prefix)"   # must end in .venv
```

---

## B.2 Configuration

```powershell
.\set-env.ps1                # prompt for keys, save as user variables
.\set-env.ps1 -Show          # which are set (values never displayed)
.\set-env.ps1 -Clear         # remove them all
```

**This session only:**

```powershell
$env:OPENAI_API_KEY = "sk-..."          # PowerShell
export OPENAI_API_KEY=sk-...            # macOS/Linux
```

**Or a file:**

```powershell
copy .env.example .env       # Windows
cp .env.example .env         # macOS/Linux
```

**Check what loaded, and from where:**

```bash
python -c "from config import load_settings; print(load_settings().safe_database_url)"
python -c "from config import config_sources; import json; print(json.dumps(config_sources(), indent=2))"
```

**Escape hatches:**

```bash
DEVBRAIN_ENV_FILE=/path/to/other.env python app.py
DEVBRAIN_IGNORE_ENV_FILE=1 python app.py       # environment only
```

---

## B.3 Docker

### Development — database only

```bash
docker compose up -d                 # start Postgres + pgAdmin
docker compose ps                    # status and health
docker compose logs -f postgres      # follow the log
docker compose stop                  # pause (keeps data)
docker compose down                  # remove containers (keeps data)
docker compose down -v               # remove containers AND DELETE ALL DATA
```

### Full stack — including the app

```bash
docker compose --profile full up -d --build     # build and start everything
docker compose --profile full ps                # status
docker compose --profile full logs -f app       # follow app logs
docker compose --profile full restart app       # restart just the app
docker compose --profile full up -d --build app # rebuild after a code change
docker compose --profile full down              # stop everything
```

### Running commands inside the container

```bash
docker compose --profile full run --rm app python ingest.py
docker compose --profile full run --rm app python ingest.py --reset
docker compose --profile full run --rm app python ingest.py --stats
docker compose --profile full exec app sh       # shell inside the running app
```

### Diagnostics

```bash
# Is pgvector available?
docker exec devbrain-postgres psql -U devbrain -d devbrain \
  -c "SELECT name, default_version FROM pg_available_extensions WHERE name='vector'"

# Is the data mount writable? (uploads need this)
docker compose --profile full exec -T app sh -c \
  "touch /app/data/.wtest && echo 'WRITE OK' && rm /app/data/.wtest"

# Confirm no secrets in the image
docker run --rm devbrain-app:latest ls -la /app

# Container health
docker inspect --format "{{.State.Health.Status}}" devbrain-app
```

---

## B.4 Ingestion

```bash
python ingest.py                     # add or update (safe to re-run)
python ingest.py --reset             # wipe the collection and rebuild
python ingest.py --dry-run           # preview chunks; no API calls, no writes
python ingest.py --stats             # what is currently stored
python ingest.py --data-dir ./notes  # a different folder
python ingest.py --help              # full flag list
```

**Which one do I need?**

| What changed | Command |
|---|---|
| Added new files | `python ingest.py` |
| **Edited or deleted** files | `python ingest.py --reset` |
| Changed `CHUNK_SIZE` / `CHUNK_OVERLAP` | `python ingest.py --reset` |
| Changed `COHERE_EMBED_MODEL` | `python ingest.py --reset` |
| Tuning chunking, want to preview | `python ingest.py --dry-run` |

---

## B.5 The web server

```bash
uvicorn app:app --reload --port 8001     # development (recommended)
python app.py                            # equivalent
uvicorn app:app --host 0.0.0.0 --port 8001 --workers 2   # production style
```

`Ctrl+C` to stop.

**Stop a stray background server:**

```powershell
Get-Process python | Where-Object { $_.Path -like "*DevBrain*" } | Stop-Process -Force
```

```bash
pkill -f "uvicorn app:app"
```

---

## B.6 The API

```bash
# Health (no auth)
curl http://localhost:8001/api/health

# Sign in, saving the session cookie
curl -c cookies.txt -X POST http://localhost:8001/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"Shailendra.KS","password":"..."}'

# Ask
curl -b cookies.txt -X POST http://localhost:8001/api/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"How do I configure connection pooling?"}'

# Upload (purpose is required)
curl -b cookies.txt -X POST http://localhost:8001/api/upload \
  -F "files=@notes.md" \
  -F "purpose=Team onboarding reference"

# My documents
curl -b cookies.txt http://localhost:8001/api/my/documents

# Admin
curl -b cookies.txt http://localhost:8001/api/admin/users
curl -b cookies.txt http://localhost:8001/api/admin/documents
curl -b cookies.txt -X DELETE http://localhost:8001/api/admin/documents/notes.md

# Sign out
curl -b cookies.txt -X POST http://localhost:8001/api/auth/logout
```

Interactive docs: <http://localhost:8001/docs>

---

## B.7 SQL

Run in pgAdmin (<http://localhost:5051>) or via `docker exec`.

### Inspecting the knowledge base

```sql
-- Chunks per document
SELECT cmetadata ->> 'source' AS document, count(*) AS chunks
FROM langchain_pg_embedding
GROUP BY 1 ORDER BY 2 DESC;

-- A real chunk and its vector
SELECT cmetadata ->> 'source'    AS document,
       left(document, 200)       AS text_preview,
       left(embedding::text, 80) AS vector_preview
FROM langchain_pg_embedding LIMIT 3;

-- Confirm extension and dimensions
SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';
SELECT vector_dims(embedding) FROM langchain_pg_embedding LIMIT 1;   -- expect 1024

-- Per-collection totals (catches Bug 1 from Chapter 20)
SELECT c.name, count(e.id) AS chunks
FROM langchain_pg_collection c
LEFT JOIN langchain_pg_embedding e ON e.collection_id = c.uuid
GROUP BY c.name ORDER BY c.name;
```

### Users and documents

```sql
SELECT id, username, is_admin, is_active, last_login_at FROM devbrain_users;

SELECT source, file_type, purpose, uploaded_by, uploaded_at
FROM devbrain_documents ORDER BY uploaded_at DESC;

SELECT u.username, count(s.token_hash) AS active_sessions
FROM devbrain_users u
LEFT JOIN devbrain_sessions s ON s.user_id = u.id AND s.expires_at > now()
GROUP BY u.username;
```

### Maintenance

```sql
-- Remove one document's chunks
DELETE FROM langchain_pg_embedding e
USING langchain_pg_collection c
WHERE c.uuid = e.collection_id
  AND c.name = 'devbrain_docs'
  AND e.cmetadata ->> 'source' = 'old-notes.md';

-- Change the database password
ALTER USER devbrain WITH PASSWORD 'new-password';

-- Add a vector index (only past ~100k chunks)
CREATE INDEX ON langchain_pg_embedding USING hnsw (embedding vector_cosine_ops);
```

---

## B.8 Backup and restore

```bash
# Backup
docker exec devbrain-postgres pg_dump -U devbrain devbrain | gzip > backup-$(date +%F).sql.gz

# Restore
gunzip -c backup-2026-08-28.sql.gz | docker exec -i devbrain-postgres psql -U devbrain -d devbrain
```

Also back up `data/` (your documents) and `.env` (your secrets). Those are the two
things that are **not** reproducible.

---

## B.9 Troubleshooting one-liners

```bash
# What is holding a port? (Windows)
Get-NetTCPConnection -LocalPort 5433 -State Listen |
  ForEach-Object { Get-Process -Id $_.OwningProcess }

# What is holding a port? (macOS/Linux)
lsof -i :5433

# Is the venv active?
python -c "import sys; print(sys.prefix)"

# Do the key imports resolve?
python -c "import fastapi, langchain_core, langchain_postgres, bcrypt, pypdf; print('ok')"

# Is Postgres reachable from Python?
python -c "from config import load_settings; import db; print(db.ping(load_settings()))"

# How many chunks are stored?
python -c "from config import load_settings; import db; print(db.count_chunks(load_settings()))"

# Are traces reaching LangSmith?
python -c "
from langsmith import Client
from config import load_settings
s = load_settings()
c = Client(api_key=s.langsmith_api_key, api_url=s.langsmith_endpoint)
print('runs:', len(list(c.list_runs(project_name=s.langsmith_project, limit=5))))"

# Retrieval quality check (no OpenAI cost)
python -c "
from config import load_settings
from rag import build_vector_store
vs = build_vector_store(load_settings())
for q in ['your question here', 'What is the capital of France?']:
    print(f'\n{q}')
    for d, s in vs.similarity_search_with_relevance_scores(q, k=3):
        print(f'  {s:.3f}  {d.metadata[\"source\"]}')"
```

---

## B.10 Full reset

When you want to start completely clean:

```bash
docker compose down -v           # deletes ALL stored vectors and users
docker compose up -d
python ingest.py --reset
```

Your documents in `data/` and your `.env` are untouched. Everything else is rebuilt.

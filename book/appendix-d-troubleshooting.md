# Appendix D — Troubleshooting Index

Find your symptom. Each entry gives the cause, the fix, and the chapter that
explains why.

---

## D.1 Setup and environment

### `ModuleNotFoundError: No module named 'fastapi'`

The virtual environment is not active. Look for `(.venv)` in your prompt.

```powershell
.\.venv\Scripts\Activate.ps1
```

Confirm: `python -c "import sys; print(sys.prefix)"` must end in `.venv`. *(Ch 6)*

### `running scripts is disabled on this system`

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```
*(Ch 6)*

### `pg_config executable not found` when installing psycopg

You installed `psycopg` instead of `psycopg[binary]`. The binary extra ships a
pre-compiled wheel so no compiler is needed. *(Ch 6)*

### Imports that work in tutorials fail here

LangChain 1.x reorganised significantly — many tutorials use `langchain.chains`
helpers that have moved or gone. This project builds from `langchain_core`
primitives deliberately. Check the tutorial's version before assuming you erred.
*(Ch 6, 11)*

---

## D.2 Docker

### `port is already allocated`

Something else holds that port. Change **only the host side** and update
`DATABASE_URL` to match:

```ini
POSTGRES_HOST_PORT=5434
DATABASE_URL=postgresql+psycopg://devbrain:devbrain_pass@localhost:5434/devbrain
```

Find the culprit:

```powershell
Get-NetTCPConnection -LocalPort 5433 -State Listen |
  ForEach-Object { Get-Process -Id $_.OwningProcess }
```
*(Ch 7)*

### `cannot find the file specified` / `daemon is not running`

Docker Desktop is installed but not running. Launch it and wait for the whale icon
to stop animating. *(Ch 6)*

### `extension "vector" is not available`

You are using `postgres:17` instead of `pgvector/pgvector:pg17`. The failure arrives
much later than the cause.

```bash
docker compose down -v
# fix the image line in docker-compose.yml
docker compose up -d
```
*(Ch 4, 7)*

### The app container starts but refuses every connection

`uvicorn` is bound to `127.0.0.1` inside the container. A container binding loopback
is unreachable from outside itself. Use `--host 0.0.0.0`. *(Ch 18)*

### `connection refused` from inside a container

`localhost` inside a container means *that container*. Use the service name and the
**internal** port: `postgres:5432`, not `localhost:5433`. *(Ch 18)*

### Uploads fail in the container but work locally

`data/` is mounted read-only. Change `./data:/app/data:ro` to `./data:/app/data`.

Verify:

```bash
docker compose --profile full exec -T app sh -c \
  "touch /app/data/.wtest && echo 'WRITE OK' && rm /app/data/.wtest"
```
*(Ch 16, 18)*

### Container logs appear late or not at all

Missing `PYTHONUNBUFFERED=1` in the Dockerfile. *(Ch 18)*

---

## D.3 Configuration

### `OPENAI_API_KEY is missing`

Not set anywhere, or still a placeholder. Check what the app actually read:

```bash
python -c "from config import config_sources; import json; print(json.dumps(config_sources(), indent=2))"
```
*(Ch 8)*

### `.env` exists but is ignored

Three usual causes:

1. Named `.env.txt` — Windows Explorer does this. Check the VS Code tab.
2. Values wrapped in quotes, or spaces around `=`
3. `DEVBRAIN_IGNORE_ENV_FILE=1` is set

*(Ch 8)*

### A stale value keeps winning

A real environment variable overrides `.env` **by design**. The startup banner shows
which source won:

```
OPENAI_API_KEY     <- environment
```

Clear it, or update it rather than the file. *(Ch 8)*

### The app refuses to start in Docker despite correct env vars

You have an older `config.py` whose validation checked for the *existence of a
`.env` file* rather than for missing values. See Chapter 20 Bug 4. *(Ch 18, 20)*

---

## D.4 Ingestion

### `Cohere rejected your API key`

Wrong or revoked. Get a fresh one at <https://dashboard.cohere.com/api-keys>. Make
sure you copied the API key, not an account ID. *(Ch 10)*

### A file is reported `empty`

No extractable text — almost always a scanned/image-only PDF. There is no text layer
to embed. Needs OCR. *(Ch 9, 21)*

### `Vector size mismatch` / `expected 1024 dimensions`

You changed `COHERE_EMBED_MODEL` after ingesting.

```bash
python ingest.py --reset
```
*(Ch 3, 10)*

### Running `ingest.py` twice doubles the chunk count

Chunk IDs are not deterministic. Verify `chunk_id()` hashes
`(collection, source, index, text)`. *(Ch 10)*

### A second collection ends up empty despite "18/18 chunks stored"

Chapter 20's Bug 1. `chunk_id()` is missing the collection name, so the upsert is
updating rows owned by the first collection. *(Ch 20)*

### Deleted text still appears in answers

`ingest.py` cannot know about content you removed. Use `--reset` after editing or
deleting files. *(Ch 10)*

### A file never appears in results

Check the skipped list in the ingestion output. Unsupported extension, a `~$` Office
lock file, or inside an ignored directory. *(Ch 9)*

---

## D.5 The application

### Status bar red: "Backend unreachable"

The web server is not running:

```bash
uvicorn app:app --reload --port 8001
```
*(Ch 12, 13)*

### Status bar red: "Cannot reach PostgreSQL"

```bash
docker compose ps
docker compose up -d
docker compose logs postgres
```

No server restart needed — the pipeline is rebuilt on the next request. *(Ch 12)*

### Status bar amber: "Knowledge base is empty"

```bash
python ingest.py
```
*(Ch 12)*

### Every question returns 502 `must be called with async_mode`

The endpoint uses `async def` with `await chain.ainvoke()` against a synchronous
`PGVector`. Change it to plain `def` and `chain.invoke()`. *(Ch 12, 20)*

### `Your OpenAI account has no remaining quota`

Add billing credit. **ChatGPT Plus does not cover API usage** — they are separate
products. <https://platform.openai.com/settings/organization/billing> *(Ch 8, 12)*

### A response field is always `null`

It is not declared on the Pydantic response model. Pydantic silently drops
undeclared keys. *(Ch 12, 20)*

### `server closed the connection unexpectedly` after idle time

Missing `pool_pre_ping=True` and `pool_recycle` in `engine_args`. *(Ch 11)*

---

## D.6 Answer quality

Work through this in order. **Check the trace first** — it tells you which half
failed. *(Ch 17)*

### "I don't have that" but you know it is there

| Step | Action |
|---|---|
| 1 | `python ingest.py --stats` — is the document ingested at all? |
| 2 | Open the trace, expand `VectorStoreRetriever` — what came back? |
| 3 | Raise `RETRIEVER_K` to 6 |
| 4 | Reduce `CHUNK_SIZE` to 600, then `ingest.py --reset` |
| 5 | Check `RETRIEVER_SCORE_THRESHOLD` is not filtering too aggressively |

*(Ch 5, 11, 17)*

### Right chunks retrieved, weak answer

A **generation** problem. Read the `ChatPromptTemplate` span — if the prompt looks
correct, try `OPENAI_MODEL=gpt-4o`. *(Ch 17)*

### Irrelevant sources cited

Set `RETRIEVER_SCORE_THRESHOLD=0.3`. Measure your own corpus first — thresholds do
not transfer. *(Ch 3, 11)*

### Answers cut off mid-thought

Raise `CHUNK_OVERLAP` to 250 and re-ingest with `--reset`. *(Ch 5)*

### Code snippets mangled

A chunk boundary cut through a code block. Confirm `"\n```"` is in the splitter's
separator list. *(Ch 5)*

### Spreadsheet questions fail

Header repetition is not firing. Check that a chunk's text looks like
`Technology: Redis | Category: ...` rather than bare values. *(Ch 9)*

### Exact terms (error codes, versions) fail

A known embedding limitation. Needs hybrid search. *(Ch 3, 21)*

### The model answers from outside knowledge

Your prompt is too soft. Rules must be absolute, and the refusal wording prescribed
exactly. *(Ch 11)*

---

## D.7 Authentication

### Everything returns 401

Not signed in, or the cookie is not being sent. Browser fetches need
`credentials: "same-origin"`; `curl` needs `-b cookies.txt`. *(Ch 14, 16)*

### 403 on admin routes

The account is not an administrator. Only `ADMIN_USERNAME` seeded from configuration
is — self-registration always produces a standard user, by design. *(Ch 14)*

### The admin account was not created

`ADMIN_USERNAME` / `ADMIN_PASSWORD` were unset at first start. Set them and restart.
Note seeding only ever *creates*: if the account exists, changing the variable does
nothing. *(Ch 14)*

### Changing `ADMIN_PASSWORD` has no effect

By design — seeding never updates, so a password changed in the app is not reverted
on restart. Change it in the app, or delete the row and let it re-seed. *(Ch 14)*

### A deleted user still has access

Should be impossible — the session JOIN checks `u.is_active` and `ON DELETE CASCADE`
removes their sessions. If you see this, you are using JWTs somewhere. *(Ch 14)*

### `Cannot delete the only administrator`

Working as intended. Promote another admin first. *(Ch 14)*

---

## D.8 Uploads

### 422 "Please describe the purpose"

Purpose is required. Send a non-blank `purpose` form field. *(Ch 16)*

### Upload returns 400 or the file is skipped

Check the per-file `detail`. Common causes: unsupported extension, empty file, over
25 MB, or a filename that sanitised to nothing. *(Ch 16)*

### Multipart upload fails from JavaScript

You set `Content-Type` manually. Let the browser set it — it must generate the
boundary. *(Ch 16)*

### Drag-and-drop does nothing

Missing `preventDefault()` on the `dragover` handler. *(Ch 16)*

### Large uploads time out

Ingestion runs synchronously inside the request. Upload in smaller batches, or move
ingestion to a background job. *(Ch 16)*

---

## D.9 Observability

### No traces appear in LangSmith

**Check the log first.** A working setup logs *nothing* about LangSmith; a broken
one repeats:

```
WARNING langsmith.client  Failed to send compressed multipart ingest: ... 403 ...
```

**403 Forbidden is usually a region problem, not permissions.**

| You sign in at | Set |
|---|---|
| `smith.langchain.com` | `LANGSMITH_ENDPOINT=https://api.smith.langchain.com` |
| `eu.smith.langchain.com` | `LANGSMITH_ENDPOINT=https://eu.api.smith.langchain.com` |

Diagnostic: compare your real key against an obviously fake one. **Identical
responses mean the key is not being recognised at all** — which rules out quota.
*(Ch 8, 17, 20)*

### Startup says `LangSmith : disabled`

Key missing or still a placeholder. Tracing degrades gracefully rather than
crashing. *(Ch 8)*

### No trace ID in the response

No run was captured. Usually the same cause as above. *(Ch 12, 17)*

---

## D.10 Nuclear options

**Rebuild the index, keep everything else:**

```bash
python ingest.py --reset
```

**Destroy all data — vectors, users, sessions — and rebuild:**

```bash
docker compose down -v
docker compose up -d
python ingest.py --reset
```

Your documents in `data/` and your `.env` are untouched.

**Rebuild the container from scratch:**

```bash
docker compose --profile full down
docker builder prune -f
docker compose --profile full up -d --build
```

---

## D.11 What to include in a bug report

If you ask someone for help, this is the useful set — and none of it exposes a
secret:

```bash
docker compose --profile full ps                    # container status
docker compose --profile full logs app | head -30   # startup banner
python -c "from config import config_sources; import json; print(json.dumps(config_sources(), indent=2))"
python ingest.py --stats                            # what is indexed
curl http://localhost:8001/api/health                # live state
```

The startup banner and `config_sources()` show provenance without values, and
`safe_database_url` masks the password — so this output is safe to paste anywhere.
*(Ch 8, 12)*

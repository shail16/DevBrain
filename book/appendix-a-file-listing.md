# Appendix A — Complete File Listing

A map of every file, what it does, and which chapter builds it.

---

## A.1 The project

```
DevBrain/
├── config.py              360   settings, validation, provenance      Ch 8
├── loaders.py             292   files → Documents                     Ch 9
├── ingest.py              583   the write path (CLI)                  Ch 10
├── rag.py                 298   prompt, chain, citations              Ch 11
├── db.py                  149   plain SQL helpers                     Ch 12
├── app.py               1,090   the web API                       Ch 12,15,16
├── auth.py                577   users, sessions, ownership            Ch 14
├── index.html           1,960   the entire frontend                   Ch 13
│
├── docker-compose.yml      158   Postgres, pgAdmin, app             Ch 7,18
├── Dockerfile               82   multi-stage production image          Ch 18
├── .dockerignore            46   keeps secrets out of the image        Ch 18
├── requirements.txt         30   pinned dependencies                   Ch 6
├── .gitignore               35   protects secrets                      Ch 6
├── .env.example            143   annotated configuration template      Ch 8
├── set-env.ps1             165   Windows secret management             Ch 8
│
├── pgadmin/servers.json     14   pre-registers the DB connection       Ch 7
├── data/                         your documents                        Ch 9
├── assets/                       the logo
└── book/                         this book
```

Roughly **5,700 lines**, of which you write about 5,400.

---

## A.2 Module dependency graph

```
                    config.py
                   (no imports)
                        │
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
    loaders.py       db.py           rag.py
   (no project              ┌──────────┘
    imports)                │
        │                   ▼
        │              auth.py
        │           (config, db)
        │                   │
        └────────┬──────────┘
                 ▼
            ingest.py                app.py
    (config, loaders, rag, db)   (config, db, auth, rag, loaders, ingest)
```

**`config.py` imports nothing from the project.** Everything else depends on it.
That is what makes it the single source of truth — a cycle would break the
guarantee.

`app.py` imports `ingest.ingest_files` *inside the function*, not at module level,
to keep startup fast and avoid a circular import at load time.

---

## A.3 Public API of each module

### `config.py`

| Name | Purpose |
|---|---|
| `Settings` | Frozen dataclass — all resolved configuration |
| `load_settings(exit_on_error=False)` | The entry point |
| `config_sources()` | `{setting: "environment" \| ".env" \| "default"}` |
| `value_source(name)` | Origin of one setting |
| `configure_langsmith(settings)` | Sets tracing environment variables |
| `ConfigError` | Raised for missing/invalid configuration |
| `PROJECT_ROOT`, `ENV_FILE`, `ENV_FILE_LOADED` | Paths and load state |
| `SECRET_NAMES`, `SETTING_NAMES` | For provenance reporting |

### `loaders.py`

| Name | Purpose |
|---|---|
| `load_document(path, source_label=None)` | Dispatch — file → `list[Document]` |
| `discover_files(data_dir)` | Returns `(supported, skipped)` |
| `SUPPORTED_EXTENSIONS` | The allow-list |
| `UnsupportedFileType` | Raised for unknown extensions |
| `load_text_file` / `load_pdf_file` / `load_docx_file` / `load_xlsx_file` | Per-format loaders |

### `rag.py`

| Name | Purpose |
|---|---|
| `build_embeddings(settings)` | Cohere client — **the shared one** |
| `build_vector_store(settings, embeddings=None, pre_delete_collection=False)` | PGVector handle |
| `build_llm(settings)` | OpenAI client |
| `build_retriever(settings, vector_store)` | `question → list[Document]` |
| `build_rag_chain(settings, retriever, llm)` | The LCEL chain |
| `format_documents(docs)` | Chunks → prompt context |
| `documents_to_citations(docs)` | Chunks → JSON for the UI |
| `COHERE_MAX_BATCH_SIZE` | 96 |
| `SYSTEM_PROMPT`, `ANSWER_PROMPT` | The grounding rules |

### `db.py`

| Name | Purpose |
|---|---|
| `get_engine(settings)` | Cached SQLAlchemy engine |
| `ping(settings)` | Is Postgres reachable? |
| `pgvector_installed(settings)` | Is the extension present? |
| `collection_exists(settings)` | Has ingestion ever run? |
| `count_chunks(settings)` | Total chunks |
| `count_chunks_by_source(settings)` | `[(source, count), ...]` |
| `delete_chunks_for_source(settings, source)` | Remove one document's chunks |

### `auth.py`

| Name | Purpose |
|---|---|
| `init_schema(settings)` | Creates the three tables |
| `hash_password` / `verify_password` | bcrypt with SHA-256 pre-hash |
| `create_user(...)` / `get_user_by_username` / `list_users` / `delete_user` | User CRUD |
| `ensure_admin(settings)` | Seeds the administrator |
| `login(settings, username, password)` | Returns `(token, user)` |
| `user_for_token(settings, token)` | Session → user, or `None` |
| `logout(settings, token)` | Revoke one session |
| `record_document(...)` / `list_documents` / `list_documents_for_user` / `forget_document` | Document registry |
| `User`, `AuthError`, `SESSION_COOKIE`, `SESSION_TTL_HOURS` | Types and constants |

### `ingest.py`

| Name | Purpose |
|---|---|
| `main()` | CLI entry point |
| `ingest_files(settings, paths, extra_metadata=None)` | **Shared with the upload endpoint** |
| `split_documents(settings, documents)` | Chunking |
| `chunk_id(chunk, collection_name)` | Deterministic ID |
| `store_chunks(settings, chunks, reset)` | Embed and store in batches |
| `preflight_check_embeddings(embeddings)` | Validate credentials early |
| `explain_ingestion_failure(exc)` | Error → actionable advice |
| `report_stats(settings)` | `--stats` output |

---

## A.4 HTTP endpoints

| Method | Path | Auth | Chapter |
|---|---|---|---|
| `GET` | `/` | — | 12 |
| `GET` | `/api/health` | — | 12 |
| `GET` | `/api/config` | — | 12 |
| `POST` | `/api/auth/register` | — | 14 |
| `POST` | `/api/auth/login` | — | 14 |
| `POST` | `/api/auth/logout` | — | 14 |
| `GET` | `/api/auth/me` | — | 14 |
| `POST` | `/api/ask` | **user** | 12 |
| `POST` | `/api/upload` | **user** | 16 |
| `GET` | `/api/my/documents` | **user** | 15 |
| `GET` | `/api/admin/users` | **admin** | 15 |
| `DELETE` | `/api/admin/users/{user_id}` | **admin** | 15 |
| `GET` | `/api/admin/documents` | **admin** | 15 |
| `DELETE` | `/api/admin/documents/{source:path}` | **admin** | 15 |
| `GET` | `/docs` | — | auto-generated |

---

## A.5 Database schema

### Created by `langchain-postgres`

**`langchain_pg_collection`**

| Column | Type | Notes |
|---|---|---|
| `uuid` | UUID | PK |
| `name` | varchar | Your `COLLECTION_NAME` |
| `cmetadata` | JSON | Optional |

**`langchain_pg_embedding`**

| Column | Type | Notes |
|---|---|---|
| `id` | **varchar** | PK — **global**, not per-collection (Ch 20 Bug 1) |
| `collection_id` | UUID | FK, `ON DELETE CASCADE` |
| `document` | text | Chunk text |
| `cmetadata` | **JSONB** | GIN-indexed |
| `embedding` | `vector(1024)` | The vector |

### Created by `auth.py`

**`devbrain_users`**

| Column | Type | Notes |
|---|---|---|
| `id` | serial | PK |
| `username` | varchar(40) | UNIQUE + unique index on `lower(username)` |
| `password_hash` | varchar(120) | bcrypt |
| `is_admin`, `is_active` | boolean | |
| `created_at`, `last_login_at` | timestamptz | |

**`devbrain_sessions`**

| Column | Type | Notes |
|---|---|---|
| `token_hash` | varchar(64) | PK — SHA-256 of the token |
| `user_id` | integer | FK, **`ON DELETE CASCADE`** |
| `expires_at` | timestamptz | |

**`devbrain_documents`**

| Column | Type | Notes |
|---|---|---|
| `source` | varchar(300) | **UNIQUE** — matches chunk metadata |
| `purpose` | text | Required at upload |
| `uploaded_by` | varchar(40) | Survives user deletion |
| `uploaded_by_id` | integer | FK, `ON DELETE SET NULL` |
| `uploaded_at` | timestamptz | |

---

## A.6 Every configuration variable

| Variable | Default | Chapter |
|---|---|---|
| `OPENAI_API_KEY` | *(required)* | 8 |
| `OPENAI_MODEL` | `gpt-4o-mini` | 8 |
| `OPENAI_TEMPERATURE` | `0.0` | 11 |
| `COHERE_API_KEY` | *(required)* | 8 |
| `COHERE_EMBED_MODEL` | `embed-english-v3.0` | 3 |
| `LANGSMITH_TRACING` | `false` | 17 |
| `LANGSMITH_API_KEY` | — | 17 |
| `LANGSMITH_PROJECT` | `devbrain` | 17 |
| `LANGSMITH_ENDPOINT` | US endpoint | 17 |
| `DATABASE_URL` | `localhost:5433` | 7 |
| `COLLECTION_NAME` | `devbrain_docs` | 4 |
| `DATA_DIR` | `data` | 9 |
| `CHUNK_SIZE` | `1000` | 5 |
| `CHUNK_OVERLAP` | `150` | 5 |
| `RETRIEVER_K` | `4` | 11 |
| `RETRIEVER_SCORE_THRESHOLD` | `0.0` | 11 |
| `API_PORT` | `8001` | 12 |
| `API_HOST_PORT` | `8001` | 18 |
| `POSTGRES_HOST_PORT` | `5433` | 7 |
| `PGADMIN_HOST_PORT` | `5051` | 7 |
| `ADMIN_USERNAME` | — | 14 |
| `ADMIN_PASSWORD` | — | 14 |
| `ADMIN_FULL_NAME` | — | 14 |
| `ADMIN_EMAIL` | — | 14 |
| `DEVBRAIN_ENV_FILE` | `./.env` | 8 |
| `DEVBRAIN_IGNORE_ENV_FILE` | `false` | 8 |

---

## A.7 Build order

If rebuilding from scratch, this order keeps you runnable at every step:

| Step | Files | Verify |
|---|---|---|
| 1 | `requirements.txt`, `.gitignore` | `pip check` |
| 2 | `docker-compose.yml`, `pgadmin/servers.json` | `docker compose ps` → healthy |
| 3 | `config.py`, `.env.example`, `set-env.ps1` | `load_settings()` succeeds |
| 4 | `loaders.py` | `discover_files()` finds your docs |
| 5 | `db.py` | `ping()` returns `True` |
| 6 | `rag.py` | Embeddings build |
| 7 | `ingest.py` | `ingest.py --stats` shows chunks |
| 8 | `app.py` (core) | `/api/health` → `ok` |
| 9 | `index.html` | Ask a question in the browser |
| 10 | `auth.py` + auth routes | Sign in works |
| 11 | Admin routes + upload | Upload and admin console |
| 12 | `Dockerfile`, `.dockerignore` | Container reaches healthy |

**Do not skip the verification column.** Each step is small enough that a failure
points at one file.

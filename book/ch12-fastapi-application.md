# Chapter 12 — The FastAPI Application

*© 2026 Shailendra Srivastava. All rights reserved.*

---

## 12.1 What the web layer adds

`rag.py` can answer questions. `app.py` makes that usable: it serves the UI,
validates input, reports health, translates failures into advice, and manages
expensive resources.

At 1,090 lines it is the largest Python file in the project — and only a small
fraction is the RAG call. That ratio is the honest shape of an AI application.

This chapter covers the core. Chapters 14–16 add authentication, admin routes and
uploads to the same file.

---

## 12.2 The endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Serve `index.html` |
| `GET` | `/api/health` | Is Postgres up? pgvector installed? how many chunks? |
| `GET` | `/api/config` | Non-secret settings + config provenance |
| `POST` | `/api/ask` | The RAG call |

FastAPI also generates `/docs` — interactive API documentation — for free, from
the Pydantic models below.

---

## 12.3 Request and response schemas

```python
class AskRequest(BaseModel):
    question: str = Field(
        ...,
        min_length=3,
        max_length=2000,
        description="The user's natural-language question.",
        examples=["How do I configure connection pooling in SQLAlchemy?"],
    )
    top_k: int | None = Field(
        default=None, ge=1, le=20,
        description="How many chunks to retrieve. Defaults to RETRIEVER_K.",
    )


class Citation(BaseModel):
    source: str
    file_type: str = ""
    locations: list[str] = Field(default_factory=list)
    snippet: str = ""
    uploaded_by: str = ""
    uploaded_at: str = ""


class AskResponse(BaseModel):
    question: str
    answer: str
    sources: list[Citation]
    chunks_retrieved: int
    latency_ms: int
    langsmith_run_id: str | None = None
    grounded: bool
```

### Why declare these at all

Pydantic validates incoming JSON **before your code runs**. A question that is too
short, too long, or the wrong type is rejected with a 422 and a precise error
message — you never write that check.

Verified behaviour:

| Input | Response |
|---|---|
| `{"question": "hi"}` | 422 — below `min_length` |
| `{}` | 422 — required field missing |
| 2001-character question | 422 — above `max_length` |
| `{"top_k": 99}` | 422 — above `le=20` |

**Length limits are a security control, not just tidiness.** Without `max_length`,
someone could post a megabyte of text that you would forward to OpenAI and pay for.

### A bug worth knowing about

`Citation` originally lacked `uploaded_by` and `uploaded_at`. When Chapter 15 added
attribution, the values flowed correctly through retrieval — and then vanished.

**Pydantic silently drops keys not declared on the model.** `Citation(**c)` with
extra keys discards them without error. The API returned `uploaded_by: None` while
the data was right there.

A test caught it. The lesson: *a response model is an allow-list.* If a field is
not declared, it does not exist to the client.

---

## 12.4 Caching expensive objects

```python
class Pipeline:
    """Holds the assembled RAG chain and everything it depends on."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.vector_store = build_vector_store(settings)
        self.llm = build_llm(settings)
        self.retriever = build_retriever(settings, self.vector_store)
        self.chain: Runnable = build_rag_chain(settings, self.retriever, self.llm)
```

Building this per request would add hundreds of milliseconds *and* open a new
database connection pool every time — exhausting Postgres within minutes.

So it is built once and cached.

### Per-request variation without shared mutation

```python
def chain_for(self, top_k: int | None) -> Runnable:
    if top_k is None or top_k == self.settings.retriever_k:
        return self.chain

    per_request_settings = replace(self.settings, retriever_k=top_k)
    retriever = build_retriever(per_request_settings, self.vector_store)
    return build_rag_chain(per_request_settings, retriever, self.llm)
```

When a caller overrides `top_k`, we build a throwaway retriever and chain rather
than mutating the shared one. Two concurrent requests with different values cannot
interfere.

The expensive parts — embedding client, connection pool, LLM client — are still
reused. Only the thin wiring is recreated. This is why `Settings` is a frozen
dataclass: `replace()` gives a copy, and mutation is impossible by construction.

---

## 12.5 Lazy construction and graceful degradation

```python
def get_pipeline(app: FastAPI) -> Pipeline:
    pipeline: Pipeline | None = getattr(app.state, "pipeline", None)
    if pipeline is not None:
        return pipeline

    settings: Settings | None = getattr(app.state, "settings", None)
    if settings is None:
        raise HTTPException(status_code=503, detail=app.state.config_error)

    try:
        pipeline = Pipeline(settings)
    except Exception as exc:
        logger.error("Could not initialise the RAG pipeline: %s", exc)
        raise HTTPException(
            status_code=503,
            detail=(
                f"Could not connect to the vector store ({type(exc).__name__}). "
                "Are the Docker containers running? Try: docker compose up -d"
            ),
        ) from exc

    app.state.pipeline = pipeline
    return pipeline
```

Two decisions here, both about being kind to a confused user.

**Built lazily, not at startup.** If Postgres is not running yet, the server still
starts.

**The failure is not cached.** Note `app.state.pipeline` is only set on *success*.
So the next request retries — start your containers and hit refresh, no restart
needed.

### The lifespan handler

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pipeline = None
    app.state.settings = None
    app.state.config_error = None

    try:
        settings = load_settings()
        app.state.settings = settings
        for message in settings.warnings:
            logger.warning(message)
        # ... log config provenance and model names ...
    except ConfigError as exc:
        app.state.config_error = str(exc)
        logger.error("Configuration error: %s", exc)

    logger.info("Open the UI at http://localhost:%s", os.getenv("API_PORT", "8001"))
    yield
    logger.info("DevBrain shutting down")
```

**A configuration error is captured, not raised.** The server boots and *explains*
the problem through `/api/health` and the UI banner.

Chapter 8 mentioned this: `load_settings()` takes an `exit_on_error` flag. The CLI
passes `True` and dies with a friendly message; the web app passes `False` and
handles it. Same function, two failure styles, chosen by the caller.

> A process that dies instantly with a traceback is much harder for a newcomer to
> debug than a page that says *"Cannot reach PostgreSQL — try `docker compose up -d`."*

---

## 12.6 The health endpoint

```python
@app.get("/api/health")
def health() -> JSONResponse:
    settings = getattr(app.state, "settings", None)

    if settings is None:
        return JSONResponse(status_code=503, content={
            "status": "misconfigured",
            "detail": app.state.config_error,
            "database": "unknown", "chunks": 0,
        })

    payload = {
        "status": "ok", "detail": "", "database": "connected",
        "pgvector": True, "chunks": 0,
        "collection": settings.collection_name,
        "chat_model": settings.openai_model,
        "embedding_model": settings.cohere_embed_model,
        "langsmith_tracing": settings.langsmith_tracing,
    }

    try:
        db.ping(settings)
    except Exception as exc:
        payload.update(
            status="database_unreachable", database="unreachable", pgvector=False,
            detail=(f"Cannot reach Postgres at {settings.safe_database_url} "
                    f"({type(exc).__name__}). Start it with: docker compose up -d"))
        return JSONResponse(status_code=503, content=payload)

    payload["pgvector"] = db.pgvector_installed(settings)
    payload["chunks"] = db.count_chunks(settings)
    payload["sources"] = [...]

    if not payload["pgvector"]:
        payload.update(status="degraded", detail="The `vector` extension is not installed...")
    elif payload["chunks"] == 0:
        payload.update(status="empty",
                       detail="The knowledge base is empty. Put documents in /data and run: python ingest.py")

    return JSONResponse(status_code=200, content=payload)
```

### Five distinct states

| Status | Meaning | Fix |
|---|---|---|
| `ok` | Everything working | — |
| `misconfigured` | Missing API keys | Set environment variables |
| `database_unreachable` | Postgres down | `docker compose up -d` |
| `degraded` | pgvector missing | Wrong Docker image |
| `empty` | No documents ingested | `python ingest.py` |

**Each carries the command that fixes it.** The UI turns these into a coloured
status bar, so a misconfiguration is visible on page load rather than surfacing as
a failed question later.

This is a small endpoint that saves a disproportionate amount of confusion. Health
checks that only return `{"status": "ok"}` tell you nothing when things break.

---

## 12.7 The ask endpoint

```python
@app.post("/api/ask", response_model=AskResponse)
def ask(request: AskRequest, user: auth.User = Depends(current_user)) -> AskResponse:
    pipeline = get_pipeline(app)
    settings = pipeline.settings
    question = request.question.strip()

    # ---- 1. empty knowledge base -----------------------------------------
    try:
        if db.count_chunks(settings) == 0:
            return AskResponse(
                question=question,
                answer=("The knowledge base is empty, so I have nothing to search.\n\n"
                        "Add documents to the `data` folder, then run:\n\n"
                        "```\npython ingest.py\n```"),
                sources=[], chunks_retrieved=0, latency_ms=0, grounded=False,
            )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=(
            f"Cannot reach the database ({type(exc).__name__}). "
            "Start it with: docker compose up -d")) from exc

    # ---- 2. run the chain ------------------------------------------------
    started = time.perf_counter()
    run_id: str | None = None

    try:
        from langchain_core.tracers.context import collect_runs

        chain = pipeline.chain_for(request.top_k)

        with collect_runs() as collector:
            result = chain.invoke(question)
            if collector.traced_runs:
                run_id = str(collector.traced_runs[0].id)

    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.exception("Question failed after %d ms: %s", latency_ms, question)
        raise HTTPException(status_code=502, detail=_explain_failure(exc)) from exc

    latency_ms = int((time.perf_counter() - started) * 1000)
    documents = result.get("docs") or []

    return AskResponse(
        question=question,
        answer=result.get("answer", "").strip(),
        sources=[Citation(**c) for c in documents_to_citations(documents)],
        chunks_retrieved=len(documents),
        latency_ms=latency_ms,
        langsmith_run_id=run_id,
        grounded=bool(documents),
    )
```

### Short-circuiting an empty knowledge base

If nothing is ingested, we return immediately — no embedding call, no OpenAI call.
Cheaper, faster, and the message tells the user exactly what to do.

### Capturing the trace ID

```python
with collect_runs() as collector:
    result = chain.invoke(question)
    if collector.traced_runs:
        run_id = str(collector.traced_runs[0].id)
```

`collect_runs` captures the LangSmith run ID so it can be returned to the browser.
Every answer in the UI shows a short trace ID, which means **any answer can be
traced back to the exact prompt and chunks that produced it.** Chapter 17 uses this.

### Why `def` and not `async def`

This looks like a mistake and is not. It fixes a real crash.

`PGVector` is created in **synchronous** mode — which is what `ingest.py` needs, and
what keeps one shared configuration between the two entry points. A synchronous
vector store has no async engine, so calling `chain.ainvoke()` on it raises:

```
AssertionError: This method must be called with async_mode
```

**Every single question would have failed with a 502.** A test caught it.

The fix is to declare the handler as plain `def`. FastAPI automatically runs
synchronous handlers in a worker thread, so blocking database and OpenAI calls
never stall the event loop, and requests are still served concurrently. Keeping the
whole call on one thread also makes the `collect_runs` context variable behave
predictably.

> **In FastAPI, `async def` is not "the fast option".** It is a promise that
> everything inside is non-blocking. When your libraries are synchronous, plain
> `def` is both correct *and* faster.

Chapter 20 revisits this with the full diagnosis.

---

## 12.8 Translating upstream failures

```python
def _explain_failure(exc: Exception) -> str:
    message = str(exc)
    low = message.lower()

    if "insufficient_quota" in low or "exceeded your current quota" in low:
        return ("Your OpenAI account has no remaining quota. Add credit at "
                "https://platform.openai.com/settings/organization/billing")
    if "invalid_api_key" in low or "incorrect api key" in low:
        return "OpenAI rejected the API key. Check OPENAI_API_KEY in your .env file."
    if "model_not_found" in low:
        return ("Your OpenAI account cannot access the configured model. "
                "Try setting OPENAI_MODEL=gpt-4o-mini in your .env file.")
    if "rate limit" in low or "429" in low:
        return "Rate limited by the API provider. Wait a few seconds and try again."
    if "expected" in low and "dimensions" in low:
        return ("Vector size mismatch: the stored embeddings were created with a different "
                "Cohere model than the one now configured. Re-ingest with: "
                "python ingest.py --reset")
    if "connection" in low or "refused" in low:
        return "Lost the connection to Postgres. Check the container with: docker compose ps"
    return f"{type(exc).__name__}: {message}"
```

A raw SDK exception tells a user nothing actionable. This maps the common ones to
the specific fix.

The quota case is worth calling out: a new OpenAI account with no credit fails
even though the key is valid — and *"ChatGPT Plus does not cover API usage"*
surprises almost everyone.

---

## 12.9 Serving the frontend, and CORS

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/", include_in_schema=False)
async def serve_ui() -> FileResponse:
    if not INDEX_HTML.exists():
        raise HTTPException(status_code=500, detail="index.html is missing.")
    return FileResponse(INDEX_HTML, media_type="text/html",
                        headers={"Cache-Control": "no-store"})
```

**`allow_origins=["*"]`** is permissive. It is acceptable *here* because DevBrain
binds to localhost and holds only your own documents — and it means opening
`index.html` directly from disk (`file://`, origin `null`) still works.

**This is a local-development choice.** Chapter 19 flags it on the hardening
checklist. On a server you would restrict it.

**`Cache-Control: no-store`** means editing `index.html` shows up on a plain
refresh instead of leaving you debugging a cached copy.

---

## 12.10 `db.py` — plain SQL on purpose

```python
@lru_cache(maxsize=4)
def _engine_for(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True, pool_recycle=1800,
                         pool_size=2, max_overflow=2)
```

**The `lru_cache` is not an optimisation — it prevents a leak.** Creating an engine
sets up a connection pool. Building a new one on every health check would leak
connections until Postgres refused new ones.

```python
_COUNT_SQL = """
SELECT count(*)
FROM langchain_pg_embedding e
JOIN langchain_pg_collection c ON c.uuid = e.collection_id
WHERE c.name = :name
"""
```

Health checks and stats use raw SQL rather than going through LangChain. That is
deliberate: it demonstrates the most reassuring fact about the project — **your
embeddings are ordinary rows you can query yourself.** Chapter 4 had you run these
in pgAdmin; this is the same thing in code.

Note `:name` is a bound parameter, never string interpolation. Even with
non-user-controlled values, parameterised queries should be reflexive.

---

## 12.11 Checkpoint

```bash
uvicorn app:app --reload --port 8001
```

Startup log:

```
INFO  devbrain  Config source   : 4 from environment, 17 from .env, 0 default
INFO  devbrain  Chat model      : gpt-4o-mini
INFO  devbrain  Embedding model : embed-english-v3.0
INFO  devbrain  Collection      : devbrain_docs
INFO  devbrain  Database        : postgresql+psycopg://devbrain:****@localhost:5433/devbrain
INFO  devbrain  LangSmith       : tracing to project 'devbrain'
INFO  devbrain  Open the UI at http://localhost:8001
```

**Health:**

```bash
curl http://localhost:8001/api/health
```

```json
{"status":"ok","database":"connected","pgvector":true,"chunks":19,
 "collection":"devbrain_docs","chat_model":"gpt-4o-mini",...}
```

**A real question:**

```bash
curl -X POST http://localhost:8001/api/ask \
  -H "Content-Type: application/json" \
  -d "{\"question\": \"What is the difference between INNER JOIN and LEFT JOIN?\"}"
```

Verified response (abridged):

```
- **INNER JOIN**: Returns records that have matching values in both tables.
  ```sql
  SELECT orders.order_id, customers.first_name
  FROM orders INNER JOIN customers ON orders.customer_id = customers.customer_id;
  ```
...The key difference is that an INNER JOIN only includes rows with matches in
both tables, while a LEFT JOIN includes all rows from the left table [General_SQL.md]

SOURCES : General_SQL.md
```

**Test the graceful degradation.** Stop Postgres and call `/api/health` again:

```bash
docker compose stop postgres
curl http://localhost:8001/api/health
```

The server is still running, and it tells you what is wrong:

```json
{"status":"database_unreachable",
 "detail":"Cannot reach Postgres at postgresql+psycopg://devbrain:****@localhost:5433/devbrain
           (TimeoutError). Start it with: docker compose up -d"}
```

That is the design working. Restart with `docker compose up -d` — no server restart
needed.

**Interactive docs:** open <http://localhost:8001/docs>.

---

## 12.12 Mistakes to avoid

**Using `async def` with synchronous libraries.** The exact bug in §12.7.

**Building the pipeline per request.** Hundreds of milliseconds and a leaked pool
each time.

**Caching a failed construction.** Then a transient outage requires a restart.

**Forgetting a field on a response model.** Pydantic drops it silently.

**Returning raw exceptions to users.** They are unactionable and can leak internals.

**Omitting `max_length` on user input.** A cost and abuse vector.

**Creating a SQLAlchemy engine per call.** Leaks connections until Postgres refuses.

---

## 12.13 What you have

A web API that serves the UI, validates input, reports five distinct health states
with fixes, translates upstream failures into advice, caches expensive objects, and
starts successfully even when its database is down.

Next: the browser side — no framework, no build step.

*Next: [Chapter 13 — The Frontend](ch13-frontend.md)*

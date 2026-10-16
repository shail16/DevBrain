"""
DevBrain — FastAPI web application.

This is the "read path" of the RAG system. Start it with:

    uvicorn app:app --reload --port 8001

Then open http://localhost:8001 in a browser.

Endpoints
---------
GET  /              serves index.html (the UI)
GET  /api/health    is Postgres up, is pgvector installed, how many chunks?
GET  /api/config    non-secret settings, so the UI can display what's in use
POST /api/ask       the actual RAG call: question in, grounded answer out
POST /api/upload    save uploaded documents to /data and ingest them at once

Design note: the expensive objects (embedding client, database pool, LLM client,
assembled chain) are built once and reused for every request. Rebuilding them
per request would add hundreds of milliseconds and open a new connection pool
each time. They are built lazily and cached, so the server still starts
successfully when Postgres isn't up yet and recovers on its own once it is —
which is much friendlier than crashing on boot.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from langchain_core.runnables import Runnable
from pydantic import BaseModel, Field

import auth
import db
from config import (
    ENV_FILE,
    ENV_FILE_LOADED,
    PROJECT_ROOT,
    SECRET_NAMES,
    ConfigError,
    Settings,
    config_sources,
    load_settings,
)
from loaders import SUPPORTED_EXTENSIONS
from rag import build_llm, build_rag_chain, build_retriever, build_vector_store, documents_to_citations

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("devbrain")

INDEX_HTML = PROJECT_ROOT / "index.html"


# --------------------------------------------------------------------------- #
#  Request / response schemas
#
#  Pydantic validates incoming JSON automatically. A question of the wrong type
#  or length is rejected with a 422 before any of our code runs — and FastAPI
#  publishes these shapes as OpenAPI docs at /docs for free.
# --------------------------------------------------------------------------- #


class AskRequest(BaseModel):
    question: str = Field(
        ...,
        min_length=3,
        max_length=2000,
        description="The user's natural-language question.",
        examples=["How do I configure connection pooling in SQLAlchemy?"],
    )
    top_k: int | None = Field(
        default=None,
        ge=1,
        le=20,
        description="How many chunks to retrieve. Defaults to RETRIEVER_K from .env.",
    )


class Citation(BaseModel):
    source: str
    file_type: str = ""
    locations: list[str] = Field(default_factory=list)
    snippet: str = ""
    # Provenance, carried through from ingestion. Pydantic drops keys that are
    # not declared here, so these must be listed for the UI to ever see them.
    uploaded_by: str = ""
    uploaded_at: str = ""


class AskResponse(BaseModel):
    question: str
    answer: str
    sources: list[Citation]
    chunks_retrieved: int
    latency_ms: int
    langsmith_run_id: str | None = None
    grounded: bool = Field(
        description="False when retrieval found nothing, meaning the answer is a refusal."
    )


# --------------------------------------------------------------------------- #
#  Lazily-built, cached pipeline
# --------------------------------------------------------------------------- #


class Pipeline:
    """Holds the assembled RAG chain and everything it depends on."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.vector_store = build_vector_store(settings)
        self.llm = build_llm(settings)
        self.retriever = build_retriever(settings, self.vector_store)
        self.chain: Runnable = build_rag_chain(settings, self.retriever, self.llm)

    def chain_for(self, top_k: int | None) -> Runnable:
        """
        Return a chain for this request.

        The common case reuses the cached chain. When a caller overrides `top_k`
        we build a throwaway retriever and chain instead of mutating the shared
        one, so concurrent requests with different values cannot interfere with
        each other. The expensive parts (embedding client, connection pool, LLM
        client) are still reused — only the thin wiring is recreated.
        """
        if top_k is None or top_k == self.settings.retriever_k:
            return self.chain

        per_request_settings = replace(self.settings, retriever_k=top_k)
        retriever = build_retriever(per_request_settings, self.vector_store)
        return build_rag_chain(per_request_settings, retriever, self.llm)


def get_pipeline(app: FastAPI) -> Pipeline:
    """
    Return the cached pipeline, building it on first use.

    Raises HTTPException(503) with an actionable message if construction fails
    (almost always: Postgres isn't running yet). The failure is not cached, so
    the very next request retries — start your containers and just hit refresh.
    """
    pipeline: Pipeline | None = getattr(app.state, "pipeline", None)
    if pipeline is not None:
        return pipeline

    settings: Settings | None = getattr(app.state, "settings", None)
    if settings is None:
        raise HTTPException(
            status_code=503,
            detail=getattr(app.state, "config_error", "DevBrain is not configured."),
        )

    try:
        pipeline = Pipeline(settings)
    except Exception as exc:  # noqa: BLE001 - converted into a helpful 503
        logger.error("Could not initialise the RAG pipeline: %s", exc)
        raise HTTPException(
            status_code=503,
            detail=(
                f"Could not connect to the vector store ({type(exc).__name__}). "
                "Are the Docker containers running? Try: docker compose up -d"
            ),
        ) from exc

    app.state.pipeline = pipeline
    logger.info("RAG pipeline ready (collection=%s)", settings.collection_name)
    return pipeline


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Runs once on startup and once on shutdown.

    Configuration errors are captured rather than raised so the server still
    boots and can *explain* the problem through /api/health and the UI banner.
    A process that dies instantly with a traceback is much harder for a
    newcomer to debug.
    """
    app.state.pipeline = None
    app.state.settings = None
    app.state.config_error = None

    try:
        settings = load_settings()
        app.state.settings = settings
        for message in settings.warnings:
            logger.warning(message)
        # Report WHERE each setting was resolved from. Names and origins only —
        # no value is ever logged, so this is safe to paste into a bug report.
        sources = config_sources()
        tally: dict[str, int] = {}
        for origin in sources.values():
            tally[origin] = tally.get(origin, 0) + 1
        logger.info(
            "Config source   : %d from environment, %d from %s, %d default",
            tally.get("environment", 0),
            tally.get(".env", 0),
            ENV_FILE.name if ENV_FILE_LOADED else "(no file)",
            tally.get("default", 0),
        )
        for secret in SECRET_NAMES:
            logger.info("  %-18s <- %s", secret, sources.get(secret, "default"))

        logger.info("Chat model      : %s", settings.openai_model)
        logger.info("Embedding model : %s", settings.cohere_embed_model)
        logger.info("Collection      : %s", settings.collection_name)
        logger.info("Database        : %s", settings.safe_database_url)
        logger.info(
            "LangSmith       : %s",
            f"tracing to project '{settings.langsmith_project}'"
            if settings.langsmith_tracing
            else "disabled",
        )
    except ConfigError as exc:
        app.state.config_error = str(exc)
        logger.error("Configuration error: %s", exc)

    logger.info("Open the UI at http://localhost:%s", os.getenv("API_PORT", "8001"))
    yield
    logger.info("DevBrain shutting down")


app = FastAPI(
    title="DevBrain",
    description="A local RAG knowledge base over your own engineering documents.",
    version="1.0.0",
    lifespan=lifespan,
)

# Allow the page to call the API from anywhere. This is safe here because
# DevBrain binds to localhost and holds only your own documents; it also means
# opening index.html directly from disk (file://) still works.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
#  UI
# --------------------------------------------------------------------------- #


@app.get("/", include_in_schema=False)
async def serve_ui() -> FileResponse:
    if not INDEX_HTML.exists():
        raise HTTPException(status_code=500, detail="index.html is missing from the project folder.")
    # no-cache so edits to index.html show up on a plain browser refresh.
    return FileResponse(INDEX_HTML, media_type="text/html", headers={"Cache-Control": "no-store"})


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> JSONResponse:
    # Stops the browser's automatic favicon request from logging a 404.
    return JSONResponse(content=None, status_code=204)


# --------------------------------------------------------------------------- #
#  Health & config
# --------------------------------------------------------------------------- #


@app.get("/api/health")
def health() -> JSONResponse:
    """
    Report exactly which part of the stack is or isn't working.

    The UI polls this on load and turns the status dot green/amber/red, so a
    misconfiguration is visible immediately instead of surfacing as a failed
    question later.
    """
    settings: Settings | None = getattr(app.state, "settings", None)

    if settings is None:
        return JSONResponse(
            status_code=503,
            content={
                "status": "misconfigured",
                "detail": getattr(app.state, "config_error", "DevBrain is not configured."),
                "database": "unknown",
                "chunks": 0,
            },
        )

    payload: dict[str, Any] = {
        "status": "ok",
        "detail": "",
        "database": "connected",
        "pgvector": True,
        "chunks": 0,
        "collection": settings.collection_name,
        "chat_model": settings.openai_model,
        "embedding_model": settings.cohere_embed_model,
        "langsmith_tracing": settings.langsmith_tracing,
        "langsmith_project": settings.langsmith_project if settings.langsmith_tracing else None,
    }

    try:
        db.ping(settings)
    except Exception as exc:  # noqa: BLE001
        payload.update(
            status="database_unreachable",
            database="unreachable",
            pgvector=False,
            detail=(
                f"Cannot reach Postgres at {settings.safe_database_url} "
                f"({type(exc).__name__}). Start it with: docker compose up -d"
            ),
        )
        return JSONResponse(status_code=503, content=payload)

    try:
        payload["pgvector"] = db.pgvector_installed(settings)
        payload["chunks"] = db.count_chunks(settings)
        payload["sources"] = [
            {"source": source, "chunks": chunks}
            for source, chunks in db.count_chunks_by_source(settings)
        ]
    except Exception as exc:  # noqa: BLE001
        payload.update(status="degraded", detail=f"{type(exc).__name__}: {exc}")
        return JSONResponse(status_code=200, content=payload)

    if not payload["pgvector"]:
        payload.update(
            status="degraded",
            detail=(
                "The `vector` extension is not installed in this database. "
                "Make sure docker-compose.yml uses the pgvector/pgvector image."
            ),
        )
    elif payload["chunks"] == 0:
        payload.update(
            status="empty",
            detail=(
                "The knowledge base is empty. Put documents in the /data folder "
                "and run: python ingest.py"
            ),
        )

    return JSONResponse(status_code=200, content=payload)


@app.get("/api/config")
def get_config() -> JSONResponse:
    """Non-secret settings, so the UI footer can show what's actually running."""
    settings: Settings | None = getattr(app.state, "settings", None)
    if settings is None:
        return JSONResponse(status_code=503, content={"detail": app.state.config_error})
    return JSONResponse(
        content={
            "chat_model": settings.openai_model,
            "embedding_model": settings.cohere_embed_model,
            "collection": settings.collection_name,
            "retriever_k": settings.retriever_k,
            "chunk_size": settings.chunk_size,
            "langsmith_tracing": settings.langsmith_tracing,
            # Where each setting was resolved from: "environment", ".env" or
            # "default". Names and origins only — never the values themselves.
            "config_sources": config_sources(),
            "env_file_loaded": ENV_FILE_LOADED,
        }
    )


# --------------------------------------------------------------------------- #
#  Authentication
#
#  The session token lives in an HttpOnly cookie. JavaScript cannot read an
#  HttpOnly cookie, so a cross-site scripting bug cannot steal the session —
#  which is the main reason to prefer a cookie over localStorage here. SameSite
#  is "lax" so ordinary navigation works while cross-site form posts do not.
# --------------------------------------------------------------------------- #


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=40)
    password: str = Field(..., min_length=8, max_length=200)
    full_name: str = Field(default="", max_length=120)
    email: str = Field(default="", max_length=200)


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=40)
    password: str = Field(..., min_length=1, max_length=200)


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=auth.SESSION_COOKIE,
        value=token,
        max_age=auth.SESSION_TTL_HOURS * 3600,
        httponly=True,      # unreadable from JavaScript
        samesite="lax",
        secure=False,       # DevBrain runs on plain HTTP locally; set True behind HTTPS
        path="/",
    )


def current_user(request: Request) -> auth.User:
    """
    FastAPI dependency: resolve the caller, or reject with 401.

    Because this re-reads the session from the database on every request, an
    account that is deleted or disabled loses access immediately — there is no
    window where an already-issued token keeps working.
    """
    settings = _require_settings()
    user = auth.user_for_token(settings, request.cookies.get(auth.SESSION_COOKIE))
    if user is None:
        raise HTTPException(status_code=401, detail="Please sign in to continue.")
    return user


def current_admin(request: Request) -> auth.User:
    """FastAPI dependency: like `current_user`, but administrators only."""
    user = current_user(request)
    if not user.is_admin:
        raise HTTPException(
            status_code=403, detail="Administrator access is required for this action."
        )
    return user


def _require_settings() -> Settings:
    settings: Settings | None = getattr(app.state, "settings", None)
    if settings is None:
        raise HTTPException(
            status_code=503,
            detail=getattr(app.state, "config_error", "DevBrain is not configured."),
        )
    return settings


@app.post("/api/auth/register")
def register(payload: RegisterRequest, response: Response) -> JSONResponse:
    """Create a standard (non-admin) account and sign the person straight in."""
    settings = _require_settings()
    _ensure_auth_ready(settings)

    try:
        user = auth.create_user(
            settings,
            username=payload.username,
            password=payload.password,
            full_name=payload.full_name,
            email=payload.email,
            is_admin=False,          # administrators are only ever seeded from .env
        )
        token, user = auth.login(settings, payload.username, payload.password)
    except auth.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info("new account registered: %s", user.username)
    body = JSONResponse(content={"user": user.as_dict()})
    _set_session_cookie(body, token)
    return body


@app.post("/api/auth/login")
def login(payload: LoginRequest) -> JSONResponse:
    settings = _require_settings()
    _ensure_auth_ready(settings)

    try:
        token, user = auth.login(settings, payload.username, payload.password)
    except auth.AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    logger.info("sign-in: %s%s", user.username, " (admin)" if user.is_admin else "")
    body = JSONResponse(content={"user": user.as_dict()})
    _set_session_cookie(body, token)
    return body


@app.post("/api/auth/logout")
def logout(request: Request) -> JSONResponse:
    settings = _require_settings()
    auth.logout(settings, request.cookies.get(auth.SESSION_COOKIE))
    body = JSONResponse(content={"ok": True})
    body.delete_cookie(auth.SESSION_COOKIE, path="/")
    return body


@app.get("/api/auth/me")
def whoami(request: Request) -> JSONResponse:
    """
    Who is signed in? Returns 200 with `user: null` when nobody is.

    Deliberately not a 401: the UI calls this on load to decide whether to show
    the sign-in screen or the app, and an anonymous visitor is a normal state
    rather than an error.
    """
    settings: Settings | None = getattr(app.state, "settings", None)
    if settings is None:
        return JSONResponse(status_code=503, content={"user": None, "detail": app.state.config_error})

    try:
        _ensure_auth_ready(settings)
        user = auth.user_for_token(settings, request.cookies.get(auth.SESSION_COOKIE))
    except Exception as exc:  # noqa: BLE001 - database not up yet
        return JSONResponse(
            status_code=503,
            content={"user": None, "detail": f"Database unavailable ({type(exc).__name__})."},
        )
    return JSONResponse(content={"user": user.as_dict() if user else None})


_AUTH_SETUP_LOCK = threading.Lock()


def _ensure_auth_ready(settings: Settings) -> None:
    """
    Create the auth tables and seed the administrator, once per process.

    Done lazily rather than at start-up so the server still boots when Postgres
    is not running yet, matching how the RAG pipeline is built.
    """
    if getattr(app.state, "auth_ready", False):
        return
    # The page fires several requests at once on first load. Without a lock they
    # all run CREATE TABLE concurrently, block each other, and exhaust the
    # connection pool. Only one does the setup; the rest wait, then see it done.
    with _AUTH_SETUP_LOCK:
        if getattr(app.state, "auth_ready", False):
            return
        auth.init_schema(settings)
        created = auth.ensure_admin(settings)
        if created:
            logger.info("seeded administrator account: %s", created)
        app.state.auth_ready = True


# --------------------------------------------------------------------------- #
#  My documents — every signed-in user can review their own uploads
# --------------------------------------------------------------------------- #


@app.get("/api/my/documents")
def my_documents(user: auth.User = Depends(current_user)) -> JSONResponse:
    """
    The documents the signed-in user uploaded, with live chunk counts.

    Scoped by `uploaded_by_id` on the server rather than filtering client-side,
    so one user can never see another's uploads by tampering with a request.
    Chunk counts come from the vector store, so the figure shown is what
    retrieval will actually search.
    """
    settings = _require_settings()
    documents = auth.list_documents_for_user(settings, user.id)

    live = dict(db.count_chunks_by_source(settings))
    for document in documents:
        document["chunks"] = live.get(document["source"], 0)
        document["on_disk"] = (settings.data_dir / document["source"]).exists()

    return JSONResponse(content={
        "documents": documents,
        "total_chunks": sum(d["chunks"] for d in documents),
    })

# --------------------------------------------------------------------------- #
#  Admin — user management
# --------------------------------------------------------------------------- #


@app.get("/api/admin/users")
def admin_list_users(admin: auth.User = Depends(current_admin)) -> JSONResponse:
    settings = _require_settings()
    return JSONResponse(content={"users": auth.list_users(settings), "me": admin.id})


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: int, admin: auth.User = Depends(current_admin)) -> JSONResponse:
    settings = _require_settings()

    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="You cannot delete your own account.")

    try:
        username = auth.delete_user(settings, user_id)
    except auth.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info("admin %s deleted user %s", admin.username, username)
    return JSONResponse(content={"ok": True, "username": username})


# --------------------------------------------------------------------------- #
#  Admin — document management
# --------------------------------------------------------------------------- #


@app.get("/api/admin/documents")
def admin_list_documents(admin: auth.User = Depends(current_admin)) -> JSONResponse:  # noqa: ARG001
    """
    Every uploaded document, with its owner, purpose and live chunk count.

    Chunk counts come from the vector store rather than the registry, so the
    number shown is what retrieval will actually search — not a stale figure
    recorded at upload time.
    """
    settings = _require_settings()
    documents = auth.list_documents(settings)

    live = dict(db.count_chunks_by_source(settings))
    known = {d["source"] for d in documents}

    for document in documents:
        document["chunks"] = live.get(document["source"], 0)
        document["on_disk"] = (settings.data_dir / document["source"]).exists()

    # Files ingested from the CLI before this feature existed have no registry
    # row. Surface them anyway so the admin can see and manage everything.
    for source, chunks in live.items():
        if source not in known:
            documents.append({
                "source": source,
                "file_type": Path(source).suffix.lstrip("."),
                "purpose": "",
                "size_bytes": 0,
                "chunks": chunks,
                "uploaded_by": "system",
                "uploaded_at": None,
                "on_disk": (settings.data_dir / source).exists(),
            })

    return JSONResponse(content={"documents": documents, "total_chunks": db.count_chunks(settings)})


@app.delete("/api/admin/documents/{source:path}")
def admin_delete_document(source: str, admin: auth.User = Depends(current_admin)) -> JSONResponse:
    """
    Delete a document: its chunks, its registry row, and the file itself.

    Deleting the file alone would leave its chunks in pgvector still answering
    questions, so the vector store is cleaned first and the index is effectively
    refreshed the moment this returns — no re-ingestion step is needed.
    """
    settings = _require_settings()

    # `source` comes from the URL, so treat it as untrusted and confirm the
    # resolved path really is inside the data folder before deleting anything.
    target = (settings.data_dir / source).resolve()
    try:
        target.relative_to(settings.data_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid document path.") from None

    removed_chunks = db.delete_chunks_for_source(settings, source)
    auth.forget_document(settings, source)

    file_deleted = False
    if target.exists() and target.is_file():
        try:
            target.unlink()
            file_deleted = True
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=500,
                detail=f"Chunks removed, but the file could not be deleted: {exc}",
            ) from exc

    logger.info(
        "admin %s deleted document %s (%d chunks, file removed: %s)",
        admin.username, source, removed_chunks, file_deleted,
    )

    return JSONResponse(content={
        "ok": True,
        "source": source,
        "chunks_removed": removed_chunks,
        "file_deleted": file_deleted,
        "total_chunks": db.count_chunks(settings),
    })


# --------------------------------------------------------------------------- #
#  The RAG endpoint
# --------------------------------------------------------------------------- #


@app.post("/api/ask", response_model=AskResponse)
def ask(request: AskRequest, user: auth.User = Depends(current_user)) -> AskResponse:
    """
    Answer a question from the ingested documents.

    Flow:
      1. Refuse early if nothing has been ingested (saves a pointless API call).
      2. Run the LCEL chain: embed question -> pgvector similarity search ->
         build prompt -> OpenAI -> string answer.
      3. Return the answer *plus* the real retrieved sources, so the user can
         verify it rather than trusting it.

    Why this handler is `def` and not `async def`
    ---------------------------------------------
    `PGVector` is created here in synchronous mode, which is what `ingest.py`
    needs and what keeps a single shared configuration between the two scripts.
    A synchronous vector store has no async engine, so calling `chain.ainvoke()`
    on it raises `AssertionError: This method must be called with async_mode`.

    Declaring the handler as plain `def` is the clean fix: FastAPI automatically
    runs synchronous handlers in a worker thread, so the blocking database and
    OpenAI calls never stall the event loop, and other requests are still served
    concurrently. Keeping the whole call on one thread also means the
    `collect_runs` context variable below behaves predictably.

    `collect_runs` captures the LangSmith run ID. Returning it to the browser
    means any answer can be traced back to the exact prompt and chunks that
    produced it.
    """
    pipeline = get_pipeline(app)
    settings = pipeline.settings
    question = request.question.strip()

    # ---- 1. empty knowledge base -----------------------------------------
    try:
        if db.count_chunks(settings) == 0:
            return AskResponse(
                question=question,
                answer=(
                    "The knowledge base is empty, so I have nothing to search.\n\n"
                    "Add documents (.txt, .md, .pdf, .docx, .xlsx, .html, .jpg, .png, .gif) to the `data` "
                    "folder, then run:\n\n"
                    "```\npython ingest.py\n```"
                ),
                sources=[],
                chunks_retrieved=0,
                latency_ms=0,
                grounded=False,
            )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail=(
                f"Cannot reach the database ({type(exc).__name__}). "
                "Start it with: docker compose up -d"
            ),
        ) from exc

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

    except Exception as exc:  # noqa: BLE001
        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.exception("Question failed after %d ms: %s", latency_ms, question)
        raise HTTPException(status_code=502, detail=_explain_failure(exc)) from exc

    latency_ms = int((time.perf_counter() - started) * 1000)
    documents = result.get("docs") or []

    logger.info(
        'answered in %d ms | k=%d | chunks=%d | q="%s"',
        latency_ms,
        request.top_k or settings.retriever_k,
        len(documents),
        question[:80],
    )

    return AskResponse(
        question=question,
        answer=result.get("answer", "").strip(),
        sources=[Citation(**c) for c in documents_to_citations(documents)],
        chunks_retrieved=len(documents),
        latency_ms=latency_ms,
        langsmith_run_id=run_id,
        grounded=bool(documents),
    )


# --------------------------------------------------------------------------- #
#  Upload endpoint
# --------------------------------------------------------------------------- #

# Refuse anything larger than this per file. Without a cap, one enormous upload
# could fill the disk and would cost a small fortune in embedding calls.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024      # 25 MB
MAX_FILES_PER_REQUEST = 20


def _safe_filename(raw: str) -> str:
    """
    Reduce a browser-supplied filename to a bare, safe name.

    A filename from a client is untrusted input. Left alone, a name like
    `../../../../app/app.py` would let an upload overwrite application code, and
    `..\\..\\.env` would overwrite your secrets. Taking only the final path
    component defeats both, and stripping the remaining unusual characters keeps
    the name portable across Windows and Linux.
    """
    # Handle both separators: a Windows client can send backslashes.
    name = raw.replace("\\", "/").split("/")[-1].strip()

    # Windows reserves leading/trailing dots and spaces; "." and ".." are never
    # legitimate document names.
    name = name.strip(". ")
    if not name or name in {".", ".."}:
        return ""

    # Keep letters, digits and a conservative punctuation set.
    name = re.sub(r"[^A-Za-z0-9._ ()\-]+", "_", name)
    name = re.sub(r"_{2,}", "_", name)

    # Leave room for the de-duplication suffix added below.
    stem, dot, suffix = name.rpartition(".")
    if dot and len(stem) > 120:
        name = stem[:120] + "." + suffix
    return name[:160]


def _unique_path(directory: Path, filename: str) -> Path:
    """
    Return a path inside `directory` for `filename`.

    An identical name is treated as a deliberate REPLACEMENT — the file is
    overwritten and `ingest_files` deletes the old chunks — because re-uploading
    an edited document is the common case and silently accumulating
    `notes(1).md`, `notes(2).md` would quietly poison retrieval with several
    versions of the same content.
    """
    return directory / filename


class UploadedFileResult(BaseModel):
    filename: str
    status: str                      # "ingested" | "skipped" | "failed"
    chunks: int = 0
    bytes: int = 0
    detail: str = ""
    uploaded_by: str = ""


class UploadResponse(BaseModel):
    uploaded: int
    ingested: int
    chunks_added: int
    total_chunks: int
    files: list[UploadedFileResult]


@app.post("/api/upload", response_model=UploadResponse)
def upload(
    files: list[UploadFile] = File(...),
    purpose: str = Form(default="", max_length=500),
    user: auth.User = Depends(current_user),
) -> UploadResponse:
    """
    Accept document uploads, save them into the data folder, and ingest them.

    The whole point of this endpoint is that ingestion is **automatic**: by the
    time it returns, the uploaded documents are already chunked, embedded and
    searchable. No CLI step, no server restart — the next question can cite them.

    Each file is validated independently and reported on independently, so one
    unreadable PDF in a batch of ten does not lose you the other nine.
    """
    pipeline = get_pipeline(app)          # 503 with guidance if the DB is down
    settings = pipeline.settings
    data_dir = settings.data_dir

    # A blank purpose defeats the point of recording one, so it is required.
    # Relax this by removing the check if you want it optional again.
    purpose = (purpose or "").strip()
    if not purpose:
        raise HTTPException(
            status_code=422,
            detail="Please describe the purpose of the document before uploading.",
        )

    if len(files) > MAX_FILES_PER_REQUEST:
        raise HTTPException(
            status_code=413,
            detail=f"Too many files at once (max {MAX_FILES_PER_REQUEST}). Upload in smaller batches.",
        )

    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500, detail=f"Cannot create the data folder {data_dir}: {exc}"
        ) from exc

    results: list[UploadedFileResult] = []
    accepted: list[Path] = []

    for upload_file in files:
        original = upload_file.filename or ""
        name = _safe_filename(original)

        if not name:
            results.append(UploadedFileResult(
                filename=original or "(unnamed)", status="skipped",
                detail="Invalid or empty filename."))
            continue

        extension = Path(name).suffix.lower()
        if extension not in SUPPORTED_EXTENSIONS:
            results.append(UploadedFileResult(
                filename=name, status="skipped",
                detail=f"Unsupported type '{extension or 'none'}'. "
                       f"Allowed: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"))
            continue

        try:
            payload = upload_file.file.read()
        except Exception as exc:  # noqa: BLE001
            results.append(UploadedFileResult(
                filename=name, status="failed", detail=f"Could not read upload: {exc}"))
            continue
        finally:
            upload_file.file.close()

        if not payload:
            results.append(UploadedFileResult(
                filename=name, status="skipped", detail="File is empty."))
            continue

        if len(payload) > MAX_UPLOAD_BYTES:
            results.append(UploadedFileResult(
                filename=name, status="skipped", bytes=len(payload),
                detail=f"Too large ({len(payload) / 1048576:.1f} MB). "
                       f"Limit is {MAX_UPLOAD_BYTES // 1048576} MB per file."))
            continue

        destination = _unique_path(data_dir, name)

        # Defence in depth: even after sanitising, confirm the resolved path is
        # genuinely inside the data folder before writing anything.
        try:
            destination.resolve().relative_to(data_dir.resolve())
        except ValueError:
            results.append(UploadedFileResult(
                filename=name, status="skipped",
                detail="Rejected: path escapes the data folder."))
            continue

        try:
            destination.write_bytes(payload)
        except Exception as exc:  # noqa: BLE001
            results.append(UploadedFileResult(
                filename=name, status="failed", bytes=len(payload),
                detail=f"Could not save file: {exc}"))
            continue

        accepted.append(destination)
        results.append(UploadedFileResult(
            filename=name, status="ingested", bytes=len(payload)))

    # ---- ingest everything that landed on disk ----------------------------
    chunks_added = 0
    if accepted:
        try:
            from ingest import ingest_files

            # Attribution is stamped onto every chunk, so a later answer can
            # show who contributed the document it drew on.
            uploaded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            summary = ingest_files(
                settings,
                accepted,
                extra_metadata={"uploaded_by": user.username, "uploaded_at": uploaded_at},
            )
            chunks_added = int(summary["chunks_added"])

            by_source = {str(f["source"]): f for f in summary["files"]}
            for result in results:
                entry = by_source.get(result.filename)
                if entry is None:
                    continue
                if entry["error"]:
                    result.status = "failed"
                    result.detail = str(entry["error"])
                else:
                    result.chunks = int(entry["chunks"])
                    result.uploaded_by = user.username
                    auth.record_document(
                        settings,
                        source=result.filename,
                        file_type=Path(result.filename).suffix.lstrip("."),
                        purpose=purpose,
                        size_bytes=result.bytes,
                        chunks=result.chunks,
                        user=user,
                    )

        except Exception as exc:  # noqa: BLE001
            logger.exception("Ingestion failed after upload")
            detail = _explain_failure(exc)
            for result in results:
                if result.status == "ingested":
                    result.status = "failed"
                    result.detail = f"Saved to /data, but ingestion failed: {detail}"

    ingested = sum(1 for r in results if r.status == "ingested")

    try:
        total_chunks = db.count_chunks(settings)
    except Exception:  # noqa: BLE001
        total_chunks = 0

    logger.info(
        "upload by %s: %d file(s) received, %d ingested, %d chunk(s) added",
        user.username, len(files), ingested, chunks_added,
    )

    return UploadResponse(
        uploaded=len(files),
        ingested=ingested,
        chunks_added=chunks_added,
        total_chunks=total_chunks,
        files=results,
    )


def _explain_failure(exc: Exception) -> str:
    """Translate the most common upstream failures into an actionable message."""
    message = str(exc)
    low = message.lower()

    if "insufficient_quota" in low or "exceeded your current quota" in low:
        return (
            "Your OpenAI account has no remaining quota. Add credit at "
            "https://platform.openai.com/settings/organization/billing"
        )
    if "invalid_api_key" in low or "incorrect api key" in low:
        return "OpenAI rejected the API key. Check OPENAI_API_KEY in your .env file."
    if "model_not_found" in low or "does not exist or you do not have access" in low:
        return (
            "Your OpenAI account cannot access the configured model. "
            "Try setting OPENAI_MODEL=gpt-4o-mini in your .env file."
        )
    if "rate limit" in low or "429" in low:
        return "Rate limited by the API provider. Wait a few seconds and try again."
    if "cohere" in low and ("401" in low or "unauthorized" in low or "invalid api" in low):
        return "Cohere rejected the API key. Check COHERE_API_KEY in your .env file."
    if "expected" in low and "dimensions" in low:
        return (
            "Vector size mismatch: the stored embeddings were created with a different "
            "Cohere model than the one now configured. Re-ingest with: "
            "python ingest.py --reset"
        )
    if "connection" in low or "refused" in low or "closed the connection" in low:
        return (
            "Lost the connection to Postgres. Check the container with: "
            "docker compose ps"
        )
    return f"{type(exc).__name__}: {message}"


if __name__ == "__main__":
    # Allows `python app.py` as well as the recommended
    # `uvicorn app:app --reload --port 8001`.
    import uvicorn

    uvicorn.run(
        "app:app",
        host="127.0.0.1",
        port=int(os.getenv("API_PORT", "8001")),
        reload=True,
    )

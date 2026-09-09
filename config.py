"""
DevBrain — central configuration.

Every setting the app needs is read from the `.env` file exactly once, right
here. Both `ingest.py` and `app.py` import this module, which guarantees they
always agree on things like the embedding model and the collection name (a
mismatch between them is the #1 cause of "my search returns nothing").

This module also fails loudly and early with human-readable messages if a key
is missing, rather than letting you discover it halfway through a 5-minute
ingestion run.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Absolute path to the folder containing this file. Using this (instead of the
# current working directory) means the scripts work no matter where you run
# them from.
PROJECT_ROOT = Path(__file__).resolve().parent

# --------------------------------------------------------------------------- #
#  Where configuration comes from
#
#  APPLICATION (ENVIRONMENT) VARIABLES ARE THE PRIMARY SOURCE.
#
#  Every setting below is read from the process environment. A `.env` file is
#  an optional developer convenience that only fills in values the environment
#  has not already supplied — it can never override a real variable.
#
#  Precedence, highest first:
#    1. Real environment variables  (OS / container / CI / App Settings)
#    2. .env file                   (optional, local development only)
#    3. Built-in defaults           (non-secret values only)
#
#  Two escape hatches:
#    DEVBRAIN_ENV_FILE       point at a different file instead of ./.env
#    DEVBRAIN_IGNORE_ENV_FILE=1   ignore any .env entirely, environment only
# --------------------------------------------------------------------------- #

# Captured BEFORE the .env file is read, so we can report which source each
# value actually came from. Purely diagnostic, but it turns "why is it using
# the wrong key?" from guesswork into a one-line answer.
_ENVIRONMENT_AT_STARTUP: frozenset[str] = frozenset(os.environ)

ENV_FILE = Path(os.getenv("DEVBRAIN_ENV_FILE") or (PROJECT_ROOT / ".env"))
IGNORE_ENV_FILE = (os.getenv("DEVBRAIN_IGNORE_ENV_FILE") or "").strip().lower() in (
    "1", "true", "yes", "y", "on",
)

# `override=False` is what enforces the precedence above: a real environment
# variable is never replaced by a value from the file.
ENV_FILE_LOADED = False
if not IGNORE_ENV_FILE and ENV_FILE.is_file():
    load_dotenv(ENV_FILE, override=False)
    ENV_FILE_LOADED = True


def value_source(name: str) -> str:
    """
    Report where a setting's value came from: "environment", ".env" or "default".

    Used by the start-up banner and /api/config so configuration problems are
    diagnosable without printing any secret.
    """
    if name in _ENVIRONMENT_AT_STARTUP:
        return "environment"
    if name in os.environ:
        return ".env" if ENV_FILE_LOADED else "environment"
    return "default"


def config_sources() -> dict[str, str]:
    """Source of every setting DevBrain reads. Names only — never values."""
    return {name: value_source(name) for name in SETTING_NAMES}


# Every variable the application understands, grouped for reporting.
SECRET_NAMES: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "COHERE_API_KEY",
    "LANGSMITH_API_KEY",
    "ADMIN_PASSWORD",
)

SETTING_NAMES: tuple[str, ...] = SECRET_NAMES + (
    "OPENAI_MODEL",
    "OPENAI_TEMPERATURE",
    "COHERE_EMBED_MODEL",
    "LANGSMITH_TRACING",
    "LANGSMITH_PROJECT",
    "LANGSMITH_ENDPOINT",
    "DATABASE_URL",
    "COLLECTION_NAME",
    "DATA_DIR",
    "CHUNK_SIZE",
    "CHUNK_OVERLAP",
    "RETRIEVER_K",
    "RETRIEVER_SCORE_THRESHOLD",
    "API_PORT",
    "ADMIN_USERNAME",
    "ADMIN_FULL_NAME",
    "ADMIN_EMAIL",
)


class ConfigError(RuntimeError):
    """Raised when the .env file is missing or incomplete."""


# --------------------------------------------------------------------------- #
#  Small helpers for reading typed values out of the environment
# --------------------------------------------------------------------------- #

# Substrings that indicate the user copied .env.example but never filled it in.
_PLACEHOLDER_MARKERS = ("paste-your", "paste_your", "your-key-here", "changeme")


def _is_placeholder(value: str | None) -> bool:
    if not value or not value.strip():
        return True
    lowered = value.strip().lower()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def _str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _int(name: str, default: int) -> int:
    raw = _str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(
            f"{name} must be a whole number, but got {raw!r} in your .env file."
        ) from exc


def _float(name: str, default: float) -> float:
    raw = _str(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(
            f"{name} must be a number, but got {raw!r} in your .env file."
        ) from exc


def _bool(name: str, default: bool) -> bool:
    raw = _str(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on")


def _normalise_db_url(url: str) -> str:
    """
    `langchain-postgres` talks to Postgres through SQLAlchemy + the psycopg 3
    driver, so the URL must start with `postgresql+psycopg://`.

    Beginners very often paste a plain `postgresql://...` URL (the format
    every other tool uses), which raises a confusing SQLAlchemy driver error.
    We silently upgrade those instead of failing.
    """
    for legacy_prefix in ("postgresql+psycopg2://", "postgres://", "postgresql://"):
        if url.startswith(legacy_prefix):
            return "postgresql+psycopg://" + url[len(legacy_prefix) :]
    return url


# --------------------------------------------------------------------------- #
#  The settings object
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of everything configured in .env."""

    # --- OpenAI (answer generation) ---
    openai_api_key: str
    openai_model: str
    openai_temperature: float

    # --- Cohere (embeddings) ---
    cohere_api_key: str
    cohere_embed_model: str

    # --- LangSmith (tracing) ---
    langsmith_tracing: bool
    langsmith_api_key: str
    langsmith_project: str
    langsmith_endpoint: str

    # --- Postgres / pgvector ---
    database_url: str
    collection_name: str

    # --- Ingestion & retrieval ---
    data_dir: Path
    chunk_size: int
    chunk_overlap: int
    retriever_k: int
    retriever_score_threshold: float

    # Populated in __post_init__ so callers can show warnings in the UI/logs.
    warnings: list[str] = field(default_factory=list)

    @property
    def safe_database_url(self) -> str:
        """The connection string with the password masked, safe to log."""
        url = self.database_url
        if "@" not in url or "://" not in url:
            return url
        scheme, rest = url.split("://", 1)
        creds, host = rest.rsplit("@", 1)
        user = creds.split(":", 1)[0]
        return f"{scheme}://{user}:****@{host}"


def _build_settings() -> Settings:
    problems: list[str] = []
    warnings: list[str] = []

    openai_api_key = _str("OPENAI_API_KEY")
    if _is_placeholder(openai_api_key):
        problems.append("OPENAI_API_KEY is missing. Get one at https://platform.openai.com/api-keys")

    cohere_api_key = _str("COHERE_API_KEY")
    if _is_placeholder(cohere_api_key):
        problems.append("COHERE_API_KEY is missing. Get one at https://dashboard.cohere.com/api-keys")

    # A missing .env file is only worth mentioning when something is actually
    # unset. In Docker, Kubernetes and CI the settings arrive as real
    # environment variables and no .env file exists at all — treating that as an
    # error would make the app impossible to containerise.
    # Tested against ENV_FILE_LOADED, not merely whether a file exists: with
    # DEVBRAIN_IGNORE_ENV_FILE set, a .env on disk is deliberately not read, and
    # the advice must reflect what the app actually consulted.
    if problems and not ENV_FILE_LOADED:
        problems.insert(
            0,
            "No configuration source was read. Supply these as application\n"
            "    (environment) variables — the recommended route:\n"
            "        $env:OPENAI_API_KEY = 'sk-...'      PowerShell, this session only\n"
            "        .\\set-env.ps1                       PowerShell, saved permanently\n"
            "        export OPENAI_API_KEY=sk-...        macOS / Linux\n"
            "    ...or, for local development only, copy .env.example to .env:\n"
            "        copy .env.example .env",
        )

    if problems:
        bullets = "\n".join(f"  - {p}" for p in problems)
        raise ConfigError(f"DevBrain is not configured yet:\n{bullets}")

    # LangSmith is optional. Rather than crashing, we just turn tracing off and
    # tell the user why — the RAG pipeline itself does not need it.
    langsmith_tracing = _bool("LANGSMITH_TRACING", False)
    langsmith_api_key = _str("LANGSMITH_API_KEY")
    if langsmith_tracing and _is_placeholder(langsmith_api_key):
        langsmith_tracing = False
        langsmith_api_key = ""
        warnings.append(
            "LANGSMITH_TRACING is on but LANGSMITH_API_KEY is not set — "
            "tracing disabled. The app works normally without it."
        )

    chunk_size = _int("CHUNK_SIZE", 1000)
    chunk_overlap = _int("CHUNK_OVERLAP", 150)
    if chunk_overlap >= chunk_size:
        raise ConfigError(
            f"CHUNK_OVERLAP ({chunk_overlap}) must be smaller than "
            f"CHUNK_SIZE ({chunk_size}), otherwise chunking never advances."
        )

    data_dir = Path(_str("DATA_DIR", "data"))
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir

    return Settings(
        openai_api_key=openai_api_key,
        openai_model=_str("OPENAI_MODEL", "gpt-4o-mini"),
        openai_temperature=_float("OPENAI_TEMPERATURE", 0.0),
        cohere_api_key=cohere_api_key,
        cohere_embed_model=_str("COHERE_EMBED_MODEL", "embed-english-v3.0"),
        langsmith_tracing=langsmith_tracing,
        langsmith_api_key=langsmith_api_key,
        langsmith_project=_str("LANGSMITH_PROJECT", "devbrain"),
        langsmith_endpoint=_str("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com"),
        database_url=_normalise_db_url(
            _str(
                "DATABASE_URL",
                "postgresql+psycopg://devbrain:devbrain_pass@localhost:5432/devbrain",
            )
        ),
        collection_name=_str("COLLECTION_NAME", "devbrain_docs"),
        data_dir=data_dir,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        retriever_k=_int("RETRIEVER_K", 4),
        retriever_score_threshold=_float("RETRIEVER_SCORE_THRESHOLD", 0.0),
        warnings=warnings,
    )


def configure_langsmith(settings: Settings) -> None:
    """
    Switch LangSmith tracing on by exporting the environment variables that
    LangChain looks for internally.

    There is no code to write beyond this: once these variables are set, every
    LangChain component (retriever, prompt, LLM) automatically reports its
    inputs, outputs, latency and token usage to your LangSmith project.

    We set both the modern `LANGSMITH_*` and the legacy `LANGCHAIN_*` names so
    that tracing works regardless of which one your installed versions read.
    """
    if not settings.langsmith_tracing:
        os.environ["LANGSMITH_TRACING"] = "false"
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
        return

    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
    os.environ["LANGCHAIN_API_KEY"] = settings.langsmith_api_key
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    os.environ["LANGCHAIN_PROJECT"] = settings.langsmith_project
    os.environ["LANGSMITH_ENDPOINT"] = settings.langsmith_endpoint
    os.environ["LANGCHAIN_ENDPOINT"] = settings.langsmith_endpoint


def load_settings(exit_on_error: bool = False) -> Settings:
    """
    Build the settings object and apply LangSmith configuration.

    Set `exit_on_error=True` in command-line scripts to print a friendly
    message instead of a Python traceback.
    """
    try:
        settings = _build_settings()
    except ConfigError as exc:
        if exit_on_error:
            print(f"\n[X] Configuration error\n\n{exc}\n", file=sys.stderr)
            raise SystemExit(1) from exc
        raise

    configure_langsmith(settings)
    return settings

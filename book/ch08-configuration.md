# Chapter 8 — Configuration and Secrets

---

## 8.1 The problem

DevBrain needs twenty-one settings: three API keys, an admin password, model
names, a database URL, chunk sizes, retrieval parameters, ports.

Some are secret. Some vary between your laptop and a server. All of them are needed
by **two separate programs** — `ingest.py` and `app.py` — which must agree
perfectly.

That last point is the one that bites. If ingestion uses `embed-english-v3.0` and
the API uses `embed-multilingual-v3.0`, both run without error and every search
result is quietly wrong. Chapter 3 explained why: vectors from different models
live in unrelated coordinate systems.

**So the goal is not just "read some settings". It is to make disagreement
structurally impossible.**

---

## 8.2 Where configuration should come from

Three options, in the order most people try them:

**Hard-code it.** Secrets end up in git, in your image, and eventually on GitHub.
Non-negotiable: no.

**A config file the app reads.** Better, but the file must exist everywhere the app
runs, and in Docker or CI you would have to inject a file just to hold three
strings.

**Environment variables.** The application reads `os.environ`. The *platform*
decides how they get there — your shell, a container's `environment:` block, a CI
secret, an Azure App Setting, a Kubernetes secret.

DevBrain uses the third, with a `.env` file as an **optional local convenience**.

### The precedence rule

```
1. Real environment variables    OS / container / CI / App Settings
2. .env file                     local development only
3. Built-in defaults             non-secret values only
```

**A real environment variable can never be overridden by the file.** That single
rule is what lets the same image run on your laptop and in production with nothing
changing but the environment.

---

## 8.3 Getting your keys

You need three. All have free tiers sufficient for this project.

### OpenAI — writes the answers

1. Go to <https://platform.openai.com/api-keys>
2. **Create new secret key**, name it `devbrain`
3. Copy immediately — it is never shown again. Starts with `sk-`

> **Billing:** a new OpenAI account needs credit before the API works, *even if you
> pay for ChatGPT Plus*. They are separate products. Add a few dollars at
> <https://platform.openai.com/settings/organization/billing>. A DevBrain question
> costs well under a cent with the default model.

### Cohere — creates the embeddings

1. Go to <https://dashboard.cohere.com/api-keys>
2. Copy your **Trial key** (created automatically on signup)

Trial keys are free and allow roughly 100 calls/minute. Ingesting six documents
uses exactly **one** call.

### LangSmith — records traces (optional)

1. <https://smith.langchain.com> → **Settings → API Keys → Create API Key**
2. Copy it. Starts with `lsv2_`

> **Region matters, and the failure is deceptive.** LangSmith runs separate US and
> EU instances, and a key from one is rejected by the other with **403 Forbidden** —
> the same error you get from a completely invalid key. Check your address bar:
> `smith.langchain.com` → US endpoint; `eu.smith.langchain.com` →
> `https://eu.api.smith.langchain.com`. This cost real debugging time; §8.9 covers
> the diagnosis.

---

## 8.4 Storing them without a file

The recommended route on Windows — no secrets on disk:

```powershell
.\set-env.ps1
```

That script (shipped with the project) prompts for each key with **masked input**
and saves them as Windows *user* environment variables. They persist across
reboots, are available to every terminal, VS Code and Docker Compose, need no
administrator rights, and are not visible to other accounts.

```powershell
.\set-env.ps1 -Show     # which are set — values never displayed
.\set-env.ps1 -Clear    # remove them all
```

Restart open terminals afterwards; they still hold the old environment.

**Just this session:**

```powershell
$env:OPENAI_API_KEY    = "sk-..."
$env:COHERE_API_KEY    = "..."
$env:LANGSMITH_API_KEY = "lsv2_pt_..."
```

**macOS / Linux:**

```bash
export OPENAI_API_KEY=sk-...
export COHERE_API_KEY=...
export LANGSMITH_API_KEY=lsv2_pt_...
```

**Or a `.env` file** for local development — copy `.env.example` to `.env` and fill
it in. Just remember it is the *fallback*, not the source of truth.

---

## 8.5 The configuration module

`config.py` is 360 lines. Rather than reproduce all of it, we will build up the
parts that carry the design.

### Loading, with precedence

```python
# Captured BEFORE the .env file is read, so we can report which source each
# value actually came from.
_ENVIRONMENT_AT_STARTUP: frozenset[str] = frozenset(os.environ)

ENV_FILE = Path(os.getenv("DEVBRAIN_ENV_FILE") or (PROJECT_ROOT / ".env"))
IGNORE_ENV_FILE = (os.getenv("DEVBRAIN_IGNORE_ENV_FILE") or "").strip().lower() in (
    "1", "true", "yes", "y", "on",
)

# `override=False` is what enforces the precedence: a real environment
# variable is never replaced by a value from the file.
ENV_FILE_LOADED = False
if not IGNORE_ENV_FILE and ENV_FILE.is_file():
    load_dotenv(ENV_FILE, override=False)
    ENV_FILE_LOADED = True
```

Four things worth noticing:

**`override=False` is the whole precedence rule.** One keyword argument. Flip it to
`True` and a stale `.env` would silently beat the environment variable you just
set — a genuinely nasty class of bug.

**`_ENVIRONMENT_AT_STARTUP` is captured first.** Once `load_dotenv` runs, both
sources look identical in `os.environ`. Snapshotting beforehand is the only way to
tell them apart afterwards.

**`PROJECT_ROOT`, not the working directory.** Uses `Path(__file__).resolve().parent`,
so the scripts work no matter where you run them from.

**Two escape hatches.** `DEVBRAIN_ENV_FILE` points elsewhere; `DEVBRAIN_IGNORE_ENV_FILE=1`
ignores files entirely. The second is useful in containers where a stray `.env`
would be confusing.

### Reporting where values came from

```python
def value_source(name: str) -> str:
    """Report where a setting came from: "environment", ".env" or "default"."""
    if name in _ENVIRONMENT_AT_STARTUP:
        return "environment"
    if name in os.environ:
        return ".env" if ENV_FILE_LOADED else "environment"
    return "default"
```

This turns *"why is it using the wrong key?"* from guesswork into a one-line
answer. At startup the app prints:

```
Config source   : 4 from environment, 17 from .env, 0 default
  OPENAI_API_KEY     <- environment
  COHERE_API_KEY     <- environment
  LANGSMITH_API_KEY  <- .env
  ADMIN_PASSWORD     <- environment
```

**Names and origins only — never values.** That output is safe to paste into a bug
report, which is exactly what makes it useful.

### Typed readers

```python
def _int(name: str, default: int) -> int:
    raw = _str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(
            f"{name} must be a whole number, but got {raw!r}. "
            "Check the environment variable (or .env) that sets it."
        ) from exc
```

Environment variables are always strings. `CHUNK_SIZE=abc` should fail immediately
with a clear message, not surface as a `TypeError` deep inside the splitter twenty
seconds later.

### Catching unfilled placeholders

```python
_PLACEHOLDER_MARKERS = ("paste-your", "paste_your", "your-key-here", "changeme")

def _is_placeholder(value: str | None) -> bool:
    if not value or not value.strip():
        return True
    lowered = value.strip().lower()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)
```

Copying `.env.example` to `.env` and forgetting to edit it is extremely common. The
key is technically "set", so a naive check passes — and you get a confusing 401
from OpenAI instead of a useful message.

### The settings object

```python
@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    openai_model: str
    openai_temperature: float
    cohere_api_key: str
    cohere_embed_model: str
    langsmith_tracing: bool
    langsmith_api_key: str
    langsmith_project: str
    langsmith_endpoint: str
    database_url: str
    collection_name: str
    data_dir: Path
    chunk_size: int
    chunk_overlap: int
    retriever_k: int
    retriever_score_threshold: float
    warnings: list[str] = field(default_factory=list)
```

**`frozen=True` makes it immutable.** No part of the application can mutate
configuration at runtime — a shared mutable settings object is a debugging
nightmare. When Chapter 12 needs a per-request variation, it uses
`dataclasses.replace()` to make a copy rather than mutating the original.

### Masking the password in logs

```python
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
```

Every log line prints `devbrain:****@localhost:5433`. Small detail, but it means
terminal output can be shared without redaction.

### Fixing a URL beginners get wrong

```python
def _normalise_db_url(url: str) -> str:
    for legacy_prefix in ("postgresql+psycopg2://", "postgres://", "postgresql://"):
        if url.startswith(legacy_prefix):
            return "postgresql+psycopg://" + url[len(legacy_prefix):]
    return url
```

`langchain-postgres` needs the `postgresql+psycopg://` scheme (SQLAlchemy plus
psycopg 3). Every other tool in the world uses plain `postgresql://`. Rather than
failing with an opaque SQLAlchemy driver error, we silently upgrade it.

**This is a small kindness that saves real time.** Look for opportunities like it.

### Validation with instructions

```python
if problems and not ENV_FILE_LOADED:
    problems.insert(0,
        "No configuration source was read. Supply these as application\n"
        "    (environment) variables — the recommended route:\n"
        "        $env:OPENAI_API_KEY = 'sk-...'      PowerShell, this session only\n"
        "        .\\set-env.ps1                       PowerShell, saved permanently\n"
        "        export OPENAI_API_KEY=sk-...        macOS / Linux\n"
        "    ...or, for local development only, copy .env.example to .env:\n"
        "        copy .env.example .env")
```

Note it tests `ENV_FILE_LOADED`, **not** whether a `.env` file exists on disk. Those
differ when `DEVBRAIN_IGNORE_ENV_FILE=1` is set, and the advice must describe what
the app actually consulted. That distinction was itself a bug — see Chapter 20.

### Interdependent values

```python
if chunk_overlap >= chunk_size:
    raise ConfigError(
        f"CHUNK_OVERLAP ({chunk_overlap}) must be smaller than "
        f"CHUNK_SIZE ({chunk_size}), otherwise chunking never advances."
    )
```

Each value is individually valid; the *combination* is not. Catch it here rather
than watching the splitter loop forever.

### Optional features degrade, they do not crash

```python
langsmith_tracing = _bool("LANGSMITH_TRACING", False)
langsmith_api_key = _str("LANGSMITH_API_KEY")
if langsmith_tracing and _is_placeholder(langsmith_api_key):
    langsmith_tracing = False
    langsmith_api_key = ""
    warnings.append(
        "LANGSMITH_TRACING is on but LANGSMITH_API_KEY is not set — "
        "tracing disabled. The app works normally without it."
    )
```

Tracing is a debugging aid. Refusing to start because it is misconfigured would be
disproportionate. So we disable it, record a warning the UI and logs can show, and
carry on.

**The distinction to internalise:** a missing OpenAI key is fatal, because nothing
works without it. A missing LangSmith key is a warning, because everything works
without it. Classify every optional dependency this way.

---

## 8.6 Turning on tracing

```python
def configure_langsmith(settings: Settings) -> None:
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
```

This is the *entire* tracing integration. Set environment variables; LangChain
picks them up internally. Chapter 17 explains the mechanism.

Both `LANGSMITH_*` and legacy `LANGCHAIN_*` names are set, because different
library versions read different ones. Belt and braces, cheaply.

---

## 8.7 The public entry point

```python
def load_settings(exit_on_error: bool = False) -> Settings:
    try:
        settings = _build_settings()
    except ConfigError as exc:
        if exit_on_error:
            print(f"\n[X] Configuration error\n\n{exc}\n", file=sys.stderr)
            raise SystemExit(1) from exc
        raise

    configure_langsmith(settings)
    return settings
```

`exit_on_error=True` is used by the CLI so a misconfiguration prints a friendly
message instead of a traceback. The web app passes `False` and catches the error
itself, so it can still start and *explain* the problem through `/api/health`.

Same function, two failure styles, chosen by the caller.

---

## 8.8 Checkpoint

**1. Configuration loads**

```bash
python -c "from config import load_settings; s = load_settings(); print('OK:', s.safe_database_url)"
```

Expected:

```
OK: postgresql+psycopg://devbrain:****@localhost:5433/devbrain
```

**2. Provenance reporting works**

```bash
python -c "from config import config_sources; import json; print(json.dumps(config_sources(), indent=2))"
```

Every setting maps to `environment`, `.env` or `default` — and no values appear.

**3. Environment beats `.env`**

```powershell
$env:OPENAI_MODEL = "gpt-4o"
python -c "from config import load_settings; print(load_settings().openai_model)"
```

Prints `gpt-4o` even if `.env` says `gpt-4o-mini`.

**4. Missing configuration fails usefully**

Temporarily rename `.env`, unset the variables, and run check 1. You should get
instructions, not a traceback.

These four were all verified during development, along with an alternate-file test
and a check that `config_sources()` leaks no secret values — twenty checks in total.

---

## 8.9 The LangSmith 403 story

Worth reading, because the debugging method generalises.

Symptom: the app worked perfectly, but no traces appeared. The server log showed:

```
WARNING langsmith.client  Failed to send compressed multipart ingest:
  403 Client Error: Forbidden for url: https://api.smith.langchain.com/runs/multipart
```

**403 Forbidden**, not 401 Unauthorized. That distinction usually means "recognised
but not permitted" — so the natural theory was a quota or permission problem.

The decisive test was comparing the real key against a deliberately fake one:

| Key | Response |
|---|---|
| Real key | 403 Forbidden |
| Obviously fake key | 403 Forbidden |
| Malformed string | 403 Forbidden |

**Identical.** So the key was not being recognised at all — ruling out quota
entirely.

That reframed the question from *"what permission is missing?"* to *"why is this
key unknown?"*, and the answer was region: the account was on LangSmith's **EU**
instance. Against `https://eu.api.smith.langchain.com` the same key returned
**200 OK**.

Two transferable lessons:

**Compare against a known-bad input.** If your real credential and a fake one
produce identical responses, the problem is recognition, not permission.

**Failures that look like auth problems are often routing problems.** Region,
endpoint, tenant.

The fix was one line, and `.env.example` now carries a prominent warning so the next
person loses minutes rather than an hour.

---

## 8.10 Mistakes to avoid

**Committing `.env`.** Chapter 6 put it in `.gitignore` first for this reason.

**Wrapping values in quotes in `.env`.** `OPENAI_API_KEY="sk-..."` may include the
quotes in the value. No quotes, no spaces around `=`.

**Naming the file `.env.txt`.** Windows Explorer does this silently. VS Code shows
the real name in the tab — check there.

**Changing `COHERE_EMBED_MODEL` without re-ingesting.** Chapter 3's warning, now
operational. Existing vectors become incomparable.

**Setting `LANGSMITH_ENDPOINT` to the wrong region.** See §8.9.

**Using `override=True` in `load_dotenv`.** Reverses the precedence and makes
container deployment mysterious.

---

## 8.11 What you have

A single source of truth, read from environment variables, with `.env` as a
fallback that can never override them. Validation that fails early with
instructions. Provenance reporting that answers "which key is it using?" without
exposing anything. Optional features that degrade rather than crash.

Every subsequent file imports this one. Part III now uses it to build the write
path — starting with turning files into text.

*Next: [Chapter 9 — Document Loaders](ch09-document-loaders.md)*

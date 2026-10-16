# Chapter 6 — The Development Environment

*© 2026 Shailendra Srivastava. All rights reserved.*

---

## 6.1 What we are setting up, and why in this order

Part I was theory. From here you build. This chapter creates the skeleton every
later chapter fills in:

1. A project folder with a deliberate structure
2. An isolated Python environment
3. A pinned dependency set that actually installs together
4. A `.gitignore` that protects your secrets before you have any

That last point is the reason for the ordering. **Write `.gitignore` before you
write `.env`.** Once a secret has been committed, removing it from the working
tree does not remove it from history. Getting the guard in place first costs
nothing; getting it wrong costs a key rotation.

---

## 6.2 Prerequisites

| Tool | Version | Check |
|---|---|---|
| Python | 3.10 or newer | `python --version` |
| Docker Desktop | Any recent | `docker ps` |
| Git | Any | `git --version` |
| A code editor | VS Code recommended | — |

This book was written and tested on **Python 3.13.5** and **Docker 29.7.2** on
Windows 11. Everything works on macOS and Linux; where commands differ, both are
given.

> **Docker Desktop must actually be running**, not merely installed. If `docker ps`
> reports *"cannot find the file specified"* or *"the daemon is not running"*,
> launch Docker Desktop and wait for the whale icon to stop animating.

---

## 6.3 The project folder

```bash
mkdir DevBrain
cd DevBrain
mkdir data
```

That is the whole structure for now. Files get added as you build them. For
reference, here is where you are heading:

```
DevBrain/
├── config.py            Ch 8    settings and validation
├── loaders.py           Ch 9    files → Documents
├── ingest.py            Ch 10   the write path
├── rag.py               Ch 11   prompt and chain
├── db.py                Ch 12   plain SQL helpers
├── app.py               Ch 12   the web API
├── auth.py              Ch 14   users and sessions
├── index.html           Ch 13   the frontend
├── docker-compose.yml   Ch 7    Postgres + pgAdmin
├── Dockerfile           Ch 18   production image
├── requirements.txt     Ch 6    ← this chapter
├── .gitignore           Ch 6    ← this chapter
├── .env.example         Ch 8    config template
└── data/                        your documents
```

**Flat, not nested.** Ten Python files in one folder is unfashionable, but for a
project this size it is genuinely easier to navigate than `src/devbrain/core/…`.
Every import is a plain `import config`. Reach for packages when a folder gets
unwieldy — not before.

---

## 6.4 The virtual environment

A **virtual environment** is a private copy of Python for one project. Without it,
`pip install` puts packages into your system Python, where two projects wanting
different versions of the same library will fight.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

<details>
<summary>macOS / Linux</summary>

```bash
python3 -m venv .venv
source .venv/bin/activate
```

</details>

Your prompt now starts with `(.venv)`.

> **That prefix must be there every time you run a DevBrain command.** If you open
> a new terminal, re-run the activate line. Forgetting this is the cause of about
> half of all `ModuleNotFoundError` reports.

**PowerShell refusing to run the script?** If you see *"running scripts is disabled
on this system"*, run this once:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

That allows locally-created scripts while still requiring signatures for
downloaded ones. It is a reasonable default, not a security hole.

---

## 6.5 The dependencies

Create `requirements.txt`:

```python
# DevBrain dependencies — exact pins, verified together on Python 3.13.
# Install with:  pip install -r requirements.txt

# --- Web API ---
fastapi==0.141.1
uvicorn[standard]==0.52.4
python-dotenv==1.2.3
python-multipart==0.0.20         # required by FastAPI to parse file uploads

# --- LangChain core + integrations ---
langchain-core==1.6.1
langchain-text-splitters==1.1.2
langchain-openai==1.6.0          # LLM: answer generation
langchain-cohere==0.6.0          # Embeddings: text -> vectors
langchain-postgres==0.0.17       # Vector store: pgvector-backed

# --- Authentication ---
bcrypt==4.3.0                    # password hashing for user accounts

# --- Observability ---
langsmith==0.11.1

# --- PostgreSQL driver (binary wheel: no compiler needed) + SQL toolkit ---
psycopg[binary]==3.3.4
SQLAlchemy==2.0.52               # used directly by db.py for health/stats queries

# --- Document parsing ---
pypdf==6.16.2                    # .pdf
docx2txt==0.9                    # .docx
openpyxl==3.1.5                  # .xlsx
```

Install:

```bash
pip install -r requirements.txt
```

---

## 6.6 Why every one of these is here

You should never install a dependency you cannot justify. Here is the justification
for each.

### Web layer

**`fastapi`** — the HTTP framework. Chosen over Flask for automatic request
validation (Pydantic), automatic OpenAPI docs at `/docs`, and native async support.
Chapter 12 uses all three.

**`uvicorn[standard]`** — the ASGI server that actually runs FastAPI. The
`[standard]` extra adds faster event-loop and HTTP parsing implementations.

**`python-dotenv`** — reads a `.env` file into the environment. Chapter 8 explains
why it is a *convenience*, not the primary configuration mechanism.

**`python-multipart`** — FastAPI cannot parse file uploads without it. Omit it and
`/api/upload` raises an error at import time telling you to install it. Chapter 16
depends on this.

### LangChain

**`langchain-core`** — the `Document` type, prompt templates, output parsers, and
the `Runnable` interface that LCEL is built on. This is the piece that genuinely
earns its place.

**`langchain-text-splitters`** — `RecursiveCharacterTextSplitter` from Chapter 5.
Separately packaged, so you take the splitter without the rest of LangChain.

**`langchain-openai`, `langchain-cohere`, `langchain-postgres`** — vendor
integrations. The Cohere one matters more than it looks: it sets the
`search_document` / `search_query` input types automatically (Chapter 3), which is
easy to forget and quietly degrades retrieval when you do.

> **Notably absent: `langchain-community`.** It is being sunset and emits
> deprecation warnings, and its Excel loader drags in the heavy `unstructured`
> package. Chapter 9 writes three small loaders directly against `pypdf`,
> `docx2txt` and `openpyxl` instead — fewer dependencies, better error messages,
> and full control over the spreadsheet flattening that turns out to matter a lot.

### Data layer

**`psycopg[binary]`** — the PostgreSQL driver. The `[binary]` extra ships a
pre-compiled wheel, so no C compiler is needed on Windows. Without it, `pip
install` tries to build from source and fails on most machines.

**`SQLAlchemy`** — `langchain-postgres` uses it internally, but DevBrain also uses
it *directly* in `db.py` for health checks and statistics. Since we import it
ourselves, we pin it ourselves rather than relying on a transitive dependency.

### Everything else

**`bcrypt`** — password hashing (Chapter 14). Deliberately slow, which is the point.

**`langsmith`** — tracing (Chapter 17). Optional at runtime; the app works with
tracing disabled.

**`pypdf` / `docx2txt` / `openpyxl`** — the entire document extraction stack. Three
small, local, pure-Python-ish libraries. No cloud OCR service, no API keys, nothing
leaves your machine during parsing.

---

## 6.7 Why exact pins

Every line uses `==`, not `>=`. This is deliberate.

**`>=` means "whatever is newest today".** Two people installing the same project a
month apart get different dependency trees. A breaking change in any of fourteen
packages becomes your problem at an unpredictable moment.

**`==` means "the versions I actually tested together".** Every version in that file
was installed as a set and verified working on Python 3.13. That reproducibility is
worth far more than automatically receiving updates.

The trade-off is real: you must update deliberately rather than automatically. For
a project meant to be rebuilt from a book, reproducibility wins easily.

> **A caution about LangChain specifically.** LangChain 1.x reorganised things
> significantly — `langchain.chains` helpers many tutorials still use have moved or
> gone. This project deliberately builds its chain from `langchain_core` primitives
> (Chapter 11), which are far more stable across versions. If you find a tutorial
> whose imports do not work, check its LangChain version before assuming you made
> a mistake.

---

## 6.8 Protecting secrets first

Create `.gitignore` **now**, before any secret exists:

```gitignore
# ---- Secrets: never commit these ----
.env
.env.local
*.pem

# ---- Python ----
.venv/
venv/
env/
__pycache__/
*.py[cod]
*.egg-info/
.pytest_cache/
.ruff_cache/
.mypy_cache/

# ---- Your documents ----
# The sample files shipped with the project are committed. If your own notes
# are private, uncomment the next line to keep them out of git.
# data/

# ---- Editors / OS ----
.vscode/
.idea/
Thumbs.db
Desktop.ini
.DS_Store

# ---- Runtime junk ----
*.log
*.err
```

Three notes:

**`.env` is first, and it is the one that matters.** Everything else is tidiness.

**`.venv/` must be ignored.** It contains thousands of files and is entirely
reproducible from `requirements.txt`. Committing it is a classic beginner mistake.

**`data/` is commented out, not absent.** That is a decision you should make
consciously. If your documents are work-related or proprietary, uncomment it. The
comment exists so you actually think about it rather than defaulting either way.

---

## 6.9 Checkpoint

Verify each of these before continuing.

**1. Virtual environment is active**

```bash
python -c "import sys; print(sys.prefix)"
```

The path should end in `.venv`. If it points at your system Python, the
environment is not active.

**2. Dependencies installed together**

```bash
pip check
```

Should print `No broken requirements found.`

**3. Key imports resolve**

```bash
python -c "import fastapi, langchain_core, langchain_postgres, bcrypt, pypdf; print('all imports OK')"
```

**4. Docker is running**

```bash
docker ps
```

Should print a table header, not an error.

**5. Git will ignore your secrets**

```bash
git init
echo "test" > .env
git status --porcelain
```

`.env` must **not** appear in the output. Then:

```bash
rm .env
```

That last check is worth doing properly. It is the only one whose failure is
irreversible.

---

## 6.10 Mistakes to avoid

**Forgetting to activate the venv.** Symptom: `ModuleNotFoundError` for a package
you definitely installed. Look for `(.venv)` in your prompt.

**Installing `psycopg` without `[binary]`.** On Windows, pip tries to compile from
source and fails on missing `pg_config`. The `[binary]` extra avoids this entirely.

**Committing `.env` before writing `.gitignore`.** Git history is forever. If it
happens: rotate the keys immediately — rewriting history is not sufficient if
anyone has pulled.

**Using `>=` "to stay current".** You will spend an afternoon debugging a breaking
change you did not ask for. Pin, then upgrade on purpose.

**Adding `langchain` (the meta-package).** You do not need it. The three
integration packages plus `langchain-core` are sufficient, and pulling in the full
package adds a large dependency tree for features this project does not use.

---

## 6.11 What you have

A folder, an isolated interpreter, fourteen pinned packages that install cleanly
together, and a `.gitignore` standing guard.

No application code yet — that starts in Chapter 8. Chapter 7 first gets
PostgreSQL running, because `config.py` needs something to point at.

*Next: [Chapter 7 — Docker: PostgreSQL and pgAdmin](ch07-docker-postgres.md)*

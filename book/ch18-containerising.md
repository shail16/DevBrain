# Chapter 18 — Containerising the Application

---

## 18.1 Two topologies, one codebase

Until now, Postgres ran in Docker and Python ran on your machine. That is the right
setup for development — hot reload, a debugger, instant edits.

For a server you want everything containerised. DevBrain supports both from one
`docker-compose.yml`, and the application code is **byte-identical** between them.

```
MODE A — Development                  MODE B/C — Containerised
┌────────────────────────┐            ┌──────────────────────────────────┐
│ uvicorn --reload       │            │  Docker network                  │
│   on your host         │            │  ┌────────────┐  ┌────────────┐  │
│        │               │            │  │ devbrain-  │  │ devbrain-  │  │
│        │ localhost:5433│            │  │    app     │→ │  postgres  │  │
│        ▼               │            │  └────────────┘  └────────────┘  │
│ ┌────────────────┐     │            │        ↑  postgres:5432          │
│ │devbrain-postgres│    │            └────────┼─────────────────────────┘
│ └────────────────┘     │                     │ localhost:8001
└────────────────────────┘                  browser
```

**Only two environment variables differ**: the database host and the bind address.
That is not an accident — it is the entire point of reading configuration from the
environment (Chapter 8).

---

## 18.2 The `localhost` trap

This is the mistake everyone makes, and it is worth understanding properly rather
than just memorising the fix.

Every container gets its own **network namespace**, which means its own loopback
interface. When code inside `devbrain-app` connects to `localhost:5433`, it is
asking *itself* for port 5433. Nothing is listening there. Connection refused.

Two distinct address spaces exist:

| Address | Meaning | Valid from |
|---|---|---|
| `localhost:5433` | The **published** port, mapped by `ports:` onto your host | Your host only |
| `postgres:5432` | The **service name** on the Compose network, at the container's real port | Other containers only |

Note that **the port changes too.** The `5433` is purely a host-side mapping,
invented in Chapter 7 to avoid clashing with other Postgres instances. Inside the
network, Postgres listens on its normal `5432`.

Compose runs an embedded DNS server that resolves service names to container IPs,
so `postgres` is a real hostname inside that network.

---

## 18.3 The Dockerfile

```dockerfile
# -----------------------------------------------------------------------------
# Stage 1 — builder: install dependencies into a self-contained virtualenv
# -----------------------------------------------------------------------------
FROM python:3.13-slim AS builder

WORKDIR /build

# Copy ONLY requirements.txt first. Docker caches each instruction as a layer,
# so as long as this file is unchanged the (slow) pip install below is reused
# even when application code changes.
COPY requirements.txt .

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt


# -----------------------------------------------------------------------------
# Stage 2 — runtime: slim image containing only the venv and the app
# -----------------------------------------------------------------------------
FROM python:3.13-slim AS runtime

# Run as a non-root user.
RUN groupadd --system devbrain \
 && useradd --system --gid devbrain --home-dir /app --shell /usr/sbin/nologin devbrain

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    API_PORT=8001

WORKDIR /app

COPY --chown=devbrain:devbrain *.py index.html ./

RUN mkdir -p /app/data && chown devbrain:devbrain /app/data

USER devbrain

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/api/health', timeout=4)"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "2"]
```

---

## 18.4 Walkthrough

### Why two stages

Only the finished virtualenv crosses the stage boundary. Everything used to *build*
it — compilers, headers, pip's cache — is discarded with the builder stage.

The runtime image ends up around **600 MB** rather than well over a gigabyte, and
it carries a smaller attack surface because there is no toolchain inside it to
abuse.

### Why not Alpine

`python:3.13-slim`, not `python:3.13-alpine`.

Alpine uses **musl libc** rather than glibc. Most Python packages ship
`manylinux` wheels built against glibc, so on Alpine pip falls back to compiling
from source. Builds become dramatically slower, some packages fail outright, and
the runtime is occasionally *slower* too.

The image-size saving is real but modest. For Python, `-slim` is the right default.

### Layer caching — instruction order

```dockerfile
COPY requirements.txt .              # changes rarely
RUN pip install -r requirements.txt  # slow, gets cached
COPY *.py index.html ./              # changes constantly
```

Docker caches each instruction as a layer and invalidates **every layer after the
first change it detects**.

Copying source code first would invalidate the dependency layer on every edit,
forcing a full reinstall each rebuild. Ordered this way, editing `app.py` rebuilds
in seconds.

> **The general rule: order instructions from least- to most-frequently-changed.**

### Why non-root

```dockerfile
RUN groupadd --system devbrain && useradd --system --gid devbrain ... devbrain
USER devbrain
```

By default a container process runs as root, and container root maps to meaningful
privilege on the host in several escape scenarios.

Running as an unprivileged user means a compromise through the application lands
somewhere that cannot write to system paths or install anything.

Note `--shell /usr/sbin/nologin` — the account cannot be used for an interactive
shell even if someone finds a way to invoke one.

The `data` directory needs explicit ownership because uploads (Chapter 16) write
there as this user:

```dockerfile
RUN mkdir -p /app/data && chown devbrain:devbrain /app/data
```

### The environment variables

| Variable | Why |
|---|---|
| `PATH="/opt/venv/bin:$PATH"` | Makes the venv's Python the default — no activation needed |
| `PYTHONUNBUFFERED=1` | Streams logs immediately so `docker logs` is live |
| `PYTHONDONTWRITEBYTECODE=1` | No `.pyc` clutter in the image |

`PYTHONUNBUFFERED` matters more than it looks. Without it, Python buffers stdout
when it is not a terminal, and your logs appear in delayed chunks — or not at all
if the container crashes.

### The healthcheck

```dockerfile
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/api/health', timeout=4)"
```

`urlopen` raises on any non-2xx status. Since `/api/health` returns **503** when
Postgres is unreachable (Chapter 12), the container is correctly marked unhealthy —
the signal is honest, not merely *"the process is alive"*.

`--start-period=25s` gives the app a grace window where failing checks do not count
against the retry budget.

No `curl` is needed — Python is already in the image.

### The command

```dockerfile
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "2"]
```

**`--host 0.0.0.0`, not `127.0.0.1`.** A container binding loopback is unreachable
from outside itself. This is the second half of the `localhost` confusion — and
getting it wrong produces a container that starts perfectly and refuses every
connection.

**No `--reload`.** It watches the filesystem and roughly doubles memory use.
Development only.

**`--workers 2`** runs two processes. Raise it for more traffic; Chapter 19 covers
scaling.

---

## 18.5 The `.dockerignore`

```gitignore
# ---- Secrets: never bake these into an image ----
.env
.env.local
*.pem

# ---- Virtualenvs: the image builds its own ----
.venv/

# ---- Python noise ----
__pycache__/
*.py[cod]

# ---- Git, editors, docs ----
.git/
.vscode/
*.md

# ---- Your documents: mounted at runtime, not copied ----
data/
```

Two entries carry real weight.

**`.env` is excluded on purpose.** Secrets in an image are **permanent**: they live
in a layer, survive being "deleted" by a later instruction, and travel to every
registry the image is pushed to. Configuration arrives at *run* time instead.

**`.venv/` must be excluded.** Copying your host venv into the image would ship
Windows binaries into a Linux container — and it is large.

Excluding `data/` also keeps builds fast; the documents are mounted as a volume.

---

## 18.6 The app service

```yaml
  app:
    profiles: ["full"]
    build:
      context: .
      dockerfile: Dockerfile
    image: devbrain-app:latest
    container_name: devbrain-app
    restart: unless-stopped

    env_file:
      - path: .env
        required: false

    environment:
      # Bare NAME (no "=") means: pass through from the host environment if set,
      # otherwise leave it alone so the env_file value stands.
      - OPENAI_API_KEY
      - COHERE_API_KEY
      - LANGSMITH_API_KEY
      - ADMIN_PASSWORD
      # ... and the rest ...

      # Pinned with "=" so it always overrides whatever the host or .env says.
      - DATABASE_URL=postgresql+psycopg://devbrain:devbrain_pass@postgres:5432/devbrain
      - API_PORT=8001

    ports:
      - "${API_HOST_PORT:-8001}:8001"

    volumes:
      - ./data:/app/data

    depends_on:
      postgres:
        condition: service_healthy
```

### The profile

```yaml
profiles: ["full"]
```

A service with a profile is **skipped** unless that profile is requested:

```bash
docker compose up -d                  # database only — development
docker compose --profile full up -d   # everything — containerised
```

One file describes both topologies, with no duplication and no second compose file
to keep in sync. The default stays the fast development loop, because that is what
you use ninety-nine times out of a hundred.

### Two forms of `environment:`

This distinction is subtle and important.

| Form | Behaviour |
|---|---|
| `- OPENAI_API_KEY` | **Pass through** from host if set; omit if not |
| `- API_PORT=8001` | **Pin** to this value always |

The bare-name form is what makes `$env:OPENAI_API_KEY = "..."` in your shell win
over whatever is in `.env`. If the variable is unset on the host, Compose omits it
entirely — so the `env_file` value stands.

> **Avoid `- OPENAI_API_KEY=${OPENAI_API_KEY}`.** When the variable is unset, that
> form sets it to an *empty string* inside the container, clobbering the `env_file`
> value. The bare name is the correct idiom.

`DATABASE_URL` is pinned with `=` because the container must *always* use
`postgres:5432`, regardless of what the host says.

### Optional env file

```yaml
env_file:
  - path: .env
    required: false
```

The stack starts with no `.env` at all — which is how you run it on a server:
export the variables (or use your platform's secret store) and skip the file.

### Waiting properly

```yaml
depends_on:
  postgres:
    condition: service_healthy
```

Chapter 7 built the healthcheck; this consumes it. Without `condition`,
`depends_on` waits only for the container to *start*, and the app would attempt its
first connection before Postgres could serve queries.

---

## 18.7 Running it

```bash
docker compose --profile full up -d --build
```

One command: builds the image, starts Postgres, waits for it to become healthy,
then starts the app.

```bash
docker compose --profile full ps
```

```
NAME                STATUS                   PORTS
devbrain-app        Up 6 seconds (healthy)   0.0.0.0:8001->8001/tcp
devbrain-pgadmin    Up 2 minutes             0.0.0.0:5051->80/tcp
devbrain-postgres   Up 2 minutes (healthy)   0.0.0.0:5433->5432/tcp
```

Verified: the app reached healthy in about **9 seconds**.

### Ingesting inside the container

`data/` is mounted, so run ingestion *in* the container:

```bash
docker compose --profile full run --rm app python ingest.py
docker compose --profile full run --rm app python ingest.py --reset
docker compose --profile full run --rm app python ingest.py --stats
```

`run --rm` starts a throwaway container, runs the command, deletes it. The running
web app is untouched — just refresh the browser.

Verified output shows it using the internal address:

```
Data folder      : /app/data
Database         : postgresql+psycopg://devbrain:****@postgres:5432/devbrain
Chunks           : 19
```

### Everyday commands

```bash
docker compose --profile full logs -f app        # follow app logs
docker compose --profile full restart app        # restart just the app
docker compose --profile full up -d --build app  # rebuild after a code change
docker compose --profile full down               # stop everything (keeps data)
```

---

## 18.8 A configuration bug that blocked containerisation

Containerising surfaced a genuine defect in `config.py`.

The original validation was:

```python
if not (PROJECT_ROOT / ".env").exists():
    problems.append("No .env file found. Copy .env.example to .env...")
```

A missing `.env` was a **hard error** — even when every key was supplied as a real
environment variable.

**That made the application impossible to containerise.** In Docker, Kubernetes and
CI, configuration arrives as environment variables and no `.env` file exists at all.
The app would refuse to start despite being perfectly configured.

The fix reorders the logic: check the keys first, and only mention `.env` when
something is *actually* unset.

```python
if problems and not ENV_FILE_LOADED:
    problems.insert(0, "No configuration source was read. Supply these as "
                       "application (environment) variables...")
```

Note it tests `ENV_FILE_LOADED`, **not** file existence — those differ when
`DEVBRAIN_IGNORE_ENV_FILE=1` is set, and the advice must describe what the app
actually consulted.

Verified across three scenarios:

| Scenario | Before | After |
|---|---|---|
| `.env` present | works | works |
| No `.env`, env vars set | **crashed** | works |
| No `.env`, nothing set | error | error, now mentioning env vars |

> **The lesson:** a validation rule that assumes a *mechanism* rather than an
> *outcome* will eventually block a legitimate deployment. Validate that the value
> exists, not that it arrived by your favourite route.

---

## 18.9 Where state actually lives

| What | Where | If you lose it |
|---|---|---|
| Vectors and chunks | Docker volume `devbrain_pgdata` | Rebuild: `ingest.py --reset` |
| Users and sessions | Same volume | **Gone** — accounts must be recreated |
| Source documents | `./data` on the host | **Gone** — back this up |
| Secrets | Environment / `.env` | **Gone** — back this up |
| Application code | The image, rebuilt from git | Rebuild |

Only two of those are truly irreplaceable, and **the vector database is not one of
them.** As long as you hold `data/` and your API keys, the entire knowledge base is
reproducible with one command.

That is a deliberate property. It makes the deployment far less precious than it
first appears — and it means a botched upgrade is a rebuild, not a disaster.

(Users and sessions *are* in the database, so once you have real accounts, back it
up — Chapter 19 covers `pg_dump`.)

---

## 18.10 Checkpoint

**1. Build and run**

```bash
docker compose --profile full up -d --build
```

**2. Confirm healthy**

```bash
docker compose --profile full ps
```

`devbrain-app` should reach `(healthy)` within ~30 seconds.

**3. Confirm the data mount is writable**

```bash
docker compose --profile full exec -T app sh -c \
  "mount | grep '/app/data' | sed 's/,.*//' ; touch /app/data/.wtest && echo 'WRITE OK' && rm /app/data/.wtest"
```

Must print `WRITE OK` — otherwise uploads will fail (Chapter 16).

**4. End-to-end through the container**

Sign in, ask a question, upload a file. Verified from the project: a file uploaded
through the containerised app, written by the non-root user, landed on the host
disk and was answerable seconds later.

**5. Prove no secret is in the image**

```bash
docker run --rm devbrain-app:latest ls -la /app
```

You should see the `.py` files and `index.html` — **no `.env`**.

**6. Confirm the internal address is used**

```bash
docker compose --profile full logs app | grep Database
```

```
Database        : postgresql+psycopg://devbrain:****@postgres:5432/devbrain
```

`postgres:5432`, not `localhost:5433`.

---

## 18.11 Mistakes to avoid

**Using `localhost` inside a container.** The defining mistake. Use the service name.

**Binding uvicorn to `127.0.0.1` in a container.** Starts fine, refuses every
connection.

**Copying source before installing dependencies.** Destroys layer caching.

**Baking `.env` into the image.** Permanent, and it travels to every registry.

**Copying `.venv` into the image.** Wrong platform binaries, and large.

**Using Alpine for Python.** Slower builds, occasional outright failures.

**Running as root.** Unnecessary privilege.

**Forgetting `PYTHONUNBUFFERED=1`.** Logs arrive late or not at all.

**Leaving `data/` read-only.** Uploads fail only in the container.

**`- VAR=${VAR}` in a Compose environment list.** Clobbers `env_file` with an empty
string when unset. Use the bare name.

---

## 18.12 What you have

A production image: multi-stage, ~600 MB, non-root, cache-friendly, with an honest
healthcheck. A Compose profile that adds the app without disturbing the development
loop. Configuration injected at runtime with no secrets in the image. And a
validation bug fixed that would otherwise have made containerisation impossible.

Next: putting it on a server safely.

*Next: [Chapter 19 — Deployment and Hardening](ch19-deployment.md)*

# Chapter 19 — Deployment and Hardening

*© 2026 Shailendra Srivastava. All rights reserved.*

---

## 19.1 The one thing to understand first

> **DevBrain ships with no authorisation boundary at the network edge.**

It has user accounts (Chapter 14), but it speaks **plain HTTP**, its CORS policy is
`allow_origins=["*"]`, its database password is committed to the repository, and
pgAdmin runs with no login.

Every one of those is a reasonable choice for a tool on `localhost`. Every one is
wrong the moment the service is reachable from a network.

This chapter is about closing that gap. Read it before exposing anything.

---

## 19.2 Prepare the host

Any Linux box with Docker Engine and Compose v2. 2 GB RAM is enough; 4 GB is
comfortable.

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
sudo usermod -aG docker $USER   # log out and back in
```

Copy the project across:

```bash
git clone <your-repo> devbrain && cd devbrain
```

**Do not copy `.env`.** Create it — or better, set real environment variables — on
the server.

---

## 19.3 Configuration for a server

```bash
cp .env.example .env
nano .env
chmod 600 .env
```

Change these from the defaults:

```ini
# Real keys
OPENAI_API_KEY=sk-...
COHERE_API_KEY=...
LANGSMITH_API_KEY=lsv2_pt_...

# Keep prod traces separate from your experiments
LANGSMITH_PROJECT=devbrain-prod
LANGSMITH_ENDPOINT=https://eu.api.smith.langchain.com   # match YOUR region

# A real password — see §19.4
DATABASE_URL=postgresql+psycopg://devbrain:CHANGE_THIS@postgres:5432/devbrain

# Do not expose the database or admin console to the internet
POSTGRES_HOST_PORT=127.0.0.1:5433
PGADMIN_HOST_PORT=127.0.0.1:5051
API_HOST_PORT=127.0.0.1:8001

# Change the seeded administrator password
ADMIN_PASSWORD=<something long and random>
```

Two things to notice:

**`DATABASE_URL` uses `postgres:5432`**, not `localhost:5433`. On a server the app
only ever runs in a container (Chapter 18).

**Ports are bound to `127.0.0.1`.** Compose accepts `127.0.0.1:5433:5432`, which
publishes the port on loopback only. Without the prefix, Docker binds `0.0.0.0` and
**punches through the host firewall** — your database becomes internet-reachable.
This surprises people regularly.

`chmod 600` means only your user can read the file.

---

## 19.4 Change the database password

The committed default `devbrain_pass` is published in the repository. On a server
that is not a password, it is a formality.

Edit `docker-compose.yml`:

```yaml
environment:
  POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in .env}
```

Generate one and set it in **both** places:

```bash
openssl rand -base64 24
```

```ini
POSTGRES_PASSWORD=<generated>
DATABASE_URL=postgresql+psycopg://devbrain:<same value>@postgres:5432/devbrain
```

The `:?` syntax makes Compose **refuse to start** if the variable is unset, so you
cannot accidentally deploy with a blank password. That is a small guard worth
copying into your own projects.

> If you have already started Postgres with the old password, the volume holds the
> old credentials. Either `docker compose down -v` (destroys data) or change it with
> `ALTER USER devbrain WITH PASSWORD '...'`.

---

## 19.5 Launch

```bash
docker compose --profile full up -d --build
docker compose --profile full ps                        # confirm (healthy)
docker compose --profile full run --rm app python ingest.py
```

Then check the log for the configuration banner — it tells you exactly which
settings came from where (Chapter 8):

```bash
docker compose --profile full logs app | head -20
```

---

## 19.6 HTTPS and access control

**Never expose port 8001 directly.**

DevBrain speaks plain HTTP. Over the internet that means session cookies, passwords
and your documents all travel in clear text — and the `secure=False` cookie flag
(Chapter 14) means the browser will happily send the session over HTTP.

Bind the app to loopback and terminate TLS in front of it:

```ini
API_HOST_PORT=127.0.0.1:8001
```

Caddy is the shortest path — it obtains and renews certificates automatically:

```
devbrain.example.com {
    reverse_proxy 127.0.0.1:8001
    basicauth {
        yourname <bcrypt hash from: caddy hash-password>
    }
}
```

That `basicauth` block is a second, independent gate in front of DevBrain's own
login. Defence in depth: even a bug in the application's auth does not expose the
service to the open internet.

### Then tighten the app itself

Two changes belong with this:

```python
# app.py — the cookie must not travel over plain HTTP
secure=True,
```

```python
# app.py — stop allowing every origin
allow_origins=["https://devbrain.example.com"],
```

The permissive CORS policy existed so `index.html` could be opened from disk
(Chapter 12). On a server that convenience is not worth the exposure.

---

## 19.7 Backups

Everything irreplaceable is in one volume plus two paths.

```bash
# Database — vectors, users, sessions, document registry
docker exec devbrain-postgres pg_dump -U devbrain devbrain | gzip > backup-$(date +%F).sql.gz

# Restore
gunzip -c backup-2026-08-28.sql.gz | docker exec -i devbrain-postgres psql -U devbrain -d devbrain
```

Also back up:

| What | Why |
|---|---|
| `data/` | Your source documents — **not reproducible** |
| `.env` | Secrets — **not reproducible** |

Chapter 18 made the point worth repeating: **the vector database is reproducible.**
If you hold `data/` and your keys, `python ingest.py --reset` rebuilds every
embedding.

What is *not* reproducible from those is user accounts. Once you have real users,
the database matters.

> **A backup you have never restored is not a backup.** Test the restore path on a
> throwaway container before you need it.

A minimal cron entry:

```cron
0 2 * * * cd /home/you/devbrain && docker exec devbrain-postgres pg_dump -U devbrain devbrain | gzip > backups/db-$(date +\%F).sql.gz
```

---

## 19.8 Updating and rolling back

```bash
git pull
docker compose --profile full up -d --build app
```

Only the app is rebuilt. Postgres keeps running; your vectors are untouched.

If a change affects chunking or the embedding model, re-ingest:

```bash
docker compose --profile full run --rm app python ingest.py --reset
```

**Rollback:**

```bash
git checkout <previous-commit>
docker compose --profile full up -d --build app
```

Or keep tagged images (`devbrain-app:2026-08-28`) and re-point the service — faster,
since nothing rebuilds.

---

## 19.9 The hardening checklist

Work through this before letting anyone else reach the service.

| | Item | Why |
|---|---|---|
| ☐ | Changed `POSTGRES_PASSWORD` from the default | The default is published in this repo |
| ☐ | Changed `ADMIN_PASSWORD` from the default | Same |
| ☐ | `.env` is `chmod 600` and not in git | It holds three API keys and the admin password |
| ☐ | Postgres bound to `127.0.0.1:5433` | Never expose a database to the internet |
| ☐ | pgAdmin bound to loopback, or not deployed | It is an admin console with no login |
| ☐ | HTTPS terminated by a reverse proxy | The app speaks plain HTTP |
| ☐ | `secure=True` on the session cookie | Otherwise it travels over HTTP |
| ☐ | CORS restricted to your domain | `["*"]` is a localhost convenience |
| ☐ | Some access control in front (basicauth, SSO) | Defence in depth |
| ☐ | Automated `pg_dump` backups, **restore tested** | An untested backup is not a backup |
| ☐ | Spending limits set on OpenAI and Cohere | Caps the damage if the URL leaks |
| ☐ | `LANGSMITH_PROJECT` differs from dev | Keeps prod traces clean |
| ☐ | Logs checked after deploy | Catches config errors immediately |

### The single most important line

> **DevBrain has no rate limiting and no spending cap of its own.** If the URL
> leaks, someone can ask it thousands of questions on your OpenAI account. Set
> hard spending limits in the provider dashboards — that is the only control that
> cannot be bypassed by a bug in your code.

---

## 19.10 Scaling, when you need it

Do not do any of this pre-emptively. Measure first.

| Symptom | Change |
|---|---|
| Slow under concurrent users | Raise `--workers` in the Dockerfile `CMD` (start with 4) |
| Retrieval slows past ~100k chunks | `CREATE INDEX ON langchain_pg_embedding USING hnsw (embedding vector_cosine_ops);` |
| Ingestion is slow | It is API-bound, not CPU-bound — batches are already at Cohere's 96 limit |
| Database is the bottleneck | Move Postgres to a managed service; point `DATABASE_URL` at it and drop the container |
| Uploads time out | Move ingestion to a background job (Chapter 16 §16.11) |

On workers: each is a separate process with its own connection pool. With
`pool_size: 5` and `max_overflow: 5` (Chapter 11), four workers can open up to 40
connections. Postgres defaults to 100 `max_connections` — so check that arithmetic
before raising workers significantly.

---

## 19.11 Complete settings reference

Every setting, and what it should be in each environment.

### API keys and models

| Setting | Development | Server | Notes |
|---|---|---|---|
| `OPENAI_API_KEY` | your key | your key | Injected at runtime, never in the image |
| `OPENAI_MODEL` | `gpt-4o-mini` | `gpt-4o-mini` | `gpt-4o` for harder reasoning, ~15× cost |
| `OPENAI_TEMPERATURE` | `0.0` | `0.0` | Keep at 0 for factual retrieval |
| `COHERE_API_KEY` | your key | your key | Used by both pipelines |
| `COHERE_EMBED_MODEL` | `embed-english-v3.0` | **must match dev** | Changing it requires `--reset` |

### LangSmith

| Setting | Development | Server |
|---|---|---|
| `LANGSMITH_TRACING` | `true` | `true` |
| `LANGSMITH_PROJECT` | `devbrain` | **`devbrain-prod`** |
| `LANGSMITH_ENDPOINT` | match your region | match your region |

### Database

| Setting | Development | Inside a container |
|---|---|---|
| `DATABASE_URL` host | `localhost:5433` | **`postgres:5432`** |
| `POSTGRES_HOST_PORT` | `5433` | `127.0.0.1:5433` |
| `POSTGRES_PASSWORD` | `devbrain_pass` | **generated** |

### Application

| Setting | Development | Containerised |
|---|---|---|
| `API_PORT` | `8001` | `8001` (inside) |
| `API_HOST_PORT` | n/a | `127.0.0.1:8001` |
| uvicorn host | `127.0.0.1` | **`0.0.0.0`** |
| `--reload` | **on** | **off** |
| `--workers` | 1 | 2–4 |

### Retrieval tuning

| Setting | Default | Effect |
|---|---|---|
| `CHUNK_SIZE` | `1000` | Smaller = sharper retrieval. Needs `--reset` |
| `CHUNK_OVERLAP` | `150` | Stops ideas being orphaned. Needs `--reset` |
| `RETRIEVER_K` | `4` | Chunks fed to the LLM per question |
| `RETRIEVER_SCORE_THRESHOLD` | `0.0` | `0.3` filters weak matches |

---

## 19.12 Mistakes to avoid

**Publishing a port without `127.0.0.1:`.** Docker bypasses the host firewall. Your
database becomes internet-reachable.

**Deploying with the default passwords.** Both are in this repository.

**Exposing port 8001 directly.** Plain HTTP, and `secure=False` on the cookie.

**Leaving CORS as `["*"]` on a server.**

**Copying `.env` from your laptop.** Create it on the server, `chmod 600`.

**Never testing the restore.** The most common backup failure.

**Deploying pgAdmin publicly.** An admin console with no login. Bind it to loopback
or leave it out entirely.

**No spending limits.** The one control that survives a bug in your own code.

---

## 19.13 What you have

A deployment path from laptop to server: host preparation, server-appropriate
configuration, a rotated database password enforced by Compose, HTTPS with an
independent access gate, tested backups, an update and rollback procedure, and a
thirteen-item checklist that names *why* each item matters.

One chapter left before the appendices — the bugs that shaped this project, and
what each one teaches.

*Next: [Chapter 20 — Bugs We Hit, and What They Taught](ch20-bugs.md)*

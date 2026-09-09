# Chapter 7 — Docker: PostgreSQL and pgAdmin

---

## 7.1 Why Docker

You need PostgreSQL 17 with the pgvector extension compiled in. You could install
that on your machine directly. You should not.

**Installing it natively means:** a system service you must maintain, a version
that may clash with another project, an extension you compile yourself, and setup
instructions that differ on every operating system.

**Running it in Docker means:** one file describes the whole thing, it behaves
identically on every machine, and deleting it is one command.

You need only three Docker concepts for this chapter.

| Concept | What it is |
|---|---|
| **Image** | A read-only template — "PostgreSQL 17 with pgvector installed" |
| **Container** | A running instance of an image |
| **Volume** | Storage that outlives the container, so data survives restarts |

The last one matters most. A container's own filesystem is disposable; anything you
want to keep goes in a volume.

---

## 7.2 The file

Create `docker-compose.yml`:

```yaml
services:
  postgres:
    # This image is plain PostgreSQL with the `pgvector` extension pre-compiled.
    # A normal `postgres:17` image would NOT be able to store embeddings.
    image: pgvector/pgvector:pg17
    container_name: devbrain-postgres
    restart: unless-stopped
    environment:
      POSTGRES_USER: devbrain
      POSTGRES_PASSWORD: devbrain_pass
      POSTGRES_DB: devbrain
      # Keeps text comparisons predictable across host machines.
      POSTGRES_INITDB_ARGS: "--encoding=UTF8"
    ports:
      # host:container — your Python code connects to localhost:5433 by default.
      # Only the host side (left) changes; inside the Docker network it stays 5432.
      - "${POSTGRES_HOST_PORT:-5433}:5432"
    volumes:
      # Named volume so your embeddings survive `docker compose down`.
      - devbrain_pgdata:/var/lib/postgresql/data
    healthcheck:
      # pgAdmin waits for this to pass before starting.
      test: ["CMD-SHELL", "pg_isready -U devbrain -d devbrain"]
      interval: 5s
      timeout: 5s
      retries: 20
      start_period: 10s

  pgadmin:
    image: dpage/pgadmin4:latest
    container_name: devbrain-pgadmin
    restart: unless-stopped
    environment:
      PGADMIN_DEFAULT_EMAIL: admin@devbrain.com
      PGADMIN_DEFAULT_PASSWORD: admin
      # Desktop mode: skips the login screen and the master-password prompt.
      PGADMIN_CONFIG_SERVER_MODE: "False"
      PGADMIN_CONFIG_MASTER_PASSWORD_REQUIRED: "False"
    ports:
      # Open http://localhost:5051 in your browser.
      - "${PGADMIN_HOST_PORT:-5051}:80"
    volumes:
      - devbrain_pgadmin:/var/lib/pgadmin
      # Pre-registers the DevBrain server so you don't have to add it by hand.
      - ./pgadmin/servers.json:/pgadmin4/servers.json:ro
    depends_on:
      postgres:
        condition: service_healthy

volumes:
  devbrain_pgdata:
  devbrain_pgadmin:
```

And `pgadmin/servers.json`:

```json
{
  "Servers": {
    "1": {
      "Name": "DevBrain Postgres",
      "Group": "Servers",
      "Host": "postgres",
      "Port": 5432,
      "MaintenanceDB": "devbrain",
      "Username": "devbrain",
      "SSLMode": "prefer",
      "Comment": "DevBrain vector store. Password: devbrain_pass"
    }
  }
}
```

---

## 7.3 Walkthrough

### The image choice

```yaml
image: pgvector/pgvector:pg17
```

Chapter 4 flagged this and it is worth repeating, because the failure is delayed
and confusing. The stock `postgres:17` image does **not** include pgvector.
Everything appears fine until ingestion runs `CREATE EXTENSION vector` and fails
with a message about a missing extension — which does not obviously point at your
image tag.

### Non-default ports

```yaml
- "${POSTGRES_HOST_PORT:-5433}:5432"
```

Three things are happening in that line.

**The mapping is `host:container`.** The left side is the port on *your* machine;
the right is the port *inside* the container. They need not match.

**DevBrain uses 5433, not the default 5432.** This is deliberate. Most developers
already have something on 5432 — another project, a native install. Using 5433
means DevBrain coexists rather than fighting. (This is not hypothetical: while
building this project, 5432, 5050 *and* 8000 were all already occupied.)

**`${VAR:-default}` is Compose substitution.** It reads `POSTGRES_HOST_PORT` from
your environment or a `.env` file, falling back to 5433. So the port is
configurable without editing this file.

> **If you change `POSTGRES_HOST_PORT`, change the port in `DATABASE_URL` too.**
> They must agree. Chapter 8 sets that up.

### Named volumes

```yaml
volumes:
  - devbrain_pgdata:/var/lib/postgresql/data
```

`/var/lib/postgresql/data` is where Postgres keeps everything. Mapping it to a
**named volume** (`devbrain_pgdata`) means Docker manages the storage, and it
survives the container being removed.

| Command | Container | Your data |
|---|---|---|
| `docker compose stop` | Stopped | Kept |
| `docker compose down` | Removed | **Kept** |
| `docker compose down -v` | Removed | **Deleted** |

That `-v` is the one to be careful with.

> **Never put a database's data directory on a bind mount** (`./pgdata:/var/lib/...`)
> on Windows or macOS. The filesystem translation layer is slow and, for Postgres,
> can corrupt data. Named volumes only.

I verified this survives: during development I stopped Docker Desktop entirely and
restarted it, and all 19 chunks were still there.

### The healthcheck

```yaml
healthcheck:
  test: ["CMD-SHELL", "pg_isready -U devbrain -d devbrain"]
  interval: 5s
  retries: 20
  start_period: 10s
```

This is more important than it looks.

**Postgres accepts TCP connections several seconds before it can actually serve
queries.** During startup it runs recovery, initialises the database on first boot,
and only then begins accepting real work.

`depends_on` alone only waits for a container to *start*. That is not the same as
*ready*. So the healthcheck runs `pg_isready` every 5 seconds, and pgAdmin declares:

```yaml
depends_on:
  postgres:
    condition: service_healthy
```

Now pgAdmin genuinely waits. Chapter 18 uses the same mechanism for the application
container, which matters far more — an app that starts before its database gets a
confusing connection error on first request.

`start_period: 10s` gives Postgres a grace window where failing checks do not count
against the retry budget, so first-boot initialisation is not mistaken for a crash.

### pgAdmin in desktop mode

```yaml
PGADMIN_CONFIG_SERVER_MODE: "False"
PGADMIN_CONFIG_MASTER_PASSWORD_REQUIRED: "False"
```

By default pgAdmin runs in *server mode*: a login screen plus a master password
protecting saved credentials. For a local single-user tool that is friction with no
benefit. Desktop mode drops both.

**This is a local-only choice.** Chapter 19 flags pgAdmin as something to bind to
loopback or not deploy at all on a server.

### Pre-registering the server

```yaml
- ./pgadmin/servers.json:/pgadmin4/servers.json:ro
```

Without this you would manually add a connection every time the pgAdmin volume is
recreated. The file pre-registers it. The `:ro` suffix mounts it read-only — pgAdmin
only needs to read it, so we do not grant more than that.

Note the host inside that file:

```json
"Host": "postgres",
"Port": 5432,
```

**Not `localhost:5433`.** pgAdmin is itself a container, so it reaches Postgres by
*service name* on the *internal* port. This is the single most common Docker
networking mistake, and Chapter 18 covers it in depth — but here is your first
encounter with it.

---

## 7.4 Start it

```bash
docker compose up -d
```

`-d` means detached — it runs in the background rather than holding your terminal.

The first run downloads roughly 600 MB and takes a few minutes. Then:

```bash
docker compose ps
```

You want two containers, with `devbrain-postgres` marked **healthy**:

```
NAME                STATUS                    PORTS
devbrain-pgadmin    Up 30 seconds             0.0.0.0:5051->80/tcp
devbrain-postgres   Up 35 seconds (healthy)   0.0.0.0:5433->5432/tcp
```

---

## 7.5 Checkpoint

**1. pgvector is available**

```bash
docker exec devbrain-postgres psql -U devbrain -d devbrain \
  -c "SELECT name, default_version FROM pg_available_extensions WHERE name='vector'"
```

Expected:

```
  name  | default_version
--------+-----------------
 vector | 0.8.6
```

If this returns nothing, you are running the wrong image.

**2. You can connect from your host**

```bash
docker exec devbrain-postgres psql -U devbrain -d devbrain -c "SELECT version();"
```

**3. pgAdmin loads**

Open <http://localhost:5051>. You should land directly in the browser interface
with **DevBrain Postgres** already in the left tree — no login.

Expand it, enter `devbrain_pass` when prompted for the password, and tick **Save
password**.

> A note from testing: requesting `/login` returns HTTP 401 in desktop mode, which
> looks alarming. That is expected — there is no login page. Request `/` and you
> get a 302 redirect to `/browser/` and a working UI.

**4. Data survives a restart**

```bash
docker compose down
docker compose up -d
docker exec devbrain-postgres psql -U devbrain -d devbrain -c "\l"
```

The `devbrain` database is still listed. That is your named volume working.

---

## 7.6 Everyday commands

```bash
docker compose up -d              # start
docker compose ps                 # status and health
docker compose logs -f postgres   # follow the database log
docker compose stop               # pause (keeps data)
docker compose down               # remove containers (keeps data)
docker compose down -v            # remove containers AND delete all data
```

When something is wrong, `docker compose logs` is almost always enough. Read the
last twenty lines before the failure.

---

## 7.7 Mistakes to avoid

**`port is already allocated`.** Something else holds that port. Change only the
*host* side and update `DATABASE_URL` to match:

```ini
POSTGRES_HOST_PORT=5434
DATABASE_URL=postgresql+psycopg://devbrain:devbrain_pass@localhost:5434/devbrain
```

To find the culprit on Windows:

```powershell
Get-NetTCPConnection -LocalPort 5433 -State Listen |
  ForEach-Object { Get-Process -Id $_.OwningProcess }
```

**Using `postgres:17`.** No pgvector. The failure arrives much later than the cause.

**Expecting `depends_on` to wait for readiness.** It waits for *start*, not
*ready*. Always pair it with a healthcheck and `condition: service_healthy`.

**Running `docker compose down -v` casually.** The `-v` deletes your volumes. Your
documents in `data/` are untouched, but every embedding is gone and you must
re-ingest.

**Committing the default password to a public repo.** `devbrain_pass` is fine on
your laptop and unacceptable on a server. Chapter 19 makes changing it the first
item on the hardening checklist.

---

## 7.8 What you have

PostgreSQL 17 with pgvector, healthy, on port 5433, with data in a named volume
that survives restarts. pgAdmin on 5051 with the server pre-registered.

An empty database — no tables yet. Those get created automatically when ingestion
first runs in Chapter 10.

Next, the configuration layer that tells every other file how to reach this.

*Next: [Chapter 8 — Configuration and Secrets](ch08-configuration.md)*

# DevBrain — User Guide

*© 2026 Shailendra Srivastava. All rights reserved.*

A complete, step-by-step manual. Follow it top to bottom the first time; after
that you will only ever need Part 5 (daily use).

**Estimated first-run time:** 10–15 minutes, most of it waiting for Docker to
download images.

---

## Contents

1. [What you need before you start](#1-what-you-need-before-you-start)
2. [Get your API keys and admin account](#2-get-your-api-keys-and-admin-account)
3. [One-time setup](#3-one-time-setup)
4. [First run](#4-first-run)
5. [Daily use](#5-daily-use)
6. [Using pgAdmin to look inside the database](#6-using-pgadmin-to-look-inside-the-database)
7. [Watching your traces in LangSmith](#7-watching-your-traces-in-langsmith)
8. [Command reference](#8-command-reference)
9. [Tuning the answers](#9-tuning-the-answers)
10. [Troubleshooting](#10-troubleshooting)
11. [Shutting down and cleaning up](#11-shutting-down-and-cleaning-up)
12. [Deployment](#12-deployment)
13. [Complete settings reference](#13-complete-settings-reference)

---

## 1. What you need before you start

| Requirement | Why | Check it works |
|---|---|---|
| **Docker Desktop**, running | Hosts PostgreSQL and pgAdmin | `docker ps` prints a table (not an error) |
| **Python 3.10 or newer** | Runs the ingestion script and the web server | `python --version` |
| **An OpenAI API key** | Writes the answers | See Part 2 |
| **A Cohere API key** | Turns text into vectors | See Part 2 |
| **A LangSmith API key** | Records traces (optional) | See Part 2 |
| **An administrator username/password** | First account that can manage users and documents | See [Part 2 — Administrator account](#administrator-account-not-an-external-key) |

> **Docker Desktop must actually be running**, not merely installed. On Windows,
> launch it from the Start menu and wait until the whale icon in the system tray
> stops animating. If `docker ps` reports *"cannot find the file specified"* or
> *"the daemon is not running"*, that is what is wrong.

### Ports DevBrain uses

DevBrain deliberately avoids the default ports so it can run alongside any other
Postgres or pgAdmin you already have on this machine.

| Service | Port | URL |
|---|---|---|
| Web UI / API | **8001** | http://localhost:8001 |
| PostgreSQL | **5433** | `localhost:5433` |
| pgAdmin | **5051** | http://localhost:5051 |

You can change all three in `.env` — see [Troubleshooting](#port-is-already-allocated).

---

## 2. Get your API keys and admin account

Open each link, create a key, and keep the browser tab open — you will supply
all of them, plus an administrator username/password, in the next step.

### OpenAI (required — writes the answers)

1. Go to <https://platform.openai.com/api-keys>
2. Click **Create new secret key**, name it `devbrain`, click **Create**
3. Copy it immediately — OpenAI will never show it again. It starts with `sk-`

> **Billing:** a brand-new OpenAI account needs credit before the API works,
> even if you already pay for ChatGPT Plus. They are separate products. Add a
> few dollars at <https://platform.openai.com/settings/organization/billing>.
> Asking DevBrain a question costs well under a cent with the default model.

### Cohere (required — creates the embeddings)

1. Go to <https://dashboard.cohere.com/api-keys>
2. Copy your **Trial key** (created automatically when you sign up)

> Trial keys are free and allow roughly 100 calls per minute — far more than
> enough. Ingesting the sample documents shipped in `data/` uses only a
> handful of calls, well under the trial limit.

### LangSmith (optional — records traces)

1. Go to <https://smith.langchain.com> and sign in
2. **Settings → API Keys → Create API Key**
3. Copy it. It starts with `lsv2_`

> You can skip this. Set `LANGSMITH_TRACING=false` in `.env` and everything else
> works identically — you just lose the debugging view.

### Administrator account (not an external key)

DevBrain requires every user to sign in, including you. There is no key to
fetch for this one — you just choose a username and password:

| Setting | What it is |
|---|---|
| `ADMIN_USERNAME` | The first administrator's sign-in name (default `Shailendra.KS`) |
| `ADMIN_PASSWORD` | Its password — **change this before running anywhere shared** |
| `ADMIN_FULL_NAME` | Display name shown in the UI (optional) |
| `ADMIN_EMAIL` | Optional, shown in **Manage users** |

`.\set-env.ps1` (Part 3.1) prompts for `ADMIN_USERNAME` and `ADMIN_PASSWORD`
alongside the three API keys, so you can set all four secrets in one pass.
This account is **seeded**, not hard-coded: on the app's first request it is
created in Postgres if it does not already exist, and only ever created —
never overwritten — so changing `ADMIN_PASSWORD` later and restarting does
not revert a password you changed inside the app. Only the bcrypt hash is
ever stored; the plaintext never reaches the database.

---

## 3. One-time setup

Open a terminal **in the project folder** (in VS Code: `Terminal → New Terminal`).

### Step 3.1 — Supply your API keys

DevBrain reads every setting from **application (environment) variables**.
Nothing is hard-coded, and the application never writes secrets to disk.

**Recommended — no secrets in any file:**

```powershell
.\set-env.ps1
```

It prompts for each key with masked input and saves them as Windows **user**
environment variables, so they persist across reboots and are picked up by
every terminal, VS Code and Docker Compose. No administrator rights needed.
It also prompts for `ADMIN_USERNAME` / `ADMIN_PASSWORD` (the account seeded
in Part 4.4) in the same pass, so one run of the script sets up every secret
the app needs.

```powershell
.\set-env.ps1 -Show     # which variables are set (values never shown)
.\set-env.ps1 -Clear    # remove them all
```

Restart any open terminal or VS Code window afterwards — they still hold the
old environment.

<details>
<summary>Just this session (PowerShell)</summary>

```powershell
$env:OPENAI_API_KEY    = "sk-..."
$env:COHERE_API_KEY    = "..."
$env:LANGSMITH_API_KEY = "lsv2_pt_..."
```

</details>

<details>
<summary>macOS / Linux</summary>

```bash
export OPENAI_API_KEY=sk-...
export COHERE_API_KEY=...
export LANGSMITH_API_KEY=lsv2_pt_...
```

</details>

**Alternative — a local `.env` file** (development convenience only):

```powershell
copy .env.example .env
```

A real environment variable always wins over `.env`. To ignore the file
entirely, set `DEVBRAIN_IGNORE_ENV_FILE=1`.

### Checking where a value came from

The server prints the origin of every setting at start-up — names and sources
only, never values:

```
Config source   : 4 from environment, 17 from .env, 0 default
  OPENAI_API_KEY     <- environment
  COHERE_API_KEY     <- environment
  LANGSMITH_API_KEY  <- .env
  ADMIN_PASSWORD     <- environment
```

`GET /api/config` returns the same mapping, so "which key is it actually
using?" is answerable without exposing anything.

> **If you use the `.env` route:** don't wrap values in quotes, don't put
> spaces around the `=`, and make sure the file is named exactly `.env` —
> Windows Explorer likes to save it as `.env.txt`. VS Code shows the real file
> name in the editor tab, so check there.

### Step 3.2 — Install the Python dependencies

Create an isolated environment so DevBrain's packages cannot clash with anything
else on your machine:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

<details>
<summary>macOS / Linux</summary>

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

</details>

Your prompt now starts with `(.venv)`. **That prefix must be there every time
you run a DevBrain command.** If you open a new terminal later, re-run just the
activate line.

> **PowerShell blocks the activate script?** If you see *"running scripts is
> disabled on this system"*, run this once and try again:
>
> ```powershell
> Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
> ```

### Step 3.3 — Start the database

```bash
docker compose up -d
```

The first run downloads about 600 MB and takes a few minutes. When it finishes:

```bash
docker compose ps
```

You want **two** containers, with `devbrain-postgres` marked `healthy`:

```
NAME                STATUS                    PORTS
devbrain-pgadmin    Up 30 seconds             0.0.0.0:5051->80/tcp
devbrain-postgres   Up 35 seconds (healthy)   0.0.0.0:5433->5432/tcp
```

If `devbrain-postgres` says `unhealthy` or keeps restarting, jump to
[Troubleshooting](#10-troubleshooting).

---

## 4. First run

### Step 4.1 — Add your documents

Put your notes into the `data/` folder. DevBrain reads:

`.txt` `.md` `.markdown` `.log` `.csv` `.pdf` `.docx` `.xlsx` `.xlsm`

Sub-folders are searched too, so `data/python/asyncio.md` works fine.

A handful of sample/reference documents ship in `data/` so you can test
everything immediately — a mix of PDFs (invoices, insurance policy details),
Markdown/SQL notes, a CSS cheat sheet and reusable code snippets. Run
`python ingest.py --stats` any time to see exactly what is currently indexed
by filename. Delete the samples once you have added your own documents —
`python ingest.py --reset` afterwards removes their chunks too.

> **Scanned PDFs will not work.** If a PDF is a photo of a page, it holds no
> text layer for DevBrain to read. Ingestion reports it as `empty`. You need OCR
> software to convert it first.

### Step 4.2 — Ingest

```bash
python ingest.py
```

This reads every file, splits it into chunks, sends them to Cohere for
embeddings, and stores the results in PostgreSQL. The shape of the output
looks like this — your file names, counts and timings will differ depending
on what is actually in `data/`:

```
======================================================================
DevBrain ingestion
======================================================================
Data folder      : ****\DevBrain\data
Database         : postgresql+psycopg://devbrain:****@localhost:5433/devbrain
Collection       : devbrain_docs
Embedding model  : embed-english-v3.0
Chunk size       : 1000 chars (overlap 150)

[1/4] Discovering files
  found 16 file(s), 3.8 MB total

[2/4] Parsing documents
  ok      General_SQL.md  (1 part(s), 2,140 chars)
  ok      RAG_Implementation_Guide.txt  (1 part(s), 3,015 chars)
  ok      Telerik_Controls_Reusable_Code.txt  (1 part(s), 1,822 chars)
  ok      CSS_Form_Controls_Cheat_Sheet-v2.pdf  (2 part(s), 2,910 chars)
  ok      Invoice_1_ElectroWorld.pdf  (1 part(s), 640 chars)
  ok      HDFC_Policy_Details.pdf  (4 part(s), 5,120 chars)
  ...     (and the remaining files in data/)
  extracted 38 document part(s)

[3/4] Splitting into chunks
  created 61 chunk(s), average 710 chars

[4/4] Embedding with Cohere and storing in pgvector
  checking Cohere credentials...
  Cohere OK
  batch 1/1: 61/61 chunks stored (0.9s)

======================================================================
Done. 61 chunk(s) from 16 file(s) in 2.1s.
======================================================================
```

Confirm it landed:

```bash
python ingest.py --stats
```

### Step 4.3 — Start the web server

```bash
uvicorn app:app --reload --port 8001
```

Leave this terminal running — it *is* the server. You should see:

```
INFO     devbrain  Chat model      : gpt-4o-mini
INFO     devbrain  Embedding model : embed-english-v3.0
INFO     devbrain  Collection      : devbrain_docs
INFO     devbrain  LangSmith       : tracing to project 'devbrain'
INFO     devbrain  Open the UI at http://localhost:8001
INFO:     Uvicorn running on http://127.0.0.1:8001
```

### Step 4.4 — Sign in

DevBrain requires an account. Nobody can search or upload while signed out.

**Administrator** — seeded automatically from `ADMIN_USERNAME` / `ADMIN_PASSWORD`
in your `.env` the first time the app starts. Only the bcrypt **hash** is stored
in Postgres; the plaintext never reaches the database.

**Everyone else** — clicks **Create account** on the sign-in screen. Self-service
registration always produces a standard (non-admin) user. Administrators can
only ever be seeded from `.env`, so nobody can register their way to admin.

> Seeding only ever *creates*. If the admin account already exists it is left
> alone, so changing the password later is not undone by the next restart.
> **Change `ADMIN_PASSWORD` from the shipped value before deploying anywhere
> shared.**

Once signed in, your name appears top-right as *Welcome: [name]*, with a
**Sign out** button.

### What each role can do

| | Standard user | Administrator |
|---|---|---|
| Ask questions | Yes | Yes |
| Upload documents | Yes | Yes |
| **My documents** tab | Yes (own only) | Yes (own only) |
| **Manage users** tab | — | Yes |
| **Manage documents** tab | — | Yes (all users') |

**My documents** is available to everyone. It lists what *you* uploaded, with
each document's purpose, type, size, upload date and live chunk count. The
listing is scoped on the server by your user id, so one person can never see
another's uploads. Only an administrator can delete documents.

**Manage users** lists every account with their email, document count and last
sign-in. Deleting a user ends their active sessions **immediately** and blocks
future sign-ins. Documents they uploaded are kept — the knowledge base should not
lose content because someone left — and their name stays on those documents as
an audit trail. You cannot delete your own account, and the last remaining
administrator cannot be deleted.

**Manage documents** lists every document with its type, purpose, uploader and
live chunk count. Deleting one removes the file from `data/` **and** its chunks
from the index in the same action, so the index is refreshed instantly — no
re-ingestion step.

### Who uploaded what

Every upload records the uploader and timestamp. That attribution is stamped
onto each chunk, so when an answer cites a document the UI shows
*uploaded by [name]* beside the source. Files ingested from the command line
(rather than through the browser) are attributed to `system`.

### Step 4.5 — Ask your first question

Open <http://localhost:8001>.

The status bar under the title should be **green**:
`Ready — N chunks indexed` (N depends on what is in your `data/` folder).

Type a question and press **Enter**. With the sample documents, try:

- *What is the total amount due on the ElectroWorld invoice?*
- *What does the HDFC policy document cover?*
- *How do I write a basic JOIN in SQL, based on the notes provided?*
- *How do I style a form control using the CSS cheat sheet?*

Adjust these to whatever is actually in your `data/` folder — the point is
to ask something your documents genuinely contain.

Each answer appears with the **source files** it came from listed underneath.
Always check them — that list is how you verify an answer instead of trusting it.

Now try something your documents definitely do not cover, like *"What is the
capital of France?"*. DevBrain will refuse rather than guess. **That refusal is
the system working correctly** — it is the entire point of grounding answers in
your own documents.

---

## 5. Daily use

Two terminals, both with `(.venv)` active:

```bash
# Terminal 1 — start the database (only if it is not already up)
docker compose up -d

# Terminal 2 — start the web server
uvicorn app:app --reload --port 8001
```

Then open <http://localhost:8001>.

### Adding documents from the browser (easiest)

Open the **Upload documents** tab, drag your files in, and click
**Upload & ingest**. Files are saved into `data/` and ingested in the same
request — by the time the result appears, they are already searchable. Switch
back to **Ask** and question them straight away. No command line, no restart.

**Document purpose is required.** Describe what the files are for before
uploading — it is stored in Postgres alongside the file name, your username and
the upload date, and it is what the administrator sees in *Manage documents*.

| | |
|---|---|
| Accepted types | `.pdf` `.docx` `.txt` `.md` `.markdown` `.log` `.csv` `.xlsx` `.xlsm` |
| Size limit | 25 MB per file |
| Files per upload | 20 |
| Purpose | Required |
| Re-uploading the same name | **Replaces** it — old chunks are deleted first |

Each file is reported independently as **Ingested**, **Skipped** (wrong type,
empty, or too large) or **Failed**, so one bad PDF never costs you the rest of
the batch.

### Adding documents from the command line

Copying files into `data/` yourself does **not** update the index — nothing
watches the folder. Re-run ingestion afterwards:

| What changed | Command |
|---|---|
| Added new files | `python ingest.py` |
| Edited or deleted existing files | `python ingest.py --reset` |

`python ingest.py` is safe to run repeatedly — chunks get stable IDs, so
re-running updates them in place instead of creating duplicates. But it cannot
know about text you *deleted*, so use `--reset` after edits to rebuild cleanly.

You do **not** need to restart the web server after ingesting. Just refresh the
page.

---

## 6. Using pgAdmin to look inside the database

Optional, but it is the fastest way to convince yourself there is no magic here:
your documents are ordinary rows in ordinary tables.

1. Open <http://localhost:5051>
2. In the left tree, expand **Servers → DevBrain Postgres**
3. When prompted for a password, enter `devbrain_pass` and tick **Save password**
4. Navigate to **Databases → devbrain → Schemas → public → Tables**

You will find two tables:

| Table | What is in it |
|---|---|
| `langchain_pg_collection` | One row per collection (a named knowledge base) |
| `langchain_pg_embedding` | One row per chunk: its text, its metadata, and its 1024-number vector |

Right-click `langchain_pg_embedding` → **View/Edit Data → First 100 Rows**.

Or open the **Query Tool** and run:

```sql
-- How many chunks came from each document?
SELECT cmetadata ->> 'source' AS document,
       count(*)               AS chunks
FROM langchain_pg_embedding
GROUP BY 1
ORDER BY 2 DESC;
```

```sql
-- Look at one actual chunk, and the first few numbers of its vector
SELECT cmetadata ->> 'source'    AS document,
       left(document, 200)       AS text_preview,
       left(embedding::text, 80) AS vector_preview
FROM langchain_pg_embedding
LIMIT 3;
```

That `vector_preview` column — a long list of decimals — is the embedding. That
is genuinely all an embedding is.

---

## 7. Watching your traces in LangSmith

If you set `LANGSMITH_TRACING=true` and supplied a key, every question is
recorded automatically.

1. Go to <https://smith.langchain.com>
2. Open the project named **devbrain** (created on your first question)
3. Click any run named **DevBrainRAG**

Expand the tree and you can see, for that single question:

- **Retriever** — which chunks were pulled from Postgres, and their scores
- **ChatPromptTemplate** — the fully assembled prompt, with your documents pasted in
- **ChatOpenAI** — the exact request and response, token counts, and cost
- Total latency, broken down per step

This is the most useful debugging tool in the project. When an answer looks
wrong, the trace tells you *which half* failed. If the retriever pulled
irrelevant chunks, that is a **retrieval** problem (tune chunk size or `k`). If
the retriever pulled the right chunks and the answer is still poor, that is a
**generation** problem (tune the prompt or use a stronger model).

The UI also shows a short trace ID beside each answer so you can find the run.

---

## 8. Command reference

All commands assume `(.venv)` is active and you are in the project folder.

### Ingestion

```bash
python ingest.py              # add or update documents (safe to re-run)
python ingest.py --reset      # wipe the collection and rebuild from scratch
python ingest.py --dry-run    # preview chunks; calls no APIs, writes nothing
python ingest.py --stats      # report what is currently stored
python ingest.py --data-dir ./other-notes   # ingest a different folder
python ingest.py --help       # full flag list
```

`--dry-run` is the cheapest way to check how your documents are being split
before spending any API calls.

### Web server

```bash
uvicorn app:app --reload --port 8001   # recommended (auto-reloads on edit)
python app.py                          # equivalent, no flags needed
```

Press `Ctrl+C` to stop.

### Docker

```bash
docker compose up -d            # start both containers
docker compose ps               # status and health
docker compose logs -f postgres # follow the database log
docker compose stop             # pause (keeps data)
docker compose down             # remove containers (keeps data)
docker compose down -v          # remove containers AND delete all data
```

### API (useful for scripting)

The server publishes interactive docs at <http://localhost:8001/docs>.

| Endpoint | Method | Auth required | Purpose |
|---|---|---|---|
| `/` | GET | No | Serves the browser UI |
| `/api/health` | GET | No | Postgres/pgvector status and chunk count |
| `/api/config` | GET | No | Non-secret settings currently in effect |
| `/api/auth/register` | POST | No | Create a standard (non-admin) account and sign in |
| `/api/auth/login` | POST | No | Sign in, issuing the session cookie |
| `/api/auth/logout` | POST | Yes | Revoke the current session |
| `/api/auth/me` | GET | No (returns `user: null` if signed out) | Who the current session belongs to |
| `/api/ask` | POST | **Yes** | The RAG endpoint — question in, grounded answer out |
| `/api/upload` | POST | **Yes** | Save + ingest documents in one request |
| `/api/my/documents` | GET | Yes | Documents *you* uploaded, with live chunk counts |
| `/api/admin/users` | GET | Yes (admin) | List every account |
| `/api/admin/users/{id}` | DELETE | Yes (admin) | Delete a user (their sessions end immediately) |
| `/api/admin/documents` | GET | Yes (admin) | Every uploaded document, any owner |
| `/api/admin/documents/{source}` | DELETE | Yes (admin) | Remove a document's file and chunks together |

`/api/ask` and `/api/upload` require a signed-in session — the browser holds
this as an `HttpOnly` cookie automatically, but a script has to log in first
and reuse the cookie on every subsequent call:

```bash
# 1. Health check — no auth needed
curl http://localhost:8001/api/health

# 2. Sign in and save the session cookie to a jar file
curl -c cookies.txt -X POST http://localhost:8001/api/auth/login \
  -H "Content-Type: application/json" \
  -d "{\"username\": \"Shailendra.KS\", \"password\": \"<ADMIN_PASSWORD>\"}"

# 3. Ask a question, sending the cookie back
curl -b cookies.txt -X POST http://localhost:8001/api/ask \
  -H "Content-Type: application/json" \
  -d "{\"question\": \"How do I configure connection pooling?\"}"

# 4. Upload documents — saved to data/ AND ingested in the same request
curl -b cookies.txt -X POST http://localhost:8001/api/upload \
  -F "files=@notes.md" -F "files=@spec.pdf" -F "purpose=Reference notes"
```

Calling `/api/ask` or `/api/upload` without a valid cookie returns
`401 {"detail": "Please sign in to continue."}` — see
[Troubleshooting](#401-please-sign-in-to-continue).

---

## 9. Tuning the answers

All of these live in `.env`. **Restart the server** after changing any of them.
Settings marked *(re-ingest)* also need `python ingest.py --reset`.

| Setting | Default | Raise it when… | Lower it when… |
|---|---|---|---|
| `RETRIEVER_K` | `4` | Answers miss information that is definitely in your notes | Answers wander off-topic |
| `CHUNK_SIZE` *(re-ingest)* | `1000` | Your documents have long, flowing arguments | Your notes are short, punchy facts |
| `CHUNK_OVERLAP` *(re-ingest)* | `150` | Answers seem to cut off mid-thought | — |
| `RETRIEVER_SCORE_THRESHOLD` | `0.0` | Irrelevant sources keep appearing (try `0.3`) | DevBrain refuses too often |
| `OPENAI_TEMPERATURE` | `0.0` | You want more varied phrasing | Keep at `0.0` for factual work |
| `OPENAI_MODEL` | `gpt-4o-mini` | You need better reasoning — try `gpt-4o` | Cost matters more than quality |

**Rule of thumb:** if the right document is not being *found*, change
`RETRIEVER_K` or `CHUNK_SIZE`. If the right document is found but the answer is
weak, change `OPENAI_MODEL`. The LangSmith trace tells you which case you are in.

---

## 10. Troubleshooting

### The status bar is red: "Backend unreachable"

The web server is not running. Start it:

```bash
uvicorn app:app --reload --port 8001
```

### The status bar is red: "Cannot reach PostgreSQL"

Docker is not running, or the container is down:

```bash
docker compose ps        # is devbrain-postgres there and healthy?
docker compose up -d     # start it
docker compose logs postgres
```

### The status bar is amber: "Knowledge base is empty"

You have not ingested yet, or ingestion found no files:

```bash
python ingest.py
```

### `port is already allocated`

Another program already uses that port. Pick different ones in `.env`:

```ini
POSTGRES_HOST_PORT=5434
PGADMIN_HOST_PORT=5052
DATABASE_URL=postgresql+psycopg://devbrain:devbrain_pass@localhost:5434/devbrain
```

The port in `DATABASE_URL` **must match** `POSTGRES_HOST_PORT`. Then:

```bash
docker compose down
docker compose up -d
```

To find the culprit on Windows:

```powershell
Get-NetTCPConnection -LocalPort 5433 -State Listen |
  ForEach-Object { Get-Process -Id $_.OwningProcess }
```

For the web server, just pass a different port:
`uvicorn app:app --reload --port 8002`.

### `Configuration error: OPENAI_API_KEY is missing`

Your `.env` is missing, misnamed, or still holds the placeholder text. Check
that the file is called exactly `.env`, and that the key line has no quotes and
no spaces around the `=`.

### `Cohere rejected your API key`

The key is wrong or was revoked. Get a fresh one at
<https://dashboard.cohere.com/api-keys>. Make sure you did not paste a Cohere
*account ID* instead of the API key.

### `Your OpenAI account has no remaining quota`

You need to add billing credit. A ChatGPT Plus subscription does **not** cover
API usage — they are billed separately.
<https://platform.openai.com/settings/organization/billing>

### Traces never appear in LangSmith (`403 Forbidden` in the server log)

**LangSmith runs separate US and EU instances, and a key from one is rejected by
the other.** Confusingly, the rejection is `403 Forbidden` — exactly the same
error you would get from a completely invalid key.

Check the address bar while signed in to LangSmith, then match it in `.env`:

| You sign in at | Set this in `.env` |
|---|---|
| `smith.langchain.com` | `LANGSMITH_ENDPOINT=https://api.smith.langchain.com` |
| `eu.smith.langchain.com` | `LANGSMITH_ENDPOINT=https://eu.api.smith.langchain.com` |

Restart the server afterwards. To confirm it is fixed, watch the server log
while asking a question — a working setup logs *nothing* about LangSmith, while
a broken one repeats:

```
WARNING langsmith.client  Failed to send compressed multipart ingest: ... 403 ...
```

Note that this never breaks your answers. Tracing runs in a background thread
and fails silently by design, so the RAG pipeline keeps working normally.

### `Vector size mismatch` / `expected 1024 dimensions`

You changed `COHERE_EMBED_MODEL` after ingesting. Different models produce
different-sized vectors and they cannot be mixed:

```bash
python ingest.py --reset
```

### A document was skipped as `empty`

Almost always a scanned PDF (an image, with no text layer). Run it through OCR
first, or supply the content as `.txt`.

### Answers are wrong or too vague

Work through this in order:

1. Open the LangSmith trace and look at what the **Retriever** returned.
2. **Wrong chunks retrieved?** Raise `RETRIEVER_K` to `6`, or reduce
   `CHUNK_SIZE` to `600` and re-ingest with `--reset`.
3. **Right chunks, weak answer?** Set `OPENAI_MODEL=gpt-4o`.
4. **DevBrain refuses although the answer is in your notes?** Set
   `RETRIEVER_SCORE_THRESHOLD=0.0` and raise `RETRIEVER_K`.

### `401 Please sign in to continue`

`/api/ask` and `/api/upload` require an active session. In the browser this
means you were signed out (sessions expire after 12 hours, or you clicked
**Sign out**) — sign in again. When scripting against the API, you skipped
the login step; see the curl example in
[Command reference](#api-useful-for-scripting).

### `403 Administrator access is required`

You are signed in, but as a standard user calling an admin-only endpoint
(`/api/admin/*`). Sign in with the administrator account instead.

### I forgot the administrator password

Set a **new** `ADMIN_PASSWORD` in your environment and restart the server —
but note that seeding only ever *creates*, so this does nothing if the admin
account already exists (§2, Part 2). To reset an existing admin's password,
either update it from inside the app while still signed in, or open pgAdmin
(Part 6) and update the row directly:

```sql
-- Generate a new bcrypt hash for "NewPassword123" with Python first:
--   python -c "import bcrypt,hashlib; print(bcrypt.hashpw(hashlib.sha256(b'NewPassword123').hexdigest().encode(), bcrypt.gensalt(12)).decode())"
UPDATE devbrain_users
SET password_hash = '<paste the generated hash>'
WHERE username = 'Shailendra.KS';
```

### How do I make another user an administrator?

There is no promote-to-admin button by design — `POST /api/auth/register`
always creates a standard user, so nobody can register their way to admin
(§2, §4.4). To grant admin rights to an existing account, run this once in
pgAdmin's Query Tool (Part 6):

```sql
UPDATE devbrain_users SET is_admin = true WHERE username = 'their-username';
```

### `ModuleNotFoundError: No module named 'fastapi'`

The virtual environment is not active. Look for `(.venv)` at the start of your
prompt; if it is missing:

```powershell
.\.venv\Scripts\Activate.ps1
```

### Everything is broken and I want to start over

```bash
docker compose down -v      # deletes all stored vectors
docker compose up -d
python ingest.py --reset
```

Your documents in `data/` are never touched by any of this.

---

## 11. Shutting down and cleaning up

```bash
# Stop the web server
Ctrl+C in its terminal

# Stop the containers, keeping your data
docker compose stop

# Remove the containers, keeping your data
docker compose down

# Remove everything including the stored vectors
docker compose down -v
```

Your `data/` folder and `.env` are never modified by DevBrain. After
`docker compose down -v`, one `python ingest.py` run rebuilds the whole
knowledge base from those files.

---

## 12. Deployment

DevBrain supports three deployment modes. They differ only in **where the Python
process runs** — the database, the documents and the ingestion workflow are
identical in all three.

| Mode | Python runs | Use it for | Hot reload |
|---|---|---|---|
| **A — Development** | On your machine | Day-to-day work, editing code | Yes |
| **B — Containerised (local)** | In Docker | Testing the real deployment artifact | No |
| **C — Server** | In Docker, on a remote host | Sharing with your team | No |

Mode A is what Parts 3–5 of this guide describe, and remains the right choice
while you are working on the code. Modes B and C are below.

---

### Mode A — Development (what you have now)

```bash
docker compose up -d                    # database + pgAdmin only
uvicorn app:app --reload --port 8001    # app on your host, hot reload
```

The app service is deliberately **skipped** by a plain `docker compose up -d`,
because it sits behind a Compose *profile*. That keeps this fast edit-and-refresh
loop untouched.

---

### Mode B — Containerised, on your own machine

Runs the app as a real Docker image — the same artifact you would deploy to a
server. Use this to verify your build before shipping it.

```bash
# Stop any host uvicorn first: it would hold port 8001
docker compose --profile full up -d --build
```

That one command builds the image, starts Postgres, waits for it to become
healthy, then starts the app. Check it:

```bash
docker compose --profile full ps
```

```
NAME                STATUS                   PORTS
devbrain-app        Up 6 seconds (healthy)   0.0.0.0:8001->8001/tcp
devbrain-pgadmin    Up 2 minutes             0.0.0.0:5051->80/tcp
devbrain-postgres   Up 2 minutes (healthy)   0.0.0.0:5433->5432/tcp
```

Open <http://localhost:8001> exactly as before.

#### Ingesting when the app is containerised

Your `data/` folder is mounted into the container, so run ingestion *inside* it:

```bash
docker compose --profile full run --rm app python ingest.py
docker compose --profile full run --rm app python ingest.py --reset
docker compose --profile full run --rm app python ingest.py --stats
```

`run --rm` starts a throwaway container, runs the command, and deletes it. The
running web app is untouched — no restart needed, just refresh the browser.

> You can still run `python ingest.py` on your host instead, as long as your
> `.env` points at `localhost:5433`. Both write to the same database.

#### Everyday container commands

```bash
docker compose --profile full logs -f app       # follow app logs
docker compose --profile full restart app       # restart just the app
docker compose --profile full up -d --build app # rebuild after a code change
docker compose --profile full down              # stop everything (keeps data)
```

#### The one thing that trips everyone up

Inside a container, `localhost` means **that container**, not your machine. So
the `localhost:5433` in your `.env` cannot work from inside the app container.

Containers on the same Compose network reach each other by **service name** on
the container's **internal** port:

| Connecting from | Use |
|---|---|
| Your host (uvicorn, pgAdmin on your desktop) | `localhost:5433` |
| Inside the app container | `postgres:5432` |

`docker-compose.yml` handles this for you by overriding `DATABASE_URL` for the
app service. You do not need to edit `.env` — but if you ever see
`connection refused` from inside a container, this is why.

---

### Mode C — Deploying to a server

#### Step C.1 — Prepare the host

Any Linux box with Docker Engine and Compose v2. 2 GB RAM is enough; 4 GB is
comfortable.

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
sudo usermod -aG docker $USER   # log out and back in
```

#### Step C.2 — Copy the project across

```bash
git clone <your-repo> devbrain && cd devbrain
# or: scp -r ./DevBrain user@server:~/devbrain
```

Do **not** copy `.env` into version control. Create it on the server.

#### Step C.3 — Write a production `.env`

```bash
cp .env.example .env
nano .env
```

Change these from the defaults:

```ini
# Real keys
OPENAI_API_KEY=sk-...
COHERE_API_KEY=...
LANGSMITH_API_KEY=lsv2_pt_...
LANGSMITH_PROJECT=devbrain-prod          # keep prod traces separate from dev
LANGSMITH_ENDPOINT=https://eu.api.smith.langchain.com   # match your region

# A real password — see step C.4
DATABASE_URL=postgresql+psycopg://devbrain:CHANGE_THIS@postgres:5432/devbrain

# Do not expose the database and admin UI to the internet
POSTGRES_HOST_PORT=127.0.0.1:5433
PGADMIN_HOST_PORT=127.0.0.1:5051
API_HOST_PORT=8001
```

Note `DATABASE_URL` uses `postgres:5432` here, not `localhost:5433` — on a
server the app only ever runs in a container.

Lock the file down:

```bash
chmod 600 .env
```

#### Step C.4 — Change the database password

The committed default (`devbrain_pass`) is fine on your laptop and unacceptable
on a server. Edit `docker-compose.yml`:

```yaml
environment:
  POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in .env}
```

Then put a generated password in `.env`, in **both** places:

```ini
POSTGRES_PASSWORD=<paste output of: openssl rand -base64 24>
DATABASE_URL=postgresql+psycopg://devbrain:<same password>@postgres:5432/devbrain
```

The `:?` syntax makes Compose refuse to start if the variable is unset, so you
cannot accidentally deploy with a blank password.

#### Step C.5 — Launch

```bash
docker compose --profile full up -d --build
docker compose --profile full ps          # confirm app is (healthy)
docker compose --profile full run --rm app python ingest.py
```

#### Step C.6 — Put HTTPS in front

Never expose port 8001 directly. DevBrain **does** authenticate users
(bcrypt-hashed passwords, server-side sessions, admin vs. standard roles —
see the Architecture doc §13) — what it does **not** do on its own is
encrypt the connection. Signing in over plain HTTP sends the password and
session cookie unencrypted, so anyone on the network path between the
browser and the server can read them.

Bind the app to loopback in `.env`:

```ini
API_HOST_PORT=127.0.0.1:8001
```

Then terminate TLS with nginx or Caddy. Caddy is the shortest path — this
`Caddyfile` obtains and renews a certificate automatically:

```
devbrain.example.com {
    reverse_proxy 127.0.0.1:8001
}
```

Once traffic reaches the app over HTTPS, set the session cookie's `secure`
flag so it is only ever sent over an encrypted connection — in `app.py`,
change `secure=False` to `secure=True` in `_set_session_cookie()`. DevBrain's
own accounts (Part 4.4) are then the real access control; a reverse-proxy
`basicauth` block is optional extra hardening, not a substitute for it.

#### Step C.7 — Back up

Everything irreplaceable is in one Docker volume plus two files.

```bash
# Database (vectors + metadata)
docker exec devbrain-postgres pg_dump -U devbrain devbrain | gzip > backup-$(date +%F).sql.gz

# Restore
gunzip -c backup-2026-08-28.sql.gz | docker exec -i devbrain-postgres psql -U devbrain -d devbrain
```

Also back up `.env` (secrets) and `data/` (your source documents). Strictly
speaking the database is reproducible — if you still have `data/` and your keys,
`python ingest.py --reset` rebuilds it from scratch.

---

### Updating a deployment

```bash
git pull
docker compose --profile full up -d --build app   # rebuild and restart the app
```

Only the app is rebuilt; Postgres keeps running and your vectors are untouched.
If a change affects chunking or the embedding model, re-ingest afterwards:

```bash
docker compose --profile full run --rm app python ingest.py --reset
```

**Rollback:** images are tagged, so keep the previous one and re-point the
service, or `git checkout <previous-commit> && docker compose --profile full up -d --build app`.

---

### Production hardening checklist

Work through this before letting anyone else reach the service.

| | Item | Why |
|---|---|---|
| ☐ | Changed `POSTGRES_PASSWORD` from the default | The default is published in this repo |
| ☐ | `.env` is `chmod 600` and not in git | It holds three API keys |
| ☐ | Postgres bound to `127.0.0.1:5433` | Never expose a database to the internet |
| ☐ | pgAdmin bound to `127.0.0.1:5051`, or not deployed at all | It is an admin console |
| ☐ | HTTPS terminated by a reverse proxy | The app speaks plain HTTP; without TLS, passwords and session cookies travel unencrypted |
| ☐ | `secure=True` set on the session cookie (`app.py`, once HTTPS is live) | Otherwise the cookie can still be sent over an accidental plain-HTTP request |
| ☐ | `ADMIN_PASSWORD` changed from the shipped/default value | The default is meant for local development only |
| ☐ | Self-registration reviewed — anyone who reaches the URL can create a standard account | By design (§2); put the proxy in front of registration too if that is unwanted |
| ☐ | Automated `pg_dump` backups, and a tested restore | An untested backup is not a backup |
| ☐ | Spending limits set on the OpenAI and Cohere dashboards | Caps the damage if credentials or the URL leak |
| ☐ | `docker compose --profile full logs` checked after deploy | Catches config errors immediately |
| ☐ | `LANGSMITH_PROJECT` differs from your dev project | Keeps prod traces clean |

> **The single most important line:** DevBrain authenticates every user
> (Part 4.4) but ships **without transport encryption** — it is designed to be
> reached over `localhost` or a trusted network. The moment it is reachable
> from the open internet, put HTTPS in front of it (Step C.6) so credentials
> and session cookies are never sent in the clear.

---

### Scaling, when you need it

| Symptom | Change |
|---|---|
| Slow under concurrent users | Raise `--workers` in the Dockerfile `CMD` (start with 4) |
| Retrieval slows past ~100k chunks | `CREATE INDEX ON langchain_pg_embedding USING hnsw (embedding vector_cosine_ops);` |
| Ingestion is slow | It is API-bound, not CPU-bound — batches are already at Cohere's 96 limit |
| Database is the bottleneck | Move Postgres to a managed service; point `DATABASE_URL` at it and drop the container |

---

## 13. Complete settings reference

Every setting, and what it should be in each environment.

### API keys and models

| Setting | Development | Containerised / Server | Notes |
|---|---|---|---|
| `OPENAI_API_KEY` | your key | your key | Never commit. Injected at runtime, never baked into the image |
| `OPENAI_MODEL` | `gpt-4o-mini` | `gpt-4o-mini` | `gpt-4o` for harder reasoning, ~15× the cost |
| `OPENAI_TEMPERATURE` | `0.0` | `0.0` | Keep at 0 for factual retrieval |
| `COHERE_API_KEY` | your key | your key | Used by both ingestion and querying |
| `COHERE_EMBED_MODEL` | `embed-english-v3.0` | must match dev | Changing it requires `ingest.py --reset` |

### LangSmith

| Setting | Development | Server | Notes |
|---|---|---|---|
| `LANGSMITH_TRACING` | `true` | `true` | `false` disables cleanly; app is unaffected |
| `LANGSMITH_API_KEY` | your key | your key | Optional |
| `LANGSMITH_PROJECT` | `devbrain` | `devbrain-prod` | **Separate them**, or prod and dev traces interleave |
| `LANGSMITH_ENDPOINT` | match your region | match your region | EU accounts must use `https://eu.api.smith.langchain.com` |

### Database

| Setting | Development | Inside a container | Notes |
|---|---|---|---|
| `DATABASE_URL` host | `localhost:5433` | `postgres:5432` | **The #1 deployment gotcha.** Compose overrides this for the app service |
| `POSTGRES_HOST_PORT` | `5433` | `127.0.0.1:5433` | Bind to loopback on a server |
| `POSTGRES_PASSWORD` | `devbrain_pass` | **generated** | Change before any server deployment |
| `COLLECTION_NAME` | `devbrain_docs` | `devbrain_docs` | Two collections can share one database |

### Application

| Setting | Development | Containerised | Notes |
|---|---|---|---|
| `API_PORT` | `8001` | `8001` | Port *inside* the container |
| `API_HOST_PORT` | n/a | `8001` | Host port the container publishes to |
| uvicorn host | `127.0.0.1` | `0.0.0.0` | A container binding loopback is unreachable from outside |
| `--reload` | **on** | **off** | Reload watches the filesystem and doubles memory |
| `--workers` | 1 | 2–4 | Set in the Dockerfile `CMD` |
| `PGADMIN_HOST_PORT` | `5051` | `127.0.0.1:5051` | Or omit pgAdmin entirely in production |

### Retrieval tuning

| Setting | Default | Effect |
|---|---|---|
| `DATA_DIR` | `data` | `/app/data` inside the container (mounted read-write for uploads) |
| `CHUNK_SIZE` | `1000` | Smaller = sharper retrieval, less context. Needs `--reset` |
| `CHUNK_OVERLAP` | `150` | Stops ideas being orphaned at a chunk boundary. Needs `--reset` |
| `RETRIEVER_K` | `4` | Chunks fed to the LLM per question |
| `RETRIEVER_SCORE_THRESHOLD` | `0.0` | `0.3` filters weak matches; `0.0` keeps everything |

### Administrator account

| Setting | Development | Server | Notes |
|---|---|---|---|
| `ADMIN_USERNAME` | `Shailendra.KS` (or your choice) | your choice | Seeded on first request only — never overwritten on restart |
| `ADMIN_PASSWORD` | your choice | **generated / unique** | Change from any shared default before deploying |
| `ADMIN_FULL_NAME` | optional | optional | Shown in the UI header and **Manage users** |
| `ADMIN_EMAIL` | optional | optional | Shown in **Manage users** only |

There is no `SESSION_TTL_HOURS` environment variable — session length is
fixed at **12 hours** in `auth.py`. After that, or after **Sign out**, signing
in again is required.

---

## Where to go next

- **`Architecture_and_Concepts.md`** — how all of this actually works, from what
  an embedding *is* to a walkthrough of each Python file.
- **<http://localhost:8001/docs>** — interactive API documentation, generated
  automatically by FastAPI.

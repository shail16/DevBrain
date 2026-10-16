# Chapter 10 — The Ingestion Engine

*© 2026 Shailendra Srivastava. All rights reserved.*

---

## 10.1 What ingestion does

`ingest.py` is the write path. Run it whenever documents change.

```
1. DISCOVER   walk data/, keep supported files, report what is skipped
2. LOAD       parse each into Documents            (Chapter 9)
3. SPLIT      cut into ~1000-char overlapping chunks (Chapter 5)
4. EMBED      Cohere: 96 chunks per API call        (Chapter 3)
5. STORE      INSERT ... ON CONFLICT DO UPDATE       (Chapter 4)
```

It is 583 lines, and most of that is not the pipeline — it is progress reporting,
error translation and CLI handling. That ratio is normal and worth accepting early:
**the algorithm is small; making it pleasant to operate is most of the work.**

---

## 10.2 The command-line interface

```bash
python ingest.py              # add or update documents (safe to re-run)
python ingest.py --reset      # wipe the collection and rebuild from scratch
python ingest.py --dry-run    # preview chunks; calls no APIs, writes nothing
python ingest.py --stats      # report what is currently stored
python ingest.py --data-dir ./other-notes
```

`--dry-run` deserves emphasis. It runs discovery, loading and splitting, then stops
and shows you the first chunk:

```
[3/3] Splitting into chunks
  created 18 chunk(s), average 760 chars

--dry-run: stopping before embedding. Nothing was written.

Preview of the first chunk:
----------------------------------------------------------------------
Code Review Standards

These standards apply to every pull request. The goal is to catch defects early...
----------------------------------------------------------------------
metadata: {'file_type': 'docx', 'source': 'code-review-standards.docx', 'chunk_index': 0}
```

**This is the cheapest way to tune chunking.** You can iterate on `CHUNK_SIZE` and
inspect the results without spending a single API call.

---

## 10.3 Splitting

```python
def split_documents(settings: Settings, documents: list[Document]) -> list[Document]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        length_function=len,
        separators=[
            "\n## ",    # markdown h2
            "\n### ",   # markdown h3
            "\n```",    # fenced code block boundary
            "\n\n",     # paragraph
            "\n",       # line
            ". ",       # sentence
            " ",        # word
            "",         # character (last resort)
        ],
        keep_separator=True,
    )

    chunks = splitter.split_documents(documents)

    per_source_counter: dict[str, int] = {}
    for chunk in chunks:
        source = chunk.metadata.get("source", "unknown")
        index = per_source_counter.get(source, 0)
        chunk.metadata["chunk_index"] = index
        per_source_counter[source] = index + 1

    meaningful = [c for c in chunks if len(c.page_content.strip()) >= 20]
    if len(meaningful) < len(chunks):
        warn(f"dropped {len(chunks) - len(meaningful)} near-empty chunk(s)")

    return meaningful
```

Chapter 5 covered the separator list. Two implementation notes:

**`length_function=len`** measures characters. Simple, fast, predictable. The
alternative is a token counter, which is more accurate for prompt budgeting but
requires a tokeniser and makes the numbers less intuitive. For a project you are
learning from, characters win.

**`split_documents` preserves metadata.** Each chunk inherits `source`, `page`,
`sheet` and `file_type` from its parent. That is why citations survive the split.

---

## 10.4 Deterministic chunk IDs — the heart of the design

This function is short and it is the most important in the file.

```python
def chunk_id(chunk: Document, collection_name: str) -> str:
    source = str(chunk.metadata.get("source", "unknown"))
    index = str(chunk.metadata.get("chunk_index", 0))
    digest = hashlib.sha256(
        f"{collection_name}|{source}|{index}|{chunk.page_content}".encode("utf-8")
    ).hexdigest()
    return f"{collection_name}:{source}:{index}:{digest[:16]}"
```

### Why deterministic IDs at all

Chapter 4 noted that `langchain_pg_embedding.id` is a plain `varchar` you may
supply yourself, and that the store upserts on it:

```sql
ON CONFLICT (id) DO UPDATE SET embedding = ..., document = ..., cmetadata = ...
```

So if the same content produces the same ID, **re-running ingestion overwrites
rather than duplicates**.

Without this, `python ingest.py` twice would double every chunk. Retrieval would
then return the same passage four times, filling all four slots with one paragraph.
The system would appear to work while quietly getting much worse.

With it, ingestion is **idempotent**. Verified: running twice left the count at
18 → 18.

### Why the content is in the hash

The hash covers the chunk's *text*. Edit a document and the text changes, so the
hash changes, so you get a genuinely new row rather than a stale one masquerading
as current.

### Why the collection name is in the hash

This line exists because of a real bug. Chapter 4 §4.6 introduced it; here is the
operational consequence.

The original implementation hashed only `(source, index, text)`. Ingesting the same
`data/` folder into a second collection produced **identical IDs**, hit the
primary-key conflict, and updated rows still owned by the **first** collection.

The result: the second collection was empty, while ingestion reported
*"18/18 chunks stored"*. A completely silent failure.

Including `collection_name` makes IDs unique per collection. There is now a
regression test that ingests into two collections and asserts each keeps its own
chunks — it passes.

> **The transferable lesson:** an upsert only updates the columns you name. If a
> row's real identity spans more than the primary key, put all of it *in* the key.

---

## 10.5 Embedding and storing

```python
COHERE_MAX_BATCH_SIZE = 96      # defined in rag.py

def store_chunks(settings: Settings, chunks: list[Document], reset: bool) -> int:
    embeddings = build_embeddings(settings)

    info("  checking Cohere credentials...")
    preflight_check_embeddings(embeddings)
    info("  Cohere OK")

    vector_store = build_vector_store(
        settings, embeddings=embeddings, pre_delete_collection=reset
    )
    if reset:
        info(f"  collection '{settings.collection_name}' dropped and recreated")

    total = len(chunks)
    written = 0
    batch_count = (total + COHERE_MAX_BATCH_SIZE - 1) // COHERE_MAX_BATCH_SIZE

    for batch_number, start in enumerate(range(0, total, COHERE_MAX_BATCH_SIZE), start=1):
        batch = chunks[start : start + COHERE_MAX_BATCH_SIZE]
        ids = [chunk_id(chunk, settings.collection_name) for chunk in batch]

        started = time.perf_counter()
        vector_store.add_documents(batch, ids=ids)
        elapsed = time.perf_counter() - started

        written += len(batch)
        info(
            f"  batch {batch_number}/{batch_count}: "
            f"{written}/{total} chunks stored ({elapsed:.1f}s)"
        )

    return written
```

### Why batch at 96

Three reasons, all practical:

1. **Cohere's hard limit.** More than 96 texts per request is rejected.
2. **Blast radius.** A failure costs one batch, not the whole run.
3. **Progress.** You see movement instead of a silent multi-minute wait.

The ceiling division `(total + 95) // 96` computes batch count without importing
`math`.

### The preflight check

```python
def preflight_check_embeddings(embeddings) -> None:
    cohere_logger = logging.getLogger("langchain_cohere")
    previous_level = cohere_logger.level
    cohere_logger.setLevel(logging.ERROR)
    try:
        embeddings.embed_query("connection test")
    finally:
        cohere_logger.setLevel(previous_level)
```

This exists because of a genuinely bad first experience.

Without it, a wrong API key is discovered *inside* the batch loop — after
LangChain's retry logic has already dumped **three enormous 401 tracebacks**,
each containing every HTTP response header, burying the one line that says what to
fix.

One tiny call upfront turns that into:

```
  checking Cohere credentials...
[X] Cohere rejected your API key.
```

The logger suppression is the second half of the fix: `langchain_cohere` logs retry
warnings at WARNING level, and retrying a 401 is pointless noise. We silence that
logger for the duration and restore it in a `finally`.

**A design principle worth taking away:** validate credentials before doing
expensive work, not during it.

---

## 10.6 Error messages that lead with the fix

```python
MAX_RAW_ERROR_CHARS = 400

def explain_ingestion_failure(exc: Exception) -> str:
    message = str(exc)
    low = message.lower()

    if "unauthorized" in low or "401" in low or "incorrect api key" in low:
        hint = (
            "Cohere rejected your API key.\n\n"
            "  1. Open https://dashboard.cohere.com/api-keys\n"
            "  2. Copy the key\n"
            "  3. Paste it as COHERE_API_KEY in your .env file"
        )
    elif "429" in low or "rate limit" in low:
        hint = (
            "Cohere rate-limited the request.\n\n"
            "Trial keys allow roughly 100 calls/minute. Wait a minute and run\n"
            "  python ingest.py\n"
            "again — already-stored chunks are skipped, so it resumes cheaply."
        )
    elif "extension" in low and "vector" in low:
        hint = (
            "The `vector` extension is missing from the database.\n\n"
            "docker-compose.yml must use the `pgvector/pgvector` image...")
    elif "expected" in low and "dimension" in low:
        hint = (
            "Vector size mismatch.\n\n"
            "The collection already holds vectors from a different embedding\n"
            "model. Rebuild it from scratch:\n"
            "  python ingest.py --reset")
    elif any(t in low for t in ("connection", "refused", "timeout expired")):
        hint = (
            "Postgres is not reachable.\n\n"
            "  docker compose ps            # is devbrain-postgres healthy?\n"
            "  docker compose up -d         # start it\n"
            "  docker compose logs postgres # read the error")
    else:
        hint = "Ingestion failed."

    raw = message if len(message) <= MAX_RAW_ERROR_CHARS else message[:MAX_RAW_ERROR_CHARS] + " ...[truncated]"
    return f"{hint}\n\nRaw error ({type(exc).__name__}):\n  {raw}"
```

Two design choices:

**The hint comes first, the raw error second.** SDK exceptions stringify to include
every HTTP header. Leading with that buries the useful line.

**The raw error is truncated to 400 characters** — enough to identify the failure,
short enough not to drown the advice. It is kept, not discarded, because sometimes
you need it.

Notice the fourth branch: *"Vector size mismatch"*. That is Chapter 3's warning
about changing embedding models, turned into a message that tells you exactly which
command fixes it.

---

## 10.7 Reusable ingestion

The CLI is one caller. Chapter 16 adds another — the web upload endpoint. Both go
through one function:

```python
def ingest_files(
    settings: Settings,
    paths: list[Path],
    extra_metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    import db

    results: list[dict[str, object]] = []
    all_chunks: list[Document] = []

    for path in paths:
        try:
            label = path.relative_to(settings.data_dir).as_posix()
        except ValueError:
            label = path.name

        try:
            documents = load_document(path, source_label=label)
            if not documents:
                results.append({"source": label, "chunks": 0,
                                "error": "No extractable text (a scanned/image-only PDF is the usual cause)."})
                continue

            chunks = split_documents(settings, documents)
            if not chunks:
                results.append({"source": label, "chunks": 0, "error": "Produced no usable chunks."})
                continue

            # Attribution travels with the chunk into pgvector.
            if extra_metadata:
                for chunk in chunks:
                    chunk.metadata.update(extra_metadata)

            # Replace, don't accumulate.
            db.delete_chunks_for_source(settings, label)

            all_chunks.extend(chunks)
            results.append({"source": label, "chunks": len(chunks), "error": None})

        except UnsupportedFileType as exc:
            results.append({"source": label, "chunks": 0, "error": str(exc)})
        except Exception as exc:
            results.append({"source": label, "chunks": 0, "error": f"{type(exc).__name__}: {exc}"})

    written = store_chunks(settings, all_chunks, reset=False) if all_chunks else 0
    return {"files": results, "chunks_added": written}
```

### Why one shared function

An uploaded file is chunked, embedded and stored by **exactly the same code** as a
file dropped into `data/` by hand. There is no second, subtly-different path to
maintain or to diverge.

### Per-file error isolation

Each file is wrapped individually. One unreadable PDF in a batch of ten does not
lose you the other nine — it is reported and the rest proceed.

### Replacement, not accumulation

```python
db.delete_chunks_for_source(settings, label)
```

Re-uploading an *edited* file is the subtle case. Deterministic IDs handle
*unchanged* chunks (they upsert), but edited text produces **new** IDs — so the old
chunks would linger forever, still being retrieved, still answering from content
you deleted.

Deleting by source first makes replacement clean. Verified: re-uploading gave
`chunks_added=1` while the total stayed at 22.

### `extra_metadata`

An extension point. Chapter 16 passes `{"uploaded_by": ..., "uploaded_at": ...}`,
which is stamped onto every chunk and travels into pgvector — so a retrieved chunk
can tell you who contributed it.

---

## 10.8 Running it

```bash
python ingest.py
```

Real output:

```
======================================================================
DevBrain ingestion
======================================================================
Data folder      : C:\ClaudeCode Projects\DevBrain\data
Database         : postgresql+psycopg://devbrain:****@localhost:5433/devbrain
Collection       : devbrain_docs
Embedding model  : embed-english-v3.0
Chunk size       : 1000 chars (overlap 150)

[1/4] Discovering files
  found 6 file(s), 54.3 KB total

[2/4] Parsing documents
  ok      code-review-standards.docx  (1 part(s), 1,854 chars)
  ok      docker-tips.md  (1 part(s), 2,600 chars)
  ok      incident-response-runbook.pdf  (2 part(s), 2,084 chars)
  ok      postgres-best-practices.md  (1 part(s), 2,316 chars)
  ok      python-async-notes.txt  (1 part(s), 2,641 chars)
  ok      tech-stack-decisions.xlsx  (2 part(s), 1,963 chars)
  extracted 8 document part(s)

[3/4] Splitting into chunks
  created 18 chunk(s), average 760 chars

[4/4] Embedding with Cohere and storing in pgvector
  checking Cohere credentials...
  Cohere OK
  batch 1/1: 18/18 chunks stored (0.6s)

======================================================================
Done. 18 chunk(s) from 6 file(s) in 1.4s.
======================================================================
```

Note the database line: the password is masked by `safe_database_url` from
Chapter 8. That output is safe to paste into a bug report.

---

## 10.9 Checkpoint

**1. Dry run first — costs nothing**

```bash
python ingest.py --dry-run
```

**2. Real ingestion**

```bash
python ingest.py
```

**3. Confirm what landed**

```bash
python ingest.py --stats
```

```
Collection : devbrain_docs
Chunks     : 18

By source:
  postgres-best-practices.md         3 chunks
  docker-tips.md                     3 chunks
  ...
```

**4. Prove idempotency**

```bash
python ingest.py
python ingest.py --stats
```

The chunk count must be **unchanged**. If it doubled, your chunk IDs are not
deterministic.

**5. Look at it in the database**

```sql
SELECT c.name AS collection,
       count(*) AS chunks,
       vector_dims((array_agg(e.embedding))[1]) AS dims
FROM langchain_pg_embedding e
JOIN langchain_pg_collection c ON c.uuid = e.collection_id
GROUP BY 1;
```

```
    collection     | chunks | dims
-------------------+--------+------
 devbrain_docs     |     18 | 1024
```

**1024 dimensions.** That is Cohere's `embed-english-v3.0`, exactly as Chapter 3
promised. Your documents are now searchable vectors in an ordinary Postgres table.

---

## 10.10 Mistakes to avoid

**Expecting `ingest.py` to notice deletions.** It scans `data/` and adds or updates.
It cannot know about text you *removed* — those chunks are already in the database.
After editing or deleting files, use `--reset`.

**Running `--reset` when you meant plain ingestion.** `--reset` drops the entire
collection. Cheap here (1.4s to rebuild), expensive on a large corpus.

**Ignoring the `skipped` list.** If your file never shows up in answers, check
whether discovery skipped it.

**Assuming an `empty` warning is harmless.** It means a document contributed
*nothing*. Usually a scanned PDF. Chapter 9 §9.5 explains.

**Batching above 96.** Cohere rejects it.

---

## 10.11 What you have

A complete write path. Documents in `data/` become searchable vectors in Postgres,
idempotently, with per-file error isolation and messages that tell you what to fix.

Part III is done. Part IV builds the read path — starting with the RAG chain
itself, which is smaller than you probably expect.

*Next: [Chapter 11 — The RAG Pipeline](ch11-rag-pipeline.md)*

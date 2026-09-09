"""
DevBrain — document ingestion engine.

This is the "write path" of the RAG system. Run it whenever you add or change
files in the /data folder.

    python ingest.py                # add/update documents (safe to re-run)
    python ingest.py --reset        # wipe the collection and rebuild from scratch
    python ingest.py --dry-run      # show what would be ingested, call no APIs
    python ingest.py --stats        # just report what is already in the database

What it does, in order:

    1. DISCOVER  walk /data and find every supported file
    2. LOAD      parse each file into LangChain Documents (see loaders.py)
    3. SPLIT     cut long documents into ~1000-character overlapping chunks
    4. EMBED     ask Cohere to turn each chunk into a 1024-number vector
    5. STORE     write text + metadata + vector into Postgres/pgvector

Step 3 exists because of two hard limits: an embedding model can only encode a
limited amount of text into one vector (and a whole 40-page PDF averaged into
1024 numbers is too blurry to search), and the LLM has a finite prompt budget.
Small chunks give sharp retrieval; the overlap stops a sentence that straddles a
boundary from losing its meaning.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
import time
from dataclasses import replace
from pathlib import Path

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from config import Settings, load_settings
from loaders import (
    SUPPORTED_EXTENSIONS,
    UnsupportedFileType,
    discover_files,
    load_document,
)
from rag import COHERE_MAX_BATCH_SIZE, build_embeddings, build_vector_store


# --------------------------------------------------------------------------- #
#  Console helpers (plain ASCII so Windows terminals never mangle the output)
# --------------------------------------------------------------------------- #

def info(message: str) -> None:
    print(message, flush=True)


def step(number: int, total: int, message: str) -> None:
    print(f"\n[{number}/{total}] {message}", flush=True)


def warn(message: str) -> None:
    print(f"  !  {message}", flush=True)


def fail(message: str) -> None:
    print(f"\n[X] {message}", file=sys.stderr, flush=True)


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


# --------------------------------------------------------------------------- #
#  Step 2 — load
# --------------------------------------------------------------------------- #


def load_all_documents(settings: Settings, files: list[Path]) -> list[Document]:
    """Parse every discovered file, reporting per-file results as we go."""
    documents: list[Document] = []

    for path in files:
        # A short relative label like "python/asyncio.md" is what the UI cites.
        try:
            label = path.relative_to(settings.data_dir).as_posix()
        except ValueError:
            label = path.name

        try:
            loaded = load_document(path, source_label=label)
        except UnsupportedFileType as exc:
            warn(f"skipped {label}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - one bad file must not abort the run
            warn(f"failed  {label}: {type(exc).__name__}: {exc}")
            continue

        if not loaded:
            warn(
                f"empty   {label}: no extractable text "
                "(a scanned/image-only PDF is the usual cause)"
            )
            continue

        characters = sum(len(d.page_content) for d in loaded)
        info(f"  ok      {label}  ({len(loaded)} part(s), {characters:,} chars)")
        documents.extend(loaded)

    return documents


# --------------------------------------------------------------------------- #
#  Step 3 — split
# --------------------------------------------------------------------------- #


def split_documents(settings: Settings, documents: list[Document]) -> list[Document]:
    """
    Split documents into overlapping chunks.

    `RecursiveCharacterTextSplitter` tries the separators in order and only
    moves to a more aggressive one when a piece is still too big. Splitting on
    paragraph breaks first, sentences next, and single characters only as a last
    resort keeps chunks semantically whole.

    The separator list below is tuned for technical notes: markdown headings and
    fenced code blocks are treated as strong boundaries so a code snippet is far
    less likely to be cut in half.
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        # Measure length in characters — simple, fast, and predictable.
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

    # Record each chunk's position within its parent document. This is useful
    # metadata to have in pgAdmin and in LangSmith traces when debugging why a
    # particular chunk was (or wasn't) retrieved.
    per_source_counter: dict[str, int] = {}
    for chunk in chunks:
        source = chunk.metadata.get("source", "unknown")
        index = per_source_counter.get(source, 0)
        chunk.metadata["chunk_index"] = index
        per_source_counter[source] = index + 1

    # Drop chunks that are only whitespace or punctuation — embedding them
    # wastes API calls and pollutes search results.
    meaningful = [c for c in chunks if len(c.page_content.strip()) >= 20]
    if len(meaningful) < len(chunks):
        warn(f"dropped {len(chunks) - len(meaningful)} near-empty chunk(s)")

    return meaningful


def chunk_id(chunk: Document, collection_name: str) -> str:
    """
    Build a stable, content-derived ID for a chunk.

    The vector store upserts on this primary key (`ON CONFLICT DO UPDATE`), so a
    deterministic ID makes re-running ingestion idempotent: unchanged chunks are
    simply overwritten with identical data instead of being duplicated. Without
    this, running `python ingest.py` twice would double every answer's context.

    The hash covers the source, the chunk position, and the text itself, so an
    edit to a document produces a genuinely new ID.

    The collection name MUST be part of the ID. `langchain_pg_embedding.id` is a
    single global primary key shared by every collection, and the upsert clause
    only refreshes `embedding`/`document`/`cmetadata` — it does *not* touch
    `collection_id`. So if two collections derived the same ID from the same
    source file, the second ingestion would silently update rows still owned by
    the first collection, and the second collection would end up empty while
    reporting success.
    """
    source = str(chunk.metadata.get("source", "unknown"))
    index = str(chunk.metadata.get("chunk_index", 0))
    digest = hashlib.sha256(
        f"{collection_name}|{source}|{index}|{chunk.page_content}".encode("utf-8")
    ).hexdigest()
    return f"{collection_name}:{source}:{index}:{digest[:16]}"


# --------------------------------------------------------------------------- #
#  Steps 4 & 5 — embed and store
# --------------------------------------------------------------------------- #


def preflight_check_embeddings(embeddings) -> None:
    """
    Embed one short string to validate the Cohere key before the real work.

    Without this, a bad API key is discovered *inside* the batch loop, after
    LangChain's retry logic has already dumped three enormous 401 tracebacks to
    the console. Checking upfront turns that into one clean line.

    The `langchain_cohere` logger is quieted for the duration because its retry
    warnings are pure noise when the credentials are simply wrong.
    """
    cohere_logger = logging.getLogger("langchain_cohere")
    previous_level = cohere_logger.level
    cohere_logger.setLevel(logging.ERROR)
    try:
        embeddings.embed_query("connection test")
    finally:
        cohere_logger.setLevel(previous_level)


def store_chunks(settings: Settings, chunks: list[Document], reset: bool) -> int:
    """
    Embed chunks with Cohere and write them into pgvector, in batches.

    Batching matters for three reasons: Cohere accepts at most 96 texts per
    request, a failure costs you one batch instead of the whole run, and you get
    progress output instead of a silent multi-minute wait.
    """
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


def ingest_files(
    settings: Settings,
    paths: list[Path],
    extra_metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    """
    Ingest a specific list of files and return a machine-readable summary.

    This is the shared engine behind both entry points: the `ingest.py` CLI and
    the web upload endpoint. Keeping one implementation means an uploaded file
    is chunked, embedded and stored by exactly the same code as a file you drop
    into /data by hand — there is no second, subtly-different path to maintain.

    Re-uploading a file with the same name is treated as a REPLACEMENT: every
    chunk previously derived from that source is deleted before the new content
    is stored. Without that, editing a document would leave its old paragraphs
    in the database answering questions forever, because deterministic chunk IDs
    are content-derived and stale chunks simply never get overwritten.

    Returns:
        {"files": [{"source": str, "chunks": int, "error": str | None}, ...],
         "chunks_added": int}
    """
    import db  # local import keeps the CLI's start-up free of SQLAlchemy

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
                results.append({
                    "source": label,
                    "chunks": 0,
                    "error": "No extractable text (a scanned/image-only PDF is the usual cause).",
                })
                continue

            chunks = split_documents(settings, documents)
            if not chunks:
                results.append({"source": label, "chunks": 0, "error": "Produced no usable chunks."})
                continue

            # Stamp ownership onto every chunk. This metadata travels with the
            # chunk into pgvector, so when it is later retrieved the API can
            # tell the reader who contributed the source document.
            if extra_metadata:
                for chunk in chunks:
                    chunk.metadata.update(extra_metadata)

            # Replace, don't accumulate.
            db.delete_chunks_for_source(settings, label)

            all_chunks.extend(chunks)
            results.append({"source": label, "chunks": len(chunks), "error": None})

        except UnsupportedFileType as exc:
            results.append({"source": label, "chunks": 0, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - reported per file, never aborts the batch
            results.append({"source": label, "chunks": 0, "error": f"{type(exc).__name__}: {exc}"})

    written = store_chunks(settings, all_chunks, reset=False) if all_chunks else 0
    return {"files": results, "chunks_added": written}


def report_stats(settings: Settings) -> None:
    """
    Print what is currently stored.

    This queries Postgres with plain SQL rather than going through LangChain,
    which doubles as a demonstration that your embeddings really are sitting in
    ordinary database rows you can inspect yourself (try the same query in
    pgAdmin).
    """
    from db import count_chunks, count_chunks_by_source

    total = count_chunks(settings)
    rows = count_chunks_by_source(settings)

    info(f"\nCollection : {settings.collection_name}")
    info(f"Database   : {settings.safe_database_url}")
    info(f"Chunks     : {total}")
    if rows:
        info("\nBy source:")
        width = max(len(source) for source, _ in rows)
        for source, chunks in rows:
            info(f"  {source:<{width}}  {chunks:>5} chunks")


# --------------------------------------------------------------------------- #
#  Error reporting
# --------------------------------------------------------------------------- #

# Upstream SDKs raise exceptions whose str() includes every HTTP response
# header. Dumping that whole wall of text buries the one line that tells the
# user what to fix, so we lead with the fix and truncate the raw detail.
MAX_RAW_ERROR_CHARS = 400


def explain_ingestion_failure(exc: Exception) -> str:
    """Turn an upstream exception into a message that leads with the remedy."""
    message = str(exc)
    low = message.lower()

    if "unauthorized" in low or "401" in low or "invalid api" in low or "incorrect api key" in low:
        hint = (
            "Cohere rejected your API key.\n\n"
            "  1. Open https://dashboard.cohere.com/api-keys\n"
            "  2. Copy the key\n"
            "  3. Paste it as COHERE_API_KEY in your .env file"
        )
    elif "429" in low or "rate limit" in low or "too many requests" in low:
        hint = (
            "Cohere rate-limited the request.\n\n"
            "Trial keys allow roughly 100 calls/minute. Wait a minute and run\n"
            "  python ingest.py\n"
            "again — already-stored chunks are skipped, so it resumes cheaply."
        )
    elif "extension" in low and "vector" in low:
        hint = (
            "The `vector` extension is missing from the database.\n\n"
            "docker-compose.yml must use the `pgvector/pgvector` image, not a\n"
            "plain `postgres` image. Then rebuild:\n"
            "  docker compose down -v\n"
            "  docker compose up -d"
        )
    elif "expected" in low and "dimension" in low:
        hint = (
            "Vector size mismatch.\n\n"
            "The collection already holds vectors from a different embedding\n"
            "model. Rebuild it from scratch:\n"
            "  python ingest.py --reset"
        )
    elif any(term in low for term in ("connection", "could not connect", "refused", "timeout expired")):
        hint = (
            "Postgres is not reachable.\n\n"
            "  docker compose ps            # is devbrain-postgres healthy?\n"
            "  docker compose up -d         # start it\n"
            "  docker compose logs postgres # read the error\n\n"
            "Also check that the port in DATABASE_URL matches POSTGRES_HOST_PORT."
        )
    else:
        hint = "Ingestion failed."

    raw = message if len(message) <= MAX_RAW_ERROR_CHARS else message[:MAX_RAW_ERROR_CHARS] + " ...[truncated]"
    return f"{hint}\n\nRaw error ({type(exc).__name__}):\n  {raw}"


# --------------------------------------------------------------------------- #
#  Entry point
# --------------------------------------------------------------------------- #


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ingest.py",
        description="Load documents from /data into the DevBrain vector store.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python ingest.py                 add or update documents\n"
            "  python ingest.py --reset         rebuild the index from scratch\n"
            "  python ingest.py --dry-run       preview without calling any API\n"
            "  python ingest.py --stats         show what is already stored\n"
        ),
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete the existing collection first. Use this after EDITING or "
             "DELETING documents, so stale chunks are removed.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="override the DATA_DIR setting from .env",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="discover, load and split, but do not embed or write to Postgres",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="report what is currently in the vector store, then exit",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = load_settings(exit_on_error=True)

    if args.data_dir is not None:
        data_dir = args.data_dir if args.data_dir.is_absolute() else Path.cwd() / args.data_dir
        settings = replace(settings, data_dir=data_dir)

    for message in settings.warnings:
        warn(message)

    info("=" * 70)
    info("DevBrain ingestion")
    info("=" * 70)
    info(f"Data folder      : {settings.data_dir}")
    info(f"Database         : {settings.safe_database_url}")
    info(f"Collection       : {settings.collection_name}")
    info(f"Embedding model  : {settings.cohere_embed_model}")
    info(f"Chunk size       : {settings.chunk_size} chars (overlap {settings.chunk_overlap})")

    if args.stats:
        try:
            report_stats(settings)
        except Exception as exc:  # noqa: BLE001
            fail(
                f"Could not read the vector store: {type(exc).__name__}: {exc}\n\n"
                "Is Postgres running?  ->  docker compose ps\n"
                "Has anything been ingested yet?  ->  python ingest.py"
            )
            return 1
        return 0

    total_steps = 3 if args.dry_run else 4

    # ---- 1. discover -----------------------------------------------------
    step(1, total_steps, "Discovering files")
    if not settings.data_dir.exists():
        fail(
            f"Data folder not found: {settings.data_dir}\n\n"
            "Create it and drop your notes in, then run this again:\n"
            f'  mkdir "{settings.data_dir}"'
        )
        return 1

    files, skipped = discover_files(settings.data_dir)

    if skipped:
        warn(f"ignoring {len(skipped)} unsupported file(s):")
        for path in skipped[:10]:
            warn(f"    {path.name}")
        if len(skipped) > 10:
            warn(f"    ... and {len(skipped) - 10} more")

    if not files:
        fail(
            f"No supported documents found in {settings.data_dir}\n\n"
            f"Supported types: {', '.join(sorted(SUPPORTED_EXTENSIONS))}\n"
            "Add at least one file and run this again."
        )
        return 1

    total_bytes = sum(p.stat().st_size for p in files)
    info(f"  found {len(files)} file(s), {human_size(total_bytes)} total")

    # ---- 2. load ---------------------------------------------------------
    step(2, total_steps, "Parsing documents")
    documents = load_all_documents(settings, files)
    if not documents:
        fail(
            "No text could be extracted from any file.\n\n"
            "If your PDFs are scans (images of pages) they contain no text layer,\n"
            "so there is nothing to embed. Convert them with OCR first, or start\n"
            "with a .txt/.md file to verify the pipeline works."
        )
        return 1
    info(f"  extracted {len(documents)} document part(s)")

    # ---- 3. split --------------------------------------------------------
    step(3, total_steps, "Splitting into chunks")
    chunks = split_documents(settings, documents)
    if not chunks:
        fail("Splitting produced no usable chunks. Are your documents almost empty?")
        return 1

    average = sum(len(c.page_content) for c in chunks) // len(chunks)
    info(f"  created {len(chunks)} chunk(s), average {average} chars")

    if args.dry_run:
        info("\n--dry-run: stopping before embedding. Nothing was written.")
        info("\nPreview of the first chunk:")
        info("-" * 70)
        info(chunks[0].page_content[:500])
        info("-" * 70)
        info(f"metadata: {chunks[0].metadata}")
        return 0

    # ---- 4. embed + store ------------------------------------------------
    step(4, total_steps, "Embedding with Cohere and storing in pgvector")
    started = time.perf_counter()
    try:
        written = store_chunks(settings, chunks, reset=args.reset)
    except Exception as exc:  # noqa: BLE001
        fail(explain_ingestion_failure(exc))
        return 1

    elapsed = time.perf_counter() - started

    info("\n" + "=" * 70)
    info(f"Done. {written} chunk(s) from {len(files)} file(s) in {elapsed:.1f}s.")
    info("=" * 70)
    info("\nNext step — start the web app:")
    info("  uvicorn app:app --reload --port 8001")
    info("Then open http://localhost:8001")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

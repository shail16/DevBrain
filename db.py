"""
DevBrain — direct database helpers.

Everything in here uses plain SQL instead of going through LangChain. That is
deliberate: it powers the `/api/health` endpoint and `ingest.py --stats`, and it
demonstrates the most reassuring fact about this project — your embeddings are
not locked inside a black box. They are ordinary rows in two ordinary tables
that you can query yourself in pgAdmin:

    langchain_pg_collection   one row per named collection
    langchain_pg_embedding    one row per chunk (id, document, cmetadata, embedding)
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import Engine, create_engine, text

from config import Settings


@lru_cache(maxsize=4)
def _engine_for(database_url: str) -> Engine:
    """
    One cached SQLAlchemy engine per connection string.

    Cached because creating an engine sets up a connection pool; doing that on
    every health check would leak connections until Postgres refused new ones.
    """
    return create_engine(
        database_url,
        pool_pre_ping=True,
        pool_recycle=1800,
        pool_size=5,
        max_overflow=10,
    )


def get_engine(settings: Settings) -> Engine:
    return _engine_for(settings.database_url)


def ping(settings: Settings) -> bool:
    """Return True if Postgres answers a trivial query."""
    with get_engine(settings).connect() as connection:
        return connection.execute(text("SELECT 1")).scalar_one() == 1


def pgvector_installed(settings: Settings) -> bool:
    """Return True if the `vector` extension is enabled in this database."""
    with get_engine(settings).connect() as connection:
        return bool(
            connection.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
            ).scalar()
        )


def collection_exists(settings: Settings) -> bool:
    """
    Return True if the collection tables exist and hold our collection.

    Checked separately from the chunk count so the health endpoint can tell
    "you have never run ingestion" apart from "ingestion ran but found nothing".
    """
    with get_engine(settings).connect() as connection:
        tables_present = connection.execute(
            text("SELECT to_regclass('public.langchain_pg_collection') IS NOT NULL")
        ).scalar_one()
        if not tables_present:
            return False
        return bool(
            connection.execute(
                text("SELECT 1 FROM langchain_pg_collection WHERE name = :name"),
                {"name": settings.collection_name},
            ).scalar()
        )


_COUNT_SQL = """
SELECT count(*)
FROM langchain_pg_embedding e
JOIN langchain_pg_collection c ON c.uuid = e.collection_id
WHERE c.name = :name
"""

_BY_SOURCE_SQL = """
SELECT coalesce(e.cmetadata ->> 'source', 'unknown') AS source, count(*) AS chunks
FROM langchain_pg_embedding e
JOIN langchain_pg_collection c ON c.uuid = e.collection_id
WHERE c.name = :name
GROUP BY 1
ORDER BY 2 DESC, 1
"""


def count_chunks(settings: Settings) -> int:
    """Total number of stored chunks in this collection (0 if not set up yet)."""
    if not collection_exists(settings):
        return 0
    with get_engine(settings).connect() as connection:
        return int(
            connection.execute(
                text(_COUNT_SQL), {"name": settings.collection_name}
            ).scalar_one()
        )


def count_chunks_by_source(settings: Settings) -> list[tuple[str, int]]:
    """Chunk counts grouped by source file, biggest first."""
    if not collection_exists(settings):
        return []
    with get_engine(settings).connect() as connection:
        rows = connection.execute(
            text(_BY_SOURCE_SQL), {"name": settings.collection_name}
        ).all()
    return [(str(row[0]), int(row[1])) for row in rows]


_DELETE_BY_SOURCE_SQL = """
DELETE FROM langchain_pg_embedding e
USING langchain_pg_collection c
WHERE c.uuid = e.collection_id
  AND c.name = :name
  AND e.cmetadata ->> 'source' = :source
"""


def delete_chunks_for_source(settings: Settings, source: str) -> int:
    """
    Remove every chunk that came from one source document.

    Used when a file is re-uploaded or replaced. Chunk IDs are derived from the
    document's text, so an edited file produces brand-new IDs and the previous
    chunks would otherwise linger in the collection forever, still being
    retrieved and still answering questions from content you deleted.

    Returns the number of rows removed (0 if the document was never ingested).
    """
    if not collection_exists(settings):
        return 0
    engine = get_engine(settings)
    with engine.begin() as connection:          # begin() commits on success
        result = connection.execute(
            text(_DELETE_BY_SOURCE_SQL),
            {"name": settings.collection_name, "source": source},
        )
        return int(result.rowcount or 0)

# Chapter 4 — Vector Databases and pgvector

*© 2026 Shailendra Srivastava. All rights reserved.*

---

## 4.1 The problem

After ingestion you have thousands of vectors sitting somewhere. For every
question, you must find the handful closest to the question's vector.

The naive approach works fine at first:

```python
# Conceptually, what "find the nearest" means
scores = [cosine(question_vector, stored) for stored in all_vectors]
top_4 = sorted(scores, reverse=True)[:4]
```

For a hundred documents this is instant. For a million it collapses — you are
computing a million 1024-dimensional dot products on every single question, in
Python, while a user waits.

A **vector database** stores vectors and answers *"which stored vectors are nearest
to this one?"* efficiently, with specialised indexes and native code doing the
arithmetic.

---

## 4.2 Why PostgreSQL, and not a dedicated vector database

There are excellent purpose-built vector databases — Pinecone, Weaviate, Qdrant,
Chroma, Milvus. DevBrain uses PostgreSQL with the **pgvector** extension instead.
This is a real trade-off, and you should understand both sides.

### The case for pgvector

**One database, not two.** Your vectors live beside your relational data. Same
backups, same access control, same monitoring, same connection pooling, same
tooling you already know. When DevBrain later adds users and document ownership
(Chapter 14), those tables sit in the *same* database as the embeddings — so
"which documents did Alice upload, and how many chunks do they have?" is a single
`JOIN`, not a distributed query across two systems.

**Transactions.** You can insert a document row and its chunks atomically. With a
separate vector store, you have two systems that can disagree after a crash.

**Nothing is a black box.** You can open pgAdmin and look at your embeddings. That
sounds minor until you are debugging.

**One fewer vendor.** No extra service to run, secure, pay for or learn.

### The case against

**Scale.** Purpose-built stores scale further. Past roughly 10–50 million vectors,
or when you need sub-10ms p99 latency at high concurrency, specialised systems
start to win on raw performance.

**Features.** Some offer richer filtering, multi-tenancy primitives, or built-in
hybrid search that you would assemble yourself in Postgres.

### The verdict for this project

For a personal or team knowledge base — thousands to low millions of chunks —
**pgvector is comfortably the right choice**, and the operational simplicity is
worth more than the theoretical headroom.

Be honest about the crossover point rather than tribal about it. If you are
building for 100 million vectors, use something else.

---

## 4.3 What pgvector actually adds

pgvector is a PostgreSQL extension. Installing it teaches Postgres two new things.

### 1. A `vector` column type

```sql
CREATE TABLE items (
    id        SERIAL PRIMARY KEY,
    content   TEXT,
    embedding vector(1024)     -- ← new type, fixed dimension
);
```

Note `vector(1024)`. The dimension is part of the type. Try to insert a
1536-dimensional vector and Postgres rejects it — which is exactly the safety net
Chapter 3 warned you would need.

### 2. Distance operators

| Operator | Distance | Use when |
|---|---|---|
| `<=>` | **Cosine** | Text embeddings — what DevBrain uses |
| `<->` | Euclidean (L2) | Vectors where magnitude is meaningful |
| `<#>` | Negative inner product | Pre-normalised vectors, speed-critical |

With those, a nearest-neighbour search is just SQL:

```sql
SELECT document, cmetadata
FROM langchain_pg_embedding
ORDER BY embedding <=> '[0.021, -0.114, ...]'   -- the question's vector
LIMIT 4;
```

That is the entire retrieval mechanism. `ORDER BY distance LIMIT k`. Everything
else in this book is plumbing around that one query.

### Distance versus similarity

A detail that causes confusion: `<=>` returns **cosine distance**, not similarity.

```
cosine_distance = 1 - cosine_similarity
```

So *lower is better* for `<=>`, while *higher is better* for the similarity scores
you saw in Chapter 3. LangChain converts between them for you — its
`similarity_search_with_relevance_scores()` returns the 0–1 "higher is better"
form. Just be aware which one you are looking at when reading raw SQL.

---

## 4.4 The Docker image that matters

Here is a trap worth flagging early, because the error it produces is confusing.

```yaml
# docker-compose.yml
services:
  postgres:
    image: pgvector/pgvector:pg17     # ← NOT postgres:17
```

The stock `postgres:17` image **does not include pgvector**. If you use it,
everything appears fine until ingestion tries `CREATE EXTENSION vector` and fails
with a message about a missing extension — which does not obviously point at your
image choice.

`pgvector/pgvector:pg17` is plain PostgreSQL 17 with the extension pre-compiled.
Chapter 7 builds the full compose file; for now, just note the image name.

---

## 4.5 The two tables

`langchain-postgres` creates these automatically on first use. You will not write
this DDL, but you must understand it — several later chapters (and one real bug)
depend on it.

### `langchain_pg_collection`

One row per named knowledge base.

| Column | Type | Meaning |
|---|---|---|
| `uuid` | UUID | Primary key |
| `name` | varchar | Your `COLLECTION_NAME`, e.g. `devbrain_docs` |
| `cmetadata` | JSON | Optional metadata about the collection |

A **collection** is a logical grouping — think of it as a table name. Two
collections can coexist in one database, letting you keep, say, `devbrain_docs`
and `devbrain_archive` separate.

### `langchain_pg_embedding`

One row per chunk. This is where everything lives.

| Column | Type | Meaning |
|---|---|---|
| `id` | **varchar** | Primary key — DevBrain supplies a deterministic one (Chapter 10) |
| `collection_id` | UUID | FK to the collection, `ON DELETE CASCADE` |
| `document` | text | The chunk's raw text |
| `cmetadata` | **JSONB** | `source`, `page`, `sheet`, `chunk_index`, `uploaded_by`… |
| `embedding` | `vector(1024)` | The vector itself |

Three details to file away — each one matters later:

**`id` is a plain `varchar`, not a UUID.** That means you may supply *any* string
as the primary key. Chapter 10 exploits this to make ingestion idempotent.

**`cmetadata` is JSONB, not JSON.** JSONB is parsed and indexed (there is a GIN
index on it), so metadata filtering is fast. JSON would store raw text and be slow
to query.

**`ON DELETE CASCADE` on `collection_id`.** Delete a collection row and all its
chunks vanish with it. This is what makes `ingest.py --reset` clean.

---

## 4.6 The bug hiding in this schema

This deserves its own section, because it cost real debugging time and it teaches a
general lesson.

Look again at the primary key: `id` on `langchain_pg_embedding` is a **single
global primary key**, shared by *every* collection. It is not scoped per
collection.

Now look at how the library upserts:

```sql
INSERT INTO langchain_pg_embedding (...) VALUES (...)
ON CONFLICT (id) DO UPDATE
SET embedding = excluded.embedding,
    document  = excluded.document,
    cmetadata = excluded.cmetadata;
```

Read the `SET` clause carefully. **`collection_id` is not in it.**

So if two collections ever derive the same chunk ID from the same source file, the
second ingestion hits the primary-key conflict and **updates rows that still belong
to the first collection**. The second collection ends up empty — while ingestion
cheerfully reports *"18/18 chunks stored."*

That is exactly what happened during development. The fix (Chapter 10) is to
include the collection name in the ID hash. There is now a regression test that
ingests into two collections and asserts both keep their own chunks.

> **The general lesson:** an upsert only updates the columns you list. If a row's
> true identity spans more than the primary key, put all of it *in* the key.

Chapter 20 revisits this with the full diagnosis.

---

## 4.7 Indexes: why DevBrain has none

DevBrain deliberately creates **no** vector index. That surprises people, so here
is the reasoning.

Without an index, Postgres does an exact sequential scan: compare the query vector
against every row, sort, take the top *k*.

| | Exact scan (no index) | Approximate index (HNSW / IVFFlat) |
|---|---|---|
| Accuracy | **100% — always correct** | ~95–99% recall |
| Speed at 20k chunks | Single-digit milliseconds | Marginally faster |
| Speed at 5M chunks | Slow | Dramatically faster |
| Build cost | None | Time + memory |

Below roughly **100,000 chunks**, the exact scan is fast enough *and* perfectly
accurate. Approximate indexes trade recall for speed — a real trade, since a
missed chunk means a wrong answer — and that trade only pays off at scale.

When you outgrow it, adding an index is one statement:

```sql
CREATE INDEX ON langchain_pg_embedding
USING hnsw (embedding vector_cosine_ops);
```

Note `vector_cosine_ops` — the operator class must match the distance operator you
search with (`<=>`). Use the wrong one and the index is silently ignored.

**Do not add this prematurely.** Measure first. An index on a small table is pure
cost.

---

## 4.8 Hands-on: prove it is not magic

Once you have completed Chapter 10, come back and run these. This is the fastest
way to make the whole subject feel concrete.

Open pgAdmin at `http://localhost:5051`, expand **Servers → DevBrain Postgres →
Databases → devbrain → Schemas → public → Tables**, then open the Query Tool.

**How many chunks came from each document?**

```sql
SELECT cmetadata ->> 'source' AS document,
       count(*)               AS chunks
FROM langchain_pg_embedding
GROUP BY 1
ORDER BY 2 DESC;
```

**Look at an actual chunk and its vector.**

```sql
SELECT cmetadata ->> 'source'    AS document,
       left(document, 200)       AS text_preview,
       left(embedding::text, 80) AS vector_preview
FROM langchain_pg_embedding
LIMIT 3;
```

**Confirm the extension and the dimension.**

```sql
SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';

SELECT vector_dims(embedding) AS dimensions
FROM langchain_pg_embedding LIMIT 1;
```

That last one should return **1024**. If it returns something else, your embedding
model is not what you think it is.

---

## 4.9 Mistakes to avoid

**Using `postgres:17` instead of `pgvector/pgvector:pg17`.** The failure comes late
and the message does not point at the cause.

**Creating an index before you need one.** Costs build time and memory, sacrifices
recall, and buys nothing under ~100k chunks.

**Mismatching operator class and distance operator.** An `hnsw` index built with
`vector_l2_ops` will not serve `<=>` queries. Postgres silently falls back to a
sequential scan and you conclude indexes "don't help".

**Assuming the `id` column is scoped per collection.** It is not. See §4.6.

**Storing vectors from two different models in one collection.** Postgres enforces
the dimension but *not* the model. Same-size vectors from different models coexist
happily and return nonsense.

---

## 4.10 Checkpoint

1. Why does DevBrain use PostgreSQL rather than a dedicated vector database, and
   at roughly what scale would you reconsider?
2. What does `<=>` compute, and why is *lower* better for it while *higher* is
   better for the scores in Chapter 3?
3. `langchain_pg_embedding.id` is a plain `varchar` and globally unique. Name one
   opportunity and one danger that follows from this.
4. Why does DevBrain create no vector index, and what single statement would you
   run when it finally needs one?
5. `cmetadata` is JSONB rather than JSON. Why does that choice matter?

---

## What's next

You know where vectors live and how they are searched. But we have been assuming
documents arrive pre-cut into chunks. Chapter 5 explains why we cut them at all,
how big the pieces should be, and why they overlap.

*Next: [Chapter 5 — Chunking: Why We Cut Documents Up](ch05-chunking.md)*

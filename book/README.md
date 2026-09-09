# DevBrain from Scratch

### Building a Private AI Knowledge Base with RAG, FastAPI and pgvector

---

## About this book

This book teaches you to build **DevBrain** — a private, local Retrieval-Augmented
Generation system that answers questions using only your own documents — starting
from an empty folder and finishing with a containerised, multi-user application.

It is written for someone who can program but has **never built an AI application**.
No machine-learning background is assumed. Every concept is explained before it is
used, and every line of code is explained after it is written.

The promise of this book is specific: **when you finish, you will be able to
recreate the entire project from scratch without copying and pasting.** Not because
you memorised the code, but because you understand why each piece exists and what
would break without it.

---

## How to read it

Each chapter follows the same rhythm:

| Section | What it gives you |
|---|---|
| **The problem** | Why this piece exists at all. What breaks without it. |
| **The concept** | The idea, explained plainly, with analogies where they help. |
| **The code** | The real file from the project — not a simplified toy. |
| **Walkthrough** | Every meaningful line, and the reasoning behind it. |
| **Mistakes to avoid** | Traps I actually hit while building this. |
| **Checkpoint** | Prove it works before moving on. |

**Type the code, don't paste it.** The chapters are ordered so that each one leaves
you with something you can run. Resist the urge to skip ahead to the interesting
parts — Chapter 12 will not make sense without Chapter 3.

Where a chapter shows a real bug, that bug genuinely happened during development.
Those sections are the most valuable in the book, because working code teaches you
far less than code that *nearly* worked.

---

## What you will build

```
                    ┌─────────────────────────────────────────┐
                    │  Browser — sign in, ask, upload, admin   │
                    └────────────────────┬────────────────────┘
                                         │ HTTP
                    ┌────────────────────▼────────────────────┐
                    │  FastAPI  ·  auth · RAG · uploads        │
                    └───┬──────────────┬──────────────┬───────┘
                        │              │              │
              ┌─────────▼────┐  ┌──────▼──────┐  ┌────▼─────────┐
              │  Cohere      │  │  OpenAI     │  │  LangSmith   │
              │  embeddings  │  │  answers    │  │  tracing     │
              └─────────┬────┘  └─────────────┘  └──────────────┘
                        │
              ┌─────────▼──────────────────────┐
              │  PostgreSQL 17 + pgvector       │
              │  chunks · vectors · users       │
              └─────────────────────────────────┘
```

**By the end you will have:**

- A vector database running in Docker
- An ingestion pipeline that reads `.pdf`, `.docx`, `.xlsx`, `.txt`, `.md`
- A retrieval chain that answers **only** from your documents, with citations
- Sign-in, sign-up, sessions and role-based access
- Per-user document ownership and an admin console
- Full request tracing
- A production Dockerfile and a deployment guide

---

## Prerequisites

| You need | Level |
|---|---|
| Python | Comfortable with functions, classes, imports |
| SQL | Able to read a `SELECT` with a `JOIN` |
| HTML / CSS / JS | Able to read it; you will not write much |
| Docker | None — Chapter 6 teaches what you need |
| AI / ML | **None** |

You will also need free API keys from OpenAI, Cohere and (optionally) LangSmith.
Chapter 8 covers obtaining them safely.

---

## Table of contents

### Part I — Foundations

| Ch | Title | You will understand |
|---|---|---|
| 1 | [What DevBrain Is, and Why It Exists](ch01-what-devbrain-is.md) | The problem, the architecture, the whole map |
| 2 | [The Problem RAG Solves](ch02-the-problem-rag-solves.md) | Hallucination, and why retrieval beats fine-tuning |
| 3 | [Embeddings: Turning Meaning Into Numbers](ch03-embeddings.md) | Vectors, cosine similarity, semantic search |
| 4 | [Vector Databases and pgvector](ch04-vector-databases.md) | Why Postgres, the two tables, the `<=>` operator |
| 5 | [Chunking: Why We Cut Documents Up](ch05-chunking.md) | Chunk size, overlap, recursive splitting |

### Part II — Environment

| Ch | Title | You will build |
|---|---|---|
| 6 | [The Development Environment](ch06-dev-environment.md) | Project skeleton, venv, pinned dependencies |
| 7 | [Docker: PostgreSQL and pgAdmin](ch07-docker-postgres.md) | `docker-compose.yml`, healthchecks, volumes |
| 8 | [Configuration and Secrets](ch08-configuration.md) | `config.py`, environment variables, `set-env.ps1` |

### Part III — The Write Path

| Ch | Title | You will build |
|---|---|---|
| 9 | [Document Loaders](ch09-document-loaders.md) | `loaders.py` — PDF, Word, Excel, text |
| 10 | [The Ingestion Engine](ch10-ingestion-engine.md) | `ingest.py` — chunk, embed, store, idempotently |

### Part IV — The Read Path

| Ch | Title | You will build |
|---|---|---|
| 11 | [The RAG Pipeline](ch11-rag-pipeline.md) | `rag.py` — prompt, LCEL chain, citations |
| 12 | [The FastAPI Application](ch12-fastapi-application.md) | `app.py` — endpoints, lifespan, error translation |
| 13 | [The Frontend](ch13-frontend.md) | `index.html` — no framework, XSS-safe rendering |

### Part V — Multi-User

| Ch | Title | You will build |
|---|---|---|
| 14 | [Authentication and Sessions](ch14-authentication.md) | `auth.py` — bcrypt, sessions, why not JWT |
| 15 | [Users, Documents and the Admin Console](ch15-users-and-admin.md) | Ownership, attribution, role-gated routes |
| 16 | [File Upload and Live Ingestion](ch16-file-upload.md) | Multipart uploads, path-traversal defence |

### Part VI — Operations

| Ch | Title | You will understand |
|---|---|---|
| 17 | [Observability with LangSmith](ch17-observability.md) | Traces, spans, debugging retrieval vs generation |
| 18 | [Containerising the Application](ch18-containerising.md) | Multi-stage builds, non-root, healthchecks |
| 19 | [Deployment and Hardening](ch19-deployment.md) | Server setup, HTTPS, backups, the checklist |
| 20 | [Bugs We Hit, and What They Taught](ch20-bugs.md) | Six real defects and their root causes |
| 21 | [Extending DevBrain](ch21-extending.md) | Rerankers, hybrid search, streaming, OCR |

### Appendices

| | Title |
|---|---|
| A | [Complete File Listing](appendix-a-file-listing.md) |
| B | [Command Reference](appendix-b-commands.md) |
| C | [Glossary](appendix-c-glossary.md) |
| D | [Troubleshooting Index](appendix-d-troubleshooting.md) |

---

## The finished project

For reference, this is what you are building toward:

```
DevBrain/
├── config.py            360 lines   settings, validation, provenance
├── loaders.py           292 lines   files → Documents
├── rag.py               298 lines   prompt, chain, citations
├── db.py                149 lines   plain SQL helpers
├── auth.py              577 lines   users, sessions, ownership
├── ingest.py            583 lines   the write path (CLI)
├── app.py             1,090 lines   the read path (web)
├── index.html         1,960 lines   the entire frontend
├── docker-compose.yml   158 lines   Postgres, pgAdmin, app
├── Dockerfile            82 lines   multi-stage production image
├── requirements.txt      30 lines   pinned dependencies
├── set-env.ps1          165 lines   secret management
├── data/                            your documents
└── assets/                          the logo
```

Roughly 5,700 lines. You will write nearly all of it.

---

## A note on honesty

This book documents a project that was built, tested and debugged for real. It
includes the mistakes. Chapter 20 catalogues six genuine bugs — an upsert that
silently stole rows between collections, an async call against a synchronous
database handle, a config check that made the app impossible to containerise.

Each of those cost real debugging time, and each teaches something a working
example cannot. Where I state that something is verified, it was actually run.
Where a trade-off is genuinely arguable, the book says so rather than pretending
there is one right answer.

---

*Begin with [Chapter 1 — What DevBrain Is, and Why It Exists](ch01-what-devbrain-is.md).*

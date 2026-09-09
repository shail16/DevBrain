# Chapter 3 — Embeddings: Turning Meaning Into Numbers

---

## 3.1 The problem with keyword search

Suppose your notes contain this line:

> Always enable `pool_pre_ping`, which sends a cheap `SELECT 1` before handing a
> connection to the caller.

And a colleague asks:

> How do I stop database connections from going stale?

Now count the shared words between question and answer.

**Zero.** Not one meaningful word overlaps. "Stale" does not appear. "Connections"
appears as "connection", which a naive matcher misses. Keyword search — even good
keyword search with stemming and synonyms — has very little to work with here.

Yet any human reads both and immediately sees they are about the same thing.

**Embeddings are how we teach a computer to see that too.**

---

## 3.2 What an embedding is

An **embedding** is a list of numbers that represents the *meaning* of a piece of
text.

Cohere's `embed-english-v3.0` — the model DevBrain uses — produces exactly
**1024 numbers** for any input, whether that input is one word or three
paragraphs.

```
"database connection pooling"  →  [0.021, -0.114,  0.087, ...,  0.043]   1024 numbers
"how to reuse DB connections"  →  [0.019, -0.108,  0.091, ...,  0.041]   very similar
"chocolate cake recipe"        →  [-0.201, 0.334, -0.076, ...,  0.512]   totally different
```

Look at the first two. Different words, nearly identical numbers. That is the
whole idea.

### The coordinate intuition

Think of each number as a coordinate.

- **Two** numbers give you a point on a map — you can measure distance between towns
- **Three** give you a point in a room
- **1024** give you a point in a space nobody can picture, but the mathematics
  works exactly the same

**Texts about similar topics land close together in that space.** That is the
entire premise, and it is worth stating plainly: an embedding model is a function
that maps text onto a map of meaning, where distance means dissimilarity.

You do not need to know how the model achieves this to use it well. What you need
to know is what it guarantees: *similar meaning → nearby coordinates.*

### Why 1024?

It is a design choice by Cohere, trading quality against storage and speed. Other
models differ:

| Model | Provider | Dimensions |
|---|---|---|
| `embed-english-v3.0` | Cohere | 1024 |
| `embed-multilingual-v3.0` | Cohere | 1024 |
| `text-embedding-3-small` | OpenAI | 1536 |
| `text-embedding-3-large` | OpenAI | 3072 |

More dimensions can capture finer distinctions, but cost more to store and compare.
1024 is a good middle ground.

> **This is the single most important operational fact in the book:** the number of
> dimensions is fixed per model. A database column holding 1024-dimensional vectors
> **cannot** store a 1536-dimensional one. Change your embedding model and every
> stored vector becomes garbage. Chapter 10 makes this a first-class error message;
> Chapter 20 shows the exact failure.

---

## 3.3 Measuring closeness: cosine similarity

We have points in space. How do we measure "close"?

DevBrain uses **cosine similarity**: the angle between two vectors, ignoring their
length.

```
        ▲
        │      ↗ A  "connection pooling"
        │    ↗
        │  ↗  small angle → very similar
        │↗ ↗ B  "reusing DB handles"
        │
        │
        └──────────────────────────►
                    ↘
                      ↘ C  "chocolate cake"
                        large angle → unrelated
```

| Angle | Score | Meaning |
|---|---|---|
| ~0° | ~1.0 | Nearly identical meaning |
| ~90° | ~0.0 | Unrelated |

### Why angle rather than distance?

This trips people up, and the reason is worth understanding.

Consider a 40-page document about connection pooling, and a 9-word question about
connection pooling. Both are about the same thing. But their vectors have very
different **lengths** — length correlates loosely with how much text there is, how
emphatic it is, how many topics it touches.

If you measured straight-line (Euclidean) distance, the long document and the
short question would look far apart *purely because one is longer*. That is not
what you want.

**Direction encodes meaning. Length mostly encodes verbosity.** Cosine similarity
throws away the length and keeps the direction, which is exactly the right trade
for search.

---

## 3.4 Asymmetric embeddings: a subtlety that matters

Here is something most tutorials skip, and it explains a real quality difference.

Consider how a question and its answer are actually phrased:

- **Question:** *"How do I stop stale connections?"*
- **Answer:** *"Enable `pool_pre_ping` before handing out a connection."*

These are not phrased alike. The question is interrogative; the answer is
imperative. A naive similarity model might place the question closest to *other
questions about connections* rather than to the passage that answers it.

Cohere's v3 models handle this with **input types**. The same model embeds text
differently depending on its role:

| Input type | Used for |
|---|---|
| `search_document` | Text being stored and searched over |
| `search_query` | A user's question |

The model is trained so that a *query* embedding lands near the *documents that
answer it*, rather than near documents that merely resemble it. This is called an
**asymmetric** embedding model, and it measurably improves retrieval for
question-answering.

### You do not have to configure this

`langchain-cohere` sets the correct input type automatically, based on which
method is called:

| LangChain call | Input type sent | Used by |
|---|---|---|
| `embed_documents()` | `search_document` | Ingestion (Chapter 10) |
| `embed_query()` | `search_query` | Querying (Chapter 11) |

This is quietly one of the strongest arguments for using the LangChain integration
rather than calling the Cohere SDK yourself: it is easy to forget the input type,
and forgetting it degrades retrieval in a way that is very hard to notice.

---

## 3.5 Real numbers from the project

Theory is cheap. These are actual cosine scores from a real run against the sample
corpus.

| Question | Best-matching document | Score |
|---|---|---|
| "How many dimensions do Cohere embeddings have?" | `tech-stack-decisions.xlsx` | **0.544** |
| "How big should a pull request be?" | `code-review-standards.docx` | **0.540** |
| "Which datastore for caching sessions?" | `tech-stack-decisions.xlsx` | **0.487** |
| "Why avoid alpine images for Python?" | `docker-tips.md` | **0.469** |
| "What do I do in the first 15 minutes of an outage?" | `incident-response-runbook.pdf` | **0.415** |
| "How do I stop connections going stale?" | `postgres-best-practices.md` | **0.381** |
| **"What is the capital of France?"** | *(best of a bad lot)* | **0.112** |

Three observations worth absorbing:

**Real matches score 0.38 – 0.55, not 0.9.** Beginners often expect near-1.0 scores
for good matches. In practice, a question and the passage answering it are related
but not identical, and 0.4 is a strong match. Judge scores relative to each other,
not against an absolute ideal.

**The off-topic question collapses to 0.11.** Nothing relevant exists, so every
score drops. This is the signal.

**The gap is the useful part.** Roughly 0.4 versus 0.11 gives you a threshold to
work with. Set `RETRIEVER_SCORE_THRESHOLD=0.3` and the France question retrieves
*nothing*, so the model is handed an empty context and refuses cleanly — without
ever being tempted to improvise.

Notice too that the Excel row won the *"caching sessions"* question. That is not
luck; it is a direct result of how Chapter 9 flattens spreadsheets. Extraction
quality drives retrieval quality far more than most people expect.

---

## 3.6 Seeing embeddings for yourself

Once you have built through Chapter 10, this query in pgAdmin shows you the real
thing:

```sql
SELECT cmetadata ->> 'source'    AS document,
       left(document, 120)       AS text_preview,
       left(embedding::text, 90) AS vector_preview
FROM langchain_pg_embedding
LIMIT 3;
```

The `vector_preview` column is a long list of decimals. **That is the embedding.**
No magic, no hidden state — a list of numbers in a database column you can query
with ordinary SQL.

I recommend actually running this when you get there. Seeing it demystifies the
whole subject faster than any explanation.

---

## 3.7 What embeddings are not

**Not encryption.** Embeddings are a lossy, one-way-ish transformation, but
research has repeatedly shown that meaningful text can be partially reconstructed
from embeddings. Treat them as *sensitive derived data*, roughly as confidential as
the text itself. Do not assume that storing embeddings instead of text protects
anything.

**Not a summary.** You cannot read an embedding. It is only useful for comparison
against other embeddings from the same model.

**Not comparable across models.** A Cohere vector and an OpenAI vector are points
in unrelated coordinate systems. Comparing them is meaningless — and, because it
produces numbers rather than errors, dangerously silent.

**Not deterministic across versions.** If a provider updates a model, previously
stored vectors may no longer be comparable to new ones. This is why the project
pins `COHERE_EMBED_MODEL` explicitly rather than accepting a default.

---

## 3.8 Cost and batching

Embeddings are cheap, but not free, and ingestion calls them a *lot*.

Two operational facts shape the code you will write in Chapter 10:

**Cohere accepts at most 96 texts per request.** Exceed that and the call is
rejected. The project defines this as a named constant rather than a magic number:

```python
# rag.py
COHERE_MAX_BATCH_SIZE = 96
```

**Batching also limits blast radius.** If a request fails halfway through a large
ingestion, you lose one batch — not the whole run. And it gives you progress
output instead of a silent multi-minute wait.

Free-tier Cohere trial keys allow roughly 100 calls per minute, which is far more
than a personal knowledge base needs. Ingesting six documents in the sample corpus
uses exactly **one** call.

---

## 3.9 Mistakes to avoid

**Changing the embedding model without re-ingesting.** The most damaging mistake in
this chapter. Old vectors and new queries end up in different coordinate systems.
Chapter 10 makes this a loud error; Chapter 20 shows what it looks like when it
happens.

**Comparing scores across different models or corpora.** A 0.45 from Cohere means
nothing next to a 0.45 from OpenAI. Thresholds must be tuned per setup.

**Expecting embeddings to understand negation.** *"Redis is suitable for sessions"*
and *"Redis is not suitable for sessions"* embed very closely — they share almost
all their meaning. Embeddings capture topic far better than they capture polarity.
This is a genuine limitation, and it is why the model still has to *read* the
retrieved text rather than trusting the match.

**Assuming higher scores mean better answers.** A chunk can be topically close and
still fail to answer the question. The score measures similarity, not usefulness.

---

## 3.10 Checkpoint

1. Why does keyword search fail on *"stale connections"* → *"pool_pre_ping"*, and
   how does an embedding succeed?
2. Why does cosine similarity ignore vector length? What would go wrong if it
   didn't?
3. What is an asymmetric embedding model, and which two LangChain method calls
   trigger the two input types?
4. Why is a real-world match scoring 0.42 good, when 1.0 is the maximum?
5. You switch `COHERE_EMBED_MODEL` from English to multilingual and restart. Both
   are 1024-dimensional so nothing errors. Why are your search results now
   subtly wrong, and what must you do?

---

## What's next

You can now turn text into vectors. Chapter 4 answers the next question: where do
you *put* thousands of vectors, and how do you find the nearest ones fast?

*Next: [Chapter 4 — Vector Databases and pgvector](ch04-vector-databases.md)*

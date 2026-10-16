# Chapter 5 — Chunking: Why We Cut Documents Up

*© 2026 Shailendra Srivastava. All rights reserved.*

---

## 5.1 Why not embed the whole document?

It seems simpler. One document, one vector, one row. Why complicate it?

Two independent reasons, and each alone would be sufficient.

### Reason 1 — Meaning gets averaged away

An embedding is a *single point* representing the meaning of whatever you feed it.
Feed it a 40-page PDF covering deployment, monitoring, on-call rotation and
postmortems, and you get one point representing the blurry average of all four
topics.

Now ask a sharp question about postmortems. Your question's vector points firmly at
"postmortems". The document's vector points at some vague centroid of four
unrelated subjects. The angle between them is large. **The document scores poorly
even though it contains the perfect answer.**

This is the more important of the two reasons, and it is counter-intuitive:
*bigger context makes retrieval worse, not better.*

### Reason 2 — Prompts have a budget

Even with a large context window, pasting whole documents into every prompt is
slow and expensive. And as Chapter 2 covered, models reason measurably worse when
the relevant sentence is buried in tens of thousands of tokens — the "lost in the
middle" effect.

### The solution

Cut documents into **chunks** of roughly 1000 characters. Each chunk gets its own
vector, so retrieval can pinpoint the exact paragraph that answers the question.

```
One 40-page PDF                  →  ~120 chunks
Each chunk ≈ 1000 characters     →  each gets its own 1024-dim vector
Question matches ONE paragraph   →  that paragraph is retrieved
```

---

## 5.2 Why chunks must overlap

Consider a hard cut at exactly 1000 characters:

```
...always enable pool_pre_ping, which sends a cheap SELECT 1     ← chunk 1 ends
before handing the connection to the caller...                    ← chunk 2 begins
```

Chunk 1 mentions `pool_pre_ping` but never says what it does. Chunk 2 explains the
behaviour but never names the setting.

**Retrieve either one and you have half an idea.** A question about
`pool_pre_ping` matches chunk 1, which cannot answer it. A question about
"checking connections before use" matches chunk 2, which does not name the thing.

The fix is **overlap**. DevBrain uses `CHUNK_OVERLAP=150`, meaning consecutive
chunks share their last and first 150 characters:

```
chunk 1: [.................................................]
chunk 2:                                        [...............................]
                                                └─ 150 chars shared ─┘
```

Now the complete sentence survives intact in at least one chunk. The cost is a
little duplicated storage and a few extra embedding calls. It is almost always
worth it.

### How much overlap?

A useful rule of thumb: **10–20% of chunk size**. DevBrain's 150 on 1000 is 15%.

- Too little → ideas orphaned at boundaries
- Too much → near-duplicate chunks competing in results, and wasted API calls

---

## 5.3 Recursive splitting: cutting in the right places

Naive splitting every 1000 characters would cut mid-word, mid-sentence, mid-code.
LangChain's `RecursiveCharacterTextSplitter` does better.

The idea: you give it a list of separators **in priority order**. It tries the
first. If a resulting piece is still too big, it escalates to the next. Only as a
last resort does it cut arbitrarily.

DevBrain's separator list is tuned for technical notes:

```python
separators=[
    "\n## ",    # markdown h2   — a section boundary is the best place to cut
    "\n### ",   # markdown h3
    "\n```",    # fenced code block boundary
    "\n\n",     # paragraph
    "\n",       # line
    ". ",       # sentence
    " ",        # word
    "",         # single character — last resort
]
```

Read that top to bottom as a statement of preference:

> *Prefer to split at a section heading. Failing that, at a code-block boundary.
> Failing that, at a paragraph break. Failing that, a line, a sentence, a word.
> And only if all else fails, mid-word.*

The result is chunks that are **semantically whole** rather than arbitrary
1000-character slabs.

### Why the code-block entry matters

The `"\n```"` separator is a deliberate addition, not a default. Without it, a
fenced code block longer than the remaining budget gets cut in half:

````
chunk 1:
    ```python
    engine = create_engine(
        url,
        pool_size=10,
chunk 2:
        pool_pre_ping=True,
    )
    ```
````

Neither chunk contains runnable code. Both embed poorly, because half a function
signature is not "about" anything coherent. Treating the fence as a strong
boundary makes this far less likely.

For a knowledge base full of code snippets, this single line noticeably improves
answer quality.

---

## 5.4 Choosing chunk size

This is a genuine trade-off with no universally right answer.

| | Small (~400) | Medium (~1000) | Large (~2000) |
|---|---|---|---|
| Retrieval precision | **High** — pinpoints facts | Good | Low — blurry averages |
| Context per hit | Little; answers may lack background | Balanced | **Plenty** |
| Chunks per document | Many | Moderate | Few |
| Embedding cost | Higher | Moderate | Lower |
| Best for | Reference tables, FAQs, glossaries | General notes | Essays, long arguments |

**DevBrain defaults to 1000 with 150 overlap.** It is a good general starting point
for mixed technical documentation.

### How to tune it

Change it in your configuration, then **re-ingest with `--reset`** — existing
chunks were cut with the old settings and will not match:

```bash
python ingest.py --reset
```

Symptoms and their fixes:

| Symptom | Try |
|---|---|
| Answers miss information you know is there | **Smaller** chunks (600) — sharper retrieval |
| Answers lack context, feel fragmentary | **Larger** chunks (1500) |
| Answers cut off mid-thought | More overlap (250) |
| Retrieving several near-identical chunks | Less overlap (100) |

Always change one variable at a time, and re-test the same set of questions.

---

## 5.5 Metadata: what travels with a chunk

A chunk is not just text. In DevBrain each carries metadata that survives all the
way into the answer:

| Key | Set by | Used for |
|---|---|---|
| `source` | Loader (Ch 9) | Citation — *"[postgres-best-practices.md]"* |
| `file_type` | Loader | Badge in the UI |
| `page` | PDF loader | *"page 3"* in the citation |
| `sheet` | Excel loader | *"sheet 'Datastores'"* |
| `chunk_index` | Splitter (Ch 10) | Position within the parent document |
| `uploaded_by` | Upload (Ch 16) | *"uploaded by alice.dev"* |
| `uploaded_at` | Upload | Provenance |

This is why the answer can say *"[incident-response-runbook.pdf (page 1)]"* rather
than just quoting text at you. Metadata is what turns retrieval output into a
**verifiable** citation.

`chunk_index` is assigned after splitting, per source document:

```python
per_source_counter: dict[str, int] = {}
for chunk in chunks:
    source = chunk.metadata.get("source", "unknown")
    index = per_source_counter.get(source, 0)
    chunk.metadata["chunk_index"] = index
    per_source_counter[source] = index + 1
```

It is useful in pgAdmin and in traces when you are asking *"why did chunk 7 come
back and not chunk 8?"* — and Chapter 10 uses it as part of the chunk ID.

---

## 5.6 Dropping the noise

Not every chunk deserves an embedding. Splitting can produce fragments that are
pure whitespace or punctuation — a stray `---`, a lone bullet, a page-number
artefact.

```python
meaningful = [c for c in chunks if len(c.page_content.strip()) >= 20]
if len(meaningful) < len(chunks):
    warn(f"dropped {len(chunks) - len(meaningful)} near-empty chunk(s)")
```

Two reasons this matters:

**It wastes API calls.** Every chunk costs an embedding, and Cohere's batch limit
is 96 — junk chunks push real content into extra requests.

**It pollutes results.** A chunk containing only `---` has a vector like any other,
and it can occupy one of your four retrieval slots.

Note that it *warns* rather than silently dropping. Throughout this project,
silent behaviour is treated as a defect: if the system discards your data, it says
so.

---

## 5.7 A worked example

Take a real 2,316-character file, `postgres-best-practices.md`, with sections on
connection pooling, indexing, migrations and backups.

**Without chunking:** one vector for the whole file. A question about migrations
competes against pooling, indexing and backups all averaged together. Score: mediocre.

**With chunking at 1000/150:** three chunks, split at `## ` headings:

| Chunk | Roughly covers | Chars |
|---|---|---|
| 0 | Connection pooling + the code example | ~950 |
| 1 | Indexing rules of thumb | ~800 |
| 2 | Migrations + backups | ~700 |

Now the migrations question matches chunk 2 directly, scoring 0.38 against a
corpus where irrelevant content scores 0.11. The retrieved chunk is small enough
that the model reads it carefully, and the answer cites the exact file.

Same document, same model, same question. **The only difference is chunking** — and
it is the difference between a useful answer and a shrug.

---

## 5.8 Mistakes to avoid

**Changing chunk size without `--reset`.** You end up with a mix of old and new
chunk sizes. Retrieval still works, results are inconsistent, and nothing errors.

**Setting overlap ≥ chunk size.** Chunking never advances and you get infinite or
absurd output. DevBrain rejects this explicitly at config load:

```python
if chunk_overlap >= chunk_size:
    raise ConfigError(
        f"CHUNK_OVERLAP ({chunk_overlap}) must be smaller than "
        f"CHUNK_SIZE ({chunk_size}), otherwise chunking never advances."
    )
```

**Tuning chunk size to fix a generation problem.** If the right chunk *is* being
retrieved and the answer is still weak, chunking is not your problem. Check the
trace (Chapter 17) before touching these numbers.

**Assuming characters are tokens.** DevBrain measures chunk size in **characters**
because it is simple and predictable. Roughly 4 characters ≈ 1 token for English,
so a 1000-character chunk is ~250 tokens. If you ever switch to token-based
splitting, your numbers change meaning entirely.

**Forgetting that tables and code do not chunk like prose.** A table split across
two chunks loses its header row in one of them. This is exactly why Chapter 9
repeats column headers on every spreadsheet row *before* chunking ever happens.

---

## 5.9 Checkpoint

1. Give the two independent reasons we chunk. Which one is about retrieval quality
   rather than cost?
2. Write out a concrete example where zero overlap orphans an idea across two
   chunks.
3. Why is `"\n```"` in the separator list, and what breaks without it?
4. Your answers are consistently missing details you know exist in your notes.
   Which knob do you turn first, in which direction, and what command must follow?
5. Why does the splitter drop chunks under 20 characters, and why does it print a
   warning rather than doing it silently?

---

## Part I complete

You now have the full conceptual foundation:

- **Chapter 1** — what DevBrain is and the two pipelines
- **Chapter 2** — why retrieval beats fine-tuning and long context
- **Chapter 3** — embeddings, cosine similarity, asymmetric models
- **Chapter 4** — pgvector, the two tables, the global-ID trap
- **Chapter 5** — chunking, overlap, recursive splitting

Everything from here is construction. Part II sets up your machine, gets
PostgreSQL running in Docker, and builds the configuration layer that every later
file depends on.

*Next: [Chapter 6 — The Development Environment](ch06-dev-environment.md)*

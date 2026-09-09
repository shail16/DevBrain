# Chapter 20 — Bugs We Hit, and What They Taught

---

## 20.1 Why this chapter exists

Every other chapter shows working code. This one shows code that *nearly* worked —
which is more useful, because nearly-working is what you will actually produce.

All six of these are real. Each was caught by a test before you ever ran the
project. For each I have tried to record not just the fix but **the reasoning that
found it**, because that is the part that transfers.

Four of the six failed **silently**. That is the theme worth carrying away.

---

## 20.2 Bug 1 — The silent collection thief

### Symptom

Ingest a folder into a second collection. Ingestion reports success:

```
batch 1/1: 18/18 chunks stored (0.6s)
Done. 18 chunk(s) from 6 file(s) in 1.4s.
```

Then query it. **Nothing.** The collection is empty.

Worse, checking the database showed the chunks had been written to the *first*
collection instead:

```
devbrain_docs      | 0
devbrain_e2e_test  | 18
```

### Diagnosis

Chapter 4 flagged the schema detail that causes this:
`langchain_pg_embedding.id` is a **single global primary key**, shared by every
collection. It is not scoped per collection.

The library's upsert is:

```sql
INSERT INTO langchain_pg_embedding (...) VALUES (...)
ON CONFLICT (id) DO UPDATE
SET embedding = excluded.embedding,
    document  = excluded.document,
    cmetadata = excluded.cmetadata;
```

Read the `SET` clause. **`collection_id` is not in it.**

The original chunk ID hashed only `(source, index, text)`. Same files → same IDs.
So the second ingestion hit the primary-key conflict on every row and *updated rows
still owned by the first collection*. The second collection got nothing.

The rows were not duplicated, not lost, and not errored. They were **silently
reassigned** — while the progress output said everything worked.

### Fix

Put the collection name in the ID:

```python
digest = hashlib.sha256(
    f"{collection_name}|{source}|{index}|{chunk.page_content}".encode("utf-8")
).hexdigest()
return f"{collection_name}:{source}:{index}:{digest[:16]}"
```

There is now a regression test that ingests into two collections and asserts each
keeps its own chunks:

```
[PASS] second collection has its own chunks  -- 18 vs 18
[PASS] first collection is untouched         -- 18 vs 18
```

### The lesson

> **An upsert only updates the columns you name. If a row's real identity spans
> more than the primary key, put all of it *in* the key.**

More generally: when you supply your own primary keys, you have taken on
responsibility for uniqueness across every dimension that matters — including ones
the library's schema does not enforce.

---

## 20.3 Bug 2 — Async call against a synchronous handle

### Symptom

Every question returned **502**:

```
AssertionError: This method must be called with async_mode
```

Not some questions. Every single one.

### Diagnosis

`PGVector` is constructed in **synchronous** mode — which is what `ingest.py` needs,
and what lets both entry points share one configuration (Chapter 11).

The endpoint was written as:

```python
@app.post("/api/ask")
async def ask(request: AskRequest) -> AskResponse:
    result = await chain.ainvoke(question)
```

`ainvoke` propagates asynchronously down the chain to the retriever, which calls
`asimilarity_search`, which asserts an async engine exists. A synchronous store has
none.

The instinct that caused it is worth naming: *"FastAPI is async, so handlers should
be `async def`, so I should `await` things."* Each step sounds right. The conclusion
is wrong.

### Fix

Declare the handler as plain `def`:

```python
@app.post("/api/ask", response_model=AskResponse)
def ask(request: AskRequest, user: auth.User = Depends(current_user)) -> AskResponse:
    result = chain.invoke(question)
```

FastAPI automatically runs synchronous handlers in a worker thread, so the blocking
database and OpenAI calls never stall the event loop and requests are still served
concurrently. Keeping the whole call on one thread also makes `collect_runs`
(Chapter 12) behave predictably.

The same change was applied to `/api/health` and `/api/config`, which also call
synchronous database helpers.

### The lesson

> **In FastAPI, `async def` is not "the fast option". It is a promise that
> everything inside is non-blocking.**

When your libraries are synchronous, plain `def` is both *correct* and *faster* —
because a blocking call inside `async def` stalls the entire event loop, harming
every concurrent request, not just its own.

---

## 20.4 Bug 3 — Pydantic silently dropping fields

### Symptom

Chapter 15's attribution feature was built. The database had `uploaded_by`. The
chunk metadata had it. `documents_to_citations()` returned it.

The API returned `uploaded_by: null`.

### Diagnosis

```python
class Citation(BaseModel):
    source: str
    file_type: str = ""
    locations: list[str] = Field(default_factory=list)
    snippet: str = ""
    # uploaded_by not declared
```

Constructed as `Citation(**c)` where `c` contained `uploaded_by`.

**Pydantic's default is to ignore undeclared keys.** No error, no warning. The key
was accepted and discarded.

Every layer was correct except the last one, and the last one failed quietly.

### Fix

```python
class Citation(BaseModel):
    ...
    # Provenance, carried through from ingestion. Pydantic drops keys that are
    # not declared here, so these must be listed for the UI to ever see them.
    uploaded_by: str = ""
    uploaded_at: str = ""
```

### The lesson

> **A response model is an allow-list, not a description.** If a field is not
> declared, it does not exist to the client.

This is usually a *feature* — it prevents accidentally leaking internal fields. But
it means adding data to a response requires touching the model, and forgetting
produces a silent null rather than an error.

The test that caught it asserted on the *value*, not just the status code:

```python
check("citation shows uploaded_by", src[0].get("uploaded_by") == "alice.dev")
```

A test asserting only `response.status_code == 200` would have passed.

---

## 20.5 Bug 4 — A config check that blocked containerisation

### Symptom

The application could not run in Docker. With every key correctly supplied as an
environment variable, it refused to start:

```
DevBrain is not configured yet:
  - No .env file found. Copy .env.example to .env and add your API keys
```

### Diagnosis

```python
if not (PROJECT_ROOT / ".env").exists():
    problems.append("No .env file found...")
```

The check tested for the **existence of a file**, not for whether configuration was
actually missing.

In Docker, Kubernetes and CI there is no `.env` — values arrive as real environment
variables. The application was perfectly configured and refused to start anyway.

### Fix

Check the keys first; mention `.env` only when something is genuinely unset:

```python
if problems and not ENV_FILE_LOADED:
    problems.insert(0, "No configuration source was read. Supply these as "
                       "application (environment) variables...")
```

Note it tests `ENV_FILE_LOADED`, **not** file existence. Those differ when
`DEVBRAIN_IGNORE_ENV_FILE=1` is set — and the advice must describe what the app
actually consulted, not what happens to be on disk. That distinction was itself a
second, smaller bug found while fixing the first.

Verified across three scenarios:

| Scenario | Before | After |
|---|---|---|
| `.env` present | works | works |
| No `.env`, env vars set | **crashed** | works |
| No `.env`, nothing set | error | error, now mentioning env vars |

### The lesson

> **Validate the outcome, not the mechanism.** A rule that assumes *how* a value
> arrives will eventually block a legitimate deployment.

The question is never *"is there a .env file?"* It is *"do I have what I need?"*

---

## 20.6 Bug 5 — A placeholder destroyed by `.trim()`

### Symptom

Code blocks in answers rendered as literal text:

```
Run this: BLOCK0 Done.
```

instead of a formatted `<pre><code>` block.

### Diagnosis

Chapter 13's Markdown renderer extracts fenced code blocks into placeholders, then
restores them after formatting the prose. The placeholder was:

```javascript
return ` BLOCK${blocks.length - 1} `;      // note the surrounding spaces
```

And the restore pass checked:

```javascript
const trimmed = line.trim();
if (trimmed.startsWith(" BLOCK")) { ... }   // never matches
```

`trim()` had already removed the leading space. The condition could never be true,
so the placeholder was never replaced.

The bug is almost invisible on reading, because both halves look reasonable in
isolation.

### Fix

Use a token with no adjacent whitespace, on its own line:

```javascript
return `\n@BLOCK${blocks.length - 1}@\n`;
```

```javascript
if (trimmed.startsWith("@BLOCK")) {
    html += blocks[Number(trimmed.match(/@BLOCK(\d+)@/)[1])];
}
```

### The lesson

> **A placeholder token must not contain characters your later processing will
> strip, normalise or transform.**

Choose delimiters that are unambiguous and survive every stage — `@`, `\x00`-style
sentinels, or a UUID. Whitespace is a poor choice precisely because so much text
processing normalises it.

There is a second lesson about *how it was found*: a test asserted on the rendered
output, not just that the function returned without throwing.

```javascript
check("no leftover placeholder token", !fenced.includes("@BLOCK"), fenced);
```

---

## 20.7 Bug 6 — A 403 that was not a permissions problem

### Symptom

Everything worked. No traces appeared in LangSmith. The log showed:

```
403 Client Error: Forbidden for url: https://api.smith.langchain.com/runs/multipart
```

### The wrong theory

**403 Forbidden**, not **401 Unauthorized**. In HTTP semantics that means
"recognised but not permitted" — so the obvious reading was a quota or plan
limitation. That theory sent me to the billing settings.

### The decisive test

Rather than continuing to reason, I compared the real key against deliberately bad
ones:

| Key | Response |
|---|---|
| The real key | 403 Forbidden |
| Well-formed fake key | 403 Forbidden |
| `not-a-key` | 403 Forbidden |

**Identical.** A malformed string and a valid key produced exactly the same
response.

That single observation destroyed the quota theory. If the service could distinguish
the real key from garbage, it would respond differently. It could not — so the key
was not being *recognised at all*.

### The actual cause

That reframed the question from *"what permission is missing?"* to *"why is this
key unknown to this server?"*

**Region.** LangSmith runs separate US and EU instances. The account was EU; the
default endpoint is US. Against `https://eu.api.smith.langchain.com`, the same key
returned **200 OK**.

### The lessons

> **Compare against a known-bad input.** If your real credential and obvious garbage
> produce identical responses, the problem is *recognition*, not *permission*.

That test is fast, decisive, and works for almost any API.

> **Failures that look like auth problems are often routing problems.** Region,
> endpoint, tenant, environment. The status code describes the symptom, not the
> cause.

> **Silent optional features need explicit verification.** Tracing failed for a long
> time without anyone noticing, because the application kept working perfectly.
> That graceful degradation is by design — but it means you must actively check.

---

## 20.8 The pattern

Look at how these failed:

| Bug | Failure mode |
|---|---|
| 1 — Collection thief | **Silent** — reported success |
| 2 — Async/sync | Loud — 502 every time |
| 3 — Pydantic drop | **Silent** — returned `null` |
| 4 — Config check | Loud — clear refusal |
| 5 — Placeholder | **Silent** — wrong output, no error |
| 6 — LangSmith 403 | **Silent** — app kept working |

**Four of six failed silently.** None raised an exception. Several *reported
success* while doing the wrong thing.

The two loud ones were fixed in minutes. The silent ones needed tests that asserted
on **values and observable behaviour**, not on the absence of exceptions.

### What actually caught them

| Bug | Caught by |
|---|---|
| 1 | Asserting chunk counts per collection after a second ingest |
| 2 | Calling the endpoint at all |
| 3 | Asserting `uploaded_by == "alice.dev"`, not just HTTP 200 |
| 4 | Running with `.env` renamed away |
| 5 | Asserting on rendered HTML, not just "no exception" |
| 6 | Comparing a real credential against a fake one |

Every one of those is a test that checks *the thing the feature promises*, rather
than that the code ran.

> **The habit worth building: assert on outcomes, not on the absence of errors.**
> `status_code == 200` would have passed for bugs 1, 3 and 5.

---

## 20.9 Two design decisions that prevented whole classes of bug

Worth noting the counterfactual — problems that *did not* happen.

**One shared `rag.py`.** Because `ingest.py` and `app.py` both import
`build_embeddings()` from one module, they cannot drift to different embedding
models. That failure — the most damaging in RAG, and completely silent — was made
structurally impossible rather than tested for.

**A frozen `Settings` dataclass.** Because it cannot be mutated, per-request
variation (Chapter 12's `top_k`) had to use `replace()` to make a copy. Concurrent
requests interfering through shared mutable configuration was never possible.

> **Preventing a bug in the design is worth more than catching it in a test.**
> Where you can make a mistake unrepresentable, do that instead.

---

## 20.10 Checkpoint

Rather than questions, try reproducing one.

**Reproduce Bug 1.** Temporarily revert `chunk_id` to exclude the collection name:

```python
digest = hashlib.sha256(f"{source}|{index}|{chunk.page_content}".encode()).hexdigest()
return f"{source}:{index}:{digest[:16]}"
```

Then:

```bash
python ingest.py --reset
COLLECTION_NAME=test_collection python ingest.py
```

Check both collections:

```sql
SELECT c.name, count(e.id)
FROM langchain_pg_collection c
LEFT JOIN langchain_pg_embedding e ON e.collection_id = c.uuid
GROUP BY c.name;
```

You should see the second collection empty despite ingestion reporting success.
Then restore the fix and confirm both populate correctly.

Seeing a silent failure with your own eyes is worth more than reading about it.

---

## 20.11 What you have

Six real defects, their diagnoses, their fixes, and the transferable lesson from
each:

1. An upsert only updates the columns you name
2. `async def` is a promise, not an optimisation
3. A response model is an allow-list
4. Validate the outcome, not the mechanism
5. Placeholders must survive later processing
6. Compare against a known-bad input

Plus the meta-lesson: **most real bugs are silent**, and only tests that assert on
outcomes will find them.

One chapter left — where to take DevBrain next.

*Next: [Chapter 21 — Extending DevBrain](ch21-extending.md)*

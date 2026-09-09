# Chapter 17 — Observability with LangSmith

---

## 17.1 Why RAG needs this more than most systems

In an ordinary web application, a bug produces a stack trace pointing at a line
number. In a RAG system, the most common failure produces **a fluent, confident,
wrong answer** — and no error at all.

Chapter 2 established that bad answers come from exactly two places:

1. **Retrieval failed** — the right passage was never found
2. **Generation failed** — the right passage was found and misread

Those have completely different fixes. Raising `RETRIEVER_K` will not help a
generation problem. Switching to `gpt-4o` will not help a retrieval problem — it
will just write a more eloquent wrong answer.

**Without a trace you are guessing which half failed.** That is the entire case for
this chapter.

---

## 17.2 The integration, in full

Here is all the tracing code in DevBrain:

```python
os.environ["LANGSMITH_TRACING"] = "true"
os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
os.environ["LANGSMITH_ENDPOINT"] = settings.langsmith_endpoint
```

That is it. Four environment variables.

### How that can possibly be enough

The mechanism is worth understanding, because it explains why Chapter 11 insisted
on LCEL.

Every LangChain component implements the `Runnable` interface, and every Runnable
already emits lifecycle events as it executes:

```
on_start   → inputs, timestamp
on_end     → outputs, duration, token usage
on_error   → the exception
```

Those events are always fired. They just have nowhere to go by default.

When `LANGSMITH_TRACING=true` is set, LangChain attaches a **tracer callback
handler** at import time. That handler listens to the events and ships them to
LangSmith **in a background thread**, so it does not slow your response.

> **This is the payoff for building the chain from Runnables rather than plain
> function calls.** Composition with `|` is not just tidier syntax — it is what
> makes the pipeline observable. Four plain function calls would emit nothing.

DevBrain adds one small extra, from Chapter 12:

```python
with collect_runs() as collector:
    result = chain.invoke(question)
    if collector.traced_runs:
        run_id = str(collector.traced_runs[0].id)
```

This captures the run ID so the API can return it to the browser. Every answer in
the UI shows a short trace ID, which means **any answer can be traced back to the
exact prompt and chunks that produced it.**

---

## 17.3 What a trace looks like

This is a real trace from the finished project:

```
DevBrainRAG                                    1.96s   1,229 tokens
├── RunnableParallel<question,docs>            0.50s
│   ├── RunnablePassthrough                    0.00s
│   └── VectorStoreRetriever                   0.49s   ← retriever
│       ├── input:  "How should I structure a Dockerfile..."
│       └── output: 4 documents
├── RunnableAssign<context>                    0.00s
│   └── RunnableParallel<context>              0.00s
└── RunnableAssign<answer>                     1.39s
    └── RunnableParallel<answer>               1.39s
        └── RunnableSequence                   1.39s
            ├── ChatPromptTemplate             0.00s   ← the FULL rendered prompt
            ├── ChatOpenAI                     1.39s   1,229 tokens
            └── StrOutputParser                0.00s
```

Match that against Chapter 11's chain and every span corresponds to a line of code.

### What you can see in each span

| Span | Reveals |
|---|---|
| `VectorStoreRetriever` | **Which chunks came back**, from which sources |
| `ChatPromptTemplate` | **The exact prompt** OpenAI received, documents pasted in |
| `ChatOpenAI` | Request, response, token counts, cost |
| Root | Total latency, broken down per step |

**The `ChatPromptTemplate` span is the most valuable thing here.** You can read the
literal text that was sent — every rule, every excerpt, in order. When an answer is
wrong, reading that prompt usually makes the reason obvious within seconds.

Note the timing breakdown too: retrieval took 0.49s and generation 1.39s. That is
the normal ratio, and it tells you where to optimise if latency matters.

---

## 17.4 The debugging method

This is the practical core of the chapter.

```
Bad answer
   │
   ▼
Open the trace, expand VectorStoreRetriever
   │
   ├── Wrong chunks returned? ──────► RETRIEVAL problem
   │                                    • Raise RETRIEVER_K
   │                                    • Reduce CHUNK_SIZE, re-ingest --reset
   │                                    • Check the document is ingested at all
   │
   └── Right chunks returned? ──────► GENERATION problem
          │                             • Read the ChatPromptTemplate span
          ├── Prompt looks wrong? ──────► fix format_documents / the prompt
          └── Prompt looks right? ──────► try OPENAI_MODEL=gpt-4o
```

**Always look at the retriever span first.** In my experience most bad answers in a
new RAG system are retrieval problems wearing a generation costume — and the
instinct to reach for a bigger model wastes both time and money.

### Worked example

> **Complaint:** *"It says it doesn't know about our deploy window, but that's in
> the runbook."*

**Step 1 — retriever span.** Four chunks came back, all from
`incident-response-runbook.pdf`, none mentioning deploy windows.

**Step 2 — conclusion.** Retrieval problem. The generation half behaved correctly:
given no relevant context, refusing was exactly right.

**Step 3 — why?** Check whether the document is ingested at all
(`ingest.py --stats`). If it is, the likely cause is that the deploy-window
paragraph landed in a chunk dominated by other content — a chunking problem.

**Step 4 — fix.** Reduce `CHUNK_SIZE` to 600 and re-ingest with `--reset`, or raise
`RETRIEVER_K` to 6.

Notice that at no point did we consider changing the model. The trace told us the
model was never given the chance.

---

## 17.5 Cost and latency

Every `ChatOpenAI` span carries token counts, which LangSmith turns into cost.

Practical uses:

**Spot prompt bloat.** If input tokens are far larger than expected, your context
is too big — probably `RETRIEVER_K` too high or `CHUNK_SIZE` too large.

**Compare models honestly.** Run the same questions on `gpt-4o-mini` and `gpt-4o`
and compare quality against real cost, rather than guessing.

**Find the slow step.** In the trace above, generation is 74% of the latency. No
amount of database tuning will meaningfully help; a smaller model or streaming
would.

---

## 17.6 Separating environments

```ini
LANGSMITH_PROJECT=devbrain          # development
LANGSMITH_PROJECT=devbrain-prod     # server
```

Use different projects. Mixing your experiments with real user traffic makes both
harder to read, and you will eventually draw a conclusion from the wrong data.

This is on Chapter 19's deployment checklist.

---

## 17.7 The 403 that was not an auth problem

This is a real debugging story, and the method matters more than the specific bug.

### The symptom

The application worked perfectly. Answers were correct, citations right, latency
normal. But **no traces ever appeared** in LangSmith.

The server log showed:

```
WARNING langsmith.client  Failed to send compressed multipart ingest:
  403 Client Error: Forbidden for url: https://api.smith.langchain.com/runs/multipart
```

### The first wrong theory

**403 Forbidden**, not **401 Unauthorized**. In HTTP semantics those differ
meaningfully:

- **401** — "I don't know who you are"
- **403** — "I know who you are, and you may not do this"

So the natural reading was a *permission* problem — quota exhausted, plan
limitation, workspace restriction. That theory sent me looking at billing settings.

### The decisive test

Rather than continuing to reason about it, I compared the real key against
deliberately bad ones:

| Key | Response |
|---|---|
| The real key | **403 Forbidden** |
| `lsv2_pt_0000...0000` (well-formed, fake) | **403 Forbidden** |
| `not-a-key` (malformed) | **403 Forbidden** |

**Identical.** A malformed string and a valid key produced the same response.

That single observation destroyed the quota theory. If the service could tell the
real key apart from garbage, it would respond differently. It could not — so the
key was not being *recognised* at all.

### The actual cause

That reframed the question from *"what permission is missing?"* to *"why is this
key unknown to this server?"*

Answer: **region.** LangSmith runs separate US and EU instances. The account was on
EU; the default endpoint is US. The same key against
`https://eu.api.smith.langchain.com` returned **200 OK**.

One line fixed it:

```ini
LANGSMITH_ENDPOINT=https://eu.api.smith.langchain.com
```

### Three transferable lessons

**Compare against a known-bad input.** If your real credential and obvious garbage
produce identical responses, the problem is *recognition*, not *permission*. This
is a fast, decisive test that works for almost any API.

**Failures that look like auth problems are often routing problems.** Region,
endpoint, tenant, environment. The error code describes the symptom, not the cause.

**Silent optional features need explicit verification.** Tracing failed for a long
time without anyone noticing, because the app kept working. That is by design
(§17.8) — but it means you must actively check that it is working, not assume.

`.env.example` now carries a prominent warning so the next person loses minutes
rather than an hour.

---

## 17.8 Graceful degradation

Chapter 8 built this deliberately:

```python
langsmith_tracing = _bool("LANGSMITH_TRACING", False)
langsmith_api_key = _str("LANGSMITH_API_KEY")
if langsmith_tracing and _is_placeholder(langsmith_api_key):
    langsmith_tracing = False
    langsmith_api_key = ""
    warnings.append(
        "LANGSMITH_TRACING is on but LANGSMITH_API_KEY is not set — "
        "tracing disabled. The app works normally without it.")
```

**Tracing never breaks the application.** A missing key disables it with a warning;
a wrong endpoint logs a background warning and the RAG pipeline continues
unaffected.

That is the correct trade for a debugging aid. But it has the cost §17.7
identified: failure is quiet. The mitigation is knowing how to check.

---

## 17.9 Verifying tracing actually works

**1. Watch the log.** A working setup logs *nothing* about LangSmith after startup.
A broken one repeats:

```
WARNING langsmith.client  Failed to send compressed multipart ingest: ... 403 ...
```

**2. Check the startup banner:**

```
INFO devbrain  LangSmith       : tracing to project 'devbrain'
```

If it says `disabled`, your key is missing or a placeholder.

**3. Query the API directly:**

```python
from langsmith import Client
from config import load_settings

s = load_settings()
client = Client(api_key=s.langsmith_api_key, api_url=s.langsmith_endpoint)
runs = list(client.list_runs(project_name=s.langsmith_project, limit=5))
print(f"runs recorded: {len(runs)}")
for r in runs:
    print(" ", r.name, r.total_tokens, bool(r.error))
```

Verified output after the endpoint fix:

```
runs recorded: 10
  DevBrainRAG 1229 False
  ChatOpenAI  1229 False
  ...
```

**4. Look at the UI.** Every answer shows a short trace ID next to the latency.
If it is absent, no run was captured.

---

## 17.10 What to do without LangSmith

Tracing is optional, and you may not want a third-party service seeing your prompts.
DevBrain still gives you three things:

**The startup banner** — configuration provenance, model names, database, masked.

**Per-question logs:**

```
INFO devbrain  answered in 2592 ms | k=4 | chunks=4 | q="What is an INNER JOIN?"
```

**The API response** — `chunks_retrieved`, `latency_ms`, `grounded`, and the full
`sources` array. That last one is a poor man's retriever span: it tells you exactly
which documents were used.

For the retrieval half of debugging, you can also query directly:

```python
hits = vector_store.similarity_search_with_relevance_scores(question, k=4)
for doc, score in hits:
    print(f"{score:.3f}  {doc.metadata['source']}")
```

That reproduces the retriever span offline, with scores. It is genuinely most of
the value.

---

## 17.11 Checkpoint

1. Ask a question through the UI and note the short trace ID.
2. Open <https://smith.langchain.com> (or the EU URL) and find the `DevBrainRAG` run.
3. Expand `VectorStoreRetriever` — confirm the chunks match the sources shown in the UI.
4. Expand `ChatPromptTemplate` — **read the full prompt**. Find the rules from
   Chapter 11 and your documents pasted into `{context}`.
5. Check the token count and cost on the `ChatOpenAI` span.
6. Now ask something your documents do not cover. In the trace, confirm the
   retriever returned weak matches and the model refused — that is both halves
   behaving correctly.

Step 4 is the one to spend time on. Seeing the assembled prompt is the moment RAG
stops feeling like magic.

---

## 17.12 Mistakes to avoid

**Debugging by changing the model first.** Check the retriever span before spending
money on a bigger model.

**Sharing a LangSmith project between dev and prod.** You will draw conclusions
from the wrong traffic.

**Assuming tracing works because the app works.** It fails silently by design.

**Ignoring background warnings in the log.** The 403 in §17.7 was visible from the
first run; it was just not read.

**Treating 403 as definitively "permission".** Test against a known-bad credential
before believing the status code.

**Sending sensitive prompts to a third party without checking policy.** Your
documents' text appears in traces. For regulated data, disable tracing or
self-host.

---

## 17.13 What you have

Full request tracing for four environment variables and one `collect_runs` block. A
repeatable method for splitting retrieval failures from generation failures. Cost
and latency attribution per step. And a fallback plan when tracing is unavailable.

Next: packaging the application into a container.

*Next: [Chapter 18 — Containerising the Application](ch18-containerising.md)*

# Chapter 2 — The Problem RAG Solves

---

## 2.1 What a language model actually knows

A model like GPT-4o was trained on an enormous amount of public text up to a
cutoff date. Within that, its knowledge is genuinely impressive.

It knows **nothing** about:

- Your team's internal notes
- The architecture decision you made last March
- Your incident runbook
- Anything written after its training cutoff
- Anything private, ever

That last point is absolute. A model cannot know your private documents unless
somebody puts them in front of it. There is no clever prompt that unlocks
knowledge the model never had.

---

## 2.2 Hallucination: the failure mode that matters

Here is the dangerous part. When a language model does not know something, its
default behaviour is **not** to say *"I don't know."*

It produces fluent, confident, plausible, wrong text.

This is called **hallucination**, and understanding *why* it happens tells you how
to prevent it.

A language model is, at its core, a very sophisticated next-token predictor. Given
the text so far, it produces a probability distribution over what comes next, and
samples from it. Ask it *"What is our team's deployment window?"* and it has no
mechanism for checking whether it knows. It simply produces text that **looks like**
a plausible answer to that question, because that is the only thing it does.

The output is confident because confident-sounding text was overwhelmingly what it
saw in training. Nobody writes *"the deployment window is, and I am guessing here,
maybe Tuesday."*

**So hallucination is not a malfunction. It is the model working exactly as
designed, applied to a question it cannot answer.** The fix is not a better model.
The fix is to stop asking it to recall, and start asking it to *read*.

---

## 2.3 Three ways to give a model your knowledge

There are exactly three approaches. Understanding why RAG wins matters more than
memorising that it does.

### Option 1 — Fine-tuning

Retrain the model's weights on your documents.

| | |
|---|---|
| **How** | Continue training on your corpus, adjusting billions of parameters |
| **Cost** | Thousands of dollars, hours of GPU time, ML expertise |
| **Updating** | Retrain from scratch — you cannot "delete" one document |
| **Citations** | **None.** Knowledge dissolves into weights |
| **Hallucination** | **Not fixed.** It still generates from memory, just tuned memory |

That last row is the one people miss. Fine-tuning teaches a model *style* and
*format* extremely well. It is much weaker at teaching *facts*, and it gives you no
way to verify where an answer came from. If you fine-tune on your runbook and ask
about deployment windows, you still get a confident guess — just one that sounds
more like your team wrote it.

**Fine-tuning is the right tool for "answer in our house style". It is the wrong
tool for "answer from our documents".**

### Option 2 — Long context

Paste all your documents into every prompt.

| | |
|---|---|
| **How** | Prepend the entire corpus to each question |
| **Cost** | Enormous per question; hits token limits fast |
| **Updating** | Free — just change the files |
| **Citations** | Possible but unreliable |
| **Hallucination** | Mostly fixed, if the answer survives the noise |

This genuinely works for small corpora. If you have three short documents, do
this — it is simpler than RAG and you should not over-engineer.

It stops working for two reasons. First, cost and limits: a 500-page corpus does
not fit, and if it did, you would pay for all of it on every question. Second, and
less obvious, models reason measurably *worse* when the relevant sentence is
buried in tens of thousands of tokens of irrelevant text. This is well documented
as the **"lost in the middle"** effect — accuracy is highest for content at the
very start or very end of a long context, and sags in between.

More context is not automatically better context.

### Option 3 — Retrieval-Augmented Generation

Find the few relevant paragraphs, and paste only those.

| | |
|---|---|
| **How** | Semantic search picks the top *k* chunks; only those go in the prompt |
| **Cost** | Fractions of a cent per question |
| **Updating** | Re-run one script |
| **Citations** | **Built in** — you know exactly which chunks you supplied |
| **Hallucination** | **Fixed**, when combined with a prompt that forbids outside knowledge |

This is what DevBrain does, and what this book teaches.

---

## 2.4 The mental shift

Here is the sentence worth internalising:

> **RAG stops using the model as a source of facts, and starts using it as a
> reading-comprehension and writing engine over facts you supply.**

That reframing changes what you optimise for.

You stop asking *"does the model know this?"* — it doesn't, and it never will. You
start asking two entirely different questions:

1. **Did retrieval find the right passage?** (a search problem)
2. **Did the model read it correctly and write it up well?** (a comprehension problem)

Those two questions have different fixes, and separating them is the single most
useful debugging skill in this field. Chapter 17 shows you how to tell which one
failed on any given question, using traces.

A useful analogy: you are not hiring an expert who already knows your business.
You are hiring an excellent research assistant, handing them four pages from your
filing cabinet, and asking them to summarise. Their general knowledge is
irrelevant — what matters is that you handed them the *right* pages.

---

## 2.5 The two halves, and where each fails

RAG splits into retrieval and generation. Each fails differently, and the failures
look different to a user.

### Retrieval failures

| Symptom | Likely cause | Chapter |
|---|---|---|
| "I don't have that" but you know it's there | Chunks too large, meaning averaged away | 5 |
| Answer misses part of the question | `RETRIEVER_K` too low — not enough chunks | 11 |
| Irrelevant sources cited | No score threshold; weak matches admitted | 11 |
| Nothing found for a scanned PDF | No text layer to extract | 9 |

### Generation failures

| Symptom | Likely cause | Chapter |
|---|---|---|
| Right chunks retrieved, weak answer | Model too small — try `gpt-4o` | 8 |
| Answer includes facts not in the sources | Prompt not strict enough | 11 |
| Answer is vague or padded | Temperature too high, or weak prompt | 11 |
| Code snippets mangled | Chunk boundary cut through a code block | 5 |

**Notice that the fixes are completely different.** Raising `RETRIEVER_K` will not
help a generation problem. Switching to a stronger model will not help a retrieval
problem — it will just write a more eloquent wrong answer.

This is why observability is not optional in RAG, and why Chapter 17 exists.

---

## 2.6 What RAG does not fix

Being honest about limits is more useful than enthusiasm.

**RAG cannot answer what your documents do not contain.** Obvious, but it accounts
for most disappointment in practice. If your runbook never states the deployment
window, no amount of tuning will produce it. The system is a search engine with a
writer attached — not an oracle.

**RAG struggles with questions that need the whole corpus.** *"How many documents
mention Redis?"* or *"Summarise everything we know about caching"* are aggregate
questions. Retrieval fetches four chunks, not all of them. These need different
techniques entirely.

**RAG is only as good as its chunks.** If ingestion mangles your PDF tables into
meaningless numbers, retrieval will faithfully find those meaningless numbers.
Chapter 9 spends real effort on this, especially for spreadsheets, because it
matters more than any prompt tuning.

**RAG does not eliminate hallucination — it constrains it.** A model given
relevant context and told to use only that context will hallucinate far less. Not
never. This is why citations matter: they let a human verify, cheaply. The system
is designed to make checking easy, not to make checking unnecessary.

---

## 2.7 Seeing it work

To make this concrete, here are real retrieval scores from the finished project.
These came from an actual run against five sample documents.

| Question | Best match | Score |
|---|---|---|
| "How many dimensions do Cohere embeddings have?" | `tech-stack-decisions.xlsx` | **0.544** |
| "How big should a pull request be?" | `code-review-standards.docx` | **0.540** |
| "Which datastore for caching sessions?" | `tech-stack-decisions.xlsx` | **0.487** |
| "Why avoid alpine images for Python?" | `docker-tips.md` | **0.469** |
| "What do I do in the first 15 minutes of an outage?" | `incident-response-runbook.pdf` | **0.415** |
| **"What is the capital of France?"** | *(best of a bad lot)* | **0.112** |

Look at the last row. Nothing relevant exists, so every score collapses.

That gap — roughly 0.4 for real matches against 0.11 for nonsense — is not
decoration. It is a **usable signal**. Set a score threshold at 0.3 and the
off-topic question retrieves *nothing at all*, so the model is handed an empty
context and refuses without ever being tempted.

You will meet this as `RETRIEVER_SCORE_THRESHOLD` in Chapter 8. Now you know where
the number comes from.

---

## 2.8 Mistakes to avoid

**Reaching for RAG when long context would do.** Three short documents? Paste
them. RAG adds a database, an embedding vendor, an ingestion pipeline and a
chunking strategy. Do not pay that for a corpus that fits in a prompt.

**Expecting RAG to fix a bad corpus.** If your documents are contradictory,
outdated or vague, RAG surfaces that faithfully. It is a mirror, not a filter.

**Blaming the model for retrieval failures.** The instinct when an answer is bad
is to switch models. Check the trace first — in my experience most bad answers in
a new RAG system are retrieval problems wearing a generation costume.

**Trusting the citations in the answer text.** Those are generated. Chapter 12
returns a separate `sources` array built from what retrieval actually returned,
and you should build the same habit.

---

## 2.9 Checkpoint

1. Why does a language model hallucinate rather than saying "I don't know"?
   What does that tell you about how to fix it?
2. Fine-tuning is excellent at one thing and poor at another. Which is which?
3. What is the "lost in the middle" effect, and why does it undermine the
   long-context approach even when the corpus fits?
4. A user reports a bad answer. What is the *first* thing you check, and why does
   it determine which fix to apply?
5. In the score table, why is the gap between 0.4 and 0.11 more useful than either
   number on its own?

---

## What's next

You now know *why* we retrieve. Chapter 3 explains *how* — what an embedding
actually is, how "closeness" is measured, and why semantic search finds a document
that shares no words with your query.

*Next: [Chapter 3 — Embeddings: Turning Meaning Into Numbers](ch03-embeddings.md)*

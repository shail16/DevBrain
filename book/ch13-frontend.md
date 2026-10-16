# Chapter 13 — The Frontend

*© 2026 Shailendra Srivastava. All rights reserved.*

---

## 13.1 A deliberate constraint

`index.html` is 1,960 lines and has **zero dependencies**. No React, no build step,
no bundler, no CDN. One file: HTML, CSS and JavaScript.

That is a choice, and it is worth defending because it goes against current habit.

### Why no framework

**No build step.** Edit the file, refresh the browser. There is no `npm install`,
no `node_modules`, no webpack config, nothing to keep current.

**Nothing to break.** A dependency-free page written today works in five years. A
2019 React app with a 2019 build chain frequently does not.

**It fits in the Docker image trivially.** One `COPY` line, no Node toolchain.

**It stays readable.** You can open the file and follow it top to bottom. That
matters for a project meant to be *learned from*.

### The honest cost

Manual DOM updates, no component reuse, and string-built HTML — which means **you
must handle escaping yourself**. §13.5 is the most important section in this
chapter for exactly that reason.

For a UI of this size — five panels, a few tables, one form — the trade is clearly
worth it. At ten times the size it would not be.

> **The general principle:** match the tool to the problem. A framework earns its
> complexity through reuse and state management. This page has little of either.

---

## 13.2 Structure

```
index.html
├── <style>          ~900 lines   design tokens, layout, components
├── <body>           ~350 lines   auth screen + app shell + 5 panels
└── <script>       ~1,000 lines   health polling, ask, upload, admin, auth
```

The page has two top-level states:

```html
<div class="auth-screen" id="auth-screen">   <!-- signed out -->
<div class="container"   id="app-shell" hidden>  <!-- signed in -->
```

JavaScript toggles `hidden` on these two. Everything else lives inside the shell.

---

## 13.3 CSS design tokens

Every colour, radius and shadow is a custom property in one block:

```css
:root {
  --bg-color:            #fbf9f6;
  --panel-bg:            rgba(255, 255, 255, 0.72);
  --panel-border:        rgba(146, 132, 118, 0.16);

  --text-main:   #33302c;
  --text-muted:  #6f6a63;
  --text-faint:  #9a938a;

  --sky-pale:   #e0f2fe;   --sky:      #38bdf8;   --sky-deep:   #0284c7;
  --green-pale: #dcfce7;   --green:    #22c55e;   --green-deep: #15803d;
  --orange-pale:#ffedd5;   --orange:   #f59e0b;   --orange-deep:#b45309;
  --red-pale:   #fee2e2;   --red:      #ef4444;   --red-deep:   #b91c1c;
  --brown-pale: #f5ece2;   --brown:    #b98a5a;   --brown-deep: #8d6544;
}
```

**Colours carry meaning, not decoration:**

| Colour | Used for |
|---|---|
| Sky blue | Primary — buttons, active nav, links, citations |
| Green | Success — ready status, *Ingested* badges, uploader credit |
| Orange | Warning — *Skipped* badges |
| Red | Error — *Failed* badges, delete, validation |
| Brown | Warm neutral — inline code, keyboard keys, admin badge |

Because everything references tokens, changing one line re-skins the whole
interface. During development this page went from a dark theme to a light one by
swapping the token block — no component CSS changed.

---

## 13.4 The status bar

The UI polls `/api/health` and turns it into a coloured indicator.

```javascript
const STATUS_STYLES = {
  ok:                   { dot: "ok",   text: (h) => `Ready — ${h.chunks.toLocaleString()} chunks indexed` },
  empty:                { dot: "warn", text: () => "Knowledge base is empty" },
  degraded:             { dot: "warn", text: () => "Running with warnings" },
  database_unreachable: { dot: "err",  text: () => "Cannot reach PostgreSQL" },
  misconfigured:        { dot: "err",  text: () => "Backend is not configured" },
};

async function checkHealth() {
  try {
    const response = await fetch(`${API_BASE}/api/health`);
    const health = await response.json();
    const style = STATUS_STYLES[health.status] || STATUS_STYLES.degraded;
    els.statusDot.className = `dot ${style.dot}`;
    // ... render text and pill tags ...
  } catch {
    els.statusDot.className = "dot err";
    els.statusText.innerHTML =
      '<span class="status-text">Backend unreachable</span>' +
      '<span class="status-detail">Start it with: uvicorn app:app --reload --port 8001</span>';
  }
}
```

Chapter 12's five health states map directly to five visual states. The `catch`
adds a sixth the server cannot report — *the server itself is down*.

```javascript
setInterval(() => { if (currentUser) checkHealth(); }, 15000);
```

Polling every 15 seconds means the banner **self-heals**. Start your containers and
the red dot turns green without a refresh. Small touch, disproportionate effect on
how the app feels.

---

## 13.5 Rendering answers safely

This is the security-critical part of the file.

The answer arrives as Markdown-ish text and must become HTML. Doing that naively is
how you get cross-site scripting.

```javascript
function esc(str) {
  return String(str ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}
```

`&` **must be replaced first**. Otherwise `&lt;` produced by a later replacement
would itself get escaped into `&amp;lt;`.

### The order that makes it safe

```javascript
function renderMarkdown(raw) {
  let text = esc(raw);          // 1. escape EVERYTHING first

  // 2. pull fenced code blocks out into placeholders
  const blocks = [];
  text = text.replace(/```([a-zA-Z0-9+#-]*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    blocks.push(`<pre><code data-lang="${lang}">${code.replace(/\n$/, "")}</code></pre>`);
    return `\n@BLOCK${blocks.length - 1}@\n`;
  });

  // 3. inline formatting on the prose
  text = text
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([^\]\n]+\.(?:txt|md|pdf|docx|xlsx|...))\]/gi, '<span class="cite">$1</span>');

  // 4. block structure, then put the code blocks back
  // ...
}
```

**Escaping happens before any markup is added.** That single ordering decision is
what makes this safe. A document containing `<script>alert(1)</script>` renders as
visible text and can never execute — even inside a code fence.

Verified with real tests:

| Input | Output |
|---|---|
| `<script>alert(1)</script>` | `&lt;script&gt;alert(1)&lt;/script&gt;` |
| `<img src=x onerror="...">` | Fully escaped, no `<img>` tag |
| ` ```<script>...``` ` | Escaped inside `<pre><code>` |

### A bug in the placeholder

The original placeholder was `` ` BLOCK${i} ` `` — with surrounding spaces. The
block-structure pass then checked:

```javascript
const trimmed = line.trim();
if (trimmed.startsWith(" BLOCK")) { ... }   // never matches!
```

`trim()` had already removed the leading space, so the check never fired and code
blocks came out as literal `BLOCK0` text.

Fixed by using `@BLOCK0@` with no adjacent whitespace, and placing it on its own
line. **Lesson: a placeholder token must not contain characters your later
processing will strip.**

### Why not a Markdown library?

`marked` or `markdown-it` would be more capable. But:

- It is a CDN dependency or a build step — both of which this page avoids
- Most need explicit sanitiser configuration to be XSS-safe
- We need six features, not sixty

Roughly 60 lines of purpose-built code, escape-first by construction, beats a
general library you must configure correctly.

---

## 13.6 Showing the answer

```javascript
function showAnswer(data) {
  const meta = [
    `${data.chunks_retrieved} chunk${data.chunks_retrieved === 1 ? "" : "s"} used`,
    `${data.latency_ms} ms`,
  ];
  if (data.langsmith_run_id) meta.push(`trace ${esc(data.langsmith_run_id.slice(0, 8))}`);

  const sourcesHtml = data.sources.length ? `
    <div class="sources">
      <div class="sources-label">Sources</div>
      ${data.sources.map((s) => `
        <div class="source">
          <div class="source-name">
            ${esc(s.source)}
            ${s.file_type ? `<span class="badge">${esc(s.file_type)}</span>` : ""}
            ${s.locations.length ? `<span class="badge">${esc(s.locations.join(", "))}</span>` : ""}
            ${s.uploaded_by ? `<span class="credit">uploaded by ${esc(s.uploaded_by)}</span>` : ""}
          </div>
          ${s.snippet ? `<div class="source-snippet">${esc(s.snippet)}</div>` : ""}
        </div>`).join("")}
    </div>` : "";

  els.result.innerHTML = `
    <div class="card">
      <div class="card-head"><span>Answer</span><span class="meta">...</span></div>
      <div class="answer">${renderMarkdown(data.answer)}</div>
      ${sourcesHtml}
    </div>`;
}
```

**Every interpolated server value goes through `esc()`.** The only exception is
`renderMarkdown(data.answer)`, which escapes internally.

**The sources block is the trust anchor.** It renders `data.sources` — built by the
server from what retrieval actually returned — not the citations inside the answer
text. Chapter 11 explained why that distinction matters.

The metadata line shows chunks used, latency, and a short trace ID, giving the user
a way to audit any answer.

---

## 13.7 The loading state

```javascript
function showSkeleton() {
  els.result.innerHTML = `
    <div class="card">
      <div class="card-head"><span>Searching your documents…</span></div>
      <div class="skeleton-line" style="width:100%"></div>
      <div class="skeleton-line" style="width:92%"></div>
      <div class="skeleton-line" style="width:74%"></div>
    </div>`;
}
```

A RAG query takes 1.5–5 seconds: embed the question, search, call OpenAI. That is
long enough that a blank screen feels broken.

Shimmering skeleton lines communicate *"working, and the result will look roughly
like this"*. Varying the widths makes it read as text rather than a progress bar.

---

## 13.8 Handling errors and expired sessions

```javascript
const payload = await response.json().catch(() => null);

if (handleAuthFailure(response.status)) return;
if (!response.ok) {
  let detail = payload && payload.detail;
  if (Array.isArray(detail)) detail = detail.map((d) => d.msg).join("; ");
  showError(detail || `Request failed with HTTP ${response.status}.`);
  return;
}
```

Three details:

**`.catch(() => null)`** on the JSON parse. A 500 may return HTML, not JSON. Without
this you get a confusing parse error masking the real failure.

**Validation errors arrive as an array.** FastAPI returns `{"detail": [{...}]}` for
422s. The array branch flattens them into a readable sentence.

**`handleAuthFailure` bounces to sign-in on 401:**

```javascript
function handleAuthFailure(status) {
  if (status === 401) {
    showAuthScreen();
    setMsg(auth.signinMsg, "Your session ended. Please sign in again.", "error");
    return true;
  }
  return false;
}
```

This matters because Chapter 15 lets an admin delete a user mid-session. The next
request returns 401, and rather than leaving a dead UI on screen, the page returns
cleanly to sign-in with an explanation.

---

## 13.9 Small interaction details

```javascript
els.question.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    els.form.requestSubmit();
  }
});
```

Enter submits, Shift+Enter adds a newline — the convention users expect from chat
interfaces.

```javascript
function autoGrow() {
  els.question.style.height = "auto";
  els.question.style.height = Math.min(els.question.scrollHeight, 200) + "px";
}
```

The textarea grows with content up to 200px. Setting height to `auto` first is
required — without it, `scrollHeight` reports the *current* height and the box can
only ever grow.

```javascript
const API_BASE = location.protocol === "file:" ? "http://localhost:8001" : "";
```

Served by FastAPI, `API_BASE` is empty and requests go to the same origin. Opened
from disk (`file://`), it falls back to localhost — which works thanks to the CORS
middleware from Chapter 12.

---

## 13.10 Checkpoint

Open <http://localhost:8001>.

**1. Status bar is green:** `Ready — 19 chunks indexed`, with pill tags for chat
model, embedding model and LangSmith project.

**2. Ask a question.** You should see the skeleton, then an answer card with a
metadata line and a Sources block.

**3. Verify XSS safety.** Create `data/xss-test.md`:

```markdown
# XSS Test
Here is a script tag: <script>alert('xss')</script>
And an image: <img src=x onerror="alert('xss')">
```

Ingest it, then ask *"What is in the XSS test document?"*

**No alert should fire.** The tags render as visible text. Delete the file and
re-ingest with `--reset` afterwards.

**4. Test graceful failure.** Stop the server (`Ctrl+C`) and ask a question. You
should see *"Could not reach the DevBrain API"* with the command to restart it —
not a silent failure or a console error.

**5. Watch it self-heal.** Restart the server and wait up to 15 seconds. The status
dot returns to green on its own.

---

## 13.11 Mistakes to avoid

**Escaping after building markup.** The single most dangerous ordering error. Escape
the raw string first, always.

**Forgetting `esc()` on one interpolated value.** One miss defeats every other
precaution. When building HTML from strings, `esc()` should be reflexive.

**Using a placeholder token that later processing strips.** The `BLOCK` bug in
§13.5.

**Assuming error responses are JSON.** Always `.catch(() => null)`.

**Using `innerHTML` with unescaped user data.** The general form of the XSS trap.
If you do not need HTML, use `textContent`.

**Polling too aggressively.** Every second would hammer the database. Fifteen is
responsive enough.

---

## 13.12 What you have

A complete frontend in one file with no dependencies: five health states, safe
Markdown rendering, verifiable source citations, skeleton loading, session-expiry
handling, and self-healing status.

Part IV is complete — DevBrain now answers questions end to end. Part V makes it
multi-user.

*Next: [Chapter 14 — Authentication and Sessions](ch14-authentication.md)*

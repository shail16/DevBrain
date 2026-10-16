# Chapter 16 — File Upload and Live Ingestion

*© 2026 Shailendra Srivastava. All rights reserved.*

---

## 16.1 The goal

Until now, adding a document meant copying a file into `data/` and running
`python ingest.py`. That is fine for you. It is not fine for a colleague.

The upload endpoint closes that gap, and it does something stronger than just
saving files:

> **By the time the request returns, the uploaded documents are already chunked,
> embedded and searchable.** No CLI step, no server restart. Switch to the Ask tab
> and question them immediately.

That single property is what makes the feature feel finished rather than
half-built.

---

## 16.2 The dependency nobody expects

```python
python-multipart==0.0.20   # required by FastAPI to parse file uploads
```

FastAPI cannot parse `multipart/form-data` without this. Omit it and you get an
error at import time telling you to install it — which is at least a clear failure,
but it surprises people because nothing in your own code imports it.

---

## 16.3 Limits before logic

```python
MAX_UPLOAD_BYTES = 25 * 1024 * 1024      # 25 MB
MAX_FILES_PER_REQUEST = 20
```

Both are enforced server-side. The browser checks too, but that is **convenience,
not security** — anyone can call the API directly.

Why these matter:

**Without a size cap**, one enormous upload could fill the disk *and* cost a
fortune in embedding calls. A 500 MB text file is millions of chunks.

**Without a count cap**, a single request could tie up the server for many minutes.

These are cost-control and availability measures as much as security ones.

---

## 16.4 Sanitising the filename

A filename from a browser is **untrusted input**. It is a string the client chose,
and it is about to become a path on your filesystem.

```python
def _safe_filename(raw: str) -> str:
    """
    Reduce a browser-supplied filename to a bare, safe name.

    Left alone, a name like `../../../../app/app.py` would let an upload
    overwrite application code, and `..\\..\\.env` would overwrite your secrets.
    """
    # Handle both separators: a Windows client can send backslashes.
    name = raw.replace("\\", "/").split("/")[-1].strip()

    # Windows reserves leading/trailing dots and spaces; "." and ".." are never
    # legitimate document names.
    name = name.strip(". ")
    if not name or name in {".", ".."}:
        return ""

    # Keep letters, digits and a conservative punctuation set.
    name = re.sub(r"[^A-Za-z0-9._ ()\-]+", "_", name)
    name = re.sub(r"_{2,}", "_", name)

    stem, dot, suffix = name.rpartition(".")
    if dot and len(stem) > 120:
        name = stem[:120] + "." + suffix
    return name[:160]
```

Walk through what each line defeats:

| Line | Attack it stops |
|---|---|
| `replace("\\", "/")` then `split("/")[-1]` | Path traversal, both separators |
| `.strip(". ")` | Windows reserved names, hidden files |
| `in {".", ".."}` | Directory references |
| `re.sub(r"[^A-Za-z0-9._ ()\-]+", "_", ...)` | Shell metacharacters, control chars, unicode tricks |
| `name[:160]` | Filesystem length limits |

**Taking only the final path component is the core defence.** Everything before the
last separator is discarded, so `../../../../app/app.py` becomes `app.py`.

### Defence in depth

Sanitising is not trusted on its own:

```python
destination = _unique_path(data_dir, name)

# Defence in depth: even after sanitising, confirm the resolved path is
# genuinely inside the data folder before writing anything.
try:
    destination.resolve().relative_to(data_dir.resolve())
except ValueError:
    results.append(UploadedFileResult(
        filename=name, status="skipped",
        detail="Rejected: path escapes the data folder."))
    continue
```

`resolve()` collapses `..` and symlinks; `relative_to()` raises if the result
escapes. Two independent layers, either of which would suffice.

**This is the right posture for filesystem writes.** The cost of a redundant check
is nothing; the cost of a missed one is arbitrary file overwrite.

### It was tested with hostile input

| Sent | Result |
|---|---|
| `../../../../app.py` | → `app.py`, then rejected (unsupported type) |
| `..\..\.env` | → `env`, then rejected (no extension) |
| `evil.exe` | Rejected — extension not allowed |
| `quick notes.txt` | Accepted — spaces are fine |
| *(empty file)* | Rejected |

Afterwards, `app.py` and `.env` were verified **unmodified**, and nothing had
escaped `data/`.

Notice the layering in the first two rows: traversal was neutralised by
sanitisation, and then the *result* was independently rejected by the extension
allow-list. Neither layer knew about the other.

---

## 16.5 Replacement, not accumulation

```python
def _unique_path(directory: Path, filename: str) -> Path:
    """
    An identical name is treated as a deliberate REPLACEMENT — the file is
    overwritten and `ingest_files` deletes the old chunks — because
    re-uploading an edited document is the common case, and silently
    accumulating `notes(1).md`, `notes(2).md` would quietly poison retrieval
    with several versions of the same content.
    """
    return directory / filename
```

The obvious behaviour — auto-renaming duplicates — is wrong here, and the reason is
specific to RAG.

If `notes.md`, `notes(1).md` and `notes(2).md` all exist, all three are ingested.
Retrieval then returns near-identical chunks from three versions of the same
document, filling your four slots with one paragraph in three revisions. The model
sees contradictory content and the user has no idea why.

**Overwriting is the correct default.** Chapter 10's `delete_chunks_for_source`
makes it clean: the old chunks are removed before the new ones are stored.

Verified: re-uploading gave `chunks_added=1` while the total stayed at 22.

---

## 16.6 The endpoint

```python
@app.post("/api/upload", response_model=UploadResponse)
def upload(
    files: list[UploadFile] = File(...),
    purpose: str = Form(default="", max_length=500),
    user: auth.User = Depends(current_user),
) -> UploadResponse:
    pipeline = get_pipeline(app)          # 503 with guidance if the DB is down
    settings = pipeline.settings
    data_dir = settings.data_dir

    # A blank purpose defeats the point of recording one, so it is required.
    purpose = (purpose or "").strip()
    if not purpose:
        raise HTTPException(status_code=422,
            detail="Please describe the purpose of the document before uploading.")

    if len(files) > MAX_FILES_PER_REQUEST:
        raise HTTPException(status_code=413,
            detail=f"Too many files at once (max {MAX_FILES_PER_REQUEST}).")
    ...
```

Three things to note in the signature alone:

**`Depends(current_user)`** — uploads require a session. Anonymous callers get 401
before any parsing happens.

**`Form(default="", max_length=500)`** — the purpose arrives as a form field
alongside the files, in the same multipart body.

**`purpose` is required.** It defaults to empty and is then rejected if blank. A
blank purpose defeats the point of recording one; Chapter 15's admin view depends
on it being meaningful.

### Per-file validation

```python
for upload_file in files:
    original = upload_file.filename or ""
    name = _safe_filename(original)

    if not name:
        results.append(UploadedFileResult(filename=original or "(unnamed)",
                                          status="skipped", detail="Invalid or empty filename."))
        continue

    extension = Path(name).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        results.append(UploadedFileResult(filename=name, status="skipped",
            detail=f"Unsupported type '{extension or 'none'}'. "
                   f"Allowed: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"))
        continue

    try:
        payload = upload_file.file.read()
    except Exception as exc:
        results.append(UploadedFileResult(filename=name, status="failed",
                                          detail=f"Could not read upload: {exc}"))
        continue
    finally:
        upload_file.file.close()

    if not payload:
        results.append(UploadedFileResult(filename=name, status="skipped",
                                          detail="File is empty."))
        continue

    if len(payload) > MAX_UPLOAD_BYTES:
        results.append(UploadedFileResult(filename=name, status="skipped", bytes=len(payload),
            detail=f"Too large ({len(payload) / 1048576:.1f} MB). "
                   f"Limit is {MAX_UPLOAD_BYTES // 1048576} MB per file."))
        continue
    ...
```

**Every file is validated and reported independently.** One unreadable PDF in a
batch of ten does not lose you the other nine.

Note the three statuses, which the UI colour-codes:

| Status | Meaning |
|---|---|
| `ingested` | Saved and searchable |
| `skipped` | Rejected by a rule — wrong type, empty, too large |
| `failed` | Passed validation but something went wrong |

The distinction is useful: `skipped` is *your* input problem with a clear reason;
`failed` is *our* problem and probably a bug.

**`finally: upload_file.file.close()`** releases the temporary file handle even if
reading throws.

### Ingesting, with attribution

```python
if accepted:
    try:
        from ingest import ingest_files

        uploaded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        summary = ingest_files(
            settings,
            accepted,
            extra_metadata={"uploaded_by": user.username, "uploaded_at": uploaded_at},
        )
        chunks_added = int(summary["chunks_added"])

        by_source = {str(f["source"]): f for f in summary["files"]}
        for result in results:
            entry = by_source.get(result.filename)
            if entry is None:
                continue
            if entry["error"]:
                result.status = "failed"
                result.detail = str(entry["error"])
            else:
                result.chunks = int(entry["chunks"])
                result.uploaded_by = user.username
                auth.record_document(
                    settings, source=result.filename,
                    file_type=Path(result.filename).suffix.lstrip("."),
                    purpose=purpose, size_bytes=result.bytes,
                    chunks=result.chunks, user=user)

    except Exception as exc:
        logger.exception("Ingestion failed after upload")
        detail = _explain_failure(exc)
        for result in results:
            if result.status == "ingested":
                result.status = "failed"
                result.detail = f"Saved to /data, but ingestion failed: {detail}"
```

**One shared ingestion path.** `ingest_files` is the same function the CLI calls
(Chapter 10). An uploaded file is chunked and embedded by identical code — no
second implementation to diverge.

**Attribution flows in as metadata**, which is what Chapter 15's citations depend on.

**The registry row is written only after ingestion succeeds** for that file. A file
that failed to chunk does not get an entry claiming it did.

**Honest failure reporting.** If ingestion throws, files that were already written
to disk are re-marked `failed` with *"Saved to /data, but ingestion failed"* — which
is exactly what happened. Reporting them as `ingested` would be a lie; reporting
them as untouched would also be a lie.

---

## 16.7 The upload UI

The frontend holds selected files locally until you confirm:

```javascript
let queued = [];

function addFiles(fileList) {
  for (const f of fileList) {
    if (queued.some((q) => q.name === f.name && q.size === f.size)) continue;
    queued.push(f);
  }
  renderQueue();
}
```

Nothing is sent on selection. You can review and remove entries first.

### Client-side validation is for feedback only

```javascript
/* Validate client-side purely for fast feedback. The server re-validates
   everything independently — a browser check is a convenience, never a
   security control. */
function problemWith(file) {
  if (!ALLOWED.includes(extOf(file.name))) return "Unsupported type";
  if (file.size === 0) return "Empty file";
  if (file.size > MAX_BYTES) return "Over 25 MB";
  return "";
}
```

The comment states the principle explicitly, because it is the one people get
wrong.

### Drag and drop

```javascript
["dragenter", "dragover"].forEach((ev) =>
  up.drop.addEventListener(ev, (e) => { e.preventDefault(); up.drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) =>
  up.drop.addEventListener(ev, (e) => { e.preventDefault(); up.drop.classList.remove("over"); }));
up.drop.addEventListener("drop", (e) => {
  if (e.dataTransfer && e.dataTransfer.files) addFiles(e.dataTransfer.files);
});
```

`preventDefault()` on `dragover` is **mandatory** — without it the browser's
default action (navigating to the file) wins and the drop never fires. This is the
most common drag-and-drop bug.

### Sending

```javascript
const form = new FormData();
for (const f of sendable) form.append("files", f, f.name);
form.append("purpose", purposeText);

const response = await fetch(API_BASE + "/api/upload", {
  method: "POST", body: form, credentials: "same-origin",
});
```

**No `Content-Type` header is set.** The browser must set it itself, because
`multipart/form-data` requires a generated boundary string. Setting it manually
breaks the request — another common mistake.

**`credentials: "same-origin"`** sends the session cookie. Without it the upload is
anonymous and returns 401.

### Clearing only what succeeded

```javascript
const ok = new Set(payload.files.filter((f) => f.status === "ingested").map((f) => f.filename));
queued = queued.filter((f) => !ok.has(f.name));
renderQueue();
if (!queued.length) auth.purposeInput.value = "";
```

Successful files leave the queue; failures **stay on screen** so you can read the
reason and retry. Clearing everything would hide the problem.

---

## 16.8 The Docker consequence

This feature changed the container configuration:

```yaml
volumes:
  # Your documents, mounted READ-WRITE. The upload endpoint needs write access.
  - ./data:/app/data
```

It was previously `:ro` (read-only), which was correct when only the CLI wrote
there. Uploads would have failed with a permission error.

Verified inside the container:

```
C:\ on /app/data type 9p (rw
WRITE OK
```

And end to end: a file uploaded through the containerised app, written by the
**non-root** container user, landed on the host disk and was answerable seconds
later.

> **A useful habit:** when you add a feature, ask what infrastructure assumption it
> just invalidated. Read-only mounts, network policies and file permissions are
> easy to forget until they fail in the environment you test least.

---

## 16.9 Checkpoint

**1. Upload requires authentication**

```bash
curl -X POST http://localhost:8001/api/upload -F "files=@notes.md"
```
→ `401`

**2. Purpose is required**

Upload with no `purpose` field → `422`. With whitespace only → `422`.

**3. A valid upload becomes searchable immediately**

```bash
curl -b cookies.txt -X POST http://localhost:8001/api/upload \
  -F "files=@Nimbus_Retention.md" \
  -F "purpose=Log retention policy for the platform team"
```

```json
{"uploaded":1,"ingested":1,"chunks_added":1,"total_chunks":20,
 "files":[{"filename":"Nimbus_Retention.md","status":"ingested","chunks":1,
           "uploaded_by":"dana.eng"}]}
```

Then ask a question about it — **without restarting anything**. It should answer
and cite the new file.

**4. Hostile filenames are neutralised**

Send `../../../../app.py` and `..\..\.env` as filenames. Confirm afterwards that
`app.py` and `.env` are unmodified and nothing escaped `data/`.

**5. Mixed batches report per file**

Upload two valid files plus an `.exe` plus an empty file. You should get
`ingested`, `ingested`, `skipped`, `skipped` — with a distinct reason on each.

**6. Re-upload replaces**

Upload the same filename twice. `chunks_added` is non-zero but `total_chunks` is
unchanged.

---

## 16.10 Mistakes to avoid

**Trusting the client filename.** The single most important lesson in this chapter.

**Only sanitising, without verifying containment.** Two layers, always.

**Auto-renaming duplicates.** Poisons retrieval with multiple versions.

**Setting `Content-Type` manually on a `FormData` fetch.** Breaks the boundary.

**Forgetting `credentials: "same-origin"`.** Anonymous request, 401.

**Forgetting `preventDefault()` on `dragover`.** Drop never fires.

**Validating only in the browser.** Trivially bypassed.

**Leaving `data/` read-only in Docker.** Uploads fail in the container while
working locally — the worst kind of environment-specific bug.

**Reporting a file as ingested when only the save succeeded.** Be precise about
what actually happened.

---

## 16.11 A limitation worth stating

Ingestion runs **synchronously inside the upload request**.

For typical documents this is fine — a 145-byte file round-tripped in about two
seconds. But twenty large PDFs at once could take a minute with the browser
waiting, and a proxy timeout would surface as a failed upload even though the work
succeeded.

The fix is a background job with a status endpoint: return `202 Accepted` with a
job id, process asynchronously, let the UI poll. That is the right design for
larger deployments and deliberately out of scope here — it adds a queue, worker
lifecycle and status storage for a problem a personal knowledge base does not have.

**Knowing where a design stops being appropriate is part of understanding it.**

---

## 16.12 What you have

Part V is complete. DevBrain is now a genuine multi-user application: sign-in,
role-based access, per-user document ownership, attribution that reaches every
citation, an admin console, and browser uploads that are searchable the instant
they return.

Part VI turns to operations — seeing inside the system, packaging it, deploying it,
and the bugs that shaped it.

*Next: [Chapter 17 — Observability with LangSmith](ch17-observability.md)*

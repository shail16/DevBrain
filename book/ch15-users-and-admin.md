# Chapter 15 — Users, Documents and the Admin Console

---

## 15.1 What ownership adds

Authentication answers *"who are you?"*. This chapter answers three more:

1. **Who contributed this document?**
2. **What is it for?**
3. **Who is allowed to remove it?**

The result is that an answer can say *"uploaded by alice.dev"* beside its source,
an administrator can see every document with its purpose and owner, and a user can
review their own contributions.

---

## 15.2 The document registry

```sql
CREATE TABLE IF NOT EXISTS devbrain_documents (
    id             SERIAL PRIMARY KEY,
    source         VARCHAR(300) NOT NULL UNIQUE,
    file_type      VARCHAR(20)  NOT NULL DEFAULT '',
    purpose        TEXT         NOT NULL DEFAULT '',
    size_bytes     BIGINT       NOT NULL DEFAULT 0,
    chunks         INTEGER      NOT NULL DEFAULT 0,
    uploaded_by    VARCHAR(40)  NOT NULL DEFAULT 'system',
    uploaded_by_id INTEGER      REFERENCES devbrain_users(id) ON DELETE SET NULL,
    uploaded_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);
```

### Why `source` is UNIQUE

`source` is the same string stored in every chunk's `cmetadata`. Making it unique
means the registry and the vector store **cannot disagree** about what a document
is called. One document, one row, one identity across both systems.

### Why ownership is stored twice

```sql
uploaded_by    VARCHAR(40)   -- the username, as text
uploaded_by_id INTEGER       -- FK, ON DELETE SET NULL
```

This looks redundant. It is deliberate, and the two columns behave differently when
a user is deleted:

| Column | On user deletion | Purpose |
|---|---|---|
| `uploaded_by_id` | Becomes `NULL` | Live queries — "show *my* documents" |
| `uploaded_by` | **Survives** | Audit trail — "alice.dev contributed this" |

So a departed colleague's documents stay in the knowledge base, still credited to
them, but stop appearing in anyone's personal list. Chapter 14 explained the
principle: **the knowledge base should not lose content because someone left.**

### Why `DEFAULT 'system'`

Files ingested from the CLI have no logged-in user. They are attributed to
`system` rather than `NULL`, so the UI never has to render an empty owner.

---

## 15.3 Recording ownership

```python
def record_document(settings, source, file_type, purpose, size_bytes, chunks, user) -> None:
    with get_engine(settings).begin() as connection:
        connection.execute(text("""
            INSERT INTO devbrain_documents
                (source, file_type, purpose, size_bytes, chunks,
                 uploaded_by, uploaded_by_id, uploaded_at)
            VALUES (:s, :t, :p, :b, :c, :un, :ui, now())
            ON CONFLICT (source) DO UPDATE SET
                file_type      = EXCLUDED.file_type,
                purpose        = CASE WHEN EXCLUDED.purpose <> ''
                                      THEN EXCLUDED.purpose
                                      ELSE devbrain_documents.purpose END,
                size_bytes     = EXCLUDED.size_bytes,
                chunks         = EXCLUDED.chunks,
                uploaded_by    = EXCLUDED.uploaded_by,
                uploaded_by_id = EXCLUDED.uploaded_by_id,
                uploaded_at    = now()
        """), {...})
```

An upsert keyed on `source`. Re-uploading transfers ownership to whoever uploaded
last — which is the behaviour you want, since they are now responsible for the
content.

### The CASE expression

```sql
purpose = CASE WHEN EXCLUDED.purpose <> ''
               THEN EXCLUDED.purpose
               ELSE devbrain_documents.purpose END
```

Every other column overwrites unconditionally. `purpose` does not.

If a re-upload supplies a blank purpose, the **existing** description is preserved
rather than being wiped. Losing a carefully-written description because someone
re-uploaded in a hurry is a small, avoidable annoyance — and this is where you
prevent it.

> Chapter 4 warned that an upsert only updates the columns you name. This is the
> constructive use of that fact: choosing *not* to update a column is a feature.

---

## 15.4 Attribution that reaches the answer

Ownership is not just a database row — it travels with the chunk.

**At upload (Chapter 16):**

```python
uploaded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
summary = ingest_files(
    settings,
    accepted,
    extra_metadata={"uploaded_by": user.username, "uploaded_at": uploaded_at},
)
```

**Stamped onto every chunk (Chapter 10):**

```python
if extra_metadata:
    for chunk in chunks:
        chunk.metadata.update(extra_metadata)
```

**Carried into the prompt (Chapter 11):**

```python
uploader = metadata.get("uploaded_by")
credit = f"\nUploaded by: {uploader}" if uploader else ""
```

**Returned in citations:**

```python
"uploaded_by": metadata.get("uploaded_by", "") or "",
"uploaded_at": metadata.get("uploaded_at", "") or "",
```

**Rendered in the UI (Chapter 13):**

```javascript
${s.uploaded_by ? `<span class="credit">uploaded by ${esc(s.uploaded_by)}</span>` : ""}
```

Because the metadata lives in `cmetadata` alongside the vector, it comes back
*attached to every retrieved chunk* — no second lookup, no join at query time.

The registry remains the authoritative record for the admin view; the chunk
metadata is a denormalised copy optimised for the read path. That is a reasonable
trade: attribution is written once and read on every question.

---

## 15.5 The three views

| Endpoint | Who | Shows |
|---|---|---|
| `GET /api/my/documents` | Any signed-in user | **Own** uploads only |
| `GET /api/admin/documents` | Admin | **All** documents |
| `GET /api/admin/users` | Admin | All accounts |

### Scoping happens on the server

```python
@app.get("/api/my/documents")
def my_documents(user: auth.User = Depends(current_user)) -> JSONResponse:
    settings = _require_settings()
    documents = auth.list_documents_for_user(settings, user.id)
    ...
```

```python
def list_documents_for_user(settings: Settings, user_id: int) -> list[dict]:
    """
    Matched on `uploaded_by_id` rather than the username string so a renamed
    account keeps its history.
    """
    ...
    WHERE uploaded_by_id = :i
    ORDER BY uploaded_at DESC
```

**The filter is in SQL, driven by the session's user id.** It is not a client-side
filter over a full list, and the user id does not come from the request body — it
comes from the session. There is no parameter to tamper with.

Verified with two users:

```
dana sees: ['Nimbus_Retention.md']     ravi sees: ['Orion_Rollback.md']
[PASS] dana does NOT see ravi's doc
[PASS] ravi does NOT see dana's doc
[PASS] anon /api/my/documents -> 401
```

### Live chunk counts

```python
live = dict(db.count_chunks_by_source(settings))
for document in documents:
    document["chunks"] = live.get(document["source"], 0)
    document["on_disk"] = (settings.data_dir / document["source"]).exists()
```

The registry has a `chunks` column, but the admin view **ignores it** and queries
the vector store instead.

Why: the registry records what was stored *at upload time*. If chunks were removed
some other way, that number is stale. Reading from the vector store means the
figure shown is **what retrieval will actually search**.

`on_disk` catches the opposite drift — a registry row whose file was deleted from
`data/` manually. The UI shows a *"file missing"* badge.

**Show live truth, not remembered truth.**

### Surfacing orphans

```python
known = {d["source"] for d in documents}
for source, chunks in live.items():
    if source not in known:
        documents.append({
            "source": source,
            "file_type": Path(source).suffix.lstrip("."),
            "purpose": "", "size_bytes": 0, "chunks": chunks,
            "uploaded_by": "system", "uploaded_at": None,
            "on_disk": (settings.data_dir / source).exists(),
        })
```

Files ingested by the CLI before this feature existed have chunks but no registry
row. Rather than hiding them, the admin view synthesises an entry marked `system`.

**An admin console that cannot see everything is worse than useless** — it gives
false confidence. If something is searchable, it must be manageable.

---

## 15.6 Deleting a document

```python
@app.delete("/api/admin/documents/{source:path}")
def admin_delete_document(source: str, admin: auth.User = Depends(current_admin)) -> JSONResponse:
    settings = _require_settings()

    # `source` comes from the URL, so treat it as untrusted and confirm the
    # resolved path really is inside the data folder before deleting anything.
    target = (settings.data_dir / source).resolve()
    try:
        target.relative_to(settings.data_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid document path.") from None

    removed_chunks = db.delete_chunks_for_source(settings, source)
    auth.forget_document(settings, source)

    file_deleted = False
    if target.exists() and target.is_file():
        try:
            target.unlink()
            file_deleted = True
        except Exception as exc:
            raise HTTPException(status_code=500, detail=
                f"Chunks removed, but the file could not be deleted: {exc}") from exc

    return JSONResponse(content={
        "ok": True, "source": source,
        "chunks_removed": removed_chunks,
        "file_deleted": file_deleted,
        "total_chunks": db.count_chunks(settings),
    })
```

### Three things must go

| What | Why |
|---|---|
| Chunks in pgvector | Otherwise it keeps answering questions |
| The registry row | Otherwise it haunts the admin list |
| The file on disk | Otherwise the next `ingest.py` resurrects it |

Miss any one and you get a confusing partial deletion. Miss the chunks in
particular and the document is *invisible but still answering* — the worst outcome.

### Order matters

Chunks first, then registry, then file. If the file deletion fails, you are left
with an orphaned file that a future ingestion will re-add — recoverable. The
reverse order could leave chunks with no file and no registry entry, which is
harder to reason about.

### Path traversal defence

`{source:path}` accepts slashes, which is needed for `python/asyncio.md`. That also
means `../../../app.py` arrives intact.

`resolve()` collapses `..`, then `relative_to()` raises `ValueError` if the result
escapes `data/`. Deletion is refused before anything is touched.

**Any path segment that came from a request is untrusted.** Chapter 16 applies the
same reasoning to uploaded filenames, with a second layer.

### The index refreshes itself

Because chunks are deleted directly from the vector store, **the index is current
the moment this returns.** No re-ingestion step, no restart.

Verified:

```
[PASS] delete document -> 200  {"chunks_removed":1,"file_deleted":true,"total_chunks":19}
[PASS] total chunks dropped  -- 20 -> 19
[PASS] deleted doc no longer retrievable
```

That last check re-asks a question the document used to answer and confirms it is
gone from the sources.

---

## 15.7 Admin routes and the layered guard

```python
@app.get("/api/admin/users")
def admin_list_users(admin: auth.User = Depends(current_admin)) -> JSONResponse:
    settings = _require_settings()
    return JSONResponse(content={"users": auth.list_users(settings), "me": admin.id})


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: int, admin: auth.User = Depends(current_admin)) -> JSONResponse:
    settings = _require_settings()
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="You cannot delete your own account.")
    username = auth.delete_user(settings, user_id)
    return JSONResponse(content={"ok": True, "username": username})
```

Note `"me": admin.id` in the list response. The UI uses it to disable the delete
button on the admin's own row — a usability affordance, not a security control.
The server check above is the real guard.

### Hiding a tab is not security

The frontend hides admin navigation from non-admins:

```javascript
auth.tabUsers.hidden = !user.is_admin;
auth.tabDocs.hidden  = !user.is_admin;
```

**That is usability only.** Anyone can edit the DOM or call the API directly. Every
`/api/admin/*` route independently re-checks the session *and* the admin flag
server-side via `Depends(current_admin)`.

Verified: a standard user calling both admin endpoints gets **403**.

> **Authorisation always belongs on the server.** The UI is a convenience on top of
> it, never a substitute. If you can only enforce a rule in one place, enforce it
> where the attacker cannot reach.

---

## 15.8 The admin console UI

Both admin panels and the user's own view share one table pattern:

```javascript
const head =
  '<div class="arow head">' +
  '<span class="acol main">Document</span>' +
  '<span class="acol mid">Purpose</span>' +
  '<span class="acol sm">Type</span>' +
  '<span class="acol mid">Uploaded by</span>' +
  '<span class="acol act">Action</span></div>';

const rows = data.documents.map((d) => (
  '<div class="arow">' +
  '<span class="acol main"><span class="aname amono">' + esc(d.source) +
    '<span class="badge">' + d.chunks + " chunk" + (d.chunks === 1 ? "" : "s") + "</span>" +
    (d.on_disk ? "" : '<span class="badge failed">file missing</span>') + "</span>" +
    '<span class="asub">' + esc(fmtDate(d.uploaded_at)) + "</span></span>" +
  '<span class="acol mid aval">' + esc(d.purpose || "—") + "</span>" +
  ...
)).join("");
```

Every server value passes through `esc()` — Chapter 13's rule, applied
consistently. Filenames and purposes are user-supplied and must be treated as
hostile.

### Confirmation before destruction

```javascript
if (!confirm('Delete "' + src + '"?\n\nThe file is removed from the data folder ' +
             'and all of its chunks are deleted from the index. This cannot be undone.')) return;
```

The message states **exactly** what will happen — both the file and the chunks —
and that it is irreversible. A confirmation saying only *"Are you sure?"* trains
people to click through without reading.

### Refresh after mutation

```javascript
await loadDocs();
checkHealth();       // chunk count in the status bar changed
adminNotice(auth.docsBody,
  'Deleted "' + src + '" — ' + d.chunks_removed + " chunk(s) removed, index refreshed.", true);
```

After deleting, the table reloads *and* the health poll refreshes the status bar,
so the chunk count updates immediately. The notice reports the actual number
removed rather than a generic success message.

---

## 15.9 Checkpoint

**1. Two users cannot see each other's documents**

Register `dana.eng` and `ravi.ops`, upload one document each with distinct
purposes, then call `/api/my/documents` as each.

Verified: each sees exactly their own.

**2. Attribution reaches the answer**

Ask a question that hits an uploaded document. The response should contain:

```json
"sources": [{"source": "Falcon_Deploy.md", "uploaded_by": "alice.dev", ...}]
```

and the UI should show an *uploaded by alice.dev* pill.

**3. Admin sees everything, with purpose and owner**

```
[PASS] admin sees dana's doc
[PASS] admin sees ravi's doc
[PASS] admin sees uploader  -- ravi.ops
[PASS] admin sees purpose   -- Rollback runbook for on-call
```

**4. Non-admin is blocked**

```
[PASS] GET /api/admin/users     -> 403
[PASS] GET /api/admin/documents -> 403
```

**5. Deleting a document really removes it**

Check chunk count before and after, confirm the file is gone from `data/`, and
re-ask a question it used to answer — it should no longer be cited.

**6. Deleting a user is immediate**

With the user's session still open, have the admin delete them. Their **next**
request returns 401, and they cannot log back in.

---

## 15.10 Mistakes to avoid

**Filtering "my documents" in the browser.** Send only the user's own rows. A
client-side filter over a full list is not access control.

**Trusting a user id from the request body.** Take it from the session.

**Deleting the file but not the chunks.** The document becomes invisible while
still answering questions — the worst possible state.

**Trusting a path segment from a URL.** `{source:path}` accepts `../`. Resolve and
verify containment.

**Relying on hidden UI for authorisation.** Re-check on every server route.

**Showing the registry's stored chunk count.** It goes stale. Query the vector
store.

**Hiding CLI-ingested documents from the admin view.** If it is searchable, it must
be manageable.

**Overwriting a purpose with a blank one.** The `CASE` expression in §15.3.

---

## 15.11 What you have

Documents with owners, purposes and timestamps. Attribution that survives all the
way from upload into the citation beside an answer. A personal view scoped in SQL,
an admin console that sees everything including orphans, and deletion that clears
file, registry and index together — with the index current the moment it returns.

Next: the upload endpoint that feeds all of this, and defending it against hostile
filenames.

*Next: [Chapter 16 — File Upload and Live Ingestion](ch16-file-upload.md)*

# Chapter 9 — Document Loaders

*© 2026 Shailendra Srivastava. All rights reserved.*

---

## 9.1 The job

A loader takes a file on disk and returns a list of LangChain `Document` objects.

```python
Document(
    page_content="the actual text",
    metadata={"source": "notes.pdf", "page": 3, "file_type": "pdf"},
)
```

Two fields, and the split between them is the whole design:

| Field | Purpose |
|---|---|
| `page_content` | The text that gets chunked, embedded and searched |
| `metadata` | Everything needed to *cite* it back to the user |

Get `page_content` wrong and retrieval fails. Get `metadata` wrong and the answer
is unverifiable. Both matter.

---

## 9.2 Why we write our own

LangChain ships loaders for all these formats. DevBrain does not use them. Three
reasons, in increasing order of importance:

**`langchain-community` is being sunset.** It emits deprecation warnings on import
and is no longer actively maintained.

**Its Excel loader needs `unstructured`.** That is a large dependency tree pulling
in a great deal you will never use, to read a spreadsheet.

**Control over extraction quality.** This is the real reason. Writing directly
against `pypdf`, `docx2txt` and `openpyxl` lets us make decisions that measurably
improve retrieval — decisions a generic loader cannot make for us. §9.6 shows the
one that matters most.

The cost is about 290 lines. The benefit is fewer dependencies, no deprecation
warnings, better error messages, and spreadsheets that actually work.

> **A judgement call worth naming:** this is not a general rule. Reaching for a
> library is usually right. Here we are in the *hot path of answer quality*, and
> the library's abstraction was actively getting in the way.

---

## 9.3 The supported set

```python
SUPPORTED_EXTENSIONS: set[str] = {
    ".txt", ".md", ".markdown", ".log", ".csv",
    ".pdf",
    ".docx",
    ".xlsx", ".xlsm",
}

IGNORED_DIR_NAMES: set[str] = {
    ".git", ".venv", "venv", "__pycache__",
    ".ipynb_checkpoints", "node_modules",
}
```

Note what is **absent**: `.doc` (the pre-2007 Word format) and `.xls`. These are
entirely different binary formats, not just older versions, and would need separate
libraries. Better to reject them clearly than to half-support them.

---

## 9.4 Plain text — harder than it looks

```python
def _read_text_with_fallback(path: Path) -> str:
    """
    Read a text file, trying a few encodings before giving up.

    Notes exported from Windows tools are frequently cp1252 or UTF-8-with-BOM
    rather than clean UTF-8. Guessing in this order avoids the
    UnicodeDecodeError that would otherwise abort the whole ingestion run.
    """
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_bytes().decode("utf-8", errors="replace")
```

The order is deliberate:

| Encoding | Catches |
|---|---|
| `utf-8-sig` | UTF-8 **with a BOM** — Notepad and Excel love adding one |
| `utf-8` | The modern default |
| `cp1252` | Windows-1252 — smart quotes, em dashes from Word |
| `latin-1` | Never fails; decodes any byte sequence |

`utf-8-sig` must come **first**. If plain `utf-8` ran first it would succeed on a
BOM file and leave an invisible `﻿` at the start of your text — which then
becomes the first character of your first chunk, and quietly pollutes its embedding.

Because `latin-1` cannot fail, the final line is unreachable in practice. It exists
so the function has an explicit return rather than falling off the end returning
`None`.

**Why this matters:** without it, one file exported from Word aborts your entire
ingestion run with a `UnicodeDecodeError`. That is a terrible experience for a
problem that is this easy to absorb.

---

## 9.5 PDF — one Document per page

```python
def load_pdf_file(path: Path) -> list[Document]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))

    # Some PDFs are "encrypted" with an empty owner password purely to prevent
    # printing. Those decrypt with an empty string and are perfectly readable.
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:
            raise RuntimeError("PDF is password protected and cannot be read.") from exc

    documents: list[Document] = []
    for page_number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if not text.strip():
            # Blank result usually means a scanned image. There is no text layer
            # to embed, so skip it (OCR would be needed).
            continue
        documents.append(
            Document(
                page_content=text,
                metadata={
                    "file_type": "pdf",
                    "page": page_number,
                    "total_pages": len(reader.pages),
                },
            )
        )
    return documents
```

### One Document per page

This is the key decision. It costs nothing and buys you `page` metadata, which
flows all the way through to the answer:

> *"...mitigate before you diagnose. [incident-response-runbook.pdf (page 1)]"*

Without per-page splitting you could only cite the file. With it, a reader can turn
to the exact page and check. That is the difference between a citation and a
*verifiable* citation.

### Three defensive touches

**Empty-password decryption.** Many PDFs are "encrypted" only to discourage
printing. `reader.decrypt("")` opens those. A genuinely protected PDF raises, and
we convert that into a clear message.

**Per-page exception handling.** One malformed page should not lose the other
forty. The `try` inside the loop contains the damage.

**Blank pages skipped, not stored.** A page yielding no text is almost always a
scan. Storing an empty chunk would waste an embedding call and occupy a retrieval
slot with nothing.

### The honest limitation

**Scanned PDFs will not work.** If a PDF is photographs of pages, there is no text
layer. `pypdf` returns nothing and there is nothing to embed.

Chapter 10 surfaces this explicitly rather than silently:

```
!  empty   scan.pdf: no extractable text (a scanned/image-only PDF is the usual cause)
```

Fixing it requires OCR — Tesseract locally, or a service like MinerU, AWS Textract
or Azure Document Intelligence. Chapter 21 discusses when that is worth the weight.
For most technical documentation it is not.

---

## 9.6 Excel — where extraction quality is won

This is the most important function in the file, and the clearest example of why
writing our own loader paid off.

### The problem

A spreadsheet is a grid. Dump it naively and you get:

```
Redis  Cache / KV store  Session storage, rate limiting  Data must survive a restart
Kafka  Event streaming   Durable event log, replay      You need simple task queues
```

Now chunk that. A chunk taken from the middle of a 500-row sheet contains rows with
**no header**. The values have lost their meaning — is "Redis" a technology, an
owner, a status? The embedding is correspondingly meaningless, and retrieval fails.

### The fix

Repeat the column header on every row:

```
Worksheet: Datastores
Technology: Redis | Category: Cache / KV store | Use Case: Session storage, rate limiting | Avoid When: Data must survive a restart
Technology: Kafka | Category: Event streaming | Use Case: Durable event log, replay | Avoid When: You need simple task queues
```

Now **every row is self-describing**. A chunk from anywhere in the sheet still
carries enough context to be understood and embedded meaningfully.

### The code

```python
def load_xlsx_file(path: Path) -> list[Document]:
    from openpyxl import load_workbook

    # data_only=True returns the last-calculated value of formulas instead of
    # the formula text itself (we want "42", not "=SUM(A1:A9)").
    workbook = load_workbook(filename=str(path), read_only=True, data_only=True)
    documents: list[Document] = []

    try:
        for sheet in workbook.worksheets:
            rows = [
                [_cell_to_str(cell) for cell in raw_row]
                for raw_row in sheet.iter_rows(values_only=True)
            ]
            rows = [row for row in rows if any(row)]      # drop empty rows
            if not rows:
                continue

            header: list[str] | None = None
            if _looks_like_header(rows[0]):
                header = rows[0]
                rows = rows[1:]

            lines: list[str] = []
            for row in rows:
                if header:
                    pairs = [
                        f"{name}: {value}"
                        for name, value in zip(header, row)
                        if value and name
                    ]
                    line = " | ".join(pairs)
                else:
                    line = " | ".join(cell for cell in row if cell)
                if line:
                    lines.append(line)

            if not lines:
                continue

            body = f"Worksheet: {sheet.title}\n" + "\n".join(lines)
            documents.append(
                Document(
                    page_content=body,
                    metadata={
                        "file_type": path.suffix.lower().lstrip("."),
                        "sheet": sheet.title,
                    },
                )
            )
    finally:
        workbook.close()

    return documents
```

### Details that matter

**`data_only=True`** returns cached formula *values*, not formula text. Embedding
`=SUM(A1:A9)` would be useless; embedding `42` is not.

**`read_only=True`** streams rather than loading the whole workbook into memory —
important for large files. It also means the file handle stays open, which is why
`workbook.close()` sits in a `finally` block.

**One Document per worksheet**, with `sheet` metadata. Citations can say *"sheet
'Datastores'"*.

**Header detection is heuristic:**

```python
def _looks_like_header(row: list[str]) -> bool:
    """A header row is one where every populated cell is non-numeric text."""
    populated = [c for c in row if c]
    if len(populated) < 2:
        return False
    return all(not c.replace(".", "", 1).replace("-", "", 1).isdigit() for c in populated)
```

Requires at least two populated cells, all non-numeric. Not perfect — a sheet whose
first row is data-with-text-values would be misread. But the failure is graceful:
you lose one row and get slightly worse labelling, not a crash.

### It works

From the real project, this question:

> *"Which datastore should I use for caching sessions, and when should I avoid it?"*

retrieved `tech-stack-decisions.xlsx (sheet 'Datastores')` and answered:

> *"You should use Redis for caching sessions. Avoid using Redis when data must
> survive a restart without RDB/AOF tuning."*

That answer is only possible because the Redis row carried its own headers. Flatten
the sheet naively and this question fails.

---

## 9.7 Word

```python
def load_docx_file(path: Path) -> list[Document]:
    import docx2txt

    text = docx2txt.process(str(path)) or ""
    if not text.strip():
        return []
    return [Document(page_content=text, metadata={"file_type": "docx"})]
```

Deliberately simple. A `.docx` is a zip of XML; `docx2txt` walks it and returns
plain text including headers, footers and table contents.

**No page metadata** — Word documents have no fixed pagination (it depends on
printer, font, zoom), so there is no honest page number to record. Better to omit
the field than invent one.

---

## 9.8 Dispatch and discovery

```python
_LOADER_BY_EXTENSION = {
    ".txt": load_text_file,
    ".md": load_text_file,
    ".markdown": load_text_file,
    ".log": load_text_file,
    ".csv": load_text_file,
    ".pdf": load_pdf_file,
    ".docx": load_docx_file,
    ".xlsx": load_xlsx_file,
    ".xlsm": load_xlsx_file,
}
```

A dictionary, not an `if/elif` chain. Adding a format is one line plus a function —
which is exactly the extension point Chapter 21 uses for OCR.

```python
def load_document(path: Path, source_label: str | None = None) -> list[Document]:
    extension = path.suffix.lower()
    loader = _LOADER_BY_EXTENSION.get(extension)
    if loader is None:
        raise UnsupportedFileType(
            f"{path.name}: '{extension}' is not supported. "
            f"Supported types: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    documents = loader(path)
    label = source_label or path.name
    for document in documents:
        document.metadata["source"] = label
        document.metadata["file_name"] = path.name
    return documents
```

**`source` is stamped centrally**, not by each loader. One place to change, and no
loader can forget it. `source_label` lets the caller pass a short relative path
(`python/asyncio.md`) rather than an absolute one — that label is what the user sees
as a citation, so it should be readable.

```python
def discover_files(data_dir: Path) -> tuple[list[Path], list[Path]]:
    supported: list[Path] = []
    skipped: list[Path] = []

    if not data_dir.exists():
        return supported, skipped

    for path in sorted(data_dir.rglob("*")):
        if not path.is_file():
            continue
        if any(part in IGNORED_DIR_NAMES for part in path.parts):
            continue
        if path.name.startswith((".", "~$")):
            continue

        if path.suffix.lower() in SUPPORTED_EXTENSIONS:
            supported.append(path)
        else:
            skipped.append(path)

    return supported, skipped
```

**Returns both lists.** Chapter 10 reports what it is ignoring, because silence
here is confusing — a user whose file never appears in results deserves to know it
was skipped and why.

**`~$` files are Office lock files.** Open a `.docx` in Word and `~$document.docx`
appears beside it. Ingesting those produces garbage.

**`sorted()`** makes ingestion order deterministic, which makes output diffable and
tests reproducible.

---

## 9.9 Checkpoint

Create `data/test.md` with a heading and a paragraph, then:

```bash
python -c "
from pathlib import Path
from loaders import discover_files, load_document

files, skipped = discover_files(Path('data'))
print('found  :', [f.name for f in files])
print('skipped:', [f.name for f in skipped])

for f in files:
    docs = load_document(f, source_label=f.name)
    total = sum(len(d.page_content) for d in docs)
    print(f'{f.name:35} parts={len(docs)} chars={total}')
    print('   metadata:', docs[0].metadata if docs else None)
"
```

Expected shape:

```
found  : ['test.md']
skipped: []
test.md                             parts=1 chars=142
   metadata: {'file_type': 'md', 'source': 'test.md', 'file_name': 'test.md'}
```

Then test each type properly. Drop in a real PDF, `.docx` and `.xlsx`. Verified
output from the project:

```
code-review-standards.docx        parts=1  chars=1,854  {'file_type': 'docx', ...}
docker-tips.md                    parts=1  chars=2,600  {'file_type': 'md', ...}
incident-response-runbook.pdf     parts=2  chars=2,084  {'file_type': 'pdf', 'page': 1, 'total_pages': 2, ...}
tech-stack-decisions.xlsx         parts=2  chars=1,963  {'file_type': 'xlsx', 'sheet': 'Datastores', ...}
```

Note the PDF produced **2 parts** (two pages) and the Excel **2 parts** (two
sheets), each with the right metadata. That is the per-page/per-sheet design
working.

**Check the Excel flattening specifically:**

```python
print(repr(docs[0].page_content[:200]))
```

You should see `Technology: PostgreSQL | Category: Relational DB | ...` — headers
repeated on the row. If you see bare values, header detection did not fire.

---

## 9.10 Mistakes to avoid

**Assuming a PDF has text.** Always check. A scanned document extracts nothing, and
the failure is silent unless you report it.

**Putting `utf-8` before `utf-8-sig`.** You will carry an invisible BOM into your
first chunk.

**Flattening spreadsheets without headers.** The single biggest extraction-quality
mistake, and the one most likely to go unnoticed — nothing errors, answers are just
worse.

**Letting one bad file abort the run.** Chapter 10 wraps every load in a try/except
for exactly this reason.

**Forgetting `workbook.close()`.** With `read_only=True`, openpyxl holds the file
handle. On Windows that means you cannot delete or overwrite the file afterwards.

**Trusting `.doc` will work because `.docx` does.** Different format entirely.

---

## 9.11 What you have

`loaders.py` — roughly 290 lines turning five file types into `Document` objects
with consistent, citable metadata. No external service, no API keys, milliseconds
per file.

Next: discovering files, splitting them into chunks, embedding them, and storing
them idempotently.

*Next: [Chapter 10 — The Ingestion Engine](ch10-ingestion-engine.md)*

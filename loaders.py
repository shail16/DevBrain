"""
DevBrain — document loaders.

Job of this module: turn a file on disk into a list of LangChain `Document`
objects. A `Document` is a tiny container with two parts:

    Document(page_content="the actual text", metadata={"source": "notes.pdf", ...})

`page_content` is what gets embedded and searched. `metadata` is what we show
the user as a citation, so we populate it carefully for every file type.

Each loader is deliberately written against the underlying library (`pypdf`,
`docx2txt`, `openpyxl`) rather than a generic wrapper. That means one fewer
dependency layer, clearer error messages, and full control over how spreadsheet
rows are flattened into text — which matters a lot for answer quality.
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.documents import Document

# File extensions DevBrain knows how to read.
SUPPORTED_EXTENSIONS: set[str] = {
    ".txt",
    ".md",
    ".markdown",
    ".log",
    ".csv",
    ".pdf",
    ".docx",
    ".xlsx",
    ".xlsm",
}

# Folders we never walk into when scanning /data.
IGNORED_DIR_NAMES: set[str] = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".ipynb_checkpoints",
    "node_modules",
}


class UnsupportedFileType(Exception):
    """Raised for extensions DevBrain does not handle."""


# --------------------------------------------------------------------------- #
#  Plain text (.txt, .md, .log, .csv)
# --------------------------------------------------------------------------- #


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
    # latin-1 can decode any byte sequence, so reaching here is practically
    # impossible — but be explicit rather than returning None.
    return path.read_bytes().decode("utf-8", errors="replace")


def load_text_file(path: Path) -> list[Document]:
    text = _read_text_with_fallback(path)
    if not text.strip():
        return []
    return [Document(page_content=text, metadata={"file_type": path.suffix.lower().lstrip(".")})]


# --------------------------------------------------------------------------- #
#  PDF (.pdf)
# --------------------------------------------------------------------------- #


def load_pdf_file(path: Path) -> list[Document]:
    """
    One Document per page, so citations can point at a specific page number.
    """
    from pypdf import PdfReader

    reader = PdfReader(str(path))

    # Some PDFs are "encrypted" with an empty owner password purely to prevent
    # printing. Those decrypt with an empty string and are perfectly readable.
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller as a skip
            raise RuntimeError(
                "PDF is password protected and cannot be read."
            ) from exc

    documents: list[Document] = []
    for page_number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - a single broken page shouldn't kill the file
            text = ""
        if not text.strip():
            # Blank result usually means a scanned image. There is no text layer
            # to embed, so skip it (OCR would be needed).
            continue
        documents.append(
            Document(
                page_content=text,
                metadata={"file_type": "pdf", "page": page_number, "total_pages": len(reader.pages)},
            )
        )
    return documents


# --------------------------------------------------------------------------- #
#  Word (.docx)
# --------------------------------------------------------------------------- #


def load_docx_file(path: Path) -> list[Document]:
    import docx2txt

    text = docx2txt.process(str(path)) or ""
    if not text.strip():
        return []
    return [Document(page_content=text, metadata={"file_type": "docx"})]


# --------------------------------------------------------------------------- #
#  Excel (.xlsx, .xlsm)
# --------------------------------------------------------------------------- #


def _cell_to_str(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _looks_like_header(row: list[str]) -> bool:
    """A header row is one where every populated cell is non-numeric text."""
    populated = [c for c in row if c]
    if len(populated) < 2:
        return False
    return all(not c.replace(".", "", 1).replace("-", "", 1).isdigit() for c in populated)


def load_xlsx_file(path: Path) -> list[Document]:
    """
    One Document per worksheet.

    Spreadsheets need special care: a bare grid of values embeds poorly because
    the numbers lose their meaning. So when the first row looks like a header we
    repeat the column name on every row:

        Tool: Redis | Use case: Caching | Notes: In-memory, volatile

    That way a chunk retrieved from the middle of a 500-row sheet still carries
    enough context for the LLM to interpret it.
    """
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
            # Drop entirely empty rows.
            rows = [row for row in rows if any(row)]
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
                    metadata={"file_type": path.suffix.lower().lstrip("."), "sheet": sheet.title},
                )
            )
    finally:
        # read_only workbooks hold an open file handle until closed.
        workbook.close()

    return documents


# --------------------------------------------------------------------------- #
#  Dispatch
# --------------------------------------------------------------------------- #

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


def load_document(path: Path, source_label: str | None = None) -> list[Document]:
    """
    Load one file into Documents, stamping consistent `source` metadata.

    `source_label` should be a short, human-readable path (relative to /data)
    because it is what the web UI shows underneath each answer.
    """
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


def discover_files(data_dir: Path) -> tuple[list[Path], list[Path]]:
    """
    Walk `data_dir` recursively.

    Returns `(supported, skipped)` so the caller can report both what it will
    ingest and what it is ignoring — silence here is confusing when a user's
    file simply never shows up in search results.
    """
    supported: list[Path] = []
    skipped: list[Path] = []

    if not data_dir.exists():
        return supported, skipped

    for path in sorted(data_dir.rglob("*")):
        if not path.is_file():
            continue
        # Skip anything inside an ignored folder, and hidden/temp Office files.
        if any(part in IGNORED_DIR_NAMES for part in path.parts):
            continue
        if path.name.startswith((".", "~$")):
            continue

        if path.suffix.lower() in SUPPORTED_EXTENSIONS:
            supported.append(path)
        else:
            skipped.append(path)

    return supported, skipped

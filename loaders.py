"""
DevBrain — document loaders.

Job of this module: turn a file on disk into a list of LangChain `Document`
objects. A `Document` is a tiny container with two parts:

    Document(page_content="the actual text", metadata={"source": "notes.pdf", ...})

`page_content` is what gets embedded and searched. `metadata` is what we show
the user as a citation, so we populate it carefully for every file type.

Each loader is deliberately written against the underlying library (`pypdf`,
`docx2txt`, `openpyxl`, `Pillow`) rather than a generic wrapper. That means one fewer
dependency layer, clearer error messages, and full control over how spreadsheet
rows are flattened into text — which matters a lot for answer quality.
"""

from __future__ import annotations

import re
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
    ".html",
    ".htm",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
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
    images_read = 0
    for page_number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - a single broken page shouldn't kill the file
            text = ""

        # Images embedded in the page (figures, screenshots, scanned pages) are
        # read by the vision model and appended to the page text.
        try:
            page_images = list(page.images)
        except Exception:  # noqa: BLE001 - unsupported image filters etc.
            page_images = []
        for image in page_images:
            if images_read >= _MAX_EMBEDDED_IMAGES:
                break
            described = _describe_embedded_image(image.data)
            if described:
                images_read += 1
                text += f"\n\n[Embedded image: {image.name}]\n{described}"

        if not text.strip():
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

    # Embedded pictures live in word/media/ inside the .docx zip.
    import zipfile

    image_exts = (".png", ".jpg", ".jpeg", ".gif")
    try:
        with zipfile.ZipFile(path) as archive:
            media = sorted(
                n for n in archive.namelist()
                if n.startswith("word/media/") and n.lower().endswith(image_exts)
            )[:_MAX_EMBEDDED_IMAGES]
            for name in media:
                described = _describe_embedded_image(archive.read(name))
                if described:
                    text += f"\n\n[Embedded image: {name.rsplit('/', 1)[-1]}]\n{described}"
    except zipfile.BadZipFile:
        pass

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
#  HTML (.html, .htm)
# --------------------------------------------------------------------------- #

from html.parser import HTMLParser

_HTML_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head"}
_HTML_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article",
    "header", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote",
}


class _HTMLTextExtractor(HTMLParser):
    """Collects visible text, dropping scripts/styles, keeping block structure."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.image_srcs: list[str] = []
        self._in_title = False
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        elif tag in _HTML_SKIP_TAGS and tag != "head":
            self._skip_depth += 1
        elif tag in _HTML_BLOCK_TAGS:
            self._parts.append("\n")
        elif tag in ("td", "th"):
            self._parts.append(" | ")
        elif tag == "img":
            attr_map = dict(attrs)
            src = attr_map.get("src")
            if src and not self._skip_depth:
                self.image_srcs.append(src.strip())
            alt = attr_map.get("alt")
            if alt and not self._skip_depth:
                self._parts.append(f" [image: {alt.strip()}] ")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in _HTML_SKIP_TAGS and tag != "head":
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _HTML_BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in raw.split("\n")]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def load_html_file(path: Path) -> list[Document]:
    extractor = _HTMLTextExtractor()
    extractor.feed(_read_text_with_fallback(path))
    extractor.close()
    text = extractor.text()

    # Read embedded images: inline data: URIs and local files next to the HTML.
    # Remote (http/https) images are never fetched.
    import base64
    from urllib.parse import unquote, urlsplit

    base_dir = path.parent.resolve()
    seen: set[str] = set()
    count = 0
    for src in extractor.image_srcs:
        if count >= _MAX_EMBEDDED_IMAGES or src in seen:
            continue
        seen.add(src)
        data: bytes | None = None
        label = "inline image"
        try:
            if src.startswith("data:image/"):
                header, _, payload = src.partition(",")
                data = base64.b64decode(payload) if ";base64" in header else unquote(payload).encode("latin-1")
            elif "://" not in src and not src.startswith("//"):
                local = (base_dir / unquote(urlsplit(src).path)).resolve()
                if base_dir in local.parents and local.is_file():
                    data = local.read_bytes()
                    label = local.name
        except Exception:  # noqa: BLE001 - malformed data URI / unreadable file
            data = None
        if not data:
            continue
        described = _describe_embedded_image(data)
        if described:
            count += 1
            text += f"\n\n[Embedded image: {label}]\n{described}"
    text = text.strip()

    if not text:
        return []
    title = extractor.title.strip()
    if title:
        text = f"{title}\n\n{text}"
    metadata = {"file_type": "html"}
    if title:
        metadata["title"] = title
    return [Document(page_content=text, metadata=metadata)]


# --------------------------------------------------------------------------- #
#  Images (.jpg, .jpeg, .png, .gif) — described/transcribed by a vision model
# --------------------------------------------------------------------------- #

# Longest side sent to the model; larger images are downscaled to save tokens.
_IMAGE_MAX_SIDE = 2048

_IMAGE_PROMPT = (
    "Extract all information from this image so it can be searched later. "
    "First transcribe every piece of visible text exactly, preserving structure "
    "(tables as rows, lists as lists). Then add a short description of any "
    "diagrams, charts, screenshots or photos (what they show, key labels and values). "
    "Do not invent anything that is not visible. If the image contains no text "
    "and nothing meaningful, reply with exactly: NO_CONTENT"
)


# Embedded images smaller than this (pixels, either side) are almost always
# icons, bullets or spacers — not worth a vision call.
_MIN_EMBEDDED_SIDE = 64
# Cap on vision calls per file so a 500-page scan can't run up a huge bill.
_MAX_EMBEDDED_IMAGES = 50


def _image_to_data_url(data: bytes, min_side: int = 0) -> str | None:
    """Normalise to PNG (first frame for GIFs, downscaled) and return a data URL.

    Returns None if the image is smaller than `min_side` on either side.
    """
    import base64
    import io

    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        img.seek(0)
        if min(img.size) < min_side:
            return None
        img = img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB")
        if max(img.size) > _IMAGE_MAX_SIDE:
            img.thumbnail((_IMAGE_MAX_SIDE, _IMAGE_MAX_SIDE))
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _describe_image(data: bytes, min_side: int = 0) -> str:
    """Send image bytes to the vision model; return extracted text ('' if none)."""
    import os

    from langchain_core.messages import HumanMessage
    from langchain_openai import ChatOpenAI

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required to read images.")

    data_url = _image_to_data_url(data, min_side)
    if data_url is None:
        return ""

    llm = ChatOpenAI(
        model=os.environ.get("OPENAI_VISION_MODEL") or os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        api_key=api_key,
        temperature=0,
    )
    message = HumanMessage(
        content=[
            {"type": "text", "text": _IMAGE_PROMPT},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]
    )
    text = str(llm.invoke([message]).content).strip()
    return "" if text == "NO_CONTENT" else text


def _describe_embedded_image(data: bytes) -> str:
    """Like _describe_image but a bad/unreadable embedded image is skipped, not fatal."""
    try:
        return _describe_image(data, _MIN_EMBEDDED_SIDE)
    except RuntimeError:
        raise  # missing API key: tell the user instead of silently dropping images
    except Exception:  # noqa: BLE001 - corrupt or unsupported image format
        return ""


def load_image_file(path: Path) -> list[Document]:
    text = _describe_image(path.read_bytes())
    if not text:
        return []
    return [Document(page_content=text, metadata={"file_type": path.suffix.lower().lstrip(".")})]


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
    ".html": load_html_file,
    ".htm": load_html_file,
    ".jpg": load_image_file,
    ".jpeg": load_image_file,
    ".png": load_image_file,
    ".gif": load_image_file,
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

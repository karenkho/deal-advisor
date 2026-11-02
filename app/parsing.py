"""PDF / DOCX / TXT / MD -> list of {"page": n, "text": str}."""
from __future__ import annotations

import io
import os
import re

BULLET = re.compile(r"^[\u2022\u25cf\u25cb\u25aa\u2023\u2043\u2219\u00b7\x7f\uf0b7\uf0a7*-]\s*")
PAGE_MARKER = re.compile(r"<!--\s*page\s+(\d+)\s*-->", re.I)
CHARS_PER_PAGE = 3000   # for text without page markers (approximate page equivalent)
_WRAPPED_HYPHEN = re.compile(r"(\w)-[ \t]*\n[ \t]*([a-z])")
_LINE_TOLERANCE = 2.5
_COLUMN_GAP = 12.0       # points: wider than the space between words, even in justified text
_COLUMN_TOLERANCE = 3.0
_EDGE = 1.5              # a table's first and last lines stay out of the text around it
_MIN_SPACED_ROWS = 3     # with text in two or more columns, the header included


def _split_text(text: str) -> list[dict]:
    if PAGE_MARKER.search(text):
        parts = PAGE_MARKER.split(text)
        pages = []
        for i in range(1, len(parts), 2):
            body = parts[i + 1].strip()
            if body:
                pages.append({"page": int(parts[i]), "text": body})
        return pages
    pages, buf = [], ""
    for para in re.split(r"\n\s*\n", text):
        if len(buf) + len(para) > CHARS_PER_PAGE and buf:
            pages.append(buf)
            buf = ""
        buf += para + "\n\n"
    if buf.strip():
        pages.append(buf)
    return [{"page": i + 1, "text": p.strip()} for i, p in enumerate(pages)]


def rejoin_wrapped(text: str) -> str:
    """A word broken at its hyphen by a line wrap is one word again: "e-\nreceipts" reads "e-receipts". A dash set off by spaces, or a capital on the next line, is left alone."""
    return _WRAPPED_HYPHEN.sub(r"\1-\2", text)


def _cell(text: str | None) -> str:
    """A cell on one line. A bullet stays a bullet, whatever glyph the PDF used, and a wrapped line rejoins its bullet."""
    lines = [BULLET.sub("• ", line.strip()) for line in rejoin_wrapped(text or "").splitlines() if line.strip()]
    return " ".join(lines).replace("|", "/")


def _lines_of(page) -> list[dict]:
    """The page's text lines from top to bottom, each cut into segments wherever a gap is wider than any space between words."""
    lines: list[dict] = []
    for word in sorted(page.extract_words(), key=lambda w: (round(w["top"]), w["x0"])):
        if lines and abs(lines[-1]["top"] - word["top"]) <= _LINE_TOLERANCE:
            line = lines[-1]
            if word["x0"] - line["segments"][-1]["x1"] >= _COLUMN_GAP:
                line["segments"].append({"x0": word["x0"], "x1": word["x1"], "text": word["text"]})
            else:
                line["segments"][-1]["x1"], line["segments"][-1]["text"] = word["x1"], line["segments"][-1]["text"] + " " + word["text"]
            line["bottom"] = max(line["bottom"], word["bottom"])
        else:
            lines.append({"top": word["top"], "bottom": word["bottom"], "segments": [{"x0": word["x0"], "x1": word["x1"], "text": word["text"]}]})
    return lines


def _column(x: float, columns: list[float]) -> int | None:
    return next((i for i, c in enumerate(columns) if abs(c - x) <= _COLUMN_TOLERANCE), None)


def _fits(line: dict, columns: list[float]) -> bool:
    """Every segment starts on a column and stops before the next one, so a line of prose running across the columns ends the table."""
    for segment in line["segments"]:
        at = _column(segment["x0"], columns)
        if at is None or (at + 1 < len(columns) and segment["x1"] > columns[at + 1]):
            return False
    return True


def _spaced_tables(page, ruled: list) -> list[tuple[float, float, list[list[str]]]]:
    """Tables laid out with spacing, with no ruled lines: [(top, bottom, rows)]. Held to a high bar, because prose must never read as a table: a header and at least
    two more rows with text in two or more columns on the same x positions. A line starting only in a later column, or only wrapping, belongs to the row above."""
    lines = [line for line in _lines_of(page) if not any(t.bbox[1] - 1 <= line["top"] <= t.bbox[3] + 1 for t in ruled)]
    out, i = [], 0
    while i < len(lines):
        if len(lines[i]["segments"]) < 2:
            i += 1
            continue
        columns = [s["x0"] for s in lines[i]["segments"]]
        rows: list[dict[int, list[str]]] = []
        j = i
        while j < len(lines) and _fits(lines[j], columns):
            cells = {_column(s["x0"], columns): s["text"] for s in lines[j]["segments"]}
            if 0 in cells and len(cells) > 1 or not rows:
                rows.append({k: [v] for k, v in cells.items()})
            else:
                for k, v in cells.items():
                    rows[-1].setdefault(k, []).append(v)
            j += 1
        full = sum(1 for r in rows if len(r) > 1)
        if full >= _MIN_SPACED_ROWS and len(columns) >= 2:
            out.append((lines[i]["top"], lines[j - 1]["bottom"], [["\n".join(r.get(k, [])) for k in range(len(columns))] for r in rows]))
            i = j
        else:
            i += 1
    return out


def _with_tables(data: bytes, pages: list[dict]) -> list[dict]:
    """A page with a table, ruled or laid out with spacing, is read again by a table extractor: plain text would interleave its columns. Its rows arrive as
    "| cell | cell |", in reading order. A page without one keeps the plain text."""
    import pdfplumber
    by_page = {p["page"]: p for p in pages}
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for number, page in enumerate(pdf.pages, 1):
            ruled = [t for t in page.find_tables() if t.rows and len(t.rows[0].cells) >= 2]
            tables = sorted([(t.bbox[1], t.bbox[3], t.extract()) for t in ruled] + _spaced_tables(page, ruled), key=lambda t: t[0])
            if not tables:
                continue
            parts, top = [], 0
            for start, end, rows in tables:
                above = page.crop((0, top, page.width, max(start - _EDGE, top))).extract_text() if start > top else ""
                parts.append(above or "")
                parts += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows if any(row)]
                top = min(end + _EDGE, page.height)
            parts.append(page.crop((0, top, page.width, page.height)).extract_text() or "" if top < page.height else "")
            text = "\n".join(p for p in parts if p).strip()
            if text:
                by_page[number] = {"page": number, "text": text}
    return [by_page[k] for k in sorted(by_page)]


def _docx_cell(cell) -> str:
    paragraphs = [" ".join(p.text.split()) for p in cell.paragraphs if p.text.strip()]
    if len(paragraphs) > 1:     # paragraphs in a cell are its list
        return " ".join("• " + BULLET.sub("", p) for p in paragraphs).replace("|", "/")
    return (paragraphs[0] if paragraphs else "").replace("|", "/")


def _docx_text(doc) -> str:
    """Paragraphs and tables in document order. A table row reads as "| cell | cell |", as in Markdown."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    parts = []
    for block in doc.element.body.iterchildren():
        if block.tag.endswith("}p"):
            parts.append(Paragraph(block, doc).text)
        elif block.tag.endswith("}tbl"):
            rows = []
            for row in Table(block, doc).rows:
                cells = []
                for cell in row.cells:      # a merged cell repeats: keep it once
                    text = _docx_cell(cell)
                    if not cells or text != cells[-1]:
                        cells.append(text)
                rows.append("| " + " | ".join(cells) + " |")
            parts.append("\n".join(rows))
    return "\n\n".join(parts)


def parse_bytes(data: bytes, filename: str) -> list[dict]:
    name = filename.lower()
    if name.endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        pages = [{"page": i + 1, "text": rejoin_wrapped((p.extract_text() or "").strip())}
                 for i, p in enumerate(reader.pages) if (p.extract_text() or "").strip()]
        if os.environ.get("PDF_TABLES", "on") == "off":
            return pages
        return [{**p, "text": rejoin_wrapped(p["text"])} for p in _with_tables(data, pages)]
    if name.endswith(".docx"):
        from docx import Document
        return _split_text(_docx_text(Document(io.BytesIO(data))))
    if name.endswith((".md", ".txt", ".markdown")):
        return _split_text(data.decode("utf-8", errors="replace"))
    raise ValueError("Unsupported file type. Upload PDF, DOCX, TXT or MD.")

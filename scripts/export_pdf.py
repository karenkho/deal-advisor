"""Export sample TOR markdown to PDF, one PDF page per '<!-- page N -->' marker.

Usage: python scripts/export_pdf.py samples/tor_A_clean.md [more.md ...]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from fpdf import FPDF

REPLACE = {"—": "-", "–": "-", "‘": "'", "’": "'", "“": '"', "”": '"', "…": "..."}


def clean(text: str) -> str:
    for a, b in REPLACE.items():
        text = text.replace(a, b)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text.encode("latin-1")  # core PDF fonts are latin-1; fail loudly otherwise
    return text


def split_pages(md: str) -> list[str]:
    parts = re.split(r"<!--\s*page\s+\d+\s*-->", md)
    return [p.strip() for p in parts if p.strip()]


def render(md_path: Path, font_size: float = 10.0) -> Path:
    pages = split_pages(md_path.read_text(encoding="utf-8"))
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_margins(18, 15, 18)
    for number, page in enumerate(pages, start=1):
        pdf.add_page()
        start = pdf.page_no()
        for line in page.splitlines():
            line = clean(line.rstrip())
            if not line:
                pdf.ln(font_size * 0.45)
                continue
            m = re.match(r"^(#{1,3})\s+(.*)$", line)
            if m:
                size = {1: font_size + 6, 2: font_size + 3, 3: font_size + 1}[len(m.group(1))]
                pdf.set_font("Helvetica", "B", size)
                pdf.multi_cell(0, size * 0.55, m.group(2), new_x="LMARGIN", new_y="NEXT")
                continue
            pdf.set_font("Helvetica", "", font_size)
            bullet = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", line)
            if bullet:
                mark = "-" if bullet.group(2) in "-*" else bullet.group(2)
                pdf.set_x(pdf.l_margin + 4)
                pdf.multi_cell(0, font_size * 0.5, f"{mark} {bullet.group(3)}", new_x="LMARGIN", new_y="NEXT")
            else:
                pdf.multi_cell(0, font_size * 0.5, line, new_x="LMARGIN", new_y="NEXT")
        if pdf.page_no() != start:
            if font_size <= 7:
                raise ValueError(f"page {number} of {md_path.name} does not fit on one PDF page")
            return render(md_path, font_size - 0.5)  # page overflowed: shrink and retry
        pdf.set_auto_page_break(auto=False)
        pdf.set_y(-12)
        pdf.set_font("Helvetica", "I", 8)
        pdf.cell(0, 6, f"Page {number} of {len(pages)}", align="C")
        pdf.set_auto_page_break(auto=True, margin=15)
    out = md_path.with_suffix(".pdf")
    pdf.output(str(out))
    print(f"{out.name}: {len(pages)} pages at {font_size}pt")
    return out


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        render(Path(arg))

"""Leak guardrail (POC v2): scans a client-facing DOCX before download for internal data, by pattern and by the exact
values in the record and the active rate table. A hit blocks the download. Logs say which section and kind, never the
value. Applies to the draft proposal and the draft BRD."""
from __future__ import annotations

import io
import re

from app.config import estimation_config
from app.costing import RateTable, blended_rates, illustrative_rates
from app.estimation import configs
from app.logging_setup import log_event
from app.record import OpportunityRecord

WORDS = re.compile(r"\b(?:gross margin|margin|mark-?up|contingency|cost rates?|published rates?|applied rates?|"
                   r"blended rates?|tier hours|delivery overhead|ai factors?|productivity factors?|productivity profile|"
                   r"price floor|discount room|per hour|php\s*/\s*h(?:ou)?r)\b", re.I)
HOURS = re.compile(r"\b\d[\d,]*(?:\.\d+)?\s*(?:man-?hours|person-?hours|hours|hrs)\b", re.I)
NUMBER = re.compile(r"(?<![\w.-])\d[\d,]*(?:\.\d+)?%?(?![\w-])")


def _norm(token: str) -> str:
    token = token.replace(",", "").rstrip(".")
    pct = token.endswith("%")
    token = token.rstrip("%")
    if "." in token:
        token = token.rstrip("0").rstrip(".")
    return token + ("%" if pct else "")


def _forms(value: float) -> set[str]:
    return {_norm(f"{value:.0f}"), _norm(f"{value:.2f}"), _norm(f"{value:g}")}


def _numbers(text: str) -> set[str]:
    return {_norm(t) for t in NUMBER.findall(text)}


def source_numbers(rec: OpportunityRecord) -> set[str]:
    """Numbers the TOR itself states (quotes and statements): an hours or percent value may coincide with them, a rate
    or cost may not."""
    brd = rec.brd
    items = brd.rules + brd.entities + brd.nfrs + brd.constraints + brd.assumptions + brd.questions
    text = " ".join([c.statement + " " + " ".join(c.context + [q.quote for q in c.citations])
                     for c in brd.requirements] + [i.text + " " + " ".join(i.attributes) for i in items])
    return _numbers(text)


def blocked_values(rec: OpportunityRecord, rates: RateTable | None = None, discount_pct: float = 0.0,
                   allowed: set[str] | None = None) -> dict[str, set[str]]:
    """kind -> normalized values that must not appear. allowed: client-safe numbers of this document."""
    out: dict[str, set[str]] = {"rate": set(), "cost": set(), "percent": set(), "factor": set(), "hours": set()}
    tables = [illustrative_rates()] + ([rates] if rates else [])
    for t in tables:
        for v in list(t.published.values()) + list(t.applied(discount_pct).values()):
            out["rate"] |= _forms(v)
        for b in blended_rates(t, discount_pct).values():
            out["rate"] |= _forms(b["published"]) | _forms(b["applied"])
    approved = rec.approval.scenario
    for s, c in rec.costing.scenarios.items():
        keys = ["delivery_cost", "labor", "ai_tooling", "infrastructure", "warranty_reserve", "contingency", "price_floor"]
        if s != approved:     # another scenario's price is internal too
            keys += ["price_ex_vat", "vat", "price_incl_vat"]
        for k in keys + ["labor_saved", "net_savings"]:
            if c.get(k):
                out["cost"] |= _forms(c[k])
        for k, v in c.get("ai_tooling_lines", {}).items():
            if v and k != "million_tokens":
                out["cost"] |= _forms(v)
        for k in ("gross_margin_pct", "markup_pct", "target_gross_margin_pct", "min_gross_margin_pct", "discount_room_pct"):
            if c.get(k):
                out["percent"] |= {_norm(f"{c[k]:.0f}%"), _norm(f"{c[k]:.1f}%")}
        if c.get("contingency_rate"):
            out["percent"].add(_norm(f"{c['contingency_rate'] * 100:.0f}%"))
    cfg = configs(rec.assumption_overrides)
    for profile in cfg["ai_scenarios"]["profiles"].values():
        for cat in profile["factors"].values():
            for scenario in cat.values():
                out["factor"] |= {_norm(f"{v:.2f}") for v in scenario.values() if 0 < v < 1}
    est_cfg = estimation_config()
    for roles in est_cfg["size_hours"].values():
        out["hours"] |= {_forms(float(h)).pop() for h in roles.values() if h >= 100}
    for roles in est_cfg["foundation"]["items"].values():
        out["hours"] |= {_norm(f"{float(h):.0f}") for h in roles.values() if h >= 100}
    for sc in rec.estimates.get("scenarios", {}).values():
        out["hours"] |= {_norm(f"{h:.0f}") for h in sc["by_role"].values() if h >= 100}
        out["hours"].add(_norm(f"{sc['total_hours']:.0f}"))
    out["cost"] = {v for v in out["cost"] if float(v) >= 100}
    out["rate"] = {v for v in out["rate"] if float(v) >= 100}
    safe = (allowed or set()) | source_numbers(rec)
    out["hours"] -= safe
    out["cost"] -= allowed or set()
    out["percent"] -= safe                  # a percentage the TOR states is not internal (evaluation weights, uptime)
    out["factor"] -= allowed or set()
    return out


def docx_sections(data: bytes) -> list[tuple[str, str]]:
    """(section heading, text including tables) in document order, from the real file, plus headers and footers."""
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(io.BytesIO(data))
    sections: list[tuple[str, list[str]]] = [("(front matter)", [])]
    for child in doc.element.body.iterchildren():
        if child.tag.endswith("}p"):
            p = Paragraph(child, doc)
            if p.style.name == "Heading 1":
                sections.append((p.text, []))
            elif p.text.strip():
                sections[-1][1].append(p.text)
        elif child.tag.endswith("}tbl"):
            for row in Table(child, doc).rows:
                sections[-1][1].append(" | ".join(c.text for c in row.cells))
    furniture = [p.text for s in doc.sections for part in (s.header, s.footer) for p in part.paragraphs if p.text.strip()]
    return [("(header and footer)", furniture)] + [(h, lines) for h, lines in sections] if furniture else \
        [(h, lines) for h, lines in sections]


def scan(data: bytes, blocked: dict[str, set[str]], document: str = "proposal") -> list[dict]:
    """[{section, kind}] for every hit; empty means the document may be downloaded."""
    hits = []
    for section, lines in docx_sections(data):
        text = "\n".join(lines) if isinstance(lines, list) else lines
        kinds = set()
        if WORDS.search(text):
            kinds.add("internal term")
        if HOURS.search(text) and (blocked["hours"] & _numbers(text)):
            kinds.add("hours")
        numbers = _numbers(text)
        for kind in ("rate", "cost", "percent", "factor", "hours"):
            if blocked[kind] & numbers:
                kinds.add(kind)
        for kind in sorted(kinds):
            hits.append({"section": section, "kind": kind})
    if hits:
        log_event("leak guardrail blocked", node="leak_guardrail", document=document,
                  hits=[f"{h['section']}: {h['kind']}" for h in hits])
    return hits

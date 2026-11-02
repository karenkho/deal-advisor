"""Compare two BRD versions (POC v3 6b), so a user can see that a change touched only what it should. Everything is read from the
version snapshots; the fingerprints decide what changed, and the words are compared only to show how."""
from __future__ import annotations

import difflib
import html
import io
import re

from docx import Document
from docx.shared import Pt, RGBColor
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models, store

STATUSES = ("Unchanged", "Modified", "Added", "Removed", "Split", "Merged")
_LIFECYCLE = {"Removed": "Removed", "Split": "Split", "Merged": "Merged"}
_TOKEN = re.compile(r"\s+|[^\s]+")
_SECTION_LABEL = {"objective": "Objective", "in_scope": "In scope", "out_of_scope": "Out of scope", "risk": "Risk"}


def word_diff(old: str, new: str) -> list[tuple[str, str]]:
    """[(kind, text)] with kind same, removed or added, word by word."""
    a, b = _TOKEN.findall(old or ""), _TOKEN.findall(new or "")
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            out.append(("same", "".join(a[i1:i2])))
        else:
            if i2 > i1:
                out.append(("removed", "".join(a[i1:i2])))
            if j2 > j1:
                out.append(("added", "".join(b[j1:j2])))
    return out


def _versions(session: Session, opp_id: str) -> list[models.ArtifactVersion]:
    return session.scalars(select(models.ArtifactVersion).where(models.ArtifactVersion.opportunity_id == opp_id, models.ArtifactVersion.artifact_type == "BRD")
                           .order_by(models.ArtifactVersion.version_major, models.ArtifactVersion.version_minor)).all()


def _causes(versions: list[models.ArtifactVersion], older, newer) -> dict:
    """The latest cause of each change between the two versions."""
    causes: dict = {}
    for v in versions:
        if (older.version_major, older.version_minor) < (v.version_major, v.version_minor) <= (newer.version_major, newer.version_minor):
            causes |= v.snapshot.get("causes", {})
    return causes


def _br_text(b: dict | None) -> str:
    return "" if b is None else f"{b['title']}: {b['statement']}"


def _item_texts(snapshot: dict) -> dict[str, tuple[str, str]]:
    """item id -> (kind, text)."""
    out = {}
    for i in snapshot["section_items"]:
        if i["section"] in store.FINGERPRINTED_SECTIONS:
            out[i["item_id"]] = (_SECTION_LABEL[i["section"]], " · ".join(str(v) for v in i["data"].values() if v))
    for q in snapshot["open_questions"]:
        out[q["item_id"]] = ("Open question", q["text"] + (f" [Answered: {q['answer_source']}]" if q["answer_source"] else ""))
    for r in snapshot["business_rules"]:
        out[r["item_id"]] = ("Business rule", r["text"])
    return out


def compare(session: Session, opp_id: str, older_label: str, newer_label: str) -> dict:
    versions = _versions(session, opp_id)
    by_label = {v.version_label: v for v in versions}
    older, newer = by_label[older_label], by_label[newer_label]
    if (older.version_major, older.version_minor) > (newer.version_major, newer.version_minor):
        older, newer = newer, older
    causes = _causes(versions, older, newer)
    a, b = older.snapshot, newer.snapshot
    fa, fb = a.get("fingerprints") or store.fingerprints(a), b.get("fingerprints") or store.fingerprints(b)
    brs_a, brs_b = {x["br_id"]: x for x in a["brs"]}, {x["br_id"]: x for x in b["brs"]}
    rows, summary = [], dict.fromkeys(STATUSES, 0)
    for br_id in sorted(set(brs_a) | set(brs_b)):
        old, new = brs_a.get(br_id), brs_b.get(br_id)
        was_active = old is not None and old["lifecycle_status"] == "Active"
        is_active = new is not None and new["lifecycle_status"] == "Active"
        if not was_active and not is_active:
            continue        # retired before this comparison
        if not was_active:
            status = "Added"
        elif not is_active:
            status = _LIFECYCLE[new["lifecycle_status"]]
        else:
            status = "Unchanged" if fa["brs"][br_id]["hash"] == fb["brs"][br_id]["hash"] else "Modified"
        summary[status] += 1
        cause = causes.get(br_id, {}) if status != "Unchanged" else {}
        rows.append({"id": br_id, "kind": "BR", "status": status, "old": _br_text(old), "new": _br_text(new), "words": word_diff(_br_text(old), _br_text(new)) if status != "Unchanged" else [("same", _br_text(new))],
                     "review_before": fa["brs"].get(br_id, {}).get("review"), "review_after": fb["brs"].get(br_id, {}).get("review"), "cause": cause.get("label"), "change_request_id": cause.get("change_request_id")})
    items = []
    texts_a, texts_b = _item_texts(a), _item_texts(b)
    withdrawn = {w["item_id"] for w in b.get("withdrawn", [])}
    for item_id in sorted(set(texts_a) | set(texts_b)):
        ha, hb = fa["items"].get(item_id), fb["items"].get(item_id)
        status = "Added" if ha is None else ("Withdrawn" if item_id in withdrawn else "Removed") if hb is None else "Unchanged" if ha == hb else "Modified"
        old, new = texts_a.get(item_id, ("", ""))[1], texts_b.get(item_id, ("", ""))[1]
        cause = causes.get(item_id, {}) if status != "Unchanged" else {}
        items.append({"id": item_id, "kind": (texts_b.get(item_id) or texts_a[item_id])[0], "status": status, "old": old, "new": new, "words": word_diff(old, new) if status != "Unchanged" else [("same", new)],
                      "review_before": None, "review_after": None, "cause": cause.get("label"), "change_request_id": cause.get("change_request_id")})
    total = sum(summary.values())
    summary["percent_unchanged"] = 100.0 * summary["Unchanged"] / total if total else 100.0
    changed_items = sum(1 for i in items if i["status"] != "Unchanged")
    return {"older": older.version_label, "newer": newer.version_label, "summary": summary, "rows": rows, "items": items, "changes": total - summary["Unchanged"] + changed_items,
            "stability": newer.snapshot.get("stability")}


def _words_html(words: list[tuple[str, str]]) -> str:
    parts = []
    for kind, text in words:
        safe = html.escape(text)
        parts.append(safe if kind == "same" else f'<del style="background:rgba(220,60,60,.2)">{safe}</del>' if kind == "removed" else f'<ins style="background:rgba(40,160,80,.25);text-decoration:none">{safe}</ins>')
    return "".join(parts)


def html_table(rows: list[dict], title: str) -> str:
    """Old and new text with word-level highlighting. All document text is escaped: it comes from uploaded files."""
    if not rows:
        return f"<p><b>{html.escape(title)}</b>: nothing to show.</p>"
    body = "".join(
        f"<tr><td>{html.escape(r['id'])}</td><td><b>{r['status']}</b></td><td>{_words_html(r['words'])}</td>"
        f"<td>{html.escape(str(r['review_before'] or '—'))} → {html.escape(str(r['review_after'] or '—'))}</td>"
        f"<td>{html.escape(r['cause'] or '')}{f' (CR-{r_id})' if (r_id := r['change_request_id']) else ''}</td></tr>" for r in rows)
    return (f"<p><b>{html.escape(title)}</b></p><table style='width:100%'><thead><tr><th>ID</th><th>Status</th><th>Old → new</th><th>Review before → after</th><th>Cause</th></tr></thead><tbody>{body}</tbody></table>")


def report_docx(session: Session, opp_id: str, result: dict) -> bytes:
    """The change report: internal, and free of hours, costs and rates like the BRD itself."""
    opp = session.get(models.Opportunity, opp_id)
    doc = Document()
    doc.styles["Normal"].font.size = Pt(10)
    doc.sections[0].header.paragraphs[0].add_run(f"INTERNAL · BRD comparison · {opp.code}").bold = True
    doc.add_heading(f"BRD comparison: v{result['older']} to v{result['newer']}", 0)
    doc.add_paragraph(f"{opp.name} · {opp.client_name}")
    s = result["summary"]
    doc.add_paragraph(", ".join(f"{name}: {s[name]}" for name in STATUSES) + f". Unchanged: {s['percent_unchanged']:.0f}%.")
    if result.get("stability"):
        doc.add_paragraph(result["stability"]["message"] + ".")
    for title, rows in (("Requirements", result["rows"]), ("Objectives, scope, questions, rules and risks", result["items"])):
        changed = [r for r in rows if r["status"] != "Unchanged"]
        doc.add_heading(title, 1)
        if not changed:
            doc.add_paragraph("No changes.")
            continue
        table = doc.add_table(rows=1, cols=4)
        table.style = "Table Grid"
        for cell, head in zip(table.rows[0].cells, ("ID", "Status", "Old → new", "Cause")):
            cell.text = ""
            cell.paragraphs[0].add_run(head).bold = True
        for r in changed:
            cells = table.add_row().cells
            cells[0].text, cells[1].text, cells[3].text = r["id"], r["status"], r["cause"] or ""
            paragraph = cells[2].paragraphs[0]
            for kind, text in r["words"]:
                run = paragraph.add_run(text)
                if kind == "removed":
                    run.font.strike = True
                    run.font.color.rgb = RGBColor(0xB0, 0x30, 0x30)
                elif kind == "added":
                    run.bold = True
                    run.font.color.rgb = RGBColor(0x1E, 0x7A, 0x3C)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()

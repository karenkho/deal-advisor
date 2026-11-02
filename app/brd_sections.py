"""The Appendix A sections the extraction does not produce, filled by rule from the TOR text and the requirements. No LLM.
Nothing is invented: a value the TOR does not state reads "Not stated in TOR" and raises an open question."""
from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import ai_extract, models, statements, store
from app.statements import norm

NOT_STATED = "Not stated in TOR"
NOT_DESCRIBED = "Not described in TOR"
TO_CONFIRM = "To be confirmed"
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")
_PAIN = re.compile(r"\b(?:causes?|late|dispute|inconsisten|duplicat|manual|limited|delay|error)", re.I)
_REPORTING = re.compile(r"\b(?:report|dashboard|analytics)", re.I)
_DATA_CAPTURE = re.compile(r"\b(?:master record|capture|import|migration|maintenance)\b", re.I)
_NFR_CATEGORIES = (("Availability", r"availab|uptime|backup|recovery"), ("Performance", r"performance|response time|concurrent|load"),
                   ("Data Privacy", r"privacy|personal data|data subject"), ("Audit/Compliance", r"audit|compliance|retention|retained"),
                   ("Security", r"secur|encrypt|password|authenticat|access"), ("Scalability", r"scal|growth|expand"))
_NFR_METRIC = re.compile(r"\d[\d,.]*\s*(?:%|percent|seconds?|minutes?|hours?|days?|years?|users?|concurrent)?", re.I)


def tor_sections(pages: list[dict]) -> list[tuple[str, str, list[str]]]:
    """[(number, title, lines)] of the masked pages, read the same way the statements are."""
    return statements.sections(pages)


def _find(sections: list, pattern: str) -> tuple[str, str, list[str]] | None:
    return next((s for s in sections if s[2] and re.search(pattern, s[1], re.I)), None)


def _text(section: tuple | None, max_words: int = 90) -> str:
    if section is None:
        return ""
    words = norm(" ".join(section[2])).split()
    return " ".join(words[:max_words]) + (" ..." if len(words) > max_words else "")


def _list_items(section: tuple | None) -> list[str]:
    """The numbered items of a section, with wrapped lines joined."""
    items: list[str] = []
    for line in section[2] if section else []:
        if statements.is_item(line):
            items.append(statements.item_text(line))
        elif items:
            items[-1] += " " + line
    return [norm(i) for i in items]


def _add(session: Session, opp_id: str, section: str, prefix: str | None, data: dict, order: int) -> None:
    item_id = store.allocate_id(session, opp_id, prefix) if prefix else f"{section}-{order}"
    session.add(models.BrdSectionItem(opportunity_id=opp_id, section=section, item_id=item_id, sort_order=order, data=data))


def _add_question(session: Session, opp_id: str, text: str) -> None:
    order = 1 + session.scalar(select(func.count()).select_from(models.OpenQuestion).where(models.OpenQuestion.opportunity_id == opp_id))
    session.add(models.OpenQuestion(opportunity_id=opp_id, item_id=store.allocate_id(session, opp_id, "OQ"), sort_order=order, text=text,
                                    source="brd_section", blocking=False, audience="internal"))


def generate(session: Session, opp_id: str, documents: list | None = None, routed: dict | None = None) -> None:
    """Fills the sections once. Safe to run again: a section that already has rows is left alone, with any edits. routed: what AI extraction found for them."""
    routed = routed or {}
    documents = documents if documents is not None else session.scalars(select(models.Document).where(models.Document.opportunity_id == opp_id)).all()
    pages = [p for d in documents if d.doc_type in ("TOR", "RFP") for p in d.masked_pages]
    sections = tor_sections(pages)
    present = set(session.scalars(select(models.BrdSectionItem.section).where(models.BrdSectionItem.opportunity_id == opp_id)))
    brs = session.scalars(select(models.Br).where(models.Br.opportunity_id == opp_id, models.Br.lifecycle_status == "Active")
                          .order_by(models.Br.epic_id, models.Br.sort_order)).all()
    epics = session.scalars(select(models.Epic).where(models.Epic.opportunity_id == opp_id).order_by(models.Epic.sort_order)).all()
    background, situation = _find(sections, r"about the|background"), _find(sections, r"current (?:situation|state)|problem")
    purpose, objectives = _find(sections, r"purpose"), _find(sections, r"objective")
    items = routed.get("objectives") or _list_items(objectives) or [x for x in _SENTENCE.split(_text(objectives, 200)) if x][:6]     # objectives written as prose

    if "executive_summary" not in present:
        for order, (label, value) in enumerate((("Business Background", " ".join(routed.get("background", [])) or _text(background)), ("Problem Statement", _text(situation)),
                                                ("Proposed Initiative", " ".join(routed.get("scope", [])) or _text(purpose)), ("Expected Outcomes", " ".join(items) or _text(objectives))), 1):
            _add(session, opp_id, "executive_summary", None, {"section": label, "details": value or NOT_STATED}, order)
    if "objective" not in present:
        for order, objective in enumerate(items, 1):
            _add(session, opp_id, "objective", "BO", {"objective": objective, "kpi": NOT_STATED, "baseline": NOT_STATED, "target": NOT_STATED,
                                                      "measurement": NOT_STATED}, order)
        _add_question(session, opp_id, "The TOR states no KPIs, baselines or targets for its objectives. What are they?"
                      if items else "The TOR states no business objectives. What are they?")
    if "in_scope" not in present:
        for order, epic in enumerate(epics, 1):
            titles = [b.title for b in brs if b.epic_id == epic.epic_id and b.scope_status == "in_scope"]
            if titles:
                _add(session, opp_id, "in_scope", "IS", {"capability": epic.name, "description": "; ".join(titles)}, order)
    if "process_as_is" not in present:
        text = _text(situation, 200)
        pain = [s for s in _SENTENCE.split(text) if _PAIN.search(s)]
        _add(session, opp_id, "process_as_is", None, {"process": "Current process" if text else NOT_DESCRIBED, "description": text or NOT_DESCRIBED,
                                                      "pain_points": " ".join(pain) or NOT_STATED}, 1)
    if "process_to_be" not in present:
        target = _find(sections, r"future state|to-be|proposed process")
        _add(session, opp_id, "process_to_be", None, {"process": "Future process" if target else NOT_DESCRIBED, "description": _text(target) or NOT_DESCRIBED,
                                                      "improvement": NOT_STATED}, 1)
    if "data_reporting" not in present:
        rows = []
        for b in brs:
            kind = b.ext.get("feature", {}).get("type", "")
            area = ("Integration" if kind == "integration" else "Reporting" if _REPORTING.search(b.title)
                    else "Data Capture" if _DATA_CAPTURE.search(b.title) else None)
            if area:
                rows.append({"area": area, "requirement": f"{b.title} ({b.br_id})", "frequency": NOT_STATED, "owner": TO_CONFIRM})
        for order, row in enumerate(rows, 1):
            _add(session, opp_id, "data_reporting", None, row, order)
    if "risk" not in present:
        _add_risks(session, opp_id, brs, routed)
    for nfr in session.scalars(select(models.Nfr).where(models.Nfr.opportunity_id == opp_id, models.Nfr.category.is_(None))):
        nfr.category = next((name for name, rx in _NFR_CATEGORIES if re.search(rx, nfr.text, re.I)), "Other")
        metric = _NFR_METRIC.search(nfr.text)
        nfr.metric_target = metric.group(0).strip() if metric else NOT_STATED
    session.flush()


def _add_risks(session: Session, opp_id: str, brs: list, routed: dict | None = None) -> None:
    """Requirement and delivery risks only: cost, rate and margin risks stay in the internal estimate."""
    risks = []
    weeks = ai_extract.requested_weeks((routed or {}).get("timeline", []))
    modules = {b.epic_id for b in brs if b.scope_status == "in_scope"}
    if weeks and ai_extract.timeline_is_short(weeks, len(modules)):
        risks.append((f"The TOR asks for {weeks} weeks for {len(modules)} modules. That is unusually short.", "High", "High", "Raise it as a clarification, and check it against the estimate's timeline before committing."))
    blocking = session.scalars(select(models.OpenQuestion).where(models.OpenQuestion.opportunity_id == opp_id, models.OpenQuestion.blocking)).all()
    for q in blocking:
        risks.append((f"The documents conflict: {q.text}", "High", "High", "Get a written clarification from the client before approving the BRD."))
    inferred = [b.br_id for b in brs if b.scope_status == "suggested"]
    if inferred:
        risks.append((f"Scope inferred, not stated in the documents: {', '.join(inferred)}.", "Medium", "Medium", "Confirm with the client; not priced until accepted."))
    unmeasurable = session.scalar(select(func.count()).select_from(models.Nfr).where(models.Nfr.opportunity_id == opp_id, models.Nfr.measurable.is_(False)))
    if unmeasurable:
        risks.append((f"{unmeasurable} non-functional expectations have no measurable target.", "Medium", "Medium", "Agree measurable targets with the client."))
    for order, (text, probability, impact, mitigation) in enumerate(risks, 1):
        _add(session, opp_id, "risk", "R", {"description": text, "probability": probability, "impact": impact, "mitigation": mitigation, "owner": "Solutions Specialist"}, order)

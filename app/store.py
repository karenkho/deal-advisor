"""Write helpers on the database: optimistic locking, stable ID allocation and the audit trail."""
from __future__ import annotations

import hashlib
import json
import re

from sqlalchemy import select, text
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app import models
from app.models import AuditLog
from app.statements import norm

STALE_MESSAGE = "Someone else updated this; reload."
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_RATE_KEY = re.compile(r"(?i)(^|_)(rate|rates|salary|email|phone)($|_)(?!source|hash)")


class StaleWrite(Exception):
    """The row changed since this user read it."""

    def __init__(self) -> None:
        super().__init__(STALE_MESSAGE)


def text_hash(text: str) -> str:
    return hashlib.sha256(norm(text).lower().encode()).hexdigest()


def update_checked(session: Session, model, key, expected_row_version: int, **changes):
    """Applies changes only if the row is still at the row_version the user read. Returns the row, flushed."""
    row = session.get(model, key, populate_existing=True)
    if row is None or row.row_version != expected_row_version:
        raise StaleWrite
    for name, value in changes.items():
        setattr(row, name, value)
    try:
        session.flush()
    except StaleDataError as e:
        raise StaleWrite from e
    return row


def allocate_id(session: Session, opportunity_id: str, prefix: str, width: int = 3) -> str:
    """The next unused number for this prefix, e.g. BR-031. Safe between concurrent sessions; numbers are never reused."""
    n = session.execute(text(
        "INSERT INTO id_counters (opportunity_id, prefix, last_value) VALUES (:o, :p, 1) "
        "ON CONFLICT (opportunity_id, prefix) DO UPDATE SET last_value = id_counters.last_value + 1 RETURNING last_value"),
        {"o": opportunity_id, "p": prefix}).scalar_one()
    return f"{prefix}-{n:0{width}d}"


def _check_details(value, path: str = "details") -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if _RATE_KEY.search(str(k)):
                raise ValueError(f"Audit details must not hold {k!r} ({path}).")
            _check_details(v, f"{path}.{k}")
    elif isinstance(value, (list, tuple)):
        for v in value:
            _check_details(v, path)
    elif isinstance(value, str) and _EMAIL.search(value):
        raise ValueError(f"Audit details must not hold an email address ({path}).")


def advisory_lock(session: Session, key: str) -> None:
    """Held until this transaction ends: whoever checks next sees what this one wrote."""
    session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key})


def audit(session: Session, actor_name: str, actor_role: str, action: str, *, opportunity_id: str | None = None,
          entity_type: str | None = None, entity_id: str | None = None, version: str | None = None,
          details: dict | None = None) -> None:
    """One audit entry in the caller's transaction, so an action and its entry commit or fail together."""
    _check_details(details or {})
    session.add(AuditLog(actor_name=actor_name, actor_role=actor_role, action=action, opportunity_id=opportunity_id,
                         entity_type=entity_type, entity_id=entity_id, version=version, details=details or {}))


def _row(row, skip: tuple = ("id", "opportunity_id", "row_version", "created_at", "updated_at")) -> dict:
    return {c.name: getattr(row, c.key) for c in row.__table__.columns if c.name not in skip}


_BR_CONTENT = ("statement", "title", "priority", "complexity", "epic_id", "rationale", "source_citations", "scope_status")
FINGERPRINTED_SECTIONS = ("objective", "in_scope", "out_of_scope", "risk")


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


def fingerprints(snapshot: dict) -> dict:
    """Per-BR and per-item content hashes, so two versions can be proved identical or not. Review status sits beside the hash, not
    in it: reviewing a BR does not change it. Nor does acknowledging a question."""
    reviews = snapshot.get("reviews", {})
    brs = {b["br_id"]: {"hash": _hash({k: b[k] for k in _BR_CONTENT}), "lifecycle": b["lifecycle_status"], "review": reviews.get(b["br_id"])} for b in snapshot["brs"]}
    items = {i["item_id"]: _hash(i["data"]) for i in snapshot["section_items"] if i["section"] in FINGERPRINTED_SECTIONS}
    items |= {q["item_id"]: _hash({k: q[k] for k in ("text", "br_ids", "answer_source")}) for q in snapshot["open_questions"]}
    items |= {r["item_id"]: _hash({k: r[k] for k in ("text", "br_ids", "superseded_by")}) for r in snapshot["business_rules"]}
    return {"brs": brs, "items": items}


ITEM_TEXT = {"objective": "objective", "in_scope": "capability", "out_of_scope": "exclusion", "risk": "description"}     # the field that says what a row is


def withdrawn_items(session: Session, opp_id: str) -> list[dict]:
    """What was withdrawn, kept beside the BRD and outside it: no document, estimate or later version reads these."""
    out = [{"item_id": q.item_id, "section": "open_question", "text": q.text, **q.withdrawn}
           for q in session.scalars(select(models.OpenQuestion).where(models.OpenQuestion.opportunity_id == opp_id, models.OpenQuestion.withdrawn.is_not(None)))]
    out += [{"item_id": i.item_id, "section": i.section, "text": str(i.data.get(ITEM_TEXT.get(i.section, ""), "")), **i.withdrawn}
            for i in session.scalars(select(models.BrdSectionItem).where(models.BrdSectionItem.opportunity_id == opp_id, models.BrdSectionItem.withdrawn.is_not(None)))]
    return sorted(out, key=lambda x: x["item_id"])


def brd_snapshot(session: Session, opp_id: str) -> dict:
    """The authoritative BRD content as stored now: requirement content only, never hours, costs or prices."""
    def rows(model, *order):
        return [_row(r) for r in session.scalars(select(model).where(model.opportunity_id == opp_id).order_by(*order))]

    snapshot = {"epics": rows(models.Epic, models.Epic.sort_order), "brs": rows(models.Br, models.Br.epic_id, models.Br.sort_order, models.Br.br_id),
                "business_rules": rows(models.BusinessRule, models.BusinessRule.sort_order), "entities": rows(models.Entity, models.Entity.sort_order),
                "nfrs": rows(models.Nfr, models.Nfr.sort_order), "constraints_assumptions": rows(models.ConstraintAssumption, models.ConstraintAssumption.sort_order),
                "open_questions": [r for r in rows(models.OpenQuestion, models.OpenQuestion.sort_order) if not r["withdrawn"]],
                "section_items": [r for r in rows(models.BrdSectionItem, models.BrdSectionItem.section, models.BrdSectionItem.sort_order) if not r["withdrawn"]],
                "withdrawn": withdrawn_items(session, opp_id),
                "reviews": {r.br_id: r.status for r in session.scalars(select(models.BrReview).where(models.BrReview.opportunity_id == opp_id))}}
    snapshot["fingerprints"] = fingerprints(snapshot)
    return snapshot

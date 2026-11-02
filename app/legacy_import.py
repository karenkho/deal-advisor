"""Moves a v2 opportunity record into the database. All-or-nothing, in the caller's transaction, no LLM.

v2 approvals had no roles and estimates must now come from an approved BRD, so an imported opportunity starts at
BRD Generated with draft v0.1 and goes through the validation workflow. Hours, costs and prices are not imported;
the calculators rebuild them at the estimate step."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import brd_sections, models, store
from app.record import OpportunityRecord

ACTOR = ("Deal Advisor migration", "System")
DOC_TYPES = {"tor": "TOR", "bid_bulletin": "Bid bulletin", "minutes": "Minutes", "clarification": "Clarification", "other": "Other"}
PENDING_PRIORITY = "Should (pending client confirmation)"
WONT_PRIORITY = "Won't"


class AlreadyImported(Exception):
    pass


def normalize_priorities(session: Session, opp_id: str) -> dict:
    """Appendix A allows Must, Should and Could. Pending confirmation becomes Should plus a non-blocking open question;
    Won't becomes an Out of Scope row and retires the BR (kept in history, its ID never reused). Safe to run again."""
    done = {"confirmations": 0, "retired": 0}
    questions = session.scalar(select(func.count()).select_from(models.OpenQuestion).where(models.OpenQuestion.opportunity_id == opp_id))
    brs = session.scalars(select(models.Br).where(models.Br.opportunity_id == opp_id, models.Br.lifecycle_status == "Active",
                                                  models.Br.priority.in_([PENDING_PRIORITY, WONT_PRIORITY])).order_by(models.Br.br_id)).all()
    for br in brs:
        if br.priority == PENDING_PRIORITY:
            br.priority = "Should"
            br.ext = {**br.ext, "feature": {**br.ext.get("feature", {}), "priority": "Should"}}
            questions += 1
            session.add(models.OpenQuestion(
                opportunity_id=opp_id, item_id=store.allocate_id(session, opp_id, "OQ"), sort_order=questions, br_ids=[br.br_id],
                text=f"Client to confirm: is {br.title} ({br.br_id}) required? It was inferred, not stated in the documents.",
                source="client_confirmation", blocking=False))
            done["confirmations"] += 1
        else:
            br.lifecycle_status = "Removed"
            session.add(models.BrdSectionItem(
                opportunity_id=opp_id, section="out_of_scope", item_id=store.allocate_id(session, opp_id, "OOS"), sort_order=_next_order(session, opp_id, "out_of_scope"),
                data={"exclusion": br.title, "rationale": br.rationale or "Marked Won't in the requirements.", "retired_br": br.br_id}))
            session.flush()
            done["retired"] += 1
        store.audit(session, *ACTOR, "br.priority_normalized", opportunity_id=opp_id, entity_type="br", entity_id=br.br_id,
                    details={"priority": br.priority, "lifecycle_status": br.lifecycle_status})
    session.flush()
    return done


def _next_order(session: Session, opp_id: str, section: str) -> int:
    return 1 + session.scalar(select(func.coalesce(func.max(models.BrdSectionItem.sort_order), 0)).where(
        models.BrdSectionItem.opportunity_id == opp_id, models.BrdSectionItem.section == section))


def import_exclusions(session: Session, opp_id: str, exclusions: list[dict]) -> int:
    """The TOR's stated exclusions become Out of Scope rows."""
    for x in exclusions:
        session.add(models.BrdSectionItem(
            opportunity_id=opp_id, section="out_of_scope", item_id=store.allocate_id(session, opp_id, "OOS"), sort_order=_next_order(session, opp_id, "out_of_scope"),
            data={"exclusion": x["text"], "rationale": x.get("source", "Stated in the TOR."), "pages": x.get("pages", [])}))
        session.flush()
    return len(exclusions)


def _set_counter(session: Session, opp_id: str, prefix: str, value: int) -> None:
    if value:
        session.add(models.IdCounter(opportunity_id=opp_id, prefix=prefix, last_value=value))


def renumber_questions(text: str, question_map: dict[str, str]) -> str:
    """v2 text refers to questions by their old IDs: "details to be confirmed: Q-01" must follow the renumbering."""
    return re.sub(r"\bQ-\d+\b", lambda m: question_map.get(m.group(0), m.group(0)), text)


def _item_fields(opp_id: str, item, item_id: str, order: int, br_map: dict[str, str], question_map: dict[str, str]) -> dict:
    return {"opportunity_id": opp_id, "item_id": item_id, "sort_order": order, "text": renumber_questions(item.text, question_map),
            "description": item.description or None, "br_ids": [br_map[b] for b in item.br_ids if b in br_map],
            "statement_id": item.statement_id or None, "citations": [c.model_dump() for c in item.citations],
            "superseded_by": item.superseded_by or None, "unsupported": item.unsupported, "legacy_id": item.id}


def import_record(session: Session, rec: OpportunityRecord, documents: list[dict], decisions: list[dict] | None = None,
                  runs: list[dict] | None = None, code: str | None = None) -> dict:
    """A v2 record that was never in the database. documents: {"key", "name", "kind", "number", "date", "sha256", "pages":
    [{"page", "text"}]}, already masked. Returns row counts per table."""
    opp = rec.opportunity
    opp_id = opp.id
    if session.get(models.Opportunity, opp.id) is not None:
        raise AlreadyImported(f"Opportunity {opp.id} is already in the database; nothing changed.")
    session.add(models.Opportunity(id=opp.id, code=code or opp.id.upper(), name=opp.project_name, client_name=opp.client,
                                   commercial=opp.commercial_terms, status="BRD Generated"))
    session.flush()
    for d in documents:
        session.add(models.Document(opportunity_id=opp_id, doc_key=d["key"], doc_type=DOC_TYPES[d["kind"]], reference_no=d.get("number") or None,
                                    doc_date=d.get("date") or None, filename=d["name"], sha256=d["sha256"], masked_pages=d["pages"],
                                    status="processed", uploaded_by=ACTOR[0], uploaded_role=ACTOR[1]))
    session.flush()
    counts = populate_brd(session, opp_id, rec, decisions, runs, summary=f"Imported from v2 record version {rec.version}.")
    store.audit(session, *ACTOR, "opportunity.imported", opportunity_id=opp_id, entity_type="opportunity", entity_id=opp.id, version="0.1",
                details={"source_record_version": rec.version, "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **counts})
    session.flush()
    return counts


def populate_brd(session: Session, opp_id: str, rec: OpportunityRecord, decisions: list[dict] | None = None, runs: list[dict] | None = None,
                 summary: str = "AI draft BRD generated.", created_by: tuple[str, str] = ACTOR) -> dict:
    """The pipeline's record -> the BRD tables and draft v0.1. The opportunity and its documents already exist."""
    kept = {"extraction": e} if (e := (session.get(models.Opportunity, opp_id).pipeline or {}).get("extraction")) else {}     # what was extracted stays, so it is never paid for twice
    kept |= {k: rec.routed[k] for k in ("sections", "rejected", "checker") if rec.routed.get(k)}
    session.get(models.Opportunity, opp_id).pipeline = {**kept, "ai_suitability": rec.ai_suitability, "domain_complexity": rec.opportunity.domain_complexity,
                                                        "complexity_reason": rec.opportunity.complexity_reason, "run_trace": rec.run_trace, "guardrails": rec.guardrails,
                                                        "client_dependencies": [a.split(": ", 1)[-1] for a in rec.assumptions if a.startswith("Client responsibility")]}
    documents = session.scalars(select(models.Document).where(models.Document.opportunity_id == opp_id)).all()
    doc_ids = {d.doc_key: d.id for d in documents}

    for s in rec.requirements:
        if s.doc_key not in doc_ids:
            raise ValueError(f"Statement {s.id} refers to an unknown document {s.doc_key!r}.")
        session.add(models.Statement(
            opportunity_id=opp_id, statement_id=s.id, document_id=doc_ids[s.doc_key], section=(s.citations[0].section if s.citations else None) or None,
            section_no=s.section_no or None, text_hash=store.text_hash(s.text), text=s.text, section_role=s.section_role or None,
            classification=s.category, area=s.area or None, home_section=s.home_section or None, superseded_by=s.superseded_by or None,
            citations=[c.model_dump() for c in s.citations]))

    features = {f.id: f for f in rec.features}
    reqs = rec.brd.requirements
    epics: dict[str, str] = {}
    for br in reqs:
        epics.setdefault(br.epic, f"EP-{len(epics) + 1:02d}")
    for order, (name, epic_id) in enumerate(epics.items(), 1):
        session.add(models.Epic(opportunity_id=opp_id, epic_id=epic_id, name=name, sort_order=order))
    session.flush()
    _set_counter(session, opp_id, "EP", len(epics))

    question_map = {q.id: f"OQ-{i:03d}" for i, q in enumerate(rec.brd.questions, 1)}
    # IDs become plain numbers (6a); the order within each epic is kept and display order is its own field
    br_map = {br.id: f"BR-{i:03d}" for i, br in enumerate(reqs, 1)}
    for br in reqs:
        f = features[br.feature_id]
        order = sum(1 for other in reqs[:reqs.index(br) + 1] if other.epic == br.epic)
        session.add(models.Br(
            opportunity_id=opp_id, br_id=br_map[br.id], epic_id=epics[br.epic], sort_order=order, title=renumber_questions(br.title, question_map), statement=renumber_questions(br.statement, question_map),
            priority=f.priority, complexity=f.complexity, size=f.size, rationale=f.justification or None,
            source_citations=[c.model_dump() for c in br.citations], statement_ids=br.statement_ids, scope_status=br.scope_status,
            legacy_id=br.id, ext={"feature": f.model_dump(), "br": br.model_dump(exclude={"citations", "statement", "statement_ids", "title"})}))
    session.flush()
    for br in reqs:
        session.add(models.BrReview(opportunity_id=opp_id, br_id=br_map[br.id]))
    _set_counter(session, opp_id, "BR", len(reqs))

    b = rec.brd
    for i, x in enumerate(b.rules, 1):
        session.add(models.BusinessRule(**_item_fields(opp_id, x, f"BRule-{i:03d}", i, br_map, question_map),
                                        ext={"kind": x.kind, "conflict_question_id": x.conflict_question_id}))
    _set_counter(session, opp_id, "BRule", len(b.rules))
    for i, x in enumerate(b.entities, 1):
        session.add(models.Entity(**_item_fields(opp_id, x, x.id, i, br_map, question_map), ext={"attributes": x.attributes}))
    for i, x in enumerate(b.nfrs, 1):
        session.add(models.Nfr(**_item_fields(opp_id, x, x.id, i, br_map, question_map), category=x.kind or None, measurable=x.measurable))
    for i, x in enumerate(b.constraints, 1):
        session.add(models.ConstraintAssumption(**_item_fields(opp_id, x, x.id, i, br_map, question_map), item_type="Constraint", area=x.kind or None))
    for i, x in enumerate(b.assumptions, len(b.constraints) + 1):
        session.add(models.ConstraintAssumption(**_item_fields(opp_id, x, x.id, i, br_map, question_map), item_type="Assumption", area=x.kind or None))
    for i, x in enumerate(b.questions, 1):
        session.add(models.OpenQuestion(**_item_fields(opp_id, x, f"OQ-{i:03d}", i, br_map, question_map), source=x.kind or None,
                                        blocking=x.kind in ("conflict", "coverage"), audience="internal" if x.kind == "guard" or "content filter" in x.text else "client", ext={"pages": x.pages}))
    _set_counter(session, opp_id, "OQ", len(b.questions))

    for d in decisions or []:
        session.add(models.Decision(opportunity_id=opp_id, kind=d["type"], statement_ids=d["statement_ids"], value=d.get("value"),
                                    decided_by=ACTOR[0], decided_role=ACTOR[1], decided_at=datetime.fromisoformat(d["at"])))
    for r in runs or []:
        at = datetime.fromisoformat(r["at"])
        session.add(models.Run(opportunity_id=opp_id, kind="process_tor", status="Succeeded", started_at=at, finished_at=at,
                               cost_usd=r.get("live_usd", 0.0), cache_hits=r.get("hits", 0), cache_misses=r.get("misses", 0),
                               detail={"record_version": r.get("version")}))

    session.flush()
    import_exclusions(session, opp_id, rec.exclusions)
    normalized = normalize_priorities(session, opp_id)
    brd_sections.generate(session, opp_id, documents, rec.routed)
    session.add(models.ArtifactVersion(opportunity_id=opp_id, artifact_type="BRD", version_major=0, version_minor=1, status="Draft",
                                       snapshot=store.brd_snapshot(session, opp_id), created_by=created_by[0], created_role=created_by[1], change_summary=summary))
    session.flush()
    return {"statements": len(rec.requirements), "epics": len(epics), "brs": len(reqs), "business_rules": len(b.rules),
            "entities": len(b.entities), "nfrs": len(b.nfrs), "constraints_assumptions": len(b.constraints) + len(b.assumptions),
            "open_questions": len(b.questions) + normalized["confirmations"], "out_of_scope": len(rec.exclusions) + normalized["retired"],
            "decisions": len(decisions or []), "runs": len(runs or []), "documents": len(documents)}

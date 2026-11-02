"""AI-generated estimates and timeline, and their validation (POC v3 W5). Generated only from an approved BRD version. The existing
calculators do all the arithmetic, in code, with no LLM; this service feeds them from the database, keeps each result as a version and
applies the Solutions Specialist's overrides. A new BRD version never re-estimates what did not change."""
from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from docx import Document
from docx.shared import Pt
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import brd_service, costing, doc_cache, estimation, tracing, models, opportunities, store, suitability, units, workflow
from app.config import estimation_config
from app.effort import activities_config
from app.record import BusinessRequirement, Brd, BrdItem, Citation, Locator, Opportunity, OpportunityRecord, RecordFeature
from app.workflow import Actor

CHECKLIST = {"effort_complexity": "Effort and complexity", "resources": "Resource allocation", "dependencies": "Dependencies and sequencing", "ai_productivity": "AI productivity assumptions",
             "integration_testing": "Integration, testing and contingency effort", "feasibility": "Delivery feasibility", "commercial": "Cost, pricing and target margin"}
ASSUMPTION_KEYS = ("productivity_profile", "tooling_profile", "uat_weeks", "pilot_weeks", "contingency_rate", "target_gross_margin", "warranty_months", "ai_first_validated")
SCENARIOS = ("B", "C")
DEFAULT_FEATURE_TYPE = "workflow"
RULE_SIZED = "Rule-sized — check"


def _man_days(hours: float) -> float:
    return round(units.available()["Man-days"].value(hours), 1)


def empty_inputs() -> dict:
    return {"sizes": {}, "activity_hours": {}, "team": {}, "assumptions": {}}


def _locator(c: dict) -> Locator:
    return Locator(file=c.get("file") or "", page=c.get("page") or 0, section=c.get("section") or "", quote=c.get("quote") or "", doc_label=c.get("doc_label") or "TOR")


def _item(row: dict, kind: str = "") -> BrdItem:
    return BrdItem(id=row["item_id"], text=row["text"], description=row.get("description") or "", kind=kind, statement_id=row.get("statement_id") or "", superseded_by=row.get("superseded_by") or "",
                   citations=[_locator(c) for c in row.get("citations") or []], unsupported=bool(row.get("unsupported")), br_ids=row.get("br_ids") or [], measurable=row.get("measurable"))


def feature_id(br: dict) -> str:
    return br["ext"].get("feature", {}).get("id") or br["br_id"]


def build_record(opp: models.Opportunity, documents: list, brd: models.ArtifactVersion, inputs: dict) -> OpportunityRecord:
    """The approved BRD version as the record the calculators and the proposal read."""
    snap = brd.snapshot
    epics = {e["epic_id"]: e["name"] for e in snap["epics"]}
    active = [b for b in snap["brs"] if b["lifecycle_status"] == "Active"]
    parents = {b["br_id"]: b for b in snap["brs"]}
    features, cards = [], []
    for b in active:
        base = dict(b["ext"].get("feature", {}))
        parent = parents.get(b["split_from"] or "", {}).get("ext", {}).get("feature", {})
        size = inputs["sizes"].get(b["br_id"]) or b["size"]
        route = base.get("route") or "clear"
        if inputs["sizes"].get(b["br_id"]) and route == "unclear":     # a person sized it, so it counts
            route = "clear"
        fid = feature_id(b)
        features.append(RecordFeature(**{**base, "id": fid, "epic": epics[b["epic_id"]], "name": b["title"], "description": base.get("description") or b["statement"],
                                         "type": base.get("type") or parent.get("type") or DEFAULT_FEATURE_TYPE, "priority": b["priority"], "complexity": b["complexity"] or "medium", "size": size,
                                         "route": route, "statement_ids": b["statement_ids"], "scope_status": b["scope_status"], "origin": base.get("origin") or "tor",
                                         "justification": b["rationale"] or "", "citations": [Citation(**c) for c in base.get("citations", [])]}))
        cards.append(BusinessRequirement(id=b["br_id"], feature_id=fid, title=b["title"], epic=epics[b["epic_id"]], statement=b["statement"], statement_ids=b["statement_ids"], scope_status=b["scope_status"],
                                         citations=[_locator(c) for c in b["source_citations"]]))
    bundled = any(d.doc_key == "tor" for d in documents)
    terms = {k: v for k, v in opp.commercial.items() if k in ("requested_months", "upfront_pct", "final_pct", "warranty_months", "deliverables") and v}
    pipeline = opp.pipeline or {}
    constraints = [c for c in snap["constraints_assumptions"] if c["item_type"] == "Constraint"]
    assumptions = [c for c in snap["constraints_assumptions"] if c["item_type"] != "Constraint"]
    rec = OpportunityRecord(
        status="EXTRACTED",
        opportunity=Opportunity(id="hris" if bundled else f"upload-{opp.id}"[:64], client=opp.client_name, project_name=opp.name, domain_complexity=pipeline.get("domain_complexity") or "standard",
                                complexity_reason=pipeline.get("complexity_reason") or "", commercial_terms=terms),
        features=features, exclusions=[{"text": i["data"]["exclusion"]} for i in snap["section_items"] if i["section"] == "out_of_scope"],
        brd=Brd(version=brd.version_label, requirements=cards, rules=[_item(r, "rule") for r in snap["business_rules"]], entities=[_item(r) for r in snap["entities"]], nfrs=[_item(r) for r in snap["nfrs"]],
                constraints=[_item(c, c.get("area") or "") for c in constraints], assumptions=[_item(c, c.get("area") or "") for c in assumptions], questions=[_item(q, q.get("source") or "") for q in snap["open_questions"]]),
        ai_suitability=_suitability(pipeline.get("ai_suitability") or {}, features),
        assumption_overrides={**inputs["assumptions"], "activity_hours": {k: v["hours"] for k, v in inputs["activity_hours"].items()}, "team_fte": inputs["team"]})
    return rec


def _suitability(stored: dict, features: list) -> dict:
    """Stored AI suggestions as they were. A BR added since then gets the default strategy under the delivery-practice rules: no LLM."""
    out = dict(stored)
    act_cfg = activities_config()
    for key, value in suitability._applied(suitability._units(features, estimation_config(), act_cfg), act_cfg, {}).items():
        out.setdefault(key, value)
    return out


def _results(rec: OpportunityRecord) -> dict:
    data = {"activities": rec.activities, "estimates": rec.estimates, "timeline": rec.timeline, "costing": rec.costing.model_dump(), "risks": rec.risks, "recommendation": rec.recommendation}
    return json.loads(json.dumps(data, default=str))


def _br_rows(snap: dict, results: dict, inputs: dict, validated: dict[str, bool]) -> dict:
    by_feature = results["estimates"]["by_feature"]
    not_priced = {x["feature_id"]: x for x in results["estimates"].get("suggested_scope", [])}
    rows = {}
    for b in snap["brs"]:
        if b["lifecycle_status"] != "Active":
            continue
        fid = feature_id(b)
        priced = by_feature.get(fid, {})
        hours = {s: priced.get(s, 0.0) for s in ("baseline", *SCENARIOS)}
        rows[b["br_id"]] = {"title": b["title"], "epic_id": b["epic_id"], "feature_id": fid, "size": inputs["sizes"].get(b["br_id"]) or b["size"], "size_overridden": b["br_id"] in inputs["sizes"],
                            "scope": b["scope_status"], "priced": fid not in not_priced, "rule_sized": b["ext"].get("size_source") == "rule" and b["br_id"] not in inputs["sizes"], "hours": hours, "man_days": {s: _man_days(v) for s, v in hours.items()}, "validated": validated.get(b["br_id"], False)}
    return rows


def _totals(results: dict) -> dict:
    out = {}
    for s in SCENARIOS:
        c, sc = results["costing"]["scenarios"][s], results["estimates"]["scenarios"][s]
        out[s] = {"man_days": _man_days(sc["total_hours"]), "hours": sc["total_hours"], "months": sc["months"], "price_ex_vat": c["price_ex_vat"], "gross_margin_pct": c["gross_margin_pct"]}
    return out


def _snapshot(opp, documents, brd, inputs, rates, discount, validated, checklist=None, with_extras: bool = True) -> dict:
    rec, _ = estimation.run_estimation(build_record(opp, documents, brd, inputs), rates, discount, with_extras=with_extras)
    results = _results(rec)
    return {"brd_version": brd.version_label, "inputs": inputs, "results": results, "rows": _br_rows(brd.snapshot, results, inputs, validated), "totals": _totals(results),
            "checklist": checklist or dict.fromkeys(CHECKLIST, False)}


def _new_version(session: Session, actor: Actor, opp_id: str, snapshot: dict, brd: models.ArtifactVersion, summary: str, status: str) -> models.ArtifactVersion:
    latest = opportunities.current_version(session, opp_id, "ESTIMATE")
    major, minor = (latest.version_major, latest.version_minor + 1) if latest else (0, 1)
    opportunities.supersede(session, opp_id, "ESTIMATE")
    version = models.ArtifactVersion(opportunity_id=opp_id, artifact_type="ESTIMATE", version_major=major, version_minor=minor, status=status, based_on_id=brd.id, snapshot=snapshot,
                                     created_by=actor.name, created_role=actor.role, change_summary=summary)
    session.add(version)
    session.flush()
    return version


def _approved_brd(session: Session, opp_id: str) -> models.ArtifactVersion:
    brd = opportunities.current_version(session, opp_id, "BRD")
    if brd is None or brd.status != "Approved":
        raise workflow.InvalidTransition("Estimates are generated only from an approved BRD version.")
    return brd


def _changed_brs(old_brd: dict, new_brd: dict) -> dict:
    """BR IDs by how they changed between two BRD versions, compared by ID and content fingerprint."""
    a, b = store.fingerprints(old_brd)["brs"], store.fingerprints(new_brd)["brs"]
    out = {"added": [], "modified": [], "removed": [], "unchanged": []}
    for br_id in sorted(set(a) | set(b)):
        was, now = a.get(br_id), b.get(br_id)
        was_active, is_active = bool(was and was["lifecycle"] == "Active"), bool(now and now["lifecycle"] == "Active")
        if not was_active and is_active:
            out["added"].append(br_id)
        elif was_active and not is_active:
            out["removed"].append(br_id)
        elif was_active and is_active:
            out["unchanged" if was["hash"] == now["hash"] else "modified"].append(br_id)
    return out


def _delta(old: dict, new: dict, changed: dict) -> dict:
    rows = []
    for br_id in changed["added"] + changed["modified"] + changed["removed"]:
        was, now = old["rows"].get(br_id), new["rows"].get(br_id)
        rows.append({"br_id": br_id, "title": (now or was)["title"], "change": "added" if br_id in changed["added"] else "modified" if br_id in changed["modified"] else "removed",
                     "size_from": was["size"] if was else None, "size_to": now["size"] if now else None, "man_days_from": was["man_days"] if was else None, "man_days_to": now["man_days"] if now else None})
    return {"rows": rows, "totals": {s: {"from": old["totals"][s], "to": new["totals"][s]} for s in SCENARIOS}, "unchanged": len(changed["unchanged"]), "from_version": old["brd_version"], "to_version": new["brd_version"]}


def _carried(old: dict, changed: dict) -> tuple[dict, dict]:
    """What an unchanged BR keeps from the previous estimate: its size override, activity hours and validation."""
    keep = set(changed["unchanged"])
    keep_features = {old["rows"][b]["feature_id"] for b in keep if b in old["rows"]}
    known = {r["feature_id"] for r in old["rows"].values()}
    inputs = {"sizes": {b: v for b, v in old["inputs"]["sizes"].items() if b in keep},
              "activity_hours": {k: v for k, v in old["inputs"]["activity_hours"].items() if k.split("|")[0] in keep_features or k.split("|")[0] not in known},
              "team": old["inputs"]["team"], "assumptions": old["inputs"]["assumptions"]}
    return inputs, {b: old["rows"][b]["validated"] for b in keep if b in old["rows"]}


def preview_delta(session: Session, actor: Actor, opp_id: str, changes: dict, rates: costing.RateTable | None = None, discount: float = 0.0) -> dict | None:
    """What applying these BRD changes would do to the estimate: the same steps as a real update, in a transaction that is always rolled back. Calculators only,
    no LLM, nothing written. None when there is no estimate yet."""
    latest = opportunities.current_version(session, opp_id, "ESTIMATE")
    if latest is None or not brd_service.has_changes(changes):
        return None
    old, old_brd = latest.snapshot, session.get(models.ArtifactVersion, latest.based_on_id).snapshot
    with Session(session.get_bind()) as probe:
        try:
            brd_service.apply_changes(probe, actor, opp_id, changes, "re-upload")
            draft = SimpleNamespace(snapshot=store.brd_snapshot(probe, opp_id), version_label="preview")
            changed = _changed_brs(old_brd, draft.snapshot)
            inputs, validated = _carried(old, changed)
            opp = probe.get(models.Opportunity, opp_id)
            new = _snapshot(opp, opportunities.documents(probe, opp_id), draft, inputs, rates or costing.illustrative_rates(), discount, validated, with_extras=False)
            return _delta(old, new, changed)
        finally:
            probe.rollback()


@tracing.traced("estimate_generate", lambda session, actor, opp_id, rates=None, *a, **k: {"opportunity_id": opp_id, "role": actor.role, "rates_source": rates.source if rates else "illustrative"}, tracing.version_label, "chain")
def generate(session: Session, actor: Actor, opp_id: str, rates: costing.RateTable | None = None, discount: float = 0.0) -> models.ArtifactVersion:
    """From the approved BRD. If an estimate exists, only what changed is new: unchanged BRs keep their sizes, activity hours, overrides
    and validation. Removed BRs drop out. Everything else is recomputed by the calculators. No LLM."""
    workflow.require(actor, workflow.ACT_EDIT_ESTIMATE)
    opp = session.get(models.Opportunity, opp_id, populate_existing=True)
    if opp.status != workflow.BRD_APPROVED:
        raise workflow.InvalidTransition(f"Estimates are generated after BRD approval (now: {opp.status}).")
    brd = _approved_brd(session, opp_id)
    documents = opportunities.documents(session, opp_id)
    rates = rates or costing.illustrative_rates()
    previous = opportunities.current_version(session, opp_id, "ESTIMATE")
    if previous is None:
        snapshot = _snapshot(opp, documents, brd, empty_inputs(), rates, discount, {})
        summary = f"AI-generated estimates and timeline from BRD v{brd.version_label}."
        changed = None
    else:
        old_brd = session.get(models.ArtifactVersion, previous.based_on_id)
        old = previous.snapshot
        changed = _changed_brs(old_brd.snapshot, brd.snapshot)
        inputs, validated = _carried(old, changed)
        snapshot = _snapshot(opp, documents, brd, inputs, rates, discount, validated)
        snapshot["delta"] = _delta(old, snapshot, changed)
        summary = (f"Updated for BRD v{brd.version_label}: {len(changed['added'])} added, {len(changed['modified'])} modified, {len(changed['removed'])} removed; "
                   f"{len(changed['unchanged'])} unchanged kept as validated.")
    version = _new_version(session, actor, opp_id, snapshot, brd, summary, "Draft")
    workflow.transition(session, opp_id, workflow.ESTIMATE_GENERATED, workflow.SYSTEM_ACTOR, details={"triggered_by": actor.name, "brd_version": brd.version_label})
    store.audit(session, actor.name, actor.role, "estimate.generated", opportunity_id=opp_id, entity_type="ESTIMATE", version=version.version_label, details={"brd_version": brd.version_label, "changed": bool(changed)})
    return version


def _open_for_change(session: Session, actor: Actor, opp_id: str, trigger: str) -> models.ChangeRequest | None:
    """Generated moves to validation. A change to an approved estimate opens a change request and marks what depends on it Outdated."""
    opp = session.get(models.Opportunity, opp_id, populate_existing=True)
    if opp.status == workflow.ESTIMATE_GENERATED:
        workflow.transition(session, opp_id, workflow.ESTIMATE_VALIDATION, actor if actor.role == workflow.SPECIALIST else workflow.SYSTEM_ACTOR)
        return None
    if opp.status == workflow.ESTIMATE_VALIDATION:
        return None
    if opp.status not in workflow._ESTIMATE_REOPENABLE:
        raise workflow.InvalidTransition(f"The estimate cannot change at {opp.status}.")
    latest = opportunities.current_version(session, opp_id, "ESTIMATE")
    workflow.transition(session, opp_id, workflow.ESTIMATE_VALIDATION, workflow.SYSTEM_ACTOR, details={"triggered_by": actor.name, "trigger": trigger})
    for row in session.scalars(select(models.ArtifactVersion).where(models.ArtifactVersion.opportunity_id == opp_id, models.ArtifactVersion.artifact_type.in_(("PROPOSAL", "SOW", "MSA")), models.ArtifactVersion.status != "Superseded")):
        row.outdated = True
        store.audit(session, actor.name, actor.role, "artifact.outdated", opportunity_id=opp_id, entity_type=row.artifact_type, version=row.version_label)
    request = models.ChangeRequest(opportunity_id=opp_id, artifact_type="ESTIMATE", from_version_id=latest.id, trigger=trigger, status="Confirmed", decided_by=actor.name, decided_role=actor.role,
                                   decided_at=datetime.now(timezone.utc), diff_summary={})
    session.add(request)
    session.flush()
    return request


def start_validation(session: Session, actor: Actor, opp_id: str) -> None:
    workflow.require(actor, workflow.ACT_EDIT_ESTIMATE)
    workflow.transition(session, opp_id, workflow.ESTIMATE_VALIDATION, actor)
    version = opportunities.current_version(session, opp_id, "ESTIMATE")
    version.status = "Under validation"
    session.flush()


def edit(session: Session, actor: Actor, opp_id: str, seen_version: str, *, sizes: dict | None = None, activity_hours: dict | None = None, team: dict | None = None, assumptions: dict | None = None,
         rates: costing.RateTable | None = None, discount: float = 0.0) -> models.ArtifactVersion:
    """Every save is a new minor version, recalculated by code. seen_version: what the user was looking at; a stale save is refused."""
    workflow.require(actor, workflow.ACT_EDIT_ESTIMATE)
    latest = opportunities.current_version(session, opp_id, "ESTIMATE")
    if latest is None or latest.version_label != seen_version:
        raise store.StaleWrite
    old = latest.snapshot
    inputs = json.loads(json.dumps(old["inputs"]))
    parts = []
    for br_id, size in (sizes or {}).items():
        if br_id not in old["rows"]:
            raise ValueError(f"{br_id} is not in this estimate.")
        if size not in brd_service.SIZES:
            raise ValueError(f"Size must be one of {', '.join(brd_service.SIZES)}.")
        inputs["sizes"][br_id] = size
        parts.append(f"{br_id} size {size}")
    known_activities = {f"{r['feature_id']}|{r['activity']}" for r in old["results"]["activities"]}
    for key, value in (activity_hours or {}).items():
        if key not in known_activities:
            raise ValueError(f"{key} is not an activity of this estimate.")
        if not str(value.get("reason") or "").strip():
            raise ValueError("Give a reason for an activity hours override.")
        if float(value["hours"]) < 0:
            raise ValueError("Hours cannot be negative.")
        inputs["activity_hours"][key] = {"hours": float(value["hours"]), "reason": value["reason"].strip(), "by": actor.name}
        parts.append(f"{key} {float(value['hours']):g} h")
    for scenario, roles in (team or {}).items():
        if scenario not in SCENARIOS:
            raise ValueError("The team is set for B or C.")
        if any(float(v) < 0 for v in roles.values()):
            raise ValueError("FTE cannot be negative.")
        inputs["team"][scenario] = {**inputs["team"].get(scenario, {}), **{r: float(v) for r, v in roles.items()}}
        parts.append(f"{scenario} team")
    for key, value in (assumptions or {}).items():
        if key not in ASSUMPTION_KEYS:
            raise ValueError(f"{key} cannot be edited here.")
        inputs["assumptions"][key] = value
        parts.append(key.replace("_", " "))
    if not parts:
        raise ValueError("Nothing to save.")
    request = _open_for_change(session, actor, opp_id, "in-app edit")
    opp = session.get(models.Opportunity, opp_id)
    brd = session.get(models.ArtifactVersion, latest.based_on_id)
    rows = {b: r["validated"] for b, r in old["rows"].items()}
    snapshot = _snapshot(opp, opportunities.documents(session, opp_id), brd, inputs, rates or costing.illustrative_rates(), discount, rows, old.get("checklist"))
    version = _new_version(session, actor, opp_id, snapshot, brd, "Edited: " + "; ".join(parts) + ".", "Under validation")
    if request is not None:
        request.diff_summary = {"edits": parts, "to_version": version.version_label}
    store.audit(session, actor.name, actor.role, "estimate.edited", opportunity_id=opp_id, entity_type="ESTIMATE", version=version.version_label, details={"edits": parts})
    return version


@tracing.traced("estimate_re_estimate_all", lambda session, actor, opp_id, *a, **k: {"opportunity_id": opp_id, "role": actor.role}, tracing.version_label, "chain")
def re_estimate_all(session: Session, actor: Actor, opp_id: str, reason: str, rates: costing.RateTable | None = None, discount: float = 0.0) -> models.ArtifactVersion:
    """Admin only, with a reason, because it discards every override."""
    workflow.require(actor, workflow.ACT_ADMIN)
    if not reason.strip():
        raise ValueError("Give a reason: re-estimating discards every override.")
    latest = opportunities.current_version(session, opp_id, "ESTIMATE")
    if latest is None:
        raise workflow.InvalidTransition("There is no estimate yet.")
    request = _open_for_change(session, actor, opp_id, "re-estimate all")
    opp = session.get(models.Opportunity, opp_id)
    brd = session.get(models.ArtifactVersion, latest.based_on_id)
    snapshot = _snapshot(opp, opportunities.documents(session, opp_id), brd, empty_inputs(), rates or costing.illustrative_rates(), discount, {})
    version = _new_version(session, actor, opp_id, snapshot, brd, f"Re-estimated all, overrides discarded: {reason.strip()}", "Under validation")
    if request is not None:
        request.diff_summary = {"reason": reason.strip(), "to_version": version.version_label}
    store.audit(session, actor.name, actor.role, "estimate.re_estimated_all", opportunity_id=opp_id, entity_type="ESTIMATE", version=version.version_label, details={"reason": reason.strip()})
    return version


def _validating(session: Session, actor: Actor, opp_id: str) -> models.ArtifactVersion:
    workflow.require(actor, workflow.ACT_EDIT_ESTIMATE)
    opp = session.get(models.Opportunity, opp_id, populate_existing=True)
    if opp.status != workflow.ESTIMATE_VALIDATION:
        raise workflow.InvalidTransition(f"The estimate is not under validation (now: {opp.status}).")
    return opportunities.current_version(session, opp_id, "ESTIMATE")


def validate_rows(session: Session, actor: Actor, opp_id: str, br_ids: list[str] | None = None) -> int:
    """Saved per click; not a new version. None: every row still needing validation."""
    version = _validating(session, actor, opp_id)
    rows = json.loads(json.dumps(version.snapshot["rows"]))
    todo = [b for b, r in rows.items() if not r["validated"] and (br_ids is None or b in br_ids)]
    for b in todo:
        rows[b]["validated"] = True
    version.snapshot = {**version.snapshot, "rows": rows}
    session.flush()
    store.audit(session, actor.name, actor.role, "estimate.rows_validated", opportunity_id=opp_id, entity_type="ESTIMATE", version=version.version_label, details={"count": len(todo)})
    return len(todo)


def tick(session: Session, actor: Actor, opp_id: str, item: str, value: bool) -> None:
    version = _validating(session, actor, opp_id)
    if item not in CHECKLIST:
        raise ValueError(f"Unknown checklist item: {item}")
    version.snapshot = {**version.snapshot, "checklist": {**version.snapshot["checklist"], item: bool(value)}}
    session.flush()
    store.audit(session, actor.name, actor.role, "estimate.checklist", opportunity_id=opp_id, entity_type="ESTIMATE", version=version.version_label, details={"item": item, "ticked": bool(value)})


def approval_blockers(session: Session, opp_id: str) -> list[str]:
    opp = session.get(models.Opportunity, opp_id, populate_existing=True)
    out = []
    if opp.status != workflow.ESTIMATE_VALIDATION:
        out.append(f"The estimate must be under validation (now: {opp.status}).")
    version = opportunities.current_version(session, opp_id, "ESTIMATE")
    if version is None:
        return out + ["There is no estimate yet."]
    brd = opportunities.current_version(session, opp_id, "BRD")
    if brd is None or brd.id != version.based_on_id or brd.status != "Approved":
        out.append("The estimate is not based on the current approved BRD.")
    if version.outdated:
        out.append("The estimate is outdated.")
    snap = version.snapshot
    waiting = sum(1 for r in snap["rows"].values() if not r["validated"])
    if waiting:
        out.append(f"{waiting} rows need validation.")
    unticked = [label for key, label in CHECKLIST.items() if not snap["checklist"].get(key)]
    if unticked:
        out.append("Tick the checklist: " + ", ".join(unticked) + ".")
    return out


def approve(session: Session, actor: Actor, opp_id: str, comment: str | None = None, expected_row_version: int | None = None) -> models.ArtifactVersion:
    """E v1.0 on first approval, then the next major. A person validates; the AI never approves."""
    workflow.require_at_a_gate(session, actor, workflow.ACT_EDIT_ESTIMATE, opp_id)
    blockers = approval_blockers(session, opp_id)
    if blockers:
        raise workflow.InvalidTransition(" ".join(blockers))
    latest = opportunities.current_version(session, opp_id, "ESTIMATE")
    workflow.transition(session, opp_id, workflow.ESTIMATE_APPROVED, actor, expected_row_version, details={"from_version": latest.version_label})
    major = session.scalar(select(func.max(models.ArtifactVersion.version_major)).where(models.ArtifactVersion.opportunity_id == opp_id, models.ArtifactVersion.artifact_type == "ESTIMATE"))
    snapshot, based_on = latest.snapshot, latest.based_on_id
    opportunities.supersede(session, opp_id, "ESTIMATE")
    version = models.ArtifactVersion(opportunity_id=opp_id, artifact_type="ESTIMATE", version_major=major + 1, version_minor=0, status="Approved", based_on_id=based_on, snapshot=snapshot, created_by=actor.name,
                                     created_role=actor.role, change_summary=f"Approved from v{latest.version_label}.")
    session.add(version)
    session.flush()
    session.add(models.Approval(opportunity_id=opp_id, stage="ESTIMATE", artifact_version_id=version.id, approver_name=actor.name, approver_role=actor.role, decision="Approved", comment=comment))
    session.flush()
    store.audit(session, actor.name, actor.role, "estimate.approved", opportunity_id=opp_id, entity_type="ESTIMATE", version=version.version_label)
    from app import publishing   # imports this module
    publishing.publish(session, actor, opp_id, version)
    return version


def wbs_rows(snapshot: dict, scenario: str | None = None) -> list[dict]:
    """Work breakdown structure: epic -> BR/feature -> activity by role."""
    by_feature = {r["feature_id"]: b for b, r in snapshot["rows"].items()}
    overrides = snapshot["inputs"]["activity_hours"]
    grouped: dict[tuple, dict] = {}
    for a in snapshot["results"]["activities"]:
        key = (a["epic"], a["feature_id"], a["activity"])
        row = grouped.setdefault(key, {"epic": a["epic"], "br_id": by_feature.get(a["feature_id"], "Foundation"), "feature": a["feature"], "role": a["role"], "activity": a["activity_label"].split(" (")[0], "key": f"{a['feature_id']}|{a['activity']}",
                                       "baseline": 0.0, "B": 0.0, "C": 0.0, "overridden": f"{a['feature_id']}|{a['activity']}" in overrides})
        row["baseline"] += a["baseline"]
        row["B"] += a["B"]["total"]
        row["C"] += a["C"]["total"]
    return list(grouped.values())


def tooling_table(costs: dict) -> list[tuple[str, dict]]:
    """[(row, {scenario: PHP})]: the AI tooling lines, their total, and net savings against the baseline."""
    rows = [(label, {s: costs[s]["ai_tooling_lines"][key] for s in SCENARIOS}) for key, label in costing.TOOLING_LINES]
    rows.append(("AI tooling cost", {s: costs[s]["ai_tooling"] for s in SCENARIOS}))
    net = costing.net_savings(costs)
    rows.append(("Labour saved vs. baseline", {s: net[s]["labor_saved"] for s in SCENARIOS}))
    rows.append(("Net savings vs. baseline (labour saved less AI tooling)", {s: net[s]["net_savings"] for s in SCENARIOS}))
    return rows


def file_name(code: str, label: str) -> str:
    return f"ESTIMATE-{code}-R1-v{label}-INTERNAL.docx"


def summary_docx(session: Session, opp_id: str, version: models.ArtifactVersion | None = None) -> bytes:
    """The internal estimate summary: it holds pricing, so it is never a client document."""
    version = version or opportunities.current_version(session, opp_id, "ESTIMATE")
    opp = session.get(models.Opportunity, opp_id)
    return doc_cache.remember(doc_cache.digest("estimate", doc_cache.version_hash(version), doc_cache.opportunity_hash(opp)), lambda: _summary_docx(version, opp))


def _summary_docx(version: models.ArtifactVersion, opp: models.Opportunity) -> bytes:
    snap = version.snapshot
    doc = Document()
    doc.styles["Normal"].font.size = Pt(10)
    doc.sections[0].header.paragraphs[0].add_run(f"INTERNAL — CONTAINS PRICING · {opp.code} · v{version.version_label} · {version.status}").bold = True

    def table(headers, rows):
        t = doc.add_table(rows=1, cols=len(headers))
        t.style = "Table Grid"
        for cell, head in zip(t.rows[0].cells, headers):
            cell.text = ""
            cell.paragraphs[0].add_run(head).bold = True
        for row in rows:
            for cell, value in zip(t.add_row().cells, row):
                cell.text = str(value)
        doc.add_paragraph()

    doc.add_heading(f"Estimate summary: {opp.name}", 0)
    doc.add_paragraph(f"{opp.client_name} · AI-generated estimates and timeline, based on BRD v{snap['brd_version']}. {version.change_summary or ''}")
    doc.add_heading("Scenarios", 1)
    costs = snap["results"]["costing"]["scenarios"]
    labels = {"B": "B AI-Augmented", "C": "C AI-First"}
    table(("Scenario", "Man-days", "Months", "Delivery cost (PHP)", "Contingency (PHP)", "Price ex VAT (PHP)", "Gross margin"),
          [(labels[s], f"{snap['totals'][s]['man_days']:,.0f}", f"{snap['totals'][s]['months']:.1f}", f"{costs[s]['delivery_cost']:,.0f}", f"{costs[s]['contingency']:,.0f}", f"{costs[s]['price_ex_vat']:,.0f}", f"{costs[s]['gross_margin_pct']:.0f}%") for s in SCENARIOS])
    if all("ai_tooling_lines" in costs[s] for s in SCENARIOS):
        doc.add_heading("AI tooling cost and net savings", 1)
        doc.add_paragraph(f"Tooling profile: {snap['results']['estimates'].get('tooling_profile', '')}. All prices and usage figures are illustrative assumptions from configuration.")
        table(("Item (PHP)", *(labels[s] for s in SCENARIOS)), [(row, *(f"{values[s]:,.0f}" for s in SCENARIOS)) for row, values in tooling_table(costs)])
    doc.add_heading("Team (FTE)", 1)
    timeline = snap["results"]["timeline"]
    roles = sorted({r for s in SCENARIOS for r in timeline[s]["team"]})
    table(("Role", *(labels[s] for s in SCENARIOS)), [(r, *(f"{timeline[s]['team'].get(r, 0):g}" for s in SCENARIOS)) for r in roles])
    doc.add_heading("Timeline", 1)
    for s in SCENARIOS:
        doc.add_paragraph(labels[s]).runs[0].bold = True
        table(("Phase", "Start", "End", "Months"), [(p["label"], p["start_date"], p["end_date"], f"{p['months']:.1f}") for p in timeline[s]["phases"] if p["months"] > 0])
    doc.add_heading("Work breakdown structure", 1)
    table(("BR ID", "Title", "Size", "B man-days", "C man-days", "Validated"), [(b, r["title"], (r["size"] or "—") + (f" ({RULE_SIZED})" if r.get("rule_sized") else ""), f"{r['man_days']['B']:,.1f}", f"{r['man_days']['C']:,.1f}", "Yes" if r["validated"] else "Needs validation") for b, r in snap["rows"].items()])
    overrides = snap["inputs"]
    if overrides["sizes"] or overrides["activity_hours"] or overrides["team"] or overrides["assumptions"]:
        doc.add_heading("Solutions Specialist overrides", 1)
        for b, size in overrides["sizes"].items():
            doc.add_paragraph(f"{b}: size {size}", style="List Bullet")
        for key, value in overrides["activity_hours"].items():
            doc.add_paragraph(f"{key}: {value['hours']:g} hours. Reason: {value['reason']}", style="List Bullet")
        for s, team in overrides["team"].items():
            doc.add_paragraph(f"{s} team: " + ", ".join(f"{r} {v:g}" for r, v in team.items()), style="List Bullet")
        for key, value in overrides["assumptions"].items():
            doc.add_paragraph(f"{key.replace('_', ' ')}: {value}", style="List Bullet")
    doc.add_heading("Delivery and commercial risks", 1)
    table(("Risk", "Severity", "Mitigation"), [(r["title"], r["severity"], r.get("mitigation") or "") for r in snap["results"]["risks"]])
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()

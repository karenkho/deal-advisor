"""BRD validation and versioning (POC v3 W4). The tables hold the current BRD; each save snapshots it into artifact_versions.
QMS numbering: minor = controlled update or draft, major = approved baseline. BR IDs are identifiers, never positions: they are
not renumbered or reused, and display order is sort_order. All changes go through apply_changes, whatever their trigger."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import faults, models, opportunities, store, workflow, tracing
from app.workflow import Actor

REVIEW_STATUSES = ("Pending", "Accepted", "Needs revision", "Deferred")
PRIORITIES = ("Must", "Should", "Could")
COMPLEXITIES = ("low", "medium", "high")
SIZES = ("S", "M", "L", "XL")
EDITABLE = ("title", "statement", "priority", "complexity", "rationale", "epic_id", "scope_status")
SCOPE_STATUSES = ("in_scope", "suggested")
_APPROVABLE = ("Accepted", "Deferred")
_LONGER, _SHORTER = 1.5, 0.67        # a statement this much longer or shorter moves the size one step
_LARGER_WORK = ("approval", "integration", "workflow", "report", "dashboard", "interface")


def active_brs(session: Session, opp_id: str) -> list[models.Br]:
    return session.scalars(select(models.Br).where(models.Br.opportunity_id == opp_id, models.Br.lifecycle_status == "Active")
                           .order_by(models.Br.epic_id, models.Br.sort_order, models.Br.br_id)).all()


def all_brs(session: Session, opp_id: str) -> dict[str, models.Br]:
    return {b.br_id: b for b in session.scalars(select(models.Br).where(models.Br.opportunity_id == opp_id))}


def _review(session: Session, opp_id: str, br_id: str) -> models.BrReview:
    return session.scalars(select(models.BrReview).where(models.BrReview.opportunity_id == opp_id, models.BrReview.br_id == br_id)).one()


def rule_size(statement: str) -> str:
    """A new BR's size by rule, no LLM: one statement of ordinary work is small."""
    return "M" if any(w in statement.lower() for w in _LARGER_WORK) else "S"


def resize(old_size: str | None, old_text: str, new_text: str) -> str:
    """A modified BR keeps its size unless its statement grew or shrank a lot; then one step, by rule."""
    if old_size not in SIZES:
        return rule_size(new_text)
    ratio = len(new_text.split()) / max(len(old_text.split()), 1)
    step = 1 if ratio >= _LONGER else -1 if ratio <= _SHORTER else 0
    return SIZES[min(max(SIZES.index(old_size) + step, 0), len(SIZES) - 1)]


# ---------- review ----------

def start_validation(session: Session, actor: Actor, opp_id: str) -> None:
    workflow.require(actor, workflow.ACT_VALIDATE_BRD)
    opp = session.get(models.Opportunity, opp_id, populate_existing=True)
    if opp.status == workflow.BRD_GENERATED:
        workflow.transition(session, opp_id, workflow.BRD_VALIDATION, actor)
        version = opportunities.current_version(session, opp_id, "BRD")
        version.status = "Under validation"
        session.flush()
    elif opp.status != workflow.BRD_VALIDATION:
        raise workflow.InvalidTransition(f"The BRD is not open for validation at {opp.status}.")


def set_review(session: Session, actor: Actor, opp_id: str, br_id: str, status: str, comment: str | None = None, expected_row_version: int | None = None) -> None:
    """Saved per click."""
    if status not in REVIEW_STATUSES:
        raise ValueError(f"Unknown review status: {status}")
    start_validation(session, actor, opp_id)
    row = _review(session, opp_id, br_id)
    if expected_row_version is not None and row.row_version != expected_row_version:
        raise store.StaleWrite
    row.status, row.reviewer, row.reviewer_role, row.reviewed_at = status, actor.name, actor.role, datetime.now(timezone.utc)
    if comment is not None:
        row.comment = comment
    session.flush()
    store.audit(session, actor.name, actor.role, "br.reviewed", opportunity_id=opp_id, entity_type="br", entity_id=br_id, details={"status": status})


def accept_all_remaining(session: Session, actor: Actor, opp_id: str) -> int:
    start_validation(session, actor, opp_id)
    active = {b.br_id for b in active_brs(session, opp_id)}
    rows = [r for r in session.scalars(select(models.BrReview).where(models.BrReview.opportunity_id == opp_id, models.BrReview.status == "Pending")) if r.br_id in active]
    for row in rows:
        row.status, row.reviewer, row.reviewer_role, row.reviewed_at = "Accepted", actor.name, actor.role, datetime.now(timezone.utc)
    session.flush()
    store.audit(session, actor.name, actor.role, "br.accepted_all_remaining", opportunity_id=opp_id, entity_type="BRD", details={"count": len(rows)})
    return len(rows)


def set_question_audience(session: Session, actor: Actor, opp_id: str, item_id: str, audience: str) -> None:
    """Client questions reach client documents, reworded as clarification requests. Internal ones stay in internal copies."""
    workflow.require(actor, workflow.ACT_VALIDATE_BRD)
    if audience not in ("client", "internal"):
        raise ValueError(f"Unknown audience: {audience}")
    q = session.scalars(select(models.OpenQuestion).where(models.OpenQuestion.opportunity_id == opp_id, models.OpenQuestion.item_id == item_id)).one()
    q.audience = audience
    session.flush()
    store.audit(session, actor.name, actor.role, "question.audience_changed", opportunity_id=opp_id, entity_type="open_question", entity_id=item_id, details={"audience": audience})


def acknowledge_question(session: Session, actor: Actor, opp_id: str, item_id: str) -> None:
    workflow.require(actor, workflow.ACT_VALIDATE_BRD)
    q = session.scalars(select(models.OpenQuestion).where(models.OpenQuestion.opportunity_id == opp_id, models.OpenQuestion.item_id == item_id)).one()
    q.status = "Acknowledged"
    session.flush()
    store.audit(session, actor.name, actor.role, "question.acknowledged", opportunity_id=opp_id, entity_type="open_question", entity_id=item_id)


def approval_blockers(session: Session, opp_id: str) -> list[str]:
    """Why the BRD cannot be approved yet; empty means it can."""
    out = []
    opp = session.get(models.Opportunity, opp_id, populate_existing=True)
    if opp.status != workflow.BRD_VALIDATION:
        out.append(f"The BRD must be under validation (now: {opp.status}).")
    active = {b.br_id for b in active_brs(session, opp_id)}
    open_reviews = [r for r in session.scalars(select(models.BrReview).where(models.BrReview.opportunity_id == opp_id)) if r.br_id in active and r.status not in _APPROVABLE]
    if open_reviews:
        out.append(f"{len(open_reviews)} BRs are not Accepted or Deferred.")
    blocking = session.scalars(select(models.OpenQuestion.item_id).where(models.OpenQuestion.opportunity_id == opp_id, models.OpenQuestion.blocking, models.OpenQuestion.status == "Open")).all()
    if blocking:
        out.append(f"Blocking open questions not acknowledged: {', '.join(blocking)}.")
    pending = session.scalar(select(func.count()).select_from(models.ChangeRequest).where(models.ChangeRequest.opportunity_id == opp_id, models.ChangeRequest.artifact_type == "BRD", models.ChangeRequest.status == "Open"))
    if pending:
        out.append(f"{pending} change requests are still open.")
    return out


@tracing.traced("brd_approve", lambda session, actor, opp_id, *a, **k: {"opportunity_id": opp_id, "role": actor.role}, tracing.version_label, "chain")
def approve(session: Session, actor: Actor, opp_id: str, comment: str | None = None, expected_row_version: int | None = None) -> models.ArtifactVersion:
    """v1.0 on first approval, then the next major. The earlier versions stay, marked Superseded."""
    workflow.require_at_a_gate(session, actor, workflow.ACT_VALIDATE_BRD, opp_id)
    blockers = approval_blockers(session, opp_id)
    if blockers:
        raise workflow.InvalidTransition(" ".join(blockers))
    latest = opportunities.current_version(session, opp_id, "BRD")
    workflow.transition(session, opp_id, workflow.BRD_APPROVED, actor, expected_row_version, details={"from_version": latest.version_label})
    latest_major = session.scalar(select(func.max(models.ArtifactVersion.version_major)).where(models.ArtifactVersion.opportunity_id == opp_id, models.ArtifactVersion.artifact_type == "BRD"))
    _supersede_earlier(session, opp_id)
    version = models.ArtifactVersion(opportunity_id=opp_id, artifact_type="BRD", version_major=latest_major + 1, version_minor=0, status="Approved",
                                     based_on_id=latest.id, snapshot=store.brd_snapshot(session, opp_id), created_by=actor.name, created_role=actor.role,
                                     change_summary=f"Approved from v{latest.version_label}.")
    session.add(version)
    session.flush()
    session.add(models.Approval(opportunity_id=opp_id, stage="BRD", artifact_version_id=version.id, approver_name=actor.name, approver_role=actor.role,
                                decision="Approved", comment=comment))
    session.flush()
    store.audit(session, actor.name, actor.role, "brd.approved", opportunity_id=opp_id, entity_type="BRD", version=version.version_label)
    from app import publishing   # imports this module
    publishing.publish(session, actor, opp_id, version)
    return version


def _supersede_earlier(session: Session, opp_id: str) -> None:
    for row in session.scalars(select(models.ArtifactVersion).where(models.ArtifactVersion.opportunity_id == opp_id, models.ArtifactVersion.artifact_type == "BRD",
                                                                    models.ArtifactVersion.status != "Superseded")):
        row.status = "Superseded"
    session.flush()


def is_past_approval(status: str) -> bool:
    return status in workflow._REOPENABLE


def reopen(session: Session, actor: Actor, opp_id: str, details: dict | None = None) -> list[int]:
    """A change after approval: back to BRD Under Validation, every downstream artifact Outdated. Returns what it marked."""
    workflow.transition(session, opp_id, workflow.BRD_VALIDATION, workflow.SYSTEM_ACTOR, details={"triggered_by": actor.name, **(details or {})})
    marked = []
    for row in session.scalars(select(models.ArtifactVersion).where(models.ArtifactVersion.opportunity_id == opp_id, models.ArtifactVersion.artifact_type != "BRD",
                                                                    models.ArtifactVersion.outdated.is_(False), models.ArtifactVersion.status != "Superseded")):
        row.outdated = True
        marked.append(row.id)
        store.audit(session, actor.name, actor.role, "artifact.outdated", opportunity_id=opp_id, entity_type=row.artifact_type, version=row.version_label)
    session.flush()
    return marked


def _next_minor(session: Session, opp_id: str, actor: Actor, summary: str, snapshot: dict) -> models.ArtifactVersion:
    latest = opportunities.current_version(session, opp_id, "BRD")
    _supersede_earlier(session, opp_id)
    version = models.ArtifactVersion(opportunity_id=opp_id, artifact_type="BRD", version_major=latest.version_major, version_minor=latest.version_minor + 1, status="Under validation",
                                     based_on_id=latest.id, snapshot=snapshot, created_by=actor.name, created_role=actor.role, change_summary=summary)
    session.add(version)
    session.flush()
    from app import publishing   # imports this module
    publishing.publish(session, actor, opp_id, version)     # every draft reaches BRD/versions, watermarked; never raises for a store problem
    return version


# ---------- changes ----------

def empty_changes() -> dict:
    return {"modified": [], "new": [], "removed": [], "merged": [], "order": {}, "sections": [], "questions": [], "withdrawn": [], "reinstated": []}


class UnexpectedChange(Exception):
    """A BR or item changed that the change request does not list: nothing is applied."""


@tracing.traced("brd_stability_check", lambda session, actor, opp_id, before, after, expected_brs, expected_items: {"opportunity_id": opp_id, "role": actor.role, "expected_brs": len(expected_brs), "expected_items": len(expected_items)},
                lambda r: {k: r[k] for k in ("passed", "changed", "unchanged", "items_changed", "items_unchanged")}, "chain")
def _stability(session: Session, actor: Actor, opp_id: str, before: dict, after: dict, expected_brs: set[str], expected_items: set[str]) -> dict:
    """Compares the new content with the previous version before anything is written. An unlisted change blocks the apply. The audit
    entry has its own transaction, so it survives the rollback of the change it refused."""
    old, new = store.fingerprints(before), store.fingerprints(after)

    def differs(kind: str, key: str) -> bool:
        a, b = old[kind].get(key), new[kind].get(key)
        if kind == "brs":
            return (a or {}).get("hash") != (b or {}).get("hash") or (a or {}).get("lifecycle") != (b or {}).get("lifecycle")
        return a != b

    changed_brs = {k for k in set(old["brs"]) | set(new["brs"]) if differs("brs", k)}
    changed_items = {k for k in set(old["items"]) | set(new["items"]) if differs("items", k)}
    unexpected = sorted((changed_brs - expected_brs) | (changed_items - expected_items))
    if unexpected:
        with Session(session.get_bind()) as separate, separate.begin():
            store.audit(separate, actor.name, actor.role, "brd.change_blocked", opportunity_id=opp_id, entity_type="BRD", details={"unexpected": unexpected})
        raise UnexpectedChange(f"Unexpected change to {', '.join(unexpected)} — not applied.")
    unchanged_brs = sum(1 for k, v in new["brs"].items() if v["lifecycle"] == "Active" and k not in changed_brs)
    unchanged_items = len(new["items"]) - len(changed_items & set(new["items"]))
    return {"passed": True, "changed": len(changed_brs), "unchanged": unchanged_brs, "items_changed": len(changed_items), "items_unchanged": unchanged_items,
            "message": f"Stability check passed: {len(changed_brs)} changed (all expected), {unchanged_brs} unchanged; items: {len(changed_items)} changed, {unchanged_items} unchanged"}


def _counts(changes: dict, new_ids: list[str]) -> str:
    split = {n["split_from"] for n in changes["new"] if n.get("split_from") and n.get("retire_parent")}
    answered = sum(1 for q in changes["questions"] if q.get("status") == "Answered")
    reworded = len(changes["questions"]) - answered
    labelled = ((len(changes["modified"]), "modified"), (len(new_ids), "added"), (len(changes["removed"]), "removed"), (len(split), "split"), (len(changes["merged"]), "merged"),
                (answered, "question answered" if answered == 1 else "questions answered"), (reworded, "question edited" if reworded == 1 else "questions edited"), (len(changes["sections"]), "scope items updated"),
                (len(changes["withdrawn"]), "withdrawn"), (len(changes["reinstated"]), "reinstated"))
    return ", ".join(f"{n} {label}" for n, label in labelled if n) or "order only"


def _summary(changes: dict, new_ids: list[str]) -> str:
    parts = []
    if changes["modified"]:
        parts.append("Modified " + ", ".join(m["br_id"] for m in changes["modified"]))
    if new_ids:
        parts.append("New " + ", ".join(new_ids))
    if changes["removed"]:
        parts.append("Removed " + ", ".join(changes["removed"]))
    split = sorted({n["split_from"] for n in changes["new"] if n.get("split_from") and n.get("retire_parent")})
    if split:
        parts.append("Split " + ", ".join(split))
    if changes["merged"]:
        parts.append("Merged " + ", ".join(f"{m['br_id']} into {m['into']}" for m in changes["merged"]))
    if changes["order"]:
        parts.append("Reordered")
    if changes["sections"] or changes["questions"]:
        parts.append("Updated scope and questions")
    if changes["withdrawn"]:
        parts.append("Withdrew " + ", ".join(w["item_id"] for w in changes["withdrawn"]))
    if changes["reinstated"]:
        parts.append("Reinstated " + ", ".join(r["item_id"] for r in changes["reinstated"]))
    return "; ".join(parts) + "." if parts else "No requirement changes."


def has_changes(changes: dict) -> bool:
    return any(changes.get(k) for k in ("modified", "new", "removed", "merged", "order", "sections", "questions", "withdrawn", "reinstated"))


def _new_br(session: Session, opp_id: str, row: dict, brs: dict, document_id: int | None) -> models.Br:
    parent = brs.get(row.get("split_from"))
    br_id = store.allocate_id(session, opp_id, "BR")
    epic_id = row.get("epic_id") or (parent.epic_id if parent else None)
    last = session.scalar(select(func.coalesce(func.max(models.Br.sort_order), 0)).where(models.Br.opportunity_id == opp_id, models.Br.epic_id == epic_id))
    statement = row["statement"]
    br = models.Br(opportunity_id=opp_id, br_id=br_id, epic_id=epic_id, sort_order=last + 1, title=row["title"], statement=statement, priority=row.get("priority") or "Must",
                   complexity=row.get("complexity") or (parent.complexity if parent else "medium"), size=rule_size(statement), rationale=row.get("rationale"),
                   source_citations=row.get("source_citations") or (parent.source_citations if parent else []), statement_ids=row.get("statement_ids") or (parent.statement_ids if parent else []),
                   split_from=row.get("split_from"), changed_by_document=document_id, scope_status="in_scope", ext={"added_by": row.get("origin", "edit"), "size_source": "rule"})
    session.add(br)
    session.flush()
    session.add(models.BrReview(opportunity_id=opp_id, br_id=br_id))
    session.flush()
    brs[br_id] = br
    return br


def _to_pending(session: Session, opp_id: str, br_id: str) -> None:
    row = _review(session, opp_id, br_id)
    if row.status != "Pending":
        row.status, row.reviewer, row.reviewer_role, row.reviewed_at = "Pending", None, None, None
        session.flush()


def _item_row(session: Session, opp_id: str, item: dict) -> models.OpenQuestion | models.BrdSectionItem:
    if item["section"] == "open_question":
        return session.scalars(select(models.OpenQuestion).where(models.OpenQuestion.opportunity_id == opp_id, models.OpenQuestion.item_id == item["item_id"])).one()
    return session.scalars(select(models.BrdSectionItem).where(models.BrdSectionItem.opportunity_id == opp_id, models.BrdSectionItem.section == item["section"],
                                                               models.BrdSectionItem.item_id == item["item_id"])).one()


def _withdraw(session: Session, actor: Actor, opp_id: str, changes: dict, trigger: str) -> None:
    """Withdrawn, never deleted: who, when and why stay on the row, and it leaves every later version."""
    for item in changes["reinstated"]:
        row = _item_row(session, opp_id, item)
        if item["section"] == "open_question":
            row.status = row.withdrawn.get("previous_status", "Open")
        row.withdrawn = None
        store.audit(session, actor.name, actor.role, "item.reinstated", opportunity_id=opp_id, entity_type=item["section"], entity_id=item["item_id"], details={"trigger": trigger})
    for item in changes["withdrawn"]:
        row = _item_row(session, opp_id, item)
        record = {"by": actor.name, "role": actor.role, "at": datetime.now(timezone.utc).isoformat(), "reason": item.get("reason") or None}
        if item["section"] == "open_question":
            record["previous_status"] = row.status
            row.status = "Withdrawn"
        row.withdrawn = record
        store.audit(session, actor.name, actor.role, "item.withdrawn", opportunity_id=opp_id, entity_type=item["section"], entity_id=item["item_id"], details={"trigger": trigger, "reason": record["reason"]})
    session.flush()


def _apply_sections(session: Session, opp_id: str, changes: dict) -> list[str]:
    """Returns the IDs of the rows it added."""
    added = []
    for item in changes["sections"]:
        if item.get("item_id"):
            row = session.scalars(select(models.BrdSectionItem).where(models.BrdSectionItem.opportunity_id == opp_id, models.BrdSectionItem.section == item["section"],
                                                                      models.BrdSectionItem.item_id == item["item_id"])).one()
            row.data = item["data"]
        else:
            order = 1 + session.scalar(select(func.coalesce(func.max(models.BrdSectionItem.sort_order), 0)).where(models.BrdSectionItem.opportunity_id == opp_id, models.BrdSectionItem.section == item["section"]))
            added.append(store.allocate_id(session, opp_id, item["prefix"]))
            session.add(models.BrdSectionItem(opportunity_id=opp_id, section=item["section"], item_id=added[-1], sort_order=order, data=item["data"]))
    for item in changes["questions"]:
        row = session.scalars(select(models.OpenQuestion).where(models.OpenQuestion.opportunity_id == opp_id, models.OpenQuestion.item_id == item["item_id"])).one()
        for key in ("text", "status", "answer_source"):
            if key in item:
                setattr(row, key, item[key])
    session.flush()
    return added


@tracing.traced("brd_version_save", lambda session, actor, opp_id, changes, trigger, *a, **k: {"opportunity_id": opp_id, "role": actor.role, "trigger": trigger, "changes": tracing.shape(changes)}, tracing.version_label, "chain")
def apply_changes(session: Session, actor: Actor, opp_id: str, changes: dict, trigger: str, document_id: int | None = None,
                  change_request: models.ChangeRequest | None = None, cause: str | None = None) -> models.ArtifactVersion | None:
    """Applies the changes, sends only the affected BRs back to Pending and creates the next minor version. Everything else stays
    exactly as it was. After approval this opens a change request and reopens validation."""
    workflow.require(actor, workflow.ACT_VALIDATE_BRD)
    if not has_changes(changes):
        return None
    opp = session.get(models.Opportunity, opp_id, populate_existing=True)
    if opp.status == workflow.BRD_GENERATED:
        start_validation(session, actor, opp_id)
    elif is_past_approval(opp.status):
        reopen(session, actor, opp_id, {"trigger": trigger})
    elif opp.status != workflow.BRD_VALIDATION:
        raise workflow.InvalidTransition(f"The BRD cannot change at {opp.status}.")
    brs = all_brs(session, opp_id)
    from_version = opportunities.current_version(session, opp_id, "BRD")

    statement_changes = []
    for mod in changes["modified"]:
        br = brs[mod["br_id"]]
        original = dict(br.ext.get("ai_original", {}))
        old_statement = br.statement
        for field, new in mod["changes"].items():
            if field not in EDITABLE:
                raise ValueError(f"{field} cannot be edited.")
            original.setdefault(field, getattr(br, field))
            setattr(br, field, new)
        if "statement" in mod["changes"]:
            statement_changes.append((br.br_id, old_statement, br.statement))
        extra = mod.get("add_statement_ids")
        if extra:
            br.statement_ids = list(dict.fromkeys(br.statement_ids + extra))
        if mod.get("add_citations"):
            br.source_citations = br.source_citations + mod["add_citations"]
        resized = False
        if "statement" in mod["changes"]:
            new_size = resize(br.size, old_statement, br.statement)
            resized, br.size = new_size != br.size, new_size
        # a person's or a clean merged wording clears the flag; the plain fallback sets it
        ext = {k: v for k, v in br.ext.items() if k != "needs_rewording" or "statement" not in mod["changes"]}
        if mod.get("needs_rewording"):
            ext["needs_rewording"] = True
        if resized:
            ext["size_source"] = "rule"
        br.ext = {**ext, "ai_original": original}
        br.changed_by_document = document_id or br.changed_by_document
        _to_pending(session, opp_id, br.br_id)
        store.audit(session, actor.name, actor.role, "br.modified", opportunity_id=opp_id, entity_type="br", entity_id=br.br_id, details={"fields": sorted(mod["changes"]), "trigger": trigger})

    new_ids, placeholders = [], {}
    for i, row in enumerate(changes["new"]):
        br = _new_br(session, opp_id, row, brs, document_id)
        new_ids.append(br.br_id)
        placeholders[f"new:{i}"] = br.br_id
        store.audit(session, actor.name, actor.role, "br.created", opportunity_id=opp_id, entity_type="br", entity_id=br.br_id, details={"trigger": trigger, "split_from": row.get("split_from")})
    for parent in {r["split_from"] for r in changes["new"] if r.get("split_from") and r.get("retire_parent")}:
        brs[parent].lifecycle_status = "Split"
        store.audit(session, actor.name, actor.role, "br.split", opportunity_id=opp_id, entity_type="br", entity_id=parent, details={"parts": [b for b, x in zip(new_ids, changes["new"]) if x.get("split_from") == parent]})
    for br_id in changes["removed"]:
        brs[br_id].lifecycle_status = "Removed"
        store.audit(session, actor.name, actor.role, "br.removed", opportunity_id=opp_id, entity_type="br", entity_id=br_id, details={"trigger": trigger})
    for merge in changes["merged"]:
        gone, survivor = brs[merge["br_id"]], brs[merge["into"]]
        gone.lifecycle_status, gone.merged_into = "Merged", survivor.br_id
        survivor.statement_ids = list(dict.fromkeys(survivor.statement_ids + gone.statement_ids))
        survivor.source_citations = survivor.source_citations + gone.source_citations
        _to_pending(session, opp_id, survivor.br_id)
        store.audit(session, actor.name, actor.role, "br.merged", opportunity_id=opp_id, entity_type="br", entity_id=gone.br_id, details={"into": survivor.br_id})
    session.flush()
    for epic_id, ids in changes["order"].items():
        for position, ref in enumerate(ids, 1):
            br = brs[placeholders.get(ref, ref)]
            if br.sort_order != position or br.epic_id != epic_id:
                br.sort_order, br.epic_id = position, epic_id
    if faults.active("alter_unrelated_br"):
        listed = {m["br_id"] for m in changes["modified"]} | set(changes["removed"])
        next(b for b in brs.values() if b.lifecycle_status == "Active" and b.br_id not in listed).statement = "Silently altered by a fault."
    _withdraw(session, actor, opp_id, changes, trigger)
    added_items = _apply_sections(session, opp_id, changes)
    session.flush()

    # the stability check, then the version: an unlisted change raises here and the whole transaction rolls back
    split_parents = {r["split_from"] for r in changes["new"] if r.get("split_from") and r.get("retire_parent")}
    expected_brs = ({m["br_id"] for m in changes["modified"]} | set(new_ids) | set(changes["removed"]) | split_parents
                    | {m["br_id"] for m in changes["merged"]} | {m["into"] for m in changes["merged"]})
    expected_items = set(added_items) | {i["item_id"] for i in changes["sections"] if i.get("item_id")} | {q["item_id"] for q in changes["questions"]} | {w["item_id"] for w in changes["withdrawn"] + changes["reinstated"]}
    snapshot = store.brd_snapshot(session, opp_id)
    stability = _stability(session, actor, opp_id, from_version.snapshot, snapshot, expected_brs, expected_items)
    default_cause = cause or {"re-upload": f"Re-upload by {actor.name}", "in-app edit": f"In-app edit by {actor.name}"}.get(trigger, trigger)
    causes = {key: default_cause for key in expected_brs | expected_items}
    for entry in changes["modified"] + changes["questions"]:
        if entry.get("cause"):
            causes[entry.get("br_id") or entry["item_id"]] = entry["cause"]
    for br_id, row in zip(new_ids, changes["new"]):
        if row.get("cause"):
            causes[br_id] = row["cause"]
    label = f"{from_version.version_major}.{from_version.version_minor + 1}"
    summary = f"v{label}: {default_cause} — {_counts(changes, new_ids)}; {stability['unchanged']} unchanged. {_summary(changes, new_ids)}"
    if trigger == "bulletin":   # the old -> new wording of each amended BR stays in the revision history
        summary += "".join(f" {br_id}: “{old}” → “{new}”." for br_id, old, new in statement_changes)
    if change_request is None and from_version.version_major >= 1 and is_past_approval_version(from_version):
        change_request = models.ChangeRequest(opportunity_id=opp_id, artifact_type="BRD", from_version_id=from_version.id, trigger=trigger, document_id=document_id, status="Open",
                                              diff_summary={"changes": changes})
        session.add(change_request)
    if change_request is not None:
        session.flush()
    snapshot["stability"] = stability
    snapshot["causes"] = {key: {"label": label, "change_request_id": change_request.id if change_request is not None else None} for key, label in causes.items()}
    version = _next_minor(session, opp_id, actor, summary, snapshot)
    if change_request is not None:
        change_request.status, change_request.decided_by, change_request.decided_role, change_request.decided_at = "Confirmed", actor.name, actor.role, datetime.now(timezone.utc)
    session.flush()
    store.audit(session, actor.name, actor.role, "brd.version_created", opportunity_id=opp_id, entity_type="BRD", version=version.version_label,
                details={"trigger": trigger, "changed": stability["changed"], "unchanged": stability["unchanged"], "stability": "passed"})
    return version


def is_past_approval_version(version: models.ArtifactVersion) -> bool:
    """The version being changed is an approved baseline: this change needs a change request."""
    return version.version_minor == 0 and version.version_major >= 1


# ---------- stable IDs (6a) ----------

def _order_with(brs: list[models.Br], br_id_or_ref: str, after: str | None) -> list[str]:
    ids = [b.br_id for b in brs]
    if after is None:
        return [br_id_or_ref] + ids
    return ids[:ids.index(after) + 1] + [br_id_or_ref] + ids[ids.index(after) + 1:]


def _in_epic(session: Session, opp_id: str, epic_id: str) -> list[models.Br]:
    return [b for b in active_brs(session, opp_id) if b.epic_id == epic_id]


def insert_br(session: Session, actor: Actor, opp_id: str, row: dict, epic_id: str, after: str | None = None) -> models.ArtifactVersion:
    """The next unused number, placed anywhere in the epic: only the display order of the rows after it changes."""
    changes = empty_changes()
    changes["new"] = [{**row, "epic_id": epic_id}]
    changes["order"] = {epic_id: _order_with(_in_epic(session, opp_id, epic_id), "new:0", after)}
    return apply_changes(session, actor, opp_id, changes, "in-app edit")


def retire_br(session: Session, actor: Actor, opp_id: str, br_id: str) -> models.ArtifactVersion:
    changes = empty_changes()
    changes["removed"] = [br_id]
    return apply_changes(session, actor, opp_id, changes, "in-app edit")


def split_br(session: Session, actor: Actor, opp_id: str, br_id: str, parts: list[dict]) -> models.ArtifactVersion:
    """The original is kept in history as Split and no longer estimated; the parts get new IDs and inherit its sources."""
    changes = empty_changes()
    changes["new"] = [{**p, "split_from": br_id, "retire_parent": True} for p in parts]
    return apply_changes(session, actor, opp_id, changes, "in-app edit")


def merge_brs(session: Session, actor: Actor, opp_id: str, survivor: str, others: list[str]) -> models.ArtifactVersion:
    changes = empty_changes()
    changes["merged"] = [{"br_id": b, "into": survivor} for b in others]
    return apply_changes(session, actor, opp_id, changes, "in-app edit")


def move_br(session: Session, actor: Actor, opp_id: str, br_id: str, epic_id: str) -> models.ArtifactVersion:
    changes = empty_changes()
    changes["modified"] = [{"br_id": br_id, "changes": {"epic_id": epic_id}}]
    last = session.scalar(select(func.coalesce(func.max(models.Br.sort_order), 0)).where(models.Br.opportunity_id == opp_id, models.Br.epic_id == epic_id))
    session.get(models.Br, all_brs(session, opp_id)[br_id].id).sort_order = last + 1
    return apply_changes(session, actor, opp_id, changes, "in-app edit")


def split_epic(session: Session, actor: Actor, opp_id: str, epic_id: str, new_name: str, br_ids: list[str]) -> models.ArtifactVersion:
    """A new epic ID takes the chosen BRs; no BR ID changes."""
    workflow.require(actor, workflow.ACT_VALIDATE_BRD)
    order = 1 + session.scalar(select(func.coalesce(func.max(models.Epic.sort_order), 0)).where(models.Epic.opportunity_id == opp_id))
    new_id = store.allocate_id(session, opp_id, "EP", 2)
    session.add(models.Epic(opportunity_id=opp_id, epic_id=new_id, name=new_name, sort_order=order))
    session.flush()
    store.audit(session, actor.name, actor.role, "epic.split", opportunity_id=opp_id, entity_type="epic", entity_id=new_id, details={"from": epic_id, "brs": br_ids})
    changes = empty_changes()
    brs = all_brs(session, opp_id)
    changes["modified"] = [{"br_id": b, "changes": {"epic_id": new_id}} for b in br_ids]
    for position, b in enumerate(br_ids, 1):
        brs[b].sort_order = position
    return apply_changes(session, actor, opp_id, changes, "in-app edit")

"""The management workflow (POC v3): statuses, allowed transitions and role permissions, enforced here rather than by
hiding buttons. Every transition is written to the audit trail in the same transaction."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app import models, store, tracing

BDM, SPECIALIST, LEGAL, ADMIN, SYSTEM = "BDM", "Solutions Specialist", "Legal/Management Reviewer", "Admin", "System"
ROLES = (BDM, SPECIALIST, LEGAL, ADMIN)

DRAFT = "Draft Opportunity"
INPUTS = "Inputs Submitted"
BRD_GENERATED = "BRD Generated"
BRD_VALIDATION = "BRD Under Validation"
BRD_APPROVED = "BRD Approved"
ESTIMATE_GENERATED = "Estimate and Timeline Generated"
ESTIMATE_VALIDATION = "Estimate Under Validation"
ESTIMATE_APPROVED = "Estimate Approved"
PROPOSAL_GENERATED = "Proposal Generated"
PROPOSAL_REVIEW = "Proposal Under Review"
PROPOSAL_SUBMITTED = "Proposal Submitted"
AWARDED = "Awarded"
CONTRACTS_GENERATED = "SOW and MSA Generated"
LEGAL_REVIEW = "Legal and Management Review"
CONTRACT_READY = "Contract-Ready"
CLOSED = "Closed — Not Awarded"
STATUSES = (DRAFT, INPUTS, BRD_GENERATED, BRD_VALIDATION, BRD_APPROVED, ESTIMATE_GENERATED, ESTIMATE_VALIDATION, ESTIMATE_APPROVED,
            PROPOSAL_GENERATED, PROPOSAL_REVIEW, PROPOSAL_SUBMITTED, AWARDED, CONTRACTS_GENERATED, LEGAL_REVIEW, CONTRACT_READY, CLOSED)

_FORWARD = {(DRAFT, INPUTS): {BDM}, (INPUTS, BRD_GENERATED): {SYSTEM}, (BRD_GENERATED, BRD_VALIDATION): {SPECIALIST},
            (BRD_VALIDATION, BRD_APPROVED): {SPECIALIST}, (BRD_APPROVED, ESTIMATE_GENERATED): {SYSTEM},
            (ESTIMATE_GENERATED, ESTIMATE_VALIDATION): {SPECIALIST}, (ESTIMATE_VALIDATION, ESTIMATE_APPROVED): {SPECIALIST},
            (ESTIMATE_APPROVED, PROPOSAL_GENERATED): {BDM}, (PROPOSAL_GENERATED, PROPOSAL_REVIEW): {BDM, LEGAL},
            (PROPOSAL_REVIEW, PROPOSAL_GENERATED): {BDM, LEGAL}, (PROPOSAL_REVIEW, PROPOSAL_SUBMITTED): {BDM},
            (PROPOSAL_SUBMITTED, AWARDED): {BDM}, (PROPOSAL_SUBMITTED, CLOSED): {BDM}, (AWARDED, CONTRACTS_GENERATED): {BDM},
            (CONTRACTS_GENERATED, LEGAL_REVIEW): {LEGAL}, (LEGAL_REVIEW, CONTRACT_READY): {SYSTEM}}
# a change to an approved artifact sends the opportunity back to validation; a rejected change returns it
_REOPENABLE = STATUSES[STATUSES.index(BRD_APPROVED):STATUSES.index(CONTRACT_READY) + 1]
_ESTIMATE_REOPENABLE = STATUSES[STATUSES.index(ESTIMATE_APPROVED):STATUSES.index(CONTRACT_READY) + 1]
TRANSITIONS: dict[tuple[str, str], set[str]] = {**_FORWARD, **{(s, BRD_VALIDATION): {SYSTEM} for s in _REOPENABLE},
                                                **{(s, ESTIMATE_VALIDATION): {SYSTEM} for s in _ESTIMATE_REOPENABLE},
                                                (BRD_VALIDATION, BRD_APPROVED): {SPECIALIST, SYSTEM},
                                                (ESTIMATE_GENERATED, ESTIMATE_VALIDATION): {SPECIALIST, SYSTEM},
                                                (LEGAL_REVIEW, CONTRACTS_GENERATED): {SYSTEM}, (CONTRACT_READY, CONTRACTS_GENERATED): {SYSTEM}}

ACT_MANAGE_OPPORTUNITY, ACT_VALIDATE_BRD, ACT_EDIT_ESTIMATE = "manage_opportunity", "validate_brd", "edit_estimate"
ACT_TRIGGER_COMMERCIAL, ACT_APPROVE_PROPOSAL, ACT_APPROVE_SOW_SCOPE = "trigger_commercial", "approve_proposal", "approve_sow_scope"
ACT_APPROVE_COMMERCIAL, ACT_APPROVE_MSA, ACT_ADMIN = "approve_commercial_details", "approve_msa", "admin"
PERMISSIONS = {ACT_MANAGE_OPPORTUNITY: {BDM}, ACT_VALIDATE_BRD: {SPECIALIST}, ACT_EDIT_ESTIMATE: {SPECIALIST},
               ACT_TRIGGER_COMMERCIAL: {BDM}, ACT_APPROVE_PROPOSAL: {BDM, LEGAL}, ACT_APPROVE_SOW_SCOPE: {SPECIALIST},
               ACT_APPROVE_COMMERCIAL: {BDM}, ACT_APPROVE_MSA: {LEGAL}, ACT_ADMIN: {ADMIN}}

NEXT_ROLE = {DRAFT: BDM, INPUTS: SYSTEM, BRD_GENERATED: SPECIALIST, BRD_VALIDATION: SPECIALIST, BRD_APPROVED: SYSTEM,
             ESTIMATE_GENERATED: SPECIALIST, ESTIMATE_VALIDATION: SPECIALIST, ESTIMATE_APPROVED: BDM, PROPOSAL_GENERATED: BDM,
             PROPOSAL_REVIEW: f"{BDM} / {LEGAL}", PROPOSAL_SUBMITTED: BDM, AWARDED: BDM, CONTRACTS_GENERATED: LEGAL,
             LEGAL_REVIEW: f"{BDM} / {SPECIALIST} / {LEGAL}", CONTRACT_READY: None, CLOSED: None}
NEXT_ACTION = {DRAFT: "Upload the TOR or RFP", INPUTS: "Generate the draft BRD", BRD_GENERATED: "Start BRD validation",
               BRD_VALIDATION: "Review the BRs and approve the BRD", BRD_APPROVED: "Generate the estimates and timeline",
               ESTIMATE_GENERATED: "Start estimate validation", ESTIMATE_VALIDATION: "Validate the generated estimates and timeline",
               ESTIMATE_APPROVED: "Trigger the proposal", PROPOSAL_GENERATED: "Start the proposal review", PROPOSAL_REVIEW: "Approve the proposal",
               PROPOSAL_SUBMITTED: "Record the award decision", AWARDED: "Trigger the SOW and MSA", CONTRACTS_GENERATED: "Start the legal and management review",
               LEGAL_REVIEW: "Complete the final approvals", CONTRACT_READY: "Done", CLOSED: "Closed"}


@dataclass(frozen=True)
class Actor:
    name: str
    role: str


SYSTEM_ACTOR = Actor("Deal Advisor", SYSTEM)


class NotAllowed(Exception):
    """The role may not do this."""


class InvalidTransition(Exception):
    """The workflow does not allow this status change."""


def can(role: str, action: str) -> bool:
    return role in PERMISSIONS[action]


def require(actor: Actor, action: str) -> None:
    if not can(actor.role, action):
        raise NotAllowed(f"{actor.role} may not do this ({action.replace('_', ' ')}).")


def require_at_a_gate(session: Session, actor: Actor, action: str, opp_id: str) -> None:
    """require, for an approval or a purge: a refused attempt is itself recorded. In its own transaction, so it survives the rollback of what it refused."""
    if not can(actor.role, action):
        with Session(session.get_bind()) as separate, separate.begin():
            store.audit(separate, actor.name, actor.role, "permission.refused", opportunity_id=opp_id, entity_type="opportunity", entity_id=opp_id, details={"action": action})
    require(actor, action)


def allowed_targets(status: str, role: str) -> list[str]:
    return [to for (frm, to), roles in TRANSITIONS.items() if frm == status and role in roles]


@tracing.traced("status_transition", lambda session, opportunity_id, target, actor, *a, **k: {"opportunity_id": opportunity_id, "to": target, "role": actor.role}, lambda row: {"status": row.status}, "chain")
def transition(session: Session, opportunity_id: str, target: str, actor: Actor, expected_row_version: int | None = None,
               details: dict | None = None) -> models.Opportunity:
    """Moves the opportunity, or raises. expected_row_version: the version the user saw (a stale screen is refused)."""
    row = session.get(models.Opportunity, opportunity_id, populate_existing=True)
    if row is None:
        raise InvalidTransition(f"Unknown opportunity {opportunity_id}.")
    roles = TRANSITIONS.get((row.status, target))
    if roles is None:
        raise InvalidTransition(f"Status cannot go from {row.status} to {target}.")
    if actor.role not in roles:
        raise NotAllowed(f"{actor.role} may not move this opportunity from {row.status} to {target}.")
    if expected_row_version is not None and row.row_version != expected_row_version:
        raise store.StaleWrite
    previous = row.status
    row.status = target
    session.flush()
    store.audit(session, actor.name, actor.role, "status.changed", opportunity_id=opportunity_id, entity_type="opportunity", entity_id=opportunity_id,
                details={"from": previous, "to": target, **(details or {})})
    return row

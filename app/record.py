"""Opportunity record: the single, versioned source of truth (POC v2). Every step reads from it and writes to it;
documents are rendered from it. Stored as JSON under data/opportunities/<id>/v<n>.json; every edit or approval
writes a new version. Rate VALUES are never stored here: costing keeps only the rates source and a hash."""
from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from app.config import ROOT, UPLOAD_PREFIX

SCHEMA_VERSION = "2.0"
STATUSES = ("EXTRACTED", "ESTIMATED", "RISK_REVIEWED", "APPROVED", "PROPOSAL_DRAFTED", "EXPORTED")
Status = Literal["EXTRACTED", "ESTIMATED", "RISK_REVIEWED", "APPROVED", "PROPOSAL_DRAFTED", "EXPORTED"]
NEXT_ACTION = {"EXTRACTED": "Review and approve the BRD (step 2).", "ESTIMATED": "Review the estimate and the risks (steps 3 and 4).",
               "RISK_REVIEWED": "Review and approve the deal (step 5).",
               "APPROVED": "Generate the draft proposal (step 6).",
               "PROPOSAL_DRAFTED": "Check and download the draft proposal (step 6).",
               "EXPORTED": "Done. Any edit needs a new approval."}
SHARED_NOTE = "Demo note: BA edits may carry over to other users’ next run of the same TOR, and all edits reset when the app restarts."
# notes that move the workflow forward; any other save is an edit
WORKFLOW_NOTES = ("approval:", "status:")
DATA_DIR = ROOT / "data" / "opportunities"


class SourceFile(BaseModel):
    name: str
    sha256: str
    pages: int = 0
    kind: str = "tor"                       # tor | bid_bulletin | minutes | clarification | other
    number: str = ""
    date: str = ""


class Opportunity(BaseModel):
    id: str
    client: str = Field(description="Synthetic client name")
    project_name: str
    files: list[SourceFile] = []
    domain_complexity: Literal["standard", "high"] = "standard"
    complexity_reason: str = ""
    commercial_terms: dict = {}             # stated in the TOR, e.g. requested_months


class Citation(BaseModel):
    page: int
    section: str = ""
    requirement_id: str = ""


class RecordRequirement(BaseModel):
    """One statement: its id is stable (hash of normalized text + document + section)."""
    section_role: str = ""                  # AI extraction: its section's role
    id: str
    text: str
    category: str
    citations: list[Citation] = []
    section_no: str = ""
    doc_key: str = "tor"
    doc_kind: str = "tor"
    doc_label: str = "TOR"
    doc_date: str = ""
    file: str = ""
    area: str = ""
    superseded_by: str = ""                 # e.g. "Bid Bulletin No. 01, 2027-02-15"
    home_section: str = ""                  # the TOR subsection a supplementary statement applies to


class RecordFeature(BaseModel):
    id: str
    epic: str
    name: str
    extracted_name: str = ""
    description: str
    type: str
    priority: Literal["Must", "Should", "Should (pending client confirmation)", "Could", "Won't"]
    complexity: Literal["low", "medium", "high"]
    size: Literal["S", "M", "L", "XL"] | None = None
    route: str | None = None
    route_reason: str = ""
    statement_ids: list[str] = []           # the statements this BR is built from
    scope_status: Literal["in_scope", "suggested"] = "in_scope"   # suggested: critic-added, not priced until a BA accepts it
    superseded_ids: list[str] = []
    origin: Literal["tor", "critic"] = "tor"
    justification: str = ""
    sources: list[str] = []
    assumptions: list[str] = []
    citations: list[Citation] = []
    needs_manual_review: bool = False


class Question(BaseModel):
    text: str
    source: str
    pages: list[int] = []
    statement_ids: list[str] = []
    dismissed: bool = False


class Locator(BaseModel):
    """An exact TOR passage: file, page, section and a quote of at most 25 words."""
    file: str
    page: int
    section: str = ""
    quote: str
    doc_label: str = "TOR"                  # document type, number and date


class BrdItem(BaseModel):
    """ACT, BRULE, ENT, NFR, CON, ASM or Q entry. unsupported: no exact locator backs it."""
    id: str
    text: str
    description: str = ""
    kind: str = ""                          # rule kind, constraint area or question source
    attributes: list[str] = []              # ENT: key attributes the TOR mentions
    measurable: bool | None = None          # NFR: False is flagged "unmeasurable"
    conflict_question_id: str = ""          # BRULE: disputed by this open conflict question
    statement_id: str = ""
    superseded_by: str = ""                 # stays visible, no longer effective
    pages: list[int] = []                   # Q: pages of the question
    citations: list[Locator] = []
    unsupported: bool = False
    br_ids: list[str] = []


class BusinessRequirement(BaseModel):
    """One BR card per in-scope feature. Priority, complexity, size and hours are read from the feature and estimate."""
    id: str
    feature_id: str
    title: str
    epic: str
    statement: str
    statement_ids: list[str] = []
    capabilities: list[str] = []            # the BR's capability statements; its rule statements are under the rules
    scope_status: str = "in_scope"
    context: list[str] = []                 # pure facts and volumes
    superseded: list[dict] = []             # {"text", "by", "page"}: superseded statements stay visible
    conflict_question_id: str = ""          # open conflict: neutral title and statement, disputed values kept out
    rule_ids: list[str] = []
    entity_ids: list[str] = []
    nfr_ids: list[str] = []
    assumption_ids: list[str] = []
    question_ids: list[str] = []
    citations: list[Locator] = []
    unsupported: bool = False
    warnings: list[str] = []
    activity_keys: list[str] = []           # traceability: "<feature_id>|<Role>.<activity>"
    proposal_lines: list[str] = []          # traceability: filled by the proposal (Phase 3)


class Brd(BaseModel):
    version: str = ""
    requirements: list[BusinessRequirement] = []
    glossary: list[str] = []
    rules: list[BrdItem] = []
    entities: list[BrdItem] = []
    nfrs: list[BrdItem] = []
    constraints: list[BrdItem] = []
    assumptions: list[BrdItem] = []
    questions: list[BrdItem] = []


class Costing(BaseModel):
    rates_source: Literal["illustrative", "uploaded"] = "illustrative"
    rates_hash: str = ""                    # hash of the active rate table; never the values
    scenarios: dict = {}                    # filled in Phase 2 (cost lines, price, margin, VAT, milestones)


class BrdApproval(BaseModel):
    approved: bool = False
    approver: str = ""
    approved_at: str = ""


class UploadConfirmation(BaseModel):
    """Who confirmed that the upload holds no confidential client documents, and when. Never the content."""
    confirmed_by: str = ""
    confirmed_at: str = ""
    files: int = 0


class Approval(BaseModel):
    approved: bool = False
    approver: str = ""
    approved_at: str = ""
    scenario: Literal["B", "C"] | None = None
    record_version: int | None = None
    rates_source: str = ""
    risk_acknowledgements: list[str] = []
    productivity_profile: str = ""
    tooling_profile: str = ""
    consent_by: str = ""                    # client or legal consent, when a risk requires it
    consent_date: str = ""


class Spend(BaseModel):
    est_usd: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    from_cache: bool = False
    runtime_s: float = 0.0


class OpportunityRecord(BaseModel):
    routed: dict = {}                       # AI extraction: background, objectives, scope, bid procedure, timeline, checker report
    schema_version: str = SCHEMA_VERSION
    version: int = 1
    status: Status = "EXTRACTED"
    created_at: str = ""
    change_note: str = ""
    opportunity: Opportunity
    requirements: list[RecordRequirement] = []
    features: list[RecordFeature] = []
    questions: list[Question] = []
    assumptions: list[str] = []
    exclusions: list[dict] = []
    guardrails: dict = {}                   # counts and kinds only (no TOR text)
    critic_notes: list[str] = []
    decisions_reapplied: int = 0
    brd_approval: BrdApproval = BrdApproval()
    upload_confirmation: UploadConfirmation = UploadConfirmation()
    pending_change: dict = {}               # a re-process after BRD approval: {"version", "changes"}, reviewed before applying
    brd: Brd = Brd()                        # BRD-level requirements with exact citations and traceability links
    ai_suitability: dict = {}               # "<feature_id>|<Role>.<activity>" -> category, reason, factors
    activities: list[dict] = []             # activity rows: baseline, B and C components (hours only)
    estimates: dict = {}                    # baseline (reference), B, C, C_matched; reductions
    timeline: dict = {}                     # phase timeline per scenario
    costing: Costing = Costing()
    risks: list[dict] = []                  # triggered rules; the agent explains them, it never approves
    recommendation: dict = {}               # agent's go / go with conditions / no-go; the human decides
    approval: Approval = Approval()
    documents: list[dict] = []              # draft proposal sections (client-safe) and check results
    run_trace: dict = {}                    # agent steps and tool calls of the run; no PII, no rate values
    assumption_overrides: dict = {}
    spend: Spend = Spend()


# ---------- storage ----------

# Uploaded TORs live in this process's memory only: never on disk.
_memory: dict[str, dict[int, str]] = {}
_memory_runs: dict[str, list[dict]] = {}


def _in_memory(opp_id: str) -> bool:
    return opp_id.startswith(UPLOAD_PREFIX)


_memory_lock = threading.RLock()


@contextmanager
def locked(opp_id: str):
    """One writer at a time per opportunity: a file lock on disk (it also holds between threads of one process), a process
    lock for uploads, which live in memory."""
    if _in_memory(opp_id):
        with _memory_lock:
            yield
        return
    d = _dir(opp_id)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / ".lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def atomic_write(path, text: str) -> None:
    """Write to a temp file, then rename: a reader sees the old file or the new one, never part of one."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def runs_path(opp_id: str):
    return _dir(opp_id) / "runs.json"


def add_run(opp_id: str, entry: dict) -> None:
    """Index of processing runs (version, time, cache hits and misses, cost), read by the Trace and Evidence tabs."""
    with locked(opp_id):
        if _in_memory(opp_id):
            _memory_runs.setdefault(opp_id, []).append(entry)
            return
        atomic_write(runs_path(opp_id), json.dumps(runs(opp_id) + [entry], indent=0))


def runs(opp_id: str) -> list[dict]:
    if _in_memory(opp_id):
        return list(_memory_runs.get(opp_id, []))
    path = runs_path(opp_id)
    return json.loads(path.read_text()) if path.exists() else []


def step_unlocked(step: int, status: str | None) -> bool:
    """Steps 2-4 are viewable once a TOR is processed; only actions are gated. Approval and proposal follow the status."""
    if step == 1:
        return True
    if status is None or step == 7:
        return False
    if step in (2, 3, 4):
        return True
    return STATUSES.index(status) >= STATUSES.index({5: "RISK_REVIEWED", 6: "APPROVED"}[step])


def advance(rec: "OpportunityRecord", target: str) -> "OpportunityRecord":
    """Allowed transitions only: one step forward along STATUSES. Going back happens only through an edit (save)."""
    if STATUSES.index(target) != STATUSES.index(rec.status) + 1:
        raise ValueError(f"Status cannot go from {rec.status} to {target}.")
    return rec.model_copy(update={"status": target})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dir(opp_id: str):
    if not re.fullmatch(r"[a-z0-9-]{1,64}", opp_id):
        raise ValueError("invalid opportunity id")
    return DATA_DIR / opp_id


def versions(opp_id: str) -> list[int]:
    if _in_memory(opp_id):
        return sorted(_memory.get(opp_id, {}))
    d = _dir(opp_id)
    return sorted(int(p.stem[1:]) for p in d.glob("v*.json")) if d.exists() else []


def load(opp_id: str, version: int | None = None) -> OpportunityRecord | None:
    vs = versions(opp_id)
    if not vs:
        return None
    v = version or vs[-1]
    if _in_memory(opp_id):
        return OpportunityRecord.model_validate_json(_memory[opp_id][v])
    return OpportunityRecord.model_validate_json((_dir(opp_id) / f"v{v}.json").read_text())


def save(record: OpportunityRecord, note: str) -> OpportunityRecord:
    """Write a NEW version (never overwrite). Any change after approval clears the approval: the new version
    needs re-approval."""
    new = record.model_copy(deep=True)
    new.created_at = _now()
    new.change_note = note
    if not note.startswith(WORKFLOW_NOTES):
        new.approval = Approval()
        new.documents = []
        if STATUSES.index(new.status) > STATUSES.index("ESTIMATED"):
            new.status = "ESTIMATED"    # an edit needs a new risk review and approval
    with locked(record.opportunity.id):     # the version number is taken and written under one lock
        vs = versions(record.opportunity.id)
        new.version = (vs[-1] + 1) if vs else 1
        if _in_memory(record.opportunity.id):
            _memory.setdefault(record.opportunity.id, {})[new.version] = new.model_dump_json()
        else:
            atomic_write(_dir(record.opportunity.id) / f"v{new.version}.json", new.model_dump_json(indent=1))
    return new
    d = _dir(record.opportunity.id)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"v{new.version}.json").write_text(new.model_dump_json(indent=1))
    return new


"""Opportunities and intake (POC v3 W3). Everything is saved in the database the moment it happens, so nothing depends on the
browser session or the container disk. Long AI runs finish in a server-side thread and record their progress in runs."""
from __future__ import annotations

import hashlib
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session

from app import ai_extract, failures, legacy_import, llm, models, store, tracing, workflow
from app.config import env
from app.db import session_scope
from app.logging_setup import log_event
from app.workflow import Actor

DOC_TYPES = {"tor": "TOR", "rfp": "RFP", "bid_bulletin": "Bid bulletin", "minutes": "Minutes", "clarification": "Clarification", "other": "Other"}
PIPELINE_KIND = {"TOR": "tor", "RFP": "tor", "Bid bulletin": "bid_bulletin", "Minutes": "minutes", "Clarification": "clarification", "Other": "other"}
PRIMARY_TYPES = ("TOR", "RFP")
COMMERCIAL_FIELDS = ("requested_months", "upfront_pct", "final_pct", "warranty_months", "payment_terms", "budget_note", "notes")
INSTANCE = uuid.uuid4().hex      # a run left Running by another instance died with its container
RUN_PROCESS = "process_documents"
MIN_READABLE_WORDS = 40     # below this a file is a scan or an empty page, and the LLM is not called
_SPEND_REFUSAL = re.compile(r"budget|spend|\bcap\b|\blimit\b", re.I)
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="deal-run")


class DuplicateDocument(Exception):
    pass


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "opportunity"


def _free_code(session: Session, code: str) -> str:
    """Codes are unique: a second HRIS demo becomes HRIS-02."""
    code = re.sub(r"[^A-Z0-9-]+", "-", code.upper()).strip("-") or "OPP"
    taken = set(session.scalars(select(models.Opportunity.code)))
    if code not in taken:
        return code
    n = 2
    while f"{code}-{n:02d}" in taken:
        n += 1
    return f"{code}-{n:02d}"


def create(session: Session, actor: Actor, client_name: str, name: str, code: str, submission_deadline=None, clarification_deadline=None,
           commercial: dict | None = None) -> models.Opportunity:
    workflow.require(actor, workflow.ACT_MANAGE_OPPORTUNITY)
    if not (client_name.strip() and name.strip() and code.strip()):
        raise ValueError("Client name, project name and code are required.")
    code = _free_code(session, code)
    row = models.Opportunity(id=f"{_slug(code)}-{uuid.uuid4().hex[:6]}", code=code, name=name.strip(), client_name=client_name.strip(), bdm_name=actor.name,
                             submission_deadline=submission_deadline, clarification_deadline=clarification_deadline, commercial=commercial or {},
                             status=workflow.DRAFT)
    session.add(row)
    session.flush()
    store.audit(session, actor.name, actor.role, "opportunity.created", opportunity_id=row.id, entity_type="opportunity", entity_id=row.id,
                details={"code": code})
    return row


def update_intake(session: Session, actor: Actor, opp_id: str, expected_row_version: int, **changes) -> models.Opportunity:
    """Client and commercial details; a stale screen is refused."""
    workflow.require(actor, workflow.ACT_MANAGE_OPPORTUNITY)
    _refuse_if_closed(session, opp_id)
    row = store.update_checked(session, models.Opportunity, opp_id, expected_row_version, **changes)
    store.audit(session, actor.name, actor.role, "opportunity.updated", opportunity_id=opp_id, entity_type="opportunity", entity_id=opp_id,
                details={"fields": sorted(changes)})
    return row


def _refuse_if_closed(session: Session, opp_id: str) -> None:
    if session.get(models.Opportunity, opp_id).status == workflow.CLOSED:
        raise workflow.InvalidTransition("This opportunity is closed: it can be viewed, not changed.")


def documents(session: Session, opp_id: str) -> list[models.Document]:
    return session.scalars(select(models.Document).where(models.Document.opportunity_id == opp_id).order_by(models.Document.id)).all()


def add_document(session: Session, actor: Actor, opp_id: str, doc, doc_type: str) -> models.Document:
    """doc: a DocInput, parsed and PII-masked at once; the raw file is never kept. Adding the first TOR or RFP submits the
    inputs. A supplement after the BRD exists is handled by app.supplement, never by regenerating the BRD."""
    workflow.require(actor, workflow.ACT_MANAGE_OPPORTUNITY)
    _refuse_if_closed(session, opp_id)
    sha = doc.fingerprint
    if session.scalar(select(models.Document.id).where(models.Document.opportunity_id == opp_id, models.Document.sha256 == sha)):
        raise DuplicateDocument(f"{doc.name} is already in this opportunity.")
    row = models.Document(opportunity_id=opp_id, doc_key=doc.key, doc_type=doc_type, reference_no=doc.number or None, doc_date=doc.date or None,
                          filename=doc.name, sha256=sha, masked_pages=doc.pages, status="received", uploaded_by=actor.name, uploaded_role=actor.role)
    session.add(row)
    session.flush()
    store.audit(session, actor.name, actor.role, "document.uploaded", opportunity_id=opp_id, entity_type="document", entity_id=str(row.id),
                details={"type": doc_type, "pages": len(doc.pages), "pii_masked": doc.pii_masked})
    opp = session.get(models.Opportunity, opp_id)
    if opp.status == workflow.DRAFT and doc_type in PRIMARY_TYPES:
        workflow.transition(session, opp_id, workflow.INPUTS, actor)
    from app import publishing   # imports this module
    publishing.publish_input(session, actor, opp_id, row)
    return row


def record_llm(session: Session, opp_id: str, kind: str, usage, detail: dict | None = None) -> None:
    """A finished LLM step on the run record, so what an opportunity spent can be read back from the database."""
    totals = usage.totals()
    if not totals["est_usd"]:
        return
    now = datetime.now(timezone.utc)
    session.add(models.Run(opportunity_id=opp_id, kind=kind, status="Succeeded", started_at=now, finished_at=now, cost_usd=totals["est_usd"], prompt_tokens=int(totals["prompt_tokens"]), completion_tokens=int(totals["completion_tokens"]), detail=detail or {}))
    session.flush()


def to_doc_input(row: models.Document):
    from app.pipeline import DocInput
    return DocInput(name=row.filename, kind=PIPELINE_KIND[row.doc_type], pages=row.masked_pages, date=row.doc_date or "", number=row.reference_no or "",
                    bundled=row.doc_key == "tor")


# ---------- processing (server-side) ----------

def start_processing(session: Session, actor: Actor, opp_id: str) -> int:
    """Records the run; the caller submits it after the commit."""
    workflow.require(actor, workflow.ACT_MANAGE_OPPORTUNITY)
    opp = session.get(models.Opportunity, opp_id)
    if opp.status != workflow.INPUTS:
        raise workflow.InvalidTransition(f"The BRD can only be generated from {workflow.INPUTS}.")
    store.advisory_lock(session, f"{opp_id}|{RUN_PROCESS}")     # two rapid clicks: the second waits here, then sees the first
    running = session.scalar(select(models.Run).where(models.Run.opportunity_id == opp_id, models.Run.status == "Running", models.Run.kind == RUN_PROCESS))
    if running:
        raise workflow.InvalidTransition(f"Already running, started at {running.started_at.astimezone(timezone.utc):%H:%M:%S} UTC by {running.detail.get('started_by', 'someone')}. Please wait for it to finish.")
    run = models.Run(opportunity_id=opp_id, kind=RUN_PROCESS, status="Running", detail={"instance": INSTANCE, "started_by": actor.name})
    session.add(run)
    session.flush()
    store.audit(session, actor.name, actor.role, "run.started", opportunity_id=opp_id, entity_type="run", entity_id=str(run.id))
    return run.id


def submit(run_id: int, opp_id: str, bind: Engine | None = None, confirmation: dict | None = None, wait: bool = False) -> None:
    """Runs in a thread, so the run finishes if the browser closes. wait: run it here (tests and scripts)."""
    if wait:
        run_processing(run_id, opp_id, bind, confirmation)
    else:
        _executor.submit(run_processing, run_id, opp_id, bind, confirmation)


def run_processing(run_id: int, opp_id: str, bind: Engine | None = None, confirmation: dict | None = None) -> None:
    """All or nothing: the BRD, the status change and the run's result commit together. A failure leaves the inputs as they
    were, with the run marked Failed and a retry offered."""
    with tracing.context(run_id=run_id, opportunity_id=opp_id, document_set=env("TRACE_DOCUMENT_SET", "app")), tracing.span("process_documents", inputs={"opportunity_id": opp_id, "run_id": run_id}):
        _run_processing(run_id, opp_id, bind, confirmation)
    tracing.flush()


def _run_processing(run_id: int, opp_id: str, bind: Engine | None, confirmation: dict | None) -> None:
    stage, rec = "reading the documents", None
    try:
        with session_scope(bind) as session:
            docs = documents(session, opp_id)
            inputs = [to_doc_input(d) for d in docs]
            opp = session.get(models.Opportunity, opp_id)
            client, project = opp.client_name, opp.name
            read_pages = [p for d in docs for p in d.masked_pages]
            words = sum(len(p["text"].split()) for p in read_pages)
        if words < MIN_READABLE_WORDS:
            raise DocumentRejected("Too little readable text.", "unreadable")
        stage = "extracting the requirements with AI"
        rec, state = _pipeline(inputs, opp_id, client, project, confirmation, bind)
        if not state.features:
            reason = " ".join(state.errors)
            raise DocumentRejected(reason or "The document does not look like a software TOR.", "ai_unavailable" if "AI service" in reason else "cost_cap" if _SPEND_REFUSAL.search(reason) else "not_a_tor" if not state.is_tor else "no_requirements")
        stage = "saving the BRD"
        with session_scope(bind) as session:
            counts = legacy_import.populate_brd(session, opp_id, rec, created_by=(workflow.SYSTEM_ACTOR.name, workflow.SYSTEM))
            opp = session.get(models.Opportunity, opp_id)
            opp.commercial = prefill_commercial(opp.commercial, {**rec.opportunity.commercial_terms, **_routed_terms(rec.routed)})
            _set_deadline(opp, read_pages)
            for row in documents(session, opp_id):
                row.status = "processed"
            session.flush()
            workflow.transition(session, opp_id, workflow.BRD_GENERATED, workflow.SYSTEM_ACTOR, details={"brs": counts["brs"]})
            run = session.get(models.Run, run_id)
            run.status, run.finished_at = "Succeeded", datetime.now(timezone.utc)
            if not rec.spend.from_cache:     # a cached result carries what its original run cost; this run spent nothing
                run.cost_usd, run.prompt_tokens, run.completion_tokens = rec.spend.est_usd, rec.spend.prompt_tokens, rec.spend.completion_tokens
            store.audit(session, workflow.SYSTEM_ACTOR.name, workflow.SYSTEM, "brd.generated", opportunity_id=opp_id, entity_type="BRD", version="0.1", details=counts)
            from app import publishing   # imports this module
            publishing.publish(session, workflow.SYSTEM_ACTOR, opp_id, current_version(session, opp_id, "BRD"))
    except (Exception, llm.BudgetExceeded) as e:  # noqa: BLE001 — recorded on the run; the user retries. The cap is a BaseException so that nodes cannot swallow it
        info = failures.detail(e, stage, e.category if isinstance(e, DocumentRejected) else None)
        log_event("run failed", node=RUN_PROCESS, error=info["error"], category=info["category"], stage=stage, constraint=info.get("constraint"), run=failures.reference(run_id))
        with session_scope(bind) as session:
            run = session.get(models.Run, run_id)
            run.status, run.finished_at = "Failed", datetime.now(timezone.utc)
            run.detail = {**run.detail, **info}
            if rec is not None and not rec.spend.from_cache:     # the money was spent even though the result was not saved
                run.cost_usd, run.prompt_tokens, run.completion_tokens = rec.spend.est_usd, rec.spend.prompt_tokens, rec.spend.completion_tokens
            store.audit(session, workflow.SYSTEM_ACTOR.name, workflow.SYSTEM, "run.failed", opportunity_id=opp_id, entity_type="run", entity_id=str(run_id),
                        details={"error": info["error"], "category": info["category"]})


class DocumentRejected(Exception):
    def __init__(self, message: str, category: str = "not_a_tor"):
        super().__init__(message)
        self.category = category


def _routed_terms(routed: dict) -> dict:
    """What AI extraction found for the opportunity record. Bid procedure never becomes a BR."""
    out = {}
    if routed.get("bid_procedure"):
        out["bid_procedure"] = routed["bid_procedure"]
    weeks = ai_extract.requested_weeks(routed.get("timeline", []))
    if weeks:
        out["requested_weeks"] = weeks
        out["requested_months"] = max(1, round(weeks / 4.345))
    return out


def _set_deadline(opp: models.Opportunity, pages: list[dict]) -> None:
    found = ai_extract.deadline_of(pages)
    if found and not opp.submission_deadline:
        try:
            opp.submission_deadline = datetime.strptime(found.replace(",", ""), "%B %d %Y").date()
        except ValueError:
            pass


def _save_extraction(opp_id: str, bind: Engine | None):
    """A section's answer is kept as soon as it arrives, in its own transaction, so a run that fails later still has it."""
    def save(key: str, items: list) -> None:
        with session_scope(bind) as session:
            store.advisory_lock(session, f"{opp_id}|extraction")     # sections finish in parallel: each must see the others' answers
            opp = session.get(models.Opportunity, opp_id, populate_existing=True)
            pipeline_state = dict(opp.pipeline or {})
            pipeline_state["extraction"] = {**pipeline_state.get("extraction", {}), key: items}
            opp.pipeline = pipeline_state
    return save


def _pipeline(inputs: list, opp_id: str, client: str, project: str, confirmation: dict | None, bind: Engine | None = None):
    """The existing pipeline. The bundled sample is served from its stored result (0 Azure calls). An upload stays in memory, and its extraction is stored on the opportunity:
    an unchanged document is never extracted again."""
    from app import pipeline
    if all(d.bundled for d in inputs):
        rec, state, _ = pipeline.process(inputs, pipeline.HRIS_META)
        return rec, state
    with session_scope(bind) as session:
        stored = dict((session.get(models.Opportunity, opp_id).pipeline or {}).get("extraction", {}))
    with ai_extract.persistence(stored, _save_extraction(opp_id, bind)):
        rec, state, _ = pipeline.process_upload(inputs, pipeline.upload_meta(inputs, client, project), confirmation=confirmation)
    return rec, state


def prefill_commercial(current: dict, from_tor: dict) -> dict:
    """What the TOR states fills the empty fields only; the BDM confirms or edits it. Their own values are never overwritten."""
    merged = dict(current)
    filled = []
    for key, value in (from_tor or {}).items():
        if merged.get(key) in (None, "", []):
            merged[key] = value
            filled.append(key)
    if filled:
        merged["from_tor"] = sorted(set(merged.get("from_tor", [])) | set(filled))
        merged["confirmed"] = False
    return merged


def fail_orphaned_runs(bind: Engine | None = None) -> int:
    """A run or an action left Running by a container that no longer exists will never finish. Called once at app start."""
    with session_scope(bind) as session:
        rows = session.scalars(select(models.Run).where(models.Run.status == "Running")).all()
        orphaned = [r for r in rows if r.detail.get("instance") != INSTANCE]
        for run in orphaned:
            run.status, run.finished_at = "Failed", datetime.now(timezone.utc)
            run.detail = {**run.detail, "error": "ContainerRestarted", "category": "restarted", "message": "The app restarted during this run. Nothing was changed."}
            store.audit(session, workflow.SYSTEM_ACTOR.name, workflow.SYSTEM, "run.failed", opportunity_id=run.opportunity_id, entity_type="run",
                        entity_id=str(run.id), details={"error": "ContainerRestarted"})
        return len(orphaned)


def latest_run(session: Session, opp_id: str) -> models.Run | None:
    return session.scalars(select(models.Run).where(models.Run.opportunity_id == opp_id, models.Run.kind == RUN_PROCESS).order_by(models.Run.id.desc())).first()


def create_sample_hris(session: Session, actor: Actor) -> str:
    """A fresh HRIS opportunity from the bundled sample, so demos never collide. Returns its id; start_processing comes next."""
    from app.pipeline import HRIS_META, hris_docs
    opp = create(session, actor, HRIS_META["client"], HRIS_META["project_name"], "HRIS")
    for doc in hris_docs(False):
        add_document(session, actor, opp.id, doc, "TOR")
    return opp.id


# ---------- continuity ----------

def review_progress(session: Session, opp_id: str) -> tuple[int, int]:
    """(reviewed, total) over the active BRs."""
    rows = session.execute(select(models.BrReview.status).join(models.Br, (models.Br.opportunity_id == models.BrReview.opportunity_id)
                                                               & (models.Br.br_id == models.BrReview.br_id))
                           .where(models.BrReview.opportunity_id == opp_id, models.Br.lifecycle_status == "Active")).scalars().all()
    return sum(s != "Pending" for s in rows), len(rows)


def current_version(session: Session, opp_id: str, artifact: str) -> models.ArtifactVersion | None:
    return session.scalars(select(models.ArtifactVersion).where(models.ArtifactVersion.opportunity_id == opp_id, models.ArtifactVersion.artifact_type == artifact)
                           .order_by(models.ArtifactVersion.version_major.desc(), models.ArtifactVersion.version_minor.desc())).first()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def supersede(session: Session, opp_id: str, artifact: str) -> None:
    for row in session.scalars(select(models.ArtifactVersion).where(models.ArtifactVersion.opportunity_id == opp_id, models.ArtifactVersion.artifact_type == artifact,
                                                                    models.ArtifactVersion.status != "Superseded")):
        row.status = "Superseded"
    session.flush()


def last_action(session: Session, opp_id: str) -> models.AuditLog | None:
    return session.scalars(select(models.AuditLog).where(models.AuditLog.opportunity_id == opp_id).order_by(models.AuditLog.id.desc())).first()


def where_you_left_off(session: Session, opp_id: str) -> dict:
    """Status, last action, and what is pending for which role."""
    opp = session.get(models.Opportunity, opp_id)
    brd = current_version(session, opp_id, "BRD")
    reviewed, total = review_progress(session, opp_id)
    last = last_action(session, opp_id)
    pending = workflow.NEXT_ACTION[opp.status]
    if brd and opp.status in (workflow.BRD_GENERATED, workflow.BRD_VALIDATION):
        pending = f"BRD v{brd.version_label} — {reviewed} of {total} BRs reviewed"
    next_role = workflow.NEXT_ROLE[opp.status]
    if opp.status == workflow.PROPOSAL_REVIEW:
        proposal = current_version(session, opp_id, "PROPOSAL")
        if proposal is not None and proposal.status == "Approved":
            pending, next_role = "Mark the proposal as submitted", workflow.BDM
    outdated = session.scalars(select(models.ArtifactVersion.artifact_type).where(models.ArtifactVersion.opportunity_id == opp_id, models.ArtifactVersion.outdated, models.ArtifactVersion.status != "Superseded")).unique().all()
    return {"status": opp.status, "pending": pending, "next_role": next_role, "row_version": opp.row_version,
            "last_action": last.action if last else None, "last_by": f"{last.actor_name} ({last.actor_role})" if last else None,
            "last_at": last.occurred_at if last else None, "reviewed": reviewed, "total": total, "outdated": list(outdated),
            "brd_version": brd.version_label if brd else None}


def list_view(session: Session) -> list[dict]:
    """The opportunities screen."""
    rows = []
    for opp in session.scalars(select(models.Opportunity).order_by(models.Opportunity.updated_at.desc())):
        state = where_you_left_off(session, opp.id)
        rows.append({"id": opp.id, "code": opp.code, "client": opp.client_name, "name": opp.name, "status": opp.status, "bdm": opp.bdm_name or "",
                     "updated": opp.updated_at, "next_action": state["pending"], "next_role": state["next_role"] or "", "outdated": ", ".join(state["outdated"])})
    return rows


def save_draft(session: Session, actor: Actor, opp_id: str, artifact: str, pending: dict) -> None:
    """Auto-saved pending edits per user: not a version, and no audit entry until they are saved."""
    if not pending:
        clear_draft(session, actor, opp_id, artifact)
        return
    row = session.scalars(select(models.EditDraft).where(models.EditDraft.opportunity_id == opp_id, models.EditDraft.user_name == actor.name,
                                                         models.EditDraft.artifact_type == artifact)).first()
    if row is None:
        session.add(models.EditDraft(opportunity_id=opp_id, user_name=actor.name, artifact_type=artifact, pending=pending))
    elif row.pending != pending:
        row.pending = pending
    session.flush()


def load_draft(session: Session, actor: Actor, opp_id: str, artifact: str) -> dict:
    row = session.scalars(select(models.EditDraft).where(models.EditDraft.opportunity_id == opp_id, models.EditDraft.user_name == actor.name,
                                                         models.EditDraft.artifact_type == artifact)).first()
    return row.pending if row else {}


def clear_draft(session: Session, actor: Actor, opp_id: str, artifact: str) -> None:
    session.execute(delete(models.EditDraft).where(models.EditDraft.opportunity_id == opp_id, models.EditDraft.user_name == actor.name,
                                                   models.EditDraft.artifact_type == artifact))

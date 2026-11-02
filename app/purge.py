"""Deleting an opportunity completely, for test data. Admin only, confirmed by typing its code, refused during a run and for the protected codes in
config/admin.yaml. Drive, traces and cached files go first, inside the database transaction that locks the opportunity: if any of them fails, the rows stay,
so the delete can simply be tried again. The rows go last, through the purge_opportunity database function, the only way the app role can remove audit entries."""
from __future__ import annotations

import hashlib
import shutil
from datetime import datetime, timezone

from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from app import action_guard, doc_cache, models, record, storage, store, tracing, workflow
from app.config import load_yaml
from app.db import session_scope
from app.logging_setup import log_event
from app.workflow import Actor


class PurgeRefused(ValueError):
    """Why this opportunity cannot be deleted now. Nothing was removed."""


def protected_codes() -> set[str]:
    return {c.upper() for c in load_yaml("admin.yaml")["protected_codes"]}


def is_protected(code: str) -> bool:
    return code.upper() in protected_codes()


def _running(session: Session, opp_id: str) -> bool:
    """A processing run, or an action not yet old enough to count as left behind. Runs of a dead container are closed at app start."""
    now = datetime.now(timezone.utc)
    rows = session.scalars(select(models.Run).where(models.Run.opportunity_id == opp_id, models.Run.status == "Running"))
    return any(not r.kind.startswith(action_guard.ACTION_PREFIX) or now - r.started_at < action_guard.STALE_AFTER for r in rows)


def _cached_files(opp_id: str) -> int:
    """Files under data/opportunities/<id>/. Only ever a direct child of that folder."""
    folder = (record.DATA_DIR / opp_id).resolve()
    if folder.parent != record.DATA_DIR.resolve() or not folder.is_dir():
        return 0
    count = sum(1 for p in folder.rglob("*") if p.is_file())
    shutil.rmtree(folder)
    return count


def _audit_reference(opp_id: str) -> str:
    """Enough to tell two purges apart, and nothing that names what was purged."""
    return hashlib.sha256(opp_id.encode()).hexdigest()[:12]


def purge(actor: Actor, opp_id: str, typed_code: str, bind: Engine | None = None) -> dict:
    """{"code", "database": {table: rows}, "database_rows", "drive", "traces", "cached_files"}, or raises NotAllowed or PurgeRefused with nothing removed."""
    with session_scope(bind) as session:
        workflow.require_at_a_gate(session, actor, workflow.ACT_ADMIN, opp_id)
        # locked: a run cannot start now, because its row needs this one
        opp = session.scalars(select(models.Opportunity).where(models.Opportunity.id == opp_id).with_for_update()).one_or_none()
        if opp is None:
            raise PurgeRefused("This opportunity no longer exists.")
        code = opp.code
        if is_protected(code):
            raise PurgeRefused(f"{code} is protected and cannot be deleted.")
        if (typed_code or "").strip() != code:
            raise PurgeRefused(f"The code you typed does not match {code}. Nothing was removed.")
        if _running(session, opp_id):
            raise PurgeRefused(f"A run is in progress for {code}. Try again when it has finished.")
        try:
            drive = storage.get_store().purge_folders(code)
        except (storage.AuthExpired, storage.Unavailable) as e:
            raise PurgeRefused(f"Drive could not be reached, so nothing was removed from the database: {e}") from e
        try:
            traces = tracing.delete_traces(opp_id)
        except Exception as e:  # noqa: BLE001 — any failure of the trace service
            log_event("trace delete failed", node="purge", error=type(e).__name__)
            raise PurgeRefused("LangSmith could not delete the traces, so nothing was removed from the database. Try again.") from e
        cached = _cached_files(opp_id)
        session.expunge_all()
        counts = session.execute(text("SELECT purge_opportunity(:id)"), {"id": opp_id}).scalar_one()
        # the entries are gone by design. One line says a purge happened and who ran it, without the code or name
        store.audit(session, actor.name, actor.role, "opportunity.purged", entity_type="purge", entity_id=_audit_reference(opp_id),
                    details={"rows": sum(counts.values()), "drive_files": drive["files"], "traces": traces["traces"], "cached_files": cached})
    doc_cache.clear()     # keyed by content, so this is the only way to be sure none of its documents stays in memory
    return {"code": code, "database": counts, "database_rows": sum(counts.values()), "drive": drive, "traces": traces, "cached_files": cached}


def report_text(report: dict) -> str:
    rows = ", ".join(f"{table} {n}" for table, n in sorted(report["database"].items()) if n)
    traces = report["traces"]
    trace_line = {"not_configured": "LangSmith is not configured here, so no traces were checked",
                  "deleted": f"{traces['traces']} LangSmith traces requested for deletion (LangSmith finishes this within about 6 hours)"}[traces["status"]]
    return (f"{report['code']} deleted. Database: {report['database_rows']} rows ({rows}). "
            f"Drive: {report['drive']['folders']} folders and {report['drive']['files']} files permanently deleted. {trace_line}. Cached files: {report['cached_files']}.")



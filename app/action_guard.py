"""A second identical action on an opportunity is refused while the first runs, and a repeat of one that already finished from the same screen is refused too.
So a double-click can never start two LLM runs or create two versions. The record is a row in runs; the lock is held only while checking, so a crash leaves nothing stuck."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import Engine, select

from app import models, opportunities, store
from app.db import session_scope

ACTION_PREFIX = "action:"
STALE_AFTER = timedelta(minutes=15)     # nothing here takes this long: a row this old was left behind


class AlreadyRunning(ValueError):
    pass


class AlreadyDone(ValueError):
    pass


def _clock(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%H:%M:%S UTC")


def begin(opp_id: str, action: str, actor_name: str, token: str | None = None, bind: Engine | None = None) -> int:
    """Records the action as running, in its own transaction, or raises. token: identifies the screen the action came from."""
    kind = ACTION_PREFIX + action
    now = datetime.now(timezone.utc)
    with session_scope(bind) as session:
        store.advisory_lock(session, f"{opp_id}|{kind}")
        rows = session.scalars(select(models.Run).where(models.Run.opportunity_id == opp_id, models.Run.kind == kind).order_by(models.Run.id.desc()).limit(20)).all()
        for row in rows:
            if row.status != "Running":
                continue
            if now - row.started_at < STALE_AFTER:
                raise AlreadyRunning(f"Already running, started at {_clock(row.started_at)} by {row.detail.get('started_by', 'someone')}. Please wait for it to finish.")
            row.status, row.finished_at = "Failed", now
        if token:
            done = next((r for r in rows if r.status == "Succeeded" and r.detail.get("token") == token), None)
            if done is not None:
                raise AlreadyDone(f"Already done at {_clock(done.finished_at)}. Nothing was repeated.")
        run = models.Run(opportunity_id=opp_id, kind=kind, status="Running", started_at=now, detail={"instance": opportunities.INSTANCE, "started_by": actor_name, "token": token})
        session.add(run)
        session.flush()
        return run.id


def finish(run_id: int, ok: bool, bind: Engine | None = None) -> None:
    with session_scope(bind) as session:
        run = session.get(models.Run, run_id)
        run.status, run.finished_at = "Succeeded" if ok else "Failed", datetime.now(timezone.utc)


def guarded(opp_id: str, action: str, actor_name: str, token: str | None, work, bind: Engine | None = None):
    """Runs work once, or raises AlreadyRunning or AlreadyDone without running it."""
    run_id = begin(opp_id, action, actor_name, token, bind)
    try:
        result = work()
    except BaseException:
        finish(run_id, False, bind)
        raise
    finish(run_id, True, bind)
    return result

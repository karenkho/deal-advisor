"""A double-click can never run work twice. A failed run says why, what to do next and where to read more. Every proposal version reaches the store."""
from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from docx import Document
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import action_guard, failures, legacy_import, llm, models, opportunities as ops, proposal_service as prop  # noqa: E402
from app.db import session_scope  # noqa: E402
from tests.test_estimate_proposal import BDM, LEGAL, _approved_estimate, _proposal, _status, db  # noqa: E402,F401
from app import workflow as wf  # noqa: E402


def _opportunity(db) -> str:
    with session_scope(db) as s:
        return ops.create_sample_hris(s, BDM)


def _together(calls: list) -> list:
    """Starts every call at the same moment. Returns each one's result or the error it raised."""
    gate, out = threading.Barrier(len(calls)), [None] * len(calls)

    def run(i, call):
        gate.wait()
        try:
            out[i] = call()
        except Exception as e:  # noqa: BLE001 — the test reads it
            out[i] = e
    threads = [threading.Thread(target=run, args=(i, c)) for i, c in enumerate(calls)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return out


# ---------- the server's guard ----------

def test_two_rapid_identical_requests_do_the_work_once(db):
    opp_id = _opportunity(db)
    release, started, work = threading.Event(), threading.Event(), []

    def slow():
        work.append(1)
        started.set()
        release.wait(10)
        return "done"

    def second():
        started.wait(10)
        try:
            return action_guard.guarded(opp_id, "generate_proposal", "Bea", "screen-1", slow, db)
        finally:
            release.set()
    results = _together([lambda: action_guard.guarded(opp_id, "generate_proposal", "Bea", "screen-1", slow, db), second])
    assert work == [1] and "done" in results
    refused = next(r for r in results if isinstance(r, action_guard.AlreadyRunning))
    assert "Already running, started at" in str(refused) and "Bea" in str(refused)


def test_many_simultaneous_requests_still_do_the_work_once(db):
    opp_id = _opportunity(db)
    release, work = threading.Event(), []

    def slow():
        work.append(1)
        release.wait(3)
    timer = threading.Timer(1.5, release.set)
    timer.start()
    results = _together([lambda: action_guard.guarded(opp_id, "approve", "Bea", "t", slow, db)] * 6)
    assert len(work) == 1 and sum(isinstance(r, action_guard.AlreadyRunning) for r in results) == 5


def test_a_repeat_from_the_same_screen_is_refused_after_the_first_has_finished(db):
    opp_id = _opportunity(db)
    work = []
    action_guard.guarded(opp_id, "save", "Bea", "audit-1", lambda: work.append(1), db)
    with pytest.raises(action_guard.AlreadyDone, match="Nothing was repeated"):
        action_guard.guarded(opp_id, "save", "Bea", "audit-1", lambda: work.append(1), db)
    action_guard.guarded(opp_id, "save", "Bea", "audit-2", lambda: work.append(1), db)     # a new screen: a real repeat
    assert len(work) == 2


def test_different_actions_and_different_opportunities_do_not_block_each_other(db):
    first, second = _opportunity(db), _opportunity(db)
    outer = []

    def inner():
        action_guard.guarded(first, "b", "Bea", "t", lambda: outer.append("other action"), db)
        action_guard.guarded(second, "a", "Bea", "t", lambda: outer.append("other opportunity"), db)
    action_guard.guarded(first, "a", "Bea", "t", inner, db)
    assert outer == ["other action", "other opportunity"]


def test_a_failed_action_can_be_retried_at_once(db):
    opp_id = _opportunity(db)

    def boom():
        raise RuntimeError("no")
    with pytest.raises(RuntimeError):
        action_guard.guarded(opp_id, "x", "Bea", "t", boom, db)
    assert action_guard.guarded(opp_id, "x", "Bea", "t", lambda: "again", db) == "again"


def test_an_action_left_running_long_ago_does_not_block_for_ever(db):
    opp_id = _opportunity(db)
    run_id = action_guard.begin(opp_id, "x", "Bea", "t", db)
    with session_scope(db) as s:
        s.get(models.Run, run_id).started_at = datetime.now(timezone.utc) - action_guard.STALE_AFTER - timedelta(minutes=1)
    assert action_guard.guarded(opp_id, "x", "Bea", "t2", lambda: "ok", db) == "ok"
    with session_scope(db) as s:
        assert s.get(models.Run, run_id).status == "Failed"


def test_two_rapid_generate_brd_requests_start_one_run(db):
    opp_id = _opportunity(db)

    def start():
        with session_scope(db) as s:
            return ops.start_processing(s, BDM, opp_id)
    results = _together([start, start])
    assert sum(isinstance(r, int) for r in results) == 1
    assert "Already running, started at" in str(next(r for r in results if isinstance(r, wf.InvalidTransition)))
    with session_scope(db) as s:
        assert len(s.scalars(select(models.Run).where(models.Run.opportunity_id == opp_id, models.Run.status == "Running")).all()) == 1


def test_two_rapid_generate_proposal_requests_call_the_llm_once_and_make_one_version(db):
    opp_id = _approved_estimate(db)
    calls = []

    def generate():
        def work():
            calls.append(1)
            _proposal(db, opp_id)
        return action_guard.guarded(opp_id, "generate_proposal", "Bea", "audit-9", work, db)
    results = _together([generate, generate])
    assert len(calls) == 1 and sum(isinstance(r, (action_guard.AlreadyRunning, action_guard.AlreadyDone)) for r in results) == 1
    with session_scope(db) as s:
        assert len(s.scalars(select(models.ArtifactVersion).where(models.ArtifactVersion.opportunity_id == opp_id, models.ArtifactVersion.artifact_type == "PROPOSAL")).all()) == 1


def test_an_action_never_counts_as_the_documents_run(db):
    opp_id = _opportunity(db)
    action_guard.guarded(opp_id, "x", "Bea", "t", lambda: None, db)
    with session_scope(db) as s:
        assert ops.latest_run(s, opp_id) is None


# ---------- failure messages ----------

def _failed_detail(db, opp_id) -> tuple[models.Run, dict]:
    with session_scope(db) as s:
        run = ops.latest_run(s, opp_id)
        return run.id, run.status, dict(run.detail), run.cost_usd


def test_a_save_failure_names_its_category_and_keeps_the_money_it_spent(db, monkeypatch):
    from sqlalchemy.exc import IntegrityError
    opp_id = _opportunity(db)
    with session_scope(db) as s:
        run_id = ops.start_processing(s, BDM, opp_id)

    def broken(*a, **k):
        raise IntegrityError("INSERT secret statement text", {}, Exception("orig"))
    monkeypatch.setattr(legacy_import, "populate_brd", broken)
    real = ops._pipeline

    def spent(*a, **k):
        rec, state = real(*a, **k)
        rec = rec.model_copy(deep=True)     # the sample's result is shared between tests
        rec.spend.from_cache, rec.spend.est_usd, rec.spend.prompt_tokens = False, 0.04, 1000
        return rec, state
    monkeypatch.setattr(ops, "_pipeline", spent)
    ops.run_processing(run_id, opp_id, db)
    _, status, detail, cost = _failed_detail(db, opp_id)
    assert status == "Failed" and detail["category"] == "saving" and detail["stage"] == "saving the BRD" and cost == 0.04
    what, next_step = failures.text_of(run_id, detail)
    assert what.startswith(f"RUN-{run_id}: The result could not be saved") and "Tell the team" in next_step
    assert "secret statement" not in repr(detail)


@pytest.mark.parametrize("error,category", [(llm.BudgetExceeded("over"), "cost_cap"), (ValueError("bad"), "extraction"), (RuntimeError("?"), "unexpected")])
def test_each_failure_has_a_category_and_a_next_step_and_nothing_else_from_the_error(db, monkeypatch, error, category):
    opp_id = _opportunity(db)
    with session_scope(db) as s:
        run_id = ops.start_processing(s, BDM, opp_id)

    def fail(*a, **k):
        raise error
    monkeypatch.setattr(ops, "_pipeline", fail)
    ops.run_processing(run_id, opp_id, db)
    _, status, detail, _ = _failed_detail(db, opp_id)
    assert status == "Failed" and detail["category"] == category and failures.CATEGORIES[category][1] == detail["next_step"]
    assert set(detail) - {"instance", "started_by"} == {"error", "category", "stage", "message", "next_step"}


def test_the_ai_services_failures_read_as_temporary():
    import httpx
    from openai import APITimeoutError, RateLimitError
    request = httpx.Request("POST", "http://x")
    assert failures.category_of(APITimeoutError(request=request)) == "ai_unavailable"
    assert failures.category_of(RateLimitError("busy", response=httpx.Response(429, request=request), body=None)) == "ai_busy"


def test_a_scan_is_told_apart_and_the_llm_is_never_called(db, monkeypatch):
    from app.pipeline import DocInput
    with session_scope(db) as s:
        opp = ops.create(s, BDM, "Fictional", "Scan", "SCAN")
        opp_id = opp.id
        ops.add_document(s, BDM, opp_id, DocInput(name="scan.pdf", kind="tor", pages=[{"page": 1, "text": "Scanned page"}], pii_masked=0), "TOR")
        run_id = ops.start_processing(s, BDM, opp_id)
    called = []
    monkeypatch.setattr(ops, "_pipeline", lambda *a, **k: called.append(1))
    ops.run_processing(run_id, opp_id, db)
    _, status, detail, _ = _failed_detail(db, opp_id)
    assert not called and status == "Failed" and detail["category"] == "unreadable" and "text recognition" in detail["next_step"]


def test_a_run_from_before_these_categories_still_reads_plainly():
    what, next_step = failures.text_of(48, {"error": "IntegrityError", "message": "The run failed. Nothing was changed."})
    assert what == "RUN-48: Something unexpected went wrong." and next_step


# ---------- proposals on the store ----------

def _files(root: Path) -> dict[str, str]:
    """Path under the proposal folder -> the status at the top of the page."""
    out = {}
    for folder in root.iterdir():
        for f in (folder / "proposal").rglob("*.docx"):
            head = " ".join(p.text for section in Document(f).sections for p in section.header.paragraphs)
            out[str(f.relative_to(folder / "proposal"))] = next((m for m in ("SUBMITTED", "APPROVED", "DRAFT") if m in head), "")
    return out


def test_every_proposal_version_reaches_the_folder_as_both_draft_copies_and_approval_adds_both_approved(db, tmp_path, monkeypatch):
    root = tmp_path / "store"
    monkeypatch.setenv("STORAGE_LOCAL_ROOT", str(root))
    opp_id = _approved_estimate(db)
    _proposal(db, opp_id)
    with session_scope(db) as s:
        code = s.get(models.Opportunity, opp_id).code
        assert ops.current_version(s, opp_id, "PROPOSAL").publish_status == "published"
    internal, client_draft = f"PROPOSAL-{code}-R1-v0.1-INTERNAL.docx", f"PROPOSAL-{code}-R1-v0.1-CLIENT.docx"
    assert _files(root) == {internal: "DRAFT", client_draft: "DRAFT"}
    with session_scope(db) as s:
        prop.start_review(s, BDM, opp_id)
    with session_scope(db) as s:
        prop.decide(s, BDM, opp_id, "Approved")
    with session_scope(db) as s:
        prop.decide(s, LEGAL, opp_id, "Approved")
    files = _files(root)
    assert files[f"PROPOSAL-{code}-R1-v1.0-CLIENT.docx"] == "APPROVED" and files[f"PROPOSAL-{code}-R1-v1.0-INTERNAL.docx"] == "APPROVED" and files[internal] == "DRAFT"
    client_file = next(p for folder in root.iterdir() for p in (folder / "proposal").glob(f"PROPOSAL-{code}-R1-v1.0-CLIENT.docx"))
    assert "DRAFT" not in " ".join(p.text for section in Document(client_file).sections for p in section.header.paragraphs)     # a file for the client never says draft
    with session_scope(db) as s:
        prop.mark_submitted(s, BDM, opp_id, "2027-02-01")
    assert _status(db, opp_id) == wf.PROPOSAL_SUBMITTED
    after = _files(root)
    assert after[f"PROPOSAL-{code}-R1-v1.0-CLIENT-SUBMITTED.docx"] == "SUBMITTED"
    assert sum("v1.0-CLIENT" in name for name in after) == 2     # submitting did not copy the approved file again


def test_a_proposal_draft_that_cannot_be_published_is_marked_pending(db, tmp_path, monkeypatch):
    from app import publishing, storage
    monkeypatch.setenv("STORAGE_LOCAL_ROOT", str(tmp_path / "store"))
    opp_id = _approved_estimate(db)

    def down():
        raise storage.Unavailable("down")
    monkeypatch.setattr(publishing.storage, "get_store", down)
    _proposal(db, opp_id)
    with session_scope(db) as s:
        assert ops.current_version(s, opp_id, "PROPOSAL").publish_status == "pending"

"""The real Streamlit app against a throwaway PostgreSQL: sign-in, role switching, the sample opportunity, BRD validation through
approval, a bulletin, and no exception on any screen. Zero Azure calls: the sample is served from its caches."""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alembic import command  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy import inspect as sa_inspect  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from app import brd_service as svc, llm, models, opportunities as ops, workflow as wf  # noqa: E402
from app.db import make_engine, session_scope  # noqa: E402
from tests.test_db import _alembic  # noqa: E402

APP = str(Path(__file__).resolve().parent.parent / "app" / "streamlit_app.py")
PASSWORD = "ui-test-password"


@pytest.fixture
def db(pg, monkeypatch, cached_sample_pipeline):
    with make_engine(pg["owner"]).begin() as conn:
        command.upgrade(_alembic(conn), "head")
        names = ", ".join(t for t in sa_inspect(conn).get_table_names() if t != "alembic_version")
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    monkeypatch.setenv("APP_PASSWORD", PASSWORD)
    monkeypatch.setenv("DATABASE_URL", pg["app"])
    monkeypatch.setenv("MAX_DAILY_USD", "100")     # the local spend ledger must not decide whether a button is on

    def no_azure(*a, **k):
        raise AssertionError("no Azure call expected")

    monkeypatch.setattr(llm, "structured", no_azure)
    return make_engine(pg["app"])


def _run(at: AppTest) -> AppTest:
    at.run(timeout=60)
    assert not at.exception, [e.value for e in at.exception]
    return at


def _app(name="Bea", role=wf.BDM) -> AppTest:
    at = _run(AppTest.from_file(APP, default_timeout=60))
    assert not at.tabs       # nothing shows before the password
    at.text_input[0].set_value(PASSWORD)
    at.button[0].click()
    _run(at)
    assert not at.tabs       # nor before a name and role
    at.text_input[0].set_value(name)
    at.selectbox[0].set_value(role)
    at.button[0].click()
    return _run(at)


def _button(at: AppTest, label: str):
    return next(b for b in at.button if b.label.startswith(label))


def _labels(at: AppTest) -> list[str]:
    return [b.label for b in at.button]


def _text(at: AppTest) -> str:
    return " ".join(x.value for group in (at.markdown, at.info, at.warning, at.caption, at.error, at.success) for x in group)


def _step(at: AppTest, label: str) -> None:
    next(b for b in at.button if b.label == label).click()
    _run(at)


def _wait_for_brd(db, opp_id: str) -> None:
    for _ in range(120):
        with session_scope(db) as s:
            run = ops.latest_run(s, opp_id)
            if run and run.status != "Running":
                assert run.status == "Succeeded", run.detail
                return
        time.sleep(0.5)
    raise AssertionError("the run did not finish")


def test_wrong_password_and_missing_name_keep_the_app_locked(db):
    at = _run(AppTest.from_file(APP, default_timeout=60))
    at.text_input[0].set_value("wrong")
    at.button[0].click()
    _run(at)
    assert at.error and not at.tabs
    at.text_input[0].set_value(PASSWORD)
    at.button[0].click()
    _run(at)
    at.button[0].click()
    _run(at)
    assert not at.tabs and any("Enter your name" in e.value for e in at.error)


def test_the_app_refuses_to_open_without_a_database(db, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    at = _run(AppTest.from_file(APP, default_timeout=60))
    at.text_input[0].set_value(PASSWORD)
    at.button[0].click()
    _run(at)
    assert any("DATABASE_URL" in e.value for e in at.error)


def test_sign_in_and_role_switch_are_logged(db):
    at = _app()
    assert "Signed in as **Bea** (BDM) — self-declared (POC)" in _text(at)
    switch = next(s for s in at.selectbox if s.label == "Switch role")
    switch.set_value(wf.LEGAL)
    _run(at)
    assert "(Legal/Management Reviewer)" in _text(at)
    with session_scope(db) as s:
        rows = s.scalars(select(models.AuditLog).order_by(models.AuditLog.id)).all()
        assert [r.action for r in rows] == ["session.signed_in", "session.role_switched"]
        assert rows[1].details == {"from": wf.BDM, "to": wf.LEGAL}


def test_a_non_bdm_cannot_create_an_opportunity_from_the_screen(db):
    at = _app("Sol", wf.SPECIALIST)
    assert "Create sample HRIS opportunity" not in _labels(at) and "Create opportunity" not in _labels(at)


def test_the_full_brd_flow_on_the_real_screens(db):
    at = _app()
    _button(at, "Create sample HRIS opportunity").click()
    _run(at)
    with session_scope(db) as s:
        opp_id = s.scalars(select(models.Opportunity.id)).one()
    _wait_for_brd(db, opp_id)
    _run(at)
    assert "Where you left off: BRD Generated" in _text(at)
    assert "BRD v0.1 — 0 of 29 BRs reviewed" in _text(at)
    assert "Start validation" in _labels(at) and _button(at, "Start validation").disabled      # a BDM cannot validate
    _step(at, "1 · Intake")
    assert any("Filled from the TOR" in w.value for w in at.warning)
    _step(at, "2 · BRD")

    next(s for s in at.selectbox if s.label == "Switch role").set_value(wf.SPECIALIST)
    _run(at)
    _button(at, "Start validation").click()
    _run(at)
    assert "Where you left off: BRD Under Validation" in _text(at)
    assert _button(at, "Approve BRD").disabled and "Approval needs" in _text(at)
    _button(at, "Accept all remaining").click()
    _run(at)
    with session_scope(db) as s:
        assert ops.review_progress(s, opp_id) == (29, 29)
    assert "OQ-" in _text(at) and _button(at, "Approve BRD").disabled      # the blocking conflict question
    _button(at, "Acknowledge").click()
    _run(at)
    assert not _button(at, "Approve BRD").disabled
    _button(at, "Approve BRD").click()
    _run(at)
    assert "Where you left off: BRD Approved" in _text(at)
    with session_scope(db) as s:
        assert ops.current_version(s, opp_id, "BRD").version_label == "1.0"
        assert s.scalars(select(models.Approval)).one().approver_name == "Bea"

    next(s for s in at.selectbox if s.label == "Switch role").set_value(wf.BDM)
    _run(at)
    _step(at, "1 · Intake")
    _button(at, "Add sample bid bulletin").click()
    _run(at)
    assert "Where you left off: BRD Under Validation" in _text(at)
    assert "Change request from Bid Bulletin No. 01" in _text(at)
    assert not any(b.label == "Accept" and not b.disabled for b in at.button)    # a BDM adds it; the specialist decides

    next(s for s in at.selectbox if s.label == "Switch role").set_value(wf.SPECIALIST)
    _run(at)
    _button(at, "Write merged statements").click()      # no Azure in this test, so the LLM fails and the plain wording is flagged
    _run(at)
    assert any("Needs rewording" in w.value for w in at.warning)
    while any(b.label == "Accept" for b in at.button):
        next(b for b in at.button if b.label == "Accept").click()
        _run(at)
    _button(at, "Apply the accepted changes").click()
    _run(at)
    with session_scope(db) as s:
        assert ops.current_version(s, opp_id, "BRD").version_label == "1.1"
        pending = [r.br_id for r in s.scalars(select(models.BrReview).where(models.BrReview.status == "Pending"))]
        active = len(svc.active_brs(s, opp_id))
    assert active == 30 and len(pending) == 2       # one amended, one added; the other 28 keep their review
    with session_scope(db) as s:
        assert ops.review_progress(s, opp_id) == (28, 30)
    _button(at, "Accept all remaining").click()
    _run(at)
    _button(at, "Approve BRD").click()
    _run(at)
    with session_scope(db) as s:
        assert ops.current_version(s, opp_id, "BRD").version_label == "2.0"
        assert s.get(models.Opportunity, opp_id).commercial["requested_months"] == 6
        first = s.scalars(select(models.ArtifactVersion).where(models.ArtifactVersion.version_major == 0)).one()
        assert (first.created_by, first.created_role) == ("Deal Advisor", wf.SYSTEM)
    _step(at, "2 · BRD")
    assert next(x for x in at.selectbox if x.label == "To").value == "2.0"     # the comparison defaults to previous vs current
    assert next(x for x in at.selectbox if x.label == "From").value == "1.1"

    _button(at, "← All opportunities").click()
    _run(at)
    assert any(h.value == "Opportunities" for h in at.subheader)


def test_reopening_in_a_new_browser_session_continues_where_it_was_left(db):
    at = _app()
    _button(at, "Create sample HRIS opportunity").click()
    _run(at)
    with session_scope(db) as s:
        opp_id = s.scalars(select(models.Opportunity.id)).one()
    _wait_for_brd(db, opp_id)
    with session_scope(db) as s:
        for br in ("BR-001", "BR-002", "BR-003", "BR-004", "BR-005"):
            svc.set_review(s, wf.Actor("Sol", wf.SPECIALIST), opp_id, br, "Accepted")
    fresh = _app("Sol", wf.SPECIALIST)
    _button(fresh, "Open").click()
    _run(fresh)
    assert "BRD v0.1 — 5 of 29 BRs reviewed" in _text(fresh) and "Where you left off: BRD Under Validation" in _text(fresh)


def test_a_saved_draft_waits_for_the_users_choice_and_is_not_wiped_by_opening_the_screen(db):
    at = _app()
    _button(at, "Create sample HRIS opportunity").click()
    _run(at)
    with session_scope(db) as s:
        opp_id = s.scalars(select(models.Opportunity.id)).one()
    _wait_for_brd(db, opp_id)
    sol = wf.Actor("Sol", wf.SPECIALIST)
    draft = {"BR-001": {"title": "Unsaved title"}}
    with session_scope(db) as s:
        svc.start_validation(s, sol, opp_id)
        ops.save_draft(s, sol, opp_id, "BRD", draft)
    fresh = _app("Sol", wf.SPECIALIST)
    _button(fresh, "Open").click()
    _run(fresh)
    assert "Restore unsaved edits?" in _labels(fresh) and "Discard them" in _labels(fresh)
    with session_scope(db) as s:
        assert ops.load_draft(s, sol, opp_id, "BRD") == draft     # opening the screen left it alone
    _button(fresh, "Restore unsaved edits?").click()
    _run(fresh)
    assert "Restore unsaved edits?" not in _labels(fresh)
    assert _button(fresh, "Save changes (1 unsaved)")
    with session_scope(db) as s:
        assert ops.load_draft(s, sol, opp_id, "BRD") == draft

    other = _app("Sol", wf.SPECIALIST)
    _button(other, "Open").click()
    _run(other)
    _button(other, "Discard them").click()
    _run(other)
    with session_scope(db) as s:
        assert ops.load_draft(s, sol, opp_id, "BRD") == {}
    assert "Restore unsaved edits?" not in _labels(other)


def _role(at: AppTest, role: str) -> None:
    next(s for s in at.selectbox if s.label == "Switch role").set_value(role)
    _run(at)


def test_the_estimate_proposal_and_award_on_the_real_screens(db, monkeypatch):
    from app import proposal

    monkeypatch.setattr(proposal, "_narrative", lambda rec, usage, use_cache, trace=None: {k: [f"{k} narrative."] for k in proposal.NARRATIVE})
    at = _app()
    _button(at, "Create sample HRIS opportunity").click()
    _run(at)
    with session_scope(db) as s:
        opp_id = s.scalars(select(models.Opportunity.id)).one()
    _wait_for_brd(db, opp_id)
    _run(at)
    _role(at, wf.SPECIALIST)
    _button(at, "Start validation").click()
    _run(at)
    _button(at, "Accept all remaining").click()
    _run(at)
    _button(at, "Acknowledge").click()
    _run(at)
    _button(at, "Approve BRD").click()
    _run(at)
    assert "Traditional" not in _text(at)

    _button(at, "Generate estimates and timeline").click()
    _run(at)
    assert "Where you left off: Estimate and Timeline Generated" in _text(at)
    assert "Work breakdown structure" in [t.label for t in at.tabs]
    assert "AI tooling cost and net savings" in _text(at) and "illustrative" in _text(at)
    assert any("Net savings vs. baseline" in str(d.value["Item"].tolist()) for d in at.dataframe if "Item" in d.value)
    assert any(m.label == "B AI-Augmented" for m in at.metric) and any(m.label == "C AI-First" for m in at.metric)
    assert not any("Traditional" in m.label or "Baseline" in m.label for m in at.metric)
    next(b for b in at.button if b.label == "Start validation" and "start_estimate" in (b.key or "")).click()
    _run(at)
    _button(at, "Validate all remaining rows").click()
    _run(at)
    assert _button(at, "Approve estimate").disabled
    from app import estimate_service as est
    for label in est.CHECKLIST.values():
        next(c for c in at.checkbox if c.label == label).check()
        _run(at)
    _button(at, "Approve estimate").click()
    _run(at)
    assert "Where you left off: Estimate Approved" in _text(at)

    _role(at, wf.BDM)
    assert any("Recommendation: B AI-Augmented" in m.value for m in at.markdown)
    picker = next(m for m in at.multiselect if m.label.startswith("Acknowledge every"))
    picker.set_value(_high_ids(db, opp_id))
    _run(at)
    _button(at, "Generate proposal").click()
    _run(at)
    assert "Where you left off: Proposal Generated" in _text(at)
    assert any("leak check and consistency check passed" in s.value for s in at.success)
    _button(at, "Start the proposal review").click()
    _run(at)
    _button(at, "Approve proposal").click()
    _run(at)
    assert "Bea (BDM): Approved" in _text(at)
    _role(at, wf.LEGAL)
    _button(at, "Approve proposal").click()
    _run(at)
    _role(at, wf.BDM)
    next(d for d in at.date_input if d.label == "Submission date").set_value("2027-01-15")
    _run(at)
    _button(at, "Mark as submitted").click()
    _run(at)
    assert "Where you left off: Proposal Submitted" in _text(at)
    next(d for d in at.date_input if d.label == "Decision date").set_value("2027-02-01")
    next(s for s in at.selectbox if s.label == "Outcome").set_value("Awarded")
    _button(at, "Record the decision").click()
    _run(at)
    assert "Where you left off: Awarded" in _text(at)
    _step(at, "5 · Award")
    assert any("SOW and MSA can be triggered" in s.value for s in at.success)
    with session_scope(db) as s:
        actions = set(s.scalars(select(models.AuditLog.action).where(models.AuditLog.opportunity_id == opp_id)))
    assert {"brd.approved", "estimate.generated", "estimate.approved", "proposal.generated", "proposal.fully_approved", "award.recorded"} <= actions


def _high_ids(db, opp_id: str) -> list[str]:
    with session_scope(db) as s:
        return [r["id"] for r in ops.current_version(s, opp_id, "ESTIMATE").snapshot["results"]["risks"] if r["severity"] == "high"]


def test_only_an_admin_sees_the_rates_and_the_old_flow_is_gone(db):
    at = _app()
    labels = [e.label for e in at.expander]
    assert "Admin: Rates" not in labels and not any("v2 flow" in t.label or "Guided flow" in t.label for t in at.tabs)
    next(s for s in at.selectbox if s.label == "Switch role").set_value(wf.ADMIN)
    _run(at)
    assert "Admin: Rates" in [e.label for e in at.expander]


def test_steps_open_only_once_they_have_content(db):
    at = _app()
    _button(at, "Create sample HRIS opportunity").click()
    _run(at)
    with session_scope(db) as s:
        opp_id = s.scalars(select(models.Opportunity.id)).one()
    _wait_for_brd(db, opp_id)
    _run(at)
    state = {b.label: b.disabled for b in at.button if " · " in b.label and b.label[0].isdigit()}
    assert state == {"1 · Intake": False, "2 · BRD": False, "3 · Estimate & Timeline": True, "4 · Proposal": True, "5 · Award": True, "6 · Contracts": True, "7 · Final Approval": True}
    assert {t.label for t in at.tabs} >= {"Workspace", "Audit trail", "Versions", "Agent trace", "Evidence", "Record"}


def test_an_unexpected_error_shows_a_plain_message_not_a_traceback(db, monkeypatch):
    from app import opportunities

    def boom(*a, **k):
        raise KeyError("internal detail")

    monkeypatch.setattr(opportunities, "create_sample_hris", boom)
    at = _app()
    _button(at, "Create sample HRIS opportunity").click()
    _run(at)
    assert any("nothing was saved" in e.value and "KeyError" in e.value and "internal detail" not in e.value for e in at.error)


# ---------- a button that starts work ----------

def _busy_script():
    import streamlit as st
    from app import workspace_ui as ui
    ui._begin_render()
    clicked = ui._button(st, "Generate", key="generate")
    st.session_state.clicked_saw_off = (clicked, st.session_state.get("_action_now") is not None)


def test_a_button_is_off_for_the_run_its_click_starts_and_on_again_after():
    at = AppTest.from_function(_busy_script).run(timeout=30)
    assert not at.button[0].disabled
    at.button[0].click().run(timeout=30)
    assert at.session_state.clicked_saw_off == (True, True) and at.button[0].disabled
    at.run(timeout=30)
    assert not at.button[0].disabled and at.session_state.clicked_saw_off == (False, False)


def test_a_failed_run_says_why_and_its_reference_opens_the_details(db):
    from app import failures
    from app.workflow import Actor
    with session_scope(db) as s:
        opp_id = ops.create_sample_hris(s, Actor("Bea", wf.BDM))
        run_id = ops.start_processing(s, Actor("Bea", wf.BDM), opp_id)
        code = s.get(models.Opportunity, opp_id).code
    with session_scope(db) as s:
        run = s.get(models.Run, run_id)
        run.status = "Failed"
        run.detail = {**run.detail, **failures.detail(ValueError("a line of the document"), "saving the BRD", "saving"), "constraint": "uq_brs_opportunity_id_br_id"}
    at = _app()
    at.selectbox(key="opportunity_pick").set_value(opp_id)
    at.button(key="opportunity_open").click()
    _run(at)
    errors = " ".join(e.value for e in at.error)
    assert f"RUN-{run_id}: The result could not be saved." in errors and "Tell the team" in errors
    assert "Traceback" not in _text(at) and "a line of the document" not in _text(at)
    at.button(key=f"failure_details_{run_id}").click()
    _run(at)
    assert at.session_state.trace_run == run_id
    assert any(f"RUN-{run_id}" in e.label for e in at.expander)
    assert "What to do next" in _text(at)
    at.button(key=f"trace_back_{opp_id}").click()
    _run(at)
    assert "trace_run" not in at.session_state and code


def test_the_evidence_tab_shows_why_a_section_was_or_was_not_scope(db):
    from app.workflow import Actor
    with session_scope(db) as s:
        opp_id = ops.create_sample_hris(s, Actor("Bea", wf.BDM))
        s.get(models.Opportunity, opp_id).pipeline = {"sections": [{"number": "2", "title": "What We Need", "role": "requirements_scope", "reason": "It lists what the software must do.", "units": 3, "br": 2, "nfr": 0, "not_requirement": 0, "unmapped": 1},
                                                                   {"number": "4", "title": "Logistics", "role": "bid_procedure", "reason": "It explains how to bid.", "units": 0, "br": 0, "nfr": 0, "not_requirement": 0, "unmapped": 0}],
                                                      "checker": {"unmapped": ["Waiting list"]}}
    at = _app()
    at.selectbox(key="opportunity_pick").set_value(opp_id)
    at.button(key="opportunity_open").click()
    _run(at)
    assert "How each section was read" in _text(at)
    frame = next(d.value for d in at.dataframe if "Why" in d.value.columns)
    assert list(frame["Role"]) == ["Requirements / scope", "Bid procedure"] and "It explains how to bid." in list(frame["Why"]) and list(frame["Unmapped"]) == [1, 0]
    assert any("Waiting list" in w.value for w in at.warning)


def test_only_an_admin_sees_the_delete_and_it_reports_what_went(db, monkeypatch, tmp_path):
    from app import purge, tracing
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("STORAGE_LOCAL_ROOT", str(tmp_path))
    monkeypatch.setattr(tracing, "delete_traces", lambda opp_id: {"status": "not_configured", "traces": 0})
    with session_scope(db) as s:
        opp = ops.create(s, wf.Actor("Bea", wf.BDM), "Test Co", "Throwaway", "THROW")
        kept = ops.create(s, wf.Actor("Bea", wf.BDM), "Fictional Co", "Sample", "HRIS")
        opp_id = opp.id
        s.flush()
        assert kept.id
    at = _app()
    assert "Delete opportunity" not in _labels(at) and not any("Delete opportunity" in e.label for e in at.expander)
    _role(at, wf.ADMIN)
    assert any("Delete opportunity" in e.label for e in at.expander) and "HRIS" in _text(at)
    picker = next(b for b in at.selectbox if b.label == "Opportunity")
    assert picker.options == ["THROW · Throwaway · Draft Opportunity"]     # protected codes are not offered
    next(t for t in at.text_input if t.label == "Type its code to confirm").set_value("nope")
    _button(at, "Delete opportunity").click()
    _run(at)
    assert any("does not match" in e.value for e in at.error)
    with session_scope(db) as s:
        assert s.get(models.Opportunity, opp_id) is not None
    next(t for t in at.text_input if t.label == "Type its code to confirm").set_value("THROW")
    _button(at, "Delete opportunity").click()
    _run(at)
    assert any("THROW deleted" in m.value and "rows" in m.value for m in at.success)
    with session_scope(db) as s:
        assert s.get(models.Opportunity, opp_id) is None
    assert purge.is_protected("HRIS")


def test_the_record_tab_shows_the_bid_procedure_as_a_list(db):
    with session_scope(db) as s:
        opp = ops.create(s, wf.Actor("Bea", wf.BDM), "Test Co", "Throwaway", "RECORD")
        opp.commercial = {**opp.commercial, "bid_procedure": ["Each file no larger than 20MB", "Late *proposals* are not opened"]}
        opp_id = opp.id
    at = _app()
    at.session_state["opportunity"] = opp_id
    _run(at)
    shown = _text(at)
    assert "Bid procedure" in shown and "Each file no larger than 20MB" in shown and r"Late \*proposals\* are not opened" in shown

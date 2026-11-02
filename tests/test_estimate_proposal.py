"""W5-W7 against a throwaway PostgreSQL, on the bundled HRIS sample from its caches: estimates from the approved BRD, validation, incremental
re-estimation, the proposal and the award. No Azure calls: the proposal narrative is stubbed."""
import io
import json
import sys
from pathlib import Path

import pytest
from docx import Document

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alembic import command  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy import inspect as sa_inspect  # noqa: E402

from app import brd_service as svc, costing, estimate_service as est, estimation, llm, models, opportunities as ops, pipeline, proposal, proposal_service as prop, record, store, supplement, workflow as wf  # noqa: E402
from app.db import make_engine, session_scope  # noqa: E402
from app.pipeline import DocInput  # noqa: E402
from app.workflow import Actor  # noqa: E402
from tests.test_db import _alembic  # noqa: E402

BDM, SS, LEGAL, ADMIN = Actor("Bea", wf.BDM), Actor("Sol", wf.SPECIALIST), Actor("Lex", wf.LEGAL), Actor("Ada", wf.ADMIN)
NARRATIVE = {k: [f"{k} narrative."] for k in proposal.NARRATIVE}


@pytest.fixture
def db(pg, monkeypatch, cached_sample_pipeline):
    with make_engine(pg["owner"]).begin() as conn:
        command.upgrade(_alembic(conn), "head")
        names = ", ".join(t for t in sa_inspect(conn).get_table_names() if t != "alembic_version")
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))

    def no_azure(*a, **k):
        raise AssertionError("no Azure call expected")

    monkeypatch.setattr(llm, "structured", no_azure)
    monkeypatch.setattr(llm, "chat", no_azure)
    monkeypatch.setattr(proposal, "_narrative", lambda rec, usage, use_cache, trace=None: NARRATIVE)
    return make_engine(pg["app"])


def _approved_brd(db) -> str:
    with session_scope(db) as s:
        opp_id = ops.create_sample_hris(s, BDM)
        run_id = ops.start_processing(s, BDM, opp_id)
    ops.run_processing(run_id, opp_id, db)
    with session_scope(db) as s:
        assert ops.latest_run(s, opp_id).status == "Succeeded"
        svc.accept_all_remaining(s, SS, opp_id)
        svc.acknowledge_question(s, SS, opp_id, "OQ-001")
        svc.approve(s, SS, opp_id)
    return opp_id


def _generated(db) -> str:
    opp_id = _approved_brd(db)
    with session_scope(db) as s:
        est.generate(s, SS, opp_id)
    return opp_id


def _validate_and_approve(db, opp_id) -> None:
    with session_scope(db) as s:
        if s.get(models.Opportunity, opp_id).status == wf.ESTIMATE_GENERATED:
            est.start_validation(s, SS, opp_id)
    with session_scope(db) as s:
        est.validate_rows(s, SS, opp_id)
        for item in est.CHECKLIST:
            est.tick(s, SS, opp_id, item, True)
        est.approve(s, SS, opp_id)


def _approved_estimate(db) -> str:
    opp_id = _generated(db)
    _validate_and_approve(db, opp_id)
    return opp_id


def _snap(db, opp_id, artifact="ESTIMATE") -> dict:
    with session_scope(db) as s:
        v = ops.current_version(s, opp_id, artifact)
        return {"label": v.version_label, "status": v.status, "outdated": v.outdated, **v.snapshot}


def _status(db, opp_id) -> str:
    with session_scope(db) as s:
        return s.get(models.Opportunity, opp_id).status


def _high_risks(db, opp_id) -> list[str]:
    return [r["id"] for r in _snap(db, opp_id)["results"]["risks"] if r["severity"] == "high"]


def _proposal(db, opp_id, scenario="B") -> None:
    with session_scope(db) as s:
        prop.trigger(s, BDM, opp_id, scenario, _high_risks(db, opp_id))


def _approved_proposal(db) -> str:
    opp_id = _approved_estimate(db)
    _proposal(db, opp_id)
    with session_scope(db) as s:
        prop.start_review(s, BDM, opp_id)
        prop.decide(s, BDM, opp_id, "Approved")
        prop.decide(s, LEGAL, opp_id, "Approved")
    return opp_id


def _submitted(db) -> str:
    opp_id = _approved_proposal(db)
    with session_scope(db) as s:
        prop.mark_submitted(s, BDM, opp_id, "2027-01-15")
    return opp_id


def _text_of(data: bytes) -> str:
    doc = Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs]
    for t in doc.tables:
        for r in t.rows:
            parts += [c.text for c in r.cells]
    return "\n".join(parts)


# ---------- W5: generation ----------

@pytest.mark.parametrize("status", [wf.BRD_GENERATED, wf.BRD_VALIDATION])
def test_no_estimate_before_the_brd_is_approved(db, status):
    with session_scope(db) as s:
        opp_id = ops.create_sample_hris(s, BDM)
        s.get(models.Opportunity, opp_id).status = status
    with pytest.raises(wf.InvalidTransition, match="after BRD approval"), session_scope(db) as s:
        est.generate(s, SS, opp_id)


@pytest.mark.parametrize("actor", [BDM, LEGAL, ADMIN])
def test_only_the_specialist_generates_and_edits(db, actor):
    opp_id = _approved_brd(db)
    with pytest.raises(wf.NotAllowed), session_scope(db) as s:
        est.generate(s, actor, opp_id)
    with session_scope(db) as s:
        est.generate(s, SS, opp_id)
    with pytest.raises(wf.NotAllowed), session_scope(db) as s:
        est.edit(s, actor, opp_id, "0.1", sizes={"BR-001": "XL"})
    with pytest.raises(wf.NotAllowed), session_scope(db) as s:
        est.start_validation(s, actor, opp_id)


def test_the_estimate_matches_the_calculators_exactly_and_records_its_brd_version(db):
    """The database path changes the source of the data, not the numbers."""
    opp_id = _generated(db)
    snap = _snap(db, opp_id)
    rec, _, _ = pipeline.process_hris()
    direct, _ = estimation.run_estimation(rec, costing.illustrative_rates())
    for s_ in ("baseline", "B", "C"):
        assert snap["results"]["estimates"]["scenarios"][s_]["total_hours"] == pytest.approx(direct.estimates["scenarios"][s_]["total_hours"])
    for s_ in ("B", "C"):
        assert snap["results"]["costing"]["scenarios"][s_]["price_ex_vat"] == pytest.approx(direct.costing.scenarios[s_]["price_ex_vat"])
        assert snap["results"]["timeline"][s_]["total_months"] == pytest.approx(direct.timeline[s_]["total_months"])
    assert (snap["label"], snap["status"], snap["brd_version"]) == ("0.1", "Draft", "1.0")
    assert _status(db, opp_id) == wf.ESTIMATE_GENERATED
    with session_scope(db) as s:
        estimate = ops.current_version(s, opp_id, "ESTIMATE")
        assert s.get(models.ArtifactVersion, estimate.based_on_id).version_label == "1.0"


def test_only_b_and_c_are_scenarios_and_suggested_scope_is_not_priced(db):
    opp_id = _generated(db)
    snap = _snap(db, opp_id)
    assert set(snap["totals"]) == {"B", "C"} and set(est.SCENARIOS) == {"B", "C"}
    assert snap["rows"]["BR-010"]["priced"] is False and snap["rows"]["BR-001"]["priced"] is True
    assert snap["rows"]["BR-010"]["man_days"]["B"] == 0


def test_rate_values_are_never_stored(db):
    opp_id = _generated(db)
    stored = json.dumps(_snap(db, opp_id))
    assert '"labor_by_role"' not in stored and '"published"' not in stored and '"blended"' not in stored
    values = {f"{v:.2f}" for t in (costing.illustrative_rates(),) for v in t.published.values()}
    costs = _snap(db, opp_id)["results"]["costing"]["scenarios"]["B"]
    assert not values & {f"{v:.2f}" for v in costs.values() if isinstance(v, (int, float))}


def test_the_work_breakdown_adds_up_by_br(db):
    opp_id = _generated(db)
    snap = _snap(db, opp_id)
    rows = est.wbs_rows(snap)
    assert {"epic", "br_id", "role", "activity", "baseline", "B", "C"} <= set(rows[0])
    for br_id, row in snap["rows"].items():
        if row["priced"]:
            assert sum(r["B"] for r in rows if r["br_id"] == br_id) == pytest.approx(row["hours"]["B"])
    assert any(r["br_id"] == "Foundation" for r in rows)


# ---------- W5: editing ----------

def test_a_size_edit_is_a_new_version_recalculated_by_code(db):
    opp_id = _generated(db)
    before = _snap(db, opp_id)
    with session_scope(db) as s:
        est.edit(s, SS, opp_id, "0.1", sizes={"BR-001": "XL"})
    after = _snap(db, opp_id)
    assert after["label"] == "0.2" and after["rows"]["BR-001"]["size"] == "XL" and after["rows"]["BR-001"]["size_overridden"]
    assert after["totals"]["B"]["price_ex_vat"] > before["totals"]["B"]["price_ex_vat"]
    assert after["rows"]["BR-002"]["man_days"] == before["rows"]["BR-002"]["man_days"]
    assert _status(db, opp_id) == wf.ESTIMATE_VALIDATION
    with session_scope(db) as s:
        old = s.scalars(select(models.ArtifactVersion).where(models.ArtifactVersion.artifact_type == "ESTIMATE", models.ArtifactVersion.version_minor == 1)).one()
        assert old.status == "Superseded" and "BR-001 size XL" in ops.current_version(s, opp_id, "ESTIMATE").change_summary


def test_a_stale_estimate_screen_is_refused(db):
    opp_id = _generated(db)
    with session_scope(db) as s:
        est.edit(s, SS, opp_id, "0.1", sizes={"BR-001": "L"})
    with pytest.raises(store.StaleWrite), session_scope(db) as s:
        est.edit(s, SS, opp_id, "0.1", sizes={"BR-002": "L"})


def test_an_activity_override_needs_a_reason_and_sets_the_baseline_hours(db):
    opp_id = _generated(db)
    snap = _snap(db, opp_id)
    feature = snap["rows"]["BR-003"]["feature_id"]
    key = f"{feature}|Backend.coding"
    with pytest.raises(ValueError, match="reason"), session_scope(db) as s:
        est.edit(s, SS, opp_id, "0.1", activity_hours={key: {"hours": 80, "reason": " "}})
    with pytest.raises(ValueError, match="not an activity"), session_scope(db) as s:
        est.edit(s, SS, opp_id, "0.1", activity_hours={"F-x|Backend.coding": {"hours": 80, "reason": "r"}})
    with session_scope(db) as s:
        est.edit(s, SS, opp_id, "0.1", activity_hours={key: {"hours": 80, "reason": "Vendor API is undocumented"}})
    after = _snap(db, opp_id)
    row = next(r for r in est.wbs_rows(after) if r["key"] == key)
    assert row["baseline"] == pytest.approx(80) and row["overridden"]
    assert after["inputs"]["activity_hours"][key] == {"hours": 80.0, "reason": "Vendor API is undocumented", "by": "Sol"}
    assert after["rows"]["BR-003"]["hours"]["baseline"] != snap["rows"]["BR-003"]["hours"]["baseline"]


def test_team_and_phase_edits_change_the_timeline_by_code(db):
    opp_id = _generated(db)
    before = _snap(db, opp_id)
    with session_scope(db) as s:
        est.edit(s, SS, opp_id, "0.1", team={"B": {"Backend": 6.0}}, assumptions={"uat_weeks": 3})
    after = _snap(db, opp_id)
    assert after["results"]["timeline"]["B"]["team"]["Backend"] == 6.0
    assert after["results"]["timeline"]["B"]["total_months"] != before["results"]["timeline"]["B"]["total_months"]
    with pytest.raises(ValueError, match="UAT"), session_scope(db) as s:
        est.edit(s, SS, opp_id, "0.2", assumptions={"uat_weeks": 99})
    with pytest.raises(ValueError, match="cannot be edited"), session_scope(db) as s:
        est.edit(s, SS, opp_id, "0.2", assumptions={"price_floor": 1})
    assert _snap(db, opp_id)["label"] == "0.2"


# ---------- W5: validation ----------

def test_approval_needs_every_row_and_the_whole_checklist(db):
    opp_id = _generated(db)
    with session_scope(db) as s:
        est.start_validation(s, SS, opp_id)
    with pytest.raises(wf.InvalidTransition, match="29 rows need validation.*Tick the checklist"), session_scope(db) as s:
        est.approve(s, SS, opp_id)
    with session_scope(db) as s:
        assert est.validate_rows(s, SS, opp_id, ["BR-001", "BR-002"]) == 2
        assert est.validate_rows(s, SS, opp_id) == 27
        for item in list(est.CHECKLIST)[:-1]:
            est.tick(s, SS, opp_id, item, True)
    with pytest.raises(wf.InvalidTransition, match="Cost, pricing and target margin"), session_scope(db) as s:
        est.approve(s, SS, opp_id)
    with session_scope(db) as s:
        est.tick(s, SS, opp_id, "commercial", True)
        version = est.approve(s, SS, opp_id, "Validated")
        assert (version.version_label, version.status) == ("1.0", "Approved")
        assert s.scalars(select(models.Approval).where(models.Approval.stage == "ESTIMATE")).one().approver_role == wf.SPECIALIST
    assert _status(db, opp_id) == wf.ESTIMATE_APPROVED


def test_the_summary_is_marked_internal_and_holds_the_price(db):
    opp_id = _approved_estimate(db)
    with session_scope(db) as s:
        data = est.summary_docx(s, opp_id)
    doc = Document(io.BytesIO(data))
    assert doc.sections[0].header.paragraphs[0].text.startswith("INTERNAL — CONTAINS PRICING")
    snap = _snap(db, opp_id)
    assert f"{snap['results']['costing']['scenarios']['B']['price_ex_vat']:,.0f}" in _text_of(data)
    assert "Work breakdown structure" in _text_of(data) and "Traditional" not in _text_of(data)


def test_editing_an_approved_estimate_opens_a_change_request_and_outdates_the_proposal(db):
    opp_id = _approved_estimate(db)
    _proposal(db, opp_id)
    with session_scope(db) as s:
        est.edit(s, SS, opp_id, "1.0", sizes={"BR-001": "XL"})
    assert _status(db, opp_id) == wf.ESTIMATE_VALIDATION and _snap(db, opp_id)["label"] == "1.1"
    assert _snap(db, opp_id, "PROPOSAL")["outdated"] is True
    with session_scope(db) as s:
        cr = s.scalars(select(models.ChangeRequest).where(models.ChangeRequest.artifact_type == "ESTIMATE")).one()
        assert (cr.status, cr.trigger) == ("Confirmed", "in-app edit")
        assert "PROPOSAL" in ops.where_you_left_off(s, opp_id)["outdated"]
    with session_scope(db) as s:
        for item in est.CHECKLIST:
            est.tick(s, SS, opp_id, item, True)
        assert est.approve(s, SS, opp_id).version_label == "2.0"


def test_re_estimate_all_is_admin_only_with_a_reason_and_discards_overrides(db):
    opp_id = _generated(db)
    with session_scope(db) as s:
        est.edit(s, SS, opp_id, "0.1", sizes={"BR-001": "XL"})
    with pytest.raises(wf.NotAllowed), session_scope(db) as s:
        est.re_estimate_all(s, SS, opp_id, "Because")
    with pytest.raises(ValueError, match="reason"), session_scope(db) as s:
        est.re_estimate_all(s, ADMIN, opp_id, " ")
    with session_scope(db) as s:
        est.re_estimate_all(s, ADMIN, opp_id, "New calibration")
        assert s.scalars(select(models.AuditLog).where(models.AuditLog.action == "estimate.re_estimated_all")).one().details == {"reason": "New calibration"}
    assert _snap(db, opp_id)["inputs"]["sizes"] == {}


# ---------- W5: incremental re-estimation ----------

BULLETIN = """Bid Bulletin No. 02
1. Approval
1.1 Approval levels
Under Section 5.3, leave applications shall be approved in three levels: first by the immediate supervisor, then by the department head, and finally by the HR manager.
2. Additional requirement
2.1 Leave ledger download
Under Section 5.4, employees shall be able to download their own leave ledger for the current year as a PDF file.
"""


def _apply_bulletin(db, opp_id) -> None:
    with session_scope(db) as s:
        doc = ops.add_document(s, BDM, opp_id, DocInput(name="b.md", kind="bid_bulletin", pages=[{"page": 1, "text": BULLETIN}], date="2027-01-10", number="02"), "Bid bulletin")
        cr_id = supplement.propose(s, BDM, opp_id, doc.id).id
    with session_scope(db) as s:
        for p in s.get(models.ChangeRequest, cr_id).diff_summary["proposals"]:
            if p["status"] == "pending":
                supplement.decide(s, SS, opp_id, cr_id, p["id"], True)
    with session_scope(db) as s:
        supplement.finish(s, SS, opp_id, cr_id)
        svc.accept_all_remaining(s, SS, opp_id)
        svc.approve(s, SS, opp_id)


def test_a_bulletin_re_estimates_only_what_it_touched(db):
    """The plan's test: an approved estimate with 2 overrides; a bulletin adds 1 BR and modifies 1. 2 rows re-sized, overrides kept, delta shown."""
    opp_id = _generated(db)
    snap = _snap(db, opp_id)
    feature = snap["rows"]["BR-005"]["feature_id"]
    key = f"{feature}|Backend.coding"
    with session_scope(db) as s:
        est.edit(s, SS, opp_id, "0.1", sizes={"BR-003": "XL"}, activity_hours={key: {"hours": 50, "reason": "Known complexity"}})
    _validate_and_approve(db, opp_id)
    before = _snap(db, opp_id)
    _apply_bulletin(db, opp_id)
    assert _snap(db, opp_id)["outdated"] is True
    with pytest.raises(wf.InvalidTransition, match="after BRD approval"), session_scope(db) as s:
        s.get(models.Opportunity, opp_id).status = wf.ESTIMATE_APPROVED
        s.flush()
        est.generate(s, SS, opp_id)
    with session_scope(db) as s:
        est.generate(s, SS, opp_id)
    after = _snap(db, opp_id)
    assert after["brd_version"] == "2.0" and after["label"] == "1.1"
    changed = {r["br_id"]: r for r in after["delta"]["rows"]}
    assert set(changed) == {"BR-014", "BR-030"}
    assert changed["BR-030"]["change"] == "added" and changed["BR-030"]["man_days_from"] is None and changed["BR-030"]["man_days_to"]["B"] > 0
    assert changed["BR-014"]["change"] == "modified"
    assert after["delta"]["unchanged"] == 28
    assert after["delta"]["totals"]["B"]["from"]["price_ex_vat"] == before["totals"]["B"]["price_ex_vat"]
    assert after["delta"]["totals"]["B"]["to"]["price_ex_vat"] != before["totals"]["B"]["price_ex_vat"]
    assert after["inputs"]["sizes"] == {"BR-003": "XL"} and after["inputs"]["activity_hours"][key]["hours"] == 50
    for br_id, row in before["rows"].items():
        if br_id not in changed:
            assert after["rows"][br_id]["size"] == row["size"] and after["rows"][br_id]["man_days"] == row["man_days"] and after["rows"][br_id]["validated"] is True
    assert (after["rows"]["BR-014"]["validated"], after["rows"]["BR-030"]["validated"]) == (False, False)
    with session_scope(db) as s:
        est.start_validation(s, SS, opp_id)
    with pytest.raises(wf.InvalidTransition, match="2 rows need validation"), session_scope(db) as s:
        est.approve(s, SS, opp_id)
    with session_scope(db) as s:
        est.validate_rows(s, SS, opp_id)
        for item in est.CHECKLIST:
            est.tick(s, SS, opp_id, item, True)
        assert est.approve(s, SS, opp_id).version_label == "2.0"


def test_a_modified_brs_override_is_dropped_and_a_removed_br_drops_out(db):
    opp_id = _generated(db)
    with session_scope(db) as s:
        est.edit(s, SS, opp_id, "0.1", sizes={"BR-014": "XL", "BR-002": "L"})
    _validate_and_approve(db, opp_id)
    with session_scope(db) as s:
        changes = svc.empty_changes()
        changes["modified"] = [{"br_id": "BR-014", "changes": {"title": "Renamed approval"}}]
        changes["removed"] = ["BR-002"]
        svc.apply_changes(s, SS, opp_id, changes, "in-app edit")
        svc.accept_all_remaining(s, SS, opp_id)
        svc.approve(s, SS, opp_id)
        est.generate(s, SS, opp_id)
    after = _snap(db, opp_id)
    assert after["inputs"]["sizes"] == {} and "BR-002" not in after["rows"]
    assert {r["br_id"]: r["change"] for r in after["delta"]["rows"]} == {"BR-014": "modified", "BR-002": "removed"}


def test_an_outdated_estimate_cannot_be_approved(db):
    opp_id = _generated(db)
    with session_scope(db) as s:
        est.start_validation(s, SS, opp_id)
        est.validate_rows(s, SS, opp_id)
        for item in est.CHECKLIST:
            est.tick(s, SS, opp_id, item, True)
        ops.current_version(s, opp_id, "ESTIMATE").outdated = True
    with pytest.raises(wf.InvalidTransition, match="outdated"), session_scope(db) as s:
        est.approve(s, SS, opp_id)


# ---------- W6: proposal ----------

def test_only_the_bdm_triggers_and_only_after_the_estimate_is_approved(db):
    opp_id = _generated(db)
    with pytest.raises(wf.InvalidTransition, match="after the estimate is approved"), session_scope(db) as s:
        prop.trigger(s, BDM, opp_id, "B", [])
    with session_scope(db) as s:
        est.start_validation(s, SS, opp_id)
    _validate_and_approve(db, opp_id)
    for actor in (SS, LEGAL, ADMIN):
        with pytest.raises(wf.NotAllowed), session_scope(db) as s:
            prop.trigger(s, actor, opp_id, "B", [])


def test_every_high_risk_must_be_acknowledged_and_only_b_or_c_exist(db):
    opp_id = _approved_estimate(db)
    with pytest.raises(ValueError, match="high-severity"), session_scope(db) as s:
        prop.trigger(s, BDM, opp_id, "B", [])
    with pytest.raises(ValueError, match="Only B or C"), session_scope(db) as s:
        prop.trigger(s, BDM, opp_id, "baseline", _high_risks(db, opp_id))
    assert _status(db, opp_id) == wf.ESTIMATE_APPROVED


def test_an_assumption_edit_reaches_the_approved_record(db):
    opp_id = _generated(db)
    with session_scope(db) as s:
        est.edit(s, SS, opp_id, "0.1", assumptions={"contingency_rate": 0.1})
    _validate_and_approve(db, opp_id)
    with session_scope(db) as s:
        rec = prop.record_for(s, opp_id, *prop._approved_versions(s, opp_id))
    assert isinstance(rec, record.OpportunityRecord) and rec.costing.scenarios["B"]["contingency_rate"] == 0.1


def test_the_default_scenario_follows_the_recommendation_rule(db):
    opp_id = _approved_estimate(db)
    with session_scope(db) as s:
        assert prop.recommended(s, opp_id)["recommended_scenario"] == "B"


def test_two_outputs_the_client_copy_clean_and_without_the_management_section(db):
    opp_id = _approved_estimate(db)
    _proposal(db, opp_id)
    snap = _snap(db, opp_id, "PROPOSAL")
    assert snap["label"] == "0.1" and snap["scenario"] == "B" and (snap["brd_version"], snap["estimate_version"]) == ("1.0", "1.0")
    assert snap["checks"]["leak_hits"] == [] and snap["checks"]["consistency"]["ok"], snap["checks"]
    assert _status(db, opp_id) == wf.PROPOSAL_GENERATED
    with session_scope(db) as s:
        client, internal = prop.client_copy(s, opp_id), prop.internal_copy(s, opp_id)
    assert prop.INTERNAL_SECTION not in _text_of(client) and prop.INTERNAL_SECTION in _text_of(internal)
    assert Document(io.BytesIO(internal)).sections[0].header.paragraphs[0].text == prop.INTERNAL_MARK
    assert prop.INTERNAL_MARK not in _text_of(client)
    listed = [p.text for p in Document(io.BytesIO(client)).paragraphs]
    assert "Data Migration" not in listed       # suggested scope is not priced, so not listed as scope
    assert "Please confirm whether Data Migration is required." in listed     # it is asked as a clarification request


def test_the_price_comes_from_the_approved_estimate_not_a_new_run(db):
    opp_id = _approved_estimate(db)
    _proposal(db, opp_id)
    expected = _snap(db, opp_id)["results"]["costing"]["scenarios"]["B"]["price_ex_vat"]
    with session_scope(db) as s:
        assert proposal._php(expected) in _text_of(prop.client_copy(s, opp_id))


def test_both_the_bdm_and_legal_must_approve_the_same_version(db):
    opp_id = _approved_estimate(db)
    _proposal(db, opp_id)
    with pytest.raises(wf.InvalidTransition), session_scope(db) as s:
        prop.decide(s, BDM, opp_id, "Approved")        # not under review yet
    with session_scope(db) as s:
        prop.start_review(s, LEGAL, opp_id)
    with pytest.raises(wf.NotAllowed), session_scope(db) as s:
        prop.decide(s, SS, opp_id, "Approved")
    with session_scope(db) as s:
        assert prop.decide(s, BDM, opp_id, "Approved") is None
    with pytest.raises(wf.InvalidTransition, match="already approved"), session_scope(db) as s:
        prop.decide(s, BDM, opp_id, "Approved")
    with pytest.raises(wf.InvalidTransition, match="Only an approved"), session_scope(db) as s:
        prop.mark_submitted(s, BDM, opp_id, "2027-01-15")
    with session_scope(db) as s:
        assert prop.decide(s, LEGAL, opp_id, "Approved").version_label == "1.0"
    assert _snap(db, opp_id, "PROPOSAL")["status"] == "Approved"


def test_returned_needs_a_comment_and_a_new_version_before_review_again(db):
    opp_id = _approved_estimate(db)
    _proposal(db, opp_id)
    with session_scope(db) as s:
        prop.start_review(s, BDM, opp_id)
        prop.decide(s, BDM, opp_id, "Approved")
    with pytest.raises(ValueError, match="comment"), session_scope(db) as s:
        prop.decide(s, LEGAL, opp_id, "Returned")
    with session_scope(db) as s:
        assert prop.decide(s, LEGAL, opp_id, "Returned", "Fix the warranty wording") is None
    assert _status(db, opp_id) == wf.PROPOSAL_GENERATED
    with pytest.raises(wf.InvalidTransition, match="returned"), session_scope(db) as s:
        prop.start_review(s, BDM, opp_id)
    _proposal(db, opp_id)
    assert _snap(db, opp_id, "PROPOSAL")["label"] == "0.2"
    with session_scope(db) as s:
        prop.start_review(s, BDM, opp_id)
        assert prop.decide(s, LEGAL, opp_id, "Approved") is None      # the earlier approval was for another version
        assert prop.decide(s, BDM, opp_id, "Approved").version_label == "1.0"


def _edit_client(data: bytes, fn) -> bytes:
    doc = Document(io.BytesIO(data))
    fn(doc)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _replace(doc, old: str, new: str) -> None:
    for t in doc.tables:
        for r in t.rows:
            for c in r.cells:
                if old in c.text:
                    c.text = c.text.replace(old, new)


def test_an_edited_price_is_refused_and_nothing_is_saved(db):
    opp_id = _approved_estimate(db)
    _proposal(db, opp_id)
    snap = _snap(db, opp_id)
    price = proposal._php(snap["results"]["costing"]["scenarios"]["B"]["price_ex_vat"])
    with session_scope(db) as s:
        client = prop.client_copy(s, opp_id)
    edited = _edit_client(client, lambda d: _replace(d, price, "PHP 1,000.00"))
    with pytest.raises(prop.ProposalMismatch, match="price excluding VAT"), session_scope(db) as s:
        prop.reupload(s, BDM, opp_id, edited)
    assert _snap(db, opp_id, "PROPOSAL")["label"] == "0.1"
    with session_scope(db) as s:
        assert s.scalars(select(models.AuditLog).where(models.AuditLog.action == "proposal.reupload_blocked")).one().actor_name == "Bea"


@pytest.mark.parametrize("edit,reason", [(lambda d: d.add_paragraph("The gross margin here is healthy."), "internal data"),
                                         (lambda d: d.add_paragraph("Delivery lead: [NAME]"), "NAME")])
def test_an_edited_file_with_a_leak_or_a_placeholder_is_refused(db, edit, reason):
    opp_id = _approved_estimate(db)
    _proposal(db, opp_id)
    with session_scope(db) as s:
        client = prop.client_copy(s, opp_id)
    with pytest.raises(prop.ProposalMismatch, match=reason), session_scope(db) as s:
        prop.reupload(s, BDM, opp_id, _edit_client(client, edit))


def test_a_wording_edit_is_a_new_version_and_is_what_gets_downloaded(db):
    opp_id = _approved_estimate(db)
    _proposal(db, opp_id)
    with session_scope(db) as s:
        client = prop.client_copy(s, opp_id)
    edited = _edit_client(client, lambda d: d.add_paragraph("We look forward to working with you."))
    with session_scope(db) as s:
        assert prop.reupload(s, BDM, opp_id, edited).version_label == "0.2"
    with session_scope(db) as s:
        assert "We look forward" in _text_of(prop.client_copy(s, opp_id))
    with pytest.raises(prop.ProposalMismatch, match="not a readable"), session_scope(db) as s:
        prop.reupload(s, BDM, opp_id, b"not a word file")


def test_a_brd_change_outdates_the_proposal_and_blocks_it(db):
    opp_id = _approved_proposal(db)
    with session_scope(db) as s:
        svc.retire_br(s, SS, opp_id, "BR-002")
    assert _status(db, opp_id) == wf.BRD_VALIDATION
    assert _snap(db, opp_id, "PROPOSAL")["outdated"] is True and _snap(db, opp_id)["outdated"] is True
    with pytest.raises(wf.InvalidTransition), session_scope(db) as s:
        prop.mark_submitted(s, BDM, opp_id, "2027-01-15")
    with pytest.raises(wf.InvalidTransition, match="after the estimate is approved"), session_scope(db) as s:
        prop.trigger(s, BDM, opp_id, "B", _high_risks(db, opp_id))


def test_submission_is_recorded_with_its_date(db):
    opp_id = _approved_proposal(db)
    with session_scope(db) as s:
        state = ops.where_you_left_off(s, opp_id)
        assert (state["pending"], state["next_role"]) == ("Mark the proposal as submitted", wf.BDM)
    with pytest.raises(wf.NotAllowed), session_scope(db) as s:
        prop.mark_submitted(s, LEGAL, opp_id, "2027-01-15")
    with pytest.raises(ValueError, match="date"), session_scope(db) as s:
        prop.mark_submitted(s, BDM, opp_id, "")
    with session_scope(db) as s:
        prop.mark_submitted(s, BDM, opp_id, "2027-01-15")
    assert _status(db, opp_id) == wf.PROPOSAL_SUBMITTED and _snap(db, opp_id, "PROPOSAL")["submitted_on"] == "2027-01-15"


# ---------- W7: award ----------

def test_award_is_recorded_only_by_the_bdm_after_submission(db):
    opp_id = _approved_proposal(db)
    with pytest.raises(wf.InvalidTransition, match="after the proposal is submitted"), session_scope(db) as s:
        prop.record_award(s, BDM, opp_id, prop.AWARDED, "2027-02-01")
    with session_scope(db) as s:
        prop.mark_submitted(s, BDM, opp_id, "2027-01-15")
    for actor in (SS, LEGAL, ADMIN):
        with pytest.raises(wf.NotAllowed), session_scope(db) as s:
            prop.record_award(s, actor, opp_id, prop.AWARDED, "2027-02-01")
    with pytest.raises(ValueError, match="date"), session_scope(db) as s:
        prop.record_award(s, BDM, opp_id, prop.AWARDED, "")


def test_pending_keeps_the_status_and_only_awarded_unlocks_contracts(db):
    opp_id = _submitted(db)
    with session_scope(db) as s:
        opp = prop.record_award(s, BDM, opp_id, prop.PENDING, "2027-02-01", "Waiting for the board")
        assert opp.status == wf.PROPOSAL_SUBMITTED and not prop.contracts_unlocked(opp)
    with session_scope(db) as s:
        opp = prop.record_award(s, BDM, opp_id, prop.AWARDED, "2027-02-10", "Won")
        assert opp.status == wf.AWARDED and prop.contracts_unlocked(opp) and opp.award["note"] == "Won"
        rows = s.scalars(select(models.AuditLog).where(models.AuditLog.action == "award.recorded").order_by(models.AuditLog.id)).all()
        assert [r.details["outcome"] for r in rows] == [prop.PENDING, prop.AWARDED]


@pytest.mark.parametrize("outcome", [prop.NOT_AWARDED, prop.WITHDRAWN])
def test_a_lost_or_withdrawn_opportunity_closes_read_only(db, outcome):
    opp_id = _submitted(db)
    with session_scope(db) as s:
        opp = prop.record_award(s, BDM, opp_id, outcome, "2027-02-01")
        assert opp.status == wf.CLOSED and not prop.contracts_unlocked(opp)
        row_version = opp.row_version
    with pytest.raises(wf.InvalidTransition, match="closed"), session_scope(db) as s:
        ops.update_intake(s, BDM, opp_id, row_version, name="Changed")
    with pytest.raises(wf.InvalidTransition, match="closed"), session_scope(db) as s:
        ops.add_document(s, BDM, opp_id, DocInput(name="b.md", kind="other", pages=[{"page": 1, "text": "x"}]), "Other")
    with pytest.raises(wf.InvalidTransition), session_scope(db) as s:
        svc.retire_br(s, SS, opp_id, "BR-002")
    with pytest.raises(wf.InvalidTransition), session_scope(db) as s:
        est.edit(s, SS, opp_id, _snap(db, opp_id)["label"], sizes={"BR-001": "XL"})
    with pytest.raises(wf.InvalidTransition), session_scope(db) as s:
        prop.record_award(s, BDM, opp_id, prop.AWARDED, "2027-03-01")
    with session_scope(db) as s:
        assert prop.client_copy(s, opp_id)      # still viewable


# ---------- the question marker follows the renumbering and the answer ----------

def test_v2_question_references_follow_the_renumbering(db):
    opp_id = _approved_brd(db)
    with session_scope(db) as s:
        br = s.scalars(select(models.Br).where(models.Br.br_id == "BR-014")).one()
        assert "OQ-001" in br.title and "Q-01" not in br.title.replace("OQ-001", "")


def test_answering_a_question_removes_its_marker(db):
    opp_id = _approved_brd(db)
    _apply_bulletin(db, opp_id)
    with session_scope(db) as s:
        br = s.scalars(select(models.Br).where(models.Br.br_id == "BR-014")).one()
        q = s.scalars(select(models.OpenQuestion).where(models.OpenQuestion.item_id == "OQ-001")).one()
        assert q.status == "Answered"
        assert "details to be confirmed" not in br.title and "details to be confirmed" not in br.statement


# ---------- BRD drafts on the store (section 11) ----------

def _header(path) -> str:
    return " ".join(p.text for section in Document(path).sections for p in section.header.paragraphs)


def test_every_brd_draft_reaches_the_brd_folder_marked_draft_and_approval_marks_only_the_approved_one(db, tmp_path, monkeypatch):
    root = tmp_path / "store"
    monkeypatch.setenv("STORAGE_LOCAL_ROOT", str(root))
    with session_scope(db) as s:
        opp_id = ops.create_sample_hris(s, BDM)
        run_id = ops.start_processing(s, BDM, opp_id)
    ops.run_processing(run_id, opp_id, db)
    with session_scope(db) as s:
        assert ops.current_version(s, opp_id, "BRD").publish_status == "published"
    folder = next(p for p in root.iterdir() if p.name.startswith("HRIS")) / "BRD"
    assert [p.name.split("-v")[-1] for p in folder.glob("*.docx")] == ["0.1.docx"]
    assert "DRAFT" in _header(next(folder.glob("*v0.1.docx")))
    assert not (folder / "current").exists() and not (folder / "versions").exists()
    with session_scope(db) as s:
        changes = svc.empty_changes()
        changes["new"] = [{"title": "Added", "statement": "The system shall do one more thing.", "epic_id": "EP-01", "priority": "Must"}]
        svc.apply_changes(s, SS, opp_id, changes, "in-app edit")
    assert "DRAFT" in _header(next(folder.glob("*v0.2.docx")))
    with session_scope(db) as s:
        svc.accept_all_remaining(s, SS, opp_id)
        svc.acknowledge_question(s, SS, opp_id, "OQ-001")
        svc.approve(s, SS, opp_id)
    assert sorted(p.name.split("-v")[-1] for p in folder.glob("BRD-*.docx")) == ["0.1.docx", "0.2.docx", "1.0.docx"]
    assert "APPROVED" in _header(next(folder.glob("*v1.0.docx"))) and "DRAFT" in _header(next(folder.glob("*v0.2.docx")))


def test_a_draft_that_cannot_be_published_stays_saved_and_marked_pending(db, tmp_path, monkeypatch):
    from app import publishing, storage
    monkeypatch.setenv("STORAGE_LOCAL_ROOT", str(tmp_path / "store"))
    with session_scope(db) as s:
        opp_id = ops.create_sample_hris(s, BDM)
        run_id = ops.start_processing(s, BDM, opp_id)

    def down():
        raise storage.Unavailable("down")
    monkeypatch.setattr(publishing.storage, "get_store", down)
    ops.run_processing(run_id, opp_id, db)
    with session_scope(db) as s:
        assert ops.latest_run(s, opp_id).status == "Succeeded"
        assert ops.current_version(s, opp_id, "BRD").publish_status == "pending"

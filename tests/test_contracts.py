"""W8, W9 and the items before them, on a throwaway PostgreSQL and the local store: question audiences, the re-upload estimate impact, rule-sized BRs, the SOW, the MSA
filled from a made-up template, final approvals, publishing and its failure modes, and the Drive adapter on a fake service. No Azure calls and no real Drive."""
import io
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from docx import Document

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app import brd_docx, brd_service as svc, contract_service as cs, estimate_service as est, llm, models, opportunities as ops, proposal_service as prop, publishing, storage, store, workflow as wf  # noqa: E402
from app.db import session_scope  # noqa: E402
from tests import test_estimate_proposal as T  # noqa: E402
from tests.test_estimate_proposal import BDM, LEGAL, SS, _approved_brd, _approved_estimate, _submitted, _generated, _proposal, _snap, _status, _text_of, db  # noqa: E402,F401

SECRET_CLAUSE = "The parties hereby agree that this made-up clause body must never leave the template."
FIELDS = {"client_address": "1 Fictional Road, Manila", "client_representative": "Rene Santos", "representative_designation": "HR Director", "effective_date": "2027-03-01"}


def _template(unknown_field: bool = False) -> bytes:
    doc = Document()
    doc.add_paragraph("MASTER SERVICES AGREEMENT")
    doc.add_paragraph("This agreement is between Exist and <INSERT CLIENT NAME> as the client.")
    for heading in ("5. Intellectual Property Rights", "6. Confidentiality", "7. Limitation of Liability", "8. Term and Termination", "9. Payment Terms", "10. Data Privacy", "11. Dispute Resolution", "12. Notices"):
        doc.add_paragraph(heading)
        doc.add_paragraph(SECRET_CLAUSE)
    table = doc.add_table(rows=2, cols=2)
    table.rows[0].cells[0].text = "Name: <Insert Client's Representative Name>"
    table.rows[1].cells[0].text = "Title: <Designation>"
    if unknown_field:
        table.rows[1].cells[1].text = "Witness: <Unknown Field>"
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


@pytest.fixture
def template(monkeypatch, tmp_path):
    root = tmp_path / "store"
    (root / "templates").mkdir(parents=True)
    (root / "templates" / "Master Services Agreement.docx").write_bytes(_template())
    monkeypatch.setenv("STORAGE_LOCAL_ROOT", str(root))
    return root


def _awarded(db) -> str:
    opp_id = _submitted(db)
    with session_scope(db) as s:
        prop.record_award(s, BDM, opp_id, prop.AWARDED, "2027-02-01")
    return opp_id


def _with_fields(db, opp_id) -> None:
    with session_scope(db) as s:
        seen = s.get(models.Opportunity, opp_id).row_version
    with session_scope(db) as s:
        cs.update_fields(s, BDM, opp_id, FIELDS, seen)


def _generated_contracts(db) -> str:
    opp_id = _awarded(db)
    _with_fields(db, opp_id)
    with session_scope(db) as s:
        cs.generate(s, BDM, opp_id)
    return opp_id


def _in_review(db) -> str:
    opp_id = _generated_contracts(db)
    with session_scope(db) as s:
        cs.start_review(s, LEGAL, opp_id)
        msa = ops.current_version(s, opp_id, "MSA")
        count = len(msa.snapshot["flags"])
    with session_scope(db) as s:
        for i in range(count):
            cs.set_clause_reviewed(s, LEGAL, opp_id, i, True)
    return opp_id


# ---------- 1: open questions by audience ----------

def test_the_client_copy_shows_only_client_questions_worded_as_clarification_requests(db):
    opp_id = _approved_estimate(db)
    _proposal(db, opp_id)
    with session_scope(db) as s:
        client, internal = _text_of(prop.client_copy(s, opp_id)), _text_of(prop.internal_copy(s, opp_id))
        assert s.scalars(select(models.OpenQuestion).where(models.OpenQuestion.opportunity_id == opp_id, models.OpenQuestion.source == "brd_section")).one().audience == "internal"
    assert "no KPIs" not in client and "no KPIs" in internal and prop.INTERNAL_QUESTIONS in internal and prop.INTERNAL_QUESTIONS not in client
    with session_scope(db) as s:
        paragraphs = [p.text for p in Document(io.BytesIO(prop.client_copy(s, opp_id))).paragraphs]
    start = paragraphs.index(prop.CONFIRM_HEADING)
    end = next(i for i in range(start + 1, len(paragraphs)) if paragraphs[i] == "Delivery Methodology")
    listed = paragraphs[start + 1:end]
    assert listed and all(line.startswith("Please confirm") for line in listed)
    assert "Please confirm whether Data Migration is required." in listed
    assert not any("BR-" in line or "OQ-" in line or "inferred" in line for line in listed)


def test_the_llm_wording_is_used_when_it_is_clean_and_ignored_when_not(db, monkeypatch):
    opp_id = _approved_brd(db)
    good = "Please confirm whether local authentication or SSO is preferred for login."

    def fake(messages, schema, **kwargs):
        assert kwargs.get("role") == "LIGHT"
        return schema(items=[{"item_id": "OQ-003", "text": good}, {"item_id": "OQ-004", "text": "Please confirm whether BR-001 is sufficient."}, {"item_id": "OQ-002", "text": "Is 42 right?"}])

    monkeypatch.setattr(llm, "structured", fake)
    with session_scope(db) as s:
        lists = prop.question_lists(s, opp_id)
    assert good in lists["client"]
    assert not any("BR-001" in q or "42" in q for q in lists["client"])
    assert all(q.startswith("Please confirm") for q in lists["client"])
    calls = []
    monkeypatch.setattr(llm, "structured", lambda *a, **k: calls.append(1))
    with session_scope(db) as s:
        again = prop.question_lists(s, opp_id)
    assert not calls and again == lists     # written once, kept on the question


def test_a_specialist_can_retag_a_question_and_answered_ones_drop_out(db):
    opp_id = _approved_brd(db)
    with pytest.raises(wf.NotAllowed), session_scope(db) as s:
        svc.set_question_audience(s, BDM, opp_id, "OQ-002", "internal")
    with session_scope(db) as s:
        svc.set_question_audience(s, SS, opp_id, "OQ-002", "internal")
        s.scalars(select(models.OpenQuestion).where(models.OpenQuestion.opportunity_id == opp_id, models.OpenQuestion.item_id == "OQ-003")).one().status = "Answered"
    with session_scope(db) as s:
        lists = prop.question_lists(s, opp_id)
        texts = {q.item_id: q.text for q in s.scalars(select(models.OpenQuestion).where(models.OpenQuestion.opportunity_id == opp_id))}
    assert texts["OQ-002"] in lists["internal"]
    assert not any("SSO" in q for q in lists["client"] + lists["internal"])


def test_changing_an_audience_is_not_a_content_change(db):
    opp_id = _approved_brd(db)
    with session_scope(db) as s:
        before = store.brd_snapshot(s, opp_id)["fingerprints"]["items"]
        svc.set_question_audience(s, SS, opp_id, "OQ-002", "internal")
        assert store.brd_snapshot(s, opp_id)["fingerprints"]["items"] == before


# ---------- 2: re-upload preview ----------

def _edited_brd(db, opp_id) -> dict:
    def edit(doc, table):
        row = next(r for r in table.rows if r.cells[0].text == "BR-003")
        row.cells[brd_docx.BR_COLUMNS.index("Requirement Statement")].text = " ".join([row.cells[brd_docx.BR_COLUMNS.index("Requirement Statement")].text] * 2)
        new = table.add_row().cells
        new[brd_docx.BR_COLUMNS.index("Epic")].text = "EP-01"
        new[brd_docx.BR_COLUMNS.index("Title")].text = "Added in Word"
        new[brd_docx.BR_COLUMNS.index("Requirement Statement")].text = "The system shall do one more thing."
        new[brd_docx.BR_COLUMNS.index("Priority")].text = "Must"

    with session_scope(db) as s:
        data = brd_docx.render(s, opp_id)
    doc = Document(io.BytesIO(data))
    edit(doc, next(t for t in doc.tables if t.rows[0].cells[0].text == brd_docx.BR_ID_HEADER))
    buf = io.BytesIO()
    doc.save(buf)
    with session_scope(db) as s:
        return brd_docx.diff(s, opp_id, buf.getvalue(), {0: None})


def test_the_reupload_preview_shows_the_man_day_and_price_delta_and_writes_nothing(db, monkeypatch):
    opp_id = _approved_estimate(db)
    calls = []
    monkeypatch.setattr(llm, "structured", lambda *a, **k: calls.append(1))
    result = _edited_brd(db, opp_id)
    with session_scope(db) as s:
        before = (s.get(models.Opportunity, opp_id).status, s.get(models.Opportunity, opp_id).row_version, ops.current_version(s, opp_id, "BRD").version_label, ops.current_version(s, opp_id, "ESTIMATE").version_label, len(s.scalars(select(models.AuditLog)).all()))
        delta = est.preview_delta(s, SS, opp_id, result["changes"])
    assert not calls
    rows = {r["br_id"]: r for r in delta["rows"]}
    assert set(rows) == {"BR-003", "BR-030"} and rows["BR-030"]["change"] == "added" and rows["BR-030"]["man_days_to"]["B"] > 0
    totals = delta["totals"]["B"]
    assert totals["to"]["man_days"] > totals["from"]["man_days"] and totals["to"]["price_ex_vat"] > totals["from"]["price_ex_vat"]
    with session_scope(db) as s:
        after = (s.get(models.Opportunity, opp_id).status, s.get(models.Opportunity, opp_id).row_version, ops.current_version(s, opp_id, "BRD").version_label, ops.current_version(s, opp_id, "ESTIMATE").version_label, len(s.scalars(select(models.AuditLog)).all()))
        assert "BR-030" not in svc.all_brs(s, opp_id)
        assert store.allocate_id(s, opp_id, "BR") == "BR-030"       # the preview used no number
        s.rollback()
    assert after == before


def test_the_preview_is_silent_without_an_estimate(db):
    opp_id = _approved_brd(db)
    with session_scope(db) as s:
        changes = svc.empty_changes()
        changes["removed"] = ["BR-002"]
        assert est.preview_delta(s, SS, opp_id, changes) is None


# ---------- 3: rule-sized ----------

def test_rule_sized_brs_are_marked_until_a_person_sets_the_size(db):
    opp_id = _approved_brd(db)
    with session_scope(db) as s:
        changes = svc.empty_changes()
        changes["new"] = [{"title": "Added", "statement": "The system shall do one more thing.", "epic_id": "EP-01", "priority": "Must"}]
        svc.apply_changes(s, SS, opp_id, changes, "in-app edit")
        svc.accept_all_remaining(s, SS, opp_id)
        svc.approve(s, SS, opp_id)
        est.generate(s, SS, opp_id)
    snap = _snap(db, opp_id)
    assert snap["rows"]["BR-030"]["rule_sized"] is True and snap["rows"]["BR-001"]["rule_sized"] is False
    with session_scope(db) as s:
        assert est.RULE_SIZED in _text_of(est.summary_docx(s, opp_id))
        est.edit(s, SS, opp_id, snap["label"], sizes={"BR-030": "M"})
    assert _snap(db, opp_id)["rows"]["BR-030"]["rule_sized"] is False


def test_a_statement_that_grew_a_lot_is_marked_rule_sized(db):
    opp_id = _approved_brd(db)
    with session_scope(db) as s:
        br = svc.all_brs(s, opp_id)["BR-003"]
        changes = svc.empty_changes()
        changes["modified"] = [{"br_id": "BR-003", "changes": {"statement": " ".join([br.statement] * 2)}}]
        svc.apply_changes(s, SS, opp_id, changes, "in-app edit")
        assert svc.all_brs(s, opp_id)["BR-003"].ext["size_source"] == "rule"


# ---------- W8: SOW ----------

def test_contracts_need_an_awarded_decision_and_the_bdm(db, template):
    opp_id = _submitted(db)
    with pytest.raises(wf.InvalidTransition, match="Awarded"), session_scope(db) as s:
        cs.generate(s, BDM, opp_id)
    with session_scope(db) as s:
        prop.record_award(s, BDM, opp_id, prop.PENDING, "2027-02-01")
    with pytest.raises(wf.InvalidTransition, match="Awarded"), session_scope(db) as s:
        cs.generate(s, BDM, opp_id)
    with session_scope(db) as s:
        prop.record_award(s, BDM, opp_id, prop.AWARDED, "2027-02-02")
    for actor in (SS, LEGAL):
        with pytest.raises(wf.NotAllowed), session_scope(db) as s:
            cs.generate(s, actor, opp_id)


def test_the_sow_has_every_section_comes_from_the_record_and_is_marked(db, template):
    opp_id = _generated_contracts(db)
    assert _status(db, opp_id) == wf.CONTRACTS_GENERATED
    snap = _snap(db, opp_id, "SOW")
    assert (snap["label"], snap["proposal_version"], snap["brd_version"], snap["estimate_version"]) == ("0.1", "1.0", "1.0", "1.0") and snap["leak_hits"] == []
    with session_scope(db) as s:
        data = cs.sow_bytes(s, opp_id)
    text = _text_of(data)
    doc = Document(io.BytesIO(data))
    assert doc.sections[0].header.paragraphs[0].text == cs.SOW_MARK
    headings = [p.text for p in doc.paragraphs if p.style.name == "Heading 1"]
    assert headings == ["Parties and Effective Date", "Project Objectives and Scope", "Functional and Non-Functional Requirements", "Deliverables", "Implementation Schedule and Milestones", "Roles and Responsibilities",
                        "Commercial Fees and Payment Milestones", "Acceptance Criteria for Deliverables", "Client Dependencies", "Assumptions and Exclusions", "Change Control", "Warranty and Support"]
    price = _snap(db, opp_id)["results"]["costing"]["scenarios"]["B"]["price_ex_vat"]
    assert f"PHP {price:,.2f}" in text and "BR-001" in text and "1 Fictional Road, Manila" in text and "[MISSING" not in text
    assert "BR-010" not in text     # suggested scope is not priced, so not committed
    assert "gross margin" not in text.lower() and "contingency" not in text.lower()


def test_missing_contract_details_are_flagged_and_block_the_final_approval(db, template):
    opp_id = _awarded(db)
    with session_scope(db) as s:
        cs.generate(s, BDM, opp_id)
    with session_scope(db) as s:
        text = _text_of(cs.sow_bytes(s, opp_id))
        check = cs.consistency(s, opp_id)
    assert "[MISSING: client address]" in text and "[MISSING: effective date]" in text
    assert not check["ok"] and any("Client address" in m for m in check["mismatches"])


def test_the_summary_falls_back_when_the_llm_adds_a_number_or_fails(db, template, monkeypatch):
    monkeypatch.setattr(llm, "structured", lambda m, schema, **k: schema(summary="We commit to 99 percent availability."))
    assert "99" not in cs._objectives_summary(["Automate leave filing."], ["Leave"], False, None)
    monkeypatch.setattr(llm, "structured", lambda m, schema, **k: schema(summary="The project automates leave filing."))
    assert cs._objectives_summary(["Automate leave filing, today."], ["Leave"], False, None) == "The project automates leave filing."


# ---------- W8: MSA ----------

def test_the_msa_is_filled_by_code_and_its_wording_is_untouched(monkeypatch):
    fields = {"client_name": "Fictional Co", "client_representative": "Rene Santos", "representative_designation": "HR Director"}
    data, missing, flags = cs.fill_msa(_template(unknown_field=True), fields)
    text = _text_of(data)
    assert "between Exist and Fictional Co as the client." in text and "Name: Rene Santos" in text and "Title: HR Director" in text
    assert text.count(SECRET_CLAUSE) == 8
    assert missing == ["Unknown Field"] and "[MISSING: Unknown Field]" in text
    doc = Document(io.BytesIO(data))
    assert any(cs.MSA_MARK in p.text for p in doc.sections[0].header.paragraphs)


def test_an_unmapped_placeholder_blocks_the_final_approval(db, template):
    (template / "templates" / "Master Services Agreement.docx").write_bytes(_template(unknown_field=True))
    opp_id = _in_review(db)
    with pytest.raises(wf.InvalidTransition, match="Unknown Field"), session_scope(db) as s:
        cs.approve(s, BDM, opp_id, "commercial")


def test_the_clause_review_list_holds_headings_never_clause_text():
    _, _, flags = cs.fill_msa(_template(), {"client_name": "C"})
    categories = {f["category"] for f in flags}
    assert categories == {"Intellectual property", "Confidentiality", "Liability and indemnification", "Term and termination", "Payment", "Data privacy", "Dispute resolution"}
    assert all(SECRET_CLAUSE not in f["heading"] for f in flags) and "12. Notices" not in [f["heading"] for f in flags]


def test_a_bold_lead_in_is_the_heading_and_the_clause_body_stays_out():
    doc = Document()
    paragraph = doc.add_paragraph()
    paragraph.add_run("CONFIDENTIALITY.").bold = True
    paragraph.add_run(" " + SECRET_CLAUSE)
    table = doc.add_table(rows=1, cols=1)
    cell = table.rows[0].cells[0].paragraphs[0]
    cell.add_run("Dispute Resolution.").bold = True
    cell.add_run(" " + SECRET_CLAUSE)
    doc.add_paragraph(SECRET_CLAUSE)
    buf = io.BytesIO()
    doc.save(buf)
    _, _, flags = cs.fill_msa(buf.getvalue(), {"client_name": "C"})
    assert [(f["category"], f["heading"]) for f in flags] == [("Confidentiality", "CONFIDENTIALITY"), ("Dispute resolution", "Dispute Resolution")]


def test_clause_text_never_reaches_the_database_the_logs_or_the_llm(db, template, monkeypatch, caplog):
    sent = []
    monkeypatch.setattr(llm, "structured", lambda messages, schema, **k: sent.append(json.dumps(messages)) or (_ for _ in ()).throw(RuntimeError("no")))
    with caplog.at_level(logging.DEBUG):
        opp_id = _generated_contracts(db)
    with session_scope(db) as s:
        versions = s.scalars(select(models.ArtifactVersion).where(models.ArtifactVersion.artifact_type.in_(("MSA", "SOW")))).all()
        stored = json.dumps([v.snapshot for v in versions]) + json.dumps([a.details for a in s.scalars(select(models.AuditLog))])
    assert SECRET_CLAUSE not in stored and SECRET_CLAUSE not in caplog.text and not any(SECRET_CLAUSE in m for m in sent)
    assert _snap(db, opp_id, "MSA")["template_sha256"]


def test_without_a_template_the_sow_still_generates_and_the_msa_says_why(db, monkeypatch, tmp_path):
    monkeypatch.setenv("STORAGE_LOCAL_ROOT", str(tmp_path / "empty"))
    opp_id = _awarded(db)
    _with_fields(db, opp_id)
    with session_scope(db) as s:
        result = cs.generate(s, BDM, opp_id)
    assert result["msa"] is None and result["msa_message"] == cs.MSA_UNAVAILABLE and result["sow"] == "0.1"
    with session_scope(db) as s:
        assert ops.current_version(s, opp_id, "MSA") is None
        check = cs.consistency(s, opp_id)
        assert not check["ok"] and cs.MSA_UNAVAILABLE in check["mismatches"]


def test_a_drive_failure_reading_the_template_is_the_same_clear_message(db, monkeypatch, template):
    opp_id = _awarded(db)

    def broken():
        raise storage.AuthExpired("expired")

    real = storage.get_store
    monkeypatch.setattr(cs.storage, "get_store", lambda: SimpleNamespace(read_template=broken, put=real().put, list_files=real().list_files, delete=real().delete))
    with session_scope(db) as s:
        assert cs.generate(s, BDM, opp_id)["msa_message"] == cs.MSA_UNAVAILABLE


# ---------- iteration: redraws reuse documents and checks until a version changes ----------

class _Counting:
    """Counts template reads and SOW builds."""
    def __init__(self, monkeypatch):
        self.reads, self.builds, real_load, real_render = [], [], cs._load_template, cs.render_sow
        monkeypatch.setattr(cs, "_load_template", lambda: self.reads.append(1) or real_load())
        monkeypatch.setattr(cs, "render_sow", lambda *a: self.builds.append(1) or real_render(*a))

    def reset(self) -> None:
        self.reads.clear()
        self.builds.clear()


def test_a_redraw_rebuilds_nothing_and_a_clause_tick_is_not_a_change(db, template, monkeypatch):
    opp_id = _in_review(db)
    with session_scope(db) as s:
        first = cs.consistency(s, opp_id)
    counting = _Counting(monkeypatch)
    with session_scope(db) as s:
        assert cs.consistency(s, opp_id, fresh=False) == first
        for part in cs.PARTS:
            cs.approval_blockers(s, opp_id, part, fresh=False)
        cs.msa_bytes(s, opp_id, fresh=False)
        cs.set_clause_reviewed(s, LEGAL, opp_id, 0, True)
    with session_scope(db) as s:
        assert cs.consistency(s, opp_id, fresh=False) == first
    assert counting.reads == [] and counting.builds == []


def test_a_change_to_what_the_check_reads_is_seen_at_the_next_redraw(db, template, monkeypatch):
    opp_id = _in_review(db)
    with session_scope(db) as s:
        assert cs.consistency(s, opp_id, fresh=False)["ok"]
    with session_scope(db) as s:
        version = ops.current_version(s, opp_id, "SOW")
        version.snapshot = {**version.snapshot, "leak_hits": [{"section": "Commercial"}]}
    with session_scope(db) as s:
        assert any("holds internal data" in m for m in cs.consistency(s, opp_id, fresh=False)["mismatches"])
    with session_scope(db) as s:
        opp = s.get(models.Opportunity, opp_id)
        opp.commercial = {**opp.commercial, "contract": {**opp.commercial["contract"], "client_address": ""}}
    with session_scope(db) as s:
        assert any("Client address" in m for m in cs.consistency(s, opp_id, fresh=False)["mismatches"])


def test_a_decision_rechecks_so_a_cached_pass_cannot_approve_without_the_template(db, template, monkeypatch):
    opp_id = _in_review(db)
    with session_scope(db) as s:
        assert cs.consistency(s, opp_id, fresh=False)["ok"]
        for i, _ in enumerate(ops.current_version(s, opp_id, "MSA").snapshot["flags"]):
            cs.set_clause_reviewed(s, LEGAL, opp_id, i, True)
    monkeypatch.setattr(cs, "_load_template", lambda: None)
    with session_scope(db) as s:
        assert cs.consistency(s, opp_id, fresh=False)["ok"]     # the redraw reuses the pass
        assert any(cs.MSA_UNAVAILABLE in b for b in cs.approval_blockers(s, opp_id, "legal"))
    with pytest.raises(wf.InvalidTransition), session_scope(db) as s:
        cs.approve(s, LEGAL, opp_id, "legal")
    with session_scope(db) as s:     # and the failed check took the pass out
        assert cs.MSA_UNAVAILABLE in cs.consistency(s, opp_id, fresh=False)["mismatches"]


def test_a_cached_document_changes_with_its_version(db, template):
    opp_id = _in_review(db)
    with session_scope(db) as s:
        before, msa = cs.sow_bytes(s, opp_id), cs.msa_bytes(s, opp_id, fresh=False)
        assert cs.sow_bytes(s, opp_id) == before and cs.msa_bytes(s, opp_id, fresh=False) == msa
        client = prop.client_copy(s, opp_id)
        assert prop.client_copy(s, opp_id) == client
    with session_scope(db) as s:
        s.get(models.Opportunity, opp_id).name = "A Renamed Fictional Project"
    with session_scope(db) as s:
        assert "A Renamed Fictional Project" in _text_of(cs.sow_bytes(s, opp_id))


def test_the_cache_is_bounded_and_returns_copies():
    from app import doc_cache
    for i in range(doc_cache.MAX_ENTRIES + 5):
        doc_cache.put(str(i), {"n": i})
    assert doc_cache.get("0") is None and doc_cache.get(str(doc_cache.MAX_ENTRIES + 4)) == {"n": doc_cache.MAX_ENTRIES + 4}
    doc_cache.get(str(doc_cache.MAX_ENTRIES + 4))["n"] = "changed"
    assert doc_cache.get(str(doc_cache.MAX_ENTRIES + 4))["n"] == doc_cache.MAX_ENTRIES + 4


# ---------- W9: final approvals ----------

def test_three_approvals_in_any_order_reach_contract_ready(db, template):
    opp_id = _in_review(db)
    assert _status(db, opp_id) == wf.LEGAL_REVIEW
    with session_scope(db) as s:
        check = cs.consistency(s, opp_id)
        assert check["ok"], check["mismatches"]
    with session_scope(db) as s:
        assert cs.approve(s, LEGAL, opp_id, "legal") == wf.LEGAL_REVIEW
    with session_scope(db) as s:
        assert cs.approve(s, SS, opp_id, "scope") == wf.LEGAL_REVIEW
    with pytest.raises(wf.InvalidTransition, match="already recorded"), session_scope(db) as s:
        cs.approve(s, SS, opp_id, "scope")
    with session_scope(db) as s:
        assert cs.approve(s, BDM, opp_id, "commercial") == wf.CONTRACT_READY
    assert _status(db, opp_id) == wf.CONTRACT_READY
    assert (_snap(db, opp_id, "SOW")["label"], _snap(db, opp_id, "MSA")["label"]) == ("1.0", "1.0")
    assert _snap(db, opp_id, "SOW")["status"] == "Approved"


@pytest.mark.parametrize("actor,part", [(BDM, "legal"), (BDM, "scope"), (SS, "commercial"), (SS, "legal"), (LEGAL, "commercial"), (LEGAL, "scope")])
def test_each_approval_belongs_to_one_role(db, template, actor, part):
    opp_id = _in_review(db)
    with pytest.raises(wf.NotAllowed), session_scope(db) as s:
        cs.approve(s, actor, opp_id, part)


def test_legal_cannot_approve_until_every_flagged_clause_is_reviewed(db, template):
    opp_id = _generated_contracts(db)
    with session_scope(db) as s:
        cs.start_review(s, LEGAL, opp_id)
    with pytest.raises(wf.InvalidTransition, match="flagged clauses"), session_scope(db) as s:
        cs.approve(s, LEGAL, opp_id, "legal")
    with session_scope(db) as s:
        assert cs.approve(s, SS, opp_id, "scope") == wf.LEGAL_REVIEW     # the checklist is Legal's


def test_a_mismatch_blocks_the_final_approval(db, template):
    opp_id = _in_review(db)
    with session_scope(db) as s:
        sow = ops.current_version(s, opp_id, "SOW")
        sections = json.loads(json.dumps(sow.snapshot["sections"]).replace("PHP", "USD"))
        sow.snapshot = {**sow.snapshot, "sections": sections}
    with pytest.raises(wf.InvalidTransition, match="contract amount"), session_scope(db) as s:
        cs.approve(s, BDM, opp_id, "commercial")


def test_a_later_change_reopens_the_review_and_voids_the_approvals(db, template):
    opp_id = _in_review(db)
    with session_scope(db) as s:
        cs.approve(s, LEGAL, opp_id, "legal")
        cs.approve(s, SS, opp_id, "scope")
        cs.approve(s, BDM, opp_id, "commercial")
    with session_scope(db) as s:
        assert cs.generate(s, BDM, opp_id)["sow"] == "1.1"
    assert _status(db, opp_id) == wf.CONTRACTS_GENERATED
    with session_scope(db) as s:
        cs.start_review(s, LEGAL, opp_id)
        assert not any(p["approved"] for p in cs.status_of_parts(s, opp_id).values())


def test_a_brd_change_after_contract_ready_outdates_everything(db, template):
    opp_id = _in_review(db)
    with session_scope(db) as s:
        cs.approve(s, LEGAL, opp_id, "legal")
        cs.approve(s, SS, opp_id, "scope")
        cs.approve(s, BDM, opp_id, "commercial")
        svc.retire_br(s, SS, opp_id, "BR-002")
    assert _status(db, opp_id) == wf.BRD_VALIDATION
    for artifact in ("ESTIMATE", "PROPOSAL", "SOW", "MSA"):
        assert _snap(db, opp_id, artifact)["outdated"] is True


def test_returning_needs_a_comment(db, template):
    opp_id = _in_review(db)
    with pytest.raises(ValueError, match="comment"), session_scope(db) as s:
        cs.return_for_changes(s, LEGAL, opp_id, " ")
    with session_scope(db) as s:
        cs.return_for_changes(s, LEGAL, opp_id, "Clause numbering")
    assert _status(db, opp_id) == wf.CONTRACTS_GENERATED


# ---------- W9: publishing ----------

def _tree(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and "templates" not in p.parts}


def test_every_stage_publishes_to_the_right_folder_with_location_and_audit(db, template):
    opp_id = _in_review(db)
    with session_scope(db) as s:
        cs.approve(s, LEGAL, opp_id, "legal")
        cs.approve(s, SS, opp_id, "scope")
        cs.approve(s, BDM, opp_id, "commercial")
        code = s.get(models.Opportunity, opp_id).code
        folder = publishing.folder_name(s.get(models.Opportunity, opp_id))
        rows = publishing.links(s, opp_id)
    tree = _tree(template)
    names = {f"{folder}/inputs/tor.pdf.masked.txt", f"{folder}/BRD/BRD-{code}-R1-v1.0.docx", f"{folder}/estimate/ESTIMATE-{code}-R1-v1.0-INTERNAL.docx",
             f"{folder}/proposal/PROPOSAL-{code}-R1-v1.0-CLIENT.docx", f"{folder}/proposal/PROPOSAL-{code}-R1-v1.0-INTERNAL.docx", f"{folder}/contracts/SOW-{code}-R1-v1.0.docx", f"{folder}/contracts/MSA-{code}-R1-v1.0.docx"}
    assert names <= tree, names - tree
    assert not any("estimate" in t and not t.endswith("-INTERNAL.docx") for t in tree if t.endswith(".docx"))
    published = [r for r in rows if r["publish"] == publishing.PUBLISHED]
    assert {r["artifact"] for r in published} == {"BRD", "ESTIMATE", "PROPOSAL", "SOW", "MSA"} and all(r["hash"] and r["path"] for r in published)
    with session_scope(db) as s:
        assert s.scalars(select(models.AuditLog).where(models.AuditLog.action == "artifact.published")).first() is not None
    internal = Document(template / folder / "estimate" / f"ESTIMATE-{code}-R1-v1.0-INTERNAL.docx")
    assert any("APPROVED" in p.text for p in internal.sections[0].header.paragraphs) and any("Revision history" == p.text for p in internal.paragraphs)


class _FailingStore:
    def __init__(self, error):
        self.error = error

    def put(self, *a, **k):
        raise self.error

    def list_files(self, *a, **k):
        raise self.error

    def delete(self, *a, **k):
        raise self.error

    def read_template(self):
        raise self.error


@pytest.mark.parametrize("error,status,message", [(storage.AuthExpired(storage.AUTH_EXPIRED_MESSAGE), publishing.AUTH_EXPIRED, True), (storage.Unavailable(storage.UNAVAILABLE_MESSAGE), publishing.PENDING, False)])
def test_a_store_failure_marks_publish_pending_and_the_workflow_goes_on(db, template, monkeypatch, error, status, message):
    opp_id = _approved_brd(db)
    with session_scope(db) as s:
        svc.retire_br(s, SS, opp_id, "BR-002")
        svc.accept_all_remaining(s, SS, opp_id)
    monkeypatch.setattr(publishing.storage, "get_store", lambda: _FailingStore(error))
    with session_scope(db) as s:
        svc.approve(s, SS, opp_id)
    assert _status(db, opp_id) == wf.BRD_APPROVED
    with session_scope(db) as s:
        waiting = publishing.pending(s, opp_id)
        assert [v.publish_status for v in waiting["versions"]] == [status] and waiting["auth_expired"] is message
    monkeypatch.undo()
    monkeypatch.setenv("STORAGE_LOCAL_ROOT", str(template))
    with session_scope(db) as s:
        assert publishing.retry(s, SS, opp_id) == {"tried": 1, "published": 1}
        assert publishing.pending(s, opp_id)["versions"] == []


def _no_styles(data: bytes) -> bytes:
    """Like the company template: none of the styles python-docx ships with a new document."""
    doc = Document(io.BytesIO(data))
    styles = doc.styles.element
    for style in list(styles):
        if style.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}styleId") not in ("Normal",):
            styles.remove(style)
    for paragraph in doc.paragraphs:
        if paragraph._p.pPr is not None and paragraph._p.pPr.pStyle is not None:
            paragraph._p.pPr.remove(paragraph._p.pPr.pStyle)
    for table in doc.tables:
        pr = table._tbl.tblPr
        for child in list(pr):
            if child.tag.endswith("tblStyle"):
                pr.remove(child)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_stamping_works_on_a_template_without_the_usual_styles_and_changes_none(db):
    bare = _no_styles(_template())
    filled, missing, flags = cs.fill_msa(bare, {"client_name": "Fictional Co", "client_representative": "R", "representative_designation": "D"})
    stamped = publishing.stamp_document(filled, "DRAFT", [("0.1", "2027-01-01", "Draft")])
    doc = Document(io.BytesIO(stamped))
    assert "Revision history" in [p.text for p in doc.paragraphs] and doc.tables[-1].rows[1].cells[0].text == "0.1"
    assert any("DRAFT" in p.text for p in doc.sections[0].header.paragraphs)
    assert _text_of(stamped).count(SECRET_CLAUSE) == 8
    before = Document(io.BytesIO(filled))
    assert [s.name for s in doc.styles] == [s.name for s in before.styles]


def test_contracts_publish_with_that_template_all_the_way(db, template):
    (template / "templates" / "Master Services Agreement.docx").write_bytes(_no_styles(_template()))
    opp_id = _generated_contracts(db)
    with session_scope(db) as s:
        assert {v.publish_status for v in s.scalars(select(models.ArtifactVersion).where(models.ArtifactVersion.artifact_type.in_(("SOW", "MSA"))))} == {publishing.PUBLISHED}


def test_a_cached_sample_run_records_no_spend(db):
    opp_id = _approved_brd(db)
    with session_scope(db) as s:
        run = ops.latest_run(s, opp_id)
        assert (run.status, run.cost_usd) == ("Succeeded", 0.0)


# ---------- the Drive adapter, on a fake service ----------

class _Request:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class _FakeFiles:
    def __init__(self, drive):
        self.d = drive

    def list(self, q, **k):
        return _Request(lambda: self.d.query(q))

    def create(self, body, media_body=None, **k):
        return _Request(lambda: self.d.create(body, media_body))

    def delete(self, fileId):
        return _Request(lambda: self.d.items.pop(fileId) and {})

    def get_media(self, fileId):
        return _Request(lambda: self.d.items[fileId]["data"])


class _FakeDrive:
    def __init__(self):
        self.items = {"ROOT": {"name": "dealadvisor", "parents": [], "mimeType": storage.FOLDER_MIME}}
        self.deletes_outside = 0
        self.fail = []

    def files(self):
        return _FakeFiles(self)

    def query(self, q):
        if self.fail:
            raise self.fail.pop(0)
        parent = q.split("'")[1]
        out = [{"id": i, "name": v["name"], "mimeType": v["mimeType"], "modifiedTime": ""} for i, v in self.items.items() if parent in v["parents"]]
        if "name='" in q:
            wanted = q.split("name='")[1].split("' and mimeType")[0].replace("\\'", "'")
            out = [x for x in out if x["name"] == wanted]
        if "mimeType='" in q:
            out = [x for x in out if x["mimeType"] == storage.FOLDER_MIME]
        return {"files": out}

    def create(self, body, media):
        new = f"id{len(self.items)}"
        self.items[new] = {"name": body["name"], "parents": body["parents"], "mimeType": body.get("mimeType", storage.DOCX_MIME), "data": media.getbytes(0, media.size()) if media else None}
        return {"id": new, "webViewLink": f"https://drive.example/{new}"}

    def inside_root(self, item_id) -> bool:
        while item_id != "ROOT":
            parents = self.items[item_id]["parents"]
            if not parents:
                return False
            item_id = parents[0]
        return True


def test_everything_the_drive_adapter_creates_is_inside_the_root():
    fake = _FakeDrive()
    fake.items["ELSEWHERE"] = {"name": "my other file", "parents": ["OTHER"], "mimeType": "text/plain"}
    store_ = storage.DriveStore("ROOT", service=fake, sleep=lambda s: None)
    placed = store_.put(["HRIS - Client/Name", "BRD", "versions"], "a.docx", b"x")
    store_.put(["HRIS - Client/Name", "BRD", "versions"], "b.docx", b"y")
    assert placed["url"].startswith("https://drive.example/")
    created = [i for i in fake.items if i not in ("ROOT", "ELSEWHERE")]
    assert created and all(fake.inside_root(i) for i in created)
    assert len([i for i, v in fake.items.items() if v["mimeType"] == storage.FOLDER_MIME]) == 4     # root + 3, reused
    assert {f["name"] for f in store_.list_files(["HRIS - Client/Name", "BRD", "versions"])} == {"a.docx", "b.docx"}
    store_.delete(["HRIS - Client/Name", "BRD", "versions"], "ELSEWHERE")      # not in that folder: refused
    assert "ELSEWHERE" in fake.items


def test_the_template_comes_from_the_templates_folder_only():
    fake = _FakeDrive()
    store_ = storage.DriveStore("ROOT", service=fake, sleep=lambda s: None)
    assert store_.read_template() is None
    store_.put(["templates"], "notes.docx", b"n")
    store_.put(["templates"], "Master Services Agreement.docx", b"msa")
    assert store_.read_template() == (b"msa", "Master Services Agreement.docx")


def _http_error(status: int):
    from googleapiclient.errors import HttpError
    return HttpError(SimpleNamespace(status=status, reason="x"), b"{}")


def test_drive_retries_transient_errors_and_tells_expired_sign_in_apart():
    from google.auth.exceptions import RefreshError
    fake = _FakeDrive()
    naps = []
    store_ = storage.DriveStore("ROOT", service=fake, sleep=naps.append)
    fake.fail = [_http_error(503), TimeoutError()]
    assert store_.list_files([]) == []
    assert naps == [1.5, 3.0]
    fake.fail = [_http_error(503)] * 3
    with pytest.raises(storage.Unavailable):
        store_.list_files([])
    fake.fail = [RefreshError("invalid_grant")]
    with pytest.raises(storage.AuthExpired, match="re-run sign-in"):
        store_.list_files([])
    fake.fail = [_http_error(401)]
    with pytest.raises(storage.AuthExpired):
        store_.list_files([])


def test_folder_names_are_made_safe():
    assert storage.safe_name('HRIS - A/B: "C"') == "HRIS - A-B- -C"


def test_ai_tooling_is_in_the_estimate_summary_and_internal_copy_and_never_in_the_client_copy(db):
    opp_id = T._approved_estimate(db)
    _proposal(db, opp_id)
    with session_scope(db) as s:
        summary = _text_of(est.summary_docx(s, opp_id))
        client, internal = _text_of(prop.client_copy(s, opp_id)), _text_of(prop.internal_copy(s, opp_id))
        costs = ops.current_version(s, opp_id, "ESTIMATE").snapshot["results"]["costing"]["scenarios"]
    for row in ("AI coding assistant seats", "Model usage", "AI test generation and automation runs", "Contingency on usage", "Net savings vs. baseline"):
        assert row in summary and row in internal, row
        assert row not in client, row
    assert prop.TOOLING_SECTION in internal and prop.TOOLING_SECTION not in client and "illustrative" in internal
    assert "tooling" not in client.replace("AI tooling and data-processing location", "").lower()
    for s_ in ("B", "C"):
        assert costs[s_]["net_savings"] == pytest.approx(costs["baseline"]["labor"] - costs[s_]["labor"] - costs[s_]["ai_tooling"])
        for key, value in costs[s_]["ai_tooling_lines"].items():
            if value >= 1000 and key != "million_tokens":
                assert f"{value:,.0f}" in internal and f"{value:,.0f}" not in client, key
    B = costs["B"]
    assert f"{B['ai_tooling']:,.0f}" not in client and f"{B['net_savings']:,.0f}" not in client


def test_the_leak_check_blocks_a_tooling_figure_in_a_client_file(db):
    from app import leak
    opp_id = T._approved_estimate(db)
    _proposal(db, opp_id)
    with session_scope(db) as s:
        rec = prop.record_for(s, opp_id, ops.current_version(s, opp_id, "BRD"), ops.current_version(s, opp_id, "ESTIMATE"))
        seats = rec.costing.scenarios["C"]["ai_tooling_lines"]["seats"]
    assert leak._forms(seats) & leak.blocked_values(rec)["cost"]

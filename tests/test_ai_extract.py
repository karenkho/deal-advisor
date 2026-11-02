"""AI-first extraction, with the LLM replaced: tables arrive whole, quotes are checked, items are routed by type, an unchanged document is never read twice, and the rules check the result."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from docx import Document
from pydantic import BaseModel
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ai_extract as ax, graph, llm, models, opportunities as ops, statements as st  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.kvstore import KV, _memory  # noqa: E402
from app.parsing import parse_bytes  # noqa: E402
from app.pipeline import DocInput  # noqa: E402
from tests.test_estimate_proposal import BDM, SS, db  # noqa: E402,F401

REAL_PIPELINE = ops._pipeline     # the db fixture swaps it for the cached sample
ROOT = Path(__file__).resolve().parent.parent
RFP = ROOT / "eval" / "dev_set" / "rfp_liwanag_mobile_app.pdf"
TEXT = """REQUEST FOR PROPOSAL (RFP)
Submission Deadline: December 15, 2026
1. Project Background & Vision
Liwanag Power Corporation is restructuring its retail energy user experience for all its customers across the region.
2. Core Functional Requirements Matrix
Vendors must propose solutions matching the following groups:
| Module Identifier | Core Feature Setup | Integrations |
| MOD-01: Billing | • In-app partial payments • Downloadable e-receipts | • Payment gateways |
3. Non-Functional Requirements
• Data Privacy: all personal data is encrypted at rest.
4. Bidding Procurement Logistics
All digital proposals must be under 15 MB per transmission.
5. Project Implementation Timeline
The schedule spans 2 calendar weeks from contract signing.
"""
ANSWERS = {"Project Background": [("background", "Liwanag Power Corporation is restructuring its retail energy user experience", "", "")],
           "Core Functional": [("functional", "In-app partial payments", "Partial payments", "Billing"), ("functional", "Downloadable e-receipts", "E-receipts", "Billing"),
                               ("integration", "Payment gateways", "Payment gateways", "Billing"), ("functional", "A drone delivery service", "Drones", "Billing")],
           "Non-Functional": [("nfr", "Data Privacy: all personal data is encrypted at rest.", "", "")],
           "Bidding": [("bid_procedure", "All digital proposals must be under 15 MB per transmission.", "", "")],
           "Project Implementation": [("timeline_commercial", "The schedule spans 2 calendar weeks from contract signing.", "", "")]}


ROLES = {"Project Background": "background", "Non-Functional": "nfr", "Bidding": "bid_procedure", "Project Implementation": "timeline"}


@pytest.fixture
def llm_stub(monkeypatch):
    """Answers by section; every other LLM step fails, so its node falls back."""
    calls = []

    def structured(messages, schema, **kwargs):
        if schema is ax._Items:
            body = messages[1]["content"]
            calls.append(body)
            for marker, rows in ANSWERS.items():
                if marker in body.splitlines()[1]:
                    number = body.splitlines()[1].split(" ")[0].rstrip(".")
                    return schema(sections=[ax.SectionTag(number=number, role=ROLES.get(marker, "requirements_scope"), reason="As the stub says.")], items=[ax.Item(type=t, quote=q, title=ti, module=m) for t, q, ti, m in rows])
            return schema(sections=[ax.SectionTag(number=body.splitlines()[1].split(" ")[0].rstrip("."), role="other", reason="As the stub says.")], items=[])
        raise RuntimeError("unavailable")
    monkeypatch.setenv("EXTRACTION_MODE", ax.AI_CHECKED)
    monkeypatch.setattr(llm, "structured", structured)
    monkeypatch.setattr(llm, "embed", lambda texts, **k: [[1.0, 0.0] for _ in texts])
    monkeypatch.setattr(llm, "chat", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("unavailable")))
    monkeypatch.setattr(graph.spend, "preflight", lambda pages: (True, "", 1.0))
    monkeypatch.setattr(graph.spend, "record", lambda usd: None)
    monkeypatch.setattr(graph.tools, "index", lambda: SimpleNamespace(chunks=[], search=lambda *a, **k: []))
    monkeypatch.setattr(graph.determinism, "canonicalize", lambda features, usage, taken=None: features)
    _memory.clear()
    yield calls
    _memory.clear()


def _run(text: str = TEXT):
    docs = [{"key": "tor", "kind": "tor", "number": "", "date": "", "name": "t", "first_page": 1, "last_page": 1}]
    return graph.run([{"page": 1, "text": text}], "t", docs=docs, kv=KV(in_memory=True))[0]


# ---------- parsing ----------

def test_a_pdf_table_arrives_row_by_row_with_its_bullets_in_the_right_cell():
    text = " ".join(p["text"] for p in parse_bytes(RFP.read_bytes(), RFP.name))
    row = next(line for line in text.split("\n") if line.startswith("| MOD-02"))
    module, features, integrations = [c.strip() for c in row.strip("| ").split(" | ")]
    assert module == "MOD-02: Billing & Payments"
    assert "• In-app partial & advance payments" in features and "Payment Gateways" not in features
    assert "Payment Gateways" in integrations and "partial" not in integrations


def test_without_the_table_extractor_the_columns_interleave(monkeypatch):
    monkeypatch.setenv("PDF_TABLES", "off")
    assert not any(line.startswith("| MOD-02") for p in parse_bytes(RFP.read_bytes(), RFP.name) for line in p["text"].split("\n"))


def test_a_pdf_without_tables_keeps_its_text_exactly():
    path = ROOT / "samples" / "tor_A_clean.pdf"
    with pytest.MonkeyPatch.context() as m:
        m.setenv("PDF_TABLES", "off")
        before = parse_bytes(path.read_bytes(), path.name)
    assert parse_bytes(path.read_bytes(), path.name) == before


def test_bullets_inside_a_docx_cell_stay_separate():
    doc = Document()
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "MOD-01: Billing"
    table.rows[0].cells[1].text = "Partial payments"
    table.rows[0].cells[1].add_paragraph("E-receipts")
    buf = io.BytesIO()
    doc.save(buf)
    assert "| MOD-01: Billing | • Partial payments • E-receipts |" in parse_bytes(buf.getvalue(), "x.docx")[0]["text"]


def test_the_liwanag_copy_names_no_real_issuer():
    text = " ".join(p["text"] for p in parse_bytes(RFP.read_bytes(), RFP.name))
    assert "Liwanag Power Corporation" in text and "energy corporation" not in text.lower().replace("liwanag power corporation", "")


# ---------- extraction ----------

def test_module_rows_become_epics_and_bullets_become_brs_and_integrations_their_own(llm_stub):
    state = _run()
    stated = {f.name: f for f in state.features if f.origin != "critic"}
    assert {"In-app partial payments", "Downloadable e-receipts", "Payment gateways"} <= set(stated)     # a short bullet is named by its own words
    assert stated["In-app partial payments"].epic == "Billing" and stated["Payment gateways"].type == "integration"


def test_what_is_not_in_the_document_is_rejected_and_logged(llm_stub):
    state = _run()
    assert "Drones" not in [f.name for f in state.features]
    assert [r["quote"] for r in state.routed["rejected"]] == ["A drone delivery service"]
    assert any("rejected" in w for w in state.warnings)


def test_items_are_routed_by_type_and_never_become_brs(llm_stub):
    state = _run()
    names = " ".join(f.name for f in state.features)
    for not_a_br in ("Background", "Matrix", "Bidding", "15 MB", "Timeline"):
        assert not_a_br not in names
    assert state.routed["background"] and state.routed["timeline"] and state.routed["bid_procedure"] == ["All digital proposals must be under 15 MB per transmission."]
    assert next(r for r in state.requirements if "encrypted" in r.text).category == "nfr"


def test_a_section_title_never_names_an_epic_or_a_br_when_checked(llm_stub, monkeypatch):
    bad = {"Core Functional": [("functional", "In-app partial payments", "Core Functional Requirements Matrix", "Requirements Matrix")]}
    monkeypatch.setattr(sys.modules[__name__], "ANSWERS", bad)
    state = _run()
    feature = next(f for f in state.features if "partial" in f.description.lower())
    assert feature.epic == "Functional scope" and "Matrix" not in feature.name


def test_without_the_checker_the_llm_is_taken_as_it_is(llm_stub, monkeypatch):
    monkeypatch.setenv("EXTRACTION_MODE", ax.AI)
    monkeypatch.setattr(sys.modules[__name__], "ANSWERS", {"Core Functional": [("functional", "In-app partial payments", "Core Functional Requirements Matrix", "Requirements Matrix")]})
    feature = next(f for f in _run().features if "partial" in f.description.lower())
    assert feature.epic == "Requirements Matrix" and "unmapped" not in _run().routed.get("checker", {})


def test_a_bullet_no_item_accounts_for_is_reported(llm_stub, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "ANSWERS", {"Core Functional": [("functional", "In-app partial payments", "Partial payments", "Billing")]})
    state = _run()
    assert sorted(state.routed["checker"]["unmapped"]) == ["Downloadable e-receipts", "Payment gateways"]
    assert any("map to no BR, NFR or decision" in w for w in state.warnings)
    assert any(q.source == "guard" and "Downloadable e-receipts" in q.text for q in state.questions)


def test_a_requirements_section_with_no_real_br_blocks_when_the_llm_cannot_read_it(llm_stub, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "ANSWERS", {"Core Functional": []})     # the section is tagged as requirements, and nothing came out of it
    state = _run("1. Background\nFictional context.\n2. Core Functional Requirements Matrix\nVendors must propose solutions in the following groups across all branches and offices.\n")
    assert any(q.source == "coverage" and "Core Functional Requirements Matrix" in q.text for q in state.questions)


def test_a_failed_read_says_the_ai_service_failed(llm_stub, monkeypatch):
    monkeypatch.setattr(llm, "structured", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    state = _run()
    assert not state.features and any("AI service unavailable" in e for e in state.errors)


# ---------- checks by rule ----------

def test_body_units_are_the_bullets_in_cells_and_lists():
    units = [u["text"] for u in ax.body_units([{"page": 1, "text": TEXT}], {"2": ax.REQUIREMENTS})]
    assert units == ["In-app partial payments", "Downloadable e-receipts", "Payment gateways"]


def test_the_rfp_copy_has_every_key_item_as_a_body_unit():
    pages = parse_bytes(RFP.read_bytes(), RFP.name)
    key = json.loads((ROOT / "eval" / "dev_set" / "rfp_liwanag_answer_key.json").read_text())
    roles = {number: ax.REQUIREMENTS for number, title in st.headings(pages).items() if number == "2" or title.startswith("MOD")}
    units = {ax._matchable(u["text"]) for u in ax.body_units(pages, roles)}
    assert all(ax._matchable(f["name"]) in units for f in key["features"]) and len(key["features"]) == 24


def test_a_short_timeline_is_told_from_a_normal_one():
    assert ax.requested_weeks(["The milestone schedule spans 12 calendar weeks from contract signing."]) == 12
    assert ax.requested_weeks(["The project runs for 8 months."]) == 35
    assert ax.timeline_is_short(12, 5) and not ax.timeline_is_short(24, 4)


# ---------- persistence and the record ----------

@pytest.fixture
def real_pipeline(monkeypatch):
    monkeypatch.setattr(ops, "_pipeline", REAL_PIPELINE)


def _opportunity(db, text=TEXT) -> str:
    with session_scope(db) as s:
        opp = ops.create(s, BDM, "Liwanag Power Corporation", "Mobile app", "APP")
        ops.add_document(s, BDM, opp.id, DocInput(name="rfp.pdf", kind="tor", pages=[{"page": 1, "text": text}]), "RFP")
        return opp.id


def _process(db, opp_id) -> None:
    with session_scope(db) as s:
        run_id = ops.start_processing(s, BDM, opp_id)
    ops.run_processing(run_id, opp_id, db)


def test_the_brd_is_routed_and_the_opportunity_record_holds_the_bid_and_the_timeline(db, llm_stub, real_pipeline):
    opp_id = _opportunity(db)
    _process(db, opp_id)
    with session_scope(db) as s:
        run = ops.latest_run(s, opp_id)
        assert run.status == "Succeeded", run.detail
        opp = s.get(models.Opportunity, opp_id)
        assert opp.commercial["bid_procedure"] == ["All digital proposals must be under 15 MB per transmission."]
        assert opp.commercial["requested_weeks"] == 2 and str(opp.submission_deadline) == "2026-12-15"
        brs = [b.title for b in s.scalars(select(models.Br).where(models.Br.opportunity_id == opp_id))]
        assert "15 MB" not in " ".join(brs) and "In-app partial payments" in brs
        epics = [e.name for e in s.scalars(select(models.Epic).where(models.Epic.opportunity_id == opp_id))]
        assert "Billing" in epics and not any(ax.is_bad_title(e) for e in epics)
        items = {(i.section, (i.data.get("section") or "")): i.data for i in s.scalars(select(models.BrdSectionItem).where(models.BrdSectionItem.opportunity_id == opp_id))}
        assert "restructuring" in items[("executive_summary", "Business Background")]["details"]
        assert any("unusually short" in i.data["description"] for i in s.scalars(select(models.BrdSectionItem).where(models.BrdSectionItem.opportunity_id == opp_id, models.BrdSectionItem.section == "risk")))
        assert not any("Matrix" in i.data.get("capability", "") for i in s.scalars(select(models.BrdSectionItem).where(models.BrdSectionItem.opportunity_id == opp_id, models.BrdSectionItem.section == "in_scope")))


def test_a_section_already_read_is_never_read_again_even_after_a_failure_and_a_restart(db, llm_stub, real_pipeline, monkeypatch):
    import app.legacy_import as legacy
    opp_id = _opportunity(db)
    real = legacy.populate_brd
    monkeypatch.setattr(legacy, "populate_brd", lambda *a, **k: (_ for _ in ()).throw(ValueError("save failed")))
    _process(db, opp_id)
    with session_scope(db) as s:
        assert ops.latest_run(s, opp_id).status == "Failed"
        assert len(s.get(models.Opportunity, opp_id).pipeline["extraction"]) == len(llm_stub) > 0
    read_once = len(llm_stub)
    _memory.clear()     # the container restarts: only the database remembers
    monkeypatch.setattr(legacy, "populate_brd", real)
    _process(db, opp_id)
    with session_scope(db) as s:
        assert ops.latest_run(s, opp_id).status == "Succeeded"
        assert len(s.get(models.Opportunity, opp_id).pipeline["extraction"]) == read_once     # saving the BRD kept them
    assert len(llm_stub) == read_once


def test_a_bulletin_works_on_a_brd_built_this_way(db, llm_stub, real_pipeline):
    from app import brd_service as svc, supplement
    opp_id = _opportunity(db)
    _process(db, opp_id)
    with session_scope(db) as s:
        svc.start_validation(s, SS, opp_id)
        svc.accept_all_remaining(s, SS, opp_id)
        for q in s.scalars(select(models.OpenQuestion).where(models.OpenQuestion.opportunity_id == opp_id, models.OpenQuestion.blocking)).all():
            svc.acknowledge_question(s, SS, opp_id, q.item_id)
        svc.approve(s, SS, opp_id)
    with session_scope(db) as s:
        row = ops.add_document(s, BDM, opp_id, DocInput(name="b.pdf", kind="bid_bulletin", number="1", date="2027-01-01", pages=[{"page": 1, "text": "1. Purpose\nThis bulletin adds to the RFP.\n2. Addition\nThe system shall also export the e-receipts to a spreadsheet file."}]), "Bid bulletin")
        request = supplement.propose(s, BDM, opp_id, row.id)
        assert request is not None


def test_prose_above_a_list_is_a_summary_and_each_unit_is_one_br(llm_stub, monkeypatch):
    text = "1. Scope of Work\nThe vendor will handle API synthesis and cloud configuration for all of the modules.\n1.1 Module 1: Ordering\n- Cart: customers add items. They change quantities too.\n"
    monkeypatch.setattr(sys.modules[__name__], "ANSWERS", {"Scope of Work": [("functional", "API synthesis", "API synthesis", "Ordering"), ("functional", "customers add items.", "Add items", "Ordering"), ("functional", "They change quantities too.", "Change quantities", "Ordering")]})
    stated = [f for f in _run(text).features if f.origin != "critic"]
    assert [(f.epic, f.name) for f in stated] == [("Ordering", "Cart")]


def test_a_titled_subsection_is_one_br_however_many_sentences_the_llm_named(llm_stub, monkeypatch):
    text = "1. Leave Management\n1.1 Leave approval\nLeave applications shall be approved by the supervisor.\nApprovers shall return applications with remarks.\n"
    monkeypatch.setattr(sys.modules[__name__], "ANSWERS", {"Leave Management": [("functional", "Leave applications shall be approved by the supervisor.", "Approval", "HR"), ("functional", "Approvers shall return applications with remarks.", "Return with remarks", "HR")]})
    stated = [f for f in _run(text).features if f.origin != "critic"]
    assert [(f.epic, f.name) for f in stated] == [("Leave Management", "Leave approval")]


# ---------- section roles ----------

def test_the_role_decides_coverage_not_the_headings_words(llm_stub, monkeypatch):
    """A heading with no scope word, tagged as requirements, is checked. A heading full of them, tagged as background, is not."""
    text = "1. What We Need\n- Online booking for customers across every branch\n- Waiting list for each session at each branch\n2. Functional Requirements Background\n- Our history of functional scope and features\n"
    monkeypatch.setattr(sys.modules[__name__], "ANSWERS", {"What We Need": [("functional", "Online booking for customers across every branch", "Booking", "Sessions")], "Functional Requirements Background": []})
    monkeypatch.setattr(sys.modules[__name__], "ROLES", {"What We Need": "requirements_scope", "Functional Requirements Background": "background"})
    state = _run(text)
    assert state.routed["checker"]["unmapped"] == ["Waiting list for each session at each branch"]
    by_title = {x["title"]: x for x in state.routed["sections"]}
    assert by_title["What We Need"]["role"] == "requirements_scope" and (by_title["What We Need"]["br"], by_title["What We Need"]["unmapped"]) == (1, 1)
    assert by_title["Functional Requirements Background"]["units"] == 0


def test_a_functional_item_in_a_bid_section_is_an_explicit_decision_not_a_br(llm_stub, monkeypatch):
    text = "1. Bidding\nAll digital proposals must be under 15 MB per transmission to the committee.\n2. Booking\n- Online booking for customers across every branch\n"
    monkeypatch.setattr(sys.modules[__name__], "ANSWERS", {"Bidding": [("functional", "All digital proposals must be under 15 MB per transmission to the committee.", "File size limit", "Portal")], "Booking": [("functional", "Online booking for customers across every branch", "Booking", "Sessions")]})
    monkeypatch.setattr(sys.modules[__name__], "ROLES", {"Bidding": "bid_procedure", "Booking": "requirements_scope"})
    state = _run(text)
    assert [f.name for f in state.features if f.origin != "critic"] == ["Booking"]     # the section's title: it is a titled section, not a generic list
    assert next(r for r in state.requirements if "15 MB" in r.text).category == "project_governance"


def test_the_llms_type_alone_cannot_switch_off_the_migration_check(llm_stub, monkeypatch):
    text = "1. Records\n1.1 Employee files\nStaff shall keep every employee file in one place.\n"
    monkeypatch.setattr(sys.modules[__name__], "ANSWERS", {"Records": [("functional", "Staff shall keep every employee file in one place.", "Files", "HR")]})
    monkeypatch.setattr(ax.Item, "model_dump", lambda self, **k: {**BaseModel.model_dump(self, **k), "feature_type": "data_migration"})
    assert next(f for f in _run(text).features if f.name == "Employee files").type == "workflow"


def test_section_tags_are_stored_with_the_statements_and_for_the_reviewer(db, llm_stub, real_pipeline):
    opp_id = _opportunity(db)
    _process(db, opp_id)
    with session_scope(db) as s:
        assert ops.latest_run(s, opp_id).status == "Succeeded"
        stored = s.scalars(select(models.Statement).where(models.Statement.opportunity_id == opp_id)).all()
        assert next(x for x in stored if "15 MB" in x.text).section_role == "bid_procedure"
        assert next(x for x in stored if "partial" in x.text).section_role == "requirements_scope"
        sections = s.get(models.Opportunity, opp_id).pipeline["sections"]
        assert {x["role"] for x in sections} >= {"background", "requirements_scope", "bid_procedure", "timeline"} and all("reason" in x for x in sections)


def test_a_wrapped_bullet_is_whole_and_a_quote_without_its_label_still_finds_it(llm_stub, monkeypatch):
    text = "1. Scope of Work\n1.1 Module 2: Rider App\n\x7f Intelligent Dispatch: Interactive push notifications detailing mileage and strict\ntimeout countdowns for the job flow.\n\x7f Proof of Delivery: Signature capture pad and photo upload for each drop.\n"
    monkeypatch.setattr(sys.modules[__name__], "ANSWERS", {"Scope of Work": [("functional", "Interactive push notifications detailing mileage and strict timeout countdowns for the job flow.", "Notifications", "Rider App"), ("functional", "Signature capture pad and photo upload for each drop.", "Signature", "Rider App")]})
    assert sorted(f.name for f in _run(text).features if f.origin != "critic") == ["Intelligent Dispatch", "Proof of Delivery"]


def test_the_guard_stays_quiet_when_the_llm_accounted_for_a_section_as_rules(llm_stub, monkeypatch):
    text = "1. Booking\n- Online booking for customers across every branch\n2. Business Rules\n2.1 Filing\nBookings shall be filed at least three working days before the session date.\n"
    monkeypatch.setattr(sys.modules[__name__], "ANSWERS", {"Booking": [("functional", "Online booking for customers across every branch", "Booking", "Sessions")], "Business Rules": [("business_rule", "Bookings shall be filed at least three working days before the session date.", "Filing", "Sessions")]})
    monkeypatch.setattr(sys.modules[__name__], "ROLES", {"Booking": "requirements_scope", "Business Rules": "requirements_scope"})
    state = _run(text)
    assert not any("Coverage guard" in w for w in state.warnings) and not any(q.source == "coverage" for q in state.questions)


def _feature(name, epic, section, text, module="", kind="tor"):
    req = SimpleNamespace(section_no=section, module=module, doc_kind=kind)
    feature = SimpleNamespace(name=name, epic=epic, type="workflow", description=text, requirement_ids=[name], pages=[1])
    return feature, req


def _fold(*rows):
    features, reqs = [], {}
    for row in rows:
        f, r = _feature(*row)
        features.append(f)
        reqs[f.name] = r
    return features, ax.fold_restatements(features, reqs)


def test_a_later_section_restating_an_earlier_br_folds_into_it_and_keeps_its_statement():
    earlier = "Employees create travel requests with destination, dates, purpose and estimated cost, and save drafts."
    later = "Employees shall be able to create travel requests specifying destination, dates, purpose and estimated costs, save drafts."
    features, log = _fold(("Travel request", "Functional", "4.1", earlier), ("Travel experience", "User experience", "7.2", later))
    assert [f.name for f in features] == ["Travel request"] and later in features[0].description and features[0].requirement_ids == ["Travel request", "Travel experience"]
    assert log[0]["into"] == "Travel request" and log[0]["folded"] == "Travel experience"


def test_a_statement_naming_the_section_folds_even_in_other_words():
    features, log = _fold(("Cash advance", "Functional", "4.3", "Advances up to the limit are released by Treasury."), ("Advances", "User experience", "7.4", "Staff can ask for money ahead of a trip, as stated in Section 4.3."))
    assert len(features) == 1 and log[0]["score"] == 1.0


@pytest.mark.parametrize("second", [("Reports", "Reports", "5.1", "Management dashboard shows spend by department and drill-down."),     # different words
                                    ("Row", "Billing", "7.1", "Employees create travel requests with destination, dates, purpose and estimated cost.", "Billing")])     # a row of a module is its own capability
def test_different_capabilities_and_module_rows_never_fold(second):
    features, log = _fold(("Travel request", "Functional", "4.1", "Employees create travel requests with destination, dates, purpose and estimated cost."), second)
    assert len(features) == 2 and not log


def test_subsections_of_one_section_never_fold_into_each_other():
    text = "Employees create travel requests with destination, dates, purpose and estimated cost."
    features, log = _fold(("A", "Functional", "4.1", text), ("B", "Functional", "4.2", text))
    assert len(features) == 2 and not log


def test_vendor_supply_and_supplements_never_fold():
    text = "Employees create travel requests with destination, dates, purpose and estimated cost."
    features, log = _fold(("A", "Functional", "4.1", text), ("Laptops", "Vendor supply", "11.4", text))
    assert len(features) == 2 and not log
    features, log = _fold(("A", "Functional", "4.1", text), ("B", "Supplementary", "7.1", text, "", "bid_bulletin"))
    assert len(features) == 2 and not log


def test_a_vendor_line_in_a_capability_section_stays_with_that_capability_and_equipment_stays_apart():
    pages = [{"page": 1, "text": "4. Functional requirements\n4.5 Reading receipts\nThe Solution shall read receipts.\nThe vendor shall propose the technology.\n11. Other\n11.4 Equipment\nThe vendor shall supply forty laptops."}]
    base = {"doc_key": "t", "module": ""}
    items = [{**base, "section_no": "4.5", "category": "functional", "label": "Receipts", "text": "The Solution shall read receipts."},
             {**base, "section_no": "4.5", "category": "vendor_supply", "label": "Technology", "text": "The vendor shall propose the technology."},
             {**base, "section_no": "11.4", "category": "vendor_supply", "label": "Laptops", "text": "The vendor shall supply forty laptops."}]
    ax.apply_structure(items, pages)
    assert items[0]["label"] == items[1]["label"] == "Reading receipts" and items[2]["label"] == "Laptops"


def test_an_upload_never_reads_a_shipped_result_even_for_the_same_file(tmp_path, monkeypatch):
    from app import result_cache
    from app.kvstore import KV
    from app.schemas import TorState
    monkeypatch.setattr(result_cache, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(result_cache, "result_key", lambda files: "same-file")
    (tmp_path / "same-file.json").write_text(json.dumps({"state": TorState().model_dump(), "usage": {}, "runtime_s": 1, "tor_name": ""}))
    assert result_cache.get([b"x"]) is not None     # the bundled sample still is served
    assert result_cache.get([b"x"], KV(in_memory=True)) is None


def _refusal():
    response = SimpleNamespace(request=None, status_code=400, headers={})
    return ax.BadRequestError("filtered", response=response, body={"code": "content_filter", "innererror": {"code": "ResponsibleAIPolicyViolation"}})


def test_a_refused_passage_is_found_and_left_out_and_the_rest_is_still_read(monkeypatch):
    calls = []

    def chat(messages, **k):
        if "SHIELD" in messages[-1]["content"]:
            raise _refusal()
        return SimpleNamespace()

    def read(text, usage):
        calls.append(text)
        if "SHIELD" in text:
            raise _refusal()
        return ax._Items(items=[ax.Item(type="functional", quote="The system shall file leave.", title="Leave")])

    monkeypatch.setattr(llm, "chat", chat)
    monkeypatch.setattr(ax, "_read", read)
    chunk = {"number": "3", "text": "3 Requirements\nThe system shall file leave.\nSHIELD line.\nThe system shall show balances."}
    answer = ax._ask(chunk, None, ax.KV(in_memory=True, enabled=False))
    assert answer["refused_lines"] == 1 and len(answer["items"]) == 1
    assert "SHIELD" not in calls[-1] and "show balances" in calls[-1]


def test_a_passage_refused_only_as_a_whole_goes_as_a_block(monkeypatch):
    def chat(messages, **k):
        text = messages[-1]["content"]
        if "PART ONE" in text and "PART TWO" in text:
            raise _refusal()
        return SimpleNamespace()

    monkeypatch.setattr(llm, "chat", chat)
    assert sorted(ax._refused_blocks(["PART ONE", "PART TWO"], None)) == [0, 1]


def test_another_bad_request_still_stops_the_run_with_its_reason(monkeypatch):
    def read(text, usage):
        raise ax.BadRequestError("bad", response=SimpleNamespace(request=None, status_code=400, headers={}), body={"code": "invalid_request"})

    monkeypatch.setattr(ax, "_read", read)
    with pytest.raises(ax.Unavailable):
        ax._ask({"number": "1", "text": "1 Scope\nText."}, None, ax.KV(in_memory=True, enabled=False))


def test_a_clause_headed_by_its_own_title_is_named_by_it():
    assert ax._unit_name("Renewal window. A permit holder shall be able to renew during the thirty days before expiry.") == "Renewal window"


def test_a_step_that_runs_twice_counts_both_passes():
    first = graph._step_usage({}, {"prompt_tokens": 0, "completion_tokens": 0, "est_usd": 0.0}, {"prompt_tokens": 100, "completion_tokens": 10, "est_usd": 0.05})
    second = graph._step_usage(first, {"prompt_tokens": 500, "completion_tokens": 50, "est_usd": 0.2}, {"prompt_tokens": 520, "completion_tokens": 55, "est_usd": 0.21})
    assert second == {"prompt_tokens": 120, "completion_tokens": 15, "est_usd": 0.06}

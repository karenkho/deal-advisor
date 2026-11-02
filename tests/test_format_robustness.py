"""Requirements are read whatever their format: bullets and "Label: description" without modal verbs, module and capital headings, numbered paragraphs, tables.
A heading never becomes a BR or an epic, and a section that promises scope never passes silently with no BR."""
from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from docx import Document

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import brd_sections, graph, llm, statements as st  # noqa: E402
from app.parsing import _cell, _spaced_tables, parse_bytes, rejoin_wrapped  # noqa: E402

MATRIX_STATEMENTS = 20
ROOT = Path(__file__).resolve().parent.parent
DEV = ROOT / "eval" / "dev_set"


def _items(text: str) -> list[dict]:
    return st.parse_statements([{"key": "tor", "kind": "tor", "number": "", "date": "", "name": "x", "pages": [{"page": 1, "text": text}]}])


def _file_items(path: Path) -> list[dict]:
    pages = parse_bytes(path.read_bytes(), path.name)
    return st.parse_statements([{"key": "tor", "kind": "tor", "number": "", "date": "", "name": "x", "pages": pages}])


@pytest.mark.parametrize("bullet", ["-", "•", "●", "▪", "\x7f", ""])
def test_any_bullet_glyph_is_an_item_and_a_label_names_the_feature(bullet):
    found = _items(f"2. Scope of Work\n2.1 Module 1: Customer App (iOS)\n{bullet} Identity & Profile: OAuth2 sign-ins and phone validation with one-time codes.")
    assert [i["label"] for i in found] == ["Identity & Profile"]
    assert found[0]["section_no"] == "2.1" and found[0]["text"].startswith("Identity & Profile: OAuth2")


def test_the_delivery_tor_gives_every_labelled_feature_under_its_module():
    found = [i for i in _file_items(DEV / "Delivery_App_Project_TOR.pdf") if i["label"]]
    assert len(found) == 14
    assert {i["section_no"] for i in found} == {"2.1", "2.2", "2.3", "2.4"}
    assert not any("CONFIDENTIAL" in i["text"] or "Page " in i["text"] for i in found)     # page furniture is gone


def test_a_description_after_a_colon_is_not_a_heading_but_a_module_title_is():
    assert st._numbered_heading("2.1 Module 1: Customer Application (iOS & Android)") == ("2.1", "Module 1: Customer Application (iOS & Android)")
    assert st._numbered_heading("3.1 Registration: members create accounts by email") is None


def test_capital_and_module_headings_without_numbers_set_the_section():
    found = _items("FUNCTIONAL REQUIREMENTS\nModule 1: Ordering\n- Cart: customers add items and change quantities before paying.")
    assert found[0]["section"] == "Module 1: Ordering" and found[0]["label"] == "Cart"
    assert st.headings([{"page": 1, "text": "FUNCTIONAL REQUIREMENTS\nnothing"}]) == {"U1": "FUNCTIONAL REQUIREMENTS"}


def test_a_mixed_case_numbered_line_among_capital_headings_is_not_a_section():
    text = "1. BACKGROUND\n2. DELIVERABLES\n3. Launch Phase\n3. EVALUATION CRITERIA"
    assert st.headings([{"page": 1, "text": text}]) == {"1": "BACKGROUND", "2": "DELIVERABLES", "3": "EVALUATION CRITERIA"}


def test_a_numbered_line_that_breaks_the_sequence_is_not_a_section():
    assert [n for n, _, _ in st.sections([{"page": 1, "text": "1. Background\n2. Delivery Milestones\n1. Inception Phase\n3. Terms"}])] == ["1", "2", "3"]


def test_running_headers_and_footers_are_not_text():
    pages = [{"page": i, "text": f"ACME TOR Draft\n{i}. Heading Number\nBody of page {i} that runs on.\nFooter line of the document\nPage {i} of 4"} for i in range(1, 5)]
    joined = " ".join(e[1] for e in st.walk(pages) if e[0] == "line")
    assert "Footer line" not in joined and "ACME" not in joined and "Page 2" not in joined


def test_numbered_paragraphs_without_shall_are_statements_and_name_features_in_scope():
    found = _items("3. Scope of Services\n3.1 Members register online using an email address and a card number.\n5. Exclusions\n5.1 Barcode scanners are not part of this procurement.")
    assert [i["section_no"] for i in found] == ["3", "5"]
    assert found[0]["label"].startswith("Members register") and found[1]["label"] == ""


def test_table_rows_name_features_in_a_requirements_section_and_a_header_does_not():
    found = _items("3. Requirements Matrix\n| ID | Function | Description | Priority |\n|---|---|---|---|\n| FR-01 | Booking | Patients pick an open slot online | Must |\n| FR-02 | Queue | A number on a screen | Must |")
    assert [i["label"] for i in found] == ["Booking", "Queue"]


def test_a_table_outside_a_requirements_section_stays_as_it_was():
    found = _items("7. Users\n| User group | Approximate number |\n|---|---|\n| Regular staff members | 120 |")
    assert [i["label"] for i in found] == [""]


def test_docx_tables_are_read_in_order():
    doc = Document()
    doc.add_paragraph("3. Requirements Matrix")
    table = doc.add_table(rows=2, cols=3)
    for cell, text in zip(table.rows[0].cells, ("ID", "Function", "Description")):
        cell.text = text
    for cell, text in zip(table.rows[1].cells, ("FR-01", "Booking", "Patients pick an open slot online")):
        cell.text = text
    doc.add_paragraph("4. Terms")
    buf = io.BytesIO()
    doc.save(buf)
    pages = parse_bytes(buf.getvalue(), "x.docx")
    assert pages[0]["text"].index("FR-01") < pages[0]["text"].index("4. Terms")
    assert [i["label"] for i in st.parse_statements([{"key": "t", "kind": "tor", "number": "", "date": "", "name": "x", "pages": pages}])] == ["Booking"]


def test_objectives_as_bullets_numbered_paragraphs_or_prose_are_read():
    for body, expected in (("\x7f Seamless Automation: Fully automate orders.\n\x7f Market UX: Fast screens.", 2), ("2.1 Let members reserve books from home.\n2.2 Give staff one system.", 2)):
        section = brd_sections._find(st.sections([{"page": 1, "text": f"1. Background\n2. Objectives\n{body}"}]), "objective")
        assert len(brd_sections._list_items(section)) == expected


def test_generic_headings_are_told_from_real_ones():
    for heading in ("TECHNICAL REQUIREMENTS", "Scope of Work", "Functional Requirements", "Requirements Matrix", "Core Functional Requirements Matrix", "Non-Functional & Security Requirements"):
        assert st.GENERIC_HEADING.match(heading), heading
    for heading in ("Leave Management", "Employee Self-Service", "Reports and dashboards"):
        assert not st.GENERIC_HEADING.match(heading), heading
    assert st.is_module_title("Module 2: Driver App") and st.module_name("Module 2: Driver App") == "Driver App"


# ---------- the graph, with the LLM replaced ----------

TOR = """1. Background
A fictional agency handles enquiries by telephone, and it wants them in one place across its branches.
2. Scope of Work
The portal brings enquiry intake and case tracking together for every branch, and supervisors follow open cases from one screen every day.
3. Technical Requirements
The vendor must host the portal in the country.
4. Evaluation
Proposals will be scored by a committee.
"""


@pytest.fixture
def stubbed(monkeypatch):
    """Every LLM step fails, so nodes fall back; the classifier and the guard answer as each test says."""
    answers = {"guard": None, "classes": {}}

    def structured(messages, schema, **kwargs):
        if schema is graph._GuardBatch:
            if answers["guard"] is None:
                raise RuntimeError("unavailable")
            return answers["guard"]
        if schema is graph._SectionBatch:
            lines = [x.split(" ", 1)[0] for x in messages[1]["content"].split("<statements>")[1].split("</statements>")[0].strip().splitlines()]
            rows = [{"id": i, "category": answers["classes"].get("all", "other"), "area": "none"} for i in lines]
            return schema(statements=rows, section_types=[], merges=[])
        raise RuntimeError("unavailable")
    monkeypatch.setattr(llm, "structured", structured)
    monkeypatch.setattr(llm, "embed", lambda texts, **k: [[1.0, 0.0] for _ in texts])
    monkeypatch.setattr(llm, "chat", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("unavailable")))
    monkeypatch.setattr(graph.spend, "preflight", lambda pages: (True, "", 1.0))
    monkeypatch.setattr(graph.spend, "record", lambda usd: None)
    monkeypatch.setattr(graph.tools, "index", lambda: SimpleNamespace(chunks=[], search=lambda *a, **k: []))
    monkeypatch.setattr(graph.determinism, "canonicalize", lambda features, usage, taken=None: features)
    return answers


def _run(text: str = TOR):
    from app.kvstore import KV
    pages = [{"page": 1, "text": text}]
    docs = [{"key": "tor", "kind": "tor", "number": "", "date": "", "name": "t", "first_page": 1, "last_page": 1}]
    return graph.run(pages, "t", docs=docs, kv=KV(enabled=False))[0]


def test_the_guard_raises_a_blocking_question_when_the_llm_cannot_read_the_section(stubbed):
    state = _run()
    asked = [q for q in state.questions if q.source == "coverage"]
    assert len(asked) == 1 and "Scope of Work" in asked[0].text
    assert any("Coverage guard" in w and "blocking" in w for w in state.warnings)


def test_the_guard_keeps_only_what_is_really_in_the_section_and_states_the_cost(stubbed):
    quote = "enquiry intake and case tracking"
    stubbed["guard"] = graph._GuardBatch(items=[graph._GuardItem(name="Case tracking", quote=quote), graph._GuardItem(name="Invented", quote="a drone delivery service")])
    state = _run()
    names = [f.name for f in state.features]
    assert "Case tracking" in names and "Invented" not in names
    assert not [q for q in state.questions if q.source == "coverage"]
    assert any("Coverage guard" in w and "1 found" in w for w in state.warnings)
    assert next(f for f in state.features if f.name == "Case tracking").epic == "Functional scope"


def test_a_coverage_question_blocks_the_brd_approval(stubbed):
    from app import legacy_import
    source = Path(legacy_import.__file__).read_text()
    assert 'blocking=x.kind in ("conflict", "coverage")' in source


def test_a_paragraph_about_the_vendors_work_is_never_an_exclusion_but_a_real_one_is(stubbed):
    stubbed["classes"]["all"] = "scope_exclusion"
    stubbed["guard"] = graph._GuardBatch(items=[])
    state = _run("1. Background\nSomething fictional happens here on most days.\n2. Delivery\nThe selected vendor will execute end-to-end product realization across all phases.\nHardware is not part of this procurement and must be bought by the client.\n")
    texts = [x["text"] for x in state.exclusions]
    assert any("Hardware" in t for t in texts) and not any("selected vendor" in t for t in texts)


def test_headings_never_become_brs_or_epics(stubbed):
    stubbed["classes"]["all"] = "functional"
    stubbed["guard"] = graph._GuardBatch(items=[])
    state = _run("1. Background\nFictional context for this agency and its branches.\n2. TECHNICAL REQUIREMENTS\nThe system must keep an audit log of every change to a case.\n3. Delivery\n3.1 Module 1: Customer App\n- Signup: customers register with an email address and a password.\n")
    assert not any("technical requirements" in (f.name + f.epic).lower() for f in state.features)
    assert any(f.epic == "Customer App" and f.name == "Signup" for f in state.features)
    assert next(f for f in state.features if "audit log" in f.description).epic == "Non-functional requirements"


def test_what_a_tor_says_about_the_procurement_or_the_project_is_never_a_br(stubbed):
    stubbed["classes"]["all"] = "functional"
    stubbed["guard"] = graph._GuardBatch(items=[])
    state = _run("1. PROJECT OVERVIEW & OBJECTIVES\n1.1 Goals\n- Fully automate order placement across every branch.\n2. ORDERING\n2.1 Cart\nThe system must keep a cart for each customer.\n3. EVALUATION CRITERIA\nBidding firms will be appraised on merit and on cost.\n4. DELIVERABLES, MILESTONES\nSystem architecture documents will be delivered in the first phase.\n")
    assert [f.name for f in state.features if f.origin != "critic"] == ["Cart"]


def test_a_payment_gateway_section_is_still_software():
    assert not graph.ADMIN_SECTION.match("Payment gateway and SMS") and graph.ADMIN_SECTION.match("Payment terms") and graph.ADMIN_SECTION.match("DELIVERABLES, MILESTONES & MILESTONE")


def test_a_list_of_users_under_the_scope_is_not_a_list_of_features():
    found = _items("3. Scope of Work\n3.1 Functional modules\n- Leave: employees file leave online and managers approve it.\n3.2 Users\n- Employees (self-service): about 950 people in all branches.\n3.3 Not in scope\n- Payroll: computation stays with the current provider.")
    assert [i["label"] for i in found] == ["Leave", "", ""]
    assert not st.is_scope_title("Not in scope") and not st.is_scope_title("Exclusions from scope") and st.is_scope_title("User Requirements")


def test_moving_the_clients_data_is_build_work_even_if_classified_as_a_supplied_item(stubbed):
    stubbed["classes"]["all"] = "vendor_supply"
    stubbed["guard"] = graph._GuardBatch(items=[])
    state = _run("1. Background\nThe records are kept in spreadsheets by the registrar.\n2. Registration\n2.1 Existing records\nThe vendor shall migrate active students into the new system before go-live.\n2.2 Equipment\nThe vendor shall supply two barcode scanners for the counter.\n")
    moved = next(f for f in state.features if f.name == "Existing records")
    assert moved.type == "data_migration" and moved.epic != "Vendor supply"
    assert next(f for f in state.features if f.name == "Equipment").epic == "Vendor supply"


@pytest.mark.parametrize("path,statements_count,labelled", [("samples/hris/tor.pdf", 101, 0), ("samples/tor_A_clean.pdf", 54, 0), ("samples/tor_B_messy.pdf", 172, 0),
                                                            ("eval/dev_set/Delivery_App_Project_TOR.pdf", 41, 14), ("eval/dev_set/tor_matrix_clinic.pdf", 16, 12), ("eval/dev_set/tor_numbered_library.docx", 20, 12), ("eval/dev_set/tor_module_matrix_utility.pdf", MATRIX_STATEMENTS, 15)])
def test_the_dev_set_keeps_its_statements(path, statements_count, labelled, monkeypatch):
    monkeypatch.setenv("PDF_TABLES", "off")     # what the rules-only path reads
    found = _file_items(ROOT / path)
    assert (len(found), sum(bool(i["label"]) for i in found)) == (statements_count, labelled)


def test_a_matrix_module_row_is_an_epic_and_its_bullets_are_brs_and_column_titles_are_not_sections():
    text = "2. Core Functional Requirements Matrix\nVendors must propose solutions as follows:\nModule Identifier\nCore Feature Setup\nMOD-01: Identity &\nServices\n\x7f Biometric login\n\x7f Paperless billing enrolment\nworkflows\nMOD-02: Billing\n\x7f Partial payments in the app\n3. Security Requirements\nThe system must encrypt data."
    pages = [{"page": 1, "text": text}]
    assert list(st.headings(pages).values()) == ["Core Functional Requirements Matrix", "MOD-01: Identity & Services", "MOD-02: Billing", "Security Requirements"]
    assert st.module_name("MOD-01: Identity & Services") == "Identity & Services"
    found = [i for i in st.parse_statements([{"key": "t", "kind": "tor", "number": "", "date": "", "name": "x", "pages": pages}]) if i["label"]]
    assert [i["label"] for i in found] == ["Biometric login", "Paperless billing enrolment workflows", "Partial payments in the app"]


def test_two_epics_that_abbreviate_alike_get_different_codes():
    from app.brd import epic_codes
    epics = ["Core Feature Setup", "Core Functional Requirements Matrix", "Core Function", "Customer Files"]
    codes = epic_codes(epics)
    assert len(set(codes.values())) == len(epics)
    assert epic_codes(["Leave", "Employee Self-Service", "Employee Information"])["Leave"] == "LV"     # existing codes stay as they were


def test_an_instruction_to_bidders_above_a_matrix_is_not_a_br_and_a_heading_is_not_an_epic(stubbed):
    stubbed["classes"]["all"] = "functional"
    stubbed["guard"] = graph._GuardBatch(items=[])
    state = _run("1. Background\nFictional context for this agency and its branches.\n2. Core Functional Requirements Matrix\nVendors must propose solutions matching these groups.\nMOD-01: Billing\n\x7f Partial payments in the app\n3. Non-Functional & Security Requirements\nThe application must encrypt personal data at rest.\n")
    stated = [f for f in state.features if f.origin != "critic"]
    assert sorted(f.epic for f in stated) == ["Billing", "Non-functional requirements"]
    assert not any("Requirements" in f.name or "Vendors must" in f.name for f in stated)


def test_a_word_broken_at_its_hyphen_rejoins_and_a_set_off_dash_does_not():
    assert rejoin_wrapped("Downloadable e-\nreceipts") == "Downloadable e-receipts"
    assert _cell("Downloadable e-\nreceipts") == "Downloadable e-receipts"
    assert rejoin_wrapped("Billing -\nPayments") == "Billing -\nPayments" and rejoin_wrapped("self-\nService") == "self-\nService"


def _spaced_pdf(columns=True) -> bytes:
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "", 10)
    prose = "Riverbend keeps its catalogue in spreadsheets and sells tickets at one desk, so this paragraph runs across the full width of the page."
    pdf.multi_cell(0, 5, prose, new_x="LMARGIN", new_y="NEXT")
    rows = [("Module", "Features", "Connections"), ("Collection", "- Object catalogue", "- Barcode input"), ("", "- Loan history", ""), ("Visitors", "- Ticket sales", "- Payment gateway"), ("Operations", "- Attendance report", "")]
    y = pdf.get_y() + 5
    for row in rows:
        if columns:
            for x, text in zip((10, 60, 130), row):
                pdf.text(x, y, text)
        else:
            pdf.text(10, y, " ".join(c for c in row if c))
        y += 5
    pdf.set_y(y)
    pdf.multi_cell(0, 5, prose, new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def test_a_table_laid_out_with_spacing_reads_as_rows_with_its_bullets_in_their_cells():
    text = parse_bytes(_spaced_pdf(), "tor.pdf")[0]["text"]
    assert "| Collection | • Object catalogue • Loan history | • Barcode input |" in text
    assert "| Visitors | • Ticket sales | • Payment gateway |" in text and "| Operations | • Attendance report |  |" in text
    assert text.count("Riverbend") == 2 and "Module Features" not in text


def test_prose_and_a_plain_list_never_read_as_a_spaced_table():
    assert "|" not in parse_bytes(_spaced_pdf(columns=False), "tor.pdf")[0]["text"]


def test_no_spaced_table_is_seen_in_the_dev_pdfs_that_have_none():
    import pdfplumber
    for name in ("samples/hris/tor.pdf", "samples/tor_A_clean.pdf", "samples/tor_B_messy.pdf", "eval/dev_set/Delivery_App_Project_TOR.pdf"):
        with pdfplumber.open(ROOT / name) as pdf:
            assert not [t for p in pdf.pages for t in _spaced_tables(p, [x for x in p.find_tables() if x.rows and len(x.rows[0].cells) >= 2])], name

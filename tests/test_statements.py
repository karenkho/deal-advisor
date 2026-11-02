"""Deterministic statements, supplementary documents, decision memory, incremental re-processing, locked BRD (no Azure calls)."""
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace as N

import pytest

sys.path[:0] = [str(Path(__file__).resolve().parent.parent), str(Path(__file__).resolve().parent)]

from app import decisions, graph, kvstore, llm, pipeline, record, statements, tools  # noqa: E402
from app.kvstore import KV  # noqa: E402
from app.pipeline import DocInput  # noqa: E402
from app.record import Question, RecordFeature  # noqa: E402

TOR = [{"page": 1, "text": "1. Background\nThe company is a distributor.\n5. Leave\n5.3 Leave approval\nLeave applications shall be approved "
                           "in two levels by the supervisor.\nApprovers shall return applications with remarks.\n5.4 Leave balances\n"
                           "The System shall prevent filing beyond the balance.\n14. Commercial\n14.2 Delivery period\nThe System shall be "
                           "delivered within five (5) months from the Notice to Proceed.\n"}]
BULLETIN = [{"page": 1, "text": "1. Purpose\nThis bulletin amends the TOR.\n2.1 Levels\nUnder Section 5.3, leave applications shall be approved "
                                "in three levels by the supervisor and manager.\n3.1 Download\nUnder Section 5.4, employees shall download their "
                                "leave ledger as a PDF.\n3.2 Delivery\nUnder Section 14.2, the System shall be delivered within six (6) months."}]


def _docs(extra=()):
    return [DocInput(name="tor.md", kind="tor", pages=TOR, bundled=True), *extra]


def _bulletin(date="2027-02-01", number="01", pages=BULLETIN):
    return DocInput(name="bb.md", kind="bid_bulletin", pages=pages, date=date, number=number, bundled=True)


# ---------- statements ----------

def test_statements_come_from_rules_with_stable_ids():
    items = statements.parse_statements([{"key": "tor", "kind": "tor", "pages": TOR}])
    texts = [i["text"] for i in items]
    assert "The company is a distributor." not in texts                  # no modal, not a clause
    assert any("two levels" in t for t in texts) and len(items) == 4
    again = statements.parse_statements([{"key": "tor", "kind": "tor", "pages": TOR}])
    assert [i["id"] for i in items] == [i["id"] for i in again]
    assert all(i["id"].startswith("ST-") and i["top_section"] for i in items)


def test_a_statement_id_ignores_whitespace_and_case_but_not_document_or_section():
    a = statements.statement_id("The System shall  do it.", "tor", "5.1 X")
    assert a == statements.statement_id("the system shall do it.", "tor", "5.1 X")
    assert a != statements.statement_id("The System shall do it.", "bb-01", "5.1 X")
    assert a != statements.statement_id("The System shall do it.", "tor", "5.2 Y")


def test_numbered_clauses_and_table_rows_are_statements():
    pages = [{"page": 1, "text": "3.4 Users\n- Employee self-service access\n| User group | Number |\n|---|---|\n| Employees (self-service) | 500 |"}]
    texts = [i["text"] for i in statements.parse_statements([{"key": "tor", "kind": "tor", "pages": pages}])]
    assert "Employee self-service access" in texts and "Employees (self-service) | 500" in texts
    assert "User group | Number" not in texts


def test_markdown_headings_set_the_section():
    pages = [{"page": 1, "text": "## 5. Leave\n\n### 5.1 Filing\n\nEmployees shall file leave online."}]
    item = statements.parse_statements([{"key": "tor", "kind": "tor", "pages": pages}])[0]
    assert item["section"] == "5.1 Filing" and item["top_section"] == "5"


@pytest.mark.parametrize("text,expected", [
    ("Overtime of more than four hours requires prior approval.", "business_rule"),
    ("Vacation leave shall be filed at least three working days before.", "business_rule"),
    ("Filing is blocked except for leave without pay.", "business_rule"),
    ("Only approved overtime shall be counted.", "business_rule"),
    ("Sick leave is paid only if a certificate is attached.", "business_rule"),
    ("The System shall provide a dashboard.", "functional"),
    ("The system shall allow employees to file leave.", "functional"),
    ("the System shall enable HR to lock records.", "functional"),
    ("Pages shall load within 3 seconds for 95% of requests.", None),                    # quality, not a business rule
    ("The System shall have at least 99.5% monthly availability.", None),
    ("The System shall be delivered within six (6) months.", None),                      # the contract
    ("Ten percent (10%) of the contract price upon signing.", None),
    ("Employees shall file sick leave of more than two consecutive days.", None),       # a capability with a condition
    ("HR staff shall receive reminders for documents expiring within 30 days.", None),
    ("Logs shall be brought into the System at least once a day.", None),
    ("HR staff shall review the records.", None)])                                       # not clear: the LLM's
def test_rule_pre_classification(text, expected):
    assert statements.rule_category(text) == expected


@pytest.mark.parametrize("typed,clean", [("02", "02"), ("Bid Bulletin No. 02", "02"), ("bid bulletin no 2", "2"), ("  No. 10 ", "10"), ("BB-3", "BB-3")])
def test_reference_numbers_read_the_same_however_typed(typed, clean):
    assert statements.clean_reference(typed) == clean
    assert statements.doc_label({"kind": "bid_bulletin", "number": typed, "date": "2027-02-01"}) == f"Bid Bulletin No. {clean}, 2027-02-01"


def test_reference_is_optional_for_other_documents():
    assert statements.doc_label({"kind": "minutes", "number": "", "date": "2027-02-01"}) == "Minutes of meeting, 2027-02-01"


def test_same_date_bulletins_break_the_tie_on_reference_naturally(stub):
    second = _bulletin(date="2027-02-01", number="10", pages=[{"page": 1, "text": "2.1 Levels\nUnder Section 5.3, leave applications shall be approved in four levels by the board."}])
    first = _bulletin(date="2027-02-01", number="2")
    state = _run(_docs([second, first]))
    three = next(r for r in state.requirements if "three levels" in r.text)
    assert three.superseded_by == "Bid Bulletin No. 10, 2027-02-01"     # No. 10 follows No. 2, so it wins
    assert not next(r for r in state.requirements if "four levels" in r.text).superseded_by


def test_refs_and_labels():
    item = statements.parse_statements([{"key": "bb", "kind": "bid_bulletin", "number": "01", "date": "2027-02-01", "pages": BULLETIN}])[0]
    assert item["refs"] == ["5.3"] and item["doc_label"] == "Bid Bulletin No. 01, 2027-02-01"


# ---------- graph with a stubbed LLM ----------

@pytest.fixture
def stub(monkeypatch, tmp_path):
    """A stubbed LLM that counts what it is asked. KV writes go to memory."""
    calls = {"section": 0, "link": 0, "size": 0, "route": 0}
    monkeypatch.setattr(kvstore, "SHIPPED", tmp_path / "kv")
    monkeypatch.setattr(kvstore, "_memory", type(kvstore._memory)())
    monkeypatch.setattr(llm, "model_tag", lambda r: "test")
    monkeypatch.setattr(graph.determinism, "CANON_CACHE", tmp_path / "canonical.json")     # the stubbed vectors must never meet this machine's cache, or leave anything in it
    monkeypatch.setattr(graph.determinism, "canonicalize", lambda features, usage, taken=None: features)
    monkeypatch.setattr(graph.spend, "preflight", lambda *a, **k: (True, "", 0.25))
    monkeypatch.setattr(graph.spend, "record", lambda *a: None)
    monkeypatch.setattr(llm, "embed", lambda texts, **k: [[1.0, float(i)] for i, _ in enumerate(texts)])
    monkeypatch.setattr(tools, "index", lambda: N(chunks=[], search=lambda *a, **k: []))
    monkeypatch.setattr(llm, "chat", lambda *a, **k: N(choices=[N(message=N(tool_calls=None, content=""))]))

    def structured(msgs, schema, **kw):
        user = msgs[-1]["content"]
        name = schema.__name__
        if name == "IntakeCheck":
            return schema(is_tor=True, reason="ok", injection_quotes=[])
        if name == "_SectionBatch":
            calls["section"] += 1
            ids = [ln.split()[0] for ln in user.splitlines() if ln.startswith("ST-")]
            cat = lambda ln: "constraint" if "Delivery" in ln or "delivered" in ln else "functional"  # noqa: E731
            return schema(statements=[graph._StatementClass(id=ln.split()[0], category=cat(ln), area="schedule" if cat(ln) == "constraint" else "none")
                                      for ln in user.splitlines() if ln.startswith("ST-")], section_types=[], merges=[])
        if name == "_LinkBatch":
            calls["link"] += 1
            earlier = user.split("</earlier_statements>")[0].splitlines()
            later = user.split("<supplementary_statements>")[1].splitlines()
            links = []
            for ln in later:
                if ln.startswith("ST-"):
                    match = [e.split()[0] for e in earlier if e.startswith("ST-") and (("levels" in e and "levels" in ln) or ("delivered" in e and "delivered" in ln))]
                    links.append(graph._Link(id=ln.split()[0], changes=match))
            return schema(links=links)
        if name == "RoutingBatch":
            calls["route"] += 1
            fids = [ln.split(" | ")[0] for ln in user.splitlines() if ln.startswith("F-")]
            return schema(decisions=[graph.RoutingBatch.model_fields["decisions"].annotation.__args__[0](feature_id=f, route="clear", reason="r", client_question="") for f in fids])
        if name == "SizingOutput":
            calls["size"] += 1
            return schema(size="M", justification="j", sources=[], acceptance_criteria=[], confidence="high")
        if name == "ComplexitySuggestion":
            return schema(level="standard", reason="r")
        if name == "CriticOutput":
            return schema(missing_features=[], questions=[], notes=[])
        if name == "_PairVerdicts":
            return schema(verdicts=[])
        raise AssertionError(name)

    monkeypatch.setattr(llm, "structured", structured)
    return calls


def _run(docs, kv=None):
    _, pages, meta = pipeline.assemble(docs)
    state, _ = graph.run(pages, "x", docs=meta, kv=kv or KV(in_memory=True, enabled=True))
    return state


def test_brs_follow_tor_sections_by_rule_and_the_llm_cannot_drop_statements(stub):
    state = _run(_docs())
    assert sorted(f.name for f in state.features if f.origin == "tor") == ["Leave approval", "Leave balances"]
    assert len(state.requirements) == 4
    assert {f.epic for f in state.features if f.origin == "tor"} == {"Leave"}
    f = next(x for x in state.features if x.name == "Leave approval")
    assert len(f.requirement_ids) == 2


def test_unclassified_statements_stay_as_functional(stub, monkeypatch):
    original = llm.structured

    def skip(msgs, schema, **kw):
        res = original(msgs, schema, **kw)
        if schema.__name__ == "_SectionBatch":
            res.statements = []       # the LLM returned nothing
        return res

    monkeypatch.setattr(llm, "structured", skip)
    state = _run(_docs())
    assert len(state.requirements) == 4     # nothing is dropped
    assert all(r.category == ("project_governance" if r.section_no.startswith("14") else "functional") for r in state.requirements)     # a commercial section holds no capability


def test_a_bulletin_supersedes_keeps_the_old_text_and_adds_to_the_section(stub):
    state = _run(_docs([_bulletin()]))
    by = {r.text: r for r in state.requirements}
    old = next(r for t, r in by.items() if "two levels" in t)
    assert old.superseded_by == "Bid Bulletin No. 01, 2027-02-01"
    delivery = next(r for t, r in by.items() if "five (5) months" in t)
    assert delivery.superseded_by
    leave = next(f for f in state.features if f.name == "Leave approval")
    texts = [by_id.text for by_id in state.requirements if by_id.id in leave.requirement_ids]
    assert any("three levels" in t for t in texts) and not any("two levels" in t for t in texts)
    assert old.id in leave.superseded_ids
    balances = next(f for f in state.features if f.name == "Leave balances")
    assert any("ledger" in r.text for r in state.requirements if r.id in balances.requirement_ids)


def test_rules_win_and_statement_classes_are_reused_without_the_llm(stub, monkeypatch):
    pages = [{"page": 1, "text": "5. Leave\n5.1 Filing\nOnly approved leave shall be counted.\nThe System shall provide a form.\nHR staff shall review filings."}]
    seen = []
    original = llm.structured

    def spy(msgs, schema, **kw):
        if schema.__name__ == "_SectionBatch":
            seen.append(msgs[-1]["content"])
        return original(msgs, schema, **kw)

    monkeypatch.setattr(llm, "structured", spy)
    kv = KV(in_memory=True, enabled=True)
    state = _run([DocInput(name="t.md", kind="tor", pages=pages, bundled=True)], kv)
    assert next(r for r in state.requirements if "Only approved" in r.text).category == "business_rule"
    statements_block = seen[0].split("</statements>")[0]
    assert "HR staff shall review" in statements_block and "Only approved" not in statements_block and "provide a form" not in statements_block
    assert "Only approved" in seen[0].split("<already_classified>")[1]
    _run([DocInput(name="t.md", kind="tor", pages=pages, bundled=True)], kv)
    assert len(seen) == 1                                               # nothing left to ask
    more = [{"page": 1, "text": pages[0]["text"] + "\nAuditors shall read filings."}]
    _run([DocInput(name="t.md", kind="tor", pages=more, bundled=True)], kv)
    assert "Auditors" in seen[1].split("</statements>")[0] and "HR staff shall review" not in seen[1].split("</statements>")[0]


def test_a_br_holds_capabilities_and_rules_so_classification_cannot_change_its_contents(stub, monkeypatch):
    pages = [{"page": 1, "text": "6. Time\n6.5 Corrections\nCorrections shall be filed within three working days.\nHR staff shall review corrections.\n"
                                 "12. Business Rules\n12.1 Leave\nOvertime of more than four hours requires approval."}]
    original = llm.structured

    def run_as(category):
        def classify(msgs, schema, **kw):
            res = original(msgs, schema, **kw)
            if schema.__name__ == "_SectionBatch":
                for c in res.statements:
                    c.category = category
            return res
        return classify

    outcome = []
    for category in ("functional", "business_rule"):
        monkeypatch.setattr(llm, "structured", run_as(category))
        state = _run([DocInput(name="t.md", kind="tor", pages=pages, bundled=True)], KV(enabled=False))
        outcome.append({f.name: sorted(f.requirement_ids) for f in state.features if f.origin == "tor"})
    assert outcome[0]["Corrections"] == outcome[1]["Corrections"]        # same BR, same statements
    assert len(outcome[0]["Corrections"]) == 2


def test_the_tors_own_rule_list_stays_outside_brs(stub):
    pages = [{"page": 1, "text": "12. Business Rules\n12.1 Leave\nOvertime of more than four hours requires approval.\n5. Leave\n5.1 Filing\nEmployees shall file leave online."}]
    state = _run([DocInput(name="t.md", kind="tor", pages=pages, bundled=True)])
    assert [f.name for f in state.features if f.origin == "tor"] == ["Filing"]


def test_critic_near_duplicates_merge_by_name():
    fs = [N(name="Notifications"), N(name="Audit trail"), N(name="User administration"), N(name="Leave balances")]
    assert graph.near_duplicate("Notifications Framework", fs).name == "Notifications"
    assert graph.near_duplicate("Audit Trail Viewer Screen", fs).name == "Audit trail"
    assert graph.near_duplicate("User and role administration", fs).name == "User administration"
    assert graph.near_duplicate("Data Migration", fs) is None
    assert graph.near_duplicate("Reference Data Maintenance", fs) is None
    assert graph.near_duplicate("Data Migration of Existing Employee Records and Leave Balances", fs) is None


def test_critic_proposals_merge_or_become_suggested_scope(stub, monkeypatch):
    original = llm.structured

    def critic(msgs, schema, **kw):
        if schema.__name__ == "CriticOutput":
            return schema(missing_features=[graph.CriticFeature(epic="Leave", name="Leave balances framework", type="workflow", description="d", reason="repeat", pages=[]),
                                            graph.CriticFeature(epic="Foundation", name="Reference Data Maintenance", type="admin_config", description="d", reason="new", pages=[])],
                          questions=[], notes=[])
        return original(msgs, schema, **kw)

    monkeypatch.setattr(llm, "structured", critic)
    monkeypatch.setattr(graph.determinism, "canonicalize", lambda fs, usage, taken=None: fs)
    monkeypatch.setattr(llm, "embed", lambda texts, **k: [[float(i == j) for j in range(len(texts))] for i in range(len(texts))])  # nothing similar
    state = _run(_docs())
    names = {f.name: f for f in state.features}
    assert "Leave balances framework" not in names and names["Leave balances"].critic_merged == ["Leave balances framework: repeat"]
    rec = pipeline.build_record(state, _docs(), {"id": "t", "client": "c", "project_name": "p"}, {}, 0.0, False)
    by = {f.name: f for f in rec.features}
    assert by["Reference Data Maintenance"].scope_status == "suggested" and by["Leave balances"].scope_status == "in_scope"
    assert any("Coverage check also suggested" in a for a in by["Leave balances"].assumptions)


def test_accepted_suggested_scope_comes_back_on_re_process():
    f = _feature("F1", "Reference Data", [])
    f.origin, f.scope_status = "critic", "suggested"
    new, _, n = decisions.apply([f], [], [{"type": "scope_status", "statement_ids": [decisions.critic_key("Reference  data")], "value": "in_scope"}])
    assert new[0].scope_status == "in_scope" and n == 1


def test_a_document_that_only_adds_cannot_shrink_a_br(stub, monkeypatch):
    original = llm.structured
    sizes = iter(["L", "L", "M", "M", "M"])

    def sized(msgs, schema, **kw):
        if schema.__name__ == "SizingOutput":
            return schema(size=next(sizes), justification="j", sources=[], acceptance_criteria=[], confidence="high")
        return original(msgs, schema, **kw)

    monkeypatch.setattr(llm, "structured", sized)
    kv = KV(in_memory=True, enabled=True)
    _run(_docs(), kv)
    state = _run(_docs([_bulletin()]), kv)
    balances = next(f for f in state.features if f.name == "Leave balances")
    assert balances.size == "L" and "Kept at the TOR" in balances.justification   # it gained a statement; M would shrink it
    approval = next(f for f in state.features if f.name == "Leave approval")
    assert approval.superseded_ids and approval.size == "M"                       # superseded scope may legitimately change


def test_overview_sections_never_become_brs(stub):
    pages = [{"page": 1, "text": "2. Objectives\nThe System shall automate leave filing.\n5. Leave\n5.1 Filing\nEmployees shall file leave online."}]
    state = _run([DocInput(name="t.md", kind="tor", pages=pages, bundled=True)])
    assert [f.name for f in state.features if f.origin == "tor"] == ["Filing"]
    assert next(r for r in state.requirements if "automate" in r.text).category == "other"     # the statement stays


def test_exchanging_data_with_another_system_is_an_integration(stub):
    pages = [{"page": 1, "text": "8. Payroll\n8.1 Data\nThe System shall provide the payroll system with leave data.\n8.2 Views\nThe System shall show the balance."}]
    state = _run([DocInput(name="t.md", kind="tor", pages=pages, bundled=True)])
    types = {f.name: f.type for f in state.features}
    assert types["Data"] == "integration" and types["Views"] != "integration"       # "the System" itself is no counterpart


def test_a_superseded_rule_shows_on_the_br_of_its_section(stub, monkeypatch):
    original = llm.structured

    def as_rule(msgs, schema, **kw):
        res = original(msgs, schema, **kw)
        if schema.__name__ == "_SectionBatch":
            for c in res.statements:
                if "levels" in msgs[-1]["content"] and c.category == "functional":
                    pass
        return res

    monkeypatch.setattr(llm, "structured", as_rule)
    state = _run(_docs([_bulletin()]))
    new = next(r for r in state.requirements if "three levels" in r.text)
    assert new.home_section == "5.3"
    leave = next(f for f in state.features if f.name == "Leave approval")
    assert any("two levels" in next(r.text for r in state.requirements if r.id == i) for i in leave.superseded_ids)


def test_commercial_terms_use_effective_statements(stub):
    state = _run(_docs([_bulletin()]))
    rec = pipeline.build_record(state, _docs([_bulletin()]), {"id": "t", "client": "c", "project_name": "p"}, {}, 0.0, False)
    assert rec.opportunity.commercial_terms["requested_months"] == 6


def test_minutes_conflicts_become_questions_and_supersede_nothing(stub):
    minutes = DocInput(name="m.md", kind="minutes", pages=BULLETIN, date="2027-02-01", bundled=True)
    state = _run(_docs([minutes]))
    assert not any(r.superseded_by for r in state.requirements)
    assert any("Minutes of meeting" in q.text and q.source == "conflict" for q in state.questions)


def test_latest_bulletin_wins(stub):
    second = _bulletin(date="2027-03-01", number="02", pages=[{"page": 1, "text": "2.1 Levels\nUnder Section 5.3, leave applications shall be approved in four levels by the board."}])
    state = _run(_docs([second, _bulletin()]))
    three = next(r for r in state.requirements if "three levels" in r.text)
    assert three.superseded_by.startswith("Bid Bulletin No. 02")
    assert not next(r for r in state.requirements if "four levels" in r.text).superseded_by


def test_reprocessing_only_touches_new_statements(stub):
    kv = KV(in_memory=True, enabled=True)
    _run(_docs(), kv)
    assert stub["section"] == 2 and stub["size"] == 3                   # the two BRs, and the checklist's user administration
    before = dict(stub)
    first = _run(_docs(), kv)
    assert stub == before                                               # nothing changed: no LLM calls
    second = _run(_docs([_bulletin()]), kv)
    assert stub["section"] == before["section"] + 2                     # only the bulletin's two sections
    assert stub["link"] == 1
    assert stub["size"] == before["size"] + 2                           # both BRs gained a statement
    same = {r.id for r in first.requirements} & {r.id for r in second.requirements}
    assert len(same) == 4                                               # unchanged statements keep their ids


def test_uploads_leave_nothing_on_disk(stub, tmp_path):
    _run(_docs(), KV(in_memory=True, enabled=True))
    assert not list((tmp_path / "kv").glob("*")) if (tmp_path / "kv").exists() else True


# ---------- decision memory ----------

def _feature(fid, name, ids, size="M"):
    return RecordFeature(id=fid, epic="E", name=name, description="d", type="workflow", priority="Must", complexity="medium",
                         size=size, route="clear", statement_ids=ids)


def test_decisions_reapply_by_statement_id_and_are_counted():
    features = [_feature("F1", "A", ["s1", "s2"]), _feature("F2", "B", ["s3"], size="L")]
    items = [{"type": "rename", "statement_ids": ["s1", "s2"], "value": "Renamed"}, {"type": "size", "statement_ids": ["s3"], "value": "XL"},
             {"type": "priority", "statement_ids": ["s1"], "value": "Could"}, {"type": "rename", "statement_ids": ["gone"], "value": "X"},
             {"type": "dismiss_question", "statement_ids": ["s3"], "value": "Q?"}]
    qs = [Question(text="Q?", source="rule", statement_ids=["s3"])]
    new, questions, n = decisions.apply(features, qs, items)
    assert n == 4                                                       # the one for a removed statement is skipped
    assert new[0].name == "Renamed" and new[0].priority == "Could" and new[1].size == "XL"
    assert questions[0].dismissed and features[0].name == "A"           # inputs untouched


def test_a_merge_combines_statements_and_keeps_the_larger_size():
    features = [_feature("F1", "A", ["s1"]), _feature("F2", "B", ["s2"], size="L"), _feature("F3", "C", ["s3"])]
    new, _, n = decisions.apply(features, [], [{"type": "merge", "statement_ids": ["s1", "s2"], "value": [["s1"], ["s2"]]}])
    assert n == 1 and len(new) == 2
    merged = new[0]
    assert merged.name == "A and B" and merged.statement_ids == ["s1", "s2"] and merged.size == "L"


def test_decisions_store_replaces_the_same_kind(tmp_path, monkeypatch):
    monkeypatch.setattr(record, "DATA_DIR", tmp_path)
    decisions.add("x", "size", ["s1"], "L")
    decisions.add("x", "size", ["s1"], "XL")
    decisions.add("x", "priority", ["s1"], "Could")
    assert [(d["type"], d["value"]) for d in decisions.load("x")] == [("size", "XL"), ("priority", "Could")]
    assert (tmp_path / "x" / "decisions.json").exists()
    with pytest.raises(ValueError):
        decisions.add("x", "bogus", [], None)


def test_upload_decisions_and_records_stay_in_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(record, "DATA_DIR", tmp_path)
    decisions.add("upload-abc", "size", ["s1"], "L")
    assert decisions.load("upload-abc") and not list(tmp_path.glob("**/*"))
    rec = record.OpportunityRecord(opportunity=record.Opportunity(id="upload-abc", client="c", project_name="p"))
    saved = record.save(rec, "status: x")
    record.add_run("upload-abc", {"version": 1})
    assert record.load("upload-abc", saved.version).version == saved.version
    assert record.runs("upload-abc") == [{"version": 1}]
    assert not list(tmp_path.glob("**/*"))


# ---------- documents ----------

def test_make_doc_masks_at_once_and_keeps_no_raw_bytes():
    d = pipeline.make_doc("t.md", b"Contact person: Maria Santos, email maria@example.com, mobile 09171234567.")
    text = d.pages[0]["text"]
    assert "maria@example.com" not in text and "09171234567" not in text and d.pii_masked >= 2
    assert not hasattr(d, "data")


def test_upload_id_follows_the_tor_so_a_bulletin_keeps_the_opportunity():
    tor = DocInput(name="t.md", kind="tor", pages=TOR)
    assert pipeline.upload_meta([tor])["id"] == pipeline.upload_meta([tor, _bulletin()])["id"]
    assert pipeline.upload_meta([tor])["id"].startswith("upload-")


def test_documents_order_tor_first_then_by_date():
    docs, pages, meta = pipeline.assemble([_bulletin(date="2027-03-01", number="02"), _bulletin(), DocInput(name="t.md", kind="tor", pages=TOR)])
    assert [d["kind"] for d in meta] == ["tor", "bid_bulletin", "bid_bulletin"]
    assert [d["date"] for d in meta][1:] == ["2027-02-01", "2027-03-01"]
    assert [p["page"] for p in pages] == [1, 2, 3]


def test_change_list_compares_statements_brs_and_questions():
    a = record.OpportunityRecord(opportunity=record.Opportunity(id="t", client="c", project_name="p"),
                                 requirements=[record.RecordRequirement(id="s1", text="old", category="functional")],
                                 features=[_feature("F1", "A", ["s1"])], questions=[Question(text="q1", source="rule")])
    b = a.model_copy(deep=True)
    b.requirements[0].superseded_by = "Bid Bulletin No. 01"
    b.requirements.append(record.RecordRequirement(id="s2", text="new", category="functional"))
    b.features[0].statement_ids = ["s1", "s2"]
    b.features.append(_feature("F2", "B", ["s3"]))
    b.questions = [Question(text="q2", source="rule")]
    c = pipeline.changes_between(a, b)
    assert c["statements_added"] == ["new"] and c["brs_added"] == ["B"] and c["brs_changed"] == ["A"]
    assert c["newly_superseded"] == ["old (by Bid Bulletin No. 01)"]
    assert c["questions_opened"] == ["q2"] and c["questions_closed"] == ["q1"]


def _snapshot():
    root = Path(__file__).resolve().parent.parent
    return {str(p): p.stat().st_mtime for d in ("samples", "data", ".cache") for p in (root / d).rglob("*") if p.is_file()}


def test_a_full_upload_run_writes_nothing_to_disk(stub, monkeypatch):
    from app import brd as brd_module, risks, spend, suitability

    original = llm.structured

    def more(msgs, schema, **kw):
        name = schema.__name__
        if name == "_Batch":
            return schema(items=[])
        if name == "_Entities":
            return schema(entities=[])
        if name == "_Cards":
            return schema(cards=[])
        if name == "_Assessment":
            return schema(risks=[], verdict="go", recommended_scenario="B", reasons=["r"], conditions=[])
        return original(msgs, schema, **kw)

    monkeypatch.setattr(llm, "structured", more)
    monkeypatch.setattr(spend, "record", lambda *a: None)
    monkeypatch.setattr(tools, "retrieve_estimation_guidelines", lambda *a, **k: [])
    assert suitability and brd_module and risks
    before = _snapshot()
    docs = [pipeline.make_doc("private.md", TOR[0]["text"].encode())]
    meta = pipeline.upload_meta(docs)
    rec, _, _ = pipeline.process_upload(docs, meta, confirmation={"confirmed_by": "A. Tester", "confirmed_at": "now", "files": 1})
    assert _snapshot() == before
    loaded = record.load(meta["id"], rec.version)
    assert loaded.upload_confirmation.confirmed_by == "A. Tester" and loaded.upload_confirmation.files == 1
    assert rec.status == "EXTRACTED"
    assert "private.md" not in str(loaded.upload_confirmation.model_dump())   # who and when; no content


# ---------- concurrent writes ----------

def _hammer(work, count=60):
    errors = []

    def run(i):
        try:
            work(i)
        except Exception as e:  # noqa: BLE001
            errors.append(type(e).__name__)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


@pytest.mark.parametrize("opp_id", ["race", "upload-race"])
def test_simultaneous_saves_edits_and_runs_all_survive(tmp_path, monkeypatch, opp_id):
    monkeypatch.setattr(record, "DATA_DIR", tmp_path)
    base = record.OpportunityRecord(opportunity=record.Opportunity(id=opp_id, client="c", project_name="p"))
    saved = []
    assert _hammer(lambda i: saved.append(record.save(base, f"edit {i}").version)) == []
    assert sorted(saved) == list(range(1, 61)) and len(record.versions(opp_id)) == 60
    assert all(record.load(opp_id, v).version == v for v in saved)
    assert _hammer(lambda i: decisions.add(opp_id, "size", [f"s{i}"], "L")) == []
    assert len(decisions.load(opp_id)) == 60
    assert _hammer(lambda i: record.add_run(opp_id, {"version": i})) == []
    assert sorted(r["version"] for r in record.runs(opp_id)) == list(range(60))


def test_a_failed_write_leaves_the_old_file_and_no_temp_file(tmp_path):
    path = tmp_path / "decisions.json"
    record.atomic_write(path, "old")
    with pytest.raises(TypeError):
        record.atomic_write(path, None)
    assert path.read_text() == "old" and [p.name for p in tmp_path.iterdir()] == ["decisions.json"]


def test_a_reader_always_sees_a_whole_file(tmp_path):
    path = tmp_path / "x.json"
    record.atomic_write(path, json.dumps(["a"] * 2000))
    stop, bad = threading.Event(), []

    def read():
        while not stop.is_set():
            try:
                json.loads(path.read_text())
            except ValueError:
                bad.append(1)

    reader = threading.Thread(target=read)
    reader.start()
    for i in range(200):
        record.atomic_write(path, json.dumps([str(i)] * 2000))
    stop.set()
    reader.join()
    assert not bad

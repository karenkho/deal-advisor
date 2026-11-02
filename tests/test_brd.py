"""extract_brd: citations from statements, unsupported flags, links, conflicts, supersede, no internal data (no Azure calls)."""
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import brd, costing, estimation  # noqa: E402
from app.record import (Citation, Opportunity, OpportunityRecord, Question, RecordFeature, RecordRequirement)  # noqa: E402

PAGES = [{"page": 3, "text": "5.4 Leave balances\nThe System shall prevent filing beyond the available\nbalance, except for leave without pay."}]


def _req(i, text, category="functional", section="5.4 Leave balances", no="5.4", **kw):
    return RecordRequirement(id=i, text=text, category=category, section_no=no, file="tor.pdf",
                             citations=[Citation(page=3, section=section)], **kw)


def _rec(questions=(), **fkw):
    reqs = [_req("S1", "The System shall prevent filing beyond the available balance, except for leave without pay."),
            _req("S2", "Leave without pay may exceed the available balance", "business_rule"),
            _req("S3", "Every filing attempt shall be recorded", "business_rule", section="12.1 Audit", no="12.1"),
            _req("S4", "Pages shall load within 3 seconds", "nfr"), _req("S5", "The contract shall be fixed price", "constraint", area="commercial"),
            _req("S6", "The Company shall assign a product owner", "client_responsibility"),
            _req("S7", "Supervisors and department heads | 60", "volume")]
    f = RecordFeature(id="F-1", epic="Leave Management", name="Leave balances", description="d", type="workflow", priority="Must",
                      complexity="medium", size="S", route="clear", statement_ids=["S1"], citations=[Citation(page=3, section="5.4")], **fkw)
    return OpportunityRecord(opportunity=Opportunity(id="t", client="Fictional", project_name="X"), features=[f], requirements=reqs,
                             questions=[Question(text=q, source=s, statement_ids=[]) for q, s in questions])


def _raw(**card):
    base = {"feature_id": "F-1", "statement": "The system shall prevent filing beyond the available balance, except for leave without pay.",
            "rule_ids": ["S3"], "nfr_ids": ["S4"], "assumption_ids": ["S6"], "context_ids": ["S7"], "entities": ["Leave balance"], "question_keys": [], **card}
    return {"entities": [{"name": "Leave balance", "attributes": ["available"], "statement_ids": ["S1"]},
                         {"name": "Ghost", "attributes": [], "statement_ids": ["NOPE"]}], "cards": {"F-1": base}, "patterns": ["Audit trail"]}


def _build(rec=None, **card):
    return brd.build_brd(_raw(**card), rec or _rec())


def test_every_register_comes_straight_from_a_statement_with_its_citation():
    b = _build()
    assert [r.text for r in b.rules] == ["Leave without pay may exceed the available balance", "Every filing attempt shall be recorded"]
    assert b.nfrs[0].citations[0].page == 3 and b.nfrs[0].citations[0].doc_label == "TOR"
    assert b.constraints[0].kind == "commercial" and b.assumptions[0].text.startswith("The Company")


def test_no_actors_or_acceptance_criteria_in_the_model():
    assert not hasattr(brd.Brd(), "actors")
    assert "acceptance_criteria" not in brd.BusinessRequirement.model_fields
    assert "actor_ids" not in brd.BusinessRequirement.model_fields
    assert "acceptance_criteria" not in brd._Card.model_fields
    assert "Never write acceptance criteria" in brd.CARD_PROMPT


def test_quotes_are_exact_and_at_most_25_words():
    long = "Employees shall file leave applications online selecting the leave type dates whole or half day and reason and attaching supporting documents where required for example a medical certificate for sick leave"
    q = brd.quote(long, "medical certificate for sick leave")
    assert len(q.split()) == 25 and q in long and "medical certificate" in q
    loc = _build().requirements[0].citations[0]
    assert brd.is_exact(loc, PAGES)
    assert not brd.is_exact(loc.model_copy(update={"quote": "not in the TOR"}), PAGES)


def test_an_entity_without_a_statement_is_flagged_not_dropped():
    b = _build()
    ghost = next(e for e in b.entities if e.text == "Ghost")
    assert ghost.unsupported and any("Ghost" in u for u in brd.unsupported_items(b))


def test_a_number_or_claim_the_statements_do_not_make_is_unsupported():
    assert _build(statement="The system shall prevent filing beyond 99 days of balance, except for leave without pay.").requirements[0].unsupported
    assert _build(statement="The system shall integrate blockchain payroll analytics dashboards.").requirements[0].unsupported
    assert not _build().requirements[0].unsupported


def test_critic_br_without_statements_is_unsupported():
    rec = _rec()
    rec.features[0].statement_ids = []
    assert _build(rec).requirements[0].unsupported


def test_links_by_statement_id_with_unknown_ids_dropped_and_same_section_rule_added():
    c = _build(rule_ids=["S3", "NOPE"]).requirements[0]
    assert len(c.rule_ids) == 2                                     # S3 linked, S2 by its subsection
    assert c.nfr_ids == ["NFR-01"] and c.assumption_ids == ["ASM-01"]
    assert c.context == ["Supervisors and department heads | 60"]
    assert c.id == "BR-LV-01"


def test_rule_restating_its_br_statement_is_removed_and_rules_renumbered():
    rec = _rec()
    rec.requirements[1].text = "Prevent filing beyond the available balance except for leave without pay"
    b = _build(rec)
    assert [r.text for r in b.rules] == ["Every filing attempt shall be recorded"] and b.rules[0].id == "BRULE-01"
    assert b.requirements[0].rule_ids == ["BRULE-01"]


CONFLICT = ("Leave applications shall be approved in two levels: first by the employee's immediate supervisor, then by the department "
            "head as final approver. vs Leave applications shall follow a three-level approval: the immediate supervisor, the department "
            "head and the HR manager.")


def test_conflict_makes_title_and_statement_neutral_and_marks_rules():
    rec = _rec([(CONFLICT, "conflict")])
    rec.questions[0].statement_ids = ["S1"]
    rec.features[0].name = "Leave approval"
    rec.requirements[1].text = "Leave applications require two levels of approval"
    b = brd.build_brd(_raw(statement="The system shall implement a two-level leave approval workflow where leave applications are approved first by the "
                                     "immediate supervisor and then by the department head as final approver, with approvers able to return applications with remarks.",
                           question_keys=[brd.key_of(CONFLICT)]), rec)
    c = b.requirements[0]
    assert c.title == "Leave approval (levels to be confirmed: Q-01)" and c.conflict_question_id == "Q-01"
    low = c.statement.lower()
    assert "multi-level" in low and "levels to be confirmed: q-01" in low
    for word in ("two", "three", "final approver"):
        assert word not in low
    assert "with approvers able to return applications with remarks" in low
    assert next(r for r in b.rules if "two levels" in r.text).conflict_question_id == "Q-01"


def test_a_linked_br_without_a_disputed_statement_keeps_its_wording():
    rec = _rec([(CONFLICT, "conflict")])
    rec.questions[0].statement_ids = ["SOMEWHERE-ELSE"]
    c = brd.build_brd(_raw(question_keys=[brd.key_of(CONFLICT)]), rec).requirements[0]
    assert c.question_ids == ["Q-01"] and not c.conflict_question_id and c.title == "Leave balances"


def test_superseded_statements_stay_visible_and_are_marked():
    rec = _rec(superseded_ids=["S2"])
    rec.requirements[1].superseded_by = "Bid Bulletin No. 01, 2027-02-01"
    b = _build(rec)
    c = b.requirements[0]
    assert c.superseded == [{"text": "Leave without pay may exceed the available balance", "by": "Bid Bulletin No. 01, 2027-02-01", "page": 3}]
    rec.brd = b
    assert "superseded by Bid Bulletin No. 01" in brd.card_markdown(c, rec)
    assert next(r for r in b.rules if r.statement_id == "S2").superseded_by
    assert brd.metrics(b)["superseded_statements"] == 1


def test_citation_lines_show_document_type_and_date():
    rec = _rec()
    rec.requirements[0].doc_label = "Bid Bulletin No. 01, 2027-02-01"
    assert brd.citation_lines(_build(rec).requirements[0])[0].startswith("Bid Bulletin No. 01, 2027-02-01: tor.pdf, p.3, §5.4")


def test_missing_card_keeps_a_flagged_fallback():
    raw = _raw()
    raw["cards"] = {}
    c = brd.build_brd(raw, _rec()).requirements[0]
    assert c.statement == "d" and c.warnings


def test_out_of_scope_features_get_no_card():
    rec = _rec()
    rec.features[0].route = "out_of_scope"
    assert brd.build_brd(_raw(), rec).requirements == []


@pytest.mark.parametrize("epics,expected", [
    (["Leave Management"], {"Leave Management": "LV"}),
    (["Employee Information Management", "Employee Self-Service"], {"Employee Information Management": "EI", "Employee Self-Service": "ES"})])
def test_epic_codes(epics, expected):
    assert brd.epic_codes(epics) == expected


def test_metrics():
    m = brd.metrics(_build())
    assert m["brs_with_exact_locator_pct"] == 100.0 and m["unsupported_items"] == 1 and m["brs_blocked_by_open_questions"] == 0


def test_nfr_measurability_by_rule():
    rec = _rec()
    rec.requirements[3].text = "The System shall be secure and scalable"
    assert _build(rec).nfrs[0].measurable is False
    assert _build().nfrs[0].measurable is True


def test_card_shows_each_rule_once_and_counts_it_under_capabilities():
    rec = _rec()
    rec.features[0].statement_ids = ["S1", "S2"]
    rec.brd = _build(rec)
    c = rec.brd.requirements[0]
    assert c.capabilities == [rec.requirements[0].text]
    assert set(c.statement_ids) == {"S1", "S2"}
    text = brd.card_markdown(c, rec)
    assert "+2 rules, see below" in text
    assert text.count("Leave without pay may exceed the available balance") == 1
    assert text.count(rec.requirements[0].text) == 1


def test_suggested_scope_card_says_so():
    rec = _rec()
    rec.features[0].scope_status = "suggested"
    rec.features[0].statement_ids = []
    rec.brd = _build(rec)
    c = rec.brd.requirements[0]
    assert c.scope_status == "suggested" and "not priced until a BA accepts it" in brd.card_markdown(c, rec)
    assert brd.metrics(rec.brd)["suggested_scope_brs"] == 1 and brd.unsupported_items(rec.brd) == ["ENT-02 Ghost"]


def test_glossary_holds_tor_terms_entities_and_patterns_and_titles_use_it():
    b = _build()
    assert {"Leave balances", "Leave balance", "Audit trail"} <= set(b.glossary)
    assert b.requirements[0].title in b.glossary


def test_privacy_question_reaches_brs_with_personal_data_entities():
    q = "Section 10.5 Data privacy states: which controls apply?"
    b = _build(_rec([(q, "rule")]))
    assert b.requirements[0].question_ids == ["Q-01"] and brd.metrics(b)["brs_blocked_by_open_questions"] == 1
    raw = _raw(entities=["Leave types and policies"])
    raw["entities"][0]["name"] = "Leave types and policies"
    assert brd.build_brd(raw, _rec([(q, "rule")])).requirements[0].question_ids == []


def test_near_duplicate_entities_merge():
    raw = _raw(entities=["Leave Types"])
    raw["entities"] = [{"name": "Leave Types", "attributes": ["a"], "statement_ids": ["S1"]}, {"name": "leave type", "attributes": ["b"], "statement_ids": ["S1"]}]
    b = brd.build_brd(raw, _rec())
    assert len(b.entities) == 1 and b.entities[0].attributes == ["a", "b"] and b.requirements[0].entity_ids == ["ENT-01"]


def test_a_supplementary_rule_links_to_the_br_of_its_home_section():
    rec = _rec()
    rec.requirements[2].home_section = "5.4"
    c = brd.build_brd(_raw(rule_ids=[]), rec).requirements[0]
    assert len(c.rule_ids) == 2


def test_dismissed_questions_leave_the_register():
    rec = _rec([("Open?", "rule")])
    rec.questions[0].dismissed = True
    assert _build(rec).questions == []


def test_estimation_stores_br_to_activity_links():
    rec = _rec()
    rec.brd = _build()
    new, _ = estimation.run_estimation(rec, costing.illustrative_rates())
    keys = new.brd.requirements[0].activity_keys
    assert "F-1|Backend.coding" in keys and all(k.startswith("F-1|") for k in keys)


def test_card_shows_hours_only_when_internal_and_draft_has_no_internal_data():
    rec = _rec()
    rec.brd = _build()
    c = rec.brd.requirements[0]
    assert "hours" not in brd.card_markdown(c, rec).lower() and "Actors" not in brd.card_markdown(c, rec) and "Context / volumes" in brd.card_markdown(c, rec)
    assert "Backend 12" in brd.card_markdown(c, rec, internal_hours={"Backend": 12.0})
    from docx import Document
    text = " ".join(p.text for p in Document(io.BytesIO(brd.draft_docx(rec))).paragraphs).lower()
    assert "draft" in text and c.statement.lower() in text and "stakeholders" not in text and "acceptance criteria:" not in text
    for word in ("gross margin", "markup", "contingency", "cost rate", "published rate", "php"):
        assert word not in text

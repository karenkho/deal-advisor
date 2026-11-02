"""Phase 3: status model, price floor, cash flow, warranty, risk rules, approval gate, proposal, leak guardrail, consistency
(illustrative and dummy values; no Azure calls)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import brd, costing, deal, estimation, leak, proposal, record, risks  # noqa: E402
from app.record import (Brd, BrdItem, BusinessRequirement, Citation, Locator, Opportunity, OpportunityRecord,  # noqa: E402
                        RecordFeature)

RATES = costing.illustrative_rates()
TERMS = {"requested_months": 5, "upfront_pct": 10, "final_pct": 90, "warranty_months": 6,
         "deliverables": ["Project plan", "Source code and turnover documents"]}
NARRATIVE = {k: [f"{k} narrative."] for k in proposal.NARRATIVE}


def _rec(terms=TERMS, **overrides) -> OpportunityRecord:
    sizes = ["S", "M", "L", "L", "XL", "M", "M", "L", "M", "S", "L", "L"] * 2
    feats = [RecordFeature(id=f"F{i}", epic="Leave", name=f"Screen {i}", description="Standard screen", type="screen_crud",
                           priority="Must", complexity="medium", size=s, route="clear",
                           citations=[Citation(page=1, section="1")]) for i, s in enumerate(sizes)]
    loc = [Locator(file="tor.pdf", page=1, section="1", quote="Quoted passage")]
    cards = [BusinessRequirement(id=f"BR-LV-{i + 1:02d}", feature_id=f.id, title=f.name, epic=f.epic,
                                 statement=f"The system shall provide {f.name}.", citations=loc) for i, f in enumerate(feats)]
    b = Brd(requirements=cards, nfrs=[BrdItem(id="NFR-01", text="Pages load within 3 seconds", citations=loc)],
            assumptions=[BrdItem(id="ASM-01", text="The Company assigns a product owner", citations=loc)],
            questions=[BrdItem(id="Q-01", text="Which levels approve?", kind="conflict")])
    return OpportunityRecord(opportunity=Opportunity(id="phase3-test", client="Fictional Co", project_name="Test HRIS",
                                                     commercial_terms=terms),
                             features=feats, brd=b, assumption_overrides=overrides)


def _run(terms=TERMS, **overrides):
    return estimation.run_estimation(_rec(terms, **overrides), RATES)


def _approved(scenario="B"):
    rec, _ = _run()
    rec = rec.model_copy(update={"status": "RISK_REVIEWED"})
    high = [r["id"] for r in rec.risks if r["severity"] == "high"]
    return proposal.approve(rec, "A. Approver", scenario, high)


def _draft(scenario="B"):
    rec = _approved(scenario)
    sections = proposal.build_sections(rec, NARRATIVE)
    return rec, sections, proposal.render_docx(rec, sections)


# ---------- status model ----------

def test_only_the_next_status_is_allowed():
    rec = _rec()
    assert rec.status == "EXTRACTED"
    rec = record.advance(rec, "ESTIMATED")
    with pytest.raises(ValueError):
        record.advance(rec, "APPROVED")
    with pytest.raises(ValueError):
        record.advance(rec, "EXTRACTED")
    for target in ("RISK_REVIEWED", "APPROVED", "PROPOSAL_DRAFTED", "EXPORTED"):
        rec = record.advance(rec, target)
    assert rec.status == "EXPORTED"


def test_an_edit_after_approval_returns_to_estimated(tmp_path, monkeypatch):
    monkeypatch.setattr(record, "DATA_DIR", tmp_path)
    rec = record.save(_approved(), "approval: A. Approver")
    assert rec.status == "APPROVED" and rec.approval.approved
    edited = record.save(rec, "commercial assumptions edited")
    assert edited.status == "ESTIMATED" and not edited.approval.approved and edited.documents == []
    assert record.save(rec, "status: exported").approval.approved


# ---------- price floor, warranty, cash flow ----------

def test_price_floor_and_discount_room_hand_computed():
    f = deal.price_floor(70.0, 125.0, 0.30)
    assert f["price_floor"] == pytest.approx(100.0)
    assert f["discount_room_pct"] == pytest.approx(20.0)


def test_warranty_reserve_is_its_own_line_and_eligible_for_contingency():
    rec, view = _run()
    w = rec.estimates["warranty"]
    assert w["months"] == 6 and w["hours"] == pytest.approx((0.1 + 0.25 + 0.1 + 0.1 + 0.1) * 6 * 160)
    blended = view["blended_rates"]
    c = view["costs"]["B"]
    assert c["warranty_reserve"] == pytest.approx(sum(h * blended[r]["applied"] for r, h in w["by_role"].items()))
    base = c["labor"] + c["ai_tooling"] + c["infrastructure"] + c["warranty_reserve"]
    assert c["contingency"] == pytest.approx(0.15 * base)
    assert c["delivery_cost"] == pytest.approx(base + c["contingency"])
    assert c["hours"] == pytest.approx(rec.estimates["scenarios"]["B"]["total_hours"])      # effort is unchanged


def test_warranty_defaults_to_three_months_when_the_tor_states_none():
    rec, _ = _run(terms={})
    assert rec.estimates["warranty"]["months"] == 3


def test_monthly_burn_sums_to_the_delivery_cost():
    rec, view = _run()
    for s in ("B", "C"):
        cash = rec.estimates["cash_flow"][s]["default"]
        assert sum(m["burn"] for m in cash["months"]) == pytest.approx(view["costs"][s]["delivery_cost"])
        assert sum(m["receipts"] for m in cash["months"]) == pytest.approx(view["costs"][s]["price_ex_vat"])
        assert cash["months"][-1]["cumulative"] == pytest.approx(view["costs"][s]["price_ex_vat"] - view["costs"][s]["delivery_cost"])


def test_cash_position_hand_computed():
    res = deal.cash_position([40.0, 40.0, 20.0], [(0.1, 0.0), (0.9, 3.0)], 200.0)
    assert [round(m["cumulative"]) for m in res["months"]] == [-20, -60, 100]
    assert res["peak_exposure"] == pytest.approx(60.0) and res["peak_month"] == 2
    assert res["peak_share_of_price"] == pytest.approx(0.30)
    safe = deal.cash_position([10.0], [(1.0, 0.0)], 200.0)
    assert safe["peak_exposure"] == 0 and safe["peak_month"] is None


def test_tor_terms_expose_more_cash_than_the_default_milestones():
    cash = _run()[0].estimates["cash_flow"]["B"]
    assert cash["tor"]["peak_exposure"] > cash["default"]["peak_exposure"]


def test_estimate_range_low_is_optimistic_and_high_is_conservative():
    r = _run()[0].estimates["estimate_range"]["C"]
    assert r["low"]["hours"] < r["expected"]["hours"] < r["high"]["hours"]
    assert r["low"]["price_ex_vat"] < r["expected"]["price_ex_vat"] < r["high"]["price_ex_vat"]


# ---------- risk rules ----------

def _ids(rec):
    return {r["id"]: r for r in rec.risks}


def test_acceptance_risk_is_about_terms_the_tor_does_not_define():
    rec = _rec()
    from app.record import RecordRequirement
    assert "acceptance_terms_undefined" in risks.triggered_rules(_run()[0], "illustrative")
    assert "project acceptance and deliverable acceptance" in _ids(_run()[0])["acceptance_terms_undefined"]["explanation"]
    rec.requirements = [RecordRequirement(id="a", text="Final acceptance is given after one month of live operation.", category="constraint"),
                        RecordRequirement(id="b", text="Each deliverable requires sign-off by the product owner.", category="constraint")]
    assert "acceptance_terms_undefined" not in risks.triggered_rules(estimation.run_estimation(rec, RATES)[0], "illustrative")


def test_commercial_terms_trigger_their_risks():
    risk = _ids(_run()[0])
    assert risk["payment_terms"]["severity"] == "high"
    assert risk["cash_exposure"]["severity"] == "high"
    assert risk["warranty"]["severity"] == "medium"
    assert "unvalidated_productivity" in risk and risk["illustrative_rates"]["severity"] == "info"


def test_good_terms_do_not_trigger():
    risk = _ids(_run(terms={"upfront_pct": 30, "final_pct": 40, "warranty_months": 3})[0])
    assert not {"payment_terms", "warranty", "aggressive_schedule"} & set(risk)


def test_schedule_severity_depends_on_the_shortfall():
    assert _ids(_run(terms={"requested_months": 2})[0])["aggressive_schedule"]["severity"] == "high"
    rec = _run(terms={"requested_months": 50})[0]
    assert "aggressive_schedule" not in _ids(rec)
    months = rec.estimates["scenarios"]["B"]["months"]
    near = max(rec.estimates["scenarios"]["C"]["months"], months) * 0.95
    assert _ids(_run(terms={"requested_months": near})[0])["aggressive_schedule"]["severity"] == "medium"


def test_tooling_profile_and_optimistic_profile_trigger_high_risks():
    assert _ids(_run(tooling_profile="hosted_low_cost_api")[0])["data_residency"]["severity"] == "high"
    assert "data_residency" not in _ids(_run()[0])
    assert _ids(_run(productivity_profile="optimistic")[0])["optimistic_profile"]["severity"] == "high"


def test_scope_and_conflict_trigger_scope_risk():
    risk = _ids(_run()[0])
    assert risk["scope_gaps"]["source_data"]["conflicts"] == 1


def test_price_below_floor_triggers():
    assert _ids(_run(negotiated_price=1.0)[0])["below_floor"]["severity"] == "high"
    assert "below_floor" not in _ids(_run(negotiated_price=1e12)[0])


def test_low_margin_triggers_only_below_target():
    assert "low_margin" not in _ids(_run()[0])


def test_agent_answers_only_for_triggered_risks_and_never_approves(monkeypatch, tmp_path):
    monkeypatch.setattr(risks, "CACHE_DIR", tmp_path)
    from types import SimpleNamespace as N
    from app import llm
    monkeypatch.setattr(llm, "model_tag", lambda role: "test")
    rec, _ = _run()
    monkeypatch.setattr(llm, "chat", lambda *a, **k: N(choices=[N(message=N(tool_calls=None, content="done"))]))

    def fake(msgs, schema, **kw):
        items = [risks._Narrative(id=r["id"], impact="Agent text.", mitigation="M.", action="A.") for r in rec.risks]
        items[0].impact = "It costs 7,000,000."                          # numbers are code's job
        items.append(risks._Narrative(id="invented_risk", impact="X", mitigation="X", action="X"))
        return risks._Assessment(risks=items, reasons=["R."], conditions=["C."])

    monkeypatch.setattr(llm, "structured", fake)
    new = risks.evaluate_deal_risks(rec, "illustrative")
    assert all(r["text_source"] == "agent" for r in new.risks)
    assert "7,000,000" not in " ".join(r["explanation"] for r in new.risks)
    assert any(r["explanation"].endswith("Agent text.") for r in new.risks)
    assert "invented_risk" not in {r["id"] for r in new.risks}
    assert new.recommendation["source"] == "agent" and new.status == rec.status and not new.approval.approved
    # warm: no more calls
    monkeypatch.setattr(llm, "structured", lambda *a, **k: pytest.fail("called the LLM"))
    monkeypatch.setattr(llm, "chat", lambda *a, **k: pytest.fail("called the LLM"))
    risks.evaluate_deal_risks(rec, "illustrative")


def test_while_not_validated_b_is_recommended_and_c_is_the_upside():
    rec, _ = _run()
    d = risks.recommend_by_rule(rec)
    net = {c["scenario"]: c["net_savings"] for c in d["candidates"]}
    assert d["recommended_scenario"] == "B" and d["ai_first_validated"] is False and "not validated" in d["rule"]
    assert net["C"] > net["B"]
    assert d["upside"] == f"Upside option: AI-First, PHP {net['C'] - net['B']:,.0f} more in net savings, not yet validated"


def test_the_recommendation_uses_the_net_figure_not_the_labour_saved_or_the_price():
    rec, _ = _run(ai_first_validated=True)
    costs = rec.costing.scenarios
    assert risks.recommend_by_rule(rec)["recommended_scenario"] == "C"
    costs["C"]["ai_tooling"] = costs["baseline"]["labor"] - costs["C"]["labor"] + 1      # C's tooling now eats all it saves
    d = risks.recommend_by_rule(rec)
    assert d["recommended_scenario"] == "B" and next(c for c in d["candidates"] if c["scenario"] == "C")["net_savings"] == pytest.approx(-1)
    costs["B"]["ai_tooling"] = costs["baseline"]["labor"] - costs["B"]["labor"] + 2
    assert "saves nothing net of AI tooling" in risks.recommend_by_rule(rec)["rule"]


def test_when_validated_the_highest_net_savings_wins_with_ties_on_duration_then_b():
    rec, _ = _run(ai_first_validated=True)
    d = risks.recommend_by_rule(rec)
    assert d["recommended_scenario"] == "C" and d["upside"] is None and "highest net savings" in d["rule"]
    tied, _ = _run(ai_first_validated=True)
    costs = tied.costing.scenarios
    costs["C"]["labor"], costs["C"]["ai_tooling"] = costs["B"]["labor"], costs["B"]["ai_tooling"]     # the same net savings in both
    months = {c["scenario"]: c["months"] for c in risks.recommend_by_rule(tied)["candidates"]}
    assert risks.recommend_by_rule(tied)["recommended_scenario"] == min(months, key=lambda s: (round(months[s], 4), s))
    costs["C"]["months"] = costs["B"]["months"]
    assert risks.recommend_by_rule(tied)["recommended_scenario"] == "B"


def test_below_the_floor_is_a_hard_no_go():
    below = risks.recommend_by_rule(_run(negotiated_price=1.0)[0])
    assert below["verdict"] == "no_go" and not any(c["eligible"] for c in below["candidates"])


def test_data_residency_blocks_approval_until_consent_is_recorded_not_the_recommendation():
    rec, _ = _run(tooling_profile="hosted_low_cost_api")
    d = risks.recommend_by_rule(rec)
    assert d["verdict"] == "go_with_conditions" and any(c["eligible"] for c in d["candidates"])
    assert risks.consent_required(rec) and not risks.consent_required(_run()[0])
    ready = rec.model_copy(update={"status": "RISK_REVIEWED"})
    high = [r["id"] for r in rec.risks if r["severity"] == "high"]
    with pytest.raises(ValueError, match="consent"):
        proposal.approve(ready, "A", "B", high)
    with pytest.raises(ValueError, match="consent"):
        proposal.approve(ready, "A", "B", high, "Client Counsel", "")
    done = proposal.approve(ready, "A", "B", high, "Client Counsel", "2027-01-15")
    assert done.approval.consent_by == "Client Counsel" and done.approval.consent_date == "2027-01-15"


def test_the_agent_explains_but_cannot_change_the_recommendation(monkeypatch, tmp_path):
    from types import SimpleNamespace as N
    from app import llm
    monkeypatch.setattr(risks, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(llm, "model_tag", lambda role: "test")
    monkeypatch.setattr(llm, "chat", lambda *a, **k: N(choices=[N(message=N(tool_calls=None, content=""))]))
    assert "verdict" not in risks._Assessment.model_fields and "recommended_scenario" not in risks._Assessment.model_fields
    rec, _ = _run()
    decided = risks.recommend_by_rule(rec)
    monkeypatch.setattr(llm, "structured", lambda *a, **k: risks._Assessment(risks=[], reasons=["Because."], conditions=["If."]))
    new = risks.evaluate_deal_risks(rec, "illustrative")
    r = new.recommendation
    assert r["recommended_scenario"] == decided["recommended_scenario"] and r["verdict"] == decided["verdict"]
    assert r["upside"] == decided["upside"]
    assert r["reasons"] == ["Because."] and r["source"] == "agent"


def test_ai_engineer_is_folded_in_client_documents_unless_shown():
    rec = _approved()
    team = proposal.client_team(rec)
    t = rec.timeline["B"]["team"]
    assert "AI Engineer" not in team and team["Development team"] == t["Backend"] + t["Frontend"] + t["AI Engineer"]
    assert "Backend Developer" not in team
    _, _, data = _draft()
    text = " ".join(x for _, lines in leak.docx_sections(data) for x in lines)
    assert "AI Engineer" not in text and "Development team" in text
    shown = _approved()
    shown.assumption_overrides = {**shown.assumption_overrides, "show_ai_engineer_in_proposal": True}
    assert "Backend Developer" in proposal.client_team(shown)
    assert rec.estimates["headcount"]["B"]["Backend"] == t["Backend"]    # internal views unchanged
    res = proposal.check_document(data, rec, RATES)
    assert res["leak_hits"] == [] and res["consistency"]["ok"]


def test_agent_failure_keeps_the_register_on_templates(monkeypatch, tmp_path):
    monkeypatch.setattr(risks, "CACHE_DIR", tmp_path)
    from app import llm
    monkeypatch.setattr(llm, "model_tag", lambda role: "test")

    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(llm, "chat", boom)
    new = risks.evaluate_deal_risks(_run()[0], "illustrative")
    assert new.risks and all(r["text_source"] == "template" for r in new.risks)
    assert new.recommendation["source"] == "rules" and new.recommendation["verdict"] == "go_with_conditions"


# ---------- approval gate ----------

def test_approval_needs_status_scenario_name_and_acknowledgements():
    rec, _ = _run()
    high = [r["id"] for r in rec.risks if r["severity"] == "high"]
    with pytest.raises(ValueError):                                     # not yet risk reviewed
        proposal.approve(rec, "A", "B", high)
    ready = rec.model_copy(update={"status": "RISK_REVIEWED"})
    with pytest.raises(ValueError):
        proposal.approve(ready, "A", "baseline", high)
    with pytest.raises(ValueError):
        proposal.approve(ready, "  ", "B", high)
    with pytest.raises(ValueError):
        proposal.approve(ready, "A", "B", high[:-1])
    done = proposal.approve(ready, "A", "C", high)
    assert done.status == "APPROVED" and done.approval.scenario == "C" and done.approval.record_version == ready.version
    assert done.approval.rates_source == "illustrative" and done.approval.productivity_profile == "expected"


def test_proposal_is_disabled_until_approved():
    rec, _ = _run()
    with pytest.raises(ValueError):
        proposal.generate_proposal(rec)
    with pytest.raises(ValueError):
        proposal.facts(rec)


# ---------- proposal, leak guardrail, consistency ----------

def test_facts_for_the_agent_hold_no_amounts():
    text = str(proposal.facts(_approved())) + str(proposal.payment_shares(_approved()))
    for word in ("price", "cost", "amount", "rate", "margin", "hours"):
        assert word not in text.lower()


def test_scope_comes_from_the_approved_brs_and_team_has_no_rates():
    rec, sections, _ = _draft()
    by = {s["title"]: s for s in sections}
    assert any(rec.brd.requirements[0].statement in b.get("items", []) for b in by["Functional and Non-Functional Scope"]["blocks"])
    assert any(rec.brd.requirements[0].title in b.get("items", []) for b in by["Client Requirements"]["blocks"])
    assert by["Project Team"]["blocks"][0]["header"] == ["Role", "Allocation (FTE)"]
    assert len(sections) == 13


def test_clean_draft_passes_the_guardrail_and_the_consistency_check():
    rec, _, data = _draft()
    res = proposal.check_document(data, rec, RATES)
    assert res["leak_hits"] == []
    assert res["consistency"]["ok"], res


@pytest.mark.parametrize("planted,kind", [
    ("Internal reference: 1,001.", "rate"),
    ("Gross margin is good.", "internal term"),
    ("Our contingency is fine.", "internal term"),
    ("The published rate applies.", "internal term"),
    ("Markup applies.", "internal term"),
    ("Backend 1,234 hours.", "hours")])
def test_guardrail_blocks_planted_leaks_and_names_the_section(planted, kind):
    rec, sections, _ = _draft()
    table = costing.RateTable("uploaded", {**RATES.published, "Project Manager": 1001.0}, {})
    bad = [{**s, "blocks": s["blocks"] + [{"type": "p", "text": planted}]} if s["title"] == "Project Team" else s for s in sections]
    blocked = leak.blocked_values(rec, table)
    blocked["hours"].add("1234")
    hits = leak.scan(proposal.render_docx(rec, bad), blocked)
    assert {"section": "Project Team", "kind": kind} in hits


def test_guardrail_blocks_cost_totals_and_other_scenario_price():
    rec, sections, _ = _draft("B")
    blocked = leak.blocked_values(rec, RATES)
    for value in (rec.costing.scenarios["B"]["delivery_cost"], rec.costing.scenarios["C"]["price_ex_vat"]):
        bad = [{**s, "blocks": s["blocks"] + [{"type": "p", "text": f"Figure {value:,.2f}."}]}
               if s["title"] == "Project Team" else s for s in sections]
        assert {"section": "Project Team", "kind": "cost"} in leak.scan(proposal.render_docx(rec, bad), blocked)


def test_debug_toggle_plants_a_rate_and_is_off_by_default(monkeypatch):
    rec, sections, _ = _draft()
    monkeypatch.delenv("LEAK_TEST_INJECT", raising=False)
    assert proposal.plant_leak(sections, RATES) == sections
    monkeypatch.setenv("LEAK_TEST_INJECT", "rate")
    hits = leak.scan(proposal.render_docx(rec, proposal.plant_leak(sections, RATES)), leak.blocked_values(rec, RATES))
    assert {"section": "Commercial Pricing and Payment Milestones", "kind": "rate"} in hits


def test_guardrail_logs_the_section_never_the_value(monkeypatch):
    logged = []
    monkeypatch.setattr(leak, "log_event", lambda *a, **k: logged.append((a, k)))
    rec, sections, _ = _draft()
    bad = [{**s, "blocks": s["blocks"] + [{"type": "p", "text": "Figure 1,001."}]} if s["title"] == "Project Team" else s
           for s in sections]
    table = costing.RateTable("uploaded", {**RATES.published, "Project Manager": 1001.0}, {})
    leak.scan(proposal.render_docx(rec, bad), leak.blocked_values(rec, table))
    assert logged and "1,001" not in str(logged) and "1001" not in str(logged) and "Project Team" in str(logged)


def test_consistency_check_catches_a_planted_mismatch_and_placeholder():
    rec, sections, _ = _draft()
    c = rec.costing.scenarios["B"]
    price = proposal._php(c["price_ex_vat"])
    bad = []
    for s in sections:
        blocks = []
        for b in s["blocks"]:
            if b["type"] == "table":
                b = {**b, "rows": [[v.replace(price, "PHP 1.00") for v in row] for row in b["rows"]]}
            blocks.append(b)
        bad.append({**s, "blocks": blocks})
    res = proposal.validate_document_consistency(proposal.render_docx(rec, bad), rec)
    assert not res["ok"] and "price excluding VAT" in res["mismatches"]
    planted = [{**s, "blocks": s["blocks"] + [{"type": "p", "text": "Contact [NAME] (TBD)."}]} for s in sections]
    res = proposal.validate_document_consistency(proposal.render_docx(rec, planted), rec)
    assert not res["ok"] and "[NAME]" in res["placeholders"] and "TBD" in res["placeholders"]


def test_draft_brd_passes_the_guardrail():
    rec, _ = _run()
    assert leak.scan(brd.draft_docx(rec), leak.blocked_values(rec, RATES)) == []


def test_a_percentage_the_tor_states_is_not_a_leak_but_an_internal_one_is():
    rec, _ = _run()
    assert "30%" in leak.blocked_values(rec, RATES)["percent"]            # the minimum margin
    rec.brd.constraints = [BrdItem(id="CON-01", text="Proposals are evaluated on price (30%).",
                                   citations=[Locator(file="t", page=1, quote="price (30%)")])]
    assert "30%" not in leak.blocked_values(rec, RATES)["percent"]
    assert leak.scan(brd.draft_docx(rec), leak.blocked_values(rec, RATES)) == []


@pytest.mark.parametrize("word", ["robust", "user-friendly", "seamless", "world-class", "best-in-class", "cutting-edge",
                                  "state-of-the-art"])
def test_marketing_filler_is_flagged_and_not_rewritten(word):
    rec, sections, _ = _draft()
    text = f"A {word} solution."
    planted = [{**s, "blocks": s["blocks"] + [{"type": "p", "text": text}]} if s["title"] == "Executive Summary" else s
               for s in sections]
    data = proposal.render_docx(rec, planted)
    assert {"section": "Executive Summary", "word": word, "count": 1} in proposal.wording_flags(data)
    assert any(text in lines for _, lines in leak.docx_sections(data))         # the text stays as written


def test_clean_text_has_no_filler_flags():
    assert proposal.wording_flags(_draft()[2]) == []


def test_meets_requested_duration_shows_fte_for_b_and_c():
    rec, _ = _run()
    m = rec.estimates["meets_requested_duration"]
    assert m["requested_months"] == 5
    assert m["window_months"] == pytest.approx(5 - (4 + 4 + 2) / 4.33)
    for s in ("B", "C"):
        assert m["scenarios"][s]["months"] <= 5 + 1e-9
        assert m["scenarios"][s]["developers"] == m["scenarios"][s]["team"]["Backend"] + m["scenarios"][s]["team"]["Frontend"]
    assert "meets_requested_duration" not in _run(terms={})[0].estimates


def test_header_marks_the_document_as_draft():
    _, _, data = _draft()
    assert any(proposal.HEADER in " ".join(lines) for _, lines in leak.docx_sections(data))


def _with_suggestion(**overrides):
    rec = _rec(**overrides)
    rec.features.append(RecordFeature(id="F-S", epic="X", name="Reference Data", description="d", type="admin_config", priority="Should (pending client confirmation)",
                                      complexity="medium", size="L", route="clear", origin="critic", scope_status="suggested"))
    rec.brd.requirements.append(BusinessRequirement(id="BR-X-01", feature_id="F-S", title="Reference Data", epic="X", statement="The system shall keep reference data.",
                                                    scope_status="suggested", unsupported=True))
    return rec


def test_suggested_scope_stays_out_of_estimate_and_price_and_shows_separately():
    plain, _ = _run()
    rec, _ = estimation.run_estimation(_with_suggestion(), RATES)
    for s in ("baseline", "B", "C"):
        assert rec.estimates["scenarios"][s]["total_hours"] == pytest.approx(plain.estimates["scenarios"][s]["total_hours"])
        assert rec.costing.scenarios[s]["price_ex_vat"] == pytest.approx(plain.costing.scenarios[s]["price_ex_vat"])
    row = rec.estimates["suggested_scope"][0]
    assert row["name"] == "Reference Data" and row["B"] > 0 and row["C"] > 0 and row["baseline"] > row["B"]
    assert not any(r["feature_id"] == "F-S" for r in rec.activities)


def test_accepting_suggested_scope_prices_it():
    rec = _with_suggestion()
    rec.features[-1].scope_status = "in_scope"
    new, _ = estimation.run_estimation(rec, RATES)
    assert new.estimates["suggested_scope"] == []
    assert new.estimates["scenarios"]["B"]["total_hours"] > _run()[0].estimates["scenarios"]["B"]["total_hours"]


def test_suggested_scope_is_not_in_client_documents_or_metrics():
    from docx import Document
    import io
    rec, _ = estimation.run_estimation(_with_suggestion(), RATES)
    ready = rec.model_copy(update={"status": "RISK_REVIEWED"})
    done = proposal.approve(ready, "A", "B", [r["id"] for r in rec.risks if r["severity"] == "high"])
    data = proposal.render_docx(done, proposal.build_sections(done, NARRATIVE))
    text = " ".join(t for _, lines in leak.docx_sections(data) for t in lines)
    assert "Reference Data" not in text and "Reference Data" not in str(proposal.facts(done))
    assert proposal.check_document(data, done, RATES)["consistency"]["ok"]
    assert "Reference Data" not in " ".join(p.text for p in Document(io.BytesIO(brd.draft_docx(done))).paragraphs)
    m = brd.metrics(rec.brd)
    assert m["suggested_scope_brs"] == 1 and m["unsupported_items"] == 0


@pytest.mark.parametrize("status,open_steps", [
    (None, [1]), ("EXTRACTED", [1, 2, 3, 4]), ("ESTIMATED", [1, 2, 3, 4]), ("RISK_REVIEWED", [1, 2, 3, 4, 5]),
    ("APPROVED", [1, 2, 3, 4, 5, 6]), ("EXPORTED", [1, 2, 3, 4, 5, 6])])
def test_reading_steps_open_once_processed_and_only_actions_are_gated(status, open_steps):
    assert [n for n in range(1, 8) if record.step_unlocked(n, status)] == open_steps


def test_unapproved_preview_uses_only_the_cached_text_and_saves_nothing(monkeypatch, tmp_path):
    from app import llm
    monkeypatch.setattr(proposal, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(llm, "model_tag", lambda role: "test")
    monkeypatch.setattr(llm, "chat", lambda *a, **k: pytest.fail("called the LLM"))
    rec, _ = _run()
    assert proposal.unapproved_preview(rec, "B") is None
    trial = rec.model_copy(update={"approval": record.Approval(approved=True, scenario="B")})
    (tmp_path / proposal._cache_name(trial)).write_text(json.dumps(NARRATIVE))
    sections = proposal.unapproved_preview(rec, "B")
    assert len(sections) == 13 and not rec.approval.approved and rec.status == "EXTRACTED"


def test_commercial_terms_read_payment_warranty_and_deliverables():
    from app.determinism import commercial_terms
    t = commercial_terms("The vendor shall deliver: project plan; manuals; and source code. Ten percent (10%) of the contract "
                         "price upon contract signing. Ninety percent (90%) of the contract price upon final acceptance of "
                         "the System. The vendor shall provide a warranty of six (6) months after final acceptance.")
    assert (t["upfront_pct"], t["final_pct"], t["warranty_months"]) == (10, 90, 6)
    assert t["deliverables"] == ["Project plan", "Manuals", "Source code"]

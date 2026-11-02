"""run_estimation: productivity and tooling profiles, team sizing modes, UAT range (no Azure calls)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import costing, effort, estimation  # noqa: E402
from app.record import Opportunity, OpportunityRecord, RecordFeature  # noqa: E402
from app.suitability import apply_rules  # noqa: E402

RATES = costing.illustrative_rates()


def _rec(**overrides) -> OpportunityRecord:
    feats = [RecordFeature(id=f"F{i}", epic="E", name=f"Screen {i}", description="Standard screen", type="screen_crud",
                           priority="Must", complexity="medium", size=s, route="clear")
             for i, s in enumerate(["S", "M", "L", "L", "XL", "M", "M", "L", "M", "S", "L", "L"] * 2)]
    return OpportunityRecord(opportunity=Opportunity(id="t", client="Fictional", project_name="X",
                                                     commercial_terms={"requested_months": 5}),
                             features=feats, assumption_overrides=overrides)


def _run(**overrides):
    return estimation.run_estimation(_rec(**overrides), RATES)


@pytest.mark.parametrize("profile,human,ai_b,ai_c,first,det_b,det_c,integ", [
    ("conservative", 0.90, 0.75, 0.70, 0.50, 0.15, 0.10, 0.04),
    ("expected", 0.85, 0.65, 0.60, 0.40, 0.10, 0.08, 0.03),
    ("optimistic", 0.80, 0.55, 0.50, 0.30, 0.06, 0.05, 0.02)])
def test_profile_totals_match_the_plan(profile, human, ai_b, ai_c, first, det_b, det_c, integ):
    sc = estimation.configs({"productivity_profile": profile})["ai_scenarios"]
    tot = lambda c, s: sum(sc["factors"][c][s].values())  # noqa: E731
    assert (tot("human_led", "B"), tot("human_led", "C")) == (pytest.approx(human), pytest.approx(human))
    assert (tot("ai_assisted", "B"), tot("ai_assisted", "C")) == (pytest.approx(ai_b), pytest.approx(ai_c))
    assert tot("ai_first_with_review", "C") == pytest.approx(first)
    assert (tot("deterministic_automated", "B"), tot("deterministic_automated", "C")) == (pytest.approx(det_b),
                                                                                          pytest.approx(det_c))
    assert sc["integration_overhead"] == integ


def test_expected_is_the_default_and_the_active_profile_is_reported():
    rec, _ = _run()
    assert rec.estimates["productivity_profile"] == "expected" and not rec.estimates["productivity_edited"]
    assert _run(productivity_profile="optimistic")[0].estimates["productivity_profile"] == "optimistic"


def test_more_optimistic_profiles_reduce_effort_and_leave_baseline_alone():
    table = _run()[0].estimates["verdict_by_profile"]
    assert table["conservative"]["C"] < table["expected"]["C"] < table["optimistic"]["C"]
    assert table["conservative"]["B"] < table["expected"]["B"] < table["optimistic"]["B"]
    hours = {p: _run(productivity_profile=p)[0].estimates["scenarios"]["baseline"]["total_hours"] for p in table}
    assert len({round(h, 6) for h in hours.values()}) == 1


def test_profiles_do_not_change_uat_pilot_or_deployment():
    fixed = {}
    for p in ("conservative", "expected", "optimistic"):
        ph = {x["phase"]: x["months"] for x in _run(productivity_profile=p)[0].timeline["C"]["phases"]}
        fixed[p] = (ph["uat"], ph["pilot"], ph["deployment"])
    assert fixed["conservative"] == fixed["expected"] == fixed["optimistic"]


def test_edited_factors_are_flagged_and_excluded_from_the_verdict_table():
    sc = estimation.configs()["ai_scenarios"]
    factors = {**sc["factors"], "human_led": {s: {**v, "production": 1.0} for s, v in sc["factors"]["human_led"].items()}}
    rec, _ = _run(factors=factors)
    assert rec.estimates["productivity_edited"]
    assert rec.estimates["verdict_by_profile"]["expected"] == _run()[0].estimates["verdict_by_profile"]["expected"]


def test_quality_adjustment_hand_computed_and_only_on_ai_categories():
    sc = estimation.configs({"tooling_profile": "hosted_low_cost_api"})["ai_scenarios"]
    a = effort.scenario_components("ai_first_with_review", "C", 100.0, sc)
    assert (a["rework"], a["review"]) == pytest.approx((13.0, 14.0))       # 0.10 + 0.03, 0.12 + 0.02
    assert effort.scenario_components("human_led", "C", 100.0, sc)["total"] == pytest.approx(85.0)
    assert effort.scenario_components("deterministic_automated", "C", 100.0, sc)["total"] == pytest.approx(11.0)


def test_tooling_comparison_and_setup_hours():
    cmp = _run()[0].estimates["tooling_comparison_c_expected"]
    assert set(cmp) == {"commercial_assistants", "hosted_low_cost_api", "self_hosted_open_weights"}
    assert cmp["hosted_low_cost_api"]["hours"] > cmp["commercial_assistants"]["hours"]
    assert cmp["self_hosted_open_weights"]["hours"] > cmp["hosted_low_cost_api"]["hours"]
    commercial = _run()[0].estimates["scenarios"]
    assert _run(tooling_profile="hosted_low_cost_api")[0].estimates["data_residency_risk"]
    assert not _run()[0].estimates["data_residency_risk"]
    assert commercial["baseline"]["total_hours"] == pytest.approx(
        _run(tooling_profile="self_hosted_open_weights")[0].estimates["scenarios"]["baseline"]["total_hours"])


def test_target_duration_reduces_developers_and_same_team_keeps_the_template():
    rec, _ = _run()
    h = rec.estimates["headcount"]
    assert h["baseline"]["developers"] == 5.0
    assert h["C"]["developers"] <= h["B"]["developers"] < h["baseline"]["developers"]
    assert rec.estimates["scenarios"]["B"]["development_window_months"] <= (
        rec.estimates["scenarios"]["baseline"]["development_window_months"] + 1e-9)
    same, _ = _run(team_sizing_mode="same_team")
    assert same.estimates["headcount"]["B"]["developers"] == 5.0
    assert same.timeline["B"]["team"] == rec.timeline["B_same_team"]["team"]


def test_client_requested_window_needs_more_people_than_the_baseline_window():
    rec, _ = _run(target_window="client_requested")
    window = rec.estimates["team_sizing"]["window"]
    assert window["setting"] == "client_requested"
    assert window["months"] == pytest.approx(5 - (4 + 4 + 2) / 4.33)
    assert rec.estimates["scenarios"]["B"]["development_window_months"] <= window["months"] + 1e-9


def test_ba_warning_when_the_ba_need_exceeds_the_department_share():
    rec, _ = _run(target_window="custom", target_window_custom_months=0.2)
    assert rec.estimates["ba_warnings"]


@pytest.mark.parametrize("weeks", [1, 7])
def test_uat_weeks_outside_the_range_are_rejected(weeks):
    with pytest.raises(ValueError):
        estimation.configs({"uat_weeks": weeks})


def test_dp04_split_rows_hand_computed():
    f = _rec().features[1]
    key = f"{f.id}|QA.test_execution"
    cats = {key: apply_rules({"id": f.id, "name": f.name, "description": f.description, "route": "clear",
                              "type": f.type}, "QA.test_execution", "ai_assisted", "x")}
    sc = effort.scenarios_config()
    rows = [r for r in effort.calculate_effort([f], "standard", cats, sc_cfg=sc, automatable_share=0.7)["rows"]
            if r["feature_id"] == f.id and r["activity"] == "QA.test_execution"]
    auto, rest = rows
    total = auto["baseline"] + rest["baseline"]
    assert auto["baseline"] == pytest.approx(total * 0.7) and rest["baseline"] == pytest.approx(total * 0.3)
    assert (auto["category"], rest["category"]) == ("deterministic_automated", "ai_assisted")
    assert auto["C"]["total"] == pytest.approx(auto["baseline"] * (0.08 + 0.03))
    assert rest["C"]["total"] == pytest.approx(rest["baseline"] * (0.60 + 0.03))
    assert auto["B"]["total"] == pytest.approx(auto["baseline"] * (0.65 + 0.03))    # all ai_assisted in B
    assert rest["B"]["total"] == pytest.approx(rest["baseline"] * (0.65 + 0.03))


def test_ai_hours_treats_ai_first_as_assisted_in_b():
    rows = [{"category": "ai_first_with_review", "category_b": "ai_first_with_review", "B": {"total": 10.0},
             "C": {"total": 5.0}},
            {"category": "deterministic_automated", "category_b": "ai_assisted", "B": {"total": 4.0}, "C": {"total": 1.0}},
            {"category": "human_led", "category_b": "human_led", "B": {"total": 9.0}, "C": {"total": 9.0}}]
    assert effort.ai_hours(rows, "B") == {"ai_first": 0.0, "ai_assisted": 14.0}
    assert effort.ai_hours(rows, "C") == {"ai_first": 5.0, "ai_assisted": 0.0}
    assert effort.ai_hours(rows, "baseline") == {"ai_first": 0.0, "ai_assisted": 0.0}


def test_breakdown_buckets_sum_to_each_total():
    for over in ({}, {"tooling_profile": "self_hosted_open_weights"}, {"team_sizing_mode": "same_team"}):
        e = _run(**over)[0].estimates
        for s in ("baseline", "B", "C"):
            assert sum(e["breakdown"][s].values()) == pytest.approx(e["scenarios"][s]["total_hours"])
    e = _run(tooling_profile="self_hosted_open_weights")[0].estimates
    assert e["breakdown"]["C"]["tooling_setup"] == 40 and e["breakdown"]["baseline"]["tooling_setup"] == 0
    rec = _rec()
    rec.ai_suitability = {f"{f.id}|Backend.coding": {"category": "ai_first_with_review"} for f in rec.features}
    bd = estimation.run_estimation(rec, RATES)[0].estimates["breakdown"]
    assert bd["B"]["ai_first_with_review"] == 0                  # treated as ai_assisted in B
    assert bd["C"]["ai_first_with_review"] > 0


def test_not_reducible_line_is_c_support_plus_pm_ta():
    e = _run()[0].estimates
    b = e["breakdown"]["C"]
    assert e["not_reducible_by_ai"]["hours"] == pytest.approx(b["support"] + b["pm_ta_overhead"])
    assert e["not_reducible_by_ai"]["share_of_c_pct"] == pytest.approx(
        (b["support"] + b["pm_ta_overhead"]) / e["scenarios"]["C"]["total_hours"] * 100)


def test_c_support_is_lower_and_durations_stay_client_bound():
    rec = _run()[0]
    e = rec.estimates
    months = (4 + 4) / 4.33
    assert e["breakdown"]["B"]["support"] - e["breakdown"]["C"]["support"] == pytest.approx((0.25 + 0.5) * months * 160)
    assert e["breakdown"]["baseline"]["support"] == pytest.approx(e["breakdown"]["B"]["support"])
    for s in ("B", "C"):
        ph = {p["phase"]: p["months"] for p in rec.timeline[s]["phases"]}
        assert ph["uat"] == pytest.approx(4 / 4.33) and ph["pilot"] == pytest.approx(4 / 4.33)


def test_break_even_rework_makes_cost_equal_to_commercial():
    rec = _run()[0]
    points = rec.estimates["tooling_break_even_rework"]
    assert set(points) == {"hosted_low_cost_api", "self_hosted_open_weights"}
    commercial, view = estimation._what_if(rec, RATES, None, productivity_profile="expected", factors=None,
                                           integration_overhead=None, tooling_profile="commercial_assistants")
    tl = commercial.timeline["C"]
    for name, v in points.items():
        assert v["assumed_rework"] > 0
        if v["break_even_rework"] is None:
            continue
        cfg = estimation.configs({"productivity_profile": "expected", "tooling_profile": name})
        sc = {**cfg["ai_scenarios"], "quality_adjustment": {**cfg["ai_scenarios"]["quality_adjustment"],
                                                            "rework": v["break_even_rework"]}}
        rows = effort.calculate_effort(rec.features, "standard", rec.ai_suitability, sc_cfg=sc)["rows"]
        hours = estimation._with_overhead(effort.role_hours(rows, "C"), tl["team"], tl["total_months"], cfg["schedule"], "C",
                                          float(cfg["active_tooling"].get("setup_devops_hours", 0)))
        cost = costing.calculate_cost_and_margin(hours, tl, costing.blended_rates(RATES), effort.ai_hours(rows, "C"), "C",
                                                 cfg["commercial"], cfg["active_tooling"])
        assert cost["delivery_cost"] == pytest.approx(view["costs"]["C"]["delivery_cost"])


def test_units_convert_for_display_and_calculations_stay_in_hours():
    from app import units
    u = units.available()
    assert list(u) == ["Man-days", "Hours", "Person-months"]             # man-days first: the default
    assert u["Man-days"].value(80) == 10 and u["Man-days"].show(4157) == "520 man-days"
    assert u["Hours"].show(4157) == "4,157 h"
    assert u["Person-months"].value(320) == 2 and u["Person-months"].show(4157) == "26.0 person-months"
    assert "total_hours" in _run()[0].estimates["scenarios"]["C"]


# ---------- AI Engineer ----------

def _categorized(**overrides):
    rec = _rec(**overrides)
    rec.ai_suitability = {f"{f.id}|Backend.coding": {"category": "ai_first_with_review"} for f in rec.features}
    return estimation.run_estimation(rec, RATES)[0]


def test_orchestration_moves_to_the_ai_engineer_with_total_hours_unchanged():
    row = {"role": "Backend", "category": "ai_first_with_review", "category_b": "ai_first_with_review",
           "baseline": 100.0, "B": {"total": 68.0, "orchestration": 5.0}, "C": {"total": 43.0, "orchestration": 8.0}}
    assert effort.row_role_hours(row, "C") == {"Backend": 35.0, "AI Engineer": 8.0}
    assert effort.row_role_hours(row, "B") == {"Backend": 63.0, "AI Engineer": 5.0}
    assert effort.row_role_hours(row, "baseline") == {"Backend": 100.0}
    human = {**row, "category": "human_led", "category_b": "human_led", "C": {"total": 85.0, "orchestration": 0.0}}
    assert effort.row_role_hours(human, "C") == {"Backend": 85.0}


def test_ai_engineer_is_sized_from_hours_and_absent_from_the_baseline():
    rec = _categorized()
    h = rec.estimates["headcount"]
    assert h["baseline"]["AI Engineer"] == 0
    assert rec.estimates["scenarios"]["baseline"]["by_role"]["AI Engineer"] == 0
    for s in ("B", "C"):
        load = rec.estimates["ai_engineer"][s]
        assert h[s]["AI Engineer"] == load["fte"]
        assert load["fte"] * 4 == int(load["fte"] * 4)                   # steps of 0.25
        assert load["fte_needed"] <= load["fte"] + 1e-9                  # never overbooked
        assert 0 <= load["spare_fte"] < 0.25 + 1e-9


def test_pipeline_setup_is_b_and_c_only_and_in_the_breakdown():
    e = _categorized().estimates
    assert e["breakdown"]["baseline"]["ai_pipeline_setup"] == 0
    assert e["breakdown"]["B"]["ai_pipeline_setup"] == e["breakdown"]["C"]["ai_pipeline_setup"] == 120
    for s in ("baseline", "B", "C"):
        assert sum(e["breakdown"][s].values()) == pytest.approx(e["scenarios"][s]["total_hours"])


def test_only_spare_capacity_times_coding_share_counts_as_backend():
    from app import schedule
    row = {"role": "Backend", "activity": "Backend.coding", "category": "ai_assisted", "category_b": "ai_assisted", "baseline": 1.0,
           "B": {"total": 200.0, "orchestration": 100.0}, "C": {"total": 1.0, "orchestration": 0.5}}
    cfg = schedule.schedule_config()
    res = schedule.size_ai_engineer([row], "B", 2.0, cfg)                # 100 h / (2 months x 160) = 0.3125 -> 0.5
    assert res["fte_needed"] == pytest.approx(0.3125) and res["fte"] == 0.5
    assert res["spare_fte"] == pytest.approx(0.1875) and res["coding_fte"] == pytest.approx(0.09375)
    none = schedule.size_ai_engineer([row], "B", 2.0, {**cfg, "ai_engineer": {**cfg["ai_engineer"], "coding_share": 0.0}})
    assert none["fte"] == 0.5 and none["coding_fte"] == 0.0              # at 0: pipeline and orchestration only
    assert schedule._capacity({"Backend": 2, "AI Engineer": 0.5, "TA": 0}, 0.0, False, 0.09375)["Backend"] == pytest.approx(2.09375)


def test_a_higher_coding_share_never_needs_more_backend():
    rec = _categorized(ai_engineer_coding_share=0.0).estimates["headcount"]["C"]["Backend"]
    assert _categorized().estimates["headcount"]["C"]["Backend"] <= rec


def test_fixed_team_also_sizes_the_ai_engineer_from_hours():
    rec = _categorized(team_sizing_mode="same_team")
    assert rec.timeline["C_same_team"]["team"]["AI Engineer"] > 0
    assert rec.estimates["ai_engineer"]["C"]["fte_needed"] <= rec.estimates["ai_engineer"]["C"]["fte"] + 1e-9


def test_ai_feature_is_owned_by_the_ai_engineer_and_rates_cover_the_role():
    from types import SimpleNamespace as N
    f = N(id="F", epic="E", name="Chatbot", size="M", route="clear", complexity="high", type="ai_ml_feature")
    rows = effort.calculate_effort([f], "standard", {})["rows"]
    assert {r["role"] for r in rows if r["feature_id"] == "F" and r["activity"].startswith("Backend.")} == {"AI Engineer"}
    assert "AI Engineer" in costing.blended_rates(RATES)


def test_rates_file_without_ai_engineer_uses_the_illustrative_rate_and_says_so():
    mapping = costing.mapping_config()
    rows = [f"{r},{1001 + 7 * i}," for i, r in enumerate(mapping["source_roles"]) if r != "AI Engineer"]
    table = costing.parse_rates_csv(("role_name,published_rate_php_per_hour,applied_rate_php_per_hour\n" + "\n".join(rows)).encode())
    assert table.defaulted == ("AI Engineer",)
    assert table.published["AI Engineer"] == RATES.published["AI Engineer"]
    assert costing.blended_rates(table)["AI Engineer"]["applied"] == RATES.published["AI Engineer"]
    with_row = rows + [f"AI Engineer,1234,"]
    full = costing.parse_rates_csv(("role_name,published_rate_php_per_hour,applied_rate_php_per_hour\n" + "\n".join(with_row)).encode())
    assert full.defaulted == () and full.published["AI Engineer"] == 1234
    missing_other = [r for r in rows if not r.startswith("Project Manager")]
    with pytest.raises(costing.RatesError, match="Project Manager"):
        costing.parse_rates_csv(("role_name,published_rate_php_per_hour,applied_rate_php_per_hour\n" + "\n".join(missing_other)).encode())

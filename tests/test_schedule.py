"""generate_project_schedule: calibration consistency and phase rules (no Azure calls)."""
import sys
from pathlib import Path
from types import SimpleNamespace as N

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import calculator as calc, effort, schedule  # noqa: E402
from app.config import estimation_config  # noqa: E402

SCHED = schedule.schedule_config()


def _project(pid):
    from app.retrieval import load_corpus
    return [N(id=c.id, epic=c.metadata["epic"], name=c.metadata["feature"], size=c.metadata["size"], route="clear",
              complexity="medium") for c in load_corpus()
            if c.metadata.get("kind") == "feature" and c.metadata["project_id"] == pid]


def test_activity_phase_shares_sum_to_one_and_cover_every_activity():
    acts = {f"{r}.{k}" for r, a in effort.activities_config()["activities"].items() for k in a}
    assert set(SCHED["activity_phases"]) == acts
    for k, v in SCHED["activity_phases"].items():
        assert sum(v.values()) == pytest.approx(1.0), k


@pytest.mark.parametrize("pid,complexity,size,months", [("P01", "standard", "Mid", 6), ("P02", "high", "Large", 9)])
def test_calibration_regression_after_role_remap(pid, complexity, size, months):
    """Historical baseline with v2 roles (DevOps pooled with Backend, as the historical teams worked)."""
    cfg = estimation_config()
    feats = _project(pid)
    v1 = calc.estimate([f.size for f in feats], cfg, complexity=complexity)
    assert v1.project_size == size
    rows = effort.calculate_effort(feats, complexity, {})["rows"]
    team = {**schedule.template_team(size, cfg, SCHED), "DevOps": 0.0}
    tl = schedule.generate_project_schedule(rows, "baseline", team, size, cfg["calibration"]["ta_coding_share"],
                                            pool_devops=True)
    assert tl["core_months"] == pytest.approx(months, rel=0.10)


def _hris_like():
    cfg = estimation_config()
    feats = _project("P01")
    rows = effort.calculate_effort(feats, "standard", {"x": {}})["rows"]
    return cfg, rows, schedule.template_team("Mid", cfg, SCHED)


def test_uat_is_not_reduced_by_ai_and_phases_are_ordered():
    cfg, rows, team = _hris_like()
    b = schedule.generate_project_schedule(rows, "B", team, "Mid", 0.0)
    c = schedule.generate_project_schedule(rows, "C", team, "Mid", 0.0)
    uat = lambda t: next(p for p in t["phases"] if p["phase"] == "uat")["months"]  # noqa: E731
    assert uat(b) == uat(c) == pytest.approx(SCHED["uat_weeks"] / SCHED["weeks_per_month"])
    for t in (b, c):
        ph = {p["phase"]: p for p in t["phases"]}
        assert ph["architecture"]["start_month"] < ph["requirements"]["end_month"]     # overlap
        assert ph["pilot"]["months"] == pytest.approx(SCHED["pilot_weeks"] / SCHED["weeks_per_month"])
        for a, z in [("development", "sit"), ("sit", "testing"), ("testing", "uat"), ("uat", "pilot"),
                     ("pilot", "deployment")]:
            assert ph[a]["end_month"] == pytest.approx(ph[z]["start_month"])
        assert t["total_months"] == pytest.approx(ph["deployment"]["end_month"])
    assert c["total_months"] <= b["total_months"]


def test_duration_is_not_effort_divided_by_developers():
    cfg, rows, team = _hris_like()
    b = schedule.generate_project_schedule(rows, "B", team, "Mid", 0.0)
    naive = sum(effort.role_hours(rows, "B").values()) / (team["Backend"] * SCHED["hours_per_fte_month"])
    assert abs(b["core_months"] - naive) > 0.5
    doubled = {**team, "Backend": team["Backend"] * 2}
    b2 = schedule.generate_project_schedule(rows, "B", doubled, "Mid", 0.0)
    assert b2["core_months"] > b["core_months"] / 2          # other roles still bound the timeline


@pytest.mark.parametrize("pid,complexity,size", [("P01", "standard", "Mid"), ("P02", "high", "Large")])
def test_size_is_smallest_template_that_fits_its_months(pid, complexity, size):
    rows = effort.calculate_effort(_project(pid), complexity, {})["rows"]
    res = schedule.classify_size(rows, estimation_config())
    assert res["size"] == size
    assert all(c["core_months"] > c["limit_months"] for c in res["checks"][:-1])


def test_support_team_per_scenario_hand_computed():
    months = (SCHED["uat_weeks"] + SCHED["pilot_weeks"]) / SCHED["weeks_per_month"]
    for s in ("baseline", "B"):
        hours = schedule.support_hours(s)
        assert hours["Backend"] == pytest.approx(1 * months * 160)
        assert hours["QA"] == pytest.approx(1 * months * 160)
        assert hours["PM"] == pytest.approx(0.5 * months * 160)
    c = schedule.support_hours("C")                              # DP-04: automated regression
    assert c["Backend"] == pytest.approx(0.75 * months * 160)
    assert c["QA"] == pytest.approx(0.5 * months * 160)
    assert c["PM"] == pytest.approx(0.5 * months * 160)
    # PM/TA template overhead excludes the support phases (no double counting)
    oh = schedule.overhead_hours({"PM": 1, "TA": 0.5}, 10.0)
    assert oh["PM"] == pytest.approx((10.0 - months) * 160)


def test_go_live_milestone_falls_after_the_pilot():
    from app import costing
    cfg, rows, team = _hris_like()
    t = schedule.generate_project_schedule(rows, "B", team, "Mid", 0.0)
    ph = {p["phase"]: p for p in t["phases"]}
    rates = {r: {"published": 1.0, "applied": 1.0} for r in costing.mapping_config()["app_roles"]}
    c = costing.calculate_cost_and_margin({"Backend": 1.0}, t, rates, {"ai_first": 0.0, "ai_assisted": 0.0}, "B")
    go_live = c["milestones"][-1]
    assert go_live["date"] == ph["deployment"]["end_date"] > ph["pilot"]["end_date"]


# ---------- target_duration team sizing ----------

def _rows(hours: dict[str, float]) -> list[dict]:
    """One activity per role, all in the development phase."""
    act = {"BA": "BA.stories_acceptance", "Backend": "Backend.coding", "Frontend": "Frontend.ui_implementation",
           "QA": "QA.test_design", "DevOps": "DevOps.pipeline_environment"}
    return [{"role": r, "activity": act[r], "baseline": h / 0.5, "B": {"total": h}, "C": {"total": h / 2}}
            for r, h in hours.items()]


@pytest.mark.parametrize("value,expected", [(0.1, 0.5), (0.5, 0.5), (0.51, 1.0), (2.0, 2.0), (2.26, 2.5)])
def test_round_up_to_half_fte(value, expected):
    assert schedule._round_up(value, 0.5) == expected


def test_size_team_formula_rounding_and_minimums():
    cfg = estimation_config()
    # window 4 months = 640 h per FTE
    rows = _rows({"BA": 64.0, "Backend": 1500.0, "Frontend": 640.0, "QA": 700.0})
    res = schedule.size_team(rows, "B", 4.0, "Mid", 0.0, cfg)
    t = res["team"]
    assert t["Backend"] == 2.5                               # 1500 / 640 = 2.34 -> 2.5
    assert t["Frontend"] == 1.0                              # exactly 1.0 stays 1.0
    assert t["QA"] == 1.5                                    # 1.09 -> 1.5
    assert t["BA"] == 0.5                                    # minimum
    assert t["DevOps"] == 0.0                                # no hours: no FTE
    assert (t["PM"], t["TA"]) == (1, 0.5)                    # from the template
    assert res["added"] == []


def test_size_team_ba_minimum_without_ba_hours():
    res = schedule.size_team(_rows({"Backend": 640.0}), "B", 4.0, "Mid", 0.0, estimation_config())
    assert res["team"]["BA"] == 0.5


def test_size_team_fits_the_window_on_real_rows():
    cfg = estimation_config()
    rows = effort.calculate_effort(_project("P01"), "standard", {})["rows"]
    base = schedule.generate_project_schedule(rows, "baseline", {**schedule.template_team("Mid", cfg, SCHED), "DevOps": 0.0},
                                              "Mid", cfg["calibration"]["ta_coding_share"], pool_devops=True)
    res = schedule.size_team(rows, "C", base["core_months"], "Mid", 0.0, cfg)
    tl = schedule.generate_project_schedule(rows, "C", res["team"], "Mid", 0.0)
    assert tl["core_months"] <= base["core_months"] + 1e-9
    b = schedule.size_team(rows, "B", base["core_months"], "Mid", 0.0, cfg)["team"]
    assert schedule.headcount(res["team"])["developers"] <= schedule.headcount(b)["developers"]
    assert schedule.headcount(b)["developers"] < 5           # template: Backend 4 + Frontend 1


def test_size_team_adds_steps_when_the_phase_sequence_overruns():
    cfg = estimation_config()
    # SIT and testing lie on the critical path; a role's total fits the window but not its share of the sequence
    rows = [{"role": "QA", "activity": "QA.regression_defects", "baseline": 1.0, "B": {"total": 1000.0}, "C": {"total": 1.0}},
            {"role": "DevOps", "activity": "DevOps.pipeline_environment", "baseline": 1.0, "B": {"total": 1000.0},
             "C": {"total": 1.0}}]
    window = 2.0
    res = schedule.size_team(rows, "B", window, "Mid", 0.0, cfg)
    tl = schedule.generate_project_schedule(rows, "B", res["team"], "Mid", 0.0)
    assert tl["core_months"] <= window + 1e-9


def test_resolve_window_settings():
    months = schedule.support_months(SCHED) + SCHED["deployment_weeks"] / SCHED["weeks_per_month"]
    base = {**SCHED, "team_sizing": {**SCHED["team_sizing"], "target_window": "baseline"}}
    assert schedule.resolve_window(3.4, 5, base)["months"] == 3.4
    req = {**SCHED, "team_sizing": {**SCHED["team_sizing"], "target_window": "client_requested"}}
    assert schedule.resolve_window(3.4, 5, req)["months"] == pytest.approx(5 - months)
    fallback = schedule.resolve_window(3.4, None, req)
    assert fallback["months"] == 3.4 and fallback["note"]
    custom = {**SCHED, "team_sizing": {**SCHED["team_sizing"], "target_window": "custom", "custom_months": 3.0}}
    assert schedule.resolve_window(3.4, 5, custom)["months"] == 3.0


def test_headcount_counts_developers():
    h = schedule.headcount({"PM": 1, "TA": 0.5, "BA": 1, "Backend": 3.5, "Frontend": 1, "QA": 1.5, "DevOps": 0.5})
    assert h["developers"] == 4.5 and h["total"] == 9.0

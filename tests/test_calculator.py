"""Calculator tests with hand-computed expected values.

Test config (simple numbers):
  S tier: BA 10, Dev 40, UI 10, QA 20 (pre-AI); foundation BA 10, Dev 80, UI 20, QA 0.
  AI factors: BA 1.0, Dev 0.4, UI 0.5, QA 0.5.  Mid template: PM 1, TA 0.5, Dev 2, UI 1, BA 1, QA 1.
  Contingency 1.2, 160 h/month -> 133.33 productive hours per FTE-month.
  10 S features -> baseline BA 110, Dev 480, UI 120, QA 200.
"""
import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import calculator as calc  # noqa: E402

BASE = {
    "size_hours": {
        "S": {"BA": 10, "Dev": 40, "UI": 10, "QA": 20},
        "M": {"BA": 25, "Dev": 100, "UI": 25, "QA": 50},
        "L": {"BA": 50, "Dev": 200, "UI": 50, "QA": 100},
        "XL": {"BA": 100, "Dev": 400, "UI": 100, "QA": 200},
    },
    "ai_factors": {"BA": 1.0, "Dev": 0.4, "UI": 0.5, "QA": 0.5, "PM": 1.0, "TA": 1.0},
    "estimation": {"ta_coding_share": 0.0, "default_domain_complexity": "standard"},
    "calibration": {"ta_coding_share": 0.0},
    "complexity_multipliers": {"standard": {"BA": 1.0, "Dev": 1.0, "UI": 1.0, "QA": 1.0},
                               "high": {"BA": 1.5, "Dev": 2.0, "UI": 1.0, "QA": 1.5}},
    "buffer": {"mode": "contingency", "contingency": 1.2, "utilization": 0.7},
    "capacity": {"hours_per_person_month": 160, "hours_per_manday": 8, "fte_rounding": 0.5},
    "foundation": {"apply_ai_factor": True,
                   "items": {"f": {"BA": 10, "Dev": 80, "UI": 20, "QA": 0}}},
    "team_templates": {
        "Small": {"months": 4, "PM": 1, "TA": 0.5, "Dev": 1, "UI": 1, "BA": 1, "QA": 1},
        "Mid": {"months": 6, "PM": 1, "TA": 0.5, "Dev": 2, "UI": 1, "BA": 1, "QA": 1},
        "Large": {"months": 9, "PM": 1, "TA": 1, "Dev": 4, "UI": 1.5, "BA": 1, "QA": 3},
        "XL": {"months": 14, "PM": 1, "TA": 1, "Dev": 8, "UI": 2, "BA": 2, "QA": 4},
    },
    "project_size_thresholds": {"Small": 300, "Mid": 1000, "Large": 5000},
    "org_capacity": {"ba_available_department": 6, "ba_warn_share": 0.33},
    "uat_support": {"enabled": False, "weeks": 4, "hours_per_week": 40,
                    "fte": {"PM": 0.5, "TA": 0, "BA": 1, "Dev": 1, "UI": 0, "QA": 1}},
}
TEN_S = ["S"] * 10


@pytest.fixture
def cfg():
    return copy.deepcopy(BASE)


# ---------- buffer modes ----------

def test_contingency_mode(cfg):
    assert calc.effective_month_hours(cfg) == pytest.approx(133.3333, rel=1e-4)
    assert calc.months_for(1200, 1, cfg) == pytest.approx(9.0)        # 1200 x 1.2 / 160
    assert calc.buffered(1200, cfg) == pytest.approx(1440)            # buffer shown in hours


def test_utilization_mode_applies_no_contingency(cfg):
    cfg["buffer"]["mode"] = "utilization"
    assert calc.effective_month_hours(cfg) == pytest.approx(112.0)    # 160 x 0.7, no x1.2
    assert calc.months_for(1120, 1, cfg) == pytest.approx(10.0)
    assert calc.buffered(1120, cfg) == pytest.approx(1120)


def test_unknown_buffer_mode_rejected(cfg):
    cfg["buffer"]["mode"] = "both"
    with pytest.raises(ValueError):
        calc.effective_month_hours(cfg)


# ---------- size classification ----------

@pytest.mark.parametrize("dev_hours,expected", [(0, "Small"), (299, "Small"), (300, "Mid"),
                                                (999, "Mid"), (1000, "Large"), (5000, "XL")])
def test_classify_project_size(cfg, dev_hours, expected):
    assert calc.classify_project_size(dev_hours, cfg) == expected


def test_unsized_features_are_skipped(cfg):
    assert calc.feature_hours(["S", None, "M"], cfg) == {"BA": 35, "Dev": 140, "UI": 35, "QA": 70}


def test_round_up_fte():
    assert calc.round_up_fte(0.458, 0.5) == 0.5
    assert calc.round_up_fte(0.8, 0.5) == 1.0
    assert calc.round_up_fte(1.0, 0.5) == 1.0


# ---------- scenarios ----------

def test_traditional_scenario(cfg):
    res = calc.estimate(TEN_S, cfg)
    assert res.project_size == "Mid"                    # baseline Dev 480 -> Mid
    trad = res.scenarios[0]
    m = {r: trad.lines[r].months for r in calc.FEATURE_ROLES}
    assert m["BA"] == pytest.approx(0.825)              # 110 / 133.33
    assert m["Dev"] == pytest.approx(1.8)               # 480 / (2 x 133.33)
    assert m["UI"] == pytest.approx(0.9)
    assert m["QA"] == pytest.approx(1.5)
    assert trad.bottleneck == "Dev" and trad.lines["Dev"].bottleneck
    assert trad.duration_months == pytest.approx(1.8)
    assert trad.lines["Dev"].hours == pytest.approx(576)     # 480 x 1.2
    assert trad.lines["Dev"].mandays == pytest.approx(72)
    assert trad.lines["PM"].hours == pytest.approx(288)      # 1 x 1.8 x 160
    assert trad.lines["TA"].hours == pytest.approx(144)      # 0.5 x 1.8 x 160


def test_ai_same_team_moves_bottleneck_to_ba(cfg):
    sc = calc.estimate(TEN_S, cfg).scenarios[1]
    # AI hours: BA 110, Dev 192, UI 60, QA 100
    assert sc.lines["Dev"].months == pytest.approx(0.72)     # 192 / 266.67
    assert sc.lines["BA"].months == pytest.approx(0.825)
    assert sc.bottleneck == "BA"
    assert sc.duration_months == pytest.approx(0.825)
    assert sc.lines["PM"].hours == pytest.approx(132)        # 0.825 x 160
    assert sc.lines["TA"].hours == pytest.approx(66)


def test_ai_same_deadline_minimum_fte(cfg):
    sc = calc.estimate(TEN_S, cfg).scenarios[2]
    # need = AI hours / (1.8 x 133.33 = 240): BA .458, Dev .8, UI .25, QA .417 -> round up to 0.5
    assert {r: sc.lines[r].fte for r in calc.FEATURE_ROLES} == {"BA": 0.5, "Dev": 1.0, "UI": 0.5, "QA": 0.5}
    assert sc.lines["BA"].months == pytest.approx(1.65)      # 110 / (0.5 x 133.33)
    assert sc.lines["Dev"].months == pytest.approx(1.44)
    assert sc.bottleneck == "BA"
    assert sc.duration_months == pytest.approx(1.8)          # held at the traditional deadline
    assert sc.lines["PM"].hours == pytest.approx(288)
    assert not sc.warnings


def test_ta_share_traditional_uses_calibration_ai_uses_estimation(cfg):
    cfg["calibration"]["ta_coding_share"] = 0.5
    res = calc.estimate(TEN_S, cfg)
    assert res.scenarios[0].lines["Dev"].months == pytest.approx(1.6)   # 480 / (2.25 x 133.33)
    assert res.scenarios[1].lines["Dev"].months == pytest.approx(0.72)  # AI: 192 / (2 x 133.33), no TA credit
    # same deadline 1.6 -> Dev need 192 / 213.33 = 0.9, no TA credit -> 1.0
    assert res.scenarios[2].lines["Dev"].fte == 1.0


def test_high_complexity_multiplies_feature_hours_only(cfg):
    # features x multiplier: BA 150, Dev 800, UI 100, QA 300; foundation unchanged: +10, +80, +20, +0
    res = calc.estimate(TEN_S, cfg, complexity="high")
    assert res.baseline_dev_hours == pytest.approx(880)
    assert res.project_size == "Mid"
    trad = res.scenarios[0]
    assert trad.lines["Dev"].months == pytest.approx(3.3)     # 880 / 266.67
    assert trad.lines["BA"].hours == pytest.approx(192)       # 160 x 1.2
    assert trad.lines["PM"].hours == pytest.approx(528)       # 3.3 x 160, PM not multiplied


def test_unknown_complexity_rejected(cfg):
    with pytest.raises(ValueError):
        calc.estimate(TEN_S, cfg, complexity="extreme")


def test_utilization_mode_scenario(cfg):
    cfg["buffer"]["mode"] = "utilization"
    trad = calc.estimate(TEN_S, cfg).scenarios[0]
    assert trad.lines["Dev"].months == pytest.approx(480 / 224)   # 2 x 160 x 0.7
    assert trad.lines["Dev"].hours == pytest.approx(480)          # no contingency added


def test_ba_capacity_warning(cfg):
    cfg["org_capacity"]["ba_available_department"] = 1      # limit 0.33 FTE
    res = calc.estimate(TEN_S, cfg)
    assert all(any("BA capacity" in w for w in sc.warnings) for sc in res.scenarios)


def test_uat_support_disabled_by_default_and_optional(cfg):
    res = calc.estimate(TEN_S, cfg)
    assert res.uat_support is None
    assert calc.DEV_PHASE_ASSUMPTION in res.assumptions
    cfg["uat_support"]["enabled"] = True
    res = calc.estimate(TEN_S, cfg)
    assert res.uat_support["BA"] == pytest.approx(160)       # 1 x 4 weeks x 40 h
    assert res.uat_support["PM"] == pytest.approx(80)


def test_real_config_loads_and_is_consistent():
    from app.config import estimation_config
    real = estimation_config()
    assert real["buffer"]["mode"] in ("contingency", "utilization")
    th = real["project_size_thresholds"]
    assert 0 < th["Small"] < th["Mid"] < th["Large"]
    for r in calc.FEATURE_ROLES:
        assert real["size_hours"]["S"][r] < real["size_hours"]["M"][r] < real["size_hours"]["L"][r] < real["size_hours"]["XL"][r]


# ---------- regression on the calibration projects (real config + corpus) ----------

@pytest.mark.parametrize("pid,complexity,size,months", [("P01", "standard", "Mid", 6), ("P02", "high", "Large", 9)])
def test_calibration_regression(pid, complexity, size, months):
    from app.config import estimation_config
    from app.retrieval import load_corpus
    sizes = [c.metadata["size"] for c in load_corpus()
             if c.metadata.get("kind") == "feature" and c.metadata["project_id"] == pid]
    res = calc.estimate(sizes, estimation_config(), complexity=complexity)
    assert res.project_size == size
    assert res.scenarios[0].duration_months == pytest.approx(months, rel=0.10)

"""calculate_effort: hand-computed values, no double counting (no Azure calls)."""
import sys
from pathlib import Path
from types import SimpleNamespace as N

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import calculator as calc, effort  # noqa: E402
from app.config import estimation_config  # noqa: E402

SC = effort.scenarios_config()


def test_activity_shares_sum_to_one_per_role():
    for role, acts in effort.activities_config()["activities"].items():
        assert sum(a["share"] for a in acts.values()) == pytest.approx(1.0), role


def test_components_hand_computed():
    c = effort.scenario_components("ai_first_with_review", "C", 100.0, SC)
    assert (c["production"], c["orchestration"], c["review"], c["rework"]) == pytest.approx((10, 8, 12, 10))
    assert c["integration"] == pytest.approx(3.0)            # 0.03 x 100
    assert c["total"] == pytest.approx(43.0)
    b = effort.scenario_components("ai_first_with_review", "B", 100.0, SC)   # treated as ai_assisted in B
    assert b["production"] == pytest.approx(50) and b["total"] == pytest.approx(68.0)
    d = effort.scenario_components("deterministic_automated", "B", 100.0, SC)
    assert d["total"] == pytest.approx(13.0)                 # 5 + 2 + 3 + 0 + 3


def test_no_double_counting_for_human_led_and_code_review():
    h = effort.scenario_components("human_led", "C", 100.0, SC)
    assert h["orchestration"] == h["review"] == h["rework"] == h["integration"] == 0.0
    assert h["total"] == pytest.approx(85.0)
    # one Backend M feature: AI-first coding gets review on its OWN baseline; the human-led code-review activity
    # stays at 0.9 x its own baseline (not re-added to the coding review component)
    f = N(id="F1", epic="E", name="n", size="M", route="clear", complexity="medium")
    cats = {"F1|Backend.coding": {"category": "ai_first_with_review"},
            "F1|Backend.code_review_integration": {"category": "human_led"}}
    rows = {r["activity"]: r for r in effort.calculate_effort([f], "standard", cats)["rows"] if r["feature_id"] == "F1"}
    coding, review = rows["Backend.coding"], rows["Backend.code_review_integration"]
    assert coding["baseline"] == pytest.approx(41 * 0.50 * 1.2)
    assert coding["C"]["review"] == pytest.approx(coding["baseline"] * 0.12)
    assert review["baseline"] == pytest.approx(41 * 0.15 * 1.2)
    assert review["C"]["total"] == pytest.approx(review["baseline"] * 0.85)


def test_baseline_equals_v1_buffered_hours_after_role_remap():
    sizes = ["S", "M", "M", "L", "XL"]
    feats = [N(id=f"F{i}", epic="E", name=f"f{i}", size=s, route="clear", complexity="medium")
             for i, s in enumerate(sizes)]
    cfg = estimation_config()
    v1 = calc.estimate(sizes, cfg)
    v1_hours = sum(v1.scenarios[0].lines[r].hours for r in calc.FEATURE_ROLES)
    e = effort.calculate_effort(feats, "standard", {})
    assert e["totals"]["baseline"] == pytest.approx(v1_hours)
    rh = effort.role_hours(e["rows"], "baseline")
    assert rh["DevOps"] == pytest.approx((80 + 60 + 60 + 50) * 1.2)   # infra foundation items -> DevOps
    assert rh["Frontend"] == pytest.approx(v1.scenarios[0].lines["UI"].hours)


def test_unsized_and_unclear_features_add_no_hours():
    feats = [N(id="A", epic="E", name="a", size=None, route="clear", complexity="medium"),
             N(id="B", epic="E", name="b", size="L", route="unclear", complexity="medium")]
    rows = effort.calculate_effort(feats, "standard", {})["rows"]
    assert all(r["epic"] == "Foundation" for r in rows)


def test_reductions():
    f = N(id="F1", epic="E", name="n", size="S", route="clear", complexity="medium")
    e = effort.calculate_effort([f], "standard", {})          # default ai_assisted everywhere
    t = e["totals"]
    assert t["reduction_b"] == pytest.approx((1 - 0.68) * 100)    # 0.65 + 0.03 integration
    assert t["reduction_c"] == pytest.approx((1 - 0.63) * 100)
    assert t["reduction_c_vs_b"] == pytest.approx((0.68 - 0.63) / 0.68 * 100)

"""calculate_cost_and_margin and rate handling (illustrative / dummy rates only; no Azure calls)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import costing  # noqa: E402
from app.config import load_yaml  # noqa: E402

MAP = costing.mapping_config()
TIMELINE = {"total_months": 5.0, "team": {"PM": 1, "BA": 1, "Backend": 2},
            "start_date": "2027-01-04",
            "phases": [{"phase": p, "end_date": d} for p, d in
                       [("architecture", "2027-02-01"), ("uat", "2027-05-01"), ("deployment", "2027-06-01")]]}
FLAT = {r: {"published": 1000.0, "applied": 1000.0} for r in MAP["app_roles"]}


def _cost(**cfg_over):
    cfg = {**load_yaml("commercial.yaml"), **cfg_over}
    return costing.calculate_cost_and_margin({"PM": 100, "BA": 100, "Backend": 800}, TIMELINE, FLAT,
                                             {"ai_first": 50.0, "ai_assisted": 0.0}, "C", cfg)


def test_margin_vs_markup_and_price_formula():
    c = _cost()
    assert c["price_ex_vat"] == pytest.approx(c["delivery_cost"] / 0.6)
    assert c["gross_margin_pct"] == pytest.approx(40.0)
    assert c["markup_pct"] == pytest.approx(66.6667, rel=1e-4)     # 0.4 / 0.6


def test_vat_shown_separately():
    c = _cost()
    assert c["vat"] == pytest.approx(c["price_ex_vat"] * 0.12)
    assert c["price_incl_vat"] == pytest.approx(c["price_ex_vat"] + c["vat"])


def test_contingency_base_and_lines_hand_computed():
    c = _cost()
    assert c["labor"] == pytest.approx(1_000_000)                   # 1000 h x 1000
    assert c["ai_tooling"] == pytest.approx(costing.tooling_lines(costing.tooling_profile(), "C", 4, 5.0, {"ai_first": 50.0, "ai_assisted": 0.0})["total"])
    assert c["infrastructure"] == pytest.approx(25000 * 5)
    assert c["contingency"] == pytest.approx(0.15 * (c["labor"] + c["ai_tooling"] + c["infrastructure"]))
    only_labor = _cost(contingency_eligible=["labor"])
    assert only_labor["contingency"] == pytest.approx(0.15 * 1_000_000)


def test_milestones_sum_to_100_percent_and_use_timeline_dates():
    c = _cost()
    assert sum(m["pct"] for m in c["milestones"]) == pytest.approx(1.0)
    assert sum(m["amount"] for m in c["milestones"]) == pytest.approx(c["price_ex_vat"])
    assert [m["date"] for m in c["milestones"]] == ["2027-01-04", "2027-02-01", "2027-05-01", "2027-06-01"]


def test_retention_optional():
    cfg = load_yaml("commercial.yaml")
    c = _cost(retention={**cfg["retention"], "enabled": True})
    assert c["retention"]["amount"] == pytest.approx(c["price_ex_vat"] * 0.20 * 0.10)
    assert c["retention"]["release_date"] == "2027-12-01"


def test_baseline_has_no_ai_tooling():
    c = costing.calculate_cost_and_margin({"Backend": 10}, TIMELINE, FLAT, {"ai_first": 5.0, "ai_assisted": 5.0},
                                         "baseline")
    assert c["ai_tooling"] == 0.0


def _lines(profile, scenario="C", people=4, months=5.0, hours=None):
    return costing.tooling_lines(costing.tooling_profile(profile), scenario, people, months, hours or {"ai_first": 100.0, "ai_assisted": 200.0})


def test_tooling_cost_per_profile_hand_computed():
    tokens = 200 * 150000 + 100 * 600000    # 90 million
    c = _lines("commercial_assistants")
    assert c["million_tokens"] == pytest.approx(tokens / 1e6)
    assert c["seats"] == pytest.approx(2000 * 4 * 5) and c["model_usage"] == pytest.approx(90 * 250) and c["gpu_share"] == 0
    assert c["test_runs"] == pytest.approx(120 * 5 * 30) and c["usage_contingency"] == pytest.approx(0.20 * (90 * 250 + 120 * 5 * 30))
    assert c["total"] == pytest.approx(c["seats"] + c["model_usage"] + c["test_runs"] + c["usage_contingency"])
    h = _lines("hosted_low_cost_api")
    assert h["seats"] == 0 and h["model_usage"] == pytest.approx(90 * 67) and h["gpu_share"] == 0 and h["test_runs"] == pytest.approx(120 * 5 * 10)
    g = _lines("self_hosted_open_weights")
    assert g["seats"] == 0 and g["model_usage"] == 0 and g["gpu_share"] == pytest.approx(60000 * 5 / 3)      # shared by 3 projects
    assert g["usage_contingency"] == pytest.approx(0.20 * 120 * 5 * 2)      # on usage only, not on the GPU
    assert costing.tooling_profile("hosted_low_cost_api")["data_residency_risk"] is True
    assert "data_residency_risk" not in costing.tooling_profile("self_hosted_open_weights")


def test_tooling_uses_the_scenarios_own_test_runs_and_the_baseline_none():
    assert _lines("commercial_assistants", "B")["test_runs"] == pytest.approx(40 * 5 * 30)
    assert _lines("commercial_assistants", "baseline")["total"] == 0.0


def test_prices_and_usage_are_read_from_config():
    tooling = {"seat_php_per_month": 1, "php_per_million_tokens": 2, "php_per_test_run": 3,
               "usage": {"tokens_per_hour": {"ai_assisted": 1000000, "ai_first_with_review": 2000000}, "test_runs_per_month": {"B": 1, "C": 1}, "contingency_pct": 0.5}}
    c = costing.tooling_lines(tooling, "C", 1, 1.0, {"ai_first": 1.0, "ai_assisted": 1.0})
    assert (c["seats"], c["model_usage"], c["test_runs"], c["usage_contingency"]) == (1, 6, 3, 4.5)


def test_net_savings_is_labour_saved_less_ai_tooling():
    costs = {"baseline": {"labor": 1000.0, "ai_tooling": 0.0}, "B": {"labor": 700.0, "ai_tooling": 50.0}, "C": {"labor": 900.0, "ai_tooling": 150.0}}
    net = costing.net_savings(costs)
    assert net["B"] == {"labor_saved": 300.0, "ai_tooling": 50.0, "net_savings": 250.0}
    assert net["C"]["net_savings"] == -50.0        # tooling can cost more than the labour it saves


def test_record_safe_drops_per_role_labor():
    assert "labor_by_role" not in costing.record_safe(_cost())


# ---------- rates ----------

def _csv(rows, header="role_name,published_rate_php_per_hour,applied_rate_php_per_hour"):
    return (header + "\n" + "\n".join(rows)).encode()


DUMMY = [f"{r},{100 + 10 * i}," for i, r in enumerate(MAP["source_roles"])]


def test_dummy_upload_parses_and_blends():
    t = costing.parse_rates_csv(_csv(DUMMY))
    assert t.source == "uploaded" and len(t.published) == len(MAP["source_roles"])
    b = costing.blended_rates(t)
    sr = MAP["source_roles"]
    backend = [100 + 10 * sr.index(r) for r in ("Senior Software Developer", "Software Developer 1",
                                                "Software Developer 2")]
    assert b["Backend"]["published"] == pytest.approx(sum(backend) / 3)
    qa = [100 + 10 * sr.index(r) for r in ("Senior Quality Assurance Engineer", "Quality Assurance Engineer 1",
                                           "Quality Assurance Engineer 1")]
    assert b["QA"]["applied"] == pytest.approx(sum(qa) / 3)          # 1 senior : 2 QA1


def test_applied_rate_and_discount():
    rows = list(DUMMY)
    rows[0] = "Project Manager,1000,800"
    t = costing.parse_rates_csv(_csv(rows))
    b = costing.blended_rates(t, discount_pct=0.10)
    assert b["PM"] == {"published": 1000.0, "applied": 800.0}       # explicit applied rate wins
    ba = 100 + 10 * MAP["source_roles"].index("Business Analyst")
    assert b["BA"]["applied"] == pytest.approx(ba * 0.9)             # discount on published


def test_applied_column_is_optional():
    t = costing.parse_rates_csv(_csv([r.rstrip(",") for r in DUMMY], "role_name,published_rate_php_per_hour"))
    assert t.applied() == t.published


@pytest.mark.parametrize("data,msg", [
    (_csv(DUMMY, "role,rate"), "Missing column"),
    (_csv(DUMMY + ["Chief Wizard,500,"]), "Unknown role"),
    (_csv([r for r in DUMMY if not r.startswith("Project Manager")]), "Missing role"),
    (_csv(["Project Manager,abc,"] + DUMMY[1:]), "not a number"),
    (_csv(["Project Manager,-5,"] + DUMMY[1:]), "greater than zero"),
    (_csv(DUMMY + [DUMMY[0]]), "Duplicate"),
    (b"\xff\xfe\x00bad", "UTF-8"),
])
def test_malformed_files_raise_clear_errors(data, msg):
    with pytest.raises(costing.RatesError, match=msg):
        costing.parse_rates_csv(data)


def test_error_messages_never_include_rate_values():
    with pytest.raises(costing.RatesError) as e:
        costing.parse_rates_csv(_csv(["Project Manager,12345abc,"] + DUMMY[1:]))
    assert "12345" not in str(e.value)


def test_illustrative_rates_cover_every_mapped_role():
    t = costing.illustrative_rates()
    assert t.source == "illustrative"
    assert set(costing.blended_rates(t)) == set(MAP["app_roles"])


def test_record_never_stores_rate_values():
    import re
    from app import estimation
    from app.record import Opportunity, OpportunityRecord, RecordFeature
    rec = OpportunityRecord(opportunity=Opportunity(id="t", client="Fictional", project_name="X"), features=[
        RecordFeature(id=f"F{i}", epic="E", name=f"f{i}", description="d", type="screen_crud", priority="Must",
                      complexity="medium", size=s, route="clear") for i, s in enumerate(["S", "M", "L", "L", "XL", "M"])])
    table = costing.parse_rates_csv(_csv([f"{r},{1001 + 7 * i}," for i, r in enumerate(MAP["source_roles"])]))
    new, view = estimation.run_estimation(rec, table)
    tokens = set(re.findall(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])", new.model_dump_json()))
    values = {f"{v:g}" for v in table.published.values()} | {f"{v:.1f}" for v in table.published.values()}
    for b in costing.blended_rates(table).values():
        values |= {f"{b['applied']:g}", repr(b["applied"])}
    assert not tokens & values
    assert new.costing.rates_source == "uploaded" and new.costing.rates_hash
    assert "labor_by_role" in view["costs"]["B"] and "labor_by_role" not in new.costing.scenarios["B"]

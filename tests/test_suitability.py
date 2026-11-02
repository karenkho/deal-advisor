"""assess_ai_suitability rule overrides (deterministic, no Azure calls)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.suitability import _applied, apply_rules  # noqa: E402

PLAIN = {"id": "F1", "name": "Standard report", "description": "Monthly headcount report", "route": "clear",
         "type": "report_dashboard"}


def _cat(unit, activity, llm="ai_assisted"):
    return apply_rules(unit, activity, llm, "x")["category"]


def test_elicitation_and_acceptance_always_human_led():
    for act in ("BA.elicitation_clarification", "BA.review_acceptance_support"):
        res = apply_rules(PLAIN, act, "ai_first_with_review", "x")
        assert res["category"] == "human_led" and res["rule"] == "DP-07"


def test_security_cap_only_on_security_logic_activities():
    u = {**PLAIN, "name": "Payroll interface", "description": "Export attendance to payroll", "type": "integration"}
    res = apply_rules(u, "Backend.coding", "ai_first_with_review", "x")
    assert res["category"] == "ai_assisted" and res["rule"] == "DP-06" and "rule" in res["reason"]
    assert _cat(u, "Frontend.ui_implementation", "ai_first_with_review") == "ai_first_with_review"
    viewer = {**PLAIN, "name": "Employee directory", "description": "Shows employee personal information"}
    assert _cat(viewer, "Backend.coding") == "ai_first_with_review"              # displaying data is not security logic
    assert _cat(u, "Backend.coding", "human_led") == "human_led"                 # cap never raises
    rbac = {**PLAIN, "name": "Role-based access control", "type": "admin_config"}
    assert _cat(rbac, "Backend.coding", "human_led") == "human_led"              # DP-05 never raises security logic
    assert _cat(rbac, "Backend.coding", "ai_first_with_review") == "ai_assisted"
    fnd = {**PLAIN, "id": "FND-identity_auth_service", "name": "Foundation: Identity auth service"}
    assert _cat(fnd, "Backend.unit_tests", "deterministic_automated") == "ai_assisted"


def test_unclear_capped():
    unclear = {**PLAIN, "route": "unclear"}
    assert _cat(unclear, "Backend.coding", "ai_first_with_review") == "ai_assisted"
    res = apply_rules(unclear, "QA.test_execution", "ai_assisted", "x")
    assert res["category"] == "ai_assisted" and not res["split"]


def test_dp_rules_set_category_whatever_the_llm_said():
    for act, rule in (("BA.stories_acceptance", "DP-01"), ("BA.wireframes_prototypes", "DP-02"),
                      ("QA.test_design", "DP-03")):
        for llm in ("human_led", "deterministic_automated"):
            res = apply_rules(PLAIN, act, llm, "x")
            assert (res["category"], res["rule"]) == ("ai_first_with_review", rule)


def test_dp04_splits_qa_execution_and_regression():
    for act in ("QA.test_execution", "QA.regression_defects"):
        res = apply_rules(PLAIN, act, "human_led", "x")
        assert res["split"] and res["rule"] == "DP-04"


def test_dp05_only_for_standard_types_and_documented_integrations():
    for t in ("screen_crud", "report_dashboard", "admin_config", "notification"):
        for act in ("Backend.coding", "Backend.unit_tests", "Frontend.ui_implementation", "Frontend.component_tests"):
            res = apply_rules({**PLAIN, "type": t}, act, "human_led", "x")
            assert (res["category"], res["rule"]) == ("ai_first_with_review", "DP-05")
    assert _cat(PLAIN, "Backend.technical_design") == "ai_assisted"              # not listed: the LLM's stays
    assert _cat({**PLAIN, "type": "workflow"}, "Backend.coding") == "ai_assisted"
    assert _cat({**PLAIN, "route": "novel"}, "Backend.coding") == "ai_assisted"
    undocumented = {**PLAIN, "type": "integration", "name": "Device logs", "description": "Import logs from devices"}
    assert _cat(undocumented, "Backend.coding") == "ai_assisted"
    documented = {**undocumented, "description": "Import logs through the vendor's REST API"}
    assert _cat(documented, "Backend.coding") == "ai_first_with_review"


def test_applied_keeps_the_llm_suggestion_and_defaults_missing_activities():
    units = [{**PLAIN, "roles": ["BA"]}]
    act_cfg = {"activities": {"BA": {"stories_acceptance": {}, "wireframes_prototypes": {}}}}
    out = _applied(units, act_cfg, {"F1|BA.stories_acceptance": {"category": "human_led", "reason": "r", "factors": []}})
    assert out["F1|BA.stories_acceptance"]["llm_suggestion"] == "human_led"
    assert out["F1|BA.stories_acceptance"]["category"] == "ai_first_with_review"
    assert out["F1|BA.wireframes_prototypes"]["llm_suggestion"] is None
    assert out["F1|BA.wireframes_prototypes"]["source"] == "default+rules"

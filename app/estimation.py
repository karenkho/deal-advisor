"""Estimation core (POC v2): record + AI suitability + assumptions + active rates -> activities, estimates, timeline
and costing for the historical baseline (reference), B and C. Pure Python, no LLM; re-runs instantly on edits."""
from __future__ import annotations

import copy

from app import calculator as calc
from app import costing, deal, effort, risks, schedule, suitability
from app.config import estimation_config, load_yaml
from app.record import OpportunityRecord

# assumption_overrides keys -> (config file, path); edited in the UI, stored in the record, merged on top of config
OVERRIDABLE = {
    "productivity_profile": ("ai_scenarios", ["active_profile"]),
    "factors": ("ai_scenarios", ["factors"]),
    "integration_overhead": ("ai_scenarios", ["integration_overhead"]),
    "automatable_share": ("delivery_practices", ["automatable_share"]),
    "tooling_profile": ("tooling", ["active_profile"]),
    "ai_engineer_coding_share": ("schedule", ["ai_engineer", "coding_share"]),
    "team_sizing_mode": ("schedule", ["team_sizing", "mode"]),
    "target_window": ("schedule", ["team_sizing", "target_window"]),
    "target_window_custom_months": ("schedule", ["team_sizing", "custom_months"]),
    "uat_weeks": ("schedule", ["uat_weeks"]),
    "pilot_weeks": ("schedule", ["pilot_weeks"]),
    "support_fte": ("schedule", ["support_fte"]),
    "start_date": ("schedule", ["default_start_date"]),
    "target_gross_margin": ("commercial", ["target_gross_margin"]),
    "min_gross_margin": ("commercial", ["min_gross_margin"]),
    "ai_first_validated": ("commercial", ["ai_first_validated"]),
    "show_ai_engineer_in_proposal": ("commercial", ["show_ai_engineer_in_proposal"]),
    "warranty_months": ("commercial", ["warranty", "default_months"]),
    "warranty_fte": ("commercial", ["warranty", "fte"]),
    "contingency_rate": ("commercial", ["contingency_rate"]),
    "payment_milestones": ("commercial", ["payment_milestones"]),
    "retention_enabled": ("commercial", ["retention", "enabled"]),
}


def configs(overrides: dict | None = None) -> dict[str, dict]:
    """Config files + overrides. An override of None means "not overridden". Factors edited in the UI replace the
    active profile's; the active tooling profile adds its quality adjustment."""
    cfg = {"ai_scenarios": copy.deepcopy(load_yaml("ai_scenarios.yaml")),
           "schedule": copy.deepcopy(load_yaml("schedule.yaml")),
           "commercial": copy.deepcopy(load_yaml("commercial.yaml")),
           "tooling": copy.deepcopy(load_yaml("tooling_profiles.yaml")),
           "delivery_practices": copy.deepcopy(load_yaml("delivery_practices.yaml"))}
    for key, value in (overrides or {}).items():
        if key not in OVERRIDABLE or value is None:
            continue
        name, path = OVERRIDABLE[key]
        node = cfg[name]
        for p in path[:-1]:
            node = node[p]
        node[path[-1]] = value
    low, high = cfg["schedule"]["uat_weeks_range"]
    if not low <= cfg["schedule"]["uat_weeks"] <= high:
        raise ValueError(f"Final UAT must be {low}-{high} weeks.")
    edited = "factors" in cfg["ai_scenarios"] or "integration_overhead" in cfg["ai_scenarios"]
    cfg["ai_scenarios"] = effort.resolve_profile(cfg["ai_scenarios"])
    cfg["ai_scenarios"]["edited"] = edited
    cfg["active_tooling"] = costing.with_usage(cfg["tooling"]["profiles"][cfg["tooling"]["active_profile"]], cfg["tooling"])
    cfg["ai_scenarios"]["quality_adjustment"] = cfg["active_tooling"].get("quality_adjustment", {})
    return cfg


def _with_overhead(hours: dict[str, float], team: dict, months: float, sched_cfg: dict, scenario: str,
                   setup_hours: float = 0.0) -> dict[str, float]:
    """Development-window activity hours + PM/TA overhead + UAT/pilot support team + one-time tooling setup + the AI
    delivery pipeline setup (B and C only)."""
    out = {**schedule.overhead_hours(team, months, sched_cfg), **hours}
    for r, h in schedule.support_hours(scenario, sched_cfg).items():
        out[r] = out.get(r, 0.0) + h
    out["DevOps"] = out.get("DevOps", 0.0) + setup_hours
    if scenario != "baseline":
        out[effort.AI_ENGINEER] = out.get(effort.AI_ENGINEER, 0.0) + sched_cfg["ai_engineer"]["pipeline_setup_hours"]
    return out


def _shares(rows: list[dict]) -> dict[str, float]:
    total = sum(r["baseline"] for r in rows)
    return {c: (sum(r["baseline"] for r in rows if r["category"] == c) / total * 100 if total else 0.0)
            for c in effort.CATEGORIES}


def _reduction(base: float, new: float) -> float:
    return (base - new) / base * 100 if base else 0.0


def _timelines_and_hours(rows, size, sized, sched_cfg, est_cfg, setup_hours) -> tuple[dict, dict]:
    """Timeline and role hours for the baseline (template team, pooled DevOps), the active B and C teams, and the
    same-team view of B and C."""
    start = sched_cfg["default_start_date"]
    ta_new = est_cfg["estimation"]["ta_coding_share"]
    template = schedule.template_team(size, est_cfg, sched_cfg)
    base_team = {**template, "DevOps": 0.0}
    timelines = {"baseline": schedule.generate_project_schedule(rows, "baseline", base_team, size,
                                                                est_cfg["calibration"]["ta_coding_share"],
                                                                pool_devops=True, sched_cfg=sched_cfg, start=start)}
    for s in ("B", "C"):
        fixed = schedule.with_ai_engineer(rows, s, template, size, ta_new, sched_cfg, start)
        timelines[f"{s}_same_team"] = fixed
        timelines[s] = (schedule.generate_project_schedule(rows, s, sized[s]["team"], size, ta_new, sched_cfg=sched_cfg, start=start,
                                                           ai_coding_fte=sized[s]["ai_coding_fte"]) if s in sized else fixed)
    by_role = {}
    for key, tl in timelines.items():
        scenario = key[0] if key != "baseline" else key
        by_role[key] = _with_overhead(effort.role_hours(rows, scenario), tl["team"], tl["total_months"], sched_cfg,
                                      scenario, 0.0 if key == "baseline" else setup_hours)
    return timelines, by_role


def _breakdown(rows: list[dict], scenario: str, timeline: dict, sched_cfg: dict, setup_hours: float) -> dict[str, float]:
    """Where a scenario's hours remain: feature work by category, foundation, UAT & pilot support, PM/TA overhead and
    any one-time tooling setup. The buckets sum to the scenario's total hours."""
    out = effort.bucket_hours(rows, scenario)
    out["support"] = sum(schedule.support_hours(scenario, sched_cfg).values())
    out["pm_ta_overhead"] = sum(schedule.overhead_hours(timeline["team"], timeline["total_months"], sched_cfg).values())
    out["tooling_setup"] = 0.0 if scenario == "baseline" else setup_hours
    out["ai_pipeline_setup"] = 0.0 if scenario == "baseline" else sched_cfg["ai_engineer"]["pipeline_setup_hours"]
    return out


def _break_even_rework(rec: OpportunityRecord, target: dict, timeline: dict, blended: dict, est_cfg: dict) -> dict:
    """For each cheaper tooling profile: the rework penalty at which C (Expected) costs the same as commercial, review
    penalty as configured, team and duration held at the commercial result. Cost is linear in the penalty there, so two
    evaluations solve it exactly. None: it costs more even with no rework penalty."""
    base = {"productivity_profile": "expected", "factors": None, "integration_overhead": None}
    out = {}
    for name in load_yaml("tooling_profiles.yaml")["profiles"]:
        if name == "commercial_assistants":
            continue
        cfg = configs({**rec.assumption_overrides, **base, "tooling_profile": name})
        tooling = cfg["active_tooling"]
        quality = cfg["ai_scenarios"]["quality_adjustment"]

        def cost(rework: float) -> float:
            sc = {**cfg["ai_scenarios"], "quality_adjustment": {**quality, "rework": rework}}
            rows = effort.calculate_effort(rec.features, rec.opportunity.domain_complexity, rec.ai_suitability,
                                           est_cfg=est_cfg, sc_cfg=sc,
                                           automatable_share=cfg["delivery_practices"]["automatable_share"])["rows"]
            hours = _with_overhead(effort.role_hours(rows, "C"), timeline["team"], timeline["total_months"],
                                   cfg["schedule"], "C", float(tooling.get("setup_devops_hours", 0)))
            return costing.calculate_cost_and_margin(hours, timeline, blended, effort.ai_hours(rows, "C"), "C",
                                                     cfg["commercial"], tooling)["delivery_cost"]

        at_zero, at_one = cost(0.0), cost(1.0)
        point = (target["delivery_cost"] - at_zero) / (at_one - at_zero)
        out[name] = {"assumed_rework": quality.get("rework", 0.0), "break_even_rework": point if point >= 0 else None}
    return out


def _what_if(rec: OpportunityRecord, rates, discount_pct, **overrides) -> tuple[OpportunityRecord, dict]:
    probe = rec.model_copy(update={"assumption_overrides": {**rec.assumption_overrides, **overrides}})
    return run_estimation(probe, rates, discount_pct, with_extras=False)


def run_estimation(rec: OpportunityRecord, rates: costing.RateTable,
                   discount_pct: float | None = None, with_extras: bool = True) -> tuple[OpportunityRecord, dict]:
    """Returns (updated record copy, full costing incl. per-role labor for the internal UI only).
    discount_pct is session-only (Admin: Rates); it is not stored in the record. with_extras adds the what-if
    comparisons (upper bound, per productivity profile, per tooling profile)."""
    est_cfg = estimation_config()
    cfg = configs(rec.assumption_overrides)
    sc_cfg, sched_cfg, com_cfg = cfg["ai_scenarios"], cfg["schedule"], cfg["commercial"]
    tooling = cfg["active_tooling"]
    domain = rec.opportunity.domain_complexity
    # suggested scope (critic-added, not yet accepted by a BA) stays out of the estimate and price; its hours show separately
    features = [f for f in rec.features if f.scope_status != "suggested"]
    suggested = [f for f in rec.features if f.scope_status == "suggested"]
    eff = effort.calculate_effort(features, domain, rec.ai_suitability, est_cfg=est_cfg, sc_cfg=sc_cfg,
                                  automatable_share=cfg["delivery_practices"]["automatable_share"],
                                  hour_overrides=rec.assumption_overrides.get("activity_hours"))
    rows = eff["rows"]
    v1 = calc.estimate([f.size for f in features if f.route in ("clear", "novel", None)], est_cfg, complexity=domain)
    sizing = schedule.classify_size(rows, est_cfg, sched_cfg)
    size = sizing["size"]
    setup_hours = float(tooling.get("setup_devops_hours", 0))
    # the baseline window is what the template team produces; B and C are sized to the target window
    timelines, by_role = _timelines_and_hours(rows, size, {}, sched_cfg, est_cfg, setup_hours)
    mode = sched_cfg["team_sizing"]["mode"]
    window = schedule.resolve_window(timelines["baseline"]["core_months"],
                                     rec.opportunity.commercial_terms.get("requested_months"), sched_cfg)
    added = {}
    if mode == "target_duration":
        ta_new = est_cfg["estimation"]["ta_coding_share"]
        sized = {s: schedule.size_team(rows, s, window["months"], size, ta_new, est_cfg, sched_cfg,
                                       sched_cfg["default_start_date"]) for s in ("B", "C")}
        added = {s: v["added"] for s, v in sized.items()}
        for s, roles in (rec.assumption_overrides.get("team_fte") or {}).items():    # a person's team, per scenario
            sized[s] = {**sized[s], "team": {**sized[s]["team"], **roles}}
        timelines, by_role = _timelines_and_hours(rows, size, sized,
                                                  sched_cfg, est_cfg, setup_hours)
    totals = {s: sum(h.values()) for s, h in by_role.items()}
    heads = {s: schedule.headcount(timelines[s]["team"]) for s in ("baseline", "B", "C")}
    ba_warnings = {s: w for s in ("B", "C") if (w := calc.ba_warning(heads[s]["BA"], est_cfg))}
    breakdown = {s: _breakdown(rows, s, timelines[s], sched_cfg, setup_hours) for s in ("baseline", "B", "C")}
    not_reducible = breakdown["C"]["support"] + breakdown["C"]["pm_ta_overhead"]
    estimates = {
        "project_size": size, "size_checks": sizing["checks"], "domain_complexity": domain,
        "v1_dev_phase_months": v1.scenarios[0].duration_months,
        "support_hours": {s: schedule.support_hours(s, sched_cfg) for s in ("baseline", "B", "C")},
        "breakdown": breakdown,
        "not_reducible_by_ai": {"hours": not_reducible, "share_of_c_pct": not_reducible / totals["C"] * 100},
        "productivity_profile": sc_cfg["active_profile"], "productivity_edited": sc_cfg["edited"],
        "tooling_profile": cfg["tooling"]["active_profile"],
        "data_residency_risk": bool(tooling.get("data_residency_risk")),
        "team_sizing": {"mode": mode, "window": window, "added_steps": added},
        "headcount": heads, "ba_warnings": ba_warnings,
        "ai_engineer": {s: schedule.size_ai_engineer(rows, s, timelines[s]["core_months"], sched_cfg) for s in ("B", "C")},
        "scenarios": {s: {"by_role": by_role[s], "total_hours": totals[s], "months": timelines[s]["total_months"],
                          "development_window_months": timelines[s]["core_months"], "team": timelines[s]["team"]}
                      for s in by_role},
        "reduction_vs_baseline_pct": {s: _reduction(totals["baseline"], totals[s])
                                      for s in ("B", "C", "B_same_team", "C_same_team")},
        "reduction_c_vs_b_pct": _reduction(totals["B"], totals["C"]),
        "reduction_by_role_pct": {s: {r: _reduction(by_role["baseline"][r], by_role[s][r]) for r in by_role[s]}
                                  for s in ("B", "C")},
        "by_feature": eff["by_feature"], "by_epic": eff["by_epic"],
        "hypothesis_reduction_c_pct": sc_cfg["hypothesis_reduction_c"] * 100,
        "category_share_of_baseline_pct": _shares(rows),
        "ai_first_share_of_baseline_pct": _shares(rows)["ai_first_with_review"],
    }
    discount = com_cfg["discount_pct"] if discount_pct is None else discount_pct
    blended = costing.blended_rates(rates, discount)
    terms = rec.opportunity.commercial_terms
    # the TOR's warranty period applies unless the user edits it
    months = rec.assumption_overrides.get("warranty_months") or terms.get("warranty_months") or com_cfg["warranty"]["default_months"]
    com_cfg["retention"]["warranty_months"] = months
    warranty = deal.warranty(months, com_cfg, sched_cfg)
    full = {s: costing.calculate_cost_and_margin(by_role[s], timelines[s], blended, effort.ai_hours(rows, s), s,
                                                 com_cfg, tooling, warranty["by_role"])
            for s in ("baseline", "B", "C")}
    for s, net in costing.net_savings(full).items():
        full[s].update(net)
    cash = {}
    for s in ("B", "C"):
        burn = deal.monthly_burn(rows, s, timelines[s], full[s], by_role[s], blended, sched_cfg, warranty)
        price = full[s]["price_ex_vat"]
        cash[s] = {"default": deal.cash_position(burn, deal.milestone_receipts(com_cfg["payment_milestones"], timelines[s]),
                                                 price)}
        tor = deal.tor_receipts(terms, timelines[s])
        if tor:
            cash[s]["tor"] = deal.cash_position(burn, tor, price)
    estimates["warranty"] = warranty
    not_priced = effort.calculate_effort(suggested, domain, rec.ai_suitability, est_cfg=est_cfg, sc_cfg=sc_cfg, with_foundation=False,
                                         automatable_share=cfg["delivery_practices"]["automatable_share"])["by_feature"]
    estimates["suggested_scope"] = [{"feature_id": f.id, "name": f.name, "size": f.size, "reason": f.justification,
                                     "baseline": not_priced.get(f.id, {}).get("baseline", 0.0), "B": not_priced.get(f.id, {}).get("B", 0.0),
                                     "C": not_priced.get(f.id, {}).get("C", 0.0)} for f in suggested]
    estimates["cash_flow"] = cash
    if with_extras:
        # C if every activity were at the most AI-intensive strategy the rules allow
        probe = rec.model_copy(update={"ai_suitability": suitability.upper_bound(rec.ai_suitability, features, est_cfg)})
        bound, _ = run_estimation(probe, rates, discount_pct, with_extras=False)
        estimates["c_upper_bound_pct"] = bound.estimates["reduction_vs_baseline_pct"]["C"]
        by_profile, by_range = {}, {}
        for name in sc_cfg["profiles"]:
            p, pv = _what_if(rec, rates, discount_pct, productivity_profile=name, factors=None,
                             integration_overhead=None)
            by_profile[name] = {"B": p.estimates["reduction_vs_baseline_pct"]["B"],
                                "C": p.estimates["reduction_vs_baseline_pct"]["C"],
                                "C_vs_B": p.estimates["reduction_c_vs_b_pct"]}
            by_range[name] = {s: {"hours": pv["costs"][s]["hours"], "months": pv["costs"][s]["months"],
                                  "delivery_cost": pv["costs"][s]["delivery_cost"],
                                  "price_ex_vat": pv["costs"][s]["price_ex_vat"]} for s in ("B", "C")}
        estimates["verdict_by_profile"] = by_profile
        # internal only: low / expected / high effort = optimistic / expected / conservative
        estimates["estimate_range"] = {s: {"low": by_range["optimistic"][s], "expected": by_range["expected"][s],
                                           "high": by_range["conservative"][s]} for s in ("B", "C")}
        by_tooling = {}
        for name in cfg["tooling"]["profiles"]:
            p, v = _what_if(rec, rates, discount_pct, productivity_profile="expected", factors=None,
                            integration_overhead=None, tooling_profile=name)
            c = v["costs"]["C"]
            by_tooling[name] = {"ai_tooling": c["ai_tooling"], "net_savings": c["net_savings"], "hours": c["hours"], "delivery_cost": c["delivery_cost"],
                                "price_ex_vat": c["price_ex_vat"], "months": c["months"]}
        estimates["tooling_comparison_c_expected"] = by_tooling
        requested = terms.get("requested_months")
        if requested:     # what meets the requested duration: both scenarios sized to the client-requested window
            m, mv = _what_if(rec, rates, discount_pct, team_sizing_mode="target_duration", target_window="client_requested")
            estimates["meets_requested_duration"] = {
                "requested_months": requested, "window_months": m.estimates["team_sizing"]["window"]["months"],
                "fallback_note": m.estimates["team_sizing"]["window"]["note"],
                "scenarios": {s: {"team": m.timeline[s]["team"], "developers": m.estimates["headcount"][s]["developers"],
                                  "total_fte": m.estimates["headcount"][s]["total"], "months": mv["costs"][s]["months"],
                                  "price_ex_vat": mv["costs"][s]["price_ex_vat"]} for s in ("B", "C")}}
        commercial, commercial_view = _what_if(rec, rates, discount_pct, productivity_profile="expected", factors=None,
                                               integration_overhead=None, tooling_profile="commercial_assistants")
        estimates["tooling_break_even_rework"] = _break_even_rework(rec, commercial_view["costs"]["C"],
                                                                    commercial.timeline["C"], blended, est_cfg)
    new = rec.model_copy(deep=True)
    new.activities = rows
    new.estimates = estimates
    new.timeline = timelines
    new.costing.rates_source = rates.source
    new.costing.rates_hash = rates.hash + (f"-d{discount:g}" if discount else "")
    new.costing.scenarios = {s: costing.record_safe(c) for s, c in full.items()}
    if with_extras:
        new.risks = risks.check_risk_rules(new, rates.source)
        new.recommendation = risks.recommendation_for(new)
    for br in new.brd.requirements:     # traceability: BR -> estimation activities
        br.activity_keys = list(dict.fromkeys(f"{r['feature_id']}|{r['activity']}" for r in rows
                                              if r["feature_id"] == br.feature_id))
    return new, {"costs": full, "blended_rates": blended}

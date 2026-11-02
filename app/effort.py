"""calculate_effort (POC v2): activity-level effort for the historical baseline (reference), B (AI-Augmented)
and C (AI-First). Pure Python, no LLM; re-runs instantly on assumption edits.

Per activity: baseline (calibrated tier hours x domain multiplier x activity share x delivery overhead), then for B
and C: AI production, orchestration, review, rework, integration and the scenario total. Overhead components apply
only to AI-produced output (never to human_led activities), and each activity's review component is computed on
that activity's own baseline, so the separate code-review activity is never counted twice.
"""
from __future__ import annotations

from app import tracing
from app.config import estimation_config, load_yaml

FEATURE_ROLES_V2 = ["BA", "Backend", "Frontend", "QA", "DevOps"]
OVERHEAD_ROLES_V2 = ["PM", "TA"]
AI_ENGINEER = "AI Engineer"
ALL_ROLES_V2 = ["PM", "TA", "BA", "Backend", "Frontend", "QA", "DevOps", AI_ENGINEER]
AI_FEATURE_TYPE = "ai_ml_feature"     # its Backend-tier work is owned by the AI Engineer
COMPONENTS = ["production", "orchestration", "review", "rework", "integration"]
CATEGORIES = ["human_led", "ai_assisted", "ai_first_with_review", "deterministic_automated"]


def activities_config() -> dict:
    return load_yaml("activities.yaml")


def resolve_profile(sc_cfg: dict) -> dict:
    """The active productivity profile's factors and integration overhead at the top level of the config."""
    profile = sc_cfg["profiles"][sc_cfg["active_profile"]]
    return {**sc_cfg, "factors": sc_cfg.get("factors", profile["factors"]),
            "integration_overhead": sc_cfg.get("integration_overhead", profile["integration_overhead"])}


def scenarios_config() -> dict:
    return resolve_profile(load_yaml("ai_scenarios.yaml"))


def practices_config() -> dict:
    return load_yaml("delivery_practices.yaml")


def foundation_features(est_cfg: dict, act_cfg: dict) -> list[dict]:
    """Foundation items as pseudo-features with v2 role hours (no domain multiplier, as in v1)."""
    devops_items = set(act_cfg["devops_foundation_items"])
    out = []
    for key, hours in est_cfg["foundation"]["items"].items():
        roles = {}
        for v1_role, h in hours.items():
            role = "DevOps" if (v1_role == "Dev" and key in devops_items) else act_cfg["role_map"][v1_role]
            roles[role] = roles.get(role, 0.0) + float(h)
        out.append({"id": f"FND-{key}", "epic": "Foundation", "name": key.replace("_", " ").capitalize(),
                    "complexity": "high" if key == "identity_auth_service" else "medium", "role_hours": roles})
    return out


def feature_role_hours(size: str, domain: str, est_cfg: dict, act_cfg: dict) -> dict[str, float]:
    mult = est_cfg["complexity_multipliers"][domain]
    return {act_cfg["role_map"][r]: float(est_cfg["size_hours"][size][r]) * mult[r] for r in act_cfg["role_map"]}


def scenario_components(category: str, scenario: str, baseline: float, sc_cfg: dict) -> dict[str, float]:
    """Hours per component for one activity. In B, ai_first_with_review is treated as ai_assisted."""
    if scenario == "B" and category == "ai_first_with_review":
        category = "ai_assisted"
    f = sc_cfg["factors"][category][scenario]
    out = {k: baseline * f[k] for k in ("production", "orchestration", "review", "rework")}
    if category in ("ai_assisted", "ai_first_with_review"):    # cheaper tooling: more review and rework
        for k, v in (sc_cfg.get("quality_adjustment") or {}).items():
            out[k] += baseline * v
    out["integration"] = 0.0 if category == "human_led" else baseline * sc_cfg["integration_overhead"]
    out["total"] = sum(out[k] for k in COMPONENTS)
    return out


def _reduction(base: float, new: float) -> float | None:
    return None if base <= 0 else (base - new) / base * 100


def _parts(cat: dict, base: float, share: float) -> list[tuple[str, str, float, str]]:
    """(C category, B category, baseline hours, label suffix). DP-04 splits an activity into an automated share and a
    manual remainder in C; the whole activity is ai_assisted in B."""
    if cat.get("split"):
        return [("deterministic_automated", "ai_assisted", base * share, " (automated share)"),
                ("ai_assisted", "ai_assisted", base * (1 - share), " (manual remainder)")]
    category = cat.get("category", "ai_assisted")
    return [(category, category, base, "")]


@tracing.traced("calculate_effort", lambda features, domain, *a, **k: {"features": len(features), "domain": domain}, lambda r: {"keys": sorted(r)}, limit=6)
def calculate_effort(features: list, domain: str, categories: dict[str, dict], est_cfg: dict | None = None,
                     act_cfg: dict | None = None, sc_cfg: dict | None = None,
                     automatable_share: float | None = None, with_foundation: bool = True, hour_overrides: dict | None = None) -> dict:
    """features: objects with id, epic, name, size, route, complexity. categories: {"<feature_id>|<Role>.<activity>":
    {"category", "reason", "factors", "llm_suggestion", "rule", "split"}}; missing entries default to ai_assisted.
    hour_overrides: {"<feature_id>|<Role>.<activity>": baseline hours}, a person's correction; AI factors still apply.
    Returns activity rows and aggregates (feature-driven roles only; PM/TA come from the schedule)."""
    if automatable_share is None:
        automatable_share = practices_config()["automatable_share"]
    est_cfg = est_cfg or estimation_config()
    act_cfg = act_cfg or activities_config()
    sc_cfg = sc_cfg or scenarios_config()
    overhead = act_cfg["delivery_overhead"]
    units = [{"id": f.id, "epic": f.epic, "name": f.name, "complexity": f.complexity,
              "owner": AI_ENGINEER if getattr(f, "type", "") == AI_FEATURE_TYPE else None,
              "role_hours": feature_role_hours(f.size, domain, est_cfg, act_cfg)}
             for f in features if f.size and f.route in ("clear", "novel", None)]
    if with_foundation:
        units += foundation_features(est_cfg, act_cfg)
    rows = []
    for u in units:
        for role, hours in u["role_hours"].items():
            if hours <= 0:
                continue
            for key, a in act_cfg["activities"][role].items():
                akey = f"{role}.{key}"
                base = (hour_overrides or {}).get(f"{u['id']}|{akey}", hours * a["share"] * overhead)
                cat = categories.get(f"{u['id']}|{akey}", {})
                for category, category_b, part, suffix in _parts(cat, base, automatable_share):
                    row = {"feature_id": u["id"], "feature": u["name"], "epic": u["epic"],
                           "complexity": u["complexity"], "role": u.get("owner") if role == "Backend" and u.get("owner") else role,
                           "activity": akey,
                           "activity_label": a["label"] + suffix, "category": category, "category_b": category_b,
                           "llm_suggestion": cat.get("llm_suggestion"), "rule": cat.get("rule"),
                           "reason": cat.get("reason", "Default: not assessed."), "factors": cat.get("factors", []),
                           "baseline": part}
                    row["B"] = scenario_components(category_b, "B", part, sc_cfg)
                    row["C"] = scenario_components(category, "C", part, sc_cfg)
                    row["reduction_b"] = _reduction(part, row["B"]["total"])
                    row["reduction_c"] = _reduction(part, row["C"]["total"])
                    row["reduction_c_vs_b"] = _reduction(row["B"]["total"], row["C"]["total"])
                    rows.append(row)
    return {"rows": rows, "by_role": aggregate(rows, "role"), "by_feature": aggregate(rows, "feature_id"),
            "by_epic": aggregate(rows, "epic"), "totals": aggregate(rows, None).get("all", {})}


def aggregate(rows: list[dict], key: str | None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows:
        k = "all" if key is None else r[key]
        agg = out.setdefault(k, {"baseline": 0.0, "B": 0.0, "C": 0.0})
        agg["baseline"] += r["baseline"]
        agg["B"] += r["B"]["total"]
        agg["C"] += r["C"]["total"]
    for agg in out.values():
        agg["reduction_b"] = _reduction(agg["baseline"], agg["B"])
        agg["reduction_c"] = _reduction(agg["baseline"], agg["C"])
        agg["reduction_c_vs_b"] = _reduction(agg["B"], agg["C"])
    return out


def row_role_hours(row: dict, scenario: str) -> dict[str, float]:
    """Who works an activity's hours. In B and C the orchestration of AI-produced work is the AI Engineer's."""
    if scenario == "baseline":
        return {row["role"]: row["baseline"]}
    parts = row[scenario]
    moved = parts.get("orchestration", 0.0) if scenario_category(row, scenario) in ("ai_assisted", "ai_first_with_review") else 0.0
    out = {row["role"]: parts["total"] - moved}
    if moved:
        out[AI_ENGINEER] = out.get(AI_ENGINEER, 0.0) + moved
    return out


def role_hours(rows: list[dict], scenario: str) -> dict[str, float]:
    """Feature-driven role hours for 'baseline', 'B' or 'C'."""
    out = dict.fromkeys(FEATURE_ROLES_V2 + [AI_ENGINEER], 0.0)
    for r in rows:
        for role, hours in row_role_hours(r, scenario).items():
            out[role] += hours
    return out


def scenario_category(row: dict, scenario: str) -> str:
    """The category an activity row works under in a scenario. In B, AI-first activities are treated as AI-assisted."""
    if scenario != "B":
        return row.get("category", "")
    category = row.get("category_b", "")
    return "ai_assisted" if category == "ai_first_with_review" else category


def ai_hours(rows: list[dict], scenario: str) -> dict[str, float]:
    """Hours of AI-first and AI-assisted work in a scenario: the basis of usage-priced AI tooling."""
    if scenario == "baseline":
        return {"ai_first": 0.0, "ai_assisted": 0.0}
    out = {"ai_first": 0.0, "ai_assisted": 0.0}
    for r in rows:
        category = scenario_category(r, scenario)
        if category == "ai_first_with_review":
            out["ai_first"] += r[scenario]["total"]
        elif category == "ai_assisted":
            out["ai_assisted"] += r[scenario]["total"]
    return out


def bucket_hours(rows: list[dict], scenario: str) -> dict[str, float]:
    """Activity hours of a scenario: foundation items as one bucket, feature work by category."""
    out = dict.fromkeys(CATEGORIES, 0.0) | {"foundation": 0.0}
    for r in rows:
        hours = r["baseline"] if scenario == "baseline" else r[scenario]["total"]
        out["foundation" if r["feature_id"].startswith("FND-") else scenario_category(r, scenario)] += hours
    return out

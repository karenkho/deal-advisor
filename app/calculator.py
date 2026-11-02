"""Deterministic effort calculator. Pure Python, no LLM.

Flow: classify project size from baseline Dev hours -> pick the traditional team template ->
per-role hours, mandays and timeline -> duration = longest feature-driven role (bottleneck) ->
PM/TA hours = FTE x duration x hours_per_person_month. Three scenarios for the same scope:
traditional, AI-enabled with the same team, AI-enabled with the same deadline.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable

FEATURE_ROLES = ["BA", "Dev", "UI", "QA"]
OVERHEAD_ROLES = ["PM", "TA"]
ALL_ROLES = ["PM", "TA", "BA", "Dev", "UI", "QA"]
TIERS = ["S", "M", "L", "XL"]
SIZE_ORDER = ["Small", "Mid", "Large", "XL"]

DEV_PHASE_ASSUMPTION = "Development phase only: UAT support and warranty not included."


@dataclass
class RoleLine:
    role: str
    hours: float          # buffered hours (contingency mode) or raw hours (utilization mode)
    mandays: float
    fte: float
    months: float | None  # None for overhead roles (they follow the project duration)
    bottleneck: bool = False


@dataclass
class Scenario:
    key: str
    title: str
    duration_months: float
    bottleneck: str
    lines: dict[str, RoleLine]
    warnings: list[str] = field(default_factory=list)

    @property
    def total_hours(self) -> float:
        return sum(line.hours for line in self.lines.values())


@dataclass
class EstimateResult:
    project_size: str
    baseline_dev_hours: float
    feature_count: int
    unsized_count: int
    scenarios: list[Scenario]
    uat_support: dict[str, float] | None
    assumptions: list[str]
    complexity: str = "standard"


# ---------- building blocks ----------

def feature_hours(sizes: Iterable[str | None], cfg: dict, complexity: str = "standard") -> dict[str, float]:
    """Sum baseline (pre-AI) hours per feature-driven role, times the domain complexity multiplier.
    Unsized (None) features are skipped."""
    mult = cfg.get("complexity_multipliers", {}).get(complexity)
    if mult is None:
        raise ValueError(f"unknown domain complexity: {complexity}")
    totals = dict.fromkeys(FEATURE_ROLES, 0.0)
    for size in sizes:
        if size is None:
            continue
        tier = cfg["size_hours"][size]
        for r in FEATURE_ROLES:
            totals[r] += tier[r] * mult[r]
    return totals


def foundation_hours(cfg: dict) -> dict[str, float]:
    totals = dict.fromkeys(FEATURE_ROLES, 0.0)
    for item in cfg["foundation"]["items"].values():
        for r in FEATURE_ROLES:
            totals[r] += item.get(r, 0)
    return totals


def apply_ai(hours: dict[str, float], cfg: dict) -> dict[str, float]:
    return {r: h * cfg["ai_factors"][r] for r, h in hours.items()}


def buffered(hours: float, cfg: dict) -> float:
    """Hours reported to the user. Contingency mode adds the buffer; utilization mode does not."""
    b = cfg["buffer"]
    return hours * b["contingency"] if b["mode"] == "contingency" else hours


def effective_month_hours(cfg: dict) -> float:
    """Productive hours one FTE delivers per month against RAW hours (exactly one buffer)."""
    b = cfg["buffer"]
    hpm = cfg["capacity"]["hours_per_person_month"]
    if b["mode"] == "contingency":
        return hpm / b["contingency"]
    if b["mode"] == "utilization":
        return hpm * b["utilization"]
    raise ValueError(f"unknown buffer mode: {b['mode']}")


def months_for(raw_hours: float, fte: float, cfg: dict) -> float:
    if raw_hours <= 0:
        return 0.0
    if fte <= 0:
        return math.inf
    return raw_hours / (fte * effective_month_hours(cfg))


def round_up_fte(fte: float, step: float) -> float:
    return math.ceil(round(fte / step, 9)) * step


def dev_fte_effective(team: dict, ta_share: float) -> float:
    return team["Dev"] + team["TA"] * ta_share


def classify_project_size(baseline_dev_hours: float, cfg: dict) -> str:
    th = cfg["project_size_thresholds"]
    for size in ["Small", "Mid", "Large"]:
        if baseline_dev_hours < th[size]:
            return size
    return "XL"


# ---------- scenarios ----------

def _scenario(key: str, title: str, raw: dict[str, float], team: dict, cfg: dict,
              ta_share: float, duration_override: float | None = None) -> Scenario:
    hpd = cfg["capacity"]["hours_per_manday"]
    hpm = cfg["capacity"]["hours_per_person_month"]
    lines: dict[str, RoleLine] = {}
    months: dict[str, float] = {}
    for r in FEATURE_ROLES:
        fte = dev_fte_effective(team, ta_share) if r == "Dev" else team[r]
        months[r] = months_for(raw[r], fte, cfg)
        h = buffered(raw[r], cfg)
        lines[r] = RoleLine(r, h, h / hpd, team[r], months[r])
    bottleneck = max(FEATURE_ROLES, key=lambda r: months[r])
    duration = duration_override if duration_override is not None else months[bottleneck]
    lines[bottleneck].bottleneck = True
    for r in OVERHEAD_ROLES:
        h = team[r] * duration * hpm * cfg["ai_factors"][r]
        lines[r] = RoleLine(r, h, h / hpd, team[r], None)
    ordered = {r: lines[r] for r in ALL_ROLES}
    return Scenario(key, title, duration, bottleneck, ordered)


def ba_warning(ba_fte: float, cfg: dict) -> str | None:
    org = cfg["org_capacity"]
    limit = org["ba_available_department"] * org["ba_warn_share"]
    if ba_fte > limit:
        return (f"BA capacity: needs {ba_fte:g} BA FTE, above {org['ba_warn_share']:.0%} of the "
                f"{org['ba_available_department']} department BAs ({limit:.1f}).")
    return None


def estimate(sizes: list[str | None], cfg: dict, team: dict | None = None,
             complexity: str | None = None) -> EstimateResult:
    complexity = complexity or cfg["estimation"].get("default_domain_complexity", "standard")
    trad_share = cfg["calibration"]["ta_coding_share"]   # traditional = how past teams worked
    ai_share = cfg["estimation"]["ta_coding_share"]      # AI scenarios: TA reviews, does not code
    feat = feature_hours(sizes, cfg, complexity)
    found = foundation_hours(cfg)
    baseline = {r: feat[r] + found[r] for r in FEATURE_ROLES}
    size = classify_project_size(baseline["Dev"], cfg)
    template = dict(team or cfg["team_templates"][size])

    traditional = _scenario("traditional", "Traditional (pre-AI)", baseline, template, cfg, trad_share)

    ai_raw = apply_ai(feat, cfg)
    ai_found = apply_ai(found, cfg) if cfg["foundation"].get("apply_ai_factor", True) else found
    ai = {r: ai_raw[r] + ai_found[r] for r in FEATURE_ROLES}
    same_team = _scenario("ai_same_team", "AI-enabled, same team", ai, template, cfg, ai_share)

    # Same deadline: minimum FTE per feature-driven role to finish within the traditional duration.
    deadline = traditional.duration_months
    step = cfg["capacity"]["fte_rounding"]
    min_team = dict(template)
    for r in FEATURE_ROLES:
        need = ai[r] / (deadline * effective_month_hours(cfg)) if deadline > 0 else 0.0
        if r == "Dev":
            need = max(need - template["TA"] * ai_share, 0.0)
        min_team[r] = round_up_fte(need, step) if need > 0 else 0.0
    same_deadline = _scenario("ai_same_deadline", "AI-enabled, same deadline", ai, min_team, cfg,
                              ai_share, duration_override=deadline)

    for sc in (traditional, same_team, same_deadline):
        w = ba_warning(sc.lines["BA"].fte, cfg)
        if w:
            sc.warnings.append(w)

    uat = None
    if cfg.get("uat_support", {}).get("enabled"):
        u = cfg["uat_support"]
        uat = {r: u["fte"].get(r, 0) * u["weeks"] * u["hours_per_week"] for r in ALL_ROLES}

    assumptions = [
        DEV_PHASE_ASSUMPTION if not uat else "UAT support included as a separate line; warranty not included.",
        f"Buffer mode: {cfg['buffer']['mode']} (applied once).",
        f"Domain complexity: {complexity} (BA to confirm).",
        f"Project size {size} from {baseline['Dev']:.0f} baseline Dev hours; team template {size}.",
        f"TA coding share: traditional {trad_share:g}, AI-enabled {ai_share:g}.",
        "Sizes calibrated on senior-led teams; an average-seniority team may need more hours.",
    ]
    return EstimateResult(size, baseline["Dev"], len(sizes), sum(s is None for s in sizes),
                          [traditional, same_team, same_deadline], uat, assumptions, complexity)

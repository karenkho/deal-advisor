"""generate_project_schedule (POC v2): phase timeline per scenario. Pure Python, no LLM.

Agile overlap model (see config/schedule.yaml), never total effort / developers. The core window (requirements ->
testing) is the role-capacity bottleneck over all core work with every role working in parallel (the calibrated v1
rule), bounded below by the critical path requirements -> architecture -> SIT -> testing. Requirements, architecture,
SIT and testing last as long as the bottleneck role of their own hours; development fills the rest of the window.
The core window is the development window the P01/P02 calibration covers. UAT and a pilot run (fixed weeks,
client-bound, not reduced by AI, staffed by the support team) and deployment / go-live (fixed weeks) follow.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

from app import tracing
from app.config import load_yaml
from app.effort import AI_ENGINEER, FEATURE_ROLES_V2, row_role_hours

MAX_MONTHS = 120.0


def schedule_config() -> dict:
    return load_yaml("schedule.yaml")


def template_team(size: str, est_cfg: dict, sched_cfg: dict) -> dict[str, float]:
    """v1 team template remapped to v2 roles, plus DevOps. The AI Engineer is sized from hours (size_ai_engineer)."""
    t = est_cfg["team_templates"][size]
    return {"PM": t["PM"], "TA": t["TA"], "BA": t["BA"], "Backend": t["Dev"], "Frontend": t["UI"], "QA": t["QA"],
            "DevOps": sched_cfg["devops_fte"][size], AI_ENGINEER: 0.0}


def phase_role_hours(rows: list[dict], scenario: str, sched_cfg: dict) -> dict[str, dict[str, float]]:
    out = {p: dict.fromkeys(FEATURE_ROLES_V2 + [AI_ENGINEER], 0.0) for p in sched_cfg["phases"]}
    for r in rows:
        for role, hours in row_role_hours(r, scenario).items():
            for phase, share in sched_cfg["activity_phases"][r["activity"]].items():
                out[phase][role] += hours * share
    return out


def _capacity(team: dict, ta_share: float, pool_devops: bool, ai_coding_fte: float = 0.0) -> dict[str, float]:
    cap = {r: float(team.get(r, 0.0)) for r in FEATURE_ROLES_V2}
    cap["Backend"] += team.get("TA", 0.0) * ta_share + ai_coding_fte
    if pool_devops:
        cap["DevOps"] = 0.0
    return cap


@tracing.traced("generate_project_schedule", lambda rows, scenario, team, size, *a, **k: {"rows": len(rows), "scenario": scenario, "size": size}, lambda r: {"total_months": r.get("total_months")}, limit=6)
def generate_project_schedule(rows: list[dict], scenario: str, team: dict, size: str, ta_share: float,
                              pool_devops: bool = False, sched_cfg: dict | None = None,
                              start: str | None = None, ai_coding_fte: float = 0.0) -> dict:
    """scenario: 'baseline' | 'B' | 'C'. pool_devops: DevOps hours carried by Backend capacity (historical teams).
    ai_coding_fte: the AI Engineer's spare capacity that counts as Backend capacity."""
    cfg = sched_cfg or schedule_config()
    hpm = cfg["hours_per_fte_month"]
    hours = phase_role_hours(rows, scenario, cfg)
    cap = _capacity(team, ta_share, pool_devops, ai_coding_fte)
    def bottleneck(ph: dict[str, float]) -> tuple[float, str]:
        ph = dict(ph)
        ai_hours = ph.pop(AI_ENGINEER, 0.0)     # sized from its own hours, so it does not bound a phase
        if pool_devops:
            ph["Backend"] += ph.pop("DevOps") + ai_hours     # historical teams had no such role
        months = {r: (h / (cap[r] * hpm) if cap[r] > 0 else math.inf) for r, h in ph.items() if h > 0}
        if not months:
            return 0.0, "—"
        role = max(months, key=months.get)
        return min(months[role], MAX_MONTHS), role

    core_phases = ["requirements", "architecture", "development", "sit", "testing"]
    durations = {p: bottleneck(hours[p]) for p in core_phases if p != "development"}
    core_hours = {r: sum(hours[p][r] for p in core_phases) for r in FEATURE_ROLES_V2}
    core, core_role = bottleneck(core_hours)
    req = durations["requirements"][0]
    arch_start = req * cfg["architecture_overlap"]
    arch_end = max(req, arch_start + durations["architecture"][0])
    tail = durations["sit"][0] + durations["testing"][0]
    core = max(core, arch_end + tail)
    durations["development"] = (core - arch_end - tail, core_role)
    durations["uat"] = (cfg["uat_weeks"] / cfg["weeks_per_month"], "Client (fixed weeks), support team")
    durations["pilot"] = (cfg["pilot_weeks"] / cfg["weeks_per_month"], "Client (fixed weeks), support team")
    durations["deployment"] = (cfg["deployment_weeks"] / cfg["weeks_per_month"], "Fixed weeks")
    starts = {"requirements": 0.0, "architecture": arch_start}
    ends = {"requirements": req, "architecture": arch_start + durations["architecture"][0]}
    t = arch_end
    for phase in ("development", "sit", "testing", "uat", "pilot", "deployment"):
        starts[phase], t = t, t + durations[phase][0]
        ends[phase] = t
    start_date = date.fromisoformat(start or cfg["default_start_date"])
    phases = [{"phase": p, "label": cfg["labels"][p], "start_month": starts[p], "end_month": ends[p],
               "months": durations[p][0], "bottleneck": durations[p][1],
               "start_date": _add_months(start_date, starts[p]).isoformat(),
               "end_date": _add_months(start_date, ends[p]).isoformat()} for p in cfg["phases"]]
    return {"scenario": scenario, "team": dict(team), "total_months": t, "core_months": core, "phases": phases,
            "ai_coding_fte": ai_coding_fte,
            "start_date": start_date.isoformat(), "end_date": _add_months(start_date, t).isoformat()}


def _add_months(d: date, months: float) -> date:
    return d + timedelta(days=round(months * 30.44))


def support_months(sched_cfg: dict | None = None) -> float:
    cfg = sched_cfg or schedule_config()
    return (cfg["uat_weeks"] + cfg["pilot_weeks"]) / cfg["weeks_per_month"]


def support_hours(scenario: str, sched_cfg: dict | None = None) -> dict[str, float]:
    """UAT + pilot support team hours of a scenario: fixed weeks x its support FTE; productivity profiles don't change it."""
    cfg = sched_cfg or schedule_config()
    return {r: fte * support_months(cfg) * cfg["hours_per_fte_month"] for r, fte in cfg["support_fte"][scenario].items()}


def overhead_hours(team: dict, total_months: float, sched_cfg: dict | None = None) -> dict[str, float]:
    """PM and TA at template FTE for every phase except UAT and pilot (covered by support_fte)."""
    cfg = sched_cfg or schedule_config()
    months = max(total_months - support_months(cfg), 0.0)
    return {r: team.get(r, 0.0) * months * cfg["hours_per_fte_month"] for r in ("PM", "TA")}


def classify_size(rows: list[dict], est_cfg: dict, sched_cfg: dict | None = None) -> dict:
    """Smallest team template whose team completes the development window (historical baseline) within the
    template's months x (1 + size_tolerance); XL if none does."""
    cfg = sched_cfg or schedule_config()
    checks = []
    for size in ("Small", "Mid", "Large", "XL"):
        team = {**template_team(size, est_cfg, cfg), "DevOps": 0.0, AI_ENGINEER: 0.0}
        core = generate_project_schedule(rows, "baseline", team, size, est_cfg["calibration"]["ta_coding_share"],
                                         pool_devops=True, sched_cfg=cfg)["core_months"]
        limit = est_cfg["team_templates"][size]["months"] * (1 + cfg["size_tolerance"])
        checks.append({"size": size, "core_months": core, "limit_months": limit})
        if core <= limit:
            return {"size": size, "checks": checks}
    return {"size": "XL", "checks": checks}


def _round_up(value: float, step: float) -> float:
    return math.ceil(value / step - 1e-9) * step


def resolve_window(baseline_months: float, requested_months: float | None, sched_cfg: dict | None = None) -> dict:
    """Target development window from the team_sizing setting. client_requested = the TOR's delivery period minus UAT,
    pilot and deployment; it falls back to the baseline window when the TOR gives no usable period."""
    cfg = sched_cfg or schedule_config()
    ts = cfg["team_sizing"]
    if ts["target_window"] == "custom":
        return {"setting": "custom", "months": float(ts["custom_months"]), "note": ""}
    if ts["target_window"] == "client_requested":
        fixed = support_months(cfg) + cfg["deployment_weeks"] / cfg["weeks_per_month"]
        if requested_months and requested_months > fixed:
            return {"setting": "client_requested", "months": requested_months - fixed, "note": ""}
        return {"setting": "baseline", "months": baseline_months,
                "note": "The TOR gives no usable delivery period: the baseline window is used."}
    return {"setting": "baseline", "months": baseline_months, "note": ""}


def size_ai_engineer(rows: list[dict], scenario: str, window_months: float, sched_cfg: dict | None = None) -> dict:
    """FTE from the role's build-time hours first, rounded up. Only spare capacity x coding share counts as Backend capacity,
    so the same time is never counted twice."""
    cfg = sched_cfg or schedule_config()
    ai = cfg["ai_engineer"]
    hours = phase_role_hours(rows, scenario, cfg)
    build = sum(hours[p][AI_ENGINEER] for p in ("requirements", "architecture", "development", "sit", "testing"))
    need = build / (window_months * cfg["hours_per_fte_month"]) if window_months else 0.0
    fte = _round_up(need, ai["fte_step"])
    spare = max(fte - need, 0.0)
    return {"fte": fte, "build_hours": build, "fte_needed": need, "spare_fte": spare, "coding_share": ai["coding_share"],
            "coding_fte": spare * ai["coding_share"]}


def with_ai_engineer(rows: list[dict], scenario: str, team: dict, size: str, ta_share: float, sched_cfg: dict | None = None,
                     start: str | None = None) -> dict:
    """The timeline of a fixed team with the AI Engineer sized to the build time that team produces."""
    cfg = sched_cfg or schedule_config()
    window = generate_project_schedule(rows, scenario, {**team, AI_ENGINEER: 0.0}, size, ta_share, sched_cfg=cfg, start=start)["core_months"]
    ai = size_ai_engineer(rows, scenario, window, cfg)
    tl = generate_project_schedule(rows, scenario, {**team, AI_ENGINEER: ai["fte"]}, size, ta_share, sched_cfg=cfg, start=start,
                                   ai_coding_fte=ai["coding_fte"])
    ai = size_ai_engineer(rows, scenario, tl["core_months"], cfg)     # coding can shorten the build: size again to what it became
    return generate_project_schedule(rows, scenario, {**team, AI_ENGINEER: ai["fte"]}, size, ta_share, sched_cfg=cfg, start=start,
                                     ai_coding_fte=ai["coding_fte"])


def size_team(rows: list[dict], scenario: str, window_months: float, size: str, ta_share: float, est_cfg: dict,
              sched_cfg: dict | None = None, start: str | None = None) -> dict:
    """target_duration sizing: FTE per feature-driven role from its development-window hours, rounded up. If a phase
    sequence (requirements -> architecture -> SIT -> testing) still overruns the window, the role limiting the longest
    of those phases gets one more step until it fits. Returns {team, added, ai_coding_fte}; added lists those extra steps."""
    cfg = sched_cfg or schedule_config()
    ts = cfg["team_sizing"]
    hours = phase_role_hours(rows, scenario, cfg)
    core = ("requirements", "architecture", "development", "sit", "testing")
    template = template_team(size, est_cfg, cfg)
    ai = size_ai_engineer(rows, scenario, window_months, cfg)
    team = {"PM": template["PM"], "TA": template["TA"], AI_ENGINEER: ai["fte"]}
    coding_fte = ai["coding_fte"]
    for role in FEATURE_ROLES_V2:
        role_hours = sum(hours[p][role] for p in core)
        needed = role_hours / (window_months * cfg["hours_per_fte_month"]) - (coding_fte if role == "Backend" else 0.0)
        fte = _round_up(max(needed, 0.0), ts["fte_step"])
        team[role] = max(fte, ts["min_fte"]) if role_hours > 0 or role == "BA" else 0.0
    added = []
    for _ in range(40):      # bounded: every step shortens a phase
        tl = generate_project_schedule(rows, scenario, team, size, ta_share, sched_cfg=cfg, start=start, ai_coding_fte=coding_fte)
        if tl["core_months"] <= window_months + 1e-9:
            break
        path = [p for p in tl["phases"] if p["phase"] in ("requirements", "architecture", "sit", "testing")]
        role = max(path, key=lambda p: p["months"])["bottleneck"]
        team[role] += ts["fte_step"]
        added.append(role)
    # rounding usually finishes the build early: the AI Engineer's hours must fit the build time that results
    ai = size_ai_engineer(rows, scenario, tl["core_months"], cfg)
    team[AI_ENGINEER] = ai["fte"]
    return {"team": team, "added": added, "ai_coding_fte": ai["coding_fte"]}


def headcount(team: dict) -> dict:
    """FTE per role, total developers (Backend + Frontend) and total team."""
    return {**team, "developers": team.get("Backend", 0.0) + team.get("Frontend", 0.0), "total": sum(team.values())}

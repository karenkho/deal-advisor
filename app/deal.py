"""Deal economics (POC v2): warranty reserve, price floor and cash-flow exposure. Pure Python, no LLM. INTERNAL ONLY."""
from __future__ import annotations

import math

from app import schedule


def warranty(months: float, cfg: dict, sched_cfg: dict) -> dict:
    """Warranty support hours per role: FTE x months x hours per FTE-month."""
    hours = {r: fte * months * sched_cfg["hours_per_fte_month"] for r, fte in cfg["warranty"]["fte"].items()}
    return {"months": months, "by_role": hours, "hours": sum(hours.values())}


def price_floor(delivery_cost: float, price: float, min_margin: float) -> dict:
    floor = delivery_cost / (1 - min_margin)
    return {"price_floor": floor, "discount_room_pct": (price - floor) / price * 100 if price else 0.0,
            "min_gross_margin_pct": min_margin * 100}


def _spread(out: list[float], amount: float, start: float, end: float) -> None:
    """Adds an amount evenly over [start, end] months; month m covers (m-1, m]. A zero-length span lands in one month."""
    if amount == 0:
        return
    if end <= start:
        out[min(int(start), len(out) - 1)] += amount
        return
    for m in range(int(start), min(math.ceil(end), len(out))):
        overlap = min(end, m + 1) - max(start, m)
        if overlap > 0:
            out[m] += amount * overlap / (end - start)


def monthly_burn(rows: list[dict], scenario: str, timeline: dict, cost: dict, by_role: dict[str, float],
                 rates: dict[str, dict[str, float]], sched_cfg: dict, warranty_info: dict) -> list[float]:
    """Delivery cost per month, from the phase timeline and staffing. Sums to the delivery cost: activity hours fall in
    their phases, support in UAT and pilot, PM/TA over the other phases, tooling and infrastructure evenly, the warranty
    reserve after go-live, and the contingency in proportion to each month's cost."""
    total = timeline["total_months"]
    n = math.ceil(total - 1e-9) + math.ceil(warranty_info["months"])
    out = [0.0] * max(n, 1)
    ph = {p["phase"]: p for p in timeline["phases"]}
    phase_hours = schedule.phase_role_hours(rows, scenario, sched_cfg)
    applied = {r: v["applied"] for r, v in rates.items()}
    activity = dict.fromkeys(applied, 0.0)
    for phase, roles in phase_hours.items():
        for role, hours in roles.items():
            activity[role] += hours
            _spread(out, hours * applied[role], ph[phase]["start_month"], ph[phase]["end_month"])
    support = schedule.support_hours(scenario, sched_cfg)
    _spread(out, sum(h * applied[r] for r, h in support.items()), ph["uat"]["start_month"], ph["pilot"]["end_month"])
    # PM/TA overhead and any one-time setup: what remains of each role's hours, outside UAT and pilot
    rest = sum((by_role.get(r, 0.0) - activity.get(r, 0.0) - support.get(r, 0.0)) * applied[r] for r in by_role)
    before, after = ph["uat"]["start_month"], total - ph["deployment"]["start_month"]
    span = before + after
    _spread(out, rest * before / span if span else rest, 0.0, before)
    _spread(out, rest * after / span if span else 0.0, ph["deployment"]["start_month"], total)
    _spread(out, cost["ai_tooling"] + cost["infrastructure"], 0.0, total)
    _spread(out, cost["warranty_reserve"], total, total + warranty_info["months"])
    lines = cost["labor"] + cost["ai_tooling"] + cost["infrastructure"] + cost["warranty_reserve"]
    scale = cost["delivery_cost"] / lines if lines else 1.0
    return [v * scale for v in out]


def cash_position(burn: list[float], receipts: list[tuple[float, float]], price: float) -> dict:
    """receipts: (share of the price, month offset when it is due). Returns the monthly table and the peak negative
    position (0 if cash never goes negative). Amounts exclude VAT."""
    n = max(len(burn), max((math.ceil(t) for _, t in receipts), default=1), 1)
    months = []
    position = peak = 0.0
    peak_month = None
    for m in range(1, n + 1):
        spent = burn[m - 1] if m <= len(burn) else 0.0
        received = sum(share * price for share, t in receipts if max(math.ceil(t - 1e-9), 1) == m)
        position += received - spent
        months.append({"month": m, "burn": spent, "receipts": received, "cumulative": position})
        if position < peak:
            peak, peak_month = position, m
    return {"months": months, "peak_exposure": -peak, "peak_month": peak_month,
            "peak_share_of_price": -peak / price if price else 0.0}


def milestone_receipts(milestones: list[dict], timeline: dict) -> list[tuple[float, float]]:
    ph = {p["phase"]: p for p in timeline["phases"]}
    months = {"start": 0.0, "architecture_end": ph["architecture"]["end_month"], "uat_end": ph["uat"]["end_month"],
              "deployment_end": ph["deployment"]["end_month"]}
    return [(m["pct"], months[m["event"]]) for m in milestones]


def tor_receipts(terms: dict, timeline: dict) -> list[tuple[float, float]] | None:
    """Payment terms the TOR asks for: upfront at signing, the rest at final acceptance (after the pilot and go-live)."""
    if "upfront_pct" not in terms or "final_pct" not in terms:
        return None
    return [(terms["upfront_pct"] / 100, 0.0), (terms["final_pct"] / 100, timeline["total_months"])]

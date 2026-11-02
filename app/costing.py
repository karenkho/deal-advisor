"""calculate_cost_and_margin (POC v2): internal costing and pricing. Pure Python, no LLM. INTERNAL ONLY.

Rates: source (seniority-level) rates come from config/rates.illustrative.yaml (bundled, fake) or an uploaded CSV
held in session memory only. config/rate_mapping.yaml blends source roles into app-role rates. Rate values are never
written to disk, logs, caches or the opportunity record (only the source and a hash)."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from datetime import date

from app import tracing
from app.config import load_yaml
from app.deal import price_floor

CSV_COLUMNS = ("role_name", "published_rate_php_per_hour", "applied_rate_php_per_hour")


OPTIONAL_ROLES = ("AI Engineer",)    # pending confirmation: a rate file without it uses the illustrative rate


class RatesError(ValueError):
    """A rate file failed validation; the caller keeps the current (illustrative) rates."""


@dataclass(frozen=True)
class RateTable:
    source: str                          # illustrative | uploaded
    published: dict[str, float]          # source role -> PHP per hour
    applied_explicit: dict[str, float]   # source role -> applied PHP per hour, only where the file gives one
    defaulted: tuple[str, ...] = ()      # roles the file did not give, at the illustrative rate

    @property
    def hash(self) -> str:
        payload = json.dumps([self.published, self.applied_explicit], sort_keys=True).encode()
        return hashlib.sha256(payload).hexdigest()[:16]

    def applied(self, discount_pct: float = 0.0) -> dict[str, float]:
        """Applied rate = the file's applied rate, else published x (1 - discount)."""
        return {r: self.applied_explicit.get(r, p * (1 - discount_pct)) for r, p in self.published.items()}


def mapping_config() -> dict:
    return load_yaml("rate_mapping.yaml")


def illustrative_rates() -> RateTable:
    rates = {k: float(v) for k, v in load_yaml("rates.illustrative.yaml")["rates"].items()}
    return RateTable("illustrative", rates, {})


def _number(value: str, role: str, column: str) -> float:
    try:
        v = float(str(value).replace(",", "").strip())
    except ValueError:
        raise RatesError(f"{role}: {column} is not a number.") from None
    if v <= 0:
        raise RatesError(f"{role}: {column} must be greater than zero.")
    return v


def parse_rates_csv(data: bytes, mapping: dict | None = None) -> RateTable:
    """CSV with columns role_name, published_rate_php_per_hour, applied_rate_php_per_hour (optional).
    Raises RatesError naming the problem (messages never include rate values)."""
    mapping = mapping or mapping_config()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise RatesError("The file is not UTF-8 text. Upload a CSV file.") from None
    reader = csv.DictReader(io.StringIO(text))
    cols = [c.strip() for c in (reader.fieldnames or [])]
    missing_cols = [c for c in CSV_COLUMNS[:2] if c not in cols]
    if missing_cols:
        raise RatesError(f"Missing column(s): {', '.join(missing_cols)}. Expected: {', '.join(CSV_COLUMNS)} "
                         "(the last one optional).")
    known = set(mapping["source_roles"])
    published: dict[str, float] = {}
    applied: dict[str, float] = {}
    unknown = []
    for row in reader:
        row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        role = row.get("role_name", "")
        if not role:
            continue
        if role not in known:
            unknown.append(role)
            continue
        if role in published:
            raise RatesError(f"Duplicate role: {role}.")
        published[role] = _number(row["published_rate_php_per_hour"], role, "published_rate_php_per_hour")
        a = row.get("applied_rate_php_per_hour", "")
        if a:
            applied[role] = _number(a, role, "applied_rate_php_per_hour")
    if unknown:
        raise RatesError(f"Unknown role name(s): {', '.join(sorted(unknown))}. Use the names in "
                         "config/rate_mapping.yaml (source_roles).")
    needed = {r for mix in mapping["app_roles"].values() for r in mix}
    missing = sorted(needed - set(published) - set(OPTIONAL_ROLES))
    if missing:
        raise RatesError(f"Missing role(s) needed for blended rates: {', '.join(missing)}.")
    defaulted = tuple(r for r in OPTIONAL_ROLES if r in needed and r not in published)
    fallback = illustrative_rates().published
    for r in defaulted:
        published[r] = fallback[r]
    return RateTable("uploaded", published, applied, defaulted)


def blended_rates(table: RateTable, discount_pct: float = 0.0, mapping: dict | None = None) -> dict[str, dict[str, float]]:
    """App role -> {"published", "applied"} blended by the seniority mix (weighted average)."""
    mapping = mapping or mapping_config()
    applied = table.applied(discount_pct)
    tracing.protect([*table.published.values(), *applied.values()])
    out = {}
    for role, mix in mapping["app_roles"].items():
        w = sum(mix.values())
        out[role] = {"published": sum(table.published[src] * n for src, n in mix.items()) / w,
                     "applied": sum(applied[src] * n for src, n in mix.items()) / w}
        tracing.protect(out[role].values())
    return out


def _milestone_dates(timeline: dict) -> dict[str, str]:
    ph = {p["phase"]: p for p in timeline["phases"]}
    return {"start": timeline["start_date"], "architecture_end": ph["architecture"]["end_date"],
            "uat_end": ph["uat"]["end_date"], "deployment_end": ph["deployment"]["end_date"]}


def tooling_profile(name: str | None = None) -> dict:
    cfg = load_yaml("tooling_profiles.yaml")
    return with_usage(cfg["profiles"][name or cfg["active_profile"]], cfg)


def with_usage(profile: dict, cfg: dict) -> dict:
    """A profile's prices with the usage assumptions every profile shares."""
    return {**profile, "usage": cfg["usage"]}


TOOLING_LINES = (("seats", "AI coding assistant seats"), ("model_usage", "Model usage"), ("gpu_share", "Self-hosted GPU share"),
                 ("test_runs", "AI test generation and automation runs"), ("usage_contingency", "Contingency on usage"))


def tooling_lines(tooling: dict, scenario: str, headcount: float, months: float, ai_hours: dict[str, float]) -> dict:
    """The AI tooling cost of a scenario, line by line, from config. The baseline uses none. The GPU share is months x monthly cost / projects sharing it."""
    if scenario not in ("B", "C"):
        return dict.fromkeys([k for k, _ in TOOLING_LINES] + ["million_tokens", "total"], 0.0)
    usage = tooling["usage"]
    tokens = usage["tokens_per_hour"]
    million = (ai_hours["ai_assisted"] * tokens["ai_assisted"] + ai_hours["ai_first"] * tokens["ai_first_with_review"]) / 1_000_000
    model = million * tooling.get("php_per_million_tokens", 0)
    runs = usage["test_runs_per_month"][scenario] * months * tooling.get("php_per_test_run", 0)
    lines = {"seats": tooling.get("seat_php_per_month", 0) * headcount * months, "model_usage": model,
             "gpu_share": tooling.get("gpu_php_per_month", 0) * months / tooling.get("gpu_shared_projects", 1),
             "test_runs": runs, "usage_contingency": (model + runs) * usage["contingency_pct"]}
    return {**lines, "million_tokens": million, "total": sum(lines.values())}


def net_savings(costs: dict[str, dict]) -> dict[str, dict]:
    """Per AI scenario: labour saved against the baseline, the AI tooling that buys it, and what is left."""
    out = {}
    for s in ("B", "C"):
        saved = costs["baseline"]["labor"] - costs[s]["labor"]
        out[s] = {"labor_saved": saved, "ai_tooling": costs[s]["ai_tooling"], "net_savings": saved - costs[s]["ai_tooling"]}
    return out


@tracing.traced("calculate_cost_and_margin", lambda role_hours, timeline, rates, ai_hours, scenario, *a, **k: {"scenario": scenario, "roles": len(rates)}, lambda r: {"keys": sorted(k for k in r if not tracing._money_key(k))}, limit=6)
def calculate_cost_and_margin(role_hours: dict[str, float], timeline: dict, rates: dict[str, dict[str, float]],
                              ai_hours: dict[str, float], scenario: str, cfg: dict | None = None,
                              tooling: dict | None = None, warranty_hours: dict[str, float] | None = None) -> dict:
    """role_hours: app role -> hours (incl. PM/TA). rates: blended app-role rates (applied rate is used).
    ai_hours: AI-first and AI-assisted hours of this scenario. scenario: 'baseline' | 'B' | 'C' (AI tooling applies
    to B and C). tooling: the active tooling profile. warranty_hours: warranty support hours per role."""
    cfg = cfg or load_yaml("commercial.yaml")
    tooling = tooling or tooling_profile()
    months = timeline["total_months"]
    headcount = sum(v for v in timeline["team"].values())
    labor_by_role = {r: h * rates[r]["applied"] for r, h in role_hours.items()}
    labor = sum(labor_by_role.values())
    tool_lines = tooling_lines(tooling, scenario, headcount, months, ai_hours)
    tool_cost = tool_lines["total"]
    infra = cfg["infrastructure"]["dev_uat_cloud_php_per_month"] * months
    reserve = sum(h * rates[r]["applied"] for r, h in (warranty_hours or {}).items())
    lines = {"labor": labor, "ai_tooling": tool_cost, "infrastructure": infra, "warranty_reserve": reserve}
    contingency = cfg["contingency_rate"] * sum(lines[k] for k in cfg["contingency_eligible"])
    cost = labor + tool_cost + infra + reserve + contingency
    margin = cfg["target_gross_margin"]
    price = cost / (1 - margin)
    vat = price * cfg["vat_rate"]
    dates = _milestone_dates(timeline)
    milestones = []
    for m in cfg["payment_milestones"]:
        milestones.append({"name": m["name"], "pct": m["pct"], "amount": price * m["pct"], "date": dates[m["event"]]})
    retention = None
    r = cfg.get("retention", {})
    if r.get("enabled"):
        go_live = milestones[-1]
        held = go_live["amount"] * r["pct_of_go_live"]
        go_live["amount"] -= held
        end = date.fromisoformat(go_live["date"])
        retention = {"amount": held, "release_date": date(end.year + (end.month - 1 + r["warranty_months"]) // 12,
                                                          (end.month - 1 + r["warranty_months"]) % 12 + 1,
                                                          min(end.day, 28)).isoformat()}
    return {"scenario": scenario, "currency": cfg["currency"], "hours": sum(role_hours.values()),
            "labor_by_role": labor_by_role, "labor": labor, "ai_tooling": tool_cost, "ai_tooling_lines": tool_lines, "infrastructure": infra,
            "warranty_reserve": reserve, "contingency": contingency, "contingency_rate": cfg["contingency_rate"], "delivery_cost": cost,
            "price_ex_vat": price, "vat": vat, "price_incl_vat": price + vat, "vat_rate": cfg["vat_rate"],
            "gross_margin_pct": (price - cost) / price * 100, "markup_pct": (price - cost) / cost * 100,
            "target_gross_margin_pct": margin * 100, "milestones": milestones, "retention": retention,
            "months": months, "contract_type": cfg["contract_type"],
            **price_floor(cost, price, cfg.get("min_gross_margin", 0.30))}


def record_safe(cost: dict) -> dict:
    """What the opportunity record may keep: totals only, no per-role labor (rates could be back-computed)."""
    return {k: v for k, v in cost.items() if k != "labor_by_role"}

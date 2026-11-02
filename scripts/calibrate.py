"""Calibrate size-tier hours and complexity multipliers from historical actuals.

Base size_hours: exact fit on the standard-complexity project (P01), fixed tier ratios.
complexity_multipliers.<level>: per-role ratio of each other project's target feature hours
to the base model. Thresholds: midpoints of template Dev capacity (calibration TA share).

Usage:
    python scripts/calibrate.py            # print results, do not modify anything
    python scripts/calibrate.py --write    # write size_hours, multipliers, thresholds into estimation.yaml
    python scripts/calibrate.py --doc      # regenerate docs/calibration.md
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from app import calculator as calc  # noqa: E402

CONFIG_PATH = ROOT / "config" / "estimation.yaml"
DOC_PATH = ROOT / "docs" / "calibration.md"
CONSISTENCY_BAND = 0.20

ASSUMPTIONS = [
    "Complexity multipliers apply to feature hours of BA/Dev/UI/QA only; foundation hours and PM/TA are not multiplied.",
    "Domain complexity is a project-level input (one level per estimate), not per feature.",
    "Project size is classified on baseline Dev hours after the complexity multiplier (features x multiplier + foundation).",
    "Tier hours are rounded to whole hours; multipliers to 2 decimals. The small residual in the fit table comes from this rounding.",
    "Same-deadline scenario keeps PM and TA at template FTE; feature-driven roles are rounded up to 0.5 FTE.",
    "Synthetic corpus projects P03-P06 are standard complexity.",
    "Consistency check compares model baseline hours with template capacity using calibration settings (TA coding share 0.75, contingency 1.2).",
    "Regression tolerance for P01/P02 traditional duration: +/-10% of actual months.",
    "P03 feature mix adjusted after the P01-only base (Dev was -32%): added 15 features (customer app extensions, "
    "business accounts, rider management and risk) and upsized live operations map and rider location streaming to XL.",
    "P05 feature mix adjusted (Dev was -28%): added 16 features (advanced warehousing, customer and supplier insights).",
    "P04 left unchanged: Dev at -19% is inside the +/-20% band.",
    "P06 feature mix varied so it is not identical to P04 (user request, after calibration freeze): case handling "
    "M->L (field inspection with photo evidence), staff user administration M->S (fixed roles), added a public FAQ "
    "and service catalogue page (S). Mix is now S6/M6/L3 vs P04 S4/M8/L2; still Small.",
    "No relabels applied to P02 (user decision): its features are M for a very senior developer and L for a typical one.",
]

FINDINGS = [
    "Small template (BA 1, UI 1 for 4 months) is larger than the model needs: about 0.5 BA and 0.5 UI "
    "for a typical Small scope (see P04/P06 BA and UI columns). Recorded only; template unchanged.",
]


def units(tier_counts: dict, ratios: dict) -> float:
    return sum(tier_counts.get(t, 0) * ratios[t] for t in calc.TIERS)


def targets(project: dict, cfg: dict) -> dict[str, float]:
    """Target feature hours = (FTE x months x 160) / contingency - foundation hours."""
    hpm = cfg["capacity"]["hours_per_person_month"]
    cont = cfg["buffer"]["contingency"]
    share = cfg["calibration"]["ta_coding_share"]
    found = calc.foundation_hours(cfg)
    team = project["team"]
    out = {}
    for r in calc.FEATURE_ROLES:
        fte = calc.dev_fte_effective(team, share) if r == "Dev" else team[r]
        out[r] = fte * project["months"] * hpm / cont - found[r]
    return out


def model_feature_hours(tier_counts: dict, tier_hours: dict, mult: dict | None = None) -> dict[str, float]:
    mult = mult or dict.fromkeys(calc.FEATURE_ROLES, 1.0)
    return {r: sum(tier_counts.get(t, 0) * tier_hours[t][r] for t in calc.TIERS) * mult[r]
            for r in calc.FEATURE_ROLES}


def calibrate(cfg: dict):
    projects = cfg["calibration"]["projects"]
    ratios = cfg["tier_ratios"]
    base_id = next(pid for pid, p in projects.items() if p["domain_complexity"] == "standard")
    base = projects[base_id]
    tgt = targets(base, cfg)
    u = units(base["tier_counts"], ratios)
    scales = {r: tgt[r] / u for r in calc.FEATURE_ROLES}
    tier_hours = {t: {r: round(scales[r] * ratios[t]) for r in calc.FEATURE_ROLES} for t in calc.TIERS}

    multipliers = {"standard": dict.fromkeys(calc.FEATURE_ROLES, 1.0)}
    for pid, p in projects.items():
        level = p["domain_complexity"]
        if level == "standard":
            continue
        model = model_feature_hours(p["tier_counts"], tier_hours)
        t = targets(p, cfg)
        multipliers[level] = {r: round(t[r] / model[r], 2) for r in calc.FEATURE_ROLES}

    rows = []
    for pid, p in projects.items():
        t = targets(p, cfg)
        std = model_feature_hours(p["tier_counts"], tier_hours)
        cal = model_feature_hours(p["tier_counts"], tier_hours, multipliers[p["domain_complexity"]])
        for r in calc.FEATURE_ROLES:
            rows.append({"project": pid, "complexity": p["domain_complexity"], "role": r, "target": t[r],
                         "standard": std[r], "model": cal[r], "error": (cal[r] - t[r]) / t[r]})
    return base_id, scales, tier_hours, multipliers, rows


def thresholds(cfg: dict) -> tuple[dict, list[dict]]:
    hpm = cfg["capacity"]["hours_per_person_month"]
    cont = cfg["buffer"]["contingency"]
    share = cfg["calibration"]["ta_coding_share"]
    caps = []
    for size in calc.SIZE_ORDER:
        t = cfg["team_templates"][size]
        fte = calc.dev_fte_effective(t, share)
        caps.append({"size": size, "dev_fte": fte, "months": t["months"], "capacity": fte * t["months"] * hpm / cont})
    bounds = {a["size"]: round((a["capacity"] + b["capacity"]) / 2) for a, b in zip(caps, caps[1:])}
    return bounds, caps


def write_config(tier_hours: dict, multipliers: dict, bounds: dict) -> None:
    text = CONFIG_PATH.read_text(encoding="utf-8")

    def sub(pattern: str, new: str) -> None:
        nonlocal text
        text, n = re.subn(pattern, new, text, flags=re.M)
        assert n == 1, f"marker not found: {pattern}"

    for t in calc.TIERS:
        h = tier_hours[t]
        key = f"{t}:" + (" " if len(t) == 1 else "")
        sub(rf"^  {re.escape(t)}:\s+\{{[^}}]*\}}\s+# @calibrated$",
            f"  {key} {{BA: {h['BA']}, Dev: {h['Dev']}, UI: {h['UI']}, QA: {h['QA']}}}   # @calibrated")
    for level, m in multipliers.items():
        if level == "standard":
            continue
        sub(rf"^  {level}:\s+\{{[^}}]*\}}\s+# @derived$",
            f"  {level}:     {{BA: {m['BA']}, Dev: {m['Dev']}, UI: {m['UI']}, QA: {m['QA']}}}   # @derived")
    for size, v in bounds.items():
        sub(rf"^  {size}: \d+   # @derived$", f"  {size}: {v}   # @derived")
    CONFIG_PATH.write_text(text, encoding="utf-8")


# ---------- corpus-based checks ----------

def corpus_projects() -> dict[str, dict]:
    from app.retrieval import load_corpus
    out: dict[str, dict] = {}
    for c in load_corpus():
        if c.metadata.get("kind") == "feature":
            p = out.setdefault(c.metadata["project_id"], {"meta": c.metadata, "sizes": []})
            p["sizes"].append(c.metadata["size"])
    return out


def regression(cfg: dict) -> list[dict]:
    corpus = corpus_projects()
    rows = []
    for pid, p in cfg["calibration"]["projects"].items():
        res = calc.estimate(corpus[pid]["sizes"], cfg, complexity=p["domain_complexity"])
        months = res.scenarios[0].duration_months
        rows.append({"project": pid, "complexity": p["domain_complexity"], "expected_size": p["template"],
                     "size": res.project_size, "expected_months": p["months"], "months": months,
                     "bottleneck": res.scenarios[0].bottleneck,
                     "pass": res.project_size == p["template"] and abs(months - p["months"]) / p["months"] <= 0.10,
                     "result": res})
    return rows


def consistency(cfg: dict) -> list[dict]:
    """Synthetic projects (P03+): model baseline (features + foundation) vs template capacity."""
    hpm = cfg["capacity"]["hours_per_person_month"]
    cont = cfg["buffer"]["contingency"]
    share = cfg["calibration"]["ta_coding_share"]
    found = calc.foundation_hours(cfg)
    rows = []
    for pid, p in sorted(corpus_projects().items()):
        if pid in cfg["calibration"]["projects"]:
            continue
        tpl = cfg["team_templates"][p["meta"]["project_size"]]
        feat = calc.feature_hours(p["sizes"], cfg, "standard")
        row = {"project": pid, "template": p["meta"]["project_size"], "months": tpl["months"], "features": len(p["sizes"])}
        for r in calc.FEATURE_ROLES:
            fte = calc.dev_fte_effective(tpl, share) if r == "Dev" else tpl[r]
            cap = fte * tpl["months"] * hpm / cont
            row[r] = (feat[r] + found[r] - cap) / cap
        res = calc.estimate(p["sizes"], cfg, complexity="standard")
        row["size"] = res.project_size
        row["trad_months"] = res.scenarios[0].duration_months
        row["dev_ok"] = abs(row["Dev"]) <= CONSISTENCY_BAND
        rows.append(row)
    return rows


# ---------- report ----------

def fit_table(rows: list[dict]) -> str:
    out = ["| Project | Complexity | Role | Target h | Base model h | Calibrated h | Error |",
           "|---|---|---|---:|---:|---:|---:|"]
    for r in rows:
        out.append(f"| {r['project']} | {r['complexity']} | {r['role']} | {r['target']:,.0f} | {r['standard']:,.0f} | "
                   f"{r['model']:,.0f} | {r['error']:+.1%} |")
    return "\n".join(out)


def regression_table(rows: list[dict]) -> str:
    out = ["| Project | Complexity | Size (expected) | Traditional months (expected) | Bottleneck | Result |",
           "|---|---|---|---:|---|---|"]
    for r in rows:
        out.append(f"| {r['project']} | {r['complexity']} | {r['size']} ({r['expected_size']}) | "
                   f"{r['months']:.2f} ({r['expected_months']}) | {r['bottleneck']} | {'PASS' if r['pass'] else 'FAIL'} |")
    return "\n".join(out)


def scenario_table(res: calc.EstimateResult) -> str:
    out = ["| Scenario | Duration (months) | Bottleneck | PM | TA | BA | Dev | UI | QA | Total FTE | Total hours |",
           "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for sc in res.scenarios:
        fte = " | ".join(f"{sc.lines[r].fte:g}" for r in calc.ALL_ROLES)
        warn = " ⚠" if sc.warnings else ""
        out.append(f"| {sc.title}{warn} | {sc.duration_months:.1f} | {sc.bottleneck} | {fte} | "
                   f"{sum(l.fte for l in sc.lines.values()):g} | {sc.total_hours:,.0f} |")
    return "\n".join(out)


def consistency_table(rows: list[dict]) -> str:
    out = ["| Project | Template | Features | BA | Dev | UI | QA | Classified | Traditional months |",
           "|---|---|---:|---:|---:|---:|---:|---|---:|"]
    for r in rows:
        cells = " | ".join(f"{r[x]:+.0%}" + (" ⚠" if x == "Dev" and not r["dev_ok"] else "") for x in calc.FEATURE_ROLES)
        out.append(f"| {r['project']} | {r['template']} ({r['months']} mo) | {r['features']} | {cells} | "
                   f"{r['size']} | {r['trad_months']:.1f} |")
    return "\n".join(out)


def write_doc(cfg, base_id, scales, rows, bounds, caps, reg, cons) -> None:
    notes = "\n".join(f"- {n}" for n in cfg["calibration"]["notes"])
    tiers = "\n".join(f"| {t} | " + " | ".join(str(cfg["size_hours"][t][r]) for r in calc.FEATURE_ROLES) + " |"
                      for t in calc.TIERS)
    mults = "\n".join(f"| {lvl} | " + " | ".join(f"{m[r]:g}" for r in calc.FEATURE_ROLES) + " |"
                      for lvl, m in cfg["complexity_multipliers"].items())
    caps_t = "\n".join(f"| {c['size']} | {c['dev_fte']:g} | {c['months']} | {c['capacity']:,.0f} | {bounds.get(c['size'], '—')} |"
                       for c in caps)
    scen = "\n\n".join(f"### {r['project']} ({r['complexity']}): {len(corpus_projects()[r['project']]['sizes'])} features, "
                       f"{r['result'].baseline_dev_hours:,.0f} baseline Dev hours\n\n{scenario_table(r['result'])}" for r in reg)
    doc = f"""# Calibration of size-tier hours

Generated by `scripts/calibrate.py`. Anonymized: the two internal pre-AI projects are referred to only by
their corpus IDs (P01 Port Management, Mid, standard complexity; P02 Energy Trading & Billing, Large,
high complexity). Feature inventories are kept outside this repository.

## Calibration facts

{notes}

## Method

1. **Base size hours** — exact fit on {base_id} (standard complexity): per role, scale = target feature hours ÷ tier units,
   with tier ratios fixed at S:M:L:XL = 1 : 2.5 : 5 : 10. Scale (S-tier hours): {', '.join(f'{r} {v:.2f}' for r, v in scales.items())}.
2. **Target feature hours** = (FTE × months × 160) ÷ contingency {cfg['buffer']['contingency']} − foundation hours;
   Dev FTE = Dev + TA × {cfg['calibration']['ta_coding_share']} (calibration TA coding share).
3. **Complexity multipliers** — `high` = P02 target feature hours ÷ P02 base-model feature hours, per role.
   Applied to feature hours of feature-driven roles only.
4. **TA coding share** — traditional scenario and size thresholds use {cfg['calibration']['ta_coding_share']};
   both AI-enabled scenarios use {cfg['estimation']['ta_coding_share']} (TA does architecture, design and AI-output review).

## Calibrated values

| Tier | BA | Dev | UI | QA |
|---|---:|---:|---:|---:|
{tiers}

| Complexity | BA | Dev | UI | QA |
|---|---:|---:|---:|---:|
{mults}

## Fit

{fit_table(rows)}

## Project size thresholds

Template Dev capacity = (Dev + TA × {cfg['calibration']['ta_coding_share']}) × months × 160 ÷ {cfg['buffer']['contingency']};
boundaries at midpoints between adjacent templates (baseline Dev hours, features × multiplier + foundation).

| Template | Dev FTE | Months | Dev capacity h | Upper bound h |
|---|---:|---:|---:|---:|
{caps_t}

## Regression (P01, P02)

{regression_table(reg)}

## Three-scenario results

{scen}

## Corpus consistency check (synthetic P03–P06, standard complexity)

Model baseline hours (features + foundation) vs template capacity per role; Dev must be within ±20%.

{consistency_table(cons)}

## Assumptions

{chr(10).join(f'- {a}' for a in ASSUMPTIONS)}

## Findings (no change made)

{chr(10).join(f'- {f}' for f in FINDINGS)}

## Limitations

- Calibrated on senior-led teams; an average-seniority team may need more hours.
- Two data points, one per complexity class; not validated out-of-sample.
- Actual effort includes change requests and rework, so values are likely slightly high.
- Estimates cover the development phase only; UAT support and warranty are not included.
"""
    DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOC_PATH.write_text(doc, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--doc", action="store_true")
    args = ap.parse_args()
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))

    base_id, scales, tier_hours, multipliers, rows = calibrate(cfg)
    bounds, caps = thresholds(cfg)
    print("base:", base_id, "tier hours:", tier_hours)
    print("multipliers:", multipliers)
    print("thresholds:", bounds)
    print(fit_table(rows))

    if args.write:
        write_config(tier_hours, multipliers, bounds)
        print("wrote", CONFIG_PATH.relative_to(ROOT))
        cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))

    if args.doc:
        reg = regression(cfg)
        cons = consistency(cfg)
        print(regression_table(reg))
        print(consistency_table(cons))
        write_doc(cfg, base_id, scales, rows, bounds, caps, reg, cons)
        print("wrote", DOC_PATH.relative_to(ROOT))


if __name__ == "__main__":
    main()

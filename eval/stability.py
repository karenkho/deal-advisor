"""Stability check: run the same input N times with caches bypassed and compare the runs. Each BR card comes from one
feature, so feature overlap and size agreement are also BR overlap and size agreement; the BRD families are compared by count.

Strict overlap matches identical names or embedding similarity; capability-matched overlap asks the MAIN model
(one small call per pair of runs) whether two features deliver the same capability.

Usage: python eval/stability.py samples/tor_A_clean.pdf [--runs 3] [--yes] [--reuse]
  --yes    required for live runs (states the estimated cost first)
  --reuse  re-analyse the saved runs in .cache/stability/ without Azure chat calls
Reports feature-set overlap (Jaccard), size agreement and total-hours spread per scenario against the targets in
config/pipeline.yaml, and writes docs/stability.md (one section per input). Development-set result, not held out.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from app import llm, spend  # noqa: E402
from app.config import CACHE_DIR, pipeline_config  # noqa: E402
from app.graph import run_calculator  # noqa: E402
from app.parsing import parse_bytes  # noqa: E402
from app import brd  # noqa: E402
from app.kvstore import KV  # noqa: E402
from app.pipeline import analyze_requirements, build_record  # noqa: E402
from app.record import Brd  # noqa: E402
from app.schemas import TorState  # noqa: E402

RUNS_DIR = CACHE_DIR / "stability"
BRD_RUN_USD = 0.02       # measured: one uncached extract_brd on the HRIS TOR
DOC = ROOT / "docs" / "stability.md"


def _unit(v) -> np.ndarray:
    v = np.array(v, dtype=np.float32)
    return v / (np.linalg.norm(v, axis=1, keepdims=True) + 1e-9)


def match(a: list, b: list, threshold: float) -> list[tuple[int, int]]:
    """One-to-one greedy matching: identical names first, then embedding similarity >= threshold."""
    if not a or not b:
        return []
    va = _unit(llm.embed([f.name for f in a]))
    vb = _unit(llm.embed([f.name for f in b]))
    da = _unit(llm.embed([f"{f.name}. {f.description}" for f in a]))
    db = _unit(llm.embed([f"{f.name}. {f.description}" for f in b]))
    sim = np.maximum(va @ vb.T, da @ db.T)
    for i, fa in enumerate(a):
        for j, fb in enumerate(b):
            if fa.name.lower() == fb.name.lower():
                sim[i, j] = 2.0
    pairs, used_a, used_b = [], set(), set()
    for s, i, j in sorted(((sim[i, j], i, j) for i in range(len(a)) for j in range(len(b))), reverse=True):
        if s < threshold:
            break
        if i not in used_a and j not in used_b:
            used_a.add(i)
            used_b.add(j)
            pairs.append((i, j))
    return pairs


class _Pair(BaseModel):
    a_id: str
    b_id: str = Field(description="Feature id in run B with the same capability, or NONE")


class _Pairs(BaseModel):
    pairs: list[_Pair]


JUDGE_PROMPT = """Two backlogs were generated from the same TOR. For each feature in backlog A, give the id of the
feature in backlog B that delivers the same capability (wording, granularity of the name and size may differ), or
NONE. Use each B id at most once. Judge capability, not wording."""


def judge(a: list, b: list) -> list[tuple[int, int]]:
    """Capability-matched pairs judged by the MAIN model (one call); one-to-one."""
    la = "\n".join(f"A{i}: {f.name} [{f.type}] - {f.description[:160]}" for i, f in enumerate(a))
    lb = "\n".join(f"B{j}: {f.name} [{f.type}] - {f.description[:160]}" for j, f in enumerate(b))
    out = llm.structured([{"role": "system", "content": JUDGE_PROMPT},
                          {"role": "user", "content": f"<backlog_a>\n{la}\n</backlog_a>\n<backlog_b>\n{lb}\n</backlog_b>"}],
                         _Pairs, node="stability_judge")
    pairs, used = [], set()
    for p in out.pairs:
        if p.a_id[1:].isdigit() and p.b_id[1:].isdigit() and p.b_id not in used:
            i, j = int(p.a_id[1:]), int(p.b_id[1:])
            if i < len(a) and j < len(b):
                used.add(p.b_id)
                pairs.append((i, j))
    return pairs


def total_hours(state: TorState) -> dict[str, float]:
    level = state.complexity.level if state.complexity else "standard"
    est = run_calculator(state.features, level)
    return {sc["title"]: sum(line["hours"] for line in sc["lines"].values()) for sc in est["scenarios"]}


def analyse(name: str, states: list[TorState]) -> dict:
    cfg = pipeline_config()["stability_targets"]
    feats = [[f for f in s.features if f.epic != "Vendor supply"] for s in states]
    jacc, jacc_cap, sizes = [], [], []
    for x, y in itertools.combinations(range(len(states)), 2):
        pairs = match(feats[x], feats[y], cfg["feature_match_similarity"])
        jacc.append(len(pairs) / (len(feats[x]) + len(feats[y]) - len(pairs)))
        cap = judge(feats[x], feats[y])
        jacc_cap.append(len(cap) / (len(feats[x]) + len(feats[y]) - len(cap)))
        sized = [(feats[x][i].size, feats[y][j].size) for i, j in pairs if feats[x][i].size and feats[y][j].size]
        sizes.append(sum(p == q for p, q in sized) / len(sized) if sized else 1.0)
    hours = [total_hours(s) for s in states]
    spread = {}
    for sc in hours[0]:
        vals = [h[sc] for h in hours]
        mean = sum(vals) / len(vals)
        spread[sc] = {"values": [round(v) for v in vals], "max_dev": max(abs(v - mean) for v in vals) / mean}
    return {"name": name, "runs": len(states), "features": [len(f) for f in feats],
            "jaccard_mean": float(np.mean(jacc)), "jaccard_min": float(min(jacc)),
            "jaccard_cap_mean": float(np.mean(jacc_cap)), "jaccard_cap_min": float(min(jacc_cap)),
            "size_agreement_mean": float(np.mean(sizes)), "size_agreement_min": float(min(sizes)),
            "hours": spread, "cost_usd": sum(s.usage.get("est_usd", 0.0) for s in states), "targets": cfg}


def render(r: dict) -> str:
    t = r["targets"]
    ok = lambda v, target: "PASS" if v >= target else "BELOW"  # noqa: E731
    lines = [f"## {r['name']}", "",
             f"{r['runs']} runs with the extraction and result caches bypassed (temperature 0, fixed seed). "
             f"Features per run: {r['features']}. Live cost of the runs: ${r['cost_usd']:.3f}. "
             "Development-set input (prompts were tuned on it), not a held-out result.", "",
             "| Metric | Mean | Worst pair | Target | Result |", "|---|---:|---:|---:|---|",
             f"| Feature-set overlap, strict (same name or embedding ≥ {t['feature_match_similarity']}) "
             f"| {r['jaccard_mean']:.2f} | {r['jaccard_min']:.2f} | ≥ {t['jaccard']} | {ok(r['jaccard_mean'], t['jaccard'])} |",
             f"| Feature-set overlap, capability-matched (LLM judge) | {r['jaccard_cap_mean']:.2f} "
             f"| {r['jaccard_cap_min']:.2f} | ≥ {t['jaccard']} | {ok(r['jaccard_cap_mean'], t['jaccard'])} |",
             f"| Size agreement (matched features) | {r['size_agreement_mean']:.0%} | {r['size_agreement_min']:.0%} "
             f"| ≥ {t['size_agreement']:.0%} | {ok(r['size_agreement_mean'], t['size_agreement'])} |"]
    for sc, v in r["hours"].items():
        res = "PASS" if v["max_dev"] <= t["total_hours_tolerance"] else "BELOW"
        lines.append(f"| Total hours, {sc} | {', '.join(map(str, v['values']))} | ±{v['max_dev']:.1%} "
                     f"| ±{t['total_hours_tolerance']:.0%} | {res} |")
    return "\n".join(lines) + "\n"


def render_brd(brds: list[Brd]) -> str:
    families = ("requirements", "rules", "entities", "nfrs", "constraints", "assumptions", "questions")
    # BR overlap on stable ids: two runs hold the same BR when it is built from the same statements
    sets = [{frozenset(c.statement_ids) for c in b.requirements} for b in brds]
    overlap = [len(x & y) / len(x | y) if x | y else 1.0 for x, y in itertools.combinations(sets, 2)]
    statements = [{i for c in b.requirements for i in c.statement_ids} for b in brds]
    same = all(s == statements[0] for s in statements)
    lines = ["", f"BR overlap on stable statement ids (same statements = same BR): mean {sum(overlap) / len(overlap):.2f}, worst pair "
             f"{min(overlap):.2f}. Statements in BRs identical across runs: {'yes' if same else 'no'}.", "",
             "BRD families per run (no count target):", "",
             "| Family | " + " | ".join(f"Run {i}" for i in range(1, len(brds) + 1)) + " |",
             "|---|" + "---:|" * len(brds)]
    lines += [f"| {k} | " + " | ".join(str(len(getattr(b, k))) for b in brds) + " |" for k in families]
    for label, key, fmt in (("BRs with an exact locator", "brs_with_exact_locator_pct", "{:.0f}%"),
                            ("Rules linked to a BR", "rules_linked_to_a_br_pct", "{:.0f}%"),
                            ("Unsupported items", "unsupported_items", "{}"),
                            ("BRs blocked by open questions", "brs_blocked_by_open_questions", "{}")):
        lines.append(f"| {label} | " + " | ".join(fmt.format(brd.metrics(b)[key]) for b in brds) + " |")
    return "\n".join(lines) + "\n"


def write_doc(section: str, name: str) -> None:
    text = DOC.read_text() if DOC.exists() else ("# Stability check\n\nGenerated by `eval/stability.py`. Features "
                                                 "from two runs match on identical names or embedding similarity "
                                                 "≥ the configured threshold (one-to-one).\n\n")
    text = re.sub(rf"## {re.escape(name)}\n.*?(?=\n## |\Z)", "", text, flags=re.S).rstrip() + "\n\n" + section
    DOC.write_text(text)


def main() -> None:
    path = Path(sys.argv[1])
    n = int(sys.argv[sys.argv.index("--runs") + 1]) if "--runs" in sys.argv else 3
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    files = [RUNS_DIR / f"{path.stem}_run{i}.json" for i in range(1, n + 1)]
    if "--reuse" not in sys.argv:
        pages = len(parse_bytes(path.read_bytes(), path.name))
        est = n * (spend.estimate_run_usd(pages) + BRD_RUN_USD)
        print(f"{n} uncached runs of {path.name} ({pages} pages): estimated USD {est:.2f}")
        if "--yes" not in sys.argv:
            sys.exit("Re-run with --yes to proceed.")
        os.environ["EXTRACT_CACHE"] = "off"
        for i, f in enumerate(files, start=1):
            state, usage, runtime, _ = analyze_requirements([path], use_cache=False)
            f.write_text(state.model_dump_json())
            print(f"  run {i}: {len(state.features)} features, ${usage['est_usd']:.3f}, {runtime:.0f} s, "
                  f"errors {state.errors}")
            brd_usage = llm.Usage(limit_usd=spend.max_run_usd())
            rec = build_record(state, [path], {"id": "stability", "client": "n/a", "project_name": path.stem}, usage,
                               runtime, False)
            try:
                result, _ = brd.extract_brd(rec, brd_usage, KV(enabled=False))
            finally:
                spend.record(brd_usage.totals()["est_usd"])
            (RUNS_DIR / f"{path.stem}_brd{i}.json").write_text(result.model_dump_json())
            print(f"  run {i} BRD: {len(result.requirements)} BRs, ${brd_usage.totals()['est_usd']:.3f}")
    states = [TorState.model_validate_json(f.read_text()) for f in files]
    result = analyse(path.stem, states)
    brds = [Brd.model_validate_json(p.read_text()) for i in range(1, n + 1)
            if (p := RUNS_DIR / f"{path.stem}_brd{i}.json").exists()]
    section = render(result) + (render_brd(brds) if brds else "")
    write_doc(section, path.stem)
    print(section)


if __name__ == "__main__":
    main()

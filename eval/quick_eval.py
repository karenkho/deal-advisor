"""Quick proof-of-value check: run the graph on both sample TORs and compare with the answer keys.

Usage: python eval/quick_eval.py [--fresh]     (--fresh bypasses the extraction cache for honest cost/runtime)
Writes docs/poc_results.md.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from rapidfuzz import fuzz  # noqa: E402

from app import llm, result_cache  # noqa: E402
from app.graph import run  # noqa: E402
from app.parsing import parse_bytes  # noqa: E402

TIERS = ["S", "M", "L", "XL"]
EMBED_MATCH = 0.65      # max cosine(answer-key name, system name | system name + description)
EMBED_MATCH_SAME_TYPE = 0.60   # relaxed threshold when the feature types agree
FUZZ_MATCH = 75         # rapidfuzz token_set_ratio(answer-key name, system name)
SAMPLES = [("tor_A_clean", "samples/tor_A_clean.pdf", "tor_A"), ("tor_B_messy", "samples/tor_B_messy.pdf", "tor_B")]
PII_STRINGS = ["Villanueva", "0917 555 0142", "danilo.villanueva@halcyonridge.example"]


def _unit(v) -> np.ndarray:
    v = np.array(v, dtype=np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


class _Match(BaseModel):
    key_id: str
    system_id: str = Field(description="Matching system feature id, or NONE")


class _Matches(BaseModel):
    matches: list[_Match]


JUDGE_PROMPT = """You check a generated software backlog against an answer key. For each answer-key feature, give the
id of the system feature that implements the same capability (it may be broader, merging several key features, or use
different words), or NONE if no system feature covers it. Judge capability, not wording or size."""


def judge_matches(key: list[dict], feats: list) -> dict[str, str] | None:
    """Semantic match judged by the MAIN model (one call per TOR). None if the call fails."""
    listing_k = "\n".join(f"{k['id']}: {k['name']} [{k['type']}]" for k in key)
    listing_s = "\n".join(f"{f.id}: {f.name} [{f.type}] - {f.description[:160]}" for f in feats)
    try:
        out = llm.structured([{"role": "system", "content": JUDGE_PROMPT},
                              {"role": "user", "content": f"<answer_key>\n{listing_k}\n</answer_key>\n"
                                                          f"<system>\n{listing_s}\n</system>"}],
                             _Matches, node="eval_judge")
    except Exception:  # noqa: BLE001
        return None
    return {m.key_id: m.system_id for m in out.matches}


def match_features(key: list[dict], feats: list, use_judge: bool = True) -> list[tuple[dict, object | None, float]]:
    """Each answer-key feature -> system feature (many-to-one allowed: the system may merge).
    Primary: LLM-judged semantic match. Score shown = max(cosine(key name, system name | name + description));
    fallback when the judge fails: embedding/fuzzy thresholds."""
    if not feats:
        return [(k, None, 0.0) for k in key]
    names = [re.sub(r"\s*\([^)]*\)", "", k["name"]).strip() for k in key]
    vk = _unit(llm.embed(names))
    vn = _unit(llm.embed([f.name for f in feats]))
    vd = _unit(llm.embed([f"{f.name}. {f.description}" for f in feats]))
    sim = np.maximum(vk @ vn.T, vk @ vd.T)
    judged = judge_matches(key, feats) if use_judge else None
    by_id = {f.id: f for f in feats}
    if judged is not None:
        out = []
        for i, k in enumerate(key):
            f = by_id.get(judged.get(k["id"], "NONE"))
            j = list(by_id).index(f.id) if f else int(sim[i].argmax())
            out.append((k, f, float(sim[i, j])))
        return out
    out = []
    for i, k in enumerate(key):
        fz = np.array([fuzz.token_set_ratio(names[i].lower(), f.name.lower()) / 100 for f in feats])
        thr = np.array([EMBED_MATCH_SAME_TYPE if f.type == k["type"] else EMBED_MATCH for f in feats])
        j = int((sim[i] - thr).argmax())
        jf = int(fz.argmax())
        if sim[i, j] >= thr[j]:
            out.append((k, feats[j], float(sim[i, j])))
        elif fz[jf] >= FUZZ_MATCH / 100:
            out.append((k, feats[jf], float(fz[jf])))
        else:
            out.append((k, None, float(sim[i, j])))
    return out


def flaw_checks(state, key: dict) -> list[tuple[str, str, bool, str]]:
    feats, qs = state.features, state.questions
    dump = state.model_dump_json()
    results = []
    for f in key["planted_flaws"]:
        kind, ok, evidence = f["kind"], False, ""
        if kind == "vague_requirement":
            hits = [x for x in feats if x.route == "unclear" and set(x.pages) & set(f["source_pages"])]
            ok = bool(hits) and any(q.source == "unclear" and set(q.pages) & set(f["source_pages"]) for q in qs)
            evidence = hits[0].name if hits else "no unclear feature on the cited page"
        elif kind == "implied_migration":
            hits = [x for x in feats if x.origin == "critic" and x.type == "data_migration"]
            ok = bool(hits)
            evidence = hits[0].name if hits else "critic added no migration feature"
        elif kind == "implied_authentication":
            q = [x for x in qs if any(t in x.text.lower() for t in ("log in", "login", "authentication", "sign-on"))]
            ua = [x for x in feats if x.origin == "critic" and "user" in x.name.lower()]
            ok = bool(q) and bool(ua)
            evidence = f"login question: {bool(q)}; critic user-admin feature: {ua[0].name if ua else 'none'}"
        elif kind == "out_of_scope":
            hits = [e for e in state.exclusions if "laptop" in e["text"].lower()]
            ok = bool(hits)
            evidence = f"exclusion on p{hits[0]['pages']}" if hits else "not excluded"
        elif kind == "conflict":
            hits = [q for q in qs if q.source == "conflict" and set(f["source_pages"]) <= set(q.pages)]
            ok = bool(hits)
            evidence = f"conflict question citing p{hits[0].pages}" if hits else "no conflict question citing both pages"
        elif kind == "prompt_injection":
            inj = [g for g in state.guard_findings if g.kind == "injection" and g.page in f["source_pages"]]
            sizes = [x.size for x in feats if x.size]
            not_all_small = bool(sizes) and any(s != "S" for s in sizes)
            ok = bool(inj) and not_all_small
            evidence = f"guard finding p{inj[0].page}; sizes not all S: {not_all_small}" if inj else "not flagged"
        elif kind == "pii":
            leaked = [s for s in PII_STRINGS if s in dump]
            ok = state.pii_masked >= 3 and not leaked
            evidence = f"{state.pii_masked} items masked; leaked in output: {leaked or 'none'}"
        results.append((f["id"], kind, ok, evidence))
    return results


def evaluate(name: str, path: str, key_prefix: str) -> dict:
    key = json.loads((ROOT / "samples" / f"{key_prefix}_answer_key.json").read_text())
    data = (ROOT / path).read_bytes()
    t0 = time.perf_counter()
    state, usage = run(parse_bytes(data, Path(path).name), name)
    runtime = time.perf_counter() - t0
    result_cache.put(data, state, usage.totals(), runtime)   # ship-able result so "Load sample" costs nothing
    in_scope = [f for f in state.features if f.route != "out_of_scope"]
    matches = match_features(key["features"], in_scope)
    fuzzy_recall = sum(f is not None for _, f, _ in match_features(key["features"], in_scope, use_judge=False)) / len(key["features"])
    matched = [(k, f) for k, f, _ in matches if f is not None]
    sized = [(k, f) for k, f in matched if f.size]
    matched_ids = {f.id for _, f in matched}
    extra = [f for f in in_scope if f.id not in matched_ids and f.route != "unclear"]
    exact = sum(k["expected_size"] == f.size for k, f in sized)
    within = sum(abs(TIERS.index(k["expected_size"]) - TIERS.index(f.size)) <= 1 for k, f in sized)
    tot = usage.totals()
    (ROOT / ".cache").mkdir(exist_ok=True)
    (ROOT / ".cache" / f"eval_{name}.json").write_text(state.model_dump_json(indent=1))
    return {"name": name, "key": key, "state": state, "runtime": runtime, "matches": matches,
            "recall": len(matched) / len(key["features"]), "fuzzy_recall": fuzzy_recall, "sized": len(sized), "exact": exact, "within": within,
            "flaws": flaw_checks(state, key), "tokens": tot["prompt_tokens"] + tot["completion_tokens"],
            "usd": tot["est_usd"], "cache_hit": state.cache_hit, "extra": extra}


def write_report(results: list[dict], fresh: bool) -> str:
    lines = ["# POC results", "",
             f"Generated by `eval/quick_eval.py` on {time.strftime('%Y-%m-%d %H:%M')}"
             f" ({'extraction cache bypassed' if fresh else 'extraction cache allowed'}). Synthetic data only.", "",
             "## Summary", "",
             "| TOR | Pages | Features (key) | Coverage recall (LLM judge) | Coverage recall (fuzzy/embedding) | Extra features not in key | Sizing exact | Sizing within one tier | Planted flaws caught | Runtime | Tokens | Est. LLM cost |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in results:
        s, n = r["state"], len(r["key"]["features"])
        flaws = r["flaws"]
        caught = f"{sum(ok for *_, ok, _ in flaws)}/{len(flaws)}" if flaws else "n/a"
        sized = max(r["sized"], 1)
        lines.append(f"| {r['name']} | {s.pages_read} | {len(s.features)} ({n}) | {r['recall']:.0%} | {r['fuzzy_recall']:.0%} | {len(r['extra'])} | "
                     f"{r['exact']}/{r['sized']} ({r['exact'] / sized:.0%}) | {r['within']}/{r['sized']} ({r['within'] / sized:.0%}) | "
                     f"{caught} | {r['runtime']:.0f} s | {r['tokens']:,} | ${r['usd']:.3f} |")
    lines += ["", "Matching: each answer-key feature is matched to an in-scope system feature by an LLM judge "
                  "(gpt-4.1-mini, one call per TOR: same capability, possibly broader or differently worded; several key "
                  "features may map to one system feature). The score column shows the embedding similarity for reference. "
                  "Fallback if the judge fails: an answer-key feature counts as found when a system feature has embedding "
                  f"cosine ≥ {EMBED_MATCH} (≥ {EMBED_MATCH_SAME_TYPE} when feature types agree; key name without parentheses vs "
                  f"system name, or vs system name + description) or rapidfuzz token-set ratio ≥ {FUZZ_MATCH}. "
                  "Several key features may map to one broader system feature.", ""]
    for r in results:
        s = r["state"]
        lines += [f"## {r['name']}", "", "### Feature coverage and sizing", "",
                  "| Key feature | Expected | System feature | Route | Size | Match |", "|---|---|---|---|---|---:|"]
        for k, f, score in r["matches"]:
            if f is None:
                lines.append(f"| {k['name']} | {k['expected_size']} | — not found — | | | {score:.2f} |")
            else:
                lines.append(f"| {k['name']} | {k['expected_size']} | {f.name} ({f.origin}) | {f.route} | {f.size or '—'} | {score:.2f} |")
        if r["extra"]:
            lines += ["", "Extra in-scope features not in the answer key (precision check): "
                      + "; ".join(f"{f.name} [{f.origin}, {f.size or '—'}]" for f in r["extra"])]
        if r["flaws"]:
            lines += ["", "### Planted flaws", "", "| Flaw | Kind | Result | Evidence |", "|---|---|---|---|"]
            lines += [f"| {fid} | {kind} | {'PASS' if ok else 'FAIL'} | {ev} |" for fid, kind, ok, ev in r["flaws"]]
        sc = s.estimate["scenarios"] if s.estimate else []
        lines += ["", "### Estimate (hours only, no pricing)", "",
                  f"Domain complexity: {s.complexity.level if s.complexity else 'n/a'}; project size "
                  f"{s.estimate['project_size'] if s.estimate else 'n/a'}.", "",
                  "| Scenario | Months | Bottleneck | Total hours |", "|---|---:|---|---:|"]
        lines += [f"| {x['title']} | {x['duration_months']:.1f} | {x['bottleneck']} | "
                  f"{sum(l['hours'] for l in x['lines'].values()):,.0f} |" for x in sc]
        lines += ["", f"Questions raised: {len(s.questions)} · exclusions: {len(s.exclusions)} · "
                      f"guard findings: {len(s.guard_findings)} · errors: {len(s.errors)}", ""]
    out = "\n".join(lines)
    (ROOT / "docs" / "poc_results.md").write_text(out, encoding="utf-8")
    return out


if __name__ == "__main__":
    fresh = "--fresh" in sys.argv
    if fresh:
        os.environ["EXTRACT_CACHE"] = "off"
    results = [evaluate(*s) for s in SAMPLES]
    report = write_report(results, fresh)
    print(report.split("## tor_A_clean")[0])
    for r in results:
        print(r["name"], [(fid, "PASS" if ok else "FAIL") for fid, _, ok, _ in r["flaws"]])

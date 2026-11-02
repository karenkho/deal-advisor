"""Suitability prompt comparison on the 30 labelled HRIS activities: V1 (category definitions only) against V3 (definitions plus retrieved guidance, which is production). V2 is cut (addendum E).
Each variant classifies the same items, with the same model. Agreement with the labels is measured on the LLM's answer and after the deterministic rules, which is what users see.

Usage: python eval/prompt_compare.py     Writes eval/results/prompt_comparison.json
"""
from __future__ import annotations

import csv
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import llm, record, suitability, tools  # noqa: E402
from app.config import estimation_config  # noqa: E402
from app.effort import activities_config  # noqa: E402

CATEGORIES = ("human_led", "ai_assisted", "ai_first_with_review", "deterministic_automated")
DEFINITIONS = """human_led: a person does the work; AI adds little or nothing.
ai_assisted: a person does the work, with AI helping.
ai_first_with_review: AI produces the work, and a person reviews and approves it.
deterministic_automated: tools or scripts do the work, without judgment from AI."""


def kappa(truth: list[str], guess: list[str]) -> float:
    """Cohen's kappa over the categories either side used."""
    n = len(truth)
    observed = sum(a == b for a, b in zip(truth, guess)) / n
    a, b = Counter(truth), Counter(guess)
    expected = sum(a[c] * b[c] for c in set(truth) | set(guess)) / (n * n)
    return (observed - expected) / (1 - expected) if expected < 1 else 0.0


def scores(truth: list[str], guess: list[str]) -> dict:
    return {"accuracy": round(sum(a == b for a, b in zip(truth, guess)) / len(truth), 3), "kappa": round(kappa(truth, guess), 3), "predicted": dict(Counter(guess)),
            "per_category": {c: {"labelled": truth.count(c), "right": sum(a == b == c for a, b in zip(truth, guess))} for c in CATEGORIES}}


def main() -> None:
    labels = list(csv.DictReader((ROOT / "eval" / "labels" / "suitability_labels.csv").open(encoding="utf-8-sig")))
    answers = json.loads((ROOT / "eval" / "labels" / "sample_answers_do_not_open_before_labelling.json").read_text())
    rec = record.load("hris")
    units = {u["id"]: u for u in suitability._units(rec.features, estimation_config(), activities_config())}
    guidance = "\n\n".join(g["text"] for g in tools.retrieve_estimation_guidelines("AI suitability guidance human_led ai_assisted ai_first_with_review deterministic_automated", k=2))
    items = []
    for row in labels:
        feature, activity = answers[row["item"]]["key"].split("|", 1)
        items.append({"item": row["item"], "feature": feature, "activity": activity, "human": row["human_category"].strip(), "stored_llm": answers[row["item"]]["llm_suggestion"], "stored_final": answers[row["item"]]["after_rules"]})
    truth = [i["human"] for i in items]
    out = {"items": items, "variants": {}}
    for name, text in (("V1", DEFINITIONS), ("V3", guidance)):
        usage = llm.Usage()
        started = time.perf_counter()
        guesses, finals = [], []
        for item in items:
            unit = units[item["feature"]]
            listing = f"{unit['id']} | {unit['name']} [{unit['type']}, complexity {unit['complexity']}, route {unit['route']}]: {unit['description']}\n    activities: {item['activity']}"
            res = llm.structured([{"role": "system", "content": suitability.PROMPT.format(guidance=text)}, {"role": "user", "content": f"<features>\n{listing}\n</features>"}], suitability._Batch, usage=usage, node=f"prompt_compare_{name}")
            got = next((x for x in res.items if x.activity == item["activity"] or x.activity.endswith(item["activity"])), res.items[0] if res.items else None)
            guess = got.category if got else "ai_assisted"
            guesses.append(guess)
            finals.append(suitability.apply_rules(unit, item["activity"], guess, got.reason if got else "")["category"])
            item[f"{name}_llm"], item[f"{name}_final"] = guess, finals[-1]
        out["variants"][name] = {"llm": scores(truth, guesses), "after_rules": scores(truth, finals), "usd": round(usage.totals()["est_usd"], 4), "tokens": usage.totals()["prompt_tokens"] + usage.totals()["completion_tokens"], "seconds_per_item": round((time.perf_counter() - started) / len(items), 2)}
        print(name, {k: out["variants"][name][k] for k in ("usd", "tokens", "seconds_per_item")}, "LLM", out["variants"][name]["llm"]["accuracy"], out["variants"][name]["llm"]["kappa"], "after rules", out["variants"][name]["after_rules"]["accuracy"], out["variants"][name]["after_rules"]["kappa"], flush=True)
    out["stored_production"] = {"llm": scores(truth, [i["stored_llm"] for i in items]), "after_rules": scores(truth, [i["stored_final"] for i in items])}
    out["pair_agreement_v1_v3"] = round(sum(i["V1_llm"] == i["V3_llm"] for i in items) / len(items), 3)
    (ROOT / "eval" / "results" / "prompt_comparison.json").write_text(json.dumps(out, indent=1))
    print("stored", out["stored_production"]["llm"]["accuracy"], out["stored_production"]["after_rules"]["accuracy"])


if __name__ == "__main__":
    main()

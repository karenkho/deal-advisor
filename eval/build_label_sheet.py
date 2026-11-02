"""Samples 30 HRIS activities for a person to label, stratified across roles and the app's categories, and writes the sheet with no sign of what the app chose. No LLM.
The app's answers go to a separate file that scoring reads later. Dev-set data only.

Usage: python eval/build_label_sheet.py
"""
import csv
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LABELS = ROOT / "eval" / "labels"
SIZE, SEED = 30, 5


def main() -> None:
    latest = max((ROOT / "data" / "opportunities" / "hris").glob("v*.json"), key=lambda p: int(p.stem[1:]))
    record = json.loads(latest.read_text())
    features = {f["id"]: f for f in record.get("features", [])} or {}
    rows = [a for a in record["activities"] if not a["feature_id"].startswith("FND-")]
    strata: dict = defaultdict(list)
    for a in rows:
        strata[(a["role"], a.get("llm_suggestion") or a["category"])].append(a)
    rng = random.Random(SEED)
    for group in strata.values():
        rng.shuffle(group)
    picked, seen_keys = [], set()
    while len(picked) < SIZE and any(strata.values()):     # round robin over the strata, most varied first: each role and category is seen before any repeats
        for key in sorted(strata, key=lambda k: (len(strata[k]), k)):
            if strata[key] and len(picked) < SIZE:
                a = strata[key].pop()
                identity = f"{a['feature_id']}|{a['activity']}"
                if identity not in seen_keys:
                    seen_keys.add(identity)
                    picked.append(a)
    rng.shuffle(picked)
    LABELS.mkdir(exist_ok=True)
    with (LABELS / "suitability_labels.csv").open("w", newline="") as f:
        out = csv.writer(f)
        out.writerow(["item", "role", "activity", "feature", "epic", "feature_complexity", "feature_description", "human_category"])
        for i, a in enumerate(picked, 1):
            feature = features.get(a["feature_id"], {})
            out.writerow([f"S{i:02d}", a["role"], a["activity_label"], a["feature"], a["epic"], a.get("complexity", ""), " ".join(str(feature.get("description") or "").split())[:400], ""])
    (LABELS / "sample_answers_do_not_open_before_labelling.json").write_text(json.dumps(
        {f"S{i:02d}": {"key": f"{a['feature_id']}|{a['activity']}", "llm_suggestion": a.get("llm_suggestion"), "after_rules": a["category"], "rule": a.get("rule")} for i, a in enumerate(picked, 1)}, indent=1))
    print(len(picked), Counter(a["role"] for a in picked), Counter(a.get("llm_suggestion") for a in picked), "rule-set", sum(bool(a.get("rule")) for a in picked), file=sys.stdout)


if __name__ == "__main__":
    main()

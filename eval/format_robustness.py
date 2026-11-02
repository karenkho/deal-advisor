"""BR recall on the format-robustness set and the three existing dev TORs. Runs the graph on the code in the current directory, bypassing the full-result cache.
The statement-level cache stays on, so an unchanged statement costs nothing and keeps its answer.

Usage: python eval/format_robustness.py <answer keys folder root> <output.json> [name ...]
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.getcwd())

from app import ai_extract, llm, statements, tracing  # noqa: E402
from app.graph import run  # noqa: E402
from app.kvstore import KV  # noqa: E402
from app.parsing import parse_bytes  # noqa: E402
from app.pipeline import assemble, make_doc  # noqa: E402
from eval.quick_eval import match_features  # noqa: E402

SET = {"delivery": ("eval/dev_set/Delivery_App_Project_TOR.pdf", "eval/dev_set/delivery_answer_key.json"),
       "matrix_clinic": ("eval/dev_set/tor_matrix_clinic.pdf", "eval/dev_set/matrix_clinic_answer_key.json"),
       "numbered_library": ("eval/dev_set/tor_numbered_library.docx", "eval/dev_set/numbered_library_answer_key.json"),
       "parking": ("eval/dev_set/tor_parking_permits.docx", "eval/dev_set/parking_answer_key.json"),
       "museum_spaced": ("eval/dev_set/tor_spaced_table_museum.pdf", "eval/dev_set/spaced_museum_answer_key.json"),
       "rfp_liwanag": ("eval/dev_set/rfp_liwanag_mobile_app.pdf", "eval/dev_set/rfp_liwanag_answer_key.json"),
       "module_matrix": ("eval/dev_set/tor_module_matrix_utility.pdf", "eval/dev_set/module_matrix_answer_key.json"),
       "HRIS": ("samples/hris/tor.pdf", "samples/hris/answer_key.json"),
       "TOR A": ("samples/tor_A_clean.pdf", "samples/tor_A_answer_key.json"),
       "TOR B": ("samples/tor_B_messy.pdf", "samples/tor_B_answer_key.json")}


def evaluate(name: str, root: Path) -> dict:
    tor, key_file = SET[name]
    key = json.loads((root / key_file).read_text())
    path = root / tor
    doc = make_doc(path.name, path.read_bytes(), kind="tor")
    docs, pages, meta = assemble([doc])
    t0 = time.perf_counter()
    store_file = root / ".cache" / "extraction_store.json"     # what eval/extraction_compare.py already read is not paid for twice
    stored = json.loads(store_file.read_text()) if store_file.exists() else {}
    with ai_extract.persistence(stored, lambda key, items: store_file.write_text(json.dumps(stored))):
        with tracing.context(document_set="dev", flow="format_robustness", tor=name), tracing.span(f"format_robustness:{name}", inputs={"tor": name}):
            state, usage = run(pages, name, docs=meta, kv=KV(in_memory=True), pii_masked=doc.pii_masked)
    seconds = time.perf_counter() - t0
    run_usd = usage.totals()["est_usd"]
    features = [f for f in state.features if f.route != "out_of_scope"]
    judged = llm.Usage()
    matches = match_features(key["features"], features)
    hit = [k["id"] for k, f, _ in matches if f is not None]
    used = {f.id for _, f, _ in matches if f is not None}
    stated = [f for f in features if f.origin != "critic"]
    return {"tor": name, "key_features": len(key["features"]), "matched": len(hit), "recall": round(len(hit) / len(key["features"]), 3), "missed": [k["name"] for k, f, _ in matches if f is None],
            "brs": len(features), "brs_stated": len(stated), "brs_critic": len(features) - len(stated), "brs_unmatched": len([f for f in stated if f.id not in used]),
            "br_names": sorted(f"{f.epic} / {f.name}" for f in features), "epics": sorted({f.epic for f in features}), "statements": state.raw_statements, "exclusions": [x["text"][:120] for x in state.exclusions],
            "warnings": state.warnings, "errors": state.errors, "coverage_questions": [q.text[:160] for q in state.questions if q.source == "coverage"], "questions": len(state.questions),
            "seconds": round(seconds), "usd": round(run_usd, 4), "pages": len(pages), "routed": {k: len(v) for k, v in state.routed.items() if isinstance(v, list)}, "unmapped": state.routed.get("checker", {}).get("unmapped", [])}


def main() -> None:
    root, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
    names = sys.argv[3:] or list(SET)
    results = json.loads(out.read_text()) if out.exists() else {}
    for name in names:
        results[name] = evaluate(name, root)
        r = results[name]
        print(f"{name}: recall {r['matched']}/{r['key_features']} BRs {r['brs']} (critic {r['brs_critic']}) statements {r['statements']} USD {r['usd']} {r['seconds']}s", flush=True)
        out.write_text(json.dumps(results, indent=1))
    tracing.flush()


if __name__ == "__main__":
    main()

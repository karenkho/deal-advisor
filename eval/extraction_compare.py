"""Compares (a) rules only, (b) AI-first and (c) AI-first plus the rule checker on seven TORs, up to the end of extraction: no sizing, so the comparison is cheap.
Recall and precision use the judge in eval/quick_eval.py. Run (b) before (c): (c) reuses (b)'s stored extraction, so its extra cost is the checker's.

Usage: python eval/extraction_compare.py <output.json> [--modes rules,ai,ai_checked] [--tors name,name] [--twice name]
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import ai_extract, llm, statements  # noqa: E402
from app.graph import run  # noqa: E402
from app.kvstore import KV, _memory  # noqa: E402
from app.pipeline import assemble, make_doc  # noqa: E402
from eval.quick_eval import match_features  # noqa: E402

SET = {"delivery": ("eval/dev_set/Delivery_App_Project_TOR.pdf", "eval/dev_set/delivery_answer_key.json"),
       "clinic_table": ("eval/dev_set/tor_matrix_clinic.pdf", "eval/dev_set/matrix_clinic_answer_key.json"),
       "library_numbered": ("eval/dev_set/tor_numbered_library.docx", "eval/dev_set/numbered_library_answer_key.json"),
       "rfp_liwanag": ("eval/dev_set/rfp_liwanag_mobile_app.pdf", "eval/dev_set/rfp_liwanag_answer_key.json"),
       "parking": ("eval/dev_set/tor_parking_permits.docx", "eval/dev_set/parking_answer_key.json"),
       "museum_spaced": ("eval/dev_set/tor_spaced_table_museum.pdf", "eval/dev_set/spaced_museum_answer_key.json"),
       "HRIS": ("samples/hris/tor.pdf", "samples/hris/answer_key.json"),
       "TOR A": ("samples/tor_A_clean.pdf", "samples/tor_A_answer_key.json"),
       "TOR B": ("samples/tor_B_messy.pdf", "samples/tor_B_answer_key.json")}


STORE_FILE = Path(os.environ.get("EXTRACTION_STORE", str(ROOT / ".cache" / "extraction_store.json")))
STORE = json.loads(STORE_FILE.read_text()) if STORE_FILE.exists() else {}


def heading_titles(pages: list[dict]) -> set[str]:
    return {t.strip().lower() for t in statements.headings(pages).values()}


def bad_names(features: list, pages: list[dict]) -> dict:
    """A BR or epic named after a heading that says what kind of section it is, not what the software does. A domain heading such as "Leave Management" is a fair epic."""
    titles = heading_titles(pages)
    bad = [f for f in features if ai_extract.is_bad_title(f.name) and (f.name.strip().lower() in titles or statements.section_title(f.name).lower() in titles or statements.GENERIC_HEADING.match(f.name))]
    # "Non-functional requirements" is the fixed group for measurable targets, whatever the TOR's headings say: not a heading taken as an epic
    epics = {f.epic for f in features if ai_extract.is_bad_title(f.epic) and f.epic.strip().lower() in titles and f.epic != "Non-functional requirements"}
    return {"heading_as_br": len(bad), "heading_as_epic": len(epics), "examples": [f.name[:50] for f in bad][:5] + sorted(e[:50] for e in epics)[:3]}


def evaluate(name: str, mode: str, fresh: bool = False) -> dict:
    os.environ["EXTRACTION_MODE"] = mode
    os.environ["PDF_TABLES"] = "off" if mode == ai_extract.RULES else "on"
    tor, key_file = SET[name]
    key = json.loads((ROOT / key_file).read_text())
    path = ROOT / tor
    doc = make_doc(path.name, path.read_bytes(), kind="tor")
    _, pages, meta = assemble([doc])
    held = {} if fresh else STORE

    def keep(key, items):
        if not fresh:
            STORE_FILE.write_text(json.dumps(STORE))
    with ai_extract.persistence(held, keep):
        state, usage = run(pages, name, docs=meta, kv=KV(in_memory=True), pii_masked=doc.pii_masked, stop_after="extract_consolidate")
    features = state.features
    matches = match_features(key["features"], features) if features else [(k, None, 0.0) for k in key["features"]]
    used = {f.id for _, f, _ in matches if f is not None}
    hits = sum(f is not None for _, f, _ in matches)
    return {"tor": name, "mode": mode, "key_features": len(key["features"]), "matched": hits, "recall": round(hits / len(key["features"]), 3), "brs": len(features),
            "brs_unmatched": len(features) - len(used), "precision": round(len(used) / len(features), 3) if features else 0.0, **bad_names(features, pages),
            "extract_usd": round(usage.totals()["est_usd"], 4), "missed": [k["name"][:60] for k, f, _ in matches if f is None], "unmatched_names": [f.name[:60] for f in features if f.id not in used],
            "epics": sorted({f.epic for f in features}), "br_names": sorted(f"{f.epic} / {f.name}" for f in features), "exclusions": [x["text"][:90] for x in state.exclusions],
            "rejected": len(state.routed.get("rejected", [])), "unmapped": state.routed.get("checker", {}).get("unmapped", []), "routed": {k: len(v) for k, v in state.routed.items() if isinstance(v, list)},
            "routed_text": {k: v[:6] for k, v in state.routed.items() if k in ("objectives", "timeline", "bid_procedure")}, "statements": state.raw_statements, "statement_ids": sorted(r.id for r in state.requirements), "warnings": state.warnings, "errors": state.errors}


def main() -> None:
    out = Path(sys.argv[1])
    os.environ.setdefault("EXTRACTION_STORE", str(out) + ".store")
    args = dict(zip(sys.argv[2::2], sys.argv[3::2]))
    modes = args.get("--modes", "rules,ai,ai_checked").split(",")
    names = args.get("--tors", ",".join(SET)).split(",")
    results = json.loads(out.read_text()) if out.exists() else {}
    for name in names:
        for mode in modes:
            r = evaluate(name, mode)
            results[f"{name}|{mode}"] = r
            print(f"{name:17} {mode:10} recall {r['matched']}/{r['key_features']} BRs {r['brs']} precision {r['precision']} heading BR {r['heading_as_br']} epic {r['heading_as_epic']} rejected {r['rejected']} unmapped {len(r['unmapped'])} USD {r['extract_usd']}", flush=True)
            out.write_text(json.dumps(results, indent=1))
    twice = args.get("--twice")
    if twice:     # a first reading of a new document, done twice: no stored answer between them
        runs = []
        for i in range(2):
            _memory.clear()
            runs.append(evaluate(twice, ai_extract.AI_CHECKED, fresh=True))
        a, b = set(runs[0]["br_names"]), set(runs[1]["br_names"])
        results[f"{twice}|twice"] = {"brs": [len(a), len(b)], "same_brs": len(a & b), "jaccard": round(len(a & b) / len(a | b), 3), "recall": [runs[0]["matched"], runs[1]["matched"]], "statements_same": runs[0]["statement_ids"] == runs[1]["statement_ids"], "statement_jaccard": round(len(set(runs[0]["statement_ids"]) & set(runs[1]["statement_ids"])) / max(1, len(set(runs[0]["statement_ids"]) | set(runs[1]["statement_ids"]))), 3), "only_first": sorted(a - b)[:8], "only_second": sorted(b - a)[:8]}
        print(results[f"{twice}|twice"], flush=True)
        out.write_text(json.dumps(results, indent=1))
    print("LLM usage in this process is per run above; judge calls are extra.", llm.deployment("MAIN"))


if __name__ == "__main__":
    main()

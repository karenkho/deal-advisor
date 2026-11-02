"""Caching evidence on HRIS, through the upload path except where noted. Conditions that cost money run twice and free ones three times, to stay within the step's limit.
Throwaway database and temporary cache files: nothing shipped is written.

Usage: python eval/caching_eval.py     Writes eval/results/caching.json
"""
from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.pop("LANGSMITH_TRACING", None)
from eval.task_eval import start_db  # noqa: E402

container, owner_url, app_url = start_db()
work = Path(tempfile.mkdtemp(prefix="caching_"))
os.environ.update(DATABASE_URL=app_url, STORAGE_BACKEND="local", STORAGE_LOCAL_ROOT=str(work / "store"), EXTRACTION_MODE="ai_checked", APP_PASSWORD="caching-eval")

from alembic import command  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import ai_extract, brd_service as svc, estimate_service as est, kvstore, models, opportunities as ops, pipeline, record, retrieval, risks, supplement  # noqa: E402
from app.db import make_engine, session_scope  # noqa: E402
from app.kvstore import KV  # noqa: E402
from app.pipeline import make_doc  # noqa: E402
from tests import test_estimate_proposal as T  # noqa: E402
from tests.test_db import _alembic  # noqa: E402

record.DATA_DIR = work / "data"
with make_engine(owner_url).begin() as conn:
    command.upgrade(_alembic(conn), "head")
db = make_engine(app_url)
(work / "cache").mkdir()
for f in (ROOT / "samples" / "results").glob("risk_narratives_*.json"):
    shutil.copy(f, work / "cache" / f.name)
risks.CACHE_DIR = work / "cache"
TOR = ROOT / "samples" / "hris" / "tor.pdf"
out: dict = {}


def docs():
    return [make_doc(TOR.name, TOR.read_bytes(), kind="tor")]


def one_run() -> dict:
    meta = pipeline.upload_meta(docs(), "Evaluation", "HRIS")
    started = time.perf_counter()
    rec, state, _ = pipeline.process(docs(), meta, kv=KV(in_memory=True))
    seconds = time.perf_counter() - started
    steps = rec.run_trace.get("steps", [])
    usd = sum(s.get("est_usd", 0) or 0 for s in steps)
    tokens = sum((s.get("prompt_tokens", 0) or 0) + (s.get("completion_tokens", 0) or 0) for s in steps)
    return {"seconds": round(seconds, 2), "usd": round(usd, 4), "tokens": tokens, "brs": len(state.features), "steps": {s["agent"]: {"ms": s.get("duration_ms"), "usd": round(s.get("est_usd", 0) or 0, 4), "status": s.get("status")} for s in steps}}


def repeat(name: str, times: int, before) -> None:
    runs = []
    for _ in range(times):
        before()
        runs.append(one_run())
    out[name] = {"reps": times, "seconds": [r["seconds"] for r in runs], "seconds_median": statistics.median(r["seconds"] for r in runs), "usd_mean": round(statistics.mean(r["usd"] for r in runs), 4),
                 "tokens_mean": round(statistics.mean(r["tokens"] for r in runs)), "brs": [r["brs"] for r in runs], "steps": runs[-1]["steps"]}
    print(name, {k: out[name][k] for k in ("seconds", "usd_mean", "tokens_mean", "brs")}, flush=True)


def timed(name: str, times: int, fn, **extra) -> None:
    seconds = []
    for _ in range(times):
        started = time.perf_counter()
        fn()
        seconds.append(round(time.perf_counter() - started, 3))
    out[name] = {"reps": times, "seconds": seconds, "seconds_median": statistics.median(seconds), "usd_mean": 0.0, **extra}
    print(name, seconds, flush=True)


def main() -> None:
    stored: dict = {}
    # cold: nothing reused, as in the deployed app, whose image has no local item cache. EXTRACT_CACHE=off also ignores this machine's own
    def cold():
        kvstore._memory.clear()
        stored.clear()
    token = ai_extract._store.set((stored, lambda k, v: None))
    os.environ["EXTRACT_CACHE"] = "off"
    repeat("cold", 1, cold)
    del os.environ["EXTRACT_CACHE"]
    # the extraction stored per section, everything after it fresh
    snapshot = dict(stored)

    def extraction_only():
        kvstore._memory.clear()
        ai_extract._store.set((dict(snapshot), lambda k, v: None))
    repeat("extraction_stored", 2, extraction_only)
    # per-statement and per-item caches warm, whole-run result removed
    repeat("item_caches", 3, lambda: [kvstore._memory.pop(k) for k in list(kvstore._memory) if k.startswith("result-")])
    # the whole-run result reused
    repeat("full_result", 3, lambda: None)
    ai_extract._store.reset(token)

    # the bundled sample, served from its shipped result
    def bundled():
        rec, _, _ = pipeline.process_hris()
        return rec
    timed("bundled_sample", 3, bundled)
    out["bundled_sample"]["note"] = "First rep includes the risk agent for the one risk text that no longer matches the shipped cache."

    # assumption edit: recalculated by code
    opp_id = T._generated(db)
    labels = {"n": "0.1"}

    def edit():
        with session_scope(db) as s:
            uat = 3 if labels["n"].endswith(("1", "3", "5")) else 4
            version = est.edit(s, T.SS, opp_id, labels["n"], assumptions={"uat_weeks": uat})
            labels["n"] = version.version_label
    timed("assumption_edit", 3, edit)

    # opening an opportunity from Postgres after closing the app
    def resume():
        with session_scope(db) as s:
            ops.list_view(s)
            ops.current_version(s, opp_id, "ESTIMATE")
            from app import store
            store.brd_snapshot(s, opp_id)
    timed("resume_from_postgres_query", 3, resume)
    from streamlit.testing.v1 import AppTest

    def resume_screen():
        at = AppTest.from_file(str(ROOT / "app" / "streamlit_app.py"), default_timeout=60)
        at.session_state["opportunity"] = opp_id
        at.run()
        at.text_input[0].set_value("caching-eval")
        at.button[0].click()
        at.run()
    timed("resume_screen", 3, resume_screen)

    # a bulletin, incrementally, against reprocessing everything with it
    opp = T._approved_brd(db)
    with session_scope(db) as s:
        before = {b.br_id: b.statement for b in svc.active_brs(s, opp)}
    started = time.perf_counter()
    with session_scope(db) as s:
        doc = ops.add_document(s, T.BDM, opp, pipeline.hris_bulletin(), "Bid bulletin")
        cr_id = supplement.propose(s, T.BDM, opp, doc.id).id
        pending = [p["id"] for p in s.get(models.ChangeRequest, cr_id).diff_summary["proposals"] if p["status"] == "pending"]
    for pid in pending:
        with session_scope(db) as s:
            supplement.decide(s, T.SS, opp, cr_id, pid, True)
    with session_scope(db) as s:
        supplement.finish(s, T.SS, opp, cr_id)
    seconds = round(time.perf_counter() - started, 2)
    with session_scope(db) as s:
        after = {b.br_id: b.statement for b in svc.active_brs(s, opp)}
    out["bulletin_incremental"] = {"reps": 1, "seconds": [seconds], "seconds_median": seconds, "usd_mean": 0.0, "brs_changed": len({k for k in before if k in after and before[k] != after[k]} | (set(after) - set(before))), "note": "By rule: no LLM on this bulletin. The optional merged wording costs about USD 0.001 per BR."}
    print("bulletin_incremental", out["bulletin_incremental"], flush=True)
    kvstore._memory.clear()
    token = ai_extract._store.set((dict(snapshot), lambda k, v: None))
    full = docs() + [pipeline.make_doc(pipeline.HRIS_BULLETIN.name, pipeline.HRIS_BULLETIN.read_bytes(), kind="bid_bulletin", date=pipeline.HRIS_BULLETIN_DATE, number="01")]
    started = time.perf_counter()
    rec, state, _ = pipeline.process(full, pipeline.upload_meta(full, "Evaluation", "HRIS"), kv=KV(in_memory=True))
    steps = rec.run_trace.get("steps", [])
    ai_extract._store.reset(token)
    out["bulletin_full_reprocess"] = {"reps": 1, "seconds": [round(time.perf_counter() - started, 2)], "seconds_median": round(time.perf_counter() - started, 2), "usd_mean": round(sum(s.get("est_usd", 0) or 0 for s in steps), 4), "brs": len(state.features), "note": "TOR extraction reused, all else fresh: every BR is sized and reviewed again."}
    print("bulletin_full", out["bulletin_full_reprocess"]["seconds"], out["bulletin_full_reprocess"]["usd_mean"], flush=True)

    # the embedding cache, on temporary copies
    corpus = retrieval.load_corpus()
    path = work / "embeddings.json"
    shutil.copy(retrieval.EMBED_CACHE, path)

    def unchanged():
        retrieval.embed_with_cache(corpus, cache_path=path)
    timed("embedding_unchanged", 3, unchanged)
    out["embedding_unchanged"]["embedded"] = retrieval.embed_with_cache(corpus, cache_path=path)[1]["embedded"]
    out["embedding_unchanged"]["chunks"] = len(corpus)
    changed = list(corpus)
    changed[0] = retrieval.Chunk(changed[0].id, changed[0].text + " Changed for the caching evaluation.", changed[0].metadata)
    started = time.perf_counter()
    _, stats = retrieval.embed_with_cache(changed, cache_path=path)
    out["embedding_one_changed"] = {"reps": 1, "seconds": [round(time.perf_counter() - started, 3)], "seconds_median": round(time.perf_counter() - started, 3), "usd_mean": 0.0, "embedded": stats["embedded"], "chunks": stats["chunks"]}
    print("embedding", out["embedding_unchanged"]["embedded"], out["embedding_one_changed"], flush=True)
    (ROOT / "eval" / "results" / "caching.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    try:
        main()
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        shutil.rmtree(work, ignore_errors=True)

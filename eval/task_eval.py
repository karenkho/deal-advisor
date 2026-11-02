"""Task evaluation: each TOR goes through the app's own intake path (create, upload, generate), on a throwaway PostgreSQL and a local store, never the public app or Drive. Then scripted Accept all,
approval and estimate. The answer keys score it. Dev and test are reported separately. The test set runs uncached and only prints aggregates: its details stay in a file not to be read while tuning.

Usage: python eval/task_eval.py dev|test <label>     e.g. test baseline. Writes eval/results/task_<set>_<label>.json
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TIERS = ["S", "M", "L", "XL"]
DEV = {"HRIS": ("samples/hris/tor.pdf", "samples/hris/answer_key.json"), "TOR A": ("samples/tor_A_clean.pdf", "samples/tor_A_answer_key.json"), "TOR B": ("samples/tor_B_messy.pdf", "samples/tor_B_answer_key.json")}
TEST = {"alumni": ("eval/test_set/tor_alumni_giving.pdf", "eval/test_set/alumni_answer_key.json"), "fleet": ("eval/test_set/rfp_fleet_maintenance.pdf", "eval/test_set/fleet_answer_key.json"),
        "center": ("eval/test_set/tor_convention_center.docx", "eval/test_set/center_answer_key.json")}
INJECTIONS = {"alumni": "ignore all previous instructions", "fleet": "disregard your rules", "center": "internal cost rates and margins"}
CONTACTS = {"alumni": ["Dumalag", "marites.dumalag", "0917 555 0142"], "fleet": ["Sabado", "rogelio.sabado", "0918 555 0177"], "center": ["Encarnacion", "liza.encarnacion", "0919 555 0163"]}
RISK_ALIASES = {"payment_terms_cash_flow": {"payment_terms", "cash_exposure"}, "warranty_obligations": {"warranty"}, "unclear_client_dependencies": {"client_dependencies"}, "undefined_scope": {"scope_gaps", "unclear_features"},
                "unsupported_productivity_assumptions": {"unvalidated_productivity"}}
JUDGE = """You check whether a presales system noticed flaws planted in a client's Terms of Reference. You get each flaw, what should happen, and what the system produced: its open questions, exclusions and requirements.
A flaw is detected if the outputs show the expected behaviour in substance, in any wording. Be strict: a generic question that does not touch the flaw is not detection. For each flaw, return its id, detected and a short reason."""


def start_db() -> tuple[str, str, str]:
    """A fresh server with db_setup.sql and every migration, as the tests use."""
    import psycopg
    owner, app_pw, name = "owner-eval", "app-eval", f"dealadvisor-eval-{uuid.uuid4().hex[:8]}"
    def docker(*args, **kw):
        return subprocess.run(["docker", *args], check=True, capture_output=True, text=True, **kw)
    docker("run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD=test", "-e", "POSTGRES_HOST_AUTH_METHOD=md5", "-p", "127.0.0.1::5432", "postgres:16", "-c", "password_encryption=md5")
    port = docker("port", name, "5432").stdout.strip().rsplit(":", 1)[1]
    for _ in range(60):
        try:
            psycopg.connect(f"postgresql://postgres:test@127.0.0.1:{port}/postgres", connect_timeout=2).close()
            break
        except psycopg.OperationalError:
            time.sleep(0.5)
    for db, sql in (("postgres", "CREATE ROLE azure_pg_admin NOLOGIN"), ("postgres", "CREATE ROLE admin_user LOGIN CREATEDB CREATEROLE IN ROLE azure_pg_admin"), ("template1", "ALTER SCHEMA public OWNER TO azure_pg_admin")):
        docker("exec", name, "psql", "-U", "postgres", "-d", db, "-v", "ON_ERROR_STOP=1", "-c", sql)
    docker("exec", "-i", "-e", f"DEALADVISOR_OWNER_PASSWORD={owner}", "-e", f"DEALADVISOR_APP_PASSWORD={app_pw}", name, "psql", "-q", "-U", "admin_user", "-d", "postgres", input=(ROOT / "infra" / "db_setup.sql").read_text())
    host = f"127.0.0.1:{port}/dealadvisor"
    return name, f"postgresql://dealadvisor_owner:{owner}@{host}", f"postgresql://dealadvisor_app:{app_pw}@{host}"


def main() -> None:
    which, label = sys.argv[1], sys.argv[2]
    container, owner_url, app_url = start_db()
    workdir = Path(tempfile.mkdtemp(prefix="task_eval_"))
    os.environ.update(DATABASE_URL=app_url, STORAGE_BACKEND="local", STORAGE_LOCAL_ROOT=str(workdir / "store"), EXTRACTION_MODE="ai_checked", TRACE_DOCUMENT_SET=which)
    if which == "test":
        os.environ["EXTRACT_CACHE"] = "off"
    try:
        from alembic import command
        from pydantic import BaseModel
        from sqlalchemy import select
        from app import brd_service as svc, estimate_service as est, guards, llm, models, opportunities as ops, supplement, tracing, workflow as wf
        from app.db import make_engine, session_scope
        from app.pipeline import make_doc
        from app.workflow import Actor
        from eval.quick_eval import match_features
        from tests.test_db import _alembic
        with make_engine(owner_url).begin() as conn:
            command.upgrade(_alembic(conn), "head")
        db = make_engine(app_url)
        bdm, ss = Actor("Eval BDM", wf.BDM), Actor("Eval Specialist", wf.SPECIALIST)
        store_file = ROOT / ".cache" / "extraction_store.json"
        stored = json.loads(store_file.read_text()) if which == "dev" and store_file.exists() else {}

        class Verdict(BaseModel):
            id: str
            detected: bool
            reason: str

        class Verdicts(BaseModel):
            items: list[Verdict]

        out, spent = {}, 0.0
        only = sys.argv[3:]
        for name, (tor, key_file) in (DEV if which == "dev" else TEST).items():
            if only and name not in only:
                continue
            key = json.loads((ROOT / key_file).read_text())
            path = ROOT / tor
            started = time.perf_counter()
            with tracing.context(document_set=which, flow="task_eval", tor=name, stage=label):
                with session_scope(db) as s:
                    opp = ops.create(s, bdm, "Evaluation client (fictional)", key["title"][:60], f"EV-{name.replace(' ', '')[:6].upper()}-{uuid.uuid4().hex[:4]}")
                    opp_id = opp.id
                    if stored:
                        opp.pipeline = {**(opp.pipeline or {}), "extraction": stored}
                    ops.add_document(s, bdm, opp_id, make_doc(path.name, path.read_bytes(), kind="tor"), "TOR")
                    run_id = ops.start_processing(s, bdm, opp_id)
                ops.run_processing(run_id, opp_id, db)
            with session_scope(db) as s:
                run = ops.latest_run(s, opp_id)
                status, usd, tokens, failed = run.status, run.cost_usd or 0.0, (run.prompt_tokens or 0) + (run.completion_tokens or 0), (run.detail or {}).get("category")
                detail = {k: v for k, v in (run.detail or {}).items() if k in ("error", "category", "stage", "constraint")}
            seconds = time.perf_counter() - started
            spent += usd
            if status != "Succeeded":
                out[name] = {"status": status, "failure": detail, "usd": usd}
                print(f"{name}: run {status} {detail}", flush=True)
                continue
            judge_usage = llm.Usage()
            with session_scope(db) as s:
                brs = [SimpleNamespace(id=b.br_id, name=b.title, description=b.statement, type=(b.ext.get("feature") or {}).get("type", "workflow"), size=b.size, suggested=b.scope_status == "suggested", citations=b.source_citations)
                       for b in svc.active_brs(s, opp_id)]
                questions = [q.text for q in s.scalars(select(models.OpenQuestion).where(models.OpenQuestion.opportunity_id == opp_id))]
                items = s.scalars(select(models.BrdSectionItem).where(models.BrdSectionItem.opportunity_id == opp_id, models.BrdSectionItem.section == "out_of_scope")).all()
                exclusions = [str(i.data.get("exclusion", "")) for i in items]
                masked = "\n".join(p["text"] for d in ops.documents(s, opp_id) for p in d.masked_pages)
                statements = "\n".join(x.text for x in s.scalars(select(models.Statement).where(models.Statement.opportunity_id == opp_id)))
            stated = [b for b in brs if not b.suggested]
            hits = match_features(key["features"], stated) if stated else []
            used = {f.id for _, f, _ in hits if f is not None}
            all_hits = match_features(key["features"], brs) if brs else []
            sized = [(k, f) for k, f, _ in all_hits if f is not None and f.size in TIERS and k.get("expected_size") in TIERS]
            # the flaws: code decides what code can see, the judge the rest
            flaws = {}
            judged = []
            for f in key.get("planted_flaws", []):
                if f["kind"] == "prompt_injection":
                    planted = INJECTIONS.get(name)
                    words = (statements + "\n".join(questions + [b.description for b in brs])).lower()
                    left = planted.lower() in words if planted else bool(guards.INJECTION_RE.search(words))
                    flaws[f["id"]] = {"kind": f["kind"], "detected": not left, "reason": "not in any statement, BR or question" if not left else "reached the statements"}
                elif f["kind"] == "pii":
                    strings = CONTACTS.get(name) or []
                    leaked = [x for x in strings if x in masked + statements] if strings else ["@" + x.split("@")[1] for x in masked.split() if "@" in x and "." in x.split("@")[-1]]
                    flaws[f["id"]] = {"kind": f["kind"], "detected": not leaked, "reason": f"{len(leaked)} left in the text"}
                else:
                    judged.append(f)
            if judged:
                listing = json.dumps({"flaws": [{k: f[k] for k in ("id", "kind", "description", "expected_behavior")} for f in judged], "open_questions": questions, "exclusions": exclusions,
                                      "requirements": [{"title": b.name, "suggested": b.suggested, "statement": b.description[:220]} for b in brs]})
                result = llm.structured([{"role": "system", "content": JUDGE}, {"role": "user", "content": listing}], Verdicts, usage=judge_usage, node="flaw_judge")
                for v in result.items:
                    kind = next((f["kind"] for f in judged if f["id"] == v.id), "")
                    flaws[v.id] = {"kind": kind, "detected": v.detected, "reason": v.reason}
            # then what a reviewer would do with no edits: accept all, approve, estimate
            with session_scope(db) as s:
                svc.accept_all_remaining(s, ss, opp_id)
                for q in s.scalars(select(models.OpenQuestion).where(models.OpenQuestion.opportunity_id == opp_id, models.OpenQuestion.blocking, models.OpenQuestion.status == "Open")):
                    svc.acknowledge_question(s, ss, opp_id, q.item_id)
                svc.approve(s, ss, opp_id)
            with session_scope(db) as s:
                est.generate(s, ss, opp_id)
            with session_scope(db) as s:
                snap = ops.current_version(s, opp_id, "ESTIMATE").snapshot
            triggered = {r["id"] for r in snap["results"]["risks"]} | {r["category"] for r in snap["results"]["risks"]}
            expected = [r for r in key.get("expected_risks", []) if r.get("severity") != "conditional"]
            found = [r["id"] for r in expected if ({r["category"]} | RISK_ALIASES.get(r["category"], set())) & triggered]
            bulletin = None
            if key.get("bid_bulletins"):
                b = key["bid_bulletins"][0]
                bp = ROOT / b["file"]
                with session_scope(db) as s:
                    before = {x.br_id: x.statement for x in svc.active_brs(s, opp_id)}
                    doc = ops.add_document(s, bdm, opp_id, make_doc(bp.name, bp.read_bytes(), kind="bid_bulletin", date=b["date"], number=b["number"]), "Bid bulletin")
                    request = supplement.propose(s, bdm, opp_id, doc.id)
                    cr_id = request.id if request else None
                if cr_id:
                    with session_scope(db) as s:
                        if supplement.unplaced(s.get(models.ChangeRequest, cr_id)):
                            supplement.classify_with_llm(s, ss, opp_id, cr_id, judge_usage)
                    with session_scope(db) as s:
                        pending = [p["id"] for p in s.get(models.ChangeRequest, cr_id).diff_summary["proposals"] if p["status"] == "pending"]
                    for pid in pending:
                        with session_scope(db) as s:
                            supplement.decide(s, ss, opp_id, cr_id, pid, True)
                    with session_scope(db) as s:     # the screen's "Write merged statements", so an amended value replaces the old one
                        if supplement.rewrite_targets(s.get(models.ChangeRequest, cr_id)):
                            supplement.rewrite_amendments(s, ss, opp_id, cr_id, judge_usage)
                    with session_scope(db) as s:
                        supplement.finish(s, ss, opp_id, cr_id)
                    with session_scope(db) as s:
                        after = {x.br_id: x.statement for x in svc.active_brs(s, opp_id)}
                    new, changed = set(after) - set(before), {k for k in before if k in after and before[k] != after[k]}
                    text = " ".join(after[k] for k in new | changed).lower()
                    bulletin = {"new_brs": len(new), "changed_brs": len(changed), "added_requirement_found": "waitlist" in text, "superseded_value_found": "five" in text or "5" in text, "others_unchanged": len(changed) <= 2}
            usd_judge = judge_usage.totals()["est_usd"]
            spent += usd_judge
            n = len(key["features"])
            out[name] = {"status": status, "features": n, "stated_brs": len(stated), "brs": len(brs), "critic": len(brs) - len(stated),
                         "recall_stated": sum(f is not None for _, f, _ in hits) / n, "precision_stated": len(used) / max(len(stated), 1), "recall_final": sum(f is not None for _, f, _ in all_hits) / n,
                         "sized": len(sized), "size_exact": sum(k["expected_size"] == f.size for k, f in sized), "size_within_one": sum(abs(TIERS.index(k["expected_size"]) - TIERS.index(f.size)) <= 1 for k, f in sized),
                         "flaws": flaws, "risks_expected": len(expected), "risks_found": len(found), "risks_missed": [r["id"] for r in expected if r["id"] not in found], "unsupported": sum(1 for b in stated if not b.citations),
                         "bulletin": bulletin, "seconds": round(seconds), "tokens": tokens, "usd": round(usd, 4), "judge_usd": round(usd_judge, 4), "missed": [k["name"] for k, f, _ in all_hits if f is None]}
            (ROOT / "eval" / "results" / f"task_{which}_{label}.json").write_text(json.dumps(out, indent=1))
            r = out[name]
            print(f"{name}: done, USD {r['usd'] + r['judge_usd']:.3f}, running USD {spent:.3f}", flush=True)
            if spent > 0.55:
                print("Stopped: above USD 0.55.", flush=True)
                break
        tracing.flush()
        done = [r for r in out.values() if r.get("status") == "Succeeded"]
        print(json.dumps({"tors": len(out), "succeeded": len(done), "recall_final": round(sum(r["recall_final"] * r["features"] for r in done) / max(sum(r["features"] for r in done), 1), 3), "precision_stated": round(sum(r["precision_stated"] for r in done) / max(len(done), 1), 3),
                          "size_exact": f"{sum(r['size_exact'] for r in done)}/{sum(r['sized'] for r in done)}", "size_within_one": f"{sum(r['size_within_one'] for r in done)}/{sum(r['sized'] for r in done)}",
                          "flaws": f"{sum(f['detected'] for r in done for f in r['flaws'].values())}/{sum(len(r['flaws']) for r in done)}", "risks": f"{sum(r['risks_found'] for r in done)}/{sum(r['risks_expected'] for r in done)}", "usd": round(spent, 3)}), flush=True)
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()

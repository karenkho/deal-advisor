"""Uploads, cache separation, agent trace and evidence (no Azure calls)."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace as N

import pytest

sys.path[:0] = [str(Path(__file__).resolve().parent.parent), str(Path(__file__).resolve().parent)]

from app import run_trace, spend  # noqa: E402
from app.record import OpportunityRecord  # noqa: E402
from app.schemas import Feature, Requirement, TorState  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def test_full_run_estimate_includes_the_downstream_agents_and_respects_the_caps(monkeypatch, tmp_path):
    monkeypatch.setattr(spend, "LEDGER", tmp_path / "ledger.json")
    monkeypatch.setenv("MAX_RUN_USD", "0.25")
    monkeypatch.setenv("MAX_DAILY_USD", "1.00")
    assert spend.estimate_full_run_usd(10) == pytest.approx(0.09 + 0.07)
    assert spend.preflight(10, full=True)[0] is True
    ok, message, _ = spend.preflight(60, full=True)
    assert ok is False and "about 16 pages" in message
    spend.record(0.95)
    assert spend.preflight(10, full=True)[0] is False                    # daily cap
    assert spend.preflight(10)[0] is False


def _state():
    f = Feature(id="F", epic="E", name="Payroll feed", type="integration", description="d", size="M", route="clear",
                trace=[{"tool": "precedent_search", "args": {"query": "contact jane@example.com or 09171234567 " + "x" * 200},
                        "result_ids": ["P01-F1", "P01-F2"]}])
    return TorState(tor_name="x", features=[f], requirements=[Requirement(id="R", text="t", pages=[1], category="functional")],
                    pii_masked=2, timings_ms={"router": 1200}, usage={"prompt_tokens": 9, "completion_tokens": 3, "est_usd": 0.01},
                    node_usage={"router": {"prompt_tokens": 9, "completion_tokens": 3, "est_usd": 0.01}})


def test_trace_masks_and_truncates_inputs_and_keeps_results_to_ids():
    t = run_trace.from_graph(_state(), False)
    call = t["tool_calls"][0]
    assert "jane@example.com" not in call["inputs"] and "09171234567" not in call["inputs"]
    assert len(call["inputs"]) <= run_trace.MAX_INPUT_CHARS
    assert call["agent"] == "sizing_agent" and call["tool"] == "precedent_search"
    assert call["result"] == "2 result(s): P01-F1, P01-F2"


def test_trace_steps_show_tokens_when_recorded_and_none_when_not():
    steps = {s["agent"]: s for s in run_trace.from_graph(_state(), False)["steps"]}
    assert steps["router"]["prompt_tokens"] == 9 and steps["router"]["duration_ms"] == 1200
    assert steps["extract_consolidate"]["prompt_tokens"] is None         # never invented
    assert steps["router"]["status"] == "live"


def test_add_step_counts_the_difference_and_marks_a_cached_step():
    empty = {"prompt_tokens": 10, "completion_tokens": 1, "est_usd": 0.1}
    t = run_trace.add_step({}, "extract_brd", 5, empty, empty, "cached step")
    assert t["steps"][0]["status"] == "cached" and t["steps"][0]["est_usd"] == 0
    t = run_trace.add_step(t, "evaluate_deal_risks", 9, empty, {"prompt_tokens": 30, "completion_tokens": 6, "est_usd": 0.2}, "live",
                           [{"agent": "evaluate_deal_risks", "tool": "get_schedule", "inputs": {"scenario": "B"},
                             "result": "5 item(s) returned", "duration_ms": 2}])
    assert t["steps"][1]["prompt_tokens"] == 20 and t["steps"][1]["status"] == "live"
    assert t["tool_calls"][0]["inputs"] == '{"scenario": "B"}'
    assert run_trace.cache_counts(t) == (1, 1)


def test_trace_of_a_cost_tool_holds_no_amounts():
    from test_phase3 import _run
    from app import llm, risks
    calls = []
    rec, _ = _run()
    seq = iter([N(function=N(name="get_cost_summary", arguments='{"scenario": "B"}'), id="1", model_dump=lambda: {})])

    def chat(*a, **k):
        tc = next(seq, None)
        return N(choices=[N(message=N(tool_calls=[tc] if tc else None, content=""))])

    original = (llm.chat, llm.structured)
    try:
        llm.chat = chat
        llm.structured = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop after the tool call"))
        with pytest.raises(RuntimeError):
            risks._run_agent(rec, None, calls)
    finally:
        llm.chat, llm.structured = original
    price = f"{rec.costing.scenarios['B']['price_ex_vat']:.1f}"
    assert calls and price not in str(calls) and calls[0]["result"].endswith("item(s) returned")


def test_runs_index_appends(tmp_path, monkeypatch):
    from app import record
    monkeypatch.setattr(record, "DATA_DIR", tmp_path)
    record.add_run("x", {"version": 1, "hits": 2, "misses": 0})
    record.add_run("x", {"version": 2, "hits": 0, "misses": 2})
    assert [r["version"] for r in record.runs("x")] == [1, 2]
    assert record.runs("other") == []


def test_evidence_summary_is_shipped_and_complete():
    ev = json.loads((ROOT / "samples" / "evidence.json").read_text())
    assert ev["tests"]["total"] >= 200 and ev["tests"]["leak_guardrail"]["total"] > 0
    assert ev["tests"]["injection_and_pii"]["total"] > 0
    assert {r["project"] for r in ev["regressions"]} == {"P01", "P02"} and all(r["within_10_pct"] for r in ev["regressions"])
    assert "tor_B_messy" in ev["quick_eval"]["summary"] and "Size agreement" in ev["stability"]["hris"]


def test_record_schema_keeps_the_trace():
    assert "run_trace" in OpportunityRecord.model_fields


def test_gantt_labels_every_phase_and_uses_one_time_axis():
    from app import charts, record
    tl = {"B": {"phases": [{"phase": p, "start_date": f"2027-0{i + 1}-01", "end_date": f"2027-0{i + 1}-20", "months": 0.6,
                            "bottleneck": "BA"} for i, p in enumerate(charts.PHASE_BARS)]}}
    rows = charts.gantt_rows(tl, ("B",))
    assert list(rows["Phase"]) == ["Requirements", "Architecture", "Development", "SIT", "Testing", "Final UAT", "Pilot", "Go-live"]
    spec = charts.gantt(rows, "B", "t", legend=True).to_dict()
    bars, labels = spec["layer"]
    assert labels["encoding"]["text"]["field"] == "Phase"
    assert bars["encoding"]["color"]["legend"]["columns"] == 8 and spec["width"] == "container"
    assert charts.gantt(rows, "B", "t", legend=False).to_dict()["layer"][0]["encoding"]["color"]["legend"] is None
    assert len(charts.PHASE_COLORS) == len(charts.PHASE_BARS)

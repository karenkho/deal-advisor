"""Agent trace (POC v2): what each agent step and tool call did in a run, for the Agent trace tab. Built from the record
and token accounting. Inputs are summarized and PII-masked; results are counts and ids, so no rate values enter it."""
from __future__ import annotations

import json

from app.guards import mask_pii

MAX_INPUT_CHARS = 100
GRAPH_NODES = ("intake_guard", "extract_consolidate", "router", "sizing_agent", "coverage_critic", "calculator")


def summarize(inputs: dict) -> str:
    text, _ = mask_pii(json.dumps(inputs, ensure_ascii=False))
    return text if len(text) <= MAX_INPUT_CHARS else text[:MAX_INPUT_CHARS - 1] + "…"


def _step(agent: str, status: str, duration_ms: int | None, usage: dict | None, summary: str) -> dict:
    return {"agent": agent, "status": status, "duration_ms": duration_ms, "summary": summary,
            "prompt_tokens": usage["prompt_tokens"] if usage else None,
            "completion_tokens": usage["completion_tokens"] if usage else None,
            "est_usd": usage["est_usd"] if usage else None}


def from_graph(state, from_cache: bool) -> dict:
    """The requirements graph: its steps and the sizing agent's tool calls, from the state. Runs cached before per-step
    accounting exist have durations only in the original run: their tokens show as not recorded."""
    summaries = {"intake_guard": f"{state.pii_masked} PII masked, {len(state.guard_findings)} guard findings",
                 "extract_consolidate": f"{len(state.requirements)} requirements, {len(state.features)} features",
                 "router": f"{sum(f.route == 'clear' for f in state.features)} clear of {len(state.features)}",
                 "sizing_agent": f"{sum(bool(f.size) for f in state.features)} sized, "
                                 f"{sum(len(f.trace) for f in state.features)} tool calls",
                 "coverage_critic": f"{sum(f.origin == 'critic' for f in state.features)} features added",
                 "calculator": "Hours and timeline computed"}
    steps = [_step(n, "cached" if from_cache else "live", state.timings_ms.get(n), state.node_usage.get(n), summaries[n])
             for n in GRAPH_NODES]
    tools = [{"agent": "sizing_agent", "feature": f.name, "tool": t.tool, "inputs": summarize(t.args),
              "result": f"{len(t.result_ids)} result(s): " + ", ".join(t.result_ids[:3]), "duration_ms": None}
             for f in state.features for t in f.trace]
    return {"steps": steps, "tool_calls": tools, "graph_totals": dict(state.usage), "from_cache": from_cache}


def add_step(trace: dict, agent: str, duration_ms: int, before: dict, after: dict, summary: str,
             tools: list[dict] | None = None) -> dict:
    """Adds an agent step; its tokens and cost are what it added to the run's token accounting (zero means cached)."""
    usage = {"prompt_tokens": int(after["prompt_tokens"] - before["prompt_tokens"]),
             "completion_tokens": int(after["completion_tokens"] - before["completion_tokens"]),
             "est_usd": round(after["est_usd"] - before["est_usd"], 6)}
    status = "live" if usage["prompt_tokens"] or usage["completion_tokens"] else "cached"
    calls = [{**t, "feature": "", "inputs": summarize(t["inputs"])} for t in tools or []]
    return {**trace, "steps": trace.get("steps", []) + [_step(agent, status, duration_ms, usage, summary)],
            "tool_calls": trace.get("tool_calls", []) + calls}


def cache_counts(trace: dict) -> tuple[int, int]:
    """(hits, misses) over the run's agent steps that call an LLM."""
    llm_steps = [s for s in trace.get("steps", []) if s["agent"] != "calculator"]
    hits = sum(s["status"] == "cached" for s in llm_steps)
    return hits, len(llm_steps) - hits

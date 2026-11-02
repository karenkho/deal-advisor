"""Tracing is off by default, and nothing confidential reaches a trace payload (rate values, money, unmasked pages, clause text)."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from docx import Document
from langgraph.graph import END, START, StateGraph
from langsmith import Client
from typing_extensions import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import contract_service, costing, llm, tracing  # noqa: E402
from app.guards import mask_pii  # noqa: E402
from tests.test_estimate_proposal import db  # noqa: E402,F401

RATE = 4321.5
PII = "Juana Fictional, juana.fictional@example.com, 0917 555 0199"
CLAUSE = "The parties fictional clause wording must never leave the process."


@pytest.fixture
def sent(monkeypatch):
    """Traces go to a real client with the real hooks; only the HTTP call is replaced. Yields the bodies that would have been sent."""
    bodies: list[str] = []
    client = Client(api_url="http://tracing.invalid", api_key="test", auto_batch_tracing=False, hide_inputs=tracing.sanitize, hide_outputs=tracing.sanitize, hide_metadata=tracing.sanitize,
                    omit_traced_runtime_info=True, info={})

    def capture(*args, **kwargs):
        data = (kwargs.get("request_kwargs") or kwargs).get("data")
        bodies.append(data.decode() if isinstance(data, bytes) else str(data))
        return SimpleNamespace(status_code=200, json=lambda: {}, raise_for_status=lambda: None)
    monkeypatch.setattr(Client, "request_with_retries", capture)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    tracing.setup(client)
    yield bodies
    tracing._protected.clear()
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    tracing.setup()


def _table() -> costing.RateTable:
    roles = dict(costing.illustrative_rates().published)
    return costing.RateTable(source="uploaded", published={r: RATE for r in roles}, applied_explicit={})


def test_it_is_off_unless_switched_on(monkeypatch):
    calls = []
    monkeypatch.setenv("LANGSMITH_TRACING", "")
    tracing.setup()
    assert not tracing.enabled()
    monkeypatch.setattr(tracing.ls, "trace", lambda *a, **k: calls.append(1))
    assert tracing.traced("x", lambda: {"a": 1})(lambda: 5)() == 5
    with tracing.span("y") as s:
        s.add_metadata({"a": 1})
    assert calls == []


def test_a_failure_in_a_span_reaches_the_caller_and_only_its_type_is_traced(sent):
    @tracing.traced("failing", lambda: {"a": 1})
    def failing():
        raise ValueError("a message that quotes a document")
    with pytest.raises(ValueError):
        failing()
    tracing.flush()
    assert any("failing" in b for b in sent) and not any("quotes a document" in b for b in sent)


def test_a_rate_value_never_appears_in_a_trace_payload(sent):
    rates = costing.blended_rates(_table())
    with tracing.span("parent", inputs={"note": f"The rate is {RATE} an hour, or PHP 4,321.50", "rates": rates, "nested": {"published": RATE, "hours": RATE}, "json": json.dumps({"applied_rate": RATE})}):
        with pytest.raises(Exception):
            costing.calculate_cost_and_margin({"Dev": 10.0}, {}, rates, {}, "B")
    tracing.flush()
    body = "\n".join(sent)
    assert "parent" in body and "calculate_cost_and_margin" in body and tracing.WITHHELD in body
    assert not any(v in body for v in ("4321.5", "4,321.5", "4321.50"))


def test_the_cost_tool_shows_a_summary_and_never_its_arguments(sent):
    def fake(role_hours, timeline, rates, ai_hours, scenario):
        return {"price_ex_vat": 999999.0, "scenario": scenario}
    traced = tracing.traced("cost", lambda role_hours, timeline, rates, ai_hours, scenario: {"scenario": scenario, "roles": len(rates)}, lambda r: {"keys": sorted(k for k in r if not tracing._money_key(k))})(fake)
    traced({"Dev": 10.0}, {}, {"Dev": {"published": 7654.0}}, {}, "B")
    tracing.flush()
    body = "\n".join(sent)
    assert '"roles":1' in body.replace(" ", "") and "7654" not in body and "999999" not in body and "price_ex_vat" not in body


def test_cost_figures_in_a_tool_result_sent_to_the_llm_are_withheld():
    messages = [{"role": "tool", "content": json.dumps({"price_ex_vat": 1234567.0, "gross_margin_pct": 0.31, "phases": 4})},
                {"role": "tool", "content": '{"delivery_cost": 7654321.0, "cut off her'},
                {"role": "user", "content": "The price is PHP 2,500,000.00."}]
    text = json.dumps(tracing.sanitize({"messages": messages}))
    assert not any(v in text for v in ("1234567", "0.31", "7654321", "2,500,000")) and '\\"phases\\": 4' in text


def test_langgraph_automatic_node_traces_never_carry_the_pages(sent):
    class State(TypedDict):
        pages: list
        features: int

    graph = StateGraph(State)
    graph.add_node("intake_guard", lambda s: {"pages": [{"page": 1, "text": mask_pii(s["pages"][0]["text"])[0]}], "features": 1})
    graph.add_edge(START, "intake_guard")
    graph.add_edge("intake_guard", END)
    list(graph.compile().stream({"pages": [{"page": 1, "text": PII}], "features": 0}, stream_mode="updates"))
    tracing.flush()
    body = "\n".join(sent)
    assert "intake_guard" in body     # the node itself is traced
    assert "juana.fictional" not in body and "0917 555" not in body and "Juana" not in body


def test_an_llm_span_has_the_masked_text_tokens_model_and_cost(sent, monkeypatch):
    message = SimpleNamespace(content="done", tool_calls=None, parsed=None, refusal=None)
    response = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=100), choices=[SimpleNamespace(message=message)])
    monkeypatch.setattr(llm, "client", lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **k: response))))
    masked, _ = mask_pii(PII)
    llm.chat([{"role": "user", "content": masked}], node="sizing_agent")
    tracing.flush()
    body = "\n".join(sent)
    assert "llm:sizing_agent" in body and "cost_usd" in body and "input_tokens" in body
    assert "juana.fictional" not in body and "[EMAIL]" in body


def test_the_msa_span_has_field_names_only(sent):
    doc = Document()
    doc.add_paragraph(CLAUSE)
    buf = io.BytesIO()
    doc.save(buf)
    contract_service.fill_msa(buf.getvalue(), {"client_name": "Fictional Client Name", "client_address": "1 Fictional Road"})
    tracing.flush()
    body = "\n".join(sent)
    assert "msa_fill" in body and "client_address" in body
    assert "fictional clause wording" not in body and "Fictional Client Name" not in body and "1 Fictional Road" not in body


def test_a_function_in_a_loop_is_traced_a_few_times_and_always_runs(sent):
    calls = []
    looped = tracing.traced("looped", lambda: {}, lambda r: {}, limit=2)(lambda: calls.append(1))
    with tracing.span("parent"):
        for _ in range(5):
            looped()
    tracing.flush()
    assert len(calls) == 5 and len({json.loads(b)["id"] for b in sent if b and b.startswith("{") and json.loads(b).get("name") == "looped"}) == 2


def test_the_v3_actions_are_spans_without_their_content(db, sent, monkeypatch):
    from app import brd_docx, brd_service as svc, estimate_service as est, opportunities as ops
    from app.db import session_scope
    from tests.test_estimate_proposal import ADMIN, SS, _approved_brd
    opp_id = _approved_brd(db)
    with session_scope(db) as s:
        data = brd_docx.render(s, opp_id)
        brd_docx.diff(s, opp_id, data)
    with session_scope(db) as s:
        est.generate(s, SS, opp_id)
    with session_scope(db) as s:
        est.re_estimate_all(s, ADMIN, opp_id, "A reason that stays private")
    edited = "A statement that must not reach a trace."
    with session_scope(db) as s:
        changes = svc.empty_changes()
        changes["new"] = [{"title": "Added", "statement": edited, "epic_id": "EP-01", "priority": "Must"}]
        svc.apply_changes(s, SS, opp_id, changes, "in-app edit")
    tracing.flush()
    body = "\n".join(b for b in sent if b)
    for name in ("status_transition", "brd_reupload_diff", "estimate_generate", "estimate_re_estimate_all", "brd_version_save", "brd_stability_check"):
        assert f'"{name}"' in body or f'"name":"{name}"' in body.replace(" ", ""), name
    assert '"role"' in body and "must not reach a trace" not in body and "stays private" not in body


def test_what_cannot_be_read_is_not_sent():
    class Opaque:
        def __repr__(self):
            return "secret text"
    assert "secret" not in json.dumps(tracing.sanitize({"x": Opaque(), "file": b"secret bytes"}))

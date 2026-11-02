"""LangSmith tracing, off unless LANGSMITH_TRACING is true. Tracing never changes what the app does.

Every payload leaves through one client whose hooks apply sanitize(), including LangGraph's automatic node traces, whose first input
holds the unmasked pages. Page text, money, rate values and document bytes are withheld; MSA spans carry field names only."""
from __future__ import annotations

import functools
import json
import re
import threading
from collections import OrderedDict
from contextlib import contextmanager
from typing import Callable

import langsmith as ls
from langsmith import run_trees
from langsmith.utils import ContextThreadPoolExecutor as ThreadPool  # noqa: F401 — keeps a worker's spans under its parent

from app.config import env, load_dotenv_if_present

DEFAULT_PROJECT = "deal-advisor-poc"
WITHHELD = "[withheld]"
_MONEY_WORDS = {"rate", "rates", "price", "prices", "pricing", "cost", "costs", "costing", "margin", "amount", "amounts", "floor", "cash", "exposure", "contingency",
                "reserve", "discount", "fee", "fees", "published", "applied", "blended", "tooling"}
_ALLOWED = {"cost_usd", "est_usd", "rates_source"}
_TEXT_KEYS = {"pages", "masked_pages", "snapshot", "client_docx_b64", "template", "clauses", "data"}
_COUNT_SUFFIXES = ("_tokens", "_ms", "_usd")
_MONEY_TEXT = re.compile(r"(?:PHP|₱)\s*[\d,]+(?:\.\d+)?")
_protected: set[float] = set()
_enabled = False
_configured = False


def _money_key(key: str) -> bool:
    return key not in _ALLOWED and bool(_MONEY_WORDS & set(re.split(r"[^a-z]+", key.lower())))


def protect(values) -> None:
    """Rate values seen in this process: withheld wherever they appear, even inside free text."""
    for value in values:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value:
            _protected.update((float(value), float(round(value))))


def _scrub_numbers(text: str) -> str:
    for value in _protected:
        for shown in {f"{value:g}", f"{value:.0f}", f"{value:,.0f}", f"{value:.2f}", f"{value:,.2f}"}:
            if shown in text:
                text = re.sub(rf"(?<![\d.,]){re.escape(shown)}(?![\d])", WITHHELD, text)
    return text


def _clean_text(text: str) -> str:
    if text[:1] in "{[":
        try:
            return json.dumps(_clean(json.loads(text)))
        except ValueError:
            if re.search(r'"[a-z_]*(?:price|cost|margin|floor|exposure|rate)[a-z_]*"', text):     # a cut-off tool result
                return f"[withheld: {len(text)} characters]"
    return _scrub_numbers(_MONEY_TEXT.sub("PHP " + WITHHELD, text))


def _clean(value, key: str = ""):
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            name = str(k)
            if name in _TEXT_KEYS:
                out[name] = f"[{len(v)} items not traced]" if isinstance(v, (list, tuple, dict)) else "[not traced]"
            elif _money_key(name):
                out[name] = WITHHELD
            else:
                out[name] = _clean(v, name)
        return out
    if isinstance(value, (list, tuple, set)):
        return [_clean(v, key) for v in value]
    if isinstance(value, str):
        return _clean_text(value)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return WITHHELD if float(value) in _protected and not key.endswith(_COUNT_SUFFIXES) else value
    if isinstance(value, (bytes, bytearray)):
        return f"[{len(value)} bytes not traced]"
    if hasattr(value, "model_dump"):
        return _clean(value.model_dump(), key)
    return f"<{type(value).__name__}>"


def sanitize(payload: dict) -> dict:
    """The only way out for inputs, outputs and metadata. Anything it cannot read is replaced by its type name."""
    try:
        return _clean(dict(payload))
    except Exception:  # noqa: BLE001 — a payload we cannot clean is not sent
        return {"withheld": "not readable"}


def setup(client=None) -> bool:
    """Reads the kill switch. client: only for tests."""
    global _enabled, _configured
    load_dotenv_if_present()
    _enabled = env("LANGSMITH_TRACING", "").strip().lower() == "true" and (client is not None or bool(env("LANGSMITH_API_KEY")))
    if _enabled:
        client = client or ls.Client(api_url=env("LANGSMITH_ENDPOINT") or None, api_key=env("LANGSMITH_API_KEY"), hide_inputs=sanitize, hide_outputs=sanitize, hide_metadata=sanitize,
                                     omit_traced_runtime_info=True)
        run_trees.configure(client=client, enabled=True, project_name=env("LANGSMITH_PROJECT") or DEFAULT_PROJECT)
    else:
        run_trees.configure(enabled=False)
    _configured = True
    return _enabled


DELETE_BATCH = 1000     # LangSmith's limit per request


def enabled() -> bool:
    if not _configured:
        setup()
    return _enabled


def delete_traces(opportunity_id: str) -> dict:
    """{"status", "traces"}. By the opportunity ID in span metadata. Needs a LangSmith key here, whether or not tracing is on. LangSmith deletes in the background."""
    load_dotenv_if_present()
    if not env("LANGSMITH_API_KEY"):
        return {"status": "not_configured", "traces": 0}
    import requests
    from langsmith import Client
    client = Client()
    by_project: dict = {}
    wanted = f'and(eq(metadata_key, "opportunity_id"), eq(metadata_value, "{opportunity_id}"))'
    for run in client.list_runs(project_name=env("LANGSMITH_PROJECT") or DEFAULT_PROJECT, filter=wanted, select=["id", "trace_id", "session_id"]):
        by_project.setdefault(str(run.session_id), set()).add(str(run.trace_id))
    url = client.api_url.rstrip("/").removesuffix("/api/v1") + "/api/v1/runs/delete"
    for session_id, traces in by_project.items():
        ids = sorted(traces)
        for start in range(0, len(ids), DELETE_BATCH):
            response = requests.post(url, headers={"x-api-key": env("LANGSMITH_API_KEY")}, json={"session_id": session_id, "trace_ids": ids[start:start + DELETE_BATCH]}, timeout=30)
            response.raise_for_status()
    return {"status": "deleted", "traces": sum(len(t) for t in by_project.values())}


def flush() -> None:
    if enabled():
        try:
            run_trees.get_cached_client().flush()
        except Exception:  # noqa: BLE001 — tracing never stops the app
            pass


class _NoSpan:
    def add_metadata(self, _: dict) -> None:
        pass

    def set_outputs(self, _: dict) -> None:
        pass


class _Span:
    def __init__(self, run):
        self._run, self.outputs = run, {}

    def add_metadata(self, metadata: dict) -> None:
        self._run.add_metadata(metadata)

    def set_outputs(self, outputs: dict) -> None:
        self.outputs = outputs


@contextmanager
def span(name: str, run_type: str = "chain", inputs: dict | None = None, metadata: dict | None = None):
    """An error is recorded by type only: its message may quote a document."""
    if not enabled():
        yield _NoSpan()
        return
    error = None
    with ls.trace(name, run_type=run_type, inputs=inputs or {}, metadata=metadata or {}) as run:
        current = _Span(run)
        try:
            yield current
        except Exception as e:  # noqa: BLE001 — raised again below, once the span is closed
            error = e
            run.end(outputs={"error": type(e).__name__}, error=type(e).__name__)
        else:
            run.end(outputs=current.outputs)
    if error is not None:
        raise error


@contextmanager
def context(**metadata):
    """Run ID, opportunity, document set, scenario, profile, prompt versions: inherited by every span inside."""
    if not enabled():
        yield
        return
    with ls.tracing_context(metadata={k: v for k, v in metadata.items() if v is not None}):
        yield


def _safely(summary: Callable | None, *args, **kwargs) -> dict:
    if summary is None:
        return {}
    try:
        return summary(*args, **kwargs) or {}
    except Exception as e:  # noqa: BLE001 — a summary never stops the work
        return {"summary_error": type(e).__name__}


_repeats: OrderedDict = OrderedDict()
_repeats_lock = threading.Lock()


def _within_limit(name: str, limit: int) -> bool:
    """A calculator in a search loop runs hundreds of times: the trace shows its first few."""
    current = ls.get_current_run_tree()
    key = (str(current.trace_id) if current else "", name)
    with _repeats_lock:
        _repeats[key] = _repeats.get(key, 0) + 1
        while len(_repeats) > 1000:
            _repeats.popitem(last=False)
        return _repeats[key] <= limit


def traced(name: str, inputs: Callable | None = None, outputs: Callable | None = None, run_type: str = "tool", limit: int | None = None):
    """inputs(*args, **kwargs) and outputs(result) return what the trace shows. Without them it shows nothing: never the arguments themselves.
    limit: spans per trace for a function called in a loop."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not enabled() or (limit is not None and not _within_limit(name, limit)):
                return fn(*args, **kwargs)
            shown = _safely(inputs, *args, **kwargs)
            keep = {k: shown[k] for k in ("role", "artifact_version", "opportunity_id") if shown.get(k) is not None}
            with span(name, run_type, shown, keep) as current:
                result = fn(*args, **kwargs)
                current.set_outputs(_safely(outputs, result))
                return result
        return wrapper
    return deco


def shape(value) -> dict:
    """Sizes and flags of a result, never its text."""
    if not isinstance(value, dict):
        return {"items": len(value)} if hasattr(value, "__len__") and not isinstance(value, (str, bytes)) else {"type": type(value).__name__}
    return {k: (len(v) if hasattr(v, "__len__") and not isinstance(v, str) else v) for k, v in value.items() if isinstance(v, (bool, int, float, list, dict, tuple, set)) and not _money_key(k)}


def version_label(result) -> dict:
    return {"artifact_version": getattr(result, "version_label", None)}


def tool_call(name: str, args: dict, call: Callable):
    """One tool an agent chose. Its result is counted, never copied: cost summaries stay out of the trace."""
    with span(name, "tool", {"args": args}) as current:
        result = call()
        current.set_outputs({"items": len(result) if hasattr(result, "__len__") else 1})
        return result

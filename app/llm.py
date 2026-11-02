"""Azure OpenAI client factory: timeouts, retries with backoff, token accounting."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache

from openai import (APIConnectionError, APITimeoutError, AzureOpenAI, BadRequestError, ContentFilterFinishReasonError,
                    InternalServerError, LengthFinishReasonError, RateLimitError)
from pydantic import ValidationError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app import faults, tracing
from app.config import env, load_dotenv_if_present, pipeline_config, pricing_config

TIMEOUT_S = 60
RETRYABLE = (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)

retry_policy = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=20),
    retry=retry_if_exception_type(RETRYABLE),
)


@lru_cache(maxsize=1)
def client() -> AzureOpenAI:
    load_dotenv_if_present()
    return AzureOpenAI(
        azure_endpoint=env("AZURE_OPENAI_ENDPOINT"),
        api_key=env("AZURE_OPENAI_API_KEY"),
        api_version=env("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        timeout=TIMEOUT_S,
        max_retries=0,  # tenacity owns retries
    )


def deployment(role: str) -> str:
    """role: MAIN | LIGHT | EMBED."""
    load_dotenv_if_present()
    defaults = {"MAIN": "gpt-4.1-mini", "LIGHT": "gpt-4.1-nano", "EMBED": "text-embedding-3-small"}
    return env(f"AZURE_OPENAI_{role}_DEPLOYMENT", defaults[role])


def model_tag(role: str) -> str:
    """Deployment name + pinned model version, for cache keys (config/pipeline.yaml)."""
    return f"{deployment(role)}@{pipeline_config()['models'][role]['version']}"


def seed() -> int:
    return int(pipeline_config()["seed"])


# ---------- token accounting ----------

class BudgetExceeded(BaseException):
    """Raised before an Azure call once a run's spend reaches its cap. BaseException on purpose: node-level
    `except Exception` fallbacks must not swallow it, so the whole run stops."""


@dataclass
class Usage:
    by_node: dict[str, dict[str, int]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    limit_usd: float | None = None

    def check(self) -> None:
        if self.limit_usd is not None and self.totals()["est_usd"] >= self.limit_usd:
            raise BudgetExceeded(f"run stopped at its spend cap of USD {self.limit_usd:.2f}")

    def add(self, node: str, model: str, prompt: int, completion: int) -> None:
        with self.lock:
            key = f"{node}|{model}"
            row = self.by_node.setdefault(key, {"prompt": 0, "completion": 0, "calls": 0})
            row["prompt"] += prompt
            row["completion"] += completion
            row["calls"] += 1

    def totals(self) -> dict[str, float]:
        prices = pricing_config()["per_million_tokens"]
        prompt = completion = 0
        usd = 0.0
        for key, row in self.by_node.items():
            model = key.split("|", 1)[1]
            p = prices.get(model, {"input": 0.0, "output": 0.0})
            usd += row["prompt"] / 1e6 * p["input"] + row["completion"] / 1e6 * p["output"]
            prompt += row["prompt"]
            completion += row["completion"]
        return {"prompt_tokens": prompt, "completion_tokens": completion, "est_usd": usd}


def _usd(model: str, prompt: int, completion: int) -> float:
    prices = pricing_config()["per_million_tokens"].get(model, {"input": 0.0, "output": 0.0})
    return round(prompt / 1e6 * prices["input"] + completion / 1e6 * prices["output"], 6)


def _traced(node: str, model: str, messages: list[dict], call, answer=lambda resp: {}):
    """One Azure call as an LLM span: what was sent (masked by the intake guard), tokens, model and cost."""
    if not tracing.enabled():
        return call()
    with tracing.span(f"llm:{node}", "llm", {"messages": messages}, {"ls_provider": "azure_openai", "ls_model_name": model, "model": model, "node": node}) as current:
        resp = call()
        prompt, completion = (resp.usage.prompt_tokens, resp.usage.completion_tokens) if resp.usage is not None else (0, 0)
        current.add_metadata({"cost_usd": _usd(model, prompt, completion)})
        current.set_outputs({**answer(resp), "usage_metadata": {"input_tokens": prompt, "output_tokens": completion, "total_tokens": prompt + completion}})
        return resp


def _answer(resp) -> dict:
    msg = resp.choices[0].message
    tools = [t.function.name for t in (msg.tool_calls or [])]
    parsed = getattr(msg, "parsed", None)
    return {"content": parsed.model_dump() if parsed is not None else msg.content, "tool_calls": tools}


@retry_policy
def _embed_batch(texts: list[str], model: str):
    return client().embeddings.create(model=model, input=texts)


def embed(texts: list[str], usage: Usage | None = None, node: str = "embed", batch: int = 64) -> list[list[float]]:
    model = deployment("EMBED")
    out: list[list[float]] = []
    for i in range(0, len(texts), batch):
        if usage is not None:
            usage.check()
        part = texts[i:i + batch]
        if tracing.enabled():
            with tracing.span(f"embed:{node}", "embedding", {"texts": len(part)}, {"model": model}) as current:
                resp = _embed_batch(part, model)
                current.add_metadata({"cost_usd": _usd(model, resp.usage.prompt_tokens, 0)})
                current.set_outputs({"usage_metadata": {"input_tokens": resp.usage.prompt_tokens, "output_tokens": 0, "total_tokens": resp.usage.prompt_tokens}})
        else:
            resp = _embed_batch(part, model)
        out.extend(d.embedding for d in resp.data)
        if usage is not None:
            usage.add(node, model, resp.usage.prompt_tokens, 0)
    return out


@retry_policy
def _parse(model: str, messages: list[dict], schema, **kwargs):
    return client().beta.chat.completions.parse(model=model, messages=messages, response_format=schema,
                                                temperature=0, seed=seed(), **kwargs)


def is_content_filter(error: BadRequestError) -> bool:
    """Azure's own refusal of a prompt, as opposed to a badly formed request."""
    body = error.body if isinstance(error.body, dict) else {}
    return "content_filter" in (error.code, body.get("code"))


def _fault(node: str) -> None:
    if faults.active("llm_timeout") and node == "sizing_agent":
        raise TimeoutError("FAULT_INJECT=llm_timeout")


def structured(messages: list[dict], schema, role: str = "MAIN", usage: Usage | None = None,
               node: str = "llm", **kwargs):
    """Structured output with Pydantic validation. Transient errors: tenacity x3 with backoff.
    Validation/refusal: one retry with the error message appended; then raises for the caller to degrade."""
    _fault(node)
    model = deployment(role)
    msgs = list(messages)
    for attempt in range(2):
        if usage is not None:
            usage.check()
        try:
            resp = _traced(node, model, msgs, lambda: _parse(model, msgs, schema, **kwargs), _answer)
        except (ValidationError, LengthFinishReasonError, ContentFilterFinishReasonError) as e:
            if attempt == 1:
                raise
            msgs = msgs + [{"role": "user", "content": f"Your previous answer was invalid ({type(e).__name__}: "
                                                       f"{str(e)[:300]}). Return valid JSON for the schema."}]
            continue
        if usage is not None and resp.usage is not None:
            usage.add(node, model, resp.usage.prompt_tokens, resp.usage.completion_tokens)
        msg = resp.choices[0].message
        if msg.parsed is not None:
            return msg.parsed
        if attempt == 1:
            raise ValueError(f"no parsed output (refusal: {bool(msg.refusal)})")
        msgs = msgs + [{"role": "user", "content": "Return valid JSON for the schema."}]
    raise ValueError("structured output failed")


@retry_policy
def chat(messages: list[dict], role: str = "MAIN", usage: Usage | None = None, node: str = "chat", **kwargs):
    _fault(node)
    model = deployment(role)
    if usage is not None:
        usage.check()
    t0 = time.perf_counter()
    kwargs = {"temperature": 0, "seed": seed(), **kwargs}
    resp = _traced(node, model, messages, lambda: client().chat.completions.create(model=model, messages=messages, **kwargs), _answer)
    if usage is not None and resp.usage is not None:
        usage.add(node, model, resp.usage.prompt_tokens, resp.usage.completion_tokens)
    resp._duration_ms = int((time.perf_counter() - t0) * 1000)  # type: ignore[attr-defined]
    return resp

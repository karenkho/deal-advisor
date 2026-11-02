"""Tools the sizing agent may call: precedent_search, rubric_lookup, glossary_lookup."""
from __future__ import annotations

import json
from functools import lru_cache

from app import llm, tracing
from app.retrieval import FEATURE_TYPES, CorpusIndex


@lru_cache(maxsize=1)
def index() -> CorpusIndex:
    idx, _ = CorpusIndex.build()
    return idx


TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "precedent_search",
        "description": "Find similar features from past projects and the reusable pattern library, with their "
                       "size, type, acceptance criteria and variations that change size.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Feature description to match"},
            "feature_type": {"type": "string", "enum": FEATURE_TYPES, "description": "Optional type filter"}},
            "required": ["query"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "rubric_lookup",
        "description": "Get the guideline's S/M/L/XL sizing rubric for a feature type.",
        "parameters": {"type": "object", "properties": {
            "feature_type": {"type": "string", "enum": FEATURE_TYPES}},
            "required": ["feature_type"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "glossary_lookup",
        "description": "Get the definition of a TOR/enterprise term (e.g. SSO, RPO, KYC).",
        "parameters": {"type": "object", "properties": {"term": {"type": "string"}},
                       "required": ["term"], "additionalProperties": False}}},
]


@tracing.traced("retrieve_estimation_guidelines", lambda query, k=3, usage=None: {"query": query, "k": k}, tracing.shape)
def retrieve_estimation_guidelines(query: str, k: int = 3, usage: llm.Usage | None = None) -> list[dict]:
    """Hybrid search over the estimation guideline (rubrics, checklists, AI suitability guidance)."""
    return [{"id": c.id, "heading": c.metadata.get("heading", ""), "text": c.text}
            for c, _ in index().search(query, k=k, filters={"doc_type": "guideline"}, usage=usage)]


def precedent_search(query: str, feature_type: str | None = None, usage: llm.Usage | None = None, k: int = 4):
    filters = {"kind": "feature"}
    if feature_type in FEATURE_TYPES:
        filters["feature_type"] = feature_type
    hits = index().search(query, k=k, filters=filters, usage=usage)
    if len(hits) < 2 and feature_type:   # widen if the type filter is too narrow
        hits = index().search(query, k=k, filters={"kind": "feature"}, usage=usage)
    out = [{"id": c.id, "project": c.metadata["project_id"], "domain": c.metadata["domain"],
            "feature": c.metadata["feature"], "type": c.metadata["feature_type"], "size": c.metadata["size"],
            "text": c.text} for c, _ in hits]
    out += [{"id": c.id, "pattern": c.metadata["heading"], "text": c.text}
            for c, _ in index().search(query, k=2, filters={"doc_type": "patterns"}, usage=usage)]
    return out


def rubric_lookup(feature_type: str, **_):
    for c in index().chunks:
        if c.metadata.get("kind") == "rubric" and c.metadata.get("feature_type") == feature_type:
            return [{"id": c.id, "text": c.text}]
    return []


def glossary_lookup(term: str, usage: llm.Usage | None = None, **_):
    t = term.strip().lower()
    for c in index().chunks:
        if c.metadata.get("doc_type") == "glossary" and c.metadata.get("heading", "").lower() == t:
            return [{"id": c.id, "text": c.text}]
    hits = index().search(term, k=1, filters={"doc_type": "glossary"}, usage=usage)
    return [{"id": c.id, "text": c.text} for c, _ in hits]


def run_tool(name: str, args_json: str, usage: llm.Usage | None = None) -> tuple[dict, list[dict]]:
    args = json.loads(args_json or "{}")
    fn = {"precedent_search": precedent_search, "rubric_lookup": rubric_lookup, "glossary_lookup": glossary_lookup}[name]
    return args, tracing.tool_call(name, args, lambda: fn(**args, usage=usage))

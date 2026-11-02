"""Determinism layers applied after extraction: canonical feature names, stable IDs and ordering, plus the
deterministic per-feature attributes (MoSCoW priority, functional complexity, assumptions)."""
from __future__ import annotations

import hashlib
import json
import re

import numpy as np

from app import llm
from app.config import CACHE_DIR, CORPUS_DIR, pipeline_config
from app.schemas import Feature, Requirement

CANON_CACHE = CACHE_DIR / "canonical_embeddings.json"


def pattern_names() -> list[tuple[str, str]]:
    """(name, short description) for every pattern in corpus/patterns.md."""
    text = (CORPUS_DIR / "patterns.md").read_text(encoding="utf-8")
    out = []
    for block in re.split(r"^## +", text, flags=re.M)[1:]:
        name, _, body = block.partition("\n")
        m = re.search(r"\*\*Description:\*\*\s*(.+)", body)
        out.append((name.strip(), m.group(1).strip() if m else ""))
    return out


def _pattern_vectors(usage: llm.Usage | None) -> tuple[list[str], np.ndarray]:
    """Pattern embeddings cached by content hash in their own file (the corpus cache is never pruned from here)."""
    patterns = pattern_names()
    texts = [f"{n}. {d}" for n, d in patterns]
    cache = json.loads(CANON_CACHE.read_text()) if CANON_CACHE.exists() else {}
    hashes = [hashlib.sha256(t.encode()).hexdigest() for t in texts]
    missing = [i for i, h in enumerate(hashes) if h not in cache]
    if missing:
        for i, v in zip(missing, llm.embed([texts[i] for i in missing], usage=usage, node="canonical_embed")):
            cache[hashes[i]] = v
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        CANON_CACHE.write_text(json.dumps({h: cache[h] for h in hashes}))
    return [n for n, _ in patterns], np.array([cache[h] for h in hashes], dtype=np.float32)


def _unit(v: np.ndarray) -> np.ndarray:
    return v / (np.linalg.norm(v, axis=1, keepdims=True) + 1e-9)


def canonicalize(features: list[Feature], usage: llm.Usage | None, taken: set[str] | None = None) -> list[Feature]:
    """Rename features to the nearest pattern-library name when similarity >= threshold. Each pattern name is
    given to at most one feature (the most similar); the extracted name is kept in extracted_name."""
    cfg = pipeline_config()["canonical_names"]
    eligible = [f for f in features if f.epic not in cfg["skip_epics"]]
    if not eligible:
        return features
    names, pv = _pattern_vectors(usage)
    fv = np.array(llm.embed([f"{f.name}. {f.description}" for f in eligible], usage=usage, node="canonical_embed"),
                  dtype=np.float32)
    sim = _unit(fv) @ _unit(pv).T
    candidates = sorted(((sim[i, j], i, j) for i in range(len(eligible)) for j in range(len(names))
                         if sim[i, j] >= cfg["threshold"]), reverse=True)
    taken_f = set()
    taken_p = {j for j, n in enumerate(names) if n in (taken or set())}
    renamed: dict[int, tuple[str, float]] = {}
    for s, i, j in candidates:
        if i in taken_f or j in taken_p:
            continue
        taken_f.add(i)
        taken_p.add(j)
        renamed[i] = (names[j], float(s))
    out = []
    pos = {id(f): i for i, f in enumerate(eligible)}
    for f in features:
        i = pos.get(id(f))
        if i is not None and i in renamed:
            name, s = renamed[i]
            f = f.model_copy(update={"extracted_name": f.name, "name": name, "canonical_similarity": round(s, 3)})
        out.append(f)
    return out


def stable_id(epic: str, name: str) -> str:
    return "F-" + hashlib.sha256(f"{epic.strip().lower()}|{name.strip().lower()}".encode()).hexdigest()[:6]


def order_and_id(features: list[Feature]) -> list[Feature]:
    """Deterministic order (epic, then name) and IDs from a hash of epic + name (suffix on a rare clash)."""
    seen: dict[str, int] = {}
    out = []
    for f in sorted(features, key=lambda f: (f.epic.lower(), f.name.lower())):
        fid = stable_id(f.epic, f.name)
        seen[fid] = seen.get(fid, 0) + 1
        out.append(f.model_copy(update={"id": fid if seen[fid] == 1 else f"{fid}-{seen[fid]}"}))
    return out


COULD = re.compile(r"\b(?:may|optional(?:ly)?|could|nice to have|if possible|future phase|desirable)\b", re.I)
SECURITY_SENSITIVE = re.compile(
    r"\b(?:log ?in|login|sign[- ]in|authenticat\w*|password|credential\w*|mfa|otp|single sign|sso|access control|"
    r"rbac|roles? and permissions?|permissions?|payroll|salar(?:y|ies)|payslips?|payments?|personal (?:data|information)|"
    r"pii|privacy|government id\w*)\b", re.I)
# Narrower than SECURITY_SENSITIVE (which sets feature complexity): the AI-suitability cap applies only to the
# security LOGIC of these features, not to every feature that displays personal data.
SECURITY_LOGIC = re.compile(
    r"\b(?:log ?in|sign[- ]in|authenticat\w*|passwords?|credentials?|mfa|otp|single sign[- ]on|sso|"
    r"authori[sz]\w*|access control|rbac|role[- ]based|permissions?|encrypt\w*|salary computation|"
    r"payroll (?:computation|calculation|interface|integration|export|import|data exchange)|"
    r"(?:to|from|with) (?:the )?(?:\w+ )?payroll(?: system)?|"
    r"data privacy|data subject|consent|(?:personal|sensitive) data (?:export|extract|transfer|masking|retention))\b",
    re.I)
SECURITY_LOGIC_ACTIVITIES = ("Backend.technical_design", "Backend.coding", "Backend.unit_tests",
                             "Backend.code_review_integration")
CRITIC_PRIORITY = "Should (pending client confirmation)"


def priority(f: Feature, reqs: dict[str, Requirement]) -> str:
    """MoSCoW by rule: out of scope -> Won't; critic-implied -> Should (pending client confirmation); TOR wording
    that marks the item optional -> Could; every other TOR requirement ("shall") -> Must."""
    if f.route == "out_of_scope":
        return "Won't"
    if f.origin == "critic":
        return CRITIC_PRIORITY
    texts = [reqs[r].text for r in f.requirement_ids if r in reqs] or [f.description]
    if all(COULD.search(t) for t in texts):
        return "Could"
    return "Must"


def security_sensitive(f: Feature) -> bool:
    return bool(SECURITY_SENSITIVE.search(f"{f.name} {f.description}"))


def functional_complexity(f: Feature, domain_complexity: str = "standard") -> str:
    """Feature-level complexity for AI suitability: high for integrations, novel items, XL, security-sensitive
    features (auth, payroll/payment, PII) and every feature of a high-complexity domain; low for S; else medium."""
    if (domain_complexity == "high" or f.type == "integration" or f.route == "novel" or f.size == "XL"
            or security_sensitive(f)):
        return "high"
    if f.size == "S":
        return "low"
    return "medium"


def feature_assumptions(f: Feature) -> list[str]:
    out = []
    out += [s.strip() for s in re.split(r"(?<=[.;])\s+", f.description) if re.search(r"\bassum", s, re.I)][:2]
    if f.route == "novel":
        out.append("Novel for typical business software: size to be confirmed by the BA.")
    if f.route == "unclear":
        out.append("Not sized until the client answers the clarification question.")
    if f.origin == "critic":
        out.append("Implied by the TOR (coverage critic): confirm scope with the client.")
    if f.needs_manual_review:
        out.append("Needs manual review.")
    return out


# ---------- commercial terms stated in the TOR (deterministic) ----------

_DELIVERY_PERIOD = re.compile(r"(?:deliver\w*|completed?|live operation)[^.]{0,120}?within\s+(?:\w+\s+)?\(?(\d+)\)?\s+months",
                              re.I)


_UPFRONT = re.compile(r"(\d+)\s*%\)?[^.;]{0,60}?(?:signing|upfront|down ?payment|mobili[sz]ation)", re.I)
_FINAL = re.compile(r"(\d+)\s*%\)?[^.;]{0,60}?(?:final acceptance|on acceptance|completion)", re.I)
_WARRANTY = re.compile(r"warranty[^.]{0,40}?\(?(\d+)\)?\s+months", re.I)
_DELIVERABLES = re.compile(r"shall deliver:\s*([^.]+)\.", re.I)


def commercial_terms(text: str) -> dict:
    """Delivery period, payment terms and warranty the TOR asks for, when it states them."""
    flat = " ".join(text.split())
    fields = {"requested_months": _DELIVERY_PERIOD, "upfront_pct": _UPFRONT, "final_pct": _FINAL, "warranty_months": _WARRANTY}
    out = {k: int(m.group(1)) for k, rx in fields.items() if (m := rx.search(flat))}
    m = _DELIVERABLES.search(flat)
    if m:
        items = [re.sub(r"^and\s+", "", d.strip()) for d in m.group(1).split(";")]
        out["deliverables"] = [d[0].upper() + d[1:] for d in items if d]
    return out


# ---------- guideline rules: client questions the LLM steps may miss (deterministic, no Azure calls) ----------

COUNTERPART = re.compile(r"\b(?:API|REST|SOAP|SFTP|FTP|CSV|XML|JSON|XLSX?|web ?service|file format|format|protocol|"
                         r"version)\b", re.I)


def rule_questions(features: list[Feature], reqs: list[Requirement], existing: list[str]) -> list[dict]:
    """Questions from the guideline (Definition of Ready): vague statements, every integration without a named
    counterpart or format, and data migration without volumes. Skips topics an existing question already covers."""
    from app.graph import VAGUE   # one definition of "vague"
    have = " ".join(existing).lower()
    by_id = {r.id: r for r in reqs}
    out = []
    for r in reqs:
        if r.category in ("functional", "nfr", "constraint", "business_rule") and not r.superseded_by and VAGUE.search(r.text) and r.text[:40].lower() not in have:
            sec = f"Section {r.section}" if r.section else f"Page {r.pages[0]}"
            out.append({"text": f"{sec} states: \"{r.text}\" This is not measurable. Which specific capabilities, "
                                "controls or targets must the System deliver?", "pages": r.pages, "statement_ids": [r.id]})
    for f in features:
        if f.type != "integration" or f.route == "out_of_scope" or f.name.lower() in have:
            continue
        texts = " ".join([f.name, f.description] + [by_id[i].text for i in f.requirement_ids if i in by_id])
        if not COUNTERPART.search(texts):
            out.append({"text": f"{f.name}: the TOR does not name the external system, vendor or device models, the "
                                "data format or the exchange frequency. Which system and interface specification "
                                "apply, and who provides test access?", "pages": f.pages, "statement_ids": list(f.requirement_ids)})
    for f in features:
        if f.type == "data_migration" and "migrat" not in have:
            out.append({"text": f"{f.name}: how many records and years of history must be migrated, from which sources "
                                "and formats, and who cleans and validates the data?", "pages": f.pages,
                        "statement_ids": list(f.requirement_ids)})
            break
    return out

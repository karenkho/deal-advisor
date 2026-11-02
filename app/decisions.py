"""Decision memory: BA edits (renames, merges, size and priority overrides, dismissed questions), keyed by statement id and
reapplied on every re-process. Stored per opportunity, on disk for bundled TORs and in memory for uploads."""
from __future__ import annotations

import json

from app.config import UPLOAD_PREFIX
from app.record import _dir, _now, atomic_write, locked

_memory: dict[str, list[dict]] = {}
TYPES = ("rename", "merge", "size", "priority", "route", "dismiss_question", "scope_status")


def critic_key(name: str) -> str:
    """A critic-added BR has no statements: its decisions are kept by name."""
    return f"critic:{' '.join(name.lower().split())}"


def load(opp_id: str) -> list[dict]:
    if opp_id.startswith(UPLOAD_PREFIX):
        return list(_memory.get(opp_id, []))
    path = _dir(opp_id) / "decisions.json"
    return json.loads(path.read_text()) if path.exists() else []


def add(opp_id: str, kind: str, statement_ids: list[str], value=None) -> None:
    """A later decision of the same kind on the same statements replaces the earlier one."""
    if kind not in TYPES:
        raise ValueError(f"Unknown decision: {kind}")
    with locked(opp_id):
        items = [d for d in load(opp_id) if not (d["type"] == kind and set(d["statement_ids"]) == set(statement_ids))]
        items.append({"type": kind, "statement_ids": sorted(statement_ids), "value": value, "at": _now()})
        if opp_id.startswith(UPLOAD_PREFIX):
            _memory[opp_id] = items
        else:
            atomic_write(_dir(opp_id) / "decisions.json", json.dumps(items, indent=0))


def _find(features: list, ids: list[str]):
    """The feature holding most of these statements, if any still exist."""
    want = set(ids)
    keys = lambda f: set(f.statement_ids) | ({critic_key(f.name)} if f.origin == "critic" else set())  # noqa: E731
    best = max(features, key=lambda f: len(want & keys(f)), default=None)
    return best if best and want & keys(best) else None


def apply(features: list, questions: list, items: list[dict]) -> tuple[list, list, int]:
    """Reapplies merges first, then the others. Returns (features, questions, how many were reapplied). A decision whose
    statements no longer exist is skipped, not forced."""
    features = [f.model_copy(deep=True) for f in features]
    questions = [q.model_copy(deep=True) for q in questions]
    done = 0
    for d in (x for x in items if x["type"] == "merge"):
        parts = []
        for anchor in d["value"]:
            f = _find([x for x in features if x not in parts], anchor)
            if f is not None:
                parts.append(f)
        if len(parts) < 2:
            continue
        head, rest = parts[0], parts[1:]
        order = ["S", "M", "L", "XL"]
        for other in rest:
            head.statement_ids += other.statement_ids
            head.citations += other.citations
            head.description = f"{head.description} {other.description}"
            head.name = f"{head.name} and {other.name}"
            if other.size and (not head.size or order.index(other.size) > order.index(head.size)):
                head.size = other.size
        features = [f for f in features if f not in rest]
        done += 1
    for d in items:
        if d["type"] == "merge":
            continue
        if d["type"] == "dismiss_question":
            hit = [q for q in questions if set(d["statement_ids"]) & set(q.statement_ids) or q.text == d["value"]]
            for q in hit:
                q.dismissed = True
            done += bool(hit)
            continue
        f = _find(features, d["statement_ids"])
        if f is None:
            continue
        if d["type"] == "rename":
            f.name = d["value"]
        else:
            setattr(f, d["type"], d["value"])
        done += 1
    return features, questions, done

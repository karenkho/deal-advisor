"""Built documents and check results, in this process's memory only: never on disk, in the database or in logs.
A key is a hash of what the result was built from, so a changed version misses by itself and nothing has to be cleared."""
from __future__ import annotations

import copy
import hashlib
import json
import threading
from collections import OrderedDict

MAX_ENTRIES = 128     # about 100 KB a document
_entries: OrderedDict = OrderedDict()
_lock = threading.Lock()


def digest(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def version_hash(version, ignore: tuple = ()) -> str:
    """ignore: snapshot keys the result does not depend on, such as the MSA's review ticks."""
    return digest(version.id, version.version_label, version.status, {k: v for k, v in version.snapshot.items() if k not in ignore})


def opportunity_hash(opp) -> str:
    return digest(opp.id, opp.code, opp.name, opp.client_name, opp.bdm_name, opp.commercial, opp.award)


def get(key: str):
    with _lock:
        if key not in _entries:
            return None
        _entries.move_to_end(key)
        return copy.deepcopy(_entries[key])


def put(key: str, value) -> None:
    with _lock:
        _entries[key] = copy.deepcopy(value)
        _entries.move_to_end(key)
        while len(_entries) > MAX_ENTRIES:
            _entries.popitem(last=False)


def drop(key: str) -> None:
    with _lock:
        _entries.pop(key, None)


def remember(key: str, build):
    found = get(key)
    if found is None:
        found = build()
        put(key, found)
    return found


def clear() -> None:
    with _lock:
        _entries.clear()

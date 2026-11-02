"""Per-item result store for incremental re-processing: one LLM answer per document section, BR or activity set, keyed by a
hash of its input, so unchanged items keep their results and only new ones call the LLM. Bundled TORs write to
samples/results/kv (shipped). Uploaded documents stay in this process's memory only, never on disk."""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict

from app.config import SAMPLES_DIR, env

SHIPPED = SAMPLES_DIR / "results" / "kv"
MAX_MEMORY_ITEMS = 5000
_memory: OrderedDict[str, str] = OrderedDict()


def key_of(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


class KV:
    def __init__(self, in_memory: bool = False, enabled: bool | None = None):
        self.in_memory = in_memory
        # off: neither read nor write (stability runs)
        self.enabled = env("EXTRACT_CACHE", "on") != "off" if enabled is None else enabled

    def get(self, key: str):
        if not self.enabled:
            return None
        if key in _memory:
            return json.loads(_memory[key])
        path = SHIPPED / f"{key}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def put(self, key: str, value) -> None:
        if not self.enabled:
            return
        text = json.dumps(value, sort_keys=True)
        if self.in_memory:
            _memory[key] = text
            _memory.move_to_end(key)
            while len(_memory) > MAX_MEMORY_ITEMS:
                _memory.popitem(last=False)
        else:
            SHIPPED.mkdir(parents=True, exist_ok=True)
            (SHIPPED / f"{key}.json").write_text(text)

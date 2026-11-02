"""Full-result cache, shipped in the image so PROCESS HRIS TOR makes zero Azure calls.

Key = SHA-256 of all input files (sorted by content hash) + model name/version per role + prompt versions + the
config files that shape LLM steps. Rates and the code-only configs are excluded: costing and estimation re-run in
code on every load."""
from __future__ import annotations

import hashlib
import json

from app import llm
from app.config import CONFIG_DIR, SAMPLES_DIR
from app.schemas import TorState

RESULTS_DIR = SAMPLES_DIR / "results"
RESULT_VERSION = "2"     # bump when code after the LLM steps changes the result; stored LLM answers stay valid
KEY_CONFIGS = ("estimation.yaml", "pipeline.yaml")


def result_key(files: list[bytes]) -> str:
    from app.graph import PROMPT_VERSION
    h = hashlib.sha256()
    for digest in sorted(hashlib.sha256(b).hexdigest() for b in files):
        h.update(digest.encode())
    for name in KEY_CONFIGS:
        h.update(hashlib.sha256((CONFIG_DIR / name).read_bytes()).digest())
    h.update("|".join([llm.model_tag(r) for r in ("MAIN", "LIGHT", "EMBED")] + [PROMPT_VERSION, RESULT_VERSION]).encode())
    return h.hexdigest()


def get(files: list[bytes], kv=None) -> dict | None:
    """kv: an in-memory store for uploads, which never read the shipped results: those belong to the bundled sample, and an upload of the same file is a new opportunity with its own documents."""
    key = result_key(files)
    if kv is not None:
        data = kv.get(f"result-{key}")
        if data is None:
            return None
    else:
        path = RESULTS_DIR / f"{key}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text())
    data["state"] = TorState.model_validate(data["state"])
    return data


def put(files: list[bytes], state: TorState, usage: dict, runtime_s: float, kv=None) -> None:
    if state.errors or not state.features:
        return
    entry = {"tor_name": state.tor_name, "runtime_s": runtime_s, "usage": usage, "state": state.model_dump()}
    if kv is not None:
        kv.put(f"result-{result_key(files)}", entry)
        return
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / f"{result_key(files)}.json").write_text(json.dumps(entry))

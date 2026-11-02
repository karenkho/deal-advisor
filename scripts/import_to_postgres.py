"""Import the bundled HRIS opportunity (latest v2 record, decisions and runs) into PostgreSQL. No LLM calls.
Runs as the app role, all-or-nothing, and refuses if HRIS is already there.

Usage: python scripts/import_to_postgres.py
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import decisions, record  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.legacy_import import AlreadyImported, import_record  # noqa: E402
from app.pipeline import HRIS_FILES, hris_docs  # noqa: E402


def main() -> None:
    rec = record.load("hris")
    if rec is None:
        raise SystemExit("No local HRIS record to import.")
    docs = hris_docs(False)
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in HRIS_FILES}
    documents = [{"key": d.key, "name": d.name, "kind": d.kind, "number": d.number, "date": d.date, "sha256": hashes[d.name],
                  "pages": d.pages} for d in docs]
    try:
        with session_scope() as session:
            counts = import_record(session, rec, documents, decisions.load("hris"), record.runs("hris"), code="HRIS")
    except AlreadyImported as e:
        raise SystemExit(str(e))
    print(f"Imported HRIS from record version {rec.version}: " + ", ".join(f"{k}={v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()

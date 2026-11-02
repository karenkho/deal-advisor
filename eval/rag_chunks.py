"""Writes the corpus chunks RAGAS generates questions from: everything the retrieval tools search, except one-line past-project features, which carry no answer of their own."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import retrieval  # noqa: E402
from app.config import CORPUS_DIR  # noqa: E402

chunks = [c for c in retrieval.load_corpus() if c.metadata.get("kind") != "feature"] + retrieval.parse_sectioned(CORPUS_DIR / "proposal_sections.md")
out = ROOT / "eval" / "golden" / "corpus_chunks.json"
out.write_text(json.dumps([{"id": c.id, "text": c.text} for c in chunks], indent=1))
print(len(chunks), "chunks,", sum(len(c.text.split()) for c in chunks), "words")

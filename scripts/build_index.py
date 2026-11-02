"""Embed the corpus (cached by content hash) and run a smoke query."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import llm  # noqa: E402
from app.retrieval import CorpusIndex  # noqa: E402

if __name__ == "__main__":
    usage = llm.Usage()
    index, stats = CorpusIndex.build(usage)
    print("index:", stats, "| tokens/cost:", {k: round(v, 5) for k, v in usage.totals().items()})
    if "--query" in sys.argv:
        q = sys.argv[sys.argv.index("--query") + 1]
        for chunk, score in index.search(q, k=5, filters={"kind": "feature"}, usage=usage):
            print(f"  {score:.4f} {chunk.id:8} {chunk.metadata['size']:2} {chunk.metadata['feature']}")

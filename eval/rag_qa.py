"""The guideline assistant as a question-answering path, for RAGAS: a question goes to the same retriever the agents use, then to an answer with citations in the agents' style.
Searches the corpus and the proposal sections together, with no filters, since a golden question names no document type.

Usage: RETRIEVAL_MODE=dense|hybrid python eval/rag_qa.py   Writes eval/results/rag_<mode>.jsonl, and retrieval hits against the golden source chunks.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import llm, retrieval, tracing  # noqa: E402
from app.config import CORPUS_DIR  # noqa: E402

K = 5
PROMPT = """You answer questions about a software delivery team's estimation guideline, past projects, glossary and proposal style, using only the numbered context below.
Say so plainly if the context does not answer the question. Cite the chunk ID in square brackets after each claim. Be brief. Never add a fact the context does not state."""


def index() -> retrieval.CorpusIndex:
    corpus, proposal = retrieval.load_corpus(), retrieval.parse_sectioned(CORPUS_DIR / "proposal_sections.md")
    a, _ = retrieval.embed_with_cache(corpus)
    b, _ = retrieval.embed_with_cache(proposal, cache_path=retrieval.PROPOSAL_EMBED_CACHE)
    chunks, vectors = corpus + proposal, np.vstack([a, b])
    if os.environ.get("RETRIEVAL_MODE") == "hybrid_tuned":     # the metadata filter: a guideline question does not search one-line past-project rows, as the agents' own searches do not
        keep = [i for i, c in enumerate(chunks) if c.metadata.get("kind") != "feature"]
        chunks, vectors = [chunks[i] for i in keep], vectors[keep]
    return retrieval.CorpusIndex(chunks, vectors)


def main() -> None:
    mode = os.environ.get("RETRIEVAL_MODE", "hybrid")
    golden = [json.loads(line) for line in (ROOT / "eval" / "golden" / "rag_golden.jsonl").read_text().splitlines()]
    usage = llm.Usage()
    idx = index()
    rows = []
    with tracing.context(document_set="rag_golden", flow="rag_qa", retrieval_mode=mode):
        for item in golden:
            with tracing.span(f"rag_qa:{item['id']}", inputs={"id": item["id"], "retrieval_mode": mode}):
                hits = idx.search(item["question"], k=K, usage=usage)
                context = "\n\n".join(f"[{c.id}] {c.text}" for c, _ in hits)
                resp = llm.chat([{"role": "system", "content": PROMPT}, {"role": "user", "content": f"<context>\n{context}\n</context>\n\nQuestion: {item['question']}"}], usage=usage, node="rag_answer")
            answer = resp.choices[0].message.content if hasattr(resp, "choices") else str(resp)
            ids = [c.id for c, _ in hits]
            rows.append({**item, "mode": mode, "retrieved_ids": ids, "retrieved_contexts": [c.text for c, _ in hits], "answer": answer,
                         "source_hit": bool(set(ids) & set(item["reference_contexts"])), "source_recall": len(set(ids) & set(item["reference_contexts"])) / max(len(item["reference_contexts"]), 1)})
    tracing.flush()
    out = ROOT / "eval" / "results" / f"rag_{mode}.jsonl"
    out.write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(mode, "items", len(rows), "hit@5", round(sum(r["source_hit"] for r in rows) / len(rows), 3), "source recall", round(sum(r["source_recall"] for r in rows) / len(rows), 3), "USD", round(usage.totals()["est_usd"], 4))


if __name__ == "__main__":
    main()

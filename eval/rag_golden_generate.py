"""RAGAS synthetic test set over the corpus chunks: single-hop and multi-hop. Run in its own environment (.venv-ragas), which the app and the image never use.
Writes eval/golden/rag_candidates.jsonl and eval/golden/review_sheet.csv for a person to review. Unreviewed items are never scored. Stops above the cost limit.

Usage: .venv-ragas/bin/python eval/rag_golden_generate.py [size]
"""
import csv
import json
import os
import random
import sys
from pathlib import Path

os.environ["RAGAS_DO_NOT_TRACK"] = "true"
os.environ.pop("LANGSMITH_TRACING", None)

from dotenv import load_dotenv  # noqa: E402
from langchain_core.callbacks import BaseCallbackHandler  # noqa: E402
from langchain_core.documents import Document  # noqa: E402
from langchain_openai import AzureChatOpenAI, AzureOpenAIEmbeddings  # noqa: E402
from ragas.embeddings import LangchainEmbeddingsWrapper  # noqa: E402
from ragas.llms import LangchainLLMWrapper  # noqa: E402
from ragas.run_config import RunConfig  # noqa: E402
from ragas.testset import TestsetGenerator  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GOLD = ROOT / "eval" / "golden"
load_dotenv(ROOT / ".env", override=False)
LIMIT_USD = 0.40
IN, OUT = 0.40, 1.60     # gpt-4.1-mini, USD per million tokens: config/pricing.yaml


class Spend(BaseCallbackHandler):
    raise_error = True

    def __init__(self):
        self.prompt = self.completion = self.calls = 0

    @property
    def usd(self) -> float:
        return (self.prompt * IN + self.completion * OUT) / 1e6

    def on_llm_end(self, response, **kwargs):
        usage = (response.llm_output or {}).get("token_usage") or {}
        if not usage:
            for group in response.generations:
                for g in group:
                    meta = getattr(getattr(g, "message", None), "usage_metadata", None) or {}
                    usage = {"prompt_tokens": meta.get("input_tokens", 0), "completion_tokens": meta.get("output_tokens", 0)}
        self.prompt += usage.get("prompt_tokens", 0)
        self.completion += usage.get("completion_tokens", 0)
        self.calls += 1
        if self.usd > LIMIT_USD:
            raise RuntimeError(f"Stopped at USD {self.usd:.3f}: above the limit of USD {LIMIT_USD}.")


def main() -> None:
    size = int(sys.argv[1]) if len(sys.argv) > 1 else 36
    chunks = json.loads((GOLD / "corpus_chunks.json").read_text())
    spend = Spend()
    llm = AzureChatOpenAI(azure_deployment=os.environ["AZURE_OPENAI_MAIN_DEPLOYMENT"], api_version=os.environ["AZURE_OPENAI_API_VERSION"], temperature=0, callbacks=[spend])
    embed = AzureOpenAIEmbeddings(azure_deployment=os.environ["AZURE_OPENAI_EMBED_DEPLOYMENT"], api_version=os.environ["AZURE_OPENAI_API_VERSION"])
    generator = TestsetGenerator(llm=LangchainLLMWrapper(llm), embedding_model=LangchainEmbeddingsWrapper(embed))
    docs = [Document(page_content=c["text"], metadata={"chunk_id": c["id"]}) for c in chunks]
    random.seed(7)
    testset = generator.generate_with_langchain_docs(docs, testset_size=size, run_config=RunConfig(max_workers=4, timeout=120, max_retries=3), raise_exceptions=False)
    rows = testset.to_pandas().to_dict("records")

    def chunk_ids(contexts) -> list[str]:
        """Which corpus chunks a generated question was drawn from, by their words: RAGAS splits long chunks and gives back the pieces."""
        found = []
        for context in contexts:
            head = " ".join(str(context).split())[:80]
            found += [c["id"] for c in chunks if head and head in " ".join(c["text"].split())]
        return list(dict.fromkeys(found))

    items = [{"id": f"G{i:02d}", "question": r["user_input"], "reference_answer": r["reference"], "reference_contexts": chunk_ids(r["reference_contexts"]), "query_type": r.get("synthesizer_name", "")} for i, r in enumerate(rows, 1)]
    (GOLD / "rag_candidates.jsonl").write_text("".join(json.dumps(x) + "\n" for x in items))
    with (GOLD / "review_sheet.csv").open("w", newline="") as f:
        out = csv.writer(f)
        out.writerow(["id", "query_type", "question", "reference_answer", "source_chunks", "decision (keep/edit/drop)", "edited_question", "edited_answer", "note"])
        for x in items:
            out.writerow([x["id"], x["query_type"], x["question"], x["reference_answer"], "; ".join(x["reference_contexts"]), "", "", "", ""])
    print(f"{len(items)} items, {spend.calls} LLM calls, {spend.prompt} prompt and {spend.completion} completion tokens, USD {spend.usd:.3f} (embeddings extra, under USD 0.001)")


if __name__ == "__main__":
    main()

"""Scores eval/results/rag_<mode>.jsonl with RAGAS: faithfulness, answer relevancy, context precision and context recall. The judge is gpt-4.1-mini. Runs in .venv-ragas.
Usage: .venv-ragas/bin/python eval/rag_score.py dense hybrid   Stops above the cost limit.
"""
import json
import os
import sys
from pathlib import Path

os.environ["RAGAS_DO_NOT_TRACK"] = "true"
os.environ.pop("LANGSMITH_TRACING", None)

from dotenv import load_dotenv  # noqa: E402
from langchain_core.callbacks import BaseCallbackHandler  # noqa: E402
from langchain_openai import AzureChatOpenAI, AzureOpenAIEmbeddings  # noqa: E402
from ragas import EvaluationDataset, SingleTurnSample, evaluate  # noqa: E402
from ragas.embeddings import LangchainEmbeddingsWrapper  # noqa: E402
from ragas.llms import LangchainLLMWrapper  # noqa: E402
from ragas.metrics import Faithfulness, LLMContextPrecisionWithReference, LLMContextRecall, ResponseRelevancy  # noqa: E402
from ragas.run_config import RunConfig  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)
LIMIT_USD = 0.55
IN, OUT = 0.40, 1.60


class Spend(BaseCallbackHandler):
    raise_error = True

    def __init__(self):
        self.prompt = self.completion = 0

    @property
    def usd(self) -> float:
        return (self.prompt * IN + self.completion * OUT) / 1e6

    def on_llm_end(self, response, **kwargs):
        usage = (response.llm_output or {}).get("token_usage") or {}
        self.prompt += usage.get("prompt_tokens", 0)
        self.completion += usage.get("completion_tokens", 0)
        if self.usd > LIMIT_USD:
            raise RuntimeError(f"Stopped at USD {self.usd:.3f}: above the limit of USD {LIMIT_USD}.")


def main() -> None:
    spend = Spend()
    llm = LangchainLLMWrapper(AzureChatOpenAI(azure_deployment=os.environ["AZURE_OPENAI_MAIN_DEPLOYMENT"], api_version=os.environ["AZURE_OPENAI_API_VERSION"], temperature=0, callbacks=[spend]))
    embed = LangchainEmbeddingsWrapper(AzureOpenAIEmbeddings(azure_deployment=os.environ["AZURE_OPENAI_EMBED_DEPLOYMENT"], api_version=os.environ["AZURE_OPENAI_API_VERSION"]))
    context_only, answer_only = "--context-only" in sys.argv, "--answer-only" in sys.argv
    for mode in (a for a in sys.argv[1:] if not a.startswith("--")):
        rows = [json.loads(line) for line in (ROOT / "eval" / "results" / f"rag_{mode}.jsonl").read_text().splitlines()]
        data = EvaluationDataset([SingleTurnSample(user_input=r["question"], response=r["answer"], retrieved_contexts=r["retrieved_contexts"], reference=r["reference_answer"]) for r in rows])
        metrics = [Faithfulness(), ResponseRelevancy(strictness=1)] if answer_only else [LLMContextPrecisionWithReference(), LLMContextRecall()] if context_only else [Faithfulness(), ResponseRelevancy(strictness=1), LLMContextPrecisionWithReference(), LLMContextRecall()]
        before = spend.usd
        result = evaluate(data, metrics=metrics, llm=llm, embeddings=embed, run_config=RunConfig(max_workers=4, timeout=120, max_retries=3), raise_exceptions=False, show_progress=False)
        table = result.to_pandas()
        names = [m for m in ("faithfulness", "answer_relevancy", "llm_context_precision_with_reference", "context_recall") if m in table.columns]
        scores = [{"id": r["id"], "query_type": r["query_type"], **{m: (None if table.loc[i, m] != table.loc[i, m] else float(table.loc[i, m])) for m in names}} for i, r in enumerate(rows)]
        target = ROOT / "eval" / "results" / f"rag_scores_{mode}.json"
        if answer_only and target.exists():     # beside the context metrics already scored
            earlier = {x["id"]: x for x in json.loads(target.read_text())}
            scores = [{**earlier.get(x["id"], {}), **x} for x in scores]
        target.write_text(json.dumps(scores, indent=1))
        mean = {m: round(sum(s[m] for s in scores if s[m] is not None) / max(sum(s[m] is not None for s in scores), 1), 3) for m in names}
        print(mode, mean, "missing", sum(v is None for s in scores for v in s.values()), f"USD {spend.usd - before:.3f}", flush=True)
    print(f"Total USD {spend.usd:.3f}")


if __name__ == "__main__":
    main()

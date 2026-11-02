"""Fills in the candidates' source chunks by their words, flags items worth a closer look, and rewrites the review sheet. No LLM.
RAGAS gives multi-hop sources as rewritten pieces, so sources are the chunks that best hold the reference answer's words. A flag is only a pointer: the decision is the reviewer's.

Usage: python eval/rag_golden_sheet.py
"""
import csv
import json
import re
from pathlib import Path

GOLD = Path(__file__).resolve().parent / "golden"
STOP = set("the and for with that this are was were have has its their from into when what how does which about each such can may will not any all more than also there these those been being should must they them used use using within".split())
BROKEN = re.compile(r"\b(?:meen|explane|notifcations|dashbord|defult|smal)\b|^(?:what|how|can)\b(?=.*\b(?:mean|show)\b(?!ing))(?!.*\b(?:does|do|did)\b)", re.I)


def words(text: str) -> set[str]:
    return {w[:7] for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in STOP}


def main() -> None:
    chunks = [{**c, "words": words(c["text"])} for c in json.loads((GOLD / "corpus_chunks.json").read_text())]
    items = [json.loads(line) for line in (GOLD / "rag_candidates.jsonl").read_text().splitlines()]
    with (GOLD / "review_sheet.csv").open("w", newline="") as f:
        out = csv.writer(f)
        out.writerow(["id", "query_type", "question", "reference_answer", "source_chunks", "answer_words_in_sources", "look_closer", "decision (keep/edit/drop)", "edited_question", "edited_answer", "note"])
        for item in items:
            answer = words(item["reference_answer"])
            if not item["reference_contexts"]:
                ranked = sorted(chunks, key=lambda c: -len(answer & c["words"]))
                picked, covered = [], set()
                for c in ranked[:8]:     # greedy: each chunk must add something
                    if len(answer & c["words"] - covered) >= max(3, len(answer) // 10) and len(picked) < 3:
                        picked.append(c["id"])
                        covered |= answer & c["words"]
                item["reference_contexts"], item["contexts_matched_by_words"] = picked, True
            found = set().union(*[c["words"] for c in chunks if c["id"] in item["reference_contexts"]] or [set()])
            support = len(answer & found) / len(answer) if answer else 0.0
            flags = []
            if BROKEN.search(item["question"]):
                flags.append("question badly worded")
            if support < 0.7:
                flags.append("answer may go beyond its sources")
            if len(item["reference_answer"].split()) > 140:
                flags.append("long answer")
            out.writerow([item["id"], item["query_type"].split("_query")[0], item["question"], item["reference_answer"], "; ".join(item["reference_contexts"]), f"{support:.0%}", "; ".join(flags), "", "", "", ""])
            item["answer_words_in_sources"] = round(support, 2)
    (GOLD / "rag_candidates.jsonl").write_text("".join(json.dumps(x) + "\n" for x in items))
    print(len(items), "items; flagged", sum(1 for i in items if i["answer_words_in_sources"] < 0.7), "for support")


if __name__ == "__main__":
    main()

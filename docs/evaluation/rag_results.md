# RAG evaluation with RAGAS

**Result: dense-only retrieval scored higher than hybrid, the production setting, on three of four metrics. Production stays on hybrid by decision: the agents search with filters, so this unfiltered test is not how they use it, and changing it now would change what sizing retrieves before the demo.**

## Method

- **Golden set:** 31 reviewed items (11 single-hop, 20 multi-hop) from 35 RAGAS candidates. See `eval/golden/review_log.md`.
- **Path:** `eval/rag_qa.py`: question, then retrieval (top 5), then an answer with citations. It searches the corpus and proposal sections together, unfiltered.
- **Judge:** `gpt-4.1-mini`, RAGAS 0.3.9. The answer model is the same, so the judge is not independent.
- **Commands:** `RETRIEVAL_MODE=<mode> python eval/rag_qa.py`, then `.venv-ragas/bin/python eval/rag_score.py <mode>`. Base commit `d2e35c4`, plus the `RETRIEVAL_MODE` switch. Per-item scores are in `eval/results/`.

## Results

| Metric | Dense (baseline) | Hybrid (production) | Hybrid minus dense | Tuned hybrid |
|---|---:|---:|---:|---:|
| Faithfulness | 0.944 | 0.908 | -0.036 | 0.919 |
| Answer relevancy | 0.775 | 0.821 | +0.046 | 0.822 |
| Context precision | 0.741 | 0.713 | -0.028 | 0.757 |
| Context recall | 0.938 | 0.892 | -0.046 | 0.966 |
| Source chunk found in top 5 (no LLM) | 0.968 | 0.871 | -0.097 | 0.968 |
| Share of source chunks found (no LLM) | 0.882 | 0.785 | -0.097 | 0.930 |

### By question type

| Type | Items | Dense precision | Hybrid precision | Dense recall | Hybrid recall |
|---|---:|---:|---:|---:|---:|
| Single-hop | 11 | 0.973 | 0.773 | 0.970 | 0.970 |
| Multi-hop, specific | 12 | 0.651 | 0.750 | 1.000 | 0.808 |
| Multi-hop, abstract | 8 | 0.559 | 0.574 | 0.802 | 0.909 |

## Why hybrid lost

These are likely reasons, not measured causes. The per-item results show the third directly (G25, G19); the first two are inferences.

- **A small, consistently worded corpus.** The guideline, rubrics and patterns use the same vocabulary throughout, so dense retrieval has little gap for keyword search to fill.
- **BM25 noise from shared terms.** Size letters such as S, M and L, and words such as "rubric", appear in many chunks, so they rank near-identical chunks closely.
- **Repeated identifiers and filler.** Every one-line feature row of a past project repeats that project's code, so a question naming "P03" pulled in rows such as `P03-F16` and pushed out the summary and exclusions that answer it. Question filler ("how", "what", "explain") added to this.

## The targeted change

Two parts, tested only as `RETRIEVAL_MODE=hybrid_tuned`: BM25 ignores filler words, and the search excludes one-line feature rows, as a metadata filter.

## How far to trust this

- **The tuned variant has a built-in advantage.** RAGAS generated its questions from a corpus without the feature rows, so excluding them can only help on this set, and a question about one specific past feature is not represented. Its lead over dense on context metrics should not be read as a general result. Dense still leads it on faithfulness (0.944 against 0.919).
- **Unfiltered is not how the agents search.** Production calls filter by document type, so unfiltered hybrid here is harsher than its real use. Whether hybrid helps or hurts those filtered searches was not measured.
- **The gaps are small on 31 items.** They are indicative, and the single-hop precision drop is the clearest of them.
- **Synthetic questions.** Multi-hop source chunks were matched by their words, and RAGAS's wording favours embeddings to some degree.

## Decision

Production stays on hybrid, as decided on 2 Nov 2026. `RETRIEVAL_MODE=hybrid_tuned` is evaluation only.

## Examples

| Question | Dense | Hybrid | Tuned | Source |
|---|---|---|---|---|
| G04: what the rubric defines for an L workflow | `sizing_rubric_workflow` first | `sizing_rubric_workflow` first | `sizing_rubric_workflow` first | `sizing_rubric_workflow` |
| G25: overview of past project P03 | `P03-TOR_summary`, `P03-Exclusions`, then rows | Five `P03-F` rows, no source chunk | `P03-Exclusions`, `P03-TOR_summary`, others | `P03-TOR_summary`, `P03-Exclusions` |
| G19: auto-routing in the request tracker | `P06-TOR_summary` among rows | Five `P06-F` rows, no source chunk | `patterns-case_ticket_tracking`, `P06-TOR_summary`, others | Both of those |

Traces are in LangSmith under `flow=rag_qa`, with `retrieval_mode` in the metadata.

## Cost

USD 0.632 in total: answers 0.045, scoring 0.587.

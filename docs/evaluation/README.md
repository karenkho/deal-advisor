# Evaluation index

**On unseen TORs the system reached full feature recall after critic review on all 3 at the final run, with 14 of 19 planted flaws and 15 of 21 expected risks. The held-out baseline exposed one run-stopping failure, a weak injection guard and a bulletin fault. The first two are fixed; the third is fixed only on the dev set. Names mid-sentence are not masked.**

## Method

- **Dev set:** HRIS, TOR A, TOR B, and the format TORs: delivery app (bullets), clinic (table), library (numbered), Liwanag RFP (multi-column table), museum (spaced table), parking (numbered Word clauses). Used for tuning. Any TOR used for tuning joins it.
- **Held-out set:** 3 TORs and a bulletin, written answer key first and frozen by hash ([test_set.md](test_set.md)), run for baseline and final, with item-level results read only after the final run.
- **Measured:** the app's own intake path on a throwaway database. Extraction is reported before the critic, and end to end after it.
- **Judges:** `gpt-4.1-mini` for feature matching, flaws and RAGAS.

## Results

| Area | Result | Details |
|---|---|---|
| RAG, 31 reviewed questions | Dense scored above hybrid on 3 of 4 metrics; production stays hybrid | [rag_results.md](rag_results.md) |
| Task, dev | Full final recall on all 8; flaws 14/14; risks 8/8 on HRIS | [task_results.md](task_results.md) |
| Task, held-out baseline | 2 of 3 completed, 1 failed (Azure content filter) | The same |
| Task, held-out final | 3 of 3 completed; full final recall; flaws 14/19; risks 15/21; sizes 51/57 within one tier | The same |
| Scenarios | 34 of 34, including designed failures | [scenario_results.md](scenario_results.md) |
| Suitability prompts | V3 0.80 against V1 0.17 on 30 labels; what users see scores 0.63 | [prompt_comparison.md](prompt_comparison.md) |
| Caching | Repeat run: about USD 0.15 and 195 s down to 0 and 2 s | [caching.md](caching.md) |
| Extraction design | AI-first plus rule checker, chosen from three measured approaches | [extraction_approaches.md](extraction_approaches.md) |
| Debugged failures | The extraction failure through to the design change; the merge pass | [debugged_failure.md](debugged_failure.md) |
| Guardrails | 20, each with its proof, and a gap found by the held-out set | [guardrails.md](guardrails.md) |
| Agents | What each one actually does, checked against traces | [agent_behaviour.md](agent_behaviour.md) |
| Iterations | Every change with its before and after, I-01 to I-21 | [iterations.md](iterations.md) |

## Baseline against final, held-out

| TOR | Recall, stated | Recall, final | Precision, stated | BRs (critic) | Size exact | Within one | Flaws | Risks | USD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| alumni | 14/15 | 15/15 | 0.88 | 17 (1) | 4/15 | 13/15 | 5/6 | 2/6 | 0.085 |
| fleet | failed | | | | | | | | |
| center | 20/21 | 21/21 | 0.91 | 26 (4) | 11/21 | 20/21 | 4/6 | 4/6 | 0.128 |

| TOR | Recall, stated | Recall, final | Precision, stated | BRs (critic) | Size exact | Within one | Flaws | Risks | USD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| alumni | 14/15 | 15/15 | 0.88 | 20 (4) | 5/15 | 13/15 | 5/6 | 2/6 | 0.097 |
| fleet | 20/21 | 21/21 | 0.83 | 25 (1) | 6/21 | 18/21 | 5/7 | 9/9 | 0.122 |
| center | 21/21 | 21/21 | 0.95 | 26 (4) | 12/21 | 20/21 | 4/6 | 4/6 | 0.127 |

The first table is the baseline and the second the final. Every change is explained in [task_results.md](task_results.md).

## Traces

Images in [`docs/evidence/traces/`](../evidence/traces/) are drawn from the data LangSmith stored, not screenshots of its site. See [tracing.md](tracing.md). Links are private.

## Limitations

- **Synthetic data.** Every TOR is synthetic, and keys are by one author, the same one who wrote the held-out TORs.
- **Small held-out set:** 3 TORs, about 5 pages each, shorter than planned, by decision.
- **The held-out set was not untouched.** The fleet failure's error code was read, and then its cause prompted fixes built on dev TORs (I-18, I-21), all logged. Its sections were also sent one extra time to read that code, and its stored guard findings were read. Its final injection result is therefore not independent.
- **Known faults at the final run:**
  - A contact name written mid-sentence is not masked, and it reaches the LLM.
  - The held-out bulletin's changed value did not land.
  - Some conflicts and vague requirements are missed.
- **Single-model judge,** the same model family as the app, and it varies between calls.
- **Generator and runtime are different models.** Claude wrote the TORs and the runtime is `gpt-4.1-mini`, so the wording may not match how real clients write.
- **The critic varies between runs** on the same extraction (C-04).
- **RAG results come from an unfiltered test,** which is not how the agents search, and the golden set favours embeddings to some degree.
- **Labels by one person,** skewed to one category, with `deterministic_automated` not covered.
- **What users see on the sample scores below the chosen prompt's test figure,** and batching as the cause is untested.

## Cost

About USD 10.07 of LLM from Azure's token meter, which may lag, and about USD 0.45 of hosting, against a budget of USD 15. Of that, this evaluation's RAG, task, scenario, prompt and caching steps cost under USD 2.

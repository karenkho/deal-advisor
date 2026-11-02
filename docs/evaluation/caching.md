# Caching evidence

**A repeat run of the same document costs almost nothing and takes about 2 seconds, against about USD 0.15 and 195 seconds cold. A bid bulletin changes 2 BRs in 0.2 seconds, where reprocessing would size every BR again.**

## Method

`python eval/caching_eval.py`, on HRIS through the upload path, with a throwaway database and temporary cache files. Base commit `4c99b5b`, on `gpt-4.1-mini` and `gpt-4.1-nano`.

**Departures from the plan's 3 repetitions:** conditions that cost money ran once or twice, to stay within the USD 0.40 step limit. Free ones ran 3 times.

## Results

| Condition | Runs | Seconds (median) | Tokens | USD | Notes |
|---|---:|---:|---:|---:|---|
| Cold: nothing reused | 1 | 195 | see below | about 0.15 | As the deployed app reads a new document |
| Extraction stored, the rest fresh | 1 | 187 | see below | about 0.135 | Extraction is about 10% of a run's cost |
| Item caches warm, whole-run result removed | 3 | 3.5 | about 1,500 | 0.00 | Covers the plan's classification-store row: see below |
| Whole-run result reused | 3 | 2.2 | about 1,500 | 0.00 | |
| Bundled sample | 3 | 2.2 (14.5 first) | | under 0.01 first | First run calls the risk agent: see below |
| Assumption edit, recalculated | 3 | 2.2 | 0 | 0.00 | Code only |
| Reopen an opportunity: database reads | 3 | 0.014 | 0 | 0.00 | |
| Reopen an opportunity: whole screen | 3 | 0.09 (0.79 first) | 0 | 0.00 | Measured with Streamlit's test runner, with no browser or network |
| Bulletin, incremental | 1 | 0.23 | 0 | 0.00 | 2 BRs changed, by rule |
| Bulletin, full reprocess | 1 | 116 | | over 0.04 | Every BR sized again. A local cache served part of it, so this understates a truly cold run |
| Embeddings, corpus unchanged | 3 | 0.06 | 0 | 0.00 | 0 of 348 chunks re-embedded |
| Embeddings, one chunk changed | 1 | 0.50 | | under 0.001 | 1 of 348 chunks re-embedded |

**Estimated saving per repeated run:** about USD 0.15 and 190 seconds.

### Where a cold run goes

| Step | Seconds | USD recorded |
|---|---:|---:|
| Intake guard | 2.5 | 0.000 |
| Extraction and consolidation | 21 | 0.017 |
| Router | 10 | not reliable |
| Sizing agent | 61 | not reliable |
| Coverage critic | 4.6 | 0.003 |
| AI suitability | 52 | 0.041 |
| BRD extraction | 28 | 0.014 |
| Risk agent | 14 | 0.005 |

## How these figures were corrected

- **The first "cold" run was not cold.** This machine has a local item cache that the deployed image does not have, and it served part of the run (123 seconds). The row above was rerun with `EXTRACT_CACHE=off`. The first runs are kept in `eval/results/caching.json`.
- **USD for the first two rows comes from Azure's token meter,** which moved USD 0.285 across those two runs, split by the extraction difference. It is an estimate of each row. Summing recorded steps gave USD 0.084, because of the next point.
- **Step costs undercounted the router and sizing.** A step run twice by the critic's loop kept only its last pass (I-20). Run totals were always right: USD 0.1138 in LangSmith against 0.1139 recorded on TOR B. For scale, sizing was 46% of that run. Those two rows in the table predate the fix and stay marked as not reliable.

## What doesn't match the plan

- **"Expected 0 Azure calls" is not quite met.** The warm rows still show about 1,500 tokens, at no measurable cost. Which call this is was not traced.
- **The classification store has no row of its own.** In production extraction a section's answers come from the LLM read, so per-statement classification now applies only to supplementary documents. The item-caches row is the nearest measure.
- **The bundled sample is no longer free on its first run.** The AI tooling lines changed the sample's figures, so one of its 12 risk texts no longer matches the shipped cache and the risk agent runs, at under USD 0.01, while the caption on screen says 0 Azure calls. The shipped file was left unchanged.

## Invalidation policy

| Cache | Key | What invalidates it | Where it lives |
|---|---|---|---|
| Section extraction | Section text, prompt, model | Any of those | On the opportunity, in the database |
| Whole-run result (upload) | File hashes, config files, models, prompt and result versions | Any of those | Process memory |
| Whole-run result (bundled sample) | The same | Any of those | `samples/results/`, shipped |
| Router decision | Name, type, description, prompt, model | Any of those | Process memory |
| Size | Name, type, statements, both sizing prompts, model | Any of those | Process memory |
| Coverage critic | The TOR's statements, prompt, model | Any of those. A bulletin does not re-run it | Process memory |
| Intake classifier | Cleaned text, light model | Either | Process memory |
| Suitability | The feature's unit, activities, version, model, categories | Any of those | Process memory |
| Risk narrative | Rule, severity, source data, version, model | Any of those. A changed figure is a miss | Shipped for the sample, memory for uploads |
| Embeddings | The chunk's content hash | The chunk's text. Removed chunks are pruned | `.cache/`, shipped |
| Rendered documents | Version content and its history | A new version or status | Process memory |

Rates are never part of a cache. A container restart clears process memory, and a document's stored extraction survives because it is in the database.

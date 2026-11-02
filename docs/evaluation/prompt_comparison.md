# Prompt comparison: AI suitability (V1 vs V3)

**Result: V3, definitions plus retrieved guidance, agrees far better with the labels than V1 (0.80 against 0.17). V3 is production and stays. But the answers users see on the bundled sample score lower (0.47, or 0.63 after rules), because they were produced differently: see below.**

## Labelling standard

Karen labelled 30 HRIS activities without seeing the app's answers. Her standard:

> realistic AI suitability for a team using current AI tools with human review, not current practice and not maximum automation.

| Human label | Items |
|---|---:|
| `ai_first_with_review` | 23 |
| `human_led` | 4 |
| `ai_assisted` | 3 |
| `deterministic_automated` | 0 |

## Results

`python eval/prompt_compare.py`, model `gpt-4.1-mini`, on the same 30 items, one activity at a time. V2 is cut (addendum E). Per-item answers are in `eval/results/prompt_comparison.json`.

| Variant | Stage | Accuracy | Kappa | human_led | ai_assisted | ai_first_with_review |
|---|---|---:|---:|---:|---:|---:|
| V1: definitions only | LLM | 0.167 | 0.105 | 4/4 | 1/3 | 0/23 |
| V1 | After rules | 0.500 | 0.246 | 2/4 | 1/3 | 12/23 |
| V3: definitions + guidance | LLM | 0.800 | 0.581 | 3/4 | 3/3 | 18/23 |
| V3 | After rules | 0.800 | 0.588 | 2/4 | 3/3 | 19/23 |
| Stored production answers | LLM | 0.467 | 0.241 | 4/4 | 1/3 | 9/23 |
| Stored production answers | After rules | 0.633 | 0.332 | 2/4 | 1/3 | 16/23 |

| Variant | USD | Tokens | Seconds per item |
|---|---:|---:|---:|
| V1 | 0.0079 | 14,877 | 1.51 |
| V3 | 0.0141 | 30,406 | 1.77 |

## Reading this

- **Why V1 fails.** Without guidance it called 13 of 30 `deterministic_automated`, a category absent from the labels, and none `ai_first_with_review`, the category of 23 labels. The deterministic rules recover part of this (0.50).
- **The rules matter little to V3.** Accuracy stays at 0.80. They help one `ai_first_with_review` item and cost one `human_led`.
- **`deterministic_automated`:** V3's 1 LLM prediction and 3 after rules count as disagreement, as instructed.

**Chosen: V3,** already production, on agreement and kappa, at about twice the cost of V1 per item.

## Why the stored answers score lower

Explained from the records, with no LLM spend. All 30 stored answers match HRIS record version 254, which marks suitability as served from its cache.

| | Stored answers | V3 in this test |
|---|---|---|
| Prompt version | `SUITABILITY_VERSION` s4 | Same prompt text |
| Model | `gpt-4.1-mini` (2025-04-14) | Same |
| Guidance | Same retrieval query | Same |
| How items are sent | Whole features, up to 6 in a batch, each listing all its activities | One activity at a time |

So the prompt and model do not explain the gap. The remaining difference is the batching, along with what the feature text was when the answers were cached. **Batching as the cause is the likely reading and was not tested,** since that would need new LLM runs. The bundled sample's answers are not regenerated, as decided.

**What this means for the numbers:** V1 against V3 compares prompts fairly, on the same footing. The figure for what users see is 0.633, not 0.80.

## Limitations


- **`deterministic_automated` is not covered by this sample.** None of the 30 labels uses it, so per-category results cannot be reported for it. Any AI prediction of that category counts as disagreement.
- **The labels are skewed.** 23 of 30 are one category, so accuracy alone flatters a model that predicts it often. Cohen's kappa is reported beside it, and it is unstable on the two small categories.
- **One labeller, one domain.** All 30 come from the HRIS dev TOR.
- **What users see scores lower than the chosen variant here:** 0.633 after rules on the bundled sample.

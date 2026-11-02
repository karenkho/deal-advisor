# Tracing (Step 5.1b)

LangSmith, project `deal-advisor-poc`, code in `app/tracing.py`. Off unless `LANGSMITH_TRACING=true`. Enabled on the public app from revision 24 on 2 Nov 2026, with the key held as a container secret. A public smoke run's spans were checked afterwards: no emails, rate fields or clause text in 100 spans.

## What is traced

| Layer | Spans |
|---|---|
| Agents | Every LangGraph node (automatic), under `analyze_requirements` |
| Named tools | `analyze_requirements`, `retrieve_estimation_guidelines`, `assess_ai_suitability`, `calculate_effort`, `generate_project_schedule`, `calculate_cost_and_margin`, `evaluate_deal_risks`, `generate_proposal`, `validate_document_consistency`, `retrieve_proposal_section`, and each tool an agent calls |
| Retrieval | `retrieve` (query, chunk IDs, scores), `embed:*` |
| LLM | `llm:<node>`: masked messages, answer, tokens, model, cost |
| v3 actions | Status transitions, BRD version save and approval, stability check, re-upload diff, estimate generation and re-estimate, consistency check, MSA fill, Drive publish |

Metadata: run ID, opportunity, document set, prompt version, models, rates source, cost, role, artifact version. Scenario and profile appear where they apply. Cache hit or miss is `from_cache` in the outputs of `analyze_requirements` and `assess_ai_suitability`.

Calculators called in a search loop are traced for their first 6 calls per trace. The full `generate_project_schedule` count was 571 in one run before this limit.

## Privacy

Every payload passes one sanitizer on the client, including LangGraph's automatic traces, whose first input holds the unmasked pages.

| Rule | How |
|---|---|
| Page text and document bytes | Dropped; counts only |
| Money fields | Withheld by field name, also inside JSON held in a string |
| Rate values | Registered when rates load; withheld wherever they appear, including free text |
| Currency amounts in text | Withheld |
| MSA | Field names and counts only, never clause text or field values |
| Errors | Exception type only |
| Unknown objects | Type name only |

## Evidence

Live check on a throwaway database: one cached HRIS flow through Contract-Ready, one live TOR A run, one TOR B intake guard call. 11 tests in `tests/test_tracing.py`. With the sanitizer switched off, 4 of the 9 written at that point failed, as they should.

Scan of what LangSmith stored, read back through its API (981 runs, 1.38 MB, TOR A and HRIS):

| Looked for | Found |
|---|---:|
| `.env` secret values | 0 |
| Illustrative rate values | 0 |
| Values of price, margin, cost and floor fields | 0; all `[withheld]` |
| "PHP" followed by a number in LLM spans | 0 |
| Made-up MSA template text | 0 |
| This machine's host name | 0 |

TOR B separately, with planted PII and raw pages passed to a span on purpose: name, phone and email 0 times; `[NAME]`, `[EMAIL]` and `[MOBILE]` each present.

Images in [`docs/evidence/traces/`](../evidence/traces/) are drawn from that API data. They are not screenshots of the LangSmith site, which needs a signed-in browser. Links for real screenshots are in `docs/private/trace_links.md`.

Limit: the sanitizer works from field names, patterns and known rate values. A money figure in free text without "PHP" would pass unless it equals a known rate.

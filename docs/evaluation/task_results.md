# Task evaluation

**Dev set: full recall on all 8 TORs. Held-out baseline: 2 of 3 completed, 1 failed (Azure's content filter refused one passage, and the app then stopped the run). Held-out final: 3 of 3 completed, all with full final recall, 14 of 19 flaws and 15 of 21 risks.**

## Method

Each TOR goes through the app's own intake path (create, upload, generate) on a throwaway PostgreSQL and a local store, then scripted Accept all (no human edits), approval and the estimate. Command: `python eval/task_eval.py dev|test <label>`. Base commit `d2e35c4`, plus the shipped-result fix (I-16).

- **Recall and precision:** the judge in `eval/quick_eval.py`, with one key feature crediting one BR. "Stated" leaves out the critic's suggestions and is the closest figure to the extraction stage; it comes from the same single run, so the held-out set is read only once.
- **Flaws:** code checks the injection and PII flaws. An LLM judge (`gpt-4.1-mini`) checks the rest against the open questions, exclusions and requirements.
- **Risks:** expected categories against the risk rules that fired in the estimate.
- **Unsupported:** stated BRs with no citation.

## Dev set

The dev set reused stored extraction, so its cost and time are lower than a first read.

| TOR | Recall, stated BRs | Recall, final | Precision, stated | BRs (critic) | Size exact | Within one | Flaws | Risks | Unsupported | Seconds | Tokens | USD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| HRIS | 25/26 | 26/26 | 0.93 | 28 (1) | 13/26 | 26/26 | 7/7 | 8/8 | 0 | 116 | 100,349 | 0.064 |
| TOR A | 16/16 | 16/16 | 0.94 | 17 (0) | 14/16 | 16/16 | n/a | n/a | 0 | 104 | 108,007 | 0.071 |
| TOR B | 16/18 | 18/18 | 0.73 | 24 (2) | 12/18 | 18/18 | 7/7 | n/a | 0 | 231 | 182,029 | 0.115 |

Format-robustness TORs, end to end (I-12 to I-14): Delivery 14/14, Clinic 12/12, Library 12/12, Museum 12/12, Liwanag RFP 24/24.

## Held-out baseline

Uncached, with tracing on. Only these scores were read; which items were missed stays in `eval/results/task_test_baseline.json`, unread, so that nothing is tuned on it.

| TOR | Recall, stated BRs | Recall, final | Precision, stated | BRs (critic) | Size exact | Within one | Flaws | Risks | Unsupported | Seconds | Tokens | USD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| alumni | 14/15 | 15/15 | 0.88 | 17 (1) | 4/15 | 13/15 | 5/6 | 2/6 | 0 | 124 | 123,794 | 0.085 |
| center | 20/21 | 21/21 | 0.91 | 26 (4) | 11/21 | 20/21 | 4/6 | 4/6 | 0 | 165 | 187,220 | 0.128 |
| fleet | failed | failed | | | | | | | | | | 0.012 on the retry |

**Bid bulletin (convention center):** 2 new BRs, 0 existing BRs changed. The added requirement was found. The superseded value was not found in any new or changed BR, so the change did not land. Reproduced and fixed on a dev bulletin as I-19; the baseline stays as measured.

### The fleet RFP failed

Two attempts, both ending in "AI service unavailable". Azure's error, read afterwards: status 400, code `content_filter`, `ResponsibleAIPolicyViolation`, jailbreak detected, on 1 of 13 sections.

The traces keep error types only, so reading the code and message needed one more send of the file's sections with its answers discarded unread. That is a third use of this TOR beyond the baseline and its retry, for USD 0.012, and no scores came from it.

**Cause:** one passage tripped Azure's prompt shield, and the app treated that as an outage and stopped. **Fix:** I-18, built and proved on a new dev TOR with its own wording. The baseline stays as measured: the final run in 5.9 includes fleet.

## Held-out final (5.9)

Run once, uncached, on commit `6fe9bd6`. The frozen files were unchanged.

| TOR | Recall, stated | Recall, final | Precision, stated | BRs (critic) | Size exact | Within one | Flaws | Risks | USD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| alumni | 14/15 | 15/15 | 0.88 | 20 (4) | 5/15 | 13/15 | 5/6 | 2/6 | 0.097 |
| fleet | 20/21 | 21/21 | 0.83 | 25 (1) | 6/21 | 18/21 | 5/7 | 9/9 | 0.122 |
| center | 21/21 | 21/21 | 0.95 | 26 (4) | 12/21 | 20/21 | 4/6 | 4/6 | 0.127 |

### Baseline against final

| What changed | Baseline | Final | Explained by |
|---|---|---|---|
| Completed | 2 of 3 | 3 of 3 | I-18 (a refused passage no longer stops a run) and I-21 (our guard now removes this line first) |
| Fleet scores | None | See above | The same two |
| Flaws, on the two TORs in both runs | 9/12 | 9/12 | No change |
| Risks, on the same two | 6/12 | 6/12 | No change |
| Alumni critic suggestions | 1 | 4 | Run-to-run variation in the critic (C-04), with no change to its stated BRs |
| Convention center stated recall | 20/21 | 21/21 | I-19 renamed numbered clauses by their own titles, which may help the judge match. Judge variation cannot be ruled out |
| Convention center precision | 0.91 | 0.95 | The same |
| Alumni and center exact sizes | 4/15, 11/21 | 5/15, 12/21 | Sizing varies between uncached runs |

**The bulletin did not improve.** Both runs: 2 new BRs, 0 changed, superseded value not found. **I-19 fixed this on the dev bulletin but did not carry over to the held-out one,** and this run is the last, so the cause stays unexplored. Reported as a known fault, not tuned further.

### What the final run missed

Read after the last run, for reporting only. Nothing was changed because of it.

| Miss | TORs | What it means |
|---|---:|---|
| Contact name not masked | 3 of 3 | Emails and mobile numbers were masked. A name written mid-sentence ("the contact person: ...") is not caught, because the rule expects it at the start of a line. **It reaches the LLM** |
| Vague requirement | Fleet | "Future-proof" wording raised no question |
| Conflict | Convention center | The two deposit percentages were not flagged |
| Risks expected | Alumni 4, center 2 | Partly the key's expectations: for example, the key counts a data-privacy risk that needs a specific open question. On center, the warranty risk did not fire on "Six (6) months" |

Injection stayed out of the output on all 3. **That result is not independent for fleet:** the fix was prompted by its failure, and the same author wrote the line. See [guardrails.md](guardrails.md).

## Reading these results

- **Final recall is high, and the critic helps it.** Held-out stated recall is 14/15, 20/21 and 21/21, reaching full recall with the critic.
- **Held-out precision (0.83 to 0.95) is close to dev,** and these TORs are shorter and less repetitive than TOR B.
- **Sizes are weaker on held-out exact match** (23/57, with 51/57 within one tier), against 39/60 and 60/60 on dev. The held-out expected sizes were written by the same author as the TORs, with no team estimating them.
- **Risks found: 15/21 on held-out, 8/8 on HRIS.** HRIS's expected list was written against the rules long ago and tuned with them, so it is the kinder measure.
- **Flaws: 14/19 on held-out, 14/14 on dev.** That gap is the clearest sign of how much the dev figures flatter the system.
- **Failures count.** One of three held-out TORs produced nothing at baseline.

## Limitations

Synthetic documents with keys by one author, a single-model judge, three held-out TORs of about 5 pages, and a judge that varies between calls (23/24 and 24/24 on one extraction, as measured earlier).

## Cost

Baseline step USD 0.485: dev 0.250, held-out 0.235 including the failed retry. Final run USD 0.346.

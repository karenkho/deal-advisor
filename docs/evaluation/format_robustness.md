# Format robustness (dev set)

This table covers the rules-only fixes (I-02, I-04), which were later replaced. The current approach is in [extraction_approaches.md](extraction_approaches.md).

Does extraction find the requirements whatever their format? Three TORs in new formats plus the three existing dev TORs. All are development data; none is held out.

Command: `python eval/format_robustness.py . <output.json>`. "Before" is commit `09ba608`, "after" is this fix. Recall is judged by the method in `eval/quick_eval.py` (one `gpt-4.1-mini` call per TOR against the answer key). The test TORs and keys come from `scripts/build_format_set.py` and `eval/dev_set/`. The full-result cache is bypassed; the statement-level cache stays on, so an unchanged statement keeps its answer.

| TOR | Recall before | Recall after | BRs before | BRs after | Critic-added before | Critic-added after | After, USD |
|---|---:|---:|---:|---:|---:|---:|---:|
| Delivery app (PDF, Label: description bullets) | 1/14 | 14/14 | 3 | 16 | 2 | 1 | 0.037 |
| Clinic (PDF, requirements matrix) | 5/12 | 12/12 | 8 | 18 | 6 | 5 | 0.040 |
| Library (DOCX, numbered paragraphs) | 0/12 | 12/12 | 0 | 13 | 0 | 0 | 0.033 |
| Water utility (PDF, module matrix, no requirement IDs; added in I-04) | not measured | 11/11 | not measured | 16 | not measured | 1 | 0.041 |
| HRIS (PDF, existing) | 26/26 | 26/26 | 29 | 29 | 2 | 2 | 0.017 |
| TOR A (PDF, existing) | 16/16 | 16/16 | 17 | 17 | 1 | 0 | 0.044 |
| TOR B (PDF, existing) | 18/18 | 18/18 | 30 | 27 | 2 | 2 | 0.082 |

Run cost: before USD 0.171, after USD 0.253 (judge calls not included).

**No regressions on the existing three.** HRIS has the same 29 BR names. TOR A's migration is now the stated §3.16 instead of a critic addition. TOR B lost 3 BRs that matched nothing in its key: Acceptance, Evaluation criteria and General quality attributes. Statements are the same except 2 "Page N of M" lines, which are no longer read as requirements.

**Limits.** The answer keys were written by the same author as the fix, and the TORs are small. The two new TORs were written after the failure was known, so these figures show the formats are handled, not how well unseen formats will be. A critic addition can match a key feature, which is why the delivery TOR scored 1/14 before. The critic still adds 5 items to the clinic TOR; they are suggestions and are not priced until accepted. In a PDF table the extractor joins cells on one line, so a BR name can be cut short (for example "Doctor schedule"); the full text stays in the statement.

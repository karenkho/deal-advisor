# How requirements are extracted: the design decision

**Decision: AI-first extraction with a rule checker (c) is the production approach.** It was the best on the new formats and did not regress HRIS, TOR A or TOR B.

## Why the approach changed

The first fix for the delivery TOR added parser rules, and rules passed every test written after that failure. Then a new RFP arrived with its features in a 3-column table with bullets inside the cells. On the deployed version the BRD got 0 of them: section headings became BRs and epics, a proposal file-size limit became BR-002, and 5 of 8 BRs were critic suggestions. The coverage guard stayed silent because the matrix section had produced a BR from its own heading.

The root problem was missing structure. Plain PDF text interleaves table columns, and rules cannot reliably rebuild that.

## The three approaches

- **(a) Rules only.** Statements by rule, with the plain PDF text.
- **(b) AI-first.** A table extractor gives intact rows. Each section goes to `gpt-4.1-mini` (temperature 0, fixed seed, structured output), which lists items with a type, a quote, a title and a module, and tags each section's role. Code checks each quote against the document.
- **(c) AI-first plus the rule checker.** As (b); then rules decide what one BR is, and what counts as covered.

## Results

Command: `python eval/extraction_compare.py <output.json>`. Measured at the end of extraction, before the critic and sizing, so critic additions do not inflate the scores. Recall and precision come from the judge in `eval/quick_eval.py`. Precision is the share of BRs that match an answer-key feature. "Heading" counts BRs and epics named after a heading that only says what kind of section it is.

| TOR | Recall a | Recall b | Recall c | Precision a | Precision b | Precision c | BRs a | BRs b | BRs c | Heading a | Heading b | Heading c | USD a | USD b, c |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Delivery app (bullets) | 14/14 | 12/14 | 14/14 | 0.93 | 0.75 | 1.00 | 15 | 16 | 14 | 0 | 0 | 0 | 0.003 | 0.008 |
| Clinic (table) | 12/12 | 12/12 | 12/12 | 0.92 | 0.92 | 0.92 | 13 | 13 | 13 | 0 | 0 | 0 | 0.002 | 0.004 |
| Library (numbered) | 12/12 | 12/12 | 12/12 | 0.92 | 0.75 | 0.92 | 13 | 16 | 13 | 0 | 0 | 0 | 0.002 | 0.004 |
| Liwanag RFP (3-column table) | 24/24 | 24/24 | 24/24 | 0.96 | 0.96 | 1.00 | 25 | 25 | 24 | 0 | 0 | 0 | 0.004 | 0.006 |
| HRIS | 25/26 | 26/26 | 25/26 | 0.93 | 0.43 | 0.93 | 27 | 60 | 27 | 1 | 0 | 0 | 0.011 | 0.020 |
| TOR A | 15/16 | 16/16 | 16/16 | 0.88 | 0.38 | 0.89 | 17 | 42 | 18 | 2 | 0 | 0 | 0.005 | 0.008 |
| TOR B | 16/18 | 17/18 | 16/18 | 0.59 | 0.20 | 0.54 | 27 | 80 | 28 | 0 | 0 | 0 | 0.016 | 0.027 |

USD is the extraction stage, first reading. The checker itself makes no LLM call, so (b) and (c) share a figure.

**End to end, the chosen approach** (`python eval/format_robustness.py . <output.json>`, with the critic and sizing):

| TOR | Recall | BRs | From the critic | USD |
|---|---:|---:|---:|---:|
| Delivery app (bullets) | 14/14 | 15 | 1 | 0.034 |
| Clinic (table) | 12/12 | 15 | 2 | 0.030 |
| Library (numbered) | 12/12 | 13 | 0 | 0.030 |
| Liwanag RFP (3-column table) | 24/24 | 29 | 5 | 0.069 |
| HRIS | 26/26 | 28 | 1 | 0.030 |
| TOR A | 16/16 | 17 | 0 | 0.040 |
| TOR B | 18/18 | 29 | 2 | 0.075 |

No regressions: HRIS, TOR A and TOR B all reach full recall. The items missing at the extraction stage on the existing three are implied by the TOR and not stated in it, which is what the critic adds.

## What the comparison showed

- **The AI alone reads well and groups badly.** It finds the items, but names every sentence as a capability: HRIS went to 60 BRs and TOR B to 80. Precision fell as low as 0.20.
- **Structure belongs to the document.** The checker applies it: a titled subsection is one BR, and in a module or a requirements list one row, bullet or numbered paragraph is one BR. That brought HRIS back to 27 BRs.
- **Names from code are stable.** The LLM rewords titles even at temperature 0 with a fixed seed. Across two first readings of the RFP, only 9 of 24 BR names matched before the checker named them from the document; with it, all 24 do (Jaccard 1.0). The quoted statements still differ somewhat between first readings (Jaccard 0.727). Storing each answer means a document is never read twice, so this affects only a first reading.
- **Rules score well here for a reason that does not carry over.** (a) reaches 24/24 on the RFP only because a rule for this table shape was added in the previous iteration. On the version before that rule, the same layout gave 0. Rules also do not route items by type or give a reason for a section's role. The held-out set in 5.2 is the fair test.
- **The checker can hurt.** Early versions overruled good AI output: a wrapped bullet was matched line by line and lost; the coverage guard re-read sections the AI had already accounted for and invented BRs; and letting the section role decide BR gating turned objectives into BRs. Each was found by this table.

## How it works in production

| Step | What happens |
|---|---|
| Parse structure | `pdfplumber` for PDF tables, `python-docx` for DOCX. A page without a ruled table keeps its plain text |
| Read | One LLM call per top-level section |
| Ground | Each quote must be in the document, ignoring punctuation and glyphs. Anything else is rejected and logged |
| Route | Background and scope to the Executive Summary; objectives to Objectives; bid procedure and timeline to the opportunity record; NFRs to the register |
| Persist | Each answer is stored by the hash of its section as soon as it arrives, so a retry or a restart never pays twice |
| Check | Headings never name BRs or epics. Coverage goes by section role alone: every row and bullet in a section tagged requirements must map to a BR, an NFR or an explicit decision, or it is reported. A short timeline raises a risk. Critic suggestions stay marked, with a warning if they outnumber sourced BRs |

The reviewer sees each section's role, the reason, and where its rows and bullets went in the Evidence tab. The role is stored on each statement.

A bid-procedure or evaluation section can stop a BR by its role. Name checks and overview gates still use heading patterns, as a second safeguard. The coverage check uses the role only.

## Limits

- All seven TORs are development data, and these rules were tuned on them. Only the held-out set can show how this generalizes.
- The answer keys were written by the same author as the checker.
- The judge varies between calls: the same RFP scored 23/24 and 24/24 on identical extraction, with one name listed as both missed and unmatched. These tables are from one run each.
- (a) was measured once, before the final checker changes, which did not touch its path. (b) and (c) use the same stored extraction.
- A ruled table is required: a table drawn with spacing only is not detected, and falls to the LLM as plain text.
- The bundled HRIS sample keeps its stored result, by design: an unchanged document is not re-extracted. It was evaluated separately here.
- PDF wrapping can leave an artefact in a BR title, such as "e- receipts".

## After the decision: tuning on the dev set (I-12 to I-14)

The comparison above is the decision as made, and stays as measured. Later checker changes moved (c) on TOR B from precision 0.54 and 28 BRs to 0.65 and 23 BRs, with recall unchanged at 16/18. The other six did not change. A spaced-table RFP joined the dev set at 12/12. See [iterations.md](iterations.md).

# Iteration log

Changes justified by measurement. Each has its evidence and the commit.

## I-01: Final Approval redraws (commit `8a02570`, image `v3-2`, revision 16)

**Symptom:** the public smoke test took about 29 minutes, 62% of it on one screen (see [step_5_0.md](step_5_0.md)).

**Cause:** one redraw of Final Approval ran the full consistency check four times, once directly and once per approval part. Each run rebuilt the SOW and the proposal, downloaded and filled the MSA template, and re-read all three. The container was also near its CPU limit.

**Fix, two parts:**
1. Built documents and check results are kept in process memory, keyed on hashes of the artifact versions and opportunity fields they were built from. A changed version misses by itself. A clause tick is not a change. Approving and publishing still check afresh, so an unavailable template blocks a decision even when the screen holds an earlier pass; a failed read also clears the cached result.
2. Container resized from 0.5 vCPU / 1 GiB to 1 vCPU / 2 GiB.

The two were applied together, so these figures do not separate their effects.

**Public URL, before and after:**

| Measure | Before (revision 15) | After (revision 16) |
|---|---:|---:|
| Final Approval redraw | 41 s | 0.8 s |
| Proposal redraw | 9.2 s | 0.8 s |
| BRD redraw | 5.3 s | 0.8 s |
| Contracts redraw | 2.9 s | 0.8 s |
| Opening a Contract-Ready opportunity | 42.6 s | 7.2 s |
| Clause review click, median of 11 (audit timeline) | 56 s | 3.9 s |
| The three final approvals (audit timeline) | 86, 172, 139 s | 4, 11, 11 s |
| Full smoke flow (audit timeline) | 1,655 s | 482 s |
| Smoke checks passed | 34 of 35 (a fault in the check itself) | 35 of 35 |

Method: "before" redraws used the app's running indicator; "after" redraws timed the step's content becoming visible, because the indicator does not appear on very quick redraws. Both are on HRIS-04, repeated, after the first visit. The first visit after a restart still builds everything once. Audit timelines include the smoke script's fixed waits.

**Tests:** 5 added: nothing is rebuilt on a redraw or a clause tick; a change to what the check reads is seen at the next redraw; a decision rechecks and is refused without the template; a cached document changes with its version; the cache is bounded.

## I-02: requirements read whatever their format

A TOR with "Label: description" bullets lost its whole scope section without warning. Root cause, fix and evidence: [debugged_failure.md](debugged_failure.md). Recall on all six dev TORs: [format_robustness.md](format_robustness.md).

| TOR | Recall before | Recall after |
|---|---:|---:|
| Delivery app | 1/14 | 14/14 |
| Clinic matrix | 5/12 | 12/12 |
| Library numbered | 0/12 | 12/12 |
| HRIS | 26/26 | 26/26 |
| TOR A | 16/16 | 16/16 |
| TOR B | 18/18 | 18/18 |

## I-03: BRD drafts reach Drive

Drafts were never published: `publish()` ran only at approval, against section 11 of the plan. Every BRD version now goes to `BRD/versions`, marked DRAFT with its revision history, and `BRD/current` still waits for approval. A store failure marks the draft pending and the work goes on. Proved on the real Drive folder with a throwaway opportunity and database, reading the files back, then trashing the folder.

## I-04: a failed run that gave no reason (a test RFP, runs 48 and 49)

**Symptom.** BRD generation showed only "The run failed. Nothing was changed."

**Cause.** The runs table showed `IntegrityError`, after all the LLM work had finished. Reproduced locally for about USD 0.05: the database rule `uq_brs_opportunity_id_br_id` refused a second `BR-005`. BR IDs are built from a two-letter epic code, and two epics ("Core Feature Setup", "Core Functional Requirements Matrix") shortened to the same code, so any TOR with such a pair would have crashed. Those epic names were themselves a parser fault from I-02: a table's column titles were read as section headings. The file was never the problem.

**Also found.** A failed run recorded USD 0 although its LLM work had been paid for.

**Fix.**
- Epic codes never repeat.
- A table's column title is not a heading; a matrix row such as "MOD-01: Billing" becomes an epic, and its bullets become BRs.
- Generic headings are recognised whatever their first words, and an instruction to bidders above a matrix is not a BR.
- A failed run records what it spent.
- Each failure has a plain category, the next step, a run reference, and details in Agent trace. No stack traces or secrets are shown. Too little text is reported as unreadable before any LLM call.

**Result.** The same RFP, locally: 31 BRs under 5 module epics, where it used to crash; USD 0.112. No answer key exists for it, because it names real companies and stays out of the repo. A fictional file of the same shape is in the dev set: `eval/dev_set/tor_module_matrix_utility.pdf`, recall 11/11. HRIS, TOR A and TOR B keep the same statements, so their recall was not re-measured this round.

## I-05: proposals on Drive

**Before.** A proposal reached the store only at approval, so `PARCELO` v0.1 had nothing on Drive.

**Now, read back from the real Drive folder with a throwaway opportunity and database:**

| Step (layout at the time; flattened in I-10) | `proposal/` | `proposal/internal/` |
|---|---|---|
| Generated, v0.1 | empty | v0.1 INTERNAL, DRAFT |
| Review started | empty | unchanged |
| Approved, v1.0 | v1.0 APPROVED | + v1.0 INTERNAL, APPROVED |
| Submitted | + v1.0-SUBMITTED, SUBMITTED | unchanged |

**Also found by reading the files back.** Every proposal carried a fixed "DRAFT — FOR MANAGEMENT REVIEW" header, including approved client copies. An approved copy no longer has it. Files already on Drive, such as `HRIS-04`'s, keep their old header.

## I-06: double-clicks

**Before.** Nothing stopped a second click while work ran.

**Fix.**
- A button that starts work is off, with a progress message, while its work runs. Every other work button is off too.
- On the server, a second identical action on an opportunity is refused while one runs ("Already running, started at …"), using a database lock and a run record.
- A repeat from the same screen after the first finished is refused too. A real repeat from a fresh screen is allowed.
- A crash cannot leave anything stuck: the lock ends with its transaction, and an abandoned record stops counting after 15 minutes or at the next app start.

**Tests.** Real threads: two rapid identical requests do the work once; six simultaneous ones do it once; two rapid BRD requests start one run; two rapid proposal requests make one version.

## I-07: AI-first extraction with a rule checker (the main design decision)

The rule fixes in I-02 and I-04 did not generalize: a new RFP lost its whole requirements table on the deployed version. Parsing now keeps tables intact, the LLM reads each section, code checks each quote, and rules check the result. The three approaches were compared on seven TORs; the full story and tables are in [extraction_approaches.md](extraction_approaches.md).

| Liwanag RFP | Before (deployed rules) | After |
|---|---:|---:|
| Features and integrations found | 0 | 24 of 24 |
| BRs from headings | 3 | 0 |
| Bid procedure as a BR | Yes | No: on the opportunity record |
| Critic suggestions / sourced BRs | 5 / 3 | 5 / 24 |
| Short timeline raised as a risk | No | Yes |

**Scope cut.** The V2 few-shot variant in 5.6 is cut; V1 and V3 remain.

## I-08: removed questions and scope items were ignored on re-upload

**Found by:** Karen re-uploaded an edited BRD with open questions deleted. "Nothing changed."

**Evidence:** the edited file was still on Drive beside the published versions. It held 5 of the 12 question rows, and the person editing had renumbered the IDs 1 to 5 to close the gaps. Version 0.2 recorded 4 items changed, no BR changed, and the summary "order only".

**Cause, in three parts:**
1. The diff walked only the rows *in the file*. It had no code for a row that was missing, so 7 absent IDs were never seen. BRs had this check; questions, objectives, scope and risks did not.
2. IDs were trusted. A renumbered row read as "OQ-002 was reworded", so 4 questions silently took another question's text. Two of them became duplicates.
3. The summary did not count edited questions, so a real change read "order only". A file whose only changes could not be applied left Apply disabled with no explanation.

**Fix:** a row missing from the file, or emptied of its text, is "removed" in the diff and needs confirming. On apply it is Withdrawn: who, when and an optional reason stay on the row, nothing is deleted, and it leaves the BRD snapshot, so no later version, regenerated document, client question list or bulletin match sees it. A row whose text exactly matches another item is that item, whatever ID it carries; the diff says so. A repeated ID, a missing table or an unclear row is refused. Typing a withdrawn ID back in reinstates it, confirmed. The stability check expects listed withdrawals and blocks unlisted ones. After approval this goes through the change request as usual. Risks are now read back from the file too.

**Proof:** `tests/test_withdrawn.py`. Replaying the real file against version 0.1 gives 4 renumbered rows matched, 0 edits, and the 7 intended withdrawals. Migration 0005 adds the `withdrawn` columns. **Not repaired:** version 0.2 of that test opportunity keeps the moved text as history; version 0.1 still holds the originals.

## I-09: AI tooling as its own cost line, and net savings

**Found by:** Karen: the estimate seemed to show no cost for AI use, although B and C depend on it.

**Check first:** the v2 profiles did still feed the v3 calculators, through `estimation.configs` into `costing.calculate_cost_and_margin`, and the choice was editable under Assumptions. Stored estimates used it, at 0.9 to 2.0% of delivery cost. But it was one figure on one tab, absent from the estimate summary and the internal proposal; there was no net figure; and the recommendation compared prices only.

**Fix:** by code, from `config/tooling_profiles.yaml`, all labelled illustrative: seats (licence x people x months), model usage (tokens per hour of AI-assisted and AI-first work x hours x price per million tokens), self-hosted GPU share, AI test runs, and a contingency on usage. Net savings = labour saved against the baseline less AI tooling, for B and C. With AI-First validated the recommendation takes the highest net savings; while not validated it stays B, C's upside is stated in net terms, and the rule says so if the chosen scenario saves nothing net. The lines appear on the estimate screen, the internal summary and the internal proposal. The client copy shows the total price only, and the leak check now blocks each of these figures.

**Note:** estimates saved earlier keep their stored totals and show a note instead of lines, because an approved version must not change. The recommendation works out their net figure from the labour and tooling they stored.

## I-10: flat Drive folders

**Requested by:** Karen: `inputs/`, `BRD/`, `estimate/`, `proposal/`, `contracts/`, with no subfolders. The copy type moves to the file name: `-CLIENT` and `-INTERNAL`. Internal copies are stamped on every page, and drafts of both copies are published.

**Design notes:** `BRD/current` and `BRD/archive` went too, so the latest approved BRD is the one reading APPROVED, and earlier baselines are restamped SUPERSEDED in place. Publishing a name again now replaces the file and keeps its link, instead of leaving a same-named duplicate. A client draft that fails the leak check stays off Drive. `scripts/drive_flatten.py` moves existing files under the same file IDs, so stored links keep working; it prints a plan unless given `--apply`, is safe to repeat, leaves a person's own copies under their names, and sends duplicates and emptied folders to Trash.

## I-11: admin delete of an opportunity

**Requested by:** Karen, for test data. The design and its limits are in [guardrails.md](guardrails.md). Migration 0006 adds `purge_opportunity`; the app role's rights on `audit_log` did not change. One decision beyond the request: a purge leaves one audit line with who, when and the counts, and no ID, code or name, so that removal itself stays accountable.

## I-12: TOR B precision: statements repeated across sections

**Explanation first.** At 0.54, TOR B had 28 BRs against 18 key features. The judge matched 16, with 12 unmatched:

| Why | BRs | Verdict |
|---|---|---|
| A later section repeats an earlier one (sections 7 and 8 repeat section 4, once saying "as stated in Section 4.3") | Travel request experience, Expense claim experience, Cash advance experience, Reminders, ERP posting or ERP integration, Bank file generation | Real duplicates: the work was counted twice |
| The vendor's line inside a capability's section became a second BR | Automatic reading of receipts (Vendor supply) | Real error |
| Real requirements the key does not list at this level | Personal workspace, Complete history | Fair BRs, and the second is a planted flaw |
| Other | Email (an interface), General (a vague statement), Equipment (hardware, handled later as vendor supply) | Left alone |

The measure itself caps this: one key feature credits one BR, so finer-grained but valid BRs always count against precision. TOR B is written to be repetitive, and it remains the lowest score.

**Fix (checker only, no prompt change, nothing re-read).** A BR folds into an earlier one if it comes from a titled subsection of a later top-level section, is not a module row or list item, and either names that section or shares a word of its name and repeats at least half its words. Its statements move to the earlier BR, so nothing is lost. A vendor line in a capability's section stays with that capability.

The bar comes from a scan of every candidate on the dev set. Word overlap alone could not separate them: a true restatement scored 0.58, while "Overtime" against "Employee master record" scored 0.62. A shared name word separates them: true restatements 0.58 and up (one miss at 0.36), the closest false match 0.35. This pass is deliberately narrow, given what the v1 merge pass did to recall (see `debugged_failure.md`).

| TOR, extraction stage | BRs before | BRs after | Recall before | Recall after | Precision before | Precision after |
|---|---:|---:|---:|---:|---:|---:|
| TOR B | 28 | 23 to 24 | 16/18 | 16/18 | 0.54 | 0.63 to 0.65 |
| Delivery, Clinic, Library, Liwanag RFP, HRIS, TOR A | | no change, no folds | | no change | | no change |

**Left over.** Expense claim experience (0.36) stays a separate BR, and Bank file generation (it spans two earlier BRs) too: lowering the bar reaches false matches.

## I-13: words broken by a PDF line wrap

"e-\nreceipts" read "e- receipts" in BR names. A word broken at its hyphen now rejoins, in page text and table cells. A dash set off by spaces, or a capital on the next line, is left alone. Before: one occurrence across the dev PDFs. After: none. Test in `tests/test_format_robustness.py`.

## I-14: tables laid out with spacing

A table with no ruled lines was read as interleaved text. A page now also has its word positions read: a header and at least two more rows with text in two or more columns, on the same x positions, count as a table, with bullets kept in their cells. The bar is high because prose must never read as a table.

| Check | Before | After |
|---|---:|---:|
| New dev TOR, museum RFP, spaced table with bullets in cells | not read as a table | 12/12 recall, precision 0.92 |
| Existing dev PDFs falsely detected | n/a | 0 of 7 |

The museum RFP joins the dev set, because it was used for tuning. **Limit:** a spaced table whose columns shift between rows, or with fewer than three rows holding text in two or more columns, is not detected and reaches the LLM as text.

## I-15: bid procedure on the Record tab

Bid procedure was stored on the opportunity record but not shown as a list. The Record tab now lists it above the raw record, as plain words.

## After I-12 to I-15: end to end on the dev set

No regressions: full recall on all 8. Command: `python eval/format_robustness.py . <out> <names>`. USD 0.338 in total.

| TOR | Recall before | Recall after | BRs before | BRs after | Critic before | Critic after |
|---|---:|---:|---:|---:|---:|---:|
| Delivery | 14/14 | 14/14 | 15 | 18 | 1 | 4 |
| Liwanag RFP | 24/24 | 24/24 | 29 | 29 | 5 | 5 |
| Clinic | 12/12 | 12/12 | 15 | 18 | 2 | 5 |
| Library | 12/12 | 12/12 | 13 | 13 | 0 | 0 |
| Museum (new) | n/a | 12/12 | n/a | 14 | n/a | 1 |
| HRIS | 26/26 | 26/26 | 28 | 28 | 1 | 1 |
| TOR A | 16/16 | 16/16 | 17 | 17 | 0 | 0 |
| TOR B | 18/18 | 18/18 | 29 | 24 | 2 | 2 |

**The rise on Delivery and Clinic is not from this tuning.** Their extraction is unchanged, as the extraction stage shows, and the extra BRs are critic suggestions, which are marked and unpriced. The critic varies between runs, and nothing stores its answers when evaluation runs in memory. This is a candidate for 5.4 (C-04).

## I-16: an upload of the bundled sample's file failed

**Found by:** 5.4, running HRIS through the intake path on the dev set: "The requirements could not be extracted".

**Cause:** uploads also read the shipped results. The identical file matched the sample's stored result, whose statements point at a document key that upload does not have, so saving failed. **Fix:** an upload reads only its own in-memory results. The bundled sample still uses the shipped one. Test in `tests/test_ai_extract.py`. HRIS through upload: failed before, 26/26 after.

## I-17: a refused approval was not logged

**Found by:** S21. A BDM approving the BRD was refused, but no record of the attempt existed, which the addendum requires. **Fix:** approvals of the BRD, estimate, proposal and contracts, and the admin purge, now write `permission.refused` in their own transaction, so it survives the rollback. Before: 2 of 3 S21 checks. After: 3 of 3. Test in `tests/test_purge.py`.

## I-18: one passage refused by Azure stopped the whole run

**Prompted by the held-out baseline.** The fleet RFP failed twice. Only its error code and message were read, not its contents: `content_filter`, `ResponsibleAIPolicyViolation`, jailbreak detected, on 1 of 13 sections. This is held-out knowledge used openly, so nothing here tunes to that file: the fix handles any refused passage, and it was built on a new dev TOR.

**Reproduced:** `eval/dev_set/tor_parking_permits.docx`, a parking permit TOR with a passage of its own wording. The app's guard removed part of it, Azure still refused the section, and the run stopped with "AI service unavailable".

**Fix:** a content-filter refusal is no longer treated as an outage. One-token probes find the smallest block Azure refuses. That block is left out, the rest of the section is read, a guard finding is recorded, and the reviewer gets an internal question naming the section, with no words quoted. If more than half the section goes, the question blocks approval. Any other bad request still stops the run.

| Parking TOR | Before | After |
|---|---:|---:|
| Run | Failed | Succeeded, USD 0.036 |
| Recall at extraction | No BRs | 7/7, precision 1.00 |
| Reviewer told | No, only "AI service unavailable" | Internal question naming section 3 |

No regressions at extraction on the other 8 dev TORs. Scenario S33 and tests in `tests/test_ai_extract.py` cover it.

## I-19: a bulletin's changed value did not land

**Prompted by the held-out baseline,** whose bulletin check found the superseded value in no BR. Scores only were read. Reproduced with a dev bulletin (`eval/dev_set/parking_bid_bulletin_01.pdf`) that changes a stated value on numbered Word clauses.

**Three causes on the dev TOR:**
1. Numbered Word clauses are stored under their section, so "Under Section 3.2" found no statement in 3.2, and the amendment stayed unplaced.
2. Accepting an unplaced proposal then changed nothing, without saying so. The screen disables this, but the service allowed it.
3. BR names kept the clause's sentence ("Renewal window. A permit holder ..."). Not the cause of the bug, found alongside it.

The evaluation script also skipped the screen's "Write merged statements" step, which is what replaces an old value. It now runs that step.

**Fix:** a reference to a numbered clause falls back to its section; finishing a request with an accepted unplaced proposal is refused; a clause headed by its own title is named by it.

| Dev bulletin | Before | After |
|---|---:|---:|
| Value change placed | Unplaced | Amends the right BR |
| "forty-five" in the BR | No | Yes |
| "thirty" left in the BR | Yes | No |
| New requirement | Added | Added |

Scenario S34 and tests in `tests/test_workflow.py` cover it. Whether it lands on the held-out bulletin is for the final run in 5.9.

## I-20: step costs undercounted steps that run twice

**Found by:** 5.7, where sizing showed USD 0.002 for 61 seconds of work. **Cause:** the critic's loop runs the router and sizing again, and a step's usage kept only its last pass. Run totals were right (USD 0.1138 in LangSmith, 0.1139 recorded on TOR B), so no spend figure was wrong; only the Agent trace tab's split and my summing of it. **Fix:** a step's usage adds every pass. Test in `tests/test_ai_extract.py`.

## I-21: the injection guard knew too few phrasings

**Prompted by the held-out baseline.** Both failed fleet runs show zero guard findings, read from stored spans without opening the file, so our guard had removed nothing before Azure refused the section.

**Reproduced on the dev set:** `eval/dev_set/injection_cases.json`, 9 attacks and 12 similar-sounding real requirements, written for this and not taken from the held-out files, plus the parking TOR's passage.

**Fix:** general patterns for setting aside a reader's instructions, for revealing them, and for notes addressed to an AI reader. They need the words together, so "ignore a reminder" and "override approval rules" stay.

| Check | Before | After |
|---|---:|---:|
| Dev attacks caught | 0/9 | 9/9 |
| Real requirements removed | 0/12 | 0/12 |
| Parking passage's sentences removed | 1 of 3 | 3 of 3 |
| New hits on the other dev TORs | n/a | 0 |

The cases helped shape the patterns, so this is not independent proof, and the fleet result in 5.9 is not independent for this flaw. See [guardrails.md](guardrails.md). S33 now holds the rule guard off, to keep proving the second layer.

## Candidates for 5.4 (dev set, TOR A, found in Step 5.0, not yet changed)

| ID | Finding | Evidence |
|---|---|---|
| C-01 | The integration question fires on features that are not integrations (recheck after I-07) | 8 features, including Audit trail and Section assignment; the answer key types 1 feature as an integration |
| C-02 | Expected exclusions are not listed as out of scope | 2 of 3 appear as client responsibilities; the third was not found by keyword |
| C-03 | Odd scope labels | Largely closed by I-02 and I-07: migration is no longer "Vendor supply", and "Deliverables and timeline" no longer appears as out of scope. Recheck in 5.4 |
| C-04 | Critic suggestions vary between runs on the same extraction | Delivery 1 and 4, Clinic 2 and 5 between two runs; both stayed at full recall |
| C-05 | Hybrid lost to dense on the unfiltered golden set | Decided: production stays on hybrid. See rag_results.md |
| C-06 | Stored suitability answers agree less with the labels than V3 run one activity at a time | Reported as a limitation, sample not regenerated. See prompt_comparison.md |
| C-07 | The bundled sample's first run calls the risk agent, because the AI tooling lines changed one risk's figures | Under USD 0.01. The on-screen caption says 0 Azure calls. Shipped file unchanged |

Each becomes an iteration only with a before and after on the dev set. Held-out outputs are never used to choose or tune these.

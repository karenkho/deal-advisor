# Screenshots to capture

Take these in a signed-in browser. Use only `HRIS`, `HRIS-04` or a new sample. Save them to `docs/evidence/`. Check that no real name or Drive folder ID shows.

## On the public app

| # | File | Screen | What must show |
|---|---|---|---|
| 1 | `01_stepper.png` | `HRIS-04`, Workspace | The status stepper at Contract-Ready |
| 2 | `02_brd_citation.png` | A BR card | Its statement and source citation |
| 3 | `03_reupload_diff.png` | BRD, re-upload | "Removed from the file", and the confirm box |
| 4 | `04_compare.png` | Versions, Compare | Only the affected BRs changed, after a bulletin |
| 5 | `05_tooling.png` | Estimate, Cost & pricing | AI tooling lines and net savings, labelled illustrative |
| 6 | `06_evidence_roles.png` | Evidence tab, on a new upload | Each section's role and reason. The sample has none, by design |
| 7 | `07_drive_folder.png` | Google Drive, `HRIS-04` | Flat folders and `-CLIENT`, `-INTERNAL` names. Hide the folder link |
| 8 | `08_purge.png` | Opportunity list, as Admin | Delete opportunity, with protected codes shown. Do not delete |

## The two guardrails

**G1, injection removed:** `guardrail_injection.png`.
1. On the public app, as BDM, create a new opportunity and upload `eval/dev_set/tor_parking_permits.docx`. It costs about USD 0.05.
2. Generate the BRD, and open the Agent trace tab.
3. **Must show:** the `intake_guard` row, with its count of guard findings. The screen shows counts only, by design. The removed sentences themselves are in that run's LangSmith trace, under `intake_guard`, since tracing is now on for the public app. Capture that too, if you want to show the words.
4. Delete the opportunity afterwards, as Admin.

**G2, leak blocked:** `guardrail_leak.png`.
1. On a local run, set `LEAK_TEST_INJECT=word`, because this flag is off on the public app.
2. Take the sample to a generated proposal.
3. **Must show:** "Client copy blocked: internal data in Commercial Terms", with no download button for the client copy.

## The handled failure

**F1, stability check:** `failure_stability.png`. On a local run with `FAULT_INJECT=alter_unrelated_br`, save any edit to a BR on the sample. **Must show:** "Unexpected change to BR-... — not applied".

**F2, Drive down, the alternative:** `failure_drive.png`. On a local run with `FAULT_INJECT=drive_down`, approve a BRD. **Must show:** "publish pending" and Retry publish.

## Already in the repo

Trace images 1 to 8 in `docs/evidence/traces/` are drawn from LangSmith data, and are labelled as such. For real screenshots of LangSmith, use the private links in `docs/private/trace_links.md`, and crop out account details.

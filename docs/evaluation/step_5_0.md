# Step 5.0: housekeeping before evaluation

Git commit measured: `4429071` (tag `poc-v3`), container revision 15, 0.5 vCPU / 1 GiB. Date: 2026-11-01. Figures marked *est.* are estimates, not timings.

## Live Intake path (TOR A, dev set)

Method: the 5-page `samples/tor_A_clean.pdf` uploaded through the Intake screen of the deployed app in a headless browser, on a throwaway opportunity (deleted afterwards). Not in any shipped cache; the token count confirms an uncached run.

| Measure | Result |
|---|---|
| Reached BRD Generated | Yes, no exception and no failed run |
| Server run time | 116 s |
| Tokens (prompt / completion) | 85,801 / 25,521 |
| Cost | USD 0.074 (app's pre-run estimate: USD 0.10) |
| Container peak | 0.18 vCPU, 307 MB, 0 restarts |
| BRs | 17 |
| Answer-key features found (matched by hand from titles) | 16 of 16 |
| BRs with no key feature | 1 (performance and availability targets) |
| Size agreement | 14 of 16 exact, 16 of 16 within one tier |

This is a smoke check, not the task evaluation in 5.4. TOR A has no planted PII or injection line, so it did not exercise those guards. An earlier version of this table showed "known PII strings: 0 of 3" for it, which proved nothing and was removed; S02 and S03 in 5.5 cover those guards.

Findings for 5.4 (dev set, not fixed here):
- **Feature type:** the integration question fired on 8 features, including Audit trail and Section assignment; the answer key types only 1 feature as an integration. The 2 expected provider questions are covered, but only through this generic wording.
- **Exclusions:** 2 of 3 expected exclusions appear in the BRD, classified as client responsibilities and not out of scope. The third was not found by keyword.
- **Scope labels:** one in-scope row carries the label "Vendor supply" over existing student records, and "Deliverables and timeline" appears as out of scope.

## `corpus/proposal_sections.md`

Method: every body line compared in memory with the text of both Exist proposal references (paragraphs and tables, 10,394 words); no text or file name printed.

| Check | Count | Lines |
|---|---|---|
| Body lines compared | 14 | 5–57 |
| Lines sharing 5 or more consecutive words with a reference | 0 | none |
| Real client or project names (name tokens from reference file names, plus reading the file) | 0 | none |
| Real people, emails, phones, amounts, dates | 0 | none |
| Mentions of Exist | 0 | none |

The section headings follow a standard proposal outline. That is structure, not copied text. No rewrite needed. The two references are the whole folder; `Presales/` was not compared.

## Where the 29-minute public run went (HRIS-04)

Audit timeline, 1,655 s from first to last action. The rest of the 29 minutes is sign-in and checks outside it.

| Stage | Seconds | Share |
|---|---:|---:|
| Intake to draft BRD (cached) | 18 | 1% |
| BRD validation and re-upload to v1.0 | 53 | 3% |
| Estimate, bulletin, BRD v2.0 | 94 | 6% |
| Estimate validation and approval | 120 | 7% |
| Proposal to approval | 232 | 14% |
| Award and contracts | 80 | 5% |
| Entering legal review | 32 | 2% |
| 11 clause reviews | 629 | 38% |
| 3 final approvals | 397 | 24% |

By cause:

| Cause | Seconds | Share | Basis |
|---|---:|---:|---|
| Page redraws (CPU), nearly all on Final Approval | 1,000–1,100 | about 65% | *est.* from the measured redraws below |
| Smoke script's fixed waits | about 350 | about 20% | *est.*: at least 5.5 s per click; a person would not have these |
| Database | 60–120 | about 5% | *est.*: 16–30 queries per redraw, 40 ms or less each |
| Drive | 60–100 | about 5% | *est.*: about 20 uploads at 2–4 s, 1.4 s per template read |
| Cached BRD run | 18 | 1% | measured |
| LLM | under 10 | under 1% | *est.*: 867 tokens in total |

Redraw of each step on HRIS-04, public container, median of 3 (Final Approval: 2 valid readings):

| Step | Seconds |
|---|---:|
| Intake | 0.9 |
| BRD | 5.3 |
| Estimate & Timeline | 1.3 |
| Proposal | 9.2 |
| Award | 1.0 |
| Contracts | 2.9 |
| **Final Approval** | **41** |

Cause: each Final Approval redraw runs the full consistency check, which rebuilds the SOW and the proposal, downloads and fills the MSA template and re-reads all three. The same call took 4.5 s on a laptop. During the run the container's per-minute CPU peaks had a median of 0.42 and a maximum of 0.46 of its 0.5 vCPU; memory peaked at 318 MB of 1 GiB.

## Proposed size (not applied)

| Size | Cost per active hour | Expected Final Approval redraw |
|---|---:|---:|
| 0.5 vCPU / 1 GiB (now) | USD 0.076 | 41 s (measured) |
| 1 vCPU / 2 GiB | USD 0.151 | about 20 s (*est.*: CPU work halves) |
| 1 vCPU / 2 GiB, check run only when a version changes | USD 0.151 | a few seconds (*est.*) |

Azure retail list prices, Southeast Asia, Standard consumption (vCPU USD 0.000034 per second, memory USD 0.000004 per GiB-second), before any free grant. With scale-to-zero kept, idle time costs nothing. Memory is not the constraint; Azure pairs 2 GiB with 1 vCPU.

## Commands

Browser and profiling scripts were one-off and not kept in the repo. Prices: Azure Retail Prices API. CPU and memory: `az monitor metrics list` (`UsageNanoCores`, `WorkingSetBytes`).

# Agentic scenario suite

**34 of 34 scenarios pass on the final code, with tool selection correct on all 34.** The first run found one real gap: a refused approval was not logged (S21, fixed as I-17). S33 and S34 cover the two faults the held-out baseline exposed (I-18 and I-19).

## Method

`python eval/run_scenarios.py`, using the definitions in `eval/scenarios/suite.yaml`. It runs real app code on a throwaway PostgreSQL and a local store. Spans are recorded locally during the run, so tools are read from what actually ran. Failures are designed with `FAULT_INJECT=<name>` (`app/faults.py`), which is test only and unset in production. Base commit `d2e35c4` plus the changes logged as I-16 and I-17.

## Metrics

| Metric | Result |
|---|---:|
| Tool selection: expected tools ran, forbidden tools did not | 34/34 |
| Task completion | 34/34 |
| Failure handling (`failure` scenarios) | 9/9 |

**Reading the tool figure:** only S01, S04, S09, S10, S11, S12, S19, S23, S25, S30, S33 and S34 name any tools, so it says less than the total suggests. Most scenarios test behaviour, not tool choice.

## Scenarios

| # | Scenario | Kind | Expected | Result | Tools |
|---|---|---|---|---:|---:|
| S01 | HRIS full v3 flow | flow | Intake to Contract-Ready with three approvals | Pass | OK |
| S02 | Injection line in a TOR | guardrail | Line removed, finding logged, never reaches the LLM | Pass | OK |
| S03 | PII in a TOR | guardrail | Masked before the LLM and logs | Pass | OK |
| S04 | Non-TOR upload | guardrail | Rejected with a reason, no further tools | Pass | OK |
| S05 | Vague requirement | behaviour | Open question raised | Pass | OK |
| S06 | Conflicting statements | behaviour | Conflict question; neutral BR title | Pass | OK |
| S07 | Implied migration | behaviour | Critic suggests scope; not priced | Pass | OK |
| S08 | Hardware item | behaviour | An exclusion or vendor supply, not a BR | Pass | OK |
| S09 | Bid bulletin | flow | Superseded statement marked; incremental only | Pass | OK |
| S10 | Risk agent on 10/90 terms | agent | Calls cost and schedule tools; cash risk high | Pass | OK |
| S11 | Proposal agent | agent | Calls both; no amounts in the LLM input | Pass | OK |
| S12 | Estimate before BRD approval | refusal | Refused by the state machine; nothing generated | Pass | OK |
| S13 | Planted leak in a proposal | guardrail | Download blocked, section named | Pass | OK |
| S14 | Hosted-API tooling profile | guardrail | Approval blocked until consent is recorded | Pass | OK |
| S15 | Price below the floor | guardrail | No-go, not overridable | Pass | OK |
| S16 | LLM timeout | failure | Item marked needs manual review; run completes | Pass | OK |
| S17 | Retrieval returns nothing | failure | Sizing falls back to the rubric, flagged | Pass | OK |
| S18 | Malformed rates CSV | failure | Clear error; illustrative rates kept | Pass | OK |
| S19 | Per-run cost cap exceeded | failure | Refused gracefully with a message | Pass | OK |
| S20 | Injection via a supplementary document | guardrail | Same guard as S02 | Pass | OK |
| S21 | Wrong role | refusal | Refused and the attempt is logged | Pass | OK |
| S22 | Stale save | refusal | Second save refused, its draft kept | Pass | OK |
| S23 | Unexpected BRD change | failure | Stability check blocks; nothing written; audit entry | Pass | OK |
| S24 | Tampered BR ID in a re-upload | refusal | Refused with a clear message; nothing changed | Pass | OK |
| S25 | Bulletin after approval | flow | Change request, only affected BRs change, v1.1, estimate Outdated, re-estimate keeps overrides | Pass | OK |
| S26 | Proposal re-upload with an edited price | refusal | Blocked by the consistency check | Pass | OK |
| S27 | MSA template missing | failure | MSA template not available; SOW still generated; no crash | Pass | OK |
| S28 | Drive unavailable | failure | Saved in the database, publish pending, Retry publish works | Pass | OK |
| S29 | Coverage guard live | failure | Guard fires: LLM extraction for the section, or a blocking question | Pass | OK |
| S30 | Re-upload with removed and renumbered questions | flow | Removed become Withdrawn after confirming; renumbered matched by text | Pass | OK |
| S31 | Admin purge | refusal | Non-admin, protected, wrong code and running run refused; full purge leaves only the anonymous line | Pass | OK |
| S32 | Tooling profile switch | flow | Tooling lines and net savings change; the client copy has no breakdown | Pass | OK |
| S33 | A passage Azure's content filter refuses | failure | Only the passage is left out; the rest is read; the reviewer is told, with no words quoted | Pass | OK |
| S34 | Bulletin that changes a stated value | flow | The BR is amended, the new value replaces the old, a new BR is added | Pass | OK |

## How far these go

- **Stubs and inline inputs.** Several scenarios use short inline text or the bundled sample's stored result, not a fresh read. S02, S03, S13, S14, S15, S18 and S32 call the code directly, without going through the screens.
- **S05 is a loose check.** It looks for a question with words such as "privacy" or "specific". The stricter flaw judge in `task_results.md` covers the same flaw on HRIS and TOR B.
- **S17's flag is thin.** It passes because justifications mention the rubric, and no separate flag is raised that retrieval came back empty.
- **S16 tests the sizing step only.** An LLM failure while reading the document stops the run by design (I-07).
- **S29 forces the gap** by removing one section's answers, so it shows that the guard fires, not that a real format produces this gap.

## Trace evidence

S10, S11 and S16 were also run with tracing on. The images are drawn from the data LangSmith stored, read through its API, and are not screenshots of the LangSmith site, which needs a signed-in browser. Links are in `docs/private/`.

- [S10: the risk agent's tool calls](../evidence/traces/6_s10_risk_agent.png)
- [S11: the proposal agent's tool calls](../evidence/traces/7_s11_proposal_agent.png)
- [S16: an LLM timeout during sizing](../evidence/traces/8_s16_llm_timeout.png)

## Cost

The local ledger does not record every direct agent call, so it undercounts this suite. Azure token metrics are the measure of total spend.

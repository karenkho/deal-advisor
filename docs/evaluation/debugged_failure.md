# Debugged failures

## Case 1: a whole scope section missing from the BRD

**Symptom.** A synthetic delivery-app TOR gave a draft BRD that missed all of §2 Scope of Work: 4 modules (§2.1 to §2.4) with 14 features, written as "Label: description" bullets without "shall" or "must". The run succeeded and showed no warning.

| Old BRD | Count or result |
|---|---|
| BRs | 3: 2 from the critic, 1 from §3.2 |
| BRs from §2 | 0 |
| OOS-001 | The §2 intro paragraph, about what the vendor will do, listed as out of scope |
| IS-001 and EP-02 | The heading "TECHNICAL REQUIREMENTS" as a scope item and an epic |
| Objectives | "Not stated in TOR", although §1.2 lists three |

**How it was found.** Karen tested the live Intake screen with a TOR in a format the dev set did not cover. All earlier TORs used `-` bullets and "shall", so the existing tests and evaluation could not see this.

**Root cause.** Found with rules only, no LLM. Several layers failed, and each would have hidden the next:

| Step | What went wrong | Evidence |
|---|---|---|
| PDF text | The extractor gives each bullet as the control character `\x7f`; the item pattern knew only `-`, `•`, `*` and numbers | 21 bullets not recognised |
| Headings | The heading pattern forbade `:`, so "2.1 Module 1: Customer Application" was not a heading | 4 of 4 modules |
| Statement filter | Only modal verbs or items were kept | 22 of 22 §2 sentences dropped; 7 of 64 kept overall |
| Overview gate | Anything under "Scope of Work" was forced to `other` | Fixing bullets alone would still have given 0 BRs |
| Exclusion | The intro's "will" made it the only §2 statement; the LLM called it an exclusion and no code checked | OOS-001 |
| Epic naming | The epic was the top section's title, and In Scope copies epics | "TECHNICAL REQUIREMENTS" |
| Objectives | The objective list had the same bullet bug | 8 lines, 0 items |
| **Coverage** | No code compared sections with BRs. The rule checks cover only migration and user admin, and the critic sees only statements that survived | Nothing flagged |

The evaluation's own measure showed it too: this TOR scored 1/14, and that one match came from a critic addition.

**Fix, in general and not for this file.**
- One shared reader (`statements.walk`) for statements and BRD sections: any bullet glyph, capital and module headings, running headers and footers removed, numbered paragraphs, and table rows from Markdown, DOCX and PDF.
- Under a scope, module or functional heading, a "Label: description" item, a numbered paragraph or a table row names its own BR.
- A module heading becomes an epic. A generic heading such as "Technical Requirements" is never an epic or a BR name.
- An exclusion must say what is left out or who else bears it. A paragraph about the vendor's work no longer qualifies.
- Text about the procurement (evaluation, deliverables, payment terms) never becomes a BR.
- **Coverage guard:** a section whose heading suggests scope, modules or functional requirements and yields 0 BRs is read by the LLM, with the cost stated in a warning and only text really in the section kept. If the LLM cannot read it, a blocking open question stops the BRD approval.

**Found along the way.** The no-regression rule caught two faults in the first version of the fix: a list of users under Scope of Work became 5 false BRs on HRIS, and TOR A lost its migration feature. The second was an older weakness: "vendor shall migrate" could be classed as a supplied item, routed out of scope, and take the migration backstop with it. Text about migrating is now always build work, and a proposal never merges into an excluded BR.

**Before and after, same TOR.**

| Measure | Before | After |
|---|---:|---:|
| Recall against the answer key | 1/14 | 14/14 |
| BRs | 3 | 16 (1 from the critic) |
| BRs from §2 | 0 | 14 |
| Epics from the modules | 0 | 4 |
| Heading as an epic or scope item | Yes | No |
| §2 intro as an exclusion | Yes | No (0 exclusions) |
| Objectives found | 0 | 3 |

Epics after: Customer Application (iOS & Android), Driver / Rider Application (iOS & Android), Foundation, Merchant Dashboard (Responsive Web Panel), Non-functional requirements, Super Admin Control Core (Web Console). All six TORs are in [format_robustness.md](format_robustness.md).

**Trace.** [`4_delivery_after_fix.png`](../evidence/traces/4_delivery_after_fix.png) is drawn from the LangSmith data of the local run in the recall table, not a screenshot of the site. The private link is in `docs/private/trace_links.md`.

**Tests.** `tests/test_format_robustness.py` covers each rule, including the guard's failure path, and pins the statement counts of all six TORs.

**Not yet shown.** The fix has not been run through the public app; Karen will test it by creating the opportunity herself.

### The fix failed to generalize

**Symptom.** The next RFP, with a 3-column requirements table with bullets in its cells, gave 0 of 24 features on the deployed version. Headings became BRs, the file-size limit became a BR, and 5 of 8 BRs came from the critic. The coverage guard stayed silent. The rule fix had passed every test written after the first failure (I-02), and the draft now on Drive made this visible at once (I-03).

**Root cause.** Each rule fixed one document's format, so the next format needed another rule. Adding one for this table would have scored well on this table, and the comparison shows that: rules reach 24/24 on it only because of a rule added for that shape.

**The decision (I-07).** Three approaches were measured on the same TORs, before choosing: rules, AI alone and AI with a rule checker. AI alone found the items but grouped them badly (HRIS 60 BRs, TOR B 80). AI with the checker kept the recall and the structure. **AI reads and drafts, code verifies, stores and calculates, people approve.** Evidence is in [extraction_approaches.md](extraction_approaches.md), and [`5_rfp_ai_checked.png`](../evidence/traces/5_rfp_ai_checked.png) shows the run.

| The RFP | Deployed rules | AI-first + checker |
|---|---:|---:|
| Features found | 0 of 24 | 24 of 24 |
| BRs from headings | 3 | 0 |
| Bid procedure as a BR | Yes | No: on the opportunity record |

**The held-out set then found something the dev set had not:** a passage Azure's content filter refuses stopped a whole run (I-18). The design held, but this failure path had not been exercised.

## Case 2: the over-aggressive merge pass (v1)

**Written from the existing v1 numbers, and not re-traced.** A rerun was cut, so this has no trace.

**Symptom.** On verbose TORs the first version produced duplicate features, so a consolidation pass was added to merge them.

**What the record shows** (`docs/poc_results.md`, commit `bd5ca56`):

| Merge pass | TOR A features | Result |
|---|---:|---|
| Unconstrained | 16 to 9 | Over-merged: separate capabilities became one |
| Constrained (similarity at least 0.65, at most 30% of items) | Merged nothing, on either TOR | No benefit |
| Disabled (`MERGE_PASS = False`) | Kept | Duplicates accepted as a known cost |

The offline re-check on cached extractions cost USD 0.005.

**A discrepancy.** The Phase 5 plan describes this as TOR A at 62% recall with flaw F1 failing, then 100% and 7 of 7. The surviving report supports the 16-to-9 over-merge and a TOR B result of 7 of 7 flaws, but not the 62% figure, and TOR A's answer key lists no planted flaws. The 62% is therefore not claimed here.

**What it taught, and where it was used.** When I-12 needed to fold restatements on TOR B, the same risk applied. That pass only touches titled subsections of a later section, needs a shared name word as well as repeated words, and keeps every statement. It was set from a scan of every candidate on the dev set, where word overlap alone would have folded "Overtime" into "Employee master record".

# What the agents actually do

Checked against the code and dev-set traces on 2 Nov 2026 (HRIS, TOR A and TOR B, one traced run each). Trace links are in `docs/private/trace_links.md`.

**These are tool-calling agents with consistent behaviour. No tool an agent chooses is hard-coded, but the choices barely vary between deals, and some retrieval is code, not an agent's choice.** In the risk agent, code sets the triggers and severity, and the agent explains them.

## 1. Risk agent (`app/risks.py`)

It chooses tools by function calling (`tool_choice="auto"`), with no fixed call order in code. Its prompt tells it to call `check_risk_rules`. Which risks trigger, their severity, the verdict and the scenario are all decided by code before the agent runs: it only explains them.

| TOR | Terms | Tools called |
|---|---|---|
| HRIS | 10/90 | rules, cost B and C, schedule B and C, guideline |
| TOR A | | rules, cost B, schedule B, guideline |
| TOR B | | rules, cost B, schedule B, guideline |

So the choice is real but narrow: every run called all four tools, and the deal changed only whether C was also examined. One run per TOR cannot show how stable that is. The agent itself is always invoked on an upload. It is skipped only when every answer is already cached, as for the bundled sample.

## 2. Sizing agent (`app/graph.py`, `size_one`)

It chooses by function calling among `precedent_search`, `rubric_lookup` and `glossary_lookup`, up to a budget of calls, and the final size comes from a second, separate LLM call without tools.

| TOR | Rubric lookups | Precedent searches | Glossary | Features routed to sizing |
|---|---:|---:|---:|---|
| HRIS | 10 | 10 | 0 | 10 live, the rest cached |
| TOR A | 17 | 16 | 0 | 16 clear or novel |
| TOR B | 23 | 23 | 1 | 24 clear or novel |

In practice it nearly always calls the rubric and precedents once per feature. It rarely uses that freedom.

**When it skips the LLM:** on a cache hit (same name, type, statements, prompts and model), which it did for most HRIS features in this trace. A feature routed `unclear` or `out_of_scope` is not sized at all. An LLM failure marks the feature for manual review.

## 3. Coverage critic (`app/graph.py`, `coverage_critic`)

Yes: it is a separate node after sizing, and its input holds the guideline's Coverage checklist and Foundation epic. HRIS and TOR B looped back once through the router and sizing for what it added, and TOR A went straight on. The loop runs at most once.

**Not an agent choice:**
- Code always fetches the checklist and 10 precedents, and the critic has no tools.
- Code adds its own rules: a login question if authentication is never mentioned, and a backstop that proposes migration or user administration if a checklist signal is ignored.
- Its proposals are capped at 5, and near-duplicates are dropped or merged by code.

## 4. Intake routing

| Step | Branches | Decided by |
|---|---|---|
| Intake guard: PII, then injections | Masked or removed, always continues | Code, plus quotes the classifier reports |
| Intake guard: is it a TOR? | Continue, or reject and end | `gpt-4.1-nano`, falling back to `gpt-4.1-mini`. If both fail, the document is processed with a warning |
| Router, per feature | `clear` (sized), `novel` (sized and flagged), `unclear` (client question, not sized), `out_of_scope` (exclusion) | `gpt-4.1-mini` |
| Router overrides | `data_migration` cannot be out of scope. If the router fails, a feature is `clear` and marked for manual review | Code |

The classifier did not appear in the HRIS trace, because its answer was cached. Document type comes from the user's choice at upload, not from a model.

## Other code-driven steps

The suitability agent's guidance and the extraction's read of each section are also fetched or run by code on every document. Neither is a tool an agent selects.

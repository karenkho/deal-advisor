# Agent graph

Generated with `graph.get_graph().draw_mermaid()`. Agent nodes (LLM decisions) are highlighted: router, sizing_agent (a tool-calling agent with consistent behaviour: it calls the rubric and precedents for nearly every feature), coverage_critic (no tools: code supplies its checklist and precedents). See [agent_behaviour.md](evaluation/agent_behaviour.md). Dotted arrows are conditional edges.

```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	intake_guard(intake_guard)
	extract_consolidate(extract_consolidate)
	router(router)
	sizing_agent(sizing_agent)
	coverage_critic(coverage_critic)
	calculator(calculator)
	__end__([<p>__end__</p>]):::last
	__start__ --> intake_guard;
	coverage_critic -.-> calculator;
	coverage_critic -.-> router;
	extract_consolidate --> router;
	intake_guard -. &nbsp;reject&nbsp; .-> __end__;
	intake_guard -.-> extract_consolidate;
	router --> sizing_agent;
	sizing_agent -.-> calculator;
	sizing_agent -.-> coverage_critic;
	calculator --> __end__;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
	classDef agent fill:#ffe9a8,stroke:#b8860b,stroke-width:2px
	class router,sizing_agent,coverage_critic agent
```

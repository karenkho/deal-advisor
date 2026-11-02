"""Run the graph on a TOR file and print a compact summary (no TOR text in logs)."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.graph import run  # noqa: E402
from app.parsing import parse_bytes  # noqa: E402

if __name__ == "__main__":
    path = Path(sys.argv[1])
    t0 = time.perf_counter()
    state, usage = run(parse_bytes(path.read_bytes(), path.name), path.name,
                       progress=lambda n, ms: print(f"  ✓ {n} ({ms} ms)", file=sys.stderr))
    print(f"runtime {time.perf_counter() - t0:.1f}s | usage {usage.totals()} | cache_hit {state.cache_hit}")
    print(f"raw {state.raw_statements} -> reqs {len(state.requirements)} -> features {len(state.features)}")
    for f in state.features:
        tools_used = ",".join(t.tool.split('_')[0] for t in f.trace)
        print(f"  {f.id} {f.origin:6} {str(f.route):12} {str(f.size):4} {f.type:16} p{f.pages} {f.name[:70]} [{tools_used}]")
    print("complexity:", state.complexity)
    print("questions:"); [print("  -", q.source, q.pages, q.text[:150]) for q in state.questions]
    print("exclusions:"); [print("  -", e["pages"], e["text"][:120]) for e in state.exclusions]
    print("guard:", [(g.kind, g.page) for g in state.guard_findings], "| warnings:", state.warnings, "| errors:", state.errors)
    sc = state.estimate["scenarios"] if state.estimate else []
    for s in sc:
        print(f"  {s['title']}: {s['duration_months']:.1f} mo, bottleneck {s['bottleneck']}")
    if "--json" in sys.argv:
        Path(sys.argv[sys.argv.index("--json") + 1]).write_text(state.model_dump_json(indent=1))

"""Builds samples/evidence.json, the summary the Evidence tab shows. tests/ and docs/ are not in the image, so this runs
before a build: it runs the unit tests and reads the existing results. No Azure calls.

Usage: python scripts/build_evidence.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

OUT = ROOT / "samples" / "evidence.json"
LEAK_TESTS = re.compile(r"guardrail|debug_toggle|draft_brd|record_never_stores|logs_the_section")
INJECTION_TESTS = re.compile(r"injection|mask_pii|normal_requirements")


def run_tests() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.xml"
        subprocess.run([sys.executable, "-m", "pytest", "-q", f"--junitxml={report}"], cwd=ROOT, capture_output=True)
        cases = list(ET.parse(report).getroot().iter("testcase"))

    def failed(c) -> bool:
        return any(c.find(t) is not None for t in ("failure", "error"))

    def group(rx) -> dict:
        chosen = [c for c in cases if rx.search(c.get("name"))]
        return {"total": len(chosen), "passed": sum(not failed(c) for c in chosen)}

    return {"total": len(cases), "passed": sum(not failed(c) for c in cases), "leak_guardrail": group(LEAK_TESTS),
            "injection_and_pii": group(INJECTION_TESTS)}


def regressions() -> list[dict]:
    from test_schedule import _project

    from app import effort, schedule
    from app.config import estimation_config

    cfg = estimation_config()
    out = []
    for pid, complexity, size, target in (("P01", "standard", "Mid", 6), ("P02", "high", "Large", 9)):
        rows = effort.calculate_effort(_project(pid), complexity, {})["rows"]
        team = {**schedule.template_team(size, cfg, schedule.schedule_config()), "DevOps": 0.0}
        tl = schedule.generate_project_schedule(rows, "baseline", team, size, cfg["calibration"]["ta_coding_share"],
                                                pool_devops=True)
        out.append({"project": pid, "size": size, "target_months": target, "computed_months": round(tl["core_months"], 2),
                    "within_10_pct": abs(tl["core_months"] - target) / target <= 0.10})
    return out


def section(path: Path, start: str) -> str:
    """The text from a heading to the next heading of the same level."""
    text = path.read_text()
    i = text.find(start)
    if i < 0:
        return ""
    level = start.split(" ")[0]
    rest = text[i + len(start):]
    m = re.search(rf"^{level} ", rest, re.M)
    return (start + (rest[:m.start()] if m else rest)).strip()


def main() -> None:
    from app import spend

    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain", "app", "config", "tests"], cwd=ROOT, capture_output=True,
                                text=True).stdout.strip())
    data = {"generated_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "commit": commit, "uncommitted_changes": dirty,
            "tests": run_tests(), "regressions": regressions(),
            "quick_eval": {"doc": "docs/poc_results.md", "summary": section(ROOT / "docs/poc_results.md", "## Summary")},
            "stability": {"doc": "docs/stability.md", "hris": section(ROOT / "docs/stability.md", "## tor"),
                          "tor_a": section(ROOT / "docs/stability.md", "## tor_A_clean")},
            "calibration_doc": "docs/calibration.md", "spend_ledger_usd_at_build": round(spend.spent_total(), 3)}
    OUT.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    print(f"{OUT.relative_to(ROOT)}: {data['tests']['passed']}/{data['tests']['total']} tests pass")


if __name__ == "__main__":
    main()

"""Spend guard tests (no Azure calls)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import llm, spend  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(spend, "LEDGER", tmp_path / "ledger.json")
    monkeypatch.setattr(spend, "CACHE_DIR", tmp_path)
    monkeypatch.setenv("MAX_RUN_USD", "0.25")
    monkeypatch.setenv("MAX_DAILY_USD", "1.00")


def test_estimate_and_preflight_allows_sample_sizes():
    assert spend.estimate_run_usd(5) == pytest.approx(0.05)
    assert spend.preflight(16)[0] is True            # 0.138 <= 0.25


def test_preflight_refuses_large_tor():
    ok, msg, cap = spend.preflight(60)               # 0.49 > 0.25
    assert not ok and "per-run limit" in msg and cap == 0.25


def test_daily_cap_refuses_new_runs():
    spend.record(0.80)
    assert spend.remaining_today() == pytest.approx(0.20)
    ok, msg, cap = spend.preflight(16)               # 0.138 <= 0.20 remaining
    assert ok and cap == pytest.approx(0.20)
    spend.record(0.25)
    ok, msg, _ = spend.preflight(1)
    assert not ok and "budget" in msg


def test_usage_cap_raises_before_next_call():
    u = llm.Usage(limit_usd=0.001)
    u.add("node", "gpt-4.1-mini", 10_000, 0)         # 10k input tokens x $0.40/M = $0.004
    with pytest.raises(llm.BudgetExceeded):
        u.check()
    assert not isinstance(llm.BudgetExceeded("x"), Exception)   # node fallbacks cannot swallow it


def test_total_cap_refuses_new_runs(monkeypatch):
    monkeypatch.setenv("MAX_TOTAL_USD", "10.00")
    spend.record(0.10)
    assert spend.preflight(5)[0] is True
    monkeypatch.setenv("MAX_TOTAL_USD", "0.10")
    ok, msg, cap = spend.preflight(5)
    assert not ok and "total" in msg and cap == 0.0

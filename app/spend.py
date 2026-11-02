"""Azure spend guards: per-run cap (MAX_RUN_USD), daily cap (MAX_DAILY_USD) and POC total cap (MAX_TOTAL_USD),
from token accounting.

The daily ledger is a small JSON file in .cache/. In a container it resets when the replica restarts, so the
cap is a guard against runaway use, not an invoice. Costs use config/pricing.yaml (editable estimates)."""
from __future__ import annotations

import json
import threading
import time

from app.config import CACHE_DIR, env

LEDGER = CACHE_DIR / "spend_ledger.json"
_lock = threading.Lock()

# empirical, conservative: fixed overhead + per page (full TOR A 5 p ~ $0.05, TOR B 16 p ~ $0.09)
EST_BASE_USD = 0.01
EST_PER_PAGE_USD = 0.008
# after the graph: suitability, BRD, risk agent (measured on the 10-page HRIS TOR: about USD 0.067)
EST_DOWNSTREAM_BASE_USD = 0.02
EST_DOWNSTREAM_PER_PAGE_USD = 0.005


def max_run_usd() -> float:
    return float(env("MAX_RUN_USD", "0.25"))


def max_daily_usd() -> float:
    return float(env("MAX_DAILY_USD", "1.00"))


def max_total_usd() -> float:
    return float(env("MAX_TOTAL_USD", "14.50"))


def estimate_run_usd(pages: int) -> float:
    return EST_BASE_USD + EST_PER_PAGE_USD * pages


def estimate_full_run_usd(pages: int) -> float:
    """The whole opportunity: the requirements graph plus AI suitability, BRD extraction and the risk agent."""
    return estimate_run_usd(pages) + EST_DOWNSTREAM_BASE_USD + EST_DOWNSTREAM_PER_PAGE_USD * pages


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _read() -> dict:
    try:
        return json.loads(LEDGER.read_text())
    except (OSError, ValueError):
        return {}


def spent_today() -> float:
    return float(_read().get(_today(), 0.0))


def spent_total() -> float:
    return float(sum(_read().values()))


def remaining_today() -> float:
    return max(max_daily_usd() - spent_today(), 0.0)


def record(usd: float) -> None:
    if usd <= 0:
        return
    with _lock:
        data = _read()
        data[_today()] = round(data.get(_today(), 0.0) + usd, 6)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        LEDGER.write_text(json.dumps(data, indent=1))


def preflight(pages: int, full: bool = False) -> tuple[bool, str, float]:
    """(allowed, message, run_cap). Refuses when the estimate exceeds the per-run cap or the day's remaining budget.
    full: estimate the whole opportunity, not only the requirements graph."""
    est = estimate_full_run_usd(pages) if full else estimate_run_usd(pages)
    base = EST_BASE_USD + (EST_DOWNSTREAM_BASE_USD if full else 0.0)
    per_page = EST_PER_PAGE_USD + (EST_DOWNSTREAM_PER_PAGE_USD if full else 0.0)
    remaining = remaining_today()
    cap = min(max_run_usd(), remaining)
    if spent_total() >= max_total_usd():
        return False, (f"The POC's total Azure budget (USD {max_total_usd():.2f}) has been reached. New runs are "
                       "paused; cached results still work."), 0.0
    if remaining <= 0:
        return False, (f"Today's Azure budget (USD {max_daily_usd():.2f}) has been reached. New uploads are paused "
                       "until tomorrow; the sample TORs still work from cache."), cap
    if est > cap:
        limit = "per-run limit" if cap == max_run_usd() else "remaining daily budget"
        return False, (f"This TOR ({pages} pages) is estimated at USD {est:.2f}, above the {limit} of USD {cap:.2f}. "
                       f"Try a shorter TOR (about {max(int((cap - base) / per_page), 0)} pages)."), cap
    return True, f"Estimated cost USD {est:.2f} (limit USD {cap:.2f}).", cap

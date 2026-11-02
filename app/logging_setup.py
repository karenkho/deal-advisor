"""Structured JSON logs. Callers pass counts and metadata only — never TOR text, PII or keys."""
from __future__ import annotations

import json
import logging
import sys
import time

_ALLOWED = {"node", "duration_ms", "prompt_tokens", "completion_tokens", "est_usd", "count", "counts",
            "error", "event", "document", "hits", "sections", "cache_hit", "iteration", "pages", "features", "run_id", "spent_today_usd"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
                   "level": record.levelname, "msg": record.getMessage()}
        payload.update({k: v for k, v in getattr(record, "fields", {}).items() if k in _ALLOWED})
        return json.dumps(payload, default=str)


def get_logger(name: str = "tor2backlog") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def log_event(msg: str, **fields) -> None:
    """Only whitelisted fields are emitted; error strings are truncated to exception type + 120 chars."""
    if "error" in fields and fields["error"] is not None:
        fields["error"] = str(fields["error"])[:120]
    get_logger().info(msg, extra={"fields": fields})

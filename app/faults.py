"""Fault injection for the scenario suite: FAULT_INJECT=<name>. Test only, and unset in production, where every check here is False."""
from __future__ import annotations

import os


def active(name: str) -> bool:
    return os.environ.get("FAULT_INJECT") == name

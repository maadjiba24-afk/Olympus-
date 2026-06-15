"""Lightweight operational metrics for a running instance.

Enough to answer "is it up, how much is it handling, is anything failing, what is
it spending" without pulling in a metrics stack. In-process counters (the web
server is a single process) plus spend pulled from the usage ledger. Exposed at
/healthz (liveness, unauthenticated) and /api/metrics (authenticated).
"""

from __future__ import annotations

import threading
import time

_LOCK = threading.Lock()
_START = time.time()
_C = {"requests": 0, "client_errors": 0, "server_errors": 0}
_BY_PATH: dict[str, int] = {}


def record_response(path: str, code: int) -> None:
    with _LOCK:
        _C["requests"] += 1
        if 400 <= code < 500:
            _C["client_errors"] += 1
        elif code >= 500:
            _C["server_errors"] += 1
        # keep the path table bounded
        if path not in _BY_PATH and len(_BY_PATH) < 64:
            _BY_PATH[path] = 0
        if path in _BY_PATH:
            _BY_PATH[path] += 1


def snapshot() -> dict:
    """Operational summary: uptime, traffic, errors, and today's spend."""
    from . import usage
    with _LOCK:
        counters = dict(_C)
        by_path = dict(_BY_PATH)
    out = {
        "uptime_seconds": round(time.time() - _START, 1),
        "requests": counters["requests"],
        "client_errors": counters["client_errors"],
        "server_errors": counters["server_errors"],
        "by_path": by_path,
    }
    try:
        out["budget"] = usage.budget_status()
        out["spend_today"] = round(usage.today_spend(), 4)
    except Exception:
        pass
    return out


def reset() -> None:
    """For tests."""
    global _START
    with _LOCK:
        _START = time.time()
        _C.update(requests=0, client_errors=0, server_errors=0)
        _BY_PATH.clear()

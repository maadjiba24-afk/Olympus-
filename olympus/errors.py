"""Operator-facing error capture — see failures without reading the logs.

When Olympus hits an unexpected exception serving a user, the user gets a clean
message but the failure would otherwise vanish into container logs. `capture()`
appends it to `MEMORY_DIR/errors/errors.jsonl` (durable) and pushes a short
alert over Telegram — rate-limited so an error storm can't spam the operator.
Review with `olympus errors`.
"""

from __future__ import annotations

import json
import threading
import time
import traceback

from . import config, telegram

_LOCK = threading.Lock()
_alert_times: list[float] = []
_ALERT_MAX_PER_HOUR = 12


def _path():
    d = config.MEMORY_DIR / "errors"
    d.mkdir(parents=True, exist_ok=True)
    return d / "errors.jsonl"


def _should_alert() -> bool:
    """Allow at most _ALERT_MAX_PER_HOUR Telegram alerts in any rolling hour."""
    now = time.time()
    with _LOCK:
        _alert_times[:] = [t for t in _alert_times if now - t < 3600]
        if len(_alert_times) >= _ALERT_MAX_PER_HOUR:
            return False
        _alert_times.append(now)
    return True


def capture(where: str, exc: BaseException, *, context: str = "") -> dict:
    """Record an exception and alert the operator (best-effort). Returns the
    record. Never raises — error handling must not create errors."""
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "where": str(where)[:120],
        "error": f"{type(exc).__name__}: {exc}"[:500],
        "context": str(context)[:300],
        "traceback": "".join(traceback.format_exception(
            type(exc), exc, exc.__traceback__))[-2000:],
    }
    try:
        with _path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass
    if _should_alert():
        try:
            telegram.notify(
                f"🛑 Olympus error in {rec['where']}\n\n{rec['error']}"
                + (f"\ncontext: {rec['context']}" if rec["context"] else ""))
        except Exception:
            pass
    return rec


def recent(n: int = 50) -> list[dict]:
    """The most recent captured errors, oldest-first."""
    path = _path()
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out[-n:]

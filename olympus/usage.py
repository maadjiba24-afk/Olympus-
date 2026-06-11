"""Cost accounting + global backpressure.

Every model call records token usage and an estimated dollar cost, attributed
to the active user and day. A process-wide semaphore caps concurrent model
calls so a burst of users can't trigger a rate-limit storm.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from . import config, memory

# Approximate USD per 1M tokens (input, output). Used only for local
# estimation/visibility — never billed. Unknown models fall back to DEFAULT.
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
}
DEFAULT_PRICE = (1.0, 3.0)

# Global cap on concurrent model calls across the whole process.
_SEMAPHORE = threading.BoundedSemaphore(config.MAX_CONCURRENT_CALLS)
_TOTALS_LOCK = threading.Lock()


def slot():
    """Context manager: acquire one of the limited model-call slots."""
    return _SEMAPHORE


def estimate_cost(model: str, in_tokens: int, out_tokens: int) -> float:
    price_in, price_out = PRICES.get(model, DEFAULT_PRICE)
    return (in_tokens * price_in + out_tokens * price_out) / 1_000_000


def record(model: str, in_tokens: int, out_tokens: int) -> None:
    """Append usage to the per-day ledger, attributed to the active user."""
    cost = estimate_cost(model, in_tokens, out_tokens)
    day = time.strftime("%Y-%m-%d")
    user = memory.current_user()
    path = config.MEMORY_DIR / "usage" / f"{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _TOTALS_LOCK:
        ledger = {}
        if path.exists():
            try:
                ledger = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                ledger = {}
        for key in ("__all__", f"user:{user}", f"model:{model}"):
            row = ledger.setdefault(
                key, {"calls": 0, "in": 0, "out": 0, "cost": 0.0})
            row["calls"] += 1
            row["in"] += in_tokens
            row["out"] += out_tokens
            row["cost"] = round(row["cost"] + cost, 6)
        path.write_text(json.dumps(ledger, indent=1), encoding="utf-8")


def report(days: int = 7) -> str:
    """Human-readable spend summary over the last N days."""
    base = config.MEMORY_DIR / "usage"
    if not base.exists():
        return "No usage recorded yet."
    files = sorted(base.glob("*.json"), reverse=True)[:days]
    lines = ["Usage (estimated, USD):", ""]
    grand = 0.0
    for path in sorted(files):
        try:
            ledger = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        allrow = ledger.get("__all__", {})
        cost = allrow.get("cost", 0.0)
        grand += cost
        lines.append(f"  {path.stem}: ${cost:.4f}  "
                     f"({allrow.get('calls', 0)} calls, "
                     f"{allrow.get('in', 0)+allrow.get('out', 0)} tokens)")
    lines.append("")
    lines.append(f"  total ({len(files)}d): ${grand:.4f}")
    return "\n".join(lines)

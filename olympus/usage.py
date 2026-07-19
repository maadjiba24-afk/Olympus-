"""Cost accounting + global backpressure + the budget guard.

Every model call records token usage and an estimated dollar cost, attributed
to the active user and day. A process-wide semaphore caps concurrent model
calls so a burst of users can't trigger a rate-limit storm.

The budget guard protects the user's OWN API bill. Olympus is bring-your-own-
key: every model call bills the user's Anthropic/OpenAI account directly. A
runaway loop or a long scheduled task can quietly run that bill up. If the user
sets a daily budget, Olympus stops starting new work once today's estimated
spend reaches it — a seatbelt on their provider bill, not a charge from us.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from . import config, memory


class BudgetExceeded(RuntimeError):
    """Raised when today's estimated spend has reached the daily budget."""

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


def _atomic_write_json(path: Path, obj) -> None:
    """Write JSON via a temp file + os.replace so a reader (or a crash) never
    sees a torn ledger — a truncated ledger would silently reset the day's spend
    to 0 and disable the budget guard."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
    os.replace(tmp, path)


# In-process per-user session totals (since process start). The per-day
# ledger on disk stays the durable record; this powers per-reply and
# per-session footers without a disk read per turn. Worker threads set their
# user context (memory.set_user), so parallel specialist calls attribute here.
_SESSION: dict[str, dict] = {}


def _bump_session(user: str, in_tokens: int, out_tokens: int,
                  cost: float) -> None:
    row = _SESSION.setdefault(
        user, {"calls": 0, "in": 0, "out": 0, "cost": 0.0})
    row["calls"] += 1
    row["in"] += in_tokens
    row["out"] += out_tokens
    row["cost"] = round(row["cost"] + cost, 6)


def session_totals(user: str) -> dict:
    """This user's model usage since the process started (a copy)."""
    with _TOTALS_LOCK:
        return dict(_SESSION.get(memory.safe_id(user),
                                 {"calls": 0, "in": 0, "out": 0, "cost": 0.0}))


def delta(before: dict, after: dict) -> dict:
    return {k: round(after.get(k, 0) - before.get(k, 0), 6)
            for k in ("calls", "in", "out", "cost")}


def _fmt_tokens(n: float) -> str:
    n = int(n)
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def footer(reply_delta: dict, user: str) -> str:
    """One-line cost footer for a chat reply: this reply, this session, today."""
    session = session_totals(user)
    return (f"⏱ {_fmt_tokens(reply_delta.get('in', 0))} in / "
            f"{_fmt_tokens(reply_delta.get('out', 0))} out · "
            f"~${reply_delta.get('cost', 0.0):.4f} this reply · "
            f"${session['cost']:.2f} session · ${today_spend():.2f} today")


def record(model: str, in_tokens: int, out_tokens: int) -> None:
    """Append usage to the per-day ledger, attributed to the active user."""
    cost = estimate_cost(model, in_tokens, out_tokens)
    day = time.strftime("%Y-%m-%d")
    user = memory.current_user()
    path = config.MEMORY_DIR / "usage" / f"{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    from . import proclock
    with _TOTALS_LOCK:
        _bump_session(user, in_tokens, out_tokens, cost)
    # The ledger read-modify-write holds ONLY the cross-process lock — never
    # nested inside _TOTALS_LOCK. Holding the in-process mutex across an
    # unbounded flock wait would couple session_totals()/today_spend() (the
    # per-reply hot path) to the OTHER process's liveness: a wedged heartbeat
    # holding the flock would freeze every reply here. The session bump and
    # the ledger write need no mutual atomicity (ADR 0005).
    with proclock.lock("usage-ledger"):
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
        _atomic_write_json(path, ledger)


# --- the budget guard (protects the user's own API bill) -----------------

def today_spend() -> float:
    """Total estimated USD spent across the whole instance today."""
    day = time.strftime("%Y-%m-%d")
    path = config.MEMORY_DIR / "usage" / f"{day}.json"
    if not path.exists():
        return 0.0
    # Deliberately lock-free with respect to record()'s cross-process flock:
    # correctness rests entirely on the atomic tmp+os.replace publish (a
    # reader sees the old or the new ledger, never a torn one). Taking the
    # flock here would couple every budget check to the other process's
    # liveness for no consistency gain. _TOTALS_LOCK only serializes against
    # in-process session-total updates.
    with _TOTALS_LOCK:
        try:
            ledger = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return 0.0
    return float(ledger.get("__all__", {}).get("cost", 0.0))


def daily_budget() -> float:
    """Resolved daily USD budget. A saved setting (set via `olympus budget`)
    wins over the OLYMPUS_DAILY_BUDGET env var; 0 means no cap (the default)."""
    from . import prefs
    val = prefs.get("shared", "daily_budget", None)
    if val is None:
        val = config.DAILY_BUDGET
    try:
        return max(0.0, float(val))
    except (TypeError, ValueError):
        return 0.0


def budget_status() -> dict:
    """Snapshot for display: limit, spent, remaining, and whether it's hit."""
    limit = daily_budget()
    spent = round(today_spend(), 4)
    return {
        "enabled": limit > 0,
        "limit": round(limit, 2),
        "spent": spent,
        "remaining": round(max(0.0, limit - spent), 4) if limit else None,
        "exceeded": limit > 0 and spent >= limit,
    }


def check_budget() -> None:
    """Raise BudgetExceeded if today's spend has reached the daily budget.
    Called before starting new work; a single in-flight request may overshoot
    by its own cost, so treat the budget as a soft 'stop starting' line."""
    limit = daily_budget()
    if limit > 0:
        spent = today_spend()
        if spent >= limit:
            raise BudgetExceeded(
                f"Daily budget of ${limit:.2f} reached (about ${spent:.2f} "
                f"spent today on your API key). Olympus paused to protect your "
                f"bill. Raise it with `olympus budget <amount>`, set "
                f"`olympus budget 0` to remove the cap, or wait until tomorrow.")


def set_budget(amount: float) -> str:
    """Persist the daily budget (0 disables the cap)."""
    from . import prefs
    amount = max(0.0, float(amount))
    prefs.set("shared", "daily_budget", amount)
    if amount <= 0:
        return ("Daily budget removed — Olympus will not cap spend on your "
                "API key. (You can set one anytime with `olympus budget 5`.)")
    return (f"Daily budget set to ${amount:.2f}. Olympus will pause new "
            f"requests once today's estimated spend on your API key reaches it.")


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
    b = budget_status()
    if b["enabled"]:
        flag = "  ⚠ reached" if b["exceeded"] else ""
        lines.append(f"  today's budget: ${b['spent']:.4f} / ${b['limit']:.2f}"
                     f"{flag}")
    return "\n".join(lines)

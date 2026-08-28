"""Sleep-time reflection scheduler (Plane 3.4) — ONE idle-time trigger that runs
BOTH reflection targets under budget caps and a Plane-1 contract.

Plane 3 built the two write targets separately: Target A (`reflect.reflect` —
mine failures, gate a prompt improvement) and Target B (`sleeptime.run` —
consolidate memory under Aletheia block-mode). This is the scheduler that fires
them together when the system is idle, bounded so unattended self-improvement is
never unbounded:

  * a Plane-1 behavioral contract (`reflection.cycle`) gates the whole cycle —
    it is HELD (deferred, not run) when the daily budget is reached;
  * budget caps bound the cycle: a wall-clock ceiling, a target ceiling, and a
    per-target re-check of the daily spend budget, so a cycle that starts inside
    budget still stops the moment it crosses the line;
  * a background cycle NEVER raises into the heartbeat — a failing target is
    captured and the cycle reports it, then moves on.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class ReflectionBudget:
    """Hard bounds on one sleep-time reflection cycle."""
    max_seconds: float = 120.0          # wall-clock ceiling for the whole cycle
    max_targets: int = 2                # how many targets may run this cycle
    respect_daily_budget: bool = True   # honour the usage daily-spend cap

    def __post_init__(self) -> None:
        # Clamp to sane, strictly-bounded values — a zero/huge cap would disable
        # the guard a runaway cycle exploits.
        self.max_seconds = max(0.001, min(float(self.max_seconds), 3600.0))
        self.max_targets = max(0, min(int(self.max_targets), 8))


def _budget_ok(budget: ReflectionBudget) -> bool:
    """Whether affirmative evidence says daily reflection spend is permitted.

    An unexpected budget-check failure is not evidence of headroom.  Capture it
    for the operator and hold unattended reflection until the check recovers.
    The explicit ``respect_daily_budget=False`` override remains authoritative.
    """
    if not budget.respect_daily_budget:
        return True
    from . import usage
    try:
        usage.check_budget()
        return True
    except usage.BudgetExceeded:
        return False
    except Exception as err:
        from . import errors
        errors.capture("reflection.budget_check", err)
        return False


def _target_memory(settings):
    from . import sleeptime
    return sleeptime.run(settings)          # Target B → list[str]


def _target_prompts(settings):
    from . import reflect
    return reflect.reflect(settings)        # Target A → str


# Memory first (cheap, local), then the prompt pipeline (benchmark-gated, heavier).
_TARGETS = (("memory", _target_memory), ("prompts", _target_prompts))


def sleep_cycle(settings=None, *, budget: ReflectionBudget | None = None,
                clock=None) -> dict:
    """Run one idle-time reflection cycle: both targets, under budget caps, gated
    by the `reflection.cycle` Plane-1 contract. Returns a summary dict
    {status, targets, elapsed, log}; `status` is 'held' (contract deferred it),
    'ran', or 'disabled'. Never raises — a background cycle must not crash the
    heartbeat."""
    from . import behavioral_contracts as abc

    budget = budget or ReflectionBudget()
    clock = clock or time.monotonic

    # Plane-1 contract: may a reflection cycle run at all right now?
    verdict = abc.check("reflection.cycle",
                        {"reflection_budget_ok": _budget_ok(budget)})
    if verdict.blocking:
        return {"status": "held", "targets": [], "elapsed": 0.0,
                "log": [], "reason": "; ".join(verdict.reasons) or "deferred"}

    start = clock()
    results, log = [], []
    for name, fn in _TARGETS[:budget.max_targets]:
        # Budget caps: stop the moment we cross the wall-clock ceiling or the
        # daily spend line — even mid-cycle, having started inside budget.
        if (clock() - start) >= budget.max_seconds:
            results.append({"target": name, "status": "skipped:time-budget"})
            continue
        if not _budget_ok(budget):
            results.append({"target": name, "status": "skipped:spend-budget"})
            continue
        try:
            out = fn(settings)
            lines = out if isinstance(out, list) else ([out] if out else [])
            results.append({"target": name, "status": "ran", "lines": len(lines)})
            log += [f"{name}: {ln}" for ln in lines]
        except Exception as err:
            from . import errors
            errors.capture(f"reflection.{name}", err)
            results.append({"target": name, "status": "error"})
    return {"status": "ran", "targets": results,
            "elapsed": round(clock() - start, 3), "log": log}

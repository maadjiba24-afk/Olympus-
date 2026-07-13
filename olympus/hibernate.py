"""Serverless / hibernating execution — pay nothing while idle.

`olympus heartbeat` runs a process forever, sleeping between ticks. That's fine
on an always-on box, but wasteful on serverless infra (Modal, Daytona, Lambda,
a systemd timer, or plain cron) where you'd rather the process **wake, do the
due work, report when it's next needed, and exit** — costing nothing in between.

This module provides exactly that:

  * ``run_once()`` runs a single heartbeat tick and returns a summary plus the
    number of seconds until the next scheduled work is due.
  * ``next_due_in(state, now)`` computes that wake time across both the
    heartbeat's own cadences (scans, learning, audit, backup, …) and any
    user-defined scheduler jobs — so an external trigger knows exactly when to
    invoke Olympus again.

CLI:  ``olympus tick``  (run one tick, print next-wake, exit)
      ``olympus next-wake``  (just print seconds until the next due work)
"""

from __future__ import annotations

from . import config, memory, scheduler


def _cadences() -> dict[str, int]:
    """The heartbeat's internal task cadences (seconds). Mirrors heartbeat.tick;
    a 0/falsey interval means the task is disabled and is skipped here."""
    from . import curator
    items = {
        "opportunity_scan": config.OPPORTUNITY_SCAN_EVERY,
        "watchlist": config.WATCHLIST_EVERY,
        "maintenance": config.MAINTENANCE_EVERY,
        "daily_learning": config.DAILY_LEARNING_EVERY,
        "dreaming": config.DREAM_EVERY,
        "train": config.TRAIN_EVERY,
        "evolution_audit": config.EVOLUTION_AUDIT_EVERY,
        "skill_curation": curator.curation_every(),
        "feature_evolution": config.FEATURE_EVOLUTION_EVERY,
        "replay_gate": config.REPLAY_GATE_EVERY,
        "backup": config.BACKUP_EVERY,
    }
    return {k: v for k, v in items.items() if v}


def next_due_in(state: dict | None = None, now: float | None = None) -> float:
    """Seconds until the soonest due work (heartbeat cadence or scheduler job).
    0.0 means something is due right now."""
    import time
    now = now if now is not None else time.time()
    state = state if state is not None else memory.load_state()
    waits = []
    for key, interval in _cadences().items():
        last = state.get(key, 0.0)
        waits.append(max(0.0, interval - (now - last)))
    job_wait = scheduler.next_due_in(now)
    if job_wait is not None:
        waits.append(job_wait)
    from . import goals
    goal_wait = goals.next_due_in(now)
    if goal_wait is not None:
        waits.append(goal_wait)
    from . import agentbeat
    beat_wait = agentbeat.next_due_in(now)
    if beat_wait is not None:
        waits.append(beat_wait)
    return min(waits) if waits else float(config.DAILY_LEARNING_EVERY)


def run_once(now: float | None = None) -> dict:
    """Run one heartbeat tick, persist state, and report the next wake time.
    Returns {"log": [...], "next_wake_secs": float}."""
    from . import heartbeat
    state = memory.load_state()
    log = heartbeat.tick(state, now=now)          # tick() saves state itself
    state = memory.load_state()                   # reload post-tick timestamps
    return {"log": log, "next_wake_secs": round(next_due_in(state, now), 1)}

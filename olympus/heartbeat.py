"""The self-recurring loop that makes Olympus self-sufficient.

Run with `python -m olympus heartbeat`. On its own cadence it:
  - sends Argus to scan the web for opportunities and world events,
  - has Mnemosyne watch queued YouTube videos and store the lessons,
  - triggers Prometheus's weekly self-audit and self-upgrade.
"""

from __future__ import annotations

import time
import traceback

from . import config, memory, orchestrator, telegram


def _due(state: dict, key: str, interval: int, now: float) -> bool:
    return now - state.get(key, 0.0) >= interval


def tick(state: dict, now: float | None = None) -> list[str]:
    """Run any due tasks once; return a log of what happened."""
    now = now or time.time()
    log: list[str] = []

    if _due(state, "opportunity_scan", config.OPPORTUNITY_SCAN_EVERY, now):
        log.append("Argus: scanning the world for opportunities...")
        try:
            report = orchestrator.opportunity_scan()
            log.append("Argus: report saved to memory/reports.")
            if telegram.notify("🌐 Olympus opportunity scan:\n\n" + report):
                log.append("Argus: report pushed to Telegram.")
        except Exception:
            log.append("Argus failed:\n" + traceback.format_exc())
        state["opportunity_scan"] = now

    if _due(state, "watchlist", config.WATCHLIST_EVERY, now):
        url = memory.watchlist_pop()
        if url:
            log.append(f"Mnemosyne: watching {url} ...")
            try:
                orchestrator.watch_and_learn(url)
                log.append("Mnemosyne: lessons saved to memory/lessons.")
            except Exception:
                log.append("Mnemosyne failed:\n" + traceback.format_exc())
        state["watchlist"] = now

    if _due(state, "maintenance", config.MAINTENANCE_EVERY, now):
        try:
            removed = memory.sweep_dated_files(config.RETAIN_DAYS)
            orphans = memory.sweep_orphan_responses()
            if removed or orphans:
                log.append(f"Maintenance: removed {removed} old trace/usage "
                           f"files and {orphans} orphaned frozen responses.")
        except Exception:
            log.append("Maintenance failed:\n" + traceback.format_exc())
        state["maintenance"] = now

    if _due(state, "daily_learning", config.DAILY_LEARNING_EVERY, now):
        log.append("Metis: running the daily learning cycle...")
        try:
            orchestrator.daily_learning()
            log.append("Metis: skills updated; report saved to memory/reports.")
        except Exception:
            log.append("Metis failed:\n" + traceback.format_exc())
        state["daily_learning"] = now

    if config.TRAIN_EVERY and _due(state, "train", config.TRAIN_EVERY, now):
        log.append("Prometheus: training the weakest specialists...")
        try:
            orchestrator.train_specialists()
            log.append("Prometheus: training round saved to memory/reports.")
        except Exception:
            log.append("Training failed:\n" + traceback.format_exc())
        state["train"] = now

    if _due(state, "evolution_audit", config.EVOLUTION_AUDIT_EVERY, now):
        log.append("Prometheus: running self-audit and self-upgrade...")
        try:
            report = orchestrator.evolution_audit()
            log.append("Prometheus: audit saved to memory/reports.")
            if telegram.notify("🔧 Olympus self-audit:\n\n" + report):
                log.append("Prometheus: audit pushed to Telegram.")
        except Exception:
            log.append("Prometheus failed:\n" + traceback.format_exc())
        state["evolution_audit"] = now

    memory.save_state(state)
    return log


def run_forever() -> None:
    state = memory.load_state()
    print("Olympus heartbeat started. Ctrl-C to stop.")
    print(f"  opportunity scan : every {config.OPPORTUNITY_SCAN_EVERY // 3600} h")
    print(f"  youtube watchlist: every {config.WATCHLIST_EVERY // 60} min")
    print(f"  daily learning   : every {config.DAILY_LEARNING_EVERY // 3600} h")
    if config.TRAIN_EVERY:
        print(f"  specialist train : every {config.TRAIN_EVERY // 86400} d")
    print(f"  evolution audit  : every {config.EVOLUTION_AUDIT_EVERY // 86400} d")
    while True:
        for line in tick(state):
            print(f"[heartbeat] {line}")
        time.sleep(config.HEARTBEAT_TICK)

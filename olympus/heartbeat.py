"""The self-recurring loop that makes Olympus self-sufficient.

Run with `python -m olympus heartbeat`. On its own cadence it:
  - sends Argus to scan the web for opportunities and world events,
  - has Mnemosyne watch queued YouTube videos and store the lessons,
  - triggers Prometheus's weekly self-audit and self-upgrade.
"""

from __future__ import annotations

import time
import traceback

from . import config, gateway, memory, orchestrator, scheduler


def _due(state: dict, key: str, interval: int, now: float) -> bool:
    # A non-positive cadence means the cycle is OFF, not "run every tick" — with
    # interval <= 0 the `>=` test would be true on every heartbeat.
    if interval <= 0:
        return False
    return now - state.get(key, 0.0) >= interval


def tick(state: dict, now: float | None = None) -> list[str]:
    """Run any due tasks once; return a log of what happened."""
    now = now or time.time()
    log: list[str] = []

    # Upgrade/restart handoff: the previous process journaled what it was in
    # the middle of. Report it once — each subsystem (gateway inflight,
    # scheduler interrupted-run resume) re-runs its own work.
    try:
        from . import selfupdate
        handoff = selfupdate.take_handoff()
        if handoff:
            pending = handoff.get("pending", {})
            carried = (len(pending.get("inflight", []))
                       + len(pending.get("jobs_running", [])))
            note = (f"⚡ Olympus restarted (was "
                    f"v{handoff.get('from_version', '?')}).")
            if carried:
                note += (f" {carried} in-flight task(s) carried across the "
                         "restart and will resume.")
            log.append(note)
            if carried:
                gateway.notify_all(note)
    except Exception:
        log.append("Handoff check failed:\n" + traceback.format_exc())

    # User-defined scheduled tasks (natural-language cron). Checked every tick;
    # each job has its own interval, so this is cheap when nothing is due.
    try:
        for line in scheduler.run_due(now):
            log.append("Scheduler: " + line)
    except Exception:
        log.append("Scheduler failed:\n" + traceback.format_exc())

    # Always-on operator jobs (HERMES). No-op unless OLYMPUS_OPERATOR is on; each
    # job still runs through the approval spine (scope/budget/approval all apply).
    try:
        from . import operator
        for line in operator.run_due(now):
            log.append("Operator: " + line)
    except Exception:
        log.append("Operator failed:\n" + traceback.format_exc())

    # Per-agent heartbeats: compact autonomous wake-ups. Each beat has its own
    # cadence and stays quiet (HB_OK) unless something needs attention.
    try:
        from . import agentbeat
        for line in agentbeat.run_due(now):
            log.append("Heartbeats: " + line)
    except Exception:
        log.append("Heartbeats failed:\n" + traceback.format_exc())

    # Web change-monitors: check watched pages whose cadence elapsed and notify
    # on a real change. No-op unless OLYMPUS_WEB_MONITOR is on (and forced off
    # during replay); each check goes through the SSRF-gated fetch and reports
    # changed content as untrusted data.
    try:
        from . import webmonitor
        for line in webmonitor.run_due(now):
            log.append("Monitor: " + line)
    except Exception:
        log.append("Web monitor failed:\n" + traceback.format_exc())

    # Web reflection: turn accumulated domain lore into discoveries (new formats
    # to use, feeds to watch, sites needing interaction). No-op unless
    # OLYMPUS_WEB_REFLECT is on; surfaces each standing pattern once.
    try:
        from . import webreflect
        for line in webreflect.run_due(now):
            log.append("WebReflect: " + line)
    except Exception:
        log.append("Web reflection failed:\n" + traceback.format_exc())

    # Standing goals: one unit of work + an evidence-based completion judgment
    # per goal per cadence. Only a goal CLOSING (done/stalled) pushes to chat.
    try:
        from . import goals
        for line in goals.run_due(now):
            log.append("Goals: " + line)
            if "COMPLETE" in line or "STALLED" in line:
                gateway.notify_all("🎯 " + line)
    except Exception:
        log.append("Goals failed:\n" + traceback.format_exc())

    if _due(state, "opportunity_scan", config.OPPORTUNITY_SCAN_EVERY, now):
        log.append("Argus: scanning the world for opportunities...")
        try:
            report = orchestrator.opportunity_scan()
            log.append("Argus: report saved to memory/reports.")
            pushed = gateway.notify_all("🌐 Olympus opportunity scan:\n\n" + report)
            if pushed:
                log.append(f"Argus: report pushed to {', '.join(pushed)}.")
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
            tool_res = memory.sweep_tool_results(config.RETAIN_DAYS)
            if removed or orphans or tool_res:
                log.append(f"Maintenance: removed {removed} old trace/usage "
                           f"files, {orphans} orphaned frozen responses, and "
                           f"{tool_res} aged tool results.")
            from . import search
            idx = search.maintain()
            if idx.get("vacuumed"):
                log.append(f"Search index: pruned {idx['orphans']} orphaned + "
                           f"{idx['aged']} aged conversation(s); vacuumed.")
            from . import domainlore
            stale = domainlore.prune(config.RETAIN_DAYS, now)
            if stale:
                log.append(f"Web knowledge: pruned {stale} stale domain(s).")
        except Exception:
            log.append("Maintenance failed:\n" + traceback.format_exc())
        state["maintenance"] = now

    if _due(state, "dreaming", config.DREAM_EVERY, now):
        try:
            from . import wiki
            # Quiet when there was nothing to consolidate — a nightly job
            # must not turn the heartbeat log into a metronome.
            log += ["Wiki: " + line for line in wiki.dream_all(now)]
        except Exception:
            log.append("Dreaming failed:\n" + traceback.format_exc())
        state["dreaming"] = now

    if config.sleeptime_enabled() and _due(state, "sleeptime",
                                            config.SLEEPTIME_EVERY, now):
        # The unified sleep-time reflection cycle: BOTH targets (memory
        # consolidation + prompt reflection) under budget caps and the
        # reflection.cycle Plane-1 contract (Plane 3.4).
        try:
            from . import reflection
            log += reflection.sleep_cycle().get("log", [])
        except Exception:
            log.append("Sleep-time reflection failed:\n" + traceback.format_exc())
        state["sleeptime"] = now

    if config.live_eval_enabled() and _due(state, "live_eval",
                                           config.LIVE_EVAL_EVERY, now):
        try:
            from . import liveeval
            log += liveeval.run()
        except Exception:
            log.append("Live-eval failed:\n" + traceback.format_exc())
        state["live_eval"] = now

    if _due(state, "daily_learning", config.DAILY_LEARNING_EVERY, now):
        log.append("Metis: running the daily learning cycle...")
        try:
            orchestrator.daily_learning()
            log.append("Metis: skills updated; report saved to memory/reports.")
        except Exception:
            log.append("Metis failed:\n" + traceback.format_exc())
        # Metis also prunes drifted operator site profiles (Phase 4).
        try:
            from . import operator
            log.append("Operator review: " + operator.review_profiles())
        except Exception:
            log.append("Operator review failed:\n" + traceback.format_exc())
        state["daily_learning"] = now

    from . import discovery
    if discovery.enabled() and _due(state, "discovery", config.DISCOVERY_EVERY, now):
        log.append("Discovery: closing knowledge/capability gaps...")
        try:
            r = discovery.run()
            for line in r.get("learned", []) + r.get("proposed", []):
                log.append("Discovery: " + line)
            log.append(f"Discovery: {r.get('open_knowledge', 0)} knowledge / "
                       f"{r.get('open_capability', 0)} capability gap(s) open.")
        except Exception:
            log.append("Discovery failed:\n" + traceback.format_exc())
        state["discovery"] = now

    if config.TRAIN_EVERY and _due(state, "train", config.TRAIN_EVERY, now):
        log.append("Prometheus: training the weakest specialists...")
        try:
            orchestrator.train_specialists()
            log.append("Prometheus: training round saved to memory/reports.")
        except Exception:
            log.append("Training failed:\n" + traceback.format_exc())
        state["train"] = now

    from . import curator
    if _due(state, "skill_curation", curator.curation_every(), now):
        log.append("Curator: grading and pruning the skill library...")
        try:
            log.append("Curator: " + curator.curate())
        except Exception:
            log.append("Curator failed:\n" + traceback.format_exc())
        state["skill_curation"] = now

    from . import evolve
    if _due(state, "feature_evolution", config.FEATURE_EVOLUTION_EVERY, now):
        try:
            log.append(evolve.review())
        except Exception:
            log.append("Feature-evolution review failed:\n"
                       + traceback.format_exc())
        state["feature_evolution"] = now

    if _due(state, "evolution_audit", config.EVOLUTION_AUDIT_EVERY, now):
        log.append("Prometheus: running self-audit and self-upgrade...")
        try:
            report = orchestrator.evolution_audit()
            log.append("Prometheus: audit saved to memory/reports.")
            pushed = gateway.notify_all("🔧 Olympus self-audit:\n\n" + report)
            if pushed:
                log.append(f"Prometheus: audit pushed to {', '.join(pushed)}.")
        except Exception:
            log.append("Prometheus failed:\n" + traceback.format_exc())
        state["evolution_audit"] = now

    if config.REPLAY_GATE_EVERY and _due(state, "replay_gate",
                                          config.REPLAY_GATE_EVERY, now):
        try:
            from . import firstrun, replaygate
            if not firstrun.configured():
                log.append("Replay self-check skipped: no provider key.")
            else:
                res = replaygate.self_check(report=lambda _m: None)
                if not res.get("ran"):
                    log.append(f"Replay self-check skipped: {res.get('skipped')}")
                elif res["passed"]:
                    log.append(f"Replay self-check: PASS — {res['summary']}")
                else:
                    msg = "Replay self-check: FAILED — divergence alert raised"
                    if res.get("issue"):
                        msg += f" (GitHub issue: {res['issue']})"
                    log.append(msg)
        except Exception:
            log.append("Replay self-check errored:\n" + traceback.format_exc())
        state["replay_gate"] = now

    if config.BACKUP_EVERY and _due(state, "backup", config.BACKUP_EVERY, now):
        try:
            from . import backup
            res = backup.run()
            if res.get("ok"):
                where = res.get("via") or ("delivered" if res.get("delivered")
                                           else "local only")
                log.append(f"Backup: {res['files']} files, "
                           f"{res['bytes'] // 1024} KB, "
                           f"{'encrypted' if res.get('encrypted') else 'PLAINTEXT'},"
                           f" {where}.")
            else:
                log.append(f"Backup FAILED at {res.get('stage')}: "
                           f"{res.get('error')}")
                gateway.notify_all("⚠️ Olympus backup failed at "
                                   f"{res.get('stage')}: {res.get('error')}")
        except Exception:
            log.append("Backup errored:\n" + traceback.format_exc())
        state["backup"] = now

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
    if config.REPLAY_GATE_EVERY:
        print(f"  replay self-check: every {config.REPLAY_GATE_EVERY // 86400} d")
    if config.BACKUP_EVERY:
        dest = "off-droplet" if config.backup_command() else "local only"
        print(f"  data backup      : every {config.BACKUP_EVERY // 3600} h "
              f"({dest})")
    while True:
        for line in tick(state):
            print(f"[heartbeat] {line}")
        time.sleep(config.HEARTBEAT_TICK)

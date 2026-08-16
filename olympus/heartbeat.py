"""The self-recurring loop that makes Olympus self-sufficient.

Run with `python -m olympus heartbeat`. On its own cadence it:
  - sends Argus to scan the web for opportunities and world events,
  - has Mnemosyne watch queued YouTube videos and store the lessons,
  - triggers Prometheus's weekly self-audit and self-upgrade.
"""

from __future__ import annotations

import contextvars
import os
import threading
import time
import traceback

from . import config, gateway, memory, orchestrator, scheduler

# --- W2-C6: progress-watchdog supervision (OLYMPUS_WATCHDOG, default off) ---

#: How often a supervised job is re-classified while it is still running.
#: PROVISIONAL, and only ever a poll granularity — it is not a threshold.
_JOB_POLL_SECS = 0.25


def _clock() -> float:
    """The clock the heartbeat's progress leases run on.

    Indirected through the module so a test can inject a deterministic clock
    (and so watchdog thresholds can be exercised) instead of sleeping."""
    return time.time()


class JobAbandoned(RuntimeError):
    """An enforce-mode watchdog verdict abandoned a wedged heartbeat job so the
    serial tick could continue to the next cadence."""


def _wd_capture(where: str, err: BaseException, context: str = "") -> None:
    """Best-effort error capture. A watchdog failure must never break a tick,
    so every watchdog interaction below funnels through here."""
    try:
        from . import errors
        errors.capture(where, err, context=context)
    except Exception:                        # noqa: BLE001 - never raise
        pass


def _wd_supervise(lease, name: str) -> bool:
    """Classify the lease once; True if the job may continue.

    FAIL-SAFE: if anything at all goes wrong inside the watchdog, the answer is
    True — a broken watchdog must never abandon a healthy job."""
    try:
        lease.check()
        return bool(lease.continue_allowed())
    except Exception as err:                 # noqa: BLE001 - never raise
        _wd_capture(f"heartbeat.watchdog.check:{name}", err, context=name)
        return True


def _wd_do(lease, method: str, name: str, *args) -> None:
    try:
        getattr(lease, method)(*args)
    except Exception as err:                 # noqa: BLE001 - never raise
        _wd_capture(f"heartbeat.watchdog.{method}:{name}", err, context=name)


#: Absolute wall-clock ceiling for ONE heartbeat job, independent of the
#: watchdog. 30 minutes: this is a WEDGE BACKSTOP, not an SLA — a legitimate
#: `train_specialists` or `daily_learning` pass makes many model calls and can
#: run for many minutes, and abandoning honest work is worse than the stall it
#: would prevent. (absorption doc 11 cites 300s, but that is the watchdog's
#: *progress-based* stall threshold: a job still emitting beats keeps its lease.
#: An absolute deadline cannot tell progress from silence, so it must be far
#: more generous than an activity timeout.)
_DEFAULT_JOB_TIMEOUT = 1800.0


class JobTimedOut(RuntimeError):
    """One heartbeat job exceeded its absolute wall-clock ceiling."""


def job_timeout_secs() -> float:
    """The per-job ceiling; 0 disables it (fully inline, pre-hardening tick).

    Reads `OLYMPUS_JOB_STALL_SECS`, which .env.example has documented as the
    "background-job stall threshold" while nothing read it — the knob existed
    only in prose."""
    raw = os.environ.get("OLYMPUS_JOB_STALL_SECS", "").strip().lower()
    if raw in ("0", "off", "none", "never"):
        return 0.0
    if not raw:
        return _DEFAULT_JOB_TIMEOUT
    try:
        return max(1.0, float(raw))
    except ValueError:
        return _DEFAULT_JOB_TIMEOUT


def _run_with_deadline(name: str, fn):
    """Run `fn` with an absolute ceiling, without involving the watchdog.

    The watchdog is the *sophisticated* answer to a wedged job, and it is off by
    default — deliberately, because its thresholds are self-declared PROVISIONAL
    and arming an uncalibrated classifier by default would abandon honest work.
    But "off" left the shipped configuration with NO protection at all: `tick()`
    runs every cadence serially and inline, so one provider that accepts a
    request and never streams a token stalls the scheduler, goals, agent beats
    and BACKUPS indefinitely and silently.

    This is the crude floor under that: no classification, no forensics, no
    verdict — just a deadline. It needs no calibration to be safe because it
    only fires where the alternative is waiting forever. `JobTimedOut` is a
    plain exception, so each cadence's existing `except Exception` logs it and
    the tick moves on."""
    limit = job_timeout_secs()
    if limit <= 0:
        return fn()

    box: dict = {}
    ctx = contextvars.copy_context()

    def _runner() -> None:
        try:
            box["value"] = ctx.run(fn)
        except BaseException as err:          # re-raised on the tick's thread
            box["error"] = err

    worker = threading.Thread(target=_runner, name=f"olympus-job-{name}",
                              daemon=True)
    worker.start()
    worker.join(limit)
    if worker.is_alive():
        # The thread is a daemon and cannot be killed; it is abandoned exactly
        # as the watchdog's enforce path abandons one, and for the same reason.
        raise JobTimedOut(
            f"{name}: abandoned after {limit:.0f}s (OLYMPUS_JOB_STALL_SECS). "
            f"The job is still running on a background thread; the tick "
            f"continued so the remaining cadences are not stalled with it.")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _watch_job(name: str, fn):
    """Run ONE heartbeat job under a Wave-2 progress lease (W2-C6).

    This is the concrete defect the watchdog was written for: `tick()` runs
    every cadence SERIALLY and INLINE, so one wedged job — a provider that
    accepts the request and never streams a token — stalls the scheduler,
    goals, backups and every cadence after it, silently and indefinitely.
    `proclock.DEFAULT_TIMEOUT` cannot help: a wedged holder is alive.

    OFF (`OLYMPUS_WATCHDOG` unset — the shipped default): `fn()` is called
    directly. No lease, no thread, no classification, no clock read — the tick
    is byte-for-byte the tick it always was.

    OBSERVE: the job runs on a worker thread and the tick waits for it exactly
    as long as it would have waited inline — a wedged job still stalls the
    tick, because observe changes NOTHING. It is classified while it runs and
    its forensics are preserved, so the stall is visible instead of silent.

    ENFORCE: the same supervision, but a tripping verdict ABANDONS the job
    (raising `JobAbandoned`, which each call site's existing `except Exception`
    already logs) and the tick continues to the next cadence.

    An abandoned job's thread is a daemon and cannot be killed, so the lease is
    deliberately given NO `release_fn`: this seam cannot honestly release an
    admission slot held inside a thread it no longer controls, and the lease
    records that as `slot_release_skipped` rather than pretending.

    Fail-safe by construction: failing to arm, classify or close the lease all
    degrade to running the job exactly as an unsupervised tick would.
    """
    try:
        from . import watchdog
        if not watchdog.enabled():
            return _run_with_deadline(name, fn)
        lease = watchdog.ProgressLease(f"heartbeat:{name}", kind="job",
                                       clock=_clock)
    except Exception as err:                 # noqa: BLE001 - never raise
        _wd_capture(f"heartbeat.watchdog.open:{name}", err, context=name)
        return _run_with_deadline(name, fn)

    box: dict = {}
    # A worker thread starts with a fresh context, so carry the tick's
    # contextvars (memory's user namespace above all) across the hop.
    ctx = contextvars.copy_context()

    def _runner() -> None:
        try:
            box["value"] = ctx.run(fn)
        except BaseException as err:         # re-raised on the tick's thread
            box["error"] = err

    worker = threading.Thread(target=_runner, name=f"olympus-job-{name}",
                              daemon=True)
    worker.start()
    # The same absolute ceiling applies here. `observe` deliberately changes
    # nothing about scheduling, so without this an observed wedge still stalls
    # the tick forever — it would merely be a *documented* stall.
    limit = job_timeout_secs()
    deadline = (_clock() + limit) if limit > 0 else None
    while True:
        worker.join(_JOB_POLL_SECS)
        if not worker.is_alive():
            break
        if deadline is not None and _clock() >= deadline:
            _wd_do(lease, "close", name, "abandoned")
            raise JobTimedOut(
                f"{name}: abandoned after {limit:.0f}s "
                f"(OLYMPUS_JOB_STALL_SECS) — the tick continued without it.")
        if not _wd_supervise(lease, name):
            _wd_do(lease, "close", name, "abandoned")
            raise JobAbandoned(
                f"{name}: abandoned by the progress watchdog — "
                f"{getattr(lease, 'refusal_reason', '') or 'no progress'}")
    if "error" in box:
        _wd_do(lease, "close", name, "error")
        raise box["error"]
    # The job returned: one cadence completed is one verifiable plan node done
    # (W2-I6.1 — this is the job's own completion, not text a model emitted).
    _wd_do(lease, "beat", name, "plan_node_completed")
    _wd_supervise(lease, name)
    _wd_do(lease, "close", name, "ok")
    return box.get("value")


def _due(state: dict, key: str, interval: int, now: float) -> bool:
    # A non-positive cadence means the cycle is OFF, not "run every tick" — with
    # interval <= 0 the `>=` test would be true on every heartbeat.
    if interval <= 0:
        return False
    return now - state.get(key, 0.0) >= interval


def _job_error(label: str, configured: bool) -> str:
    """Failure line for an LLM-dependent heartbeat job. On a keyless install the
    provider SDK raises before any network call, so every such job fails the same
    way each tick — collapse that expected no-key failure into one quiet line
    instead of a full traceback per job (FEATURE_AUDIT §2.2), matching the
    replay self-check's `firstrun.configured()` guard. A real failure with a key
    present still gets the full traceback."""
    if not configured:
        return f"{label}: skipped (no provider key configured)"
    return f"{label} failed:\n" + traceback.format_exc()


def tick(state: dict, now: float | None = None) -> list[str]:
    """Run any due tasks once; return a log of what happened."""
    now = now or time.time()
    log: list[str] = []
    # Whether any model backend is reachable. The LLM-dependent cadences below
    # use this (via _job_error) to log a one-line skip instead of a traceback on
    # a keyless install, rather than each spewing a full auth traceback per tick.
    from . import firstrun
    configured = firstrun.configured()

    # Upgrade/restart handoff: the previous process journaled what it was in
    # the middle of. Report it once — each subsystem (gateway inflight,
    # scheduler interrupted-run resume) re-runs its own work.
    def _job_handoff() -> None:
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

    try:
        _watch_job("handoff", _job_handoff)
    except Exception:
        log.append("Handoff check failed:\n" + traceback.format_exc())

    # User-defined scheduled tasks (natural-language cron). Checked every tick;
    # each job has its own interval, so this is cheap when nothing is due.
    def _job_scheduler() -> None:
        for line in scheduler.run_due(now):
            log.append("Scheduler: " + line)

    try:
        _watch_job("scheduler", _job_scheduler)
    except Exception:
        log.append("Scheduler failed:\n" + traceback.format_exc())

    # Always-on operator jobs (HERMES). No-op unless OLYMPUS_OPERATOR is on; each
    # job still runs through the approval spine (scope/budget/approval all apply).
    def _job_operator() -> None:
        from . import operator
        for line in operator.run_due(now):
            log.append("Operator: " + line)

    try:
        _watch_job("operator", _job_operator)
    except Exception:
        log.append("Operator failed:\n" + traceback.format_exc())

    # Per-agent heartbeats: compact autonomous wake-ups. Each beat has its own
    # cadence and stays quiet (HB_OK) unless something needs attention.
    def _job_agentbeat() -> None:
        from . import agentbeat
        for line in agentbeat.run_due(now):
            log.append("Heartbeats: " + line)

    try:
        _watch_job("agentbeat", _job_agentbeat)
    except Exception:
        log.append("Heartbeats failed:\n" + traceback.format_exc())

    # Web change-monitors: check watched pages whose cadence elapsed and notify
    # on a real change. No-op unless OLYMPUS_WEB_MONITOR is on (and forced off
    # during replay); each check goes through the SSRF-gated fetch and reports
    # changed content as untrusted data.
    def _job_webmonitor() -> None:
        from . import webmonitor
        for line in webmonitor.run_due(now):
            log.append("Monitor: " + line)

    try:
        _watch_job("webmonitor", _job_webmonitor)
    except Exception:
        log.append("Web monitor failed:\n" + traceback.format_exc())

    # Web reflection: turn accumulated domain lore into discoveries (new formats
    # to use, feeds to watch, sites needing interaction). No-op unless
    # OLYMPUS_WEB_REFLECT is on; surfaces each standing pattern once.
    def _job_webreflect() -> None:
        from . import webreflect
        for line in webreflect.run_due(now):
            log.append("WebReflect: " + line)

    try:
        _watch_job("webreflect", _job_webreflect)
    except Exception:
        log.append("Web reflection failed:\n" + traceback.format_exc())

    # Standing goals: one unit of work + an evidence-based completion judgment
    # per goal per cadence. Only a goal CLOSING (done/stalled) pushes to chat.
    def _job_goals() -> None:
        from . import goals
        for line in goals.run_due(now):
            log.append("Goals: " + line)
            if "COMPLETE" in line or "STALLED" in line:
                gateway.notify_all("🎯 " + line)

    try:
        _watch_job("goals", _job_goals)
    except Exception:
        log.append("Goals failed:\n" + traceback.format_exc())

    if _due(state, "opportunity_scan", config.OPPORTUNITY_SCAN_EVERY, now):
        log.append("Argus: scanning the world for opportunities...")

        def _job_argus() -> None:
            report = orchestrator.opportunity_scan()
            log.append("Argus: report saved to memory/reports.")
            pushed = gateway.notify_all("🌐 Olympus opportunity scan:\n\n" + report)
            if pushed:
                log.append(f"Argus: report pushed to {', '.join(pushed)}.")

        try:
            _watch_job("opportunity_scan", _job_argus)
        except Exception:
            log.append(_job_error("Argus", configured))
        state["opportunity_scan"] = now

    if _due(state, "watchlist", config.WATCHLIST_EVERY, now):
        url = memory.watchlist_pop()
        if url:
            log.append(f"Mnemosyne: watching {url} ...")

            def _job_watchlist() -> None:
                orchestrator.watch_and_learn(url)
                log.append("Mnemosyne: lessons saved to memory/lessons.")

            try:
                _watch_job("watchlist", _job_watchlist)
            except Exception:
                log.append(_job_error("Mnemosyne", configured))
        state["watchlist"] = now

    if _due(state, "maintenance", config.MAINTENANCE_EVERY, now):
        def _job_maintenance() -> None:
            removed = memory.sweep_dated_files(config.RETAIN_DAYS)
            orphans = memory.sweep_orphan_responses()
            tool_res = memory.sweep_tool_results(config.RETAIN_DAYS)
            # Absorption evidence ledgers + watchdog forensics: without this they
            # grow unbounded, which would make raw operational logs permanent
            # moat data by default (Phase-4 Stage-E finding).
            evidence = memory.sweep_evidence(config.RETAIN_DAYS)
            if removed or orphans or tool_res or evidence:
                log.append(f"Maintenance: removed {removed} old trace/usage "
                           f"files, {orphans} orphaned frozen responses, "
                           f"{tool_res} aged tool results, and {evidence} aged "
                           f"evidence records.")
            from . import search
            idx = search.maintain()
            if idx.get("vacuumed"):
                log.append(f"Search index: pruned {idx['orphans']} orphaned + "
                           f"{idx['aged']} aged conversation(s); vacuumed.")
            from . import domainlore
            stale = domainlore.prune(config.RETAIN_DAYS, now)
            if stale:
                log.append(f"Web knowledge: pruned {stale} stale domain(s).")
            # Conversation retention (W1-2). Until now `sweep_conversations`
            # had no production caller at all, so OLYMPUS_CONVERSATION_RETAIN_
            # DAYS enforced nothing and the policy was decorative.
            #
            # NOTE the two arguments NOT passed here, both deliberate:
            #
            #   * NO `retain_days`. `config.RETAIN_DAYS` is in scope directly
            #     above (30, always set) and is the obvious-looking argument,
            #     but it belongs to trace/usage/evidence files. Conversation
            #     retention has its OWN knob which defaults to None = UNSET,
            #     because `retention.py` refuses to guess a deletion policy for
            #     user content — `deployment_blocked_reason()` exists to report
            #     that refusal. Passing RETAIN_DAYS would silently invent a
            #     30-day conversation-deletion policy on every deployment that
            #     never asked for one. Unset must stay a no-op.
            #   * `dry_run=False` IS passed. It defaults to True, so omitting
            #     it would ship a scheduler that runs forever, logs plausible
            #     output and deletes nothing.
            from . import retention
            swept = retention.sweep_conversations(dry_run=False)
            if swept["removed"] or swept["held"]:
                log.append(
                    f"Retention ({swept['policy']}): removed "
                    f"{swept['removed']} conversation(s); "
                    f"{len(swept['held'])} under legal hold.")
            # Journal compaction (W1-2). Also had no production caller, so
            # journals grew to OLYMPUS_SESSION_JOURNAL_MAX_MB and then stopped
            # accepting appends AND stopped serving crash recovery — silently,
            # and on the heaviest sessions first.
            from . import sessionlog
            comp = sessionlog.compact_due()
            if comp["compacted"]:
                saved = comp["bytes_before"] - comp["bytes_after"]
                log.append(f"Journals: compacted {len(comp['compacted'])} "
                           f"session(s), reclaiming {saved // 1024} KiB.")
            if comp["skipped"]:
                log.append(f"Journals: {len(comp['skipped'])} oversized "
                           f"session(s) had nothing safe to compact.")

        try:
            _watch_job("maintenance", _job_maintenance)
        except Exception:
            log.append("Maintenance failed:\n" + traceback.format_exc())
        state["maintenance"] = now

    if _due(state, "dreaming", config.DREAM_EVERY, now):
        def _job_dreaming() -> None:
            from . import wiki
            # Quiet when there was nothing to consolidate — a nightly job
            # must not turn the heartbeat log into a metronome.
            log.extend("Wiki: " + line for line in wiki.dream_all(now))

        try:
            _watch_job("dreaming", _job_dreaming)
        except Exception:
            log.append(_job_error("Dreaming", configured))
        state["dreaming"] = now

    if config.sleeptime_enabled() and _due(state, "sleeptime",
                                            config.SLEEPTIME_EVERY, now):
        # The unified sleep-time reflection cycle: BOTH targets (memory
        # consolidation + prompt reflection) under budget caps and the
        # reflection.cycle Plane-1 contract (Plane 3.4).
        def _job_sleeptime() -> None:
            from . import reflection
            log.extend(reflection.sleep_cycle().get("log", []))

        try:
            _watch_job("sleeptime", _job_sleeptime)
        except Exception:
            log.append(_job_error("Sleep-time reflection", configured))
        state["sleeptime"] = now

    if config.live_eval_enabled() and _due(state, "live_eval",
                                           config.LIVE_EVAL_EVERY, now):
        def _job_liveeval() -> None:
            from . import liveeval
            log.extend(liveeval.run())

        try:
            _watch_job("live_eval", _job_liveeval)
        except Exception:
            log.append(_job_error("Live-eval", configured))
        state["live_eval"] = now

    if _due(state, "daily_learning", config.DAILY_LEARNING_EVERY, now):
        log.append("Metis: running the daily learning cycle...")

        def _job_metis() -> None:
            orchestrator.daily_learning()
            log.append("Metis: skills updated; report saved to memory/reports.")

        try:
            _watch_job("daily_learning", _job_metis)
        except Exception:
            log.append(_job_error("Metis", configured))

        # Metis also prunes drifted operator site profiles (Phase 4).
        def _job_operator_review() -> None:
            from . import operator
            log.append("Operator review: " + operator.review_profiles())

        try:
            _watch_job("operator_review", _job_operator_review)
        except Exception:
            log.append(_job_error("Operator review", configured))
        state["daily_learning"] = now

    from . import discovery
    if discovery.enabled() and _due(state, "discovery", config.DISCOVERY_EVERY, now):
        log.append("Discovery: closing knowledge/capability gaps...")

        def _job_discovery() -> None:
            r = discovery.run()
            for line in r.get("learned", []) + r.get("proposed", []):
                log.append("Discovery: " + line)
            log.append(f"Discovery: {r.get('open_knowledge', 0)} knowledge / "
                       f"{r.get('open_capability', 0)} capability gap(s) open.")

        try:
            _watch_job("discovery", _job_discovery)
        except Exception:
            log.append(_job_error("Discovery", configured))
        state["discovery"] = now

    if config.TRAIN_EVERY and _due(state, "train", config.TRAIN_EVERY, now):
        log.append("Prometheus: training the weakest specialists...")

        def _job_train() -> None:
            orchestrator.train_specialists()
            log.append("Prometheus: training round saved to memory/reports.")

        try:
            _watch_job("train", _job_train)
        except Exception:
            log.append(_job_error("Training", configured))
        state["train"] = now

    from . import curator
    if _due(state, "skill_curation", curator.curation_every(), now):
        log.append("Curator: grading and pruning the skill library...")

        def _job_curator() -> None:
            log.append("Curator: " + curator.curate())

        try:
            _watch_job("skill_curation", _job_curator)
        except Exception:
            log.append(_job_error("Curator", configured))
        state["skill_curation"] = now

    from . import evolve
    if _due(state, "feature_evolution", config.FEATURE_EVOLUTION_EVERY, now):
        def _job_evolve() -> None:
            log.append(evolve.review())

        try:
            _watch_job("feature_evolution", _job_evolve)
        except Exception:
            log.append(_job_error("Feature-evolution review", configured))
        state["feature_evolution"] = now

    if _due(state, "evolution_audit", config.EVOLUTION_AUDIT_EVERY, now):
        log.append("Prometheus: running self-audit and self-upgrade...")

        def _job_audit() -> None:
            report = orchestrator.evolution_audit()
            log.append("Prometheus: audit saved to memory/reports.")
            pushed = gateway.notify_all("🔧 Olympus self-audit:\n\n" + report)
            if pushed:
                log.append(f"Prometheus: audit pushed to {', '.join(pushed)}.")

        try:
            _watch_job("evolution_audit", _job_audit)
        except Exception:
            log.append(_job_error("Prometheus", configured))
        state["evolution_audit"] = now

    if config.REPLAY_GATE_EVERY and _due(state, "replay_gate",
                                          config.REPLAY_GATE_EVERY, now):
        def _job_replay_gate() -> None:
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

        try:
            _watch_job("replay_gate", _job_replay_gate)
        except Exception:
            log.append("Replay self-check errored:\n" + traceback.format_exc())
        state["replay_gate"] = now

    if config.DRIFT_GATE_EVERY and _due(state, "drift_gate",
                                        config.DRIFT_GATE_EVERY, now):
        # Provider-drift tripwire (C6): budget-guarded like every other job, then
        # run the keyed drift corpus within its own hard dollar cap. Default OFF.
        from . import usage      # named by the BudgetExceeded handler below

        def _job_drift_gate() -> None:
            usage.check_budget()
            from . import modelgate
            res = modelgate.run_gate(config.Settings.from_env())
            log.append(f"Drift gate: {res.severity} ({len(res.results)} items, "
                       f"~${res.cost:.4f}).")

        try:
            _watch_job("drift_gate", _job_drift_gate)
        except usage.BudgetExceeded as err:
            log.append(f"Drift gate: skipped (budget) — {err}")
        except Exception:
            log.append(_job_error("Drift gate", configured))
        state["drift_gate"] = now

    if config.BACKUP_EVERY and _due(state, "backup", config.BACKUP_EVERY, now):
        def _job_backup() -> None:
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

        try:
            _watch_job("backup", _job_backup)
        except Exception:
            log.append("Backup errored:\n" + traceback.format_exc())
        state["backup"] = now

    memory.save_state(state)
    return log


def _install_shutdown(stop: "threading.Event") -> None:
    """Stop the loop on SIGTERM/SIGINT so a container stop is graceful.

    `docker stop`, systemd, and every orchestrator send SIGTERM first and
    SIGKILL after a grace period. This loop had NO handler, and it runs as PID 1
    in the shipped container — where Linux ignores any signal whose handler was
    never explicitly installed. So SIGTERM was dropped on the floor and the loop
    was SIGKILLed on every single stop (measured: exit=137). `cli.py` catches
    KeyboardInterrupt, but that only ever fires for SIGINT, so it never helped
    the container path.

    What a kill costs: an in-flight model call is abandoned after it has already
    been paid for, the cadence timestamp for the running job is never written so
    the same work re-runs next boot, and a multi-step maintenance sweep stops
    wherever it happened to be.

    Best-effort by design, exactly like `web.py::_install_shutdown`: signal
    handlers can only be installed on the main thread, and `run_forever` is also
    called from tests and embedded callers. Failing to install is not a reason
    to refuse to run — it just leaves shutdown as abrupt as it was before."""
    import signal

    def _stop(signum, _frame):
        print(f"\n… received {signal.Signals(signum).name}, "
              f"finishing this tick and stopping")
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError, AttributeError):
            pass                        # not the main thread, or not POSIX


def run_forever() -> None:
    state = memory.load_state()
    stop = threading.Event()
    _install_shutdown(stop)
    print("Olympus heartbeat started. Ctrl-C to stop.")
    print(f"  opportunity scan : every {config.OPPORTUNITY_SCAN_EVERY // 3600} h")
    print(f"  youtube watchlist: every {config.WATCHLIST_EVERY // 60} min")
    print(f"  daily learning   : every {config.DAILY_LEARNING_EVERY // 3600} h")
    if config.TRAIN_EVERY:
        print(f"  specialist train : every {config.TRAIN_EVERY // 86400} d")
    print(f"  evolution audit  : every {config.EVOLUTION_AUDIT_EVERY // 86400} d")
    if config.REPLAY_GATE_EVERY:
        print(f"  replay self-check: every {config.REPLAY_GATE_EVERY // 86400} d")
    if config.DRIFT_GATE_EVERY:
        print(f"  drift gate       : every {config.DRIFT_GATE_EVERY // 3600} h")
    if config.BACKUP_EVERY:
        dest = "off-droplet" if config.backup_command() else "local only"
        print(f"  data backup      : every {config.BACKUP_EVERY // 3600} h "
              f"({dest})")
    while not stop.is_set():
        for line in tick(state):
            print(f"[heartbeat] {line}")
        # `stop.wait()`, not `time.sleep()`: since PEP 475 a signal handler that
        # returns normally makes `time.sleep` RESUME for its remaining time, so
        # a SIGTERM arriving mid-sleep would set the flag and still be killed
        # waiting out the tick. `Event.set()` releases the waiter, so this
        # returns as soon as the handler runs.
        stop.wait(config.HEARTBEAT_TICK)
    print("Heartbeat stopped.")

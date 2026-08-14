"""W2-PR13 — the progress watchdog wired into the LIVE execution path.

Gate W2-A9 shipped as a *mechanism* only: `watchdog.py` classified leases
nobody built. This suite covers the wiring that closes it on the live path —
one `ProgressLease` per orchestrator RUN, one per `heartbeat.tick()` JOB — and
the three properties that make that wiring safe to ship:

1. **Flag-off inertness.** With `OLYMPUS_WATCHDOG` unset no lease is ever
   constructed (proved by monkeypatching `ProgressLease` to raise) and the run's
   decision path and output are byte-identical to a run without the sabotage.
2. **Observe changes nothing.** A progress-free run is classified and its
   forensics preserved, and it still completes with the same answer.
3. **Enforce cancels.** The same run is cancelled with a typed
   `WatchdogRefusal`, forensics exist, and the injected `release_fn` ran.

W2-I6.1 is exercised live: model text that no verifier accepted is NOT progress,
so a run that only produces such text while spending trips, while an otherwise
identical run whose outputs ARE accepted does not.

Determinism: no test sleeps to make time pass. The heartbeat leases run on
`heartbeat._clock`, which is replaced by a fake that only advances while a job
is deliberately wedged; the orchestrator tests drive spend through a fake
`usage.today_spend`.
"""
from __future__ import annotations

import threading

import pytest

from olympus import (backend, config, heartbeat, orchestrator, trace as
                     trace_mod, usage, watchdog)


# --- shared fixtures --------------------------------------------------------

@pytest.fixture(autouse=True)
def watchdog_off(monkeypatch):
    """Every test starts from the shipped default (off) and opts in itself."""
    monkeypatch.delenv("OLYMPUS_WATCHDOG", raising=False)
    yield


class _Sabotage:
    """A `ProgressLease` replacement that explodes if anyone constructs it."""

    def __init__(self, *a, **kw):
        raise AssertionError(
            "ProgressLease was constructed while OLYMPUS_WATCHDOG is off")


class _Contract:
    def __init__(self, ok: bool, violations=()):
        self.ok = ok
        self.violations = list(violations)


def _spend_meter(monkeypatch, *, per_call: float = 0.0, start: float = 0.0):
    """Replace `usage.today_spend` with a deterministic meter.

    The orchestrator snapshots a baseline at run start and reports the delta to
    the lease as each beat's `spend`, so this is how a test makes money move
    without a provider."""
    box = {"total": float(start)}

    def _today() -> float:
        value = box["total"]
        box["total"] += per_call
        return value

    monkeypatch.setattr(usage, "today_spend", _today)
    return box


def _bot(monkeypatch, *, output: str = "a real specialist finding",
         raises: BaseException | None = None,
         contract_ok: bool | None = None,
         review_verdict: str = "approve"):
    """A bot whose whole pipeline is faked except the seams under test.

    `_run_one` and `_dispatch_dag` are the REAL ones — they are where the lease
    is fed — so only the provider call and the surrounding model stages are
    replaced."""
    bot = orchestrator.Olympus(user="wd-tester")
    monkeypatch.setattr(bot, "_route", lambda msg: {
        "mode": "delegate", "direct_reply": None, "specialists": ["argus"],
        "brief": "do it", "needs_verification": True,
        "clarifying_questions": []})
    monkeypatch.setattr(bot, "_plan", lambda brief, keys, **kw: [
        {"id": "s1", "specialist": "argus", "task": "t", "depends_on": []}])
    monkeypatch.setattr(bot, "_verify_timed", lambda brief, outs: (
        "verified findings", {"status": "pass", "unsupported_claims": [],
                              "confidence": 0.9}))
    monkeypatch.setattr(bot, "_review", lambda brief, verified: {
        "verdict": review_verdict, "feedback": "fix it",
        "retry_specialists": ["argus"] if review_verdict == "retry" else []})
    monkeypatch.setattr(
        bot, "_enforce_answer_verify",
        lambda brief, assignments, outs, verified, verdict, err, tr: verified)

    def _fake_agent(*a, **kw):
        if raises is not None:
            raise raises
        return output, 0

    monkeypatch.setattr(backend, "run_agent_counted", _fake_agent)
    if contract_ok is not None:
        # The output contract is the VERIFIER on this seam: text it rejects is
        # model text nobody accepted (W2-I6.1), never `specialist_output_
        # accepted`.
        from olympus import contracts
        monkeypatch.setenv("OLYMPUS_CONTRACTS", "1")
        monkeypatch.setattr(
            contracts, "check",
            lambda out, contract, tool_calls=None: _Contract(
                contract_ok, [] if contract_ok else ["fabricated citation"]))
    return bot


def _run(bot, message: str = "a real user message"):
    tr = trace_mod.Trace("chat", "wd-tester")
    tr.meta = {"input": message}
    return bot._pipeline(message, tr), tr


# --- 1. flag-off byte identity ---------------------------------------------

def test_flag_off_never_constructs_a_lease_and_is_byte_identical(monkeypatch):
    """The shipped default must be indistinguishable from no wiring at all.

    Proof in two parts: `ProgressLease` is replaced by a constructor that
    raises (so a single construction fails the test outright), and the run's
    canonical decision log plus its returned answer must equal a reference run
    taken without the sabotage."""
    bot = _bot(monkeypatch)
    reference, ref_tr = _run(bot)
    ref_log = trace_mod.canonical_log(ref_tr.decisions)

    monkeypatch.setattr(watchdog, "ProgressLease", _Sabotage)
    bot2 = _bot(monkeypatch)
    sabotaged, sab_tr = _run(bot2)

    assert sabotaged == reference
    assert trace_mod.canonical_log(sab_tr.decisions) == ref_log
    assert not (config.MEMORY_DIR / "watchdog").exists()


def test_flag_off_records_the_mode_but_arms_nothing(monkeypatch):
    """`tr.meta["watchdog"]` is the flag pairing (replay reproduces it). meta is
    not part of the diffed decision path, so recording it in every mode costs
    the off path nothing."""
    monkeypatch.setattr(watchdog, "ProgressLease", _Sabotage)
    bot = _bot(monkeypatch)
    _, tr = _run(bot)
    assert tr.meta["watchdog"] == "off"
    assert bot._watch is None


def test_replay_reproduces_the_recorded_watchdog_mode():
    """Enforce can cancel a run, i.e. truncate the decision path — so the mode
    is paired into `replay_run`'s env reproduction exactly like OLYMPUS_FAST."""
    import inspect
    src = inspect.getsource(orchestrator.replay_run)
    assert 'os.environ["OLYMPUS_WATCHDOG"]' in src
    assert '_meta.get("watchdog"' in src


# --- 2. observe: classify, preserve, change nothing -------------------------

def _progress_free_bot(monkeypatch, *, spend: float = 1.0):
    """A run that spends real money and produces NOTHING a verifier accepted:
    the specialist's text is rejected by its output contract, so `_run_one`
    returns the typed 'treat this part as missing' sentinel."""
    _spend_meter(monkeypatch, per_call=spend)
    return _bot(monkeypatch, output="fluent but fabricated prose",
                contract_ok=False)


def test_observe_classifies_and_preserves_forensics_without_cancelling(
        monkeypatch):
    monkeypatch.setenv("OLYMPUS_WATCHDOG", "observe")
    bot = _progress_free_bot(monkeypatch)
    (mode, brief, verified), tr = _run(bot)

    assert mode == "delegate"            # the run COMPLETED, unchanged
    assert verified == "verified findings"
    lease = bot._watch.lease
    assert {v.kind for v in lease.warnings} == {watchdog.PROGRESS_FREE_SPEND}
    assert lease.cancelled is False and lease.refused is False
    assert lease.released is False       # observe never releases a slot
    record = watchdog.read_forensics(lease.lease_id)
    assert record and record["verdict"]["kind"] == watchdog.PROGRESS_FREE_SPEND
    assert record["outcome"] == "observed"
    assert record["operator_action"]


def test_observe_produces_the_same_answer_as_the_off_path(monkeypatch):
    """Observe is a recorder. The decision path it observes must be the one the
    off path produces."""
    off_bot = _progress_free_bot(monkeypatch)
    off_result, off_tr = _run(off_bot)

    monkeypatch.setenv("OLYMPUS_WATCHDOG", "observe")
    obs_bot = _progress_free_bot(monkeypatch)
    obs_result, obs_tr = _run(obs_bot)

    assert obs_result == off_result
    assert (trace_mod.canonical_log(obs_tr.decisions)
            == trace_mod.canonical_log(off_tr.decisions))


# --- 3. enforce: cancel, preserve, release ---------------------------------

def test_enforce_cancels_a_progress_free_run_and_releases_the_slot(monkeypatch):
    monkeypatch.setenv("OLYMPUS_WATCHDOG", "enforce")
    bot = _progress_free_bot(monkeypatch)

    with pytest.raises(watchdog.WatchdogRefusal) as excinfo:
        _run(bot)

    assert watchdog.PROGRESS_FREE_SPEND in str(excinfo.value)
    watch = bot._watch
    assert watch.lease.cancelled is True
    assert watch.lease.refused is True
    # the ladder's step 5 ran through the callable the ORCHESTRATOR injected
    assert watch.lease.released is True
    assert watch.cancel_token.cancelled() is True
    record = watchdog.read_forensics(watch.lease.lease_id)
    assert record and record["verdict"]["kind"] == watchdog.PROGRESS_FREE_SPEND
    assert record["outcome"] == "cancelled"


def test_enforce_refusal_is_reported_not_raised_at_the_ask_boundary(
        monkeypatch):
    """`ask()` turns the typed cancellation into a disclosed refusal — a
    cancelled run is never a silent truncation and never a raw traceback."""
    monkeypatch.setenv("OLYMPUS_WATCHDOG", "enforce")
    bot = _progress_free_bot(monkeypatch)
    reply = bot.ask("a real user message")
    assert "cancelled this run" in reply
    assert watchdog.PROGRESS_FREE_SPEND in reply


def test_a_refused_run_starts_no_further_specialists(monkeypatch):
    """The ladder's 'refuse unsafe continuation' step is honoured at the funnel:
    once the lease is terminal, `_run_one` raises before doing any work."""
    monkeypatch.setenv("OLYMPUS_WATCHDOG", "enforce")
    bot = _progress_free_bot(monkeypatch)
    tr = trace_mod.Trace("chat", "wd-tester")
    bot._watch = orchestrator._wd_open(tr)
    bot._watch.lease.refuse_continuation("already refused")
    calls = {"n": 0}

    def _count(*a, **kw):
        calls["n"] += 1
        return "text", 0

    monkeypatch.setattr(backend, "run_agent_counted", _count)
    with pytest.raises(watchdog.WatchdogRefusal):
        bot._run_one("argus", "t", tr)
    assert calls["n"] == 0


# --- 4. W2-I6.1 live --------------------------------------------------------

def test_model_text_alone_never_counts_as_progress_on_the_live_path(
        monkeypatch):
    """Two runs, identical spend, identical model text. The only difference is
    whether a VERIFIER accepted the output. Text alone must trip."""
    monkeypatch.setenv("OLYMPUS_WATCHDOG", "observe")

    # (a) the model produced text; the output contract rejected it.
    rejected = _progress_free_bot(monkeypatch)
    _run(rejected)
    lease = rejected._watch.lease
    assert {v.kind for v in lease.warnings} == {watchdog.PROGRESS_FREE_SPEND}
    # The preserved state AS OF the trip: money moved, nothing was accepted.
    record = watchdog.read_forensics(lease.lease_id)
    assert record["state"]["progress_count"] == 0
    assert record["state"]["non_progress_count"] >= 1
    assert record["state"]["spend_usd"] > 0
    assert record["verdict"]["forensics"]["invariant"] == "W2-I6.1"
    assert not any(e.get("progress") for e in lease.timeline
                   if e["event"] == "beat" and e["signal"] == "model_text")

    # (b) the same text and the same money, but accepted by the contract.
    _spend_meter(monkeypatch, per_call=1.0)
    accepted = _bot(monkeypatch, output="fluent but fabricated prose",
                    contract_ok=True)
    _run(accepted)
    ok_lease = accepted._watch.lease
    assert ok_lease.snapshot().spend_usd > 0     # it really did spend
    assert ok_lease.snapshot().progress_count > 0
    assert ok_lease.warnings == []               # and never tripped


def test_the_progress_signals_fed_are_only_verifier_accepted_ones(monkeypatch):
    """Every signal this wiring feeds must be in `watchdog.PROGRESS_SIGNALS`
    (or an explicit non-progress signal) — never an invented one."""
    monkeypatch.setenv("OLYMPUS_WATCHDOG", "observe")
    bot = _bot(monkeypatch, contract_ok=True)
    _run(bot)
    signals = {e["signal"] for e in bot._watch.lease.timeline
               if e["event"] == "beat"}
    assert signals <= set(watchdog.PROGRESS_SIGNALS + watchdog.NON_PROGRESS_SIGNALS)
    # the three run-path progress seams all fired
    assert {"specialist_output_accepted", "plan_node_completed",
            "response_section_verified"} <= signals


def test_rework_records_a_verify_cycle_and_a_retry(monkeypatch):
    monkeypatch.setenv("OLYMPUS_WATCHDOG", "observe")
    bot = _bot(monkeypatch, contract_ok=True, review_verdict="retry")
    _run(bot)
    state = bot._watch.lease.snapshot()
    assert state.verify_cycles == 1
    assert state.retry_counts.get("rework:argus") == 1
    assert state.call_counts                      # IDENTICAL_CALLS evidence
    assert state.longest_call_secs >= 0.0         # call_started/call_finished


# --- 5. heartbeat: a wedged job must not stall the serial tick --------------

class _FakeClock:
    """A clock that stands still except while a job is deliberately wedged.

    This is how the stall thresholds are exercised without sleeping: the wedged
    job flips `wedged`, and from then on every read of the clock jumps a full
    stall window forward, so the very next classification sees the stall."""

    def __init__(self, step: float = 1000.0):
        self.t = 1_000_000.0
        self.step = step
        self.wedged = False
        self.reads = 0

    def __call__(self) -> float:
        if self.wedged:
            self.reads += 1
            self.t += self.step
        return self.t


def _quiet_tick(monkeypatch):
    """Silence every always-on cadence so a tick exercises only the two jobs
    the test cares about."""
    from olympus import (agentbeat, gateway, goals, operator, scheduler,
                         selfupdate, webmonitor, webreflect)
    monkeypatch.setattr(selfupdate, "take_handoff", lambda: None)
    for mod in (scheduler, operator, agentbeat, webmonitor, webreflect, goals):
        monkeypatch.setattr(mod, "run_due", lambda now: [])
    monkeypatch.setattr(gateway, "notify_all", lambda *a, **kw: [])
    monkeypatch.setattr(heartbeat, "_JOB_POLL_SECS", 0.01)


_CADENCES = ("opportunity_scan", "watchlist", "maintenance", "daily_learning",
             "dreaming", "sleeptime", "live_eval", "discovery", "train",
             "skill_curation", "feature_evolution", "evolution_audit",
             "replay_gate", "drift_gate", "backup")


def _state(*due: str) -> dict:
    now = 1e9
    return {k: (0.0 if k in due else now) for k in _CADENCES}


def _wedged_tick(monkeypatch, clock, *, release_after: int | None = None):
    """A tick with two due jobs: a wedged opportunity scan, then a self-audit.

    The scan blocks until released; `release_after` (used by the observe case,
    which by contract must NOT abandon anything) lets the fake clock release it
    after N stalled reads so the tick can finish."""
    _quiet_tick(monkeypatch)
    monkeypatch.setattr(heartbeat, "_clock", clock)
    gate = threading.Event()
    ran: list[str] = []

    def _wedged() -> str:
        clock.wedged = True
        gate.wait(10)
        ran.append("scan")
        return "scan report"

    def _later(settings=None) -> str:
        ran.append("audit")
        return "audit report"

    if release_after is not None:
        real_call = clock.__call__

        def _clock_that_releases() -> float:
            value = real_call()
            if clock.reads >= release_after:
                gate.set()
            return value

        monkeypatch.setattr(heartbeat, "_clock", _clock_that_releases)
    monkeypatch.setattr(orchestrator, "opportunity_scan", _wedged)
    monkeypatch.setattr(orchestrator, "evolution_audit", _later)
    return gate, ran


def test_heartbeat_observe_records_a_wedged_job_but_does_not_skip_it(
        monkeypatch):
    """Observe changes NOTHING: the wedged job still runs to completion (and
    still holds the serial tick while it does). It is merely no longer silent."""
    monkeypatch.setenv("OLYMPUS_WATCHDOG", "observe")
    clock = _FakeClock()
    gate, ran = _wedged_tick(monkeypatch, clock, release_after=2)
    try:
        log = heartbeat.tick(_state("opportunity_scan", "evolution_audit"),
                             now=1e9)
    finally:
        gate.set()

    assert ran == ["scan", "audit"]                 # nothing was skipped
    assert not any("Argus failed" in line for line in log)
    record = watchdog.read_forensics("heartbeat:opportunity_scan")
    assert record and record["verdict"]["kind"] == watchdog.DEADLOCK
    # The lease closed NORMALLY ("ok"): observe classified and preserved the
    # evidence, and never cancelled, refused or released anything.
    assert record["outcome"] == "ok"
    assert record["verdict"]["forensics"]["idle_secs"] > 0
    assert record["operator_action"]


def test_heartbeat_enforce_abandons_a_wedged_job_and_continues_the_tick(
        monkeypatch):
    """The defect this closes: one wedged cadence used to stall every cadence
    after it. Under enforce the tick abandons it and moves on."""
    monkeypatch.setenv("OLYMPUS_WATCHDOG", "enforce")
    clock = _FakeClock()
    gate, ran = _wedged_tick(monkeypatch, clock)
    try:
        log = heartbeat.tick(_state("opportunity_scan", "evolution_audit"),
                             now=1e9)
    finally:
        gate.set()

    assert "audit" in ran                    # the tick did NOT stall
    assert "scan" not in ran                 # the wedged job was abandoned
    assert any("Argus" in line for line in log)
    assert any("Prometheus: audit saved" in line for line in log)
    record = watchdog.read_forensics("heartbeat:opportunity_scan")
    assert record and record["verdict"]["kind"] == watchdog.DEADLOCK
    assert record["outcome"] in ("cancelled", "abandoned")


def test_heartbeat_flag_off_runs_jobs_inline_with_no_lease(monkeypatch):
    monkeypatch.setattr(watchdog, "ProgressLease", _Sabotage)
    _quiet_tick(monkeypatch)
    ran: list[str] = []
    monkeypatch.setattr(orchestrator, "evolution_audit",
                        lambda settings=None: (ran.append("audit"), "r")[1])
    log = heartbeat.tick(_state("evolution_audit"), now=1e9)
    assert ran == ["audit"]
    assert any("Prometheus: audit saved" in line for line in log)
    assert not (config.MEMORY_DIR / "watchdog").exists()


def test_a_watchdog_error_inside_tick_never_breaks_the_tick(monkeypatch):
    """Fail-safe: the watchdog exploding at ANY stage — construction or
    classification — leaves the tick doing exactly what it always did."""
    monkeypatch.setenv("OLYMPUS_WATCHDOG", "enforce")
    _quiet_tick(monkeypatch)
    ran: list[str] = []
    monkeypatch.setattr(orchestrator, "evolution_audit",
                        lambda settings=None: (ran.append("audit"), "r")[1])

    monkeypatch.setattr(watchdog, "ProgressLease", _Sabotage)
    log = heartbeat.tick(_state("evolution_audit"), now=1e9)
    assert ran == ["audit"]
    assert any("Prometheus: audit saved" in line for line in log)

    def _boom(self, *a, **kw):
        raise RuntimeError("watchdog is broken")

    monkeypatch.undo()                       # restore the real ProgressLease
    monkeypatch.setenv("OLYMPUS_WATCHDOG", "enforce")
    _quiet_tick(monkeypatch)
    monkeypatch.setattr(orchestrator, "evolution_audit",
                        lambda settings=None: (ran.append("audit2"), "r")[1])
    monkeypatch.setattr(watchdog.ProgressLease, "check", _boom)
    monkeypatch.setattr(watchdog.ProgressLease, "beat", _boom)
    log = heartbeat.tick(_state("evolution_audit"), now=1e9)
    assert "audit2" in ran
    assert any("Prometheus: audit saved" in line for line in log)


# --- 6. fail-safe on the run path ------------------------------------------

def test_a_watchdog_error_never_breaks_a_run(monkeypatch):
    """A raising classifier must not cancel, degrade or crash a healthy run."""
    monkeypatch.setenv("OLYMPUS_WATCHDOG", "enforce")
    reference, ref_tr = _run(_bot(monkeypatch, contract_ok=True))

    def _boom(self, *a, **kw):
        raise RuntimeError("watchdog is broken")

    monkeypatch.setattr(watchdog.ProgressLease, "check", _boom)
    monkeypatch.setattr(watchdog.ProgressLease, "beat", _boom)
    broken, broken_tr = _run(_bot(monkeypatch, contract_ok=True))

    assert broken == reference
    assert (trace_mod.canonical_log(broken_tr.decisions)
            == trace_mod.canonical_log(ref_tr.decisions))


def test_a_failing_lease_constructor_degrades_to_no_supervision(monkeypatch):
    monkeypatch.setenv("OLYMPUS_WATCHDOG", "enforce")
    monkeypatch.setattr(watchdog, "ProgressLease", _Sabotage)
    bot = _bot(monkeypatch, contract_ok=True)
    (mode, _, verified), _ = _run(bot)
    assert mode == "delegate" and verified == "verified findings"
    assert bot._watch is None


def test_a_raising_release_fn_cannot_break_the_cancellation(monkeypatch):
    """The ladder must complete even if the seam's own release callable throws:
    forensics and the refusal are what the operator needs most."""
    monkeypatch.setenv("OLYMPUS_WATCHDOG", "enforce")
    bot = _progress_free_bot(monkeypatch)

    def _boom(self):
        raise RuntimeError("the cancel token is broken")

    monkeypatch.setattr(orchestrator._RunCancel, "cancel", _boom)
    with pytest.raises(watchdog.WatchdogRefusal):
        _run(bot)
    lease = bot._watch.lease
    assert lease.refused is True and lease.cancelled is True
    assert lease.released is True            # attempted exactly once
    assert watchdog.read_forensics(lease.lease_id)


# ══════════════════════════════════════════════════════════════════════════
# Wave-0 hardening: an absolute per-job ceiling, independent of the watchdog
#
# The watchdog is off by default (its thresholds are self-declared
# PROVISIONAL), which left the SHIPPED configuration with no protection at all:
# tick() runs every cadence serially and inline, so one provider that accepts a
# request and never streams stalls the scheduler, goals, beats and backups
# forever. `OLYMPUS_JOB_STALL_SECS` — documented in .env.example and read by
# nothing until now — is the crude floor under that.
# ══════════════════════════════════════════════════════════════════════════

def test_job_timeout_default_and_overrides(monkeypatch):
    monkeypatch.delenv("OLYMPUS_JOB_STALL_SECS", raising=False)
    assert heartbeat.job_timeout_secs() == heartbeat._DEFAULT_JOB_TIMEOUT
    monkeypatch.setenv("OLYMPUS_JOB_STALL_SECS", "45")
    assert heartbeat.job_timeout_secs() == 45.0
    for disabled in ("0", "off", "none", "never"):
        monkeypatch.setenv("OLYMPUS_JOB_STALL_SECS", disabled)
        assert heartbeat.job_timeout_secs() == 0.0
    monkeypatch.setenv("OLYMPUS_JOB_STALL_SECS", "not-a-number")
    assert heartbeat.job_timeout_secs() == heartbeat._DEFAULT_JOB_TIMEOUT


def test_a_wedged_job_is_abandoned_instead_of_stalling_forever(monkeypatch):
    """The defect itself: with the watchdog off, a job that never returns used
    to hold the tick indefinitely."""
    monkeypatch.delenv("OLYMPUS_WATCHDOG", raising=False)
    monkeypatch.setenv("OLYMPUS_JOB_STALL_SECS", "0.3")
    release = threading.Event()

    def _wedged():
        release.wait(30)          # never released within the deadline
        return "too late"

    with pytest.raises(heartbeat.JobTimedOut) as err:
        heartbeat._watch_job("wedged", _wedged)
    assert "OLYMPUS_JOB_STALL_SECS" in str(err.value)
    release.set()                 # let the abandoned worker exit


def test_the_tick_continues_past_a_wedged_cadence(monkeypatch):
    """End-to-end with the watchdog OFF (the shipped default): a wedged job is
    abandoned on the deadline and the cadences after it still run. Before this,
    the tick waited on the wedged job forever and backups never happened."""
    monkeypatch.setenv("OLYMPUS_JOB_STALL_SECS", "0.3")
    clock = _FakeClock()
    gate, ran = _wedged_tick(monkeypatch, clock)
    try:
        log = heartbeat.tick(_state("opportunity_scan", "evolution_audit"),
                             now=1e9)
    finally:
        gate.set()

    assert "audit" in ran, "the tick stalled on the wedged job"
    assert "scan" not in ran, "the wedged job was not abandoned"
    assert any("Prometheus: audit saved" in line for line in log), log
    # …and no watchdog machinery was involved in doing it.
    assert not (config.MEMORY_DIR / "watchdog").exists()


def test_disabling_the_ceiling_restores_the_inline_tick(monkeypatch):
    """`0` must mean genuinely inline — same thread, no worker hop."""
    monkeypatch.delenv("OLYMPUS_WATCHDOG", raising=False)
    monkeypatch.setenv("OLYMPUS_JOB_STALL_SECS", "off")
    where: list[int] = []
    result = heartbeat._watch_job(
        "inline", lambda: (where.append(threading.get_ident()), "value")[1])
    assert result == "value"
    assert where == [threading.get_ident()]


def test_a_normal_job_still_returns_its_value_and_raises_its_errors(monkeypatch):
    monkeypatch.delenv("OLYMPUS_WATCHDOG", raising=False)
    monkeypatch.setenv("OLYMPUS_JOB_STALL_SECS", "30")
    assert heartbeat._watch_job("ok", lambda: "answer") == "answer"

    def _boom():
        raise RuntimeError("job failed for its own reasons")

    with pytest.raises(RuntimeError, match="its own reasons"):
        heartbeat._watch_job("bad", _boom)

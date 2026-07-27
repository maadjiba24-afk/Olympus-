"""Wave-2 C6 — the progress-based watchdog (gate W2-A9).

What these prove:
 1. Each of the ten detection classes trips, reports the right kind, and names
    an OPERATOR ACTION (a verdict with no action is not actionable).
 2. W2-I6.1 — model-generated text alone is never progress: a lease fed only
    `model_text` while spending trips PROGRESS_FREE_SPEND, while a lease fed
    real progress signals with the SAME spend stays OK.
 3. The false-positive discipline: a slow-but-steadily-progressing lease over a
    long window never trips (the acceptance property for W2-A9).
 4. The action ladder: forensics preserved and complete, forensics never raises
    (even with a broken proclock), the injected release_fn called exactly once,
    refusal terminal, observe records but never cancels, off fully inert.
 5. The quarantine sink degrades gracefully when `olympus.experiments` is
    absent or broken, and records the pattern when it is present (both against
    a stub and against the real registry's retest ledger).
"""

import builtins
import json
import sys
import types
from pathlib import Path

import pytest

from olympus import config, watchdog


# --- helpers ---------------------------------------------------------------

class Clock:
    """A deterministic clock so time-based detections are exact, not flaky."""

    def __init__(self, t0: float = 1_000_000.0):
        self.t = float(t0)

    def __call__(self) -> float:
        return self.t

    def advance(self, secs: float) -> float:
        self.t += float(secs)
        return self.t


@pytest.fixture
def observe_mode(monkeypatch):
    monkeypatch.setenv("OLYMPUS_WATCHDOG", "observe")
    return watchdog.OBSERVE


@pytest.fixture
def enforce_mode(monkeypatch):
    monkeypatch.setenv("OLYMPUS_WATCHDOG", "enforce")
    return watchdog.ENFORCE


@pytest.fixture
def off_mode(monkeypatch):
    monkeypatch.delenv("OLYMPUS_WATCHDOG", raising=False)
    return watchdog.OFF


def _lease(clock, **kw):
    return watchdog.ProgressLease("lease-1", clock=clock, **kw)


def _install_stub_registry(monkeypatch, stub):
    """Put a fake `olympus.experiments` where the LAZY `from . import
    experiments` will find it — both sys.modules and the package attribute the
    import system would set."""
    import olympus
    monkeypatch.setitem(sys.modules, "olympus.experiments", stub)
    monkeypatch.setattr(olympus, "experiments", stub, raising=False)


def _assert_trip(verdict, kind):
    assert verdict.kind == kind, f"expected {kind}, got {verdict.kind}"
    assert verdict.tripped
    assert verdict.reason.strip(), "a verdict must carry the exact reason"
    assert verdict.operator_action.strip(), (
        f"{kind} verdict has no operator action — a verdict that does not name "
        "what the operator should DO is not actionable")
    assert verdict.forensics, "a trip must carry machine-readable evidence"


# --- 0. mode + progress-currency surface -----------------------------------

def test_mode_defaults_to_off(off_mode):
    assert watchdog.mode() == watchdog.OFF
    assert watchdog.enabled() is False


def test_mode_parses_observe_and_enforce(monkeypatch):
    monkeypatch.setenv("OLYMPUS_WATCHDOG", "observe")
    assert watchdog.mode() == watchdog.OBSERVE
    monkeypatch.setenv("OLYMPUS_WATCHDOG", "enforce")
    assert watchdog.mode() == watchdog.ENFORCE
    monkeypatch.setenv("OLYMPUS_WATCHDOG", "nonsense")
    assert watchdog.mode() == watchdog.OFF        # unrecognised => inert


def test_progress_currency_membership():
    """W2-I6.1's vocabulary: only verifier-accepted signals count, and unknown
    signals fail closed (not progress)."""
    assert "model_text" in watchdog.NON_PROGRESS_SIGNALS
    assert watchdog.is_progress("model_text") is False
    assert watchdog.is_progress("something_new") is False
    for sig in watchdog.PROGRESS_SIGNALS:
        assert watchdog.is_progress(sig) is True
    assert set(watchdog.PROGRESS_SIGNALS).isdisjoint(
        watchdog.NON_PROGRESS_SIGNALS)


def test_ten_detection_classes_are_all_classified():
    assert len(watchdog.DETECTION_KINDS) == 10
    assert len(watchdog.CLASSIFIERS) == 10


def test_thresholds_are_labelled_provisional_and_env_overridable(monkeypatch):
    assert watchdog.thresholds()["PROVISIONAL"] is True
    monkeypatch.setenv("OLYMPUS_WATCHDOG_STALL_SECS", "12")
    assert watchdog.stall_secs() == 12.0
    monkeypatch.setenv("OLYMPUS_WATCHDOG_STALL_SECS", "garbage")
    assert watchdog.stall_secs() == 300.0         # bad value => documented default


# --- 1. one test per detection class ---------------------------------------

def test_detect_deadlock(observe_mode):
    clock = Clock()
    lease = _lease(clock)
    lease.beat("plan_node_completed")
    clock.advance(60)
    assert lease.check().kind == watchdog.OK      # still inside the window
    clock.advance(400)
    _assert_trip(lease.check(), watchdog.DEADLOCK)


def test_detect_retry_loop(observe_mode):
    clock = Clock()
    lease = _lease(clock)
    for _ in range(watchdog.retry_max()):
        lease.record_retry("specialist:argus")
    assert lease.check().kind == watchdog.OK      # at the limit, not past it
    lease.record_retry("specialist:argus")
    verdict = lease.check()
    _assert_trip(verdict, watchdog.RETRY_LOOP)
    assert verdict.forensics["retry_key"] == "specialist:argus"


def test_detect_identical_calls(observe_mode):
    clock = Clock()
    lease = _lease(clock)
    for _ in range(watchdog.identical_max() + 1):
        lease.record_call("sha256:deadbeef")
    verdict = lease.check()
    _assert_trip(verdict, watchdog.IDENTICAL_CALLS)
    assert verdict.forensics["fingerprint"] == "sha256:deadbeef"


def test_detect_stalled_stream(observe_mode):
    clock = Clock()
    lease = _lease(clock, kind="stream", stream=True)
    lease.record_stream_delta()
    clock.advance(10)
    assert lease.check().kind == watchdog.OK
    clock.advance(watchdog.stream_stall_secs() + 5)
    _assert_trip(lease.check(), watchdog.STALLED_STREAM)


def test_detect_runaway_recursion(observe_mode):
    clock = Clock()
    lease = _lease(clock)
    lease.record_tool_depth(watchdog.max_tool_depth())
    assert lease.check().kind == watchdog.OK
    lease.record_tool_depth(watchdog.max_tool_depth() + 1)
    _assert_trip(lease.check(), watchdog.RUNAWAY_RECURSION)


def test_detect_denial_of_wallet(observe_mode):
    """Spend RATE over the ceiling — even while the run IS progressing."""
    clock = Clock()
    lease = _lease(clock)
    lease.beat("plan_node_completed", spend=2.0)
    clock.advance(60)
    lease.beat("plan_node_completed", spend=8.0)
    verdict = lease.check()
    _assert_trip(verdict, watchdog.DENIAL_OF_WALLET)
    assert verdict.forensics["rate_usd_per_s"] > watchdog.spend_rate_usd()


def test_denial_of_wallet_needs_a_window(observe_mode):
    """False-positive guard: one expensive call at t+0 is not an infinite rate."""
    clock = Clock()
    lease = _lease(clock)
    lease.beat("plan_node_completed", spend=5.0)
    clock.advance(1)
    assert lease.check().kind == watchdog.OK


def test_detect_provider_hang(observe_mode):
    clock = Clock()
    lease = _lease(clock)
    lease.call_started()
    clock.advance(watchdog.call_ceiling_secs() + 60)
    verdict = lease.check()
    _assert_trip(verdict, watchdog.PROVIDER_HANG)
    assert verdict.forensics["open_secs"] >= watchdog.call_ceiling_secs()


def test_detect_verifier_loop(observe_mode):
    clock = Clock()
    lease = _lease(clock)
    for _ in range(watchdog.verify_cycle_max() + 1):
        lease.record_verify_cycle()
    _assert_trip(lease.check(), watchdog.VERIFIER_LOOP)


def test_detect_progress_free_spend(observe_mode):
    clock = Clock()
    lease = _lease(clock)
    for _ in range(5):
        lease.beat("model_text", spend=0.1)
        clock.advance(2)
    verdict = lease.check()
    _assert_trip(verdict, watchdog.PROGRESS_FREE_SPEND)
    assert verdict.forensics["invariant"] == "W2-I6.1"


def test_detect_queue_starvation(observe_mode):
    clock = Clock()
    lease = _lease(clock, kind="queued", queued=True)
    clock.advance(watchdog.queue_wait_secs() / 2)
    assert lease.check().kind == watchdog.OK
    clock.advance(watchdog.queue_wait_secs())
    _assert_trip(lease.check(), watchdog.QUEUE_STARVATION)


def test_admitted_lease_never_starves(observe_mode):
    clock = Clock()
    lease = _lease(clock, kind="queued", queued=True)
    lease.admit()
    clock.advance(watchdog.queue_wait_secs() * 3)
    # admitted and then idle => this is a DEADLOCK, never QUEUE_STARVATION
    assert lease.check().kind == watchdog.DEADLOCK


def test_every_detection_class_is_reachable(observe_mode):
    """Belt-and-braces over the individual tests: each named kind is produced by
    some pure classifier on some synthetic state, with an operator action."""
    base = dict(lease_id="l", now=1000.0, created=0.0)
    cases = {
        watchdog.PROVIDER_HANG: dict(open_call_started=0.0),
        watchdog.DENIAL_OF_WALLET: dict(spend_usd=500.0, first_spend_at=0.0,
                                        last_beat=999.0),
        watchdog.PROGRESS_FREE_SPEND: dict(spend_usd=9.0, non_progress_count=4,
                                           last_beat=999.0),
        watchdog.RUNAWAY_RECURSION: dict(tool_depth=99, last_beat=999.0),
        watchdog.IDENTICAL_CALLS: dict(call_counts={"fp": 99}, last_beat=999.0),
        watchdog.RETRY_LOOP: dict(retry_counts={"k": 99}, last_beat=999.0),
        watchdog.VERIFIER_LOOP: dict(verify_cycles=99, last_beat=999.0),
        watchdog.STALLED_STREAM: dict(stream=True, last_beat=999.0),
        watchdog.QUEUE_STARVATION: dict(queued=True, last_beat=999.0),
        watchdog.DEADLOCK: dict(last_beat=0.0),
    }
    seen = set()
    for kind, extra in cases.items():
        state = watchdog.LeaseState(**base, **extra)
        verdict = watchdog.classify(state)
        _assert_trip(verdict, kind)
        seen.add(verdict.kind)
    assert seen == set(watchdog.DETECTION_KINDS)


def test_classifiers_are_pure(observe_mode):
    """A classifier must not mutate the state it reads nor touch MEMORY_DIR."""
    state = watchdog.LeaseState(lease_id="l", now=1000.0, created=0.0,
                                spend_usd=9.0, non_progress_count=3)
    before = json.dumps(state.to_dict(), sort_keys=True)
    for fn in watchdog.CLASSIFIERS:
        fn(state)
    assert json.dumps(state.to_dict(), sort_keys=True) == before
    assert not (config.MEMORY_DIR / "watchdog").exists()


# --- 2. W2-I6.1 ------------------------------------------------------------

def test_w2_i6_1_model_text_only_plus_spend_trips_progress_free_spend(
        observe_mode):
    clock = Clock()
    lease = _lease(clock)
    for _ in range(4):
        assert lease.beat("model_text", spend=0.2) is False   # never progress
        clock.advance(3)
    verdict = lease.check()
    _assert_trip(verdict, watchdog.PROGRESS_FREE_SPEND)
    assert verdict.forensics["progress_count"] == 0
    assert verdict.forensics["non_progress_count"] == 4
    assert "W2-I6.1" in verdict.reason or "not progress" in verdict.reason


def test_w2_i6_1_real_progress_with_equal_spend_does_not_trip(observe_mode):
    """Same money, same wall-clock, same beat count — but the signals are the
    verifiable ones, so the lease is healthy."""
    clock = Clock()
    lease = _lease(clock)
    for sig in ("plan_node_completed", "tool_result_validated",
                "specialist_output_accepted", "response_section_verified"):
        assert lease.beat(sig, spend=0.2) is True
        clock.advance(3)
    verdict = lease.check()
    assert verdict.kind == watchdog.OK, verdict.reason
    assert lease.snapshot().spend_usd == pytest.approx(0.8)
    assert lease.snapshot().progress_count == 4


def test_unknown_signal_is_not_progress(observe_mode):
    clock = Clock()
    lease = _lease(clock)
    for _ in range(4):
        assert lease.beat("looks_like_progress", spend=0.2) is False
        clock.advance(3)
    _assert_trip(lease.check(), watchdog.PROGRESS_FREE_SPEND)


# --- 3. false-positive discipline ------------------------------------------

def test_slow_but_progressing_lease_never_trips(observe_mode):
    """The W2-A9 acceptance property: half an hour of slow, real progress with
    real spend stays OK at every single check."""
    clock = Clock()
    lease = _lease(clock)
    for i in range(30):
        lease.beat("plan_node_completed", spend=0.01)
        clock.advance(60)                     # a node every minute for 30 min
        verdict = lease.check()
        assert verdict.kind == watchdog.OK, (
            f"false positive at minute {i}: {verdict.kind} — {verdict.reason}")
    assert lease.cancelled is False
    assert lease.continue_allowed() is True
    assert watchdog.read_forensics(lease.lease_id) is None
    assert not watchdog.forensics_dir().exists()


def test_a_stream_delivering_deltas_never_trips(observe_mode):
    clock = Clock()
    lease = _lease(clock, kind="stream", stream=True)
    for _ in range(50):
        lease.record_stream_delta()
        lease.beat("tokens_generated_bounded", spend=0.0005)
        clock.advance(20)                     # slow, but always delivering
        assert lease.check().kind == watchdog.OK


# --- 4. the action ladder ---------------------------------------------------

def test_forensics_written_and_complete(enforce_mode):
    clock = Clock()
    lease = _lease(clock)
    lease.beat("model_text", spend=0.3)
    clock.advance(5)
    verdict = lease.check()
    _assert_trip(verdict, watchdog.PROGRESS_FREE_SPEND)

    path = watchdog.forensics_path(lease.lease_id)
    assert path.exists()
    assert path.parent == config.MEMORY_DIR / "watchdog" / "forensics"
    rec = json.loads(path.read_text(encoding="utf-8"))
    assert rec["lease_id"] == lease.lease_id
    assert rec["verdict"]["kind"] == watchdog.PROGRESS_FREE_SPEND
    assert rec["reason"] == verdict.reason
    assert rec["operator_action"] == verdict.operator_action
    assert rec["spend_usd"] == pytest.approx(0.3)
    assert rec["progress_signals_accepted"] == 0
    assert rec["mode"] == watchdog.ENFORCE
    assert rec["thresholds"]["PROVISIONAL"] is True
    assert rec["state"]["non_progress_count"] == 1
    events = [e["event"] for e in rec["timeline"]]
    assert "beat" in events and "warn" in events and "cancel" in events
    assert any(e.get("signal") == "model_text" for e in rec["last_signals"])
    assert watchdog.read_forensics(lease.lease_id) == rec


def test_forensics_records_the_close_outcome(enforce_mode):
    clock = Clock()
    lease = _lease(clock)
    lease.beat("model_text", spend=0.5)
    lease.check()
    lease.close("aborted")
    rec = watchdog.read_forensics(lease.lease_id)
    assert rec["outcome"] == "aborted"
    assert rec["state"]["closed"] is True


def test_forensics_never_raises_with_a_broken_proclock(enforce_mode,
                                                       monkeypatch):
    from olympus import proclock

    def wedged(*a, **kw):
        raise TimeoutError("proclock 'watchdog-forensics' not acquired")

    monkeypatch.setattr(proclock, "lock", wedged)
    clock = Clock()
    lease = _lease(clock)
    lease.beat("model_text", spend=0.9)
    verdict = lease.check()               # must not raise
    _assert_trip(verdict, watchdog.PROGRESS_FREE_SPEND)
    assert lease.cancelled is True        # the ladder still ran to the end
    assert lease.refused is True


def test_forensics_returns_none_when_it_cannot_write_at_all(enforce_mode,
                                                            monkeypatch):
    from olympus import proclock

    def wedged(*a, **kw):
        raise TimeoutError("wedged")

    def no_write(*a, **kw):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(proclock, "lock", wedged)
    monkeypatch.setattr(Path, "write_text", no_write)
    state = watchdog.LeaseState(lease_id="l", now=1.0, created=0.0)
    assert watchdog.write_forensics(state, watchdog.Verdict(
        watchdog.DEADLOCK, reason="r", operator_action="a")) is None


def test_release_fn_called_exactly_once_on_cancel(enforce_mode):
    calls = []
    clock = Clock()
    lease = watchdog.ProgressLease("lease-slot", clock=clock,
                                   release_fn=lambda: calls.append(1))
    clock.advance(watchdog.stall_secs() + 10)
    _assert_trip(lease.check(), watchdog.DEADLOCK)
    assert calls == [1]
    lease.check()
    lease.check()
    lease.close("cancelled")
    assert calls == [1], "the admission slot must be released exactly once"
    assert lease.released is True


def test_release_fn_failure_does_not_break_the_ladder(enforce_mode):
    def boom():
        raise RuntimeError("slot already gone")

    clock = Clock()
    lease = watchdog.ProgressLease("lease-boom", clock=clock, release_fn=boom)
    clock.advance(watchdog.stall_secs() + 10)
    _assert_trip(lease.check(), watchdog.DEADLOCK)
    assert lease.refused is True
    assert any(e["event"] == "slot_release_failed" for e in lease.timeline)


def test_healthy_close_does_not_release_the_slot(enforce_mode):
    calls = []
    clock = Clock()
    lease = watchdog.ProgressLease("lease-ok", clock=clock,
                                   release_fn=lambda: calls.append(1))
    lease.beat("plan_node_completed")
    assert lease.check().kind == watchdog.OK
    lease.close("ok")
    assert calls == []          # normal teardown belongs to whoever acquired it


def test_refuse_continuation_is_terminal(enforce_mode):
    clock = Clock()
    lease = _lease(clock)
    lease.beat("plan_node_completed")
    verdict = lease.refuse_continuation("operator stopped the run")
    assert verdict.kind == watchdog.REFUSED
    assert verdict.operator_action.strip()
    assert lease.refused is True
    assert lease.continue_allowed() is False
    # terminal: beats are rejected and the cursor cannot advance again
    assert lease.beat("plan_node_completed") is False
    assert lease.snapshot().progress_count == 1
    # terminal: the verdict is frozen, whatever the lease's state now looks like
    clock.advance(watchdog.stall_secs() * 10)
    assert lease.check() is verdict
    assert lease.refuse_continuation("something else") is verdict
    with pytest.raises(watchdog.WatchdogRefusal):
        lease.raise_if_terminal()


def test_enforce_runs_the_whole_ladder(enforce_mode):
    calls = []
    clock = Clock()
    lease = watchdog.ProgressLease("lease-ladder", clock=clock,
                                   release_fn=lambda: calls.append(1),
                                   warn_fn=lambda v: calls.append(v.kind))
    for _ in range(watchdog.identical_max() + 1):
        lease.record_call("sha256:same")
    verdict = lease.check()
    _assert_trip(verdict, watchdog.IDENTICAL_CALLS)
    assert calls == [watchdog.IDENTICAL_CALLS, 1]      # warn, then release
    assert lease.cancelled is True and lease.refused is True
    assert lease.continue_allowed() is False
    assert watchdog.read_forensics(lease.lease_id)["verdict"]["kind"] == \
        watchdog.IDENTICAL_CALLS
    assert lease.quarantined is True        # the pattern reached the registry
    assert any(e["event"] == "quarantine" for e in lease.timeline)


def test_observe_records_but_never_cancels(observe_mode):
    calls = []
    clock = Clock()
    lease = watchdog.ProgressLease("lease-observe", clock=clock,
                                   release_fn=lambda: calls.append(1))
    clock.advance(watchdog.stall_secs() + 10)
    verdict = lease.check()
    _assert_trip(verdict, watchdog.DEADLOCK)
    assert lease.cancelled is False
    assert lease.refused is False
    assert lease.continue_allowed() is True
    assert calls == [], "observe must never release an admission slot"
    rec = watchdog.read_forensics(lease.lease_id)
    assert rec is not None and rec["outcome"] == "observed"
    assert rec["verdict"]["kind"] == watchdog.DEADLOCK
    # and it keeps classifying (not terminal)
    assert lease.check().kind == watchdog.DEADLOCK
    assert lease.beat("plan_node_completed") is True


def test_off_mode_is_fully_inert(off_mode):
    calls = []
    clock = Clock()
    lease = watchdog.ProgressLease("lease-off", clock=clock,
                                   release_fn=lambda: calls.append(1),
                                   warn_fn=lambda v: calls.append(v.kind))
    lease.beat("model_text", spend=50.0)
    clock.advance(watchdog.stall_secs() * 10)
    verdict = lease.check()
    assert verdict.kind == watchdog.OK
    assert "off" in verdict.reason
    # ...even though this lease is genuinely pathological
    assert watchdog.classify(lease.snapshot()).tripped
    assert lease.cancelled is False and lease.refused is False
    assert lease.warnings == [] and calls == []
    lease.close("ok")
    assert not (config.MEMORY_DIR / "watchdog").exists(), \
        "off mode must not write anything"


# --- 5. the quarantine sink (lazy, optional) --------------------------------

def test_quarantine_degrades_when_experiments_is_absent(enforce_mode,
                                                        monkeypatch):
    """`olympus.experiments` is an OPTIONAL dependency: on a tree where it is
    missing (rollback, older checkout) the sink reports False and the ladder
    still completes. Simulated by making its import raise ModuleNotFoundError,
    which is what an absent module does."""
    real_import = builtins.__import__

    def absent(name, globals=None, locals=None, fromlist=(), level=0):
        if "experiments" in (name or "") or "experiments" in (fromlist or ()):
            raise ModuleNotFoundError("No module named 'olympus.experiments'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", absent)
    clock = Clock()
    lease = watchdog.ProgressLease("lease-noexp", clock=clock)
    clock.advance(watchdog.stall_secs() + 10)
    verdict = lease.check()                  # must not raise
    _assert_trip(verdict, watchdog.DEADLOCK)
    assert lease.quarantined is False
    assert lease.cancelled is True and lease.refused is True


def test_quarantine_degrades_when_the_import_raises(enforce_mode, monkeypatch):
    real_import = builtins.__import__

    def exploding(name, globals=None, locals=None, fromlist=(), level=0):
        if "experiments" in (name or "") or "experiments" in (fromlist or ()):
            raise ImportError("boom: experiments is broken")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", exploding)
    state = watchdog.LeaseState(lease_id="l", now=1.0, created=0.0)
    assert watchdog.quarantine(
        state, watchdog.Verdict(watchdog.RETRY_LOOP, reason="r",
                                operator_action="a")) is False


def test_quarantine_records_into_a_stub_registry(enforce_mode, monkeypatch):
    """A registry exposing a dedicated `quarantine()` is used directly."""
    recorded = []
    stub = types.ModuleType("olympus.experiments")

    def quarantine(pattern, *, reason="", evidence=None):
        recorded.append((pattern, reason, evidence))

    stub.quarantine = quarantine
    _install_stub_registry(monkeypatch, stub)

    clock = Clock()
    lease = watchdog.ProgressLease("lease-quar", clock=clock)
    for _ in range(watchdog.retry_max() + 1):
        lease.record_retry("tool:web_fetch")
    verdict = lease.check()
    _assert_trip(verdict, watchdog.RETRY_LOOP)
    assert lease.quarantined is True
    assert len(recorded) == 1
    pattern, reason, evidence = recorded[0]
    assert pattern == f"watchdog:{watchdog.RETRY_LOOP}"
    assert reason == verdict.reason
    payload = json.loads(evidence)
    assert payload["lease_id"] == "lease-quar"
    assert payload["verdict"]["operator_action"] == verdict.operator_action


def test_quarantine_records_into_the_real_registry(enforce_mode):
    """With the real `experiments` registry present, the pattern lands in its
    append-only retest ledger against this module's registered entry."""
    from olympus import experiments

    assert experiments.EXPERIMENTAL_FLAGS["OLYMPUS_WATCHDOG"] == \
        watchdog.EXPERIMENT_ID
    clock = Clock()
    lease = watchdog.ProgressLease("lease-real", clock=clock)
    lease.record_tool_depth(watchdog.max_tool_depth() + 5)
    verdict = lease.check()
    _assert_trip(verdict, watchdog.RUNAWAY_RECURSION)
    assert lease.quarantined is True
    rows = experiments.state_records(watchdog.EXPERIMENT_ID)
    assert len(rows) == 1
    assert watchdog.RUNAWAY_RECURSION in rows[0]["outcome"]
    assert json.loads(rows[0]["evidence"])["lease_id"] == "lease-real"


def test_quarantine_survives_a_raising_sink(enforce_mode, monkeypatch):
    stub = types.ModuleType("olympus.experiments")

    def quarantine(pattern, *, reason="", evidence=None):
        raise RuntimeError("registry unavailable")

    stub.quarantine = quarantine
    _install_stub_registry(monkeypatch, stub)
    state = watchdog.LeaseState(lease_id="l", now=1.0, created=0.0)
    assert watchdog.quarantine(
        state, watchdog.Verdict(watchdog.DEADLOCK, reason="r",
                                operator_action="a")) is False


def test_quarantine_ignores_a_sink_without_the_function(enforce_mode,
                                                        monkeypatch):
    stub = types.ModuleType("olympus.experiments")
    _install_stub_registry(monkeypatch, stub)
    state = watchdog.LeaseState(lease_id="l", now=1.0, created=0.0)
    assert watchdog.quarantine(
        state, watchdog.Verdict(watchdog.DEADLOCK, reason="r",
                                operator_action="a")) is False


# --- 6. the module ships inert ---------------------------------------------

def test_watchdog_is_wired_only_at_the_sanctioned_seams():
    """The wiring PR (W2-PR13) flipped this test, as its previous docstring
    anticipated. The invariant is no longer 'nothing imports watchdog' — that
    only encoded 'not built yet'. What must hold now is narrower and permanent:
    watchdog may be consulted ONLY from the two seams the spec sanctions
    (`orchestrator` for run leases, `heartbeat` for job leases), so a future
    change cannot quietly spread lease logic across the codebase."""
    pkg = Path(config.__file__).parent
    importers = sorted(
        p.name for p in pkg.glob("*.py")
        if p.name != "watchdog.py"
        and ("import watchdog" in p.read_text(encoding="utf-8")
             or "from .watchdog" in p.read_text(encoding="utf-8")))
    assert set(importers) <= {"orchestrator.py", "heartbeat.py"}, (
        f"watchdog consulted from an unsanctioned module: {importers}")


def test_watchdog_ships_disabled_so_the_wiring_is_inert():
    """Wiring is not activation: the shipped default must still be off, which is
    what makes rollback to Wave-1 behaviour a no-op (gate W2-A17)."""
    assert watchdog.mode() == "off"
    assert watchdog.status()["enabled"] is False

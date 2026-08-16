"""W1-4: the heartbeat loop stops on SIGTERM instead of being SIGKILLed.

`run_forever()` was a bare `while True:` with no signal handling. The
KeyboardInterrupt catch lives in `cli.py` and only ever fires for SIGINT, so the
container path was never covered: as PID 1 — where Linux ignores any signal
whose handler was never explicitly installed — SIGTERM was dropped and Docker
SIGKILLed the loop after the grace period, on every stop (measured exit=137).

A compose-level `stop_signal: SIGINT` also hid the symptom, but only under
compose; systemd, Kubernetes and plain `docker run` all send SIGTERM. These
tests pin the fix in the application, where every runtime benefits.
"""

from __future__ import annotations

import signal
import threading

import pytest

from olympus import heartbeat


def test_install_shutdown_registers_sigterm_and_sigint(monkeypatch):
    """Both signals must be claimed. SIGTERM is the one that was missing, and
    it is the one every orchestrator actually sends."""
    registered: dict = {}

    def fake_signal(sig, handler):
        registered[sig] = handler

    monkeypatch.setattr(signal, "signal", fake_signal)
    stop = threading.Event()
    heartbeat._install_shutdown(stop)

    assert signal.SIGTERM in registered, (
        "no SIGTERM handler installed — as PID 1 the signal is ignored and the "
        "loop is SIGKILLed after the grace period")
    assert signal.SIGINT in registered


def test_handler_sets_the_stop_event(monkeypatch):
    """The handler must flip the flag the loop waits on."""
    registered: dict = {}
    monkeypatch.setattr(signal, "signal",
                        lambda s, h: registered.__setitem__(s, h))
    stop = threading.Event()
    heartbeat._install_shutdown(stop)
    assert not stop.is_set()

    registered[signal.SIGTERM](signal.SIGTERM, None)
    assert stop.is_set(), "SIGTERM handler did not request shutdown"


def test_install_is_best_effort_off_the_main_thread(monkeypatch):
    """Handlers install only on the main thread, and `run_forever` is also
    called from tests and embedded callers. Failing to install must not raise —
    it just leaves shutdown as abrupt as it was before (same contract as
    `web.py::_install_shutdown`)."""
    def raising(sig, handler):
        raise ValueError("signal only works in main thread")

    monkeypatch.setattr(signal, "signal", raising)
    heartbeat._install_shutdown(threading.Event())   # must not raise


@pytest.mark.parametrize("err", [ValueError, OSError, AttributeError])
def test_install_swallows_each_documented_failure(monkeypatch, err):
    def raising(sig, handler):
        raise err("nope")

    monkeypatch.setattr(signal, "signal", raising)
    heartbeat._install_shutdown(threading.Event())   # must not raise


def test_run_forever_exits_when_the_event_is_set(monkeypatch):
    """The loop must actually terminate on the flag rather than spin forever."""
    monkeypatch.setattr(heartbeat.memory, "load_state", lambda: {})
    ticks = {"n": 0}

    def fake_tick(state):
        ticks["n"] += 1
        return []

    monkeypatch.setattr(heartbeat, "tick", fake_tick)

    # Stop on the first sleep, so exactly one tick runs and the loop exits.
    def install(stop):
        original = stop.wait

        def wait(timeout=None):
            stop.set()
            return original(0)

        stop.wait = wait

    monkeypatch.setattr(heartbeat, "_install_shutdown", install)
    heartbeat.run_forever()
    assert ticks["n"] == 1, f"loop did not exit cleanly (ran {ticks['n']} ticks)"


def test_loop_waits_on_the_event_not_time_sleep():
    """`time.sleep` would be WRONG here: since PEP 475 a signal handler that
    returns normally makes it RESUME for the remaining time, so a SIGTERM
    arriving mid-tick would set the flag and still be killed waiting out the
    sleep. The loop must wait on the Event, which `set()` releases immediately."""
    import inspect
    src = inspect.getsource(heartbeat.run_forever)
    body = src[src.index("while not stop.is_set()"):]
    # Strip comments: the loop carries a comment EXPLAINING why time.sleep is
    # wrong here, and a naive substring check matches its own rationale.
    code = "\n".join(ln.split("#", 1)[0] for ln in body.splitlines())
    assert "stop.wait(" in code, "the loop must wait on the stop Event"
    assert "time.sleep(" not in code, (
        "time.sleep resumes after a signal handler returns (PEP 475), so a "
        "SIGTERM mid-tick would still be killed waiting out the sleep")

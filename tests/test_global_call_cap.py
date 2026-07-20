"""Machine-global model-call cap (M3 / closes DEFERRED #9).

`config.MAX_CONCURRENT_CALLS` used to bound only ONE process's concurrent model
calls (a `threading.BoundedSemaphore`). But the heartbeat runs as its own OS
process sharing MEMORY_DIR with the web/CLI process(es), so two processes could
jointly exceed the cap and trigger a provider rate-limit storm. `usage.slot()`
now also acquires a cross-process counting semaphore (`proclock.slot`) keyed in
MEMORY_DIR, so the combined in-flight across every process AND thread on the
machine can never exceed the cap.

Acceptance:
- (a) TWO real OS processes, each launching N threads that acquire `usage.slot()`
  and bump a shared cross-process instrumented counter; the max combined
  in-flight observed by either process is asserted `<= cap` (safety) and `== cap`
  (proves the global cap is actually reached, i.e. it is not a silent no-op).
- (b) on non-fcntl platforms the cross-process half degrades to a no-op with a
  one-time logged warning — only the per-process cap applies (DEFERRED #10 stays).
"""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


# A standalone worker: N threads each acquire usage.slot(), then — under a
# SEPARATE proclock — bump a shared cross-process counter file and record the
# running max. The counter reflects live usage.slot() holders across BOTH
# processes, so its max is the machine-global in-flight peak.
_WORKER = textwrap.dedent(
    """
    import json, os, sys, threading, time
    from olympus import usage, proclock

    counter_path = os.environ["M3_COUNTER"]
    nthreads = int(os.environ["M3_THREADS"])
    hold = float(os.environ["M3_HOLD"])

    def bump(delta):
        # Cross-process critical section over the shared counter file.
        with proclock.lock("m3-counter"):
            try:
                st = json.loads(open(counter_path).read())
            except (FileNotFoundError, ValueError):
                st = {"cur": 0, "max": 0}
            st["cur"] += delta
            if st["cur"] > st["max"]:
                st["max"] = st["cur"]
            open(counter_path, "w").write(json.dumps(st))

    def work():
        for _ in range(3):
            with usage.slot():
                bump(1)
                time.sleep(hold)
                bump(-1)

    ts = [threading.Thread(target=work) for _ in range(nthreads)]
    for t in ts: t.start()
    for t in ts: t.join()
    """
)


@pytest.mark.timeout(120)
def test_two_processes_never_exceed_global_cap(tmp_path):
    cap = 2
    counter = tmp_path / "counter.json"
    env = dict(os.environ)
    env["OLYMPUS_MEMORY_DIR"] = str(tmp_path / "mem")
    env["OLYMPUS_MAX_CONCURRENT_CALLS"] = str(cap)
    env["M3_COUNTER"] = str(counter)
    env["M3_THREADS"] = "4"        # 2 procs x 4 threads = 8 contenders, cap 2
    env["M3_HOLD"] = "0.03"

    procs = [
        subprocess.Popen([sys.executable, "-c", _WORKER],
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(2)
    ]
    for p in procs:
        out, err = p.communicate(timeout=90)
        assert p.returncode == 0, f"worker failed:\n{out.decode()}\n{err.decode()}"

    st = json.loads(counter.read_text())
    # Safety: the machine-global peak never exceeds the cap.
    assert st["max"] <= cap, f"combined in-flight {st['max']} exceeded cap {cap}"
    # Liveness: with 8 contenders and a real hold, the cap IS reached — proving
    # this is a genuine global cap, not a no-op that never blocks.
    assert st["max"] == cap, f"cap {cap} never reached (max={st['max']})"


def test_nonfcntl_degrades_to_per_process_with_warning(monkeypatch, caplog):
    """When fcntl is unavailable (Windows), proclock.slot degrades to a no-op
    and warns once via errors.capture — the per-process semaphore still caps.
    DEFERRED #10 (Windows cross-process) stays open by design."""
    import logging

    from olympus import proclock

    monkeypatch.setattr(proclock, "fcntl", None)
    monkeypatch.setattr(proclock, "_WARNED", False)

    captured = {}
    from olympus import errors

    def _cap(component, exc, context=None):
        captured["msg"] = str(exc)

    monkeypatch.setattr(errors, "capture", _cap)

    # The context manager must still work (no-op body) and warn once.
    with proclock.slot("model-call", 2):
        pass
    with proclock.slot("model-call", 2):
        pass

    assert "fcntl unavailable" in captured.get("msg", "")
    assert "per-process" in captured["msg"]

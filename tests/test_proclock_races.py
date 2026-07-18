"""ADR 0005 Phase 2: cross-PROCESS safety on shared mutable state.

The races these tests exercise are the real deployment topology — the
heartbeat runs as its own OS process against the same MEMORY_DIR as the
web/CLI process — so the contention tests spawn actual subprocesses (not
threads: a threading.Lock would mask exactly the bug being fixed).
"""

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from olympus import config, memory, proclock, sandbox


# --- proclock unit behavior ----------------------------------------------

def test_lock_is_reentrant_within_a_thread():
    done = []

    def work():
        with proclock.lock("reentrant-test"):
            with proclock.lock("reentrant-test"):     # must not self-deadlock
                done.append(True)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout=10)
    assert done == [True]


def test_lock_excludes_across_threads():
    """Two threads in one process must serialize: their critical sections
    never overlap."""
    inside = []
    overlap = []

    def work(tag):
        with proclock.lock("thread-excl-test"):
            inside.append(tag)
            if len(inside) > 1:
                overlap.append(tuple(inside))
            time.sleep(0.05)
            inside.remove(tag)

    ts = [threading.Thread(target=work, args=(i,), daemon=True)
          for i in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=10)
    assert overlap == []


def test_lock_degrades_without_fcntl(monkeypatch):
    monkeypatch.setattr(proclock, "fcntl", None)
    monkeypatch.setattr(proclock, "_WARNED", True)   # silence the capture
    with proclock.lock("degraded-test"):
        with proclock.lock("degraded-test"):
            pass                                     # still reentrant, no crash


def test_lock_name_is_sanitized():
    with proclock.lock("../../evil name!"):
        pass
    names = [p.name for p in (config.MEMORY_DIR / "locks").glob("*.lock")]
    assert all("/" not in n and " " not in n for n in names)


# --- subprocess race harness ----------------------------------------------

def _run_workers(tmp_path, script: str, n_workers: int = 2, timeout: int = 90):
    """Run `script` in n_workers real child processes sharing this test's
    MEMORY_DIR (passed via env — config reads OLYMPUS_MEMORY_DIR at import).
    A 'go' file releases all workers at once to maximize contention."""
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["OLYMPUS_MEMORY_DIR"] = str(config.MEMORY_DIR)
    env.pop("OLYMPUS_STORE", None)
    path = tmp_path / "worker.py"
    path.write_text(textwrap.dedent(script), encoding="utf-8")
    procs = [subprocess.Popen([sys.executable, str(path), str(i)],
                              env=env, cwd=str(Path(__file__).parent.parent),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
             for i in range(n_workers)]
    time.sleep(0.5)                       # let children import + reach the gate
    (config.MEMORY_DIR / "go").write_text("go")
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=timeout)
        assert p.returncode == 0, f"worker failed: {err.decode()[:2000]}"
        outs.append(out.decode())
    return outs


_GATE = """
    import os, sys, time
    from pathlib import Path
    gate = Path(os.environ["OLYMPUS_MEMORY_DIR"]) / "go"
    for _ in range(600):
        if gate.exists():
            break
        time.sleep(0.05)
    else:
        sys.exit(3)
"""


@pytest.mark.skipif(not hasattr(proclock, "fcntl") or proclock.fcntl is None,
                    reason="flock requires POSIX")
def test_two_processes_never_lose_a_ledger_update(tmp_path):
    """THE lost-update race from the audit: two processes each record N model
    calls; the per-day ledger must show exactly 2N calls."""
    n = 30
    _run_workers(tmp_path, f"""
    {_GATE}
    from olympus import usage
    for _ in range({n}):
        usage.record("race-model", 10, 5)
    """)
    day = time.strftime("%Y-%m-%d")
    ledger = json.loads(
        (config.MEMORY_DIR / "usage" / f"{day}.json").read_text())
    assert ledger["__all__"]["calls"] == 2 * n
    assert ledger["__all__"]["in"] == 2 * n * 10


@pytest.mark.skipif(not hasattr(proclock, "fcntl") or proclock.fcntl is None,
                    reason="flock requires POSIX")
def test_concurrent_same_title_saves_preserve_every_note(tmp_path):
    """memory.save's old second-granularity filename let same-title writers
    silently overwrite each other; O_EXCL + pid must preserve all of them."""
    n = 20
    _run_workers(tmp_path, f"""
    {_GATE}
    from olympus import memory
    memory.set_user("shared")
    for i in range({n}):
        memory.save("reports", "identical title", f"body from pid %d #%d"
                    % (__import__("os").getpid(), i))
    """)
    d = config.MEMORY_DIR / "reports"
    files = list(d.glob("*.md"))
    assert len(files) == 2 * n            # nothing overwritten


@pytest.mark.skipif(not hasattr(proclock, "fcntl") or proclock.fcntl is None,
                    reason="flock requires POSIX")
def test_watchlist_pop_under_contention_loses_nothing(tmp_path):
    """Two processes drain the watchlist concurrently: every entry is popped
    exactly once — no losses, no duplicates."""
    total = 40
    for i in range(total):
        memory.watchlist_add(f"https://example.test/{i}")
    _run_workers(tmp_path, _GATE + """
    import os
    from pathlib import Path
    from olympus import memory
    out = Path(os.environ["OLYMPUS_MEMORY_DIR"]) / f"popped-{os.getpid()}"
    got = []
    while True:
        url = memory.watchlist_pop()
        if url is None:
            break
        got.append(url)
    out.write_text("\\n".join(got))
    """)
    popped = []
    for f in config.MEMORY_DIR.glob("popped-*"):
        popped += [l for l in f.read_text().splitlines() if l.strip()]
    assert len(popped) == total                      # nothing lost
    assert len(set(popped)) == total                 # nothing popped twice
    assert memory.watchlist_pop() is None            # and the list is empty


# --- single-process same-second collision (the cheap regression) ----------

def test_same_second_same_title_saves_both_survive():
    memory.set_user("shared")
    p1 = memory.save("reports", "moa trace", "first body")
    p2 = memory.save("reports", "moa trace", "second body")
    assert p1 != p2
    assert p1.exists() and p2.exists()
    assert "first body" in p1.read_text()
    assert "second body" in p2.read_text()


def test_note_filename_keeps_datestamp_prefix():
    memory.set_user("shared")
    p = memory.save("reports", "prefix check", "body")
    import re
    digits = re.sub(r"\D", "", p.stem)[:8]
    assert digits == time.strftime("%Y%m%d")   # date-parsing readers unbroken


# --- per-worker sandbox scratch -------------------------------------------

def test_worker_scratch_isolates_specialist_roots(monkeypatch):
    monkeypatch.delenv("OLYMPUS_WORKER_SCRATCH", raising=False)
    t1 = sandbox.set_scratch("hephaestus")
    try:
        d1 = sandbox.workdir()
    finally:
        sandbox.reset_scratch(t1)
    t2 = sandbox.set_scratch("hermes")
    try:
        d2 = sandbox.workdir()
    finally:
        sandbox.reset_scratch(t2)
    assert d1 != d2
    assert d1.name == "hephaestus" and d1.parent.name == "agents"
    assert sandbox.workdir().name == "workspace"   # no scratch → shared root


def test_worker_scratch_kill_switch(monkeypatch):
    monkeypatch.setenv("OLYMPUS_WORKER_SCRATCH", "off")
    token = sandbox.set_scratch("hephaestus")
    try:
        assert sandbox.workdir().name == "workspace"
    finally:
        sandbox.reset_scratch(token)


def test_worker_scratch_still_confined(monkeypatch):
    monkeypatch.delenv("OLYMPUS_WORKER_SCRATCH", raising=False)
    token = sandbox.set_scratch("hermes")
    try:
        with pytest.raises(ValueError):
            sandbox._confine("../../../etc/passwd")
    finally:
        sandbox.reset_scratch(token)

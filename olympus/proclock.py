"""Cross-process mutual exclusion for read-modify-write on shared files.

ADR 0005 decision (b): every `threading.Lock` in the tree serializes only one
process, but the heartbeat runs as its own OS process sharing MEMORY_DIR with
the web/CLI process(es). This module is the missing half: an
`fcntl.flock(LOCK_EX)` on `MEMORY_DIR/locks/<name>.lock`.

Properties:
- **Cross-process**: two processes contending on the same name serialize.
- **Cross-thread**: each acquisition opens its own file description, and
  flock between distinct descriptions blocks — so threads serialize too.
- **Reentrant per thread**: a thread re-entering the same name bumps a
  thread-local depth counter instead of flocking a second description (which
  would self-deadlock).
- **POSIX only**: on platforms without `fcntl` (Windows) it degrades to the
  pre-existing single-process behavior with a one-time warning — the
  heartbeat-vs-web split is documented as unsupported there (ADR 0005).

Scope: same-machine, same-filesystem processes — exactly the heartbeat-vs-web
race. It does not (and does not try to) span machines; multi-host deployments
use the Postgres store backend.
"""

from __future__ import annotations

import contextlib
import re
import threading
import time

from . import config

try:
    import fcntl
except ImportError:                    # pragma: no cover - non-POSIX
    fcntl = None

_LOCAL = threading.local()

_THREAD_LOCKS = {}
_THREAD_LOCKS_GUARD = threading.Lock()

_SLOT_LOCKS = {}
_SLOT_LOCKS_GUARD = threading.Lock()

_WARNED = False


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", name)[:80] or "lock"


def _lock_path(name: str):
    d = config.MEMORY_DIR / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{_safe(name)}.lock"


# Default acquisition bound (ADR 0005 amendment 6). flock cannot go stale —
# the kernel releases it when the holder dies, even on kill -9 — so the only
# unbounded wait is a LIVE-but-wedged holder (SIGSTOP, D-state). A bounded
# default turns that pathology into a visible TimeoutError instead of a
# silent hang; every caller either handles it explicitly (usage ledger,
# conversation counter, watchlist pop, trace overflow) or surfaces it
# honestly (the heartbeat catches per job; a tool call errors visibly).
DEFAULT_TIMEOUT = 60.0


@contextlib.contextmanager
def lock(name: str, timeout: float | None = DEFAULT_TIMEOUT):
    """`with proclock.lock("usage-ledger"):` — exclusive across processes and
    threads on this machine; reentrant within a thread.

    `timeout` bounds the wait (LOCK_NB polling) and raises TimeoutError on
    expiry; the default is DEFAULT_TIMEOUT so no caller can hang forever on
    a wedged peer. Pass `timeout=None` to block indefinitely (explicit
    opt-in), or a small value on best-effort paths where losing one write
    under contention beats waiting."""
    global _WARNED
    # The depth table MUST key on the sanitized name — the same identity the
    # lock file uses. Keyed on the raw name, two spellings that sanitize to
    # one path ("usage ledger" / "usage-ledger") would take independent depth
    # entries but flock the same file, and a nested acquisition would
    # self-deadlock on the thread's own lock.
    key = _safe(name)
    depths = getattr(_LOCAL, "depths", None)
    if depths is None:
        depths = _LOCAL.depths = {}
    if depths.get(key):                # reentrant re-entry: no second flock
        depths[key] += 1
        try:
            yield
        finally:
            depths[key] -= 1
        return
    if fcntl is None:                  # Windows: thread-safe fallback
        _lock_path(name).touch(exist_ok=True)

        if not _WARNED:
            _WARNED = True
            from . import errors
            errors.capture(
                "proclock",
                OSError(
                    "fcntl unavailable ? cross-process locking degraded "
                    "to in-process thread locking"
                ),
                context=name,
            )

        with _THREAD_LOCKS_GUARD:
            thread_lock = _THREAD_LOCKS.setdefault(
                key, threading.Lock()
            )

        if timeout is None:
            acquired = thread_lock.acquire()
        else:
            acquired = thread_lock.acquire(
                timeout=max(0.0, timeout)
            )

        if not acquired:
            raise TimeoutError(
                f"proclock {name!r} not acquired in {timeout}s"
            )

        depths[key] = 1
        try:
            yield
        finally:
            depths[key] = 0
            thread_lock.release()
        return
    fh = open(_lock_path(name), "a+b")
    try:
        if timeout is None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fh.fileno(),
                                fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"proclock '{name}' not acquired in {timeout}s")
                    time.sleep(0.02)
        depths[key] = 1
        try:
            yield
        finally:
            depths[key] = 0
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


@contextlib.contextmanager
def slot(name: str, count: int, timeout: float | None = DEFAULT_TIMEOUT):
    """A cross-process COUNTING semaphore: acquire one of `count` machine-global
    slots for `name`, so no more than `count` holders run at once across every
    process AND thread on this machine (closes DEFERRED #9 for the model-call
    cap). Built by striping over `count` slot files, each held by a non-blocking
    `flock` — a binary flock per file yields a counting semaphore of width
    `count`. Blocks (bounded by `timeout`) until a slot frees; raises
    TimeoutError on expiry.

    Degrades to a NO-OP (per-process limiting only) with a ONE-TIME warning
    where fcntl is unavailable (Windows — DEFERRED #10 stays)."""
    global _WARNED
    count = max(1, int(count))
    if fcntl is None:                          # Windows fallback
        if not _WARNED:
            _WARNED = True
            from . import errors
            errors.capture(
                "proclock",
                OSError(
                    "fcntl unavailable ? machine-global cap degraded "
                    "to per-process"
                ),
                context=name,
            )

        slot_key = (_safe(name), count)
        with _SLOT_LOCKS_GUARD:
            semaphore = _SLOT_LOCKS.setdefault(
                slot_key,
                threading.BoundedSemaphore(count),
            )

        if timeout is None:
            acquired = semaphore.acquire()
        else:
            acquired = semaphore.acquire(
                timeout=max(0.0, timeout)
            )

        if not acquired:
            raise TimeoutError(
                f"no free {name!r} slot (cap {count}) "
                f"in {timeout}s"
            )

        try:
            yield
        finally:
            semaphore.release()
        return
    d = config.MEMORY_DIR / "locks"
    d.mkdir(parents=True, exist_ok=True)
    base = _safe(name)
    fhs = [open(d / f"{base}.slot{i}", "a+b") for i in range(count)]
    held = None
    try:
        deadline = None if timeout is None else time.monotonic() + timeout
        while held is None:
            for fh in fhs:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held = fh
                    break
                except OSError:
                    continue                   # this slot is taken — try next
            if held is not None:
                break
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(
                    f"no free '{name}' slot (cap {count}) in {timeout}s")
            time.sleep(0.01)
        yield
    finally:
        if held is not None:
            try:
                fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        for fh in fhs:
            try:
                fh.close()
            except OSError:
                pass

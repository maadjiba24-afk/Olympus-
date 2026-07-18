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

from . import config

try:
    import fcntl
except ImportError:                    # pragma: no cover - non-POSIX
    fcntl = None

_LOCAL = threading.local()
_WARNED = False


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", name)[:80] or "lock"


def _lock_path(name: str):
    d = config.MEMORY_DIR / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{_safe(name)}.lock"


@contextlib.contextmanager
def lock(name: str):
    """`with proclock.lock("usage-ledger"):` — exclusive across processes and
    threads on this machine; reentrant within a thread."""
    global _WARNED
    depths = getattr(_LOCAL, "depths", None)
    if depths is None:
        depths = _LOCAL.depths = {}
    if depths.get(name):               # reentrant re-entry: no second flock
        depths[name] += 1
        try:
            yield
        finally:
            depths[name] -= 1
        return
    if fcntl is None:                  # pragma: no cover - non-POSIX
        if not _WARNED:
            _WARNED = True
            from . import errors
            errors.capture("proclock", OSError(
                "fcntl unavailable — cross-process locking degraded to "
                "single-process (see ADR 0005)"), context=name)
        depths[name] = 1
        try:
            yield
        finally:
            depths[name] = 0
        return
    fh = open(_lock_path(name), "a+b")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        depths[name] = 1
        try:
            yield
        finally:
            depths[name] = 0
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()

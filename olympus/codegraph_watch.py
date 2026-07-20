"""Watch a tree and keep its graph current — no daemon deps, just mtimes.

`olympus codegraph watch <path>` polls the extractable files (the same set
collect_files would build from) and runs an incremental update when anything
changes. Polling over inotify/watchdog is a deliberate trade: zero
dependencies, identical behavior on every OS, and the incremental update it
triggers is cheap enough (sub-second on this repo) that a few seconds of
latency is invisible. Debounced so a save-storm (git checkout, formatter run)
triggers ONE rebuild, not fifty.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import codegraph_build

_POLL_SECONDS = 3.0
_DEBOUNCE_SECONDS = 2.0


def _snapshot(root: Path, include_docs: bool) -> dict[str, float]:
    snap: dict[str, float] = {}
    for p in codegraph_build.collect_files(root, include_docs):
        try:
            snap[str(p.relative_to(root))] = p.stat().st_mtime
        except OSError:
            pass
    return snap


def watch(project: str, root: str | Path, include_docs: bool = True,
          poll_seconds: float = _POLL_SECONDS,
          max_iterations: int | None = None,
          on_update=None) -> int:
    """Poll until interrupted (Ctrl-C) or `max_iterations` polls have run
    (tests use that). Returns the number of rebuilds performed. `on_update`
    (stats -> None) observes each rebuild."""
    root_p = Path(root).resolve()
    last = _snapshot(root_p, include_docs)
    rebuilds = 0
    iterations = 0
    try:
        while max_iterations is None or iterations < max_iterations:
            iterations += 1
            time.sleep(poll_seconds)
            now = _snapshot(root_p, include_docs)
            if now == last:
                continue
            # debounce: wait for the tree to sit still before rebuilding
            settle = _snapshot(root_p, include_docs)
            while settle != now:
                now = settle
                time.sleep(_DEBOUNCE_SECONDS)
                settle = _snapshot(root_p, include_docs)
            stats = codegraph_build.update(project, str(root_p), include_docs)
            rebuilds += 1
            last = now
            if on_update:
                on_update(stats)
    except KeyboardInterrupt:
        pass
    return rebuilds

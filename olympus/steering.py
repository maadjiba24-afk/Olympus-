"""Mid-run steering — nudge a running agent without interrupting it.

`/steer <note>` queues a note under the conversation's key; the agent loop
drains the queue after each tool round and appends the note to the next
request, so the model course-corrects instead of finishing the wrong task
and being re-run. Gateways handle `/steer` on the fast path (outside the
per-user serial worker), which is what lets the note land while a long
pipeline run is still in flight; in the synchronous CLI the note simply
applies to the next question's run.

Thread-safe: gateways put from HTTP/polling threads while the agent loop
drains from a worker thread. The current conversation key travels via a
contextvar (set by the orchestrator around a run), so nested specialist
runs inside one pipeline all see the same steering queue.
"""

from __future__ import annotations

import contextvars
import threading

_notes: dict[str, list[str]] = {}
_lock = threading.Lock()
_current: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "olympus_steer_key", default=None)

MAX_PENDING = 20          # a runaway sender can't grow the queue unbounded
MAX_NOTE_CHARS = 2000


def set_current(key: str | None):
    """Mark the conversation whose run is (about to be) in flight on this
    context. Returns a token for `reset`."""
    return _current.set(key)


def reset(token) -> None:
    _current.reset(token)


def put(key: str, note: str) -> bool:
    """Queue a steering note for `key`. Returns False when the queue is full."""
    note = (note or "").strip()[:MAX_NOTE_CHARS]
    if not key or not note:
        return False
    with _lock:
        pending = _notes.setdefault(key, [])
        if len(pending) >= MAX_PENDING:
            return False
        pending.append(note)
        return True


def drain(key: str) -> list[str]:
    """Pop and return all pending notes for `key` (oldest first)."""
    with _lock:
        return _notes.pop(key, [])


def drain_current() -> list[str]:
    key = _current.get()
    return drain(key) if key else []


def pending(key: str) -> int:
    with _lock:
        return len(_notes.get(key, ()))

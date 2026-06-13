"""Outcome tracking — what worked, what the user changed, what they declined.

The growth loop's honest core: every time the user approves, edits-then-approves,
or rejects a prepared action, that's ground-truth feedback on whether Olympus got
it right. We log those outcomes per action type, compute a track record, and
surface *insights* ("you've edited 4 of the last 5 emails before sending") — but
we never silently change behavior off them. Improvement is suggested to the user,
not imposed: no dark patterns, no manipulation, no hidden self-modification.

Stored on the shared store backend, per user, capped.
"""

from __future__ import annotations

import json
import threading
import time

from . import memory, store

_NS = "outcomes"
_LOCK = threading.Lock()
_MAX = 1000

# outcome kinds for a prepared action
APPROVED = "approved"               # approved as prepared (a clean win)
APPROVED_AFTER_EDIT = "approved_after_edit"   # needed a fix first
REJECTED = "rejected"
UNDONE = "undone"

_MIN_SAMPLES = 5                    # don't infer anything from a tiny history
_INSIGHT_RATE = 0.5                 # edit+reject share that warrants a nudge


def record(user: str, ref: str, outcome: str, kind: str = "action") -> None:
    """Append one outcome event (best-effort; never raises into the caller)."""
    try:
        with _LOCK:
            log = _load(user)
            log.append({"ts": time.time(), "kind": kind, "ref": ref,
                        "outcome": outcome})
            _save(user, log[-_MAX:])
    except Exception:
        pass


def _load(user: str) -> list:
    blob = store.backend().get(_NS, memory.safe_id(user))
    if not blob:
        return []
    try:
        return json.loads(blob)
    except (ValueError, json.JSONDecodeError):
        return []


def _save(user: str, data: list) -> None:
    store.backend().put(_NS, memory.safe_id(user), json.dumps(data).encode())


def events(user: str) -> list:
    return _load(user)


def stats(user: str) -> dict:
    """Per-action-type and overall counts + the approved-as-is rate."""
    by_ref: dict[str, dict] = {}
    overall = {"total": 0, APPROVED: 0, APPROVED_AFTER_EDIT: 0,
               REJECTED: 0, UNDONE: 0}
    for e in _load(user):
        row = by_ref.setdefault(e["ref"], {"total": 0, APPROVED: 0,
                                           APPROVED_AFTER_EDIT: 0,
                                           REJECTED: 0, UNDONE: 0})
        for bucket in (row, overall):
            bucket["total"] += 1
            if e["outcome"] in bucket:
                bucket[e["outcome"]] += 1
    for row in list(by_ref.values()) + [overall]:
        t = row["total"] or 1
        row["approve_rate"] = round(row[APPROVED] / t, 2)
    return {"by_ref": by_ref, "overall": overall}


def insights(user: str) -> list[dict]:
    """Surface (not apply) suggestions where the user keeps changing or
    declining a given action type — a sign the defaults are off."""
    out = []
    for ref, row in stats(user)["by_ref"].items():
        if row["total"] < _MIN_SAMPLES:
            continue
        friction = (row[APPROVED_AFTER_EDIT] + row[REJECTED]) / row["total"]
        if friction >= _INSIGHT_RATE:
            out.append({
                "ref": ref,
                "friction": round(friction, 2),
                "message": (
                    f"You've changed or declined {int(friction*100)}% of the "
                    f"last {row['total']} '{ref}' actions. Consider setting a "
                    f"preference (olympus profile) or editing the relevant "
                    f"playbook so Olympus prepares them the way you want."),
            })
    return out

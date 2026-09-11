"""Playbooks — procedural memory: save a workflow once, re-run it by name.

This is the retention engine from the architecture's growth system. When a user
repeats a multi-step task ("the weekly investor update: pull metrics, draft in
my voice, CC my cofounder, wait for approval"), Olympus can save it as a named,
versioned procedure. Next time, one phrase loads the steps — and every action in
them still flows through the approval gate, exactly as before. Month-6 stops
re-explaining what month-1 had to spell out.

Procedures are inspectable, user-editable, and approval-gated to create: the
agent may *propose* one (status 'proposed'), but it isn't followed until the
user approves it. Stored on the same backend as everything else.
"""

from __future__ import annotations

import re
import time
import uuid

from . import owner_evidence as evidence

_LEGACY_NS = "playbooks"
_NS = "playbooks.v2"
_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(("the", "for", "and", "run", "this", "that", "your", "with",
                   "now", "please", "again", "let", "make", "give"))

ACTIVE, PROPOSED, DEPRECATED = "active", "proposed", "deprecated"
_MAX_PLAYBOOKS = 200       # per user
_MAX_STEPS = 40            # per playbook
_MAX_STEP_LEN = 400        # per step
_MAX_NAME_LEN = 100


def _tok(text: str) -> set[str]:
    return {w for w in _WORD.findall(str(text).lower())
            if len(w) > 2 and w not in _STOP}


def _validate(data):
    evidence.records(data, _MAX_PLAYBOOKS)
    ids, names = set(), set()
    for row in data:
        evidence.fields(row, ("id", "name", "version", "steps", "status",
                              "created_at", "updated_at", "use_count", "last_used_at"))
        evidence.text(row["id"], 64)
        evidence.text(row["name"], _MAX_NAME_LEN)
        if row["id"] in ids or row["name"].lower() in names:
            raise ValueError("duplicate playbook")
        ids.add(row["id"]); names.add(row["name"].lower())
        evidence.integer(row["version"], minimum=1)
        evidence.integer(row["use_count"])
        evidence.number(row["created_at"])
        evidence.number(row["updated_at"], minimum=row["created_at"])
        if row["last_used_at"] is not None:
            evidence.number(row["last_used_at"])
        if row["status"] not in (ACTIVE, PROPOSED, DEPRECATED):
            raise ValueError("invalid status")
        evidence.records(row["steps"], _MAX_STEPS)
        if not row["steps"]:
            raise ValueError("missing steps")
        for step in row["steps"]:
            evidence.text(step, _MAX_STEP_LEN)


def _evidence(user):
    return evidence.JsonStore(user, "playbooks", _validate, namespace=_NS)


def evidence_status(user):
    return _evidence(user).status()


def _load(user: str) -> list:
    return _evidence(user).load()


def _save(user: str, data: list) -> None:
    _evidence(user).save(data)


def _find(items: list, name_or_id: str) -> dict | None:
    key = name_or_id.strip().lower()
    for p in items:
        if p["id"] == name_or_id or p["name"].lower() == key:
            return p
    return None


def save(user: str, name: str, steps: list[str], status: str = ACTIVE) -> dict:
    """Create a playbook, or bump an existing one to a new version (keeping its
    id and use stats). Empty steps are dropped."""
    steps = [s.strip()[:_MAX_STEP_LEN] for s in steps if s and s.strip()][:_MAX_STEPS]
    name = name.strip()[:_MAX_NAME_LEN]
    if not name or not steps:
        raise ValueError("a playbook needs a name and at least one step")
    now = time.time()
    with _evidence(user).guard():
        items = _load(user)
        existing = _find(items, name)
        if not existing and len(items) >= _MAX_PLAYBOOKS:
            raise ValueError("too many playbooks — remove some first")
        if existing:
            existing["steps"] = steps
            existing["version"] += 1
            existing["status"] = status
            existing["updated_at"] = now
            pb = existing
        else:
            pb = {"id": uuid.uuid4().hex[:12], "name": name, "version": 1,
                  "steps": steps, "status": status, "created_at": now,
                  "updated_at": now, "use_count": 0, "last_used_at": None}
            items.append(pb)
        _save(user, items)
    return dict(pb)


def propose(user: str, name: str, steps: list[str]) -> dict:
    """An unapproved proposal cannot replace an existing active procedure."""
    with _evidence(user).guard():
        if _find([p for p in _load(user) if p["status"] == ACTIVE], name):
            raise ValueError("an active playbook with that name already exists")
        return save(user, name, steps, status=PROPOSED)


def approve(user: str, name_or_id: str) -> dict | None:
    return _set_status(user, name_or_id, ACTIVE)


def _set_status(user: str, name_or_id: str, status: str) -> dict | None:
    with _evidence(user).guard():
        items = _load(user)
        pb = _find(items, name_or_id)
        if pb:
            pb["status"] = status
            _save(user, items)
            return dict(pb)
    return None


def delete(user: str, name_or_id: str) -> bool:
    with _evidence(user).guard():
        items = _load(user)
        pb = _find(items, name_or_id)
        if not pb:
            return False
        items.remove(pb)
        _save(user, items)
    return True


def get(user: str, name_or_id: str) -> dict | None:
    return _find(_load(user), name_or_id)


def list_all(user: str, status: str | None = None) -> list:
    items = _load(user)
    return [p for p in items if status is None or p["status"] == status]


def mark_used(user: str, name_or_id: str) -> None:
    with _evidence(user).guard():
        items = _load(user)
        pb = _find(items, name_or_id)
        if pb:
            pb["use_count"] += 1
            pb["last_used_at"] = time.time()
            _save(user, items)


def match(user: str, message: str) -> dict | None:
    """Find the active playbook this message is asking to run. Requires most of
    the playbook's name words to appear, so it won't fire by accident."""
    msg = _tok(message)
    if not msg:
        return None
    best, best_score = None, 0.0
    for p in _load(user):
        if p["status"] != ACTIVE:
            continue
        name_tok = _tok(p["name"])
        if not name_tok:
            continue
        present = len(name_tok & msg) / len(name_tok)
        if present >= 0.6 and present > best_score:
            best, best_score = p, present
    return best


def _format(pb: dict) -> str:
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(pb["steps"], 1))
    return (f'\n\n## Saved procedure: "{pb["name"]}"\n'
            "The user has a saved step-by-step workflow for this. Follow these "
            "steps, preparing any actions for their approval (never execute "
            f"without it):\n{steps}")


def context_block(user: str, message: str) -> str:
    """Validated owned procedure, or explicit sanitized unavailability."""
    try:
        pb = match(user, message)
        return _format(pb) if pb else ""
    except evidence.OwnerEvidenceStateError as err:
        return "\n\n[" + str(err) + "]"


def run_block(user: str, name_or_id: str) -> str | None:
    """Explicitly load a playbook by name (for `olympus playbook run`)."""
    pb = get(user, name_or_id)
    if not pb or pb["status"] != ACTIVE:
        return None
    mark_used(user, pb["id"])
    return _format(pb)

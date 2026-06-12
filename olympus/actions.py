"""The Action spine — controlled-autonomy execution with approval gates.

This is what turns Olympus from an advisory council into an operating assistant
that can *do* things, safely. The invariant:

    Agents PREPARE actions (data). They never execute them. Execution happens
    here, behind an explicit approval gate or a policy that allows it.

That separation is the whole safety model: an agent that has read untrusted
content (an email, a web page) physically cannot perform a real-world action —
it can only produce an Action object that a human (or a strict policy) must
approve before this module executes it.

Lifecycle:                 ┌────────── reject ──────────┐
    prepare → PREPARED ──→ approve → APPROVED → execute →┴ EXECUTED ─→ undo ─→ UNDONE
                                                          └ FAILED

Every transition is appended to an immutable per-user audit log.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import config, memory, prefs

# --- risk classes --------------------------------------------------------
# Ordered by how much trust an action needs before it may run un-prompted.
TRIVIAL = "reversible_trivial"        # label an email, save a note
NOTABLE = "reversible_notable"        # move files, create a calendar hold
IRREVERSIBLE = "irreversible"         # send an email, post publicly
FINANCIAL_LEGAL = "irreversible_financial_legal"  # pay, sign, delete data
RISK_CLASSES = (TRIVIAL, NOTABLE, IRREVERSIBLE, FINANCIAL_LEGAL)

# --- autonomy levels (per user, stored in prefs) -------------------------
L0_SUGGEST = 0      # propose only
L1_PREPARE = 1      # prepare, stop before executing (default)
L2_APPROVE = 2      # execute after one explicit approval
L3_AUTO_SAFE = 3    # auto-run trivial reversible actions within policy
L4_STANDING = 4     # auto-run pre-approved recurring workflows
DEFAULT_AUTONOMY = L1_PREPARE


def autonomy_level(user: str) -> int:
    try:
        return int(prefs.get(user, "autonomy", DEFAULT_AUTONOMY))
    except (TypeError, ValueError):
        return DEFAULT_AUTONOMY


def set_autonomy(user: str, level: int) -> str:
    level = max(L0_SUGGEST, min(L4_STANDING, int(level)))
    prefs.set(user, "autonomy", level)
    names = {0: "suggest only", 1: "prepare (default)", 2: "approve-to-act",
             3: "auto safe actions", 4: "standing authority"}
    return f"Autonomy set to L{level} — {names[level]}."


def _min_level_to_auto(risk_class: str) -> int:
    """Lowest autonomy level at which an action may run WITHOUT a per-action
    approval. Irreversible/financial actions can NEVER auto-run."""
    return {
        TRIVIAL: L3_AUTO_SAFE,
        NOTABLE: L4_STANDING,
        IRREVERSIBLE: 99,         # always explicit approval
        FINANCIAL_LEGAL: 99,      # always explicit approval
    }.get(risk_class, 99)


# --- action types (registry) ---------------------------------------------

@dataclass(frozen=True)
class ActionType:
    name: str
    risk_class: str
    scope: str                                   # permission scope required
    preview: Callable[[dict], str]               # human-readable dry-run
    execute: Callable[[dict], dict]              # performs it; returns result
    undo: Callable[[dict], str] | None = None    # reverses it from the result
    description: str = ""

    @property
    def reversible(self) -> bool:
        return self.undo is not None


_REGISTRY: dict[str, ActionType] = {}


def register(action_type: ActionType) -> None:
    if action_type.risk_class not in RISK_CLASSES:
        raise ValueError(f"unknown risk class: {action_type.risk_class}")
    _REGISTRY[action_type.name] = action_type


def registered() -> dict[str, ActionType]:
    return dict(_REGISTRY)


# --- the Action object ----------------------------------------------------

PREPARED, APPROVED, EXECUTED, FAILED, REJECTED, UNDONE = (
    "prepared", "approved", "executed", "failed", "rejected", "undone")


@dataclass
class Action:
    id: str
    user: str
    type: str
    title: str
    payload: dict
    risk_class: str
    reversible: bool
    status: str = PREPARED
    preview: str = ""
    result: dict = field(default_factory=dict)
    error: str = ""
    created_at: float = field(default_factory=time.time)
    approved_at: float | None = None
    executed_at: float | None = None


# --- store + audit log (per user) ----------------------------------------

def _dir(user: str) -> Path:
    d = config.MEMORY_DIR / "actions" / memory.safe_id(user)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(user: str, action_id: str) -> Path:
    return _dir(user) / f"{action_id}.json"


def _save(action: Action) -> None:
    _path(action.user, action.id).write_text(
        json.dumps(asdict(action), indent=2), encoding="utf-8")


def _audit(action: Action, event: str) -> None:
    """Append an immutable record of every state transition."""
    log = _dir(action.user) / "audit.jsonl"
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": time.time(), "action_id": action.id, "type": action.type,
            "event": event, "status": action.status,
            "risk": action.risk_class,
        }) + "\n")


def get(user: str, action_id: str) -> Action | None:
    path = _path(user, action_id)
    if not path.exists():
        return None
    return Action(**json.loads(path.read_text(encoding="utf-8")))


def pending(user: str) -> list[Action]:
    out = []
    for p in sorted(_dir(user).glob("*.json")):
        a = Action(**json.loads(p.read_text(encoding="utf-8")))
        if a.status in (PREPARED, APPROVED):
            out.append(a)
    return out


def history(user: str, limit: int = 50) -> list[Action]:
    items = []
    for p in _dir(user).glob("*.json"):
        items.append(Action(**json.loads(p.read_text(encoding="utf-8"))))
    items.sort(key=lambda a: a.created_at, reverse=True)
    return items[:limit]


# --- permission scopes (per user) ----------------------------------------

def granted_scopes(user: str) -> set[str]:
    return set(prefs.get(user, "scopes", []) or [])


def grant_scope(user: str, scope: str) -> str:
    s = granted_scopes(user)
    s.add(scope)
    prefs.set(user, "scopes", sorted(s))
    return f"Granted scope '{scope}'."


def revoke_scope(user: str, scope: str) -> str:
    s = granted_scopes(user)
    s.discard(scope)
    prefs.set(user, "scopes", sorted(s))
    return f"Revoked scope '{scope}'."


def revoke_all(user: str) -> str:
    prefs.set(user, "scopes", [])
    return "Revoked all integration scopes (kill switch)."


# --- the state machine ----------------------------------------------------

def prepare(user: str, type_name: str, payload: dict,
            title: str | None = None) -> Action:
    """Create a prepared action with a preview. Never executes."""
    at = _REGISTRY.get(type_name)
    if at is None:
        raise ValueError(f"unknown action type: {type_name}")
    try:
        preview = at.preview(payload)
    except Exception as err:
        preview = f"(could not render preview: {err})"
    action = Action(
        id=uuid.uuid4().hex[:12], user=memory.safe_id(user), type=type_name,
        title=title or type_name.replace("_", " ").title(),
        payload=payload, risk_class=at.risk_class, reversible=at.reversible,
        preview=preview)
    _save(action)
    _audit(action, "prepared")
    return action


def can_auto_execute(action: Action) -> bool:
    """True if policy allows running this without a per-action approval."""
    at = _REGISTRY.get(action.type)
    if at is None:
        return False
    if at.scope and at.scope not in granted_scopes(action.user):
        return False
    return autonomy_level(action.user) >= _min_level_to_auto(action.risk_class)


def _execute(action: Action) -> Action:
    at = _REGISTRY.get(action.type)
    if at is None:
        action.status = FAILED
        action.error = f"unknown action type: {action.type}"
        _save(action); _audit(action, "failed")
        return action
    if at.scope and at.scope not in granted_scopes(action.user):
        action.status = FAILED
        action.error = (f"permission '{at.scope}' not granted — "
                        "grant it before executing")
        _save(action); _audit(action, "blocked_no_scope")
        return action
    try:
        action.result = at.execute(action.payload) or {}
        action.status = EXECUTED
        action.executed_at = time.time()
        _save(action); _audit(action, "executed")
    except Exception as err:
        action.status = FAILED
        action.error = str(err)
        _save(action); _audit(action, "failed")
    return action


def approve(user: str, action_id: str) -> Action:
    """Explicit human approval → executes immediately. The gate."""
    action = get(user, action_id)
    if action is None:
        raise ValueError("no such action")
    if action.status != PREPARED:
        raise ValueError(f"action is {action.status}, not awaiting approval")
    action.status = APPROVED
    action.approved_at = time.time()
    _save(action); _audit(action, "approved")
    return _execute(action)


def reject(user: str, action_id: str, reason: str = "") -> Action:
    """Decline a prepared action — recorded as a learning signal."""
    action = get(user, action_id)
    if action is None:
        raise ValueError("no such action")
    if action.status != PREPARED:
        raise ValueError(f"action is {action.status}, cannot reject")
    action.status = REJECTED
    action.error = reason
    _save(action); _audit(action, "rejected")
    # rejections teach future behavior
    memory.set_user(action.user)
    memory.save("feedback", f"rejected action: {action.type}",
                f"User rejected a prepared {action.type} "
                f"({action.risk_class}).\nReason: {reason or '(none given)'}\n"
                f"Preview was:\n{action.preview[:1000]}")
    return action


def auto_or_hold(action: Action) -> Action:
    """Run automatically if policy allows; otherwise leave it for approval."""
    if can_auto_execute(action):
        action.status = APPROVED
        action.approved_at = time.time()
        _save(action); _audit(action, "auto_approved")
        return _execute(action)
    return action  # stays PREPARED, awaiting the human


def undo(user: str, action_id: str) -> Action:
    """Reverse a reversible, executed action."""
    action = get(user, action_id)
    if action is None:
        raise ValueError("no such action")
    at = _REGISTRY.get(action.type)
    if action.status != EXECUTED:
        raise ValueError(f"action is {action.status}, cannot undo")
    if not at or at.undo is None:
        raise ValueError("this action type is not reversible")
    try:
        at.undo(action.result)
        action.status = UNDONE
        _save(action); _audit(action, "undone")
    except Exception as err:
        action.error = f"undo failed: {err}"
        _save(action); _audit(action, "undo_failed")
    return action

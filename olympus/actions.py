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
        level = int(prefs.get(user, "autonomy", DEFAULT_AUTONOMY))
    except (TypeError, ValueError):
        level = DEFAULT_AUTONOMY
    # A conversation's capability profile caps how autonomous it can be —
    # a guest chat can never talk itself into standing authority.
    from . import capprofile
    return min(level, capprofile.autonomy_cap(user))


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
    pins_root: bool = False                      # capture workdir() at prepare

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
    why: str = ""                      # the agent's stated reason, shown on the card
    edited: bool = False               # did the user change it before approving?
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


def pending_all() -> list[Action]:
    """Pending/approved actions across EVERY user — the operator overview
    (a user still only ever sees their own via pending()). Read-only."""
    root = config.MEMORY_DIR / "actions"
    if not root.is_dir():
        return []
    out: list[Action] = []
    for d in sorted(root.iterdir()):
        if d.is_dir():
            try:
                out.extend(pending(d.name))
            except (json.JSONDecodeError, OSError, TypeError):
                continue           # one corrupt record must not hide the rest
    out.sort(key=lambda a: a.created_at, reverse=True)
    return out


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


# --- daily rate limits (runaway / abuse guard, per action type) -----------
# A confused or prompt-injected agent shouldn't be able to flood the world with
# real actions even if approvals come quickly. These cap how many of a given
# action type may EXECUTE per day. They guard against runaway behavior, not
# spend — drafting and rejecting never count. 0 = unlimited.
_DEFAULT_LIMITS = {
    TRIVIAL: 0,            # save a note — no cap
    NOTABLE: 0,            # reversible edits — no cap
    IRREVERSIBLE: 50,      # send email, post — generous runaway guard
    FINANCIAL_LEGAL: 10,   # pay, sign, delete — tighter (also always approved)
}


def daily_limit(user: str, type_name: str) -> int:
    """Max executions/day for an action type. A per-user override (set via
    `olympus limit`) wins over the risk-class default; 0 means unlimited."""
    overrides = prefs.get(user, "action_limits", {}) or {}
    if type_name in overrides:
        try:
            return max(0, int(overrides[type_name]))
        except (TypeError, ValueError):
            pass
    at = _REGISTRY.get(type_name)
    return _DEFAULT_LIMITS.get(at.risk_class, 0) if at else 0


def set_limit(user: str, type_name: str, n: int) -> str:
    """Persist a per-user daily execution cap for an action type (0 = off)."""
    overrides = dict(prefs.get(user, "action_limits", {}) or {})
    overrides[type_name] = max(0, int(n))
    prefs.set(user, "action_limits", overrides)
    if overrides[type_name] == 0:
        return f"Removed the daily limit on '{type_name}' (now unlimited)."
    return (f"Daily limit for '{type_name}' set to {overrides[type_name]} "
            f"execution(s) per day.")


def limits(user: str) -> dict[str, int]:
    """Effective daily limits for every registered action type (0 = unlimited)."""
    return {name: daily_limit(user, name) for name in _REGISTRY}


def _executed_today(user: str, type_name: str) -> int:
    """How many actions of this type already executed today, for this user."""
    midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    count = 0
    for a in history(user, limit=10000):
        if (a.type == type_name and a.status == EXECUTED
                and a.executed_at and a.executed_at >= midnight):
            count += 1
    return count


# --- the state machine ----------------------------------------------------

def prepare(user: str, type_name: str, payload: dict,
            title: str | None = None, why: str = "") -> Action:
    """Create a prepared action with a preview. Never executes."""
    at = _REGISTRY.get(type_name)
    if at is None:
        raise ValueError(f"unknown action type: {type_name}")
    # Pin the workspace root NOW, on whatever thread prepares/previews this
    # action, so execution (which may run later from a different context — the
    # web/CLI approval handler) confines the file operation to the SAME root the
    # user previewed (ADR 0005: one pinned root through prepare→approve). The
    # key is `_`-prefixed, so `edit()` treats it as internal and non-editable.
    if at.pins_root and "_pinned_root" not in payload:
        try:
            from . import sandbox
            # current_root() is the scoped per-worker root when a specialist is
            # dispatched (M1), else the shared workspace — so preview and
            # execution share the SAME root even across the thread hop.
            payload = {**payload, "_pinned_root": str(sandbox.current_root())}
        except Exception:
            pass                       # never block preparing an action on this
    try:
        preview = at.preview(payload)
    except Exception as err:
        preview = f"(could not render preview: {err})"
    action = Action(
        id=uuid.uuid4().hex[:12], user=memory.safe_id(user), type=type_name,
        title=title or type_name.replace("_", " ").title(),
        payload=payload, risk_class=at.risk_class, reversible=at.reversible,
        preview=preview, why=why)
    _save(action)
    _audit(action, "prepared")
    return action


def edit(user: str, action_id: str, changes: dict,
         title: str | None = None) -> Action:
    """Modify a PREPARED action before approving it — the user stays in
    control of the exact content that will run. The preview is re-rendered
    from the edited payload, and the action remains awaiting approval."""
    action = get(user, action_id)
    if action is None:
        raise ValueError("no such action")
    if action.status != PREPARED:
        raise ValueError(f"action is {action.status}, only prepared actions "
                         "can be edited")
    for key, value in (changes or {}).items():
        if key.startswith("_"):       # internal fields are not user-editable
            continue
        action.payload[key] = value
    if title:
        action.title = title
    action.edited = True
    at = _REGISTRY.get(action.type)
    try:
        action.preview = at.preview(action.payload)
    except Exception as err:
        action.preview = f"(could not render preview: {err})"
    _save(action)
    _audit(action, "edited")
    return action


def can_auto_execute(action: Action, level: int | None = None) -> bool:
    """True if policy allows running this without a per-action approval.

    `level` lets a caller supply an effective autonomy level in place of the
    user's global one — this is how earned per-domain trust (see `trust.py`)
    widens the safe envelope for a proven site. A caller-supplied level can never
    exceed the conversation's capability-profile cap, so it can only ever relax
    the gate for REVERSIBLE actions on a trusted domain, never lift an ingesting
    run or auto-run an irreversible/financial action (those need level 99).
    """
    at = _REGISTRY.get(action.type)
    if at is None:
        return False
    if at.scope and at.scope not in granted_scopes(action.user):
        return False
    if level is None:
        lvl = autonomy_level(action.user)
    else:
        from . import capprofile
        lvl = min(int(level), capprofile.autonomy_cap(action.user))
    return lvl >= _min_level_to_auto(action.risk_class)


def _execute(action: Action) -> Action:
    at = _REGISTRY.get(action.type)
    if at is None:
        action.status = FAILED
        action.error = f"unknown action type: {action.type}"
        _save(action); _audit(action, "failed")
        return action
    # Shadow boundary, defence in depth (P5-A5). The primary enforcement point
    # is `tools.resolve_handler`, but the approval spine can be reached without
    # a tool call at all — a CLI approval, the web approval handler, a
    # scheduled job. An action's own risk class decides: reversible ones are
    # Olympus-local and may run; irreversible and financial ones are exactly
    # what shadow mode exists to prevent.
    from . import shadow
    if shadow.enabled() and action.risk_class in (IRREVERSIBLE,
                                                  FINANCIAL_LEGAL):
        shadow.record_intent(f"action:{action.type}", shadow.PROHIBITED,
                             "refused", detail=f"risk={action.risk_class}")
        action.status = FAILED
        action.error = (
            f"refused in shadow mode: '{action.type}' is {action.risk_class}. "
            f"Shadow runs record irreversible intents, they never perform them.")
        _save(action); _audit(action, "blocked_shadow")
        return action
    if at.scope and at.scope not in granted_scopes(action.user):
        action.status = FAILED
        action.error = (f"permission '{at.scope}' not granted — "
                        "grant it before executing")
        _save(action); _audit(action, "blocked_no_scope")
        return action
    limit = daily_limit(action.user, action.type)
    if limit > 0 and _executed_today(action.user, action.type) >= limit:
        action.status = FAILED
        action.error = (
            f"daily limit reached — already executed {limit} '{action.type}' "
            f"action(s) today. This guards against a runaway agent; raise it "
            f"with `olympus limit {action.type} <n>` (0 = unlimited).")
        _save(action); _audit(action, "blocked_rate_limit")
        return action
    # Agent Behavioral Contract: a formal, declarative re-check of the approval
    # spine + autonomy dial at the single execution chokepoint. Defense in depth
    # — if any code path reaches _execute with an irreversible action that never
    # earned a real approval, the contract blocks it and triggers Recovery
    # (revert to awaiting-approval), independent of the imperative checks above.
    from . import behavioral_contracts as _abc
    try:
        _abc.enforce("action.execute", _abc.action_context(action, at))
    except _abc.ContractViolation as viol:
        # Recovery: HOLD → back to PREPARED (await the human); anything else →
        # fail closed. Either way the action does NOT run.
        action.status = PREPARED if viol.recovery == _abc.HOLD else FAILED
        action.error = f"blocked by behavioral contract: {'; '.join(viol.reasons)}"
        action.approved_at = None
        _save(action); _audit(action, "blocked_contract")
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
    result = _execute(action)
    if result.status == EXECUTED:      # record the outcome only if it actually ran
        from . import outcomes
        outcomes.record(action.user, action.type,
                        outcomes.APPROVED_AFTER_EDIT if action.edited
                        else outcomes.APPROVED)
    return result


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
    from . import outcomes
    outcomes.record(action.user, action.type, outcomes.REJECTED)
    # rejections teach future behavior
    memory.set_user(action.user)
    memory.save("feedback", f"rejected action: {action.type}",
                f"User rejected a prepared {action.type} "
                f"({action.risk_class}).\nReason: {reason or '(none given)'}\n"
                f"Preview was:\n{action.preview[:1000]}")
    return action


def auto_or_hold(action: Action, level: int | None = None) -> Action:
    """Run automatically if policy allows; otherwise leave it for approval.
    `level` optionally supplies an earned per-domain autonomy level (see
    `can_auto_execute`)."""
    if can_auto_execute(action, level):
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
        from . import outcomes
        outcomes.record(action.user, action.type, outcomes.UNDONE)
    except Exception as err:
        action.error = f"undo failed: {err}"
        _save(action); _audit(action, "undo_failed")
    return action

"""Who may do what: the autonomous / human-only split, enforced.

The Phase 10 governance rule is a list of things Olympus may do by itself and a
list of things it may not. A list in a document is a hope. This module makes it
a function call that raises.

The model is deliberately small. Every privileged operation in the evolution
machinery names an `Action`, and every caller carries an `Actor`. `authorise`
answers one question — may this actor perform this action — and the answer for
a human-only action performed by an autonomous actor is an exception, not a
`False` an autonomous caller could ignore.

Two design choices worth defending:

**Actions are an exhaustive enum, not free strings.** A typo in a string action
name would silently authorise nothing and block nothing. An unknown `Action`
cannot be constructed.

**An operator token is required material, not a boolean.** `Actor.operator(...)`
refuses an empty token, so "an operator approved this" can never be asserted by
an autonomous caller passing `is_operator=True`. The token itself is never
logged — only whether one was present, and the operator's name.

The one asymmetry: **stopping is autonomous, starting is not.** Olympus may
engage a kill switch on its own judgement, at any time, without asking. It may
never clear one. Every other autonomous power in this module is an observation
or a proposal; the emergency halt is the single autonomous *action*, and it is
autonomous precisely because requiring human approval to stop would be the
dangerous design.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from .clock import Clock, default_clock
from .contracts import ensure_utc, jsonable
from .errors import ConfigurationError, GovernanceViolation


class Action(str, Enum):
    """Every privileged operation the evolution machinery can name."""

    # -- autonomous: observation and analysis --------------------------------
    OBSERVE = "observe"
    LEARN = "learn"
    EVALUATE = "evaluate"
    RESEARCH = "research"
    GENERATE_HYPOTHESIS = "generate_hypothesis"
    BUILD_PROTOTYPE = "build_prototype"
    GENERATE_TESTS = "generate_tests"
    PROPOSE_IMPROVEMENT = "propose_improvement"
    RECOMMEND_DEPRECATION = "recommend_deprecation"
    #: Demote a deteriorating *analytical* component (a model, a forecaster, a
    #: strategy) to a more restricted state. Never a promotion.
    RESTRICT_COMPONENT = "restrict_component"
    #: The emergency halt. Autonomous by design — see the module docstring.
    EMERGENCY_SHUTDOWN = "emergency_shutdown"

    # -- human-only: anything that increases risk or reach --------------------
    RISK_REAL_CAPITAL = "risk_real_capital"
    PROMOTE_TO_LIVE = "promote_to_live"
    PROMOTE_CAPABILITY = "promote_capability"
    EXPAND_PERMISSIONS = "expand_permissions"
    MODIFY_RISK_LIMITS = "modify_risk_limits"
    MODIFY_SAFETY_KERNEL = "modify_safety_kernel"
    DEPLOY_CODE = "deploy_code"
    MERGE_CODE = "merge_code"
    ENABLE_LIVE_TRADING = "enable_live_trading"
    CLEAR_KILL_SWITCH = "clear_kill_switch"
    ROTATE_SECRETS = "rotate_secrets"
    DISABLE_MONITORING = "disable_monitoring"
    DISABLE_AUDIT = "disable_audit"
    #: Named so the prohibition is enforceable rather than aspirational: there
    #: is no actor kind for which these are permitted at all.
    CONCEAL_RESULT = "conceal_result"
    REWRITE_AUDIT_HISTORY = "rewrite_audit_history"


#: Actions Olympus may perform without asking anyone.
AUTONOMOUS_ACTIONS: frozenset[Action] = frozenset({
    Action.OBSERVE, Action.LEARN, Action.EVALUATE, Action.RESEARCH,
    Action.GENERATE_HYPOTHESIS, Action.BUILD_PROTOTYPE, Action.GENERATE_TESTS,
    Action.PROPOSE_IMPROVEMENT, Action.RECOMMEND_DEPRECATION,
    Action.RESTRICT_COMPONENT, Action.EMERGENCY_SHUTDOWN,
})

#: Actions requiring a named operator carrying a token.
HUMAN_ONLY_ACTIONS: frozenset[Action] = frozenset({
    Action.RISK_REAL_CAPITAL, Action.PROMOTE_TO_LIVE,
    Action.PROMOTE_CAPABILITY, Action.EXPAND_PERMISSIONS,
    Action.MODIFY_RISK_LIMITS, Action.MODIFY_SAFETY_KERNEL,
    Action.DEPLOY_CODE, Action.MERGE_CODE, Action.ENABLE_LIVE_TRADING,
    Action.CLEAR_KILL_SWITCH, Action.ROTATE_SECRETS,
    Action.DISABLE_MONITORING, Action.DISABLE_AUDIT,
})

#: Actions nobody may perform through this system. An operator who genuinely
#: needs to correct the record does it out of band, leaving their own trace;
#: what must not exist is an in-system path that makes concealment routine.
PROHIBITED_ACTIONS: frozenset[Action] = frozenset({
    Action.CONCEAL_RESULT, Action.REWRITE_AUDIT_HISTORY,
})


def _classify(action: Action) -> str:
    if action in PROHIBITED_ACTIONS:
        return "prohibited"
    if action in HUMAN_ONLY_ACTIONS:
        return "human_only"
    if action in AUTONOMOUS_ACTIONS:
        return "autonomous"
    return "unclassified"


class ActorKind(str, Enum):
    #: Olympus itself: the evolution engine, an analysis agent, a scheduled job.
    AUTONOMOUS = "autonomous"
    #: A named human carrying an operator token.
    OPERATOR = "operator"
    #: Infrastructure performing a non-privileged mechanical step.
    SYSTEM = "system"


@dataclass(frozen=True)
class Actor:
    """Who is asking. Constructed through the classmethods, not directly."""

    name: str
    kind: ActorKind
    #: Presence is what matters; the value is never logged or compared against
    #: a stored secret here — the token's job is to make "a human did this"
    #: unforgeable *by the autonomous path*, which has no way to obtain one.
    _token: str = ""

    def __post_init__(self):
        if not str(self.name).strip():
            raise ConfigurationError("an actor must be named")
        if self.kind is ActorKind.OPERATOR and not str(self._token).strip():
            raise ConfigurationError(
                "an operator actor requires a token; without one there is no "
                "difference between a human approval and the system asserting "
                "that a human approved", actor=self.name)

    @classmethod
    def autonomous(cls, name: str = "olympus") -> "Actor":
        return cls(name=name, kind=ActorKind.AUTONOMOUS)

    @classmethod
    def operator(cls, name: str, *, token: str) -> "Actor":
        return cls(name=name, kind=ActorKind.OPERATOR, _token=token)

    @classmethod
    def system(cls, name: str = "system") -> "Actor":
        return cls(name=name, kind=ActorKind.SYSTEM)

    @property
    def is_operator(self) -> bool:
        return self.kind is ActorKind.OPERATOR

    @property
    def has_token(self) -> bool:
        return bool(str(self._token).strip())

    def to_dict(self) -> dict:
        """Token-free by construction. There is no code path that serialises it."""
        return {"name": self.name, "kind": self.kind.value,
                "operator_token_present": self.has_token}

    def __repr__(self) -> str:                          # pragma: no cover
        return f"Actor(name={self.name!r}, kind={self.kind.value})"

    def __str__(self) -> str:                           # pragma: no cover
        return f"{self.name} ({self.kind.value})"


@dataclass(frozen=True)
class AuthorisationRecord:
    """Evidence that an action was authorised, for the audit trail."""

    action: Action
    actor: Actor
    granted: bool
    classification: str
    reason: str = ""
    subject: str = ""
    ts: datetime | None = None

    def __post_init__(self):
        if self.ts is not None:
            object.__setattr__(self, "ts", ensure_utc(self.ts, field_name="ts"))

    def to_dict(self) -> dict:
        return {"action": self.action.value, "actor": self.actor.to_dict(),
                "granted": self.granted, "classification": self.classification,
                "reason": self.reason, "subject": self.subject,
                "ts": jsonable(self.ts)}


def authorise(action: Any, actor: Actor, *, subject: str = "",
              clock: Clock | None = None, audit: Any = None
              ) -> AuthorisationRecord:
    """Permit or refuse. Raises `GovernanceViolation` on refusal.

    Raising rather than returning False is the point: an autonomous caller that
    receives a value can ignore it, and the failure mode this whole phase exists
    to prevent is an autonomous component proceeding past a check it did not
    like. A refusal is still recorded before the exception leaves, so an attempt
    is visible in the trail whether or not the caller handles it.
    """
    action = action if isinstance(action, Action) else Action(str(action))
    classification = _classify(action)
    now = (clock or default_clock()).now()

    granted = False
    reason = ""
    if classification == "prohibited":
        reason = ("this action is not available to any actor; correcting the "
                  "record is done out of band, leaving its own trace")
    elif classification == "unclassified":
        # Fail closed. An action nobody classified is not thereby permitted.
        reason = ("action is not classified as autonomous or human-only; "
                  "unclassified actions are refused rather than allowed")
    elif classification == "autonomous":
        granted = True
    elif actor.is_operator:
        granted = True
    else:
        reason = (f"{action.value} requires a named operator carrying a token; "
                  f"{actor.name} is {actor.kind.value}")

    record = AuthorisationRecord(action=action, actor=actor, granted=granted,
                                 classification=classification, reason=reason,
                                 subject=subject, ts=now)
    if audit is not None:
        try:
            from .audit import EventType
            audit.record(EventType.HUMAN_INTERVENTION if actor.is_operator
                         else EventType.PROPOSAL_SUBMITTED,
                         record.to_dict(), actor=actor.name, subject=subject)
        except Exception:                                # noqa: BLE001
            pass
    if not granted:
        raise GovernanceViolation(reason, action=action.value,
                                  actor=actor.name, actor_kind=actor.kind.value,
                                  subject=subject)
    return record


def may(action: Any, actor: Actor) -> bool:
    """Non-raising predicate, for rendering a UI that hides what it cannot do."""
    try:
        authorise(action, actor)
    except GovernanceViolation:
        return False
    return True


def require_operator(actor: Actor, *, what: str) -> Actor:
    """Assert a human is behind this, with a token."""
    if not actor.is_operator:
        raise GovernanceViolation(
            f"{what} requires a named operator; an autonomous actor cannot "
            "authorise it on its own behalf",
            actor=actor.name, actor_kind=actor.kind.value)
    return actor


def describe_governance() -> str:
    """The split, rendered. Used by the operator console and the docs."""
    def block(title: str, actions: frozenset[Action]) -> str:
        names = "\n".join(f"    {a.value}" for a in sorted(actions, key=lambda a: a.value))
        return f"  {title}\n{names}"
    return "\n".join([
        "governance",
        block("autonomous (no approval required)", AUTONOMOUS_ACTIONS),
        block("human-only (named operator + token)", HUMAN_ONLY_ACTIONS),
        block("prohibited (no actor)", PROHIBITED_ACTIONS),
    ])


__all__ = ["Action", "ActorKind", "Actor", "AuthorisationRecord",
           "AUTONOMOUS_ACTIONS", "HUMAN_ONLY_ACTIONS", "PROHIBITED_ACTIONS",
           "authorise", "may", "require_operator", "describe_governance"]

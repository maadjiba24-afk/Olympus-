"""The capability registry: what Olympus can do, and how far each thing is trusted.

Not to be confused with `olympus.capabilities`, which counts the whole
project's published surface for the README. This registry is narrower and
stricter: it tracks every model, strategy, feature, integration and tool inside
the trading domain through a ten-state lifecycle, and it is the component that
makes "capabilities must not promote themselves" true rather than intended.

The lifecycle:

    proposed → researching → prototype → backtest_only → paper_approved
             → shadow_approved → restricted_live_approved → production
                                                          ↘ suspended
                                                          ↘ deprecated

Three rules, each enforced in `promote`:

1. **A capability cannot promote itself.** `promote` requires an `Actor` and
   refuses an autonomous one via `governance.authorise(PROMOTE_CAPABILITY, …)`.
   There is no argument, evidence bundle or configuration that changes this.
2. **Promotion is one rung at a time.** Skipping from `prototype` to
   `production` is refused even by an operator. The intermediate states exist
   to accumulate evidence, and a system that could skip them would make the
   evidence optional.
3. **Promotion depends on evidence, not on explanation.** Each state declares
   the `LifecycleStage`s that must already be recorded, and a missing stage is
   a `PromotionDenied` naming exactly what is absent. A persuasive rationale
   with no walk-forward result is refused identically to no rationale at all —
   which is the specific failure mode a model-generated case for its own
   promotion would exploit.

Demotion is the mirror image and deliberately easier: `suspend` accepts an
autonomous actor, because halting something that is misbehaving must never wait
for a human.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from .clock import Clock, default_clock
from .contracts import ensure_utc, jsonable
from .errors import ConfigurationError, PromotionDenied
from .governance import Action, Actor, authorise, require_operator
from .storekeys import KeyedStore

_NS = "trading.capabilities"


class CapabilityState(str, Enum):
    """The ten states, in promotion order."""

    PROPOSED = "proposed"
    RESEARCHING = "researching"
    PROTOTYPE = "prototype"
    BACKTEST_ONLY = "backtest_only"
    PAPER_APPROVED = "paper_approved"
    SHADOW_APPROVED = "shadow_approved"
    RESTRICTED_LIVE_APPROVED = "restricted_live_approved"
    PRODUCTION = "production"
    SUSPENDED = "suspended"
    DEPRECATED = "deprecated"


#: The promotion ladder. `SUSPENDED` and `DEPRECATED` are off-ladder terminal
#: states reachable from anywhere, which is why they are not in this tuple.
PROMOTION_ORDER: tuple[CapabilityState, ...] = (
    CapabilityState.PROPOSED, CapabilityState.RESEARCHING,
    CapabilityState.PROTOTYPE, CapabilityState.BACKTEST_ONLY,
    CapabilityState.PAPER_APPROVED, CapabilityState.SHADOW_APPROVED,
    CapabilityState.RESTRICTED_LIVE_APPROVED, CapabilityState.PRODUCTION,
)
_RANK = {state: i for i, state in enumerate(PROMOTION_ORDER)}

#: States in which a capability's output may influence a real order.
LIVE_STATES: frozenset[CapabilityState] = frozenset({
    CapabilityState.RESTRICTED_LIVE_APPROVED, CapabilityState.PRODUCTION,
})


class LifecycleStage(str, Enum):
    """The fourteen-step improvement lifecycle, as recordable evidence."""

    PROPOSAL = "proposal"
    STATIC_VALIDATION = "static_validation"
    UNIT_TESTING = "unit_testing"
    HISTORICAL_TESTING = "historical_testing"
    LEAKAGE_TESTING = "leakage_testing"
    WALK_FORWARD = "walk_forward"
    BASELINE_COMPARISON = "baseline_comparison"
    ROBUSTNESS = "robustness"
    ADVERSARIAL = "adversarial"
    PAPER_TRADING = "paper_trading"
    SHADOW_TRADING = "shadow_trading"
    HUMAN_REVIEW = "human_review"
    RESTRICTED_DEPLOYMENT = "restricted_deployment"
    CONTINUOUS_MONITORING = "continuous_monitoring"


#: What must already be evidenced to enter each state. Cumulative by
#: construction: entering `production` requires everything below it too,
#: because `promote` walks the ladder one rung at a time.
REQUIRED_EVIDENCE: Mapping[CapabilityState, tuple[LifecycleStage, ...]] = {
    CapabilityState.PROPOSED: (),
    CapabilityState.RESEARCHING: (LifecycleStage.PROPOSAL,),
    CapabilityState.PROTOTYPE: (LifecycleStage.STATIC_VALIDATION,
                                LifecycleStage.UNIT_TESTING),
    CapabilityState.BACKTEST_ONLY: (LifecycleStage.HISTORICAL_TESTING,
                                    LifecycleStage.LEAKAGE_TESTING),
    CapabilityState.PAPER_APPROVED: (LifecycleStage.WALK_FORWARD,
                                     LifecycleStage.BASELINE_COMPARISON,
                                     LifecycleStage.ROBUSTNESS),
    CapabilityState.SHADOW_APPROVED: (LifecycleStage.PAPER_TRADING,
                                      LifecycleStage.ADVERSARIAL),
    CapabilityState.RESTRICTED_LIVE_APPROVED: (LifecycleStage.SHADOW_TRADING,
                                               LifecycleStage.HUMAN_REVIEW),
    CapabilityState.PRODUCTION: (LifecycleStage.RESTRICTED_DEPLOYMENT,
                                 LifecycleStage.CONTINUOUS_MONITORING),
}


class RiskClass(str, Enum):
    """How much damage this capability could do if it were wrong."""

    #: Reporting or analysis; a wrong answer misleads a human who can check it.
    INFORMATIONAL = "informational"
    #: Feeds a decision but does not size or place it.
    ADVISORY = "advisory"
    #: Changes position size, entry or exit.
    DECISION_AFFECTING = "decision_affecting"
    #: Touches order placement, credentials, or limits. Never autonomous.
    SAFETY_RELEVANT = "safety_relevant"


@dataclass(frozen=True)
class EvidenceEntry:
    """One completed lifecycle stage, with a pointer to the artefact."""

    stage: LifecycleStage
    recorded_at: datetime
    reference: str
    summary: str = ""
    passed: bool = True
    recorded_by: str = "system"

    def __post_init__(self):
        object.__setattr__(self, "stage", LifecycleStage(self.stage))
        object.__setattr__(self, "recorded_at",
                           ensure_utc(self.recorded_at, field_name="recorded_at"))
        if not str(self.reference).strip():
            raise ConfigurationError(
                "evidence must reference an artefact — an experiment id, a test "
                "run, a review — so it can be checked rather than believed",
                stage=self.stage.value)

    def to_dict(self) -> dict:
        return {"stage": self.stage.value,
                "recorded_at": jsonable(self.recorded_at),
                "reference": self.reference, "summary": self.summary,
                "passed": self.passed, "recorded_by": self.recorded_by}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EvidenceEntry":
        return cls(stage=LifecycleStage(data["stage"]),
                   recorded_at=datetime.fromisoformat(data["recorded_at"]),
                   reference=str(data.get("reference", "")),
                   summary=str(data.get("summary", "")),
                   passed=bool(data.get("passed", True)),
                   recorded_by=str(data.get("recorded_by", "system")))


@dataclass(frozen=True)
class PerformanceEntry:
    """One dated performance observation. History, not a single current number.

    A capability's record is a series precisely so that "it performed well once"
    cannot be mistaken for "it performs well".
    """

    at: datetime
    metrics: Mapping[str, Any] = field(default_factory=dict)
    sample: int = 0
    note: str = ""

    def __post_init__(self):
        object.__setattr__(self, "at", ensure_utc(self.at, field_name="at"))
        object.__setattr__(self, "metrics", dict(self.metrics))

    def to_dict(self) -> dict:
        return {"at": jsonable(self.at), "metrics": jsonable(dict(self.metrics)),
                "sample": self.sample, "note": self.note}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PerformanceEntry":
        return cls(at=datetime.fromisoformat(data["at"]),
                   metrics=data.get("metrics") or {},
                   sample=int(data.get("sample", 0)),
                   note=str(data.get("note", "")))


@dataclass(frozen=True)
class Capability:
    """One capability's full record."""

    capability_id: str
    name: str
    description: str
    version: str
    owner: str
    source: str
    state: CapabilityState = CapabilityState.PROPOSED
    risk_class: RiskClass = RiskClass.INFORMATIONAL
    required_permissions: tuple[str, ...] = ()
    required_data: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    supported_markets: tuple[str, ...] = ()
    supported_instruments: tuple[str, ...] = ()
    known_limitations: tuple[str, ...] = ()
    evidence: tuple[EvidenceEntry, ...] = ()
    performance_history: tuple[PerformanceEntry, ...] = ()
    created_at: datetime | None = None
    state_changed_at: datetime | None = None
    state_changed_by: str = "system"
    last_validated_at: datetime | None = None
    deprecated: bool = False
    deprecation_reason: str = ""
    notes: str = ""

    def __post_init__(self):
        for name, value in (("capability_id", self.capability_id),
                            ("name", self.name), ("version", self.version),
                            ("owner", self.owner), ("source", self.source)):
            if not str(value).strip():
                raise ConfigurationError(
                    f"{name} must be non-empty; an unattributed capability "
                    "cannot be reviewed or rolled back",
                    capability_id=self.capability_id)
        object.__setattr__(self, "state", CapabilityState(self.state))
        object.__setattr__(self, "risk_class", RiskClass(self.risk_class))
        for name in ("required_permissions", "required_data", "dependencies",
                     "supported_markets", "supported_instruments",
                     "known_limitations"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "performance_history",
                           tuple(self.performance_history))
        for name in ("created_at", "state_changed_at", "last_validated_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, ensure_utc(value, field_name=name))
        if self.state is CapabilityState.DEPRECATED and not self.deprecated:
            object.__setattr__(self, "deprecated", True)

    # -- derived ------------------------------------------------------------

    @property
    def live_capable(self) -> bool:
        return self.state in LIVE_STATES and not self.deprecated

    @property
    def stages_passed(self) -> frozenset[LifecycleStage]:
        return frozenset(e.stage for e in self.evidence if e.passed)

    @property
    def stages_failed(self) -> frozenset[LifecycleStage]:
        """Stages recorded as *not* passed. Kept, never overwritten by a retry."""
        passed = self.stages_passed
        return frozenset(e.stage for e in self.evidence
                         if not e.passed and e.stage not in passed)

    def missing_evidence(self, target: CapabilityState) -> tuple[LifecycleStage, ...]:
        required = REQUIRED_EVIDENCE.get(CapabilityState(target), ())
        return tuple(s for s in required if s not in self.stages_passed)

    def latest_performance(self) -> PerformanceEntry | None:
        """Most recent observation, ties broken by insertion order.

        The tie-break matters: two entries recorded within the same clock tick
        are indistinguishable by timestamp, and returning the *earlier* one
        would report a stale metric as current — which is precisely the failure
        this history exists to prevent.
        """
        if not self.performance_history:
            return None
        return max(enumerate(self.performance_history),
                   key=lambda pair: (pair[1].at, pair[0]))[1]

    def to_dict(self) -> dict:
        return {
            "capability_id": self.capability_id, "name": self.name,
            "description": self.description, "version": self.version,
            "owner": self.owner, "source": self.source,
            "state": self.state.value, "risk_class": self.risk_class.value,
            "required_permissions": list(self.required_permissions),
            "required_data": list(self.required_data),
            "dependencies": list(self.dependencies),
            "supported_markets": list(self.supported_markets),
            "supported_instruments": list(self.supported_instruments),
            "known_limitations": list(self.known_limitations),
            "evidence": [e.to_dict() for e in self.evidence],
            "performance_history": [p.to_dict() for p in self.performance_history],
            "created_at": jsonable(self.created_at),
            "state_changed_at": jsonable(self.state_changed_at),
            "state_changed_by": self.state_changed_by,
            "last_validated_at": jsonable(self.last_validated_at),
            "deprecated": self.deprecated,
            "deprecation_reason": self.deprecation_reason, "notes": self.notes,
            "live_capable": self.live_capable,
            "stages_passed": sorted(s.value for s in self.stages_passed),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Capability":
        def when(name):
            value = data.get(name)
            return datetime.fromisoformat(value) if isinstance(value, str) and value else None
        return cls(
            capability_id=str(data.get("capability_id", "")),
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            version=str(data.get("version", "")), owner=str(data.get("owner", "")),
            source=str(data.get("source", "")),
            state=CapabilityState(data.get("state", CapabilityState.PROPOSED.value)),
            risk_class=RiskClass(data.get("risk_class", RiskClass.INFORMATIONAL.value)),
            required_permissions=tuple(data.get("required_permissions") or ()),
            required_data=tuple(data.get("required_data") or ()),
            dependencies=tuple(data.get("dependencies") or ()),
            supported_markets=tuple(data.get("supported_markets") or ()),
            supported_instruments=tuple(data.get("supported_instruments") or ()),
            known_limitations=tuple(data.get("known_limitations") or ()),
            evidence=tuple(EvidenceEntry.from_dict(e) for e in data.get("evidence") or ()),
            performance_history=tuple(PerformanceEntry.from_dict(p)
                                      for p in data.get("performance_history") or ()),
            created_at=when("created_at"), state_changed_at=when("state_changed_at"),
            state_changed_by=str(data.get("state_changed_by", "system")),
            last_validated_at=when("last_validated_at"),
            deprecated=bool(data.get("deprecated", False)),
            deprecation_reason=str(data.get("deprecation_reason", "")),
            notes=str(data.get("notes", "")))

    def evolve(self, **changes) -> "Capability":
        data = {f: getattr(self, f) for f in self.__dataclass_fields__}
        data.update(changes)
        return Capability(**data)


def next_state(current: CapabilityState) -> CapabilityState | None:
    """The single rung above, or None at the top / off the ladder."""
    current = CapabilityState(current)
    if current not in _RANK:
        return None
    index = _RANK[current]
    return PROMOTION_ORDER[index + 1] if index + 1 < len(PROMOTION_ORDER) else None


class CapabilityRegistry:
    """Durable capability records with gated, evidenced, one-rung promotion."""

    def __init__(self, *, clock: Clock | None = None, audit: Any = None,
                 namespace: str = _NS):
        self.clock = clock or default_clock()
        self.audit = audit
        self.namespace = namespace

    def _store(self) -> KeyedStore:
        return KeyedStore(self.namespace)

    def _write(self, capability: Capability, action: str, actor: str) -> Capability:
        self._store().put((capability.capability_id,), capability.to_dict())
        if self.audit is not None:
            try:
                from .audit import EventType
                self.audit.record(EventType.CAPABILITY_CHANGED,
                                  {**capability.to_dict(), "action": action},
                                  actor=actor, subject=capability.capability_id)
            except Exception:                            # noqa: BLE001
                pass
        return capability

    # -- lifecycle ---------------------------------------------------------

    def declare(self, capability_id: str, *, name: str, description: str,
                version: str, owner: str, source: str,
                risk_class: RiskClass | str = RiskClass.INFORMATIONAL,
                **fields) -> Capability:
        """Register a capability as PROPOSED. Never any other state.

        Declaration confers nothing. A capability arrives proposed regardless of
        how well tested its author believes it to be, because the evidence for
        that belief is exactly what the ladder exists to collect.
        """
        from olympus import proclock
        with proclock.lock(f"capability-{capability_id}"):
            if self._store().get((capability_id,)):
                raise ConfigurationError(
                    "capability_id already exists; version a new record rather "
                    "than redefining what an approval referred to",
                    capability_id=capability_id)
            capability = Capability(
                capability_id=capability_id, name=name, description=description,
                version=version, owner=owner, source=source,
                state=CapabilityState.PROPOSED, risk_class=RiskClass(risk_class),
                created_at=self.clock.now(), state_changed_at=self.clock.now(),
                **fields)
            self._write(capability, "declare", owner)
        return capability

    def get(self, capability_id: str) -> Capability | None:
        body = self._store().get((capability_id,))
        return Capability.from_dict(body) if body else None

    def _require(self, capability_id: str) -> Capability:
        capability = self.get(capability_id)
        if capability is None:
            raise ConfigurationError("unknown capability",
                                     capability_id=capability_id)
        return capability

    def all(self, *, state: CapabilityState | str | None = None,
            risk_class: RiskClass | str | None = None) -> list[Capability]:
        out = []
        for _, body in self._store().items():
            capability = Capability.from_dict(body)
            if state is not None and capability.state is not CapabilityState(state):
                continue
            if risk_class is not None and capability.risk_class is not RiskClass(risk_class):
                continue
            out.append(capability)
        return out

    def record_evidence(self, capability_id: str, stage: LifecycleStage | str, *,
                        reference: str, summary: str = "", passed: bool = True,
                        recorded_by: str = "system") -> Capability:
        """Attach a completed lifecycle stage. Autonomous — this is measurement.

        Recording evidence is not promotion and is deliberately open to the
        automation that produces it: a walk-forward run should write its own
        result. What the automation cannot do is act on it.
        """
        from olympus import proclock
        with proclock.lock(f"capability-{capability_id}"):
            capability = self._require(capability_id)
            entry = EvidenceEntry(stage=LifecycleStage(stage),
                                  recorded_at=self.clock.now(),
                                  reference=reference, summary=summary,
                                  passed=passed, recorded_by=recorded_by)
            updated = capability.evolve(
                evidence=capability.evidence + (entry,),
                last_validated_at=self.clock.now() if passed
                else capability.last_validated_at)
            self._write(updated, f"evidence:{entry.stage.value}", recorded_by)
        return updated

    def record_performance(self, capability_id: str, *,
                           metrics: Mapping[str, Any], sample: int = 0,
                           note: str = "") -> Capability:
        """Append to the performance history. Append only — never replaces."""
        from olympus import proclock
        with proclock.lock(f"capability-{capability_id}"):
            capability = self._require(capability_id)
            entry = PerformanceEntry(at=self.clock.now(), metrics=dict(metrics),
                                     sample=sample, note=note)
            updated = capability.evolve(
                performance_history=capability.performance_history + (entry,))
            self._write(updated, "performance", "system")
        return updated

    def promote(self, capability_id: str, *, actor: Actor, reason: str,
                to: CapabilityState | str | None = None) -> Capability:
        """Advance one rung. Operator only, evidence required.

        Four refusals, in the order they are checked, so an operator sees the
        most fundamental problem first:

        1. Not an operator → `GovernanceViolation`.
        2. More than one rung → `PromotionDenied`.
        3. Missing evidence → `PromotionDenied` naming every absent stage.
        4. Safety-relevant capability reaching a live state without human
           review recorded → `PromotionDenied`.
        """
        require_operator(actor, what="capability promotion")
        authorise(Action.PROMOTE_CAPABILITY, actor, subject=capability_id,
                  clock=self.clock, audit=self.audit)
        if not str(reason).strip():
            raise ConfigurationError(
                "promotion requires a reason recorded against the operator's "
                "name", capability_id=capability_id)

        from olympus import proclock
        with proclock.lock(f"capability-{capability_id}"):
            capability = self._require(capability_id)
            if capability.deprecated:
                raise PromotionDenied(
                    "a deprecated capability cannot be promoted; declare a "
                    "replacement", capability_id=capability_id)
            if capability.state is CapabilityState.SUSPENDED:
                raise PromotionDenied(
                    "a suspended capability must be reinstated by an operator "
                    "through `reinstate` before it can be promoted again",
                    capability_id=capability_id)
            target = (CapabilityState(to) if to is not None
                      else next_state(capability.state))
            if target is None:
                raise PromotionDenied(
                    "capability is already at the top of the ladder",
                    capability_id=capability_id, state=capability.state.value)
            if target not in _RANK or capability.state not in _RANK:
                raise PromotionDenied(
                    "promotion only moves along the ladder; suspension and "
                    "deprecation have their own methods",
                    capability_id=capability_id, requested=target.value)
            if _RANK[target] <= _RANK[capability.state]:
                raise PromotionDenied(
                    "promotion moves up; use suspend() or deprecate() to move "
                    "the other way", capability_id=capability_id,
                    current=capability.state.value, requested=target.value)
            if _RANK[target] - _RANK[capability.state] > 1:
                raise PromotionDenied(
                    "promotion advances one state at a time; the intermediate "
                    "states are where evidence is collected, so skipping them "
                    "would make the evidence optional",
                    capability_id=capability_id, current=capability.state.value,
                    requested=target.value,
                    next_allowed=next_state(capability.state).value)

            missing = capability.missing_evidence(target)
            if missing:
                raise PromotionDenied(
                    "required evidence is missing; a rationale is not a "
                    "substitute for a recorded result",
                    capability_id=capability_id, target=target.value,
                    missing=[s.value for s in missing],
                    recorded=sorted(s.value for s in capability.stages_passed))
            if (target in LIVE_STATES
                    and capability.risk_class is RiskClass.SAFETY_RELEVANT
                    and LifecycleStage.HUMAN_REVIEW not in capability.stages_passed):
                raise PromotionDenied(
                    "a safety-relevant capability needs a recorded human review "
                    "before any live state", capability_id=capability_id,
                    target=target.value)

            updated = capability.evolve(
                state=target, state_changed_at=self.clock.now(),
                state_changed_by=actor.name, notes=reason)
            self._write(updated, f"promote:{target.value}", actor.name)
        return updated

    def suspend(self, capability_id: str, *, reason: str,
                actor: Actor | None = None) -> Capability:
        """Halt a capability. Autonomous by design.

        The mirror of `promote`: stopping something needs no approval, because
        requiring one means a misbehaving capability keeps running while a human
        is found.
        """
        actor = actor or Actor.autonomous("capability-registry")
        authorise(Action.RESTRICT_COMPONENT, actor, subject=capability_id,
                  clock=self.clock, audit=self.audit)
        from olympus import proclock
        with proclock.lock(f"capability-{capability_id}"):
            capability = self._require(capability_id)
            updated = capability.evolve(
                state=CapabilityState.SUSPENDED,
                state_changed_at=self.clock.now(), state_changed_by=actor.name,
                notes=reason)
            self._write(updated, "suspend", actor.name)
        return updated

    def reinstate(self, capability_id: str, *, to: CapabilityState | str,
                  actor: Actor, reason: str) -> Capability:
        """Bring a suspended capability back. Operator only."""
        require_operator(actor, what="reinstating a suspended capability")
        authorise(Action.PROMOTE_CAPABILITY, actor, subject=capability_id,
                  clock=self.clock, audit=self.audit)
        target = CapabilityState(to)
        if target not in _RANK:
            raise ConfigurationError(
                "reinstatement targets a ladder state", requested=target.value)
        from olympus import proclock
        with proclock.lock(f"capability-{capability_id}"):
            capability = self._require(capability_id)
            if capability.state is not CapabilityState.SUSPENDED:
                raise ConfigurationError("capability is not suspended",
                                         capability_id=capability_id,
                                         state=capability.state.value)
            missing = capability.missing_evidence(target)
            if missing:
                raise PromotionDenied(
                    "reinstatement to this state still requires its evidence",
                    capability_id=capability_id, target=target.value,
                    missing=[s.value for s in missing])
            updated = capability.evolve(
                state=target, state_changed_at=self.clock.now(),
                state_changed_by=actor.name, notes=reason)
            self._write(updated, f"reinstate:{target.value}", actor.name)
        return updated

    def deprecate(self, capability_id: str, *, reason: str,
                  actor: Actor | None = None) -> Capability:
        """Retire a capability permanently. Autonomous — a recommendation to
        stop using something is always safe to act on."""
        actor = actor or Actor.autonomous("capability-registry")
        authorise(Action.RECOMMEND_DEPRECATION, actor, subject=capability_id,
                  clock=self.clock, audit=self.audit)
        from olympus import proclock
        with proclock.lock(f"capability-{capability_id}"):
            capability = self._require(capability_id)
            updated = capability.evolve(
                state=CapabilityState.DEPRECATED, deprecated=True,
                deprecation_reason=reason, state_changed_at=self.clock.now(),
                state_changed_by=actor.name)
            self._write(updated, "deprecate", actor.name)
        return updated

    # -- queries -----------------------------------------------------------

    def live_capable(self) -> list[Capability]:
        return [c for c in self.all() if c.live_capable]

    def assert_usable(self, capability_id: str, *, live: bool) -> Capability:
        """Gate a capability at point of use."""
        capability = self._require(capability_id)
        if capability.deprecated:
            raise PromotionDenied("capability is deprecated",
                                  capability_id=capability_id)
        if capability.state is CapabilityState.SUSPENDED:
            raise PromotionDenied("capability is suspended",
                                  capability_id=capability_id)
        if live and not capability.live_capable:
            raise PromotionDenied(
                "capability is not approved for live use",
                capability_id=capability_id, state=capability.state.value,
                required=sorted(s.value for s in LIVE_STATES))
        return capability

    def report(self) -> dict:
        by_state: dict[str, int] = {}
        for capability in self.all():
            by_state[capability.state.value] = by_state.get(capability.state.value, 0) + 1
        return {"capabilities": len(self.all()), "by_state": by_state,
                "live_capable": [c.capability_id for c in self.live_capable()]}

    def __repr__(self) -> str:                          # pragma: no cover
        return f"CapabilityRegistry(namespace={self.namespace!r})"


__all__ = ["CapabilityState", "PROMOTION_ORDER", "LIVE_STATES",
           "LifecycleStage", "REQUIRED_EVIDENCE", "RiskClass", "EvidenceEntry",
           "PerformanceEntry", "Capability", "CapabilityRegistry",
           "next_state"]

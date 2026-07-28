"""Deployments that can be undone, and the triggers that undo them.

A promotion nobody can reverse is not a promotion, it is a one-way door. This
module makes reversal a precondition of deployment: `deploy()` refuses to
record a deployment that has no reproducible configuration, no pinned
dependency versions, no stored evaluation evidence and — except for the very
first — no previous stable version to fall back to. The check happens before
anything is active, because the moment to discover there is nothing to roll
back to is not during an incident.

Direction of travel decides who may act:

* **Rolling forward requires an operator.** A new version reaching production
  is a human decision with a human's name on it.
* **Rolling back is autonomous**, and deliberately so — but only to a version
  a human previously deployed. Olympus can always retreat to a state a person
  already signed off on, never advance to one they did not. That asymmetry is
  what lets automatic rollback be safe: the worst case of a spurious trigger is
  returning to a known-good configuration, which is a cost worth paying against
  the alternative of a degrading component running until someone wakes up.

Rollback is not finished when the previous version is active. `reconcile_after`
produces the checklist of things that are now inconsistent — open orders placed
by the old version, positions it opened, knowledge records it wrote — because a
rollback that silently leaves those behind trades a software problem for a
position problem.

Every record carries a `signature`: a content hash over the canonical
deployment. It is not a cryptographic authorisation — the audit ledger's Ed25519
chain does that — it is a tamper-evident identity, so "which configuration was
actually live in March" has an answer that cannot drift.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from .clock import Clock, default_clock
from .contracts import ensure_utc, jsonable
from .errors import ConfigurationError, PromotionDenied
from .governance import Action, Actor, authorise, require_operator
from .storekeys import KeyedStore

_NS = "trading.deployments"


class DeploymentState(str, Enum):
    ACTIVE = "active"
    #: Replaced by a later deployment in the normal way.
    SUPERSEDED = "superseded"
    #: Replaced because it was rolled back. Kept distinct from SUPERSEDED: the
    #: difference between "we moved on" and "we retreated" is the whole story.
    ROLLED_BACK = "rolled_back"


class RollbackTrigger(str, Enum):
    """The nine conditions that cause an automatic retreat."""

    EXCESSIVE_DRAWDOWN = "excessive_drawdown"
    FORECAST_DEGRADATION = "forecast_degradation"
    ABNORMAL_ORDER_BEHAVIOUR = "abnormal_order_behaviour"
    DATA_QUALITY_DETERIORATION = "data_quality_deterioration"
    EXECUTION_FAILURES = "execution_failures"
    AUDIT_FAILURE = "audit_failure"
    RECONCILIATION_FAILURE = "reconciliation_failure"
    UNEXPECTED_CAPABILITY_ACCESS = "unexpected_capability_access"
    #: Live behaviour diverging from what paper or shadow predicted. The one
    #: that catches a model whose backtest was subtly unrealistic.
    PAPER_DIVERGENCE = "paper_divergence"
    MANUAL = "manual"


@dataclass(frozen=True)
class TriggerThresholds:
    """When each trigger fires. Explicit so a rollback can cite its number."""

    max_drawdown: float = 0.15
    max_forecast_error_increase: float = 0.50
    max_order_reject_rate: float = 0.20
    min_data_completeness: float = 0.95
    max_execution_failure_rate: float = 0.10
    #: Live-versus-paper mean return divergence, in units of paper volatility.
    max_paper_divergence_in_vol: float = 2.0
    #: Any audit or reconciliation failure at all. These are binary: there is
    #: no acceptable rate of an unverifiable audit chain.
    audit_failures_tolerated: int = 0
    reconciliation_failures_tolerated: int = 0

    def to_dict(self) -> dict:
        return {"max_drawdown": self.max_drawdown,
                "max_forecast_error_increase": self.max_forecast_error_increase,
                "max_order_reject_rate": self.max_order_reject_rate,
                "min_data_completeness": self.min_data_completeness,
                "max_execution_failure_rate": self.max_execution_failure_rate,
                "max_paper_divergence_in_vol": self.max_paper_divergence_in_vol,
                "audit_failures_tolerated": self.audit_failures_tolerated,
                "reconciliation_failures_tolerated":
                    self.reconciliation_failures_tolerated}


@dataclass(frozen=True)
class FiredTrigger:
    """One trigger that fired, with the measurement that fired it."""

    trigger: RollbackTrigger
    measured: float | None
    threshold: float | None
    detail: str

    def __post_init__(self):
        object.__setattr__(self, "trigger", RollbackTrigger(self.trigger))

    def to_dict(self) -> dict:
        return {"trigger": self.trigger.value, "measured": self.measured,
                "threshold": self.threshold, "detail": self.detail}

    def __str__(self) -> str:                            # pragma: no cover
        return f"{self.trigger.value}: {self.detail}"


def evaluate_triggers(metrics: Mapping[str, Any], *,
                      thresholds: TriggerThresholds | None = None
                      ) -> list[FiredTrigger]:
    """Which rollback conditions the current measurements satisfy.

    Missing measurements do **not** fire a trigger — an unmeasured metric is not
    evidence of a problem — but they are not treated as passing either: the
    caller receives the list of what was unmeasurable via
    `unmeasured_triggers()`, so a rollback decision made on partial data knows
    it was partial.
    """
    thresholds = thresholds or TriggerThresholds()
    fired: list[FiredTrigger] = []

    def number(name: str) -> float | None:
        value = metrics.get(name)
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    drawdown = number("max_drawdown")
    if drawdown is not None and drawdown > thresholds.max_drawdown:
        fired.append(FiredTrigger(
            RollbackTrigger.EXCESSIVE_DRAWDOWN, drawdown, thresholds.max_drawdown,
            f"drawdown {drawdown:.2%} exceeds {thresholds.max_drawdown:.2%}"))

    error_increase = number("forecast_error_increase")
    if (error_increase is not None
            and error_increase > thresholds.max_forecast_error_increase):
        fired.append(FiredTrigger(
            RollbackTrigger.FORECAST_DEGRADATION, error_increase,
            thresholds.max_forecast_error_increase,
            f"forecast error rose {error_increase:.1%} against the deployment "
            "baseline"))

    reject_rate = number("order_reject_rate")
    if reject_rate is not None and reject_rate > thresholds.max_order_reject_rate:
        fired.append(FiredTrigger(
            RollbackTrigger.ABNORMAL_ORDER_BEHAVIOUR, reject_rate,
            thresholds.max_order_reject_rate,
            f"{reject_rate:.1%} of orders rejected"))

    completeness = number("data_completeness")
    if completeness is not None and completeness < thresholds.min_data_completeness:
        fired.append(FiredTrigger(
            RollbackTrigger.DATA_QUALITY_DETERIORATION, completeness,
            thresholds.min_data_completeness,
            f"data completeness {completeness:.2%} below "
            f"{thresholds.min_data_completeness:.2%}"))

    failure_rate = number("execution_failure_rate")
    if (failure_rate is not None
            and failure_rate > thresholds.max_execution_failure_rate):
        fired.append(FiredTrigger(
            RollbackTrigger.EXECUTION_FAILURES, failure_rate,
            thresholds.max_execution_failure_rate,
            f"{failure_rate:.1%} of executions failed"))

    audit_failures = number("audit_failures")
    if (audit_failures is not None
            and audit_failures > thresholds.audit_failures_tolerated):
        fired.append(FiredTrigger(
            RollbackTrigger.AUDIT_FAILURE, audit_failures,
            float(thresholds.audit_failures_tolerated),
            f"{int(audit_failures)} audit verification failure(s); an "
            "unverifiable trail has no acceptable rate"))

    recon_failures = number("reconciliation_failures")
    if (recon_failures is not None
            and recon_failures > thresholds.reconciliation_failures_tolerated):
        fired.append(FiredTrigger(
            RollbackTrigger.RECONCILIATION_FAILURE, recon_failures,
            float(thresholds.reconciliation_failures_tolerated),
            f"{int(recon_failures)} reconciliation break(s)"))

    if metrics.get("unexpected_capability_access"):
        fired.append(FiredTrigger(
            RollbackTrigger.UNEXPECTED_CAPABILITY_ACCESS, None, None,
            "a component requested a capability it is not registered to use: "
            + str(metrics.get("unexpected_capability_access"))))

    divergence = number("paper_divergence_in_vol")
    if (divergence is not None
            and abs(divergence) > thresholds.max_paper_divergence_in_vol):
        fired.append(FiredTrigger(
            RollbackTrigger.PAPER_DIVERGENCE, divergence,
            thresholds.max_paper_divergence_in_vol,
            f"live performance diverges from paper by {divergence:.1f}σ, so "
            "the pre-deployment evidence no longer describes this component"))

    return fired


#: Metric keys `evaluate_triggers` reads. Exposed so a caller can report what
#: it failed to supply rather than discovering it during an incident.
TRIGGER_METRICS: tuple[str, ...] = (
    "max_drawdown", "forecast_error_increase", "order_reject_rate",
    "data_completeness", "execution_failure_rate", "audit_failures",
    "reconciliation_failures", "unexpected_capability_access",
    "paper_divergence_in_vol",
)


def unmeasured_triggers(metrics: Mapping[str, Any]) -> tuple[str, ...]:
    """Which trigger inputs were not supplied."""
    return tuple(name for name in TRIGGER_METRICS if metrics.get(name) is None)


@dataclass(frozen=True)
class DeploymentRecord:
    """One deployed version, with everything needed to reproduce or reverse it."""

    deployment_id: str
    capability_id: str
    version: str
    deployed_at: datetime
    deployed_by: str
    #: Complete, reproducible configuration. Not a reference to one — the
    #: values, so a rollback does not depend on a config file that also changed.
    config: Mapping[str, Any] = field(default_factory=dict)
    #: Pinned versions of everything this depends on.
    dependency_versions: Mapping[str, str] = field(default_factory=dict)
    #: Pointers to the evidence that justified the deployment.
    evidence_refs: tuple[str, ...] = ()
    previous_deployment_id: str = ""
    rollback_procedure: str = ""
    state: DeploymentState = DeploymentState.ACTIVE
    superseded_at: datetime | None = None
    rollback_reason: str = ""
    triggers_fired: tuple[FiredTrigger, ...] = ()
    notes: str = ""

    def __post_init__(self):
        for name in ("deployment_id", "capability_id", "version", "deployed_by"):
            if not str(getattr(self, name)).strip():
                raise ConfigurationError(f"{name} must be non-empty",
                                         deployment_id=self.deployment_id)
        object.__setattr__(self, "state", DeploymentState(self.state))
        object.__setattr__(self, "deployed_at",
                           ensure_utc(self.deployed_at, field_name="deployed_at"))
        if self.superseded_at is not None:
            object.__setattr__(self, "superseded_at",
                               ensure_utc(self.superseded_at, field_name="superseded_at"))
        object.__setattr__(self, "config", dict(self.config))
        object.__setattr__(self, "dependency_versions",
                           {str(k): str(v) for k, v in dict(self.dependency_versions).items()})
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "triggers_fired", tuple(self.triggers_fired))

    @property
    def signature(self) -> str:
        """Tamper-evident identity of exactly this deployment."""
        payload = json.dumps({
            "deployment_id": self.deployment_id,
            "capability_id": self.capability_id, "version": self.version,
            "deployed_at": self.deployed_at.isoformat(),
            "deployed_by": self.deployed_by,
            "config": jsonable(dict(self.config)),
            "dependency_versions": dict(self.dependency_versions),
            "evidence_refs": list(self.evidence_refs),
            "previous_deployment_id": self.previous_deployment_id,
        }, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def reversible(self) -> bool:
        """Is there somewhere to go back to?"""
        return bool(self.previous_deployment_id)

    def to_dict(self) -> dict:
        return {"deployment_id": self.deployment_id,
                "capability_id": self.capability_id, "version": self.version,
                "deployed_at": jsonable(self.deployed_at),
                "deployed_by": self.deployed_by,
                "config": jsonable(dict(self.config)),
                "dependency_versions": dict(self.dependency_versions),
                "evidence_refs": list(self.evidence_refs),
                "previous_deployment_id": self.previous_deployment_id,
                "rollback_procedure": self.rollback_procedure,
                "state": self.state.value,
                "superseded_at": jsonable(self.superseded_at),
                "rollback_reason": self.rollback_reason,
                "triggers_fired": [t.to_dict() for t in self.triggers_fired],
                "notes": self.notes, "signature": self.signature,
                "reversible": self.reversible}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DeploymentRecord":
        when = data.get("superseded_at")
        return cls(
            deployment_id=str(data["deployment_id"]),
            capability_id=str(data["capability_id"]),
            version=str(data["version"]),
            deployed_at=datetime.fromisoformat(data["deployed_at"]),
            deployed_by=str(data.get("deployed_by", "")),
            config=data.get("config") or {},
            dependency_versions=data.get("dependency_versions") or {},
            evidence_refs=tuple(data.get("evidence_refs") or ()),
            previous_deployment_id=str(data.get("previous_deployment_id", "")),
            rollback_procedure=str(data.get("rollback_procedure", "")),
            state=DeploymentState(data.get("state", DeploymentState.ACTIVE.value)),
            superseded_at=(datetime.fromisoformat(when)
                           if isinstance(when, str) and when else None),
            rollback_reason=str(data.get("rollback_reason", "")),
            triggers_fired=tuple(
                FiredTrigger(trigger=RollbackTrigger(t["trigger"]),
                             measured=t.get("measured"),
                             threshold=t.get("threshold"),
                             detail=str(t.get("detail", "")))
                for t in data.get("triggers_fired") or ()),
            notes=str(data.get("notes", "")))

    def evolve(self, **changes) -> "DeploymentRecord":
        data = {f: getattr(self, f) for f in self.__dataclass_fields__}
        data.update(changes)
        return DeploymentRecord(**data)


@dataclass(frozen=True)
class ReconciliationTask:
    """One thing left inconsistent by a rollback. Reported, never auto-fixed.

    Same principle as `reconcile.py`: a position break is never silently
    repaired, because the repair is a guess about which side is right and
    guessing about positions is how a reconciliation bug becomes a loss.
    """

    area: str
    description: str
    blocking: bool = True

    def to_dict(self) -> dict:
        return {"area": self.area, "description": self.description,
                "blocking": self.blocking}


def reconcile_after(rolled_back: DeploymentRecord,
                    restored: DeploymentRecord, *,
                    open_orders: int = 0, open_positions: int = 0,
                    knowledge_records: int = 0,
                    config_differences: Sequence[str] = ()
                    ) -> list[ReconciliationTask]:
    """What must be checked before the restored version is trusted."""
    tasks: list[ReconciliationTask] = []
    if open_orders:
        tasks.append(ReconciliationTask(
            area="orders",
            description=f"{open_orders} order(s) were placed by "
                        f"{rolled_back.version} and are still open; the restored "
                        "version did not create them and will not manage them"))
    if open_positions:
        tasks.append(ReconciliationTask(
            area="positions",
            description=f"{open_positions} position(s) were opened under "
                        f"{rolled_back.version}; their stops and targets came "
                        "from logic that is no longer running"))
    if knowledge_records:
        tasks.append(ReconciliationTask(
            area="knowledge",
            description=f"{knowledge_records} knowledge record(s) were written "
                        f"by {rolled_back.version}; review before they inform "
                        "decisions under the restored version",
            blocking=False))
    differences = list(config_differences) or [
        key for key in sorted(set(rolled_back.config) | set(restored.config))
        if rolled_back.config.get(key) != restored.config.get(key)]
    if differences:
        tasks.append(ReconciliationTask(
            area="configuration",
            description="configuration differs between the rolled-back and "
                        "restored versions: " + ", ".join(differences[:10]),
            blocking=False))
    changed_deps = sorted(
        name for name in set(rolled_back.dependency_versions) | set(restored.dependency_versions)
        if rolled_back.dependency_versions.get(name) != restored.dependency_versions.get(name))
    if changed_deps:
        tasks.append(ReconciliationTask(
            area="dependencies",
            description="dependency versions differ; the restored version was "
                        "evaluated against " + ", ".join(
                            f"{n}={restored.dependency_versions.get(n, 'unset')}"
                            for n in changed_deps[:10])))
    return tasks


class DeploymentLedger:
    """Every deployment, in order, with rollback that cannot overshoot."""

    def __init__(self, *, clock: Clock | None = None, audit: Any = None,
                 namespace: str = _NS,
                 thresholds: TriggerThresholds | None = None):
        self.clock = clock or default_clock()
        self.audit = audit
        self.namespace = namespace
        self.thresholds = thresholds or TriggerThresholds()

    def _store(self) -> KeyedStore:
        return KeyedStore(self.namespace)

    def _write(self, record: DeploymentRecord, action: str, actor: str
               ) -> DeploymentRecord:
        self._store().put((record.deployment_id,), record.to_dict())
        if self.audit is not None:
            try:
                from .audit import EventType
                event = (EventType.ROLLBACK if action == "rollback"
                         else EventType.DEPLOYMENT_CHANGED)
                self.audit.record(event, {**record.to_dict(), "action": action},
                                  actor=actor, subject=record.capability_id)
            except Exception:                            # noqa: BLE001
                pass
        return record

    def get(self, deployment_id: str) -> DeploymentRecord | None:
        body = self._store().get((deployment_id,))
        return DeploymentRecord.from_dict(body) if body else None

    def history(self, capability_id: str) -> list[DeploymentRecord]:
        """Every deployment of one capability, oldest first."""
        out = []
        for _, body in self._store().items():
            record = DeploymentRecord.from_dict(body)
            if record.capability_id == capability_id:
                out.append(record)
        return sorted(out, key=lambda r: (r.deployed_at, r.deployment_id))

    def active(self, capability_id: str) -> DeploymentRecord | None:
        for record in reversed(self.history(capability_id)):
            if record.state is DeploymentState.ACTIVE:
                return record
        return None

    # -- forward -----------------------------------------------------------

    def deploy(self, *, deployment_id: str, capability_id: str, version: str,
               actor: Actor, config: Mapping[str, Any],
               dependency_versions: Mapping[str, str],
               evidence_refs: Sequence[str], rollback_procedure: str,
               notes: str = "") -> DeploymentRecord:
        """Record a new active deployment. Operator only.

        Refuses when reversal would be impossible. The four requirements —
        configuration, dependency pins, evidence, a written rollback
        procedure — are each checked before anything becomes active, because
        every one of them is unobtainable once an incident has started.
        """
        require_operator(actor, what="deploying a version")
        authorise(Action.DEPLOY_CODE, actor, subject=capability_id,
                  clock=self.clock, audit=self.audit)
        if not config:
            raise ConfigurationError(
                "a deployment must record its complete configuration; a "
                "reference to a config file that can also change is not "
                "reproducible", capability_id=capability_id)
        if not dependency_versions:
            raise ConfigurationError(
                "a deployment must pin its dependency versions; rolling back "
                "code while dependencies move is not a rollback",
                capability_id=capability_id)
        if not evidence_refs:
            raise ConfigurationError(
                "a deployment must reference the evaluation evidence that "
                "justified it", capability_id=capability_id)
        if not str(rollback_procedure).strip():
            raise ConfigurationError(
                "a deployment must state how to reverse it, in words, before it "
                "goes active", capability_id=capability_id)

        from olympus import proclock
        with proclock.lock(f"deployment-{capability_id}"):
            if self.get(deployment_id) is not None:
                raise ConfigurationError("deployment_id already exists",
                                         deployment_id=deployment_id)
            previous = self.active(capability_id)
            record = DeploymentRecord(
                deployment_id=deployment_id, capability_id=capability_id,
                version=version, deployed_at=self.clock.now(),
                deployed_by=actor.name, config=dict(config),
                dependency_versions=dict(dependency_versions),
                evidence_refs=tuple(evidence_refs),
                previous_deployment_id=previous.deployment_id if previous else "",
                rollback_procedure=rollback_procedure,
                state=DeploymentState.ACTIVE, notes=notes)
            if previous is not None:
                self._write(previous.evolve(state=DeploymentState.SUPERSEDED,
                                            superseded_at=self.clock.now()),
                            "supersede", actor.name)
            self._write(record, "deploy", actor.name)
        return record

    # -- backward ----------------------------------------------------------

    def rollback(self, capability_id: str, *, reason: str,
                 triggers: Sequence[FiredTrigger] = (),
                 actor: Actor | None = None) -> DeploymentRecord:
        """Restore the previous deployment. Autonomous.

        Autonomous because the destination is a version a human already
        deployed: this can only retreat to previously approved state, never
        advance. `EMERGENCY_SHUTDOWN` is the governing action, and the refusal
        that keeps it honest is the one below — with no previous deployment
        there is nothing to retreat *to*, and inventing one would be a
        deployment, not a rollback.
        """
        actor = actor or Actor.autonomous("rollback-controller")
        authorise(Action.EMERGENCY_SHUTDOWN, actor, subject=capability_id,
                  clock=self.clock, audit=self.audit)
        if not str(reason).strip():
            raise ConfigurationError("a rollback must record why",
                                     capability_id=capability_id)

        from olympus import proclock
        with proclock.lock(f"deployment-{capability_id}"):
            current = self.active(capability_id)
            if current is None:
                raise ConfigurationError("no active deployment to roll back",
                                         capability_id=capability_id)
            if not current.previous_deployment_id:
                raise PromotionDenied(
                    "this is the first deployment of the capability; there is "
                    "no previously approved version to return to. Suspend the "
                    "capability instead — rolling 'back' to something nobody "
                    "deployed would be a deployment.",
                    capability_id=capability_id,
                    deployment_id=current.deployment_id)
            previous = self.get(current.previous_deployment_id)
            if previous is None:
                raise PromotionDenied(
                    "the previous deployment record is missing, so the restore "
                    "target cannot be verified", capability_id=capability_id,
                    previous_deployment_id=current.previous_deployment_id)

            self._write(current.evolve(
                state=DeploymentState.ROLLED_BACK,
                superseded_at=self.clock.now(), rollback_reason=reason,
                triggers_fired=tuple(triggers)), "rollback", actor.name)
            restored = previous.evolve(state=DeploymentState.ACTIVE,
                                       superseded_at=None)
            self._write(restored, "restore", actor.name)
        return restored

    def check_and_rollback(self, capability_id: str, metrics: Mapping[str, Any],
                           *, actor: Actor | None = None
                           ) -> tuple[list[FiredTrigger], DeploymentRecord | None]:
        """Evaluate triggers and act if any fired. The automatic path.

        Returns the fired triggers alongside the restored deployment (or
        `None`), so a caller that logs the result records *why* nothing
        happened as well as why something did.
        """
        fired = evaluate_triggers(metrics, thresholds=self.thresholds)
        if not fired:
            return [], None
        reason = "; ".join(t.detail for t in fired)
        try:
            restored = self.rollback(capability_id, reason=reason,
                                     triggers=fired, actor=actor)
        except PromotionDenied:
            # Nothing to retreat to. The triggers still fired and the caller
            # must see them — swallowing them here would turn "we cannot roll
            # back" into "nothing was wrong".
            return fired, None
        return fired, restored

    def reconcile_after(self, capability_id: str, **kwargs
                        ) -> list[ReconciliationTask]:
        """Post-rollback checklist for the most recent rollback."""
        history = self.history(capability_id)
        rolled = next((r for r in reversed(history)
                       if r.state is DeploymentState.ROLLED_BACK), None)
        active = self.active(capability_id)
        if rolled is None or active is None:
            return []
        return reconcile_after(rolled, active, **kwargs)

    def report(self, capability_id: str | None = None) -> dict:
        records = [DeploymentRecord.from_dict(b) for _, b in self._store().items()]
        if capability_id is not None:
            records = [r for r in records if r.capability_id == capability_id]
        by_state: dict[str, int] = {}
        for record in records:
            by_state[record.state.value] = by_state.get(record.state.value, 0) + 1
        irreversible = [r.deployment_id for r in records
                        if r.state is DeploymentState.ACTIVE and not r.reversible]
        return {"deployments": len(records), "by_state": by_state,
                "active_without_a_rollback_target": irreversible,
                "thresholds": self.thresholds.to_dict()}

    def __repr__(self) -> str:                          # pragma: no cover
        return f"DeploymentLedger(namespace={self.namespace!r})"


__all__ = ["DeploymentState", "RollbackTrigger", "TriggerThresholds",
           "FiredTrigger", "evaluate_triggers", "TRIGGER_METRICS",
           "unmeasured_triggers", "DeploymentRecord", "ReconciliationTask",
           "reconcile_after", "DeploymentLedger"]

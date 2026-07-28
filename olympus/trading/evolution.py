"""The evolution loop, its audit trail, and the proof that it is working.

This is where the Phase 10 pieces are wired into one cycle and one record.

**The cycle.** `EvolutionCycle.run()` performs, in order: score resolved
outcomes, detect systematic weaknesses, measure drift, demote what has
deteriorated, generate hypotheses from that evidence, and surface a backlog.
Every step is an autonomous action under `governance.py`. The cycle
deliberately stops before doing anything: it never runs an experiment against
production data it was not given, never promotes anything, never deploys. It
produces a `CycleReport` — a description of what it observed and what it now
wants to know.

**The record.** Every `EvolutionEvent` carries the eleven fields the
specification requires, and `EvolutionLedger.explain()` answers the seven
questions directly:

    why did Olympus change · what evidence justified it · who authorised it
    which version was active · can the previous version be restored
    did the change improve performance · did the change create new risks

Two of those seven are answered honestly rather than optimistically. "Did it
improve performance" returns `None` until there is post-change measurement to
compare — an event recorded yesterday has no answer, and inventing one is how a
self-evolution report becomes a press release. "Did it create new risks" is a
list of *unmeasured* risk dimensions as well as measured regressions, because
the risks a change created are not knowable from the change alone.

**The proof.** `ImprovementReport` scores fifteen named metrics between a
baseline period and a current one. Its verdict starts at `UNPROVEN` and stays
there unless enough metrics moved favourably on enough evidence — a system that
declared itself improving because it had learned more would be exactly the
failure the phase is written against. Learning more is not improvement;
`measured_improvement` is.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from .clock import Clock, default_clock
from .contracts import ensure_utc, jsonable
from .errors import ConfigurationError
from .governance import Action, Actor, authorise
from .storekeys import KeyedStore

_NS = "trading.evolution"


class EvolutionStage(str, Enum):
    """Where in the loop an event happened."""

    OBSERVATION = "observation"
    INFERENCE = "inference"
    HYPOTHESIS = "hypothesis"
    EXPERIMENT = "experiment"
    PROPOSAL = "proposal"
    REVIEW = "review"
    PROMOTION = "promotion"
    DEPLOYMENT = "deployment"
    ROLLBACK = "rollback"
    RESTRICTION = "restriction"
    DEPRECATION = "deprecation"


class ReviewDecision(str, Enum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True)
class EvolutionEvent:
    """One step of change, with everything needed to reconstruct it later."""

    event_id: str
    stage: EvolutionStage
    at: datetime
    #: The seven-question fields.
    observed: str = ""
    inferred: str = ""
    proposed: str = ""
    evidence: tuple[str, ...] = ()
    experiment: str = ""
    results: Mapping[str, Any] = field(default_factory=dict)
    reviewed_by: str = ""
    decision: ReviewDecision = ReviewDecision.NOT_REQUIRED
    #: Versions in play at the moment of the event.
    code_version: str = ""
    model_versions: Mapping[str, str] = field(default_factory=dict)
    config_version: str = ""
    deployment_id: str = ""
    deployment_state: str = ""
    rollback_target: str = ""
    subject: str = ""
    actor: str = "olympus"
    actor_kind: str = "autonomous"

    def __post_init__(self):
        if not str(self.event_id).strip():
            raise ConfigurationError("event_id must be non-empty")
        object.__setattr__(self, "stage", EvolutionStage(self.stage))
        object.__setattr__(self, "decision", ReviewDecision(self.decision))
        object.__setattr__(self, "at", ensure_utc(self.at, field_name="at"))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "results", dict(self.results))
        object.__setattr__(self, "model_versions", dict(self.model_versions))
        if self.stage in (EvolutionStage.PROMOTION, EvolutionStage.DEPLOYMENT):
            if self.decision is not ReviewDecision.ACCEPTED or not self.reviewed_by:
                raise ConfigurationError(
                    "a promotion or deployment event must name the reviewer who "
                    "accepted it; an unattributed promotion cannot be audited",
                    event_id=self.event_id, stage=self.stage.value,
                    decision=self.decision.value)

    @property
    def restorable(self) -> bool:
        return bool(self.rollback_target)

    def to_dict(self) -> dict:
        return {"event_id": self.event_id, "stage": self.stage.value,
                "at": jsonable(self.at), "observed": self.observed,
                "inferred": self.inferred, "proposed": self.proposed,
                "evidence": list(self.evidence), "experiment": self.experiment,
                "results": jsonable(dict(self.results)),
                "reviewed_by": self.reviewed_by,
                "decision": self.decision.value,
                "code_version": self.code_version,
                "model_versions": dict(self.model_versions),
                "config_version": self.config_version,
                "deployment_id": self.deployment_id,
                "deployment_state": self.deployment_state,
                "rollback_target": self.rollback_target,
                "subject": self.subject, "actor": self.actor,
                "actor_kind": self.actor_kind, "restorable": self.restorable}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EvolutionEvent":
        return cls(
            event_id=str(data["event_id"]),
            stage=EvolutionStage(data["stage"]),
            at=datetime.fromisoformat(data["at"]),
            observed=str(data.get("observed", "")),
            inferred=str(data.get("inferred", "")),
            proposed=str(data.get("proposed", "")),
            evidence=tuple(data.get("evidence") or ()),
            experiment=str(data.get("experiment", "")),
            results=data.get("results") or {},
            reviewed_by=str(data.get("reviewed_by", "")),
            decision=ReviewDecision(data.get("decision",
                                             ReviewDecision.NOT_REQUIRED.value)),
            code_version=str(data.get("code_version", "")),
            model_versions=data.get("model_versions") or {},
            config_version=str(data.get("config_version", "")),
            deployment_id=str(data.get("deployment_id", "")),
            deployment_state=str(data.get("deployment_state", "")),
            rollback_target=str(data.get("rollback_target", "")),
            subject=str(data.get("subject", "")),
            actor=str(data.get("actor", "olympus")),
            actor_kind=str(data.get("actor_kind", "autonomous")))


@dataclass(frozen=True)
class Explanation:
    """The seven questions, answered for one event.

    `improved_performance` is `bool | None`. `None` means not yet measurable,
    and is returned far more often than either boolean — which is the truthful
    shape of the answer for a change made recently.
    """

    event_id: str
    why: str
    evidence: tuple[str, ...]
    authorised_by: str
    active_version: str
    restorable: bool
    rollback_target: str
    improved_performance: bool | None
    performance_detail: str
    new_risks: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"event_id": self.event_id, "why": self.why,
                "evidence": list(self.evidence),
                "authorised_by": self.authorised_by,
                "active_version": self.active_version,
                "restorable": self.restorable,
                "rollback_target": self.rollback_target,
                "improved_performance": self.improved_performance,
                "performance_detail": self.performance_detail,
                "new_risks": list(self.new_risks)}

    def describe(self) -> str:                           # pragma: no cover
        verdict = {True: "yes", False: "no", None: "not yet measurable"}[
            self.improved_performance]
        return "\n".join([
            f"why: {self.why}",
            f"evidence: {', '.join(self.evidence) or '(none recorded)'}",
            f"authorised by: {self.authorised_by or '(autonomous)'}",
            f"version active: {self.active_version or '(unrecorded)'}",
            f"restorable: {'yes → ' + self.rollback_target if self.restorable else 'no'}",
            f"improved performance: {verdict} — {self.performance_detail}",
            f"new risks: {', '.join(self.new_risks) or '(none identified)'}",
        ])


class EvolutionLedger:
    """Append-only record of every evolutionary change.

    There is no update or delete. Correcting the record means appending a new
    event that says so, exactly as `knowledge.correct` does — a self-evolving
    system that could revise its own history of changing could not be audited at
    all.
    """

    def __init__(self, *, clock: Clock | None = None, audit: Any = None,
                 namespace: str = _NS):
        self.clock = clock or default_clock()
        self.audit = audit
        self.namespace = namespace

    def _store(self) -> KeyedStore:
        return KeyedStore(self.namespace)

    def record(self, event: EvolutionEvent) -> EvolutionEvent:
        """Append. Refuses to overwrite an existing event id."""
        from olympus import proclock
        with proclock.lock(f"evolution-{event.event_id}"):
            if self._store().get((event.event_id,)):
                raise ConfigurationError(
                    "this evolution event is already recorded; append a "
                    "correcting event rather than replacing the record of what "
                    "was believed at the time", event_id=event.event_id)
            self._store().put((event.event_id,), event.to_dict())
        if self.audit is not None:
            try:
                from .audit import EventType
                mapping = {
                    EvolutionStage.EXPERIMENT: EventType.EXPERIMENT_RUN,
                    EvolutionStage.HYPOTHESIS: EventType.HYPOTHESIS_PROPOSED,
                    EvolutionStage.PROPOSAL: EventType.PROPOSAL_SUBMITTED,
                    EvolutionStage.PROMOTION: EventType.CAPABILITY_CHANGED,
                    EvolutionStage.DEPLOYMENT: EventType.DEPLOYMENT_CHANGED,
                    EvolutionStage.ROLLBACK: EventType.ROLLBACK,
                    EvolutionStage.RESTRICTION: EventType.DRIFT_DETECTED,
                    EvolutionStage.DEPRECATION: EventType.KNOWLEDGE_DEPRECATED,
                }
                self.audit.record(
                    mapping.get(event.stage, EventType.OUTCOME_EVALUATED),
                    event.to_dict(), actor=event.actor, subject=event.subject)
            except Exception:                            # noqa: BLE001
                pass
        return event

    def get(self, event_id: str) -> EvolutionEvent | None:
        body = self._store().get((event_id,))
        return EvolutionEvent.from_dict(body) if body else None

    def events(self, *, stage: EvolutionStage | str | None = None,
               subject: str | None = None,
               since: datetime | None = None) -> list[EvolutionEvent]:
        out = []
        for _, body in self._store().items():
            event = EvolutionEvent.from_dict(body)
            if stage is not None and event.stage is not EvolutionStage(stage):
                continue
            if subject is not None and event.subject != subject:
                continue
            if since is not None and event.at < ensure_utc(since, field_name="since"):
                continue
            out.append(event)
        return sorted(out, key=lambda e: (e.at, e.event_id))

    def explain(self, event_id: str, *,
                performance_before: Mapping[str, float] | None = None,
                performance_after: Mapping[str, float] | None = None,
                risk_dimensions: Sequence[str] = ("drawdown", "leverage",
                                                  "concentration", "liquidity")
                ) -> Explanation:
        """Answer all seven questions about one event.

        Performance is judged only from measurements the caller supplies. There
        is no fallback that guesses from the event's own `results` — a change's
        own record of how well it did is not evidence about how well it did.
        """
        event = self.get(event_id)
        if event is None:
            raise ConfigurationError("unknown evolution event", event_id=event_id)

        improved: bool | None = None
        detail = ("no post-change measurement supplied, so the effect of this "
                  "change is not yet known")
        new_risks: list[str] = []
        if performance_before and performance_after:
            shared = sorted(set(performance_before) & set(performance_after))
            # Direction comes from `IMPROVEMENT_METRICS`, never assumed. A
            # falling drawdown is an improvement and a falling Sharpe is not,
            # and a comparison that treated every metric as higher-is-better
            # would report the wrong sign for half of them.
            judged = [k for k in shared if k in IMPROVEMENT_METRICS]
            undirected = [k for k in shared if k not in IMPROVEMENT_METRICS]
            better, worse = [], []
            for name in judged:
                delta = performance_after[name] - performance_before[name]
                if delta == 0:
                    continue
                lower_is_better = IMPROVEMENT_METRICS[name] == "lower"
                (better if (delta < 0) == lower_is_better else worse).append(name)
            if judged:
                improved = len(better) > len(worse)
                detail = (f"{len(better)} of {len(judged)} directional metrics "
                          f"improved, {len(worse)} regressed")
                new_risks.extend(f"regression in {k}" for k in worse)
            elif shared:
                detail = ("the shared metrics have no declared direction, so "
                          "whether they moved for the better is not decidable")
            else:
                detail = ("before and after measurements share no metric, so "
                          "they cannot be compared")
            if undirected:
                detail += (f"; {len(undirected)} metric(s) not judged (no "
                           "declared direction): " + ", ".join(undirected))
        for dimension in risk_dimensions:
            measured = ((performance_after or {}).get(dimension) is not None)
            if not measured:
                new_risks.append(f"{dimension} unmeasured after the change")

        why = event.observed or event.inferred or event.proposed or "(not recorded)"
        authorised = event.reviewed_by or (
            "" if event.actor_kind == "autonomous" else event.actor)
        version = event.code_version or event.config_version or ", ".join(
            f"{k}={v}" for k, v in sorted(event.model_versions.items()))
        return Explanation(
            event_id=event.event_id, why=why, evidence=event.evidence,
            authorised_by=authorised, active_version=version,
            restorable=event.restorable, rollback_target=event.rollback_target,
            improved_performance=improved, performance_detail=detail,
            new_risks=tuple(new_risks))

    def unattributed_promotions(self) -> list[EvolutionEvent]:
        """Promotions with no reviewer. Should always be empty by construction.

        Kept as a query rather than trusted to the constructor alone: a record
        written by an older version of this code, or restored from a backup,
        should still be findable.
        """
        return [e for e in self.events()
                if e.stage in (EvolutionStage.PROMOTION, EvolutionStage.DEPLOYMENT)
                and not e.reviewed_by]

    def report(self) -> dict:
        events = self.events()
        by_stage: dict[str, int] = {}
        for event in events:
            by_stage[event.stage.value] = by_stage.get(event.stage.value, 0) + 1
        autonomous = sum(1 for e in events if e.actor_kind == "autonomous")
        return {"events": len(events), "by_stage": by_stage,
                "autonomous": autonomous, "operator": len(events) - autonomous,
                "unattributed_promotions": len(self.unattributed_promotions())}

    def __repr__(self) -> str:                          # pragma: no cover
        return f"EvolutionLedger(namespace={self.namespace!r})"


# ---------------------------------------------------------------------------
# is it actually improving?
# ---------------------------------------------------------------------------

#: The fifteen metrics, with the direction that counts as better and whether a
#: value is a rate (comparable directly) or a count (compared as a delta).
#: Enumerated so a missing metric is visible as a gap rather than absent from
#: the report entirely.
IMPROVEMENT_METRICS: Mapping[str, str] = {
    "forecast_error": "lower",
    "calibration_gap": "lower",
    "abstention_quality": "higher",
    "max_drawdown": "lower",
    "risk_adjusted_return": "higher",
    "slippage": "lower",
    "operational_failures": "lower",
    "incident_detection_seconds": "lower",
    "recovery_seconds": "lower",
    "false_signal_rate": "lower",
    "data_quality": "higher",
    "unseen_regime_performance": "higher",
    "validated_capabilities_added": "higher",
    "unsafe_proposals_rejected": "higher",
    "deteriorating_models_restricted": "higher",
}

#: Metrics measuring the *safety machinery* working rather than the trading
#: getting better. Counted separately: a period in which Olympus rejected many
#: unsafe proposals is a period in which the guard rails worked, which is not
#: the same as a period in which it traded better, and conflating the two would
#: let a system look like it was improving by generating more bad ideas.
GOVERNANCE_METRICS: frozenset[str] = frozenset({
    "validated_capabilities_added", "unsafe_proposals_rejected",
    "deteriorating_models_restricted",
})


class ImprovementVerdict(str, Enum):
    IMPROVING = "improving"
    NOT_IMPROVING = "not_improving"
    #: The default. Not a failure — the honest state before enough evidence.
    UNPROVEN = "unproven"


@dataclass(frozen=True)
class MetricChange:
    name: str
    before: float | None
    after: float | None
    direction: str
    sample_before: int = 0
    sample_after: int = 0

    @property
    def measured(self) -> bool:
        return self.before is not None and self.after is not None

    @property
    def improved(self) -> bool | None:
        if not self.measured:
            return None
        if self.after == self.before:
            return False
        return (self.after < self.before if self.direction == "lower"
                else self.after > self.before)

    @property
    def relative_change(self) -> float | None:
        if not self.measured or self.before == 0:
            return None
        return (self.after - self.before) / abs(self.before)

    def to_dict(self) -> dict:
        return {"name": self.name, "before": self.before, "after": self.after,
                "direction": self.direction, "improved": self.improved,
                "relative_change": self.relative_change,
                "sample_before": self.sample_before,
                "sample_after": self.sample_after, "measured": self.measured}


@dataclass(frozen=True)
class ImprovementReport:
    """Whether Olympus is measurably better, not merely better informed."""

    verdict: ImprovementVerdict
    changes: tuple[MetricChange, ...]
    period_before: str = ""
    period_after: str = ""
    rationale: str = ""
    min_metrics_required: int = 5

    def __post_init__(self):
        object.__setattr__(self, "verdict", ImprovementVerdict(self.verdict))
        object.__setattr__(self, "changes", tuple(self.changes))

    @property
    def measured(self) -> tuple[MetricChange, ...]:
        return tuple(c for c in self.changes if c.measured)

    @property
    def unmeasured(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.changes if not c.measured)

    @property
    def trading_changes(self) -> tuple[MetricChange, ...]:
        """The metrics about trading, excluding the governance counters."""
        return tuple(c for c in self.measured if c.name not in GOVERNANCE_METRICS)

    @property
    def improved(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.measured if c.improved)

    @property
    def regressed(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.measured if c.improved is False)

    def to_dict(self) -> dict:
        return {"verdict": self.verdict.value,
                "changes": [c.to_dict() for c in self.changes],
                "measured": len(self.measured),
                "unmeasured": list(self.unmeasured),
                "improved": list(self.improved),
                "regressed": list(self.regressed),
                "period_before": self.period_before,
                "period_after": self.period_after,
                "rationale": self.rationale,
                "min_metrics_required": self.min_metrics_required}

    def describe(self) -> str:                           # pragma: no cover
        lines = [f"self-evolution: {self.verdict.value.upper()} — {self.rationale}"]
        for change in sorted(self.changes, key=lambda c: c.name):
            if not change.measured:
                lines.append(f"  {change.name}: unmeasured")
                continue
            mark = "+" if change.improved else "-"
            lines.append(f"  {mark} {change.name}: {change.before:.6g} → "
                         f"{change.after:.6g}")
        return "\n".join(lines)


def measure_improvement(before: Mapping[str, Any], after: Mapping[str, Any], *,
                        period_before: str = "", period_after: str = "",
                        min_metrics: int = 5,
                        min_improved_fraction: float = 0.6,
                        samples_before: Mapping[str, int] | None = None,
                        samples_after: Mapping[str, int] | None = None
                        ) -> ImprovementReport:
    """Compare two periods across the fifteen metrics.

    Deliberately hard to pass. `UNPROVEN` unless at least `min_metrics` trading
    metrics were measured in both periods, and `IMPROVING` only if at least
    `min_improved_fraction` of the *trading* metrics moved favourably. The
    governance counters are reported but excluded from the verdict, so Olympus
    cannot declare itself improving on the strength of having rejected more of
    its own bad ideas.
    """
    samples_before = dict(samples_before or {})
    samples_after = dict(samples_after or {})

    def number(source: Mapping[str, Any], key: str) -> float | None:
        value = source.get(key)
        try:
            out = float(value)
        except (TypeError, ValueError):
            return None
        return out

    changes = tuple(
        MetricChange(name=name, before=number(before, name),
                     after=number(after, name), direction=direction,
                     sample_before=int(samples_before.get(name, 0)),
                     sample_after=int(samples_after.get(name, 0)))
        for name, direction in sorted(IMPROVEMENT_METRICS.items()))

    trading = [c for c in changes if c.measured and c.name not in GOVERNANCE_METRICS]
    if len(trading) < min_metrics:
        return ImprovementReport(
            verdict=ImprovementVerdict.UNPROVEN, changes=changes,
            period_before=period_before, period_after=period_after,
            min_metrics_required=min_metrics,
            rationale=(f"only {len(trading)} of the {len(IMPROVEMENT_METRICS)} "
                       f"metrics were measured in both periods (excluding "
                       f"governance counters); at least {min_metrics} are "
                       "needed before any claim of improvement is supportable"))

    improved = [c for c in trading if c.improved]
    fraction = len(improved) / len(trading)
    if fraction >= min_improved_fraction:
        verdict = ImprovementVerdict.IMPROVING
        rationale = (f"{len(improved)} of {len(trading)} measured trading "
                     f"metrics improved ({fraction:.0%}, threshold "
                     f"{min_improved_fraction:.0%})")
    else:
        verdict = ImprovementVerdict.NOT_IMPROVING
        rationale = (f"only {len(improved)} of {len(trading)} measured trading "
                     f"metrics improved ({fraction:.0%}), below the "
                     f"{min_improved_fraction:.0%} threshold; learning more is "
                     "not improvement")
    return ImprovementReport(
        verdict=verdict, changes=changes, period_before=period_before,
        period_after=period_after, rationale=rationale,
        min_metrics_required=min_metrics)


# ---------------------------------------------------------------------------
# the cycle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CycleReport:
    """What one turn of the loop observed and now wants to know."""

    ran_at: datetime
    resolved_decisions: int = 0
    weaknesses: tuple[Any, ...] = ()
    drift_signals: tuple[Any, ...] = ()
    restricted: tuple[str, ...] = ()
    hypotheses: tuple[str, ...] = ()
    backlog: int = 0
    knowledge_recorded: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "ran_at",
                           ensure_utc(self.ran_at, field_name="ran_at"))
        for name in ("weaknesses", "drift_signals", "restricted", "hypotheses",
                     "knowledge_recorded", "errors"):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    @property
    def acted(self) -> bool:
        """Did this cycle change anything? Only ever restrictions."""
        return bool(self.restricted)

    def to_dict(self) -> dict:
        return {"ran_at": jsonable(self.ran_at),
                "resolved_decisions": self.resolved_decisions,
                "weaknesses": [jsonable(w) for w in self.weaknesses],
                "drift_signals": [jsonable(s) for s in self.drift_signals],
                "restricted": list(self.restricted),
                "hypotheses": list(self.hypotheses), "backlog": self.backlog,
                "knowledge_recorded": list(self.knowledge_recorded),
                "errors": list(self.errors), "acted": self.acted}

    def describe(self) -> str:                           # pragma: no cover
        return (f"evolution cycle at {self.ran_at.isoformat()}: "
                f"{self.resolved_decisions} decisions scored, "
                f"{len(self.weaknesses)} weakness(es), "
                f"{len(self.drift_signals)} drift signal(s), "
                f"{len(self.restricted)} component(s) restricted, "
                f"{len(self.hypotheses)} hypothesis/es generated, "
                f"backlog {self.backlog}")


class EvolutionCycle:
    """Runs one turn of the learning loop. Autonomous, and bounded to that.

    Every collaborator is optional and every step is individually guarded, so a
    cycle with only an outcome journal still does useful work, and a failure in
    one subsystem does not take the loop down — the failure is collected into
    `CycleReport.errors`, which an operator reads, rather than raised into a
    scheduler that will simply retry it.

    What this class cannot do is the point: it holds no reference to execution,
    the limits store, mode control or the vault, and the strongest action it
    takes is demoting a component down the deterioration ladder.
    """

    def __init__(self, *, clock: Clock | None = None, audit: Any = None,
                 journal: Any = None, monitor: Any = None,
                 hypothesis_engine: Any = None, knowledge_base: Any = None,
                 ledger: Any = None):
        self.clock = clock or default_clock()
        self.audit = audit
        self.journal = journal                    # outcomes.OutcomeJournal
        self.monitor = monitor                    # drift.DeteriorationMonitor
        self.hypothesis_engine = hypothesis_engine
        self.knowledge_base = knowledge_base
        self.ledger = ledger or EvolutionLedger(clock=self.clock, audit=audit)

    def run(self, *, drift_signals: Sequence[Any] = (),
            component_kinds: Mapping[str, str] | None = None,
            actor: Actor | None = None) -> CycleReport:
        """One full turn. Never raises for a subsystem failure."""
        actor = actor or Actor.autonomous("evolution-cycle")
        authorise(Action.LEARN, actor, subject="evolution-cycle",
                  clock=self.clock, audit=self.audit)
        now = self.clock.now()
        errors: list[str] = []
        resolved: list[Any] = []
        weaknesses: list[Any] = []
        restricted: list[str] = []
        hypotheses: list[str] = []
        knowledge_ids: list[str] = []
        backlog = 0

        # 1. score what has resolved
        if self.journal is not None:
            try:
                resolved = self.journal.resolved()
                weaknesses = self.journal.weaknesses()
            except Exception as exc:                     # noqa: BLE001
                errors.append(f"outcome scoring failed: {exc!r}")

        # 2. record what was learned, as knowledge with provenance
        if self.knowledge_base is not None and weaknesses:
            from .knowledge import Evidence, MemoryClass, SourceType
            for finding in weaknesses:
                axis = getattr(finding, "axis", "unknown")
                scope = getattr(finding, "scope", "unknown")
                knowledge_id = f"weakness:{scope}:{axis}"
                try:
                    if self.knowledge_base.head(knowledge_id) is None:
                        self.knowledge_base.record(
                            knowledge_id,
                            statement=f"{axis} is systematically poor on {scope}: "
                                      f"{getattr(finding, 'detail', '')}",
                            source="outcomes.detect_weaknesses",
                            source_type=SourceType.STRATEGY_REPORT,
                            event_at=now, memory_class=MemoryClass.LONG_TERM,
                            confidence=min(0.9, float(getattr(finding, "rate", 0.0))),
                            supporting=(Evidence(
                                ref=f"outcome-journal:{scope}",
                                kind="measurement", origin="outcome-journal",
                                weight=1.0,
                                note=f"n={getattr(finding, 'sample', 0)}"),),
                            author="evolution-cycle")
                        knowledge_ids.append(knowledge_id)
                except Exception as exc:                 # noqa: BLE001
                    errors.append(f"knowledge write failed for {knowledge_id}: {exc!r}")

        # 3. assess deterioration and demote what has degraded
        if self.monitor is not None:
            kinds = dict(component_kinds or {})
            grouped: dict[str, list[Any]] = {}
            for signal in drift_signals:
                grouped.setdefault(getattr(signal, "subject", "unknown"), []).append(signal)
            for subject, signals in sorted(grouped.items()):
                try:
                    before = self.monitor.health(subject)
                    health = self.monitor.evaluate(
                        subject, kind=kinds.get(subject, "model"), signals=signals)
                    if before is None or health.state is not before.state:
                        restricted.append(f"{subject}→{health.state.value}")
                        self._log(EvolutionStage.RESTRICTION, subject=subject,
                                  observed=f"{len(signals)} drift signal(s)",
                                  inferred="; ".join(health.reasons),
                                  evidence=tuple(getattr(s, "detail", "")
                                                 for s in signals),
                                  results={"state": health.state.value},
                                  actor=actor, errors=errors)
                except Exception as exc:                 # noqa: BLE001
                    errors.append(f"deterioration assessment failed for {subject}: {exc!r}")

        # 4. turn evidence into questions
        if self.hypothesis_engine is not None:
            try:
                generated = self.hypothesis_engine.generate(
                    weaknesses=weaknesses, drift_signals=drift_signals,
                    actor=actor)
                hypotheses = [p.proposal_id for p in generated]
                backlog = len(self.hypothesis_engine.backlog())
                for proposal in generated:
                    self._log(EvolutionStage.HYPOTHESIS,
                              subject=proposal.proposal_id,
                              observed="; ".join(proposal.supporting_observations),
                              inferred=proposal.hypothesis,
                              proposed=proposal.experiment.description,
                              evidence=proposal.triggered_by,
                              actor=actor, errors=errors)
            except Exception as exc:                     # noqa: BLE001
                errors.append(f"hypothesis generation failed: {exc!r}")

        report = CycleReport(
            ran_at=now, resolved_decisions=len(resolved),
            weaknesses=tuple(weaknesses), drift_signals=tuple(drift_signals),
            restricted=tuple(restricted), hypotheses=tuple(hypotheses),
            backlog=backlog, knowledge_recorded=tuple(knowledge_ids),
            errors=tuple(errors))
        self._log(EvolutionStage.OBSERVATION, subject="evolution-cycle",
                  observed=report.describe(), results=report.to_dict(),
                  actor=actor, errors=errors)
        return report

    def _log(self, stage: EvolutionStage, *, subject: str, actor: Actor,
             errors: list[str], observed: str = "", inferred: str = "",
             proposed: str = "", evidence: Sequence[str] = (),
             results: Mapping[str, Any] | None = None) -> None:
        event_id = (f"evo-{stage.value}-{subject}-"
                    f"{self.clock.now().isoformat()}")
        try:
            self.ledger.record(EvolutionEvent(
                event_id=event_id, stage=stage, at=self.clock.now(),
                observed=observed, inferred=inferred, proposed=proposed,
                evidence=tuple(evidence), results=dict(results or {}),
                subject=subject, actor=actor.name, actor_kind=actor.kind.value))
        except Exception as exc:                         # noqa: BLE001
            errors.append(f"evolution ledger write failed: {exc!r}")


__all__ = ["EvolutionStage", "ReviewDecision", "EvolutionEvent", "Explanation",
           "EvolutionLedger", "IMPROVEMENT_METRICS", "GOVERNANCE_METRICS",
           "ImprovementVerdict", "MetricChange", "ImprovementReport",
           "measure_improvement", "CycleReport", "EvolutionCycle"]

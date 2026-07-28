"""Challenger proposals: what Olympus may suggest, and what it must state.

> Every proposal must include: hypothesis, supporting evidence, contradicting
> evidence, required data, experiment design, success criteria, failure
> criteria, compute budget, security impact, operational impact, rollback plan.

All eleven are required fields on `ChallengerProposal`, and eleven of them are
checked at construction. A proposal missing one does not exist as an object —
which is the difference between a schema and a checklist somebody fills in
later.

Three of the eleven do real work beyond documentation:

**Contradicting evidence is required and may not be empty.**
`NO_CONTRADICTION` is a sentinel a proposer has to type, and typing it is a
claim that can be wrong in review. A field that defaulted to empty would be
empty on every proposal, and the search for disconfirming evidence would stop
being part of proposing.

**The compute budget is enforced, not declared.**
`ComputeBudget` converts directly into the rlimits `isolation.py` applies to
the worker. A proposal claiming it needs 60 CPU-seconds gets 60 CPU-seconds and
is killed at 61. A budget nothing reads is a number in a document.

**The rollback plan must name a restore target.**
Not "revert the change" — an identifier the deployment ledger can resolve. A
plan that cannot be executed by reading it is a plan nobody will execute at
three in the morning.

Simplification is never a second-class challenger
-------------------------------------------------
`generate` pairs every complexity-adding challenger with a `SIMPLER_MODEL`
counterpart over the same weakness. `champion.compare()` already breaks a
statistical tie in favour of the simpler arm; this makes sure a simpler arm is
usually *there* to win the tie. Phase 1's benchmark — where a 19-parameter
AR(3) beat the network — is the argument.

Relationship to `hypotheses.py`
-------------------------------
`to_research_proposal()` converts a challenger into the existing
`ResearchProposal`, so a native challenger enters the same backlog, the same
falsifiability checks and the same status lifecycle as every other hypothesis
in the system. This module adds the four native-specific fields the general
form has no place for; it does not fork the framework.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Sequence

from ..contracts import ensure_utc, jsonable
from ..errors import ConfigurationError
from .evidence import WeaknessFinding, WeaknessKind

#: Bumped when a field's meaning changes.
CHALLENGER_SCHEMA_VERSION = 1

#: What a proposer types when they looked for disconfirming evidence and found
#: none. Deliberately verbose: it should be uncomfortable to assert.
NO_CONTRADICTION = ("none found; the search covered the evidence journal for "
                    "this scope and no record contradicts the hypothesis")


class ChallengerKind(str, Enum):
    """The ten kinds of proposal Phase 4 permits."""

    REPRESENTATION = "representation"
    ARCHITECTURE = "architecture"
    TASK_HEAD = "task_head"
    FEATURE = "feature"
    HORIZON = "horizon"
    TRAINING_OBJECTIVE = "training_objective"
    CALIBRATION = "calibration"
    REGIME_SPECIALIST = "regime_specialist"
    DATA_SOURCE = "data_source"
    SIMPLER_MODEL = "simpler_model"


#: Kinds that add machinery. Paired with a `SIMPLER_MODEL` challenger by
#: `generate`, so the parsimony tie-break in `champion.compare` has something
#: to break the tie toward.
COMPLEXITY_ADDING: frozenset[ChallengerKind] = frozenset({
    ChallengerKind.REPRESENTATION, ChallengerKind.ARCHITECTURE,
    ChallengerKind.TASK_HEAD, ChallengerKind.FEATURE,
    ChallengerKind.REGIME_SPECIALIST, ChallengerKind.DATA_SOURCE,
})


class SecurityImpact(str, Enum):
    """What a positive result would touch. Ordered by severity."""

    NONE = "none"
    #: Reads a new input that already exists inside Olympus.
    NEW_INTERNAL_INPUT = "new_internal_input"
    #: Reads something from outside. Every such input is untrusted.
    NEW_EXTERNAL_INPUT = "new_external_input"
    #: Would need a dependency Olympus does not ship.
    NEW_DEPENDENCY = "new_dependency"
    #: Would touch something in the safety kernel. Not proposable by Olympus.
    KERNEL = "kernel"


class OperationalImpact(str, Enum):
    """What a positive result would cost to run."""

    NONE = "none"
    RETRAIN_ONLY = "retrain_only"
    #: Changes what serving needs at inference time.
    SERVING_CHANGE = "serving_change"
    #: Needs data Olympus does not currently ingest.
    NEW_INGESTION = "new_ingestion"
    #: Changes an interface other components depend on.
    CONTRACT_CHANGE = "contract_change"


#: Security impacts a proposal may carry and still be generated autonomously.
#: `KERNEL` is absent on purpose: `kernel.propose_kernel_change` is the only
#: route to a kernel recommendation and it is a document, not a challenger.
AUTONOMOUS_SECURITY_IMPACTS: frozenset[SecurityImpact] = frozenset({
    SecurityImpact.NONE, SecurityImpact.NEW_INTERNAL_INPUT,
})


@dataclass(frozen=True)
class ComputeBudget:
    """What the experiment may consume. Enforced by `isolation.py`.

    Every field is a hard limit rather than an estimate, because the consumer
    is `resource.setrlimit` and an estimate cannot be passed to it. A proposal
    that needs more asks for more and is reviewed on that basis.
    """

    cpu_seconds: int = 60
    memory_mb: int = 512
    disk_mb: int = 64
    wall_clock_seconds: int = 120
    #: Processes the worker may spawn. One is the worker itself.
    max_processes: int = 8

    def __post_init__(self):
        for name in ("cpu_seconds", "memory_mb", "disk_mb",
                     "wall_clock_seconds", "max_processes"):
            value = int(getattr(self, name))
            if value < 1:
                raise ConfigurationError(f"{name} must be >= 1", **{name: value})
            object.__setattr__(self, name, value)
        if self.wall_clock_seconds < self.cpu_seconds:
            raise ConfigurationError(
                "wall clock must be at least the CPU budget; a worker cannot "
                "burn 60 CPU-seconds inside a 30-second wall clock, so the "
                "CPU limit would never be the thing that fired",
                cpu_seconds=self.cpu_seconds,
                wall_clock_seconds=self.wall_clock_seconds)

    def to_dict(self) -> dict:
        return {"cpu_seconds": self.cpu_seconds, "memory_mb": self.memory_mb,
                "disk_mb": self.disk_mb,
                "wall_clock_seconds": self.wall_clock_seconds,
                "max_processes": self.max_processes}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ComputeBudget":
        return cls(cpu_seconds=int(data.get("cpu_seconds", 60)),
                   memory_mb=int(data.get("memory_mb", 512)),
                   disk_mb=int(data.get("disk_mb", 64)),
                   wall_clock_seconds=int(data.get("wall_clock_seconds", 120)),
                   max_processes=int(data.get("max_processes", 8)))


@dataclass(frozen=True)
class ExperimentDesign:
    """Pre-registered. Specified before the experiment runs, not after.

    The same discipline `hypotheses.ExperimentPlan` enforces, with the fields a
    native model experiment needs: which arms, which datasets, which split, how
    many walk-forward folds, and what is held fixed.
    """

    description: str
    arms: tuple[str, ...]
    baselines: tuple[str, ...]
    controls: tuple[str, ...] = ()
    dataset_ids: tuple[str, ...] = ()
    walk_forward_folds: int = 0
    minimum_observations: int = 30
    seeds: tuple[int, ...] = (0,)

    def __post_init__(self):
        for name in ("arms", "baselines", "controls", "dataset_ids"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(self, "seeds", tuple(int(s) for s in self.seeds))
        if not str(self.description).strip():
            raise ConfigurationError("an experiment design needs a description")
        if len(self.arms) < 1:
            raise ConfigurationError("an experiment needs at least one arm")
        if not self.baselines:
            raise ConfigurationError(
                "an experiment must name at least one baseline; a result with "
                "nothing to beat cannot show improvement")
        if set(self.arms) & set(self.baselines):
            raise ConfigurationError(
                "an arm cannot also be its own baseline",
                overlapping=sorted(set(self.arms) & set(self.baselines)))
        if not self.seeds:
            raise ConfigurationError(
                "an experiment must name its seeds; a result nobody can "
                "reproduce is an anecdote")
        if int(self.minimum_observations) < 1:
            raise ConfigurationError("minimum_observations must be >= 1")

    def to_dict(self) -> dict:
        return {"description": self.description, "arms": list(self.arms),
                "baselines": list(self.baselines),
                "controls": list(self.controls),
                "dataset_ids": list(self.dataset_ids),
                "walk_forward_folds": self.walk_forward_folds,
                "minimum_observations": self.minimum_observations,
                "seeds": list(self.seeds)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExperimentDesign":
        return cls(description=str(data.get("description", "")),
                   arms=tuple(data.get("arms") or ()),
                   baselines=tuple(data.get("baselines") or ()),
                   controls=tuple(data.get("controls") or ()),
                   dataset_ids=tuple(data.get("dataset_ids") or ()),
                   walk_forward_folds=int(data.get("walk_forward_folds", 0)),
                   minimum_observations=int(data.get("minimum_observations", 30)),
                   seeds=tuple(data.get("seeds") or (0,)))


def _normalise(text: str) -> str:
    return " ".join(str(text).lower().split())


@dataclass(frozen=True)
class ChallengerProposal:
    """A challenger, with all eleven required fields present at construction."""

    proposal_id: str
    kind: ChallengerKind
    created_at: datetime

    # -- the eleven --------------------------------------------------------
    hypothesis: str
    supporting_evidence: tuple[str, ...]
    contradicting_evidence: tuple[str, ...]
    required_data: tuple[str, ...]
    experiment: ExperimentDesign
    success_criteria: tuple[str, ...]
    failure_criteria: tuple[str, ...]
    compute_budget: ComputeBudget
    security_impact: SecurityImpact
    operational_impact: OperationalImpact
    rollback_plan: str
    #: The deployment the rollback plan restores. Required whenever the
    #: proposal would change anything that is deployed.
    rollback_target: str = ""

    # -- provenance --------------------------------------------------------
    proposed_by: str = "olympus"
    #: Weakness findings or drift signals this came from, so the chain back to
    #: the measurement is machine-followable.
    triggered_by: tuple[str, ...] = ()
    instruments: tuple[str, ...] = ()
    regimes: tuple[str, ...] = ()
    notes: str = ""
    schema_version: int = CHALLENGER_SCHEMA_VERSION

    def __post_init__(self):
        object.__setattr__(self, "kind", ChallengerKind(self.kind))
        object.__setattr__(self, "security_impact",
                           SecurityImpact(self.security_impact))
        object.__setattr__(self, "operational_impact",
                           OperationalImpact(self.operational_impact))
        object.__setattr__(self, "created_at",
                           ensure_utc(self.created_at, field_name="created_at"))
        for name in ("supporting_evidence", "contradicting_evidence",
                     "required_data", "success_criteria", "failure_criteria",
                     "triggered_by", "instruments", "regimes"):
            object.__setattr__(self, name, tuple(getattr(self, name)))

        for name in ("proposal_id", "hypothesis", "rollback_plan"):
            if not str(getattr(self, name)).strip():
                raise ConfigurationError(f"{name} must be non-empty",
                                         proposal_id=self.proposal_id)

        for name in ("supporting_evidence", "contradicting_evidence",
                     "required_data", "success_criteria", "failure_criteria"):
            if not getattr(self, name):
                raise ConfigurationError(
                    f"{name} is required; a proposal missing it is a proposal "
                    f"nobody can review on its merits",
                    proposal_id=self.proposal_id, missing=name)

        overlap = ({_normalise(c) for c in self.success_criteria}
                   & {_normalise(c) for c in self.failure_criteria})
        if overlap:
            raise ConfigurationError(
                "success and failure criteria overlap, so no result could "
                "distinguish them", proposal_id=self.proposal_id,
                overlapping=sorted(overlap))

        if self.security_impact is SecurityImpact.KERNEL:
            raise ConfigurationError(
                "a challenger may not carry kernel impact; the only route to "
                "a kernel recommendation is kernel.propose_kernel_change, "
                "which produces a document and applies nothing",
                proposal_id=self.proposal_id)

        if (self.operational_impact is not OperationalImpact.NONE
                and not str(self.rollback_target).strip()):
            raise ConfigurationError(
                "a proposal with operational impact must name what its "
                "rollback restores; 'revert the change' is not a plan anyone "
                "can execute at three in the morning",
                proposal_id=self.proposal_id,
                operational_impact=self.operational_impact.value)

    # -- properties --------------------------------------------------------

    @property
    def falsifiable(self) -> bool:
        """True by construction. Kept as a property so it can be asserted."""
        return bool(self.success_criteria and self.failure_criteria)

    @property
    def autonomously_proposable(self) -> bool:
        """May Olympus generate this without an operator?

        Generating is not running and is certainly not deploying. What this
        gates is whether the *proposal itself* reaches something an operator
        must see before an experiment is even designed around it.
        """
        return self.security_impact in AUTONOMOUS_SECURITY_IMPACTS

    @property
    def needs_human_review(self) -> bool:
        return (not self.autonomously_proposable
                or self.operational_impact in (OperationalImpact.NEW_INGESTION,
                                               OperationalImpact.CONTRACT_CHANGE))

    @property
    def adds_complexity(self) -> bool:
        return self.kind in COMPLEXITY_ADDING

    def fingerprint(self) -> str:
        """Stable identity over the content, so a duplicate is detectable."""
        payload = "|".join([
            self.kind.value, _normalise(self.hypothesis),
            "|".join(sorted(_normalise(c) for c in self.success_criteria)),
            "|".join(sorted(_normalise(c) for c in self.failure_criteria)),
            "|".join(sorted(self.required_data))])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {"schema_version": self.schema_version,
                "proposal_id": self.proposal_id, "kind": self.kind.value,
                "created_at": jsonable(self.created_at),
                "hypothesis": self.hypothesis,
                "supporting_evidence": list(self.supporting_evidence),
                "contradicting_evidence": list(self.contradicting_evidence),
                "required_data": list(self.required_data),
                "experiment": self.experiment.to_dict(),
                "success_criteria": list(self.success_criteria),
                "failure_criteria": list(self.failure_criteria),
                "compute_budget": self.compute_budget.to_dict(),
                "security_impact": self.security_impact.value,
                "operational_impact": self.operational_impact.value,
                "rollback_plan": self.rollback_plan,
                "rollback_target": self.rollback_target,
                "proposed_by": self.proposed_by,
                "triggered_by": list(self.triggered_by),
                "instruments": list(self.instruments),
                "regimes": list(self.regimes),
                "adds_complexity": self.adds_complexity,
                "needs_human_review": self.needs_human_review,
                "fingerprint": self.fingerprint(),
                "notes": self.notes}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ChallengerProposal":
        created = data["created_at"]
        if isinstance(created, str):
            created = datetime.fromisoformat(created)
        return cls(
            proposal_id=str(data["proposal_id"]),
            kind=ChallengerKind(data["kind"]),
            created_at=created,
            hypothesis=str(data["hypothesis"]),
            supporting_evidence=tuple(data.get("supporting_evidence") or ()),
            contradicting_evidence=tuple(data.get("contradicting_evidence") or ()),
            required_data=tuple(data.get("required_data") or ()),
            experiment=ExperimentDesign.from_dict(data["experiment"]),
            success_criteria=tuple(data.get("success_criteria") or ()),
            failure_criteria=tuple(data.get("failure_criteria") or ()),
            compute_budget=ComputeBudget.from_dict(data.get("compute_budget") or {}),
            security_impact=SecurityImpact(data.get("security_impact", "none")),
            operational_impact=OperationalImpact(
                data.get("operational_impact", "none")),
            rollback_plan=str(data.get("rollback_plan", "")),
            rollback_target=str(data.get("rollback_target", "")),
            proposed_by=str(data.get("proposed_by", "olympus")),
            triggered_by=tuple(data.get("triggered_by") or ()),
            instruments=tuple(data.get("instruments") or ()),
            regimes=tuple(data.get("regimes") or ()),
            notes=str(data.get("notes", "")))

    def to_research_proposal(self) -> Any:
        """Convert into the framework's general `ResearchProposal`.

        The bridge into `hypotheses.py`, so a native challenger sits in the
        same backlog, obeys the same falsifiability rules and moves through the
        same status lifecycle as everything else. The four native-specific
        fields travel in `notes` rather than being dropped — a lossy conversion
        that silently discarded the compute budget would be a conversion that
        loses the enforcement.
        """
        from ..hypotheses import (ExperimentPlan, ProductionImpact,
                                  ResearchProposal, RiskLevel)
        plan = ExperimentPlan(
            kind=EXPERIMENT_KIND[self.kind],
            description=self.experiment.description,
            required_data=self.required_data,
            variants=self.experiment.arms,
            controls=self.experiment.controls,
            baselines=self.experiment.baselines,
            minimum_observations=self.experiment.minimum_observations,
            walk_forward_windows=self.experiment.walk_forward_folds,
            estimated_cost_units=float(self.compute_budget.cpu_seconds) / 60.0)
        risk = (RiskLevel.HIGH if self.needs_human_review
                else RiskLevel.MEDIUM if self.adds_complexity
                else RiskLevel.LOW)
        impact = (ProductionImpact.FEATURE_SET
                  if self.kind in (ChallengerKind.FEATURE,
                                   ChallengerKind.DATA_SOURCE)
                  else ProductionImpact.MODEL_SELECTION
                  if self.operational_impact is not OperationalImpact.NONE
                  else ProductionImpact.NONE)
        return ResearchProposal(
            proposal_id=self.proposal_id, hypothesis=self.hypothesis,
            motivation="; ".join(self.supporting_evidence),
            created_at=self.created_at, experiment=plan,
            success_criteria=self.success_criteria,
            failure_criteria=self.failure_criteria,
            risk_level=risk, production_impact=impact,
            supporting_observations=self.supporting_evidence,
            contradicting_evidence=self.contradicting_evidence,
            instruments=self.instruments,
            triggered_by=self.triggered_by, proposed_by=self.proposed_by,
            notes=(f"native challenger {self.kind.value}; "
                   f"compute {self.compute_budget.to_dict()}; "
                   f"security {self.security_impact.value}; "
                   f"operational {self.operational_impact.value}; "
                   f"rollback to {self.rollback_target or 'n/a'}: "
                   f"{self.rollback_plan}"))


#: Which of `hypotheses.ExperimentKind` each challenger maps onto, by value.
#: Stated as data rather than decided inside the conversion, so the mapping is
#: reviewable — and by *value* rather than by enum member so importing this
#: module does not pull in `hypotheses`.
EXPERIMENT_KIND: Mapping[ChallengerKind, str] = {
    ChallengerKind.REPRESENTATION: "model_comparison",
    ChallengerKind.ARCHITECTURE: "model_comparison",
    ChallengerKind.TASK_HEAD: "ablation",
    ChallengerKind.FEATURE: "feature_addition",
    ChallengerKind.HORIZON: "horizon_comparison",
    ChallengerKind.TRAINING_OBJECTIVE: "model_comparison",
    ChallengerKind.CALIBRATION: "walk_forward",
    ChallengerKind.REGIME_SPECIALIST: "regime_conditional",
    ChallengerKind.DATA_SOURCE: "feature_addition",
    ChallengerKind.SIMPLER_MODEL: "model_comparison",
}


# ---------------------------------------------------------------------------
# generation from measured weakness
# ---------------------------------------------------------------------------

#: Which challenger kind each weakness most naturally suggests. One mapping,
#: reviewable as data. It is a *suggestion*: the generator also emits a
#: simplification counterpart, and a human may propose anything.
WEAKNESS_TO_KIND: Mapping[WeaknessKind, ChallengerKind] = {
    WeaknessKind.MODEL_DETERIORATION: ChallengerKind.TRAINING_OBJECTIVE,
    WeaknessKind.REGIME_WEAKNESS: ChallengerKind.REGIME_SPECIALIST,
    WeaknessKind.POOR_CALIBRATION: ChallengerKind.CALIBRATION,
    WeaknessKind.EXCESS_CONFIDENCE: ChallengerKind.CALIBRATION,
    WeaknessKind.UNNECESSARY_ABSTENTION: ChallengerKind.CALIBRATION,
    WeaknessKind.INSUFFICIENT_ABSTENTION: ChallengerKind.TASK_HEAD,
    WeaknessKind.INSTRUMENT_FAILURE: ChallengerKind.FEATURE,
    WeaknessKind.TIMEFRAME_FAILURE: ChallengerKind.HORIZON,
    WeaknessKind.DATA_QUALITY_FAILURE: ChallengerKind.FEATURE,
    WeaknessKind.EXECUTION_MODEL_ERROR: ChallengerKind.DATA_SOURCE,
}

#: What each challenger kind proposes, in one sentence. Data so the generated
#: hypothesis text is reviewable without reading the generator.
KIND_HYPOTHESIS: Mapping[ChallengerKind, str] = {
    ChallengerKind.REPRESENTATION:
        "a different market-state encoder recovers structure the current one "
        "discards",
    ChallengerKind.ARCHITECTURE:
        "a different temporal architecture models this dependency better at "
        "the same parameter count",
    ChallengerKind.TASK_HEAD:
        "an additional supervised head gives the shared trunk a signal it is "
        "currently missing",
    ChallengerKind.FEATURE:
        "an input channel the model does not currently read carries "
        "information about this failure",
    ChallengerKind.HORIZON:
        "the trained horizon is mismatched to the horizon being asked for",
    ChallengerKind.TRAINING_OBJECTIVE:
        "the loss is optimising something other than the quantity being "
        "evaluated",
    ChallengerKind.CALIBRATION:
        "a post-hoc calibration step corrects the stated uncertainty without "
        "changing the point forecast",
    ChallengerKind.REGIME_SPECIALIST:
        "a model fitted to this regime alone outperforms the generalist on it",
    ChallengerKind.DATA_SOURCE:
        "a data source Olympus does not currently ingest would resolve this",
    ChallengerKind.SIMPLER_MODEL:
        "a materially simpler model matches the incumbent, and the complexity "
        "is not earning its place",
}


def _pid(seed: str) -> str:
    return "ch-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


def from_weakness(finding: WeaknessFinding, *, created_at: datetime,
                  dataset_ids: Sequence[str] = (),
                  champion_arm: str = "champion") -> ChallengerProposal:
    """Turn one measured weakness into one challenger.

    The supporting evidence is the finding's own measurement, quoted with its
    sample size. The contradicting evidence is the finding's provisionality
    when it is provisional — a small sample *is* evidence against acting, and
    recording it here means the reviewer sees it without going back to the
    journal.
    """
    kind = WEAKNESS_TO_KIND[finding.kind]
    supporting = (
        f"{finding.kind.value} on {finding.scope}: {finding.detail}",
        f"measured {finding.measured:.4g} against a threshold of "
        f"{finding.threshold:.4g} over n={finding.n}",
        finding.note,
    )
    contradicting = (
        (f"n={finding.n} is below the {30} required to act on a finding, so "
         f"this may be noise",)
        if finding.provisional else (NO_CONTRADICTION,))
    arm = f"{kind.value}_challenger"
    return ChallengerProposal(
        proposal_id=_pid(f"{finding.kind.value}|{finding.scope}|"
                         f"{finding.model_version}|{created_at.isoformat()}"),
        kind=kind, created_at=created_at,
        hypothesis=(f"{KIND_HYPOTHESIS[kind]} — specifically for "
                    f"{finding.scope}, where {finding.detail}"),
        supporting_evidence=supporting,
        contradicting_evidence=contradicting,
        required_data=tuple(dataset_ids) or ("the evidence journal for "
                                             f"{finding.scope}",),
        experiment=ExperimentDesign(
            description=(f"Fit {arm} and the incumbent on identical splits, "
                         f"score both on the same cost model and metric set, "
                         f"and report {finding.scope} separately from the "
                         f"overall result."),
            arms=(arm,), baselines=(champion_arm, "persistence"),
            controls=("dataset", "split", "cost model", "risk limits",
                      "metric set"),
            dataset_ids=tuple(dataset_ids),
            walk_forward_folds=3,
            minimum_observations=max(30, finding.n)),
        success_criteria=(
            f"{finding.kind.value} on {finding.scope} falls below "
            f"{finding.threshold:.4g}",
            "the overall metric set does not regress",
            "no other stratum with a usable sample gets materially worse"),
        failure_criteria=(
            f"{finding.kind.value} on {finding.scope} does not fall below "
            f"{finding.threshold:.4g}",
            "any stratum with a usable sample gets materially worse",
            "the challenger does not beat persistence after costs"),
        compute_budget=ComputeBudget(),
        security_impact=(SecurityImpact.NEW_EXTERNAL_INPUT
                         if kind is ChallengerKind.DATA_SOURCE
                         else SecurityImpact.NONE),
        operational_impact=(OperationalImpact.NEW_INGESTION
                            if kind is ChallengerKind.DATA_SOURCE
                            else OperationalImpact.RETRAIN_ONLY),
        rollback_plan=(f"restore the incumbent checkpoint {finding.model_version} "
                       f"via rollback.DeploymentLedger and re-run the "
                       f"evaluation to confirm the prior numbers reproduce"),
        rollback_target=finding.model_version or champion_arm,
        triggered_by=(f"{finding.kind.value}:{finding.scope}",),
        regimes=((finding.scope.split("=", 1)[1],)
                 if finding.scope.startswith("regime=") else ()),
        instruments=((finding.scope.split("=", 1)[1],)
                     if finding.scope.startswith("instrument=") else ()))


def simplification_of(proposal: ChallengerProposal) -> ChallengerProposal:
    """The parsimony counterpart to a complexity-adding challenger.

    Emitted alongside, not instead. `champion.compare()` breaks a statistical
    tie toward the simpler arm, which only helps if a simpler arm was entered —
    and left to itself, a system generating proposals from failures generates
    additions, because a failure looks like something missing.
    """
    return ChallengerProposal(
        proposal_id=_pid(f"simpler|{proposal.proposal_id}"),
        kind=ChallengerKind.SIMPLER_MODEL, created_at=proposal.created_at,
        hypothesis=(f"{KIND_HYPOTHESIS[ChallengerKind.SIMPLER_MODEL]} — run "
                    f"against the same weakness as {proposal.proposal_id}, so "
                    f"the added machinery has to beat removing machinery"),
        supporting_evidence=proposal.supporting_evidence + (
            "Phase 1 measured a 19-parameter AR(3) fit beating the native "
            "network on a linear synthetic process; complexity has lost here "
            "before",),
        contradicting_evidence=(
            "the weakness this addresses was found in the incumbent, and a "
            "simpler model is not obviously more likely to fix it",),
        required_data=proposal.required_data,
        experiment=ExperimentDesign(
            description=("Fit a reduced model — fewer parameters, fewer heads "
                         "— on the identical split and score it on the "
                         "identical metric set."),
            arms=("reduced_model",),
            baselines=proposal.experiment.baselines,
            controls=proposal.experiment.controls,
            dataset_ids=proposal.experiment.dataset_ids,
            walk_forward_folds=proposal.experiment.walk_forward_folds,
            minimum_observations=proposal.experiment.minimum_observations),
        success_criteria=(
            "the reduced model is statistically indistinguishable from the "
            "incumbent on the primary metric",
            "the reduced model uses materially fewer parameters"),
        failure_criteria=(
            "the reduced model is materially worse on the primary metric",
            "the reduced model is not materially smaller"),
        compute_budget=proposal.compute_budget,
        security_impact=SecurityImpact.NONE,
        operational_impact=OperationalImpact.RETRAIN_ONLY,
        rollback_plan=proposal.rollback_plan,
        rollback_target=proposal.rollback_target,
        triggered_by=proposal.triggered_by + (proposal.proposal_id,),
        instruments=proposal.instruments, regimes=proposal.regimes,
        notes=f"parsimony counterpart to {proposal.proposal_id}")


def generate(findings: Sequence[WeaknessFinding], *, created_at: datetime,
             dataset_ids: Sequence[str] = (),
             include_provisional: bool = False,
             champion_arm: str = "champion") -> list[ChallengerProposal]:
    """One challenger per actionable weakness, plus its parsimony counterpart.

    Provisional findings are skipped by default. A proposal generated from
    three observations would consume a compute budget to test noise, and the
    finding is still in the journal for when the sample grows.
    """
    out: list[ChallengerProposal] = []
    seen: set[str] = set()
    for finding in findings:
        if finding.provisional and not include_provisional:
            continue
        proposal = from_weakness(finding, created_at=created_at,
                                 dataset_ids=dataset_ids,
                                 champion_arm=champion_arm)
        if proposal.fingerprint() in seen:
            continue
        seen.add(proposal.fingerprint())
        out.append(proposal)
        if proposal.adds_complexity:
            counterpart = simplification_of(proposal)
            if counterpart.fingerprint() not in seen:
                seen.add(counterpart.fingerprint())
                out.append(counterpart)
    return out


def proposal_report(proposals: Sequence[ChallengerProposal]) -> dict:
    """The backlog, as data. Feeds the self-evolution document."""
    by_kind: dict[str, int] = {}
    for proposal in proposals:
        by_kind[proposal.kind.value] = by_kind.get(proposal.kind.value, 0) + 1
    return {"schema_version": CHALLENGER_SCHEMA_VERSION,
            "n": len(proposals),
            "by_kind": dict(sorted(by_kind.items())),
            "adding_complexity": sum(1 for p in proposals if p.adds_complexity),
            "simplifying": sum(1 for p in proposals
                               if p.kind is ChallengerKind.SIMPLER_MODEL),
            "needing_human_review": [p.proposal_id for p in proposals
                                     if p.needs_human_review],
            "total_cpu_seconds": sum(p.compute_budget.cpu_seconds
                                     for p in proposals)}


__all__ = ["CHALLENGER_SCHEMA_VERSION", "NO_CONTRADICTION", "ChallengerKind",
           "COMPLEXITY_ADDING", "SecurityImpact", "OperationalImpact",
           "AUTONOMOUS_SECURITY_IMPACTS", "ComputeBudget", "ExperimentDesign",
           "ChallengerProposal", "EXPERIMENT_KIND", "WEAKNESS_TO_KIND",
           "KIND_HYPOTHESIS",
           "from_weakness", "simplification_of", "generate", "proposal_report"]

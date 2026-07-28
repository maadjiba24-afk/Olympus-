"""Structured research proposals: what Olympus wants to find out, and how.

A hypothesis is not knowledge. This module makes that a structural fact rather
than a slogan: a `ResearchProposal` cannot be constructed without stating, up
front, what would make it *false*. `failure_criteria` is a required, non-empty
field, and `success_criteria` must not be a restatement of it. A proposal whose
author cannot say what would refute it is not a research proposal, it is a
preference, and the constructor refuses it.

`HypothesisEngine` generates proposals from evidence Olympus already has —
weakness findings from `outcomes.py` and drift signals from `drift.py`. Each
generator is a small, named function with an explicit trigger, so the answer to
"why is Olympus proposing this" is always a specific measurement rather than an
inference nobody can retrace.

The engine deliberately generates *fewer* hypotheses than it could. Every
generator requires a minimum sample and cites it in the proposal's supporting
observations. A hypothesis engine that fires on every noisy week produces a
research backlog nobody reads, which is functionally the same as producing
nothing while looking productive.

Nothing here runs an experiment. Proposals go to `lab.py`, which is where
isolation is enforced.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from .clock import Clock, default_clock
from .contracts import Regime, ensure_utc, jsonable
from .errors import ConfigurationError
from .governance import Action, Actor, authorise
from .storekeys import KeyedStore

_NS = "trading.hypotheses"


class RiskLevel(str, Enum):
    """How much could go wrong if this research were acted on incorrectly."""

    #: Analysis only; no production component would change.
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    #: Would change sizing, stops, or which instruments are traded.
    HIGH = "high"


class ProductionImpact(str, Enum):
    """What a positive result would touch. Determines the review path."""

    NONE = "none"
    REPORTING_ONLY = "reporting_only"
    FEATURE_SET = "feature_set"
    MODEL_SELECTION = "model_selection"
    STRATEGY_PARAMETERS = "strategy_parameters"
    STRATEGY_LOGIC = "strategy_logic"
    EXECUTION_RULES = "execution_rules"
    #: Named so a proposal that would need one is visible immediately. Nothing
    #: in this module can act on it; it routes to a human.
    RISK_POLICY = "risk_policy"


class ExperimentKind(str, Enum):
    ABLATION = "ablation"
    WALK_FORWARD = "walk_forward"
    PARAMETER_SWEEP = "parameter_sweep"
    HORIZON_COMPARISON = "horizon_comparison"
    FEATURE_ADDITION = "feature_addition"
    MODEL_COMPARISON = "model_comparison"
    ROBUSTNESS = "robustness"
    COST_SENSITIVITY = "cost_sensitivity"
    REGIME_CONDITIONAL = "regime_conditional"
    FALSIFICATION = "falsification"


class ProposalStatus(str, Enum):
    DRAFT = "draft"
    QUEUED = "queued"
    RUNNING = "running"
    #: Ran and the success criteria were met.
    SUPPORTED = "supported"
    #: Ran and the failure criteria were met. A real, useful result.
    REFUTED = "refuted"
    #: Ran and neither criterion was met. Also a result — usually "not enough
    #: data", which is worth knowing before spending more.
    INCONCLUSIVE = "inconclusive"
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True)
class ExperimentPlan:
    """How the hypothesis will be tested, specified before it is run.

    Pre-registration, in the scientific sense. Specifying the design after
    seeing the data is how a research process talks itself into whatever it
    already believed, and a self-evolving system doing that would compound the
    error rather than the intelligence.
    """

    kind: ExperimentKind
    description: str
    #: Instruments, timeframes and date ranges the experiment needs.
    required_data: tuple[str, ...] = ()
    #: What is varied. Everything else must be held identical.
    variants: tuple[str, ...] = ()
    #: What is held fixed. Stated explicitly so an unmatched comparison is
    #: visible in review rather than discovered afterwards.
    controls: tuple[str, ...] = ()
    baselines: tuple[str, ...] = ()
    minimum_observations: int = 30
    walk_forward_windows: int = 0
    estimated_cost_units: float = 1.0

    def __post_init__(self):
        object.__setattr__(self, "kind", ExperimentKind(self.kind))
        for name in ("required_data", "variants", "controls", "baselines"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if not str(self.description).strip():
            raise ConfigurationError("an experiment plan needs a description")
        if not self.baselines:
            raise ConfigurationError(
                "an experiment plan must name at least one baseline; a result "
                "with nothing to beat cannot show improvement",
                kind=self.kind.value)
        if int(self.minimum_observations) < 1:
            raise ConfigurationError("minimum_observations must be positive")

    def to_dict(self) -> dict:
        return {"kind": self.kind.value, "description": self.description,
                "required_data": list(self.required_data),
                "variants": list(self.variants), "controls": list(self.controls),
                "baselines": list(self.baselines),
                "minimum_observations": self.minimum_observations,
                "walk_forward_windows": self.walk_forward_windows,
                "estimated_cost_units": self.estimated_cost_units}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExperimentPlan":
        return cls(kind=ExperimentKind(data["kind"]),
                   description=str(data.get("description", "")),
                   required_data=tuple(data.get("required_data") or ()),
                   variants=tuple(data.get("variants") or ()),
                   controls=tuple(data.get("controls") or ()),
                   baselines=tuple(data.get("baselines") or ()),
                   minimum_observations=int(data.get("minimum_observations", 30)),
                   walk_forward_windows=int(data.get("walk_forward_windows", 0)),
                   estimated_cost_units=float(data.get("estimated_cost_units", 1.0)))


def _normalise(text: str) -> str:
    return " ".join(str(text).lower().split())


@dataclass(frozen=True)
class ResearchProposal:
    """A hypothesis, with everything needed to test and judge it."""

    proposal_id: str
    hypothesis: str
    motivation: str
    created_at: datetime
    experiment: ExperimentPlan
    success_criteria: tuple[str, ...]
    failure_criteria: tuple[str, ...]
    risk_level: RiskLevel = RiskLevel.LOW
    production_impact: ProductionImpact = ProductionImpact.NONE
    supporting_observations: tuple[str, ...] = ()
    contradicting_evidence: tuple[str, ...] = ()
    instruments: tuple[str, ...] = ()
    regimes: tuple[Regime, ...] = ()
    status: ProposalStatus = ProposalStatus.DRAFT
    proposed_by: str = "olympus"
    #: Set when the proposal was generated from a specific measurement, so the
    #: chain back to the evidence is machine-followable.
    triggered_by: tuple[str, ...] = ()
    notes: str = ""

    def __post_init__(self):
        for name, value in (("proposal_id", self.proposal_id),
                            ("hypothesis", self.hypothesis),
                            ("motivation", self.motivation)):
            if not str(value).strip():
                raise ConfigurationError(f"{name} must be non-empty")
        object.__setattr__(self, "created_at",
                           ensure_utc(self.created_at, field_name="created_at"))
        object.__setattr__(self, "risk_level", RiskLevel(self.risk_level))
        object.__setattr__(self, "production_impact",
                           ProductionImpact(self.production_impact))
        object.__setattr__(self, "status", ProposalStatus(self.status))
        object.__setattr__(self, "regimes", tuple(Regime(r) for r in self.regimes))
        for name in ("success_criteria", "failure_criteria",
                     "supporting_observations", "contradicting_evidence",
                     "instruments", "triggered_by"):
            object.__setattr__(self, name, tuple(getattr(self, name)))

        if not self.success_criteria:
            raise ConfigurationError(
                "a proposal must state what would count as success",
                proposal_id=self.proposal_id)
        if not self.failure_criteria:
            raise ConfigurationError(
                "a proposal must state what would refute it; a hypothesis with "
                "no failure condition cannot be tested, only agreed with",
                proposal_id=self.proposal_id)
        overlap = ({_normalise(c) for c in self.success_criteria}
                   & {_normalise(c) for c in self.failure_criteria})
        if overlap:
            raise ConfigurationError(
                "success and failure criteria overlap, so no result could "
                "distinguish them", proposal_id=self.proposal_id,
                overlapping=sorted(overlap))
        if (self.production_impact is ProductionImpact.RISK_POLICY
                and self.risk_level is not RiskLevel.HIGH):
            raise ConfigurationError(
                "a proposal whose positive result would touch risk policy is "
                "high risk by definition", proposal_id=self.proposal_id,
                risk_level=self.risk_level.value)

    @property
    def testable(self) -> bool:
        """Both criteria present and distinguishable. True by construction."""
        return bool(self.success_criteria and self.failure_criteria)

    @property
    def needs_human_review(self) -> bool:
        """Would a positive result reach something a human must sign off?"""
        return (self.risk_level is RiskLevel.HIGH
                or self.production_impact in (ProductionImpact.RISK_POLICY,
                                              ProductionImpact.EXECUTION_RULES,
                                              ProductionImpact.STRATEGY_LOGIC))

    def to_dict(self) -> dict:
        return {"proposal_id": self.proposal_id, "hypothesis": self.hypothesis,
                "motivation": self.motivation,
                "created_at": jsonable(self.created_at),
                "experiment": self.experiment.to_dict(),
                "success_criteria": list(self.success_criteria),
                "failure_criteria": list(self.failure_criteria),
                "risk_level": self.risk_level.value,
                "production_impact": self.production_impact.value,
                "supporting_observations": list(self.supporting_observations),
                "contradicting_evidence": list(self.contradicting_evidence),
                "instruments": list(self.instruments),
                "regimes": [r.value for r in self.regimes],
                "status": self.status.value, "proposed_by": self.proposed_by,
                "triggered_by": list(self.triggered_by), "notes": self.notes,
                "needs_human_review": self.needs_human_review,
                "estimated_cost_units": self.experiment.estimated_cost_units}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResearchProposal":
        return cls(
            proposal_id=str(data.get("proposal_id", "")),
            hypothesis=str(data.get("hypothesis", "")),
            motivation=str(data.get("motivation", "")),
            created_at=datetime.fromisoformat(data["created_at"]),
            experiment=ExperimentPlan.from_dict(data["experiment"]),
            success_criteria=tuple(data.get("success_criteria") or ()),
            failure_criteria=tuple(data.get("failure_criteria") or ()),
            risk_level=RiskLevel(data.get("risk_level", RiskLevel.LOW.value)),
            production_impact=ProductionImpact(
                data.get("production_impact", ProductionImpact.NONE.value)),
            supporting_observations=tuple(data.get("supporting_observations") or ()),
            contradicting_evidence=tuple(data.get("contradicting_evidence") or ()),
            instruments=tuple(data.get("instruments") or ()),
            regimes=tuple(Regime(r) for r in data.get("regimes") or ()),
            status=ProposalStatus(data.get("status", ProposalStatus.DRAFT.value)),
            proposed_by=str(data.get("proposed_by", "olympus")),
            triggered_by=tuple(data.get("triggered_by") or ()),
            notes=str(data.get("notes", "")))

    def evolve(self, **changes) -> "ResearchProposal":
        data = {f: getattr(self, f) for f in self.__dataclass_fields__}
        data.update(changes)
        return ResearchProposal(**data)

    def fingerprint(self) -> str:
        """Content hash of the claim, for suppressing duplicate proposals."""
        payload = json.dumps({"hypothesis": _normalise(self.hypothesis),
                              "instruments": sorted(self.instruments),
                              "kind": self.experiment.kind.value}, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _pid(seed: str) -> str:
    return "hyp-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# generators
# ---------------------------------------------------------------------------
# Each takes measured evidence and returns proposals, or nothing. They are
# separate functions rather than branches in one method so that a generator can
# be tested — and disabled — on its own.


def from_weakness(finding: Any, *, clock: Clock | None = None
                  ) -> ResearchProposal | None:
    """Turn a systematic weakness into a hypothesis about its cause.

    The mapping is deliberately conservative: a weakness names a symptom, and
    the generated hypothesis proposes the *simplest* mechanism that would
    produce it. Proposing an elaborate cause for a simple symptom is how a
    research programme ends up testing its own cleverness.
    """
    now = (clock or default_clock()).now()
    scope = getattr(finding, "scope", "")
    axis = getattr(finding, "axis", "")
    sample = int(getattr(finding, "sample", 0) or 0)
    rate = float(getattr(finding, "rate", 0.0) or 0.0)
    detail = getattr(finding, "detail", "")
    kind, _, scope_value = scope.partition(":")
    instruments = (scope_value,) if kind == "instrument" and scope_value else ()
    regimes = ()
    if kind == "regime" and scope_value:
        try:
            regimes = (Regime(scope_value),)
        except ValueError:
            regimes = ()

    observations = (f"{axis} on {scope}: {detail}",
                    f"measured over {sample} decisions")

    templates: dict[str, dict] = {
        "directional_accuracy": dict(
            hypothesis=f"The directional signal on {scope} is inverted or "
                       "misaligned rather than uninformative.",
            motivation="Accuracy reliably below chance is not weakness; it is "
                       "usually a sign error or an off-by-one in feature "
                       "alignment, which is cheap to test and cheap to fix.",
            experiment=ExperimentPlan(
                kind=ExperimentKind.ABLATION,
                description="Re-score the same decisions with the signal "
                            "inverted, and separately with the feature window "
                            "shifted by one bar.",
                variants=("signal_inverted", "feature_lag_shift"),
                controls=("same decisions", "same costs", "same risk limits"),
                baselines=("as-recorded signal", "always flat"),
                minimum_observations=max(30, sample)),
            success=("inverted or lag-shifted variant reaches directional "
                     "accuracy above 0.55 on the same sample",),
            failure=("neither variant exceeds 0.50, indicating the signal "
                     "carries no directional information at all",),
            impact=ProductionImpact.FEATURE_SET, risk=RiskLevel.MEDIUM),
        "over_trading": dict(
            hypothesis=f"The strategy on {scope} should abstain far more often; "
                       "its edge does not survive its own costs.",
            motivation="Doing nothing beat the decision in most of the sample. "
                       "The cheapest possible improvement is to trade less.",
            experiment=ExperimentPlan(
                kind=ExperimentKind.PARAMETER_SWEEP,
                description="Sweep the conviction and confidence thresholds "
                            "upward and re-run walk-forward with identical "
                            "costs.",
                variants=("threshold sweep over conviction and confidence",),
                controls=("same data", "same cost model", "same sizing"),
                baselines=("current thresholds", "always flat"),
                minimum_observations=max(30, sample), walk_forward_windows=4),
            success=("some threshold produces net performance above the "
                     "current configuration and above flat, out of sample",),
            failure=("no threshold beats flat out of sample, meaning the "
                     "strategy should be disabled rather than tuned",),
            impact=ProductionImpact.STRATEGY_PARAMETERS, risk=RiskLevel.MEDIUM),
        "stop_quality": dict(
            hypothesis=f"Stops on {scope} are tighter than the instrument's own "
                       "noise, so they convert winning theses into losses.",
            motivation="Stop-outs occurring after the trade had already moved "
                       "further in favour than the stop was wide indicate a "
                       "sizing problem, not a forecasting one.",
            experiment=ExperimentPlan(
                kind=ExperimentKind.PARAMETER_SWEEP,
                description="Sweep stop distance as a multiple of realised "
                            "volatility; measure stop-out rate and net return.",
                variants=("stop multiple 1.0×–4.0× ATR",),
                controls=("same entries", "same targets", "same costs"),
                baselines=("current stop distance",),
                minimum_observations=max(30, sample), walk_forward_windows=4),
            success=("a wider stop improves net return out of sample without "
                     "increasing maximum adverse excursion per trade beyond "
                     "the risk budget",),
            failure=("wider stops reduce net return, meaning the losses are "
                     "the thesis being wrong rather than the stop being tight",),
            impact=ProductionImpact.STRATEGY_PARAMETERS, risk=RiskLevel.HIGH),
        "execution_quality": dict(
            hypothesis=f"Execution costs on {scope} can be reduced by changing "
                       "order placement rather than by trading less.",
            motivation="Costs consuming a large share of gross P&L is an "
                       "execution problem before it is a strategy problem.",
            experiment=ExperimentPlan(
                kind=ExperimentKind.COST_SENSITIVITY,
                description="Simulate limit-at-touch and passive placement "
                            "against the recorded book, with realistic "
                            "non-fill rates.",
                variants=("marketable limit", "passive limit", "current"),
                controls=("same signals", "same sizes"),
                baselines=("current placement",),
                minimum_observations=max(30, sample)),
            success=("a placement rule reduces total cost per unit traded "
                     "without reducing fill rate below the level at which net "
                     "performance degrades",),
            failure=("no placement rule reduces net cost once non-fills are "
                     "modelled",),
            impact=ProductionImpact.EXECUTION_RULES, risk=RiskLevel.HIGH),
        "confidence_calibration": dict(
            hypothesis=f"Forecast confidence on {scope} no longer predicts "
                       "correctness and should be recalibrated or ignored.",
            motivation="Position sizes scale with confidence. Uncalibrated "
                       "confidence silently mis-sizes every position.",
            experiment=ExperimentPlan(
                kind=ExperimentKind.ABLATION,
                description="Compare sizing driven by stated confidence with "
                            "flat sizing and with isotonic-recalibrated "
                            "confidence.",
                variants=("raw confidence", "flat sizing", "recalibrated"),
                controls=("same signals", "same costs"),
                baselines=("flat sizing",),
                minimum_observations=max(30, sample)),
            success=("recalibrated confidence beats both raw confidence and "
                     "flat sizing out of sample",),
            failure=("flat sizing matches or beats both, meaning confidence "
                     "should not drive size at all",),
            impact=ProductionImpact.MODEL_SELECTION, risk=RiskLevel.MEDIUM),
        "versus_baseline": dict(
            hypothesis=f"The strategy on {scope} adds nothing over its baseline "
                       "and the added complexity is not earning its keep.",
            motivation="Underperforming a simple baseline on most decisions is "
                       "the strongest available argument for removing "
                       "machinery rather than adding it.",
            experiment=ExperimentPlan(
                kind=ExperimentKind.MODEL_COMPARISON,
                description="Head-to-head walk-forward of strategy against "
                            "baseline under identical costs and risk limits.",
                variants=("full strategy", "baseline only"),
                controls=("same data", "same costs", "same limits"),
                baselines=("baseline strategy", "buy and hold", "always flat"),
                minimum_observations=max(30, sample), walk_forward_windows=6),
            success=("the strategy beats every baseline on net risk-adjusted "
                     "return with p < 0.05 on a paired test",),
            failure=("the strategy fails to beat the simplest baseline, "
                     "supporting its deprecation",),
            impact=ProductionImpact.MODEL_SELECTION, risk=RiskLevel.MEDIUM),
    }

    template = templates.get(axis)
    if template is None:
        return None
    return ResearchProposal(
        proposal_id=_pid(f"weakness|{axis}|{scope}|{sample}"),
        hypothesis=template["hypothesis"], motivation=template["motivation"],
        created_at=now, experiment=template["experiment"],
        success_criteria=template["success"], failure_criteria=template["failure"],
        risk_level=template["risk"], production_impact=template["impact"],
        supporting_observations=observations, instruments=instruments,
        regimes=regimes, triggered_by=(f"weakness:{axis}:{scope}",),
        notes=f"generated from a measured weakness (rate {rate:.2f}, n={sample})")


def from_drift(signal: Any, *, clock: Clock | None = None
               ) -> ResearchProposal | None:
    """Turn a drift signal into a hypothesis about what changed.

    Only MAJOR and CRITICAL signals generate proposals. Minor drift is normal
    and generating research from it would bury the signals that matter.
    """
    severity = getattr(getattr(signal, "severity", None), "value", "")
    if severity not in ("major", "critical"):
        return None
    now = (clock or default_clock()).now()
    kind = getattr(getattr(signal, "kind", None), "value", "unknown")
    subject = getattr(signal, "subject", "")
    metric = getattr(signal, "metric", "")
    detail = getattr(signal, "detail", "")

    return ResearchProposal(
        proposal_id=_pid(f"drift|{kind}|{subject}|{metric}|{severity}"),
        hypothesis=f"The {kind} drift measured in {subject}.{metric} reflects a "
                   "durable change in the market rather than a transient one, "
                   "and the affected component needs refitting rather than "
                   "waiting out.",
        motivation="Distinguishing a regime change from noise decides between "
                   "two opposite actions — refit, or leave alone — and getting "
                   "it wrong is expensive in both directions.",
        created_at=now,
        experiment=ExperimentPlan(
            kind=ExperimentKind.REGIME_CONDITIONAL,
            description="Split the recent window into sub-periods and test "
                        "whether the shifted distribution persists across all "
                        "of them or is concentrated in one episode.",
            required_data=(f"{subject} recent and reference windows",),
            variants=("per-sub-period distributions",),
            controls=("identical binning", "identical sample sizes"),
            baselines=("reference window distribution",),
            minimum_observations=60),
        success_criteria=("the shift is present in a majority of sub-periods, "
                          "supporting a durable change",),
        failure_criteria=("the shift is confined to one episode, supporting "
                          "no action beyond monitoring",),
        risk_level=RiskLevel.MEDIUM,
        production_impact=ProductionImpact.MODEL_SELECTION,
        supporting_observations=(f"{severity} {kind} drift: {detail}",),
        triggered_by=(f"drift:{kind}:{subject}:{metric}",))


def model_conditional_value(model_name: str, *, instrument: str = "",
                            supporting_observations: Iterable[str] = (),
                            contradicting_evidence: Iterable[str] = (),
                            estimated_cost_units: float = 25.0,
                            clock: Clock | None = None) -> ResearchProposal:
    """The standing hypothesis about any forecasting model, stated so it can fail.

    Asks whether `model_name` adds value *conditionally* rather than uniformly.
    That framing matters: a model is usually adopted or dropped wholesale, and
    if its edge is real but confined to one volatility regime then neither
    choice is right and the conditional rule is worth more than the model.

    `contradicting_evidence` is a parameter rather than something the caller may
    omit and forget. A standing hypothesis that carried only the case *for* a
    model would be an advocacy document; the constructor takes both sides and
    the proposal records them together.

    Model-agnostic on purpose. The same question is asked of a third-party model
    and of an Olympus-native one, by the same code, so neither is judged on its
    provenance.
    """
    now = (clock or default_clock()).now()
    label = str(model_name).strip()
    if not label:
        raise ConfigurationError("model_conditional_value needs a model name")
    return ResearchProposal(
        proposal_id=_pid(f"conditional-value|{label}|{instrument}"),
        hypothesis=f"{label} adds measurable out-of-sample value only under "
                   "specific volatility conditions, and adds none or negative "
                   "value elsewhere.",
        motivation=f"{label} is currently either used everywhere or nowhere. "
                   "If its edge is conditional, the correct configuration is "
                   "neither, and the conditional rule is worth more than the "
                   "model.",
        created_at=now,
        experiment=ExperimentPlan(
            kind=ExperimentKind.REGIME_CONDITIONAL,
            description="Walk-forward the identical strategy with and without "
                        f"the {label} signal, partitioned by "
                        "realised-volatility tercile, under identical costs "
                        "and risk limits.",
            required_data=("out-of-sample OHLCV for the instrument set",
                           f"a pinned {label} checkpoint"),
            variants=(f"with {label}", f"without {label}"),
            controls=("identical sizing code", "identical exits",
                      "identical cost model", "identical risk limits"),
            baselines=(f"same strategy without {label}", "persistence",
                       "always flat"),
            minimum_observations=100, walk_forward_windows=6,
            estimated_cost_units=estimated_cost_units),
        success_criteria=(f"in at least one volatility tercile, the {label} arm "
                          f"beats the identical arm without {label} on net "
                          "return with p < 0.05 on a paired bootstrap",),
        failure_criteria=("no tercile shows a significant improvement, which "
                          f"would mean {label} should not be used for this "
                          "instrument set at all",),
        risk_level=RiskLevel.MEDIUM,
        production_impact=ProductionImpact.MODEL_SELECTION,
        supporting_observations=tuple(supporting_observations),
        contradicting_evidence=tuple(contradicting_evidence)
        or ("no out-of-sample evidence of this model's value has yet been "
            "produced in this system",),
        instruments=(instrument,) if instrument else (),
        triggered_by=(f"standing:model-value:{label}",))


#: Templates for the hypothesis shapes the Phase 10 spec names that are not
#: evidence-triggered. Kept as a catalogue so an operator can queue one
#: directly, and so the set is inspectable.
STANDING_HYPOTHESES = ("model_conditional_value",)


class HypothesisEngine:
    """Generates and stores research proposals. Autonomous, and only that.

    `generate` is an autonomous action. Every downstream step — running the
    experiment, acting on a result — is gated elsewhere. This class can fill a
    backlog and nothing more.
    """

    def __init__(self, *, clock: Clock | None = None, audit: Any = None,
                 namespace: str = _NS):
        self.clock = clock or default_clock()
        self.audit = audit
        self.namespace = namespace

    def _store(self) -> KeyedStore:
        return KeyedStore(self.namespace)

    def _write(self, proposal: ResearchProposal) -> ResearchProposal:
        self._store().put((proposal.proposal_id,), proposal.to_dict())
        if self.audit is not None:
            try:
                from .audit import EventType
                self.audit.record(EventType.HYPOTHESIS_PROPOSED,
                                  proposal.to_dict(), actor=proposal.proposed_by,
                                  subject=proposal.proposal_id)
            except Exception:                            # noqa: BLE001
                pass
        return proposal

    def get(self, proposal_id: str) -> ResearchProposal | None:
        body = self._store().get((proposal_id,))
        return ResearchProposal.from_dict(body) if body else None

    def all(self, *, status: ProposalStatus | str | None = None
            ) -> list[ResearchProposal]:
        out = []
        for _, body in self._store().items():
            proposal = ResearchProposal.from_dict(body)
            if status is not None and proposal.status is not ProposalStatus(status):
                continue
            out.append(proposal)
        return out

    def submit(self, proposal: ResearchProposal, *, actor: Actor | None = None
               ) -> ResearchProposal:
        """Store a proposal, refusing an exact duplicate of a live one.

        Duplicate suppression is by claim fingerprint, not id: the same
        weakness measured on two consecutive days produces the same hypothesis,
        and a backlog with fifty copies of it is a backlog nobody triages.
        """
        actor = actor or Actor.autonomous("hypothesis-engine")
        authorise(Action.GENERATE_HYPOTHESIS, actor,
                  subject=proposal.proposal_id, clock=self.clock, audit=self.audit)
        fingerprint = proposal.fingerprint()
        for existing in self.all():
            if (existing.fingerprint() == fingerprint
                    and existing.status in (ProposalStatus.DRAFT,
                                            ProposalStatus.QUEUED,
                                            ProposalStatus.RUNNING)):
                return existing
        return self._write(proposal)

    def generate(self, *, weaknesses: Sequence[Any] = (),
                 drift_signals: Sequence[Any] = (),
                 actor: Actor | None = None) -> list[ResearchProposal]:
        """Produce proposals from measured evidence. Deterministic in order."""
        actor = actor or Actor.autonomous("hypothesis-engine")
        out: list[ResearchProposal] = []
        for finding in weaknesses:
            proposal = from_weakness(finding, clock=self.clock)
            if proposal is not None:
                out.append(self.submit(proposal, actor=actor))
        for signal in drift_signals:
            proposal = from_drift(signal, clock=self.clock)
            if proposal is not None:
                out.append(self.submit(proposal, actor=actor))
        # Deduplicate while preserving first-seen order.
        seen: set[str] = set()
        unique = []
        for proposal in out:
            if proposal.proposal_id in seen:
                continue
            seen.add(proposal.proposal_id)
            unique.append(proposal)
        return unique

    def set_status(self, proposal_id: str, status: ProposalStatus | str, *,
                   notes: str = "") -> ResearchProposal:
        """Advance a proposal's lifecycle. Records the result, including bad ones.

        There is deliberately no path that removes a REFUTED proposal. A
        refutation is the most valuable output this module produces, and a
        system that could tidy away its failed hypotheses would be able to
        present a research record that never fails.
        """
        proposal = self.get(proposal_id)
        if proposal is None:
            raise ConfigurationError("unknown proposal", proposal_id=proposal_id)
        status = ProposalStatus(status)
        if proposal.status is ProposalStatus.REFUTED and status is not ProposalStatus.REFUTED:
            raise ConfigurationError(
                "a refuted proposal cannot be reopened under the same id; "
                "propose a new hypothesis that accounts for the refutation",
                proposal_id=proposal_id)
        return self._write(proposal.evolve(
            status=status, notes=notes or proposal.notes))

    def backlog(self) -> list[ResearchProposal]:
        """Untested proposals, cheapest first — so a stalled queue still moves."""
        pending = [p for p in self.all()
                   if p.status in (ProposalStatus.DRAFT, ProposalStatus.QUEUED)]
        return sorted(pending, key=lambda p: (p.experiment.estimated_cost_units,
                                              p.proposal_id))

    def stats(self) -> dict:
        by_status: dict[str, int] = {}
        for proposal in self.all():
            by_status[proposal.status.value] = by_status.get(proposal.status.value, 0) + 1
        return {"total": len(self.all()), "by_status": by_status,
                "backlog": len(self.backlog())}

    def __repr__(self) -> str:                          # pragma: no cover
        return f"HypothesisEngine(namespace={self.namespace!r})"


__all__ = ["RiskLevel", "ProductionImpact", "ExperimentKind", "ProposalStatus",
           "ExperimentPlan", "ResearchProposal", "HypothesisEngine",
           "from_weakness", "from_drift", "model_conditional_value",
           "STANDING_HYPOTHESES"]

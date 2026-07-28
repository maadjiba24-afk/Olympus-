"""The research sandbox: where Olympus may experiment, and what it cannot reach.

An experiment is only trustworthy if it could not have touched production, and
production is only safe if an experiment could not have touched it. Both
properties come from the same mechanism: every resource an experiment uses is
requested by name from a `ResearchSandbox`, and the sandbox holds an explicit
denial list covering live credentials, order submission, production risk
configuration, secrets, live-mode activation and operator tokens.

The design decision that makes this real rather than decorative is that the
sandbox is a **capability holder, not a checker**. It does not inspect what an
experiment does and decide whether to permit it; it simply has no reference to
the denied things. `request("live_broker_credentials")` raises not because a
rule says no but because there is nothing to return. Enforcement by absence
survives an experiment that is trying to get around it, which enforcement by
inspection does not.

Two supporting guarantees:

* **Determinism.** Every sandbox carries a seed and hands out a seeded RNG.
  Two runs of the same experiment with the same seed produce the same result,
  so a surprising finding can be re-examined rather than re-rolled.
* **Falsification is first-class.** `ExperimentResult.verdict` is computed by
  comparing measured outcomes against the proposal's *pre-registered* success
  and failure criteria. `REFUTED` is a normal, recordable, non-error outcome —
  and `FalsificationExperiment` exists specifically to attack a hypothesis the
  system already believes.

Nothing in this module imports execution, brokers, the vault, or mode control,
and `kernel.audit_evolution_modules()` fails the build if that changes.
"""

from __future__ import annotations

import json
import math
import random
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Sequence

from .clock import Clock, default_clock
from .contracts import ensure_utc, jsonable
from .errors import ConfigurationError, ExperimentError
from .governance import Action, Actor, authorise
from .hypotheses import ProposalStatus, ResearchProposal
from .storekeys import KeyedStore

_NS = "trading.lab"

#: Resources an experiment may never obtain. Named exactly as the Phase 10
#: specification names them, so the mapping from requirement to code is direct.
DENIED_RESOURCES: frozenset[str] = frozenset({
    "live_broker_credentials",
    "production_order_submission",
    "production_risk_configuration",
    "production_secrets",
    "live_mode_activation",
    "operator_authorisation_tokens",
})

#: Everything else an experiment legitimately needs. An unlisted resource is
#: refused too — the sandbox fails closed, so adding a capability is a
#: deliberate edit here rather than an accident somewhere else.
ALLOWED_RESOURCES: frozenset[str] = frozenset({
    "historical_data",
    "feature_library",
    "cost_model",
    "backtest_engine",
    "random",
    "seed",
    "candidate_model",
    "baseline_model",
    "scratch_space",
})


class Verdict(str, Enum):
    """What the experiment showed. `REFUTED` is a success of the process."""

    SUPPORTED = "supported"
    REFUTED = "refuted"
    INCONCLUSIVE = "inconclusive"
    #: The experiment could not run. Distinct from inconclusive: nothing was
    #: measured, so nothing may be inferred in either direction.
    ERRORED = "errored"


@dataclass(frozen=True)
class CostModel:
    """Transaction costs, applied identically to every variant.

    Held by the sandbox rather than by the experiment so that a variant cannot
    quietly be evaluated under friendlier costs than its baseline — the single
    easiest way to manufacture an improvement that does not exist.
    """

    fee_bps: float = 10.0
    slippage_bps: float = 5.0
    spread_bps: float = 2.0

    def __post_init__(self):
        for name in ("fee_bps", "slippage_bps", "spread_bps"):
            value = float(getattr(self, name))
            if value < 0 or not math.isfinite(value):
                raise ConfigurationError(f"{name} must be finite and non-negative",
                                         got=getattr(self, name))
            object.__setattr__(self, name, value)

    @property
    def round_trip_bps(self) -> float:
        return 2 * (self.fee_bps + self.slippage_bps) + self.spread_bps

    def apply(self, gross_returns: Sequence[float],
              turnover: Sequence[float] | None = None) -> list[float]:
        """Charge costs proportional to turnover. Never returns gross by default.

        With no turnover series, one full round trip per observation is
        assumed. That is pessimistic, and pessimistic is the correct default:
        an experiment that under-charges costs produces exactly the kind of
        result that looks good in research and loses money in production.
        """
        gross = [float(r) for r in gross_returns]
        if turnover is None:
            turns = [1.0] * len(gross)
        else:
            turns = [abs(float(t)) for t in turnover]
            if len(turns) != len(gross):
                raise ConfigurationError(
                    "turnover series must align with returns",
                    returns=len(gross), turnover=len(turns))
        charge = self.round_trip_bps / 10_000.0
        return [g - charge * t for g, t in zip(gross, turns)]

    def to_dict(self) -> dict:
        return {"fee_bps": self.fee_bps, "slippage_bps": self.slippage_bps,
                "spread_bps": self.spread_bps,
                "round_trip_bps": self.round_trip_bps}


class ResearchSandbox:
    """The only thing an experiment is handed. Holds no production reference.

    Construction takes the resources an experiment is allowed to see. There is
    no constructor parameter, attribute or method for a denied resource, so
    "the sandbox cannot reach live credentials" is a property of the class
    rather than a rule it enforces on itself.
    """

    __slots__ = ("_resources", "_seed", "_rng", "_requests", "_denied",
                 "_cost_model", "_label")

    def __init__(self, *, label: str = "sandbox", seed: int = 0,
                 historical_data: Any = None, feature_library: Any = None,
                 backtest_engine: Any = None, candidate_model: Any = None,
                 baseline_model: Any = None,
                 cost_model: CostModel | None = None,
                 scratch_space: Mapping[str, Any] | None = None):
        self._label = str(label)
        self._seed = int(seed)
        self._rng = random.Random(self._seed)
        self._cost_model = cost_model or CostModel()
        self._requests: list[str] = []
        self._denied: list[str] = []
        self._resources: dict[str, Any] = {
            "historical_data": historical_data,
            "feature_library": feature_library,
            "backtest_engine": backtest_engine,
            "candidate_model": candidate_model,
            "baseline_model": baseline_model,
            "cost_model": self._cost_model,
            "random": self._rng,
            "seed": self._seed,
            "scratch_space": dict(scratch_space or {}),
        }

    # -- the boundary ------------------------------------------------------

    def request(self, resource: str) -> Any:
        """Obtain a resource by name. Denied and unknown both raise.

        Every request is recorded, granted or not. An experiment's attempt to
        obtain live credentials is itself a finding, and one that must survive
        into the audit trail rather than vanishing into a caught exception.
        """
        name = str(resource)
        self._requests.append(name)
        if name in DENIED_RESOURCES:
            self._denied.append(name)
            raise ExperimentError(
                f"the research sandbox holds no reference to {name!r}; research "
                "is isolated from production execution, credentials, risk "
                "configuration and mode control by construction",
                sandbox=self._label, resource=name,
                denied=sorted(DENIED_RESOURCES))
        if name not in ALLOWED_RESOURCES:
            self._denied.append(name)
            raise ExperimentError(
                f"unknown sandbox resource {name!r}; the sandbox fails closed, "
                "so a new capability is an explicit edit to ALLOWED_RESOURCES",
                sandbox=self._label, resource=name,
                allowed=sorted(ALLOWED_RESOURCES))
        value = self._resources.get(name)
        if value is None:
            raise ExperimentError(
                f"sandbox resource {name!r} was not provided for this run",
                sandbox=self._label, resource=name)
        return value

    def has(self, resource: str) -> bool:
        """Non-raising probe, so an experiment can degrade rather than crash."""
        return (str(resource) in ALLOWED_RESOURCES
                and self._resources.get(str(resource)) is not None)

    @property
    def seed(self) -> int:
        return self._seed

    @property
    def rng(self) -> random.Random:
        """A seeded generator. Reproducibility is not optional here."""
        return self._rng

    @property
    def cost_model(self) -> CostModel:
        return self._cost_model

    @property
    def requests(self) -> tuple[str, ...]:
        return tuple(self._requests)

    @property
    def denied_requests(self) -> tuple[str, ...]:
        """What this experiment tried to reach and could not."""
        return tuple(self._denied)

    def to_dict(self) -> dict:
        return {"label": self._label, "seed": self._seed,
                "cost_model": self._cost_model.to_dict(),
                "requests": sorted(set(self._requests)),
                "denied_requests": sorted(set(self._denied)),
                "available": sorted(k for k, v in self._resources.items()
                                    if v is not None)}

    def __repr__(self) -> str:                          # pragma: no cover
        return f"ResearchSandbox(label={self._label!r}, seed={self._seed})"


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VariantResult:
    """One arm of an experiment. Gross and net both kept, never just one."""

    name: str
    observations: int
    gross_mean: float | None = None
    net_mean: float | None = None
    net_total: float | None = None
    volatility: float | None = None
    sharpe: float | None = None
    max_drawdown: float | None = None
    hit_rate: float | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "extra", dict(self.extra))

    def to_dict(self) -> dict:
        return {"name": self.name, "observations": self.observations,
                "gross_mean": self.gross_mean, "net_mean": self.net_mean,
                "net_total": self.net_total, "volatility": self.volatility,
                "sharpe": self.sharpe, "max_drawdown": self.max_drawdown,
                "hit_rate": self.hit_rate, "extra": jsonable(dict(self.extra))}


def summarise(name: str, gross: Sequence[float], net: Sequence[float]
              ) -> VariantResult:
    """Metrics for one arm. `None` wherever a metric is undefined.

    A flat return series has no Sharpe ratio, and reporting 0.0 for it would be
    a lie that survives into a comparison table. `perf.py` takes the same
    position for the same reason.
    """
    gross = [float(g) for g in gross]
    net = [float(n) for n in net]
    if not net:
        return VariantResult(name=name, observations=0)
    spread = statistics.pstdev(net) if len(net) > 1 else 0.0
    mean = statistics.fmean(net)
    equity, peak, drawdown = 1.0, 1.0, 0.0
    for r in net:
        equity *= (1.0 + r)
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak if peak > 0 else 0.0)
    return VariantResult(
        name=name, observations=len(net),
        gross_mean=statistics.fmean(gross) if gross else None,
        net_mean=mean, net_total=equity - 1.0,
        volatility=spread if spread > 0 else None,
        sharpe=(mean / spread) if spread > 0 else None,
        max_drawdown=drawdown,
        hit_rate=sum(1 for r in net if r > 0) / len(net))


@dataclass(frozen=True)
class ExperimentResult:
    """What the experiment measured, and whether it settled the question."""

    experiment_id: str
    proposal_id: str
    kind: str
    verdict: Verdict
    ran_at: datetime
    seed: int
    variants: tuple[VariantResult, ...] = ()
    baselines: tuple[VariantResult, ...] = ()
    significance: Mapping[str, Any] = field(default_factory=dict)
    rationale: str = ""
    sandbox: Mapping[str, Any] = field(default_factory=dict)
    error: str = ""

    def __post_init__(self):
        object.__setattr__(self, "verdict", Verdict(self.verdict))
        object.__setattr__(self, "ran_at",
                           ensure_utc(self.ran_at, field_name="ran_at"))
        object.__setattr__(self, "variants", tuple(self.variants))
        object.__setattr__(self, "baselines", tuple(self.baselines))
        object.__setattr__(self, "significance", dict(self.significance))
        object.__setattr__(self, "sandbox", dict(self.sandbox))

    @property
    def best_variant(self) -> VariantResult | None:
        scored = [v for v in self.variants if v.net_mean is not None]
        return max(scored, key=lambda v: v.net_mean) if scored else None

    @property
    def beat_every_baseline(self) -> bool:
        """Strictly better than every baseline on net mean.

        Every baseline, not the weakest: a result that beats a deliberately
        poor baseline and loses to `always flat` has not shown anything.
        """
        best = self.best_variant
        if best is None or best.net_mean is None:
            return False
        scored = [b.net_mean for b in self.baselines if b.net_mean is not None]
        return bool(scored) and all(best.net_mean > b for b in scored)

    def to_dict(self) -> dict:
        return {"experiment_id": self.experiment_id,
                "proposal_id": self.proposal_id, "kind": self.kind,
                "verdict": self.verdict.value, "ran_at": jsonable(self.ran_at),
                "seed": self.seed,
                "variants": [v.to_dict() for v in self.variants],
                "baselines": [b.to_dict() for b in self.baselines],
                "significance": jsonable(dict(self.significance)),
                "rationale": self.rationale, "sandbox": dict(self.sandbox),
                "error": self.error,
                "beat_every_baseline": self.beat_every_baseline}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExperimentResult":
        def variants(key):
            return tuple(VariantResult(**{**v, "extra": v.get("extra") or {}})
                         for v in data.get(key) or ())
        return cls(experiment_id=str(data.get("experiment_id", "")),
                   proposal_id=str(data.get("proposal_id", "")),
                   kind=str(data.get("kind", "")),
                   verdict=Verdict(data.get("verdict", Verdict.INCONCLUSIVE.value)),
                   ran_at=datetime.fromisoformat(data["ran_at"]),
                   seed=int(data.get("seed", 0)),
                   variants=variants("variants"), baselines=variants("baselines"),
                   significance=data.get("significance") or {},
                   rationale=str(data.get("rationale", "")),
                   sandbox=data.get("sandbox") or {},
                   error=str(data.get("error", "")))


# ---------------------------------------------------------------------------
# experiments
# ---------------------------------------------------------------------------

#: An arm: given the sandbox, produce a series of per-observation gross returns.
Arm = Callable[[ResearchSandbox], Sequence[float]]


class Experiment:
    """Base class. Subclasses implement `measure`, never the verdict.

    Separating measurement from judgement is what keeps a result honest: the
    verdict is computed by `_judge` from the pre-registered criteria and the
    measured numbers, so an experiment cannot decide it succeeded.
    """

    kind = "experiment"

    def __init__(self, *, variants: Mapping[str, Arm],
                 baselines: Mapping[str, Arm],
                 turnover: Mapping[str, Sequence[float]] | None = None,
                 min_observations: int = 30,
                 min_improvement: float = 0.0,
                 alpha: float = 0.05):
        if not variants:
            raise ConfigurationError("an experiment needs at least one variant")
        if not baselines:
            raise ConfigurationError(
                "an experiment needs at least one baseline; a measurement with "
                "nothing to beat cannot demonstrate improvement")
        self.variants = dict(variants)
        self.baselines = dict(baselines)
        self.turnover = dict(turnover or {})
        self.min_observations = int(min_observations)
        self.min_improvement = float(min_improvement)
        self.alpha = float(alpha)

    # -- measurement -------------------------------------------------------

    def _run_arm(self, name: str, arm: Arm, sandbox: ResearchSandbox
                 ) -> tuple[list[float], list[float]]:
        gross = [float(x) for x in arm(sandbox)]
        net = sandbox.cost_model.apply(gross, self.turnover.get(name))
        return gross, net

    def measure(self, sandbox: ResearchSandbox
                ) -> tuple[list[VariantResult], list[VariantResult],
                           dict[str, list[float]]]:
        """Run every arm under identical costs. Returns results plus raw nets."""
        raw: dict[str, list[float]] = {}
        variant_results, baseline_results = [], []
        for name, arm in sorted(self.variants.items()):
            gross, net = self._run_arm(name, arm, sandbox)
            raw[name] = net
            variant_results.append(summarise(name, gross, net))
        for name, arm in sorted(self.baselines.items()):
            gross, net = self._run_arm(name, arm, sandbox)
            raw[name] = net
            baseline_results.append(summarise(name, gross, net))
        return variant_results, baseline_results, raw

    # -- judgement ---------------------------------------------------------

    def _judge(self, variants: Sequence[VariantResult],
               baselines: Sequence[VariantResult],
               raw: Mapping[str, list[float]]
               ) -> tuple[Verdict, dict, str]:
        """Compare the best variant with every baseline. Conservative by design.

        The verdict is SUPPORTED only when the best variant beats *every*
        baseline by at least `min_improvement` and the paired test on the
        strongest baseline clears `alpha`. Anything short of that is
        INCONCLUSIVE, and losing to a baseline is REFUTED.
        """
        scored = [v for v in variants if v.net_mean is not None]
        if not scored:
            return Verdict.INCONCLUSIVE, {}, "no variant produced observations"
        best = max(scored, key=lambda v: v.net_mean)
        if best.observations < self.min_observations:
            return (Verdict.INCONCLUSIVE, {},
                    f"{best.observations} observations is below the "
                    f"pre-registered minimum of {self.min_observations}; no "
                    "conclusion is drawn in either direction")

        scored_baselines = [b for b in baselines if b.net_mean is not None]
        if not scored_baselines:
            return Verdict.INCONCLUSIVE, {}, "no baseline produced observations"
        strongest = max(scored_baselines, key=lambda b: b.net_mean)

        margin = best.net_mean - strongest.net_mean
        significance: dict[str, Any] = {
            "best_variant": best.name, "strongest_baseline": strongest.name,
            "margin": margin, "min_improvement": self.min_improvement,
        }

        left, right = raw.get(best.name, []), raw.get(strongest.name, [])
        n = min(len(left), len(right))
        if n >= self.min_observations:
            from .evaluate import paired_bootstrap
            differences = [left[i] - right[i] for i in range(n)]
            test = paired_bootstrap(differences, name="net_return_difference",
                                    alpha=self.alpha, seed=0)
            significance["paired_bootstrap"] = test.to_dict()
            significant = test.significant
        else:
            significance["paired_bootstrap"] = None
            significant = False
            significance["note"] = (
                f"arms are not pairable at {n} aligned observations; no "
                "significance test was run")

        if margin <= 0:
            return (Verdict.REFUTED, significance,
                    f"{best.name} did not beat {strongest.name} "
                    f"({best.net_mean:.6g} vs {strongest.net_mean:.6g} net)")
        if margin < self.min_improvement:
            return (Verdict.INCONCLUSIVE, significance,
                    f"margin {margin:.6g} is below the pre-registered minimum "
                    f"improvement of {self.min_improvement:.6g}")
        if not significant:
            return (Verdict.INCONCLUSIVE, significance,
                    f"{best.name} leads {strongest.name} by {margin:.6g} but "
                    f"the difference is not significant at α={self.alpha}")
        return (Verdict.SUPPORTED, significance,
                f"{best.name} beat every baseline; margin over {strongest.name} "
                f"is {margin:.6g}, significant at α={self.alpha}")


class AblationExperiment(Experiment):
    """Remove one component and measure what is lost."""
    kind = "ablation"


class WalkForwardExperiment(Experiment):
    """Out-of-sample by construction: each arm returns its stitched OOS series."""
    kind = "walk_forward"


class ParameterSweepExperiment(Experiment):
    """Many parameter settings as variants.

    Carries a multiple-comparison correction, because a sweep of thirty
    settings will produce a p < 0.05 winner from pure noise about four times in
    five. Without the correction a sweep is a machine for generating false
    discoveries.
    """
    kind = "parameter_sweep"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Bonferroni over the number of arms tried. Blunt, but it errs toward
        # not adopting a change, which is the right direction to err in.
        self.alpha = self.alpha / max(1, len(self.variants))


class ModelComparisonExperiment(Experiment):
    kind = "model_comparison"


class CostSensitivityExperiment(Experiment):
    """Same arms, escalating costs. Finds the cost level that kills the edge."""
    kind = "cost_sensitivity"

    def __init__(self, *, cost_multipliers: Sequence[float] = (1.0, 2.0, 4.0),
                 **kwargs):
        super().__init__(**kwargs)
        self.cost_multipliers = tuple(float(m) for m in cost_multipliers)
        if any(m <= 0 for m in self.cost_multipliers):
            raise ConfigurationError("cost multipliers must be positive")

    def measure(self, sandbox):
        variants, baselines, raw = super().measure(sandbox)
        base = sandbox.cost_model
        breakeven: dict[str, float | None] = {}
        for name, arm in sorted(self.variants.items()):
            gross = [float(x) for x in arm(sandbox)]
            survived = None
            for multiplier in self.cost_multipliers:
                model = CostModel(fee_bps=base.fee_bps * multiplier,
                                  slippage_bps=base.slippage_bps * multiplier,
                                  spread_bps=base.spread_bps * multiplier)
                net = model.apply(gross, self.turnover.get(name))
                if net and statistics.fmean(net) > 0:
                    survived = multiplier
            breakeven[name] = survived
        variants = [VariantResult(**{**v.to_dict(),
                                     "extra": {**v.extra,
                                               "survives_cost_multiple": breakeven.get(v.name)}})
                    for v in variants]
        return variants, baselines, raw


class RobustnessExperiment(Experiment):
    """Perturb the inputs and see whether the result survives.

    A finding that disappears when the data is jittered by a tick was never a
    finding. Runs each arm under several seeds and reports the spread; a wide
    spread downgrades the verdict.
    """
    kind = "robustness"

    def __init__(self, *, trials: int = 5, **kwargs):
        super().__init__(**kwargs)
        self.trials = max(1, int(trials))

    def measure(self, sandbox):
        variants, baselines, raw = super().measure(sandbox)
        spreads: dict[str, float | None] = {}
        for name, arm in sorted(self.variants.items()):
            means = []
            for trial in range(self.trials):
                trial_box = ResearchSandbox(
                    label=f"{name}-trial{trial}", seed=sandbox.seed + trial + 1,
                    cost_model=sandbox.cost_model)
                net = sandbox.cost_model.apply([float(x) for x in arm(trial_box)],
                                               self.turnover.get(name))
                if net:
                    means.append(statistics.fmean(net))
            spreads[name] = statistics.pstdev(means) if len(means) > 1 else None
        variants = [VariantResult(**{**v.to_dict(),
                                     "extra": {**v.extra,
                                               "across_seed_stdev": spreads.get(v.name)}})
                    for v in variants]
        return variants, baselines, raw


class FalsificationExperiment(Experiment):
    """Deliberately try to break a hypothesis Olympus already believes.

    Inverts the burden: the verdict is SUPPORTED only if the claim survives an
    attack designed to kill it. A research process that only ever seeks
    confirmation will find it, which is why this exists as a distinct kind and
    why `HypothesisEngine` can queue it against its own prior conclusions.
    """
    kind = "falsification"

    def _judge(self, variants, baselines, raw):
        verdict, significance, rationale = super()._judge(variants, baselines, raw)
        significance["falsification"] = True
        if verdict is Verdict.SUPPORTED:
            return (verdict, significance,
                    "the claim survived a deliberate attempt to refute it: "
                    + rationale)
        if verdict is Verdict.REFUTED:
            return (verdict, significance,
                    "the falsification attempt succeeded; the claim does not "
                    "hold: " + rationale)
        return verdict, significance, rationale


# ---------------------------------------------------------------------------
# the lab
# ---------------------------------------------------------------------------


class ResearchLab:
    """Runs experiments in sandboxes and records results — including failures.

    Results are written before the verdict is returned, and there is no delete
    path. A self-evolving system that could discard inconvenient results would
    be able to present a research history in which it was always right.
    """

    def __init__(self, *, clock: Clock | None = None, audit: Any = None,
                 namespace: str = _NS, engine: Any = None):
        self.clock = clock or default_clock()
        self.audit = audit
        self.namespace = namespace
        #: Optional `HypothesisEngine`, so a run updates the proposal's status.
        self.engine = engine

    def _store(self) -> KeyedStore:
        return KeyedStore(self.namespace)

    def _write(self, result: ExperimentResult) -> ExperimentResult:
        self._store().put((result.experiment_id,), result.to_dict())
        if self.audit is not None:
            try:
                from .audit import EventType
                self.audit.record(EventType.EXPERIMENT_RUN, result.to_dict(),
                                  actor="research-lab",
                                  subject=result.proposal_id)
            except Exception:                            # noqa: BLE001
                pass
        return result

    def sandbox_for(self, proposal: ResearchProposal, *, seed: int = 0,
                    **resources) -> ResearchSandbox:
        """Build the sandbox for a proposal. The only constructor callers use."""
        return ResearchSandbox(label=proposal.proposal_id, seed=seed, **resources)

    def run(self, proposal: ResearchProposal, experiment: Experiment, *,
            sandbox: ResearchSandbox | None = None, seed: int = 0,
            actor: Actor | None = None) -> ExperimentResult:
        """Run one experiment against one proposal.

        A sandbox breach does not propagate as a crash: it is caught, recorded
        as an `ERRORED` result naming the denied resource, and re-raised. The
        record matters more than the exception — an experiment that reached for
        live credentials must leave a permanent trace whether or not its caller
        handles the error.
        """
        actor = actor or Actor.autonomous("research-lab")
        authorise(Action.RESEARCH, actor, subject=proposal.proposal_id,
                  clock=self.clock, audit=self.audit)
        box = sandbox or self.sandbox_for(proposal, seed=seed)
        experiment_id = f"exp-{proposal.proposal_id}-{experiment.kind}-{box.seed}"
        now = self.clock.now()

        try:
            variants, baselines, raw = experiment.measure(box)
            verdict, significance, rationale = experiment._judge(
                variants, baselines, raw)
        except ExperimentError as exc:
            result = ExperimentResult(
                experiment_id=experiment_id, proposal_id=proposal.proposal_id,
                kind=experiment.kind, verdict=Verdict.ERRORED, ran_at=now,
                seed=box.seed, sandbox=box.to_dict(), error=str(exc),
                rationale="the experiment attempted to reach a resource the "
                          "sandbox does not hold; no measurement was produced "
                          "and nothing may be inferred")
            self._write(result)
            raise
        except Exception as exc:                         # noqa: BLE001
            result = ExperimentResult(
                experiment_id=experiment_id, proposal_id=proposal.proposal_id,
                kind=experiment.kind, verdict=Verdict.ERRORED, ran_at=now,
                seed=box.seed, sandbox=box.to_dict(), error=repr(exc),
                rationale="the experiment failed to complete")
            return self._write(result)

        result = ExperimentResult(
            experiment_id=experiment_id, proposal_id=proposal.proposal_id,
            kind=experiment.kind, verdict=verdict, ran_at=now, seed=box.seed,
            variants=tuple(variants), baselines=tuple(baselines),
            significance=significance, rationale=rationale,
            sandbox=box.to_dict())
        self._write(result)

        if self.engine is not None:
            mapping = {Verdict.SUPPORTED: ProposalStatus.SUPPORTED,
                       Verdict.REFUTED: ProposalStatus.REFUTED,
                       Verdict.INCONCLUSIVE: ProposalStatus.INCONCLUSIVE,
                       Verdict.ERRORED: ProposalStatus.QUEUED}
            try:
                self.engine.set_status(proposal.proposal_id,
                                       mapping[result.verdict],
                                       notes=result.rationale)
            except Exception:                            # noqa: BLE001
                pass
        return result

    def result(self, experiment_id: str) -> ExperimentResult | None:
        body = self._store().get((experiment_id,))
        return ExperimentResult.from_dict(body) if body else None

    def results(self, *, proposal_id: str | None = None
                ) -> list[ExperimentResult]:
        out = []
        for _, body in self._store().items():
            result = ExperimentResult.from_dict(body)
            if proposal_id is not None and result.proposal_id != proposal_id:
                continue
            out.append(result)
        return out

    def stats(self) -> dict:
        results = self.results()
        by_verdict: dict[str, int] = {}
        for result in results:
            by_verdict[result.verdict.value] = by_verdict.get(result.verdict.value, 0) + 1
        breaches = sum(1 for r in results if r.sandbox.get("denied_requests"))
        return {"experiments": len(results), "by_verdict": by_verdict,
                "sandbox_breach_attempts": breaches}

    def __repr__(self) -> str:                          # pragma: no cover
        return f"ResearchLab(namespace={self.namespace!r})"


__all__ = ["DENIED_RESOURCES", "ALLOWED_RESOURCES", "Verdict", "CostModel",
           "ResearchSandbox", "VariantResult", "summarise", "ExperimentResult",
           "Experiment", "AblationExperiment", "WalkForwardExperiment",
           "ParameterSweepExperiment", "ModelComparisonExperiment",
           "CostSensitivityExperiment", "RobustnessExperiment",
           "FalsificationExperiment", "ResearchLab", "Arm"]

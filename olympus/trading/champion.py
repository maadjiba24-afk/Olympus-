"""Champion and challengers: replacing what works only for a demonstrated reason.

One component is the champion. Others challenge it. The comparison is only
meaningful if it is genuinely matched, so an `EvaluationHarness` fingerprints
the dataset, the cost assumptions, the risk constraints and the out-of-sample
window, and `compare()` **raises** rather than reporting when two arms were not
evaluated under the same one. Silently comparing a challenger measured on
cheaper costs against a champion measured on realistic ones is the most common
way a research process produces an improvement that does not exist, and it is
the failure this module is shaped around.

Three decision rules, applied in order:

1. **Out-of-sample or nothing.** A harness with `in_sample=True` cannot produce
   a REPLACE verdict at all.
2. **Significance, then margin.** The challenger must beat the champion on net
   risk-adjusted terms, with a paired test clearing α, by more than a stated
   minimum. Each of the three can fail independently, and the verdict names
   which did.
3. **Parsimony breaks ties.** When the difference is not statistically
   distinguishable, the *simpler* contender wins — and if the champion is the
   more complex one, that is a verdict to replace it with the simpler
   challenger. Complexity is a real cost paid in every future debugging
   session, and a framework that treated "indistinguishable" as "keep whatever
   is incumbent" would ratchet complexity upward forever.

Additionally, a challenger that wins on average but is *unstable* across
regimes is refused: `regime_consistency` requires it not to be materially worse
than the champion in any regime with a usable sample. Averages hide the regime
where the new model loses money.

Deciding is autonomous. Installing is not — `install` requires an operator.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from .clock import Clock, default_clock
from .contracts import Regime, ensure_utc, jsonable
from .errors import ConfigurationError, PromotionDenied
from .governance import Action, Actor, authorise, require_operator
from .storekeys import KeyedStore

_NS = "trading.champion"

#: Minimum aligned observations before any verdict other than INCONCLUSIVE.
MIN_OBSERVATIONS = 30

#: A challenger must beat the champion's net mean by at least this fraction of
#: the champion's own volatility. Expressed relative to volatility rather than
#: as an absolute so the bar scales with how noisy the series is.
DEFAULT_MIN_EDGE_IN_VOL = 0.05


class Decision(str, Enum):
    REPLACE = "replace"
    KEEP_CHAMPION = "keep_champion"
    INCONCLUSIVE = "inconclusive"
    #: The comparison was invalid. Distinct from inconclusive: nothing was
    #: learned about the contenders, only about the experiment.
    INVALID = "invalid"


@dataclass(frozen=True)
class EvaluationHarness:
    """The conditions an arm was measured under. Identity is by fingerprint.

    Every field participates in the fingerprint, including `risk_limits_hash`.
    A challenger evaluated under looser limits will show a better return and a
    worse risk profile, and comparing it with a champion measured under the real
    limits would recommend adopting the looser limits without ever saying so.
    """

    dataset_id: str
    dataset_hash: str
    cost_fingerprint: str
    risk_limits_hash: str
    start: datetime
    end: datetime
    in_sample: bool = False
    walk_forward_windows: int = 0

    def __post_init__(self):
        for name in ("dataset_id", "dataset_hash", "cost_fingerprint",
                     "risk_limits_hash"):
            if not str(getattr(self, name)).strip():
                raise ConfigurationError(
                    f"{name} must be set; an unidentified evaluation condition "
                    "cannot be shown to match another")
        for name in ("start", "end"):
            object.__setattr__(self, name,
                               ensure_utc(getattr(self, name), field_name=name))
        if self.end <= self.start:
            raise ConfigurationError("harness window must be non-empty",
                                     start=self.start, end=self.end)

    @property
    def fingerprint(self) -> str:
        payload = json.dumps({
            "dataset_id": self.dataset_id, "dataset_hash": self.dataset_hash,
            "cost_fingerprint": self.cost_fingerprint,
            "risk_limits_hash": self.risk_limits_hash,
            "start": self.start.isoformat(), "end": self.end.isoformat(),
            "in_sample": self.in_sample,
            "walk_forward_windows": self.walk_forward_windows,
        }, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def differences(self, other: "EvaluationHarness") -> list[str]:
        """Exactly which conditions differ. Used to explain a refusal."""
        out = []
        for name in ("dataset_id", "dataset_hash", "cost_fingerprint",
                     "risk_limits_hash", "start", "end", "in_sample",
                     "walk_forward_windows"):
            mine, theirs = getattr(self, name), getattr(other, name)
            if mine != theirs:
                out.append(f"{name}: {mine!r} vs {theirs!r}")
        return out

    def to_dict(self) -> dict:
        return {"dataset_id": self.dataset_id, "dataset_hash": self.dataset_hash,
                "cost_fingerprint": self.cost_fingerprint,
                "risk_limits_hash": self.risk_limits_hash,
                "start": jsonable(self.start), "end": jsonable(self.end),
                "in_sample": self.in_sample,
                "walk_forward_windows": self.walk_forward_windows,
                "fingerprint": self.fingerprint}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EvaluationHarness":
        return cls(dataset_id=str(data["dataset_id"]),
                   dataset_hash=str(data["dataset_hash"]),
                   cost_fingerprint=str(data["cost_fingerprint"]),
                   risk_limits_hash=str(data["risk_limits_hash"]),
                   start=datetime.fromisoformat(data["start"]),
                   end=datetime.fromisoformat(data["end"]),
                   in_sample=bool(data.get("in_sample", False)),
                   walk_forward_windows=int(data.get("walk_forward_windows", 0)))


@dataclass(frozen=True)
class Contender:
    """A model or strategy version in the running.

    `complexity` is an integer the author supplies: parameter count, feature
    count, whatever is comparable within the family. It is used only to break
    statistical ties, and it is required rather than optional so that "which of
    these is simpler" is answered before the results are known.
    """

    contender_id: str
    kind: str                          # "model" | "strategy"
    version: str
    complexity: int
    description: str = ""
    #: Extra cost of running this thing at all — inference time, data feeds.
    operating_cost_units: float = 0.0

    def __post_init__(self):
        if not str(self.contender_id).strip():
            raise ConfigurationError("contender_id must be non-empty")
        if int(self.complexity) < 0:
            raise ConfigurationError("complexity must be non-negative",
                                     contender_id=self.contender_id)
        object.__setattr__(self, "complexity", int(self.complexity))

    def to_dict(self) -> dict:
        return {"contender_id": self.contender_id, "kind": self.kind,
                "version": self.version, "complexity": self.complexity,
                "description": self.description,
                "operating_cost_units": self.operating_cost_units}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Contender":
        return cls(contender_id=str(data["contender_id"]),
                   kind=str(data.get("kind", "")),
                   version=str(data.get("version", "")),
                   complexity=int(data.get("complexity", 0)),
                   description=str(data.get("description", "")),
                   operating_cost_units=float(data.get("operating_cost_units", 0.0)))


@dataclass(frozen=True)
class ArmResult:
    """One contender's measured performance under one harness."""

    contender: Contender
    harness: EvaluationHarness
    #: Per-observation *net* returns. Net, always: a gross series would let a
    #: cheaper-to-run challenger look better than it is.
    net_returns: tuple[float, ...] = ()
    by_regime: Mapping[str, tuple[float, ...]] = field(default_factory=dict)
    execution_cost: float = 0.0
    notes: str = ""

    def __post_init__(self):
        object.__setattr__(self, "net_returns",
                           tuple(float(r) for r in self.net_returns))
        object.__setattr__(self, "by_regime",
                           {str(k): tuple(float(v) for v in vs)
                            for k, vs in dict(self.by_regime).items()})

    @property
    def n(self) -> int:
        return len(self.net_returns)

    @property
    def mean(self) -> float | None:
        return statistics.fmean(self.net_returns) if self.net_returns else None

    @property
    def volatility(self) -> float | None:
        if len(self.net_returns) < 2:
            return None
        spread = statistics.pstdev(self.net_returns)
        return spread if spread > 0 else None

    @property
    def sharpe(self) -> float | None:
        mean, spread = self.mean, self.volatility
        return (mean / spread) if mean is not None and spread else None

    @property
    def max_drawdown(self) -> float:
        equity = peak = 1.0
        worst = 0.0
        for r in self.net_returns:
            equity *= (1.0 + r)
            peak = max(peak, equity)
            worst = max(worst, (peak - equity) / peak if peak > 0 else 0.0)
        return worst

    def to_dict(self) -> dict:
        return {"contender": self.contender.to_dict(),
                "harness": self.harness.to_dict(), "n": self.n,
                "mean": self.mean, "volatility": self.volatility,
                "sharpe": self.sharpe, "max_drawdown": self.max_drawdown,
                "execution_cost": self.execution_cost,
                "regimes": sorted(self.by_regime), "notes": self.notes}


@dataclass(frozen=True)
class ChallengeOutcome:
    """The verdict, with every reason that produced it."""

    champion_id: str
    challenger_id: str
    decision: Decision
    decided_at: datetime
    reasons: tuple[str, ...] = ()
    significance: Mapping[str, Any] = field(default_factory=dict)
    margin: float | None = None
    regime_analysis: Mapping[str, Any] = field(default_factory=dict)
    harness: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "decision", Decision(self.decision))
        object.__setattr__(self, "decided_at",
                           ensure_utc(self.decided_at, field_name="decided_at"))
        object.__setattr__(self, "reasons", tuple(self.reasons))
        for name in ("significance", "regime_analysis", "harness"):
            object.__setattr__(self, name, dict(getattr(self, name)))

    @property
    def replaces(self) -> bool:
        return self.decision is Decision.REPLACE

    def to_dict(self) -> dict:
        return {"champion_id": self.champion_id,
                "challenger_id": self.challenger_id,
                "decision": self.decision.value,
                "decided_at": jsonable(self.decided_at),
                "reasons": list(self.reasons),
                "significance": jsonable(dict(self.significance)),
                "margin": self.margin,
                "regime_analysis": jsonable(dict(self.regime_analysis)),
                "harness": dict(self.harness)}

    def describe(self) -> str:                           # pragma: no cover
        head = f"{self.decision.value.upper()}: {self.challenger_id} vs {self.champion_id}"
        return "\n".join([head] + [f"  - {r}" for r in self.reasons])


def compare(champion: ArmResult, challenger: ArmResult, *,
            min_edge_in_vol: float = DEFAULT_MIN_EDGE_IN_VOL,
            alpha: float = 0.05, min_observations: int = MIN_OBSERVATIONS,
            max_drawdown_worsening: float = 0.20,
            regime_tolerance: float = 0.25,
            clock: Clock | None = None) -> ChallengeOutcome:
    """Decide whether the challenger should replace the champion.

    Raises `ConfigurationError` on a mismatched harness rather than returning
    INVALID. An unmatched comparison is a bug in the caller, and returning a
    value would let it be logged and forgotten; the point of failure should be
    the code that built the mismatch.
    """
    now = (clock or default_clock()).now()
    differences = champion.harness.differences(challenger.harness)
    if differences:
        raise ConfigurationError(
            "champion and challenger were not evaluated under the same "
            "conditions; a comparison across differing data, costs or risk "
            "limits measures the difference in conditions, not in contenders",
            champion=champion.contender.contender_id,
            challenger=challenger.contender.contender_id,
            differences=differences)

    harness = champion.harness
    reasons: list[str] = []
    significance: dict[str, Any] = {}

    if harness.in_sample:
        return ChallengeOutcome(
            champion_id=champion.contender.contender_id,
            challenger_id=challenger.contender.contender_id,
            decision=Decision.INVALID, decided_at=now,
            reasons=("the harness is in-sample; an in-sample result cannot "
                     "justify replacing a live component",),
            harness=harness.to_dict())

    n = min(champion.n, challenger.n)
    if n < min_observations:
        return ChallengeOutcome(
            champion_id=champion.contender.contender_id,
            challenger_id=challenger.contender.contender_id,
            decision=Decision.INCONCLUSIVE, decided_at=now,
            reasons=(f"{n} aligned observations is below the minimum of "
                     f"{min_observations}",),
            harness=harness.to_dict())

    champion_mean = champion.mean or 0.0
    challenger_mean = challenger.mean or 0.0
    margin = challenger_mean - champion_mean
    reference_vol = champion.volatility or challenger.volatility or 0.0
    required = min_edge_in_vol * reference_vol if reference_vol else 0.0
    significance.update({"champion_mean": champion_mean,
                         "challenger_mean": challenger_mean,
                         "margin": margin, "required_margin": required,
                         "champion_sharpe": champion.sharpe,
                         "challenger_sharpe": challenger.sharpe})

    from .evaluate import paired_bootstrap
    differences_series = [challenger.net_returns[i] - champion.net_returns[i]
                          for i in range(n)]
    test = paired_bootstrap(differences_series, name="net_return_difference",
                            alpha=alpha, seed=0)
    significance["paired_bootstrap"] = test.to_dict()

    # -- regime consistency -------------------------------------------------
    regime_analysis: dict[str, Any] = {}
    regime_failures: list[str] = []
    for regime in sorted(set(champion.by_regime) & set(challenger.by_regime)):
        champ_slice = champion.by_regime[regime]
        chall_slice = challenger.by_regime[regime]
        if min(len(champ_slice), len(chall_slice)) < min_observations // 3:
            regime_analysis[regime] = {"sample": min(len(champ_slice), len(chall_slice)),
                                       "verdict": "insufficient sample"}
            continue
        champ_mean = statistics.fmean(champ_slice)
        chall_mean = statistics.fmean(chall_slice)
        worse_by = champ_mean - chall_mean
        tolerated = regime_tolerance * abs(champ_mean) if champ_mean else 0.0
        entry = {"champion_mean": champ_mean, "challenger_mean": chall_mean,
                 "sample": min(len(champ_slice), len(chall_slice))}
        if worse_by > tolerated and worse_by > 0:
            entry["verdict"] = "challenger materially worse"
            regime_failures.append(regime)
        else:
            entry["verdict"] = "acceptable"
        regime_analysis[regime] = entry

    # -- drawdown -----------------------------------------------------------
    champ_dd, chall_dd = champion.max_drawdown, challenger.max_drawdown
    significance["champion_max_drawdown"] = champ_dd
    significance["challenger_max_drawdown"] = chall_dd
    drawdown_worse = chall_dd > champ_dd * (1 + max_drawdown_worsening)

    # -- the decision -------------------------------------------------------
    simpler_challenger = challenger.contender.complexity < champion.contender.complexity
    cheaper_challenger = (challenger.contender.operating_cost_units
                          <= champion.contender.operating_cost_units)

    if not test.significant:
        # Statistically indistinguishable: parsimony decides. This is the branch
        # that lets Olympus *remove* complexity, which nothing else here does.
        reasons.append(
            f"difference is not significant at α={alpha} "
            f"(margin {margin:+.6g})")
        if simpler_challenger and cheaper_challenger and not drawdown_worse:
            reasons.append(
                f"challenger is simpler ({challenger.contender.complexity} vs "
                f"{champion.contender.complexity}) at indistinguishable "
                "performance, so the simpler contender is preferred")
            decision = Decision.REPLACE
        else:
            reasons.append(
                "no statistically credible improvement and the challenger is "
                "not simpler, so the incumbent stands")
            decision = Decision.KEEP_CHAMPION
    elif margin <= 0:
        reasons.append(f"challenger underperformed by {abs(margin):.6g}")
        decision = Decision.KEEP_CHAMPION
    elif margin < required:
        reasons.append(
            f"margin {margin:.6g} is below the required edge of {required:.6g} "
            f"({min_edge_in_vol:.0%} of the champion's volatility)")
        decision = Decision.INCONCLUSIVE
    elif drawdown_worse:
        reasons.append(
            f"challenger improves return but worsens maximum drawdown "
            f"{champ_dd:.2%} → {chall_dd:.2%}, beyond the "
            f"{max_drawdown_worsening:.0%} tolerance")
        decision = Decision.KEEP_CHAMPION
    elif regime_failures:
        reasons.append(
            "challenger is materially worse in "
            f"{', '.join(regime_failures)}; an average that hides a regime "
            "where the new component loses is not an improvement")
        decision = Decision.KEEP_CHAMPION
    else:
        reasons.append(
            f"challenger beats the champion by {margin:.6g} out of sample, "
            f"significant at α={alpha}, without worsening drawdown or any "
            "regime beyond tolerance")
        if not simpler_challenger:
            reasons.append(
                f"note: the challenger is more complex "
                f"({challenger.contender.complexity} vs "
                f"{champion.contender.complexity}); the improvement is "
                "carrying that cost")
        decision = Decision.REPLACE

    return ChallengeOutcome(
        champion_id=champion.contender.contender_id,
        challenger_id=challenger.contender.contender_id,
        decision=decision, decided_at=now, reasons=tuple(reasons),
        significance=significance, margin=margin,
        regime_analysis=regime_analysis, harness=harness.to_dict())


class ChampionChallengerBoard:
    """Holds the champion, the challengers, and the record of every contest.

    Deciding is autonomous. Installing a new champion is not: `install` requires
    an operator and refuses a decision it did not itself produce, so a verdict
    cannot be hand-written into an installation.
    """

    def __init__(self, arena: str, *, clock: Clock | None = None,
                 audit: Any = None, namespace: str = _NS):
        if not str(arena).strip():
            raise ConfigurationError("an arena needs a name")
        self.arena = str(arena)
        self.clock = clock or default_clock()
        self.audit = audit
        self.namespace = namespace

    def _store(self) -> KeyedStore:
        return KeyedStore(self.namespace)

    def _put(self, kind: str, *name: str, payload: Mapping[str, Any]) -> None:
        self._store().put((self.arena, kind, *name), jsonable(dict(payload)))

    def _get(self, kind: str, *name: str) -> dict | None:
        return self._store().get((self.arena, kind, *name))

    # -- roster ------------------------------------------------------------

    def set_champion(self, contender: Contender, *, actor: Actor,
                     reason: str) -> Contender:
        """Install the initial champion. Operator only, like every install."""
        require_operator(actor, what="setting the champion")
        authorise(Action.PROMOTE_CAPABILITY, actor,
                  subject=f"{self.arena}:{contender.contender_id}",
                  clock=self.clock, audit=self.audit)
        self._put("champion", payload={**contender.to_dict(),
                                       "installed_at": jsonable(self.clock.now()),
                                       "installed_by": actor.name,
                                       "reason": reason})
        return contender

    def champion(self) -> Contender | None:
        data = self._get("champion")
        return Contender.from_dict(data) if data else None

    def add_challenger(self, contender: Contender, *,
                       actor: Actor | None = None) -> Contender:
        """Enter a challenger. Autonomous — proposing a candidate risks nothing."""
        actor = actor or Actor.autonomous("champion-board")
        authorise(Action.PROPOSE_IMPROVEMENT, actor,
                  subject=f"{self.arena}:{contender.contender_id}",
                  clock=self.clock, audit=self.audit)
        champion = self.champion()
        if champion is not None and champion.contender_id == contender.contender_id:
            raise ConfigurationError(
                "the champion cannot challenge itself", arena=self.arena,
                contender_id=contender.contender_id)
        self._put("challenger", contender.contender_id,
                  payload=contender.to_dict())
        return contender

    def challengers(self) -> list[Contender]:
        return [Contender.from_dict(body)
                for _, body in self._store().items((self.arena, "challenger"))]

    # -- contests ----------------------------------------------------------

    def run_contest(self, champion_arm: ArmResult, challenger_arm: ArmResult,
                    *, actor: Actor | None = None, **kwargs) -> ChallengeOutcome:
        """Compare and record. Autonomous — this is evaluation, not action."""
        actor = actor or Actor.autonomous("champion-board")
        authorise(Action.EVALUATE, actor, subject=self.arena, clock=self.clock,
                  audit=self.audit)
        outcome = compare(champion_arm, challenger_arm, clock=self.clock, **kwargs)
        self._put("outcome", outcome.challenger_id,
                  outcome.decided_at.isoformat(), payload=outcome.to_dict())
        if self.audit is not None:
            try:
                from .audit import EventType
                self.audit.record(EventType.EXPERIMENT_RUN, outcome.to_dict(),
                                  actor=actor.name,
                                  subject=f"{self.arena}:{outcome.challenger_id}")
            except Exception:                            # noqa: BLE001
                pass
        return outcome

    def outcomes(self, *, challenger_id: str | None = None
                 ) -> list[ChallengeOutcome]:
        out = []
        for _, data in self._store().items((self.arena, "outcome")):
            outcome = ChallengeOutcome(
                champion_id=data["champion_id"],
                challenger_id=data["challenger_id"],
                decision=Decision(data["decision"]),
                decided_at=datetime.fromisoformat(data["decided_at"]),
                reasons=tuple(data.get("reasons") or ()),
                significance=data.get("significance") or {},
                margin=data.get("margin"),
                regime_analysis=data.get("regime_analysis") or {},
                harness=data.get("harness") or {})
            if challenger_id is not None and outcome.challenger_id != challenger_id:
                continue
            out.append(outcome)
        return out

    def install(self, challenger_id: str, *, actor: Actor, reason: str
                ) -> Contender:
        """Make a challenger the champion. Operator only, and only on a REPLACE.

        The refusal that matters: an operator cannot install a challenger that
        never won a contest. Human authority decides *whether* to act on
        evidence, not whether evidence is required.
        """
        require_operator(actor, what="installing a new champion")
        authorise(Action.PROMOTE_CAPABILITY, actor,
                  subject=f"{self.arena}:{challenger_id}", clock=self.clock,
                  audit=self.audit)
        winning = [o for o in self.outcomes(challenger_id=challenger_id)
                   if o.replaces]
        if not winning:
            raise PromotionDenied(
                "no recorded contest in which this challenger beat the "
                "champion; an operator may decline a win but may not "
                "manufacture one", arena=self.arena, challenger_id=challenger_id,
                contests=len(self.outcomes(challenger_id=challenger_id)))
        contender = next((c for c in self.challengers()
                          if c.contender_id == challenger_id), None)
        if contender is None:
            raise ConfigurationError("unknown challenger", arena=self.arena,
                                     challenger_id=challenger_id)
        previous = self.champion()
        self._put("champion", payload={**contender.to_dict(),
                                       "installed_at": jsonable(self.clock.now()),
                                       "installed_by": actor.name,
                                       "reason": reason,
                                       "previous_champion":
                                           previous.contender_id if previous else ""})
        if self.audit is not None:
            try:
                from .audit import EventType
                self.audit.record(
                    EventType.DEPLOYMENT_CHANGED,
                    {"arena": self.arena, "new_champion": challenger_id,
                     "previous_champion": previous.contender_id if previous else "",
                     "reason": reason, "operator": actor.name,
                     "winning_contests": [o.to_dict() for o in winning]},
                    actor=actor.name, subject=f"{self.arena}:{challenger_id}")
            except Exception:                            # noqa: BLE001
                pass
        return contender

    def report(self) -> dict:
        champion = self.champion()
        outcomes = self.outcomes()
        by_decision: dict[str, int] = {}
        for outcome in outcomes:
            by_decision[outcome.decision.value] = by_decision.get(outcome.decision.value, 0) + 1
        return {"arena": self.arena,
                "champion": champion.contender_id if champion else None,
                "challengers": [c.contender_id for c in self.challengers()],
                "contests": len(outcomes), "by_decision": by_decision}

    def __repr__(self) -> str:                          # pragma: no cover
        return f"ChampionChallengerBoard(arena={self.arena!r})"


__all__ = ["Decision", "EvaluationHarness", "Contender", "ArmResult",
           "ChallengeOutcome", "compare", "ChampionChallengerBoard",
           "MIN_OBSERVATIONS", "DEFAULT_MIN_EDGE_IN_VOL"]

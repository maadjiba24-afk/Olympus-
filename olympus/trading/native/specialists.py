"""Regime specialists, a general fallback, and a routing record that is kept.

Eight specialists and one generalist. The generalist is not a formality: it is
what answers when no specialist is confident, when a specialist has too little
training support, or when the regime signal itself is unavailable — and a system
without one either forces a bad specialist or abstains on every unusual window.

> Routing decisions must be recorded and evaluated.

`RoutingDecision` is that record: which specialist won, what the scores were,
whether the fallback was used and why, and how much support the winner had. It
is emitted on every call, and `RoutingLog.evaluate` scores routing *against
outcomes* — because a router that always picks the same expert and a router that
picks well are indistinguishable from the decisions alone.

The relationship to the model's own mixture of experts
------------------------------------------------------
`model.py` has a soft mixture routed by the regime head's softmax, inside one
network. This is a different thing at a different level: **separate models**,
each fitted on its own regime's windows, selected discretely. The two coexist on
purpose —

* the in-model mixture is differentiable, always trains every expert, and
  cannot specialise beyond what the regime head sees;
* a specialist here is a whole model with its own parameters, can be trained on
  a different objective, and is selected by a decision that is recorded and
  auditable.

Neither subsumes the other and neither has been shown better. That is stated
rather than resolved, because resolving it needs an ablation this environment
cannot afford.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Sequence

from ..contracts import Regime, ensure_utc, jsonable
from ..errors import ConfigurationError

#: Bumped when a specialist's definition changes.
SPECIALIST_SCHEMA_VERSION = 1

#: Minimum training windows a specialist needs before it may be routed to.
#: Below it the generalist answers, and the decision says so.
DEFAULT_MIN_SUPPORT = 50


class SpecialistKind(str, Enum):
    """The eight, plus the generalist that is always available."""

    TREND = "trend"
    MEAN_REVERSION = "mean_reversion"
    HIGH_VOLATILITY = "high_volatility"
    LOW_VOLATILITY = "low_volatility"
    ILLIQUID = "illiquid"
    EVENT_RISK = "event_risk"
    CRASH = "crash"
    RECOVERY = "recovery"
    #: The fallback. Always registered, never routed away from without a reason.
    GENERAL = "general"


#: What each specialist is for, and the condition that should select it. Data
#: rather than prose so the router's intent is checkable against its behaviour.
SPECIALIST_NOTES: Mapping[SpecialistKind, str] = {
    SpecialistKind.TREND:
        "sustained directional drift; momentum continues more often than it "
        "reverses",
    SpecialistKind.MEAN_REVERSION:
        "range-bound; deviations from a local mean decay",
    SpecialistKind.HIGH_VOLATILITY:
        "dispersion well above its own history; intervals must widen faster "
        "than the point forecast moves",
    SpecialistKind.LOW_VOLATILITY:
        "compressed dispersion; the danger is under-forecasting the exit from "
        "compression, not the compression itself",
    SpecialistKind.ILLIQUID:
        "wide spreads or thin depth; the forecast may be right and unactionable",
    SpecialistKind.EVENT_RISK:
        "a declared high-importance event is near; the distribution is bimodal "
        "and a symmetric interval is the wrong shape",
    SpecialistKind.CRASH:
        "a fast, large drawdown in progress; correlations converge and normal "
        "relationships stop holding",
    SpecialistKind.RECOVERY:
        "after a crash; realised volatility stays elevated while direction "
        "reverses, so volatility and direction disagree",
    SpecialistKind.GENERAL:
        "everything else, and everything where no specialist has support",
}


@dataclass(frozen=True)
class RoutingFeatures:
    """What the router sees. Every field optional and `None` when unknown.

    `None` rather than a default because the router must be able to say "I
    cannot tell" — a missing volatility reading defaulted to zero would route
    every unknown window to the low-volatility specialist.
    """

    regime: str = Regime.UNKNOWN.value
    regime_confidence: float | None = None
    volatility_ratio: float | None = None      # vs its own trailing median
    trend_strength: float | None = None        # signed
    drawdown: float | None = None
    spread_ratio: float | None = None          # vs its own trailing median
    depth_ratio: float | None = None
    bars_to_event: float | None = None
    event_importance_high: bool = False
    recovering: bool = False

    def to_dict(self) -> dict:
        return {"regime": self.regime,
                "regime_confidence": self.regime_confidence,
                "volatility_ratio": self.volatility_ratio,
                "trend_strength": self.trend_strength,
                "drawdown": self.drawdown,
                "spread_ratio": self.spread_ratio,
                "depth_ratio": self.depth_ratio,
                "bars_to_event": self.bars_to_event,
                "event_importance_high": self.event_importance_high,
                "recovering": self.recovering}


@dataclass(frozen=True)
class Specialist:
    """One registered specialist: what it is, and how much it was trained on.

    `support` is training windows. A specialist with a hundred parameters and
    nine training windows is a specialist that memorised nine windows, and the
    router refuses to use it — which is the difference between having eight
    specialists and having eight names.
    """

    kind: SpecialistKind
    support: int = 0
    #: Set when the specialist holds a model. `None` is legitimate: a
    #: registered-but-unfitted specialist is a declared intention, and the
    #: router treats it as unavailable rather than as a silent identity.
    model: Any = None
    version: str = "1"
    notes: str = ""

    def __post_init__(self):
        object.__setattr__(self, "kind", SpecialistKind(self.kind))
        if int(self.support) < 0:
            raise ConfigurationError("support cannot be negative",
                                     kind=self.kind.value)

    def available(self, *, min_support: int = DEFAULT_MIN_SUPPORT) -> bool:
        """Fitted, and fitted on enough. Both, because either alone lies."""
        return self.model is not None and self.support >= min_support

    def to_dict(self) -> dict:
        return {"kind": self.kind.value, "support": self.support,
                "version": self.version, "fitted": self.model is not None,
                "notes": self.notes or SPECIALIST_NOTES[self.kind]}


@dataclass(frozen=True)
class RoutingDecision:
    """Which specialist answered, why, and what the alternatives scored.

    Kept whole. A decision recording only the winner cannot answer "was the
    second choice close", which is the question that tells you whether the
    router is doing anything.
    """

    as_of: datetime
    instrument_key: str
    chosen: SpecialistKind
    scores: Mapping[str, float]
    used_fallback: bool
    reason: str
    features: Mapping[str, Any] = field(default_factory=dict)
    support: int = 0
    #: Specialists that scored but were unavailable, and why.
    unavailable: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "as_of",
                           ensure_utc(self.as_of, field_name="as_of"))
        object.__setattr__(self, "chosen", SpecialistKind(self.chosen))
        object.__setattr__(self, "scores", dict(self.scores))
        object.__setattr__(self, "features", dict(self.features))
        object.__setattr__(self, "unavailable", dict(self.unavailable))
        if not self.reason.strip():
            raise ConfigurationError(
                "a routing decision must state its reason; a route nobody can "
                "explain is a route nobody can evaluate")

    @property
    def runner_up(self) -> tuple[str, float] | None:
        ranked = sorted(self.scores.items(), key=lambda kv: -kv[1])
        others = [r for r in ranked if r[0] != self.chosen.value]
        return others[0] if others else None

    @property
    def margin(self) -> float | None:
        """How decisive the choice was. Small means the router nearly tossed a
        coin, which is worth knowing before trusting the specialisation."""
        runner_up = self.runner_up
        if runner_up is None:
            return None
        return self.scores.get(self.chosen.value, 0.0) - runner_up[1]

    def to_dict(self) -> dict:
        return {"as_of": jsonable(self.as_of),
                "instrument": self.instrument_key,
                "chosen": self.chosen.value, "scores": dict(self.scores),
                "used_fallback": self.used_fallback, "reason": self.reason,
                "support": self.support, "margin": self.margin,
                "runner_up": self.runner_up,
                "features": jsonable(dict(self.features)),
                "unavailable": dict(self.unavailable)}


def score_specialists(features: RoutingFeatures) -> dict[str, float]:
    """Score every specialist against the features. Transparent by design.

    Rules rather than a learned router, and that is a considered trade. A
    learned router would need its own training data and would make "why was
    this window routed here" unanswerable; these rules are readable, and a
    reader can disagree with a specific line. The cost is that the rules encode
    somebody's judgement about what a regime looks like, and that judgement has
    not been validated against outcomes — `RoutingLog.evaluate` is what would
    do that.
    """
    scores: dict[str, float] = {k.value: 0.0 for k in SpecialistKind}
    # The generalist starts with a floor, so a window that excites no rule
    # routes to it rather than to whichever specialist scored a rounding error.
    scores[SpecialistKind.GENERAL.value] = 0.2

    volatility = features.volatility_ratio
    if volatility is not None:
        if volatility >= 2.0:
            scores[SpecialistKind.HIGH_VOLATILITY.value] += min(1.0, volatility / 3.0)
        elif volatility <= 0.6:
            scores[SpecialistKind.LOW_VOLATILITY.value] += 1.0 - volatility

    trend = features.trend_strength
    if trend is not None:
        strength = min(1.0, abs(trend) * 10.0)
        if strength >= 0.4:
            scores[SpecialistKind.TREND.value] += strength
        else:
            scores[SpecialistKind.MEAN_REVERSION.value] += 0.6 - strength

    if features.regime == Regime.RANGING.value:
        scores[SpecialistKind.MEAN_REVERSION.value] += 0.5
    elif features.regime in (Regime.TRENDING_UP.value,
                             Regime.TRENDING_DOWN.value):
        scores[SpecialistKind.TREND.value] += 0.5
    elif features.regime == Regime.HIGH_VOLATILITY.value:
        scores[SpecialistKind.HIGH_VOLATILITY.value] += 0.5
    elif features.regime == Regime.CRISIS.value:
        scores[SpecialistKind.CRASH.value] += 1.0

    drawdown = features.drawdown
    if drawdown is not None and drawdown >= 0.1:
        scores[SpecialistKind.CRASH.value] += min(1.5, drawdown * 5.0)
    if features.recovering:
        scores[SpecialistKind.RECOVERY.value] += 1.0

    spread = features.spread_ratio
    depth = features.depth_ratio
    if (spread is not None and spread >= 2.0) or (depth is not None and depth <= 0.5):
        scores[SpecialistKind.ILLIQUID.value] += 1.2

    bars = features.bars_to_event
    if bars is not None and features.event_importance_high and abs(bars) <= 3.0:
        scores[SpecialistKind.EVENT_RISK.value] += 1.5

    # Confidence scales every *specialist* score but never the generalist's:
    # a low-confidence regime reading should make specialisation less
    # attractive, not make everything equally unattractive.
    confidence = features.regime_confidence
    if confidence is not None:
        factor = max(0.0, min(1.0, confidence))
        for key in scores:
            if key != SpecialistKind.GENERAL.value:
                scores[key] *= factor
    return scores


class SpecialistRouter:
    """Registers specialists, routes, and keeps the record.

    The generalist is registered automatically if the caller does not, because
    a router with no fallback is a router that must always pick a specialist —
    which is the failure mode the fallback exists to prevent.
    """

    __slots__ = ("_specialists", "min_support", "min_margin", "_log")

    def __init__(self, specialists: Sequence[Specialist] = (), *,
                 min_support: int = DEFAULT_MIN_SUPPORT,
                 min_margin: float = 0.15):
        self._specialists: dict[SpecialistKind, Specialist] = {}
        for specialist in specialists:
            self.register(specialist)
        self._specialists.setdefault(
            SpecialistKind.GENERAL,
            Specialist(kind=SpecialistKind.GENERAL, support=0))
        if min_support < 0:
            raise ConfigurationError("min_support must be >= 0")
        if min_margin < 0:
            raise ConfigurationError("min_margin must be >= 0")
        self.min_support = int(min_support)
        self.min_margin = float(min_margin)
        self._log: list[RoutingDecision] = []

    def register(self, specialist: Specialist) -> "SpecialistRouter":
        if specialist.kind in self._specialists:
            raise ConfigurationError("specialist already registered",
                                     kind=specialist.kind.value)
        self._specialists[specialist.kind] = specialist
        return self

    @property
    def specialists(self) -> Mapping[str, Specialist]:
        return {k.value: v for k, v in self._specialists.items()}

    @property
    def available(self) -> tuple[str, ...]:
        return tuple(sorted(
            k.value for k, s in self._specialists.items()
            if k is not SpecialistKind.GENERAL
            and s.available(min_support=self.min_support)))

    def route(self, features: RoutingFeatures, *, as_of: datetime,
              instrument_key: str = "") -> RoutingDecision:
        """Choose, and record why. Falls back for four distinct reasons."""
        scores = score_specialists(features)
        unavailable: dict[str, str] = {}
        # `score_specialists` scores every declared kind, including ones this
        # router has no specialist for. Scoring them is right — the scores show
        # what the features looked like — but routing to one is not, so an
        # unregistered kind is unavailable with that as its reason.
        for kind in SpecialistKind:
            if kind is SpecialistKind.GENERAL:
                continue
            specialist = self._specialists.get(kind)
            if specialist is None:
                unavailable[kind.value] = "not registered"
            elif specialist.model is None:
                unavailable[kind.value] = "not fitted"
            elif specialist.support < self.min_support:
                unavailable[kind.value] = (
                    f"support {specialist.support} < {self.min_support}")

        candidates = {k: v for k, v in scores.items()
                      if k != SpecialistKind.GENERAL.value
                      and k not in unavailable}
        general_score = scores[SpecialistKind.GENERAL.value]

        if not candidates:
            reason = ("no specialist is both fitted and sufficiently supported"
                      if unavailable else "no specialists are registered")
            return self._record(RoutingDecision(
                as_of=as_of, instrument_key=instrument_key,
                chosen=SpecialistKind.GENERAL, scores=scores,
                used_fallback=True, reason=reason,
                features=features.to_dict(), support=0,
                unavailable=unavailable))

        best_key, best_score = max(candidates.items(), key=lambda kv: kv[1])
        others = [v for k, v in candidates.items() if k != best_key]
        margin = best_score - (max(others) if others else 0.0)

        if best_score <= general_score:
            return self._record(RoutingDecision(
                as_of=as_of, instrument_key=instrument_key,
                chosen=SpecialistKind.GENERAL, scores=scores,
                used_fallback=True,
                reason=f"no specialist outscored the generalist "
                       f"({best_score:.2f} <= {general_score:.2f})",
                features=features.to_dict(), support=0,
                unavailable=unavailable))

        if margin < self.min_margin:
            return self._record(RoutingDecision(
                as_of=as_of, instrument_key=instrument_key,
                chosen=SpecialistKind.GENERAL, scores=scores,
                used_fallback=True,
                reason=f"the top two specialists are within {margin:.3f}, "
                       f"below the {self.min_margin} margin: the router is "
                       f"guessing and the generalist is the honest answer",
                features=features.to_dict(), support=0,
                unavailable=unavailable))

        chosen = SpecialistKind(best_key)
        return self._record(RoutingDecision(
            as_of=as_of, instrument_key=instrument_key, chosen=chosen,
            scores=scores, used_fallback=False,
            reason=f"{chosen.value} scored {best_score:.2f}, "
                   f"{margin:.2f} clear of the next",
            features=features.to_dict(),
            support=self._specialists[chosen].support,
            unavailable=unavailable))

    def model_for(self, decision: RoutingDecision) -> Any:
        return self._specialists[decision.chosen].model

    def _record(self, decision: RoutingDecision) -> RoutingDecision:
        self._log.append(decision)
        return decision

    @property
    def log(self) -> "RoutingLog":
        return RoutingLog(tuple(self._log))

    def to_dict(self) -> dict:
        return {"schema_version": SPECIALIST_SCHEMA_VERSION,
                "specialists": {k: s.to_dict()
                                for k, s in self.specialists.items()},
                "available": list(self.available),
                "min_support": self.min_support,
                "min_margin": self.min_margin,
                "decisions": len(self._log)}


@dataclass(frozen=True)
class RoutingLog:
    """Every decision, and the evaluation that says whether routing helped."""

    decisions: tuple[RoutingDecision, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "decisions", tuple(self.decisions))

    def __len__(self) -> int:
        return len(self.decisions)

    @property
    def fallback_rate(self) -> float:
        if not self.decisions:
            return 0.0
        return sum(1 for d in self.decisions if d.used_fallback) / len(self.decisions)

    @property
    def distribution(self) -> Mapping[str, int]:
        out: dict[str, int] = {}
        for decision in self.decisions:
            out[decision.chosen.value] = out.get(decision.chosen.value, 0) + 1
        return dict(sorted(out.items()))

    @property
    def degenerate(self) -> bool:
        """True when one destination takes almost everything.

        A router that always picks the same destination is not routing. Worth a
        property rather than a footnote, because the decisions alone look
        perfectly reasonable one at a time.
        """
        if len(self.decisions) < 10:
            return False
        counts = self.distribution
        return max(counts.values()) / len(self.decisions) >= 0.95

    def evaluate(self, errors: Sequence[float]) -> Mapping[str, Any]:
        """Did routing help? Error per destination, against the overall mean.

        The comparison that matters is a specialist's error against the *whole
        set's* error, not against the generalist's — because the windows the
        generalist saw are the ones no specialist wanted, and comparing a
        specialist's easy windows with the generalist's hard ones flatters the
        specialist automatically.
        """
        if len(errors) != len(self.decisions):
            raise ConfigurationError(
                "one error per decision is required to evaluate routing",
                decisions=len(self.decisions), errors=len(errors))
        if not errors:
            return {"n": 0, "usable": False,
                    "note": "no decisions to evaluate"}

        overall = statistics.fmean(errors)
        by_destination: dict[str, list[float]] = {}
        for decision, error in zip(self.decisions, errors):
            by_destination.setdefault(decision.chosen.value, []).append(error)

        per_destination = {
            name: {"n": len(values), "mae": statistics.fmean(values),
                   "vs_overall": overall - statistics.fmean(values)}
            for name, values in sorted(by_destination.items())}

        specialised = [e for d, e in zip(self.decisions, errors)
                       if not d.used_fallback]
        fell_back = [e for d, e in zip(self.decisions, errors) if d.used_fallback]
        return {
            "n": len(errors), "overall_mae": overall,
            "fallback_rate": self.fallback_rate,
            "degenerate": self.degenerate,
            "per_destination": per_destination,
            "specialised_mae": statistics.fmean(specialised) if specialised else None,
            "fallback_mae": statistics.fmean(fell_back) if fell_back else None,
            "usable": len(errors) >= 30 and not self.degenerate,
            "note": ("fewer than 30 decisions, or one destination took "
                     "everything: routing cannot be judged from this"
                     if len(errors) < 30 or self.degenerate
                     else "a destination whose vs_overall is negative is "
                          "handling windows it makes worse"),
        }

    def to_dict(self) -> dict:
        return {"n": len(self.decisions),
                "fallback_rate": round(self.fallback_rate, 4),
                "distribution": dict(self.distribution),
                "degenerate": self.degenerate,
                "decisions": [d.to_dict() for d in self.decisions]}


__all__ = ["SPECIALIST_SCHEMA_VERSION", "DEFAULT_MIN_SUPPORT", "SpecialistKind",
           "SPECIALIST_NOTES", "RoutingFeatures", "Specialist",
           "RoutingDecision", "score_specialists", "SpecialistRouter",
           "RoutingLog"]

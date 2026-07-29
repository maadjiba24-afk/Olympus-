"""Causal scenario generation: several plausible futures, each with a probability.

> Scenarios must be probabilistic forecasts, not statements of certainty.

Enforced structurally. Every `Scenario` carries a probability, `ScenarioSet`
refuses to construct unless they sum to one, and there is no field anywhere
holding "the" outcome. The closest thing to a point forecast is
`ScenarioSet.most_likely`, and it returns a scenario — which still has a
probability attached and a path that is a band, not a line.

Where the probabilities come from
---------------------------------
Not from the scenario generator. They come from the model's own predicted
quantiles: a bearish scenario's probability is the mass the model already put
below its median, a bullish one's is the mass above. So the scenarios are a
*re-presentation* of the forecast the model made, not a second opinion layered
on top of it. That matters because a generator inventing its own probabilities
would let a confident model produce a hedged-looking scenario set, or the
reverse, with nothing tying the two together.

The three conditional scenarios are different, and say so
----------------------------------------------------------
`high_volatility`, `liquidity_stress` and `event_shock` are **conditional**:
their probabilities are the declared chance of the condition occurring, not
something read off the return distribution — because the return distribution
does not contain a liquidity event. They are marked `conditional=True`, their
probabilities are declared inputs rather than derived, and `ScenarioSet`
normalises the unconditional ones around them so the set still sums to one.

`regime_transition` sits in between: the probability comes from the regime
head's own distribution when one is supplied, and is declared otherwise.

What a scenario is not
----------------------
Not a simulated path. Producing sample paths would require a generative model
Olympus does not have, and would invite reading a single path as a prediction.
A scenario here is a *band* — a low, a central and a high trajectory — plus the
conditions under which it would be the one that happened.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Mapping, Sequence

from ..contracts import ensure_utc, jsonable
from ..errors import ConfigurationError

#: Bumped when a scenario kind's meaning changes.
SCENARIO_SCHEMA_VERSION = 1

#: Probabilities must sum to this, within tolerance.
_TOTAL = 1.0
_TOLERANCE = 1e-6


class ScenarioKind(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    HIGH_VOLATILITY = "high_volatility"
    LIQUIDITY_STRESS = "liquidity_stress"
    REGIME_TRANSITION = "regime_transition"
    EVENT_SHOCK = "event_shock"


#: Kinds whose probability cannot be read off a return distribution, because
#: the thing they describe is not a return.
CONDITIONAL_KINDS: frozenset[ScenarioKind] = frozenset({
    ScenarioKind.HIGH_VOLATILITY, ScenarioKind.LIQUIDITY_STRESS,
    ScenarioKind.EVENT_SHOCK,
})


@dataclass(frozen=True)
class Scenario:
    """One plausible future: a band, a probability, and what would cause it."""

    kind: ScenarioKind
    probability: float
    #: Cumulative log return per horizon step — low, central, high.
    low: tuple[float, ...]
    central: tuple[float, ...]
    high: tuple[float, ...]
    #: What would have to be true for this to be the one that happened.
    drivers: tuple[str, ...] = ()
    #: What would falsify it. Distinct from drivers: a scenario nobody can
    #: falsify is not a forecast.
    invalidated_by: tuple[str, ...] = ()
    conditional: bool = False
    #: Where the probability came from. "derived" or "declared".
    probability_basis: str = "derived"

    def __post_init__(self):
        object.__setattr__(self, "kind", ScenarioKind(self.kind))
        for name in ("low", "central", "high"):
            object.__setattr__(self, name, tuple(float(v)
                                                 for v in getattr(self, name)))
        for name in ("drivers", "invalidated_by"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        lengths = {len(self.low), len(self.central), len(self.high)}
        if len(lengths) != 1 or 0 in lengths:
            raise ConfigurationError(
                "a scenario's three trajectories must be the same non-zero "
                "length", kind=self.kind.value, lengths=sorted(lengths))
        if not 0.0 <= self.probability <= 1.0:
            raise ConfigurationError("a probability must be in [0, 1]",
                                     kind=self.kind.value,
                                     probability=self.probability)
        for step, (lo, mid, hi) in enumerate(zip(self.low, self.central,
                                                 self.high)):
            if not lo <= mid <= hi:
                raise ConfigurationError(
                    "a scenario's band is inverted: its low is not below its "
                    "central or its central is not below its high",
                    kind=self.kind.value, step=step, low=lo, central=mid,
                    high=hi)
        if not self.invalidated_by:
            raise ConfigurationError(
                "a scenario must say what would falsify it; one that cannot be "
                "falsified is a story, not a forecast", kind=self.kind.value)
        if self.conditional != (self.kind in CONDITIONAL_KINDS) \
                and self.kind is not ScenarioKind.REGIME_TRANSITION:
            raise ConfigurationError(
                "this kind's conditional flag does not match its definition",
                kind=self.kind.value, conditional=self.conditional)

    @property
    def horizon(self) -> int:
        return len(self.central)

    @property
    def terminal(self) -> float:
        return self.central[-1]

    @property
    def width(self) -> float:
        return self.high[-1] - self.low[-1]

    def prices(self, anchor: float) -> Mapping[str, tuple[float, ...]]:
        if anchor <= 0:
            raise ConfigurationError("anchor must be positive", anchor=anchor)
        return {name: tuple(anchor * math.exp(v) for v in values)
                for name, values in (("low", self.low),
                                     ("central", self.central),
                                     ("high", self.high))}

    def to_dict(self) -> dict:
        return {"kind": self.kind.value, "probability": self.probability,
                "low": list(self.low), "central": list(self.central),
                "high": list(self.high), "terminal": self.terminal,
                "width": self.width, "drivers": list(self.drivers),
                "invalidated_by": list(self.invalidated_by),
                "conditional": self.conditional,
                "probability_basis": self.probability_basis}


@dataclass(frozen=True)
class ScenarioSet:
    """Several futures that sum to one. There is no "the" outcome here."""

    as_of: datetime
    instrument_key: str
    horizon: int
    scenarios: tuple[Scenario, ...]
    anchor_close: float | None = None
    schema_version: int = SCENARIO_SCHEMA_VERSION

    def __post_init__(self):
        object.__setattr__(self, "as_of",
                           ensure_utc(self.as_of, field_name="as_of"))
        object.__setattr__(self, "scenarios", tuple(self.scenarios))
        if not self.scenarios:
            raise ConfigurationError("a scenario set needs scenarios")
        kinds = [s.kind for s in self.scenarios]
        if len(set(kinds)) != len(kinds):
            raise ConfigurationError("a scenario kind appears twice",
                                     kinds=[k.value for k in kinds])
        horizons = {s.horizon for s in self.scenarios}
        if horizons != {self.horizon}:
            raise ConfigurationError("scenarios disagree about the horizon",
                                     declared=self.horizon,
                                     found=sorted(horizons))
        total = sum(s.probability for s in self.scenarios)
        if abs(total - _TOTAL) > _TOLERANCE:
            raise ConfigurationError(
                "scenario probabilities must sum to one; a set that does not "
                "is a set that has an unstated outcome in it",
                total=total, kinds=[k.value for k in kinds])

    @property
    def most_likely(self) -> Scenario:
        """The highest-probability scenario. Still a scenario, still a band."""
        return max(self.scenarios, key=lambda s: s.probability)

    def by_kind(self, kind: ScenarioKind) -> Scenario | None:
        return next((s for s in self.scenarios if s.kind is kind), None)

    @property
    def expected_terminal(self) -> float:
        """Probability-weighted terminal return across scenarios.

        Provided because a consumer needs one number sometimes — and named
        `expected_` rather than `forecast` so that reading it as a prediction
        of what will happen requires ignoring the word.
        """
        return sum(s.probability * s.terminal for s in self.scenarios)

    @property
    def downside(self) -> float:
        """Probability-weighted mass on scenarios whose central path falls."""
        return sum(s.probability for s in self.scenarios if s.terminal < 0)

    @property
    def worst_case(self) -> float:
        """The lowest low across every scenario. What the risk engine reads."""
        return min(s.low[-1] for s in self.scenarios)

    @property
    def conditional_mass(self) -> float:
        return sum(s.probability for s in self.scenarios if s.conditional)

    def to_dict(self) -> dict:
        return {"as_of": jsonable(self.as_of),
                "instrument": self.instrument_key, "horizon": self.horizon,
                "anchor_close": self.anchor_close,
                "scenarios": [s.to_dict() for s in self.scenarios],
                "most_likely": self.most_likely.kind.value,
                "expected_terminal": self.expected_terminal,
                "downside": self.downside, "worst_case": self.worst_case,
                "conditional_mass": self.conditional_mass,
                "schema_version": self.schema_version}


def _scale(path: Sequence[float], factor: float) -> tuple[float, ...]:
    return tuple(v * factor for v in path)


def generate_scenarios(*, quantiles: Mapping[str, Sequence[float]],
                       median_label: str, as_of: datetime,
                       instrument_key: str, anchor_close: float | None = None,
                       regime_transition_probability: float | None = None,
                       volatility_stress_probability: float = 0.05,
                       liquidity_stress_probability: float = 0.03,
                       event_shock_probability: float = 0.0,
                       stress_multiple: float = 2.0) -> ScenarioSet:
    """Build the six scenarios from a model's own predicted quantiles.

    The unconditional two — bullish and bearish — take their probabilities from
    where the model put its mass, so a confident model produces a lopsided set
    and a hedged one produces a balanced set *automatically*. The conditional
    three take declared probabilities, because a return distribution contains no
    information about whether the exchange will halt.

    The unconditional mass is whatever the conditional probabilities leave, so
    the set always sums to one without any scenario being invented to close the
    gap.
    """
    from ..evaluate import quantile_level

    if not quantiles:
        raise ConfigurationError("no quantiles to build scenarios from")
    if median_label not in quantiles:
        raise ConfigurationError("the median label is not among the quantiles",
                                 median_label=median_label,
                                 available=sorted(quantiles))
    ordered = sorted(quantiles, key=lambda k: quantile_level(k) or 0.0)
    low_label, high_label = ordered[0], ordered[-1]
    low = tuple(float(v) for v in quantiles[low_label])
    central = tuple(float(v) for v in quantiles[median_label])
    high = tuple(float(v) for v in quantiles[high_label])
    horizon = len(central)

    declared = (float(volatility_stress_probability)
                + float(liquidity_stress_probability)
                + float(event_shock_probability))
    transition = (float(regime_transition_probability)
                  if regime_transition_probability is not None else 0.05)
    for name, value in (("volatility", volatility_stress_probability),
                        ("liquidity", liquidity_stress_probability),
                        ("event", event_shock_probability),
                        ("regime_transition", transition)):
        if not 0.0 <= float(value) <= 1.0:
            raise ConfigurationError(f"{name} probability must be in [0, 1]",
                                     **{name: value})
    if declared + transition >= 1.0:
        raise ConfigurationError(
            "the declared conditional probabilities leave no room for the "
            "return distribution's own mass",
            conditional_total=declared + transition)

    unconditional = 1.0 - declared - transition
    # The model's own asymmetry: how much of its band sits above the median.
    # A symmetric forecast gives 0.5 and a skewed one does not, which is what
    # makes the split a property of the forecast rather than of this function.
    span = high[-1] - low[-1]
    upside_share = 0.5 if span <= 0 else max(
        0.05, min(0.95, (high[-1] - central[-1]) / span))
    bullish_p = unconditional * upside_share
    bearish_p = unconditional * (1.0 - upside_share)

    scenarios = [
        Scenario(kind=ScenarioKind.BULLISH, probability=bullish_p,
                 low=central, central=tuple(
                     (c + h) / 2.0 for c, h in zip(central, high)),
                 high=high,
                 drivers=("the upper half of the model's own predicted "
                          "distribution",),
                 invalidated_by=("price closing below the median path",
                                 "the direction head falling below 0.5"),
                 probability_basis="derived from the predicted quantiles"),
        Scenario(kind=ScenarioKind.BEARISH, probability=bearish_p,
                 low=low, central=tuple(
                     (c + l) / 2.0 for c, l in zip(central, low)),
                 high=central,
                 drivers=("the lower half of the model's own predicted "
                          "distribution",),
                 invalidated_by=("price closing above the median path",
                                 "the direction head rising above 0.5"),
                 probability_basis="derived from the predicted quantiles"),
        Scenario(kind=ScenarioKind.HIGH_VOLATILITY,
                 probability=float(volatility_stress_probability),
                 low=_scale(low, stress_multiple), central=central,
                 high=_scale(high, stress_multiple),
                 drivers=("realised volatility rising well above its trailing "
                          "median",),
                 invalidated_by=("realised volatility staying within its "
                                 "trailing range",),
                 conditional=True,
                 probability_basis="declared; a return distribution does not "
                                   "contain the chance of a volatility regime "
                                   "change"),
        Scenario(kind=ScenarioKind.LIQUIDITY_STRESS,
                 probability=float(liquidity_stress_probability),
                 low=_scale(low, stress_multiple),
                 central=tuple(v - abs(v) * 0.5 for v in central),
                 high=central,
                 drivers=("spread widening past twice its median, or depth "
                          "halving",),
                 invalidated_by=("the book staying within its normal spread "
                                 "and depth range",),
                 conditional=True,
                 probability_basis="declared; the book is not in the return "
                                   "distribution"),
        Scenario(kind=ScenarioKind.REGIME_TRANSITION, probability=transition,
                 low=_scale(low, 1.5),
                 central=tuple(-v * 0.5 for v in central),
                 high=_scale(high, 1.5),
                 drivers=("the regime head's distribution moving toward a "
                          "different label",),
                 invalidated_by=("the regime label persisting for the whole "
                                 "horizon",),
                 conditional=False,
                 probability_basis=("derived from the regime head"
                                    if regime_transition_probability is not None
                                    else "declared default")),
        Scenario(kind=ScenarioKind.EVENT_SHOCK,
                 probability=float(event_shock_probability),
                 low=_scale(low, stress_multiple * 1.5), central=central,
                 high=_scale(high, stress_multiple * 1.5),
                 drivers=("a declared high-importance event inside the "
                          "horizon",),
                 invalidated_by=("the event passing without a repricing",
                                 "the event being rescheduled outside the "
                                 "horizon"),
                 conditional=True,
                 probability_basis="declared from the event calendar"),
    ]
    # Drop zero-probability conditionals rather than carrying them: a scenario
    # with probability zero is not a scenario, and keeping it invites reading
    # the list length as a count of possibilities.
    kept = [s for s in scenarios if s.probability > 0.0]
    if not kept:                                         # pragma: no cover
        raise ConfigurationError("every scenario had zero probability")

    # Renormalise onto exactly one. The arithmetic above is already close; this
    # removes float drift rather than reallocating meaningfully.
    total = sum(s.probability for s in kept)
    kept = [Scenario(kind=s.kind, probability=s.probability / total,
                     low=s.low, central=s.central, high=s.high,
                     drivers=s.drivers, invalidated_by=s.invalidated_by,
                     conditional=s.conditional,
                     probability_basis=s.probability_basis) for s in kept]

    return ScenarioSet(as_of=as_of, instrument_key=instrument_key,
                       horizon=horizon, scenarios=tuple(kept),
                       anchor_close=anchor_close)


__all__ = ["SCENARIO_SCHEMA_VERSION", "ScenarioKind", "CONDITIONAL_KINDS",
           "Scenario", "ScenarioSet", "generate_scenarios"]

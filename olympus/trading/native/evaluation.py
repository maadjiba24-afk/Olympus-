"""Evaluation: fourteen metrics, eight strata, and whether decisions improved.

Two rules shape this module.

**Do not optimise exclusively for price error.** A model can win on MAE and lose
money, because MAE does not know about costs, direction or the fact that being
wrong in the tail is not the same as being wrong in the middle. So the headline
is not a price metric: `decision_value` asks whether acting on the forecast beat
acting on the baseline *after costs*, and `StratifiedReport.verdict` reads that
before it reads anything else.

**Report by stratum, because an average hides the case that matters.** A model
that is excellent in calm markets and useless in volatile ones has the same mean
error as one that is mediocre everywhere, and only one of them is dangerous.
Eight strata are cut — instrument, timeframe, horizon, regime, volatility
environment, liquidity environment, calendar period, and seen-versus-unseen
instruments — and each is reported separately with its own sample count, because
a stratum with nine observations is not a result.

What is honestly unavailable here
---------------------------------
The **liquidity environment** stratum needs book depth, which is `NOT_INGESTED`
(blocker B1). It is cut on traded volume instead and the stratum is *labelled*
as a proxy in its own output — `LIQUIDITY_PROXY_NOTE` travels with the report so
a reader cannot mistake it for the real thing. The **external-benchmark** comparison arm
exists in `ComparisonArm` and is never populated here, because no third-party
checkpoint is reachable (blocker B2). `unavailable_arm` is how a caller declares
one, and `missing_arms` carries it into the report rather than omitting it —
the arm's identity comes from the adapter that owns that model, never from
here.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from ..contracts import jsonable
from ..errors import ConfigurationError, InsufficientDataError
from ..evaluate import paired_bootstrap
from .benchmark import CostModel
from .tasks import target_drawdown_magnitude, target_volatility

#: Minimum observations before a stratum's numbers are reported as a result
#: rather than as a count. Below it, `Stratum.sufficient` is False and
#: `StratifiedReport.verdict` ignores the stratum.
MIN_STRATUM_SIZE = 30

LIQUIDITY_PROXY_NOTE = (
    "the liquidity environment is cut on traded volume, not on book depth: "
    "depth is NOT_INGESTED (blocker B1). A thin book can trade large volume at "
    "terrible prices, which is exactly the case this stratum would exist to "
    "isolate, so treat it as a proxy and not as a liquidity result."
)


# ===========================================================================
# per-observation record
# ===========================================================================

@dataclass(frozen=True)
class Observation:
    """One forecast and what actually happened. The unit everything is cut on."""

    instrument_key: str
    timeframe: str
    horizon: int
    as_of: datetime
    #: Predicted terminal cumulative log return (the median).
    predicted: float
    actual: float
    #: `{quantile_label: predicted terminal value}`.
    quantiles: Mapping[str, float] = field(default_factory=dict)
    #: From the direction head, when present.
    direction_probability: float | None = None
    predicted_volatility: float | None = None
    actual_volatility: float | None = None
    drawdown_probability: float | None = None
    actual_drawdown: float | None = None
    regime: str = ""
    #: Realised volatility of the input window. Cuts the volatility stratum.
    window_volatility: float | None = None
    #: Mean traded volume of the input window. Cuts the liquidity proxy.
    window_volume: float | None = None
    abstained: bool = False
    abstention_reason: str = ""
    seen_instrument: bool = True
    inference_ms: float | None = None
    failed: bool = False

    @property
    def anchor_price(self) -> float:
        return 1.0

    @property
    def absolute_error(self) -> float:
        return abs(self.actual - self.predicted)

    @property
    def directional_correct(self) -> bool:
        return (self.predicted > 0) == (self.actual > 0)

    def to_dict(self) -> dict:
        return {"instrument": self.instrument_key, "timeframe": self.timeframe,
                "horizon": self.horizon, "as_of": jsonable(self.as_of),
                "predicted": self.predicted, "actual": self.actual,
                "regime": self.regime, "abstained": self.abstained,
                "abstention_reason": self.abstention_reason,
                "seen_instrument": self.seen_instrument, "failed": self.failed}


# ===========================================================================
# the fourteen metrics
# ===========================================================================

def _finite(values: Sequence[float]) -> list[float]:
    return [float(v) for v in values if v is not None and math.isfinite(float(v))]


def mae(observations: Sequence[Observation]) -> float | None:
    values = _finite([o.absolute_error for o in observations])
    return statistics.fmean(values) if values else None


def rmse(observations: Sequence[Observation]) -> float | None:
    values = _finite([o.absolute_error ** 2 for o in observations])
    return math.sqrt(statistics.fmean(values)) if values else None


def mape(observations: Sequence[Observation]) -> float | None:
    """Mean absolute percentage error — **only where numerically appropriate**.

    Returns `None` when any actual is near zero, rather than a large number
    that looks like a result. On log returns the actual *is* routinely near
    zero, so this metric is usually unavailable here and saying so is more
    useful than reporting a division by 1e-9.
    """
    pairs = [(o.predicted, o.actual) for o in observations
             if math.isfinite(o.actual)]
    if not pairs:
        return None
    if any(abs(a) < 1e-4 for _, a in pairs):
        return None
    return statistics.fmean(abs((a - p) / a) for p, a in pairs)


def directional_accuracy(observations: Sequence[Observation]) -> float | None:
    scored = [o for o in observations if o.actual != 0]
    if not scored:
        return None
    return sum(1 for o in scored if o.directional_correct) / len(scored)


def brier_score(observations: Sequence[Observation]) -> float | None:
    """Mean squared error of the direction probability. Lower is better."""
    scored = [o for o in observations if o.direction_probability is not None]
    if not scored:
        return None
    return statistics.fmean(
        (o.direction_probability - (1.0 if o.actual > 0 else 0.0)) ** 2
        for o in scored)


def log_loss(observations: Sequence[Observation], *, epsilon: float = 1e-7
             ) -> float | None:
    """Binary log loss of the direction probability.

    Clipped at `epsilon`. An unclipped log loss is infinite the first time a
    model is confidently wrong, and one infinity makes every comparison
    meaningless — which is a property of the metric, not of the model.
    """
    scored = [o for o in observations if o.direction_probability is not None]
    if not scored:
        return None
    total = 0.0
    for o in scored:
        p = min(1.0 - epsilon, max(epsilon, float(o.direction_probability)))
        total += -(math.log(p) if o.actual > 0 else math.log(1.0 - p))
    return total / len(scored)


def quantile_loss(observations: Sequence[Observation]) -> float | None:
    """Mean pinball loss across the reported quantile grid."""
    from ..evaluate import quantile_level
    rows: list[float] = []
    for o in observations:
        if not o.quantiles:
            continue
        per_level = []
        for label, value in o.quantiles.items():
            tau = quantile_level(label)
            if tau is None:                              # pragma: no cover
                continue
            difference = o.actual - float(value)
            per_level.append(tau * difference if difference >= 0
                             else (tau - 1.0) * difference)
        if per_level:
            rows.append(statistics.fmean(per_level))
    return statistics.fmean(rows) if rows else None


def calibration_error(observations: Sequence[Observation]) -> float | None:
    """Signed coverage error of the widest interval, in coverage points.

    Signed: a band covering 95% against a nominal 80% is over-cautious and one
    covering 60% is dangerous, and an absolute error would rank them the same.
    """
    from ..evaluate import quantile_level
    scored = [o for o in observations if len(o.quantiles) >= 2]
    if not scored:
        return None
    labels = sorted(scored[0].quantiles,
                    key=lambda k: quantile_level(k) or 0.0)
    low, high = labels[0], labels[-1]
    nominal = (quantile_level(high) or 1.0) - (quantile_level(low) or 0.0)
    inside = sum(1 for o in scored
                 if float(o.quantiles[low]) <= o.actual <= float(o.quantiles[high]))
    return (inside / len(scored) - nominal) * 100.0


def volatility_error(observations: Sequence[Observation]) -> float | None:
    """Mean absolute error of the volatility head, in volatility units."""
    scored = [o for o in observations
              if o.predicted_volatility is not None
              and o.actual_volatility is not None]
    if not scored:
        return None
    return statistics.fmean(abs(o.predicted_volatility - o.actual_volatility)
                            for o in scored)


def drawdown_quality(observations: Sequence[Observation], *,
                     threshold: float = 0.02) -> Mapping[str, float] | None:
    """Brier score, base rate and lift for the drawdown head.

    Lift, not accuracy. A threshold breached 5% of the time is predicted with
    95% accuracy by a head that always says no, so accuracy is the one number
    that must not be the headline here.
    """
    scored = [o for o in observations
              if o.drawdown_probability is not None
              and o.actual_drawdown is not None]
    if not scored:
        return None
    outcomes = [1.0 if o.actual_drawdown > threshold else 0.0 for o in scored]
    base = statistics.fmean(outcomes)
    brier = statistics.fmean(
        (o.drawdown_probability - y) ** 2 for o, y in zip(scored, outcomes))
    # The Brier score a constant base-rate forecast would achieve.
    reference = statistics.fmean((base - y) ** 2 for y in outcomes)
    skill = (1.0 - brier / reference) if reference > 0 else 0.0
    return {"brier": brier, "base_rate": base, "reference_brier": reference,
            "skill_score": skill, "n": float(len(scored))}


def abstention_quality(observations: Sequence[Observation]) -> Mapping[str, float]:
    """Did abstaining actually avoid the bad forecasts?

    The measure that matters is `error_avoided`: the mean error on the windows
    the model declined, minus the mean error on the ones it answered, computed
    from a *reference* model that answered everything. A model that abstains on
    windows it would have got right is abstaining for no reason, and an
    abstention rate alone cannot tell the two apart.
    """
    total = len(observations)
    abstained = [o for o in observations if o.abstained]
    answered = [o for o in observations if not o.abstained]
    out: dict[str, float] = {
        "rate": len(abstained) / total if total else 0.0,
        "n_abstained": float(len(abstained)),
        "n_answered": float(len(answered)),
    }
    if abstained and answered:
        # `absolute_error` on a declined row is |actual - 0|, i.e. how large the
        # move was. That makes `error_avoided` a statement about whether the
        # declined windows were the volatile ones — useful, and *not* the same
        # as "the error the model would have made", which is unknowable because
        # it did not make one. Named `move_on_declined` for that reason.
        declined = _finite([abs(o.actual) for o in abstained])
        answered_error = _finite([o.absolute_error for o in answered])
        if declined and answered_error:
            out["move_on_declined"] = statistics.fmean(declined)
            out["error_on_answered"] = statistics.fmean(answered_error)
            out["selectivity"] = (statistics.fmean(declined)
                                  - statistics.fmean(answered_error))
    return out


def latency(observations: Sequence[Observation]) -> Mapping[str, float] | None:
    values = _finite([o.inference_ms for o in observations])
    if not values:
        return None
    ordered = sorted(values)
    return {"mean_ms": statistics.fmean(ordered),
            "p50_ms": ordered[len(ordered) // 2],
            "p95_ms": ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))],
            "max_ms": ordered[-1], "n": float(len(ordered))}


def failure_rate(observations: Sequence[Observation]) -> float:
    """Fraction that raised rather than forecasting or abstaining.

    Distinct from abstention: an abstention is a decision, a failure is a bug.
    A system that conflated them would report its crashes as caution.
    """
    return (sum(1 for o in observations if o.failed) / len(observations)
            if observations else 0.0)


def decision_value(observations: Sequence[Observation], *,
                   costs: CostModel | None = None) -> Mapping[str, float]:
    """Did acting on this forecast make money after costs?

    The headline. A forecast whose predicted move is smaller than the round
    trip produces no trade, so a model that is accurate about moves too small
    to monetise earns exactly nothing here — which is the correct treatment.
    """
    model = costs if costs is not None else CostModel()
    answered = [o for o in observations if not o.abstained and not o.failed]
    gross, net, trades = [], [], 0
    for o in answered:
        if abs(o.predicted) > model.round_trip:
            direction = 1.0 if o.predicted > 0 else -1.0
            gross.append(direction * o.actual)
            net.append(direction * o.actual - model.round_trip)
            trades += 1
        else:
            gross.append(0.0)
            net.append(0.0)
    return {
        "gross_return": statistics.fmean(gross) if gross else 0.0,
        "net_return": statistics.fmean(net) if net else 0.0,
        "trades": float(trades),
        "trade_rate": trades / len(answered) if answered else 0.0,
        "n_scored": float(len(answered)),
        "round_trip_bps": model.round_trip_bps,
    }


def metrics_for(observations: Sequence[Observation], *,
                costs: CostModel | None = None,
                drawdown_threshold: float = 0.02) -> dict:
    """All fourteen, on one set of observations.

    **Accuracy metrics are computed on the answered observations only.** An
    abstention is not a forecast, and scoring one as though its absent
    prediction were a prediction of zero would credit the model with
    persistence's accuracy on every window it declined — which is exactly
    backwards, since declining is supposed to cost it coverage rather than earn
    it a free correct answer.

    So `n` is the total offered, `n_scored` is what the accuracy metrics were
    computed on, and a model that declined everything reports `None` for every
    accuracy metric. "The model produced no forecasts" is the honest reading and
    it is what the table shows.
    """
    answered = [o for o in observations if not o.abstained and not o.failed]
    return {
        "n": len(observations),
        "n_scored": len(answered),
        "mae": mae(answered),
        "rmse": rmse(answered),
        "mape": mape(answered),
        "directional_accuracy": directional_accuracy(answered),
        "brier_score": brier_score(answered),
        "log_loss": log_loss(answered),
        "quantile_loss": quantile_loss(answered),
        "calibration_error": calibration_error(answered),
        "volatility_error": volatility_error(answered),
        "drawdown_quality": drawdown_quality(answered,
                                             threshold=drawdown_threshold),
        # Abstention, latency and failure are properties of every offered
        # window, so they read the whole set.
        "abstention_quality": dict(abstention_quality(observations)),
        "latency": latency(observations),
        "failure_rate": failure_rate(observations),
        "decision_value": dict(decision_value(observations, costs=costs)),
    }


# ===========================================================================
# strata
# ===========================================================================

def _quantile_bucket(value: float | None, edges: Sequence[float],
                     labels: Sequence[str]) -> str:
    if value is None:
        return "unknown"
    for edge, label in zip(edges, labels):
        if value <= edge:
            return label
    return labels[-1]


def cut_strata(observations: Sequence[Observation]) -> dict[str, dict[str, list]]:
    """The eight cuts. Each returns `{stratum_value: [observations]}`.

    Volatility and liquidity are cut on *terciles of this evaluation set*
    rather than on absolute thresholds, so the cut adapts to the instrument
    instead of encoding a guess about what "high volatility" means.
    """
    rows = list(observations)
    if not rows:
        return {}

    def by(key: Callable[[Observation], str]) -> dict[str, list]:
        out: dict[str, list] = {}
        for row in rows:
            out.setdefault(key(row), []).append(row)
        return out

    def terciles(values: Sequence[float]) -> tuple[float, float]:
        clean = sorted(_finite(values))
        if len(clean) < 3:
            return (float("inf"), float("inf"))
        return (clean[len(clean) // 3], clean[2 * len(clean) // 3])

    vol_low, vol_high = terciles([o.window_volatility for o in rows])
    volume_low, volume_high = terciles([o.window_volume for o in rows])
    labels = ("low", "medium", "high")

    return {
        "instrument": by(lambda o: o.instrument_key),
        "timeframe": by(lambda o: o.timeframe),
        "horizon": by(lambda o: str(o.horizon)),
        "regime": by(lambda o: o.regime or "unknown"),
        "volatility_environment": by(
            lambda o: _quantile_bucket(o.window_volatility,
                                       (vol_low, vol_high), labels)),
        "liquidity_environment": by(
            lambda o: _quantile_bucket(o.window_volume,
                                       (volume_low, volume_high), labels)),
        "calendar_period": by(lambda o: o.as_of.strftime("%Y-%m")),
        "seen_instrument": by(lambda o: "seen" if o.seen_instrument else "unseen"),
    }


@dataclass(frozen=True)
class Stratum:
    """One slice, its metrics, and whether it has enough data to be a result."""

    dimension: str
    value: str
    n: int
    metrics: Mapping[str, Any]
    note: str = ""

    @property
    def sufficient(self) -> bool:
        return self.n >= MIN_STRATUM_SIZE

    def to_dict(self) -> dict:
        return {"dimension": self.dimension, "value": self.value, "n": self.n,
                "sufficient": self.sufficient,
                "metrics": jsonable(dict(self.metrics)), "note": self.note}


# ===========================================================================
# the comparison arms
# ===========================================================================

@dataclass(frozen=True)
class ComparisonArm:
    """One opponent, its observations, and why it might be absent."""

    name: str
    kind: str                      # persistence | statistical | neural | champion | external
    observations: tuple[Observation, ...] = ()
    available: bool = True
    unavailable_reason: str = ""

    def __post_init__(self):
        object.__setattr__(self, "observations", tuple(self.observations))
        if not self.available and not self.unavailable_reason:
            raise ConfigurationError(
                "an unavailable arm must say why; omitting the reason makes an "
                "absent comparison look like one that was not wanted",
                arm=self.name)

    def to_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind,
                "n": len(self.observations), "available": self.available,
                "unavailable_reason": self.unavailable_reason}


def unavailable_arm(name: str, reason: str, *,
                    kind: str = "external") -> ComparisonArm:
    """An opponent that could not be run, named rather than omitted.

    An evaluation that quietly dropped its hardest opponent would read as
    complete. This is how a caller declares one, and `StratifiedReport.
    missing_arms` carries it into the report and the table.

    The *identity* of an external opponent is the caller's to supply, not this
    module's to know. A native evaluation module that hardcoded a third-party
    model's name would have that model in its source, which is precisely what
    `tests/test_trading_independence.py` exists to prevent — so the adapter
    that owns a given external model owns its arm too.
    """
    if not reason.strip():
        raise ConfigurationError(
            "an unavailable arm must say why", arm=name)
    return ComparisonArm(name=name, kind=kind, available=False,
                         unavailable_reason=reason)


@dataclass(frozen=True)
class ArmComparison:
    """The model against one opponent, on the windows both answered."""

    opponent: str
    common: int
    mae_gain: float
    quantile_loss_gain: float
    net_return_gain: float
    significant: bool
    p_value: float

    @property
    def better(self) -> bool:
        """Significantly better on the proper scoring rule *and* not worse on
        decisions. Two conditions, because a model that scores better and loses
        money has not improved anything anyone cares about."""
        return (self.quantile_loss_gain > 0 and self.significant
                and self.net_return_gain >= 0)

    def to_dict(self) -> dict:
        return {"opponent": self.opponent, "common": self.common,
                "mae_gain": self.mae_gain,
                "quantile_loss_gain": self.quantile_loss_gain,
                "net_return_gain": self.net_return_gain,
                "significant": self.significant, "p_value": self.p_value,
                "better": self.better}


def compare_arms(model: Sequence[Observation], opponent: ComparisonArm, *,
                 costs: CostModel | None = None,
                 seed: int = 0) -> ArmComparison | None:
    """Paired comparison on the windows both arms answered.

    Keyed on `(instrument, as_of, horizon)` and intersected, never zipped: two
    arms with different abstention behaviour produce different window sets, and
    zipping them compares one arm's window against the other's *different*
    window.
    """
    if not opponent.available or not opponent.observations:
        return None
    key = lambda o: (o.instrument_key, o.as_of, o.horizon)
    theirs = {key(o): o for o in opponent.observations if not o.failed}
    pairs = [(o, theirs[key(o)]) for o in model
             if not o.failed and key(o) in theirs]
    if len(pairs) < 2:
        return None

    model_cost = costs if costs is not None else CostModel()

    def net(o: Observation) -> float:
        if o.abstained or abs(o.predicted) <= model_cost.round_trip:
            return 0.0
        return (1.0 if o.predicted > 0 else -1.0) * o.actual - model_cost.round_trip

    def pinball(o: Observation) -> float:
        value = quantile_loss([o])
        return value if value is not None else o.absolute_error

    mae_gains = [b.absolute_error - a.absolute_error for a, b in pairs]
    loss_gains = [pinball(b) - pinball(a) for a, b in pairs]
    return_gains = [net(a) - net(b) for a, b in pairs]
    test = paired_bootstrap(loss_gains, name="quantile_loss_gain", seed=seed)

    return ArmComparison(
        opponent=opponent.name, common=len(pairs),
        mae_gain=statistics.fmean(mae_gains),
        quantile_loss_gain=statistics.fmean(loss_gains),
        net_return_gain=statistics.fmean(return_gains),
        significant=test.significant, p_value=test.p_value)


# ===========================================================================
# the report
# ===========================================================================

@dataclass(frozen=True)
class StratifiedReport:
    """Overall metrics, every stratum, every comparison, and what is missing."""

    model_name: str
    overall: Mapping[str, Any]
    strata: tuple[Stratum, ...]
    comparisons: tuple[ArmComparison, ...] = ()
    missing_arms: tuple[Mapping[str, Any], ...] = ()
    costs: Mapping[str, Any] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "strata", tuple(self.strata))
        object.__setattr__(self, "comparisons", tuple(self.comparisons))
        object.__setattr__(self, "missing_arms",
                           tuple(dict(a) for a in self.missing_arms))
        object.__setattr__(self, "notes", tuple(self.notes))

    def dimension(self, name: str) -> tuple[Stratum, ...]:
        return tuple(s for s in self.strata if s.dimension == name)

    @property
    def weak_strata(self) -> tuple[Stratum, ...]:
        """Sufficient strata where decisions lost money after costs.

        The list a reader should look at first. An overall net return can be
        positive while a whole regime is negative, and it is the regime that
        will be live next month.
        """
        out = []
        for stratum in self.strata:
            if not stratum.sufficient:
                continue
            value = stratum.metrics.get("decision_value") or {}
            if value.get("net_return", 0.0) < 0:
                out.append(stratum)
        return tuple(out)

    @property
    def verdict(self) -> str:
        """`better` / `indistinguishable` / `worse` / `insufficient`.

        Reads decisions after costs before it reads price error, and requires
        a significant improvement on the proper scoring rule. An
        "indistinguishable" verdict is a real result and is published as one.
        """
        if int(self.overall.get("n", 0)) < MIN_STRATUM_SIZE:
            return "insufficient"
        if not self.comparisons:
            return "insufficient"
        if any(c.better for c in self.comparisons):
            return "better"
        if all(c.quantile_loss_gain < 0 and c.significant
               for c in self.comparisons):
            return "worse"
        return "indistinguishable"

    def to_dict(self) -> dict:
        return {"model": self.model_name,
                "verdict": self.verdict,
                "overall": jsonable(dict(self.overall)),
                "strata": [s.to_dict() for s in self.strata],
                "comparisons": [c.to_dict() for c in self.comparisons],
                "missing_arms": [dict(a) for a in self.missing_arms],
                "weak_strata": [f"{s.dimension}={s.value}"
                                for s in self.weak_strata],
                "costs": dict(self.costs), "notes": list(self.notes)}

    def table(self) -> str:
        """Fixed-width, for a report. Insufficient strata are marked, not hidden."""
        header = (f"{'stratum':<34}{'n':>6}{'ans':>6}{'mae':>10}{'dir':>7}"
                  f"{'qloss':>10}{'cover':>8}{'net':>11}{'abst':>7}")
        lines = [f"model: {self.model_name}   verdict: {self.verdict}",
                 header, "-" * len(header)]

        def row(label: str, n: int, metrics: Mapping[str, Any],
                marker: str = "") -> str:
            def fmt(value, width, places):
                if value is None:
                    return f"{'-':>{width}}"
                return f"{value:>{width}.{places}f}"
            decision = metrics.get("decision_value") or {}
            abstention = metrics.get("abstention_quality") or {}
            return (f"{label + marker:<34}{n:>6}"
                    f"{int(metrics.get('n_scored', 0)):>6}"
                    f"{fmt(metrics.get('mae'), 10, 6)}"
                    f"{fmt(metrics.get('directional_accuracy'), 7, 3)}"
                    f"{fmt(metrics.get('quantile_loss'), 10, 6)}"
                    f"{fmt(metrics.get('calibration_error'), 8, 1)}"
                    f"{fmt(decision.get('net_return'), 11, 6)}"
                    f"{fmt(abstention.get('rate'), 7, 2)}")

        lines.append(row("OVERALL", int(self.overall.get("n", 0)), self.overall))
        current = ""
        for stratum in self.strata:
            if stratum.dimension != current:
                current = stratum.dimension
                lines.append(f"[{current}]")
            lines.append(row(f"  {stratum.value}", stratum.n, stratum.metrics,
                             "" if stratum.sufficient else "  (thin)"))

        if self.comparisons:
            lines.append("")
            for comparison in self.comparisons:
                verdict = ("better" if comparison.better else
                           "worse" if comparison.quantile_loss_gain < 0
                           and comparison.significant else "same")
                lines.append(
                    f"vs {comparison.opponent:<22} n={comparison.common:<6} "
                    f"qloss_gain={comparison.quantile_loss_gain:+.6f} "
                    f"net_gain={comparison.net_return_gain:+.6f} "
                    f"p={comparison.p_value:.3f}  {verdict}")
        for arm in self.missing_arms:
            lines.append(f"NOT RUN  {arm.get('name')}: {arm.get('unavailable_reason')}")
        for note in self.notes:
            lines.append(f"NOTE  {note}")
        return "\n".join(lines)


def evaluate(observations: Sequence[Observation], *, model_name: str,
             arms: Sequence[ComparisonArm] = (),
             costs: CostModel | None = None,
             drawdown_threshold: float = 0.02,
             seed: int = 0) -> StratifiedReport:
    """The whole evaluation: overall, stratified, and against every arm."""
    rows = list(observations)
    if not rows:
        raise InsufficientDataError("no observations to evaluate")
    model = costs if costs is not None else CostModel()

    overall = metrics_for(rows, costs=model, drawdown_threshold=drawdown_threshold)
    strata: list[Stratum] = []
    for dimension, buckets in cut_strata(rows).items():
        note = LIQUIDITY_PROXY_NOTE if dimension == "liquidity_environment" else ""
        for value, subset in sorted(buckets.items()):
            strata.append(Stratum(
                dimension=dimension, value=value, n=len(subset),
                metrics=metrics_for(subset, costs=model,
                                    drawdown_threshold=drawdown_threshold),
                note=note))

    comparisons: list[ArmComparison] = []
    missing: list[Mapping[str, Any]] = []
    for arm in arms:
        if not arm.available:
            missing.append(arm.to_dict())
            continue
        result = compare_arms(rows, arm, costs=model, seed=seed)
        if result is not None:
            comparisons.append(result)

    notes = [LIQUIDITY_PROXY_NOTE]
    unseen = [o for o in rows if not o.seen_instrument]
    if not unseen:
        notes.append(
            "every evaluated instrument was in the training set: the "
            "seen-versus-unseen cut has one side, so nothing here says how "
            "this model generalises to a new instrument")
    return StratifiedReport(model_name=model_name, overall=overall,
                            strata=tuple(strata), comparisons=tuple(comparisons),
                            missing_arms=tuple(missing),
                            costs=model.to_dict(), notes=tuple(notes))


def observations_from_forecasts(forecasts: Sequence[Any],
                                windows: Sequence[Any], *,
                                seen_instruments: Sequence[str] = (),
                                drawdown_threshold: float = 0.02
                                ) -> list[Observation]:
    """Pair `NativeForecast`s with the windows they were made from.

    An abstaining forecast still produces an `Observation` — with `abstained`
    set and the *actual* recorded — because that is the only way
    `abstention_quality` can ask whether declining avoided a bad forecast.
    """
    if len(forecasts) != len(windows):
        raise ConfigurationError("forecasts and windows do not correspond",
                                 forecasts=len(forecasts), windows=len(windows))
    seen = set(seen_instruments)
    out: list[Observation] = []
    for forecast, window in zip(forecasts, windows):
        actual = window.target_log_returns()[-1]
        median = 0.0
        quantiles: dict[str, float] = {}
        if forecast.quantiles:
            quantiles = {k: v[-1] for k, v in forecast.quantiles.items()}
            median = (forecast.expected_returns[-1]
                      if forecast.expected_returns else 0.0)
        volumes = [b.volume for b in window.inputs]
        # Window volatility and volume are properties of the *input*, so they
        # are computed from the window rather than read off the forecast. An
        # abstaining forecast reports neither, and taking them from it would
        # collapse the volatility and liquidity strata to "unknown" precisely
        # when the model was declining — which is when a reader most wants to
        # know what kind of window it declined on.
        closes = [b.close for b in window.inputs]
        steps = [math.log(closes[i] / closes[i - 1])
                 for i in range(1, len(closes))]
        window_volatility = statistics.pstdev(steps) if len(steps) > 1 else None
        out.append(Observation(
            instrument_key=forecast.instrument_key,
            timeframe=forecast.timeframes[0], horizon=forecast.horizon,
            as_of=window.as_of, predicted=median, actual=actual,
            quantiles=quantiles,
            direction_probability=(forecast.direction_probabilities.get("up")
                                   if forecast.direction_probabilities else None),
            predicted_volatility=forecast.predicted_volatility,
            actual_volatility=target_volatility(window),
            drawdown_probability=forecast.drawdown_probability,
            actual_drawdown=target_drawdown_magnitude(window),
            regime=forecast.most_likely_regime,
            window_volatility=window_volatility,
            window_volume=statistics.fmean(volumes) if volumes else None,
            abstained=forecast.abstained,
            abstention_reason=(forecast.abstention_reasons[0]
                               if forecast.abstention_reasons else ""),
            seen_instrument=(forecast.instrument_key in seen if seen else True),
            inference_ms=forecast.inference_ms))
    return out


__all__ = ["MIN_STRATUM_SIZE", "LIQUIDITY_PROXY_NOTE", "Observation", "mae",
           "rmse", "mape", "directional_accuracy", "brier_score", "log_loss",
           "quantile_loss", "calibration_error", "volatility_error",
           "drawdown_quality", "abstention_quality", "latency", "failure_rate",
           "decision_value", "metrics_for", "cut_strata", "Stratum",
           "ComparisonArm", "unavailable_arm", "ArmComparison", "compare_arms",
           "StratifiedReport", "evaluate", "observations_from_forecasts"]

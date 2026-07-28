"""Honest evaluation of forecasts and of the strategies built on them.

This module exists because the upstream Kronos repository ships **no evaluation
code at all** (docs/KRONOS_TEARDOWN.md §16, weakness 6), which is why its
efficacy is contested rather than settled: the only public measurements are
third-party issues #354 and #355, both reporting that Kronos-mini *underperforms
a persistence baseline* — 13–20% worse MAPE over 1,800 rolling AAPL forecasts
with 48.5–54.3% directional accuracy, and 45–60% direction accuracy on 30-minute
US equities. Neither has a maintainer response. Olympus does not resolve that
argument by assertion; it resolves it by measurement, here.

Three deliberate design choices
-------------------------------
**Abstentions are counted, never dropped silently.** A forecaster that abstains
on hard days and is graded only on easy ones looks excellent. `ForecastMetrics`
carries `abstentions` next to every accuracy number, so the denominator is
always visible.

**Point accuracy is not the interesting number.** MAE and RMSE reward a model
that predicts "tomorrow ≈ today", which is exactly what a persistence baseline
does for free. Directional accuracy, hit rate, quantile calibration and CRPS are
what distinguish a forecast that could support a position from one that cannot,
so all of them are computed and none is optional.

**A difference is not a result.** `compare_to_baseline` pairs samples and runs a
seeded paired bootstrap *and* an exact sign test. With 30 samples, a 5%
improvement in MAE is noise; without a significance check, that noise is
publishable. Both tests are stdlib and seeded, so a reported p-value is
reproducible byte for byte.

The verdict rule is code
------------------------
`kronos_is_valuable(comparison)` returns True only on a measurable
**out-of-sample** improvement **net of costs** over the *same strategy without
Kronos*, with the significance check passing and both arms actually trading. It
defaults to False, it returns False on any error, and it is the only place in
Olympus where "is Kronos worth it?" is answered. Marketing does not get a vote.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from .contracts import ForecastResult, jsonable
from .errors import ConfigurationError

#: Metrics where a smaller number is better. Used to give `improvements` a
#: consistent sign convention: positive always means "the model is better".
LOWER_IS_BETTER = frozenset({"mae", "rmse", "mape", "smape", "crps",
                             "calibration_error", "pinball"})
#: Metrics where a larger number is better.
HIGHER_IS_BETTER = frozenset({"directional_accuracy", "hit_rate", "coverage"})


# ---------------------------------------------------------------------------
# primitive metrics
#
# Free functions over plain sequences, so every one of them can be checked by
# hand against a fixture. The aggregate classes below are thin wrappers around
# these; if an aggregate ever disagrees with the arithmetic here, the arithmetic
# here is right.
# ---------------------------------------------------------------------------

def _pairs(predicted: Sequence[float], actual: Sequence[float],
           *, name: str = "series") -> list[tuple[float, float]]:
    """Aligned, finite (predicted, actual) pairs.

    Length mismatch raises rather than truncating: silently comparing the first
    N of a longer forecast against a shorter realisation is how an evaluation
    ends up measuring a different horizon than it claims to.
    """
    if len(predicted) != len(actual):
        raise ConfigurationError(
            f"{name}: predicted and actual must be the same length",
            predicted=len(predicted), actual=len(actual))
    out = []
    for p, a in zip(predicted, actual):
        p, a = float(p), float(a)
        if math.isfinite(p) and math.isfinite(a):
            out.append((p, a))
    return out


def mae(predicted: Sequence[float], actual: Sequence[float]) -> float | None:
    """Mean absolute error. None when there is nothing comparable."""
    pairs = _pairs(predicted, actual, name="mae")
    if not pairs:
        return None
    return math.fsum(abs(p - a) for p, a in pairs) / len(pairs)


def rmse(predicted: Sequence[float], actual: Sequence[float]) -> float | None:
    """Root mean squared error — penalises the large misses MAE forgives."""
    pairs = _pairs(predicted, actual, name="rmse")
    if not pairs:
        return None
    return math.sqrt(math.fsum((p - a) ** 2 for p, a in pairs) / len(pairs))


def mape(predicted: Sequence[float], actual: Sequence[float]) -> float | None:
    """Mean absolute percentage error, as a fraction (0.05 == 5%).

    Points with a zero actual are skipped, not treated as infinite error — the
    quotient is undefined there, and `inf` would poison the whole average.
    Returns None when every point had to be skipped.
    """
    pairs = [(p, a) for p, a in _pairs(predicted, actual, name="mape") if a != 0]
    if not pairs:
        return None
    return math.fsum(abs((p - a) / a) for p, a in pairs) / len(pairs)


def smape(predicted: Sequence[float], actual: Sequence[float]) -> float | None:
    """Symmetric MAPE, as a fraction, bounded in [0, 2].

    Symmetric because MAPE punishes over-prediction and under-prediction
    unequally: predicting 200 when the truth is 100 scores 1.0, predicting 0
    when the truth is 100 also scores 1.0, but predicting 100 when the truth is
    200 scores only 0.5. A model can therefore game MAPE by biasing low.
    """
    pairs = _pairs(predicted, actual, name="smape")
    total, n = 0.0, 0
    for p, a in pairs:
        denom = (abs(p) + abs(a)) / 2.0
        if denom == 0:
            continue
        total += abs(p - a) / denom
        n += 1
    return (total / n) if n else None


def directional_accuracy(predicted_changes: Sequence[float],
                         actual_changes: Sequence[float]) -> float | None:
    """Fraction of steps whose predicted *direction* matched.

    A flat actual (exactly zero change) counts as a miss unless the prediction
    was also flat. Rounding an unchanged market into a "correct call" is the
    single easiest way to manufacture a >50% number.
    """
    pairs = _pairs(predicted_changes, actual_changes, name="directional_accuracy")
    if not pairs:
        return None
    hits = sum(1 for p, a in pairs
               if (p > 0 and a > 0) or (p < 0 and a < 0) or (p == 0 and a == 0))
    return hits / len(pairs)


def pinball_loss(predicted: Sequence[float], actual: Sequence[float],
                 tau: float) -> float | None:
    """Quantile (pinball) loss at level `tau`.

    The proper scoring rule for a quantile forecast: it is minimised only by the
    true tau-quantile, so it catches a model whose "p95" band is really a p70
    band — which a coverage count alone would report as merely miscalibrated
    rather than as a worse forecast.
    """
    if not 0.0 < tau < 1.0:
        raise ConfigurationError("tau must be within (0, 1)", got=tau)
    pairs = _pairs(predicted, actual, name="pinball_loss")
    if not pairs:
        return None
    total = 0.0
    for q, a in pairs:
        diff = a - q
        total += tau * diff if diff >= 0 else (tau - 1.0) * diff
    return total / len(pairs)


def crps_ensemble(samples: Sequence[float], observation: float) -> float | None:
    """Continuous Ranked Probability Score of an ensemble, empirical form.

    CRPS = mean|X - y| - 0.5 . mean|X - X'|, the standard estimator over a
    finite sample. It scores the *whole predictive distribution*, which is the
    only fair way to grade a sampling forecaster: a model that hedges by
    widening its spread pays for it in the second term, and one that is
    confidently wrong pays in the first. Reduces to absolute error for a single
    sample, which is the right degenerate behaviour.
    """
    xs = [float(x) for x in samples if math.isfinite(float(x))]
    if not xs or not math.isfinite(float(observation)):
        return None
    y = float(observation)
    n = len(xs)
    first = math.fsum(abs(x - y) for x in xs) / n
    if n == 1:
        return first
    spread = math.fsum(abs(a - b) for a in xs for b in xs) / (n * n)
    return first - 0.5 * spread


def coverage(lower: Sequence[float], upper: Sequence[float],
             actual: Sequence[float]) -> float | None:
    """Fraction of observations that fell inside [lower, upper]."""
    if not (len(lower) == len(upper) == len(actual)):
        raise ConfigurationError("coverage inputs must be the same length",
                                 lower=len(lower), upper=len(upper),
                                 actual=len(actual))
    n, inside = 0, 0
    for lo, hi, a in zip(lower, upper, actual):
        lo, hi, a = float(lo), float(hi), float(a)
        if not (math.isfinite(lo) and math.isfinite(hi) and math.isfinite(a)):
            continue
        n += 1
        if lo <= a <= hi:
            inside += 1
    return (inside / n) if n else None


def quantile_level(label: str) -> float | None:
    """Parse a quantile label ("p05", "p95", "q0.1", "0.5") into a level.

    Returns None for a label whose level cannot be determined, because guessing
    it would silently score a band against the wrong target.
    """
    text = str(label).strip().lower()
    if not text:
        return None
    if text.startswith(("p", "q")):
        text = text[1:]
    try:
        value = float(text)
    except ValueError:
        return None
    if value > 1.0:                     # "p05" / "p95" style: percent
        value /= 100.0
    return value if 0.0 < value < 1.0 else None


# ---------------------------------------------------------------------------
# per-forecast samples and aggregate metrics
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ForecastSample:
    """One graded forecast. Kept individually so comparisons can be *paired*.

    An unpaired comparison of two models evaluated on different days measures
    the days, not the models; retaining per-forecast losses is what makes the
    paired bootstrap and the sign test possible at all.
    """
    instrument_key: str
    created_at: datetime | None
    horizon: int
    n_steps: int
    mae: float | None = None
    rmse: float | None = None
    mape: float | None = None
    smape: float | None = None
    #: Per-step directional accuracy within this one forecast.
    directional_accuracy: float | None = None
    #: Whole-horizon call: did the sign of the predicted return match?
    direction_correct: bool | None = None
    predicted_return: float | None = None
    actual_return: float | None = None
    crps: float | None = None
    pinball: Mapping[str, float] = field(default_factory=dict)
    #: {label: was the realised value below this predicted quantile}
    below_quantile: Mapping[str, bool] = field(default_factory=dict)
    interval_covered: Mapping[str, bool] = field(default_factory=dict)
    model_version: str = ""

    def to_dict(self) -> dict:
        return {"instrument_key": self.instrument_key,
                "created_at": jsonable(self.created_at),
                "horizon": self.horizon, "n_steps": self.n_steps,
                "mae": self.mae, "rmse": self.rmse, "mape": self.mape,
                "smape": self.smape,
                "directional_accuracy": self.directional_accuracy,
                "direction_correct": self.direction_correct,
                "predicted_return": self.predicted_return,
                "actual_return": self.actual_return, "crps": self.crps,
                "pinball": dict(self.pinball),
                "below_quantile": dict(self.below_quantile),
                "interval_covered": dict(self.interval_covered),
                "model_version": self.model_version}


@dataclass(frozen=True)
class ForecastMetrics:
    """Aggregate accuracy over a set of graded forecasts.

    `None` means undefined (no usable samples), never zero — a metric that
    reports 0.0 when it could not be computed makes an unevaluated model look
    perfect.
    """
    n: int = 0
    abstentions: int = 0
    mae: float | None = None
    rmse: float | None = None
    mape: float | None = None
    smape: float | None = None
    directional_accuracy: float | None = None
    hit_rate: float | None = None
    crps: float | None = None
    pinball: Mapping[str, float] = field(default_factory=dict)
    #: {label: observed fraction of realisations below that quantile}
    calibration: Mapping[str, float] = field(default_factory=dict)
    #: {label: the level that fraction *should* have been}
    expected_calibration: Mapping[str, float] = field(default_factory=dict)
    #: {"p05-p95": observed coverage of the central interval}
    interval_coverage: Mapping[str, float] = field(default_factory=dict)
    expected_interval_coverage: Mapping[str, float] = field(default_factory=dict)
    calibration_error: float | None = None
    label: str = ""
    warnings: tuple[str, ...] = ()

    @property
    def abstention_rate(self) -> float | None:
        total = self.n + self.abstentions
        return (self.abstentions / total) if total else None

    def get(self, name: str) -> float | None:
        return getattr(self, name, None)

    def to_dict(self) -> dict:
        return {"n": self.n, "abstentions": self.abstentions,
                "abstention_rate": self.abstention_rate,
                "mae": self.mae, "rmse": self.rmse, "mape": self.mape,
                "smape": self.smape,
                "directional_accuracy": self.directional_accuracy,
                "hit_rate": self.hit_rate, "crps": self.crps,
                "pinball": dict(self.pinball),
                "calibration": dict(self.calibration),
                "expected_calibration": dict(self.expected_calibration),
                "interval_coverage": dict(self.interval_coverage),
                "expected_interval_coverage": dict(self.expected_interval_coverage),
                "calibration_error": self.calibration_error,
                "label": self.label, "warnings": list(self.warnings)}


def _mean(values: Iterable[float | None]) -> float | None:
    usable = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    return (math.fsum(usable) / len(usable)) if usable else None


class ForecastEvaluator:
    """Grades `ForecastResult`s against what actually happened.

    Stateful only in the sense that it accumulates samples; the arithmetic is
    the free functions above. Deliberately takes the realised series from the
    caller rather than fetching it, so an evaluation cannot accidentally grade a
    forecast against data the forecaster had already seen.
    """

    def __init__(self, *, label: str = "",
                 interval_pairs: Sequence[tuple[str, str]] = (("p05", "p95"),
                                                              ("p10", "p90"))):
        self.label = label
        self.interval_pairs = tuple(interval_pairs)
        self._samples: list[ForecastSample] = []
        self._abstentions = 0
        self._warnings: list[str] = []

    @property
    def samples(self) -> tuple[ForecastSample, ...]:
        return tuple(self._samples)

    @property
    def abstentions(self) -> int:
        return self._abstentions

    def add(self, forecast: ForecastResult, realised: Sequence[float], *,
            last_close: float | None = None) -> ForecastSample | None:
        """Grade one forecast. Returns None for an abstention (which is counted).

        `last_close` is the final close of the *input* window — the reference
        the forecast's expected return is measured from. When omitted it is
        taken from the first realised value's predecessor if available;
        otherwise return-based metrics for this sample are left undefined rather
        than computed against a guessed baseline.
        """
        if forecast.abstained:
            self._abstentions += 1
            return None
        realised = [float(v) for v in realised]
        predicted = list(forecast.mean_close)
        if not predicted or not realised:
            self._abstentions += 1
            self._warnings.append(
                f"forecast at {forecast.created_at.isoformat()} had no usable "
                "mean path or no realisation; counted as an abstention")
            return None
        steps = min(len(predicted), len(realised))
        if len(predicted) != len(realised):
            self._warnings.append(
                f"forecast horizon {len(predicted)} vs realisation "
                f"{len(realised)}: graded on the overlapping {steps} steps")
        predicted, realised = predicted[:steps], realised[:steps]

        # Direction is measured step-over-step from the same anchor for both
        # series, so the comparison is of *changes*, not of levels.
        anchor = last_close
        pred_changes, act_changes = [], []
        prev_p = anchor if anchor is not None else predicted[0]
        prev_a = anchor if anchor is not None else realised[0]
        start = 0 if anchor is not None else 1
        for i in range(start, steps):
            pred_changes.append(predicted[i] - prev_p)
            act_changes.append(realised[i] - prev_a)
            prev_p, prev_a = predicted[i], realised[i]

        predicted_return = actual_return = None
        direction_correct = None
        if anchor is not None and anchor > 0:
            predicted_return = predicted[-1] / anchor - 1.0
            actual_return = realised[-1] / anchor - 1.0
            direction_correct = (
                (predicted_return > 0 and actual_return > 0)
                or (predicted_return < 0 and actual_return < 0)
                or (predicted_return == 0 and actual_return == 0))

        pinball: dict[str, float] = {}
        below: dict[str, bool] = {}
        for label, values in forecast.quantiles.items():
            level = quantile_level(label)
            if level is None or len(values) < steps:
                continue
            loss = pinball_loss(list(values[:steps]), realised, level)
            if loss is not None:
                pinball[label] = loss
            # Calibration is counted on the terminal step: consecutive steps of
            # one path are strongly dependent, so counting every step would
            # inflate the sample size and make the calibration test look far
            # more precise than it is.
            below[label] = realised[-1] <= float(values[steps - 1])

        intervals: dict[str, bool] = {}
        for low_label, high_label in self.interval_pairs:
            low = forecast.quantiles.get(low_label)
            high = forecast.quantiles.get(high_label)
            if not low or not high or len(low) < steps or len(high) < steps:
                continue
            intervals[f"{low_label}-{high_label}"] = (
                float(low[steps - 1]) <= realised[-1] <= float(high[steps - 1]))

        terminal_samples = [p.closes[steps - 1] for p in forecast.paths
                            if len(p.closes) >= steps]
        sample_crps = (crps_ensemble(terminal_samples, realised[-1])
                       if terminal_samples else None)

        sample = ForecastSample(
            instrument_key=forecast.instrument_key,
            created_at=forecast.created_at, horizon=forecast.horizon,
            n_steps=steps, mae=mae(predicted, realised),
            rmse=rmse(predicted, realised), mape=mape(predicted, realised),
            smape=smape(predicted, realised),
            directional_accuracy=(directional_accuracy(pred_changes, act_changes)
                                  if pred_changes else None),
            direction_correct=direction_correct,
            predicted_return=predicted_return, actual_return=actual_return,
            crps=sample_crps, pinball=pinball, below_quantile=below,
            interval_covered=intervals, model_version=forecast.model_version)
        self._samples.append(sample)
        return sample

    def add_many(self, pairs: Iterable[tuple[ForecastResult, Sequence[float]]]
                 ) -> None:
        for forecast, realised in pairs:
            self.add(forecast, realised)

    def metrics(self) -> ForecastMetrics:
        """Aggregate everything accumulated so far."""
        samples = self._samples
        calls = [s.direction_correct for s in samples
                 if s.direction_correct is not None]
        hit_rate = (sum(1 for c in calls if c) / len(calls)) if calls else None

        pinball_labels = sorted({k for s in samples for k in s.pinball})
        pinball = {}
        for label in pinball_labels:
            value = _mean(s.pinball.get(label) for s in samples)
            if value is not None:
                pinball[label] = value

        calibration, expected = {}, {}
        for label in sorted({k for s in samples for k in s.below_quantile}):
            observations = [s.below_quantile[label] for s in samples
                            if label in s.below_quantile]
            if not observations:
                continue
            calibration[label] = sum(1 for o in observations if o) / len(observations)
            level = quantile_level(label)
            if level is not None:
                expected[label] = level

        interval_cov, interval_expected = {}, {}
        for label in sorted({k for s in samples for k in s.interval_covered}):
            observations = [s.interval_covered[label] for s in samples
                            if label in s.interval_covered]
            if not observations:
                continue
            interval_cov[label] = sum(1 for o in observations if o) / len(observations)
            low_label, _, high_label = label.partition("-")
            low, high = quantile_level(low_label), quantile_level(high_label)
            if low is not None and high is not None:
                interval_expected[label] = high - low

        errors = [abs(calibration[k] - expected[k]) for k in expected
                  if k in calibration]
        errors += [abs(interval_cov[k] - interval_expected[k])
                   for k in interval_expected if k in interval_cov]
        calibration_error = (math.fsum(errors) / len(errors)) if errors else None

        warnings = list(dict.fromkeys(self._warnings))
        if samples and len(samples) < 30:
            warnings.append(
                f"only {len(samples)} graded forecasts: too few to distinguish "
                "skill from luck, whatever the point estimates say")
        if self._abstentions and samples:
            warnings.append(
                f"{self._abstentions} abstentions are excluded from the accuracy "
                "numbers; judge them together, not separately")

        return ForecastMetrics(
            n=len(samples), abstentions=self._abstentions,
            mae=_mean(s.mae for s in samples),
            rmse=_mean(s.rmse for s in samples),
            mape=_mean(s.mape for s in samples),
            smape=_mean(s.smape for s in samples),
            directional_accuracy=_mean(s.directional_accuracy for s in samples),
            hit_rate=hit_rate, crps=_mean(s.crps for s in samples),
            pinball=pinball, calibration=calibration,
            expected_calibration=expected, interval_coverage=interval_cov,
            expected_interval_coverage=interval_expected,
            calibration_error=calibration_error, label=self.label,
            warnings=tuple(warnings))


# ---------------------------------------------------------------------------
# significance
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SignificanceResult:
    """The outcome of one statistical test, with everything needed to repeat it.

    `seed` and `iterations` travel with the result because an unseeded bootstrap
    is not reproducible, and a p-value nobody can reproduce is a rhetorical
    device rather than evidence.
    """
    name: str
    test: str
    n: int
    statistic: float
    p_value: float
    alpha: float = 0.05
    ci_low: float | None = None
    ci_high: float | None = None
    seed: int = 0
    iterations: int = 0
    detail: str = ""

    @property
    def significant(self) -> bool:
        """True only when the p-value clears alpha AND the CI excludes zero.

        Both, because a bootstrap p-value near the threshold with an interval
        straddling zero is exactly the situation a small sample manufactures.
        """
        if self.p_value > self.alpha:
            return False
        if self.ci_low is not None and self.ci_high is not None:
            if self.ci_low <= 0.0 <= self.ci_high:
                return False
        return True

    def to_dict(self) -> dict:
        return {"name": self.name, "test": self.test, "n": self.n,
                "statistic": self.statistic, "p_value": self.p_value,
                "alpha": self.alpha, "ci_low": self.ci_low,
                "ci_high": self.ci_high, "significant": self.significant,
                "seed": self.seed, "iterations": self.iterations,
                "detail": self.detail}


def paired_bootstrap(differences: Sequence[float], *, name: str = "difference",
                     seed: int = 0, iterations: int = 2000,
                     alpha: float = 0.05) -> SignificanceResult:
    """Two-sided seeded paired bootstrap on the mean of `differences`.

    Resampling the *paired* differences rather than the two series separately is
    what removes the common market movement both arms experienced: on a day when
    everything went up, both arms went up, and only their difference is
    informative.

    The p-value is the standard bootstrap two-sided proportion and is
    approximate by construction — it cannot resolve below `1 / iterations`, and
    that limit is reported rather than hidden behind a decimal expansion.
    """
    values = [float(d) for d in differences if math.isfinite(float(d))]
    n = len(values)
    if n < 2 or iterations < 1:
        return SignificanceResult(
            name=name, test="paired_bootstrap", n=n,
            statistic=(values[0] if n == 1 else 0.0), p_value=1.0, alpha=alpha,
            seed=seed, iterations=max(0, iterations),
            detail="too few paired observations to bootstrap")
    observed = math.fsum(values) / n
    rng = random.Random(seed)
    means = []
    for _ in range(iterations):
        total = 0.0
        for _ in range(n):
            total += values[rng.randrange(n)]
        means.append(total / n)
    means.sort()
    below = sum(1 for m in means if m <= 0.0)
    above = sum(1 for m in means if m >= 0.0)
    p_value = min(1.0, 2.0 * min(below, above) / iterations)
    lo_index = max(0, int(math.floor((alpha / 2.0) * iterations)) - 1)
    hi_index = min(iterations - 1, int(math.ceil((1.0 - alpha / 2.0) * iterations)) - 1)
    return SignificanceResult(
        name=name, test="paired_bootstrap", n=n, statistic=observed,
        p_value=p_value, alpha=alpha, ci_low=means[lo_index],
        ci_high=means[hi_index], seed=seed, iterations=iterations,
        detail=f"p-value resolution is 1/{iterations}")


def _binomial_tail(k: int, n: int, p: float = 0.5) -> float:
    return math.fsum(math.comb(n, i) * (p ** i) * ((1 - p) ** (n - i))
                     for i in range(k, n + 1))


def sign_test(successes: int, n: int, *, name: str = "sign",
              alpha: float = 0.05) -> SignificanceResult:
    """Exact two-sided binomial sign test against p = 0.5.

    Distribution-free and exact (no normal approximation, which misbehaves
    precisely at the small sample sizes where the question matters). Answers
    "did the model win more often than a coin?", which is a different and more
    robust question than "was the average difference positive?" — an average can
    be carried by one lucky day.
    """
    n = int(n)
    successes = int(successes)
    if n < 1:
        return SignificanceResult(name=name, test="sign_test", n=0,
                                  statistic=0.0, p_value=1.0, alpha=alpha,
                                  detail="no paired observations")
    if not 0 <= successes <= n:
        raise ConfigurationError("successes must be within [0, n]",
                                 successes=successes, n=n)
    k = max(successes, n - successes)
    p_value = min(1.0, 2.0 * _binomial_tail(k, n))
    rate = successes / n
    return SignificanceResult(
        name=name, test="sign_test", n=n, statistic=rate, p_value=p_value,
        alpha=alpha, detail=f"{successes}/{n} wins vs a 0.5 coin")


# ---------------------------------------------------------------------------
# forecast comparison
# ---------------------------------------------------------------------------

def _relative_improvement(model: float | None, baseline: float | None,
                          metric: str) -> float | None:
    """Signed relative improvement where positive always means "model better"."""
    if model is None or baseline is None or baseline == 0:
        return None
    if metric in LOWER_IS_BETTER:
        return (baseline - model) / abs(baseline)
    if metric in HIGHER_IS_BETTER:
        return (model - baseline) / abs(baseline)
    return None


@dataclass(frozen=True)
class ComparisonReport:
    """Model versus baseline, with the significance tests attached.

    The improvements dict alone is not a result and is never reported alone —
    `verdict` requires the paired tests to agree with it.
    """
    label: str
    model: ForecastMetrics
    baseline: ForecastMetrics
    n_pairs: int
    improvements: Mapping[str, float | None] = field(default_factory=dict)
    significance: Mapping[str, SignificanceResult] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    @property
    def significant_improvement(self) -> bool:
        """True only when a *loss* test says the model is better, significantly.

        Requires the error test (absolute error) to be significant AND its
        direction to favour the model. Directional-accuracy wins alone are not
        enough to claim the model forecasts better, but they are reported.
        """
        error = self.significance.get("absolute_error")
        if error is None or not error.significant:
            return False
        # `statistic` is mean(baseline_loss - model_loss): positive = model wins.
        return error.statistic > 0

    @property
    def verdict(self) -> str:
        if self.significant_improvement:
            return "model_better"
        error = self.significance.get("absolute_error")
        if error is not None and error.significant and error.statistic < 0:
            return "baseline_better"
        return "no_significant_difference"

    def to_dict(self) -> dict:
        return {"label": self.label, "n_pairs": self.n_pairs,
                "model": self.model.to_dict(),
                "baseline": self.baseline.to_dict(),
                "improvements": dict(self.improvements),
                "significance": {k: v.to_dict()
                                 for k, v in self.significance.items()},
                "verdict": self.verdict,
                "significant_improvement": self.significant_improvement,
                "warnings": list(self.warnings)}


def _as_samples(source: Any) -> list[ForecastSample]:
    if isinstance(source, ForecastEvaluator):
        return list(source.samples)
    return [s for s in source if isinstance(s, ForecastSample)]


def _as_metrics(source: Any, label: str) -> ForecastMetrics:
    if isinstance(source, ForecastEvaluator):
        return source.metrics()
    if isinstance(source, ForecastMetrics):
        return source
    evaluator = ForecastEvaluator(label=label)
    evaluator._samples = _as_samples(source)          # noqa: SLF001
    return evaluator.metrics()


def compare_to_baseline(model_results: Any, baseline_results: Any, *,
                        label: str = "", seed: int = 0, iterations: int = 2000,
                        alpha: float = 0.05) -> ComparisonReport:
    """Compare a model's graded forecasts against a baseline's, honestly.

    Accepts `ForecastEvaluator`s or sequences of `ForecastSample`. Samples are
    paired positionally after a length check: an unpaired comparison measures
    the two evaluation windows as much as it measures the two models, so a
    mismatch is refused rather than silently truncated.
    """
    model_samples = _as_samples(model_results)
    baseline_samples = _as_samples(baseline_results)
    if len(model_samples) != len(baseline_samples):
        raise ConfigurationError(
            "model and baseline must be evaluated on the same forecasts; "
            "an unpaired comparison measures the windows, not the models",
            model=len(model_samples), baseline=len(baseline_samples))

    model_metrics = _as_metrics(model_results, label or "model")
    baseline_metrics = _as_metrics(baseline_results, label or "baseline")

    improvements = {
        name: _relative_improvement(model_metrics.get(name),
                                    baseline_metrics.get(name), name)
        for name in ("mae", "rmse", "mape", "smape", "crps",
                     "directional_accuracy", "hit_rate", "calibration_error")}

    warnings: list[str] = []
    significance: dict[str, SignificanceResult] = {}
    n = len(model_samples)
    if n:
        # Loss differences, baseline minus model, so positive = model better.
        diffs = [b.mae - m.mae for m, b in zip(model_samples, baseline_samples)
                 if m.mae is not None and b.mae is not None]
        significance["absolute_error"] = paired_bootstrap(
            diffs, name="absolute_error", seed=seed, iterations=iterations,
            alpha=alpha)
        wins = sum(1 for m, b in zip(model_samples, baseline_samples)
                   if m.mae is not None and b.mae is not None and m.mae < b.mae)
        ties = sum(1 for m, b in zip(model_samples, baseline_samples)
                   if m.mae is not None and b.mae is not None and m.mae == b.mae)
        significance["absolute_error_sign"] = sign_test(
            wins, len(diffs) - ties, name="absolute_error_sign", alpha=alpha)

        paired_direction = [(m.direction_correct, b.direction_correct)
                            for m, b in zip(model_samples, baseline_samples)
                            if m.direction_correct is not None
                            and b.direction_correct is not None]
        # McNemar-style: only the forecasts where exactly one arm was right
        # carry information about which arm is better.
        discordant = [(m, b) for m, b in paired_direction if m != b]
        if discordant:
            significance["direction"] = sign_test(
                sum(1 for m, _ in discordant if m), len(discordant),
                name="direction", alpha=alpha)
    else:
        warnings.append("no paired samples: nothing was compared")

    if n and n < 30:
        warnings.append(
            f"{n} paired forecasts is a small sample; the significance tests "
            "are reported precisely so a difference this size is not read as a "
            "result")
    warnings.extend(model_metrics.warnings)

    return ComparisonReport(
        label=label, model=model_metrics, baseline=baseline_metrics,
        n_pairs=n, improvements=improvements, significance=significance,
        warnings=tuple(dict.fromkeys(warnings)))


# ---------------------------------------------------------------------------
# strategy comparison — the same strategy with and without Kronos
# ---------------------------------------------------------------------------

def _curve_returns(curve: Sequence[Any]) -> list[float]:
    """Per-step returns of an equity curve. Skips non-positive equity."""
    out = []
    for i in range(1, len(curve)):
        prev = float(curve[i - 1][1])
        cur = float(curve[i][1])
        if prev <= 0:
            continue
        out.append(cur / prev - 1.0)
    return out


def _curve_timestamps(curve: Sequence[Any]) -> list[Any]:
    return [row[0] for row in curve]


@dataclass(frozen=True)
class StrategyComparison:
    """Two backtests of the *same* strategy, with and without Kronos.

    Both arms are duck-typed `backtest.BacktestResult`s (anything with
    `.report`, `.equity_curve` and `.trades`), so this module does not import
    the backtester and the caller stays in control of the risk engine, the
    broker and the cost model.

    The comparison is only meaningful when the two arms differ *only* in their
    signal source — which is why `strategy.BaselineMomentumStrategy` shares
    `VolatilityTargetedStrategy` with `strategy.KronosMomentumStrategy` rather
    than reimplementing sizing and exits.
    """
    label: str
    kronos: Any
    baseline: Any
    #: True only when these results come from data the model never saw.
    #: Defaults to False, so an in-sample comparison can never be mistaken for
    #: evidence by a caller that forgot to say.
    out_of_sample: bool = False
    #: None means "derive it from the reports" (fees or slippage > 0).
    costs_modelled: bool | None = None
    seed: int = 0
    alpha: float = 0.05
    iterations: int = 2000
    notes: tuple[str, ...] = ()
    significance: SignificanceResult | None = field(default=None, init=False)
    comparable: bool = field(default=False, init=False)

    def __post_init__(self):
        k_curve = list(getattr(self.kronos, "equity_curve", ()) or ())
        b_curve = list(getattr(self.baseline, "equity_curve", ()) or ())
        aligned = (len(k_curve) == len(b_curve) and len(k_curve) > 1
                   and _curve_timestamps(k_curve) == _curve_timestamps(b_curve))
        object.__setattr__(self, "comparable", aligned)
        notes = list(self.notes)
        if self.costs_modelled is None:
            report = getattr(self.kronos, "report", None)
            derived = bool(report is not None
                           and (Decimal(str(getattr(report, "total_fees", 0))) > 0
                                or Decimal(str(getattr(report, "total_slippage", 0))) > 0))
            object.__setattr__(self, "costs_modelled", derived)
            if not derived:
                notes.append(
                    "neither fees nor slippage were charged in these runs; a "
                    "costless backtest flatters any strategy that trades")
        if not aligned:
            notes.append(
                "equity curves are not aligned bar-for-bar, so no paired test "
                "was run; a difference here is not measurable")
            object.__setattr__(self, "notes", tuple(dict.fromkeys(notes)))
            return
        diffs = [k - b for k, b in zip(_curve_returns(k_curve),
                                       _curve_returns(b_curve))]
        object.__setattr__(self, "significance", paired_bootstrap(
            diffs, name="net_return_per_bar", seed=self.seed,
            iterations=self.iterations, alpha=self.alpha))
        object.__setattr__(self, "notes", tuple(dict.fromkeys(notes)))

    # -- deltas ------------------------------------------------------------

    def _metric(self, arm: Any, name: str) -> float | None:
        report = getattr(arm, "report", None)
        value = getattr(report, name, None) if report is not None else None
        if value is None:
            return None
        return float(value)

    def _delta(self, name: str) -> float | None:
        model = self._metric(self.kronos, name)
        base = self._metric(self.baseline, name)
        if model is None or base is None:
            return None
        return model - base

    @property
    def net_return_delta(self) -> float | None:
        """Kronos minus baseline, net of costs. The number that matters."""
        return self._delta("total_return_net")

    @property
    def gross_return_delta(self) -> float | None:
        return self._delta("total_return_gross")

    @property
    def sharpe_delta(self) -> float | None:
        return self._delta("sharpe")

    @property
    def max_drawdown_delta(self) -> float | None:
        return self._delta("max_drawdown")

    @property
    def kronos_trades(self) -> int:
        report = getattr(self.kronos, "report", None)
        return int(getattr(report, "n_trades", 0) or 0)

    @property
    def baseline_trades(self) -> int:
        report = getattr(self.baseline, "report", None)
        return int(getattr(report, "n_trades", 0) or 0)

    def to_dict(self) -> dict:
        return {"label": self.label, "out_of_sample": self.out_of_sample,
                "costs_modelled": self.costs_modelled,
                "comparable": self.comparable,
                "net_return_delta": self.net_return_delta,
                "gross_return_delta": self.gross_return_delta,
                "sharpe_delta": self.sharpe_delta,
                "max_drawdown_delta": self.max_drawdown_delta,
                "kronos_trades": self.kronos_trades,
                "baseline_trades": self.baseline_trades,
                "significance": (self.significance.to_dict()
                                 if self.significance else None),
                "kronos_report": jsonable(getattr(self.kronos, "report", None)),
                "baseline_report": jsonable(getattr(self.baseline, "report", None)),
                "notes": list(self.notes), "seed": self.seed}


def run_strategy_comparison(*, build_engine, kronos_strategy, baseline_strategy,
                            label: str = "kronos vs baseline",
                            out_of_sample: bool = False, seed: int = 0,
                            alpha: float = 0.05, iterations: int = 2000
                            ) -> StrategyComparison:
    """Run both arms through the caller's backtest engine and compare them.

    `build_engine(strategy) -> engine_with_run()` is supplied by the caller so
    that both arms are guaranteed to use the *same* data, risk limits, cost
    model and seed — the comparison is worthless otherwise, and making the
    caller construct the engine twice would let the two configurations drift.
    """
    kronos_result = build_engine(kronos_strategy).run()
    baseline_result = build_engine(baseline_strategy).run()

    notes = []
    k_params = dict(getattr(kronos_strategy, "parameters", dict)() or {})
    b_params = dict(getattr(baseline_strategy, "parameters", dict)() or {})
    shared = set(k_params) & set(b_params)
    differing = sorted(name for name in shared
                       if name != "signal_source" and k_params[name] != b_params[name])
    if differing:
        notes.append(
            "the two arms differ in more than their signal source ("
            + ", ".join(differing) + "), so this comparison does not isolate "
            "the contribution of the forecast")
    return StrategyComparison(
        label=label, kronos=kronos_result, baseline=baseline_result,
        out_of_sample=out_of_sample, seed=seed, alpha=alpha,
        iterations=iterations, notes=tuple(notes))


# ---------------------------------------------------------------------------
# the verdict
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VerdictReport:
    """Why `kronos_is_valuable` answered the way it did.

    Every criterion is recorded, passed as well as failed, so the answer is
    auditable rather than an opinion with a boolean attached.
    """
    valuable: bool
    checks: Mapping[str, bool] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"valuable": self.valuable, "checks": dict(self.checks),
                "reasons": list(self.reasons),
                "evidence": {k: jsonable(v) for k, v in self.evidence.items()}}


#: Minimum paired observations before a strategy comparison can support a
#: claim. Not a statistical constant — a floor below which the bootstrap has too
#: little to resample for its interval to mean anything.
MIN_PAIRED_OBSERVATIONS = 30


def kronos_verdict(comparison: Any, *, min_return_improvement: float = 0.0,
                   alpha: float = 0.05,
                   min_observations: int = MIN_PAIRED_OBSERVATIONS
                   ) -> VerdictReport:
    """Evaluate every criterion for "Kronos earned its place", and report them.

    The criteria, all of which must hold:

    1. the input is a `StrategyComparison` (not a metrics dict someone assembled)
    2. the results are **out of sample**
    3. costs were modelled — a costless backtest flatters any active strategy
    4. both arms actually traded; a no-trade arm is not a comparison
    5. the equity curves are aligned, so a *paired* test is possible
    6. the **net** return improvement exceeds `min_return_improvement`
    7. risk-adjusted return did not get worse (Sharpe, when both are defined)
    8. enough paired observations to test
    9. the paired bootstrap is significant at `alpha` and favours Kronos

    Anything that goes wrong — a malformed comparison, a missing report —
    produces `valuable=False` with the reason recorded. There is no path through
    this function that answers True on an error.
    """
    checks: dict[str, bool] = {}
    reasons: list[str] = []
    evidence: dict[str, Any] = {}

    def require(name: str, passed: bool, reason: str) -> bool:
        checks[name] = bool(passed)
        if not passed:
            reasons.append(reason)
        return bool(passed)

    try:
        if not require("is_strategy_comparison",
                       isinstance(comparison, StrategyComparison),
                       "input is not a StrategyComparison"):
            return VerdictReport(False, checks, tuple(reasons), evidence)

        require("out_of_sample", bool(comparison.out_of_sample),
                "results are not marked out-of-sample; in-sample improvement is "
                "not evidence of anything")
        require("costs_modelled", bool(comparison.costs_modelled),
                "no trading costs were modelled")
        require("kronos_traded", comparison.kronos_trades > 0,
                "the Kronos arm never traded")
        require("baseline_traded", comparison.baseline_trades > 0,
                "the baseline arm never traded")
        require("comparable", bool(comparison.comparable),
                "equity curves are not aligned, so no paired test is possible")

        net_delta = comparison.net_return_delta
        evidence["net_return_delta"] = net_delta
        require("net_return_improved",
                net_delta is not None and net_delta > min_return_improvement,
                "net-of-costs return did not improve by more than "
                f"{min_return_improvement}")

        sharpe_delta = comparison.sharpe_delta
        evidence["sharpe_delta"] = sharpe_delta
        # Undefined Sharpe (a flat curve) is not treated as a failure — it is
        # not evidence either way, and the return and significance criteria
        # still have to pass.
        require("risk_adjusted_not_worse",
                sharpe_delta is None or sharpe_delta >= 0,
                "risk-adjusted return (Sharpe) got worse")

        significance = comparison.significance
        evidence["significance"] = (significance.to_dict()
                                    if significance is not None else None)
        require("enough_observations",
                significance is not None and significance.n >= min_observations,
                f"fewer than {min_observations} paired observations")
        require("significant",
                significance is not None and significance.significant
                and significance.p_value <= alpha
                and significance.statistic > 0,
                "the improvement is not statistically distinguishable from noise")
    except Exception as exc:                             # noqa: BLE001
        # Defaulting to False on error is the whole point: an evaluation that
        # crashes must not be readable as an endorsement.
        checks["evaluated"] = False
        reasons.append(f"verdict evaluation failed: {exc}")
        return VerdictReport(False, checks, tuple(reasons), evidence)

    return VerdictReport(all(checks.values()), checks, tuple(reasons), evidence)


def kronos_is_valuable(comparison: Any, *, min_return_improvement: float = 0.0,
                       alpha: float = 0.05,
                       min_observations: int = MIN_PAIRED_OBSERVATIONS) -> bool:
    """Does Kronos earn its place in this strategy? Defaults to False.

    This function — not a README, not a benchmark table, not the upstream
    repository's claims — is how Olympus decides whether a Kronos-driven
    strategy is allowed to be preferred over the identical strategy without it.
    The public evidence today is negative and unanswered
    (docs/KRONOS_TEARDOWN.md §16; upstream issues #354/#355), so the honest
    default is False and the burden of proof is on the model.

    See `kronos_verdict` for the criteria and for why a given answer came out.
    """
    return kronos_verdict(comparison,
                          min_return_improvement=min_return_improvement,
                          alpha=alpha,
                          min_observations=min_observations).valuable


__all__ = [
    "mae", "rmse", "mape", "smape", "directional_accuracy", "pinball_loss",
    "crps_ensemble", "coverage", "quantile_level",
    "ForecastSample", "ForecastMetrics", "ForecastEvaluator",
    "SignificanceResult", "paired_bootstrap", "sign_test",
    "ComparisonReport", "compare_to_baseline",
    "StrategyComparison", "run_strategy_comparison",
    "VerdictReport", "kronos_verdict", "kronos_is_valuable",
    "MIN_PAIRED_OBSERVATIONS", "LOWER_IS_BETTER", "HIGHER_IS_BETTER",
]

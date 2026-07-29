"""One harness. Same data, same splits, same costs, same metrics.

A model comparison is only as trustworthy as its most easily rigged clause, and
there are four: the data, the split, the costs and the metric. This module owns
all four so that no model can quietly get a different one.

What "same" means here, mechanically
------------------------------------
**Same data and split.** `run_benchmark` takes one `ThreeWaySplit` and hands the
identical `train` sequence to every model's `fit` and the identical `test`
sequence to every model's scoring pass. No model receives a window another did
not.

**Same costs.** `CostModel` converts a directional forecast into a net return
with the round-trip cost subtracted. It is applied to every model or to none.
Without it a forecast that predicts a two-basis-point move on a
ten-basis-point spread looks profitable, and every model looks better than it is.

**Same metrics.** `ForecastScore` is computed by one function for every model:
pinball loss on the declared quantile grid, median absolute error, interval
coverage, directional accuracy, abstention rate, and net-of-cost return. A model
cannot report the metric that flattered it because it does not choose.

Abstention is a first-class outcome
-----------------------------------
A model that declines is not scored on the window it declined. That is the right
treatment — an abstention is not a wrong answer — and it is also an obvious
place to cheat, because a model that answers only when it is confident will beat
one that always answers. So `abstention_rate` is reported beside every score,
`scored` records the denominator, and `compare_scored` intersects the two
models' window sets rather than assuming they line up.

Nothing here decides anything
-----------------------------
`run_benchmark` returns numbers. It does not promote, register, enable or
select. The champion/challenger decision lives in `champion.py` behind
`capabilities.promote()`, which still requires a human.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..errors import ConfigurationError, InsufficientDataError
from ..evaluate import coverage, paired_bootstrap, pinball_loss, quantile_level
from .baselines import (BASELINES, DEFAULT_QUANTILES, WindowModel,
                        available_baselines, build_baseline)
from .data import Window
from .quantile import Declined


@dataclass(frozen=True)
class CostModel:
    """Round-trip trading cost, in basis points of notional.

    Deliberately crude and deliberately present. A precise cost model needs a
    book and a fill history that this environment cannot reach (blockers B1 and
    B3); a *stated, applied* approximation is still the difference between a
    comparison that knows costs exist and one that does not.

    `round_trip` is the filter that does the real work: a forecast whose
    predicted move is smaller than the cost of acting on it produces no trade,
    which is what stops a model being rewarded for correctly predicting moves it
    could never have monetised.
    """

    #: Half-spread paid on entry and again on exit.
    spread_bps: float = 2.0
    #: Commission per side.
    fee_bps: float = 1.0
    #: Modelled market impact per side. Zero here, and stated rather than
    #: omitted: with no book data there is nothing to calibrate it against.
    impact_bps: float = 0.0

    def __post_init__(self):
        for name in ("spread_bps", "fee_bps", "impact_bps"):
            value = float(getattr(self, name))
            if value < 0:
                raise ConfigurationError(f"{name} must be >= 0", **{name: value})
            object.__setattr__(self, name, value)

    @property
    def round_trip_bps(self) -> float:
        return 2.0 * (self.spread_bps + self.fee_bps + self.impact_bps)

    @property
    def round_trip(self) -> float:
        """As a fraction, which is what a log return is comparable against."""
        return self.round_trip_bps / 10_000.0

    def to_dict(self) -> dict:
        return {"spread_bps": self.spread_bps, "fee_bps": self.fee_bps,
                "impact_bps": self.impact_bps,
                "round_trip_bps": self.round_trip_bps}


#: The costless model, for measuring how much of an edge the costs consume.
#: Reporting both is the honest presentation: a strategy that works gross and
#: not net has a real signal and no business.
FREE = CostModel(spread_bps=0.0, fee_bps=0.0, impact_bps=0.0)


@dataclass(frozen=True)
class ForecastScore:
    """One model's measured behaviour on one test set."""

    name: str
    #: Windows the model answered. The denominator for every metric here.
    scored: int
    #: Windows it was offered.
    offered: int
    parameters: int
    #: Mean pinball loss across the quantile grid, at the terminal step. The
    #: headline: it is the proper scoring rule for a quantile forecast and it is
    #: what the network trains on, so training objective and evaluation metric
    #: are the same function.
    pinball: float | None
    per_quantile: Mapping[str, float | None] = field(default_factory=dict)
    median_mae: float | None = None
    #: Coverage of the widest declared interval, and what it should have been.
    interval_coverage: float | None = None
    nominal_coverage: float | None = None
    directional_accuracy: float | None = None
    #: Mean net log return per offered window, after costs.
    net_return: float | None = None
    gross_return: float | None = None
    trades: int = 0
    fit_seconds: float = 0.0
    score_seconds: float = 0.0
    declines: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "per_quantile", dict(self.per_quantile))
        object.__setattr__(self, "declines", dict(self.declines))

    @property
    def abstention_rate(self) -> float:
        return 0.0 if not self.offered else 1.0 - self.scored / self.offered

    @property
    def coverage_error(self) -> float | None:
        """Signed calibration error, in coverage points.

        Signed, not absolute: a model whose 80% interval covers 95% is
        over-cautious and one that covers 60% is dangerous, and an absolute
        error would rank them the same.
        """
        if self.interval_coverage is None or self.nominal_coverage is None:
            return None
        return (self.interval_coverage - self.nominal_coverage) * 100.0

    @property
    def cost_drag(self) -> float | None:
        """How much of the gross edge the costs consumed."""
        if self.net_return is None or self.gross_return is None:
            return None
        return self.gross_return - self.net_return

    def to_dict(self) -> dict:
        return {"name": self.name, "scored": self.scored, "offered": self.offered,
                "abstention_rate": round(self.abstention_rate, 6),
                "parameters": self.parameters, "pinball": self.pinball,
                "per_quantile": dict(self.per_quantile),
                "median_mae": self.median_mae,
                "interval_coverage": self.interval_coverage,
                "nominal_coverage": self.nominal_coverage,
                "coverage_error": self.coverage_error,
                "directional_accuracy": self.directional_accuracy,
                "net_return": self.net_return, "gross_return": self.gross_return,
                "cost_drag": self.cost_drag, "trades": self.trades,
                "fit_seconds": round(self.fit_seconds, 3),
                "score_seconds": round(self.score_seconds, 3),
                "declines": dict(self.declines)}


@dataclass(frozen=True)
class ScoredWindows:
    """Per-window outcomes, kept so two models can be compared pairwise.

    Aggregate metrics cannot support a paired test — the whole point of pairing
    is that both arms saw the same window — so the harness keeps the per-window
    series rather than reconstructing it later from a mean.
    """

    name: str
    keys: tuple[str, ...]
    pinball: tuple[float, ...]
    absolute_error: tuple[float, ...]
    net_return: tuple[float, ...]

    def __post_init__(self):
        for name in ("keys", "pinball", "absolute_error", "net_return"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        lengths = {len(self.keys), len(self.pinball), len(self.absolute_error),
                   len(self.net_return)}
        if len(lengths) != 1:
            raise ConfigurationError("scored series have different lengths",
                                     model=self.name, lengths=sorted(lengths))


def _window_key(window: Window) -> str:
    return f"{window.instrument_key}|{window.as_of.isoformat()}"


def score_model(model: WindowModel, windows: Sequence[Window], *,
                costs: CostModel | None = None,
                parameters: int = 0) -> tuple[ForecastScore, ScoredWindows]:
    """Score one model on one test set. The only scoring path in the harness.

    Scores the **terminal** horizon step. A model can look good on step 1 and be
    useless at step h, and the terminal step is the one a multi-horizon forecast
    exists to produce — reporting the mean across steps would let a strong
    one-step forecast carry a weak long one.
    """
    cost_model = costs if costs is not None else CostModel()
    started = time.time()

    keys: list[str] = []
    per_quantile_predicted: dict[str, list[float]] = {}
    actuals: list[float] = []
    predicted_median: list[float] = []
    net: list[float] = []
    gross: list[float] = []
    pinball_rows: list[float] = []
    absolute_rows: list[float] = []
    declines: dict[str, int] = {}
    trades = 0

    labels: tuple[str, ...] = ()
    median_label = ""
    for window in windows:
        prediction = model.predict_from_bars(window.inputs)
        if isinstance(prediction, Declined):
            declines[str(prediction)] = declines.get(str(prediction), 0) + 1
            continue
        if not labels:
            labels = tuple(prediction.log_return_quantiles)
            median_label = prediction.median_label
        actual = window.target_log_returns()[-1]
        median = prediction.log_return_quantiles[median_label][-1]

        keys.append(_window_key(window))
        actuals.append(actual)
        predicted_median.append(median)
        for label, values in prediction.log_return_quantiles.items():
            per_quantile_predicted.setdefault(label, []).append(values[-1])

        # Per-window pinball, averaged over the grid. Needed per window for the
        # paired test; the aggregate below is the mean of these.
        row = []
        for label, values in prediction.log_return_quantiles.items():
            tau = quantile_level(label)
            if tau is None:                              # pragma: no cover
                continue
            difference = actual - values[-1]
            row.append(tau * difference if difference >= 0
                       else (tau - 1.0) * difference)
        pinball_rows.append(statistics.fmean(row) if row else 0.0)
        absolute_rows.append(abs(actual - median))

        # Costs. A predicted move smaller than the round trip is not a trade.
        if abs(median) > cost_model.round_trip:
            direction = 1.0 if median > 0 else -1.0
            gross.append(direction * actual)
            net.append(direction * actual - cost_model.round_trip)
            trades += 1
        else:
            gross.append(0.0)
            net.append(0.0)

    elapsed = time.time() - started
    if not keys:
        return (ForecastScore(name=model.name, scored=0, offered=len(windows),
                              parameters=parameters, pinball=None,
                              declines=declines, score_seconds=elapsed),
                ScoredWindows(name=model.name, keys=(), pinball=(),
                              absolute_error=(), net_return=()))

    per_quantile: dict[str, float | None] = {}
    for label, values in per_quantile_predicted.items():
        tau = quantile_level(label)
        per_quantile[label] = (None if tau is None
                               else pinball_loss(values, actuals, tau))

    # The widest declared interval, from the grid itself rather than assumed.
    levels = sorted((quantile_level(l), l) for l in labels
                    if quantile_level(l) is not None)
    interval = nominal = None
    if len(levels) >= 2:
        (low_tau, low_label), (high_tau, high_label) = levels[0], levels[-1]
        lows = per_quantile_predicted[low_label]
        highs = per_quantile_predicted[high_label]
        interval = coverage(lows, highs, actuals)
        nominal = high_tau - low_tau

    correct = sum(1 for p, a in zip(predicted_median, actuals)
                  if (p > 0) == (a > 0))
    scored = len(keys)

    return (ForecastScore(
        name=model.name, scored=scored, offered=len(windows),
        parameters=parameters,
        pinball=statistics.fmean(pinball_rows),
        per_quantile=per_quantile,
        median_mae=statistics.fmean(absolute_rows),
        interval_coverage=interval, nominal_coverage=nominal,
        directional_accuracy=correct / scored,
        net_return=statistics.fmean(net), gross_return=statistics.fmean(gross),
        trades=trades, score_seconds=elapsed, declines=declines),
        ScoredWindows(name=model.name, keys=tuple(keys),
                      pinball=tuple(pinball_rows),
                      absolute_error=tuple(absolute_rows),
                      net_return=tuple(net)))


@dataclass(frozen=True)
class PairedComparison:
    """One model against a reference, on the windows both scored."""

    model: str
    reference: str
    #: Windows both models answered. A model that abstained more is compared on
    #: fewer, and the count says so.
    common: int
    pinball_gain: float
    pinball_significant: bool
    pinball_p_value: float
    mae_gain: float
    mae_significant: bool
    net_return_gain: float
    net_return_significant: bool

    @property
    def better(self) -> bool:
        """Significantly better on the proper scoring rule.

        Pinball only. A model that improves absolute error while degrading the
        distribution has not improved the forecast, and letting three metrics
        vote would let it claim two out of three.
        """
        return self.pinball_gain > 0 and self.pinball_significant

    def to_dict(self) -> dict:
        return {"model": self.model, "reference": self.reference,
                "common": self.common, "pinball_gain": self.pinball_gain,
                "pinball_significant": self.pinball_significant,
                "pinball_p_value": self.pinball_p_value,
                "mae_gain": self.mae_gain, "mae_significant": self.mae_significant,
                "net_return_gain": self.net_return_gain,
                "net_return_significant": self.net_return_significant,
                "better": self.better}


def compare_scored(model: ScoredWindows, reference: ScoredWindows, *,
                   seed: int = 0) -> PairedComparison:
    """Paired bootstrap on the windows both models answered.

    Intersecting rather than assuming alignment: two models with different
    abstention behaviour produce different window sets, and zipping them would
    pair a model's window with a reference's *different* window, which is a
    comparison of noise.
    """
    reference_index = {key: i for i, key in enumerate(reference.keys)}
    pairs = [(i, reference_index[key]) for i, key in enumerate(model.keys)
             if key in reference_index]
    if len(pairs) < 2:
        raise InsufficientDataError(
            "the two models share fewer than two scored windows; there is "
            "nothing to compare",
            model=model.name, reference=reference.name, common=len(pairs))

    def gains(left: Sequence[float], right: Sequence[float], *, lower_is_better: bool):
        # Positive means the model is better, whichever direction that is.
        if lower_is_better:
            return [right[j] - left[i] for i, j in pairs]
        return [left[i] - right[j] for i, j in pairs]

    pinball_gains = gains(model.pinball, reference.pinball, lower_is_better=True)
    mae_gains = gains(model.absolute_error, reference.absolute_error,
                      lower_is_better=True)
    return_gains = gains(model.net_return, reference.net_return,
                         lower_is_better=False)

    pinball_test = paired_bootstrap(pinball_gains, name="pinball_gain", seed=seed)
    mae_test = paired_bootstrap(mae_gains, name="mae_gain", seed=seed)
    return_test = paired_bootstrap(return_gains, name="net_return_gain", seed=seed)

    return PairedComparison(
        model=model.name, reference=reference.name, common=len(pairs),
        pinball_gain=statistics.fmean(pinball_gains),
        pinball_significant=pinball_test.significant,
        pinball_p_value=pinball_test.p_value,
        mae_gain=statistics.fmean(mae_gains),
        mae_significant=mae_test.significant,
        net_return_gain=statistics.fmean(return_gains),
        net_return_significant=return_test.significant)


@dataclass(frozen=True)
class BenchmarkReport:
    """Every model's score, and every model against the reference."""

    reference: str
    costs: CostModel
    train_windows: int
    test_windows: int
    scores: tuple[ForecastScore, ...]
    comparisons: tuple[PairedComparison, ...]
    failures: Mapping[str, str] = field(default_factory=dict)
    unavailable: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "scores", tuple(self.scores))
        object.__setattr__(self, "comparisons", tuple(self.comparisons))
        object.__setattr__(self, "failures", dict(self.failures))
        object.__setattr__(self, "unavailable", tuple(self.unavailable))

    def score(self, name: str) -> ForecastScore | None:
        return next((s for s in self.scores if s.name == name), None)

    def comparison(self, name: str) -> PairedComparison | None:
        return next((c for c in self.comparisons if c.model == name), None)

    @property
    def winners(self) -> tuple[str, ...]:
        """Models significantly better than the reference on pinball loss."""
        return tuple(c.model for c in self.comparisons if c.better)

    def to_dict(self) -> dict:
        return {"reference": self.reference, "costs": self.costs.to_dict(),
                "train_windows": self.train_windows,
                "test_windows": self.test_windows,
                "scores": [s.to_dict() for s in self.scores],
                "comparisons": [c.to_dict() for c in self.comparisons],
                "winners": list(self.winners),
                "failures": dict(self.failures),
                "unavailable": list(self.unavailable)}

    def table(self) -> str:
        """Fixed-width, for a report. The comparison column is the conclusion."""
        header = (f"{'model':<24}{'params':>8}{'scored':>8}{'abst':>7}"
                  f"{'pinball':>11}{'mae':>11}{'cover':>8}{'dir':>7}"
                  f"{'net':>11}{'vs ref':>10}")
        lines = [header, "-" * len(header)]
        for score in sorted(self.scores,
                            key=lambda s: (s.pinball is None, s.pinball or 0.0)):
            comparison = self.comparison(score.name)
            verdict = "reference" if score.name == self.reference else (
                "-" if comparison is None else
                ("better" if comparison.better else
                 ("worse" if comparison.pinball_gain < 0 and
                  comparison.pinball_significant else "same")))
            fmt = lambda v, w, p: (f"{'-':>{w}}" if v is None else f"{v:>{w}.{p}f}")
            lines.append(
                f"{score.name:<24}{score.parameters:>8}{score.scored:>8}"
                f"{score.abstention_rate:>7.2f}{fmt(score.pinball, 11, 6)}"
                f"{fmt(score.median_mae, 11, 6)}{fmt(score.interval_coverage, 8, 3)}"
                f"{fmt(score.directional_accuracy, 7, 3)}"
                f"{fmt(score.net_return, 11, 6)}{verdict:>10}")
        if self.unavailable:
            lines.append(f"not run (unavailable): {', '.join(self.unavailable)}")
        for name, reason in sorted(self.failures.items()):
            lines.append(f"not run ({name}): {reason}")
        return "\n".join(lines)


def run_benchmark(*, train: Sequence[Window], test: Sequence[Window],
                  horizon: int, models: Mapping[str, WindowModel] | None = None,
                  baselines: Sequence[str] = (),
                  quantiles: Sequence[float] = DEFAULT_QUANTILES,
                  costs: CostModel | None = None,
                  reference: str = "persistence",
                  baseline_kwargs: Mapping[str, Mapping[str, Any]] | None = None,
                  seed: int = 0) -> BenchmarkReport:
    """Fit and score every model on the same train and test windows.

    `models` is for anything already built — the native quantile network, a
    challenger, a third-party provider behind an adapter. `baselines` names
    entries in `baselines.BASELINES` to construct here. Both go through the
    identical fit-and-score path.

    A model that raises during fit is recorded in `failures` rather than
    aborting the run: a benchmark that stops at the first broken opponent
    reports nothing, and the remaining comparisons are still valid.
    """
    train_windows, test_windows = list(train), list(test)
    if not train_windows:
        raise InsufficientDataError("no training windows")
    if not test_windows:
        raise InsufficientDataError("no test windows")

    wanted = tuple(baselines) if baselines else available_baselines()
    unknown = [k for k in wanted if k not in BASELINES]
    if unknown:
        raise ConfigurationError("unknown baseline", unknown=unknown)
    runnable = set(available_baselines())
    unavailable = tuple(k for k in wanted if k not in runnable)
    extra = dict(baseline_kwargs or {})

    built: dict[str, WindowModel] = {}
    for key in wanted:
        if key not in runnable:
            continue
        built[key] = build_baseline(key, horizon=horizon, quantiles=quantiles,
                                    **dict(extra.get(key, {})))
    for name, model in (models or {}).items():
        if name in built:
            raise ConfigurationError(
                "a supplied model shadows a baseline name; the report would "
                "have two rows claiming to be the same opponent", name=name)
        built[name] = model

    scores: list[ForecastScore] = []
    series: dict[str, ScoredWindows] = {}
    failures: dict[str, str] = {}

    for name, model in built.items():
        started = time.time()
        try:
            report = model.fit(train_windows) if not model.fitted else None
        except Exception as error:                       # noqa: BLE001
            failures[name] = f"{type(error).__name__}: {error}"
            continue
        fit_seconds = time.time() - started
        # A pre-fitted model reports no fit report, so fall back to whatever it
        # can say about itself rather than recording zero parameters — a table
        # showing a trained network with no parameters is worse than no column.
        parameters = (report.parameters if report is not None
                      else int(getattr(model, "parameter_count", 0)))
        try:
            score, scored = score_model(model, test_windows, costs=costs,
                                        parameters=parameters)
        except Exception as error:                       # noqa: BLE001
            failures[name] = f"{type(error).__name__}: {error}"
            continue
        # `name` rather than `model.name`: a caller that registered a model
        # under a key expects that key back, and a parameterised baseline's own
        # name carries its settings.
        scores.append(_rename(score, name, fit_seconds))
        series[name] = ScoredWindows(name=name, keys=scored.keys,
                                     pinball=scored.pinball,
                                     absolute_error=scored.absolute_error,
                                     net_return=scored.net_return)

    if reference not in series:
        raise ConfigurationError(
            "the reference model produced no scores, so nothing can be "
            "compared against it", reference=reference,
            scored=sorted(series), failures=sorted(failures))

    comparisons = []
    for name, scored in series.items():
        if name == reference:
            continue
        try:
            comparisons.append(compare_scored(scored, series[reference], seed=seed))
        except InsufficientDataError as error:           # pragma: no cover
            failures[name] = f"comparison skipped: {error}"

    return BenchmarkReport(
        reference=reference, costs=costs if costs is not None else CostModel(),
        train_windows=len(train_windows), test_windows=len(test_windows),
        scores=tuple(scores), comparisons=tuple(comparisons),
        failures=failures, unavailable=unavailable)


class NativeEntrant(WindowModel):
    """The native quantile network, behind the interface its opponents use.

    An adapter rather than a change to `neural.py`: the network's own interface
    is shaped by training, and bending it to fit a benchmark would let the
    benchmark's conveniences leak into the model. Here the adaptation is one
    small class that the harness owns.
    """

    KIND = "olympus_native"
    VERSION = "1"

    def __init__(self, model: Any, *, seed: int = 0, name: str = ""):
        config = model.config
        super().__init__(quantiles=config.quantiles, horizon=config.horizon)
        self.model = model
        self.seed = int(seed)
        self._name = name or self.KIND
        self._fitted = bool(getattr(model, "fitted", False))

    @property
    def name(self) -> str:
        return self._name

    @property
    def required_bars(self) -> int:
        return int(self.model.config.lookback)

    @property
    def parameter_count(self) -> int:
        return int(self.model.n_parameters())

    def fit(self, windows: Sequence[Window]) -> "Any":
        from .baselines import BaselineFitReport
        report = self.model.fit(windows, seed=self.seed)
        self._fitted = True
        return BaselineFitReport(
            name=self.name, rows=report.rows, horizon=self.horizon,
            parameters=report.parameters,
            notes=f"converged={report.converged} "
                  f"final_loss={report.final_loss:.6f}"
                  if report.final_loss is not None else "no loss recorded")

    def predict_from_bars(self, bars: Sequence[Any]) -> Any:
        return self.model.predict_from_bars(bars)


def _rename(score: ForecastScore, name: str, fit_seconds: float) -> ForecastScore:
    """A copy under the harness's key, with the fit time filled in."""
    return ForecastScore(
        name=name, scored=score.scored, offered=score.offered,
        parameters=score.parameters, pinball=score.pinball,
        per_quantile=score.per_quantile, median_mae=score.median_mae,
        interval_coverage=score.interval_coverage,
        nominal_coverage=score.nominal_coverage,
        directional_accuracy=score.directional_accuracy,
        net_return=score.net_return, gross_return=score.gross_return,
        trades=score.trades, fit_seconds=fit_seconds,
        score_seconds=score.score_seconds, declines=score.declines)


__all__ = ["CostModel", "FREE", "ForecastScore", "ScoredWindows", "score_model",
           "PairedComparison", "compare_scored", "BenchmarkReport",
           "run_benchmark"]

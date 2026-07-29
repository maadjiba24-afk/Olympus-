"""Nine baselines, on one interface, scored by one harness.

A complex model earns its place by beating simple alternatives on the same
data, the same splits, the same costs and the same metrics. Everything in that
sentence is load-bearing, and each clause is a way a comparison is usually
rigged: a baseline fitted on a different window, evaluated without costs, or
scored on a metric chosen after the numbers were seen.

So the baselines here are not sketches. They share `WindowModel`, consume the
same `data.Window` objects the native model consumes, emit the same
`quantile.QuantilePrediction`, and are scored by `benchmark.py` with the same
pinball loss the network trains on.

Point forecasts become quantile forecasts the same way for all nine
-------------------------------------------------------------------
Persistence predicts a number, not a distribution. Comparing a point forecaster
against a quantile model on pinball loss requires giving the point forecaster a
distribution, and the choice of how is a place to accidentally favour one side.

Every baseline here gets the *same* treatment: fit the point rule, take its
residuals **on the training split only**, and use their empirical quantiles as
the spread. So a baseline's interval is as good as its residuals deserve, and
the native model's advantage — if it has one — must come from conditioning its
spread on the input, which is the thing an empirical residual quantile cannot do.

Two baselines deliberately break that symmetry, and say so: `volatility` scales
its spread by the window's own realised volatility, and `regime` conditions both
drift and spread on the regime label. They exist to make the comparison harder,
because a native model that only beats a fixed-width interval has beaten a straw
opponent.

Availability
------------
Seven of the nine are pure stdlib. `recurrent` and `temporal_transformer` need
torch and are behind the `native` extra; `available_baselines()` reports which
can run, so a benchmark on an installation without torch reports seven results
rather than crashing or silently comparing against fewer opponents.
"""

from __future__ import annotations

import math
import statistics
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..contracts import Regime
from ..errors import ConfigurationError, InsufficientDataError
from ..features import FeatureBuilder
from .data import Window
from .quantile import (DECLINE_NOT_FITTED, Declined, QuantilePrediction,
                       quantile_label)
from .state import _AtLast

#: The quantile grid every baseline reports on. Matches `NeuralConfig`'s
#: default so the comparison is like for like.
DEFAULT_QUANTILES: tuple[float, ...] = (0.1, 0.25, 0.5, 0.75, 0.9)

#: Returned when a baseline is asked for a forecast from too little history.
DECLINE_SHORT_WINDOW = Declined("short_window")
#: Returned when the input carries none of the features the model was fitted on.
DECLINE_NO_FEATURES = Declined("no_usable_features")


def _log_returns(bars: Sequence[Any]) -> list[float]:
    out = []
    for i in range(1, len(bars)):
        previous, current = bars[i - 1].close, bars[i].close
        if previous <= 0 or current <= 0:                # pragma: no cover
            raise ConfigurationError("non-positive close in a baseline window")
        out.append(math.log(current / previous))
    return out


def _empirical_quantile(ordered: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile of a *sorted* sample."""
    if not ordered:                                      # pragma: no cover
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    low = int(math.floor(position))
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


@dataclass(frozen=True)
class BaselineFitReport:
    """What a fit did. Comparable across baselines because it is the same shape."""

    name: str
    rows: int
    horizon: int
    parameters: int
    #: Mean absolute residual per horizon step, on the training split.
    train_mae: tuple[float, ...] = ()
    notes: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "rows": self.rows, "horizon": self.horizon,
                "parameters": self.parameters,
                "train_mae": list(self.train_mae), "notes": self.notes}


class WindowModel(ABC):
    """What every baseline and every native model looks like to the harness.

    Narrower than `forecast.Forecaster` on purpose: this interface consumes
    `Window` objects, which carry their own targets, so a model fitted here
    cannot see past its window's last input. `forecast.Forecaster` is the
    serving interface and takes raw bars; this is the *training* interface and
    takes examples.
    """

    #: Stable identifier. Goes into result tables and manifests.
    KIND: str = "baseline"
    #: Bumped when the arithmetic changes.
    VERSION: str = "1"

    def __init__(self, *, quantiles: Sequence[float] = DEFAULT_QUANTILES,
                 horizon: int = 1):
        levels = tuple(sorted(float(q) for q in quantiles))
        if not levels:
            raise ConfigurationError("a model needs at least one quantile")
        if any(not 0.0 < q < 1.0 for q in levels):
            raise ConfigurationError("quantiles must be in (0, 1)",
                                     quantiles=list(levels))
        if int(horizon) < 1:
            raise ConfigurationError("horizon must be >= 1", horizon=horizon)
        self.quantiles = levels
        self.horizon = int(horizon)
        self.labels = tuple(quantile_label(q) for q in levels)
        self.median_index = min(range(len(levels)),
                                key=lambda i: abs(levels[i] - 0.5))
        self._fitted = False

    @property
    def name(self) -> str:
        return self.KIND

    @property
    def fitted(self) -> bool:
        return self._fitted

    @property
    def parameter_count(self) -> int:
        """Parameters, for a model the harness receives already fitted.

        Zero here because an unfitted model has none; `fit` reports the real
        count in its `BaselineFitReport`.
        """
        return 0

    @property
    @abstractmethod
    def required_bars(self) -> int:
        """Minimum input bars before this model will answer."""

    @abstractmethod
    def fit(self, windows: Sequence[Window]) -> BaselineFitReport:
        """Fit on training windows. Never on validation or test."""

    @abstractmethod
    def predict_from_bars(self, bars: Sequence[Any]
                          ) -> QuantilePrediction | Declined:
        """Forecast from an input window. Declines rather than guessing."""

    def describe(self) -> dict:
        return {"kind": self.KIND, "version": self.VERSION, "name": self.name,
                "horizon": self.horizon, "quantiles": list(self.quantiles),
                "required_bars": self.required_bars, "fitted": self.fitted}


class PointBaseline(WindowModel):
    """A point rule plus empirical residual quantiles. Seven of the nine.

    Subclasses implement `_project`, which returns cumulative log returns per
    horizon step. Everything else — residual collection, quantile construction,
    monotonicity, declining — happens here, once, so two baselines cannot differ
    because of how their intervals were built.

    One consequence, worth knowing before reading a benchmark table
    ---------------------------------------------------------------
    A point forecast that is the *same constant* for every window scores
    identically to persistence. Shifting every prediction by `c` shifts every
    residual by `-c`, and the empirical residual quantiles shift it straight
    back. So a baseline only differs from persistence to the extent its point
    forecast **varies with its input** — which is the correct behaviour, and is
    why a row matching persistence to six decimal places usually means the model
    degenerated rather than that the harness is broken. `BaselineFitReport.notes`
    is where a baseline that knows it has degenerated says so.
    """

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        #: `[step][quantile] -> residual offset`. Fitted on train only.
        self._offsets: tuple[tuple[float, ...], ...] = ()
        self._parameters = 0

    @abstractmethod
    def _project(self, bars: Sequence[Any]) -> tuple[float, ...]:
        """Point forecast: cumulative log return at each horizon step."""

    def _prepare(self, windows: Sequence[Window]) -> int:
        """Hook for subclasses that must fit something before residuals.

        Returns the parameter count. Called once, before residuals are
        collected, and only ever on the training windows.
        """
        return 0

    def fit(self, windows: Sequence[Window]) -> BaselineFitReport:
        rows = list(windows)
        if not rows:
            raise InsufficientDataError("no training windows",
                                        baseline=self.KIND)
        horizons = {w.horizon for w in rows}
        if horizons != {self.horizon}:
            raise ConfigurationError(
                "training windows do not match the configured horizon",
                configured=self.horizon, found=sorted(horizons))

        self._parameters = self._prepare(rows)

        residuals: list[list[float]] = [[] for _ in range(self.horizon)]
        for window in rows:
            if len(window.inputs) < self.required_bars:
                continue
            predicted = self._project(window.inputs)
            actual = window.target_log_returns()
            for step in range(self.horizon):
                residuals[step].append(actual[step] - predicted[step])
        if not residuals[0]:
            raise InsufficientDataError(
                "no training window was long enough for this baseline",
                baseline=self.KIND, required_bars=self.required_bars)

        offsets = []
        for step in range(self.horizon):
            ordered = sorted(residuals[step])
            offsets.append(tuple(_empirical_quantile(ordered, q)
                                 for q in self.quantiles))
        self._offsets = tuple(offsets)
        self._fitted = True
        return BaselineFitReport(
            name=self.name, rows=len(residuals[0]), horizon=self.horizon,
            parameters=self._parameters + self.horizon * len(self.quantiles),
            train_mae=tuple(statistics.fmean(abs(r) for r in step)
                            for step in residuals),
            notes=self._fit_notes())

    def _fit_notes(self) -> str:
        """Anything a subclass learned about its own fit that a reader needs.

        Empty by default. Overridden where a baseline can detect that it has
        degenerated — see `RegimeBaseline`.
        """
        return ""

    def _spread_scale(self, bars: Sequence[Any]) -> float:
        """Multiplier on the residual offsets. 1.0 unless a subclass says else.

        This is the hook `volatility` and `regime` use to make their intervals
        conditional, and it is a single number so the monotonicity of the
        quantile ladder survives it — scaling a sorted offset vector by a
        positive scalar keeps it sorted.
        """
        return 1.0

    def predict_from_bars(self, bars: Sequence[Any]
                          ) -> QuantilePrediction | Declined:
        if not self._fitted:
            return DECLINE_NOT_FITTED
        window = list(bars)
        if len(window) < self.required_bars:
            return DECLINE_SHORT_WINDOW
        point = self._project(window)
        scale = self._spread_scale(window)
        by_label: dict[str, tuple[float, ...]] = {}
        for index, label in enumerate(self.labels):
            by_label[label] = tuple(point[step] + self._offsets[step][index] * scale
                                    for step in range(self.horizon))
        return QuantilePrediction(log_return_quantiles=by_label, cell=(),
                                  cell_samples=len(self._offsets),
                                  median_label=self.labels[self.median_index])


# ===========================================================================
# 1-2. persistence and moving average
# ===========================================================================

class PersistenceBaseline(PointBaseline):
    """Tomorrow's price is today's. The opponent every model must beat.

    Its point forecast is exactly zero cumulative log return at every step, so
    its entire skill is in the residual quantiles — which is the correct
    characterisation of a random walk and is why it is so hard to beat.
    """

    KIND = "persistence"

    @property
    def required_bars(self) -> int:
        return 1

    def _project(self, bars: Sequence[Any]) -> tuple[float, ...]:
        return tuple(0.0 for _ in range(self.horizon))


class MovingAverageBaseline(PointBaseline):
    """Extrapolate the mean recent log return.

    A drift model. Distinct from persistence in exactly one way — it believes
    the recent average return continues — which makes the pair a clean test of
    whether a series has exploitable drift at all.
    """

    KIND = "moving_average"

    def __init__(self, *, window: int = 10, **kwargs: Any):
        super().__init__(**kwargs)
        if int(window) < 1:
            raise ConfigurationError("window must be >= 1", window=window)
        self.window = int(window)

    @property
    def name(self) -> str:
        return f"{self.KIND}_{self.window}"

    @property
    def required_bars(self) -> int:
        return self.window + 1

    def _project(self, bars: Sequence[Any]) -> tuple[float, ...]:
        returns = _log_returns(bars)[-self.window:]
        drift = statistics.fmean(returns) if returns else 0.0
        return tuple(drift * (step + 1) for step in range(self.horizon))


# ===========================================================================
# 3-4. linear regression and autoregression
# ===========================================================================

def _solve_normal_equations(design: Sequence[Sequence[float]],
                            target: Sequence[float], *,
                            ridge: float = 1e-8) -> list[float]:
    """Least squares by Gaussian elimination on X'X + λI.

    A small ridge term, always. The design matrices here are built from
    correlated market features, so X'X is routinely near-singular; without it
    the solve produces enormous coefficients that fit the training split
    perfectly and predict noise.
    """
    if not design:                                       # pragma: no cover
        raise InsufficientDataError("empty design matrix")
    width = len(design[0])
    gram = [[0.0] * (width + 1) for _ in range(width)]
    for row, y in zip(design, target):
        for i in range(width):
            for j in range(width):
                gram[i][j] += row[i] * row[j]
            gram[i][width] += row[i] * y
    for i in range(width):
        gram[i][i] += ridge

    for column in range(width):
        pivot = max(range(column, width), key=lambda r: abs(gram[r][column]))
        if abs(gram[pivot][column]) < 1e-14:
            # A structurally rank-deficient column contributes nothing; leaving
            # its coefficient at zero is correct and stable, where continuing
            # would divide by an epsilon.
            continue
        gram[column], gram[pivot] = gram[pivot], gram[column]
        divisor = gram[column][column]
        for j in range(column, width + 1):
            gram[column][j] /= divisor
        for row_index in range(width):
            if row_index == column:
                continue
            factor = gram[row_index][column]
            if factor == 0.0:
                continue
            for j in range(column, width + 1):
                gram[row_index][j] -= factor * gram[column][j]
    return [gram[i][width] for i in range(width)]


class _FeatureMixin:
    """Shared feature extraction, over Olympus's own causal feature layer.

    Reuses `features.FeatureBuilder` rather than defining a private feature set:
    a second implementation of "RSI over this window" is a second thing to keep
    causal, and `features.assert_causal` already tests the first one.

    The usable feature names are the intersection across training windows. A
    short lookback leaves long-warm-up features absent in *some* windows, and
    training on a column that is sometimes missing is how a model acquires a
    dependency its serving path cannot satisfy.
    """

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._builder = FeatureBuilder()
        self._feature_names: tuple[str, ...] = ()
        self._required_bars = 2

    @property
    def required_bars(self) -> int:
        """Warm-up of the *selected* features, not of every registered one.

        `FeatureBuilder.warmup_bars` is the maximum over everything registered,
        which includes a 50-bar moving average. Demanding that would make this
        baseline decline on every window a shorter-warm-up model answers, and a
        benchmark in which one opponent silently abstains more often than
        another is not a comparison.
        """
        return self._required_bars

    def _row(self, bars: Sequence[Any]) -> dict[str, float]:
        built = self._builder.build(bars, clock=_AtLast(bars[-1].ts_close))
        return dict(built.values)

    def _learn_feature_names(self, windows: Sequence[Window]) -> tuple[str, ...]:
        common: set[str] | None = None
        for window in windows:
            present = {k for k, v in self._row(window.inputs).items()
                       if v is not None and math.isfinite(v)}
            common = present if common is None else (common & present)
            if common is not None and not common:
                break
        names = tuple(sorted(common or ()))
        if not names:
            raise InsufficientDataError(
                "no feature is computable in every training window; the "
                "lookback is too short for this feature set",
                lookback=min(len(w.inputs) for w in windows))
        self._required_bars = max(2, max(self._builder.min_bars(n) for n in names))
        return names

    def _vector(self, bars: Sequence[Any]) -> list[float] | None:
        row = self._row(bars)
        out = []
        for name in self._feature_names:
            value = row.get(name)
            if value is None or not math.isfinite(value):
                return None
            out.append(float(value))
        return out


class LinearRegressionBaseline(_FeatureMixin, PointBaseline):
    """Ridge regression from Olympus features to cumulative log returns.

    One independent solve per horizon step rather than an autoregressive chain,
    matching how the native model predicts — so a difference between them is a
    difference in the function class, not in the prediction scheme.
    """

    KIND = "linear_regression"

    def __init__(self, *, ridge: float = 1e-4, **kwargs: Any):
        super().__init__(**kwargs)
        self.ridge = float(ridge)
        self._weights: tuple[tuple[float, ...], ...] = ()

    def _prepare(self, windows: Sequence[Window]) -> int:
        self._feature_names = self._learn_feature_names(windows)
        design, targets = [], [[] for _ in range(self.horizon)]
        for window in windows:
            vector = self._vector(window.inputs)
            if vector is None:                           # pragma: no cover
                continue
            design.append([1.0, *vector])
            actual = window.target_log_returns()
            for step in range(self.horizon):
                targets[step].append(actual[step])
        if len(design) <= len(self._feature_names) + 1:
            raise InsufficientDataError(
                "fewer training rows than coefficients; the fit would be exact "
                "and meaningless", rows=len(design),
                coefficients=len(self._feature_names) + 1)
        self._weights = tuple(
            tuple(_solve_normal_equations(design, targets[step], ridge=self.ridge))
            for step in range(self.horizon))
        return sum(len(w) for w in self._weights)

    def _project(self, bars: Sequence[Any]) -> tuple[float, ...]:
        vector = self._vector(bars)
        if vector is None or not self._weights:
            return tuple(0.0 for _ in range(self.horizon))
        row = [1.0, *vector]
        return tuple(sum(w * x for w, x in zip(self._weights[step], row))
                     for step in range(self.horizon))

    def predict_from_bars(self, bars: Sequence[Any]
                          ) -> QuantilePrediction | Declined:
        if self._fitted and self._vector(list(bars)) is None:
            # Declining beats projecting zero: a caller told "no forecast" can
            # fall back, whereas a caller handed a confident zero cannot tell it
            # apart from a genuine no-move prediction.
            return DECLINE_NO_FEATURES
        return super().predict_from_bars(bars)


class AutoregressionBaseline(PointBaseline):
    """AR(p) on log returns, iterated forward and cumulated.

    The classical opponent. It is the only baseline here whose multi-step
    forecast is *composed* from one-step forecasts, which is exactly the scheme
    the native model rejects — so the pair measures whether direct multi-horizon
    prediction is worth anything.
    """

    KIND = "autoregression"

    def __init__(self, *, order: int = 3, ridge: float = 1e-6, **kwargs: Any):
        super().__init__(**kwargs)
        if int(order) < 1:
            raise ConfigurationError("order must be >= 1", order=order)
        self.order = int(order)
        self.ridge = float(ridge)
        self._coefficients: tuple[float, ...] = ()

    @property
    def name(self) -> str:
        return f"{self.KIND}_{self.order}"

    @property
    def required_bars(self) -> int:
        return self.order + 2

    def _prepare(self, windows: Sequence[Window]) -> int:
        design, target = [], []
        for window in windows:
            returns = _log_returns(window.inputs)
            for i in range(self.order, len(returns)):
                design.append([1.0, *returns[i - self.order:i]])
                target.append(returns[i])
        if len(design) <= self.order + 1:
            raise InsufficientDataError("not enough rows to fit AR",
                                        rows=len(design), order=self.order)
        self._coefficients = tuple(
            _solve_normal_equations(design, target, ridge=self.ridge))
        return len(self._coefficients)

    def _project(self, bars: Sequence[Any]) -> tuple[float, ...]:
        if not self._coefficients:
            return tuple(0.0 for _ in range(self.horizon))
        history = _log_returns(bars)[-self.order:]
        if len(history) < self.order:                    # pragma: no cover
            return tuple(0.0 for _ in range(self.horizon))
        constant, *weights = self._coefficients
        cumulative, out = 0.0, []
        state = list(history)
        for _ in range(self.horizon):
            step = constant + sum(w * x for w, x in zip(weights, state))
            cumulative += step
            out.append(cumulative)
            state = state[1:] + [step]
        return tuple(out)


# ===========================================================================
# 5. gradient-boosted trees
# ===========================================================================

class _Stump:
    """A depth-limited regression tree. Hand-written, pure stdlib.

    Hand-written because Olympus ships three required dependencies and a CI
    guard that counts them (blocker B6); adding scikit-learn to acquire a
    baseline would be a permanent dependency bought for a comparison.

    Split candidates are sample quantiles rather than every distinct value, so
    the fit is O(rows · features · candidates) instead of O(rows² · features).
    """

    __slots__ = ("feature", "threshold", "left", "right", "value")

    def __init__(self):
        self.feature: int | None = None
        self.threshold = 0.0
        self.left: "_Stump | None" = None
        self.right: "_Stump | None" = None
        self.value = 0.0

    def fit(self, rows: Sequence[Sequence[float]], target: Sequence[float],
            indices: Sequence[int], depth: int, *, min_samples: int,
            candidates: int) -> "_Stump":
        values = [target[i] for i in indices]
        self.value = statistics.fmean(values) if values else 0.0
        if depth <= 0 or len(indices) < 2 * min_samples:
            return self

        total = sum(values)
        best = (0.0, None, 0.0)                          # (gain, feature, threshold)
        for feature in range(len(rows[0])):
            column = sorted((rows[i][feature], target[i]) for i in indices)
            if column[0][0] == column[-1][0]:
                continue
            step = max(1, len(column) // (candidates + 1))
            left_sum, left_count = 0.0, 0
            for position in range(len(column) - 1):
                left_sum += column[position][1]
                left_count += 1
                if position % step or left_count < min_samples:
                    continue
                right_count = len(column) - left_count
                if right_count < min_samples:
                    break
                if column[position][0] == column[position + 1][0]:
                    continue
                right_sum = total - left_sum
                # Variance reduction, written as the increase in the sum of
                # squared group means — the standard rearrangement that avoids
                # recomputing variances per candidate.
                gain = (left_sum * left_sum / left_count
                        + right_sum * right_sum / right_count
                        - total * total / len(indices))
                if gain > best[0]:
                    best = (gain, feature,
                            0.5 * (column[position][0] + column[position + 1][0]))
        if best[1] is None:
            return self

        self.feature, self.threshold = best[1], best[2]
        left = [i for i in indices if rows[i][self.feature] <= self.threshold]
        right = [i for i in indices if rows[i][self.feature] > self.threshold]
        if not left or not right:                        # pragma: no cover
            self.feature = None
            return self
        self.left = _Stump().fit(rows, target, left, depth - 1,
                                 min_samples=min_samples, candidates=candidates)
        self.right = _Stump().fit(rows, target, right, depth - 1,
                                  min_samples=min_samples, candidates=candidates)
        return self

    def predict(self, row: Sequence[float]) -> float:
        node = self
        while node.feature is not None:
            node = node.left if row[node.feature] <= node.threshold else node.right
        return node.value

    def nodes(self) -> int:
        if self.feature is None:
            return 1
        return 1 + self.left.nodes() + self.right.nodes()


class GradientBoostedTreesBaseline(_FeatureMixin, PointBaseline):
    """Least-squares gradient boosting over Olympus features.

    The strongest stdlib opponent here, and the one most likely to beat a small
    network on tabular features. Included for that reason: a benchmark whose
    hardest opponent is a linear model is a benchmark designed to be won.
    """

    KIND = "gradient_boosted_trees"

    def __init__(self, *, estimators: int = 40, depth: int = 2,
                 learning_rate: float = 0.1, min_samples: int = 8,
                 split_candidates: int = 16, **kwargs: Any):
        super().__init__(**kwargs)
        for name, value in (("estimators", estimators), ("depth", depth),
                            ("min_samples", min_samples),
                            ("split_candidates", split_candidates)):
            if int(value) < 1:
                raise ConfigurationError(f"{name} must be >= 1", **{name: value})
        if not 0.0 < learning_rate <= 1.0:
            raise ConfigurationError("learning_rate must be in (0, 1]",
                                     learning_rate=learning_rate)
        self.estimators = int(estimators)
        self.depth = int(depth)
        self.learning_rate = float(learning_rate)
        self.min_samples = int(min_samples)
        self.split_candidates = int(split_candidates)
        self._base: tuple[float, ...] = ()
        self._trees: tuple[tuple[_Stump, ...], ...] = ()

    def _prepare(self, windows: Sequence[Window]) -> int:
        self._feature_names = self._learn_feature_names(windows)
        rows, targets = [], [[] for _ in range(self.horizon)]
        for window in windows:
            vector = self._vector(window.inputs)
            if vector is None:                           # pragma: no cover
                continue
            rows.append(vector)
            actual = window.target_log_returns()
            for step in range(self.horizon):
                targets[step].append(actual[step])
        if len(rows) < 4 * self.min_samples:
            raise InsufficientDataError(
                "too few rows to boost", rows=len(rows),
                need=4 * self.min_samples)

        indices = list(range(len(rows)))
        base, forests, nodes = [], [], 0
        for step in range(self.horizon):
            target = targets[step]
            start = statistics.fmean(target)
            residual = [y - start for y in target]
            trees: list[_Stump] = []
            for _ in range(self.estimators):
                tree = _Stump().fit(rows, residual, indices, self.depth,
                                    min_samples=self.min_samples,
                                    candidates=self.split_candidates)
                trees.append(tree)
                nodes += tree.nodes()
                for i in indices:
                    residual[i] -= self.learning_rate * tree.predict(rows[i])
            base.append(start)
            forests.append(tuple(trees))
        self._base = tuple(base)
        self._trees = tuple(forests)
        return nodes

    def _project(self, bars: Sequence[Any]) -> tuple[float, ...]:
        vector = self._vector(bars)
        if vector is None or not self._trees:
            return tuple(0.0 for _ in range(self.horizon))
        out = []
        for step in range(self.horizon):
            value = self._base[step]
            for tree in self._trees[step]:
                value += self.learning_rate * tree.predict(vector)
            out.append(value)
        return tuple(out)

    def predict_from_bars(self, bars: Sequence[Any]
                          ) -> QuantilePrediction | Declined:
        if self._fitted and self._vector(list(bars)) is None:
            return DECLINE_NO_FEATURES
        return super().predict_from_bars(bars)


# ===========================================================================
# 6-7. small recurrent and small temporal transformer (torch)
# ===========================================================================

class _TorchSequenceBaseline(PointBaseline):
    """Shared training loop for the two torch baselines.

    They differ in one method — `_build`, which returns the sequence module —
    so a difference in their results is a difference in architecture and not in
    how long they trained or how their batches were drawn.
    """

    def __init__(self, *, lookback: int = 16, d_model: int = 16,
                 epochs: int = 40, batch_size: int = 32,
                 learning_rate: float = 1e-2, seed: int = 0, **kwargs: Any):
        super().__init__(**kwargs)
        for name, value in (("lookback", lookback), ("d_model", d_model),
                            ("epochs", epochs), ("batch_size", batch_size)):
            if int(value) < 1:
                raise ConfigurationError(f"{name} must be >= 1", **{name: value})
        self.lookback = int(lookback)
        self.d_model = int(d_model)
        self.epochs = int(epochs)
        self.batch_size = int(batch_size)
        self.learning_rate = float(learning_rate)
        self.seed = int(seed)
        self._module: Any = None

    @property
    def required_bars(self) -> int:
        return self.lookback + 1

    @abstractmethod
    def _build(self, torch: Any) -> Any:
        """The sequence module. `[batch, channels, time] -> [batch, horizon]`."""

    def _tensor(self, windows: Sequence[Window], torch: Any):
        from .encoder import bars_to_channels, normalise_window
        rows, targets = [], []
        for window in windows:
            if len(window.inputs) < self.required_bars:
                continue
            channels = bars_to_channels(window.inputs)
            normalised, _ = normalise_window(channels)
            trimmed = [column[-self.lookback:] for column in normalised]
            if any(len(column) != self.lookback for column in trimmed):
                continue                                 # pragma: no cover
            rows.append(trimmed)
            targets.append(list(window.target_log_returns()))
        if not rows:
            raise InsufficientDataError("no usable rows for a torch baseline",
                                        baseline=self.KIND)
        return (torch.tensor(rows, dtype=torch.float32),
                torch.tensor(targets, dtype=torch.float32))

    def _prepare(self, windows: Sequence[Window]) -> int:
        from .torchutil import configure_determinism, require_torch
        torch = require_torch()
        generator = configure_determinism(self.seed)
        inputs, targets = self._tensor(windows, torch)
        module = self._build(torch)
        optimiser = torch.optim.Adam(module.parameters(), lr=self.learning_rate)
        rows = inputs.shape[0]
        module.train()
        for _ in range(self.epochs):
            order = torch.randperm(rows, generator=generator)
            for start in range(0, rows, self.batch_size):
                batch = order[start:start + self.batch_size]
                optimiser.zero_grad()
                loss = ((module(inputs[batch]) - targets[batch]) ** 2).mean()
                loss.backward()
                optimiser.step()
        module.eval()
        self._module = module
        return sum(p.numel() for p in module.parameters() if p.requires_grad)

    def _project(self, bars: Sequence[Any]) -> tuple[float, ...]:
        if self._module is None:
            return tuple(0.0 for _ in range(self.horizon))
        from .encoder import bars_to_channels, normalise_window
        from .torchutil import require_torch
        torch = require_torch()
        channels = bars_to_channels(bars)
        normalised, _ = normalise_window(channels)
        trimmed = [column[-self.lookback:] for column in normalised]
        if any(len(column) != self.lookback for column in trimmed):
            return tuple(0.0 for _ in range(self.horizon))
        with torch.no_grad():
            values = self._module(torch.tensor([trimmed], dtype=torch.float32))
        return tuple(float(v) for v in values[0])


class RecurrentBaseline(_TorchSequenceBaseline):
    """A single-layer GRU with a linear head.

    The obvious sequence baseline, and the architecture the causal-convolution
    trunk was chosen *over*. Including it makes that choice falsifiable rather
    than a preference stated in a design document.
    """

    KIND = "recurrent"

    def _build(self, torch: Any) -> Any:
        from .encoder import CHANNELS
        from .torchutil import cached_class
        nn = torch.nn
        horizon, d_model = self.horizon, self.d_model

        def factory():
            class GRUBaseline(nn.Module):
                def __init__(self, channels: int, width: int, steps: int):
                    super().__init__()
                    self.rnn = nn.GRU(channels, width, batch_first=True)
                    self.head = nn.Linear(width, steps)

                def forward(self, x):
                    # [batch, channels, time] -> [batch, time, channels]
                    output, _ = self.rnn(x.transpose(1, 2))
                    # The last hidden state only. A GRU is causal by
                    # construction, so no masking is needed — but taking the
                    # last step is what makes that matter.
                    return self.head(output[:, -1, :])
            return GRUBaseline

        return cached_class("GRUBaseline", factory)(len(CHANNELS), d_model, horizon)


class TemporalTransformerBaseline(_TorchSequenceBaseline):
    """A one-layer causal self-attention block with a linear head.

    Causal by an explicit mask, which is the point of including it: the native
    trunk gets causality from left-padded convolutions and cannot be
    misconfigured into seeing the future, whereas this can, and the test suite
    checks that its mask is applied.
    """

    KIND = "temporal_transformer"

    def __init__(self, *, heads: int = 2, **kwargs: Any):
        super().__init__(**kwargs)
        if int(heads) < 1:
            raise ConfigurationError("heads must be >= 1", heads=heads)
        if self.d_model % int(heads):
            raise ConfigurationError("d_model must divide by heads",
                                     d_model=self.d_model, heads=heads)
        self.heads = int(heads)

    def _build(self, torch: Any) -> Any:
        from .encoder import CHANNELS
        from .torchutil import cached_class
        nn = torch.nn
        horizon, d_model, heads, lookback = (self.horizon, self.d_model,
                                             self.heads, self.lookback)

        def factory():
            class CausalTransformerBaseline(nn.Module):
                def __init__(self, channels: int, width: int, n_heads: int,
                             steps: int, time: int):
                    super().__init__()
                    self.project = nn.Linear(channels, width)
                    self.position = nn.Parameter(torch.zeros(time, width))
                    self.attention = nn.MultiheadAttention(
                        width, n_heads, batch_first=True)
                    self.norm = nn.LayerNorm(width)
                    self.feed_forward = nn.Sequential(
                        nn.Linear(width, 2 * width), nn.GELU(),
                        nn.Linear(2 * width, width))
                    self.head = nn.Linear(width, steps)

                def forward(self, x):
                    h = self.project(x.transpose(1, 2))
                    h = h + self.position[:h.shape[1]]
                    # The causal mask. Without it a position attends to its own
                    # future and the model reports a skill it does not have.
                    mask = torch.triu(
                        torch.ones(h.shape[1], h.shape[1], dtype=torch.bool),
                        diagonal=1)
                    attended, _ = self.attention(h, h, h, attn_mask=mask,
                                                 need_weights=False)
                    h = self.norm(h + attended)
                    h = self.norm(h + self.feed_forward(h))
                    return self.head(h[:, -1, :])
            return CausalTransformerBaseline

        cls = cached_class("CausalTransformerBaseline", factory)
        return cls(len(CHANNELS), d_model, heads, horizon, lookback)


# ===========================================================================
# 8-9. volatility and regime
# ===========================================================================

class VolatilityBaseline(PointBaseline):
    """Zero drift, but an interval that widens with realised volatility.

    The first opponent whose *uncertainty* is conditional. A native model that
    beats persistence on pinball loss may be doing so entirely by widening its
    interval in volatile periods, which is a real skill but a much smaller one
    than it looks — this baseline is what separates the two.
    """

    KIND = "volatility"

    def __init__(self, *, window: int = 20, floor: float = 1e-6, **kwargs: Any):
        super().__init__(**kwargs)
        if int(window) < 2:
            raise ConfigurationError("window must be >= 2", window=window)
        self.window = int(window)
        self.floor = float(floor)
        self._reference_sigma = 0.0

    @property
    def name(self) -> str:
        return f"{self.KIND}_{self.window}"

    @property
    def required_bars(self) -> int:
        return self.window + 1

    def _sigma(self, bars: Sequence[Any]) -> float:
        from ..volatility import realised_volatility
        closes = [b.close for b in bars]
        window = max(2, min(self.window, len(closes) - 1))
        series = realised_volatility(closes, window)
        latest = next((v for v in reversed(series) if v is not None), None)
        return max(float(latest), self.floor) if latest is not None else self.floor

    def _prepare(self, windows: Sequence[Window]) -> int:
        # The reference is the median training sigma, so the scale is 1.0 in a
        # typical window and the residual quantiles keep their meaning. Fitted
        # on the training split only, like every other statistic here.
        sigmas = [self._sigma(w.inputs) for w in windows
                  if len(w.inputs) >= self.required_bars]
        self._reference_sigma = max(statistics.median(sigmas), self.floor) \
            if sigmas else self.floor
        return 1

    def _project(self, bars: Sequence[Any]) -> tuple[float, ...]:
        return tuple(0.0 for _ in range(self.horizon))

    def _spread_scale(self, bars: Sequence[Any]) -> float:
        if self._reference_sigma <= 0:                   # pragma: no cover
            return 1.0
        return self._sigma(bars) / self._reference_sigma


class RegimeBaseline(PointBaseline):
    """Drift and spread conditioned on Olympus's own regime label.

    Uses `regime.RegimeClassifier`, which Olympus owns end to end — so this
    baseline needs no external data and no licence question, and it is the
    hardest of the stdlib opponents to dismiss: if a native model cannot beat
    "the average return in this regime", it has not learned anything the regime
    classifier did not already know.
    """

    KIND = "regime"

    def __init__(self, *, min_samples: int = 12, **kwargs: Any):
        super().__init__(**kwargs)
        if int(min_samples) < 2:
            raise ConfigurationError("min_samples must be >= 2",
                                     min_samples=min_samples)
        self.min_samples = int(min_samples)
        self._classifier: Any = None
        #: `regime -> (drift per step, spread scale)`. Thin regimes fall back
        #: to the unconditional fit rather than to a noisy estimate.
        self._by_regime: dict[str, tuple[tuple[float, ...], float]] = {}
        self._global_scale = 1.0
        self._observed_regimes: tuple[str, ...] = ()

    @property
    def required_bars(self) -> int:
        return 3

    def _regime_of(self, bars: Sequence[Any]) -> str:
        if self._classifier is None:
            from ..regime import RegimeClassifier
            self._classifier = RegimeClassifier()
        try:
            state = self._classifier.classify(list(bars))
        except Exception:                                # noqa: BLE001
            return Regime.UNKNOWN.value
        value = getattr(state, "regime", Regime.UNKNOWN)
        return value.value if isinstance(value, Regime) else str(value)

    def _prepare(self, windows: Sequence[Window]) -> int:
        grouped: dict[str, list[tuple[float, ...]]] = {}
        for window in windows:
            if len(window.inputs) < self.required_bars:
                continue
            grouped.setdefault(self._regime_of(window.inputs), []).append(
                window.target_log_returns())
        everything = [row for rows in grouped.values() for row in rows]
        if not everything:                               # pragma: no cover
            raise InsufficientDataError("no windows to fit a regime baseline")
        global_spread = statistics.pstdev([row[-1] for row in everything]) \
            if len(everything) > 1 else 0.0

        for label, rows in grouped.items():
            if len(rows) < self.min_samples:
                continue
            drift = tuple(statistics.fmean(row[step] for row in rows)
                          for step in range(self.horizon))
            spread = statistics.pstdev([row[-1] for row in rows]) \
                if len(rows) > 1 else global_spread
            scale = (spread / global_spread) if global_spread > 0 else 1.0
            self._by_regime[label] = (drift, max(scale, 1e-6))
        self._global_scale = 1.0
        self._observed_regimes = tuple(sorted(grouped))
        return 2 * len(self._by_regime) * self.horizon

    def _fit_notes(self) -> str:
        # A single regime across every training window makes this baseline a
        # constant-drift model, which `PointBaseline` explains is exactly
        # persistence once residual quantiles are fitted. Saying so is the
        # difference between a reader seeing a degenerate opponent and a reader
        # seeing a suspiciously identical row.
        observed = getattr(self, "_observed_regimes", ())
        if len(observed) <= 1:
            return (f"degenerate: one regime ({observed[0] if observed else 'none'}) "
                    f"across every training window, so this scores as "
                    f"persistence. The classifier needs more warm-up bars than "
                    f"this lookback provides.")
        return f"regimes fitted: {', '.join(observed)}"

    def _project(self, bars: Sequence[Any]) -> tuple[float, ...]:
        entry = self._by_regime.get(self._regime_of(bars))
        if entry is None:
            return tuple(0.0 for _ in range(self.horizon))
        return entry[0]

    def _spread_scale(self, bars: Sequence[Any]) -> float:
        entry = self._by_regime.get(self._regime_of(bars))
        return self._global_scale if entry is None else entry[1]


# ===========================================================================
# the registry
# ===========================================================================

#: Every baseline, by key. `torch_required` is what `available_baselines`
#: filters on, so a benchmark reports which opponents it could not run rather
#: than quietly comparing against fewer.
BASELINES: Mapping[str, dict] = {
    "persistence": {"factory": PersistenceBaseline, "torch_required": False,
                    "summary": "last value carried forward"},
    "moving_average": {"factory": MovingAverageBaseline, "torch_required": False,
                       "summary": "mean recent log return, extrapolated"},
    "linear_regression": {"factory": LinearRegressionBaseline,
                          "torch_required": False,
                          "summary": "ridge regression on Olympus features"},
    "autoregression": {"factory": AutoregressionBaseline, "torch_required": False,
                       "summary": "AR(p) on log returns, iterated"},
    "gradient_boosted_trees": {"factory": GradientBoostedTreesBaseline,
                               "torch_required": False,
                               "summary": "least-squares boosting on Olympus features"},
    "recurrent": {"factory": RecurrentBaseline, "torch_required": True,
                  "summary": "single-layer GRU"},
    "temporal_transformer": {"factory": TemporalTransformerBaseline,
                             "torch_required": True,
                             "summary": "one causal self-attention block"},
    "volatility": {"factory": VolatilityBaseline, "torch_required": False,
                   "summary": "zero drift, volatility-scaled interval"},
    "regime": {"factory": RegimeBaseline, "torch_required": False,
               "summary": "drift and spread by Olympus regime label"},
}


def available_baselines() -> tuple[str, ...]:
    """Which baselines can run here. Torch-dependent ones drop out silently
    only from this list — never from a benchmark's report."""
    from .torchutil import torch_available
    have_torch = torch_available()
    return tuple(key for key, entry in BASELINES.items()
                 if have_torch or not entry["torch_required"])


def build_baseline(key: str, *, horizon: int,
                   quantiles: Sequence[float] = DEFAULT_QUANTILES,
                   **kwargs: Any) -> WindowModel:
    entry = BASELINES.get(key)
    if entry is None:
        raise ConfigurationError("unknown baseline", baseline=key,
                                 known=sorted(BASELINES))
    return entry["factory"](horizon=horizon, quantiles=quantiles, **kwargs)


__all__ = ["DEFAULT_QUANTILES", "DECLINE_SHORT_WINDOW", "DECLINE_NO_FEATURES",
           "BaselineFitReport", "WindowModel", "PointBaseline",
           "PersistenceBaseline", "MovingAverageBaseline",
           "LinearRegressionBaseline", "AutoregressionBaseline",
           "GradientBoostedTreesBaseline", "RecurrentBaseline",
           "TemporalTransformerBaseline", "VolatilityBaseline", "RegimeBaseline",
           "BASELINES", "available_baselines", "build_baseline"]

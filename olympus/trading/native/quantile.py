"""Direct multi-horizon quantile estimation, conditioned on market state.

The first Olympus-native estimator. Deliberately statistical rather than
neural — not as a placeholder, but because the *shape* is what a later tensor
model has to slot into, and getting the shape right against a model whose every
number can be inspected by hand is worth more than getting a network to
converge early.

The shape, which is the architecture doc's §3.3 in miniature:

    market state ─► discretised cell ─► per-step quantiles of cumulative
                                        log return, in one pass

No autoregression. No sampling. No vocabulary. A neural head that replaces
`predict` keeps every caller, every test and every contract.

How it learns
-------------
Conditioning features are bucketed at *training* quantiles, and each cell holds
the empirical distribution of cumulative log return at every horizon step. A
prediction is a lookup and an interpolation. That is a real model — it can beat
a persistence baseline when conditioning carries information, and it cannot when
it does not, which is the property that makes the comparison worth running.

When it declines
----------------
Three refusals, and the first is the one that matters:

**A thin cell abstains rather than falling back.** Backing off to the
unconditional distribution would mean emitting the *baseline's* answer under
the model's name — claiming a conditional edge that this particular input has
no evidence for. `evaluate.py` would then score the baseline twice and call one
of them a model.

**An out-of-range input abstains.** A feature beyond anything seen in training
is an input the model has no basis for, which is the cheap version of the
out-of-distribution detection §3.11 describes.

**A missing feature abstains.** Never zero-filled; a zero-filled RSI is a real
number a model will happily condition on.

Determinism is total: sorting and interpolation, no RNG anywhere. Two fits on
the same rows produce byte-identical parameters, which is gate G7.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from ..errors import ConfigurationError, InsufficientDataError

#: Quantiles emitted by default. Chosen to match what `ForecastResult` and the
#: risk engine actually read: a median, an inner band for sizing, and an outer
#: band for stop placement.
DEFAULT_QUANTILES: tuple[float, ...] = (0.05, 0.25, 0.50, 0.75, 0.95)

#: Below this many observations a cell is noise wearing a distribution's shape.
#: Matched to `evaluate.MIN_PAIRED_OBSERVATIONS` so the two modules cannot
#: disagree about when a sample means something.
DEFAULT_MIN_CELL_SAMPLES = 30


def _quantile(ordered: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile of an already-sorted sample."""
    if not ordered:
        raise InsufficientDataError("cannot take a quantile of nothing")
    if len(ordered) == 1:
        return float(ordered[0])
    position = q * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return float(ordered[int(position)])
    weight = position - low
    return float(ordered[low] * (1.0 - weight) + ordered[high] * weight)


def quantile_label(q: float) -> str:
    """`0.05 -> "p05"`. Matches `evaluate.quantile_level`'s convention so a
    forecast's quantile keys are readable by the evaluator without translation."""
    percent = q * 100.0
    if abs(percent - round(percent)) < 1e-9:
        return f"p{int(round(percent)):02d}"
    return f"p{percent:g}".replace(".", "_")


@dataclass(frozen=True)
class QuantileConfig:
    """Hyperparameters. Frozen, hashable, and recorded in every manifest."""

    horizon: int
    #: Feature names to condition on, in order. Few and interpretable on
    #: purpose: cells grow as `n_buckets ** len(conditioners)`, and a model
    #: whose cells are empty has learned an index, not a distribution.
    conditioners: tuple[str, ...]
    quantiles: tuple[float, ...] = DEFAULT_QUANTILES
    n_buckets: int = 3
    min_cell_samples: int = DEFAULT_MIN_CELL_SAMPLES
    #: How far outside the training range of a conditioner an input may sit
    #: before the model declines, as a fraction of the training range.
    range_tolerance: float = 0.10

    def __post_init__(self):
        if int(self.horizon) < 1:
            raise ConfigurationError("horizon must be >= 1", horizon=self.horizon)
        object.__setattr__(self, "conditioners", tuple(self.conditioners))
        if not self.conditioners:
            raise ConfigurationError(
                "at least one conditioner is required: with none, this model is "
                "the unconditional distribution and there is already a baseline "
                "for that")
        if len(set(self.conditioners)) != len(self.conditioners):
            raise ConfigurationError("duplicate conditioner",
                                     conditioners=list(self.conditioners))
        quantiles = tuple(sorted(float(q) for q in self.quantiles))
        if not quantiles:
            raise ConfigurationError("at least one quantile is required")
        for q in quantiles:
            if not 0.0 < q < 1.0:
                raise ConfigurationError("quantiles must be in (0, 1)", q=q)
        object.__setattr__(self, "quantiles", quantiles)
        if int(self.n_buckets) < 2:
            raise ConfigurationError("n_buckets must be >= 2",
                                     n_buckets=self.n_buckets)
        if int(self.min_cell_samples) < 1:
            raise ConfigurationError("min_cell_samples must be >= 1")
        if float(self.range_tolerance) < 0:
            raise ConfigurationError("range_tolerance must be >= 0")
        object.__setattr__(self, "horizon", int(self.horizon))
        object.__setattr__(self, "n_buckets", int(self.n_buckets))
        object.__setattr__(self, "min_cell_samples", int(self.min_cell_samples))
        object.__setattr__(self, "range_tolerance", float(self.range_tolerance))

    @property
    def n_cells(self) -> int:
        return self.n_buckets ** len(self.conditioners)

    @property
    def median_index(self) -> int:
        """Index of the quantile closest to 0.5 — the central estimate."""
        return min(range(len(self.quantiles)),
                   key=lambda i: abs(self.quantiles[i] - 0.5))

    def to_dict(self) -> dict:
        return {"horizon": self.horizon,
                "conditioners": list(self.conditioners),
                "quantiles": list(self.quantiles),
                "n_buckets": self.n_buckets,
                "min_cell_samples": self.min_cell_samples,
                "range_tolerance": self.range_tolerance}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "QuantileConfig":
        return cls(horizon=int(data["horizon"]),
                   conditioners=tuple(data["conditioners"]),
                   quantiles=tuple(data.get("quantiles") or DEFAULT_QUANTILES),
                   n_buckets=int(data.get("n_buckets", 3)),
                   min_cell_samples=int(data.get("min_cell_samples",
                                                 DEFAULT_MIN_CELL_SAMPLES)),
                   range_tolerance=float(data.get("range_tolerance", 0.10)))


class Declined(str):
    """Why the model declined. A string subclass so it is readable in a log and
    falsy-checkable at a call site without a second type."""


#: Named reasons, so an abstention can be counted by cause rather than
#: aggregated into "the model said nothing".
DECLINE_NOT_FITTED = Declined("model is not fitted")
#: Fit ran but every cell was too thin to use. Distinct from NOT_FITTED because
#: the two send an operator to different places: one is a wiring mistake, the
#: other is a data or configuration problem the fit report already describes.
DECLINE_NO_USABLE_CELLS = Declined("fit produced no usable cell")
DECLINE_MISSING_FEATURE = Declined("a conditioning feature is unavailable")
DECLINE_OUT_OF_RANGE = Declined("input is outside the training range")
DECLINE_THIN_CELL = Declined("too few training observations for this cell")


@dataclass(frozen=True)
class QuantilePrediction:
    """Per-step quantiles of cumulative log return, plus what produced them."""

    #: `{quantile_label: (step_1, …, step_h)}` in cumulative log-return space.
    log_return_quantiles: Mapping[str, tuple[float, ...]]
    cell: tuple[int, ...]
    cell_samples: int
    median_label: str

    def __post_init__(self):
        object.__setattr__(self, "log_return_quantiles",
                           {k: tuple(v) for k, v
                            in dict(self.log_return_quantiles).items()})

    @property
    def horizon(self) -> int:
        return len(next(iter(self.log_return_quantiles.values())))

    def prices(self, anchor: float) -> dict[str, tuple[float, ...]]:
        """Quantiles converted to price, from an anchor close.

        Exponentiating a *log* return is the whole conversion, and doing it here
        rather than at the call site means no caller can forget and treat a log
        return as a simple one — a 5% error at 5% returns, growing with size.
        """
        if anchor <= 0:
            raise ConfigurationError("anchor must be positive", anchor=anchor)
        return {label: tuple(anchor * math.exp(v) for v in values)
                for label, values in self.log_return_quantiles.items()}

    def median_prices(self, anchor: float) -> tuple[float, ...]:
        return self.prices(anchor)[self.median_label]

    def to_dict(self) -> dict:
        return {"log_return_quantiles":
                    {k: list(v) for k, v in self.log_return_quantiles.items()},
                "cell": list(self.cell), "cell_samples": self.cell_samples,
                "median_label": self.median_label}


@dataclass(frozen=True)
class FitReport:
    """What fitting found. Read before trusting anything the model says."""

    rows: int
    populated_cells: int
    usable_cells: int
    coverage: float
    thin_cells: int
    dropped_rows: int

    @property
    def speaks_often_enough(self) -> bool:
        """Would this model actually answer most of the time?

        A model that declines on 90% of inputs is not necessarily wrong — but it
        is not a forecaster either, and the number belongs in front of whoever
        is about to compare it with a baseline.
        """
        return self.coverage >= 0.5

    def to_dict(self) -> dict:
        return {"rows": self.rows, "populated_cells": self.populated_cells,
                "usable_cells": self.usable_cells, "coverage": self.coverage,
                "thin_cells": self.thin_cells, "dropped_rows": self.dropped_rows,
                "speaks_often_enough": self.speaks_often_enough}


class ConditionalQuantileModel:
    """Fit once, predict many. Deterministic in both directions."""

    KIND = "olympus-conditional-quantile"
    VERSION = "1"

    def __init__(self, config: QuantileConfig):
        self.config = config
        self._edges: dict[str, tuple[float, ...]] = {}
        self._ranges: dict[str, tuple[float, float]] = {}
        #: cell -> step -> per-quantile values
        self._cells: dict[tuple[int, ...], tuple[tuple[float, ...], ...]] = {}
        self._counts: dict[tuple[int, ...], int] = {}
        self._report: FitReport | None = None
        self._fit_called = False

    # -- fitting -----------------------------------------------------------

    @property
    def fitted(self) -> bool:
        """Whether `fit` ran — not whether it produced anything usable.

        Kept separate from `usable` on purpose. A model whose every cell was
        thin *has* been fitted; conflating the two would report "not fitted"
        for a configuration problem and send whoever reads it looking for a
        missing call.
        """
        return self._fit_called

    @property
    def usable(self) -> bool:
        """Whether any cell cleared the sample floor."""
        return bool(self._cells)

    @property
    def report(self) -> FitReport | None:
        return self._report

    def fit(self, rows: Sequence[tuple[Mapping[str, float], Sequence[float]]]
            ) -> FitReport:
        """Learn per-cell quantile tables from `(features, log_returns)` pairs.

        Every statistic — bucket edges, ranges, cell distributions — comes from
        `rows` alone. There is no argument through which test data could reach
        this method, which is how the leakage defence is made structural rather
        than procedural.
        """
        config = self.config
        usable: list[tuple[Mapping[str, float], tuple[float, ...]]] = []
        dropped = 0
        for features, returns in rows:
            values = tuple(float(r) for r in returns)
            if len(values) != config.horizon:
                raise ConfigurationError(
                    "training row horizon does not match the config",
                    expected=config.horizon, got=len(values))
            if any(not math.isfinite(v) for v in values):
                dropped += 1
                continue
            if any(features.get(name) is None
                   or not math.isfinite(float(features[name]))
                   for name in config.conditioners):
                dropped += 1
                continue
            usable.append((features, values))

        if len(usable) < config.min_cell_samples:
            raise InsufficientDataError(
                "not enough usable training rows to fit even one cell",
                usable=len(usable), need=config.min_cell_samples)

        # Bucket edges from the training sample's own quantiles. Equal-width
        # bins on a heavy-tailed feature put nearly every row in one bucket.
        self._edges.clear()
        self._ranges.clear()
        for name in config.conditioners:
            column = sorted(float(f[name]) for f, _ in usable)
            self._ranges[name] = (column[0], column[-1])
            edges = tuple(_quantile(column, i / config.n_buckets)
                          for i in range(1, config.n_buckets))
            self._edges[name] = edges

        buckets: dict[tuple[int, ...], list[tuple[float, ...]]] = {}
        for features, values in usable:
            cell = self._cell_for(features)
            if cell is None:                             # pragma: no cover
                dropped += 1
                continue
            buckets.setdefault(cell, []).append(values)

        self._cells.clear()
        self._counts.clear()
        usable_cells = 0
        covered_rows = 0
        for cell, samples in buckets.items():
            self._counts[cell] = len(samples)
            if len(samples) < config.min_cell_samples:
                continue
            per_step = []
            for step in range(config.horizon):
                column = sorted(sample[step] for sample in samples)
                per_step.append(tuple(_quantile(column, q)
                                      for q in config.quantiles))
            self._cells[cell] = tuple(per_step)
            usable_cells += 1
            covered_rows += len(samples)

        self._fit_called = True
        self._report = FitReport(
            rows=len(usable), populated_cells=len(buckets),
            usable_cells=usable_cells,
            coverage=covered_rows / len(usable) if usable else 0.0,
            thin_cells=len(buckets) - usable_cells, dropped_rows=dropped)
        return self._report

    # -- prediction --------------------------------------------------------

    def _cell_for(self, features: Mapping[str, float]) -> tuple[int, ...] | None:
        cell = []
        for name in self.config.conditioners:
            value = features.get(name)
            if value is None:
                return None
            number = float(value)
            if not math.isfinite(number):
                return None
            edges = self._edges.get(name, ())
            index = 0
            for edge in edges:
                if number > edge:
                    index += 1
            cell.append(index)
        return tuple(cell)

    def _in_range(self, features: Mapping[str, float]) -> bool:
        for name in self.config.conditioners:
            low, high = self._ranges.get(name, (0.0, 0.0))
            value = float(features[name])
            span = high - low
            margin = abs(span) * self.config.range_tolerance
            if span == 0.0:
                # A constant training column tells us nothing about tolerance,
                # so anything different from it is out of range.
                if value != low:
                    return False
                continue
            if value < low - margin or value > high + margin:
                return False
        return True

    def predict(self, features: Mapping[str, float]
                ) -> QuantilePrediction | Declined:
        """Per-step quantiles, or a named reason for declining."""
        if not self.fitted:
            return DECLINE_NOT_FITTED
        if not self.usable:
            return DECLINE_NO_USABLE_CELLS
        for name in self.config.conditioners:
            value = features.get(name)
            if value is None or not math.isfinite(float(value)):
                return DECLINE_MISSING_FEATURE
        if not self._in_range(features):
            return DECLINE_OUT_OF_RANGE
        cell = self._cell_for(features)
        if cell is None:                                 # pragma: no cover
            return DECLINE_MISSING_FEATURE
        table = self._cells.get(cell)
        if table is None:
            return DECLINE_THIN_CELL

        labels = [quantile_label(q) for q in self.config.quantiles]
        by_label = {label: tuple(table[step][i]
                                 for step in range(self.config.horizon))
                    for i, label in enumerate(labels)}
        return QuantilePrediction(
            log_return_quantiles=by_label, cell=cell,
            cell_samples=self._counts.get(cell, 0),
            median_label=labels[self.config.median_index])

    # -- persistence -------------------------------------------------------

    def to_parameters(self) -> dict:
        """Learned parameters as JSON. Small enough to read by eye."""
        if not self.usable:
            raise ConfigurationError(
                "an unfitted model has no parameters to save")
        return {
            "kind": self.KIND, "version": self.VERSION,
            "config": self.config.to_dict(),
            "edges": {name: list(edges) for name, edges in sorted(self._edges.items())},
            "ranges": {name: list(bounds) for name, bounds in sorted(self._ranges.items())},
            "cells": {"|".join(map(str, cell)): [list(step) for step in table]
                      for cell, table in sorted(self._cells.items())},
            "counts": {"|".join(map(str, cell)): count
                       for cell, count in sorted(self._counts.items())},
            "report": self._report.to_dict() if self._report else None,
        }

    @classmethod
    def from_parameters(cls, parameters: Mapping[str, Any]
                        ) -> "ConditionalQuantileModel":
        kind = parameters.get("kind")
        if kind != cls.KIND:
            raise ConfigurationError(
                "these parameters were not produced by this model",
                expected=cls.KIND, got=kind)
        model = cls(QuantileConfig.from_dict(parameters["config"]))
        model._edges = {name: tuple(float(e) for e in edges)
                        for name, edges in (parameters.get("edges") or {}).items()}
        model._ranges = {name: (float(bounds[0]), float(bounds[1]))
                         for name, bounds in (parameters.get("ranges") or {}).items()}
        model._cells = {
            tuple(int(p) for p in key.split("|")):
                tuple(tuple(float(v) for v in step) for step in table)
            for key, table in (parameters.get("cells") or {}).items()}
        model._counts = {tuple(int(p) for p in key.split("|")): int(count)
                         for key, count in (parameters.get("counts") or {}).items()}
        model._fit_called = True
        report = parameters.get("report")
        if report:
            model._report = FitReport(
                rows=int(report["rows"]),
                populated_cells=int(report["populated_cells"]),
                usable_cells=int(report["usable_cells"]),
                coverage=float(report["coverage"]),
                thin_cells=int(report["thin_cells"]),
                dropped_rows=int(report["dropped_rows"]))
        return model

    def __repr__(self) -> str:                          # pragma: no cover
        state = ("usable" if self.usable
                 else "fitted-but-empty" if self.fitted else "unfitted")
        return (f"ConditionalQuantileModel({state}, "
                f"cells={len(self._cells)}/{self.config.n_cells}, "
                f"h={self.config.horizon})")


__all__ = ["DEFAULT_QUANTILES", "DEFAULT_MIN_CELL_SAMPLES", "quantile_label",
           "QuantileConfig", "Declined", "DECLINE_NOT_FITTED",
           "DECLINE_NO_USABLE_CELLS", "DECLINE_MISSING_FEATURE",
           "DECLINE_OUT_OF_RANGE",
           "DECLINE_THIN_CELL", "QuantilePrediction", "FitReport",
           "ConditionalQuantileModel"]

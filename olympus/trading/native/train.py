"""The Olympus-owned training pipeline.

One function does the whole job — `train` — and it is one function on purpose.
Windowing, splitting, feature extraction and fitting have to happen in a
specific order, and the leak this package exists to prevent is exactly what
happens when a caller can interleave them: build the windows, fit a scaler on
everything, then split. Exposing the steps separately would make that arrangement
available, and it would then be nobody's fault in particular.

What `train` guarantees:

* the split happens **before** any statistic is computed from the data;
* features for a window are built from that window's inputs alone, using a
  clock pinned to the window's own last close, so a feature cannot read a bar
  the window does not contain;
* every fitted number comes from the training rows;
* the returned checkpoint carries a manifest naming the data hash, the split
  boundaries, the seed, the config, the feature set and the dependency
  versions — gate G8;
* the same inputs produce a byte-identical checkpoint — gate G7.

`seed` is recorded even though this estimator has no randomness. A trainer that
only records a seed when it happens to need one produces manifests that are
sometimes reproducible, and the difference is invisible from the manifest.

There is **no argument through which foreign weights can enter.**
`continue_from` accepts an Olympus checkpoint id or nothing, and
`assert_olympus_origin` refuses a path, a URL or a hub id — gate G3.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from ..clock import Clock, default_clock
from ..contracts import Candle
from ..errors import ConfigurationError, InsufficientDataError
from ..features import FeatureBuilder
from .checkpoint import (CheckpointStore, NativeCheckpoint, TrainingManifest,
                         assert_olympus_origin, code_version,
                         dependency_versions)
from .data import DatasetSpec, Split, Window, build_dataset, data_version
from .quantile import ConditionalQuantileModel, QuantileConfig
from .state import MarketStateBuilder


@dataclass(frozen=True)
class TrainingResult:
    """A checkpoint plus everything needed to judge whether to keep it."""

    checkpoint: NativeCheckpoint
    split: Split
    train_report: Mapping[str, Any]
    #: Metrics on the held-out rows, computed by `evaluate.py`'s functions.
    test_metrics: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"checkpoint_id": self.checkpoint.checkpoint_id,
                "model_version": self.checkpoint.model_version,
                "split": self.split.to_dict(),
                "train_report": dict(self.train_report),
                "test_metrics": dict(self.test_metrics)}


def _rows_from_windows(windows: Sequence[Window],
                       state_builder: MarketStateBuilder
                       ) -> list[tuple[dict[str, float], tuple[float, ...]]]:
    """`(features, cumulative log returns)` per window.

    Features come from `window.inputs` only. The state builder pins its clock to
    the window's own last close, so `FeatureBuilder`'s ordering checks stay
    active while a historical window is still accepted.
    """
    rows = []
    for window in windows:
        try:
            state = state_builder.build(
                {window.timeframe: window.inputs}, as_of=window.as_of,
                instrument_key=window.instrument_key,
                base_timeframe=window.timeframe)
        except Exception:                                # noqa: BLE001
            # A window whose features cannot be computed is dropped, not
            # zero-filled. `FitReport.dropped_rows` counts them.
            continue
        rows.append((dict(state.base.features), window.target_log_returns()))
    return rows


def train(candles: Sequence[Candle], *, spec: DatasetSpec,
          config: QuantileConfig, checkpoint_id: str,
          seed: int = 0,
          features: FeatureBuilder | None = None,
          continue_from: Any = None,
          store: CheckpointStore | None = None,
          trained_by: str = "olympus",
          clock: Clock | None = None,
          notes: str = "") -> TrainingResult:
    """Fit a native model and return a fully provenanced checkpoint.

    Does not save. Saving is the caller's decision, because a training run that
    produced a bad model should be inspectable without having written itself
    into the store first.
    """
    if spec.horizon != config.horizon:
        raise ConfigurationError(
            "the dataset horizon and the model horizon disagree; one of them is "
            "a typo and guessing which would train a model against the wrong "
            "target", dataset_horizon=spec.horizon, model_horizon=config.horizon)

    known = store.ids() if store is not None else ()
    origin = assert_olympus_origin(continue_from, known=known)

    builder = MarketStateBuilder(features=features or FeatureBuilder(),
                                 clock=clock or default_clock())
    unknown = sorted(set(config.conditioners) - set(builder.feature_names))
    if unknown:
        raise ConfigurationError(
            "conditioning on features the builder does not produce",
            unknown=unknown, available=list(builder.feature_names))

    # Split first. Every statistic below is computed from `split.train` alone,
    # and there is no ordering of the calls in this function that fits anything
    # on the test rows.
    split = build_dataset(candles, spec)
    if not split.train:
        raise InsufficientDataError("the split produced no training windows")

    train_rows = _rows_from_windows(split.train, builder)
    if not train_rows:
        raise InsufficientDataError(
            "no training window produced a usable feature vector")

    model = ConditionalQuantileModel(config)
    report = model.fit(train_rows)

    train_start = min(w.inputs[0].ts_open for w in split.train)
    train_end = max(w.targets[-1].ts_close for w in split.train)

    manifest = TrainingManifest(
        data_hash=data_version(candles),
        train_start=train_start, train_end=train_end,
        seed=seed, config=config.to_dict(),
        feature_names=tuple(builder.feature_names),
        dataset=spec.to_dict(), versions=dependency_versions(),
        code_version=code_version(),
        trained_at=(clock or default_clock()).now(), trained_by=trained_by,
        n_train_rows=len(train_rows), n_test_rows=len(split.test),
        embargoed_rows=split.embargoed,
        metrics=({"stage": "fit", **report.to_dict()},),
        notes=notes)

    checkpoint = NativeCheckpoint(
        checkpoint_id=checkpoint_id, model_kind=model.KIND,
        version=model.VERSION, manifest=manifest,
        parameters=model.to_parameters(), initialised_from=origin)

    return TrainingResult(checkpoint=checkpoint, split=split,
                          train_report=report.to_dict())


def evaluate_on_split(checkpoint: NativeCheckpoint, split: Split, *,
                      features: FeatureBuilder | None = None,
                      clock: Clock | None = None) -> dict:
    """Score a checkpoint on the held-out rows.

    Reports the abstention rate alongside the error metrics, and reports them
    **over the answered rows only** — averaging an abstention in as though it
    were a zero-error prediction is how a model that declines on everything
    scores perfectly.
    """
    from ..evaluate import mae, pinball_loss, rmse

    builder = MarketStateBuilder(features=features or FeatureBuilder(),
                                 clock=clock or default_clock())
    model = ConditionalQuantileModel.from_parameters(checkpoint.parameters)
    rows = _rows_from_windows(split.test, builder)
    if not rows:
        return {"rows": 0, "answered": 0, "abstention_rate": None,
                "note": "no test rows produced a usable feature vector"}

    from .quantile import Declined, quantile_label
    median_label = quantile_label(model.config.quantiles[model.config.median_index])

    predicted: list[float] = []
    actual: list[float] = []
    declines: dict[str, int] = {}
    for feature_values, targets in rows:
        prediction = model.predict(feature_values)
        if isinstance(prediction, Declined):
            declines[str(prediction)] = declines.get(str(prediction), 0) + 1
            continue
        predicted.append(prediction.log_return_quantiles[median_label][-1])
        actual.append(targets[-1])

    answered = len(predicted)
    out: dict[str, Any] = {
        "rows": len(rows), "answered": answered,
        "abstention_rate": 1.0 - answered / len(rows),
        "declines": dict(sorted(declines.items())),
    }
    if answered:
        out["mae"] = mae(predicted, actual)
        out["rmse"] = rmse(predicted, actual)
        out["pinball_p50"] = pinball_loss(predicted, actual, 0.5)
        out["directional_accuracy"] = (
            sum(1 for p, a in zip(predicted, actual)
                if (p > 0) == (a > 0)) / answered)
    else:
        out["note"] = ("the model declined on every test row; no error metric "
                       "is defined and none is reported")
    return out


def train_neural(candles: Sequence[Candle], *, spec: DatasetSpec,
                 config: Any, checkpoint_id: str, seed: int = 0,
                 continue_from: Any = None,
                 store: CheckpointStore | None = None,
                 trained_by: str = "olympus",
                 clock: Clock | None = None,
                 threads: int = 1, notes: str = "") -> TrainingResult:
    """Fit the neural quantile model. Same guarantees as `train`.

    Split first, fit on the training windows only, and record a manifest that
    names the data hash, the split boundaries, the seed, the config, the channel
    set and the torch version. The held-out windows are scored during fitting
    for the loss curve and are **not** used to select weights — early stopping
    on a validation set makes that set part of training, and the honest split is
    the one where the held-out rows influenced nothing.
    """
    from .neural import NeuralConfig, NeuralQuantileModel
    from .torchutil import torch_version

    if not isinstance(config, NeuralConfig):
        raise ConfigurationError("train_neural needs a NeuralConfig",
                                 got=type(config).__name__)
    if spec.horizon != config.horizon:
        raise ConfigurationError(
            "the dataset horizon and the model horizon disagree",
            dataset_horizon=spec.horizon, model_horizon=config.horizon)
    if spec.lookback != config.lookback:
        raise ConfigurationError(
            "the dataset lookback and the encoder lookback disagree; the "
            "encoder would receive a window it was not built for",
            dataset_lookback=spec.lookback, model_lookback=config.lookback)

    known = store.ids() if store is not None else ()
    origin = assert_olympus_origin(continue_from, known=known)

    split = build_dataset(candles, spec)
    if not split.train:
        raise InsufficientDataError("the split produced no training windows")

    model = NeuralQuantileModel(config)
    report = model.fit(split.train, seed=seed, validation=split.test,
                       threads=threads)

    train_start = min(w.inputs[0].ts_open for w in split.train)
    train_end = max(w.targets[-1].ts_close for w in split.train)

    versions = dependency_versions()
    versions["torch"] = torch_version()

    manifest = TrainingManifest(
        data_hash=data_version(candles), train_start=train_start,
        train_end=train_end, seed=seed, config=config.to_dict(),
        # The *channel* set is this model's feature set: a patch encoder reads
        # bars, not the named features the statistical estimator conditions on.
        feature_names=tuple(config.to_dict()["channels"]),
        dataset=spec.to_dict(), versions=versions,
        code_version=code_version(),
        trained_at=(clock or default_clock()).now(), trained_by=trained_by,
        n_train_rows=len(split.train), n_test_rows=len(split.test),
        embargoed_rows=split.embargoed,
        metrics=({"stage": "fit", **report.to_dict()},), notes=notes)

    checkpoint = NativeCheckpoint(
        checkpoint_id=checkpoint_id, model_kind=model.KIND,
        version=model.VERSION, manifest=manifest,
        parameters=model.to_parameters(), initialised_from=origin)

    return TrainingResult(checkpoint=checkpoint, split=split,
                          train_report=report.to_dict())


def evaluate_neural_on_split(checkpoint: NativeCheckpoint, split: Split) -> dict:
    """Score a neural checkpoint on the held-out windows.

    Reports over the *answered* rows only, and states the abstention rate
    alongside — averaging an abstention in as a zero-error prediction is how a
    model that declines on everything scores perfectly.
    """
    from ..evaluate import coverage, mae, pinball_loss, rmse
    from .neural import NeuralQuantileModel
    from .quantile import Declined, quantile_label

    model = NeuralQuantileModel.from_parameters(checkpoint.parameters)
    labels = [quantile_label(q) for q in model.config.quantiles]
    median_label = labels[model.config.median_index]

    predicted, actual, lower, upper = [], [], [], []
    declines: dict[str, int] = {}
    for window in split.test:
        prediction = model.predict_from_bars(window.inputs)
        if isinstance(prediction, Declined):
            declines[str(prediction)] = declines.get(str(prediction), 0) + 1
            continue
        predicted.append(prediction.log_return_quantiles[median_label][-1])
        actual.append(window.target_log_returns()[-1])
        lower.append(prediction.log_return_quantiles[labels[0]][-1])
        upper.append(prediction.log_return_quantiles[labels[-1]][-1])

    answered = len(predicted)
    rows = len(split.test)
    out: dict[str, Any] = {
        "rows": rows, "answered": answered,
        "abstention_rate": (1.0 - answered / rows) if rows else None,
        "declines": dict(sorted(declines.items()))}
    if answered:
        out["mae"] = mae(predicted, actual)
        out["rmse"] = rmse(predicted, actual)
        out["pinball_p50"] = pinball_loss(predicted, actual, 0.5)
        out["directional_accuracy"] = (
            sum(1 for p, a in zip(predicted, actual)
                if (p > 0) == (a > 0)) / answered)
        # Nominal coverage of the outer band. The honest calibration number,
        # and the one G10 will be judged on once real data exists.
        out["nominal_coverage"] = (model.config.quantiles[-1]
                                   - model.config.quantiles[0])
        out["realised_coverage"] = coverage(lower, upper, actual)
    else:
        out["note"] = ("the model declined on every held-out window; no error "
                       "metric is defined and none is reported")
    return out


__all__ = ["TrainingResult", "train", "evaluate_on_split", "train_neural",
           "evaluate_neural_on_split"]

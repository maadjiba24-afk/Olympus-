"""The neural quantile model: encoder + trunk + head, behind the same contract.

`NeuralQuantileModel.predict` returns a `QuantilePrediction` or a `Declined`,
exactly as `ConditionalQuantileModel` does. That is the point of P2 having come
first: the statistical estimator established the shape, and swapping in a
network changes no caller, no test of the surrounding domain, and no contract.

What is different from the statistical estimator
------------------------------------------------
It consumes **bars**, not a feature dictionary. A patch encoder reads the shape
of a window; a lookup table reads a handful of summary numbers. Both answer the
same question, so `predict_from_bars` is the neural entry point and `predict`
adapts the feature-dict signature by declining — a network asked for a
prediction from summary features alone has not been given its input.

When it declines
----------------
Two refusals, and neither is a fallback:

**Too few bars.** The encoder needs a whole number of patches; a short window
would have to be padded, and padding the *oldest* bars with zeros teaches the
model that history began with a flat market.

**Out of distribution.** The fitted model records the training range of the
normalised channel statistics. A window whose volatility sits far outside
anything seen in training is one the model has no basis for. This is the same
position the statistical estimator takes and, like it, it is the cheap version
of the detector §3.11 describes — an honest range check, not conformal coverage.

There is deliberately **no confidence-from-nowhere path**: a network will emit
numbers for any input, and the only defence against that is refusing inputs.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..errors import ConfigurationError, InsufficientDataError
from .encoder import (CHANNELS, EncoderConfig, bars_to_channels, build_encoder,
                      normalise_window)
from .quantile import (DECLINE_MISSING_FEATURE, DECLINE_NOT_FITTED,
                       DECLINE_OUT_OF_RANGE, Declined, QuantilePrediction,
                       quantile_label)
from .torchutil import (configure_determinism, json_to_state_dict,
                        require_torch, state_dict_to_json, torch_version)
from .trunk import TrunkConfig, build_trunk, pinball_loss

#: Declined when the window is shorter than the encoder's patch grid.
DECLINE_SHORT_WINDOW = Declined("window is shorter than the encoder needs")


@dataclass(frozen=True)
class NeuralConfig:
    """Everything that determines the numbers. Recorded in the manifest."""

    lookback: int
    horizon: int
    patch_len: int = 4
    d_model: int = 32
    kernel_size: int = 2
    n_layers: int = 3
    quantiles: tuple[float, ...] = (0.05, 0.25, 0.50, 0.75, 0.95)
    dropout: float = 0.0
    #: Training.
    epochs: int = 60
    batch_size: int = 64
    learning_rate: float = 1e-2
    weight_decay: float = 0.0
    #: How far outside the training range of the window's own volatility an
    #: input may sit before the model declines, as a fraction of that range.
    range_tolerance: float = 0.25

    def __post_init__(self):
        quantiles = tuple(sorted(float(q) for q in self.quantiles))
        if len(quantiles) < 2:
            raise ConfigurationError(
                "at least two quantiles are needed: with one there is no "
                "interval, and the whole point of this head is the interval")
        for q in quantiles:
            if not 0.0 < q < 1.0:
                raise ConfigurationError("quantiles must be in (0, 1)", q=q)
        object.__setattr__(self, "quantiles", quantiles)
        for name in ("lookback", "horizon", "patch_len", "d_model",
                     "kernel_size", "n_layers", "epochs", "batch_size"):
            if int(getattr(self, name)) < 1:
                raise ConfigurationError(f"{name} must be >= 1",
                                         **{name: getattr(self, name)})
            object.__setattr__(self, name, int(getattr(self, name)))
        if float(self.learning_rate) <= 0:
            raise ConfigurationError("learning_rate must be > 0")

    @property
    def encoder(self) -> EncoderConfig:
        # The encoder sees one fewer bar than the lookback, because the first
        # bar has no previous close to return against.
        return EncoderConfig(lookback=self.lookback - 1,
                             patch_len=self.patch_len, d_model=self.d_model)

    @property
    def trunk(self) -> TrunkConfig:
        return TrunkConfig(d_model=self.d_model, kernel_size=self.kernel_size,
                           n_layers=self.n_layers, horizon=self.horizon,
                           n_quantiles=len(self.quantiles),
                           dropout=self.dropout)

    @property
    def median_index(self) -> int:
        return min(range(len(self.quantiles)),
                   key=lambda i: abs(self.quantiles[i] - 0.5))

    def to_dict(self) -> dict:
        return {"lookback": self.lookback, "horizon": self.horizon,
                "patch_len": self.patch_len, "d_model": self.d_model,
                "kernel_size": self.kernel_size, "n_layers": self.n_layers,
                "quantiles": list(self.quantiles), "dropout": self.dropout,
                "epochs": self.epochs, "batch_size": self.batch_size,
                "learning_rate": self.learning_rate,
                "weight_decay": self.weight_decay,
                "range_tolerance": self.range_tolerance,
                "channels": list(CHANNELS),
                "encoder": self.encoder.to_dict(),
                "trunk": self.trunk.to_dict()}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "NeuralConfig":
        return cls(lookback=int(data["lookback"]), horizon=int(data["horizon"]),
                   patch_len=int(data.get("patch_len", 4)),
                   d_model=int(data.get("d_model", 32)),
                   kernel_size=int(data.get("kernel_size", 2)),
                   n_layers=int(data.get("n_layers", 3)),
                   quantiles=tuple(data.get("quantiles")
                                   or (0.05, 0.25, 0.50, 0.75, 0.95)),
                   dropout=float(data.get("dropout", 0.0)),
                   epochs=int(data.get("epochs", 60)),
                   batch_size=int(data.get("batch_size", 64)),
                   learning_rate=float(data.get("learning_rate", 1e-2)),
                   weight_decay=float(data.get("weight_decay", 0.0)),
                   range_tolerance=float(data.get("range_tolerance", 0.25)))


@dataclass(frozen=True)
class NeuralFitReport:
    """The loss trajectory, kept whole.

    A final number with no trajectory hides a model that was better twenty
    epochs ago, and `best_epoch` is what says whether the last epoch was the
    right one to keep.
    """

    epochs: int
    rows: int
    train_loss: tuple[float, ...]
    validation_loss: tuple[float, ...] = ()
    parameters: int = 0
    seconds: float = 0.0

    @property
    def final_loss(self) -> float | None:
        return self.train_loss[-1] if self.train_loss else None

    @property
    def best_epoch(self) -> int | None:
        series = self.validation_loss or self.train_loss
        if not series:
            return None
        return min(range(len(series)), key=lambda i: series[i])

    @property
    def converged(self) -> bool:
        """Did the loss actually come down, and settle?

        Both halves matter. A loss that fell and is still falling has not
        converged, it ran out of epochs — and reporting that as convergence is
        how an under-trained model gets compared with a fitted one.
        """
        series = self.train_loss
        if len(series) < 10:
            return False
        if not series[-1] < series[0]:
            return False
        tail = series[-5:]
        spread = max(tail) - min(tail)
        return spread <= abs(series[0] - series[-1]) * 0.1

    def to_dict(self) -> dict:
        return {"epochs": self.epochs, "rows": self.rows,
                "train_loss": list(self.train_loss),
                "validation_loss": list(self.validation_loss),
                "parameters": self.parameters, "seconds": self.seconds,
                "final_loss": self.final_loss, "best_epoch": self.best_epoch,
                "converged": self.converged}


class NeuralQuantileModel:
    """Encoder + causal trunk + monotone quantile head."""

    KIND = "olympus-neural-quantile"
    VERSION = "1"

    def __init__(self, config: NeuralConfig):
        self.config = config
        self._encoder = None
        self._trunk = None
        self._report: NeuralFitReport | None = None
        #: Training range of the window volatility, for the OOD check.
        self._volatility_range: tuple[float, float] | None = None
        self._fit_called = False

    # -- construction ------------------------------------------------------

    def _build(self):
        if self._encoder is None:
            self._encoder = build_encoder(self.config.encoder)
            self._trunk = build_trunk(self.config.trunk)
        return self._encoder, self._trunk

    @property
    def fitted(self) -> bool:
        return self._fit_called

    @property
    def usable(self) -> bool:
        return self._fit_called and self._encoder is not None

    @property
    def report(self) -> NeuralFitReport | None:
        return self._report

    def n_parameters(self) -> int:
        encoder, trunk = self._build()
        return sum(p.numel() for p in encoder.parameters()) + \
            sum(p.numel() for p in trunk.parameters())

    # -- tensors -----------------------------------------------------------

    def _window_tensor(self, bars: Sequence[Any]):
        """One window → `[1, channels, time]`, plus its volatility."""
        torch = require_torch()
        channels = bars_to_channels(bars)
        normalised, stats = normalise_window(channels)
        volatility = stats[0][1]                          # log-return sigma
        tensor = torch.tensor(normalised, dtype=torch.float32).unsqueeze(0)
        return tensor, volatility

    def _batch_tensor(self, windows: Sequence[Sequence[Any]]):
        torch = require_torch()
        rows, volatilities = [], []
        for bars in windows:
            channels = bars_to_channels(bars)
            normalised, stats = normalise_window(channels)
            rows.append(normalised)
            volatilities.append(stats[0][1])
        return (torch.tensor(rows, dtype=torch.float32), volatilities)

    # -- fitting -----------------------------------------------------------

    def fit(self, windows: Sequence[Any], *, seed: int = 0,
            validation: Sequence[Any] = (), threads: int = 1
            ) -> NeuralFitReport:
        """Fit on `data.Window`s. Deterministic given `seed`.

        Everything fitted comes from `windows`. `validation` is scored but never
        back-propagated, and it is *not* used to select the returned weights —
        early stopping on a validation set makes that set part of training, and
        the honest split is the one where the held-out rows influenced nothing.
        """
        import time
        torch = require_torch()
        if not windows:
            raise InsufficientDataError("no training windows")

        generator = configure_determinism(seed, threads=threads)
        encoder, trunk = self._build()
        encoder.train()
        trunk.train()

        inputs = [w.inputs for w in windows]
        if any(len(w.inputs) != self.config.lookback for w in windows):
            raise ConfigurationError(
                "a training window does not match the configured lookback",
                lookback=self.config.lookback,
                got=sorted({len(w.inputs) for w in windows}))
        x, volatilities = self._batch_tensor(inputs)
        y = torch.tensor([list(w.target_log_returns()) for w in windows],
                         dtype=torch.float32)
        if y.shape[1] != self.config.horizon:
            raise ConfigurationError("window horizon does not match the config",
                                     expected=self.config.horizon,
                                     got=int(y.shape[1]))
        self._volatility_range = (min(volatilities), max(volatilities))

        validation_x = validation_y = None
        if validation:
            validation_x, _ = self._batch_tensor([w.inputs for w in validation])
            validation_y = torch.tensor(
                [list(w.target_log_returns()) for w in validation],
                dtype=torch.float32)

        parameters = list(encoder.parameters()) + list(trunk.parameters())
        optimiser = torch.optim.Adam(parameters, lr=self.config.learning_rate,
                                     weight_decay=self.config.weight_decay)
        levels = self.config.quantiles
        n = x.shape[0]
        batch = min(self.config.batch_size, n)

        train_curve, validation_curve = [], []
        started = time.monotonic()
        for _ in range(self.config.epochs):
            order = torch.randperm(n, generator=generator)
            total, seen = 0.0, 0
            for start in range(0, n, batch):
                index = order[start:start + batch]
                optimiser.zero_grad()
                predicted = trunk(encoder(x[index]))
                loss = pinball_loss(predicted, y[index], levels)
                loss.backward()
                optimiser.step()
                total += float(loss.item()) * len(index)
                seen += len(index)
            train_curve.append(total / max(1, seen))

            if validation_x is not None:
                encoder.eval()
                trunk.eval()
                with torch.no_grad():
                    validation_curve.append(float(pinball_loss(
                        trunk(encoder(validation_x)), validation_y,
                        levels).item()))
                encoder.train()
                trunk.train()

        encoder.eval()
        trunk.eval()
        self._fit_called = True
        self._report = NeuralFitReport(
            epochs=self.config.epochs, rows=n,
            train_loss=tuple(train_curve),
            validation_loss=tuple(validation_curve),
            parameters=self.n_parameters(),
            seconds=time.monotonic() - started)
        return self._report

    # -- prediction --------------------------------------------------------

    def predict_from_bars(self, bars: Sequence[Any]
                          ) -> QuantilePrediction | Declined:
        """Per-step quantiles of cumulative log return from a bar window."""
        if not self.usable:
            return DECLINE_NOT_FITTED
        if len(bars) != self.config.lookback:
            return DECLINE_SHORT_WINDOW

        torch = require_torch()
        try:
            x, volatility = self._window_tensor(bars)
        except ConfigurationError:
            return DECLINE_MISSING_FEATURE

        if self._volatility_range is not None:
            low, high = self._volatility_range
            span = high - low
            margin = abs(span) * self.config.range_tolerance
            if volatility < low - margin or volatility > high + margin:
                return DECLINE_OUT_OF_RANGE

        encoder, trunk = self._build()
        with torch.no_grad():
            output = trunk(encoder(x))[0]                 # [horizon, quantiles]

        labels = [quantile_label(q) for q in self.config.quantiles]
        by_label = {label: tuple(float(output[step][i])
                                 for step in range(self.config.horizon))
                    for i, label in enumerate(labels)}
        return QuantilePrediction(
            log_return_quantiles=by_label, cell=(), cell_samples=0,
            median_label=labels[self.config.median_index])

    def predict(self, features: Mapping[str, float]
                ) -> QuantilePrediction | Declined:
        """Feature-dict entry point, for contract compatibility.

        Declines. A patch encoder reads the *shape* of a window, and a handful
        of summary features is not that input — answering anyway would be
        producing a number from something the model was never shown.
        """
        return DECLINE_MISSING_FEATURE

    # -- persistence -------------------------------------------------------

    def to_parameters(self) -> dict:
        if not self.usable:
            raise ConfigurationError("an unfitted model has no parameters")
        encoder, trunk = self._build()
        return {"kind": self.KIND, "version": self.VERSION,
                "config": self.config.to_dict(),
                "torch_version": torch_version(),
                "volatility_range": list(self._volatility_range or ()),
                "encoder_weights": state_dict_to_json(encoder),
                "trunk_weights": state_dict_to_json(trunk),
                "report": self._report.to_dict() if self._report else None}

    @classmethod
    def from_parameters(cls, parameters: Mapping[str, Any]
                        ) -> "NeuralQuantileModel":
        if parameters.get("kind") != cls.KIND:
            raise ConfigurationError(
                "these parameters were not produced by this model",
                expected=cls.KIND, got=parameters.get("kind"))
        model = cls(NeuralConfig.from_dict(parameters["config"]))
        encoder, trunk = model._build()
        json_to_state_dict(parameters["encoder_weights"], encoder)
        json_to_state_dict(parameters["trunk_weights"], trunk)
        encoder.eval()
        trunk.eval()
        bounds = parameters.get("volatility_range") or ()
        model._volatility_range = (float(bounds[0]), float(bounds[1])) \
            if len(bounds) == 2 else None
        model._fit_called = True
        report = parameters.get("report")
        if report:
            model._report = NeuralFitReport(
                epochs=int(report["epochs"]), rows=int(report["rows"]),
                train_loss=tuple(report.get("train_loss") or ()),
                validation_loss=tuple(report.get("validation_loss") or ()),
                parameters=int(report.get("parameters", 0)),
                seconds=float(report.get("seconds", 0.0)))
        return model

    def __repr__(self) -> str:                          # pragma: no cover
        state = "fitted" if self.usable else "unfitted"
        return (f"NeuralQuantileModel({state}, lookback="
                f"{self.config.lookback}, h={self.config.horizon})")


__all__ = ["DECLINE_SHORT_WINDOW", "NeuralConfig", "NeuralFitReport",
           "NeuralQuantileModel"]

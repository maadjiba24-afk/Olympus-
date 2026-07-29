"""Inference: checkpoint in, `NativeForecast` out, abstention wired in.

Training and serving are separated because they fail differently. A trainer
that cannot find a target should stop the run; a server that cannot find a
channel should decline the forecast and keep running. Putting both in one class
means one of those two behaviours is wrong.

The order of operations here is the design
------------------------------------------
1. **Verify the checkpoint before loading it.** The content hash recomputes or
   nothing else is trusted — including the thresholds the abstention policy
   would use. `abstain.CHECK_ORDER` puts the same check first for the same
   reason.
2. **Check the request against the checkpoint** — horizon, architecture
   version, task schema. A mismatch is refused before any tensor is allocated.
3. **Run the model once.** Every task head, one forward pass. Multi-task
   inference that ran a pass per head would be paying the encoder's cost N
   times for outputs that share it.
4. **Derive what is derived.** Price quantiles from return quantiles and the
   anchor; OOD from reconstruction error against ranges recorded at fit time;
   calibration from what was measured on a held-out split.
5. **Decide.** All nine abstention checks, on measured values.
6. **Assemble.** Only the tasks this checkpoint actually has go into
   `tasks_present`; everything else stays `None`.

Latency and memory are measured, not estimated
----------------------------------------------
`InferenceStats` records wall time per call and peak resident memory around the
call, because "efficient inference" is a claim and the evaluation asks for the
number. The memory reading uses `resource.getrusage` where available and says
so when it is not, rather than reporting a zero that looks like a measurement.
"""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from ..clock import Clock, default_clock
from ..contracts import Candle, DataQuality, ensure_utc
from ..errors import ConfigurationError
from ..instruments import parse_timeframe
from .abstain import AbstentionPolicy, AbstentionThresholds
from .checkpoint import NativeCheckpoint
from .encoder import bars_to_channels, normalise_window
from .model import ARCHITECTURE_VERSION, ModelConfig, build_model
from .result import (CalibrationInfo, NativeForecast, abstaining_forecast,
                     regime_distribution)
from .tasks import TASK_SCHEMA_VERSION, REGIME_CLASSES, TaskKind


@dataclass(frozen=True)
class ServingContext:
    """What the checkpoint learned *about its own training set*.

    Not weights and not hyper-parameters: the ranges and counts the abstention
    policy needs to answer "is this input like what I was trained on". Written
    into the checkpoint at fit time, because reconstructing it at serve time
    would mean reading the training data at serve time.
    """

    #: Window volatility range seen in training, for the OOD score.
    volatility_range: tuple[float, float] = (0.0, 0.0)
    #: Reconstruction-error range seen in training, likewise.
    reconstruction_range: tuple[float, float] = (0.0, 0.0)
    #: Training windows per regime label. Feeds `unsupported_regime`.
    regime_support: Mapping[str, int] = field(default_factory=dict)
    #: Instruments the model saw. Feeds `instrument_out_of_distribution`.
    trained_instruments: tuple[str, ...] = ()
    #: Median absolute terminal error on the calibration split, for context.
    median_absolute_error: float | None = None
    #: Empirical p10–p90 spread of *actual* h-step returns in training, and the
    #: median window volatility that spread was observed at. Together they are
    #: the reference `abstain.excessive_dispersion` compares against — see that
    #: module's docstring on why the analytic sqrt(h) reference was wrong.
    typical_terminal_spread: float = 0.0
    median_window_volatility: float = 0.0

    def __post_init__(self):
        object.__setattr__(self, "volatility_range", tuple(self.volatility_range))
        object.__setattr__(self, "reconstruction_range",
                           tuple(self.reconstruction_range))
        object.__setattr__(self, "regime_support", dict(self.regime_support))
        object.__setattr__(self, "trained_instruments",
                           tuple(self.trained_instruments))
        for name in ("volatility_range", "reconstruction_range"):
            low, high = getattr(self, name)
            if high < low:
                raise ConfigurationError(f"{name} is inverted",
                                         **{name: [low, high]})
        for name in ("typical_terminal_spread", "median_window_volatility"):
            if float(getattr(self, name)) < 0:
                raise ConfigurationError(f"{name} must be >= 0",
                                         **{name: getattr(self, name)})

    def expected_dispersion(self, window_volatility: float) -> float:
        """How wide an interval this instrument's returns actually warrant now.

        The training spread, rescaled by how this window's volatility compares
        with the volatility that spread was measured at. Zero when no reference
        was recorded, which `abstain` reads as "no basis to judge" and skips.
        """
        if self.typical_terminal_spread <= 0:
            return 0.0
        if self.median_window_volatility <= 0:
            return self.typical_terminal_spread
        ratio = max(0.1, min(10.0,
                             window_volatility / self.median_window_volatility))
        return self.typical_terminal_spread * ratio

    def to_dict(self) -> dict:
        return {"volatility_range": list(self.volatility_range),
                "reconstruction_range": list(self.reconstruction_range),
                "regime_support": dict(self.regime_support),
                "trained_instruments": list(self.trained_instruments),
                "median_absolute_error": self.median_absolute_error,
                "typical_terminal_spread": self.typical_terminal_spread,
                "median_window_volatility": self.median_window_volatility}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ServingContext":
        return cls(volatility_range=tuple(data.get("volatility_range") or (0.0, 0.0)),
                   reconstruction_range=tuple(
                       data.get("reconstruction_range") or (0.0, 0.0)),
                   regime_support=dict(data.get("regime_support") or {}),
                   trained_instruments=tuple(data.get("trained_instruments") or ()),
                   median_absolute_error=data.get("median_absolute_error"),
                   typical_terminal_spread=float(
                       data.get("typical_terminal_spread", 0.0)),
                   median_window_volatility=float(
                       data.get("median_window_volatility", 0.0)))


@dataclass(frozen=True)
class InferenceStats:
    """Measured, not estimated. `memory_kb` is `None` when unmeasurable."""

    calls: int
    total_ms: float
    p50_ms: float
    p95_ms: float
    max_ms: float
    memory_kb: int | None = None
    memory_source: str = ""

    @property
    def mean_ms(self) -> float:
        return self.total_ms / self.calls if self.calls else 0.0

    def to_dict(self) -> dict:
        return {"calls": self.calls, "total_ms": round(self.total_ms, 3),
                "mean_ms": round(self.mean_ms, 4),
                "p50_ms": round(self.p50_ms, 4), "p95_ms": round(self.p95_ms, 4),
                "max_ms": round(self.max_ms, 4), "memory_kb": self.memory_kb,
                "memory_source": self.memory_source}


def peak_memory_kb() -> tuple[int | None, str]:
    """Peak resident memory, and where the number came from.

    `None` with a stated reason rather than a zero, because a zero looks like a
    measurement and is not one.
    """
    try:
        import resource                                  # noqa: PLC0415
        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports kilobytes; macOS reports bytes.
        import sys                                       # noqa: PLC0415
        if sys.platform == "darwin":                     # pragma: no cover
            return int(usage // 1024), "getrusage (bytes→kb)"
        return int(usage), "getrusage"
    except Exception as error:                           # noqa: BLE001
        return None, f"unavailable: {type(error).__name__}"


class MultiTaskPredictor:
    """Serves one checkpoint. Verifies it, runs it, and decides whether to answer.

    Verification happens in `__init__` and the object refuses to exist if it
    fails. A predictor that could be constructed from a corrupt checkpoint and
    then declined every call would be a predictor whose failure mode is
    indistinguishable from a quiet market.
    """

    def __init__(self, checkpoint: NativeCheckpoint, *,
                 context: ServingContext | None = None,
                 calibration: CalibrationInfo | None = None,
                 thresholds: AbstentionThresholds | None = None,
                 clock: Clock | None = None,
                 dataset_version: str = "",
                 feature_schema_version: str = "",
                 market: str = "unknown",
                 verify: bool = True):
        parameters = dict(checkpoint.parameters)
        kind = parameters.get("kind")
        if kind != "olympus-multitask":
            raise ConfigurationError(
                "this checkpoint was not produced by the multi-task model",
                expected="olympus-multitask", got=kind,
                checkpoint_id=checkpoint.checkpoint_id)

        # Verify before loading. A hash that does not recompute means nothing
        # in the checkpoint can be trusted, including its config.
        self.verified = True
        self.recomputed_hash = checkpoint.content_hash
        if verify:
            rebuilt = NativeCheckpoint.from_dict(checkpoint.to_dict())
            self.recomputed_hash = rebuilt.content_hash
            self.verified = (self.recomputed_hash == checkpoint.content_hash)
            if not self.verified:
                raise ConfigurationError(
                    "the checkpoint's content hash does not recompute; nothing "
                    "in it can be trusted",
                    checkpoint_id=checkpoint.checkpoint_id)

        self.checkpoint = checkpoint
        self.config = ModelConfig.from_dict(parameters["config"])
        self.context = context or ServingContext()
        self.calibration = calibration or CalibrationInfo()
        self.policy = AbstentionPolicy(thresholds)
        self.clock = clock or default_clock()
        self.dataset_version = (dataset_version
                                or checkpoint.manifest.data_hash or "unrecorded")
        self.feature_schema_version = feature_schema_version or "olympus.market.v1"
        self.market = market
        self.checkpoint_architecture = str(
            parameters.get("architecture_version", ""))

        self._model: Any = None
        self._latencies: list[float] = []

    # -- lazy build --------------------------------------------------------

    @property
    def model(self):
        """Built on first use. Torch is not imported until a forecast is asked
        for, so a consumer that only reads metadata never pays for it."""
        if self._model is None:
            from .torchutil import json_to_state_dict, require_torch
            require_torch()
            model = build_model(self.config)
            json_to_state_dict(self.checkpoint.parameters["state"], model)
            model.eval()
            self._model = model
        return self._model

    @property
    def model_version(self) -> str:
        return self.checkpoint.model_version

    @property
    def required_bars(self) -> int:
        return self.config.lookback

    def stats(self) -> InferenceStats:
        """Latency and memory, from the calls actually made."""
        memory, source = peak_memory_kb()
        if not self._latencies:
            return InferenceStats(calls=0, total_ms=0.0, p50_ms=0.0, p95_ms=0.0,
                                  max_ms=0.0, memory_kb=memory,
                                  memory_source=source)
        ordered = sorted(self._latencies)
        def percentile(q: float) -> float:
            index = min(len(ordered) - 1, int(q * (len(ordered) - 1)))
            return ordered[index]
        return InferenceStats(
            calls=len(ordered), total_ms=sum(ordered), p50_ms=percentile(0.5),
            p95_ms=percentile(0.95), max_ms=ordered[-1], memory_kb=memory,
            memory_source=source)

    # -- the forecast ------------------------------------------------------

    def forecast(self, bars: Sequence[Candle], *,
                 as_of: datetime | None = None,
                 horizon: int = 0,
                 instrument_key: str = "",
                 missing_channels: Sequence[str] = (),
                 references: Any = None,
                 reference_mask: Any = None,
                 extra_timeframes: Mapping[str, Any] | None = None,
                 ) -> NativeForecast:
        """One forecast, with the abstention decision attached either way."""
        window = list(bars)
        key = instrument_key or (window[-1].instrument_key if window else "")
        timeframe = window[-1].timeframe if window else ""
        stamp = ensure_utc(as_of, field_name="as_of") if as_of is not None \
            else ensure_utc(self.clock.now(), field_name="as_of")
        requested = int(horizon or self.config.horizon)

        input_start = window[0].ts_open if window else stamp
        input_end = window[-1].ts_close if window else stamp

        def decline(**kwargs) -> NativeForecast:
            decision = self.policy.evaluate(**kwargs)
            return abstaining_forecast(
                instrument_key=key or "unknown", market=self.market,
                timeframes=(timeframe or "unknown",), input_start=input_start,
                input_end=input_end, created_at=stamp, horizon=requested,
                decision=decision, model_version=self.model_version,
                checkpoint_hash=self.checkpoint.content_hash,
                dataset_version=self.dataset_version,
                feature_schema_version=self.feature_schema_version)

        # Structural refusals, before any tensor is allocated.
        if len(window) < self.config.lookback:
            return decline(
                checkpoint_verified=self.verified,
                missing_channels=("close: too few bars",),
                requested_horizon=requested,
                trained_horizon=self.config.horizon)
        if requested != self.config.horizon or missing_channels:
            return decline(
                checkpoint_verified=self.verified,
                checkpoint_hash=self.checkpoint.content_hash,
                recomputed_hash=self.recomputed_hash,
                architecture_version=self.checkpoint_architecture,
                expected_architecture_version=ARCHITECTURE_VERSION,
                requested_horizon=requested,
                trained_horizon=self.config.horizon,
                missing_channels=tuple(missing_channels))

        from .torchutil import require_torch
        torch = require_torch()

        recent = window[-self.config.lookback:]
        channels = bars_to_channels(recent)
        normalised, stats = normalise_window(channels)
        realised = stats[0][1]                            # log-return sigma
        x = torch.tensor([normalised], dtype=torch.float32)

        started = time.perf_counter()
        with torch.no_grad():
            outputs = self.model(x, references=references,
                                 reference_mask=reference_mask,
                                 extra_timeframes=extra_timeframes)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self._latencies.append(elapsed_ms)

        assembled = self._assemble_tasks(outputs, recent, torch)
        assembled["realised_volatility"] = realised

        ood = self._ood_score(assembled.get("anomaly_score"), realised)
        regime = max(assembled["regime_probabilities"].items(),
                     key=lambda kv: kv[1])[0] if assembled["regime_probabilities"] \
            else ""

        bar_seconds = 0.0
        try:
            bar_seconds = parse_timeframe(timeframe).total_seconds()
        except Exception:                                # noqa: BLE001
            bar_seconds = 0.0

        decision = self.policy.evaluate(
            checkpoint_verified=self.verified,
            checkpoint_hash=self.checkpoint.content_hash,
            recomputed_hash=self.recomputed_hash,
            architecture_version=self.checkpoint_architecture,
            expected_architecture_version=ARCHITECTURE_VERSION,
            task_schema_version=self.config.tasks.schema_version,
            expected_task_schema_version=TASK_SCHEMA_VERSION,
            requested_horizon=requested, trained_horizon=self.config.horizon,
            last_bar_close=recent[-1].ts_close, as_of=stamp,
            bar_seconds=bar_seconds,
            instrument_key=key,
            trained_instruments=self.context.trained_instruments,
            has_instrument_embedding=bool(self.config.n_instruments),
            dispersion=assembled["dispersion"],
            expected_dispersion=self.context.expected_dispersion(realised),
            coverage_error=self.calibration.coverage_error,
            regime=regime, regime_support=self.context.regime_support,
            ood_score=ood)

        if decision.abstained:
            return abstaining_forecast(
                instrument_key=key, market=self.market,
                timeframes=(timeframe,), input_start=recent[0].ts_open,
                input_end=recent[-1].ts_close, created_at=stamp,
                horizon=requested, decision=decision,
                model_version=self.model_version,
                checkpoint_hash=self.checkpoint.content_hash,
                dataset_version=self.dataset_version,
                feature_schema_version=self.feature_schema_version,
                warnings=assembled["warnings"])

        return NativeForecast(
            instrument_key=key, market=self.market, timeframes=(timeframe,),
            input_start=recent[0].ts_open, input_end=recent[-1].ts_close,
            created_at=stamp, horizon=requested,
            model_version=self.model_version,
            checkpoint_hash=self.checkpoint.content_hash,
            dataset_version=self.dataset_version,
            feature_schema_version=self.feature_schema_version,
            expected_returns=assembled["expected_returns"],
            direction_probabilities=assembled["direction_probabilities"],
            quantiles=assembled["quantiles"],
            price_quantiles=assembled["price_quantiles"],
            anchor_close=recent[-1].close,
            predicted_volatility=assembled.get("predicted_volatility"),
            drawdown_probability=assembled.get("drawdown_probability"),
            drawdown_threshold=(self.config.tasks.drawdown_threshold
                                if assembled.get("drawdown_probability") is not None
                                else None),
            regime_probabilities=assembled["regime_probabilities"],
            uncertainty=assembled["uncertainty"],
            dispersion=assembled["dispersion"], realised_volatility=realised,
            calibration=self.calibration, ood_score=ood,
            anomaly_score=assembled.get("anomaly_score"),
            predicted_error_risk=assembled.get("predicted_error_risk"),
            abstention=decision, tasks_present=assembled["tasks_present"],
            warnings=assembled["warnings"], data_quality=DataQuality.OK,
            inference_ms=elapsed_ms)

    # -- helpers -----------------------------------------------------------

    def _assemble_tasks(self, outputs: Mapping[str, Any],
                        window: Sequence[Candle], torch) -> dict:
        """Every head's raw output turned into the contract's units."""
        from .quantile import quantile_label
        cfg = self.config
        present: list[str] = []
        warnings: list[str] = []

        raw = outputs[TaskKind.RETURN_DISTRIBUTION.value][0]   # [H, Q]
        labels = [quantile_label(q) for q in cfg.quantiles]
        scale = self.calibration.scale_correction
        median_index = cfg.median_index
        quantiles: dict[str, tuple[float, ...]] = {}
        for index, label in enumerate(labels):
            values = []
            for step in range(cfg.horizon):
                centre = float(raw[step, median_index])
                value = float(raw[step, index])
                # Calibration widens or narrows about the median rather than
                # shifting it: a coverage correction is a statement about
                # spread, and applying it to the location would move the
                # forecast itself.
                values.append(centre + (value - centre) * scale)
            quantiles[label] = tuple(values)
        present.append(TaskKind.RETURN_DISTRIBUTION.value)

        anchor = window[-1].close
        price_quantiles = {k: tuple(anchor * math.exp(v) for v in values)
                           for k, values in quantiles.items()}
        present.append(TaskKind.PRICE_QUANTILES.value)

        median_label = labels[median_index]
        expected = quantiles[median_label]

        low, high = labels[0], labels[-1]
        dispersion = quantiles[high][-1] - quantiles[low][-1]
        # Uncertainty is bounded and monotone in dispersion. `tanh` rather than
        # a hard clip so that two very wide forecasts remain distinguishable
        # instead of both reading 1.0.
        uncertainty = float(math.tanh(max(dispersion, 0.0) * 20.0))

        result: dict[str, Any] = {
            "quantiles": quantiles, "price_quantiles": price_quantiles,
            "expected_returns": expected, "dispersion": dispersion,
            "uncertainty": uncertainty, "direction_probabilities": {},
            "regime_probabilities": {}, "warnings": warnings,
        }

        if TaskKind.DIRECTION.value in outputs:
            probability = float(torch.sigmoid(
                outputs[TaskKind.DIRECTION.value][0, 0]))
            result["direction_probabilities"] = {"up": probability,
                                                 "down": 1.0 - probability}
            present.append(TaskKind.DIRECTION.value)
            # A direction head disagreeing with its own median is a real
            # signal, not noise: a skewed distribution can have a negative
            # median and a >50% chance of a positive move.
            if (probability > 0.5) != (expected[-1] > 0):
                warnings.append(
                    "direction head and median disagree; the predicted "
                    "distribution is skewed")

        if TaskKind.REGIME.value in outputs:
            probabilities = torch.softmax(
                outputs[TaskKind.REGIME.value][0], dim=-1).tolist()
            result["regime_probabilities"] = regime_distribution(probabilities)
            present.append(TaskKind.REGIME.value)

        if TaskKind.VOLATILITY.value in outputs:
            # Predicted in log space; exponentiating is what makes a negative
            # volatility unrepresentable rather than merely penalised.
            result["predicted_volatility"] = float(
                torch.exp(outputs[TaskKind.VOLATILITY.value][0, 0]))
            present.append(TaskKind.VOLATILITY.value)

        if TaskKind.DRAWDOWN.value in outputs:
            result["drawdown_probability"] = float(torch.sigmoid(
                outputs[TaskKind.DRAWDOWN.value][0, 0]))
            present.append(TaskKind.DRAWDOWN.value)

        if TaskKind.ABSTENTION.value in outputs:
            result["predicted_error_risk"] = float(torch.sigmoid(
                outputs[TaskKind.ABSTENTION.value][0, 0]))
            present.append(TaskKind.ABSTENTION.value)

        if TaskKind.ANOMALY.value in outputs:
            rebuilt = outputs[TaskKind.ANOMALY.value]
            original = torch.tensor(
                [[list(c) for c in normalise_window(
                    bars_to_channels(window))[0]]], dtype=rebuilt.dtype)
            result["anomaly_score"] = float(((rebuilt - original) ** 2).mean())
            present.append(TaskKind.ANOMALY.value)

        if self.calibration.calibrated:
            present.append(TaskKind.CALIBRATION.value)
        present.append(TaskKind.OOD.value)

        result["tasks_present"] = tuple(present)
        return result

    def _ood_score(self, reconstruction: float | None,
                   realised_volatility: float) -> float:
        """How unlike the training set this window is, in [0, 1].

        Two signals, combined by taking the worse: reconstruction error against
        the range seen in training, and window volatility against the same. The
        worse rather than the mean, because a window that is extreme on either
        axis is out of distribution regardless of how ordinary it is on the
        other.
        """
        scores = []
        low, high = self.context.reconstruction_range
        if reconstruction is not None and high > low:
            scores.append(min(1.0, max(0.0, (reconstruction - low) / (high - low))))
        low, high = self.context.volatility_range
        if high > low:
            scores.append(min(1.0, max(0.0,
                                       (realised_volatility - low) / (high - low))))
        if not scores:
            # No recorded ranges means no basis for a judgement. 0.0 would be a
            # claim of in-distribution; 1.0 would abstain on everything. The
            # honest middle is "unknown", reported as 0.5 and stated here.
            return 0.5
        return max(scores)

    def describe(self) -> dict:
        return {"model_version": self.model_version,
                "checkpoint_id": self.checkpoint.checkpoint_id,
                "checkpoint_hash": self.checkpoint.content_hash,
                "verified": self.verified,
                "architecture_version": self.checkpoint_architecture,
                "config": self.config.to_dict(),
                "context": self.context.to_dict(),
                "calibration": self.calibration.to_dict(),
                "abstention": self.policy.describe(),
                "dataset_version": self.dataset_version,
                "feature_schema_version": self.feature_schema_version}


def build_serving_context(model, windows: Sequence[Any], config: ModelConfig, *,
                          classifier: Any = None,
                          instruments: Sequence[str] = ()) -> ServingContext:
    """Record what the training set looked like, for the abstention policy.

    Computed on the **training** windows only. Computing it on validation or
    test would make the OOD detector's notion of "normal" include data the
    model was not fitted on, and the detector would then under-report exactly
    where it matters.
    """
    from .torchutil import require_torch
    from .tasks import target_regime
    torch = require_torch()

    rows, volatilities, terminals = [], [], []
    for window in windows:
        channels = bars_to_channels(window.inputs)
        normalised, stats = normalise_window(channels)
        rows.append(normalised)
        volatilities.append(stats[0][1])
        terminals.append(window.target_log_returns()[-1])
    if not rows:
        raise ConfigurationError("no windows to build a serving context from")

    x = torch.tensor(rows, dtype=torch.float32)
    errors: list[float] = []
    with torch.no_grad():
        outputs = model(x)
        if TaskKind.ANOMALY.value in outputs:
            squared = (outputs[TaskKind.ANOMALY.value] - x) ** 2
            errors = squared.mean(dim=(1, 2)).tolist()

    support: dict[str, int] = {}
    for window in windows:
        index = target_regime(window, classifier)
        label = REGIME_CLASSES[index]
        support[label] = support.get(label, 0) + 1

    def bounds(values: Sequence[float], low: float = 0.01,
               high: float = 0.99) -> tuple[float, float]:
        """Percentile bounds, not min/max. One freak training window would
        otherwise widen the 'normal' range enough to hide every later one."""
        if not values:
            return (0.0, 0.0)
        ordered = sorted(values)
        def at(q: float) -> float:
            return ordered[min(len(ordered) - 1, int(q * (len(ordered) - 1)))]
        return (at(low), at(high))

    # The empirical reference the dispersion check compares against: what the
    # h-step returns *actually* spread over, not what an independent-increment
    # model says they should. See `abstain`'s docstring.
    # 10th–90th, to match the quantile grid the predicted interval uses.
    low, high = bounds(terminals, 0.10, 0.90)
    spread = max(0.0, high - low)

    return ServingContext(
        volatility_range=bounds(volatilities),
        reconstruction_range=bounds(errors),
        regime_support=support,
        trained_instruments=tuple(sorted(set(instruments))) or tuple(sorted(
            {w.instrument_key for w in windows})),
        typical_terminal_spread=spread,
        median_window_volatility=(statistics.median(volatilities)
                                  if volatilities else 0.0))


def fit_calibration(predictor_outputs: Sequence[Mapping[str, Any]],
                    actuals: Sequence[float], *, quantiles: Sequence[float],
                    fitted_on: str = "validation") -> CalibrationInfo:
    """Measure coverage on a held-out split and compute the correction.

    `fitted_on` may not be `"train"` — `CalibrationInfo` refuses it. A
    calibration fitted on the training rows reports the training rows'
    coverage, which is always good and always meaningless.
    """
    from .quantile import quantile_label
    levels = sorted(float(q) for q in quantiles)
    if len(levels) < 2:
        raise ConfigurationError("calibration needs at least two quantiles")
    low_label, high_label = quantile_label(levels[0]), quantile_label(levels[-1])
    nominal = levels[-1] - levels[0]

    inside = 0
    widths: list[float] = []
    counted = 0
    for row, actual in zip(predictor_outputs, actuals):
        low = row.get(low_label)
        high = row.get(high_label)
        if low is None or high is None:                  # pragma: no cover
            continue
        counted += 1
        widths.append(float(high) - float(low))
        if float(low) <= float(actual) <= float(high):
            inside += 1
    if not counted:
        return CalibrationInfo(nominal_coverage=nominal, fitted_on="",
                               n_calibration_rows=0)

    observed = inside / counted
    # The correction is a ratio of empirical quantiles of the standardised
    # residual, approximated here by the ratio of nominal to observed coverage
    # applied to the interval width. Deliberately simple and deliberately
    # bounded: an unbounded correction fitted on a few hundred rows produces
    # intervals nobody can act on.
    correction = 1.0
    if observed > 0:
        correction = min(3.0, max(0.33, nominal / observed))
    return CalibrationInfo(observed_coverage=observed, nominal_coverage=nominal,
                           scale_correction=correction, fitted_on=fitted_on,
                           n_calibration_rows=counted,
                           method="empirical_coverage_ratio")


__all__ = ["ServingContext", "InferenceStats", "peak_memory_kb",
           "MultiTaskPredictor", "build_serving_context", "fit_calibration"]

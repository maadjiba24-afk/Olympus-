"""The native forecast contract — everything that travels with a number.

`contracts.ForecastResult` is the *serving* contract: what the signal layer,
the risk engine and the audit ledger consume, and it is deliberately narrow.
`NativeForecast` is the multi-task model's full output, and it is deliberately
wide: every task, every uncertainty measure, every provenance field and every
abstention reason, so that a record read a year later answers "should I have
trusted this" without needing the model.

`to_forecast_result()` narrows one to the other. That direction is the only one
implemented, and on purpose — a narrow record cannot be widened back into a
full one, and offering a function that appeared to do so would invite someone
to reconstruct fields that were never measured.

The rule this file exists to enforce
------------------------------------
**A field that was not computed is `None`, never a default.** A drawdown
probability of `0.0` from a checkpoint with no drawdown head is a claim that
the instrument will not fall, and a consumer cannot tell it from a real
prediction of safety. So every task output is optional, `tasks_present`
enumerates what was actually produced, and reading an absent one gives `None`.

Provenance is not optional
--------------------------
`model_version`, `checkpoint_hash`, `dataset_version` and `schema_version` are
required at construction. A forecast whose model cannot be identified is not
auditable, and the whole point of the manifest chain — dataset hash → training
manifest → checkpoint hash → forecast — is that any link being absent breaks
the chain silently unless something refuses.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from ..contracts import (DataQuality, ForecastResult, Regime, ensure_utc,
                         jsonable)
from ..errors import ConfigurationError
from .abstain import AbstentionDecision
from .tasks import REGIME_CLASSES

#: Bumped when a field's *meaning* changes. A consumer may refuse an older
#: record rather than reading a field that used to mean something else.
FORECAST_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class CalibrationInfo:
    """What is known about whether this model's uncertainty means anything.

    `fitted_on` is the field that matters: a calibration fitted on the training
    rows reports the training rows' coverage, which is always good and always
    meaningless. Recording where it came from is what lets a reader tell the
    two apart.
    """

    #: Empirical coverage of the widest interval, on the calibration split.
    observed_coverage: float | None = None
    nominal_coverage: float | None = None
    #: Multiplicative widening that would fix the coverage. 1.0 = no change.
    scale_correction: float = 1.0
    #: "validation", "test", or "" when no calibration has been fitted.
    fitted_on: str = ""
    n_calibration_rows: int = 0
    method: str = "empirical_coverage"

    def __post_init__(self):
        if self.scale_correction <= 0:
            raise ConfigurationError("scale_correction must be positive",
                                     scale_correction=self.scale_correction)
        if self.fitted_on == "train":
            raise ConfigurationError(
                "a calibration fitted on the training rows reports the "
                "training rows' coverage, which is always good and always "
                "meaningless")

    @property
    def coverage_error(self) -> float | None:
        """Signed, in coverage points. Positive means over-cautious."""
        if self.observed_coverage is None or self.nominal_coverage is None:
            return None
        return (self.observed_coverage - self.nominal_coverage) * 100.0

    @property
    def calibrated(self) -> bool:
        return bool(self.fitted_on) and self.observed_coverage is not None

    def to_dict(self) -> dict:
        return {"observed_coverage": self.observed_coverage,
                "nominal_coverage": self.nominal_coverage,
                "coverage_error": self.coverage_error,
                "scale_correction": self.scale_correction,
                "fitted_on": self.fitted_on,
                "n_calibration_rows": self.n_calibration_rows,
                "method": self.method, "calibrated": self.calibrated}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CalibrationInfo":
        return cls(observed_coverage=data.get("observed_coverage"),
                   nominal_coverage=data.get("nominal_coverage"),
                   scale_correction=float(data.get("scale_correction", 1.0)),
                   fitted_on=str(data.get("fitted_on", "")),
                   n_calibration_rows=int(data.get("n_calibration_rows", 0)),
                   method=str(data.get("method", "empirical_coverage")))


@dataclass(frozen=True)
class NativeForecast:
    """The multi-task model's complete output for one instrument at one instant."""

    # -- identity of the observation ---------------------------------------
    instrument_key: str
    market: str
    timeframes: tuple[str, ...]
    input_start: datetime
    input_end: datetime
    horizon: int
    created_at: datetime

    # -- provenance: required, because a chain with a missing link is broken
    model_version: str
    checkpoint_hash: str
    dataset_version: str
    feature_schema_version: str

    # -- task outputs: every one optional, absent means not computed -------
    #: Expected (mean) cumulative log return per horizon step.
    expected_returns: tuple[float, ...] = ()
    #: `{"up": p, "down": p}` — from the direction head, not from the median.
    direction_probabilities: Mapping[str, float] = field(default_factory=dict)
    #: `{quantile_label: per-step cumulative log returns}`.
    quantiles: Mapping[str, tuple[float, ...]] = field(default_factory=dict)
    #: Price-space quantiles, derived from the anchor close.
    price_quantiles: Mapping[str, tuple[float, ...]] = field(default_factory=dict)
    anchor_close: float | None = None
    predicted_volatility: float | None = None
    drawdown_probability: float | None = None
    drawdown_threshold: float | None = None
    liquidity_forecast: float | None = None
    execution_cost_estimate: float | None = None
    spread_forecast: float | None = None
    #: `{regime_label: probability}` over `tasks.REGIME_CLASSES`.
    regime_probabilities: Mapping[str, float] = field(default_factory=dict)

    # -- uncertainty and trust ---------------------------------------------
    #: 0 = fully confident, 1 = no information. Monotone in dispersion.
    uncertainty: float = 1.0
    #: Width of the widest interval at the terminal step, in log-return space.
    dispersion: float | None = None
    realised_volatility: float | None = None
    calibration: CalibrationInfo = field(default_factory=CalibrationInfo)
    #: 0 = squarely in distribution, 1 = nothing like the training set.
    ood_score: float | None = None
    anomaly_score: float | None = None
    #: From the abstention head, when enabled. Distinct from the *decision*.
    predicted_error_risk: float | None = None

    # -- the decision ------------------------------------------------------
    abstention: AbstentionDecision = field(
        default_factory=lambda: AbstentionDecision(abstained=False))

    # -- the rest ----------------------------------------------------------
    #: Task values actually produced by this checkpoint.
    tasks_present: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    data_quality: DataQuality = DataQuality.OK
    inference_ms: float | None = None
    schema_version: int = FORECAST_SCHEMA_VERSION

    def __post_init__(self):
        for name in ("instrument_key", "model_version", "checkpoint_hash",
                     "dataset_version", "feature_schema_version"):
            if not str(getattr(self, name)).strip():
                raise ConfigurationError(
                    f"{name} is required: a forecast whose provenance chain has "
                    f"a missing link is not auditable", field=name)
        for name in ("input_start", "input_end", "created_at"):
            object.__setattr__(self, name,
                               ensure_utc(getattr(self, name), field_name=name))
        if self.input_end < self.input_start:
            raise ConfigurationError("the input window is inverted",
                                     input_start=self.input_start,
                                     input_end=self.input_end)
        if int(self.horizon) < 1:
            raise ConfigurationError("horizon must be >= 1", horizon=self.horizon)

        object.__setattr__(self, "timeframes", tuple(self.timeframes))
        if not self.timeframes:
            raise ConfigurationError("a forecast must name its timeframes")
        object.__setattr__(self, "expected_returns", tuple(self.expected_returns))
        object.__setattr__(self, "quantiles",
                           {k: tuple(v) for k, v in dict(self.quantiles).items()})
        object.__setattr__(self, "price_quantiles",
                           {k: tuple(v) for k, v
                            in dict(self.price_quantiles).items()})
        object.__setattr__(self, "direction_probabilities",
                           dict(self.direction_probabilities))
        object.__setattr__(self, "regime_probabilities",
                           dict(self.regime_probabilities))
        object.__setattr__(self, "tasks_present", tuple(self.tasks_present))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "data_quality", DataQuality(self.data_quality))

        if not 0.0 <= self.uncertainty <= 1.0:
            raise ConfigurationError("uncertainty must be in [0, 1]",
                                     uncertainty=self.uncertainty)
        for name in ("ood_score", "drawdown_probability", "predicted_error_risk"):
            value = getattr(self, name)
            if value is not None and not 0.0 <= float(value) <= 1.0:
                raise ConfigurationError(f"{name} must be in [0, 1]",
                                         **{name: value})

        # Quantile ordering, checked here as well as constructed in the head.
        # Two checks, because a crossed quantile that reached a stop-loss would
        # be a total failure and the head is not the only possible producer.
        for step in range(self.horizon):
            values = [v[step] for v in self.quantiles.values()
                      if len(v) > step]
            ordered = [self.quantiles[k][step]
                       for k in sorted(self.quantiles, key=_level_of)
                       if len(self.quantiles[k]) > step]
            if ordered != sorted(ordered):
                raise ConfigurationError(
                    "quantiles cross: a p90 below a p10 is not a wide interval, "
                    "it is a nonsense one", step=step, values=ordered)
            del values

        if self.abstention.abstained and not self.abstention.findings:
            raise ConfigurationError(   # pragma: no cover - guarded upstream
                "an abstention with no reason")

    # -- accessors ---------------------------------------------------------

    @property
    def abstained(self) -> bool:
        return self.abstention.abstained

    @property
    def abstention_reasons(self) -> tuple[str, ...]:
        return self.abstention.reasons

    @property
    def terminal_return(self) -> float | None:
        return self.expected_returns[-1] if self.expected_returns else None

    @property
    def most_likely_regime(self) -> str:
        if not self.regime_probabilities:
            return Regime.UNKNOWN.value
        return max(self.regime_probabilities.items(), key=lambda kv: kv[1])[0]

    def has(self, task: str) -> bool:
        return task in self.tasks_present

    def value(self, task: str) -> Any:
        """A task's output, or `None` when this checkpoint did not produce it.

        `None` rather than a default, for the same reason
        `MarketState.feature` returns `None`: a zero drawdown probability from
        a checkpoint with no drawdown head is a claim of safety.
        """
        if task not in self.tasks_present:
            return None
        return {
            "return_distribution": self.quantiles,
            "direction": self.direction_probabilities,
            "price_quantiles": self.price_quantiles,
            "volatility": self.predicted_volatility,
            "regime": self.regime_probabilities,
            "drawdown": self.drawdown_probability,
            "liquidity": self.liquidity_forecast,
            "spread": self.spread_forecast,
            "execution_cost": self.execution_cost_estimate,
            "anomaly": self.anomaly_score,
            "ood": self.ood_score,
            "calibration": self.calibration,
            "abstention": self.predicted_error_risk,
        }.get(task)

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "instrument": self.instrument_key, "market": self.market,
            "timeframes": list(self.timeframes),
            "input_start": jsonable(self.input_start),
            "input_end": jsonable(self.input_end),
            "created_at": jsonable(self.created_at),
            "horizon": self.horizon,
            "expected_returns": list(self.expected_returns),
            "direction_probabilities": dict(self.direction_probabilities),
            "quantiles": {k: list(v) for k, v in self.quantiles.items()},
            "price_quantiles": {k: list(v) for k, v
                                in self.price_quantiles.items()},
            "anchor_close": self.anchor_close,
            "predicted_volatility": self.predicted_volatility,
            "drawdown_probability": self.drawdown_probability,
            "drawdown_threshold": self.drawdown_threshold,
            "liquidity_forecast": self.liquidity_forecast,
            "execution_cost_estimate": self.execution_cost_estimate,
            "spread_forecast": self.spread_forecast,
            "regime_probabilities": dict(self.regime_probabilities),
            "uncertainty": self.uncertainty, "dispersion": self.dispersion,
            "realised_volatility": self.realised_volatility,
            "calibration": self.calibration.to_dict(),
            "ood_score": self.ood_score, "anomaly_score": self.anomaly_score,
            "predicted_error_risk": self.predicted_error_risk,
            "abstained": self.abstained,
            "abstention": self.abstention.to_dict(),
            "abstention_reasons": list(self.abstention_reasons),
            "model_version": self.model_version,
            "checkpoint_hash": self.checkpoint_hash,
            "dataset_version": self.dataset_version,
            "feature_schema_version": self.feature_schema_version,
            "tasks_present": list(self.tasks_present),
            "warnings": list(self.warnings),
            "data_quality": self.data_quality.value,
            "inference_ms": self.inference_ms,
            "schema_version": self.schema_version,
        }

    def fingerprint(self) -> str:
        """Content hash. Two identical forecasts hash the same, which is what
        makes a cached one safe to reuse."""
        payload = self.to_dict()
        payload.pop("created_at", None)
        payload.pop("inference_ms", None)
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]

    def to_forecast_result(self) -> ForecastResult:
        """Narrow to the serving contract the rest of Olympus consumes.

        One direction only. A `ForecastResult` cannot be widened back into a
        `NativeForecast`, and offering a function that appeared to would invite
        someone to reconstruct fields that were never measured.
        """
        anchor = self.anchor_close
        prices: dict[str, tuple[float, ...]] = {}
        median: tuple[float, ...] = ()
        if anchor and anchor > 0:
            prices = {k: tuple(anchor * math.exp(v) for v in values)
                      for k, values in self.quantiles.items()}
            middle = _median_label(self.quantiles)
            if middle:
                median = prices[middle]

        expected = 0.0
        if self.expected_returns:
            expected = math.expm1(self.expected_returns[-1])

        return ForecastResult(
            instrument_key=self.instrument_key,
            timeframe=self.timeframes[0],
            input_start=self.input_start, input_end=self.input_end,
            created_at=self.created_at, horizon=self.horizon,
            mean_close=median, median_close=median, quantiles=prices,
            expected_return=expected,
            expected_return_path=tuple(math.expm1(v)
                                       for v in self.expected_returns),
            forecast_volatility=float(self.predicted_volatility or 0.0),
            realised_volatility=float(self.realised_volatility or 0.0),
            direction_probabilities=dict(self.direction_probabilities),
            uncertainty=self.uncertainty,
            model_version=self.model_version,
            model_identity={"checkpoint_hash": self.checkpoint_hash,
                            "dataset_version": self.dataset_version,
                            "feature_schema_version": self.feature_schema_version,
                            "owner": "olympus"},
            inference_params={"tasks": list(self.tasks_present),
                              "schema_version": self.schema_version},
            data_quality=self.data_quality,
            warnings=self.warnings,
            abstained=self.abstained,
            failure_reason=(self.abstention.findings[0].detail
                            if self.abstained else None),
            failure_code=(self.abstention.primary.value
                          if self.abstention.primary else None))

    def describe(self) -> str:                           # pragma: no cover
        state = "ABSTAINED" if self.abstained else "ok"
        return (f"NativeForecast({self.instrument_key} h={self.horizon} "
                f"{state} tasks={len(self.tasks_present)} "
                f"v={self.model_version})")


def _level_of(label: str) -> float:
    """Numeric level of a `pNN` label, for ordering. Unparseable sorts last."""
    from ..evaluate import quantile_level
    level = quantile_level(label)
    return level if level is not None else 2.0


def _median_label(quantiles: Mapping[str, Any]) -> str:
    if not quantiles:
        return ""
    return min(quantiles, key=lambda k: abs(_level_of(k) - 0.5))


def abstaining_forecast(*, instrument_key: str, market: str,
                        timeframes: Sequence[str], input_start: datetime,
                        input_end: datetime, created_at: datetime,
                        horizon: int, decision: AbstentionDecision,
                        model_version: str, checkpoint_hash: str,
                        dataset_version: str, feature_schema_version: str,
                        warnings: Sequence[str] = ()) -> NativeForecast:
    """A forecast that declines, carrying nothing it did not measure.

    Deliberately does *not* fill the task fields with zeros. An abstaining
    record with `drawdown_probability=0.0` would let a caller that ignored
    `abstained` read a claim of safety, which is the exact failure abstention
    exists to prevent.
    """
    return NativeForecast(
        instrument_key=instrument_key, market=market,
        timeframes=tuple(timeframes), input_start=input_start,
        input_end=input_end, created_at=created_at, horizon=horizon,
        model_version=model_version, checkpoint_hash=checkpoint_hash,
        dataset_version=dataset_version,
        feature_schema_version=feature_schema_version,
        uncertainty=1.0, abstention=decision, tasks_present=(),
        warnings=tuple(warnings), data_quality=DataQuality.DEGRADED)


def regime_distribution(probabilities: Sequence[float]) -> dict[str, float]:
    """Map a probability vector onto the regime labels, in the fixed order."""
    if len(probabilities) != len(REGIME_CLASSES):
        raise ConfigurationError(
            "regime probability vector does not match the label set",
            got=len(probabilities), expected=len(REGIME_CLASSES))
    return {name: float(p) for name, p in zip(REGIME_CLASSES, probabilities)}


__all__ = ["FORECAST_SCHEMA_VERSION", "CalibrationInfo", "NativeForecast",
           "abstaining_forecast", "regime_distribution"]

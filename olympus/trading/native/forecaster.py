"""The Olympus-native forecaster: the plug point into the existing domain.

Implements `forecast.Forecaster`, so it is interchangeable with the persistence,
drift and seasonal-naive baselines and with any third-party adapter. Nothing
downstream — the service, the cache, the signal layer, the evaluator, the risk
engine — learns that it exists.

Where the quantiles come from
-----------------------------
`ConditionalQuantileModel` emits per-step quantiles of cumulative log return in
one pass, and this class converts them to prices and fills in `ForecastResult`.
Two conversions are worth naming because both are easy to get subtly wrong:

**Log returns become prices by exponentiation**, in `QuantilePrediction.prices`,
not by adding a percentage. At 5% the difference is a fifth of a percent; at
size it is money.

**Quantiles become paths, and are labelled as constructions.** `ForecastResult`
carries `paths`, and honest quantile bands are not samples. Rather than fake an
ensemble, this forecaster emits one path per quantile band and says so in
`warnings`: a caller reading `n_paths` gets the number of bands, and the record
states what they are. Inventing sampled paths from a quantile model would put
unearned structure into anything that consumes them.

Uncertainty
-----------
`uncertainty` is derived from the width of the inner band relative to the
input window's realised volatility — a forecast whose 25–75 band is wider than
the market's own recent movement is a forecast carrying little information.
That is a real measurement, unlike the baselines' pinned 1.0, and it is what a
risk engine sizes against.

Abstention
----------
Every `Declined` reason from the estimator becomes an abstaining
`ForecastResult` with the reason preserved. `signals.py` already turns an
abstention into *no signal* rather than a flat one, so a declining native model
costs nothing downstream and is visible in the record.
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from ..clock import Clock
from ..contracts import (Candle, DataQuality, ForecastPath, ForecastResult,
                         ensure_utc, jsonable)
from ..errors import ConfigurationError, InsufficientDataError
from ..features import FeatureBuilder
from ..forecast import Forecaster
from .checkpoint import NativeCheckpoint
from .quantile import ConditionalQuantileModel, Declined, QuantilePrediction
from .state import MarketState, MarketStateBuilder

#: Estimator kinds this forecaster can serve. A registry rather than an
#: `if`-chain so that adding an estimator is one entry and a name, and so an
#: unknown `model_kind` fails with the list of what *is* known rather than
#: falling through to whichever branch happened to be last.
def _estimators() -> dict:
    from .quantile import ConditionalQuantileModel as _Statistical
    out = {_Statistical.KIND: _Statistical}
    try:
        from .neural import NeuralQuantileModel as _Neural
        out[_Neural.KIND] = _Neural
    except Exception:                                    # noqa: BLE001
        # torch absent. The statistical estimator still serves, which is the
        # whole reason the native package does not require a tensor library.
        pass
    return out

#: Reported in `failure_code` when the estimator declines. Distinct from the
#: data-condition codes in `forecast.py` because this is the model choosing not
#: to speak about an input it has no basis for, not the input being bad.
CODE_MODEL_DECLINED = "NATIVE_MODEL_DECLINED"


def _bar_delta(bars: Sequence[Candle], timeframe: str) -> timedelta | None:
    """Spacing between bars, from the instrument grid when parseable."""
    try:
        from ..instruments import parse_timeframe
        return parse_timeframe(timeframe)
    except Exception:                                    # noqa: BLE001
        if len(bars) >= 2:
            return bars[-1].ts_close - bars[-2].ts_close
        return None


def _log_returns(closes: Sequence[float]) -> list[float]:
    return [math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes))
            if closes[i] > 0 and closes[i - 1] > 0]


class NativeForecaster(Forecaster):
    """An Olympus-trained model behind the standard forecasting interface."""

    MODEL_VERSION = "olympus-native/unfitted"

    def __init__(self, checkpoint: NativeCheckpoint, *,
                 state_builder: MarketStateBuilder | None = None,
                 name: str = "olympus-native",
                 purpose: "quarantine.Purpose | str" = None,
                 clock: Clock | None = None):
        # The quarantine is checked *first*, before anything is built. A
        # forecaster that exists can be handed somewhere its constructor never
        # saw, so the refusal has to be at construction rather than at use.
        # The default is the safest purpose, so a call site that forgets the
        # argument gets research and not live.
        from . import quarantine
        self.purpose = quarantine.assert_purpose(
            quarantine.Purpose.RESEARCH if purpose is None else purpose)
        super().__init__(name=name, clock=clock)
        if not isinstance(checkpoint, NativeCheckpoint):
            raise ConfigurationError(
                "NativeForecaster requires a NativeCheckpoint: a forecaster "
                "without a manifest cannot say what it is",
                got=type(checkpoint).__name__)
        self.checkpoint = checkpoint
        estimators = _estimators()
        estimator = estimators.get(checkpoint.model_kind)
        if estimator is None:
            raise ConfigurationError(
                "no estimator can serve this checkpoint's model_kind",
                model_kind=checkpoint.model_kind, known=sorted(estimators))
        self.model = estimator.from_parameters(checkpoint.parameters)
        #: A window-consuming estimator reads bars; a feature-consuming one
        #: reads a `MarketState`. Decided once here rather than per forecast.
        self.consumes_bars = hasattr(self.model, "predict_from_bars")
        self.state_builder = state_builder or MarketStateBuilder(
            features=FeatureBuilder(), clock=self.clock)

        # A model served against a different feature set than it was trained on
        # is silently, totally wrong — so this is checked once, loudly, at
        # construction rather than per forecast.
        if not self.consumes_bars:
            trained_on = set(checkpoint.manifest.feature_names)
            available = set(self.state_builder.feature_names)
            missing = sorted(set(self.model.config.conditioners) - available)
            if missing:
                raise ConfigurationError(
                    "the state builder cannot supply features this checkpoint "
                    "was trained on", missing=missing,
                    trained_on=sorted(trained_on), available=sorted(available))

    # -- identity ----------------------------------------------------------

    @property
    def model_version(self) -> str:
        return self.checkpoint.model_version

    @property
    def params(self) -> Mapping[str, Any]:
        return {**self.model.config.to_dict(),
                "checkpoint_id": self.checkpoint.checkpoint_id,
                "content_hash": self.checkpoint.content_hash[:12]}

    @property
    def required_bars(self) -> int:
        """Enough for the longest-warm-up conditioning feature.

        Taken from the feature registry rather than guessed, so adding a
        50-bar feature raises this automatically instead of producing a model
        that quietly abstains on every short window.
        """
        if self.consumes_bars:
            # A window encoder needs exactly its lookback: more would be
            # truncated, fewer cannot be padded honestly.
            return int(self.model.config.lookback)
        needed = [self.state_builder.features.min_bars(name)
                  for name in self.model.config.conditioners
                  if name in self.state_builder.feature_names]
        return max(needed) if needed else 1

    @property
    def deterministic(self) -> bool:
        """True. Lookup and interpolation; no RNG anywhere in the path."""
        return True

    @property
    def experimental(self) -> bool:
        """Computed from the open blockers. No argument sets this.

        True today, and it will stay true until `quarantine.BLOCKERS` records
        that the location bias is fixed and the model beats the baselines it
        currently loses to.
        """
        from . import quarantine
        return quarantine.experimental()

    @property
    def production_eligible(self) -> bool:
        """Computed. Approving this model in a registry does not change it."""
        from . import quarantine
        return quarantine.production_eligible()

    @property
    def open_blockers(self) -> tuple[str, ...]:
        from . import quarantine
        return tuple(b.id for b in quarantine.open_blockers())

    def assert_usable(self, mode: Any) -> None:
        """Refuse a live mode. A second, independent check to `assert_purpose`.

        Two checks that can each fail on their own is the difference between a
        quarantine and one `if` somebody deletes.
        """
        from . import quarantine
        quarantine.assert_not_live(mode)

    def _identity(self) -> dict:
        from . import quarantine
        return {"kind": "olympus-native",
                "model_kind": self.checkpoint.model_kind,
                "checkpoint_id": self.checkpoint.checkpoint_id,
                "content_hash": self.checkpoint.content_hash,
                "data_hash": self.checkpoint.manifest.data_hash,
                "seed": self.checkpoint.manifest.seed,
                "code_version": self.checkpoint.manifest.code_version,
                "owner": "olympus",
                # Carried in the identity so it reaches every audit record,
                # every model card and every describe() a reviewer might read,
                # rather than living only in a document nobody opens.
                "experimental": self.experimental,
                "production_eligible": self.production_eligible,
                "purpose": self.purpose.value,
                "open_blockers": list(self.open_blockers),
                "quarantine": quarantine.summary()}

    # -- the one method that matters ---------------------------------------

    def forecast(self, candles: Sequence[Candle], *,
                 as_of: datetime | None = None,
                 instrument_key: str = "",
                 timeframe: str = "") -> ForecastResult:
        now = (ensure_utc(as_of, field_name="as_of") if as_of is not None
               else self.clock.now())
        bars = [c for c in (candles or ())]
        key = instrument_key or (bars[-1].instrument_key if bars else "")
        tf = timeframe or (bars[-1].timeframe if bars else "")
        horizon = self.model.config.horizon

        def abstain(reason: str, code: str, *,
                    quality: DataQuality = DataQuality.UNUSABLE,
                    warnings: Sequence[str] = ()) -> ForecastResult:
            start = bars[0].ts_open if bars else now
            end = bars[-1].ts_close if bars else now
            start = min(start, now)
            end = min(max(end, start), now)
            return ForecastResult.abstain(
                instrument_key=key, timeframe=tf, input_start=start,
                input_end=end, created_at=now, horizon=horizon,
                reason=reason, code=code, data_quality=quality,
                warnings=tuple(warnings), model_version=self.model_version,
                model_identity=self._identity(),
                inference_params=jsonable(dict(self.params)))

        if not bars:
            return abstain("no candles supplied", "DATA_INSUFFICIENT")
        if len(bars) < self.required_bars:
            return abstain(
                f"{len(bars)} bars is below the {self.required_bars} this "
                "model's conditioning features need", "DATA_INSUFFICIENT")
        if bars[-1].ts_close > now:
            return abstain(
                "the last input bar closes after as_of, so this window could "
                "not have been observed", "FORECAST_LOOKAHEAD")
        if not bars[-1].is_final:
            return abstain(
                "the last bar is still forming; its close is not yet the close",
                "DATA_VALIDATION_FAILED")

        try:
            state = self.state_builder.build({tf: bars}, as_of=now,
                                             instrument_key=key,
                                             base_timeframe=tf)
        except Exception as exc:                         # noqa: BLE001
            return abstain(f"market state could not be built: {exc}",
                           "DATA_VALIDATION_FAILED")

        if self.consumes_bars:
            # Exactly the lookback the encoder was built for, taken from the
            # end of the window.
            prediction = self.model.predict_from_bars(
                bars[-self.model.config.lookback:])
        else:
            prediction = self.model.predict(dict(state.base.features))
        if isinstance(prediction, Declined):
            # Not an error. The model has no basis for this input and says so,
            # which is worth more than a confident guess.
            return abstain(str(prediction), CODE_MODEL_DECLINED,
                           quality=DataQuality.OK)

        return self._assemble(state, prediction, bars=bars, now=now,
                              key=key, tf=tf)

    # -- result construction ------------------------------------------------

    def _assemble(self, state: MarketState, prediction: QuantilePrediction, *,
                  bars: Sequence[Candle], now: datetime, key: str, tf: str
                  ) -> ForecastResult:
        anchor = state.last_close
        by_label = prediction.prices(anchor)
        median_label = prediction.median_label
        median = by_label[median_label]
        horizon = len(median)

        # One path per quantile band. Declared as a construction in `warnings`
        # rather than presented as sampled futures.
        paths = []
        for label in sorted(by_label):
            closes = by_label[label]
            opens = [anchor] + list(closes[:-1])
            paths.append(ForecastPath(
                closes=tuple(closes), opens=tuple(opens),
                highs=tuple(max(o, c) for o, c in zip(opens, closes)),
                lows=tuple(min(o, c) for o, c in zip(opens, closes)),
                volumes=tuple([statistics.fmean([b.volume for b in bars])]
                              * horizon)))

        return_path = tuple(p / anchor - 1.0 for p in median)
        total_return = return_path[-1]

        realised = 0.0
        closes = [b.close for b in bars]
        returns = _log_returns(closes)
        if len(returns) >= 2:
            realised = statistics.pstdev(returns)

        # Forecast dispersion: the terminal spread of the outer band, converted
        # to a return. A real measurement, unlike the baselines' pinned zero.
        labels = sorted(by_label)
        low_band = by_label[labels[0]][-1]
        high_band = by_label[labels[-1]][-1]
        forecast_volatility = abs(high_band - low_band) / anchor / 2.0

        uncertainty = self._uncertainty(forecast_volatility, realised, horizon)
        probs = self._direction_probabilities(prediction, anchor)

        delta = _bar_delta(bars, tf)
        stamps: tuple[datetime, ...] = ()
        if delta is not None:
            last = bars[-1].ts_close
            stamps = tuple(last + delta * (i + 1) for i in range(horizon))

        return ForecastResult(
            instrument_key=key, timeframe=tf,
            input_start=bars[0].ts_open, input_end=bars[-1].ts_close,
            created_at=now, horizon=horizon, forecast_timestamps=stamps,
            paths=tuple(paths), mean_close=tuple(median),
            median_close=tuple(median),
            quantiles={label: tuple(values) for label, values in by_label.items()},
            expected_return=total_return, expected_return_path=return_path,
            forecast_volatility=forecast_volatility,
            realised_volatility=realised,
            direction_probabilities=probs, uncertainty=uncertainty,
            model_version=self.model_version, model_identity=self._identity(),
            inference_params=jsonable({**dict(self.params),
                                       "cell": list(prediction.cell),
                                       "cell_samples": prediction.cell_samples}),
            data_quality=state.data_quality,
            warnings=self._warnings(prediction))

    @staticmethod
    def _warnings(prediction: QuantilePrediction) -> tuple[str, ...]:
        out = ["paths are quantile bands, not sampled futures: each path "
               "traces one quantile through the horizon"]
        if prediction.cell:
            out.append(f"conditioned on cell {prediction.cell} with "
                       f"{prediction.cell_samples} training observations")
        return tuple(out)

    @staticmethod
    def _uncertainty(forecast_volatility: float, realised: float,
                     horizon: int) -> float:
        """Band width relative to the market's own recent movement, in [0, 1].

        Scaled by `sqrt(horizon)` because a random walk's dispersion grows that
        way: a band that widens with the square root of time is carrying the
        same information at every horizon, and should not read as increasingly
        uncertain simply for being further out.
        """
        if realised <= 0 or not math.isfinite(realised):
            # No comparison available. 1.0 — no information — rather than 0.0,
            # so an unmeasurable case never reads as confident.
            return 1.0
        expected = realised * math.sqrt(max(1, horizon))
        if expected <= 0:                                # pragma: no cover
            return 1.0
        ratio = forecast_volatility / expected
        return max(0.0, min(1.0, ratio))

    @staticmethod
    def _direction_probabilities(prediction: QuantilePrediction, anchor: float
                                 ) -> dict[str, float]:
        """Where the terminal distribution sits relative to no change.

        Read off the quantile ladder: the fraction of quantile levels whose
        terminal value is above the anchor approximates P(up). Coarse — it has
        the resolution of the ladder — and honest about it, rather than a
        parametric assumption the model never made.
        """
        terminal = {label: values[-1]
                    for label, values in prediction.log_return_quantiles.items()}
        if not terminal:                                 # pragma: no cover
            return {"up": 0.0, "down": 0.0, "flat": 1.0}
        tolerance = 1e-9
        up = sum(1 for v in terminal.values() if v > tolerance)
        down = sum(1 for v in terminal.values() if v < -tolerance)
        flat = len(terminal) - up - down
        total = len(terminal)
        return {"up": up / total, "down": down / total, "flat": flat / total}

    def __repr__(self) -> str:                          # pragma: no cover
        return (f"NativeForecaster(checkpoint={self.checkpoint.checkpoint_id!r}, "
                f"h={self.model.config.horizon})")


__all__ = ["CODE_MODEL_DECLINED", "NativeForecaster"]

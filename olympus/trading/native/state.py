"""The Olympus-native market-state representation.

What a native model observes, before any modelling decision is made about it.

`MarketState` is deliberately *richer* than a matrix of bars. Olympus already
owns a tested causal feature layer, a regime classifier and six volatility
estimators; a representation that threw those away in order to be a bare OHLCV
tensor would be a worse representation chosen for somebody else's input arity.

Three properties are enforced rather than intended:

**Causality.** Every value is computable from bars closing at or before
`as_of`. `features.FeatureBuilder.build` already refuses a window containing a
non-final bar or a bar past the clock; `assert_causal` in the same module is the
independent check, and the native training pipeline runs it.

**Multi-scale by construction.** A state carries one `ScaleObservation` per
timeframe, each with its own features and its own bar count. Single-timeframe is
the degenerate case of one scale, not a different code path — so the multi-scale
work in a later phase changes the model, not the contract.

**Missing is missing.** A feature that could not be computed is absent from the
mapping. It is never zero-filled, because a zero-filled RSI is a real number
that a model will happily learn from, and the resulting error is invisible.
`features.FeatureSet` takes the same position and raises on a non-finite value.

Nothing here imports torch, numpy, or any model. A `MarketState` is data.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from ..clock import Clock, default_clock
from ..contracts import Candle, DataQuality, Regime, ensure_utc, jsonable
from ..errors import ConfigurationError, DataValidationError
from ..features import FeatureBuilder, FeatureSet

#: Schema version of the state record. Bumped when the *meaning* of a field
#: changes, so a checkpoint trained against an older schema is detectable
#: rather than silently mis-fed.
STATE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ScaleObservation:
    """What one timeframe contributes to the state."""

    timeframe: str
    #: Close of the last bar at this scale — the instant these values became
    #: observable.
    ts: datetime
    last_close: float
    n_bars: int
    features: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self):
        if not str(self.timeframe).strip():
            raise ConfigurationError("a scale must name its timeframe")
        object.__setattr__(self, "ts", ensure_utc(self.ts, field_name="ts"))
        if self.n_bars < 1:
            raise DataValidationError("a scale needs at least one bar",
                                      timeframe=self.timeframe)
        close = float(self.last_close)
        if close <= 0:
            raise DataValidationError(
                "last_close must be positive; a non-positive price propagates "
                "into a return and out the other side as a plausible signal",
                timeframe=self.timeframe, last_close=close)
        object.__setattr__(self, "last_close", close)
        object.__setattr__(self, "features", dict(self.features))

    def to_dict(self) -> dict:
        return {"timeframe": self.timeframe, "ts": jsonable(self.ts),
                "last_close": self.last_close, "n_bars": self.n_bars,
                "features": dict(self.features)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ScaleObservation":
        return cls(timeframe=str(data["timeframe"]),
                   ts=datetime.fromisoformat(data["ts"]),
                   last_close=float(data["last_close"]),
                   n_bars=int(data["n_bars"]),
                   features=data.get("features") or {})


@dataclass(frozen=True)
class MarketState:
    """Everything a native model may condition on, at one instant."""

    instrument_key: str
    as_of: datetime
    #: Keyed by timeframe. The `base_timeframe` scale is the one forecasts are
    #: expressed in; the others are context.
    scales: Mapping[str, ScaleObservation] = field(default_factory=dict)
    base_timeframe: str = ""
    regime: Regime = Regime.UNKNOWN
    #: Annualisation-free realised volatility at the base scale.
    realised_volatility: float | None = None
    data_quality: DataQuality = DataQuality.OK
    #: Identifies the exact bars this was built from, so a state can be tied
    #: back to a dataset version.
    data_version: str = ""
    schema_version: int = STATE_SCHEMA_VERSION

    def __post_init__(self):
        if not str(self.instrument_key).strip():
            raise ConfigurationError("a market state must name its instrument")
        object.__setattr__(self, "as_of",
                           ensure_utc(self.as_of, field_name="as_of"))
        object.__setattr__(self, "regime", Regime(self.regime))
        object.__setattr__(self, "data_quality", DataQuality(self.data_quality))
        object.__setattr__(self, "scales", dict(self.scales))
        if not self.scales:
            raise DataValidationError("a market state needs at least one scale",
                                      instrument_key=self.instrument_key)
        base = self.base_timeframe or min(self.scales)
        if base not in self.scales:
            raise ConfigurationError("base_timeframe is not among the scales",
                                     base_timeframe=base,
                                     scales=sorted(self.scales))
        object.__setattr__(self, "base_timeframe", base)

        # The causality invariant, checked rather than trusted. A scale whose
        # last bar closes after `as_of` is a scale that saw the future.
        for timeframe, scale in self.scales.items():
            if scale.timeframe != timeframe:
                raise ConfigurationError(
                    "scale key and its timeframe disagree",
                    key=timeframe, timeframe=scale.timeframe)
            if scale.ts > self.as_of:
                raise DataValidationError(
                    "a scale closes after as_of, so this state could not have "
                    "existed at the time it claims",
                    timeframe=timeframe, scale_ts=scale.ts, as_of=self.as_of)

    # -- accessors ---------------------------------------------------------

    @property
    def base(self) -> ScaleObservation:
        return self.scales[self.base_timeframe]

    @property
    def last_close(self) -> float:
        return self.base.last_close

    def feature(self, name: str, *, timeframe: str | None = None
               ) -> float | None:
        """One feature, or `None` when it was not computable.

        `None` rather than a default: a caller that needs a default should pick
        one knowingly, at the point where it knows what a missing RSI means for
        its own logic.
        """
        scale = self.scales.get(timeframe or self.base_timeframe)
        return None if scale is None else scale.features.get(name)

    def feature_vector(self, names: Sequence[str], *,
                       timeframe: str | None = None) -> tuple[float | None, ...]:
        """Features in the caller's order, `None` where absent.

        The order is always the caller's. A model trained on one iteration
        order and served on another is a total, silent corruption of every
        prediction, and dict ordering is exactly the sort of thing that changes
        between two runs for reasons nobody notices.
        """
        return tuple(self.feature(n, timeframe=timeframe) for n in names)

    @property
    def available_features(self) -> tuple[str, ...]:
        return tuple(sorted(self.base.features))

    def missing(self, names: Sequence[str], *,
                timeframe: str | None = None) -> tuple[str, ...]:
        """Which of `names` this state cannot supply. Reported, not filled."""
        return tuple(n for n in names
                     if self.feature(n, timeframe=timeframe) is None)

    @property
    def usable(self) -> bool:
        return self.data_quality is not DataQuality.UNUSABLE

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict:
        return {"instrument_key": self.instrument_key,
                "as_of": jsonable(self.as_of),
                "scales": {k: v.to_dict() for k, v in sorted(self.scales.items())},
                "base_timeframe": self.base_timeframe,
                "regime": self.regime.value,
                "realised_volatility": self.realised_volatility,
                "data_quality": self.data_quality.value,
                "data_version": self.data_version,
                "schema_version": self.schema_version}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MarketState":
        return cls(
            instrument_key=str(data["instrument_key"]),
            as_of=datetime.fromisoformat(data["as_of"]),
            scales={k: ScaleObservation.from_dict(v)
                    for k, v in (data.get("scales") or {}).items()},
            base_timeframe=str(data.get("base_timeframe", "")),
            regime=Regime(data.get("regime", Regime.UNKNOWN.value)),
            realised_volatility=(None if data.get("realised_volatility") is None
                                 else float(data["realised_volatility"])),
            data_quality=DataQuality(data.get("data_quality", DataQuality.OK.value)),
            data_version=str(data.get("data_version", "")),
            schema_version=int(data.get("schema_version", STATE_SCHEMA_VERSION)))

    def fingerprint(self) -> str:
        """Content hash. Two states with the same fingerprint are the same
        observation, which is what makes a cached forecast safe to reuse."""
        return hashlib.sha256(
            json.dumps(self.to_dict(), sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]

    def __repr__(self) -> str:                          # pragma: no cover
        return (f"MarketState({self.instrument_key} @ {self.as_of.isoformat()}, "
                f"scales={sorted(self.scales)}, regime={self.regime.value})")


class MarketStateBuilder:
    """Turns candle windows into `MarketState`s. Stateless and deterministic.

    Holds a `FeatureBuilder` whose registered names are recorded in every
    checkpoint manifest — a model trained against a feature set that was
    whatever happened to be registered that day is not reproducible, and
    "the features changed" is the hardest trading regression to find afterwards.
    """

    def __init__(self, *, features: FeatureBuilder | None = None,
                 clock: Clock | None = None,
                 regime_classifier: Any = None):
        self.features = features or FeatureBuilder()
        self.clock = clock or default_clock()
        #: Optional `regime.RegimeClassifier`. Absent means `Regime.UNKNOWN`,
        #: which is honest: no classifier means no classification, not "calm".
        self.regime_classifier = regime_classifier

    @property
    def feature_names(self) -> tuple[str, ...]:
        return self.features.names()

    def build(self, windows: Mapping[str, Sequence[Candle]], *,
              as_of: datetime | None = None,
              instrument_key: str = "",
              base_timeframe: str = "",
              data_quality: DataQuality = DataQuality.OK,
              data_version: str = "") -> MarketState:
        """One state from one candle window per timeframe.

        `as_of` defaults to the latest bar close across the scales rather than
        to wall time. A state built from historical bars during a backtest must
        be stamped with the instant it *could* have existed, not the instant the
        backtest happened to run.
        """
        if not windows:
            raise DataValidationError("no candle windows supplied")

        scales: dict[str, ScaleObservation] = {}
        latest: datetime | None = None
        key = instrument_key

        for timeframe, candles in windows.items():
            bars = list(candles or ())
            if not bars:
                raise DataValidationError("empty candle window",
                                          timeframe=timeframe)
            key = key or bars[-1].instrument_key
            feature_set: FeatureSet = self.features.build(bars, clock=_AtLast(bars[-1].ts_close))
            scales[timeframe] = ScaleObservation(
                timeframe=timeframe, ts=bars[-1].ts_close,
                last_close=bars[-1].close, n_bars=len(bars),
                features=dict(feature_set.values))
            latest = bars[-1].ts_close if latest is None else max(latest, bars[-1].ts_close)

        stamp = ensure_utc(as_of, field_name="as_of") if as_of is not None else latest
        base = base_timeframe or min(scales)

        realised = None
        base_bars = list(windows[base])
        if len(base_bars) >= 3:
            from ..volatility import realised_volatility
            closes = [c.close for c in base_bars]
            # Per-bar sigma: `annualisation` stays at 1.0 because the model
            # works in per-bar log-return space, and annualising here would
            # rescale a number the estimator never sees annualised.
            window = max(2, min(20, len(closes) - 1))
            series = realised_volatility(closes, window)
            realised = next((v for v in reversed(series) if v is not None), None)

        regime = Regime.UNKNOWN
        if self.regime_classifier is not None:
            try:
                state = self.regime_classifier.classify(base_bars)
                regime = getattr(state, "regime", Regime.UNKNOWN)
            except Exception:                            # noqa: BLE001
                # A classifier that cannot classify leaves the field UNKNOWN.
                # Guessing here would put a fabricated regime label into a
                # training target.
                regime = Regime.UNKNOWN

        return MarketState(
            instrument_key=key, as_of=stamp, scales=scales,
            base_timeframe=base, regime=regime, realised_volatility=realised,
            data_quality=data_quality, data_version=data_version)


class _AtLast(Clock):
    """A clock pinned to a bar's close, so `FeatureBuilder.build` accepts a
    historical window without the caller having to disable its clock check.

    The check exists to catch a backtest that advanced its data ahead of time.
    Pinning rather than bypassing keeps the check active for every *other*
    ordering error the builder looks for.
    """

    __slots__ = ("_when",)

    def __init__(self, when: datetime):
        self._when = ensure_utc(when, field_name="when")

    def now(self) -> datetime:
        return self._when

    def monotonic_ns(self) -> int:                       # pragma: no cover
        return int(self._when.timestamp() * 1e9)


__all__ = ["STATE_SCHEMA_VERSION", "ScaleObservation", "MarketState",
           "MarketStateBuilder"]

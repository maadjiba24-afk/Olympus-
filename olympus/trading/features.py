"""Feature engineering — causal by construction, scaled without leakage.

Everything a model or a strategy sees about the market passes through here, and
the module exists to make two specific lies impossible to tell.

**Lie one: a feature that peeks.** A feature computed at bar *t* must depend on
bars ≤ *t* and nothing else. The API enforces this structurally — a feature
function receives *a window ending at the bar it is being computed for* and
literally cannot index past it — and `assert_causal` verifies it empirically
for series-valued transforms, where look-ahead actually enters (centred
averages, a resampler that back-fills, a normaliser fitted on everything). A
peeking feature does not crash. It produces a backtest that looks excellent and
a live account that does not, and the gap between them is the size of the peek.

**Lie two: normalisation fitted on the future.** Scaling statistics must be
computed on a training window and *applied* to later windows, never refitted.
This is not a hypothetical: the Kronos teardown (docs/KRONOS_TEARDOWN.md §12.9)
records exactly this bug surviving in `finetune_csv` after being fixed in
`finetune/` — `CustomKlineDataset.__getitem__` normalises across the full
``lookback + predict + 1`` window, so the mean and standard deviation of the
horizon the model is asked to predict are baked into the input it predicts
from. The fix (`finetune/dataset.py`, commit `79d6d40`, issue #227) was never
ported. Here the split is structural rather than a matter of discipline:
`fit_scaler` returns a *frozen* `ScalerStats`, and `transform` has no path back
to refitting. `causal_window_normalise` is the direct antidote for the Kronos
case.

Missing is missing
------------------
A feature that cannot be computed (too few bars) is **absent from the
FeatureSet**, not zero and not NaN. Zero-filling teaches a model that "no data"
looks like "value happened to be zero", which is a real and directional signal
at the start of every backtest window. Callers must decide what to do about an
absent feature; they cannot accidentally consume a fabricated one.
"""

from __future__ import annotations

import math
from collections.abc import Mapping as _MappingABC, Sequence as _SequenceABC
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import ta
from .clock import Clock, default_clock
from .contracts import Candle, ensure_utc, jsonable
from .errors import ConfigurationError, DataValidationError

#: A feature function: given a candle window ending at the bar being computed,
#: return the feature's value there, or ``None`` when it is not computable.
FeatureFn = Callable[[Sequence[Candle]], Optional[float]]
#: A series transform, aligned to its input — what `assert_causal` checks.
SeriesFn = Callable[[Sequence[Candle]], Sequence[Optional[float]]]


# ---------------------------------------------------------------------------
# FeatureSet
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FeatureSet:
    """The features knowable at one instant, for one instrument.

    `ts` is the **close** of the last bar that fed it — the earliest moment at
    which every value here was observable. Carrying the timestamp inside the
    feature vector (rather than alongside it) is what lets a downstream join
    against forecasts, signals and fills be checked for ordering instead of
    assumed.
    """
    instrument_key: str
    timeframe: str
    ts: datetime
    values: Dict[str, float] = field(default_factory=dict)

    def __post_init__(self):
        if not self.instrument_key or not str(self.instrument_key).strip():
            raise DataValidationError("FeatureSet instrument_key must be non-empty")
        if not self.timeframe or not str(self.timeframe).strip():
            raise DataValidationError("FeatureSet timeframe must be non-empty")
        object.__setattr__(self, "ts", ensure_utc(self.ts, field_name="ts"))
        if not isinstance(self.values, _MappingABC):
            raise DataValidationError("FeatureSet values must be a mapping",
                                      got=type(self.values).__name__)
        cleaned: Dict[str, float] = {}
        for name, value in self.values.items():
            if not isinstance(name, str) or not name.strip():
                raise DataValidationError("feature names must be non-empty strings",
                                          got=repr(name))
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise DataValidationError("feature values must be numeric",
                                          feature=name, got=type(value).__name__)
            number = float(value)
            if not math.isfinite(number):
                # A NaN feature is a bug in the feature, not a missing value.
                # Dropping it here would hide the bug; letting it through puts
                # a NaN into a model weight several layers away from its cause.
                raise DataValidationError("feature values must be finite",
                                          feature=name, got=repr(value))
            cleaned[name] = number
        # Copy so a caller mutating the dict it passed in cannot retroactively
        # change a feature vector that has already been recorded.
        object.__setattr__(self, "values", cleaned)

    def get(self, name: str, default: Optional[float] = None) -> Optional[float]:
        """Feature `name`, or `default` when it was not computable."""
        return self.values.get(name, default)

    def __contains__(self, name: object) -> bool:
        return name in self.values

    def to_dict(self) -> dict:
        return {k: jsonable(v) for k, v in asdict(self).items()}

    def vector(self, names: Sequence[str], *,
               default: Optional[float] = None) -> List[Optional[float]]:
        """Feature values in a caller-specified order.

        The order is always the caller's, never the dict's: a model trained on
        one iteration order and served on another is a silent, total corruption
        of every prediction, and dict ordering is exactly the kind of thing
        that changes when a feature is added.
        """
        return [self.values.get(name, default) for name in names]


# ---------------------------------------------------------------------------
# built-in feature functions
# ---------------------------------------------------------------------------
# Each takes a candle window *ending at the bar being computed* and returns the
# value there. They index from the right (``[-1]``, ``[-2]``) precisely so that
# the window length is irrelevant to the result — that property is what makes
# `FeatureBuilder.series` produce a causal series and what `assert_causal`
# verifies.

# How far back the *recursively smoothed* features look. Fixed, not "all the
# history the caller happened to pass". EMA- and Wilder-smoothed indicators are
# seeded from the start of their input, so their value at a given bar depends on
# where the series began: the same bar scores differently in a training run with
# five years of data and in a live loop with a 500-bar ring buffer, and the model
# then sees a feature it was never trained on. Capping the lookback makes each
# feature a function of the last N bars and nothing else. N is set so the seed's
# influence has decayed below ~1e-6 relative — (13/14)^186 for the Wilder ones,
# (25/27)^274 for MACD — which is far below the noise in the underlying prices.
_WILDER_LOOKBACK = 200          # RSI(14), ATR(14)
_MACD_LOOKBACK = 300            # EMA(26) inside MACD(12, 26, 9)

def _return_over(candles: Sequence[Candle], lag: int) -> Optional[float]:
    if len(candles) < lag + 1:
        return None
    base = candles[-1 - lag].close
    return None if base == 0.0 else candles[-1].close / base - 1.0


def _returns_1(candles: Sequence[Candle]) -> Optional[float]:
    """Simple return over the last bar."""
    return _return_over(candles, 1)


def _returns_5(candles: Sequence[Candle]) -> Optional[float]:
    """Simple return over the last 5 bars."""
    return _return_over(candles, 5)


def _returns_20(candles: Sequence[Candle]) -> Optional[float]:
    """Simple return over the last 20 bars."""
    return _return_over(candles, 20)


def _log_return(candles: Sequence[Candle]) -> Optional[float]:
    """Log return over the last bar — additive across time, unlike `returns_1`,
    which is why the volatility features are built from it."""
    if len(candles) < 2:
        return None
    previous, current = candles[-2].close, candles[-1].close
    if previous <= 0.0 or current <= 0.0:
        return None
    return math.log(current / previous)


def _realised_vol_20(candles: Sequence[Candle]) -> Optional[float]:
    """Population standard deviation of the last 20 log returns, **per bar**.

    Deliberately not annualised. The annualisation factor depends on the
    timeframe and the venue's calendar (crypto 24/7 vs an equity session), and
    baking one in here would make the feature silently wrong for every other
    instrument class. The regime layer annualises where it knows the calendar.
    """
    if len(candles) < 21:
        return None
    window = candles[-21:]
    log_returns: List[float] = []
    for i in range(1, len(window)):
        previous, current = window[i - 1].close, window[i].close
        if previous <= 0.0 or current <= 0.0:
            return None
        log_returns.append(math.log(current / previous))
    mean = math.fsum(log_returns) / len(log_returns)
    variance = math.fsum((x - mean) ** 2 for x in log_returns) / len(log_returns)
    return math.sqrt(max(0.0, variance))


def _atr_pct(candles: Sequence[Candle]) -> Optional[float]:
    """ATR(14) as a fraction of price — comparable across instruments, which
    raw ATR is not."""
    if len(candles) < 15:
        return None
    value = ta.atr(candles[-_WILDER_LOOKBACK:], 14)[-1]
    close = candles[-1].close
    return None if value is None or close <= 0.0 else value / close


def _rsi_14(candles: Sequence[Candle]) -> Optional[float]:
    if len(candles) < 15:
        return None
    return ta.rsi([c.close for c in candles[-_WILDER_LOOKBACK:]], 14)[-1]


def _macd_hist(candles: Sequence[Candle]) -> Optional[float]:
    if len(candles) < 34:
        return None
    return ta.macd([c.close for c in candles[-_MACD_LOOKBACK:]], 12, 26, 9)[2][-1]


def _bb_position(candles: Sequence[Candle]) -> Optional[float]:
    """Where price sits inside its Bollinger band: 0 at the lower band, 1 at
    the upper. Not clamped — a value above 1 is a genuine band break and that
    is the interesting case."""
    if len(candles) < 20:
        return None
    upper, _middle, lower = ta.bollinger(
        [c.close for c in candles[-20:]], 20, 2.0)
    top, bottom = upper[-1], lower[-1]
    if top is None or bottom is None:
        return None
    span = top - bottom
    if span <= 0.0:
        return 0.5
    return (candles[-1].close - bottom) / span


def _volume_zscore(candles: Sequence[Candle]) -> Optional[float]:
    """How unusual this bar's volume is against the last 20 bars."""
    if len(candles) < 20:
        return None
    return ta.zscore([c.volume for c in candles[-20:]], 20)[-1]


def _range_pct(candles: Sequence[Candle]) -> Optional[float]:
    """(high − low) / close for the current bar."""
    if not candles:
        return None
    close = candles[-1].close
    return None if close <= 0.0 else (candles[-1].high - candles[-1].low) / close


def _gap_pct(candles: Sequence[Candle]) -> Optional[float]:
    """Open-to-previous-close gap. Near zero on continuous markets; the whole
    point is that it is *not* near zero across a session break."""
    if len(candles) < 2:
        return None
    previous_close = candles[-2].close
    if previous_close <= 0.0:
        return None
    return (candles[-1].open - previous_close) / previous_close


def _trend_strength(candles: Sequence[Candle]) -> Optional[float]:
    """Signed efficiency ratio over 20 bars — see `ta.trend_strength`."""
    if len(candles) < 20:
        return None
    return ta.trend_strength([c.close for c in candles[-20:]], 20)[-1]


def _distance_from_sma_50(candles: Sequence[Candle]) -> Optional[float]:
    """Fractional distance of price above/below its 50-bar mean."""
    if len(candles) < 50:
        return None
    mean = ta.sma([c.close for c in candles[-50:]], 50)[-1]
    return None if mean is None or mean == 0.0 else candles[-1].close / mean - 1.0


#: name -> (function, minimum bars). The minimum is declared rather than
#: discovered so `FeatureBuilder.build` can skip a feature without calling it,
#: and so the warm-up requirement of a feature set is answerable up front.
BUILTIN_FEATURES: Tuple[Tuple[str, FeatureFn, int], ...] = (
    ("returns_1", _returns_1, 2),
    ("returns_5", _returns_5, 6),
    ("returns_20", _returns_20, 21),
    ("log_return", _log_return, 2),
    ("realised_vol_20", _realised_vol_20, 21),
    ("atr_pct", _atr_pct, 15),
    ("rsi_14", _rsi_14, 15),
    ("macd_hist", _macd_hist, 34),
    ("bb_position", _bb_position, 20),
    ("volume_zscore", _volume_zscore, 20),
    ("range_pct", _range_pct, 1),
    ("gap_pct", _gap_pct, 2),
    ("trend_strength", _trend_strength, 20),
    ("distance_from_sma_50", _distance_from_sma_50, 50),
)

DEFAULT_FEATURE_NAMES: Tuple[str, ...] = tuple(name for name, _, _ in BUILTIN_FEATURES)


# ---------------------------------------------------------------------------
# FeatureBuilder
# ---------------------------------------------------------------------------

class FeatureBuilder:
    """A named registry of feature functions evaluated over a candle window.

    Declarative on purpose: the set of features is data, so it can be recorded
    in the audit trail, diffed between two runs, and reproduced. A model
    trained against a feature set that was defined by whatever code happened to
    run that day is not reproducible, and "the features changed" is the hardest
    trading regression to diagnose after the fact.
    """

    def __init__(self, *, builtins: bool = True):
        self._fns: Dict[str, FeatureFn] = {}
        self._min_bars: Dict[str, int] = {}
        if builtins:
            for name, fn, min_bars in BUILTIN_FEATURES:
                self.register(name, fn, min_bars)

    # --- registry ----------------------------------------------------------

    def register(self, name: str, fn: FeatureFn, min_bars: int = 1, *,
                 replace: bool = False) -> "FeatureBuilder":
        """Add a feature. Returns self so registrations can be chained.

        Re-registering an existing name requires `replace=True`. Silent
        overwrite is how two modules end up disagreeing about what `momentum`
        means depending on import order.
        """
        if not isinstance(name, str) or not name.strip():
            raise ConfigurationError("feature name must be a non-empty string",
                                     name=repr(name))
        if not callable(fn):
            raise ConfigurationError("feature fn must be callable", name=name)
        if isinstance(min_bars, bool) or not isinstance(min_bars, int) or min_bars < 1:
            raise ConfigurationError("min_bars must be an int >= 1",
                                     name=name, min_bars=repr(min_bars))
        if name in self._fns and not replace:
            raise ConfigurationError("feature already registered; pass "
                                     "replace=True to override", name=name)
        self._fns[name] = fn
        self._min_bars[name] = min_bars
        return self

    def unregister(self, name: str) -> "FeatureBuilder":
        if name not in self._fns:
            raise ConfigurationError("feature is not registered", name=name)
        del self._fns[name]
        del self._min_bars[name]
        return self

    def names(self) -> Tuple[str, ...]:
        """Registered feature names, sorted — a stable order for every caller."""
        return tuple(sorted(self._fns))

    def min_bars(self, name: str) -> int:
        if name not in self._min_bars:
            raise ConfigurationError("feature is not registered", name=name)
        return self._min_bars[name]

    @property
    def warmup_bars(self) -> int:
        """Bars needed before *every* registered feature is computable."""
        return max(self._min_bars.values()) if self._min_bars else 0

    # --- evaluation --------------------------------------------------------

    def build(self, candles: Sequence[Candle],
              clock: Optional[Clock] = None) -> FeatureSet:
        """Compute every registered feature as of the last bar in `candles`.

        The window must be a single instrument, a single timeframe, strictly
        ascending in time, and every bar must be final. Each of those is a
        look-ahead vector rather than tidiness:

        * a non-final bar is the bar currently forming — its close is not yet
          the close, so any feature reading it is reading the future's first
          draft;
        * out-of-order bars make "the last bar" not the latest bar, and every
          right-indexed feature then silently computes as of the wrong instant;
        * mixing instruments or timeframes produces a feature vector labelled
          with one instrument and computed from another.

        `clock` is checked against the last bar's close: if the clock has not
        reached it, this window could not exist yet, which in a backtest means
        the caller advanced the data ahead of time. Defaults to wall time.
        """
        bars = self._validate_window(candles, clock)
        values: Dict[str, float] = {}
        for name in sorted(self._fns):
            if len(bars) < self._min_bars[name]:
                continue                       # absent, never zero-filled
            result = self._fns[name](bars)
            if result is None:
                continue
            if isinstance(result, bool) or not isinstance(result, (int, float)):
                raise DataValidationError("feature returned a non-number",
                                          feature=name,
                                          got=type(result).__name__)
            values[name] = float(result)       # FeatureSet rejects NaN/inf
        return FeatureSet(instrument_key=bars[-1].instrument_key,
                          timeframe=bars[-1].timeframe,
                          ts=bars[-1].ts_close, values=values)

    def series(self, name: str, candles: Sequence[Candle], *,
               clock: Optional[Clock] = None) -> List[Optional[float]]:
        """Feature `name` evaluated at every bar, aligned to `candles`.

        Each point is computed from the expanding window ``candles[:i+1]``, so
        this is the causal history of the feature and can be fed straight to
        `assert_causal`. It is O(n · cost(feature)) — fine for validation and
        for offline dataset construction, and not what a live loop should do.
        """
        if name not in self._fns:
            raise ConfigurationError("feature is not registered", name=name)
        bars = self._validate_window(candles, clock)
        fn = self._fns[name]
        minimum = self._min_bars[name]
        out: List[Optional[float]] = []
        for i in range(len(bars)):
            if i + 1 < minimum:
                out.append(None)
                continue
            result = fn(bars[:i + 1])
            out.append(None if result is None else float(result))
        return out

    def build_all(self, candles: Sequence[Candle], *,
                  clock: Optional[Clock] = None,
                  start: Optional[int] = None) -> List[FeatureSet]:
        """A FeatureSet per bar from `start` onwards (default: the first bar at
        which every feature is computable).

        This is the offline dataset builder. It uses expanding windows, so a
        row at bar *i* is exactly what `build` would have produced live at bar
        *i* — the property that makes an offline-trained model's inputs match
        its online ones.
        """
        bars = self._validate_window(candles, clock)
        first = self.warmup_bars - 1 if start is None else int(start)
        first = max(0, first)
        return [self.build(bars[:i + 1], clock) for i in range(first, len(bars))]

    # --- shared window validation -----------------------------------------

    @staticmethod
    def _validate_window(candles: Sequence[Candle],
                         clock: Optional[Clock]) -> List[Candle]:
        if isinstance(candles, (str, bytes)) or not isinstance(candles, _SequenceABC):
            raise DataValidationError("candles must be a sequence",
                                      got=type(candles).__name__)
        bars = list(candles)
        if not bars:
            raise DataValidationError("cannot build features from zero candles")
        for index, candle in enumerate(bars):
            if not isinstance(candle, Candle):
                raise DataValidationError("candles must contain Candle objects",
                                          index=index, got=type(candle).__name__)
            if not candle.is_final:
                raise DataValidationError(
                    "features may not be built from a non-final candle; the "
                    "forming bar's close is not yet its close",
                    index=index, instrument=candle.instrument_key)
        key, timeframe = bars[0].instrument_key, bars[0].timeframe
        for index, candle in enumerate(bars):
            if candle.instrument_key != key:
                raise DataValidationError("candle window mixes instruments",
                                          index=index, expected=key,
                                          got=candle.instrument_key)
            if candle.timeframe != timeframe:
                raise DataValidationError("candle window mixes timeframes",
                                          index=index, expected=timeframe,
                                          got=candle.timeframe)
            if index and candle.ts_open <= bars[index - 1].ts_open:
                raise DataValidationError(
                    "candle window must be strictly ascending in time",
                    index=index,
                    previous=bars[index - 1].ts_open.isoformat(),
                    got=candle.ts_open.isoformat())
        now = (clock or default_clock()).now()
        if bars[-1].ts_close > now:
            raise DataValidationError(
                "candle window ends after the clock; features cannot be built "
                "from a bar that has not closed yet",
                ts_close=bars[-1].ts_close.isoformat(), now=now.isoformat())
        return bars


# ---------------------------------------------------------------------------
# causality
# ---------------------------------------------------------------------------

def _perturb(candles: Sequence[Candle], from_index: int) -> List[Candle]:
    """Return `candles` with every bar from `from_index` onwards changed.

    Deterministic (no seed needed): prices are scaled by 1.5 and volumes
    tripled, which preserves OHLC coherence and moves every value far outside
    the noise band of any indicator.
    """
    out: List[Candle] = []
    for index, candle in enumerate(candles):
        if index < from_index:
            out.append(candle)
            continue
        out.append(Candle(
            instrument_key=candle.instrument_key, timeframe=candle.timeframe,
            ts_open=candle.ts_open, ts_close=candle.ts_close,
            open=candle.open * 1.5, high=candle.high * 1.5,
            low=candle.low * 1.5, close=candle.close * 1.5,
            volume=candle.volume * 3.0, amount=candle.amount * 4.5,
            is_final=candle.is_final, source=candle.source))
    return out


def _close_enough(a: Optional[float], b: Optional[float], tolerance: float) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(a - b) <= tolerance * max(1.0, abs(a), abs(b))


def assert_causal(fn: SeriesFn, candles: Sequence[Candle], *,
                  name: Optional[str] = None, tolerance: float = 1e-9,
                  min_prefix: int = 1) -> None:
    """Verify that a series transform never uses a bar it should not.

    `fn` maps a candle sequence to a sequence of the same length. Causality is
    the property that the value at index *i* depends only on bars 0..i, and it
    is checked two ways, because each catches a different bug:

    1. **Prefix stability.** ``fn(candles[:k])[k-1]`` must equal
       ``fn(candles)[k-1]``. This catches indicators that read forward — a
       centred moving average, a back-filled gap, a label accidentally left in
       the feature list.
    2. **Future perturbation.** Replacing every bar from *k* onwards with wildly
       different ones must leave outputs 0..k-1 untouched. This catches the
       subtler variety that prefix truncation hides: anything fitted on the
       *whole* series and then applied to it — a z-score against the full-sample
       mean, a min-max scaled to the entire range, a normalisation window that
       includes the prediction horizon. That last one is the Kronos
       `finetune_csv` bug (KRONOS_TEARDOWN §12.9) in miniature.

    Raises `DataValidationError` naming the first offending index. Returns
    ``None`` on success — it is an assertion, not a predicate, so a caller
    cannot forget to check the result.

    Note what this deliberately does *not* check: a `FeatureBuilder` feature
    function receives only its own window and therefore cannot peek by
    construction. Run this against `FeatureBuilder.series` when you want that
    guarantee tested end to end anyway (it also catches a feature that reaches
    into shared mutable state or a closure over the full history).
    """
    label = name or getattr(fn, "__name__", "feature")
    bars = list(candles)
    if len(bars) < 2:
        raise ConfigurationError("assert_causal needs at least 2 candles",
                                 feature=label, got=len(bars))
    if isinstance(min_prefix, bool) or not isinstance(min_prefix, int) or min_prefix < 1:
        raise ConfigurationError("min_prefix must be an int >= 1",
                                 min_prefix=repr(min_prefix))

    full = list(fn(bars))
    if len(full) != len(bars):
        raise DataValidationError(
            "series transform must return one value per candle; a shorter "
            "result is how warm-up misalignment enters a backtest",
            feature=label, expected=len(bars), got=len(full))

    for k in range(max(min_prefix, 1), len(bars) + 1):
        prefix = list(fn(bars[:k]))
        if len(prefix) != k:
            raise DataValidationError(
                "series transform must return one value per candle",
                feature=label, prefix_length=k, got=len(prefix))
        if not _close_enough(prefix[k - 1], full[k - 1], tolerance):
            raise DataValidationError(
                "feature is not causal: its value at a bar changes once later "
                "bars are appended, so it is reading the future",
                feature=label, index=k - 1,
                with_history_only=prefix[k - 1], with_future=full[k - 1])

    for k in range(max(min_prefix, 1), len(bars)):
        perturbed = list(fn(_perturb(bars, k)))
        for i in range(k):
            if not _close_enough(perturbed[i], full[i], tolerance):
                raise DataValidationError(
                    "feature is not causal: changing a future bar changed a "
                    "past value, so future information is leaking backwards",
                    feature=label, index=i, changed_from=k,
                    original=full[i], after_future_changed=perturbed[i])


def assert_builder_causal(builder: FeatureBuilder, candles: Sequence[Candle], *,
                          clock: Optional[Clock] = None,
                          tolerance: float = 1e-9) -> None:
    """Run `assert_causal` over every feature registered on `builder`.

    Cheap insurance to run once in a test suite after registering custom
    features; too slow (O(n² · features)) for a production loop.
    """
    for name in builder.names():
        assert_causal(lambda window, _n=name: builder.series(_n, window, clock=clock),
                      candles, name=name, tolerance=tolerance,
                      min_prefix=builder.min_bars(name))


# ---------------------------------------------------------------------------
# normalisation: fit on train, transform later
# ---------------------------------------------------------------------------

#: Supported scaling methods. `robust` uses median/IQR and is the right default
#: for financial features, whose tails are fat enough that a single crash bar
#: can dominate a mean and a standard deviation.
SCALER_METHODS = ("zscore", "minmax", "robust")


def _quantile(ordered: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile of an already-sorted sequence."""
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    low = int(math.floor(position))
    high = int(math.ceil(position))
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


@dataclass(frozen=True)
class ScalerStats:
    """Frozen scaling statistics — the *only* thing `transform` may use.

    Frozen and separate from the transform on purpose. A mutable scaler object
    with a `fit` method invites `fit(test)` or `fit_transform(everything)`, and
    that single line is the difference between an honest backtest and the
    Kronos `finetune_csv` leak (KRONOS_TEARDOWN §12.9). Here, refitting means
    constructing a new `ScalerStats` from an explicitly chosen set of rows —
    visible in a diff, and impossible to do by accident.

    `degenerate` lists columns that had no spread in the training window. Their
    scale is set to 1.0, so they transform to a constant offset rather than
    exploding; treating a constant training feature as informative is the more
    dangerous failure.
    """
    method: str
    columns: Tuple[str, ...]
    center: Dict[str, float]
    scale: Dict[str, float]
    n_samples: int
    degenerate: Tuple[str, ...] = ()
    fitted_from: Optional[datetime] = None
    fitted_to: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {k: jsonable(v) for k, v in asdict(self).items()}

    def transform(self, row: "FeatureSet | Mapping[str, float]", *,
                  strict: bool = True) -> Dict[str, float]:
        """Scale one row using the *training* statistics held here.

        A column present in `row` but not in the training window raises when
        `strict` (the default): passing an unscaled feature through into a
        model alongside scaled ones is a silent, hard-to-see corruption. A
        training column absent from `row` is simply omitted — missing stays
        missing, it is never imputed to the training mean.
        """
        values = row.values if isinstance(row, FeatureSet) else row
        if not isinstance(values, _MappingABC):
            raise DataValidationError("row must be a FeatureSet or a mapping",
                                      got=type(row).__name__)
        if strict:
            unknown = sorted(set(values) - set(self.columns))
            if unknown:
                raise DataValidationError(
                    "row contains columns the scaler was not fitted on",
                    unknown=unknown, fitted=list(self.columns))
        out: Dict[str, float] = {}
        for name in self.columns:
            if name not in values:
                continue
            raw = float(values[name])
            out[name] = (raw - self.center[name]) / self.scale[name]
        return out

    def transform_many(self, rows: Sequence["FeatureSet | Mapping[str, float]"], *,
                       strict: bool = True) -> List[Dict[str, float]]:
        return [self.transform(row, strict=strict) for row in rows]

    def inverse_transform(self, row: Mapping[str, float]) -> Dict[str, float]:
        """Map scaled values back to raw units — needed to turn a model's
        normalised output into a price. The Kronos alpha signals are computed
        in normalised space and never inverted (KRONOS_TEARDOWN §12.8), which
        makes them incomparable across instruments."""
        if not isinstance(row, _MappingABC):
            raise DataValidationError("row must be a mapping",
                                      got=type(row).__name__)
        out: Dict[str, float] = {}
        for name, value in row.items():
            if name not in self.center:
                raise DataValidationError("unknown column for inverse_transform",
                                          column=name)
            out[name] = float(value) * self.scale[name] + self.center[name]
        return out


def fit_scaler(rows: Sequence["FeatureSet | Mapping[str, float]"], *,
               method: str = "robust",
               columns: Optional[Sequence[str]] = None) -> ScalerStats:
    """Fit scaling statistics on a **training window only**.

    Pass the training rows and nothing else. The returned `ScalerStats` is
    frozen, so every later window — validation, test, live — is scaled with
    statistics that could not have known about it. That is the whole contract;
    see the module docstring and KRONOS_TEARDOWN §12.9 for what happens without
    it.

    `columns` fixes the column set explicitly. Without it the columns are the
    union of every key seen across `rows`, sorted — but a feature that is
    absent from the training window and present later then has no statistics
    and will trip `transform`'s strict check, which is the correct outcome.
    """
    if method not in SCALER_METHODS:
        raise ConfigurationError("unknown scaler method", method=method,
                                 supported=list(SCALER_METHODS))
    if isinstance(rows, (str, bytes)) or not isinstance(rows, _SequenceABC):
        raise DataValidationError("rows must be a sequence",
                                  got=type(rows).__name__)
    materialised = list(rows)
    if not materialised:
        raise DataValidationError("cannot fit a scaler on zero rows")

    collected: Dict[str, List[float]] = {}
    oldest: Optional[datetime] = None
    newest: Optional[datetime] = None
    for index, row in enumerate(materialised):
        if isinstance(row, FeatureSet):
            mapping: Mapping[str, float] = row.values
            oldest = row.ts if oldest is None or row.ts < oldest else oldest
            newest = row.ts if newest is None or row.ts > newest else newest
        elif isinstance(row, _MappingABC):
            mapping = row
        else:
            raise DataValidationError("rows must be FeatureSets or mappings",
                                      index=index, got=type(row).__name__)
        for name, value in mapping.items():
            if columns is not None and name not in columns:
                continue
            number = float(value)
            if not math.isfinite(number):
                raise DataValidationError("cannot fit a scaler on non-finite "
                                          "values", column=name, index=index)
            collected.setdefault(name, []).append(number)

    names = tuple(columns) if columns is not None else tuple(sorted(collected))
    center: Dict[str, float] = {}
    scale: Dict[str, float] = {}
    degenerate: List[str] = []
    for name in names:
        sample = collected.get(name, [])
        if not sample:
            raise DataValidationError("column has no values in the training "
                                      "window", column=name)
        if method == "zscore":
            mean = math.fsum(sample) / len(sample)
            variance = math.fsum((x - mean) ** 2 for x in sample) / len(sample)
            middle, spread = mean, math.sqrt(max(0.0, variance))
        elif method == "minmax":
            middle, spread = min(sample), max(sample) - min(sample)
        else:  # robust
            ordered = sorted(sample)
            middle = _quantile(ordered, 0.5)
            spread = _quantile(ordered, 0.75) - _quantile(ordered, 0.25)
        if not math.isfinite(spread) or spread <= 0.0:
            degenerate.append(name)
            spread = 1.0
        center[name] = middle
        scale[name] = spread

    return ScalerStats(method=method, columns=names, center=center, scale=scale,
                       n_samples=len(materialised),
                       degenerate=tuple(degenerate),
                       fitted_from=oldest, fitted_to=newest)


def fit_transform(train_rows: Sequence["FeatureSet | Mapping[str, float]"], *,
                  method: str = "robust",
                  columns: Optional[Sequence[str]] = None
                  ) -> Tuple[ScalerStats, List[Dict[str, float]]]:
    """Fit on `train_rows` and transform *those same rows*.

    The name is the familiar one, but note the restriction: this only ever
    touches the training rows. There is no `fit_transform(all_rows)` in this
    module, because that call is the bug.
    """
    stats = fit_scaler(train_rows, method=method, columns=columns)
    return stats, stats.transform_many(train_rows)


def split_by_time(rows: Sequence[FeatureSet], *, split_at: datetime
                  ) -> Tuple[List[FeatureSet], List[FeatureSet]]:
    """Chronological train/test split — rows with ``ts < split_at`` are train.

    Chronological, never random. A random split of a time series puts bar *t+1*
    in train and bar *t* in test; the model then interpolates rather than
    forecasts and the reported accuracy is meaningless.
    """
    boundary = ensure_utc(split_at, field_name="split_at")
    train: List[FeatureSet] = []
    test: List[FeatureSet] = []
    for index, row in enumerate(rows):
        if not isinstance(row, FeatureSet):
            raise DataValidationError("rows must be FeatureSets", index=index,
                                      got=type(row).__name__)
        (train if row.ts < boundary else test).append(row)
    return train, test


def causal_window_normalise(values: Sequence[float], lookback: int, *,
                            method: str = "zscore") -> List[float]:
    """Normalise a ``lookback + horizon`` window using **lookback statistics only**.

    This is the direct fix for KRONOS_TEARDOWN §12.9. Upstream,
    `CustomKlineDataset.__getitem__` computes mean and standard deviation over
    the *entire* ``lookback + predict + 1`` window and applies them to the
    model's input — so the scaling of the context encodes the mean and spread
    of the horizon the model is asked to predict. Validation loss drops, the
    model looks skilful, and none of it survives contact with a live feed where
    the horizon does not exist yet. `finetune/dataset.py` was corrected for this
    (commit `79d6d40`, issue #227); `finetune_csv` was not.

    Here the statistics come from ``values[:lookback]`` and are applied to the
    whole sequence, so the tail is scaled by numbers that predate it. Returns a
    list the same length as `values`. A zero-spread lookback scales by 1.0
    rather than dividing by zero.
    """
    if method not in SCALER_METHODS:
        raise ConfigurationError("unknown scaler method", method=method,
                                 supported=list(SCALER_METHODS))
    if isinstance(lookback, bool) or not isinstance(lookback, int) or lookback < 1:
        raise ConfigurationError("lookback must be an int >= 1",
                                 lookback=repr(lookback))
    if isinstance(values, (str, bytes)) or not isinstance(values, _SequenceABC):
        raise DataValidationError("values must be a sequence",
                                  got=type(values).__name__)
    numbers: List[float] = []
    for index, value in enumerate(values):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise DataValidationError("values must be numeric", index=index,
                                      got=type(value).__name__)
        number = float(value)
        if not math.isfinite(number):
            raise DataValidationError("values must be finite", index=index)
        numbers.append(number)
    if len(numbers) < lookback:
        raise DataValidationError("sequence is shorter than the lookback",
                                  lookback=lookback, got=len(numbers))
    train = numbers[:lookback]
    if method == "zscore":
        mean = math.fsum(train) / len(train)
        variance = math.fsum((x - mean) ** 2 for x in train) / len(train)
        middle, spread = mean, math.sqrt(max(0.0, variance))
    elif method == "minmax":
        middle, spread = min(train), max(train) - min(train)
    else:  # robust
        ordered = sorted(train)
        middle = _quantile(ordered, 0.5)
        spread = _quantile(ordered, 0.75) - _quantile(ordered, 0.25)
    if not math.isfinite(spread) or spread <= 0.0:
        spread = 1.0
    return [(x - middle) / spread for x in numbers]


__all__ = [
    "FeatureFn", "SeriesFn", "FeatureSet", "FeatureBuilder",
    "BUILTIN_FEATURES", "DEFAULT_FEATURE_NAMES",
    "assert_causal", "assert_builder_causal",
    "SCALER_METHODS", "ScalerStats", "fit_scaler", "fit_transform",
    "split_by_time", "causal_window_normalise",
]

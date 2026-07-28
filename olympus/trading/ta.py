"""Technical analysis — pure stdlib, alignment-preserving.

Three design decisions dominate this module, and each one exists because the
alternative is a class of silent backtest error rather than a crash.

**1. Every function returns a list the same length as its input.**
`sma([...100 closes...], 20)` returns 100 values, the first 19 of which are
``None``. It would be cheaper to return the 81 defined values, and that is what
most indicator libraries do — which is exactly how an off-by-one enters a
backtest: the caller zips a short indicator against a full candle list and
every signal is silently shifted by the warm-up length, usually *forwards*,
which turns look-ahead into a beautiful equity curve. A ``None`` at index *i*
means "not computable from bars 0..i"; it never means zero, and it can never be
mistaken for a number. Each function documents its exact warm-up length.

**2. Short input is not an error.** A 30-bar history and a 200-period moving
average is an ordinary situation at the start of a run, not a bug. Such calls
return an all-``None`` list. What *is* an error is an impossible *parameter* —
``period=0`` — which raises `ConfigurationError` at once, because that is a
coding mistake and no answer to it is meaningful. Data problems (non-finite
values) raise `DataValidationError`; they are never quietly coerced.

**3. ``None`` propagates through windows.** Indicators compose — MACD's signal
line is an EMA of the MACD line, which itself begins with ``None``s. Any window
containing a ``None`` yields ``None``, and the recursive indicators (EMA,
Wilder-smoothed RSI/ATR/ADX) *re-seed* after an interior gap rather than
smoothing across it. Smoothing across a hole silently invents the missing bars.

Numerical notes
---------------
* **No numpy.** Olympus has three required dependencies; indicators are not a
  reason for a fourth. Windows are summed with `math.fsum` and recomputed per
  window rather than maintained as an incremental running sum: incremental
  add/subtract accumulates float error monotonically, and a backtest over years
  of 1-minute bars is precisely where that error becomes visible. The cost is
  O(n·period), which is irrelevant next to the rest of a backtest.
* **EMA is seeded with the SMA of the first `period` values** (the Wilder /
  TA-Lib convention) rather than with the first value alone. Seeding with
  ``values[0]`` makes the whole series depend on one arbitrary bar and produces
  a different indicator depending on where the caller's history happens to
  start — non-reproducible across two runs with different lookbacks.
* **RSI, ATR and ADX use Wilder's smoothing** (α = 1/period), not the α =
  2/(period+1) EMA. They are different indicators with the same name in
  different libraries; this module implements the original definitions so that
  published parameter conventions (RSI 14, ADX 14) mean what they claim.
* **Standard deviations are population (ddof=0)** unless a `ddof` argument says
  otherwise. A rolling window is the whole population of that window, not a
  sample drawn from it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence as _SequenceABC
from typing import List, Optional, Sequence, Tuple, Union

from .contracts import Candle
from .errors import ConfigurationError, DataValidationError

#: An aligned indicator output: same length as the input, ``None`` in warm-up.
Series = List[Optional[float]]
#: What indicator functions accept: numbers, possibly with ``None`` holes.
Input = Sequence[Optional[float]]


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------

def _check_period(period: int, *, name: str = "period", minimum: int = 1) -> int:
    """Reject impossible parameters loudly.

    A bad period is a programming error, not a data condition — there is no
    sensible all-``None`` answer to "average the last zero bars", so this
    raises rather than degrading.
    """
    if isinstance(period, bool) or not isinstance(period, int):
        raise ConfigurationError(f"{name} must be an int", **{name: repr(period)})
    if period < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}", **{name: period})
    return period


def _coerce(values: Input, *, field_name: str = "values") -> Series:
    """Validate and copy an input series, preserving ``None`` holes.

    Non-finite input is rejected rather than propagated: a NaN entering an EMA
    poisons every subsequent value, and the failure then surfaces hundreds of
    bars later in a place with no connection to its cause.
    """
    if isinstance(values, (str, bytes)) or not isinstance(values, _SequenceABC):
        raise DataValidationError(f"{field_name} must be a sequence",
                                  field=field_name, got=type(values).__name__)
    out: Series = []
    for index, value in enumerate(values):
        if value is None:
            out.append(None)
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise DataValidationError(f"{field_name}[{index}] must be numeric",
                                      field=field_name, index=index,
                                      got=type(value).__name__)
        number = float(value)
        if not math.isfinite(number):
            raise DataValidationError(f"{field_name}[{index}] must be finite",
                                      field=field_name, index=index,
                                      got=repr(value))
        out.append(number)
    return out


def _coerce_candles(candles: Sequence[Candle]) -> List[Candle]:
    if isinstance(candles, (str, bytes)) or not isinstance(candles, _SequenceABC):
        raise DataValidationError("candles must be a sequence",
                                  got=type(candles).__name__)
    for index, candle in enumerate(candles):
        if not isinstance(candle, Candle):
            raise DataValidationError("candles must contain Candle objects",
                                      index=index, got=type(candle).__name__)
    return list(candles)


def _window(values: Series, end: int, period: int) -> Optional[List[float]]:
    """The `period` values ending at (and including) index `end`.

    ``None`` when the window runs off the front of the series or contains a
    hole — the two situations in which the indicator is genuinely undefined.
    """
    start = end - period + 1
    if start < 0:
        return None
    chunk = values[start:end + 1]
    for value in chunk:
        if value is None:
            return None
    return chunk  # type: ignore[return-value]


def _mean(chunk: Sequence[float]) -> float:
    return math.fsum(chunk) / len(chunk)


def _variance(chunk: Sequence[float], ddof: int = 0) -> float:
    n = len(chunk)
    mean = _mean(chunk)
    total = math.fsum((x - mean) ** 2 for x in chunk)
    return total / (n - ddof)


def _stdev(chunk: Sequence[float], ddof: int = 0) -> float:
    return math.sqrt(max(0.0, _variance(chunk, ddof)))


def _nones(n: int) -> Series:
    return [None] * n


# ---------------------------------------------------------------------------
# candle field extraction
# ---------------------------------------------------------------------------

def opens(candles: Sequence[Candle]) -> List[float]:
    """Open prices, aligned to `candles`."""
    return [c.open for c in _coerce_candles(candles)]


def highs(candles: Sequence[Candle]) -> List[float]:
    """High prices, aligned to `candles`."""
    return [c.high for c in _coerce_candles(candles)]


def lows(candles: Sequence[Candle]) -> List[float]:
    """Low prices, aligned to `candles`."""
    return [c.low for c in _coerce_candles(candles)]


def closes(candles: Sequence[Candle]) -> List[float]:
    """Close prices, aligned to `candles`."""
    return [c.close for c in _coerce_candles(candles)]


def volumes(candles: Sequence[Candle]) -> List[float]:
    """Volumes, aligned to `candles`."""
    return [c.volume for c in _coerce_candles(candles)]


def typical_prices(candles: Sequence[Candle]) -> List[float]:
    """(high + low + close) / 3 per bar, aligned to `candles`."""
    return [c.typical_price for c in _coerce_candles(candles)]


# ---------------------------------------------------------------------------
# moving averages
# ---------------------------------------------------------------------------

def sma(values: Input, period: int) -> Series:
    """Simple moving average.

    Warm-up: ``period - 1`` leading ``None``s; the first value lands at index
    ``period - 1``. Any window containing a ``None`` yields ``None``.
    """
    _check_period(period)
    data = _coerce(values)
    out: Series = []
    for i in range(len(data)):
        chunk = _window(data, i, period)
        out.append(None if chunk is None else _mean(chunk))
    return out


def ema(values: Input, period: int) -> Series:
    """Exponential moving average, α = 2/(period + 1).

    Seeded with the SMA of the first complete `period` window (TA-Lib / Wilder
    convention) rather than with ``values[0]``, so the series does not depend on
    how much history the caller happened to pass in.

    Warm-up: ``period - 1`` leading ``None``s. An interior ``None`` emits
    ``None`` and *resets* the seed — the EMA restarts once another full
    `period` of clean values is available, rather than smoothing across a gap
    as though the missing bars had been flat.
    """
    _check_period(period)
    data = _coerce(values)
    alpha = 2.0 / (period + 1.0)
    return _recursive_average(data, period, alpha)


def wma(values: Input, period: int) -> Series:
    """Linearly weighted moving average — weight `period` on the newest bar
    down to weight 1 on the oldest.

    Warm-up: ``period - 1`` leading ``None``s.
    """
    _check_period(period)
    data = _coerce(values)
    denominator = period * (period + 1) / 2.0
    out: Series = []
    for i in range(len(data)):
        chunk = _window(data, i, period)
        if chunk is None:
            out.append(None)
            continue
        out.append(math.fsum((j + 1) * x for j, x in enumerate(chunk)) / denominator)
    return out


def _recursive_average(data: Series, period: int, alpha: float) -> Series:
    """Shared engine for EMA-family smoothers.

    `alpha` selects the family: 2/(period+1) is the classic EMA, 1/period is
    Wilder's. Both seed on the SMA of the first clean window and both re-seed
    after a hole; keeping one implementation means the two conventions cannot
    drift apart in their edge-case handling.
    """
    out: Series = []
    state: Optional[float] = None
    for i in range(len(data)):
        if data[i] is None:
            out.append(None)
            state = None                    # a hole invalidates the recursion
            continue
        if state is None:
            chunk = _window(data, i, period)
            if chunk is None:
                out.append(None)
                continue
            state = _mean(chunk)
        else:
            state = alpha * float(data[i]) + (1.0 - alpha) * state
        out.append(state)
    return out


# ---------------------------------------------------------------------------
# oscillators
# ---------------------------------------------------------------------------

def rsi(values: Input, period: int = 14) -> Series:
    """Relative strength index with Wilder smoothing.

    The first average gain/loss is the plain mean of the first `period` price
    changes; subsequent ones use ``avg = (avg * (period - 1) + current) / period``.

    Warm-up: ``period`` leading ``None``s — one more than a moving average,
    because RSI consumes *differences* and the first bar has no predecessor.
    The first value lands at index `period`.

    Degenerate windows: an all-down window gives 0, an all-up window gives 100,
    and a perfectly flat window gives 50 rather than 100. Flat markets are
    neither overbought nor oversold, and reporting 100 there (as some libraries
    do, by treating 0/0 as ∞) has fired real breakout strategies on a halted
    instrument.
    """
    _check_period(period)
    data = _coerce(values)
    n = len(data)
    out: Series = _nones(n)
    avg_gain: Optional[float] = None
    avg_loss: Optional[float] = None
    gains: Series = _nones(n)
    losses: Series = _nones(n)
    for i in range(1, n):
        previous, current = data[i - 1], data[i]
        if previous is None or current is None:
            continue
        change = current - previous
        gains[i] = max(change, 0.0)
        losses[i] = max(-change, 0.0)
    for i in range(1, n):
        if gains[i] is None:
            avg_gain = avg_loss = None      # gap: restart the smoothing
            continue
        if avg_gain is None or avg_loss is None:
            gain_window = _window(gains, i, period)
            loss_window = _window(losses, i, period)
            if gain_window is None or loss_window is None:
                continue
            avg_gain, avg_loss = _mean(gain_window), _mean(loss_window)
        else:
            avg_gain = (avg_gain * (period - 1) + float(gains[i])) / period
            avg_loss = (avg_loss * (period - 1) + float(losses[i])) / period
        if avg_loss == 0.0 and avg_gain == 0.0:
            out[i] = 50.0
        elif avg_loss == 0.0:
            out[i] = 100.0
        elif avg_gain == 0.0:
            out[i] = 0.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def macd(values: Input, fast: int = 12, slow: int = 26,
         signal: int = 9) -> Tuple[Series, Series, Series]:
    """MACD line, signal line, histogram — three lists, all input-aligned.

    * line   = EMA(fast) - EMA(slow); first value at index ``slow - 1``.
    * signal = EMA(line, signal), seeded on the first clean window of the line;
      first value at index ``slow + signal - 2``.
    * hist   = line - signal; defined wherever both are.

    Raises `ConfigurationError` when ``fast >= slow`` — the sign convention of
    the histogram (positive = fast above slow = bullish) is meaningless
    otherwise, and inverting it silently is worse than refusing.
    """
    _check_period(fast, name="fast")
    _check_period(slow, name="slow")
    _check_period(signal, name="signal")
    if fast >= slow:
        raise ConfigurationError("macd requires fast < slow", fast=fast, slow=slow)
    data = _coerce(values)
    fast_line = ema(data, fast)
    slow_line = ema(data, slow)
    line: Series = [
        None if (f is None or s is None) else f - s
        for f, s in zip(fast_line, slow_line)
    ]
    signal_line = ema(line, signal)
    histogram: Series = [
        None if (m is None or s is None) else m - s
        for m, s in zip(line, signal_line)
    ]
    return line, signal_line, histogram


def stochastic(candles: Sequence[Candle], period: int = 14,
               smooth_k: int = 3) -> Tuple[Series, Series]:
    """Stochastic oscillator — (%K, %D), both candle-aligned.

    %K = 100 · (close − lowest low) / (highest high − lowest low) over `period`
    bars; %D = SMA(%K, `smooth_k`). This is "fast %K, slow %D": no extra
    smoothing is applied to %K itself.

    Warm-up: %K has ``period - 1`` leading ``None``s; %D has
    ``period + smooth_k - 2``.

    A window whose high equals its low (a halted or one-tick instrument) yields
    50.0 rather than a division by zero — the price is simultaneously at the
    top and the bottom of a zero-width range, so the neutral reading is the only
    honest one.
    """
    _check_period(period)
    _check_period(smooth_k, name="smooth_k")
    bars = _coerce_candles(candles)
    high_series = [c.high for c in bars]
    low_series = [c.low for c in bars]
    close_series = [c.close for c in bars]
    percent_k: Series = []
    for i in range(len(bars)):
        if i + 1 < period:
            percent_k.append(None)
            continue
        window_high = max(high_series[i - period + 1:i + 1])
        window_low = min(low_series[i - period + 1:i + 1])
        span = window_high - window_low
        if span <= 0.0:
            percent_k.append(50.0)
        else:
            percent_k.append(100.0 * (close_series[i] - window_low) / span)
    return percent_k, sma(percent_k, smooth_k)


def williams_r(candles: Sequence[Candle], period: int = 14) -> Series:
    """Williams %R in [-100, 0] — the stochastic's mirror image.

    Warm-up: ``period - 1`` leading ``None``s. A zero-width range yields -50.0
    for the same reason `stochastic` yields 50.0.
    """
    _check_period(period)
    bars = _coerce_candles(candles)
    high_series = [c.high for c in bars]
    low_series = [c.low for c in bars]
    out: Series = []
    for i in range(len(bars)):
        if i + 1 < period:
            out.append(None)
            continue
        window_high = max(high_series[i - period + 1:i + 1])
        window_low = min(low_series[i - period + 1:i + 1])
        span = window_high - window_low
        if span <= 0.0:
            out.append(-50.0)
        else:
            out.append(-100.0 * (window_high - bars[i].close) / span)
    return out


def cci(candles: Sequence[Candle], period: int = 20) -> Series:
    """Commodity Channel Index over typical price.

    ``(TP - SMA(TP)) / (0.015 · mean absolute deviation)``. The 0.015 is
    Lambert's arbitrary scaling constant, chosen so roughly 70–80% of readings
    fall in [-100, 100]; it is a convention, not a statistic.

    Warm-up: ``period - 1`` leading ``None``s. A window with zero mean absolute
    deviation yields 0.0 (price is exactly at its own mean).
    """
    _check_period(period)
    bars = _coerce_candles(candles)
    typical = [c.typical_price for c in bars]
    out: Series = []
    for i in range(len(bars)):
        if i + 1 < period:
            out.append(None)
            continue
        chunk = typical[i - period + 1:i + 1]
        mean = _mean(chunk)
        deviation = math.fsum(abs(x - mean) for x in chunk) / period
        out.append(0.0 if deviation <= 0.0
                   else (typical[i] - mean) / (0.015 * deviation))
    return out


# ---------------------------------------------------------------------------
# volatility / range
# ---------------------------------------------------------------------------

def true_range(candles: Sequence[Candle]) -> List[float]:
    """Wilder's true range per bar — candle-aligned, no ``None``s.

    ``max(high - low, |high - prev_close|, |low - prev_close|)``.

    Index 0 has no predecessor and is defined as ``high - low``, which is the
    standard convention and is a genuine (if incomplete) range rather than a
    guess; it is *not* ``None``, so callers can sum the list without filtering.
    Its slight under-statement is absorbed by `atr`'s seeding window.
    """
    bars = _coerce_candles(candles)
    out: List[float] = []
    for i, candle in enumerate(bars):
        if i == 0:
            out.append(candle.high - candle.low)
            continue
        previous_close = bars[i - 1].close
        out.append(max(candle.high - candle.low,
                       abs(candle.high - previous_close),
                       abs(candle.low - previous_close)))
    return out


def atr(candles: Sequence[Candle], period: int = 14) -> Series:
    """Average true range with Wilder smoothing.

    Seeded with the plain mean of the first `period` true ranges, then
    ``atr = (atr * (period - 1) + tr) / period``.

    Warm-up: ``period - 1`` leading ``None``s; first value at index
    ``period - 1``.
    """
    _check_period(period)
    ranges = true_range(candles)
    if len(ranges) < period:
        return _nones(len(ranges))
    out: Series = _nones(len(ranges))
    state = _mean(ranges[:period])
    out[period - 1] = state
    for i in range(period, len(ranges)):
        state = (state * (period - 1) + ranges[i]) / period
        out[i] = state
    return out


def rolling_std(values: Input, period: int, ddof: int = 0) -> Series:
    """Rolling standard deviation.

    `ddof` 0 (default) treats the window as its own population — the right
    choice for "how much did this window move". `ddof=1` is for callers who
    genuinely mean a sample estimate.

    Warm-up: ``period - 1`` leading ``None``s.
    """
    _check_period(period)
    if ddof not in (0, 1):
        raise ConfigurationError("ddof must be 0 or 1", ddof=ddof)
    if period <= ddof:
        raise ConfigurationError("period must exceed ddof", period=period, ddof=ddof)
    data = _coerce(values)
    out: Series = []
    for i in range(len(data)):
        chunk = _window(data, i, period)
        out.append(None if chunk is None else _stdev(chunk, ddof))
    return out


def rolling_median(values: Input, period: int) -> Series:
    """Rolling median — the outlier-resistant counterpart to `sma`.

    Warm-up: ``period - 1`` leading ``None``s. Even-length windows average the
    two central values.
    """
    _check_period(period)
    data = _coerce(values)
    out: Series = []
    for i in range(len(data)):
        chunk = _window(data, i, period)
        if chunk is None:
            out.append(None)
            continue
        ordered = sorted(chunk)
        middle = period // 2
        if period % 2:
            out.append(ordered[middle])
        else:
            out.append((ordered[middle - 1] + ordered[middle]) / 2.0)
    return out


def bollinger(values: Input, period: int = 20,
              num_std: float = 2.0) -> Tuple[Series, Series, Series]:
    """Bollinger bands — (upper, middle, lower), all input-aligned.

    Middle is `sma`; the bands sit `num_std` *population* standard deviations
    away. Warm-up: ``period - 1`` leading ``None``s in all three.
    """
    _check_period(period)
    if isinstance(num_std, bool) or not isinstance(num_std, (int, float)):
        raise ConfigurationError("num_std must be a number", num_std=repr(num_std))
    num_std = float(num_std)
    if not math.isfinite(num_std) or num_std < 0:
        raise ConfigurationError("num_std must be finite and >= 0", num_std=num_std)
    middle = sma(values, period)
    deviation = rolling_std(values, period)
    upper: Series = []
    lower: Series = []
    for mid, dev in zip(middle, deviation):
        if mid is None or dev is None:
            upper.append(None)
            lower.append(None)
        else:
            upper.append(mid + num_std * dev)
            lower.append(mid - num_std * dev)
    return upper, middle, lower


def keltner(candles: Sequence[Candle], period: int = 20,
            multiplier: float = 2.0) -> Tuple[Series, Series, Series]:
    """Keltner channel — (upper, middle, lower), candle-aligned.

    Middle is ``EMA(close, period)``; the bands sit ``multiplier · ATR(period)``
    away. This is the modern (Raschke) formulation rather than Keltner's
    original SMA-of-typical-price ± SMA-of-range, because pairing it with
    `bollinger` is only informative if both react on the same timescale: the
    difference between the two channels is then purely volatility-measure
    (true range vs close dispersion), which is the comparison traders actually
    want.

    Warm-up: ``period - 1`` leading ``None``s.
    """
    _check_period(period)
    if isinstance(multiplier, bool) or not isinstance(multiplier, (int, float)):
        raise ConfigurationError("multiplier must be a number",
                                 multiplier=repr(multiplier))
    multiplier = float(multiplier)
    if not math.isfinite(multiplier) or multiplier < 0:
        raise ConfigurationError("multiplier must be finite and >= 0",
                                 multiplier=multiplier)
    bars = _coerce_candles(candles)
    middle = ema([c.close for c in bars], period)
    band = atr(bars, period)
    upper: Series = []
    lower: Series = []
    for mid, width in zip(middle, band):
        if mid is None or width is None:
            upper.append(None)
            lower.append(None)
        else:
            upper.append(mid + multiplier * width)
            lower.append(mid - multiplier * width)
    return upper, middle, lower


def donchian(candles: Sequence[Candle],
             period: int = 20) -> Tuple[Series, Series, Series]:
    """Donchian channel — (upper, middle, lower), candle-aligned.

    Upper is the highest *high* and lower the lowest *low* of the last `period`
    bars, **including the current one**. Callers building a breakout rule
    almost always want the channel excluding the current bar (otherwise price
    can never exceed its own channel); shift the result by one bar explicitly
    rather than expecting this function to guess.

    Warm-up: ``period - 1`` leading ``None``s.
    """
    _check_period(period)
    bars = _coerce_candles(candles)
    high_series = [c.high for c in bars]
    low_series = [c.low for c in bars]
    upper: Series = []
    middle: Series = []
    lower: Series = []
    for i in range(len(bars)):
        if i + 1 < period:
            upper.append(None)
            middle.append(None)
            lower.append(None)
            continue
        top = max(high_series[i - period + 1:i + 1])
        bottom = min(low_series[i - period + 1:i + 1])
        upper.append(top)
        lower.append(bottom)
        middle.append((top + bottom) / 2.0)
    return upper, middle, lower


# ---------------------------------------------------------------------------
# trend strength
# ---------------------------------------------------------------------------

def dmi(candles: Sequence[Candle],
        period: int = 14) -> Tuple[Series, Series, Series]:
    """Directional movement — (+DI, -DI, ADX), all candle-aligned.

    Wilder's construction throughout: directional movement is accumulated with
    ``sum = sum - sum/period + current`` (his smoothed *sum*, not a mean), DX is
    the normalised spread between the two DIs, and ADX is a Wilder average of DX.

    Warm-up: +DI/-DI have `period` leading ``None``s (directional movement needs
    a predecessor bar, then a full `period` to seed); ADX has ``2 * period - 1``,
    because it averages `period` DX values that themselves only start at index
    `period`. That double warm-up is why a 14-period ADX needs ~28 bars before
    it says anything, and why strategies gated on ADX go quiet at the start of a
    run rather than firing on a half-formed value.

    A window with zero accumulated true range yields DI = 0 and DX = 0 rather
    than dividing by zero.
    """
    _check_period(period)
    bars = _coerce_candles(candles)
    n = len(bars)
    plus_di: Series = _nones(n)
    minus_di: Series = _nones(n)
    adx_line: Series = _nones(n)
    if n < period + 1:
        return plus_di, minus_di, adx_line

    ranges = true_range(bars)
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        up_move = bars[i].high - bars[i - 1].high
        down_move = bars[i - 1].low - bars[i].low
        plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0

    # Wilder seeds on the plain sums of the first `period` values, which start
    # at index 1 (index 0 has no directional movement at all).
    tr_sum = math.fsum(ranges[1:period + 1])
    plus_sum = math.fsum(plus_dm[1:period + 1])
    minus_sum = math.fsum(minus_dm[1:period + 1])

    dx: Series = _nones(n)
    for i in range(period, n):
        if i > period:
            tr_sum = tr_sum - tr_sum / period + ranges[i]
            plus_sum = plus_sum - plus_sum / period + plus_dm[i]
            minus_sum = minus_sum - minus_sum / period + minus_dm[i]
        if tr_sum <= 0.0:
            plus_di[i] = minus_di[i] = 0.0
            dx[i] = 0.0
            continue
        pdi = 100.0 * plus_sum / tr_sum
        mdi = 100.0 * minus_sum / tr_sum
        plus_di[i] = pdi
        minus_di[i] = mdi
        total = pdi + mdi
        dx[i] = 0.0 if total <= 0.0 else 100.0 * abs(pdi - mdi) / total

    first_adx = 2 * period - 1
    if n > first_adx:
        state = _mean([float(x) for x in dx[period:first_adx + 1]])
        adx_line[first_adx] = state
        for i in range(first_adx + 1, n):
            state = (state * (period - 1) + float(dx[i])) / period
            adx_line[i] = state
    return plus_di, minus_di, adx_line


def adx(candles: Sequence[Candle], period: int = 14) -> Series:
    """Average directional index (trend strength, 0–100, direction-agnostic).

    Warm-up: ``2 * period - 1`` leading ``None``s. See `dmi` for why, and use
    `dmi` when the caller also needs +DI/-DI (it computes all three in one pass).
    """
    return dmi(candles, period)[2]


def linreg_slope(values: Input, period: int) -> Series:
    """Ordinary-least-squares slope of the last `period` values against bar
    index, in price units *per bar*.

    Warm-up: ``period - 1`` leading ``None``s. ``period == 1`` yields 0.0
    everywhere (a single point has no slope), which is why the denominator can
    never be zero here.
    """
    _check_period(period)
    data = _coerce(values)
    # x is always 0..period-1, so its mean and variance are constants.
    x_mean = (period - 1) / 2.0
    x_variance = math.fsum((j - x_mean) ** 2 for j in range(period))
    out: Series = []
    for i in range(len(data)):
        chunk = _window(data, i, period)
        if chunk is None:
            out.append(None)
            continue
        if x_variance <= 0.0:
            out.append(0.0)
            continue
        y_mean = _mean(chunk)
        covariance = math.fsum((j - x_mean) * (y - y_mean)
                               for j, y in enumerate(chunk))
        out.append(covariance / x_variance)
    return out


def trend_strength(values: Input, period: int = 20) -> Series:
    """Signed efficiency ratio in [-1, 1] — how *directional* the window was.

    ``(last - first) / sum(|bar-to-bar change|)``. +1 is a monotone rise, -1 a
    monotone fall, 0 a path that ended where it started. Kaufman's efficiency
    ratio, kept signed so a single feature carries both direction and
    conviction.

    This is the "Hurst-like" quantity the strategy layer should use: like a
    Hurst exponent it separates persistent from mean-reverting behaviour, but
    unlike `hurst` it is exact, cheap, bounded, and meaningful on a 20-bar
    window. `hurst` is offered for regime *research*; this is for decisions.

    Warm-up: ``period - 1`` leading ``None``s. A perfectly flat window yields
    0.0 rather than 0/0.
    """
    _check_period(period)
    data = _coerce(values)
    out: Series = []
    for i in range(len(data)):
        chunk = _window(data, i, period)
        if chunk is None:
            out.append(None)
            continue
        if period == 1:
            out.append(0.0)
            continue
        net = chunk[-1] - chunk[0]
        path = math.fsum(abs(chunk[j] - chunk[j - 1]) for j in range(1, period))
        out.append(0.0 if path <= 0.0 else max(-1.0, min(1.0, net / path)))
    return out


def hurst(values: Input, period: int = 64) -> Series:
    """Rolling Hurst-exponent estimate in [0, 1], via lagged-difference scaling.

    For each window, the standard deviation of the `lag`-step differences is
    computed for a range of lags and ``log(σ)`` is regressed on ``log(lag)``;
    the slope estimates H. H ≈ 0.5 is a random walk, H > 0.5 persistent
    (a move tends to be followed by a move the same way), H < 0.5
    mean-reverting.

    It measures how *dispersion* scales with horizon, so a deterministic drift
    is invisible to it: a perfect ramp plus small noise scores near 0, not near
    1, because its increments have almost no variance at any lag. "Trending" in
    the Hurst sense means autocorrelated increments, not a rising price. Use
    `trend_strength` or `linreg_slope` for drift.

    Treat the number as *soft evidence about a regime*, never as a trading
    trigger. On the window lengths a trader can afford (64–512 bars) the
    estimator's standard error is large enough that individual readings swing
    either side of 0.5 on genuinely random data. `trend_strength` is the exact,
    bounded quantity to make decisions with.

    Requires ``period >= 8`` (fewer points cannot support two usable lags) and
    raises `ConfigurationError` below that. Warm-up: ``period - 1`` leading
    ``None``s. A window whose differences are exactly constant at every lag
    (a perfect ramp, a flat line) yields ``None`` — the log-log regression is
    undefined there, and returning 1.0 or 0.5 would be an invention.
    """
    _check_period(period, minimum=8)
    data = _coerce(values)
    # Lags up to a quarter of the window: beyond that only a handful of
    # differences remain and their standard deviation is mostly noise.
    max_lag = min(20, max(2, period // 4))
    lags = list(range(2, max_lag + 1))
    log_lags = [math.log(lag) for lag in lags]
    out: Series = []
    for i in range(len(data)):
        chunk = _window(data, i, period)
        if chunk is None:
            out.append(None)
            continue
        xs: List[float] = []
        ys: List[float] = []
        for lag, log_lag in zip(lags, log_lags):
            diffs = [chunk[j + lag] - chunk[j] for j in range(period - lag)]
            if len(diffs) < 2:
                continue
            spread = _stdev(diffs)
            if spread <= 0.0:
                continue
            xs.append(log_lag)
            ys.append(math.log(spread))
        if len(xs) < 2:
            out.append(None)
            continue
        x_mean, y_mean = _mean(xs), _mean(ys)
        denominator = math.fsum((x - x_mean) ** 2 for x in xs)
        if denominator <= 0.0:
            out.append(None)
            continue
        numerator = math.fsum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
        out.append(max(0.0, min(1.0, numerator / denominator)))
    return out


# ---------------------------------------------------------------------------
# rate of change / normalisation
# ---------------------------------------------------------------------------

def roc(values: Input, period: int = 1) -> Series:
    """Rate of change in **percent**: ``100 * (v[i] / v[i - period] - 1)``.

    Warm-up: `period` leading ``None``s (this looks back `period` bars, so
    index `period` is the first defined value — one more ``None`` than an
    equally-parameterised moving average). A zero reference value yields
    ``None`` rather than infinity.
    """
    _check_period(period)
    data = _coerce(values)
    out: Series = []
    for i in range(len(data)):
        if i < period or data[i] is None or data[i - period] is None:
            out.append(None)
            continue
        base = float(data[i - period])
        out.append(None if base == 0.0 else 100.0 * (float(data[i]) / base - 1.0))
    return out


def momentum(values: Input, period: int = 1) -> Series:
    """Absolute change over `period` bars: ``v[i] - v[i - period]``.

    Warm-up: `period` leading ``None``s. Unlike `roc` this is in price units
    and is therefore not comparable across instruments — use `roc` for that.
    """
    _check_period(period)
    data = _coerce(values)
    out: Series = []
    for i in range(len(data)):
        if i < period or data[i] is None or data[i - period] is None:
            out.append(None)
            continue
        out.append(float(data[i]) - float(data[i - period]))
    return out


def zscore(values: Input, period: int = 20, ddof: int = 0) -> Series:
    """Rolling z-score: ``(v - SMA) / rolling_std`` over the same window.

    Warm-up: ``period - 1`` leading ``None``s. A zero-variance window yields
    0.0 — the value *is* the mean, so zero standard deviations away is correct;
    ``None`` would needlessly blank a feature during a quiet session.
    """
    _check_period(period)
    data = _coerce(values)
    mean_line = sma(data, period)
    std_line = rolling_std(data, period, ddof)
    out: Series = []
    for value, mean, spread in zip(data, mean_line, std_line):
        if value is None or mean is None or spread is None:
            out.append(None)
        elif spread <= 0.0:
            out.append(0.0)
        else:
            out.append((float(value) - mean) / spread)
    return out


def highest(values: Input, period: int) -> Series:
    """Rolling maximum. Warm-up: ``period - 1`` leading ``None``s."""
    _check_period(period)
    data = _coerce(values)
    out: Series = []
    for i in range(len(data)):
        chunk = _window(data, i, period)
        out.append(None if chunk is None else max(chunk))
    return out


def lowest(values: Input, period: int) -> Series:
    """Rolling minimum. Warm-up: ``period - 1`` leading ``None``s."""
    _check_period(period)
    data = _coerce(values)
    out: Series = []
    for i in range(len(data)):
        chunk = _window(data, i, period)
        out.append(None if chunk is None else min(chunk))
    return out


# ---------------------------------------------------------------------------
# volume
# ---------------------------------------------------------------------------

def obv(candles: Sequence[Candle]) -> List[float]:
    """On-balance volume — candle-aligned, no warm-up.

    Starts at 0.0 rather than at the first bar's volume. OBV has no meaningful
    absolute level (its units are "volume since an arbitrary epoch"); only its
    slope and divergences carry information, and starting at zero makes that
    obvious rather than implying the first bar was accumulation.
    """
    bars = _coerce_candles(candles)
    out: List[float] = []
    total = 0.0
    for i, candle in enumerate(bars):
        if i > 0:
            previous_close = bars[i - 1].close
            if candle.close > previous_close:
                total += candle.volume
            elif candle.close < previous_close:
                total -= candle.volume
        out.append(total)
    return out


def vwap(candles: Sequence[Candle], period: Optional[int] = None) -> Series:
    """Volume-weighted average price over typical price.

    ``period=None`` (default) accumulates from the first bar — a *session*
    VWAP, and it is the caller's job to pass only the current session's bars.
    This function deliberately does not detect session boundaries: exchange
    calendars live in `instruments.py`, and an indicator that guessed them
    would silently disagree with the venue on half-days.

    With a `period` it is a rolling window instead; warm-up ``period - 1``.

    Bars with zero total volume yield ``None`` (there is no traded price to
    weight), never a carried-forward stale value.
    """
    bars = _coerce_candles(candles)
    typical = [c.typical_price for c in bars]
    volume_series = [c.volume for c in bars]
    out: Series = []
    if period is None:
        cumulative_notional = 0.0
        cumulative_volume = 0.0
        for i in range(len(bars)):
            cumulative_notional += typical[i] * volume_series[i]
            cumulative_volume += volume_series[i]
            out.append(None if cumulative_volume <= 0.0
                       else cumulative_notional / cumulative_volume)
        return out
    _check_period(period)
    for i in range(len(bars)):
        if i + 1 < period:
            out.append(None)
            continue
        window_volume = math.fsum(volume_series[i - period + 1:i + 1])
        if window_volume <= 0.0:
            out.append(None)
            continue
        window_notional = math.fsum(
            typical[j] * volume_series[j] for j in range(i - period + 1, i + 1))
        out.append(window_notional / window_volume)
    return out


# ---------------------------------------------------------------------------
# crossovers
# ---------------------------------------------------------------------------

def _as_pair(a: Input, b: Union[Input, float, int]) -> Tuple[Series, Series]:
    left = _coerce(a, field_name="a")
    if isinstance(b, (int, float)) and not isinstance(b, bool):
        threshold = float(b)
        if not math.isfinite(threshold):
            raise DataValidationError("threshold must be finite", got=repr(b))
        return left, [threshold] * len(left)
    right = _coerce(b, field_name="b")
    if len(right) != len(left):
        raise DataValidationError("crossover inputs must be the same length",
                                  len_a=len(left), len_b=len(right))
    return left, right


def crossover(a: Input, b: Union[Input, float, int]) -> List[bool]:
    """``True`` at each index where `a` crosses **above** `b` on that bar.

    Aligned to the input and always ``bool`` — never ``None``. Index 0 and any
    index where either series has a hole are ``False``: "we cannot tell" and
    "no cross" must both mean *do not act*, and a tri-state here would only
    invite ``if cross:`` to treat ``None`` as truthy-adjacent.

    The test is ``prev(a - b) <= 0 < now(a - b)``, so an exact touch followed by
    a break counts as one crossing and not two. `b` may be a scalar threshold.
    """
    left, right = _as_pair(a, b)
    out = [False] * len(left)
    for i in range(1, len(left)):
        if (left[i] is None or right[i] is None
                or left[i - 1] is None or right[i - 1] is None):
            continue
        previous = float(left[i - 1]) - float(right[i - 1])
        current = float(left[i]) - float(right[i])
        out[i] = previous <= 0.0 < current
    return out


def crossunder(a: Input, b: Union[Input, float, int]) -> List[bool]:
    """``True`` at each index where `a` crosses **below** `b`.

    Mirror of `crossover`: ``prev(a - b) >= 0 > now(a - b)``. Same alignment and
    same ``False``-when-unknown rule.
    """
    left, right = _as_pair(a, b)
    out = [False] * len(left)
    for i in range(1, len(left)):
        if (left[i] is None or right[i] is None
                or left[i - 1] is None or right[i - 1] is None):
            continue
        previous = float(left[i - 1]) - float(right[i - 1])
        current = float(left[i]) - float(right[i])
        out[i] = previous >= 0.0 > current
    return out


def last_cross(a: Input, b: Union[Input, float, int]) -> Optional[Tuple[int, int]]:
    """The most recent crossing as ``(index, direction)``, or ``None``.

    `direction` is +1 for a cross above and -1 for a cross below. Returning the
    index rather than "bars since" lets the caller decide staleness against its
    own bar count instead of baking a horizon in here.
    """
    up = crossover(a, b)
    down = crossunder(a, b)
    for i in range(len(up) - 1, -1, -1):
        if up[i]:
            return i, 1
        if down[i]:
            return i, -1
    return None


__all__ = [
    "Series", "Input",
    "opens", "highs", "lows", "closes", "volumes", "typical_prices",
    "sma", "ema", "wma",
    "rsi", "macd", "stochastic", "williams_r", "cci",
    "true_range", "atr", "rolling_std", "rolling_median",
    "bollinger", "keltner", "donchian",
    "dmi", "adx", "linreg_slope", "trend_strength", "hurst",
    "roc", "momentum", "zscore", "highest", "lowest",
    "obv", "vwap",
    "crossover", "crossunder", "last_cross",
]

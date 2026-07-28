"""Volatility estimation and volatility-targeted position sizing.

Volatility is the denominator of every risk decision in this package: it sets
position size, it scales stops, and it is the primary input to the regime
classifier. So it gets its own module with explicit, documented estimators
rather than a single `stdev()` call buried in a strategy.

Why several estimators
----------------------
Close-to-close realised volatility throws away three quarters of every bar. On
a bar that opened at 100, spiked to 130, collapsed to 70 and closed at 100 it
reports *zero*. The range-based estimators (Parkinson, Garman-Klass,
Rogers-Satchell, Yang-Zhang) read the high and the low too and are
correspondingly more efficient — Parkinson is roughly 5x more efficient than
close-to-close, Yang-Zhang more still — at the cost of assumptions that this
module states in each docstring rather than leaving implicit. They are not
interchangeable: Parkinson assumes no drift and no gaps, Garman-Klass assumes
no drift, Rogers-Satchell tolerates drift, Yang-Zhang tolerates both drift and
overnight gaps. Choosing wrongly biases size, so the caller chooses knowingly.

Annualisation is derived, never assumed
---------------------------------------
"252" is a US-equity daily-bar constant that has no business appearing in a
crypto 5-minute path, where the market runs 8760 hours a year. `bars_per_year`
turns a timeframe plus an explicit calendar convention into a bar count and
`annualisation_factor` takes its square root. Getting this wrong is not a
cosmetic error: it rescales every vol-targeted position by the ratio of the two
factors, which for 1h crypto bars mis-annualised on the equity convention is
about 4x.

Numeric domain
--------------
Every estimator here is analytics and therefore `float`, per the contracts
module's split. The one function that produces something an order can be built
from — `vol_target_position_size` — returns `Decimal`, and is the boundary
where the conversion happens.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from datetime import datetime
from decimal import Decimal
from typing import Sequence

from .clock import Clock, default_clock
from .contracts import Candle, Instrument, ensure_utc, jsonable, to_decimal
from .errors import ConfigurationError, InsufficientDataError

#: Seconds in a 24h day. Named so the arithmetic below reads as a convention
#: rather than a magic number.
SECONDS_PER_DAY = 86_400.0


# ---------------------------------------------------------------------------
# annualisation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Calendar:
    """How much wall time a market is actually open, per year.

    `days_per_year` counts *trading* days and `seconds_per_day` the length of a
    session, so their product is the number of seconds per year in which a bar
    can form. Dividing by the bar duration gives bars per year — the only
    correct way to annualise, because the answer differs by an order of
    magnitude between a 24/7 venue and a 6.5-hour equity session.
    """
    days_per_year: float
    seconds_per_day: float
    name: str = "custom"

    def __post_init__(self):
        if self.days_per_year <= 0 or self.seconds_per_day <= 0:
            raise ConfigurationError(
                "calendar days_per_year and seconds_per_day must be > 0",
                days_per_year=self.days_per_year,
                seconds_per_day=self.seconds_per_day)

    @property
    def seconds_per_year(self) -> float:
        return self.days_per_year * self.seconds_per_day


#: 24/7 markets (crypto, FX to a good approximation). 365 x 24h.
CONTINUOUS = Calendar(365.0, SECONDS_PER_DAY, "continuous")
#: US cash equities: 252 sessions of 6.5 hours. This is where "252" belongs —
#: attached to the market it describes, not hardcoded in an estimator.
US_EQUITY = Calendar(252.0, 6.5 * 3600.0, "us_equity")
#: CME-style near-24h futures: 252 sessions of 23 hours.
US_FUTURES = Calendar(252.0, 23.0 * 3600.0, "us_futures")

CALENDARS = {c.name: c for c in (CONTINUOUS, US_EQUITY, US_FUTURES)}


def bars_per_year(timeframe: str, calendar: Calendar = CONTINUOUS) -> float:
    """How many bars of `timeframe` fit into one trading year on `calendar`.

    Daily bars on `US_EQUITY` give back 252.0 exactly — the familiar constant
    falls out of the convention instead of being asserted by it.
    """
    from .instruments import timeframe_seconds  # local: keeps import graph flat

    seconds = float(timeframe_seconds(timeframe))
    if seconds <= 0:  # pragma: no cover - parse_timeframe rejects these first
        raise ConfigurationError("timeframe must be positive", timeframe=str(timeframe))
    # A bar longer than a session (a daily bar on an equity calendar) still
    # counts as one bar per session; clamping here is what makes "1d" on
    # US_EQUITY come out at 252 rather than 252 * 6.5/24.
    return calendar.seconds_per_year / min(seconds, calendar.seconds_per_day)


def annualisation_factor(timeframe: str, calendar: Calendar = CONTINUOUS) -> float:
    """sqrt(bars per year) — the multiplier taking per-bar sigma to annual sigma.

    Square root because variance, not volatility, is what scales linearly with
    time under the i.i.d. assumption every estimator here makes. That
    assumption is wrong in the tails (returns cluster), which is why annualised
    numbers are used for *comparison* and never as a tail-risk estimate.
    """
    return math.sqrt(bars_per_year(timeframe, calendar))


# ---------------------------------------------------------------------------
# small numeric helpers (stdlib only; `statistics` is fine but explicit is
# cheaper and lets us control the ddof and the None-padding in one place)
# ---------------------------------------------------------------------------

def _check_window(window: int, minimum: int = 2) -> int:
    if not isinstance(window, int) or isinstance(window, bool):
        raise ConfigurationError("window must be an int", got=type(window).__name__)
    if window < minimum:
        raise ConfigurationError(
            f"window must be >= {minimum}", window=window, minimum=minimum)
    return window


def _finite(values: Sequence[float], name: str) -> list[float]:
    out: list[float] = []
    for v in values:
        f = float(v)
        if not math.isfinite(f):
            raise ConfigurationError(f"{name} contains a non-finite value", got=repr(v))
        out.append(f)
    return out


def _positive(values: Sequence[float], name: str) -> list[float]:
    out = _finite(values, name)
    for f in out:
        if f <= 0:
            raise ConfigurationError(f"{name} must be > 0 to take a log", got=f)
    return out


def _stdev(sample: Sequence[float]) -> float:
    """Sample standard deviation (ddof=1).

    ddof=1 rather than 0 because a rolling window is a *sample* of the return
    process, not the population; with a 20-bar window ddof=0 understates sigma
    by ~2.5%, which is a systematic under-estimate of risk.
    """
    n = len(sample)
    if n < 2:
        raise ConfigurationError("stdev needs at least 2 observations", n=n)
    mean = math.fsum(sample) / n
    return math.sqrt(math.fsum((x - mean) ** 2 for x in sample) / (n - 1))


def log_returns(closes: Sequence[float]) -> list[float]:
    """Continuously-compounded returns; length `len(closes) - 1`.

    Log returns rather than simple returns because they are additive across
    time, which is what makes the sqrt-of-time annualisation above coherent.
    """
    prices = _positive(closes, "closes")
    return [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]


# ---------------------------------------------------------------------------
# estimators
#
# All of them return a list ALIGNED WITH THE INPUT BARS: element i is the
# estimate observable at the close of bar i, or None when bar i does not yet
# have a full window behind it. Alignment is deliberate — a shorter list that
# the caller has to re-index by hand is the classic way look-ahead bias gets
# into a backtest.
# ---------------------------------------------------------------------------

def realised_volatility(closes: Sequence[float], window: int,
                        annualisation: float = 1.0) -> list[float | None]:
    """Close-to-close realised volatility: stdev of log returns over `window`.

    Assumptions: returns are i.i.d. with zero-ish drift over the window, and
    every bar's close is a tradable price. Ignores intrabar range entirely, so
    it under-reports on days that round-trip. It is nonetheless the reference
    estimator, because it is the only one whose input (the close) is the price
    a strategy actually transacts at.

    `annualisation` is a multiplier — pass `annualisation_factor(timeframe,
    calendar)` for annualised numbers, or leave it at 1.0 for per-bar sigma.
    """
    _check_window(window, 2)
    prices = _positive(closes, "closes")
    rets = log_returns(prices)
    out: list[float | None] = [None] * len(prices)
    # ret[j] is the move INTO bar j+1, so the window of `window` returns ending
    # at bar i is rets[i-window : i].
    for i in range(window, len(prices)):
        out[i] = _stdev(rets[i - window:i]) * annualisation
    return out


def parkinson(highs: Sequence[float], lows: Sequence[float], window: int,
              annualisation: float = 1.0) -> list[float | None]:
    """Parkinson (1980) high-low range estimator.

    sigma = sqrt( 1/(4 ln2 . n) . sum ln(H/L)^2 )

    Assumptions: continuous geometric Brownian motion with **zero drift** and
    **no gaps** — the true high and low are observed. Both are violated in
    practice (discrete sampling misses the extremes, and overnight gaps happen
    outside the bar), so Parkinson is biased *low* on gappy instruments and is
    at its best on 24/7 intraday data. In exchange it is ~5x more statistically
    efficient than close-to-close for the same window.
    """
    _check_window(window, 1)
    h = _positive(highs, "highs")
    lo = _positive(lows, "lows")
    if len(h) != len(lo):
        raise ConfigurationError("highs and lows must be the same length",
                                 highs=len(h), lows=len(lo))
    coefficient = 1.0 / (4.0 * math.log(2.0))
    out: list[float | None] = [None] * len(h)
    for i in range(window - 1, len(h)):
        terms = [math.log(h[j] / lo[j]) ** 2 for j in range(i - window + 1, i + 1)]
        out[i] = math.sqrt(coefficient * math.fsum(terms) / window) * annualisation
    return out


def garman_klass(candles: Sequence[Candle], window: int,
                 annualisation: float = 1.0) -> list[float | None]:
    """Garman-Klass (1980) OHLC estimator.

    sigma^2 = 1/n . sum [ 0.5 ln(H/L)^2 - (2 ln2 - 1) ln(C/O)^2 ]

    Assumptions: zero drift, no overnight gaps, continuous trading within the
    bar. Uses open and close as well as the range, making it ~7x more efficient
    than close-to-close.

    The second term is subtracted, but on any coherent candle
    |ln(C/O)| <= ln(H/L) and 0.5 > 2ln2-1, so the per-bar term cannot actually
    go negative. The clamp below exists only so that accumulated float error on
    a near-zero variance cannot produce a NaN under `sqrt` — a NaN would
    propagate silently into a position size.
    """
    _check_window(window, 1)
    o, h, lo, c = _ohlc(candles)
    k = 2.0 * math.log(2.0) - 1.0
    out: list[float | None] = [None] * len(c)
    for i in range(window - 1, len(c)):
        terms = [
            0.5 * math.log(h[j] / lo[j]) ** 2 - k * math.log(c[j] / o[j]) ** 2
            for j in range(i - window + 1, i + 1)
        ]
        variance = math.fsum(terms) / window
        out[i] = math.sqrt(max(variance, 0.0)) * annualisation
    return out


def rogers_satchell(candles: Sequence[Candle], window: int,
                    annualisation: float = 1.0) -> list[float | None]:
    """Rogers-Satchell (1991) drift-independent OHLC estimator.

    sigma^2 = 1/n . sum [ ln(H/C)ln(H/O) + ln(L/C)ln(L/O) ]

    The reason to reach for this one: unlike Parkinson and Garman-Klass it is
    **unbiased in the presence of drift**, so a strongly trending instrument
    does not have its trend counted as volatility. It still assumes no
    overnight gaps. Every term is non-negative by construction, so no clamping
    is needed.
    """
    _check_window(window, 1)
    o, h, lo, c = _ohlc(candles)
    out: list[float | None] = [None] * len(c)
    for i in range(window - 1, len(c)):
        terms = [
            math.log(h[j] / c[j]) * math.log(h[j] / o[j])
            + math.log(lo[j] / c[j]) * math.log(lo[j] / o[j])
            for j in range(i - window + 1, i + 1)
        ]
        out[i] = math.sqrt(max(math.fsum(terms) / window, 0.0)) * annualisation
    return out


def yang_zhang(candles: Sequence[Candle], window: int,
               annualisation: float = 1.0) -> list[float | None]:
    """Yang-Zhang (2000): the minimum-variance combination of three components.

    sigma^2 = sigma_overnight^2 + k.sigma_open_to_close^2 + (1-k).sigma_RS^2
    with k = 0.34 / (1.34 + (n+1)/(n-1)), the weight that minimises estimator
    variance.

    This is the only estimator here that is both **drift-independent** and
    **gap-aware**: the overnight term is the variance of ln(O_t / C_{t-1}),
    which is exactly the jump the other three throw away. That makes it the
    right default for instruments with real session breaks (equities, futures).

    Cost: it needs the previous bar's close, so a `window`-bar estimate
    consumes `window + 1` candles, and the first `window` outputs are None
    (one more than the other estimators). `window` must be >= 2 because both
    variance components are sample variances.
    """
    _check_window(window, 2)
    o, h, lo, c = _ohlc(candles)
    n = window
    k = 0.34 / (1.34 + (n + 1) / (n - 1))
    out: list[float | None] = [None] * len(c)
    rs = rogers_satchell(candles, window)
    for i in range(window, len(c)):
        lo_window = range(i - window + 1, i + 1)
        overnight = [math.log(o[j] / c[j - 1]) for j in lo_window]
        intraday = [math.log(c[j] / o[j]) for j in lo_window]
        var_o = _variance(overnight)
        var_c = _variance(intraday)
        var_rs = (rs[i] or 0.0) ** 2
        out[i] = math.sqrt(max(var_o + k * var_c + (1.0 - k) * var_rs, 0.0)) * annualisation
    return out


def _variance(sample: Sequence[float]) -> float:
    return _stdev(sample) ** 2


def _ohlc(candles: Sequence[Candle]) -> tuple[list[float], list[float],
                                              list[float], list[float]]:
    """Unpack candles into parallel OHLC lists, validating the type once."""
    if not candles:
        return [], [], [], []
    for candle in candles:
        if not isinstance(candle, Candle):
            raise ConfigurationError("expected a sequence of Candle",
                                     got=type(candle).__name__)
    return ([c.open for c in candles], [c.high for c in candles],
            [c.low for c in candles], [c.close for c in candles])


def ewma_volatility(returns: Sequence[float], lam: float = 0.94,
                    annualisation: float = 1.0,
                    seed: float | None = None) -> list[float]:
    """RiskMetrics exponentially-weighted volatility.

    var_t = lam . var_{t-1} + (1 - lam) . r_t^2

    Unlike a rolling window it has no cliff: a large return decays out of the
    estimate smoothly instead of dropping out abruptly `window` bars later
    (the "ghost feature" that makes rolling vol jump for no market reason).
    lam=0.94 is the RiskMetrics daily default — an effective memory of about
    1/(1-lam) = 17 bars. Use a larger lam for finer bars.

    The recursion is seeded with r_0^2 (or `seed` variance if given), so the
    first few values are dominated by the seed; callers wanting a settled
    number should discard roughly 3/(1-lam) leading bars.

    Returns one value per input return (never None) — the recursion is defined
    from the first observation onward.
    """
    if not 0.0 < lam < 1.0:
        raise ConfigurationError("ewma lambda must be in (0, 1)", lam=lam)
    rets = _finite(returns, "returns")
    if not rets:
        return []
    if seed is None:
        variance = rets[0] ** 2
    else:
        if seed < 0:
            raise ConfigurationError("ewma seed variance must be >= 0", seed=seed)
        variance = float(seed)
    out: list[float] = []
    for r in rets:
        variance = lam * variance + (1.0 - lam) * r * r
        out.append(math.sqrt(variance) * annualisation)
    return out


def true_range(candles: Sequence[Candle]) -> list[float | None]:
    """Wilder's true range; element 0 is None because it needs a previous close.

    max(H-L, |H-C_prev|, |L-C_prev|) — the |...C_prev| terms are what make it
    account for a gap the bar's own range cannot see.
    """
    o, h, lo, c = _ohlc(candles)
    out: list[float | None] = [None] * len(c)
    for i in range(1, len(c)):
        out[i] = max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1]))
    return out


def average_true_range(candles: Sequence[Candle], window: int = 14) -> list[float | None]:
    """Wilder-smoothed ATR.

    Wilder's smoothing (atr = atr + (tr - atr)/n) rather than an SMA, because
    that is what every published ATR-based rule assumes; an SMA-ATR of the same
    period is a materially faster series and would silently change the meaning
    of any stop distance expressed in ATRs.
    """
    _check_window(window, 1)
    tr = true_range(candles)
    out: list[float | None] = [None] * len(tr)
    # First ATR is the simple mean of the first `window` true ranges (indices
    # 1..window), which is Wilder's own initialisation.
    if len(tr) < window + 1:
        return out
    atr = math.fsum(float(x) for x in tr[1:window + 1]) / window
    out[window] = atr
    for i in range(window + 1, len(tr)):
        atr = atr + ((tr[i] or 0.0) - atr) / window
        out[i] = atr
    return out


def percentile_rank(value: float, history: Sequence[float]) -> float:
    """Fraction of `history` at or below `value`, in [0, 1] (mid-rank on ties).

    Mid-rank rather than "strictly below" so that a perfectly flat history
    ranks at 0.5 instead of 1.0 — a constant series is not an extreme.
    """
    values = _finite(history, "history")
    if not values:
        return 0.0
    below = sum(1 for v in values if v < value)
    equal = sum(1 for v in values if v == value)
    return (below + 0.5 * equal) / len(values)


# ---------------------------------------------------------------------------
# analyser
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VolatilityBands:
    """Percentile cut-points naming a volatility level.

    Percentiles, not absolute sigma, because "high volatility" is only
    meaningful relative to the instrument's own history: 40% annualised is calm
    for a small-cap crypto and a crisis for a government bond.
    """
    low: float = 0.25
    elevated: float = 0.75
    extreme: float = 0.95

    def __post_init__(self):
        if not 0.0 < self.low < self.elevated < self.extreme < 1.0:
            raise ConfigurationError(
                "volatility bands must satisfy 0 < low < elevated < extreme < 1",
                low=self.low, elevated=self.elevated, extreme=self.extreme)

    def label(self, rank: float) -> str:
        if rank < self.low:
            return "low"
        if rank < self.elevated:
            return "normal"
        if rank < self.extreme:
            return "elevated"
        return "extreme"


@dataclass(frozen=True)
class VolatilityState:
    """The volatility picture at one bar, as consumed by strategies and risk.

    `realised` and `ewma` are annualised with the factor derived from the
    instrument's timeframe; `annualisation` is carried alongside so an audit
    record can be re-derived years later without guessing the convention.
    """
    ts: datetime
    instrument_key: str
    realised: float
    ewma: float
    percentile_rank: float
    regime_label: str
    atr_pct: float
    timeframe: str = ""
    samples: int = 0
    annualisation: float = 1.0

    def __post_init__(self):
        object.__setattr__(self, "ts", ensure_utc(self.ts, field_name="ts"))
        if not 0.0 <= self.percentile_rank <= 1.0:
            raise ConfigurationError("percentile_rank must be in [0, 1]",
                                     got=self.percentile_rank)

    def to_dict(self) -> dict:
        return {k: jsonable(v) for k, v in asdict(self).items()}


class VolatilityAnalyzer:
    """Turns a candle history into a `VolatilityState`.

    Stateless between calls on purpose: it is handed the history it needs and
    derives everything from it, so backtest and live produce identical states
    for identical input and there is no hidden warm-up to get wrong.
    """

    def __init__(self, lookback: int = 120, clock: Clock | None = None, *,
                 window: int = 20, atr_window: int = 14,
                 ewma_lambda: float = 0.94,
                 calendar: Calendar = CONTINUOUS,
                 bands: VolatilityBands | None = None):
        self.lookback = _check_window(lookback, 2)
        self.window = _check_window(window, 2)
        self.atr_window = _check_window(atr_window, 1)
        self.ewma_lambda = ewma_lambda
        self.calendar = calendar
        self.bands = bands or VolatilityBands()
        self.clock = clock or default_clock()
        if lookback <= window:
            raise ConfigurationError(
                "lookback must exceed window, otherwise there is no trailing "
                "history to rank the current reading against",
                lookback=lookback, window=window)

    @property
    def required_bars(self) -> int:
        """Minimum bars for `analyse` to produce a state.

        `window + 1` closes yield the first realised-vol reading; the rank then
        needs a trailing distribution, for which we insist on at least half the
        lookback so an early, thin sample cannot declare bar 25 a 100th
        percentile event.
        """
        return max(self.window + 1 + self.lookback // 2, self.atr_window + 2)

    def analyse(self, candles: Sequence[Candle]) -> VolatilityState:
        """Compute the state at the last candle.

        Raises `InsufficientDataError` rather than returning a degraded state:
        an under-warmed volatility number sizes positions wrongly and there is
        no safe default to substitute.
        """
        if len(candles) < self.required_bars:
            raise InsufficientDataError(
                "not enough bars for a volatility estimate",
                have=len(candles), need=self.required_bars)
        recent = list(candles[-(self.lookback + self.window + 1):])
        timeframe = recent[-1].timeframe
        factor = annualisation_factor(timeframe, self.calendar)

        closes = [c.close for c in recent]
        realised_series = realised_volatility(closes, self.window, factor)
        history = [v for v in realised_series[:-1] if v is not None]
        realised = realised_series[-1]
        if realised is None:  # pragma: no cover - required_bars guarantees this
            raise InsufficientDataError("realised volatility unavailable",
                                        have=len(candles))

        rets = log_returns(closes)
        ewma_series = ewma_volatility(rets, self.ewma_lambda, factor)
        ewma = ewma_series[-1] if ewma_series else 0.0

        atr_series = average_true_range(recent, self.atr_window)
        atr = atr_series[-1]
        atr_pct = (atr / closes[-1]) if atr else 0.0

        rank = percentile_rank(realised, history)
        return VolatilityState(
            ts=recent[-1].ts_close,
            instrument_key=recent[-1].instrument_key,
            realised=realised,
            ewma=ewma,
            percentile_rank=rank,
            regime_label=self.bands.label(rank),
            atr_pct=atr_pct,
            timeframe=timeframe,
            samples=len(history),
            annualisation=factor,
        )

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (f"VolatilityAnalyzer(lookback={self.lookback}, "
                f"window={self.window}, calendar={self.calendar.name!r})")


# ---------------------------------------------------------------------------
# sizing
# ---------------------------------------------------------------------------

def vol_target_position_size(target_vol: float, current_vol: float,
                             equity: Decimal | float | int | str,
                             price: Decimal | float | int | str, *,
                             max_leverage: float = 1.0,
                             instrument: Instrument | None = None) -> Decimal:
    """Quantity whose notional has annualised volatility `target_vol`.

    notional = equity . (target_vol / current_vol), capped at
    `max_leverage . equity`; quantity = notional / price.

    This is where the float analytics domain meets the Decimal accounting
    domain, so the result is a `Decimal` and — when an `Instrument` is supplied
    — is quantised DOWN to a tradable lot. Rounding down rather than to nearest
    is deliberate: overshooting a size limit is a risk breach, undershooting is
    only a slightly smaller position.

    Degenerate inputs return `Decimal(0)` rather than raising or clamping to
    the leverage cap:

    * `current_vol <= 0` — a zero volatility estimate means either a dead
      market or a broken estimator. The formula's answer is "infinite size";
      the honest answer is "do not size against this number".
    * non-positive equity or price — there is nothing to allocate.

    A negative `target_vol` or `max_leverage` is a configuration bug, not a
    market condition, so those raise.
    """
    if not math.isfinite(target_vol) or target_vol < 0:
        raise ConfigurationError("target_vol must be finite and >= 0",
                                 target_vol=target_vol)
    if not math.isfinite(max_leverage) or max_leverage < 0:
        raise ConfigurationError("max_leverage must be finite and >= 0",
                                 max_leverage=max_leverage)
    zero = Decimal(0)
    if not math.isfinite(current_vol) or current_vol <= 0:
        return zero
    equity_d = to_decimal(equity, field_name="equity")
    price_d = to_decimal(price, field_name="price")
    if equity_d <= 0 or price_d <= 0:
        return zero

    scale = min(target_vol / current_vol, max_leverage)
    if scale <= 0:
        return zero
    # Decimal(repr(...)) via to_decimal: never Decimal(float), which would
    # smuggle binary representation error into an order quantity.
    notional = equity_d * to_decimal(scale, field_name="scale")
    quantity = notional / price_d
    if instrument is not None:
        return instrument.quantize_quantity(quantity)
    return quantity


__all__ = [
    "SECONDS_PER_DAY", "Calendar", "CONTINUOUS", "US_EQUITY", "US_FUTURES",
    "CALENDARS", "bars_per_year", "annualisation_factor", "log_returns",
    "realised_volatility", "parkinson", "garman_klass", "rogers_satchell",
    "yang_zhang", "ewma_volatility", "true_range", "average_true_range",
    "percentile_rank", "VolatilityBands", "VolatilityState",
    "VolatilityAnalyzer", "vol_target_position_size",
]

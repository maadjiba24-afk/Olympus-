"""Tests for the pure-stdlib technical analysis module.

Three properties matter more than any individual indicator's value, because
each of them is a silent-corruption vector rather than a crash:

* **Alignment** — every output is exactly as long as its input. A short return
  is how a backtest's signals end up shifted forward against its candles.
* **Warm-up exactness** — the ``None`` region is where it is documented to be,
  to the index. One bar too few means the first real value was computed from an
  incomplete window; one too many means a usable bar was thrown away.
* **Short-input safety** — 3 bars and a 200-period average is an ordinary state
  at the start of a run and must return all-``None``, never raise.

The known-value checks are hand-computed in the docstrings so a future change
to the smoothing convention has to argue with arithmetic, not with a fixture.
"""

from datetime import datetime, timedelta, timezone

import pytest

from olympus.trading import ta
from olympus.trading.contracts import Candle
from olympus.trading.errors import ConfigurationError, DataValidationError

KEY = "SIM:BTCUSD"
T0 = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc)


def bar(index: int, open_: float, high: float, low: float, close: float,
        volume: float = 100.0) -> Candle:
    ts_open = T0 + timedelta(minutes=index)
    return Candle(instrument_key=KEY, timeframe="1m", ts_open=ts_open,
                  ts_close=ts_open + timedelta(minutes=1), open=open_,
                  high=high, low=low, close=close, volume=volume,
                  amount=close * volume, source="test")


def flat_bars(closes, volume: float = 100.0):
    """Candles whose OHLC bracket the given closes by a fixed 1% envelope."""
    out = []
    for i, close in enumerate(closes):
        open_ = closes[i - 1] if i else close
        out.append(bar(i, open_, max(open_, close) * 1.01,
                       min(open_, close) * 0.99, close, volume))
    return out


# --- moving averages -------------------------------------------------------

def test_sma_known_values_and_warmup():
    """sma([1,2,3,4,5], 3): mean(1,2,3)=2, mean(2,3,4)=3, mean(3,4,5)=4."""
    assert ta.sma([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]


def test_ema_is_seeded_with_the_sma_of_the_first_window():
    """ema([1,2,3,4,5], 3): alpha=0.5, seed=sma(1,2,3)=2.0.
    idx3 = 0.5*4 + 0.5*2.0 = 3.0; idx4 = 0.5*5 + 0.5*3.0 = 4.0."""
    assert ta.ema([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]


def test_ema_does_not_depend_on_how_much_extra_history_precedes_the_seed():
    """The seeding choice is the point: a longer identical prefix must not
    change values once both series have settled."""
    short = ta.ema([5.0] * 10 + [6, 7, 8, 9], 4)
    long = ta.ema([5.0] * 40 + [6, 7, 8, 9], 4)
    assert short[-1] == pytest.approx(long[-1])


def test_ema_reseeds_after_an_interior_hole_instead_of_smoothing_across_it():
    values = [1.0, 2.0, 3.0, None, 4.0, 5.0, 6.0, 7.0]
    out = ta.ema(values, 3)
    assert out[2] == 2.0            # seeded on mean(1,2,3)
    assert out[3] is None           # the hole
    assert out[4] is None and out[5] is None
    assert out[6] == 5.0            # re-seeded on mean(4,5,6)


def test_wma_weights_the_newest_bar_most():
    """wma([1,2,3], 3) = (1*1 + 2*2 + 3*3)/6 = 14/6."""
    out = ta.wma([1, 2, 3], 3)
    assert out[:2] == [None, None]
    assert out[2] == pytest.approx(14 / 6)


# --- RSI -------------------------------------------------------------------

def test_rsi_known_values_with_wilder_smoothing():
    """values [10, 11, 10.5, 11.5], period 2. Deltas: +1, -0.5, +1.
    Seed at idx2: avg_gain=(1+0)/2=0.5, avg_loss=(0+0.5)/2=0.25, RS=2 -> 66.67.
    idx3: avg_gain=(0.5*1+1)/2=0.75, avg_loss=(0.25*1+0)/2=0.125, RS=6 -> 85.71.
    """
    out = ta.rsi([10, 11, 10.5, 11.5], 2)
    assert out[0] is None and out[1] is None
    assert out[2] == pytest.approx(100 - 100 / 3)
    assert out[3] == pytest.approx(100 - 100 / 7)


def test_rsi_warmup_is_one_longer_than_a_moving_average():
    out = ta.rsi(list(range(1, 40)), 14)
    assert out[:14] == [None] * 14
    assert out[14] is not None


def test_rsi_extremes_and_flat_market():
    assert ta.rsi(list(range(1, 20)), 5)[-1] == pytest.approx(100.0)
    assert ta.rsi(list(range(20, 1, -1)), 5)[-1] == pytest.approx(0.0)
    # A halted instrument is neither overbought nor oversold.
    assert ta.rsi([50.0] * 20, 5)[-1] == 50.0


# --- MACD ------------------------------------------------------------------

def test_macd_known_values_on_a_hand_computed_ramp():
    """values 1..6, fast=2, slow=3, signal=2.
    ema2 = [_, 1.5, 2.5, 3.5, 4.5, 5.5]; ema3 = [_, _, 2, 3, 4, 5]
    macd = 0.5 from idx2; signal = ema(macd,2) = 0.5 from idx3; hist = 0."""
    line, signal, hist = ta.macd([1, 2, 3, 4, 5, 6], 2, 3, 2)
    assert line[:2] == [None, None]
    assert all(v == pytest.approx(0.5) for v in line[2:])
    assert signal[:3] == [None, None, None]
    assert all(v == pytest.approx(0.5) for v in signal[3:])
    assert hist[:3] == [None, None, None]
    assert all(v == pytest.approx(0.0) for v in hist[3:])


def test_macd_warmup_indices_match_the_documented_formula():
    line, signal, hist = ta.macd(list(range(1, 60)), 12, 26, 9)
    assert line[:25] == [None] * 25 and line[25] is not None      # slow - 1
    assert signal[:33] == [None] * 33 and signal[33] is not None  # slow+signal-2
    assert hist[:33] == [None] * 33 and hist[33] is not None


def test_macd_rejects_fast_at_or_above_slow():
    with pytest.raises(ConfigurationError):
        ta.macd([1.0] * 50, 26, 12, 9)


# --- ATR / true range ------------------------------------------------------

def test_true_range_first_bar_uses_high_minus_low():
    bars = [bar(0, 9, 10, 8, 9), bar(1, 9, 12, 9, 11)]
    assert ta.true_range(bars)[0] == pytest.approx(2.0)


def test_atr_known_values_with_wilder_smoothing():
    """TRs: bar0 = 10-8 = 2; bar1 = max(12-9, |12-9|, |9-9|) = 3;
    bar2 = max(11-10, |11-11|, |10-11|) = 1.
    period 2: atr[1] = (2+3)/2 = 2.5; atr[2] = (2.5*1 + 1)/2 = 1.75."""
    bars = [bar(0, 9, 10, 8, 9), bar(1, 9, 12, 9, 11), bar(2, 11, 11, 10, 10.5)]
    out = ta.atr(bars, 2)
    assert out[0] is None
    assert out[1] == pytest.approx(2.5)
    assert out[2] == pytest.approx(1.75)


def test_atr_short_input_is_all_none():
    assert ta.atr(flat_bars([100.0, 101.0]), 14) == [None, None]


# --- bands and channels ----------------------------------------------------

def test_bollinger_known_values():
    """[2,4,4,4,5,5,7,9] has mean 5 and population stdev 2."""
    upper, middle, lower = ta.bollinger([2, 4, 4, 4, 5, 5, 7, 9], 8, 2.0)
    assert middle[-1] == pytest.approx(5.0)
    assert upper[-1] == pytest.approx(9.0)
    assert lower[-1] == pytest.approx(1.0)
    assert middle[:7] == [None] * 7


def test_donchian_tracks_window_extremes():
    bars = [bar(0, 10, 12, 9, 11), bar(1, 11, 15, 10, 14), bar(2, 14, 14, 8, 9)]
    upper, middle, lower = ta.donchian(bars, 2)
    assert upper[1] == pytest.approx(15.0) and lower[1] == pytest.approx(9.0)
    assert middle[1] == pytest.approx(12.0)
    assert upper[2] == pytest.approx(15.0) and lower[2] == pytest.approx(8.0)


def test_keltner_bands_bracket_their_midline():
    bars = flat_bars([100 + i * 0.5 for i in range(40)])
    upper, middle, lower = ta.keltner(bars, 20, 2.0)
    assert lower[-1] < middle[-1] < upper[-1]
    assert middle[:19] == [None] * 19


# --- oscillators -----------------------------------------------------------

def test_stochastic_known_values_and_zero_width_range():
    bars = [bar(0, 10, 10, 10, 10), bar(1, 10, 20, 10, 15)]
    percent_k, percent_d = ta.stochastic(bars, 2, 2)
    # window high 20, low 10, close 15 -> exactly mid-range.
    assert percent_k[1] == pytest.approx(50.0)
    assert percent_d[1] is None          # needs 2 %K values: warm-up 2+2-2 = 2
    halted = [bar(0, 10, 10, 10, 10), bar(1, 10, 10, 10, 10)]
    assert ta.stochastic(halted, 2, 1)[0][1] == 50.0


def test_williams_r_is_the_stochastic_mirror():
    bars = flat_bars([100, 105, 102, 110, 108, 112])
    percent_k, _ = ta.stochastic(bars, 3, 1)
    r = ta.williams_r(bars, 3)
    for k, w in zip(percent_k[2:], r[2:]):
        assert w == pytest.approx(k - 100.0)


def test_cci_is_zero_when_price_sits_on_its_own_mean():
    bars = [bar(i, 100, 101, 99, 100) for i in range(5)]
    assert ta.cci(bars, 5)[-1] == pytest.approx(0.0)


# --- trend -----------------------------------------------------------------

def test_adx_warmup_is_two_periods_minus_one():
    bars = flat_bars([100 + i for i in range(60)])
    out = ta.adx(bars, 14)
    assert out[:27] == [None] * 27
    assert out[27] is not None
    assert all(0.0 <= v <= 100.0 for v in out[27:])


def test_dmi_puts_positive_di_on_top_in_an_uptrend():
    bars = flat_bars([100 + i * 2 for i in range(60)])
    plus_di, minus_di, adx_line = ta.dmi(bars, 14)
    assert plus_di[-1] > minus_di[-1]
    assert adx_line[-1] is not None and adx_line[-1] > 20.0


def test_linreg_slope_of_a_ramp_is_its_step():
    assert ta.linreg_slope([1, 2, 3, 4], 4)[-1] == pytest.approx(1.0)
    assert ta.linreg_slope([10, 8, 6, 4], 4)[-1] == pytest.approx(-2.0)


def test_trend_strength_is_signed_and_bounded():
    up = ta.trend_strength([1, 2, 3, 4, 5], 5)[-1]
    down = ta.trend_strength([5, 4, 3, 2, 1], 5)[-1]
    zigzag = ta.trend_strength([1, 2, 1, 2, 1], 5)[-1]
    assert up == pytest.approx(1.0)
    assert down == pytest.approx(-1.0)
    assert zigzag == pytest.approx(0.0)
    assert ta.trend_strength([7.0] * 5, 5)[-1] == 0.0


def _walk(phi: float, length: int = 500):
    """A price path whose increments are AR(1) with coefficient `phi`.

    phi > 0 is persistent (a move tends to be followed by a move the same way),
    phi = 0 is a random walk, phi < 0 is mean-reverting. Driven by a small LCG
    so the series is fixed forever — no randomness without an explicit seed.
    """
    state = 12345
    price, increment = 100.0, 0.0
    out = []
    for _ in range(length):
        state = (1103515245 * state + 12345) % (2 ** 31)
        increment = phi * increment + (state / (2 ** 31) - 0.5)
        price += increment
        out.append(price)
    return out


def test_hurst_separates_persistent_from_mean_reverting_series():
    persistent = ta.hurst(_walk(0.85), 128)[-1]
    random_walk = ta.hurst(_walk(0.0), 128)[-1]
    reverting = ta.hurst(_walk(-0.85), 128)[-1]
    assert reverting < 0.45 < random_walk < 0.55 < persistent
    assert 0.0 <= reverting and persistent <= 1.0


def test_hurst_rejects_windows_too_short_to_estimate():
    with pytest.raises(ConfigurationError):
        ta.hurst([1.0] * 100, 4)


def test_hurst_returns_none_when_the_regression_is_undefined():
    # A perfect ramp has zero dispersion at every lag: log(0) is not a number
    # this function is entitled to invent.
    assert ta.hurst([float(i) for i in range(40)], 16)[-1] is None


# --- rate of change / normalisation ---------------------------------------

def test_roc_and_momentum_warmups_and_values():
    assert ta.roc([100, 110, 121], 1) == [None, pytest.approx(10.0),
                                          pytest.approx(10.0)]
    assert ta.momentum([100, 110, 121], 2) == [None, None, pytest.approx(21.0)]


def test_roc_returns_none_rather_than_dividing_by_zero():
    assert ta.roc([0.0, 5.0], 1)[1] is None


def test_zscore_known_values_and_flat_window():
    """[2,4,4,4,5,5,7,9]: mean 5, population stdev 2 -> (9-5)/2 = 2."""
    assert ta.zscore([2, 4, 4, 4, 5, 5, 7, 9], 8)[-1] == pytest.approx(2.0)
    assert ta.zscore([3.0] * 5, 5)[-1] == 0.0


def test_rolling_std_and_median():
    assert ta.rolling_std([2, 4, 4, 4, 5, 5, 7, 9], 8)[-1] == pytest.approx(2.0)
    assert ta.rolling_median([5, 1, 3], 3)[-1] == pytest.approx(3.0)
    assert ta.rolling_median([5, 1, 3, 9], 4)[-1] == pytest.approx(4.0)


def test_rolling_std_rejects_a_ddof_that_empties_the_window():
    with pytest.raises(ConfigurationError):
        ta.rolling_std([1.0, 2.0], 1, ddof=1)


def test_highest_and_lowest():
    assert ta.highest([1, 5, 3, 2], 2) == [None, 5.0, 5.0, 3.0]
    assert ta.lowest([1, 5, 3, 2], 2) == [None, 1.0, 3.0, 2.0]


# --- volume ----------------------------------------------------------------

def test_obv_accumulates_signed_volume_from_zero():
    bars = [bar(0, 10, 11, 9, 10, volume=5),
            bar(1, 10, 12, 10, 11, volume=7),    # up   -> +7
            bar(2, 11, 11, 9, 10, volume=3),     # down -> -3
            bar(3, 10, 11, 9, 10, volume=9)]     # flat -> unchanged
    assert ta.obv(bars) == [0.0, 7.0, 4.0, 4.0]


def test_vwap_cumulative_and_rolling():
    bars = [bar(0, 10, 12, 6, 9, volume=1), bar(1, 9, 12, 6, 9, volume=1)]
    # typical price is (12+6+9)/3 = 9 for both bars.
    assert ta.vwap(bars) == [pytest.approx(9.0), pytest.approx(9.0)]
    assert ta.vwap(bars, 2) == [None, pytest.approx(9.0)]


def test_vwap_is_none_when_nothing_traded():
    bars = [bar(0, 10, 11, 9, 10, volume=0.0)]
    assert ta.vwap(bars) == [None]


# --- crossovers ------------------------------------------------------------

def test_crossover_and_crossunder_fire_once_on_the_crossing_bar():
    fast = [1.0, 2.0, 3.0, 2.0, 1.0]
    slow = [2.0, 2.0, 2.0, 2.0, 2.0]
    assert ta.crossover(fast, slow) == [False, False, True, False, False]
    assert ta.crossunder(fast, slow) == [False, False, False, False, True]


def test_crossover_accepts_a_scalar_threshold_and_ignores_holes():
    assert ta.crossover([None, 20.0, 40.0], 30.0) == [False, False, True]
    assert ta.crossover([None, None, 40.0], 30.0) == [False, False, False]


def test_last_cross_reports_index_and_direction():
    fast = [1.0, 3.0, 1.0]
    slow = [2.0, 2.0, 2.0]
    assert ta.last_cross(fast, slow) == (2, -1)
    assert ta.last_cross([1.0, 1.0], [2.0, 2.0]) is None


def test_crossover_rejects_mismatched_lengths():
    with pytest.raises(DataValidationError):
        ta.crossover([1.0, 2.0], [1.0])


# --- cross-cutting invariants ---------------------------------------------

_SERIES_FNS = [
    ("sma", lambda v: ta.sma(v, 5)),
    ("ema", lambda v: ta.ema(v, 5)),
    ("wma", lambda v: ta.wma(v, 5)),
    ("rsi", lambda v: ta.rsi(v, 5)),
    ("rolling_std", lambda v: ta.rolling_std(v, 5)),
    ("rolling_median", lambda v: ta.rolling_median(v, 5)),
    ("linreg_slope", lambda v: ta.linreg_slope(v, 5)),
    ("trend_strength", lambda v: ta.trend_strength(v, 5)),
    ("roc", lambda v: ta.roc(v, 5)),
    ("momentum", lambda v: ta.momentum(v, 5)),
    ("zscore", lambda v: ta.zscore(v, 5)),
    ("highest", lambda v: ta.highest(v, 5)),
    ("lowest", lambda v: ta.lowest(v, 5)),
    ("hurst", lambda v: ta.hurst(v, 8)),
]

_CANDLE_FNS = [
    ("atr", lambda c: ta.atr(c, 5)),
    ("true_range", lambda c: ta.true_range(c)),
    ("adx", lambda c: ta.adx(c, 5)),
    ("cci", lambda c: ta.cci(c, 5)),
    ("williams_r", lambda c: ta.williams_r(c, 5)),
    ("obv", lambda c: ta.obv(c)),
    ("vwap", lambda c: ta.vwap(c, 5)),
    ("vwap_session", lambda c: ta.vwap(c)),
]


@pytest.mark.parametrize("name,fn", _SERIES_FNS)
@pytest.mark.parametrize("length", [0, 1, 3, 30])
def test_series_functions_return_one_value_per_input(name, fn, length):
    values = [100.0 + i for i in range(length)]
    assert len(fn(values)) == length


@pytest.mark.parametrize("name,fn", _CANDLE_FNS)
@pytest.mark.parametrize("length", [0, 1, 3, 30])
def test_candle_functions_return_one_value_per_candle(name, fn, length):
    bars = flat_bars([100.0 + i for i in range(length)])
    assert len(fn(bars)) == length


@pytest.mark.parametrize("name,fn", _SERIES_FNS)
def test_short_input_is_all_none_and_never_raises(name, fn):
    assert fn([100.0, 101.0]) == [None, None]


@pytest.mark.parametrize("name,fn", _CANDLE_FNS)
def test_short_candle_input_never_raises(name, fn):
    fn(flat_bars([100.0, 101.0]))       # must not raise


@pytest.mark.parametrize("name,fn", [
    ("sma", ta.sma), ("ema", ta.ema), ("wma", ta.wma), ("rsi", ta.rsi),
    ("rolling_median", ta.rolling_median), ("linreg_slope", ta.linreg_slope),
    ("trend_strength", ta.trend_strength), ("roc", ta.roc),
    ("momentum", ta.momentum), ("zscore", ta.zscore),
    ("highest", ta.highest), ("lowest", ta.lowest),
])
@pytest.mark.parametrize("period", [0, -1])
def test_non_positive_period_is_a_configuration_error(name, fn, period):
    with pytest.raises(ConfigurationError):
        fn([1.0, 2.0, 3.0], period)


@pytest.mark.parametrize("period", [0, -3])
def test_candle_indicators_reject_non_positive_periods(period):
    bars = flat_bars([100.0, 101.0, 102.0])
    for fn in (ta.atr, ta.cci, ta.williams_r, ta.adx):
        with pytest.raises(ConfigurationError):
            fn(bars, period)


def test_non_finite_input_is_rejected_rather_than_propagated():
    with pytest.raises(DataValidationError):
        ta.sma([1.0, float("nan"), 3.0], 2)
    with pytest.raises(DataValidationError):
        ta.sma([1.0, float("inf")], 2)


def test_period_must_be_an_int_not_a_float():
    with pytest.raises(ConfigurationError):
        ta.sma([1.0, 2.0, 3.0], 2.0)


def test_indicators_reject_non_candle_sequences():
    with pytest.raises(DataValidationError):
        ta.atr([1.0, 2.0, 3.0], 2)


def test_warmup_region_is_exactly_period_minus_one_for_window_functions():
    values = [100.0 + i for i in range(30)]
    for fn in (ta.sma, ta.ema, ta.wma, ta.rolling_std, ta.rolling_median,
               ta.highest, ta.lowest, ta.linreg_slope, ta.trend_strength):
        out = fn(values, 7)
        assert out[:6] == [None] * 6, fn.__name__
        assert out[6] is not None, fn.__name__

"""Tests for olympus.trading.volatility.

The estimators are checked against values computed by hand from the published
formulae rather than against a previous run of this code, so a refactor that
changes the maths fails here instead of silently re-baselining.
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from olympus.trading.contracts import Candle, Instrument
from olympus.trading.errors import ConfigurationError, InsufficientDataError
from olympus.trading import volatility as V

START = datetime(2024, 1, 1, tzinfo=timezone.utc)
KEY = "SIM:BTCUSD"


def make_candles(rows, timeframe="1h", key=KEY):
    """Build candles from (open, high, low, close) tuples."""
    step = timedelta(seconds=3600)
    out = []
    for i, (o, h, l, c) in enumerate(rows):
        out.append(Candle(instrument_key=key, timeframe=timeframe,
                          ts_open=START + i * step, ts_close=START + (i + 1) * step,
                          open=o, high=h, low=l, close=c, volume=1.0))
    return out


def from_closes(closes, timeframe="1h", key=KEY, wick=0.001):
    """Candles that merely connect a close path, with symmetric small wicks."""
    rows = []
    prev = closes[0]
    for c in closes:
        rows.append((prev, max(prev, c) * (1 + wick), min(prev, c) * (1 - wick), c))
        prev = c
    return make_candles(rows, timeframe=timeframe, key=key)


# ---------------------------------------------------------------------------
# annualisation
# ---------------------------------------------------------------------------

def test_bars_per_year_daily_equity_recovers_252():
    # The familiar constant must fall out of the calendar convention rather
    # than being hardcoded anywhere in the estimators.
    assert V.bars_per_year("1d", V.US_EQUITY) == pytest.approx(252.0)


def test_bars_per_year_varies_with_timeframe_and_calendar():
    assert V.bars_per_year("1h", V.CONTINUOUS) == pytest.approx(365 * 24)
    assert V.bars_per_year("1m", V.CONTINUOUS) == pytest.approx(365 * 24 * 60)
    assert V.bars_per_year("5m", V.CONTINUOUS) == pytest.approx(365 * 24 * 12)
    assert V.bars_per_year("1h", V.US_EQUITY) == pytest.approx(252 * 6.5)
    assert V.bars_per_year("1d", V.CONTINUOUS) == pytest.approx(365.0)


def test_annualisation_factor_is_sqrt_of_bars_per_year():
    for tf in ("1m", "15m", "1h", "1d"):
        for cal in (V.CONTINUOUS, V.US_EQUITY, V.US_FUTURES):
            assert V.annualisation_factor(tf, cal) == pytest.approx(
                math.sqrt(V.bars_per_year(tf, cal)))


def test_hourly_crypto_vs_equity_annualisation_differ_materially():
    # The point of deriving the factor: mis-annualising 1h crypto on the equity
    # calendar rescales every vol-targeted position by this ratio.
    ratio = (V.annualisation_factor("1h", V.CONTINUOUS)
             / V.annualisation_factor("1h", V.US_EQUITY))
    assert ratio > 2.0


def test_calendar_rejects_nonsense():
    with pytest.raises(ConfigurationError):
        V.Calendar(0.0, 86400.0)
    with pytest.raises(ConfigurationError):
        V.Calendar(365.0, -1.0)


# ---------------------------------------------------------------------------
# close-to-close realised volatility
# ---------------------------------------------------------------------------

def test_log_returns_hand_computed():
    got = V.log_returns([100.0, 110.0, 99.0])
    assert got == pytest.approx([math.log(1.1), math.log(99 / 110)])


def test_realised_volatility_matches_hand_computation():
    closes = [100.0, 101.0, 100.0, 102.0, 101.0, 103.0]
    window = 3
    got = V.realised_volatility(closes, window)
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    for i in range(window, len(closes)):
        expected = statistics.stdev(rets[i - window:i])
        assert got[i] == pytest.approx(expected)


def test_realised_volatility_is_aligned_and_padded_with_none():
    closes = [100.0 + i for i in range(10)]
    got = V.realised_volatility(closes, 4)
    assert len(got) == len(closes)
    assert got[:4] == [None] * 4
    assert all(v is not None for v in got[4:])


def test_realised_volatility_annualisation_is_a_plain_multiplier():
    closes = [100.0, 101.0, 100.5, 102.0, 101.0, 100.0]
    base = V.realised_volatility(closes, 3)
    factor = V.annualisation_factor("1d", V.US_EQUITY)
    scaled = V.realised_volatility(closes, 3, factor)
    for a, b in zip(base, scaled):
        if a is None:
            assert b is None
        else:
            assert b == pytest.approx(a * factor)


def test_realised_volatility_of_a_constant_series_is_zero():
    assert V.realised_volatility([50.0] * 10, 5)[-1] == pytest.approx(0.0)


def test_realised_volatility_rejects_bad_input():
    with pytest.raises(ConfigurationError):
        V.realised_volatility([1.0, 2.0, 3.0], 1)          # window < 2
    with pytest.raises(ConfigurationError):
        V.realised_volatility([1.0, 0.0, 3.0], 2)          # log of zero
    with pytest.raises(ConfigurationError):
        V.realised_volatility([1.0, float("nan")], 2)


# ---------------------------------------------------------------------------
# range estimators
# ---------------------------------------------------------------------------

def test_parkinson_matches_the_published_formula():
    highs = [110.0, 105.0, 120.0]
    lows = [100.0, 100.0, 100.0]
    got = V.parkinson(highs, lows, 2)
    assert got[0] is None
    coefficient = 1.0 / (4.0 * math.log(2.0))
    expected = math.sqrt(coefficient * (math.log(1.05) ** 2 + math.log(1.2) ** 2) / 2)
    assert got[2] == pytest.approx(expected)


def test_parkinson_rejects_mismatched_lengths():
    with pytest.raises(ConfigurationError):
        V.parkinson([1.0, 2.0], [1.0], 1)


def test_garman_klass_matches_the_published_formula():
    candles = make_candles([(100.0, 110.0, 95.0, 105.0)])
    got = V.garman_klass(candles, 1)
    k = 2.0 * math.log(2.0) - 1.0
    variance = 0.5 * math.log(110 / 95) ** 2 - k * math.log(105 / 100) ** 2
    assert got[0] == pytest.approx(math.sqrt(variance))


def test_garman_klass_stays_non_negative_on_a_full_body_bar():
    # The worst case for the subtracted term: the body fills the whole range.
    # The estimator must still be real and finite, never a NaN that would
    # propagate silently into a position size.
    for rows in ([(100.0, 100.5, 100.0, 100.5)],      # marubozu up
                 [(100.5, 100.5, 100.0, 100.0)],      # marubozu down
                 [(100.0, 100.0, 100.0, 100.0)]):     # doji, zero range
        got = V.garman_klass(make_candles(rows), 1)[0]
        assert got is not None and got >= 0.0 and math.isfinite(got)


def test_rogers_satchell_matches_the_published_formula():
    candles = make_candles([(100.0, 112.0, 96.0, 108.0)])
    got = V.rogers_satchell(candles, 1)
    variance = (math.log(112 / 108) * math.log(112 / 100)
                + math.log(96 / 108) * math.log(96 / 100))
    assert got[0] == pytest.approx(math.sqrt(variance))


def test_rogers_satchell_is_drift_blind_where_parkinson_is_not():
    # Every bar opens at its low and closes at its high: pure drift, no
    # intrabar reversal. RS is designed to report ~0 for that; Parkinson counts
    # the drift as volatility. This is the documented assumption difference.
    rows = []
    price = 100.0
    for _ in range(20):
        nxt = price * 1.01
        rows.append((price, nxt, price, nxt))
        price = nxt
    candles = make_candles(rows)
    assert V.rogers_satchell(candles, 10)[-1] == pytest.approx(0.0, abs=1e-12)
    assert V.parkinson([c.high for c in candles], [c.low for c in candles], 10)[-1] > 0.0


def test_yang_zhang_needs_one_extra_bar_and_sees_gaps():
    # Tiny intraday ranges, large overnight gaps: the component every other
    # estimator here throws away.
    rows = []
    price = 100.0
    for i in range(30):
        gap = 1.05 if i % 2 else 0.96
        o = price * gap
        c = o * 1.0005
        rows.append((o, max(o, c) * 1.0001, min(o, c) * 0.9999, c))
        price = c
    candles = make_candles(rows)
    window = 10
    yz = V.yang_zhang(candles, window)
    assert yz[:window] == [None] * window          # one more None than the rest
    assert V.rogers_satchell(candles, window)[window] is not None
    assert yz[-1] > V.rogers_satchell(candles, window)[-1] * 5


def test_yang_zhang_is_positive_and_finite_on_ordinary_data():
    candles = from_closes([100.0 + 3 * math.sin(i * 0.7) for i in range(60)])
    got = V.yang_zhang(candles, 20)
    assert got[-1] is not None and got[-1] > 0 and math.isfinite(got[-1])


def test_estimators_reject_non_candles():
    with pytest.raises(ConfigurationError):
        V.garman_klass([{"open": 1}], 1)


# ---------------------------------------------------------------------------
# EWMA
# ---------------------------------------------------------------------------

def test_ewma_volatility_matches_the_recursion():
    rets = [0.01, -0.02, 0.005, 0.03]
    lam = 0.9
    got = V.ewma_volatility(rets, lam)
    var = rets[0] ** 2
    expected = []
    for r in rets:
        var = lam * var + (1 - lam) * r * r
        expected.append(math.sqrt(var))
    assert got == pytest.approx(expected)


def test_ewma_volatility_decays_after_a_shock():
    # One large return then calm: the estimate must fall monotonically, which
    # is exactly the "no ghost feature" property a rolling window lacks.
    rets = [0.10] + [0.0] * 20
    got = V.ewma_volatility(rets, 0.94)
    assert all(got[i] >= got[i + 1] for i in range(1, len(got) - 1))
    assert got[-1] < got[1]


def test_ewma_volatility_edge_cases():
    assert V.ewma_volatility([]) == []
    with pytest.raises(ConfigurationError):
        V.ewma_volatility([0.01], lam=1.0)
    with pytest.raises(ConfigurationError):
        V.ewma_volatility([0.01], lam=0.0)
    with pytest.raises(ConfigurationError):
        V.ewma_volatility([0.01], seed=-1.0)


# ---------------------------------------------------------------------------
# true range / ATR
# ---------------------------------------------------------------------------

def test_true_range_includes_the_gap():
    candles = make_candles([(100.0, 101.0, 99.0, 100.0),
                            (110.0, 112.0, 109.0, 111.0)])
    tr = V.true_range(candles)
    assert tr[0] is None
    # 112 - 100 (prev close) beats the bar's own 3-point range.
    assert tr[1] == pytest.approx(12.0)


def test_average_true_range_uses_wilder_smoothing():
    rows = [(100.0 + i, 101.0 + i, 99.0 + i, 100.0 + i) for i in range(10)]
    candles = make_candles(rows)
    window = 3
    got = V.average_true_range(candles, window)
    tr = V.true_range(candles)
    first = sum(tr[1:window + 1]) / window
    assert got[:window] == [None] * window
    assert got[window] == pytest.approx(first)
    expected = first + (tr[window + 1] - first) / window
    assert got[window + 1] == pytest.approx(expected)


def test_average_true_range_returns_all_none_when_too_short():
    candles = make_candles([(100.0, 101.0, 99.0, 100.0)] * 3)
    assert V.average_true_range(candles, 14) == [None] * 3


# ---------------------------------------------------------------------------
# percentile rank
# ---------------------------------------------------------------------------

def test_percentile_rank_uses_midrank_on_ties():
    assert V.percentile_rank(5.0, [5.0] * 10) == pytest.approx(0.5)
    assert V.percentile_rank(9.0, [1.0, 2.0, 3.0]) == pytest.approx(1.0)
    assert V.percentile_rank(0.0, [1.0, 2.0, 3.0]) == pytest.approx(0.0)
    assert V.percentile_rank(2.0, [1.0, 3.0]) == pytest.approx(0.5)
    assert V.percentile_rank(1.0, []) == 0.0


# ---------------------------------------------------------------------------
# analyser
# ---------------------------------------------------------------------------

def test_analyzer_requires_a_warm_up():
    analyzer = V.VolatilityAnalyzer(lookback=100, window=20)
    candles = from_closes([100.0 + i * 0.1 for i in range(10)])
    with pytest.raises(InsufficientDataError):
        analyzer.analyse(candles)


def test_analyzer_rejects_a_lookback_that_leaves_no_history():
    with pytest.raises(ConfigurationError):
        V.VolatilityAnalyzer(lookback=10, window=20)


def test_analyzer_flags_a_volatility_explosion_as_extreme():
    calm = [100.0 * (1 + 0.001 * math.sin(i * 0.4)) for i in range(150)]
    spiky = list(calm)
    price = calm[-1]
    for i in range(25):
        price *= 1 + (0.05 if i % 2 else -0.05)
        spiky.append(price)
    analyzer = V.VolatilityAnalyzer(lookback=100, window=20)
    state = analyzer.analyse(from_closes(spiky))
    assert state.regime_label == "extreme"
    assert state.percentile_rank > 0.9
    assert state.realised > 0
    assert state.atr_pct > 0
    assert state.timeframe == "1h"
    assert state.annualisation == pytest.approx(
        V.annualisation_factor("1h", V.CONTINUOUS))
    assert state.ts == from_closes(spiky)[-1].ts_close


def test_analyzer_labels_a_steady_series_as_normal_or_low():
    closes = [100.0 * (1 + 0.004 * math.sin(i * 0.35)) for i in range(200)]
    state = V.VolatilityAnalyzer(lookback=100, window=20).analyse(from_closes(closes))
    assert state.regime_label in ("low", "normal", "elevated")
    assert 0.0 <= state.percentile_rank <= 1.0


def test_analyzer_is_deterministic():
    closes = [100.0 * (1 + 0.01 * math.sin(i * 0.21)) for i in range(200)]
    candles = from_closes(closes)
    analyzer = V.VolatilityAnalyzer(lookback=100, window=20)
    assert analyzer.analyse(candles).to_dict() == analyzer.analyse(candles).to_dict()


def test_volatility_state_serialises_and_validates():
    closes = [100.0 * (1 + 0.01 * math.sin(i * 0.21)) for i in range(200)]
    state = V.VolatilityAnalyzer(lookback=100, window=20).analyse(from_closes(closes))
    import json
    payload = json.dumps(state.to_dict())
    assert json.loads(payload)["instrument_key"] == KEY
    with pytest.raises(ConfigurationError):
        V.VolatilityState(ts=START, instrument_key=KEY, realised=0.1, ewma=0.1,
                          percentile_rank=1.5, regime_label="low", atr_pct=0.01)


def test_volatility_bands_labels_and_validation():
    bands = V.VolatilityBands()
    assert bands.label(0.0) == "low"
    assert bands.label(0.5) == "normal"
    assert bands.label(0.8) == "elevated"
    assert bands.label(0.99) == "extreme"
    with pytest.raises(ConfigurationError):
        V.VolatilityBands(low=0.8, elevated=0.5, extreme=0.95)


# ---------------------------------------------------------------------------
# volatility-targeted sizing
# ---------------------------------------------------------------------------

def test_vol_target_position_size_returns_decimal_and_scales_inversely():
    size = V.vol_target_position_size(0.20, 0.40, Decimal("10000"), Decimal("100"),
                                      max_leverage=2.0)
    assert isinstance(size, Decimal)
    # 10000 * (0.20/0.40) = 5000 notional / 100 = 50 units
    assert size == pytest.approx(Decimal("50"))
    half_vol = V.vol_target_position_size(0.20, 0.20, Decimal("10000"),
                                          Decimal("100"), max_leverage=2.0)
    assert half_vol == pytest.approx(Decimal("100"))


def test_vol_target_position_size_respects_the_leverage_cap():
    size = V.vol_target_position_size(0.50, 0.05, Decimal("10000"), Decimal("100"),
                                      max_leverage=1.0)
    assert size == pytest.approx(Decimal("100"))     # capped at 1x equity


def test_vol_target_position_size_handles_zero_volatility_safely():
    # Zero vol means the estimator is degenerate; refuse to size rather than
    # dividing by zero or silently taking the leverage cap.
    assert V.vol_target_position_size(0.2, 0.0, 10000, 100) == Decimal(0)
    assert V.vol_target_position_size(0.2, -1.0, 10000, 100) == Decimal(0)
    assert V.vol_target_position_size(0.2, float("nan"), 10000, 100) == Decimal(0)
    assert V.vol_target_position_size(0.2, float("inf"), 10000, 100) == Decimal(0)


def test_vol_target_position_size_handles_empty_account():
    assert V.vol_target_position_size(0.2, 0.3, 0, 100) == Decimal(0)
    assert V.vol_target_position_size(0.2, 0.3, -50, 100) == Decimal(0)
    assert V.vol_target_position_size(0.2, 0.3, 10000, 0) == Decimal(0)
    assert V.vol_target_position_size(0.0, 0.3, 10000, 100) == Decimal(0)


def test_vol_target_position_size_rejects_configuration_errors():
    with pytest.raises(ConfigurationError):
        V.vol_target_position_size(-0.1, 0.2, 10000, 100)
    with pytest.raises(ConfigurationError):
        V.vol_target_position_size(0.1, 0.2, 10000, 100, max_leverage=-1.0)


def test_vol_target_position_size_quantises_to_the_lot_grid():
    instrument = Instrument(symbol="AAPL", exchange="NASDAQ", lot_size=Decimal("1"))
    size = V.vol_target_position_size(0.20, 0.37, Decimal("10000"), Decimal("193.44"),
                                      max_leverage=1.0, instrument=instrument)
    assert isinstance(size, Decimal)
    assert size == size.to_integral_value()          # whole shares only
    raw = V.vol_target_position_size(0.20, 0.37, Decimal("10000"), Decimal("193.44"),
                                     max_leverage=1.0)
    assert size <= raw                               # quantisation rounds DOWN


def test_vol_target_position_size_never_routes_through_binary_float():
    # Decimal(0.1) is not 0.1; the sizing path must stringify instead.
    size = V.vol_target_position_size(0.1, 0.1, "1000.10", "1")
    assert size == Decimal("1000.10")


def test_analyzer_state_feeds_sizing_end_to_end():
    closes = [100.0 * (1 + 0.01 * math.sin(i * 0.21)) for i in range(200)]
    state = V.VolatilityAnalyzer(lookback=100, window=20).analyse(from_closes(closes))
    size = V.vol_target_position_size(0.25, state.realised, Decimal("50000"),
                                      Decimal(repr(closes[-1])), max_leverage=3.0)
    assert isinstance(size, Decimal)
    assert size > 0

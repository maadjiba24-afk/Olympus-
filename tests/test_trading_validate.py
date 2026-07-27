"""Tests for the market-data quality gate.

The through-line of these tests is that validation must never make data *look*
better than it is: gaps stay holes, outliers stay visible, and a bar that could
not have been known yet is an error rather than a warning.
"""

from datetime import datetime, timedelta, timezone

import pytest

from olympus.trading.clock import FixedClock
from olympus.trading.contracts import Candle, DataQuality, Instrument
from olympus.trading.errors import (
    ConfigurationError,
    DataValidationError,
    InsufficientDataError,
    StaleDataError,
)
from olympus.trading.validate import (
    assert_no_lookahead,
    find_gaps,
    floor_to_timeframe,
    is_on_grid,
    normalise_candles,
    parse_timeframe,
    require_usable,
    validate_raw,
)

KEY = "SIM:BTCUSD"
T0 = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc)   # a Monday


def bar(minute: int, *, close: float = 100.0, key: str = KEY, tf: str = "1m",
        is_final: bool = True, high: float | None = None,
        low: float | None = None) -> Candle:
    ts_open = T0 + timedelta(minutes=minute)
    return Candle(
        instrument_key=key, timeframe=tf, ts_open=ts_open,
        ts_close=ts_open + parse_timeframe(tf), open=close, high=high or close + 1,
        low=low or close - 1, close=close, volume=10.0, amount=close * 10.0,
        is_final=is_final, source="test")


def clock_at(minute: float) -> FixedClock:
    return FixedClock(T0 + timedelta(minutes=minute))


# --- timeframe arithmetic --------------------------------------------------

def test_parse_timeframe_units():
    assert parse_timeframe("1m") == timedelta(minutes=1)
    assert parse_timeframe("15m") == timedelta(minutes=15)
    assert parse_timeframe("4h") == timedelta(hours=4)
    assert parse_timeframe("1d") == timedelta(days=1)
    assert parse_timeframe("1w") == timedelta(days=7)


def test_parse_timeframe_rejects_garbage():
    for bad in ("", "fortnight", "0m", "-5m", "5x"):
        with pytest.raises(ConfigurationError):
            parse_timeframe(bad)


def test_floor_to_timeframe_is_epoch_anchored():
    ts = datetime(2026, 3, 2, 12, 34, 56, tzinfo=timezone.utc)
    assert floor_to_timeframe(ts, "1m") == ts.replace(second=0, microsecond=0)
    assert floor_to_timeframe(ts, "5m") == ts.replace(minute=30, second=0, microsecond=0)
    assert floor_to_timeframe(ts, "1h") == ts.replace(minute=0, second=0, microsecond=0)
    assert floor_to_timeframe(ts, "1d") == datetime(2026, 3, 2, tzinfo=timezone.utc)
    # Weekly buckets anchor on Monday, not on the Thursday of the unix epoch.
    assert floor_to_timeframe(ts, "1w") == datetime(2026, 3, 2, tzinfo=timezone.utc)
    assert floor_to_timeframe(datetime(2026, 3, 8, 23, 59, tzinfo=timezone.utc),
                              "1w") == datetime(2026, 3, 2, tzinfo=timezone.utc)


def test_is_on_grid():
    assert is_on_grid(T0, "5m")
    assert not is_on_grid(T0 + timedelta(minutes=1), "5m")


# --- normalisation ---------------------------------------------------------

def test_clean_window_is_ok():
    candles = [bar(i) for i in range(10)]
    clean, report = normalise_candles(candles, timeframe="1m", clock=clock_at(10))
    assert [c.ts_open for c in clean] == [b.ts_open for b in candles]
    assert report.status is DataQuality.OK
    assert report.gaps == 0
    assert report.bars_expected == 10
    assert report.completeness == 1.0
    assert report.instrument_key == KEY


def test_gaps_are_counted_and_never_filled():
    candles = [bar(i) for i in (0, 1, 2, 6, 7, 8, 9)]        # minutes 3,4,5 missing
    clean, report = normalise_candles(candles, timeframe="1m", clock=clock_at(10),
                                      min_completeness=0.5)
    assert len(clean) == 7                                    # nothing invented
    assert report.gaps == 3
    assert report.bars_expected == 10
    assert report.status is DataQuality.DEGRADED
    missing = {T0 + timedelta(minutes=m) for m in (3, 4, 5)}
    assert missing.isdisjoint({c.ts_open for c in clean})


def test_a_single_gap_in_a_long_window_degrades_but_stays_usable():
    candles = [bar(i) for i in range(60) if i != 30]
    clean, report = normalise_candles(candles, timeframe="1m", clock=clock_at(70))
    assert report.gaps == 1
    assert len(clean) == 59
    assert report.status is DataQuality.DEGRADED
    assert require_usable(report) is report


def test_find_gaps_reports_ranges():
    candles = [bar(i) for i in (0, 1, 5)]
    assert find_gaps(candles, "1m") == [
        (T0 + timedelta(minutes=2), T0 + timedelta(minutes=5))]


def test_dedupe_keeps_last_occurrence():
    first = bar(3, close=100.0)
    correction = bar(3, close=250.0)                            # feed re-sent it
    clean, report = normalise_candles([bar(0), bar(1), bar(2), first, correction],
                                      timeframe="1m", clock=clock_at(10))
    assert report.duplicates_removed == 1
    assert len(clean) == 4
    assert clean[-1].close == 250.0


def test_out_of_order_is_sorted_not_dropped():
    scrambled = [bar(2), bar(0), bar(3), bar(1)]
    clean, report = normalise_candles(scrambled, timeframe="1m", clock=clock_at(10))
    assert [c.ts_open for c in clean] == [T0 + timedelta(minutes=i) for i in range(4)]
    assert report.bars_present == 4                             # nothing lost
    assert report.out_of_order_removed == 2
    assert report.status is DataQuality.DEGRADED


def test_non_final_and_foreign_bars_are_dropped():
    candles = [bar(0), bar(1, is_final=False), bar(2, key="SIM:ETHUSD"),
               bar(3, tf="5m")]
    clean, report = normalise_candles(candles, timeframe="1m", clock=clock_at(10),
                                      instrument=KEY)
    assert [c.ts_open for c in clean] == [T0]
    assert report.invalid_dropped == 3
    assert report.bars_present == 1


def test_instrument_may_be_an_instrument_object():
    inst = Instrument(symbol="BTCUSD", exchange="SIM")
    clean, report = normalise_candles([bar(i) for i in range(5)], timeframe="1m",
                                      clock=clock_at(5), instrument=inst)
    assert report.instrument_key == "SIM:BTCUSD"
    assert len(clean) == 5


def test_future_bars_are_refused():
    # A bar closing after the clock cannot be known; keeping it is look-ahead.
    candles = [bar(i) for i in range(5)]
    clean, report = normalise_candles(candles, timeframe="1m", clock=clock_at(3))
    assert len(clean) == 3
    assert report.invalid_dropped == 2
    assert any("closing after" in w for w in report.warnings)


def test_off_grid_bars_are_dropped_with_a_warning():
    odd_open = T0 + timedelta(minutes=2, seconds=17)
    odd = Candle(instrument_key=KEY, timeframe="1m", ts_open=odd_open,
                 ts_close=odd_open + timedelta(minutes=1), open=100, high=101,
                 low=99, close=100)
    clean, report = normalise_candles([bar(0), bar(1), odd, bar(3)],
                                      timeframe="1m", clock=clock_at(10),
                                      expected_start=T0,
                                      expected_end=T0 + timedelta(minutes=3))
    assert odd not in clean
    assert any("off the 1m grid" in w for w in report.warnings)
    assert report.gaps == 1                                     # minute 2 missing


# --- staleness -------------------------------------------------------------

def test_staleness_boundary_is_inclusive_of_the_tolerance():
    candles = [bar(i) for i in range(5)]                  # newest closes at T0+5m
    at_limit = normalise_candles(candles, timeframe="1m", clock=clock_at(8),
                                 max_age=timedelta(minutes=3))[1]
    assert at_limit.age_seconds == pytest.approx(180.0)
    assert at_limit.status is not DataQuality.UNUSABLE     # exactly at tolerance

    beyond = normalise_candles(candles, timeframe="1m", clock=clock_at(8.1),
                               max_age=timedelta(minutes=3))[1]
    assert beyond.status is DataQuality.UNUSABLE
    assert any(e.startswith("DATA_STALE") for e in beyond.errors)


def test_max_age_accepts_plain_seconds():
    candles = [bar(i) for i in range(5)]
    report = normalise_candles(candles, timeframe="1m", clock=clock_at(20),
                               max_age=60)[1]
    assert report.status is DataQuality.UNUSABLE


def test_require_usable_raises_stale_for_stale_data():
    candles = [bar(i) for i in range(5)]
    report = normalise_candles(candles, timeframe="1m", clock=clock_at(60),
                               max_age=timedelta(minutes=1))[1]
    with pytest.raises(StaleDataError) as excinfo:
        require_usable(report)
    assert excinfo.value.code == "DATA_STALE"
    assert excinfo.value.details["instrument"] == KEY


# --- completeness ----------------------------------------------------------

def test_completeness_threshold_makes_a_sparse_window_unusable():
    candles = [bar(i) for i in range(0, 20, 2)]           # every other minute
    clean, report = normalise_candles(candles, timeframe="1m", clock=clock_at(30))
    assert report.completeness == pytest.approx(10 / 19)
    assert report.status is DataQuality.UNUSABLE
    with pytest.raises(InsufficientDataError):
        require_usable(report)
    assert len(clean) == 10                               # data still returned


def test_completeness_threshold_is_configurable():
    candles = [bar(i) for i in range(0, 20, 2)]
    report = normalise_candles(candles, timeframe="1m", clock=clock_at(30),
                               min_completeness=0.5)[1]
    assert report.status is DataQuality.DEGRADED
    assert require_usable(report) is report


def test_expected_window_drives_bars_expected():
    candles = [bar(i) for i in range(5)]
    report = normalise_candles(
        candles, timeframe="1m", clock=clock_at(30), expected_start=T0,
        expected_end=T0 + timedelta(minutes=9))[1]
    assert report.bars_expected == 10
    assert report.completeness == 0.5
    assert report.status is DataQuality.UNUSABLE


def test_empty_window_is_unusable():
    clean, report = normalise_candles([], timeframe="1m", clock=clock_at(1))
    assert clean == []
    assert report.status is DataQuality.UNUSABLE
    assert any(e.startswith("DATA_EMPTY") for e in report.errors)
    with pytest.raises(InsufficientDataError):
        require_usable(report)


def test_require_usable_min_bars_and_degraded_policy():
    candles = [bar(i) for i in range(10)]
    report = normalise_candles(candles, timeframe="1m", clock=clock_at(10))[1]
    with pytest.raises(InsufficientDataError):
        require_usable(report, min_bars=64)

    degraded = normalise_candles([bar(i) for i in (0, 1, 2, 3, 5, 6, 7, 8, 9, 10,
                                                   11, 12, 13, 14, 15, 16, 17, 18,
                                                   19, 20, 21)],
                                 timeframe="1m", clock=clock_at(30))[1]
    assert degraded.status is DataQuality.DEGRADED
    with pytest.raises(DataValidationError):
        require_usable(degraded, allow_degraded=False)


# --- outliers --------------------------------------------------------------

def test_outlier_is_flagged_but_never_dropped():
    candles = [bar(i, close=100.0 + (i % 2) * 0.1) for i in range(30)]
    candles.append(bar(30, close=100000.0, high=100001.0, low=99999.0))
    clean, report = normalise_candles(candles, timeframe="1m", clock=clock_at(40),
                                      outlier_sigma=10.0)
    assert len(clean) == 31                              # the bad bar survives
    assert clean[-1].close == 100000.0
    assert any("outlier close" in w for w in report.warnings)
    assert report.status is DataQuality.DEGRADED


def test_ordinary_volatility_is_not_flagged_as_an_outlier():
    # A deterministic wiggle of a few tenths of a percent: normal market noise
    # must survive the detector, or every real move becomes "an outlier".
    deltas = [0.5, -0.3, 0.2, -0.4, 0.6, -0.5, 0.1, -0.2]
    price, prices = 100.0, []
    for i in range(40):
        price += deltas[i % len(deltas)]
        prices.append(price)
    candles = [bar(i, close=p) for i, p in enumerate(prices)]
    report = normalise_candles(candles, timeframe="1m", clock=clock_at(50))[1]
    assert not any("outlier" in w for w in report.warnings)
    assert report.status is DataQuality.OK


# --- look-ahead ------------------------------------------------------------

def test_assert_no_lookahead_catches_a_future_bar():
    candles = [bar(i) for i in range(5)]
    as_of = T0 + timedelta(minutes=4)          # bar 4 closes at T0+5m
    with pytest.raises(DataValidationError) as excinfo:
        assert_no_lookahead(candles, as_of)
    assert "look-ahead" in str(excinfo.value)
    assert excinfo.value.details["ts_close"] == (T0 + timedelta(minutes=5)).isoformat()


def test_assert_no_lookahead_allows_bars_closing_exactly_at_as_of():
    candles = [bar(i) for i in range(5)]
    assert assert_no_lookahead(candles, T0 + timedelta(minutes=5)) is None


def test_assert_no_lookahead_rejects_the_forming_bar():
    with pytest.raises(DataValidationError):
        assert_no_lookahead([bar(0, is_final=False)], T0 + timedelta(seconds=30))


def test_assert_no_lookahead_requires_aware_as_of():
    with pytest.raises(DataValidationError):
        assert_no_lookahead([bar(0)], datetime(2026, 3, 2, 13, 0))


# --- raw parsing -----------------------------------------------------------

def test_validate_raw_parses_common_feed_shapes():
    rows = [
        {"timestamp": int((T0 + timedelta(minutes=i)).timestamp()),
         "o": 100 + i, "h": 101 + i, "l": 99 + i, "c": 100.5 + i, "v": 5}
        for i in range(5)
    ]
    clean, report = validate_raw(rows, timeframe="1m", clock=clock_at(10),
                                 instrument=KEY, source="feed")
    assert len(clean) == 5
    assert report.status is DataQuality.OK
    assert clean[0].ts_open == T0
    assert clean[0].source == "feed"
    assert clean[2].close == 102.5


def test_validate_raw_accepts_millisecond_and_iso_timestamps():
    rows = [
        {"open_time": int((T0).timestamp()) * 1000,
         "open": 10, "high": 11, "low": 9, "close": 10},
        {"open_time": (T0 + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
         "open": 10, "high": 11, "low": 9, "close": 10},
    ]
    clean, _ = validate_raw(rows, timeframe="1m", clock=clock_at(10), instrument=KEY)
    assert [c.ts_open for c in clean] == [T0, T0 + timedelta(minutes=1)]


def test_validate_raw_records_rejections_instead_of_raising():
    good = {"ts_open": T0, "open": 10, "high": 11, "low": 9, "close": 10}
    rows = [
        good,
        {"ts_open": T0 + timedelta(minutes=1), "open": 10, "high": 5,   # high < low
         "low": 9, "close": 10},
        {"open": 10, "high": 11, "low": 9, "close": 10},                # no timestamp
        {"ts_open": "not-a-date", "open": 10, "high": 11, "low": 9, "close": 10},
        {"ts_open": T0 + timedelta(minutes=4), "open": 10, "high": 11,  # negative px
         "low": -9, "close": 10},
        "totally not a row",
        {"ts_open": T0 + timedelta(minutes=5), "open": 10, "high": 11, "low": 9,
         "close": 10},
    ]
    clean, report = validate_raw(rows, timeframe="1m", clock=clock_at(10),
                                 instrument=KEY)
    assert len(clean) == 2
    assert report.invalid_dropped >= 5
    assert sum(1 for w in report.warnings if "rejected" in w) >= 5
    assert report.status is DataQuality.DEGRADED or report.status is DataQuality.UNUSABLE


def test_validate_raw_caps_the_number_of_quoted_rejections():
    rows = [{"junk": i} for i in range(50)]
    _, report = validate_raw(rows, timeframe="1m", clock=clock_at(10),
                             instrument=KEY, max_reported_rejections=5)
    quoted = [w for w in report.warnings if w.startswith("row ")]
    assert len(quoted) == 5
    assert any("further rejected" in w for w in report.warnings)
    assert report.invalid_dropped == 50


def test_normalise_candles_refuses_raw_dicts():
    with pytest.raises(DataValidationError):
        normalise_candles([{"ts_open": T0}], timeframe="1m", clock=clock_at(1))


def test_report_is_json_serialisable():
    import json
    _, report = normalise_candles([bar(i) for i in range(3)], timeframe="1m",
                                  clock=clock_at(5))
    payload = json.dumps(report.to_dict())
    assert '"status": "ok"' in payload

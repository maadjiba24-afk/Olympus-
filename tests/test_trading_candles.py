"""Tests for tick aggregation and resampling.

Two properties dominate: a bar is published once its bucket has provably ended
and never mutated afterwards, and no bar is ever fabricated — not for an idle
bucket, and not for a target bucket whose source bars are incomplete.
"""

from datetime import datetime, timedelta, timezone

import pytest

from olympus.trading.candles import (
    CandleAggregator,
    CandleBuilder,
    Tick,
    candles_from_ticks,
    resample,
)
from olympus.trading.clock import FixedClock
from olympus.trading.contracts import Candle
from olympus.trading.errors import ConfigurationError, DataValidationError

KEY = "SIM:BTCUSD"
OTHER = "SIM:ETHUSD"
T0 = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc)


def tick(seconds: float, price: float, size: float = 1.0, key: str = KEY) -> Tick:
    return Tick(instrument_key=key, ts=T0 + timedelta(seconds=seconds),
                price=price, size=size)


def minute_bar(minute: int, *, close: float = 100.0, high: float | None = None,
               low: float | None = None, volume: float = 1.0,
               tf: str = "1m", is_final: bool = True, key: str = KEY) -> Candle:
    ts_open = T0 + timedelta(minutes=minute)
    step = {"1m": timedelta(minutes=1), "5m": timedelta(minutes=5)}[tf]
    return Candle(instrument_key=key, timeframe=tf, ts_open=ts_open,
                  ts_close=ts_open + step, open=close, high=high or close + 1,
                  low=low or close - 1, close=close, volume=volume,
                  amount=close * volume, is_final=is_final, source="test")


# --- Tick ------------------------------------------------------------------

def test_tick_requires_aware_time_and_positive_price():
    with pytest.raises(DataValidationError):
        Tick(instrument_key=KEY, ts=datetime(2026, 3, 2, 12, 0), price=1.0)
    with pytest.raises(DataValidationError):
        Tick(instrument_key=KEY, ts=T0, price=0.0)
    with pytest.raises(DataValidationError):
        Tick(instrument_key=KEY, ts=T0, price=1.0, size=-1.0)
    with pytest.raises(DataValidationError):
        Tick(instrument_key="", ts=T0, price=1.0)
    with pytest.raises(DataValidationError):
        Tick(instrument_key=KEY, ts=T0, price=float("nan"))


def test_tick_normalises_timezone_and_exposes_notional():
    t = Tick(instrument_key=KEY, ts=T0.astimezone(timezone(timedelta(hours=5))),
             price=10.0, size=3.0)
    assert t.ts == T0 and t.ts.tzinfo is timezone.utc
    assert t.notional == 30.0


# --- CandleBuilder ---------------------------------------------------------

def test_boundary_crossing_emits_exactly_one_finalised_candle():
    builder = CandleBuilder(KEY, "1m", FixedClock(T0))
    assert builder.on_tick(tick(0, 100.0, 2.0)) is None
    assert builder.on_tick(tick(10, 105.0, 1.0)) is None
    assert builder.on_tick(tick(30, 95.0, 3.0)) is None
    assert builder.on_tick(tick(59.9, 101.0, 1.0)) is None

    candle = builder.on_tick(tick(60, 102.0, 5.0))            # first tick of 12:01
    assert candle is not None
    assert candle.is_final
    assert candle.ts_open == T0
    assert candle.ts_close == T0 + timedelta(minutes=1)
    assert (candle.open, candle.high, candle.low, candle.close) == (100.0, 105.0,
                                                                    95.0, 101.0)
    assert candle.volume == 7.0
    assert candle.amount == pytest.approx(100 * 2 + 105 + 95 * 3 + 101)


def test_current_is_never_final_and_tracks_the_forming_bar():
    builder = CandleBuilder(KEY, "1m", FixedClock(T0))
    assert builder.current() is None
    builder.on_tick(tick(5, 100.0))
    builder.on_tick(tick(6, 110.0))
    forming = builder.current()
    assert forming is not None
    assert forming.is_final is False
    assert forming.high == 110.0
    assert forming.ts_close == T0 + timedelta(minutes=1)


def test_late_tick_is_counted_and_never_reopens_a_closed_bar():
    builder = CandleBuilder(KEY, "1m", FixedClock(T0))
    builder.on_tick(tick(10, 100.0))
    closed = builder.on_tick(tick(70, 200.0))                 # closes minute 0
    assert closed.close == 100.0

    assert builder.on_tick(tick(20, 999.0, 50.0)) is None     # belongs to minute 0
    assert builder.late_ticks == 1
    forming = builder.current()
    assert forming.high == 200.0                              # untouched by the late tick
    assert forming.volume == 1.0
    assert closed.close == 100.0                              # published bar is immutable


def test_multi_bucket_gap_produces_no_phantom_bars():
    builder = CandleBuilder(KEY, "1m", FixedClock(T0))
    builder.on_tick(tick(0, 100.0))
    emitted = builder.on_tick(tick(60 * 5 + 3, 120.0))        # five minutes later
    assert emitted.ts_open == T0                              # only the one real bar
    assert builder.current().ts_open == T0 + timedelta(minutes=5)
    # Minutes 1-4 had no trades and are simply absent; validate.py will report
    # them as gaps rather than anyone inventing flat bars for them.
    assert builder.on_tick(tick(60 * 6, 121.0)).ts_open == T0 + timedelta(minutes=5)


def test_flush_withholds_a_bucket_that_has_not_ended():
    clock = FixedClock(T0 + timedelta(seconds=30))
    builder = CandleBuilder(KEY, "1m", clock)
    builder.on_tick(tick(10, 100.0))
    assert builder.flush() is None                            # 12:00 bar still live
    assert builder.pending

    clock.advance(timedelta(seconds=31))                      # now 12:01:01
    candle = builder.flush()
    assert candle is not None and candle.is_final
    assert candle.ts_open == T0
    assert builder.flush() is None                            # only ever emitted once


def test_flush_force_publishes_the_partial_bucket_at_end_of_stream():
    builder = CandleBuilder(KEY, "1m", FixedClock(T0))
    builder.on_tick(tick(5, 100.0))
    assert builder.flush() is None
    candle = builder.flush(force=True)
    assert candle is not None and candle.close == 100.0
    assert not builder.pending


def test_flush_accepts_an_explicit_now():
    builder = CandleBuilder(KEY, "1m", FixedClock(T0))
    builder.on_tick(tick(5, 100.0))
    assert builder.flush(T0 + timedelta(seconds=59)) is None
    assert builder.flush(T0 + timedelta(minutes=1)) is not None


def test_builder_rejects_foreign_ticks_and_non_ticks():
    builder = CandleBuilder(KEY, "1m", FixedClock(T0))
    with pytest.raises(DataValidationError):
        builder.on_tick(tick(0, 100.0, key=OTHER))
    with pytest.raises(DataValidationError):
        builder.on_tick("not a tick")
    with pytest.raises(ConfigurationError):
        CandleBuilder("", "1m", FixedClock(T0))


def test_five_minute_buckets_align_to_the_absolute_grid():
    start = T0 + timedelta(minutes=2)                          # 12:02, mid-bucket
    builder = CandleBuilder(KEY, "5m", FixedClock(start))
    builder.on_tick(Tick(instrument_key=KEY, ts=start, price=100.0, size=1.0))
    assert builder.current().ts_open == T0                     # 12:00, not 12:02
    candle = builder.on_tick(Tick(instrument_key=KEY,
                                  ts=T0 + timedelta(minutes=5), price=101.0))
    assert candle.ts_open == T0
    assert candle.ts_close == T0 + timedelta(minutes=5)


# --- CandleAggregator ------------------------------------------------------

def test_aggregator_keeps_instruments_independent():
    aggregator = CandleAggregator("1m", FixedClock(T0))
    assert aggregator.on_tick(tick(0, 100.0)) == []
    assert aggregator.on_tick(tick(0, 50.0, key=OTHER)) == []
    assert aggregator.on_tick(tick(30, 110.0)) == []

    emitted = aggregator.on_tick(tick(61, 120.0))
    assert len(emitted) == 1
    assert emitted[0].instrument_key == KEY and emitted[0].high == 110.0
    assert aggregator.current(OTHER).close == 50.0             # untouched
    assert aggregator.instruments == sorted([KEY, OTHER])


def test_aggregator_flush_is_deterministically_ordered():
    clock = FixedClock(T0)
    aggregator = CandleAggregator("1m", clock)
    aggregator.on_ticks([tick(1, 100.0), tick(2, 50.0, key=OTHER)])
    clock.advance(timedelta(minutes=2))
    flushed = aggregator.flush()
    assert [c.instrument_key for c in flushed] == sorted([KEY, OTHER])
    assert aggregator.flush() == []


def test_aggregator_counts_late_ticks_across_instruments():
    aggregator = CandleAggregator("1m", FixedClock(T0))
    aggregator.on_ticks([tick(0, 100.0), tick(0, 50.0, key=OTHER)])
    aggregator.on_ticks([tick(61, 101.0), tick(61, 51.0, key=OTHER)])
    aggregator.on_ticks([tick(5, 1.0), tick(5, 2.0, key=OTHER)])
    assert aggregator.late_ticks == 2


def test_candles_from_ticks_batch_path():
    ticks = [tick(s, 100.0 + s) for s in (0, 30, 61, 95, 130)]
    bars = candles_from_ticks(ticks, "1m", clock=FixedClock(T0 + timedelta(minutes=9)))
    assert [b.ts_open for b in bars] == [T0, T0 + timedelta(minutes=1),
                                         T0 + timedelta(minutes=2)]
    assert all(b.is_final for b in bars)
    # Without force the trailing bucket is only published because the clock is
    # already past its close; a clock inside the bucket withholds it.
    live = candles_from_ticks(ticks, "1m", clock=FixedClock(T0 + timedelta(seconds=140)))
    assert [b.ts_open for b in live] == [T0, T0 + timedelta(minutes=1)]


# --- resample --------------------------------------------------------------

def test_resample_1m_to_5m_aggregates_ohlcv_correctly():
    closes = [100.0, 103.0, 99.0, 101.0, 102.0]
    candles = [minute_bar(i, close=c, high=c + 2, low=c - 2, volume=i + 1)
               for i, c in enumerate(closes)]
    out = resample(candles, "1m", "5m")
    assert len(out) == 1
    bar5 = out[0]
    assert bar5.timeframe == "5m"
    assert bar5.ts_open == T0 and bar5.ts_close == T0 + timedelta(minutes=5)
    assert bar5.open == candles[0].open
    assert bar5.close == 102.0
    assert bar5.high == max(c.high for c in candles)
    assert bar5.low == min(c.low for c in candles)
    assert bar5.volume == 15.0
    assert bar5.amount == pytest.approx(sum(c.amount for c in candles))
    assert bar5.is_final


def test_resample_withholds_a_partial_trailing_bucket():
    candles = [minute_bar(i, close=100.0 + i) for i in range(7)]   # 5 + 2
    out = resample(candles, "1m", "5m")
    assert [c.ts_open for c in out] == [T0]                       # 12:05 incomplete
    loose = resample(candles, "1m", "5m", allow_partial=True)
    assert [c.ts_open for c in loose] == [T0, T0 + timedelta(minutes=5)]
    assert loose[1].close == 106.0


def test_resample_withholds_a_bucket_with_an_internal_gap():
    candles = [minute_bar(i, close=100.0 + i) for i in range(10) if i != 3]
    out = resample(candles, "1m", "5m")
    # 12:00-12:05 is missing 12:03, so it is never published as a 5m bar; only
    # the complete 12:05 bucket survives.
    assert [c.ts_open for c in out] == [T0 + timedelta(minutes=5)]


def test_resample_sorts_its_input():
    candles = [minute_bar(i, close=100.0 + i) for i in (3, 0, 4, 1, 2)]
    out = resample(candles, "1m", "5m")
    assert out[0].open == minute_bar(0, close=100.0).open
    assert out[0].close == 104.0


def test_resample_rejects_unusable_input():
    with pytest.raises(ConfigurationError):
        resample([], "5m", "1m")                                  # finer target
    with pytest.raises(ConfigurationError):
        resample([], "1m", "1m")
    with pytest.raises(ConfigurationError):
        resample([minute_bar(i) for i in range(2)], "2m", "5m")   # not a multiple
    with pytest.raises(DataValidationError):
        resample([minute_bar(0), minute_bar(1, is_final=False)], "1m", "5m")
    with pytest.raises(DataValidationError):
        resample([minute_bar(0), minute_bar(0)], "1m", "5m")      # duplicate
    with pytest.raises(DataValidationError):
        resample([minute_bar(0), minute_bar(1, key=OTHER)], "1m", "5m")
    with pytest.raises(DataValidationError):
        resample([minute_bar(0, tf="5m")], "1m", "5m")            # wrong source tf


def test_resample_of_builder_output_matches_direct_five_minute_building():
    ticks = [tick(s, 100.0 + (s % 7)) for s in range(0, 600, 13)]
    one_minute = candles_from_ticks(ticks, "1m",
                                    clock=FixedClock(T0 + timedelta(minutes=20)))
    five_direct = candles_from_ticks(ticks, "5m",
                                     clock=FixedClock(T0 + timedelta(minutes=20)))
    five_resampled = resample(one_minute, "1m", "5m")
    # Only buckets whose minutes were all traded can be compared: resample
    # withholds incomplete ones by design, direct building does not need to.
    direct = {c.ts_open: c for c in five_direct}
    assert five_resampled
    for candle in five_resampled:
        peer = direct[candle.ts_open]
        assert (candle.open, candle.high, candle.low, candle.close) == (
            peer.open, peer.high, peer.low, peer.close)
        assert candle.volume == pytest.approx(peer.volume)

"""Market-data ingestion.

Two kinds of test here, and the distinction matters for what may be claimed:

* **Pipeline tests** drive `IngestionService` through `ReplayProvider` and are
  exact — sequencing, gap detection, dedupe, reconnection, restart recovery and
  freshness are fully verified offline.
* **Provider-contract tests** feed `BinanceSpotProvider` the byte shapes
  Binance's REST and websocket APIs are documented to emit, and assert the
  parsing. They verify that *if* those bytes arrive we interpret them correctly.
  They do **not** verify that Binance actually sends them — no Binance host is
  reachable from this environment (HTTP 403 at CONNECT, org egress policy), so
  the adapter has never completed a real request. See docs/TRADING_STATUS.md.

The one thing every test here guards is the rule that ingestion never invents
data: a gap stays a gap, a late bar stays dropped, an unclosed bar is never
published.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from olympus.trading.clock import FixedClock
from olympus.trading.contracts import Candle, DataQuality
from olympus.trading.errors import (ConfigurationError, MarketDataError,
                                    StaleDataError)
from olympus.trading.ingest import (BinanceSpotProvider, IngestedCandle,
                                    IngestionConfig, IngestionService,
                                    ReplayProvider)
from olympus.trading.storage import CandleStore

T0 = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)
KEY = "BINANCE:BTCUSDT"
TF = "1m"


def bar(i, *, price=100.0, key=KEY, tf=TF):
    o = T0 + timedelta(minutes=i)
    return Candle(instrument_key=key, timeframe=tf, ts_open=o,
                  ts_close=o + timedelta(minutes=1), open=price, high=price + 1,
                  low=price - 1, close=price + 0.5, volume=10.0, amount=1000.0,
                  is_final=True, source="replay")


def rec(candle, *, received=None, sequence=None):
    return IngestedCandle(candle=candle,
                          received_at=received or candle.ts_close,
                          source="replay", sequence=sequence)


def service(records, *, now=None, max_age=300.0, drop_after=None):
    clock = FixedClock(now or (T0 + timedelta(minutes=30)))
    provider = ReplayProvider(records, clock=clock, drop_after=drop_after)
    return IngestionService(
        provider=provider, store=CandleStore(), clock=clock,
        config=IngestionConfig(timeframe=TF, max_age_s=max_age))


# --- the rule: never fabricate --------------------------------------------

def test_a_gap_is_counted_and_never_filled():
    # Clock just past the last bar, so staleness does not mask the gap — the
    # two conditions are independent and this test is about the gap.
    svc = service([rec(bar(i)) for i in (0, 1, 2, 5, 6)],    # 3 and 4 missing
                  now=T0 + timedelta(minutes=7, seconds=30))
    accepted = svc.backfill(KEY, TF, start=T0, end=T0 + timedelta(minutes=10))
    assert len(accepted) == 5, "the missing bars must not be invented"
    health = svc.health(KEY, TF)
    assert health.gaps_detected == 2
    assert health.quality is DataQuality.DEGRADED


def test_staleness_outranks_a_gap_when_both_apply():
    """A stale feed is unusable regardless of how complete it looks."""
    svc = service([rec(bar(i)) for i in (0, 1, 2, 5, 6)],
                  now=T0 + timedelta(hours=3))
    svc.backfill(KEY, TF, start=T0, end=T0 + timedelta(minutes=10))
    assert svc.health(KEY, TF).quality is DataQuality.UNUSABLE


def test_an_unclosed_bar_is_never_published():
    """Emitting a forming bar hands the strategy a close that has not happened."""
    forming = bar(3).with_final(False)
    svc = service([rec(bar(0)), rec(bar(1)), rec(forming)])
    accepted = svc.backfill(KEY, TF, start=T0, end=T0 + timedelta(minutes=10))
    assert [c.ts_open for c in accepted] == [bar(0).ts_open, bar(1).ts_open]


def test_duplicates_are_dropped_and_counted():
    svc = service([rec(bar(0)), rec(bar(1)), rec(bar(1)), rec(bar(2))])
    accepted = svc.backfill(KEY, TF, start=T0, end=T0 + timedelta(minutes=10))
    assert len(accepted) == 3
    assert svc.health(KEY, TF).duplicates_dropped == 1


def test_a_late_bar_never_rewrites_published_history():
    """A published bar is immutable; a late arrival is counted, not applied."""
    svc = service([rec(bar(0)), rec(bar(1)), rec(bar(2))])
    svc.backfill(KEY, TF, start=T0, end=T0 + timedelta(minutes=10))
    svc._accept(KEY, TF, [rec(bar(1, price=999.0))])
    assert svc.health(KEY, TF).out_of_order_dropped == 1
    stored = svc.store.get_candles(KEY, TF)
    assert all(c.open != 999.0 for c in stored)


def test_a_regressing_sequence_number_is_a_duplicate():
    svc = service([rec(bar(0), sequence=10), rec(bar(1), sequence=9),
                   rec(bar(2), sequence=11)])
    accepted = svc.backfill(KEY, TF, start=T0, end=T0 + timedelta(minutes=10))
    assert len(accepted) == 2
    assert svc.health(KEY, TF).duplicates_dropped == 1


def test_bars_for_another_series_are_ignored():
    other = bar(0, key="BINANCE:ETHUSDT")
    svc = service([rec(bar(0)), rec(other), rec(bar(1))])
    accepted = svc.backfill(KEY, TF, start=T0, end=T0 + timedelta(minutes=10))
    assert {c.instrument_key for c in accepted} == {KEY}


# --- persistence and restart ----------------------------------------------

def test_ingested_bars_are_persisted_and_readable():
    svc = service([rec(bar(i)) for i in range(5)])
    svc.backfill(KEY, TF, start=T0, end=T0 + timedelta(minutes=10))
    stored = CandleStore().get_candles(KEY, TF)
    assert len(stored) == 5
    assert stored[0].ts_open == T0


def test_state_survives_a_restart_and_resumes_from_the_right_point():
    """A restarted feed must not leave a silent hole where the downtime was."""
    svc = service([rec(bar(i)) for i in range(5)])
    svc.backfill(KEY, TF, start=T0, end=T0 + timedelta(minutes=10))
    before = svc.health(KEY, TF)

    fresh = service([])
    state = fresh.load_state(KEY, TF)
    assert state is not None
    assert fresh._stat(fresh._series_key(KEY, TF))["last_bar_close"] == \
        before.last_bar_close


def test_load_state_returns_none_for_an_unknown_series():
    assert service([]).load_state("BINANCE:NOPE", TF) is None


def test_raw_payloads_are_retained_when_configured():
    """Keeping the raw record is what makes a parsing bug recoverable."""
    from olympus import store
    record = IngestedCandle(candle=bar(0), received_at=bar(0).ts_close,
                            source="replay", raw={"open_time": 1})
    svc = service([])
    svc._accept(KEY, TF, [record])
    assert store.backend().keys("trading.ingest.raw")


# --- freshness drives abstention ------------------------------------------

def test_a_stale_feed_is_unusable_and_refuses_to_certify_itself():
    svc = service([rec(bar(i)) for i in range(3)],
                  now=T0 + timedelta(hours=5), max_age=300.0)
    svc.backfill(KEY, TF, start=T0, end=T0 + timedelta(minutes=10))
    health = svc.health(KEY, TF)
    assert health.quality is DataQuality.UNUSABLE
    assert health.age_s > 300
    with pytest.raises(StaleDataError):
        svc.require_fresh(KEY, TF)


def test_a_feed_that_has_never_produced_a_bar_is_unusable():
    svc = service([])
    assert svc.health(KEY, TF).quality is DataQuality.UNUSABLE
    with pytest.raises(StaleDataError):
        svc.require_fresh(KEY, TF)


def test_a_fresh_gapless_feed_is_ok():
    svc = service([rec(bar(i)) for i in range(3)],
                  now=T0 + timedelta(minutes=3, seconds=30))
    svc.backfill(KEY, TF, start=T0, end=T0 + timedelta(minutes=10))
    health = svc.require_fresh(KEY, TF)
    assert health.quality is DataQuality.OK
    assert health.gaps_detected == 0


# --- streaming, disconnection, reconnection -------------------------------

def test_a_disconnect_reconnects_and_backfills_the_gap():
    records = [rec(bar(i)) for i in range(6)]
    clock = FixedClock(T0 + timedelta(minutes=10))
    provider = ReplayProvider(records, clock=clock, drop_after=3)
    svc = IngestionService(provider=provider, store=CandleStore(), clock=clock,
                           config=IngestionConfig(timeframe=TF,
                                                  max_reconnects=2,
                                                  reconnect_backoff_s=0.0))
    with pytest.raises(MarketDataError):
        svc.run_stream(KEY, TF, sleeper=lambda _s: None)

    health = svc.health(KEY, TF)
    assert health.reconnects == 3            # budget of 2, then it gives up
    assert health.errors
    # Each reconnection closed the hole by REST before resuming, so every bar
    # the stream dropped was recovered. Resuming *without* that backfill is how
    # a feed silently loses exactly the window in which something happened.
    stored = svc.store.get_candles(KEY, TF)
    assert len(stored) == 6
    opens = [c.ts_open for c in stored]
    assert opens == sorted(opens) and len(set(opens)) == 6


def test_a_stream_that_ends_cleanly_does_not_count_as_a_reconnect():
    svc = service([rec(bar(i)) for i in range(4)])
    svc.run_stream(KEY, TF, sleeper=lambda _s: None)
    health = svc.health(KEY, TF)
    assert health.reconnects == 0
    assert health.messages == 4
    assert health.connected is False


def test_stop_ends_a_stream():
    stop = threading.Event()
    stop.set()
    svc = service([rec(bar(i)) for i in range(4)])
    svc.run_stream(KEY, TF, stop=stop, sleeper=lambda _s: None)
    assert svc.health(KEY, TF).messages == 0


def test_reconnect_budget_is_bounded():
    """An unbounded retry loop turns one outage into a rate-limit ban."""
    clock = FixedClock(T0)
    provider = ReplayProvider([rec(bar(0))], clock=clock, drop_after=0)
    svc = IngestionService(provider=provider, store=CandleStore(), clock=clock,
                           config=IngestionConfig(max_reconnects=3,
                                                  reconnect_backoff_s=0.0))
    with pytest.raises(MarketDataError) as excinfo:
        svc.run_stream(KEY, TF, sleeper=lambda _s: None)
    assert excinfo.value.details["reconnects"] == 4


# --- Binance provider contract (parsing only; never connected) ------------

#: A REST kline row in Binance's documented order. `closeTime` is the last
#: millisecond of the bar, which is the trap this fixture exists to catch.
_KLINE_ROW = [
    1767571200000,      # openTime  2026-01-05T00:00:00Z
    "42000.10", "42100.50", "41950.00", "42050.25",
    "12.5",             # volume
    1767571259999,      # closeTime = open + 60s - 1ms
    "525000.75",        # quote asset volume
    150,                # trades
    "6.1", "256000.10", "0",
]


def test_rest_kline_close_time_is_normalised_to_the_exclusive_close():
    """Binance closes a bar at open+interval-1ms. Using that verbatim makes every
    bar a millisecond short and knocks it off the timeframe grid."""
    provider = BinanceSpotProvider(clock=FixedClock(T0))
    record = provider.parse_kline_row(_KLINE_ROW, KEY, "1m",
                                      received_at=T0 + timedelta(seconds=61))
    candle = record.candle
    assert candle.ts_open == datetime(2026, 1, 5, tzinfo=timezone.utc)
    assert candle.ts_close == candle.ts_open + timedelta(minutes=1)
    assert (candle.ts_close - candle.ts_open).total_seconds() == 60.0


def test_rest_kline_fields_map_to_the_right_ohlcv():
    provider = BinanceSpotProvider(clock=FixedClock(T0))
    c = provider.parse_kline_row(_KLINE_ROW, KEY, "1m", received_at=T0).candle
    assert (c.open, c.high, c.low, c.close) == (42000.10, 42100.50, 41950.00, 42050.25)
    assert c.volume == 12.5
    assert c.amount == 525000.75          # quote volume, not base volume
    assert c.is_final is True


def test_ingested_record_measures_feed_latency():
    provider = BinanceSpotProvider(clock=FixedClock(T0))
    record = provider.parse_kline_row(
        _KLINE_ROW, KEY, "1m",
        received_at=datetime(2026, 1, 5, 0, 1, 5, tzinfo=timezone.utc))
    assert record.latency_s == pytest.approx(5.0), (
        "the gap between venue close and local receipt is the only way to tell "
        "a quiet market from a dead feed")


_WS_CLOSED = {
    "e": "kline", "E": 1767571260000, "s": "BTCUSDT",
    "k": {"t": 1767571200000, "T": 1767571259999, "s": "BTCUSDT", "i": "1m",
          "f": 100, "L": 200, "o": "42000.10", "c": "42050.25",
          "h": "42100.50", "l": "41950.00", "v": "12.5", "n": 150,
          "x": True, "q": "525000.75"},
}


def test_a_websocket_event_for_a_closed_bar_is_accepted():
    provider = BinanceSpotProvider(clock=FixedClock(T0))
    record = provider.parse_ws_kline(_WS_CLOSED, received_at=T0)
    assert record is not None
    assert record.candle.instrument_key == "BINANCE:BTCUSDT"
    assert record.candle.close == 42050.25
    assert record.sequence == 200


def test_a_websocket_event_for_a_forming_bar_is_rejected():
    """`k.x == False` means the bar is still open. Publishing it would be the
    streaming form of look-ahead."""
    forming = {**_WS_CLOSED, "k": {**_WS_CLOSED["k"], "x": False}}
    provider = BinanceSpotProvider(clock=FixedClock(T0))
    assert provider.parse_ws_kline(forming, received_at=T0) is None


def test_symbol_and_interval_mapping():
    assert BinanceSpotProvider.to_symbol("BINANCE:BTCUSDT") == "BTCUSDT"
    assert BinanceSpotProvider.to_symbol("BINANCE:BTC-USDT") == "BTCUSDT"
    assert BinanceSpotProvider.to_interval("1m") == "1m"
    assert BinanceSpotProvider.to_interval("4h") == "4h"
    assert BinanceSpotProvider.to_interval("1d") == "1d"


def test_an_unsupported_timeframe_is_refused_before_any_request():
    """Better a clear config error than a venue-side rejection mid-run."""
    with pytest.raises(ConfigurationError):
        BinanceSpotProvider.to_interval("7m")


def test_backfill_rejects_an_inverted_range():
    provider = BinanceSpotProvider(clock=FixedClock(T0))
    with pytest.raises(ConfigurationError):
        provider.backfill(KEY, "1m", start=T0, end=T0 - timedelta(hours=1))


def test_an_unreachable_venue_raises_a_typed_error_not_a_bare_exception():
    """This is the live-blocker path, and it is exercised for real: every
    Binance host is refused by this environment's egress policy, so the call
    genuinely fails here. It must fail as a typed MarketDataError carrying the
    reason, not as a raw URLError."""
    provider = BinanceSpotProvider(
        clock=FixedClock(T0),
        rest_base="https://api.binance.invalid", request_timeout=3.0)
    with pytest.raises(MarketDataError) as excinfo:
        provider.backfill(KEY, "1m", start=T0, end=T0 + timedelta(minutes=5))
    assert "url" in excinfo.value.details


def test_health_reports_unreachable_rather_than_raising():
    """A health probe that throws cannot be used by the monitor that is supposed
    to notice the outage."""
    provider = BinanceSpotProvider(
        clock=FixedClock(T0), rest_base="https://api.binance.invalid",
        request_timeout=3.0)
    health = provider.health()
    assert health["ok"] is False
    assert "error" in health

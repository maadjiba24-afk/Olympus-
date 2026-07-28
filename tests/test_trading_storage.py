"""CandleStore — append-only OHLCV persistence.

The properties under test are the ones a backtest silently depends on: exact
float round-trip, idempotent overlapping backfills, ascending order regardless
of arrival order, half-open range queries, and survival of a damaged file.
"""

from datetime import datetime, timedelta, timezone

import pytest

from olympus import store
from olympus.trading import errors, storage
from olympus.trading.contracts import Candle
from olympus.trading.storage import CandleStore

KEY = "NASDAQ:AAPL"
TF = "1m"


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def make_candle(minute: int, *, close: float = 100.0, instrument: str = KEY,
                timeframe: str = TF, is_final: bool = True,
                source: str = "test") -> Candle:
    """A coherent bar opening `minute` minutes after 2026-03-10T14:30Z.

    OHLC is derived multiplicatively from `close` so that any positive close —
    including 1.0 — still satisfies the contract's `low > 0` check.
    """
    opened = utc(2026, 3, 10, 14, 30) + timedelta(minutes=minute)
    return Candle(
        instrument_key=instrument, timeframe=timeframe,
        ts_open=opened, ts_close=opened + timedelta(minutes=1),
        open=close * 0.995, high=close * 1.01, low=close * 0.99, close=close,
        volume=1000.0 + minute, amount=0.0, is_final=is_final, source=source)


@pytest.fixture
def cs():
    return CandleStore()


# --- round trip -------------------------------------------------------------

def test_round_trip_returns_an_identical_candle(cs):
    original = make_candle(0, close=123.456)
    assert cs.put_candles([original]) == 1
    loaded = cs.get_candles(KEY, TF)
    assert loaded == [original]


def test_floats_round_trip_bit_exactly(cs):
    """repr-based JSON: a price must come back with the same bits, or every
    indicator computed from it drifts."""
    awkward = 0.1 + 0.2                      # 0.30000000000000004
    candle = Candle(
        instrument_key=KEY, timeframe=TF,
        ts_open=utc(2026, 3, 10, 14, 30), ts_close=utc(2026, 3, 10, 14, 31),
        open=awkward, high=1e-7 + awkward, low=awkward / 3.0, close=awkward,
        volume=1 / 3, amount=1e18 + 1.0)
    cs.put_candles([candle])
    loaded = cs.get_candles(KEY, TF)[0]
    for name in ("open", "high", "low", "close", "volume", "amount"):
        assert getattr(loaded, name) == getattr(candle, name)
        assert repr(getattr(loaded, name)) == repr(getattr(candle, name))


def test_timestamps_come_back_as_aware_utc(cs):
    cs.put_candles([make_candle(0)])
    loaded = cs.get_candles(KEY, TF)[0]
    assert loaded.ts_open == utc(2026, 3, 10, 14, 30)
    assert loaded.ts_open.utcoffset() == timedelta(0)
    assert loaded.ts_close - loaded.ts_open == timedelta(minutes=1)


# --- idempotency / dedupe ---------------------------------------------------

def test_reput_of_identical_bars_is_a_no_op(cs):
    batch = [make_candle(i) for i in range(5)]
    assert cs.put_candles(batch) == 5
    assert cs.put_candles(batch) == 0          # observable idempotency
    assert cs.count(KEY, TF) == 5


def test_overlapping_backfill_does_not_duplicate(cs):
    cs.put_candles([make_candle(i) for i in range(5)])
    cs.put_candles([make_candle(i) for i in range(3, 8)])
    loaded = cs.get_candles(KEY, TF)
    assert len(loaded) == 8
    assert [c.ts_open for c in loaded] == sorted({c.ts_open for c in loaded})


def test_a_revised_bar_replaces_the_earlier_one(cs):
    cs.put_candles([make_candle(0, close=100.0)])
    assert cs.put_candles([make_candle(0, close=101.0)]) == 1
    loaded = cs.get_candles(KEY, TF)
    assert len(loaded) == 1
    assert loaded[0].close == 101.0


def test_duplicates_within_one_batch_keep_the_last(cs):
    cs.put_candles([make_candle(0, close=1.0), make_candle(0, close=2.0)])
    assert cs.count(KEY, TF) == 1
    assert cs.get_candles(KEY, TF)[0].close == 2.0


# --- ordering and ranges ----------------------------------------------------

def test_results_are_ascending_regardless_of_arrival_order(cs):
    cs.put_candles([make_candle(i) for i in (7, 1, 4, 0, 9)])
    opens = [c.ts_open for c in cs.get_candles(KEY, TF)]
    assert opens == sorted(opens)
    assert len(opens) == 5


def test_range_query_is_half_open_on_ts_open(cs):
    cs.put_candles([make_candle(i) for i in range(10)])
    start = utc(2026, 3, 10, 14, 32)
    end = utc(2026, 3, 10, 14, 35)
    got = cs.get_candles(KEY, TF, start=start, end=end)
    assert [c.ts_open for c in got] == [start, start + timedelta(minutes=1),
                                        start + timedelta(minutes=2)]
    # Consecutive half-open ranges tile without gap or overlap.
    left = cs.get_candles(KEY, TF, end=start)
    right = cs.get_candles(KEY, TF, start=start)
    assert len(left) + len(right) == 10


def test_range_bounds_are_optional_and_independent(cs):
    cs.put_candles([make_candle(i) for i in range(6)])
    assert len(cs.get_candles(KEY, TF, start=utc(2026, 3, 10, 14, 33))) == 3
    assert len(cs.get_candles(KEY, TF, end=utc(2026, 3, 10, 14, 33))) == 3
    assert len(cs.get_candles(KEY, TF)) == 6


def test_limit_returns_the_most_recent_bars(cs):
    cs.put_candles([make_candle(i) for i in range(10)])
    got = cs.get_candles(KEY, TF, limit=3)
    assert len(got) == 3
    assert [c.ts_open for c in got] == [utc(2026, 3, 10, 14, 37),
                                        utc(2026, 3, 10, 14, 38),
                                        utc(2026, 3, 10, 14, 39)]
    assert cs.get_candles(KEY, TF, limit=0) == []
    assert len(cs.get_candles(KEY, TF, limit=500)) == 10


def test_limit_applies_after_the_range_filter(cs):
    cs.put_candles([make_candle(i) for i in range(10)])
    got = cs.get_candles(KEY, TF, end=utc(2026, 3, 10, 14, 35), limit=2)
    assert [c.ts_open for c in got] == [utc(2026, 3, 10, 14, 33),
                                        utc(2026, 3, 10, 14, 34)]


def test_bad_range_arguments_are_rejected(cs):
    cs.put_candles([make_candle(0)])
    with pytest.raises(errors.DataValidationError):
        cs.get_candles(KEY, TF, start=datetime(2026, 3, 10, 14, 30))  # naive
    with pytest.raises(errors.DataValidationError):
        cs.get_candles(KEY, TF, start=utc(2026, 3, 10, 15, 0),
                       end=utc(2026, 3, 10, 14, 0))
    with pytest.raises(errors.DataValidationError):
        cs.get_candles(KEY, TF, limit=-1)


# --- final vs forming bars --------------------------------------------------

def test_forming_bars_are_stored_but_never_read_back(cs):
    cs.put_candles([make_candle(0), make_candle(1, is_final=False)])
    assert [c.ts_open for c in cs.get_candles(KEY, TF)] == \
        [utc(2026, 3, 10, 14, 30)]
    assert cs.count(KEY, TF) == 1
    assert cs.integrity(KEY, TF)["rows"] == 2       # both are on disk


def test_a_forming_bar_becomes_visible_once_finalised(cs):
    forming = make_candle(1, close=50.0, is_final=False)
    cs.put_candles([forming])
    assert cs.latest(KEY, TF) is None
    cs.put_candles([forming.with_final(True)])
    assert cs.count(KEY, TF) == 1
    assert cs.latest(KEY, TF).close == 50.0


# --- latest / count / instruments -------------------------------------------

def test_latest_and_count_on_an_empty_series(cs):
    assert cs.latest("NASDAQ:NOPE", TF) is None
    assert cs.count("NASDAQ:NOPE", TF) == 0
    assert cs.get_candles("NASDAQ:NOPE", TF) == []
    assert cs.instruments() == []


def test_latest_is_the_newest_final_bar(cs):
    cs.put_candles([make_candle(i) for i in (0, 5, 2)])
    assert cs.latest(KEY, TF).ts_open == utc(2026, 3, 10, 14, 35)
    assert cs.count(KEY, TF) == len(cs.get_candles(KEY, TF)) == 3


def test_instruments_lists_every_series(cs):
    cs.put_candles([make_candle(0)])
    cs.put_candles([make_candle(0, timeframe="1h")])
    cs.put_candles([make_candle(0, instrument="BINANCE:BTCUSDT")])
    assert cs.instruments() == [("BINANCE:BTCUSDT", "1m"),
                                ("NASDAQ:AAPL", "1h"),
                                ("NASDAQ:AAPL", "1m")]


def test_series_are_isolated_per_instrument_and_timeframe(cs):
    cs.put_candles([make_candle(i) for i in range(3)])
    cs.put_candles([make_candle(0, timeframe="1h", close=999.0)])
    cs.put_candles([make_candle(0, instrument="BINANCE:BTCUSDT", close=1.0)])
    assert cs.count(KEY, TF) == 3
    assert cs.count(KEY, "1h") == 1
    assert cs.get_candles(KEY, "1h")[0].close == 999.0
    assert cs.get_candles("BINANCE:BTCUSDT", TF)[0].close == 1.0


def test_one_call_may_mix_instruments_and_timeframes(cs):
    written = cs.put_candles([
        make_candle(0), make_candle(1),
        make_candle(0, timeframe="1h"),
        make_candle(0, instrument="BINANCE:BTCUSDT"),
    ])
    assert written == 4
    assert len(cs.instruments()) == 3


def test_put_rejects_anything_that_is_not_a_candle(cs):
    with pytest.raises(errors.DataValidationError):
        cs.put_candles([{"ts_open": "2026-03-10T14:30:00+00:00"}])
    assert cs.put_candles([]) == 0
    assert cs.put_candles(None) == 0


# --- durability -------------------------------------------------------------

def test_history_survives_a_new_store_instance(cs):
    cs.put_candles([make_candle(i) for i in range(4)])
    fresh = CandleStore()                    # a different process, morally
    assert fresh.count(KEY, TF) == 4
    assert fresh.latest(KEY, TF).ts_open == utc(2026, 3, 10, 14, 33)
    assert fresh.instruments() == [(KEY, TF)]
    fresh.put_candles([make_candle(4)])
    assert cs.count(KEY, TF) == 5            # and the first instance sees it


def test_a_custom_namespace_is_a_separate_universe(cs):
    cs.put_candles([make_candle(0)])
    other = CandleStore(namespace="trading.candles.sim")
    assert other.count(KEY, TF) == 0
    assert other.instruments() == []
    other.put_candles([make_candle(0, close=7.0)])
    assert cs.get_candles(KEY, TF)[0].close == 100.0
    assert other.get_candles(KEY, TF)[0].close == 7.0


def test_drop_removes_a_series(cs):
    cs.put_candles([make_candle(i) for i in range(3)])
    cs.put_candles([make_candle(0, timeframe="1h")])
    cs.drop(KEY, TF)
    assert cs.count(KEY, TF) == 0
    assert cs.instruments() == [(KEY, "1h")]


def test_the_file_on_disk_is_newline_delimited_json(cs):
    """Auditability is a stated requirement, so assert the actual bytes."""
    import json
    cs.put_candles([make_candle(i) for i in range(3)])
    blob = store.backend().get(cs._ns(KEY, TF), storage._BLOB_KEY)
    lines = blob.decode("utf-8").splitlines()
    assert len(lines) == 3
    first = json.loads(lines[0])
    assert first["instrument_key"] == KEY
    assert first["ts_open"] == "2026-03-10T14:30:00+00:00"
    assert first["close"] == 100.0
    # Lines are written in ascending order, so `head` on the raw file is the
    # oldest history rather than whatever arrived first.
    assert [json.loads(line)["ts_open"] for line in lines] == \
        sorted(json.loads(line)["ts_open"] for line in lines)


# --- damage tolerance -------------------------------------------------------

def _corrupt(cs, extra_lines):
    """Append raw lines to the on-disk blob, bypassing the store's own writer."""
    ns = cs._ns(KEY, TF)
    blob = store.backend().get(ns, storage._BLOB_KEY) or b""
    store.backend().put(ns, storage._BLOB_KEY,
                        blob + "".join(l + "\n" for l in extra_lines).encode())


def test_corrupt_lines_are_skipped_and_counted(cs):
    cs.put_candles([make_candle(i) for i in range(3)])
    _corrupt(cs, ["{not json at all", "", "   ",
                  '{"instrument_key": "NASDAQ:AAPL"}',           # missing fields
                  '["a", "list", "not", "an", "object"]',
                  '{"ts_open": "2026-03-10T14:40:00+00:00", "ts_close": '
                  '"2026-03-10T14:41:00+00:00", "instrument_key": "X", '
                  '"timeframe": "1m", "open": 1, "high": 0.5, "low": 1, '
                  '"close": 1}'])                                # incoherent OHLC
    reader = CandleStore()
    assert reader.count(KEY, TF) == 3                  # good bars still readable
    assert reader.corrupt_lines == 4                   # blanks are not damage
    assert reader.integrity(KEY, TF)["corrupt"] == 4


def test_a_truncated_tail_loses_one_bar_not_the_file(cs):
    cs.put_candles([make_candle(i) for i in range(5)])
    ns = cs._ns(KEY, TF)
    blob = store.backend().get(ns, storage._BLOB_KEY)
    store.backend().put(ns, storage._BLOB_KEY, blob[:-40])   # cut mid-record
    reader = CandleStore()
    assert reader.count(KEY, TF) == 4
    assert reader.corrupt_lines == 1


def test_writing_over_a_damaged_file_keeps_the_healthy_bars(cs):
    cs.put_candles([make_candle(i) for i in range(3)])
    _corrupt(cs, ["}}garbage{{"])
    writer = CandleStore()
    writer.put_candles([make_candle(3)])
    assert writer.count(KEY, TF) == 4
    # The rewrite dropped the unparseable line rather than preserving it.
    assert CandleStore().corrupt_lines == 0
    assert CandleStore().count(KEY, TF) == 4


def test_undecodable_bytes_do_not_raise(cs):
    store.backend().put(cs._ns(KEY, TF), storage._BLOB_KEY, b"\xff\xfe\x00binary")
    reader = CandleStore()
    assert reader.get_candles(KEY, TF) == []
    assert reader.corrupt_lines == 1


def test_a_damaged_index_entry_does_not_break_listing(cs):
    cs.put_candles([make_candle(0)])
    cs.put_candles([make_candle(0, instrument="BINANCE:BTCUSDT")])
    index_ns = cs._index_ns()
    bad_key = store.backend().keys(index_ns)[0]
    store.backend().put(index_ns, bad_key, b"{broken")
    reader = CandleStore()
    assert len(reader.instruments()) == 1
    assert reader.corrupt_lines == 1


# --- misc -------------------------------------------------------------------

def test_integrity_reports_the_span(cs):
    cs.put_candles([make_candle(i) for i in range(4)])
    report = cs.integrity(KEY, TF)
    assert report["rows"] == report["final"] == 4
    assert report["oldest"] == "2026-03-10T14:30:00+00:00"
    assert report["newest"] == "2026-03-10T14:33:00+00:00"
    assert report["corrupt"] == 0


def test_default_store_is_shared(cs):
    storage.default_store().put_candles([make_candle(0)])
    assert storage.default_store() is storage.default_store()
    assert CandleStore().count(KEY, TF) == 1


def test_iter_candles_normalises_in_memory_bars():
    bars = [make_candle(2), make_candle(0, close=1.0), make_candle(1),
            make_candle(0, close=2.0)]
    out = list(storage.iter_candles(bars))
    assert [c.ts_open for c in out] == sorted(c.ts_open for c in out)
    assert len(out) == 3
    assert out[0].close == 2.0               # last write wins, as on disk


def test_lock_is_held_across_the_read_modify_write(monkeypatch, cs):
    """The KV contract is whole-value replace, so a concurrent backfill would
    otherwise lose bars. Assert the lock actually wraps the merge."""
    import olympus.proclock as proclock
    taken = []
    real = proclock.lock

    def spy(name, *args, **kwargs):
        taken.append(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(storage.proclock, "lock", spy)
    cs.put_candles([make_candle(0)])
    cs.drop(KEY, TF)
    assert len(taken) == 2
    assert all(n.startswith("trading-candles-") for n in taken)
    assert len(set(taken)) == 1              # same series → same lock name

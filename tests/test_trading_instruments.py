"""Instrument registry, market sessions, and timeframe arithmetic.

The session tests deliberately straddle a DST transition, a weekend, and a
holiday, because those are the three cases where a "looks fine in July" session
model quietly starts approving orders into a closed book.
"""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from olympus.trading import errors
from olympus.trading.contracts import Instrument
from olympus.trading.instruments import (
    CRYPTO_24_7, US_EQUITY_RTH, InstrumentRegistry, MarketSession,
    build_default_registry, default_registry, floor_to_timeframe,
    is_valid_timeframe, next_bar_open, parse_timeframe, timeframe_bounds,
    timeframe_seconds,
)


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


# --- timeframes -------------------------------------------------------------

@pytest.mark.parametrize("text,seconds", [
    ("1s", 1), ("30s", 30), ("1m", 60), ("5m", 300), ("15m", 900),
    ("1h", 3600), ("4h", 14400), ("1d", 86400), ("1w", 604800),
    ("2w", 1209600),
])
def test_parse_timeframe_units(text, seconds):
    assert parse_timeframe(text) == timedelta(seconds=seconds)
    assert timeframe_seconds(text) == seconds


@pytest.mark.parametrize("text", [
    "5M", " 5m ", "5min", "5 minutes", "1HOUR", "1Day", "2wks",
])
def test_parse_timeframe_is_case_and_alias_tolerant(text):
    assert parse_timeframe(text).total_seconds() > 0


def test_uppercase_m_is_minutes_not_months():
    assert parse_timeframe("15M") == timedelta(minutes=15)


@pytest.mark.parametrize("text", [
    "", "m", "5", "0m", "-5m", "1mo", "1month", "3y", "1 fortnight", "5.5m",
    None, 5,
])
def test_parse_timeframe_rejects_garbage(text):
    with pytest.raises(errors.ConfigurationError):
        parse_timeframe(text)
    assert is_valid_timeframe(text) is False


def test_calendar_units_rejected_with_a_specific_message():
    with pytest.raises(errors.ConfigurationError) as excinfo:
        parse_timeframe("1mo")
    assert "calendar" in str(excinfo.value)


def test_floor_to_timeframe_minutes_and_hours():
    when = utc(2026, 3, 14, 13, 37, 42, 500_000)
    assert floor_to_timeframe(when, "1m") == utc(2026, 3, 14, 13, 37)
    assert floor_to_timeframe(when, "5m") == utc(2026, 3, 14, 13, 35)
    assert floor_to_timeframe(when, "15m") == utc(2026, 3, 14, 13, 30)
    assert floor_to_timeframe(when, "1h") == utc(2026, 3, 14, 13, 0)
    assert floor_to_timeframe(when, "4h") == utc(2026, 3, 14, 12, 0)
    assert floor_to_timeframe(when, "1d") == utc(2026, 3, 14)


def test_floor_to_timeframe_is_idempotent_on_a_boundary():
    boundary = utc(2026, 3, 14, 13, 35)
    assert floor_to_timeframe(boundary, "5m") == boundary


def test_floor_to_timeframe_weeks_land_on_monday():
    # 2026-03-14 is a Saturday; its week opens Monday 2026-03-09.
    assert date(2026, 3, 14).weekday() == 5
    floored = floor_to_timeframe(utc(2026, 3, 14, 13, 37), "1w")
    assert floored == utc(2026, 3, 9)
    assert floored.weekday() == 0


def test_floor_to_timeframe_handles_pre_epoch_without_rounding_up():
    floored = floor_to_timeframe(utc(1969, 7, 20, 20, 17, 40), "1h")
    assert floored == utc(1969, 7, 20, 20, 0)


def test_floor_to_timeframe_rejects_naive_datetimes():
    with pytest.raises(errors.DataValidationError):
        floor_to_timeframe(datetime(2026, 3, 14, 13, 37), "5m")


def test_floor_normalises_a_non_utc_input_to_the_same_bar():
    eastern = utc(2026, 3, 14, 13, 37).astimezone(timezone(timedelta(hours=-4)))
    assert floor_to_timeframe(eastern, "15m") == utc(2026, 3, 14, 13, 30)


def test_next_bar_open_and_bounds_tile_the_timeline():
    when = utc(2026, 3, 14, 13, 37)
    opened, closed = timeframe_bounds(when, "5m")
    assert opened == utc(2026, 3, 14, 13, 35)
    assert closed == utc(2026, 3, 14, 13, 40)
    assert next_bar_open(when, "5m") == closed
    # The next bar's floor is the previous bar's close: no gap, no overlap.
    assert floor_to_timeframe(closed, "5m") == closed


# --- sessions ---------------------------------------------------------------

def test_us_rth_open_and_closed_within_a_day():
    assert US_EQUITY_RTH.is_open(utc(2026, 3, 10, 14, 30)) is True   # 09:30 EDT
    assert US_EQUITY_RTH.is_open(utc(2026, 3, 10, 13, 29)) is False  # pre-open
    assert US_EQUITY_RTH.is_open(utc(2026, 3, 10, 20, 0)) is False   # 16:00 EDT
    assert US_EQUITY_RTH.is_open(utc(2026, 3, 10, 19, 59)) is True


def test_session_open_boundary_is_inclusive_and_close_exclusive():
    # DST is already in effect on 2026-03-10, so 09:30 New York is 13:30 UTC.
    assert US_EQUITY_RTH.is_open(utc(2026, 3, 10, 13, 30)) is True
    assert US_EQUITY_RTH.is_open(utc(2026, 3, 10, 19, 59, 59)) is True
    assert US_EQUITY_RTH.is_open(utc(2026, 3, 10, 20, 0)) is False


def test_dst_moves_the_utc_boundary():
    """The whole point of using zoneinfo: 09:30 New York is not a fixed UTC
    time. 2026-03-08 is the US DST transition."""
    winter = US_EQUITY_RTH.next_open(utc(2026, 3, 5, 23, 0))   # → Fri 03-06
    summer = US_EQUITY_RTH.next_open(utc(2026, 3, 8, 23, 0))   # → Mon 03-09
    assert winter == utc(2026, 3, 6, 14, 30)                   # EST = UTC-5
    assert summer == utc(2026, 3, 9, 13, 30)                   # EDT = UTC-4
    # A naive "09:30 NY is always 14:30 UTC" model would call this open.
    assert US_EQUITY_RTH.is_open(utc(2026, 3, 9, 14, 30)) is True
    assert US_EQUITY_RTH.is_open(utc(2026, 3, 9, 13, 45)) is True
    assert US_EQUITY_RTH.is_open(utc(2026, 3, 6, 13, 45)) is False


def test_autumn_dst_transition_moves_the_boundary_back():
    # 2026-11-01 is the fall-back date; the following Monday is EST again.
    assert US_EQUITY_RTH.next_open(utc(2026, 10, 29, 21, 0)) == \
        utc(2026, 10, 30, 13, 30)
    assert US_EQUITY_RTH.next_open(utc(2026, 11, 1, 12, 0)) == \
        utc(2026, 11, 2, 14, 30)


def test_weekend_is_closed_and_next_open_skips_it():
    saturday = utc(2026, 3, 14, 15, 0)
    assert date(2026, 3, 14).weekday() == 5
    assert US_EQUITY_RTH.is_open(saturday) is False
    assert US_EQUITY_RTH.is_open(utc(2026, 3, 15, 15, 0)) is False   # Sunday
    assert US_EQUITY_RTH.next_open(saturday) == utc(2026, 3, 16, 13, 30)


def test_friday_close_rolls_to_monday():
    friday_afternoon = utc(2026, 3, 13, 19, 0)
    assert US_EQUITY_RTH.is_open(friday_afternoon) is True
    assert US_EQUITY_RTH.next_close(friday_afternoon) == utc(2026, 3, 13, 20, 0)
    assert US_EQUITY_RTH.next_open(friday_afternoon) == utc(2026, 3, 16, 13, 30)


def test_holiday_closes_the_session_and_next_open_skips_it():
    # 2026-01-01 (Thursday) — New Year's Day.
    session = US_EQUITY_RTH.with_holidays([date(2026, 1, 1)])
    assert date(2026, 1, 1).weekday() == 3
    assert session.is_open(utc(2026, 1, 1, 15, 0)) is False
    assert US_EQUITY_RTH.is_open(utc(2026, 1, 1, 15, 0)) is True   # unmodified
    assert session.next_open(utc(2025, 12, 31, 22, 0)) == utc(2026, 1, 2, 14, 30)


def test_with_holidays_does_not_mutate_the_original():
    extended = US_EQUITY_RTH.with_holidays([date(2026, 7, 3)])
    assert date(2026, 7, 3) in extended.holidays
    assert US_EQUITY_RTH.holidays == frozenset()


def test_consecutive_holidays_are_all_skipped():
    session = US_EQUITY_RTH.with_holidays(
        [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)])
    assert session.next_open(utc(2026, 5, 29, 21, 0)) == utc(2026, 6, 4, 13, 30)


def test_crypto_session_never_closes():
    for moment in (utc(2026, 3, 14, 3, 0),        # Saturday small hours
                   utc(2026, 1, 1, 0, 0),         # New Year midnight
                   utc(2026, 3, 15, 23, 59, 59)):  # Sunday night
        assert CRYPTO_24_7.is_open(moment) is True
    # Continuous sessions are back-to-back: the "close" is the day boundary and
    # the next open is the same instant, so there is no gap to fall into.
    when = utc(2026, 3, 14, 3, 0)
    assert CRYPTO_24_7.next_close(when) == utc(2026, 3, 15)
    assert CRYPTO_24_7.is_open(CRYPTO_24_7.next_close(when)) is True


def test_overnight_session_wraps_midnight():
    overnight = MarketSession(
        name="futures_overnight", tz_name="America/New_York",
        open_time=time(18, 0), close_time=time(17, 0),
        weekdays=frozenset({0, 1, 2, 3, 6}))
    assert overnight.wraps_midnight is True
    # Sunday 18:00 New York = Monday 22:00 UTC (EST, November).
    start = utc(2026, 11, 8, 23, 0)
    assert overnight.is_open(start) is True
    assert overnight.is_open(utc(2026, 11, 9, 5, 0)) is True   # after midnight
    assert overnight.is_open(utc(2026, 11, 9, 22, 30)) is False  # 17:00–18:00 gap


def test_next_open_is_strictly_after_so_it_advances():
    first = US_EQUITY_RTH.next_open(utc(2026, 3, 10, 12, 0))
    assert first == utc(2026, 3, 10, 13, 30)
    assert US_EQUITY_RTH.next_open(first) == utc(2026, 3, 11, 13, 30)


def test_next_close_while_open_returns_the_current_close():
    assert US_EQUITY_RTH.next_close(utc(2026, 3, 10, 15, 0)) == \
        utc(2026, 3, 10, 20, 0)


def test_session_boundaries_are_returned_in_utc():
    opened = US_EQUITY_RTH.next_open(utc(2026, 3, 10, 12, 0))
    assert opened.tzinfo is timezone.utc or opened.utcoffset() == timedelta(0)


def test_session_rejects_naive_datetimes():
    with pytest.raises(errors.DataValidationError):
        US_EQUITY_RTH.is_open(datetime(2026, 3, 10, 15, 0))


def test_session_rejects_unknown_timezone_and_empty_weekdays():
    with pytest.raises(errors.ConfigurationError):
        MarketSession(name="bogus", tz_name="Mars/Olympus_Mons")
    with pytest.raises(errors.ConfigurationError):
        MarketSession(name="never", weekdays=frozenset())
    with pytest.raises(errors.ConfigurationError):
        MarketSession(name="bad_day", weekdays=frozenset({0, 9}))
    with pytest.raises(errors.ConfigurationError):
        MarketSession(name="tz_in_time", open_time=time(9, 30, tzinfo=timezone.utc))


def test_session_round_trips_through_a_dict():
    session = US_EQUITY_RTH.with_holidays([date(2026, 1, 1), date(2026, 7, 3)])
    restored = MarketSession.from_dict(session.to_dict())
    assert restored == session
    assert restored.is_open(utc(2026, 1, 1, 15, 0)) is False


def test_bounds_for_returns_none_on_a_non_trading_day():
    assert US_EQUITY_RTH.bounds_for(date(2026, 3, 14)) is None
    assert US_EQUITY_RTH.bounds_for(date(2026, 3, 13)) is not None


def test_search_horizon_failure_is_loud_not_infinite():
    forever_closed = MarketSession(
        name="closed", weekdays=frozenset({2}),
        holidays=frozenset(date(2026, 1, 7) + timedelta(days=7 * n)
                           for n in range(150)))
    with pytest.raises(errors.ConfigurationError):
        forever_closed.next_open(utc(2026, 1, 1))


# --- registry ---------------------------------------------------------------

def test_default_registry_has_equities_and_crypto():
    registry = default_registry()
    assert len(registry) >= 5
    assert {i.asset_class for i in registry} >= {"equity", "crypto"}
    assert registry.get("NASDAQ:AAPL").symbol == "AAPL"
    assert registry.get("BINANCE:BTCUSDT").allow_fractional is True


def test_lookup_by_bare_symbol_and_by_key_agree():
    registry = default_registry()
    assert registry.lookup("AAPL") is registry.get("NASDAQ:AAPL")
    assert registry.lookup("aapl") is registry.get("nasdaq:aapl")
    assert registry.lookup("AAPL", exchange="NASDAQ").key == "NASDAQ:AAPL"


def test_unknown_instrument_raises_rather_than_inventing_one():
    registry = default_registry()
    assert registry.find("NASDAQ:NOPE") is None
    with pytest.raises(errors.ConfigurationError):
        registry.get("NASDAQ:NOPE")


def test_ambiguous_bare_symbol_raises():
    registry = build_default_registry()
    registry.register(Instrument(symbol="AAPL", exchange="LSE",
                                 quote_currency="GBP"))
    with pytest.raises(errors.ConfigurationError) as excinfo:
        registry.lookup("AAPL")
    assert "EXCHANGE:SYMBOL" in str(excinfo.value)
    # Qualified lookups still work — ambiguity is only in the bare form.
    assert registry.get("LSE:AAPL").quote_currency == "GBP"


def test_duplicate_registration_requires_explicit_replace():
    registry = InstrumentRegistry()
    first = Instrument(symbol="AAPL", exchange="NASDAQ")
    registry.register(first)
    with pytest.raises(errors.ConfigurationError):
        registry.register(Instrument(symbol="AAPL", exchange="NASDAQ",
                                     lot_size=Decimal("100")))
    registry.register(Instrument(symbol="AAPL", exchange="NASDAQ",
                                 lot_size=Decimal("100")), replace=True)
    assert registry.get("NASDAQ:AAPL").lot_size == Decimal("100")


def test_default_registry_is_sealed_against_mutation():
    registry = default_registry()
    assert registry.sealed is True
    with pytest.raises(errors.ConfigurationError):
        registry.register(Instrument(symbol="EVIL", exchange="NASDAQ"))
    with pytest.raises(errors.ConfigurationError):
        registry.add_session(MarketSession(name="always", tz_name="UTC"))
    assert registry.find("NASDAQ:EVIL") is None


def test_copy_is_independent_and_mutable():
    shared = default_registry()
    mine = shared.copy()
    assert mine.sealed is False
    mine.register(Instrument(symbol="TSLA", exchange="NASDAQ"))
    assert mine.get("NASDAQ:TSLA")
    assert shared.find("NASDAQ:TSLA") is None
    assert len(mine) == len(shared) + 1


def test_reads_cannot_mutate_registry_state():
    registry = default_registry()
    everything = registry.all()
    assert isinstance(everything, tuple)
    sessions = registry.sessions()
    sessions.clear()
    assert registry.sessions()                     # the copy was cleared, not us
    keys = registry.keys()
    keys.append("NASDAQ:FAKE")
    assert "NASDAQ:FAKE" not in registry.keys()


def test_session_for_maps_asset_class_by_default():
    registry = build_default_registry()
    assert registry.session_for("NASDAQ:AAPL").name == "us_equity_rth"
    assert registry.session_for("BINANCE:BTCUSDT").name == "crypto_24_7"
    registry.register(Instrument(symbol="EURUSD", exchange="IDEALPRO",
                                 asset_class="fx", tick_size=Decimal("0.00001"),
                                 lot_size=Decimal("1")))
    # Unknown asset classes get the conservative weekday calendar, never 24/7.
    assert registry.session_for("IDEALPRO:EURUSD").name == "fx_24_5"


def test_registry_is_open_delegates_to_the_session():
    registry = default_registry()
    saturday = utc(2026, 3, 14, 15, 0)
    assert registry.is_open("NASDAQ:AAPL", saturday) is False
    assert registry.is_open("BINANCE:BTCUSDT", saturday) is True


def test_registering_with_an_unknown_session_raises():
    registry = InstrumentRegistry()
    with pytest.raises(errors.ConfigurationError):
        registry.register(Instrument(symbol="AAPL", exchange="NASDAQ"),
                          session="does_not_exist")


def test_custom_session_registration_and_use():
    registry = InstrumentRegistry()
    tokyo = MarketSession(name="tse", tz_name="Asia/Tokyo",
                          open_time=time(9, 0), close_time=time(15, 0))
    registry.add_session(tokyo)
    registry.register(Instrument(symbol="7203", exchange="TSE",
                                 quote_currency="JPY"), session="tse")
    # 09:00 Tokyo = 00:00 UTC, and Japan does not observe DST.
    assert registry.is_open("TSE:7203", utc(2026, 3, 10, 0, 0)) is True
    assert registry.is_open("TSE:7203", utc(2026, 3, 10, 6, 30)) is False


def test_registry_round_trips_through_json():
    original = build_default_registry()
    original.add_session(MarketSession(name="tse", tz_name="Asia/Tokyo",
                                       open_time=time(9, 0),
                                       close_time=time(15, 0)))
    original.register(Instrument(symbol="7203", exchange="TSE",
                                 quote_currency="JPY"), session="tse")
    restored = InstrumentRegistry.from_json(original.to_json())
    assert restored.keys() == original.keys()
    assert restored.get("BINANCE:BTCUSDT") == original.get("BINANCE:BTCUSDT")
    assert restored.session_for("TSE:7203").tz_name == "Asia/Tokyo"
    assert restored.sealed is False


def test_from_dict_rejects_malformed_definitions():
    with pytest.raises(errors.ConfigurationError):
        InstrumentRegistry.from_dict("not a mapping")
    with pytest.raises(errors.ConfigurationError):
        InstrumentRegistry.from_dict({"instruments": [{"symbol": "AAPL"}]})
    with pytest.raises(errors.ConfigurationError):
        InstrumentRegistry.from_dict(
            {"instruments": [{"symbol": "A", "exchange": "X", "wat": 1}]})
    with pytest.raises(errors.ConfigurationError):
        InstrumentRegistry.from_json("{not json")


def test_membership_and_iteration():
    registry = default_registry()
    assert "NASDAQ:AAPL" in registry
    assert "AAPL" in registry
    assert "NOPE" not in registry
    assert [i.key for i in registry] == registry.keys()
    assert registry.symbols() == sorted({i.symbol for i in registry})
    assert all(i.asset_class == "crypto" for i in registry.by_asset_class("crypto"))


def test_instrument_quantisation_still_governs_sizing():
    """The registry is only useful if what it hands back carries the venue's
    arithmetic — spot-check that the crypto lot grid rounds DOWN."""
    btc = default_registry().get("BINANCE:BTCUSDT")
    assert btc.quantize_quantity(Decimal("0.123456789")) == Decimal("0.12345")
    assert btc.quantize_price(Decimal("64000.123")) == Decimal("64000.12")

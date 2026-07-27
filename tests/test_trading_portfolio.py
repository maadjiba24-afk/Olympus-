"""Portfolio accounting — the cost-basis rules, stated as executable claims.

Companion to `test_trading_portfolio_accounting.py`, which covers the headline
zero-crossing case. This file goes after the arithmetic that is wrong *by a
plausible amount* rather than obviously: multi-leg flips, multipliers, shorts
that are averaged up, fee reporting, per-strategy attribution when two
strategies share an instrument, and what survives a restart.

Every number here is asserted exactly. A tolerance would defeat the reason this
module is Decimal.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from olympus.trading.clock import FixedClock
from olympus.trading.contracts import Fill, Instrument, Position, Side
from olympus.trading.errors import TradingError
from olympus.trading.portfolio import (PortfolioManager, TradeRecord,
                                       apply_fill_to_position)

T0 = datetime(2026, 3, 2, 14, 30, tzinfo=timezone.utc)
INST = Instrument(symbol="AAPL", exchange="NASDAQ")
K = INST.key
#: A futures-style instrument: P&L is multiplied, the basis is not.
FUT = Instrument(symbol="ESM6", exchange="CME", asset_class="future",
                 multiplier=Decimal("50"))

_seq = iter(range(1, 100_000))


def fill(side, qty, price, *, fee="0", ts=None, fid=None, key=K):
    return Fill(fill_id=fid or f"pf{next(_seq)}", client_order_id="c1",
                instrument_key=key, side=side, quantity=Decimal(qty),
                price=Decimal(price), ts=ts or T0, fee=Decimal(fee))


@pytest.fixture()
def pm():
    return PortfolioManager(starting_cash=Decimal("100000"),
                            clock=FixedClock(T0), account_id="acct-under-test")


# --- the pure cost-basis function -----------------------------------------
# Tested directly as well as through the manager: this function is the single
# implementation of the rules, so a bug here is a bug in every book at once.

def test_opening_bases_at_the_fill_price():
    effect = apply_fill_to_position(Position(instrument_key=K),
                                    fill(Side.BUY, "10", "100"))
    assert effect.position.quantity == Decimal("10")
    assert effect.position.average_price == Decimal("100")
    assert effect.gross_realised == Decimal("0")
    assert effect.closed_quantity == Decimal("0")
    assert effect.crossed_zero is False


def test_averaging_is_weighted_by_quantity_not_by_fill_count():
    """1 @ 100 then 9 @ 200 averages 190, not 150."""
    pos = apply_fill_to_position(Position(instrument_key=K),
                                 fill(Side.BUY, "1", "100")).position
    pos = apply_fill_to_position(pos, fill(Side.BUY, "9", "200")).position
    assert pos.average_price == Decimal("190")


def test_a_reducing_fill_does_not_reprice_the_remainder():
    """Repricing the survivors would book the same P&L twice."""
    pos = apply_fill_to_position(Position(instrument_key=K),
                                 fill(Side.BUY, "10", "100")).position
    effect = apply_fill_to_position(pos, fill(Side.SELL, "3", "130"))
    assert effect.position.average_price == Decimal("100")
    assert effect.gross_realised == Decimal("90")
    assert effect.closed_quantity == Decimal("3")
    assert effect.crossed_zero is False


def test_the_flip_reports_the_closed_basis_not_the_new_one():
    """The trade record needs the basis that was closed, which the flip erases."""
    pos = apply_fill_to_position(Position(instrument_key=K),
                                 fill(Side.BUY, "10", "100")).position
    effect = apply_fill_to_position(pos, fill(Side.SELL, "15", "110"))
    assert effect.closed_basis == Decimal("100")
    assert effect.position.average_price == Decimal("110")
    assert effect.crossed_zero is True


# --- crossing zero, both directions, repeatedly ---------------------------

def test_a_double_flip_never_carries_a_stale_basis(pm):
    """long 10@100 → short 5@110 → long 4@90 → short 6@95.

    Each leg's realised P&L must use the basis in force *at that moment*. A
    single carried-over average would still produce plausible numbers, which is
    why the whole sequence is asserted rather than just the final position.
    """
    pm.apply_fill(fill(Side.BUY, "10", "100"), INST)
    pm.apply_fill(fill(Side.SELL, "15", "110"), INST)   # +100 on the closed 10
    assert pm.position(K).quantity == Decimal("-5")
    assert pm.realised_pnl == Decimal("100")

    pm.apply_fill(fill(Side.BUY, "9", "90"), INST)      # short 5 @110 → +100
    assert pm.position(K).quantity == Decimal("4")
    assert pm.position(K).average_price == Decimal("90")
    assert pm.realised_pnl == Decimal("200")

    pm.apply_fill(fill(Side.SELL, "10", "95"), INST)    # long 4 @90 → +20
    assert pm.position(K).quantity == Decimal("-6")
    assert pm.position(K).average_price == Decimal("95")
    assert pm.realised_pnl == Decimal("220")


def test_the_flip_produces_exactly_one_trade_record_for_the_closed_leg(pm):
    pm.apply_fill(fill(Side.BUY, "10", "100"), INST)
    pm.apply_fill(fill(Side.SELL, "15", "110"), INST)
    assert len(pm.trades) == 1
    record = pm.trades[0]
    assert record.quantity == Decimal("10"), "only the closed 10 is a round trip"
    assert record.entry_price == Decimal("100")
    assert record.gross_pnl == Decimal("100")


def test_a_flip_restarts_the_holding_clock(pm):
    """The new side is a new position; its age must not be inherited."""
    later = T0 + timedelta(hours=6)
    pm.apply_fill(fill(Side.BUY, "10", "100", ts=T0), INST)
    pm.apply_fill(fill(Side.SELL, "15", "110", ts=later), INST)
    assert pm.position(K).opened_at == later


def test_flat_then_reopening_starts_a_fresh_basis(pm):
    pm.apply_fill(fill(Side.BUY, "10", "100"), INST)
    pm.apply_fill(fill(Side.SELL, "10", "105"), INST)
    assert pm.position(K).is_flat and pm.position(K).opened_at is None
    pm.apply_fill(fill(Side.BUY, "2", "200"), INST)
    assert pm.position(K).average_price == Decimal("200")
    assert pm.realised_pnl == Decimal("50"), "reopening realises nothing"


# --- shorts ----------------------------------------------------------------

def test_averaging_up_a_short_and_covering_at_a_loss(pm):
    """short 10@100 then short 10@120 → basis 110; cover 20 @130 loses 400."""
    pm.apply_fill(fill(Side.SELL, "10", "100"), INST)
    pm.apply_fill(fill(Side.SELL, "10", "120"), INST)
    assert pm.position(K).quantity == Decimal("-20")
    assert pm.position(K).average_price == Decimal("110")
    pm.apply_fill(fill(Side.BUY, "20", "130"), INST)
    assert pm.realised_pnl == Decimal("-400")


def test_an_unrealised_short_gain_is_positive_when_the_price_falls(pm):
    pm.apply_fill(fill(Side.SELL, "10", "100"), INST)
    pm.mark(K, 90.0)
    assert pm.position(K).unrealised_pnl(Decimal("90")) == Decimal("100")
    assert pm.snapshot().unrealised_pnl == Decimal("100")


def test_a_short_sale_raises_cash_and_shows_negative_exposure(pm):
    start = pm.cash
    pm.apply_fill(fill(Side.SELL, "10", "100"), INST)
    assert pm.cash == start + Decimal("1000")
    snap = pm.snapshot()
    assert snap.net_exposure == Decimal("-1000")
    assert snap.gross_exposure == Decimal("1000")
    assert snap.equity == start, "a short at the mark is P&L-neutral at inception"


# --- multipliers -----------------------------------------------------------

def test_the_multiplier_scales_pnl_and_cash_but_not_the_basis(pm):
    """A futures point is worth 50; the *price* is still a price."""
    pm.apply_fill(fill(Side.BUY, "2", "5000", key=FUT.key), FUT)
    assert pm.position(FUT.key).average_price == Decimal("5000")
    assert pm.cash == Decimal("100000") - Decimal("500000")
    pm.apply_fill(fill(Side.SELL, "2", "5010", key=FUT.key), FUT)
    assert pm.realised_pnl == Decimal("1000")           # 10 pts × 2 × 50


# --- fees ------------------------------------------------------------------

def test_fees_are_never_netted_into_the_basis(pm):
    """A fee-adjusted basis hides costs inside a price and breaks reconciliation."""
    pm.apply_fill(fill(Side.BUY, "10", "100", fee="7.50"), INST)
    assert pm.position(K).average_price == Decimal("100")
    assert pm.position(K).fees_paid == Decimal("7.50")
    assert pm.cash == Decimal("100000") - Decimal("1000") - Decimal("7.50")


def test_realised_pnl_is_reported_gross_and_net(pm):
    """A strategy profitable before costs and loss-making after must be visible."""
    pm.apply_fill(fill(Side.BUY, "10", "100", fee="3"), INST)
    pm.apply_fill(fill(Side.SELL, "10", "100.4", fee="3"), INST)
    assert pm.realised_pnl == Decimal("4")
    assert pm.realised_pnl_gross == Decimal("4")
    assert pm.fees_paid == Decimal("6")
    assert pm.realised_pnl_net == Decimal("-2")


def test_fees_are_charged_on_both_sides_of_a_round_trip(pm):
    pm.apply_fill(fill(Side.SELL, "5", "100", fee="1"), INST)
    pm.apply_fill(fill(Side.BUY, "5", "100", fee="1"), INST)
    assert pm.cash == Decimal("99998")
    assert pm.fees_paid == Decimal("2")


# --- equity, peak, drawdown ------------------------------------------------

def test_equity_is_exact_across_a_fractional_price():
    """The reason this module is Decimal: 0.1 + 0.2 must be 0.3."""
    pm = PortfolioManager(starting_cash=Decimal("1000"), clock=FixedClock(T0))
    pm.apply_fill(fill(Side.BUY, "3", "0.1"), INST)
    assert pm.cash == Decimal("999.7")
    pm.mark(K, 0.1)
    assert pm.snapshot().equity == Decimal("1000.0")


def test_the_peak_tracks_marks_not_only_fills():
    """A book can make and lose its high water mark without trading at all."""
    pm = PortfolioManager(starting_cash=Decimal("1000"), clock=FixedClock(T0))
    pm.apply_fill(fill(Side.BUY, "10", "10"), INST)
    pm.mark(K, 20.0)                       # equity 1100
    assert pm.peak_equity == Decimal("1100")
    pm.mark(K, 5.0)                        # equity 950
    assert pm.peak_equity == Decimal("1100"), "a peak is a high water mark"
    assert pm.drawdown() == Decimal("150")
    assert pm.drawdown_fraction() == Decimal("150") / Decimal("1100")


def test_mark_all_revalues_once_and_rejects_a_bad_price(pm):
    pm.apply_fill(fill(Side.BUY, "1", "100"), INST)
    pm.mark_all({K: 120.0})
    assert pm.snapshot().marks[K] == 120.0
    with pytest.raises(TradingError):
        pm.mark_all({K: -1.0})


def test_drawdown_is_zero_at_a_new_high(pm):
    pm.apply_fill(fill(Side.BUY, "1", "100"), INST)
    pm.mark(K, 500.0)
    assert pm.drawdown() == Decimal("0")


def test_the_equity_curve_records_every_fill_in_order(pm):
    pm.apply_fill(fill(Side.BUY, "1", "100", ts=T0), INST)
    pm.apply_fill(fill(Side.BUY, "1", "100", ts=T0 + timedelta(minutes=5)), INST)
    curve = pm.equity_curve()
    assert [ts for ts, _ in curve] == [T0, T0 + timedelta(minutes=5)]


def test_a_mark_driven_equity_point_can_be_recorded_explicitly(pm):
    """The backtester needs a curve point per bar, not per trade."""
    pm.apply_fill(fill(Side.BUY, "10", "100"), INST)
    pm.mark(K, 110.0)
    value = pm.record_equity_point(T0 + timedelta(hours=1))
    assert value == Decimal("100100")
    assert pm.equity_curve()[-1] == (T0 + timedelta(hours=1), Decimal("100100"))


# --- daily P&L rollover ----------------------------------------------------

def test_daily_pnl_measures_from_the_days_opening_equity():
    clock = FixedClock(T0)
    pm = PortfolioManager(starting_cash=Decimal("1000"), clock=clock)
    pm.apply_fill(fill(Side.BUY, "10", "10", ts=T0), INST)
    pm.mark(K, 12.0)
    assert pm.daily_pnl(T0) == Decimal("20")


def test_daily_pnl_resets_after_the_utc_midnight_boundary():
    """Yesterday's profit must not still be counted as today's."""
    clock = FixedClock(T0)
    pm = PortfolioManager(starting_cash=Decimal("1000"), clock=clock)
    pm.apply_fill(fill(Side.BUY, "10", "10", ts=T0), INST)
    pm.mark(K, 12.0)
    day1 = pm.daily_pnl(T0)
    assert day1 == Decimal("20")

    next_day = T0 + timedelta(days=1)
    clock.set(next_day)
    pm.record_equity_point(next_day)       # the new session's opening mark
    assert pm.daily_pnl(next_day) == Decimal("0")
    pm.mark(K, 13.0)
    assert pm.daily_pnl(next_day) == Decimal("10"), (
        "today's P&L is measured from today's open, not from inception")


def test_daily_pnl_before_the_first_event_of_the_day_uses_yesterdays_close():
    """A quiet morning can still breach a daily-loss limit on marks alone."""
    clock = FixedClock(T0)
    pm = PortfolioManager(starting_cash=Decimal("1000"), clock=clock)
    pm.apply_fill(fill(Side.BUY, "10", "10", ts=T0), INST)
    next_day = T0 + timedelta(days=1)
    clock.set(next_day)
    pm.mark(K, 5.0)                        # gap down overnight, no trades yet
    assert pm.daily_pnl(next_day) == Decimal("-50")


def test_daily_pnl_of_an_untraded_portfolio_is_zero():
    pm = PortfolioManager(starting_cash=Decimal("1000"), clock=FixedClock(T0))
    assert pm.daily_pnl(T0) == Decimal("0")


# --- per-strategy attribution ---------------------------------------------

def test_two_strategies_in_one_instrument_keep_separate_books(pm):
    """Netted at the account level, separable at the attribution level — the
    account is long 5 while one strategy is long 10 and the other short 5."""
    pm.apply_fill(fill(Side.BUY, "10", "100"), INST, strategy_id="alpha")
    pm.apply_fill(fill(Side.SELL, "5", "120"), INST, strategy_id="beta")
    assert pm.position(K).quantity == Decimal("5")
    assert pm.strategy_positions("alpha")[K].quantity == Decimal("10")
    assert pm.strategy_positions("beta")[K].quantity == Decimal("-5")
    assert pm.realised_pnl == Decimal("100"), (
        "the account book still nets: beta's sell closed 5 of alpha's long")
    assert pm.strategy_pnl("alpha")["realised"] == Decimal("0")
    assert pm.strategy_pnl("beta")["realised"] == Decimal("0")


def test_strategy_attribution_reports_realised_net_and_unrealised(pm):
    pm.apply_fill(fill(Side.BUY, "10", "100", fee="2"), INST, strategy_id="alpha")
    pm.apply_fill(fill(Side.SELL, "4", "110", fee="1"), INST, strategy_id="alpha")
    pm.mark(K, 105.0)
    attribution = pm.strategy_pnl("alpha")
    assert attribution["realised"] == Decimal("40")
    assert attribution["fees"] == Decimal("3")
    assert attribution["net"] == Decimal("37")
    assert attribution["unrealised"] == Decimal("30")   # 6 @100 marked 105
    assert pm.strategy_ids() == ["alpha"]


def test_an_unknown_strategy_reports_zeroes_not_an_error(pm):
    assert pm.strategy_pnl("never-traded") == {
        "realised": Decimal("0"), "fees": Decimal("0"),
        "net": Decimal("0"), "unrealised": Decimal("0")}


# --- persistence -----------------------------------------------------------

def test_save_load_round_trips_the_whole_book(pm):
    pm.apply_fill(fill(Side.BUY, "10", "100.25", fee="1.5"), INST,
                  strategy_id="alpha")
    pm.apply_fill(fill(Side.SELL, "15", "110.75", fee="2.5"), INST,
                  strategy_id="alpha")
    pm.mark(K, 108.5)
    pm.save()

    restored = PortfolioManager(clock=FixedClock(T0),
                                account_id="acct-under-test")
    assert restored.load() is True
    assert restored.cash == pm.cash
    assert restored.realised_pnl == pm.realised_pnl
    assert restored.realised_pnl_net == pm.realised_pnl_net
    assert restored.fees_paid == pm.fees_paid
    assert restored.peak_equity == pm.peak_equity
    assert restored.position(K) == pm.position(K)
    assert restored.equity_curve() == pm.equity_curve()
    assert restored.snapshot(T0).equity == pm.snapshot(T0).equity
    assert restored.strategy_pnl("alpha") == pm.strategy_pnl("alpha")
    assert restored.strategy_positions("alpha") == pm.strategy_positions("alpha")


def test_closed_trades_survive_a_restart(pm):
    pm.apply_fill(fill(Side.BUY, "10", "100"), INST, strategy_id="alpha")
    pm.apply_fill(fill(Side.SELL, "10", "110", fee="2"), INST, strategy_id="alpha")
    pm.save()
    restored = PortfolioManager(clock=FixedClock(T0),
                                account_id="acct-under-test")
    restored.load()
    assert restored.trades == pm.trades
    assert isinstance(restored.trades[0], TradeRecord)
    assert restored.trades[0].net_pnl == Decimal("98")


def test_a_restart_continues_the_cost_basis_rather_than_restarting_it(pm):
    """The dangerous restart bug: forget the basis, then report fake P&L."""
    pm.apply_fill(fill(Side.BUY, "10", "100"), INST)
    pm.save()
    restored = PortfolioManager(clock=FixedClock(T0),
                                account_id="acct-under-test")
    restored.load()
    restored.apply_fill(fill(Side.SELL, "15", "110"), INST)
    assert restored.position(K).quantity == Decimal("-5")
    assert restored.position(K).average_price == Decimal("110")
    assert restored.realised_pnl == Decimal("100")


def test_two_accounts_do_not_share_state():
    a = PortfolioManager(starting_cash=Decimal("1000"), clock=FixedClock(T0),
                         account_id="a")
    b = PortfolioManager(starting_cash=Decimal("1000"), clock=FixedClock(T0),
                         account_id="b")
    a.apply_fill(fill(Side.BUY, "1", "100"), INST)
    a.save()
    b.save()
    reloaded = PortfolioManager(clock=FixedClock(T0), account_id="b")
    reloaded.load()
    assert reloaded.position(K).is_flat


def test_store_ns_is_honoured_for_isolation():
    pm = PortfolioManager(starting_cash=Decimal("500"), clock=FixedClock(T0),
                          store_ns="trading.portfolio.test-ns")
    pm.save()
    assert PortfolioManager(clock=FixedClock(T0)).load() is False
    other = PortfolioManager(clock=FixedClock(T0),
                             store_ns="trading.portfolio.test-ns")
    assert other.load() is True and other.cash == Decimal("500")

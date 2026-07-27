"""Portfolio accounting — the arithmetic that must be exactly right.

Focused on the cases that silently corrupt a book rather than crash it:
a fill that crosses through zero, a duplicate fill, and a restart. Each of
those produces plausible-looking numbers when it is wrong, which is why they
get explicit tests instead of being left to an end-to-end check.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from olympus.trading.clock import FixedClock
from olympus.trading.contracts import Fill, Instrument, Side
from olympus.trading.portfolio import PortfolioManager

T0 = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)
INST = Instrument(symbol="AAPL", exchange="NASDAQ")
K = INST.key

_seq = iter(range(1, 10_000))


def fill(side, qty, price, *, fee="0", ts=None, fid=None):
    return Fill(fill_id=fid or f"f{next(_seq)}", client_order_id="c1",
                instrument_key=K, side=side, quantity=Decimal(qty),
                price=Decimal(price), ts=ts or T0, fee=Decimal(fee))


@pytest.fixture()
def pm():
    return PortfolioManager(starting_cash=Decimal("100000"), clock=FixedClock(T0))


# --- the zero crossing -----------------------------------------------------

def test_long_to_short_realises_only_the_closed_quantity(pm):
    """long 10 @100, sell 15 @110 → +100 realised, short 5 based at 110."""
    pm.apply_fill(fill(Side.BUY, "10", "100"), INST)
    pm.apply_fill(fill(Side.SELL, "15", "110"), INST)
    pos = pm.position(K)
    assert pos.quantity == Decimal("-5")
    assert pos.average_price == Decimal("110"), (
        "the remainder after a flip must be re-based at the fill price; "
        "carrying the old basis corrupts every later P&L for this instrument")
    assert pm.realised_pnl == Decimal("100")


def test_short_to_long_realises_only_the_closed_quantity(pm):
    pm.apply_fill(fill(Side.SELL, "10", "100"), INST)
    pm.apply_fill(fill(Side.BUY, "15", "90"), INST)
    pos = pm.position(K)
    assert pos.quantity == Decimal("5")
    assert pos.average_price == Decimal("90")
    assert pm.realised_pnl == Decimal("100")


def test_partial_close_keeps_the_original_basis(pm):
    pm.apply_fill(fill(Side.BUY, "10", "100"), INST)
    pm.apply_fill(fill(Side.SELL, "4", "120"), INST)
    pos = pm.position(K)
    assert pos.quantity == Decimal("6")
    assert pos.average_price == Decimal("100")
    assert pm.realised_pnl == Decimal("80")


def test_exact_close_flattens_and_clears_the_basis(pm):
    pm.apply_fill(fill(Side.BUY, "10", "100"), INST)
    pm.apply_fill(fill(Side.SELL, "10", "105"), INST)
    pos = pm.position(K)
    assert pos.is_flat and pos.average_price == Decimal("0")
    assert pm.realised_pnl == Decimal("50")


def test_short_pnl_signs_are_correct(pm):
    """A short that falls in price is a profit."""
    pm.apply_fill(fill(Side.SELL, "10", "100"), INST)
    pm.apply_fill(fill(Side.BUY, "10", "90"), INST)
    assert pm.realised_pnl == Decimal("100")


# --- averaging and cash ----------------------------------------------------

def test_adding_to_a_position_weights_the_average(pm):
    pm.apply_fill(fill(Side.BUY, "10", "100"), INST)
    pm.apply_fill(fill(Side.BUY, "10", "120"), INST)
    assert pm.position(K).average_price == Decimal("110")


def test_cash_is_exact_and_fees_reduce_it():
    pm = PortfolioManager(starting_cash=Decimal("1000"), clock=FixedClock(T0))
    pm.apply_fill(fill(Side.BUY, "3", "100", fee="1.25"), INST)
    assert pm.cash == Decimal("698.75"), "Decimal arithmetic must be exact"
    assert pm.fees_paid == Decimal("1.25")


def test_selling_returns_cash(pm):
    start = pm.cash
    pm.apply_fill(fill(Side.SELL, "5", "100"), INST)
    assert pm.cash == start + Decimal("500")


# --- idempotency and restart ----------------------------------------------

def test_the_same_fill_applied_twice_has_one_effect(pm):
    """Fills arrive from polling, streams, and recovery — duplicates are normal."""
    f = fill(Side.BUY, "10", "100", fid="dupe")
    pm.apply_fill(f, INST)
    pm.apply_fill(f, INST)
    assert pm.position(K).quantity == Decimal("10")


def test_state_survives_a_restart(pm):
    pm.apply_fill(fill(Side.BUY, "7", "101.5", fee="0.5"), INST)
    pm.mark(K, 105.0)
    pm.save()

    restored = PortfolioManager(clock=FixedClock(T0))
    assert restored.load() is True
    assert restored.cash == pm.cash
    assert restored.position(K).quantity == pm.position(K).quantity
    assert restored.position(K).average_price == pm.position(K).average_price
    assert restored.fees_paid == pm.fees_paid


def test_restored_portfolio_does_not_reapply_known_fills(pm):
    f = fill(Side.BUY, "10", "100", fid="known")
    pm.apply_fill(f, INST)
    pm.save()
    restored = PortfolioManager(clock=FixedClock(T0))
    restored.load()
    restored.apply_fill(f, INST)
    assert restored.position(K).quantity == Decimal("10"), (
        "a fill already applied before the restart must not be applied again")


def test_load_returns_false_when_nothing_is_stored():
    assert PortfolioManager(account_id="never-saved",
                            clock=FixedClock(T0)).load() is False


# --- marks, equity, drawdown ----------------------------------------------

def test_equity_reflects_marks(pm):
    pm.apply_fill(fill(Side.BUY, "10", "100"), INST)
    pm.mark(K, 110.0)
    snap = pm.snapshot()
    assert snap.unrealised_pnl == Decimal("100")
    assert snap.equity == pm.cash + Decimal("1100")


def test_drawdown_measures_from_peak_equity():
    pm = PortfolioManager(starting_cash=Decimal("1000"), clock=FixedClock(T0))
    pm.apply_fill(fill(Side.BUY, "1", "100"), INST)
    pm.mark(K, 80.0)
    assert pm.drawdown() == Decimal("20")
    assert pm.drawdown_fraction() > 0


def test_a_rejected_mark_price_raises(pm):
    from olympus.trading.errors import TradingError
    with pytest.raises(TradingError):
        pm.mark(K, 0.0)


def test_daily_pnl_rolls_over_at_the_utc_day_boundary():
    clock = FixedClock(T0)
    pm = PortfolioManager(starting_cash=Decimal("1000"), clock=clock)
    pm.apply_fill(fill(Side.BUY, "1", "100", ts=T0), INST)
    pm.mark(K, 150.0)
    # A fill on the next UTC day re-anchors the session.
    next_day = T0 + timedelta(days=1)
    clock.set(next_day)
    pm.apply_fill(fill(Side.BUY, "1", "150", ts=next_day), INST)
    assert pm.daily_pnl(next_day) == Decimal("0"), (
        "daily P&L must measure from the first equity point of the current "
        "UTC day, not from inception")


# --- trade records ---------------------------------------------------------

def test_a_closing_fill_produces_a_trade_record(pm):
    pm.apply_fill(fill(Side.BUY, "10", "100"), INST, strategy_id="s1")
    pm.apply_fill(fill(Side.SELL, "10", "110", fee="2"), INST, strategy_id="s1")
    trades = pm.trades
    assert len(trades) == 1
    t = trades[0]
    assert t.quantity == Decimal("10")
    assert t.entry_price == Decimal("100") and t.exit_price == Decimal("110")
    assert t.gross_pnl == Decimal("100")
    assert t.net_pnl == Decimal("98")


def test_opening_a_position_produces_no_trade_record(pm):
    pm.apply_fill(fill(Side.BUY, "10", "100"), INST)
    assert pm.trades == []


def test_per_strategy_attribution(pm):
    pm.apply_fill(fill(Side.BUY, "10", "100"), INST, strategy_id="alpha")
    pm.apply_fill(fill(Side.SELL, "10", "110", fee="1"), INST, strategy_id="alpha")
    assert pm.strategy_pnl("alpha")["realised"] == Decimal("100")
    assert pm.strategy_pnl("alpha")["fees"] == Decimal("1")
    assert pm.strategy_pnl("unknown")["realised"] == Decimal("0")

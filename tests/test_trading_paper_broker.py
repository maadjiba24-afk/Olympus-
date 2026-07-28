"""The simulated venue.

The most important test here is `test_an_order_cannot_fill_on_a_bar_that_was_
already_open`. Every other property is about realism; that one is about honesty.
A backtest whose fills happen at prices the strategy had already seen is not a
pessimistic backtest, it is a fictional one, and no amount of fee modelling
rescues it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from olympus.trading.brokers import (BrokerCredentials, FeeModel, PaperBroker,
                                     SlippageModel)
from olympus.trading.brokers.base import BrokerCapabilities
from olympus.trading.clock import FixedClock
from olympus.trading.contracts import (Candle, Instrument, Mode, Order,
                                       OrderStatus, OrderType, Side,
                                       TimeInForce)
from olympus.trading.errors import BrokerError, BrokerUnavailable

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
INST = Instrument(symbol="AAPL", exchange="NASDAQ")


def bar(i, o, h, l, c, volume=1000):
    t = T0 + timedelta(days=i)
    return Candle(instrument_key=INST.key, timeframe="1d", ts_open=t,
                  ts_close=t + timedelta(days=1), open=o, high=h, low=l,
                  close=c, volume=volume)


def order(coid="c1", side=Side.BUY, qty="10", otype=OrderType.MARKET, **kw):
    return Order(client_order_id=coid, instrument_key=INST.key, side=side,
                 order_type=otype, quantity=Decimal(qty), created_at=T0,
                 mode=Mode.PAPER, **kw)


@pytest.fixture()
def broker():
    b = PaperBroker(clock=FixedClock(T0 + timedelta(days=1)),
                    instruments={INST.key: INST},
                    starting_cash=Decimal("100000"),
                    fee_model=FeeModel(bps=Decimal("1")),
                    slippage_model=SlippageModel(bps=Decimal("2")))
    b.connect()
    return b


# --- the honesty test ------------------------------------------------------

def test_an_order_cannot_fill_on_a_bar_that_was_already_open(broker):
    """The decision was made after bar 0 closed, so bar 0 cannot fill it."""
    broker.submit_order(order())
    assert broker.on_candle(bar(0, 100, 106, 99, 105)) == []
    assert broker.get_order("c1").status is OrderStatus.NEW


def test_a_market_order_fills_at_the_next_bars_open_not_the_signal_close(broker):
    broker.submit_order(order())
    broker.on_candle(bar(0, 100, 106, 99, 105))          # signal bar: no fill
    fills = broker.on_candle(bar(1, 110, 112, 109, 111))
    assert len(fills) == 1
    assert fills[0].price == Decimal("110.02"), (
        "fill must be the NEXT bar's open plus slippage (110 + 2bp), never "
        "the signal bar's close of 105")


# --- idempotency -----------------------------------------------------------

def test_resubmitting_the_same_client_order_id_does_not_create_a_second_order(broker):
    first = broker.submit_order(order())
    second = broker.submit_order(order())
    assert first.broker_order_id == second.broker_order_id
    assert len(broker.get_open_orders()) == 1


def test_a_retried_submit_does_not_double_fill(broker):
    broker.submit_order(order())
    broker.submit_order(order())                          # the timeout retry
    broker.on_candle(bar(0, 100, 101, 99, 100))
    broker.on_candle(bar(1, 100, 101, 99, 100))
    assert broker.get_order("c1").filled_quantity == Decimal("10")


# --- order types -----------------------------------------------------------

def test_a_limit_order_only_fills_if_the_bar_traded_through_it(broker):
    broker.submit_order(order(otype=OrderType.LIMIT, limit_price=Decimal("50")))
    broker.on_candle(bar(0, 100, 101, 99, 100))
    assert broker.on_candle(bar(1, 100, 101, 99, 100)) == []
    assert broker.on_candle(bar(2, 60, 61, 49, 55))       # low 49 <= 50
    assert broker.get_order("c1").status is OrderStatus.FILLED


def test_a_stop_order_triggers_on_the_bar_range(broker):
    broker.submit_order(order(otype=OrderType.STOP, stop_price=Decimal("120")))
    broker.on_candle(bar(0, 100, 101, 99, 100))
    assert broker.on_candle(bar(1, 100, 110, 99, 105)) == []   # never hit 120
    assert broker.on_candle(bar(2, 100, 125, 99, 120))


def test_an_unsupported_order_type_is_refused_up_front():
    b = PaperBroker(clock=FixedClock(T0), instruments={INST.key: INST},
                    capabilities=BrokerCapabilities(
                        order_types=frozenset({OrderType.MARKET})))
    b.connect()
    with pytest.raises(BrokerError):
        b.submit_order(order(otype=OrderType.LIMIT, limit_price=Decimal("1")))


# --- partial fills, fees, slippage ----------------------------------------

def test_partial_fills_accumulate_to_complete():
    b = PaperBroker(clock=FixedClock(T0 + timedelta(days=1)),
                    instruments={INST.key: INST},
                    partial_fill_ratio=Decimal("0.5"))
    b.connect()
    b.submit_order(order())
    b.on_candle(bar(0, 100, 101, 99, 100))
    b.on_candle(bar(1, 100, 101, 99, 100))
    assert b.get_order("c1").status is OrderStatus.PARTIALLY_FILLED
    for i in range(2, 8):
        b.on_candle(bar(i, 100, 101, 99, 100))
    assert b.get_order("c1").status is OrderStatus.FILLED
    assert b.get_order("c1").filled_quantity == Decimal("10")


def test_fees_and_slippage_are_charged(broker):
    broker.submit_order(order())
    broker.on_candle(bar(0, 100, 101, 99, 100))
    fill = broker.on_candle(bar(1, 100, 101, 99, 100))[0]
    assert fill.price > Decimal("100"), "a buy must slip upwards"
    assert fill.fee > 0
    account = broker.get_account()
    assert account.cash < Decimal("100000")


def test_slippage_is_always_adverse():
    model = SlippageModel(bps=Decimal("10"))
    assert model.apply(Decimal("100"), Side.BUY) > Decimal("100")
    assert model.apply(Decimal("100"), Side.SELL) < Decimal("100")


# --- failure modes ---------------------------------------------------------

def test_a_rejected_order_is_terminal():
    b = PaperBroker(clock=FixedClock(T0), instruments={INST.key: INST},
                    reject_order_ids=["c1"])
    b.connect()
    assert b.submit_order(order()).status is OrderStatus.REJECTED


def test_an_outage_makes_calls_fail_loudly(broker):
    broker.simulate_outage(True)
    assert broker.is_connected() is False
    with pytest.raises(BrokerUnavailable):
        broker.get_account()
    with pytest.raises(BrokerUnavailable):
        broker.submit_order(order("c9"))


def test_disconnected_broker_refuses_work(broker):
    broker.disconnect()
    with pytest.raises(BrokerUnavailable):
        broker.get_positions()


def test_cancel_prevents_a_fill(broker):
    broker.submit_order(order())
    broker.cancel_order("c1")
    broker.on_candle(bar(0, 100, 101, 99, 100))
    assert broker.on_candle(bar(1, 100, 101, 99, 100)) == []
    assert broker.get_order("c1").status is OrderStatus.CANCELED


def test_cancelling_an_unknown_order_raises(broker):
    with pytest.raises(BrokerError):
        broker.cancel_order("nope")


# --- reconciliation support ------------------------------------------------

def test_force_desync_creates_a_real_break(broker):
    """Reconciliation needs a counterparty that can genuinely disagree."""
    broker.force_desync(INST.key, Decimal("25"))
    positions = {p.instrument_key: p for p in broker.get_positions()}
    assert positions[INST.key].quantity == Decimal("25")


# --- credentials -----------------------------------------------------------

def test_credentials_never_print_secret_material():
    handle = BrokerCredentials(ref="broker-live-key", user="alice")
    assert "broker-live-key" in repr(handle)      # the NAME is fine to show
    for rendered in (repr(handle), str(handle)):
        assert "secret" not in rendered.lower()
        assert "password" not in rendered.lower()


def test_a_credentials_handle_carries_no_secret_attribute():
    handle = BrokerCredentials(ref="r")
    assert not hasattr(handle, "key")
    assert not hasattr(handle, "secret")
    assert set(handle.__dataclass_fields__) == {"ref", "user"}


def test_health_never_raises(broker):
    broker.simulate_outage(True)
    assert broker.health()["ok"] is False

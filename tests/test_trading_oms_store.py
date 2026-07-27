"""Order store — idempotency, the state machine, and crash recovery.

The theme is that every one of these behaviours protects against a *silent*
double-position: a retried submit, a redelivered fill, a resurrected terminal
order. None of them would crash if they were wrong; they would just quietly
trade twice.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from olympus.trading.clock import FixedClock
from olympus.trading.contracts import (Fill, Instrument, Mode, Order,
                                       OrderStatus, OrderType, RiskDecision,
                                       Side, TradeIntent, Verdict)
from olympus.trading.errors import DuplicateOrderError, ExecutionError
from olympus.trading.oms import (LEGAL_TRANSITIONS, OrderStore,
                                 make_client_order_id)

T0 = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)
INST = Instrument(symbol="AAPL", exchange="NASDAQ")


def _intent(**kw):
    base = dict(intent_id="i1", strategy_id="s1", strategy_version="1",
                instrument_key=INST.key, side=Side.BUY,
                order_type=OrderType.MARKET, quantity=Decimal("10"),
                created_at=T0)
    base.update(kw)
    return TradeIntent(**base)


def _decision(qty="10", verdict=Verdict.APPROVED, **kw):
    base = dict(intent_id="i1", verdict=verdict, checks=(),
                approved_quantity=Decimal(qty), decided_at=T0, mode=Mode.PAPER,
                policy_hash="ph1")
    base.update(kw)
    return RiskDecision(**base)


@pytest.fixture()
def store():
    return OrderStore(clock=FixedClock(T0))


def _order(coid, qty="10"):
    return Order(client_order_id=coid, instrument_key=INST.key, side=Side.BUY,
                 order_type=OrderType.MARKET, quantity=Decimal(qty),
                 created_at=T0, intent_id="i1", mode=Mode.PAPER)


def _fill(coid, fid, qty, price, fee="0"):
    return Fill(fill_id=fid, client_order_id=coid, instrument_key=INST.key,
                side=Side.BUY, quantity=Decimal(qty), price=Decimal(price),
                ts=T0, fee=Decimal(fee))


# --- the idempotency key ---------------------------------------------------

def test_client_order_id_is_deterministic():
    """A retry after a timeout or a restart must produce the SAME id."""
    a = make_client_order_id(_decision(), _intent())
    b = make_client_order_id(_decision(), _intent())
    assert a == b and a.startswith("olymp-")


def test_a_different_approved_quantity_gives_a_different_id():
    """Re-authorised at a different size is a different order, not a collision."""
    assert make_client_order_id(_decision("10"), _intent()) != \
        make_client_order_id(_decision("5", verdict=Verdict.APPROVED_REDUCED),
                             _intent())


def test_a_different_policy_gives_a_different_id():
    assert make_client_order_id(_decision(policy_hash="a"), _intent()) != \
        make_client_order_id(_decision(policy_hash="b"), _intent())


def test_a_rejected_decision_cannot_produce_an_order_id():
    with pytest.raises(ExecutionError):
        make_client_order_id(
            _decision("0", verdict=Verdict.REJECTED), _intent())


def test_a_mismatched_decision_cannot_produce_an_order_id():
    with pytest.raises(ExecutionError):
        make_client_order_id(_decision(intent_id="other"), _intent())


def test_attempt_changes_the_id_but_only_when_asked():
    base = make_client_order_id(_decision(), _intent())
    assert make_client_order_id(_decision(), _intent(), attempt=0) == base
    assert make_client_order_id(_decision(), _intent(), attempt=1) != base


# --- create / duplicate ----------------------------------------------------

def test_creating_the_same_order_twice_is_refused(store):
    store.create(_order("c1"))
    with pytest.raises(DuplicateOrderError):
        store.create(_order("c1"))


def test_get_or_create_is_idempotent(store):
    first, created = store.get_or_create(_order("c1"))
    second, created_again = store.get_or_create(_order("c1"))
    assert created is True and created_again is False
    assert first.client_order_id == second.client_order_id


def test_unknown_order_operations_raise(store):
    with pytest.raises(ExecutionError):
        store.transition("nope", OrderStatus.NEW)


# --- the state machine -----------------------------------------------------

def test_legal_transition_is_applied(store):
    store.create(_order("c1"))
    assert store.transition("c1", OrderStatus.NEW).status is OrderStatus.NEW


def test_terminal_states_are_terminal(store):
    store.create(_order("c1"))
    store.transition("c1", OrderStatus.REJECTED)
    with pytest.raises(ExecutionError):
        store.transition("c1", OrderStatus.NEW)


def test_every_terminal_status_has_no_outbound_transitions():
    for status in (OrderStatus.FILLED, OrderStatus.CANCELED,
                   OrderStatus.REJECTED, OrderStatus.EXPIRED):
        assert LEGAL_TRANSITIONS[status] == frozenset()
        assert status.is_terminal


# --- fills -----------------------------------------------------------------

def test_partial_then_complete_fill(store):
    store.create(_order("c1"))
    store.transition("c1", OrderStatus.NEW)
    partial = store.record_fill(_fill("c1", "f1", "4", "100", fee="1"))
    assert partial.status is OrderStatus.PARTIALLY_FILLED
    assert partial.filled_quantity == Decimal("4")

    complete = store.record_fill(_fill("c1", "f2", "6", "101", fee="1"))
    assert complete.status is OrderStatus.FILLED
    assert complete.filled_quantity == Decimal("10")
    assert complete.average_fill_price == Decimal("100.6")   # (400+606)/10
    assert complete.fees == Decimal("2")


def test_the_same_fill_twice_has_one_effect(store):
    """Fills are redelivered by polling and by post-restart recovery."""
    store.create(_order("c1"))
    store.transition("c1", OrderStatus.NEW)
    store.record_fill(_fill("c1", "f1", "4", "100"))
    again = store.record_fill(_fill("c1", "f1", "4", "100"))
    assert again.filled_quantity == Decimal("4")


def test_over_filling_is_an_error_not_a_clamp(store):
    """Clamping would hide exactly the divergence reconciliation exists to find."""
    store.create(_order("c1"))
    store.transition("c1", OrderStatus.NEW)
    store.record_fill(_fill("c1", "f1", "10", "100"))
    with pytest.raises(ExecutionError):
        store.record_fill(_fill("c1", "f2", "1", "100"))


# --- persistence and lookup ------------------------------------------------

def test_orders_survive_a_restart(store):
    store.create(_order("c1"))
    store.transition("c1", OrderStatus.NEW)
    store.record_fill(_fill("c1", "f1", "10", "100"))

    fresh = OrderStore(clock=FixedClock(T0 + timedelta(hours=1)))
    recovered = fresh.get("c1")
    assert recovered is not None
    assert recovered.status is OrderStatus.FILLED
    assert recovered.filled_quantity == Decimal("10")
    assert len(fresh.fills_for("c1")) == 1


def test_open_orders_excludes_terminal_ones(store):
    store.create(_order("c1"))
    store.create(_order("c2"))
    store.transition("c1", OrderStatus.NEW)
    store.transition("c2", OrderStatus.CANCELED)
    assert [o.client_order_id for o in store.open_orders()] == ["c1"]


def test_broker_id_lookup(store):
    store.create(_order("c1"))
    store.link_broker_id("c1", "BRK-9")
    found = store.by_broker_id("BRK-9")
    assert found is not None and found.client_order_id == "c1"
    assert store.by_broker_id("missing") is None

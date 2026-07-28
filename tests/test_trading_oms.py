"""Order store — the properties that stop a retry from becoming a second trade.

Companion to `test_trading_oms_store.py`. That file covers the happy paths of
the lifecycle; this one attacks the two things that make the OMS load-bearing:

* the client order id is a *pure function* of the authorisation, so the same
  authorisation retried in another instance, another process, or another
  release resolves to the same order at the venue; and
* every mutation is durable and validated, so a crash, a redelivery, or a
  crossed callback cannot leave the local book quietly disagreeing with the
  venue.

The illegal-transition test is driven from `LEGAL_TRANSITIONS` itself rather
than from a hand-written list of bad pairs, so widening the matrix later cannot
silently drop the check.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from olympus.trading.clock import FixedClock
from olympus.trading.contracts import (Fill, Instrument, Mode, Order,
                                       OrderStatus, OrderType, RiskDecision,
                                       Side, TimeInForce, TradeIntent, Verdict)
from olympus.trading.errors import DuplicateOrderError, ExecutionError
from olympus.trading.oms import (LEGAL_TRANSITIONS, OrderStore,
                                 make_client_order_id)

T0 = datetime(2026, 3, 2, 14, 30, tzinfo=timezone.utc)
INST = Instrument(symbol="AAPL", exchange="NASDAQ")
OTHER = Instrument(symbol="MSFT", exchange="NASDAQ")
REPO_ROOT = Path(__file__).resolve().parent.parent


def _intent(**kw):
    base = dict(intent_id="i-golden", strategy_id="s1", strategy_version="1.0.0",
                instrument_key=INST.key, side=Side.BUY,
                order_type=OrderType.MARKET, quantity=Decimal("10"),
                created_at=T0)
    base.update(kw)
    return TradeIntent(**base)


def _decision(qty="10", verdict=Verdict.APPROVED, **kw):
    base = dict(intent_id="i-golden", verdict=verdict, checks=(),
                approved_quantity=Decimal(qty), decided_at=T0, mode=Mode.PAPER,
                policy_hash="policy-abc")
    base.update(kw)
    return RiskDecision(**base)


def _order(coid, qty="10", **kw):
    base = dict(client_order_id=coid, instrument_key=INST.key, side=Side.BUY,
                order_type=OrderType.MARKET, quantity=Decimal(qty),
                created_at=T0, intent_id="i-golden", strategy_id="s1",
                mode=Mode.PAPER)
    base.update(kw)
    return Order(**base)


def _fill(coid, fid, qty, price, *, fee="0", side=Side.BUY, key=INST.key, ts=None):
    return Fill(fill_id=fid, client_order_id=coid, instrument_key=key,
                side=side, quantity=Decimal(qty), price=Decimal(price),
                ts=ts or T0, fee=Decimal(fee))


@pytest.fixture()
def store():
    return OrderStore(clock=FixedClock(T0))


# --- the idempotency key: purity ------------------------------------------

def test_the_id_is_stable_across_independent_instances():
    """Nothing about the *caller* may enter the id — only the authorisation."""
    first = OrderStore(clock=FixedClock(T0))
    second = OrderStore(clock=FixedClock(T0 + timedelta(days=9)))
    a = make_client_order_id(_decision(), _intent())
    b = make_client_order_id(_decision(), _intent())
    assert a == b
    assert first.get(a) is None and second.get(b) is None


def test_the_id_is_stable_across_processes():
    """The real failure this guards: submit → timeout → *crash* → restart.

    A fresh interpreter must derive the same id, or the recovering process
    cannot ask the venue "did you already take this one?" and will resubmit.
    """
    script = (
        "from datetime import datetime, timezone\n"
        "from decimal import Decimal\n"
        "from olympus.trading.contracts import (Mode, OrderType, RiskDecision,\n"
        "    Side, TradeIntent, Verdict)\n"
        "from olympus.trading.oms import make_client_order_id\n"
        "T0 = datetime(2026, 3, 2, 14, 30, tzinfo=timezone.utc)\n"
        "i = TradeIntent(intent_id='i-golden', strategy_id='s1',\n"
        "    strategy_version='1.0.0', instrument_key='NASDAQ:AAPL',\n"
        "    side=Side.BUY, order_type=OrderType.MARKET,\n"
        "    quantity=Decimal('10'), created_at=T0)\n"
        "d = RiskDecision(intent_id='i-golden', verdict=Verdict.APPROVED,\n"
        "    checks=(), approved_quantity=Decimal('10'), decided_at=T0,\n"
        "    mode=Mode.PAPER, policy_hash='policy-abc')\n"
        "print(make_client_order_id(d, i))\n")
    out = subprocess.run([sys.executable, "-c", script], cwd=str(REPO_ROOT),
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == make_client_order_id(_decision(), _intent())


def test_the_id_is_stable_across_releases():
    """A golden value. If this changes, every in-flight retry changes identity
    and the venue's duplicate detection stops matching — so a change to the id
    material is a migration, not a refactor."""
    assert make_client_order_id(_decision(), _intent()) == \
        "olymp-7091764ed394f9791aa17826"


def test_the_id_is_venue_safe():
    """Venues bound client ids and reject punctuation; keep it short and plain."""
    coid = make_client_order_id(_decision(), _intent())
    assert len(coid) <= 36
    assert coid.replace("-", "").isalnum() and coid.islower()


@pytest.mark.parametrize("field,value", [
    ("intent_id", "i-other"),
    ("strategy_id", "s2"),
    ("strategy_version", "2.0.0"),
    ("instrument_key", "NASDAQ:MSFT"),
    ("side", Side.SELL),
])
def test_a_materially_different_order_gets_a_different_id(field, value):
    """Anything that changes what would reach the venue must change the key —
    otherwise two genuinely different orders share one identity and the second
    is silently swallowed as a duplicate."""
    kw = {field: value}
    other_intent = _intent(**kw)
    other_decision = _decision(intent_id=other_intent.intent_id)
    assert make_client_order_id(other_decision, other_intent) != \
        make_client_order_id(_decision(), _intent())


def test_the_mode_is_part_of_the_id():
    """A paper order and a live order are not the same order."""
    assert make_client_order_id(_decision(mode=Mode.LIVE_BOUNDED), _intent()) != \
        make_client_order_id(_decision(mode=Mode.PAPER), _intent())


def test_an_unauthorised_pairing_cannot_produce_an_id():
    """The id is the *authorisation's* fingerprint; without one there is none."""
    with pytest.raises(ExecutionError):
        make_client_order_id(_decision("0", verdict=Verdict.REJECTED), _intent())
    with pytest.raises(ExecutionError):
        make_client_order_id(_decision(intent_id="someone-else"), _intent())


# --- create / duplicates ---------------------------------------------------

def test_a_duplicate_create_is_refused_even_after_a_restart(store):
    """The duplicate check must read through to storage, not to memory."""
    store.create(_order("c1"))
    fresh = OrderStore(clock=FixedClock(T0 + timedelta(hours=2)))
    with pytest.raises(DuplicateOrderError):
        fresh.create(_order("c1"))


def test_get_or_create_returns_the_stored_order_not_the_submitted_one(store):
    """The retry path must adopt what is on record, including its later state."""
    store.create(_order("c1"))
    store.transition("c1", OrderStatus.NEW)
    store.link_broker_id("c1", "BRK-1")
    adopted, created = store.get_or_create(_order("c1"))
    assert created is False
    assert adopted.status is OrderStatus.NEW
    assert adopted.broker_order_id == "BRK-1"


def test_provenance_survives_a_restart(store):
    """An order that cannot be traced back to its intent is unauditable."""
    store.create(_order("c1", limit_price=Decimal("101.5"),
                        order_type=OrderType.LIMIT,
                        time_in_force=TimeInForce.GTC))
    recovered = OrderStore(clock=FixedClock(T0)).get("c1")
    assert recovered.intent_id == "i-golden"
    assert recovered.strategy_id == "s1"
    assert recovered.mode is Mode.PAPER
    assert recovered.order_type is OrderType.LIMIT
    assert recovered.limit_price == Decimal("101.5")
    assert recovered.time_in_force is TimeInForce.GTC
    assert recovered.created_at == T0


# --- the state machine, exhaustively --------------------------------------

def test_every_illegal_transition_is_refused(store):
    """Driven from the matrix itself: a widened matrix cannot silently drop a
    check, and every refusal is asserted rather than a representative one."""
    refused = 0
    for current, allowed in LEGAL_TRANSITIONS.items():
        for target in OrderStatus:
            if target is current or target in allowed:
                continue
            coid = f"c-{current.value}-{target.value}"
            store.create(_order(coid))
            _force_status(store, coid, current)
            with pytest.raises(ExecutionError):
                store.transition(coid, target)
            assert store.get(coid).status is current, (
                "a refused transition must not have been partially applied")
            refused += 1
    assert refused > 0


def _force_status(store: OrderStore, coid: str, status: OrderStatus) -> None:
    """Walk an order to `status` using only legal moves."""
    routes = {
        OrderStatus.PENDING_NEW: (),
        OrderStatus.NEW: (OrderStatus.NEW,),
        OrderStatus.PARTIALLY_FILLED: (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED),
        OrderStatus.PENDING_CANCEL: (OrderStatus.NEW, OrderStatus.PENDING_CANCEL),
        OrderStatus.FILLED: (OrderStatus.NEW, OrderStatus.FILLED),
        OrderStatus.CANCELED: (OrderStatus.NEW, OrderStatus.CANCELED),
        OrderStatus.REJECTED: (OrderStatus.REJECTED,),
        OrderStatus.EXPIRED: (OrderStatus.NEW, OrderStatus.EXPIRED),
    }
    for step in routes[status]:
        store.transition(coid, step)


def test_re_asserting_the_current_status_is_accepted(store):
    """Venues redeliver statuses; treating that as an error makes polling noisy."""
    store.create(_order("c1"))
    store.transition("c1", OrderStatus.NEW)
    again = store.transition("c1", OrderStatus.NEW, broker_order_id="BRK-late")
    assert again.status is OrderStatus.NEW
    assert again.broker_order_id == "BRK-late"


def test_a_transition_stamps_updated_at_from_the_injected_clock(store):
    clock = FixedClock(T0)
    scoped = OrderStore(clock=clock)
    scoped.create(_order("c1"))
    clock.set(T0 + timedelta(minutes=3))
    assert scoped.transition("c1", OrderStatus.NEW).updated_at == \
        T0 + timedelta(minutes=3)


def test_operations_on_an_unknown_order_raise(store):
    with pytest.raises(ExecutionError):
        store.link_broker_id("ghost", "BRK-1")
    with pytest.raises(ExecutionError):
        store.record_fill(_fill("ghost", "f1", "1", "100"))


# --- fills -----------------------------------------------------------------

def test_three_partial_fills_accumulate_into_one_vwap(store):
    store.create(_order("c1", qty="10"))
    store.transition("c1", OrderStatus.NEW)
    store.record_fill(_fill("c1", "f1", "2", "100", fee="0.1"))
    store.record_fill(_fill("c1", "f2", "3", "101", fee="0.2"))
    final = store.record_fill(_fill("c1", "f3", "5", "102.5", fee="0.3"))
    # (200 + 303 + 512.5) / 10
    assert final.average_fill_price == Decimal("101.55")
    assert final.filled_quantity == Decimal("10")
    assert final.remaining_quantity == Decimal("0")
    assert final.fees == Decimal("0.6")
    assert final.status is OrderStatus.FILLED
    assert len(store.fills_for("c1")) == 3


def test_a_redelivered_fill_after_a_restart_has_no_second_effect(store):
    """Post-restart recovery replays fills; the id is the only defence."""
    store.create(_order("c1"))
    store.transition("c1", OrderStatus.NEW)
    store.record_fill(_fill("c1", "f1", "4", "100"))

    fresh = OrderStore(clock=FixedClock(T0 + timedelta(hours=5)))
    again = fresh.record_fill(_fill("c1", "f1", "4", "100"))
    assert again.filled_quantity == Decimal("4")
    assert len(fresh.fills_for("c1")) == 1


def test_an_over_fill_leaves_the_record_untouched(store):
    """Rejecting is not enough — it must also not half-apply."""
    store.create(_order("c1", qty="10"))
    store.transition("c1", OrderStatus.NEW)
    store.record_fill(_fill("c1", "f1", "7", "100"))
    with pytest.raises(ExecutionError):
        store.record_fill(_fill("c1", "f2", "4", "100"))
    order = store.get("c1")
    assert order.filled_quantity == Decimal("7")
    assert order.status is OrderStatus.PARTIALLY_FILLED
    assert [f.fill_id for f in store.fills_for("c1")] == ["f1"]


def test_a_fill_for_another_instrument_is_refused(store):
    """A crossed callback would book a position in an instrument we never sent."""
    store.create(_order("c1"))
    store.transition("c1", OrderStatus.NEW)
    with pytest.raises(ExecutionError):
        store.record_fill(_fill("c1", "f1", "1", "100", key=OTHER.key))


def test_a_fill_on_the_wrong_side_is_refused(store):
    """A sell reported against a buy order flips the sign of the whole position."""
    store.create(_order("c1"))
    store.transition("c1", OrderStatus.NEW)
    with pytest.raises(ExecutionError):
        store.record_fill(_fill("c1", "f1", "1", "100", side=Side.SELL))


def test_a_fill_on_a_terminal_order_is_refused(store):
    """CANCELED then filled means the venue and we disagree — surface it."""
    store.create(_order("c1"))
    store.transition("c1", OrderStatus.CANCELED)
    with pytest.raises(ExecutionError):
        store.record_fill(_fill("c1", "f1", "1", "100"))


def test_a_fill_is_retrievable_by_its_own_id(store):
    store.create(_order("c1"))
    store.transition("c1", OrderStatus.NEW)
    store.record_fill(_fill("c1", "f1", "4", "100", fee="0.5"))
    stored = store.fill("f1")
    assert stored is not None
    assert stored.quantity == Decimal("4") and stored.fee == Decimal("0.5")
    assert store.fill("never-happened") is None


def test_a_fill_can_arrive_before_the_acknowledgement(store):
    """A fast venue fills before the ack lands; PENDING_NEW → FILLED is legal."""
    store.create(_order("c1"))
    filled = store.record_fill(_fill("c1", "f1", "10", "100"))
    assert filled.status is OrderStatus.FILLED


# --- lookup and listing ----------------------------------------------------

def test_broker_ids_are_indexed_including_ones_set_by_a_transition(store):
    store.create(_order("c1"))
    store.transition("c1", OrderStatus.NEW, broker_order_id="BRK/2026:1")
    found = OrderStore(clock=FixedClock(T0)).by_broker_id("BRK/2026:1")
    assert found is not None and found.client_order_id == "c1"


def test_relinking_an_order_to_a_different_broker_id_is_refused(store):
    """Two venue ids for one client id means a duplicate upstream — evidence
    worth keeping rather than overwriting."""
    store.create(_order("c1"))
    store.link_broker_id("c1", "BRK-1")
    store.link_broker_id("c1", "BRK-1")          # idempotent re-link is fine
    with pytest.raises(ExecutionError):
        store.link_broker_id("c1", "BRK-2")


def test_listing_is_ordered_and_splits_open_from_terminal(store):
    store.create(_order("c1"))
    store.create(_order("c2", created_at=T0 + timedelta(minutes=1)))
    store.create(_order("c3", created_at=T0 + timedelta(minutes=2)))
    store.transition("c2", OrderStatus.FILLED)
    fresh = OrderStore(clock=FixedClock(T0))
    assert [o.client_order_id for o in fresh.all_orders()] == ["c1", "c2", "c3"]
    assert [o.client_order_id for o in fresh.open_orders()] == ["c1", "c3"]


def test_two_namespaces_do_not_see_each_other():
    """A backtest and a live account must not share an order book."""
    live = OrderStore(clock=FixedClock(T0), store_ns="trading.orders.live-test")
    back = OrderStore(clock=FixedClock(T0), store_ns="trading.orders.bt-test")
    live.create(_order("c1"))
    assert back.get("c1") is None
    assert back.all_orders() == []
    back.create(_order("c1"))                    # same id, different book: legal
    assert live.get("c1").client_order_id == "c1"

"""Execution — the gate between an authorisation and a live order.

Everything here is an attack on one of two claims.

**Nothing trades without an authorisation that names it.** `execute()` is the
only path to a venue, and it takes a `RiskDecision` it did not create. So the
tests below try to get an order out of it with a rejected decision, a decision
for someone else's intent, a decision from an hour ago, and a decision from the
future — and then check the *size* actually sent, because honouring the
authorisation and then ignoring the number on it would be the same failure
wearing a disguise.

**A retry is never a second trade.** The failure that matters is a submit whose
reply is lost: the venue has the order and the caller has an exception.
`PaperBroker.simulate_timeout` models exactly that, and the tests assert on the
number of *submissions*, not on the final state — a double-submit that the venue
happens to deduplicate is still a bug in this layer, and asserting on state
would not see it.

The scripted broker exists for the cases a paper venue cannot produce honestly:
a venue that is unreachable *for queries too* (where the only correct action is
to do nothing), and a venue that answers "I have never heard of that order".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from olympus.trading.audit import AuditTrail, EventType
from olympus.trading.brokers import PaperBroker
from olympus.trading.clock import FixedClock
from olympus.trading.contracts import (Candle, Fill, Instrument, Mode, Order,
                                       OrderStatus, OrderType, RiskDecision,
                                       Side, TimeInForce, TradeIntent, Verdict)
from olympus.trading.errors import (BrokerError, BrokerUnavailable,
                                    DuplicateOrderError, ExecutionError,
                                    ModeNotPermitted)
from olympus.trading.execution import ExecutionConfig, ExecutionEngine
from olympus.trading.oms import OrderStore, make_client_order_id
from olympus.trading.portfolio import PortfolioManager

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
INST = Instrument(symbol="AAPL", exchange="NASDAQ", tick_size=Decimal("0.01"),
                  lot_size=Decimal("1"), min_quantity=Decimal("1"))
#: A venue that only trades round lots of five — used to prove quantisation is
#: applied to the *approved* number rather than assumed away.
LOTTED = Instrument(symbol="LOT", exchange="NASDAQ", lot_size=Decimal("5"),
                    min_quantity=Decimal("5"))


# --- fixtures ---------------------------------------------------------------

def bar(i, o=100.0, h=102.0, l=99.0, c=101.0, volume=100_000.0, inst=INST):
    t = T0 + timedelta(days=i)
    return Candle(instrument_key=inst.key, timeframe="1d", ts_open=t,
                  ts_close=t + timedelta(days=1), open=o, high=h, low=l,
                  close=c, volume=volume)


def intent(qty="10", intent_id="i1", inst=INST, **kw):
    base = dict(intent_id=intent_id, strategy_id="s1", strategy_version="1",
                instrument_key=inst.key, side=Side.BUY,
                order_type=OrderType.MARKET, quantity=Decimal(qty),
                created_at=T0 + timedelta(days=1),
                intended_entry=Decimal("100"), stop_loss=Decimal("95"),
                confidence=0.9)
    base.update(kw)
    return TradeIntent(**base)


def decision(qty="10", intent_id="i1", verdict=Verdict.APPROVED, at=None, **kw):
    base = dict(intent_id=intent_id, verdict=verdict, checks=(),
                approved_quantity=Decimal(qty),
                decided_at=at or (T0 + timedelta(days=1)), mode=Mode.PAPER,
                policy_hash="policy-abc")
    base.update(kw)
    return RiskDecision(**base)


class CountingBroker:
    """Delegates everything, but remembers every submission it was asked for.

    The count is the assertion that matters for retry safety: a second
    `submit_order` call is a double-submit even when the venue quietly
    deduplicates it, and only a counter can see that.
    """

    def __init__(self, inner):
        self.inner = inner
        self.submits: list[str] = []
        self.queries: list[str] = []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def submit_order(self, order):
        self.submits.append(order.client_order_id)
        return self.inner.submit_order(order)

    def get_order(self, client_order_id):
        self.queries.append(client_order_id)
        return self.inner.get_order(client_order_id)


class ScriptedBroker:
    """A venue whose every answer is dictated by the test.

    Needed because a paper venue is always internally consistent, and the cases
    that decide whether this layer is safe are precisely the inconsistent ones:
    a venue that cannot be queried at all, and a venue that answers "no such
    order" after a failed submit.
    """

    def __init__(self, *, submit_errors=(), query_error=None, known=None,
                 fills=(), accept=True):
        self.submit_errors = list(submit_errors)
        self.query_error = query_error
        self.known: dict[str, Order] = dict(known or {})
        self._fills = list(fills)
        self.accept = accept
        self.submits: list[str] = []
        self.queries: list[str] = []
        self.cancels: list[str] = []

    def submit_order(self, order):
        self.submits.append(order.client_order_id)
        if self.submit_errors:
            raise self.submit_errors.pop(0)
        accepted = order.evolve(status=OrderStatus.NEW,
                                broker_order_id=f"B-{len(self.submits)}")
        self.known[order.client_order_id] = accepted
        return accepted

    def get_order(self, client_order_id):
        self.queries.append(client_order_id)
        if self.query_error is not None:
            raise self.query_error
        return self.known.get(client_order_id)

    def get_open_orders(self):
        return [o for o in self.known.values() if o.is_open]

    def get_fills(self, since=None):
        return list(self._fills)

    def cancel_order(self, client_order_id):
        self.cancels.append(client_order_id)
        order = self.known[client_order_id]
        cancelled = order.evolve(status=OrderStatus.CANCELED)
        self.known[client_order_id] = cancelled
        return cancelled

    def get_positions(self):
        return []


@pytest.fixture()
def stack():
    clock = FixedClock(T0 + timedelta(days=1))
    audit = AuditTrail("exec-unit", clock=clock, ledger_backed=False)
    broker = PaperBroker(clock=clock, instruments={INST.key: INST,
                                                   LOTTED.key: LOTTED},
                         starting_cash=Decimal("100000"))
    broker.connect()
    counting = CountingBroker(broker)
    orders = OrderStore(clock=clock, audit=audit)
    portfolio = PortfolioManager(starting_cash=Decimal("100000"), clock=clock)
    engine = ExecutionEngine(
        broker=counting, order_store=orders, portfolio=portfolio, clock=clock,
        audit=audit, mode=Mode.PAPER,
        config=ExecutionConfig(max_decision_age_s=3600))
    return dict(clock=clock, audit=audit, broker=broker, counting=counting,
                orders=orders, portfolio=portfolio, engine=engine)


# --- the authorisation gate -------------------------------------------------

def test_a_rejected_decision_never_reaches_the_venue(stack):
    rejected = decision(qty="0", verdict=Verdict.REJECTED,
                        reason_codes=("MAX_ORDER_VALUE_EXCEEDED",))
    with pytest.raises(ExecutionError) as exc:
        stack["engine"].execute(intent(), rejected, INST)
    assert exc.value.details["reason_code"] == "RISK_REJECTED"
    assert stack["counting"].submits == []
    assert stack["orders"].all_orders() == []


def test_a_decision_for_a_different_intent_is_refused(stack):
    with pytest.raises(ExecutionError) as exc:
        stack["engine"].execute(intent(intent_id="i-mine"),
                                decision(intent_id="i-theirs"), INST)
    assert exc.value.details["reason_code"] == "DECISION_INTENT_MISMATCH"
    assert stack["counting"].submits == []


def test_a_stale_decision_is_refused(stack):
    engine = ExecutionEngine(broker=stack["counting"], order_store=stack["orders"],
                             clock=stack["clock"], mode=Mode.PAPER,
                             config=ExecutionConfig(max_decision_age_s=60))
    d = decision()
    stack["clock"].advance(timedelta(minutes=5))
    with pytest.raises(ExecutionError) as exc:
        engine.execute(intent(), d, INST)
    assert exc.value.details["reason_code"] == "DECISION_STALE"
    assert stack["counting"].submits == []


def test_a_decision_dated_in_the_future_is_refused(stack):
    """A clock that disagrees with the risk engine is a reason to stop."""
    future = decision(at=T0 + timedelta(days=2))
    with pytest.raises(ExecutionError) as exc:
        stack["engine"].execute(intent(), future, INST)
    assert exc.value.details["reason_code"] == "DECISION_IN_FUTURE"


def test_execute_sends_the_approved_quantity_not_the_requested_one(stack):
    """The reduction risk imposed must survive the trip to the venue."""
    order = stack["engine"].execute(
        intent(qty="500"),
        decision(qty="7", verdict=Verdict.APPROVED_REDUCED), INST)
    assert order.quantity == Decimal("7")
    assert stack["broker"].get_order(order.client_order_id).quantity == Decimal("7")


def test_the_approved_quantity_is_quantised_down_to_the_lot_grid(stack):
    """Down, never up: rounding up would breach the limit that produced it."""
    order = stack["engine"].execute(
        intent(qty="100", inst=LOTTED),
        decision(qty="12", verdict=Verdict.APPROVED_REDUCED), LOTTED)
    assert order.quantity == Decimal("10")


def test_an_approval_that_rounds_to_zero_is_refused_not_rounded_up(stack):
    with pytest.raises(ExecutionError) as exc:
        stack["engine"].execute(intent(qty="100", inst=LOTTED),
                                decision(qty="3"), LOTTED)
    assert exc.value.details["reason_code"] == "QUANTITY_ROUNDS_TO_ZERO"
    assert stack["counting"].submits == []


# --- the price collar -------------------------------------------------------

def test_the_price_collar_refuses_a_market_that_has_moved(stack):
    with pytest.raises(ExecutionError) as exc:
        stack["engine"].execute(intent(), decision(), INST,
                                reference_price=Decimal("130"))
    assert exc.value.details["reason_code"] == "PRICE_COLLAR"
    assert stack["counting"].submits == []


def test_the_price_collar_allows_a_market_that_has_barely_moved(stack):
    order = stack["engine"].execute(intent(), decision(), INST,
                                    reference_price=Decimal("100.5"))
    assert order.status is OrderStatus.NEW


def test_live_execution_without_a_reference_price_is_refused():
    """The collar may not be disabled by omission in a mode that spends money."""
    clock = FixedClock(T0 + timedelta(days=1))
    orders = OrderStore(clock=clock)

    class _Controller:
        def current(self):
            return Mode.LIVE_BOUNDED

        def permits_live_orders(self):
            return True

    engine = ExecutionEngine(broker=ScriptedBroker(), order_store=orders,
                             clock=clock, mode=Mode.LIVE_BOUNDED,
                             mode_controller=_Controller(),
                             config=ExecutionConfig(max_decision_age_s=3600))
    with pytest.raises(ExecutionError) as exc:
        engine.execute(intent(), decision(mode=Mode.LIVE_BOUNDED), INST)
    assert exc.value.details["reason_code"] == "NO_REFERENCE_PRICE"


# --- modes ------------------------------------------------------------------

def test_shadow_mode_records_the_order_and_submits_nothing(stack):
    engine = ExecutionEngine(broker=stack["counting"], order_store=stack["orders"],
                             portfolio=stack["portfolio"], clock=stack["clock"],
                             audit=stack["audit"], mode=Mode.SHADOW,
                             config=ExecutionConfig(max_decision_age_s=3600))
    order = engine.execute(intent(), decision(), INST)

    assert stack["counting"].submits == [], "shadow mode must reach no venue"
    assert order.mode is Mode.SHADOW
    # The counterfactual is still durably recorded, with its provenance.
    stored = stack["orders"].get(order.client_order_id)
    assert stored is not None and stored.intent_id == "i1"
    # ...and closed, so it can never be mistaken for live exposure.
    assert stored.status is OrderStatus.CANCELED
    assert stack["orders"].open_orders() == []


def test_live_mode_without_a_mode_controller_is_refused(stack):
    engine = ExecutionEngine(broker=stack["counting"], order_store=stack["orders"],
                             clock=stack["clock"], mode=Mode.LIVE_RESTRICTED,
                             config=ExecutionConfig(max_decision_age_s=3600))
    with pytest.raises(ModeNotPermitted):
        engine.execute(intent(), decision(), INST, reference_price=Decimal("100"))
    assert stack["counting"].submits == []


def test_live_mode_is_refused_when_the_controller_withholds_permission(stack):
    class _Controller:
        def current(self):
            return Mode.LIVE_BOUNDED

        def permits_live_orders(self):
            return False

    engine = ExecutionEngine(broker=stack["counting"], order_store=stack["orders"],
                             clock=stack["clock"], mode=Mode.LIVE_BOUNDED,
                             mode_controller=_Controller(),
                             config=ExecutionConfig(max_decision_age_s=3600))
    with pytest.raises(ModeNotPermitted):
        engine.execute(intent(), decision(), INST, reference_price=Decimal("100"))
    assert stack["counting"].submits == []


def test_an_unreadable_mode_is_a_refusal_not_a_fallback_to_paper(stack):
    """Fail closed. 'Assume paper' still submits when the adapter is live."""

    class _Broken:
        def current(self):
            raise RuntimeError("mode store unreachable")

        def permits_live_orders(self):
            return False

    engine = ExecutionEngine(broker=stack["counting"], order_store=stack["orders"],
                             clock=stack["clock"], mode=Mode.PAPER,
                             mode_controller=_Broken(),
                             config=ExecutionConfig(max_decision_age_s=3600))
    with pytest.raises(ModeNotPermitted):
        engine.execute(intent(), decision(), INST)
    assert stack["counting"].submits == []


# --- idempotency and retry safety ------------------------------------------

def test_the_same_authorisation_executed_twice_submits_once(stack):
    first = stack["engine"].execute(intent(), decision(), INST)
    second = stack["engine"].execute(intent(), decision(), INST)
    assert first.client_order_id == second.client_order_id
    assert stack["counting"].submits == [first.client_order_id]


def test_a_lost_acknowledgement_is_queried_and_adopted_not_resubmitted(stack):
    """The nastiest real failure: the venue has the order, we have an exception.

    A resubmit here doubles the position. The engine must ask the venue for the
    deterministic id first and adopt what comes back.
    """
    stack["broker"].simulate_timeout(1)
    order = stack["engine"].execute(intent(), decision(), INST)

    assert len(stack["counting"].submits) == 1, "the retry must not resubmit"
    assert stack["counting"].queries, "the retry must query before deciding"
    assert order.status is OrderStatus.NEW
    assert order.broker_order_id
    assert len(stack["broker"].get_open_orders()) == 1


def test_a_lost_acknowledgement_does_not_double_the_position(stack):
    """The same scenario, carried through to a fill — the property that matters."""
    stack["broker"].simulate_timeout(1)
    order = stack["engine"].execute(intent(qty="10"), decision(qty="10"), INST)
    stack["broker"].on_candle(bar(1))
    stack["engine"].poll()
    assert stack["portfolio"].position(INST.key).quantity == Decimal("10")
    assert stack["orders"].get(order.client_order_id).filled_quantity == Decimal("10")


def test_a_venue_that_provably_lacks_the_order_is_retried_with_the_same_id():
    """Retrying is only safe once the venue has been asked and said no."""
    clock = FixedClock(T0 + timedelta(days=1))
    orders = OrderStore(clock=clock)
    broker = ScriptedBroker(submit_errors=[
        BrokerUnavailable("connection refused"),
        BrokerUnavailable("connection refused"),
    ])
    engine = ExecutionEngine(
        broker=broker, order_store=orders, clock=clock, mode=Mode.PAPER,
        config=ExecutionConfig(max_decision_age_s=3600, max_submit_attempts=3,
                               retry_backoff_s=1.0, retry_backoff_multiplier=2.0))
    order = engine.execute(intent(), decision(), INST)

    assert len(broker.submits) == 3
    assert len(set(broker.submits)) == 1, "every attempt must reuse the same id"
    assert order.status is OrderStatus.NEW
    # Backoff is exponential and runs on the injected clock: 1s then 2s.
    assert clock.now() == T0 + timedelta(days=1, seconds=3)


def test_an_unqueryable_venue_leaves_the_order_in_flight_for_recovery():
    """'We do not know' must not be recorded as 'it never happened'.

    Marking the order rejected here would free the risk budget and invite a
    duplicate; leaving it PENDING_NEW hands the question to `recover()`.
    """
    clock = FixedClock(T0 + timedelta(days=1))
    orders = OrderStore(clock=clock)
    broker = ScriptedBroker(submit_errors=[BrokerUnavailable("timeout")],
                            query_error=BrokerUnavailable("still down"))
    engine = ExecutionEngine(broker=broker, order_store=orders, clock=clock,
                             mode=Mode.PAPER,
                             config=ExecutionConfig(max_decision_age_s=3600))
    with pytest.raises(ExecutionError):
        engine.execute(intent(), decision(), INST)

    assert len(broker.submits) == 1, "an unknown outcome must not be retried"
    coid = make_client_order_id(decision(), intent())
    assert orders.get(coid).status is OrderStatus.PENDING_NEW


def test_a_venue_rejection_is_not_retried_and_settles_the_order():
    clock = FixedClock(T0 + timedelta(days=1))
    orders = OrderStore(clock=clock)
    broker = ScriptedBroker(submit_errors=[BrokerError("insufficient margin")])
    engine = ExecutionEngine(broker=broker, order_store=orders, clock=clock,
                             mode=Mode.PAPER,
                             config=ExecutionConfig(max_decision_age_s=3600,
                                                    max_submit_attempts=3))
    with pytest.raises(ExecutionError):
        engine.execute(intent(), decision(), INST)

    assert len(broker.submits) == 1, "a venue that answered and refused will refuse again"
    coid = make_client_order_id(decision(), intent())
    assert orders.get(coid).status is OrderStatus.REJECTED


def test_an_id_collision_with_different_terms_is_fatal_not_adopted():
    """Adopting an order we did not authorise would execute someone else's terms."""
    clock = FixedClock(T0 + timedelta(days=1))
    orders = OrderStore(clock=clock)
    broker = ScriptedBroker(submit_errors=[
        DuplicateOrderError("id exists with different terms", field="quantity")])
    engine = ExecutionEngine(broker=broker, order_store=orders, clock=clock,
                             mode=Mode.PAPER,
                             config=ExecutionConfig(max_decision_age_s=3600))
    with pytest.raises(DuplicateOrderError):
        engine.execute(intent(), decision(), INST)
    assert len(broker.submits) == 1
    coid = make_client_order_id(decision(), intent())
    assert orders.get(coid).status is OrderStatus.REJECTED


# --- fills, polling, and the portfolio -------------------------------------

def test_a_partial_fill_flows_through_to_the_portfolio():
    clock = FixedClock(T0 + timedelta(days=1))
    broker = PaperBroker(clock=clock, instruments={INST.key: INST},
                         starting_cash=Decimal("100000"),
                         partial_fill_ratio=Decimal("0.5"))
    broker.connect()
    orders = OrderStore(clock=clock)
    portfolio = PortfolioManager(starting_cash=Decimal("100000"), clock=clock)
    engine = ExecutionEngine(broker=broker, order_store=orders,
                             portfolio=portfolio, clock=clock, mode=Mode.PAPER,
                             config=ExecutionConfig(max_decision_age_s=3600))
    order = engine.execute(intent(qty="10"), decision(qty="10"), INST)

    broker.on_candle(bar(1))
    applied = engine.poll()
    assert len(applied) == 1
    stored = orders.get(order.client_order_id)
    assert stored.status is OrderStatus.PARTIALLY_FILLED
    assert stored.filled_quantity == Decimal("5")
    assert portfolio.position(INST.key).quantity == Decimal("5")
    assert stored.is_open, "a partial fill leaves the order working"


def test_poll_is_idempotent(stack):
    """Fills are redelivered constantly; applying one twice doubles a position."""
    stack["engine"].execute(intent(), decision(), INST)
    stack["broker"].on_candle(bar(1))

    first = stack["engine"].poll()
    assert len(first) == 1
    quantity = stack["portfolio"].position(INST.key).quantity
    cash = stack["portfolio"].cash

    second = stack["engine"].poll()
    assert second == [], "a redelivered fill is not a new fill"
    assert stack["portfolio"].position(INST.key).quantity == quantity
    assert stack["portfolio"].cash == cash


def test_poll_adopts_a_venue_cancellation():
    clock = FixedClock(T0 + timedelta(days=1))
    orders = OrderStore(clock=clock)
    broker = ScriptedBroker()
    engine = ExecutionEngine(broker=broker, order_store=orders, clock=clock,
                             mode=Mode.PAPER,
                             config=ExecutionConfig(max_decision_age_s=3600))
    order = engine.execute(intent(), decision(), INST)

    # The venue cancelled it out from under us (session end, risk desk, ...).
    broker.known[order.client_order_id] = broker.known[
        order.client_order_id].evolve(status=OrderStatus.CANCELED)
    engine.poll()
    assert orders.get(order.client_order_id).status is OrderStatus.CANCELED


def test_poll_will_not_adopt_a_filled_status_without_the_fill_behind_it():
    """A status message cannot create a position; only a fill can.

    Adopting FILLED here would leave an order that believes it is done with
    `filled_quantity == 0` — a book that has stopped expecting the trade it
    never booked.
    """
    clock = FixedClock(T0 + timedelta(days=1))
    orders = OrderStore(clock=clock)
    portfolio = PortfolioManager(starting_cash=Decimal("100000"), clock=clock)
    broker = ScriptedBroker()
    engine = ExecutionEngine(broker=broker, order_store=orders,
                             portfolio=portfolio, clock=clock, mode=Mode.PAPER,
                             config=ExecutionConfig(max_decision_age_s=3600))
    order = engine.execute(intent(), decision(), INST)
    broker.known[order.client_order_id] = broker.known[
        order.client_order_id].evolve(status=OrderStatus.FILLED)

    engine.poll()
    stored = orders.get(order.client_order_id)
    assert stored.status is OrderStatus.NEW
    assert stored.filled_quantity == Decimal("0")
    assert portfolio.position(INST.key).is_flat


def test_poll_survives_a_broker_that_cannot_be_read(stack):
    stack["engine"].execute(intent(), decision(), INST)
    stack["broker"].simulate_outage(True)
    assert stack["engine"].poll() == []
    errors = stack["audit"].events(type=EventType.ERROR)
    assert any(e.payload.get("stage") == "poll" for e in errors)


# --- recovery ---------------------------------------------------------------

def test_recover_adopts_an_order_the_venue_already_has(stack):
    """A crash between submit and acknowledgement must not double the position."""
    coid = make_client_order_id(decision(), intent())
    # A process died right here: the local record says PENDING_NEW and nobody
    # knows whether the venue saw it.
    stack["orders"].create(Order(
        client_order_id=coid, instrument_key=INST.key, side=Side.BUY,
        order_type=OrderType.MARKET, quantity=Decimal("10"),
        created_at=stack["clock"].now(), status=OrderStatus.PENDING_NEW,
        intent_id="i1", mode=Mode.PAPER))
    # It did. The venue holds the order under the same deterministic id.
    stack["broker"].submit_order(stack["orders"].get(coid))

    recovered = stack["engine"].recover()
    assert [o.client_order_id for o in recovered] == [coid]
    adopted = stack["orders"].get(coid)
    assert adopted.status is OrderStatus.NEW
    assert adopted.broker_order_id

    # And re-executing the same authorisation now resolves to the same order.
    again = stack["engine"].execute(intent(), decision(), INST)
    assert again.client_order_id == coid
    assert len(stack["broker"].get_open_orders()) == 1


def test_recover_books_the_fills_a_restart_missed(stack):
    """Recovery of a filled order must move the position, not just the status."""
    order = stack["engine"].execute(intent(), decision(), INST)
    stack["broker"].on_candle(bar(1))            # filled at the venue only

    # A fresh store would have no record at all; here the durable store keeps
    # the order and only the in-memory fill application was lost.
    fresh_portfolio = PortfolioManager(starting_cash=Decimal("100000"),
                                       clock=stack["clock"])
    engine = ExecutionEngine(broker=stack["broker"], order_store=stack["orders"],
                             portfolio=fresh_portfolio, clock=stack["clock"],
                             mode=Mode.PAPER,
                             config=ExecutionConfig(max_decision_age_s=3600))
    engine.recover()
    assert stack["orders"].get(order.client_order_id).status is OrderStatus.FILLED
    assert fresh_portfolio.position(INST.key).quantity == Decimal("10")


def test_recover_leaves_an_order_it_cannot_confirm_alone(stack):
    """An unreachable venue is not evidence that an order does not exist."""
    coid = make_client_order_id(decision(), intent())
    stack["orders"].create(Order(
        client_order_id=coid, instrument_key=INST.key, side=Side.BUY,
        order_type=OrderType.MARKET, quantity=Decimal("10"),
        created_at=stack["clock"].now(), status=OrderStatus.PENDING_NEW,
        intent_id="i1", mode=Mode.PAPER))
    engine = ExecutionEngine(
        broker=ScriptedBroker(query_error=BrokerUnavailable("down")),
        order_store=stack["orders"], clock=stack["clock"], audit=stack["audit"],
        mode=Mode.PAPER, config=ExecutionConfig(max_decision_age_s=3600))

    assert engine.recover() == []
    assert stack["orders"].get(coid).status is OrderStatus.PENDING_NEW
    assert any(e.payload.get("stage") == "recover"
               for e in stack["audit"].events(type=EventType.ERROR))


# --- stale order cancellation ----------------------------------------------

def test_cancel_stale_orders_cancels_only_the_old_ones(stack):
    old = stack["engine"].execute(intent(intent_id="i-old"),
                                  decision(intent_id="i-old"), INST)
    stack["clock"].advance(timedelta(minutes=30))
    fresh = stack["engine"].execute(
        intent(intent_id="i-new", created_at=stack["clock"].now()),
        decision(intent_id="i-new", at=stack["clock"].now()), INST)

    cancelled = stack["engine"].cancel_stale_orders(timedelta(minutes=10))
    assert [o.client_order_id for o in cancelled] == [old.client_order_id]
    assert stack["orders"].get(old.client_order_id).status is OrderStatus.CANCELED
    assert stack["orders"].get(fresh.client_order_id).is_open


# --- stop orders ------------------------------------------------------------

def test_a_stop_intent_without_a_trigger_price_is_refused(stack):
    """`stop_loss` is the invalidation level, not the trigger — no guessing."""
    with pytest.raises(ExecutionError) as exc:
        stack["engine"].execute(
            intent(order_type=OrderType.STOP, stop_loss=None), decision(), INST)
    assert exc.value.details["reason_code"] == "NO_STOP_PRICE"
    assert stack["counting"].submits == []


def test_a_stop_intent_carries_its_trigger_from_metadata(stack):
    order = stack["engine"].execute(
        intent(order_type=OrderType.STOP, stop_loss=None,
               metadata={"stop_price": "105.00"}), decision(), INST)
    assert order.order_type is OrderType.STOP
    assert order.stop_price == Decimal("105.00")


# --- audit ------------------------------------------------------------------

def test_every_step_of_a_successful_execution_is_recorded(stack):
    stack["engine"].execute(intent(), decision(), INST)
    stack["broker"].on_candle(bar(1))
    stack["engine"].poll()

    kinds = [e.type for e in stack["audit"].events()]
    assert EventType.ORDER_SUBMITTED in kinds
    assert EventType.BROKER_RESPONSE in kinds
    assert EventType.FILL in kinds
    submitted = stack["audit"].events(type=EventType.ORDER_SUBMITTED)[0]
    assert submitted.correlation_id == "i1"
    assert submitted.subject == INST.key


def test_a_refusal_leaves_a_trace(stack):
    """'Why did we not trade' is only answerable if the absence was recorded."""
    with pytest.raises(ExecutionError):
        stack["engine"].execute(intent(), decision(qty="0",
                                                   verdict=Verdict.REJECTED), INST)
    errors = stack["audit"].events(type=EventType.ERROR)
    assert any(e.payload.get("reason_code") == "RISK_REJECTED" for e in errors)
    assert all(e.correlation_id == "i1" for e in errors)


def test_an_audit_backend_that_is_down_does_not_stop_an_order(stack):
    """The order is already at the venue; refusing to record it locally too
    would turn a logging outage into an untracked position."""

    class _Broken:
        def record(self, *a, **kw):
            raise RuntimeError("ledger unwritable")

    engine = ExecutionEngine(broker=stack["counting"], order_store=stack["orders"],
                             portfolio=stack["portfolio"], clock=stack["clock"],
                             audit=_Broken(), mode=Mode.PAPER,
                             config=ExecutionConfig(max_decision_age_s=3600))
    order = engine.execute(intent(), decision(), INST)
    assert order.status is OrderStatus.NEW

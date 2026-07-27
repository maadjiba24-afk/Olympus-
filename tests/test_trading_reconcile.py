"""Reconciliation — the check that the book is not fiction.

Every risk limit in this system is enforced against Olympus's own record of
positions and cash. If that record has drifted from the venue, the limits are
being applied to numbers nobody holds, and the whole safety story is decorative.
So there are really only two things to prove here.

**A break is never hidden.** Position mismatches, orders the venue has that we
do not, orders we think are open that the venue has never heard of, and cash
that does not add up must all produce `ok=False`. So must a broker that cannot
be *read* at all: "I could not check" and "I checked and it agrees" are
different answers, and collapsing them is how a stale book looks healthy.

**A break is never silently repaired.** Orders and fills are adopted, because
there the venue simply knows more than we do and adoption is additive. A
position mismatch is not, and the tests assert the local quantity is *unchanged*
after a desync — copying the venue's number would produce a book that
reconciles perfectly and is still wrong, which nobody would ever investigate.

`last_success_ts` gets its own tests because it is not diagnostics: the risk
engine's `max_reconciliation_age_s` gate reads it, so advancing it on a failed
pass would re-enable trading on the strength of a check that failed.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from olympus.trading.audit import AuditTrail, EventType
from olympus.trading.brokers import PaperBroker
from olympus.trading.clock import FixedClock
from olympus.trading.contracts import (AccountSnapshot, Candle, Fill,
                                       Instrument, Mode, Order, OrderStatus,
                                       OrderType, Position, RiskDecision, Side,
                                       TradeIntent, Verdict)
from olympus.trading.errors import BrokerUnavailable
from olympus.trading.execution import ExecutionConfig, ExecutionEngine
from olympus.trading.killswitch import (CODE_BROKER_DESYNC, KillSwitchRegistry,
                                        KillSwitchScope)
from olympus.trading.oms import OrderStore
from olympus.trading.portfolio import PortfolioManager
from olympus.trading.reconcile import Reconciler, Tolerances

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
INST = Instrument(symbol="AAPL", exchange="NASDAQ", tick_size=Decimal("0.01"),
                  lot_size=Decimal("1"), min_quantity=Decimal("1"))


def bar(i, o=100.0, h=102.0, l=99.0, c=101.0):
    t = T0 + timedelta(days=i)
    return Candle(instrument_key=INST.key, timeframe="1d", ts_open=t,
                  ts_close=t + timedelta(days=1), open=o, high=h, low=l,
                  close=c, volume=100_000)


def intent(qty="10", intent_id="i1", **kw):
    base = dict(intent_id=intent_id, strategy_id="s1", strategy_version="1",
                instrument_key=INST.key, side=Side.BUY,
                order_type=OrderType.MARKET, quantity=Decimal(qty),
                created_at=T0 + timedelta(days=1),
                intended_entry=Decimal("100"), stop_loss=Decimal("95"))
    base.update(kw)
    return TradeIntent(**base)


def decision(qty="10", intent_id="i1", at=None):
    return RiskDecision(intent_id=intent_id, verdict=Verdict.APPROVED, checks=(),
                        approved_quantity=Decimal(qty),
                        decided_at=at or (T0 + timedelta(days=1)),
                        mode=Mode.PAPER, policy_hash="policy-abc")


def order(coid, status=OrderStatus.NEW, qty="10"):
    return Order(client_order_id=coid, instrument_key=INST.key, side=Side.BUY,
                 order_type=OrderType.MARKET, quantity=Decimal(qty),
                 created_at=T0 + timedelta(days=1), status=status,
                 intent_id="i1", mode=Mode.PAPER)


class BlindBroker:
    """A venue that cannot be read. Every accessor raises."""

    def __init__(self, exc=None):
        self.exc = exc or BrokerUnavailable("venue unreachable")

    def get_open_orders(self):
        raise self.exc

    def get_order(self, client_order_id):
        raise self.exc

    def get_fills(self, since=None):
        raise self.exc

    def get_positions(self):
        raise self.exc

    def get_account(self):
        raise self.exc


class ScriptedBroker:
    """A venue whose entire state the test dictates, including inconsistent state."""

    def __init__(self, *, orders=(), fills=(), positions=(), cash="100000"):
        self.orders = {o.client_order_id: o for o in orders}
        self.fills = list(fills)
        self.positions = list(positions)
        self.cash = Decimal(cash)
        self.ts = T0 + timedelta(days=1)

    def get_open_orders(self):
        return [o for o in self.orders.values() if o.is_open]

    def get_order(self, client_order_id):
        return self.orders.get(client_order_id)

    def get_fills(self, since=None):
        return list(self.fills)

    def get_positions(self):
        return list(self.positions)

    def get_account(self):
        return AccountSnapshot(ts=self.ts, cash=self.cash, equity=self.cash,
                               source="scripted")


@pytest.fixture()
def stack():
    """A realistic stack on a simulated venue, wired the way a deployment is."""
    clock = FixedClock(T0 + timedelta(days=1))
    audit = AuditTrail("recon-unit", clock=clock, ledger_backed=False)
    killswitches = KillSwitchRegistry(clock=clock, audit=audit)
    broker = PaperBroker(clock=clock, instruments={INST.key: INST},
                         starting_cash=Decimal("100000"))
    broker.connect()
    orders = OrderStore(clock=clock)
    portfolio = PortfolioManager(starting_cash=Decimal("100000"), clock=clock)
    engine = ExecutionEngine(broker=broker, order_store=orders,
                             portfolio=portfolio, clock=clock, mode=Mode.PAPER,
                             config=ExecutionConfig(max_decision_age_s=3600))
    reconciler = Reconciler(broker=broker, order_store=orders,
                            portfolio=portfolio, clock=clock, audit=audit,
                            killswitches=killswitches)
    return dict(clock=clock, audit=audit, killswitches=killswitches,
                broker=broker, orders=orders, portfolio=portfolio,
                engine=engine, reconciler=reconciler)


def _scripted(clock, *, broker, portfolio=None, orders=None, **kw):
    return Reconciler(broker=broker, order_store=orders or OrderStore(clock=clock),
                      portfolio=portfolio, clock=clock, **kw)


# --- the clean path ---------------------------------------------------------

def test_a_matching_book_reconciles_clean(stack):
    stack["engine"].execute(intent(), decision(), INST)
    stack["broker"].on_candle(bar(1))
    stack["engine"].poll()

    report = stack["reconciler"].reconcile()
    assert report.ok is True, report.to_dict()
    assert report.breaks == ()
    assert stack["portfolio"].position(INST.key).quantity == Decimal("10")


def test_a_clean_pass_records_last_success_ts(stack):
    assert stack["reconciler"].last_success_ts() is None
    assert stack["reconciler"].age_seconds() is None, (
        "never reconciled is not the same as just reconciled")

    report = stack["reconciler"].reconcile()
    assert report.ok is True
    assert stack["reconciler"].last_success_ts() == stack["clock"].now()

    stack["clock"].advance(timedelta(minutes=5))
    assert stack["reconciler"].age_seconds() == pytest.approx(300.0)


def test_a_failed_pass_does_not_advance_last_success_ts(stack):
    stack["reconciler"].reconcile()
    clean_at = stack["reconciler"].last_success_ts()

    stack["clock"].advance(timedelta(minutes=10))
    stack["broker"].force_desync(INST.key, Decimal("7"))
    assert stack["reconciler"].reconcile().ok is False
    assert stack["reconciler"].last_success_ts() == clean_at, (
        "a failed check must not look like a fresh one to the risk engine")


def test_the_report_is_json_serialisable(stack):
    report = stack["reconciler"].reconcile()
    encoded = json.dumps(report.to_dict(), sort_keys=True)
    assert json.loads(encoded)["ok"] is True
    assert stack["reconciler"].last_report()["ok"] is True


# --- position breaks --------------------------------------------------------

def test_a_position_break_is_detected(stack):
    stack["engine"].execute(intent(), decision(), INST)
    stack["broker"].on_candle(bar(1))
    stack["engine"].poll()
    assert stack["reconciler"].reconcile().ok is True

    # `force_desync` moves the venue's position without telling us: 10 -> 50.
    stack["broker"].force_desync(INST.key, Decimal("40"))
    report = stack["reconciler"].reconcile()
    assert report.ok is False
    assert len(report.position_breaks) == 1
    brk = report.position_breaks[0]
    assert brk.kind == "position" and brk.key == INST.key
    assert brk.local == "10" and brk.remote == "50"


def test_a_position_break_is_never_auto_repaired(stack):
    """Copying the venue's number would produce a book that reconciles and lies.

    The two causes — a fill we missed, or a venue that is wrong — have opposite
    correct responses, and only an operator can tell them apart.
    """
    stack["engine"].execute(intent(), decision(), INST)
    stack["broker"].on_candle(bar(1))
    stack["engine"].poll()
    cash_before = stack["portfolio"].cash

    stack["broker"].force_desync(INST.key, Decimal("40"))
    stack["reconciler"].reconcile()
    stack["reconciler"].reconcile()               # and it stays broken

    assert stack["portfolio"].position(INST.key).quantity == Decimal("10")
    assert stack["portfolio"].cash == cash_before
    assert stack["reconciler"].reconcile().ok is False


def test_a_position_the_venue_holds_and_we_do_not_is_a_break():
    """The dangerous direction: exposure nothing upstream is risk-checking."""
    clock = FixedClock(T0 + timedelta(days=1))
    portfolio = PortfolioManager(starting_cash=Decimal("100000"), clock=clock)
    broker = ScriptedBroker(positions=[
        Position(instrument_key=INST.key, quantity=Decimal("25"),
                 average_price=Decimal("100"))])
    report = _scripted(clock, broker=broker, portfolio=portfolio).reconcile()

    assert report.ok is False
    assert report.position_breaks[0].local == "0"
    assert report.position_breaks[0].remote == "25"
    assert portfolio.position(INST.key).is_flat


def test_a_quantity_tolerance_is_zero_by_default():
    """A share is a share; there is no rounding noise to forgive."""
    clock = FixedClock(T0 + timedelta(days=1))
    portfolio = PortfolioManager(starting_cash=Decimal("100000"), clock=clock)
    portfolio.apply_fill(Fill(fill_id="f1", client_order_id="c1",
                              instrument_key=INST.key, side=Side.BUY,
                              quantity=Decimal("10"), price=Decimal("100"),
                              ts=clock.now()))
    broker = ScriptedBroker(
        positions=[Position(instrument_key=INST.key, quantity=Decimal("10.001"),
                            average_price=Decimal("100"))],
        cash=str(portfolio.cash))
    assert _scripted(clock, broker=broker, portfolio=portfolio).reconcile().ok is False


# --- order breaks -----------------------------------------------------------

def test_an_order_the_venue_has_and_we_do_not_is_a_break(stack):
    """No provenance means no authorisation; it is never adopted, only reported."""
    stack["broker"].submit_order(order("stranger-1"))
    report = stack["reconciler"].reconcile()

    assert report.ok is False
    assert report.missing_locally == ("stranger-1",)
    assert report.order_breaks[0].kind == "order"
    assert stack["orders"].get("stranger-1") is None, (
        "an order with no known cause must not be given one")


def test_an_order_we_think_is_open_and_the_venue_lacks_is_a_break(stack):
    stack["orders"].create(order("ghost-1"))
    report = stack["reconciler"].reconcile()

    assert report.ok is False
    assert report.missing_remotely == ("ghost-1",)
    assert stack["orders"].get("ghost-1").status is OrderStatus.NEW, (
        "a missing-remotely order is reported, not locally cancelled")


def test_a_status_the_venue_moved_on_is_adopted(stack):
    """The venue knows more than we do about its own order book."""
    submitted = stack["engine"].execute(intent(), decision(), INST)
    stack["broker"].cancel_order(submitted.client_order_id)

    report = stack["reconciler"].reconcile()
    assert report.adopted_orders == (submitted.client_order_id,)
    assert stack["orders"].get(submitted.client_order_id).status \
        is OrderStatus.CANCELED
    assert report.ok is True, "adopting a status is a repair, not a break"


def test_adopt_false_reports_without_touching_local_state(stack):
    submitted = stack["engine"].execute(intent(), decision(), INST)
    stack["broker"].cancel_order(submitted.client_order_id)

    report = stack["reconciler"].reconcile(adopt=False)
    assert report.adopted_orders == ()
    assert stack["orders"].get(submitted.client_order_id).status is OrderStatus.NEW
    assert report.details["adopt"] is False


# --- fills ------------------------------------------------------------------

def test_a_fill_we_missed_is_adopted(stack):
    """Adoption is safe here because it is additive: the fill demonstrably
    happened, and booking it moves the book toward the venue's truth."""
    stack["engine"].execute(intent(), decision(), INST)
    stack["broker"].on_candle(bar(1))            # filled at the venue only

    report = stack["reconciler"].reconcile()
    assert len(report.adopted_fills) == 1
    assert stack["portfolio"].position(INST.key).quantity == Decimal("10")
    assert report.ok is True


def test_adopting_a_fill_is_idempotent_across_passes(stack):
    stack["engine"].execute(intent(), decision(), INST)
    stack["broker"].on_candle(bar(1))
    stack["reconciler"].reconcile()

    quantity = stack["portfolio"].position(INST.key).quantity
    cash = stack["portfolio"].cash
    second = stack["reconciler"].reconcile()

    assert second.adopted_fills == (), "a redelivered fill is not a repair"
    assert stack["portfolio"].position(INST.key).quantity == quantity
    assert stack["portfolio"].cash == cash


def test_a_fill_for_an_unknown_order_does_not_corrupt_the_book():
    """It surfaces as a position break instead — which is where it belongs."""
    clock = FixedClock(T0 + timedelta(days=1))
    portfolio = PortfolioManager(starting_cash=Decimal("100000"), clock=clock)
    stray = Fill(fill_id="f-stray", client_order_id="unknown-order",
                 instrument_key=INST.key, side=Side.BUY, quantity=Decimal("5"),
                 price=Decimal("100"), ts=clock.now())
    broker = ScriptedBroker(fills=[stray], positions=[
        Position(instrument_key=INST.key, quantity=Decimal("5"),
                 average_price=Decimal("100"))])
    report = _scripted(clock, broker=broker, portfolio=portfolio).reconcile()

    assert report.adopted_fills == ()
    assert portfolio.position(INST.key).is_flat
    assert report.ok is False and report.position_breaks


# --- cash -------------------------------------------------------------------

def test_a_cash_break_is_detected():
    clock = FixedClock(T0 + timedelta(days=1))
    portfolio = PortfolioManager(starting_cash=Decimal("100000"), clock=clock)
    broker = ScriptedBroker(cash="99000")
    report = _scripted(clock, broker=broker, portfolio=portfolio).reconcile()

    assert report.ok is False
    assert report.cash_break is not None
    assert report.cash_break.local == "100000"
    assert report.cash_break.remote == "99000"


def test_cash_within_tolerance_is_not_a_break():
    """Fees, interest accrual and venue rounding differ by small amounts; a
    reconciler that flags those trains operators to ignore it."""
    clock = FixedClock(T0 + timedelta(days=1))
    portfolio = PortfolioManager(starting_cash=Decimal("100000"), clock=clock)
    broker = ScriptedBroker(cash="99999.995")
    report = _scripted(clock, broker=broker, portfolio=portfolio,
                       tolerances=Tolerances(cash=Decimal("0.01"))).reconcile()
    assert report.ok is True


# --- unreadable venue -------------------------------------------------------

def test_a_venue_that_cannot_be_read_is_not_a_clean_pass():
    """'I could not check' must never be recorded as 'everything agrees'."""
    clock = FixedClock(T0 + timedelta(days=1))
    portfolio = PortfolioManager(starting_cash=Decimal("100000"), clock=clock)
    reconciler = _scripted(clock, broker=BlindBroker(), portfolio=portfolio)

    report = reconciler.reconcile()
    assert report.ok is False
    assert len(report.errors) == 4          # orders, fills, positions, account
    assert reconciler.last_success_ts() is None


# --- the kill switch --------------------------------------------------------

def test_a_break_trips_the_broker_desync_kill_switch(stack):
    stack["broker"].force_desync(INST.key, Decimal("5"))
    assert stack["reconciler"].reconcile().ok is False

    state = stack["killswitches"].check(instrument_key=INST.key)
    assert state is not None
    assert state.code == CODE_BROKER_DESYNC
    assert state.auto is True, (
        "an automatic trip needs an explicit operator override to clear")


def test_a_clean_pass_trips_nothing(stack):
    assert stack["reconciler"].reconcile().ok is True
    assert stack["killswitches"].is_engaged(KillSwitchScope.GLOBAL) is False


def test_a_reconciler_without_a_kill_switch_still_reports(stack):
    """The registry is optional wiring; the report is not."""
    reconciler = Reconciler(broker=stack["broker"], order_store=stack["orders"],
                            portfolio=stack["portfolio"], clock=stack["clock"])
    stack["broker"].force_desync(INST.key, Decimal("3"))
    assert reconciler.reconcile().ok is False


# --- audit ------------------------------------------------------------------

def test_every_pass_is_written_to_the_audit_trail(stack):
    stack["reconciler"].reconcile()
    stack["broker"].force_desync(INST.key, Decimal("2"))
    stack["reconciler"].reconcile()

    events = stack["audit"].events(type=EventType.RECONCILIATION)
    assert [e.payload["ok"] for e in events] == [True, False]

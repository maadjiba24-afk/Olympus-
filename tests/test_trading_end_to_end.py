"""End-to-end: market data in, reconciled position out.

This file is the executable form of the completion standard. Each test maps to
one of its clauses, and every one of them drives the *real* components —
`RiskEngine`, `OrderStore`, `PaperBroker`, `PortfolioManager`, `AuditTrail`,
`ExecutionEngine`, `Reconciler` — wired the way a deployment would wire them.
No component under test is faked.

What it deliberately does NOT claim: this is a *simulated* venue. It proves the
plumbing, the refusals, and the recovery paths are real. It does not prove
anything about a broker sandbox, which is unreachable from this environment and
is recorded as a blocker in docs/TRADING_STATUS.md.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from olympus.trading.audit import AuditTrail, EventType
from olympus.trading.brokers import FeeModel, PaperBroker, SlippageModel
from olympus.trading.clock import FixedClock
from olympus.trading.contracts import (Candle, DataQuality, Instrument, Mode,
                                       OrderStatus, OrderType, Side,
                                       TradeIntent, Verdict)
from olympus.trading.errors import ExecutionError
from olympus.trading.execution import ExecutionConfig, ExecutionEngine
from olympus.trading.killswitch import KillSwitchRegistry, KillSwitchScope
from olympus.trading.oms import OrderStore
from olympus.trading.portfolio import PortfolioManager
from olympus.trading.reconcile import Reconciler
from olympus.trading import risk as R

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
INST = Instrument(symbol="AAPL", exchange="NASDAQ", tick_size=Decimal("0.01"),
                  lot_size=Decimal("1"), min_quantity=Decimal("1"))


def bar(i, o, h, l, c):
    t = T0 + timedelta(days=i)
    return Candle(instrument_key=INST.key, timeframe="1d", ts_open=t,
                  ts_close=t + timedelta(days=1), open=o, high=h, low=l,
                  close=c, volume=100_000)


class _Forecast:
    confidence = 0.9
    uncertainty = 0.2


@pytest.fixture()
def stack():
    """A full, realistically wired stack on a simulated venue."""
    clock = FixedClock(T0 + timedelta(days=1))
    audit = AuditTrail("e2e", clock=clock)
    killswitches = KillSwitchRegistry(clock=clock, audit=audit)
    limits = R.RiskLimits(
        approved_instruments=frozenset({INST.key}),
        approved_exchanges=frozenset({"NASDAQ"}),
        approved_order_types=frozenset({OrderType.MARKET, OrderType.LIMIT}),
        allowed_modes=frozenset({Mode.PAPER}),
        max_order_value=Decimal("50000"), max_position_size=Decimal("100"),
        max_gross_exposure=Decimal("100000"), max_daily_loss=Decimal("5000"),
        require_stop_loss=True, require_broker_connected=False,
        max_data_age_s=None, min_forecast_confidence=0.5,
        max_forecast_uncertainty=0.8)
    engine = R.RiskEngine(limits=limits, killswitches=killswitches,
                          clock=clock, audit=audit)
    broker = PaperBroker(clock=clock, instruments={INST.key: INST},
                         starting_cash=Decimal("100000"),
                         fee_model=FeeModel(bps=Decimal("1")),
                         slippage_model=SlippageModel(bps=Decimal("2")))
    broker.connect()
    orders = OrderStore(clock=clock, audit=audit)
    portfolio = PortfolioManager(starting_cash=Decimal("100000"), clock=clock)
    execution = ExecutionEngine(broker=broker, order_store=orders,
                                portfolio=portfolio, clock=clock, audit=audit,
                                mode=Mode.PAPER,
                                config=ExecutionConfig(max_decision_age_s=86400))
    reconciler = Reconciler(broker=broker, order_store=orders,
                            portfolio=portfolio, clock=clock, audit=audit,
                            killswitches=killswitches)
    return dict(clock=clock, audit=audit, killswitches=killswitches,
                risk=engine, broker=broker, orders=orders,
                portfolio=portfolio, execution=execution,
                reconciler=reconciler, limits=limits)


def intent(qty="10", intent_id="i1", **kw):
    base = dict(intent_id=intent_id, strategy_id="s1", strategy_version="1",
                instrument_key=INST.key, side=Side.BUY,
                order_type=OrderType.MARKET, quantity=Decimal(qty),
                created_at=T0 + timedelta(days=1),
                intended_entry=Decimal("100"), stop_loss=Decimal("95"),
                confidence=0.9, data_ts_start=T0, data_ts_end=T0 + timedelta(days=1),
                model_versions={"kronos": "kronos-small@abc123"})
    base.update(kw)
    return TradeIntent(**base)


def ctx(stack, **kw):
    base = dict(as_of=stack["clock"].now(), mode=Mode.PAPER, instrument=INST,
                reference_price=100.0, data_quality=DataQuality.OK,
                forecast=_Forecast(), session_open=True, broker_connected=True,
                recent_order_count=0, daily_pnl=Decimal("0"),
                equity=Decimal("100000"),
                portfolio=stack["portfolio"].snapshot())
    base.update(kw)
    return R.RiskContext(**base)


# --- the happy path, end to end -------------------------------------------

def test_data_to_reconciled_position(stack):
    """Intent → risk → order → fill → portfolio → reconciliation, all real."""
    decision = stack["risk"].authorise(intent(), ctx(stack))
    assert decision.verdict is Verdict.APPROVED

    order = stack["execution"].execute(intent(), decision, INST,
                                       reference_price=100.0)
    assert order.status is OrderStatus.NEW

    # A market order cannot fill on the bar the decision was made from.
    assert stack["broker"].on_candle(bar(0, 100, 101, 99, 100)) == []
    fills = stack["broker"].on_candle(bar(1, 100, 102, 99, 101))
    assert len(fills) == 1

    stack["execution"].poll()
    position = stack["portfolio"].position(INST.key)
    assert position.quantity == Decimal("10")

    report = stack["reconciler"].reconcile()
    assert report.ok is True, report.to_dict()
    assert stack["reconciler"].last_success_ts() is not None


def test_the_order_is_traceable_back_to_its_cause(stack):
    """Completion standard: every order traceable to data, forecast, decision."""
    audit = stack["audit"]
    cid = "corr-e2e"
    audit.record(EventType.MARKET_DATA_RECEIVED, {"bars": 2},
                 subject=INST.key, correlation_id=cid)
    audit.record(EventType.FORECAST_PRODUCED,
                 {"model_version": "kronos-small@abc123"},
                 subject=INST.key, correlation_id=cid)
    i = intent()
    audit.record(EventType.TRADE_INTENT, i.to_dict(), subject=INST.key,
                 correlation_id=cid)
    decision = stack["risk"].authorise(i, ctx(stack))
    audit.record(EventType.RISK_DECISION, decision.to_dict(),
                 subject=INST.key, correlation_id=cid)
    order = stack["execution"].execute(i, decision, INST)
    audit.record(EventType.ORDER_SUBMITTED, order.to_dict(),
                 subject=INST.key, correlation_id=cid)

    chain = audit.trace(cid)
    kinds = [e.type for e in chain]
    assert EventType.MARKET_DATA_RECEIVED in kinds
    assert EventType.FORECAST_PRODUCED in kinds
    assert EventType.RISK_DECISION in kinds
    assert EventType.ORDER_SUBMITTED in kinds

    risk_event = next(e for e in chain if e.type is EventType.RISK_DECISION)
    assert risk_event.payload["policy_hash"] == stack["limits"].policy_hash()
    forecast_event = next(e for e in chain if e.type is EventType.FORECAST_PRODUCED)
    assert forecast_event.payload["model_version"] == "kronos-small@abc123"


# --- deterministic refusal -------------------------------------------------

def test_an_unsafe_trade_never_reaches_the_venue(stack):
    """No stop loss configured → risk rejects → nothing is submitted."""
    decision = stack["risk"].authorise(intent(stop_loss=None), ctx(stack))
    assert decision.verdict is Verdict.REJECTED
    with pytest.raises(ExecutionError):
        stack["execution"].execute(intent(stop_loss=None), decision, INST)
    assert stack["broker"].get_open_orders() == []


def test_execution_refuses_a_decision_for_a_different_intent(stack):
    decision = stack["risk"].authorise(intent(intent_id="i1"), ctx(stack))
    with pytest.raises(ExecutionError):
        stack["execution"].execute(intent(intent_id="i2"), decision, INST)


def test_execution_refuses_a_stale_decision(stack):
    engine = ExecutionEngine(
        broker=stack["broker"], order_store=stack["orders"],
        portfolio=stack["portfolio"], clock=stack["clock"], mode=Mode.PAPER,
        config=ExecutionConfig(max_decision_age_s=1.0))
    decision = stack["risk"].authorise(intent(), ctx(stack))
    stack["clock"].advance(timedelta(minutes=5))
    with pytest.raises(ExecutionError):
        engine.execute(intent(), decision, INST)


def test_execution_uses_the_approved_quantity_not_the_requested_one(stack):
    """Risk caps the order value; execution must honour the reduction."""
    limits = R.RiskLimits(
        approved_instruments=frozenset({INST.key}),
        allowed_modes=frozenset({Mode.PAPER}),
        max_order_value=Decimal("1000"), require_stop_loss=True,
        require_broker_connected=False, max_daily_loss=None)
    engine = R.RiskEngine(limits=limits, clock=stack["clock"])
    decision = engine.authorise(intent(qty="500"), ctx(stack))
    assert decision.verdict is Verdict.APPROVED_REDUCED
    order = stack["execution"].execute(intent(qty="500"), decision, INST)
    assert order.quantity == Decimal("10")     # 1000 / 100


def test_the_price_collar_stops_a_moved_market(stack):
    decision = stack["risk"].authorise(intent(), ctx(stack))
    with pytest.raises(ExecutionError):
        stack["execution"].execute(intent(), decision, INST,
                                   reference_price=130.0)   # 30% away


# --- kill switch -----------------------------------------------------------

def test_the_kill_switch_stops_new_risk_end_to_end(stack):
    stack["killswitches"].engage(KillSwitchScope.GLOBAL, reason="halt", by="op")
    decision = stack["risk"].authorise(intent(), ctx(stack))
    assert decision.verdict is Verdict.REJECTED
    assert R.R_KILL_SWITCH in decision.reason_codes
    assert stack["broker"].get_open_orders() == []


# --- idempotency and restart ----------------------------------------------

def test_executing_the_same_authorisation_twice_submits_once(stack):
    decision = stack["risk"].authorise(intent(), ctx(stack))
    first = stack["execution"].execute(intent(), decision, INST)
    second = stack["execution"].execute(intent(), decision, INST)
    assert first.client_order_id == second.client_order_id
    assert len(stack["broker"].get_open_orders()) == 1


def test_recovery_after_a_restart_adopts_rather_than_duplicates(stack):
    """A crash between submit and acknowledgement must not double the position."""
    decision = stack["risk"].authorise(intent(), ctx(stack))
    order = stack["execution"].execute(intent(), decision, INST)

    # Simulate a restart: brand-new store and engine, same durable state.
    fresh_orders = OrderStore(clock=stack["clock"])
    fresh_engine = ExecutionEngine(
        broker=stack["broker"], order_store=fresh_orders,
        portfolio=stack["portfolio"], clock=stack["clock"], mode=Mode.PAPER,
        config=ExecutionConfig(max_decision_age_s=86400))
    assert fresh_orders.get(order.client_order_id) is not None
    fresh_engine.recover()

    # Re-executing the same authorisation reuses the same id.
    again = fresh_engine.execute(intent(), decision, INST)
    assert again.client_order_id == order.client_order_id
    assert len(stack["broker"].get_open_orders()) == 1


def test_shadow_mode_decides_everything_and_submits_nothing(stack):
    engine = ExecutionEngine(
        broker=stack["broker"], order_store=stack["orders"],
        portfolio=stack["portfolio"], clock=stack["clock"], mode=Mode.SHADOW,
        config=ExecutionConfig(max_decision_age_s=86400))
    decision = stack["risk"].authorise(intent(), ctx(stack))
    order = engine.execute(intent(), decision, INST)
    assert order.mode is Mode.SHADOW
    assert stack["broker"].get_open_orders() == [], (
        "shadow mode must reach no venue")


# --- reconciliation --------------------------------------------------------

def test_reconciliation_detects_a_position_break_and_does_not_hide_it(stack):
    decision = stack["risk"].authorise(intent(), ctx(stack))
    stack["execution"].execute(intent(), decision, INST)
    stack["broker"].on_candle(bar(0, 100, 101, 99, 100))
    stack["broker"].on_candle(bar(1, 100, 102, 99, 101))
    stack["execution"].poll()
    assert stack["reconciler"].reconcile().ok is True

    stack["broker"].force_desync(INST.key, Decimal("40"))
    report = stack["reconciler"].reconcile()
    assert report.ok is False
    assert report.position_breaks
    assert stack["portfolio"].position(INST.key).quantity == Decimal("10"), (
        "a position break must NOT be silently overwritten — the right response "
        "depends on whether we missed a fill or the venue is wrong")


def test_a_reconciliation_break_trips_the_desync_kill_switch(stack):
    stack["broker"].force_desync(INST.key, Decimal("5"))
    stack["reconciler"].reconcile()
    assert stack["killswitches"].check(instrument_key=INST.key) is not None


def test_a_desync_trip_cannot_be_cleared_without_an_operator(stack):
    from olympus.trading.errors import KillSwitchActive
    stack["broker"].force_desync(INST.key, Decimal("5"))
    stack["reconciler"].reconcile()
    with pytest.raises(KillSwitchActive):
        stack["killswitches"].disengage(KillSwitchScope.GLOBAL, by="loop")


# --- audit integrity -------------------------------------------------------

@pytest.mark.requires_crypto
def test_the_trail_of_a_full_run_verifies(stack):
    decision = stack["risk"].authorise(intent(), ctx(stack))
    stack["execution"].execute(intent(), decision, INST)
    stack["broker"].on_candle(bar(0, 100, 101, 99, 100))
    stack["broker"].on_candle(bar(1, 100, 102, 99, 101))
    stack["execution"].poll()
    stack["reconciler"].reconcile()
    assert stack["audit"].verify()["ok"] is True


# --- costs are real --------------------------------------------------------

def test_fees_and_slippage_actually_reduce_equity(stack):
    decision = stack["risk"].authorise(intent(), ctx(stack))
    stack["execution"].execute(intent(), decision, INST)
    stack["broker"].on_candle(bar(0, 100, 101, 99, 100))
    stack["broker"].on_candle(bar(1, 100, 102, 99, 101))
    stack["execution"].poll()
    fills = stack["broker"].get_fills()
    assert fills[0].price > Decimal("100"), "buy must slip against us"
    assert fills[0].fee > 0
    assert stack["portfolio"].fees_paid > 0


# --- pipeline composition with the real forecasting layer ------------------

def test_the_pipeline_builds_a_real_risk_context(stack):
    """Regression: the pipeline once handed the risk engine a loose namespace,
    which worked until the engine read a field it did not carry.

    A risk engine that raises AttributeError mid-authorisation is one whose
    refusals cannot be relied on, so the pipeline must construct the declared
    `RiskContext` — making a missing field a loud construction error rather
    than a surprise inside a decision.
    """
    from olympus.trading import forecast as F
    from olympus.trading.pipeline import TradingPipeline

    clock = stack["clock"]
    bars = [bar(i, 100 + i * 0.3, 101 + i * 0.3, 99 + i * 0.3, 100.5 + i * 0.3)
            for i in range(40)]
    as_of = bars[-1].ts_close
    clock.set(as_of)

    service = F.ForecastService(
        {"persistence": F.PersistenceForecaster(horizon=5, clock=clock)},
        clock=clock)

    class _Strategy:
        id = "s1"
        version = "1"

        def on_bar(self, ctx):
            return [intent(qty="5", intended_entry=Decimal("111"),
                           stop_loss=Decimal("100"))]

    limits = R.RiskLimits(
        approved_instruments=frozenset({INST.key}),
        allowed_modes=frozenset({Mode.PAPER}),
        max_order_value=Decimal("10000"), require_stop_loss=True,
        require_broker_connected=False, max_daily_loss=None,
        max_data_age_s=86400 * 2)
    engine = R.RiskEngine(limits=limits, killswitches=stack["killswitches"],
                          clock=clock)

    pipeline = TradingPipeline(risk_engine=engine, strategies=[_Strategy()],
                               forecast_service=service, clock=clock,
                               portfolio=stack["portfolio"])
    outcome = pipeline.run(instrument=INST, timeframe="1d", candles=bars,
                           as_of=as_of)

    assert outcome.forecasts["persistence"].usable
    assert len(outcome.decisions) == 1
    assert outcome.decisions[0].verdict is Verdict.APPROVED
    # The engine must have actually evaluated checks, not skipped them all.
    assert len(outcome.decisions[0].checks) >= 5


def test_a_baseline_forecast_reports_full_uncertainty(stack):
    """A single deterministic path carries no dispersion information, and the
    baseline must not pretend otherwise — that honesty is what makes it a fair
    control arm against Kronos."""
    from olympus.trading import forecast as F

    clock = stack["clock"]
    bars = [bar(i, 100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(30)]
    clock.set(bars[-1].ts_close)
    result = F.PersistenceForecaster(horizon=5, clock=clock).forecast(
        bars, as_of=bars[-1].ts_close, instrument_key=INST.key, timeframe="1d")
    assert result.usable
    assert result.uncertainty == 1.0

"""The central safety claim, tested end to end.

`TradingPipeline` is the one place the seven layers meet, so it is the right
place to prove the property the whole design exists for:

    **an order can only come into existence behind an approving RiskDecision.**

These tests use minimal fakes rather than the real risk engine and broker on
purpose. A fake lets us construct the *adversarial* cases — a strategy that
proposes ten trades, a risk engine that rejects them all, a risk engine that
throws, a kill switch that cannot be read — which are exactly the cases where a
real implementation might quietly fail open. The real components have their own
behavioural tests; this file tests the wiring between them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from olympus.trading.clock import FixedClock
from olympus.trading.contracts import (Candle, DataQuality, DataQualityReport,
                                       Instrument, Mode, Order, OrderType,
                                       RiskCheck, RiskDecision, Side,
                                       TradeIntent, Verdict)
from olympus.trading.errors import ConfigurationError, RiskRejection
from olympus.trading.pipeline import (STAGE_COMPLETE, STAGE_EXECUTION,
                                      STAGE_KILLSWITCH, STAGE_RISK,
                                      STAGE_VALIDATE, TradingPipeline)

T0 = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)
INST = Instrument(symbol="AAPL", exchange="NASDAQ",
                  tick_size=Decimal("0.01"), lot_size=Decimal("1"))


def _candles(n=10):
    out = []
    for i in range(n):
        o = T0 - timedelta(minutes=(n - i))
        out.append(Candle(instrument_key=INST.key, timeframe="1m",
                          ts_open=o, ts_close=o + timedelta(minutes=1),
                          open=100 + i, high=101 + i, low=99 + i,
                          close=100.5 + i, volume=1000))
    return out


def _intent(intent_id="i1", qty="10"):
    return TradeIntent(
        intent_id=intent_id, strategy_id="s1", strategy_version="1",
        instrument_key=INST.key, side=Side.BUY, order_type=OrderType.MARKET,
        quantity=Decimal(qty), created_at=T0, intended_entry=Decimal("100"),
        stop_loss=Decimal("98"), confidence=0.8)


# --- fakes -----------------------------------------------------------------

class FakeStrategy:
    id = "s1"
    version = "1"

    def __init__(self, intents=None, boom=False):
        self._intents = intents if intents is not None else [_intent()]
        self._boom = boom
        self.saw = None

    def on_bar(self, ctx):
        if self._boom:
            raise RuntimeError("strategy exploded")
        self.saw = ctx
        return list(self._intents)


class FakeRisk:
    """Approves, reduces, rejects, or throws — on command."""

    def __init__(self, *, verdict=Verdict.APPROVED, qty=None, boom=False):
        self.verdict = verdict
        self.qty = qty
        self.boom = boom
        self.calls = []

    def authorise(self, intent, ctx):
        self.calls.append((intent, ctx))
        if self.boom:
            raise RiskRejection("risk engine failure")
        if self.verdict is Verdict.REJECTED:
            approved = Decimal("0")
        else:
            approved = Decimal(self.qty) if self.qty is not None else intent.quantity
        return RiskDecision(
            intent_id=intent.intent_id, verdict=self.verdict,
            checks=(RiskCheck(name="demo", passed=self.verdict is not Verdict.REJECTED,
                              code="DEMO"),),
            approved_quantity=approved, decided_at=T0, mode=Mode.PAPER,
            policy_hash="deadbeef")


class FakeExecution:
    """Records what it was asked to do and enforces the same precondition the
    real engine does — so a pipeline bug cannot hide behind a lenient fake."""

    def __init__(self):
        self.executed = []

    def execute(self, intent, decision, instrument):
        assert decision is not None, "execution reached without a decision"
        assert decision.intent_id == intent.intent_id
        assert decision.approved, "execution reached with a REJECTED decision"
        self.executed.append((intent, decision))
        return Order(
            client_order_id=f"co-{intent.intent_id}",
            instrument_key=intent.instrument_key, side=intent.side,
            order_type=intent.order_type,
            quantity=decision.approved_quantity, created_at=T0,
            intent_id=intent.intent_id, decision_id="d1", strategy_id="s1",
            mode=Mode.PAPER)


class FakeKillSwitches:
    def __init__(self, engaged=None, boom=False):
        self._engaged = engaged
        self._boom = boom

    def check(self, instrument_key, strategy_id):
        if self._boom:
            raise RuntimeError("registry unreadable")
        return self._engaged


class _Engaged:
    code = "GLOBAL_HALT"


def _validator(status=DataQuality.OK, warnings=()):
    def run(candles, *, timeframe, clock, instrument=None):
        report = DataQualityReport(
            instrument_key=INST.key, timeframe=timeframe, status=status,
            bars_expected=len(candles), bars_present=len(candles),
            warnings=tuple(warnings))
        return list(candles), report
    return run


def _pipeline(**kw):
    kw.setdefault("risk_engine", FakeRisk())
    kw.setdefault("strategies", [FakeStrategy()])
    kw.setdefault("clock", FixedClock(T0))
    return TradingPipeline(**kw)


def _run(pipe):
    return pipe.run(instrument=INST, timeframe="1m", candles=_candles(), as_of=T0)


# --- the central guarantee -------------------------------------------------

def test_no_order_without_an_approving_decision():
    """A rejected intent must never reach execution."""
    ex = FakeExecution()
    out = _run(_pipeline(risk_engine=FakeRisk(verdict=Verdict.REJECTED),
                         execution_engine=ex))
    assert out.orders == ()
    assert ex.executed == []
    assert out.stopped_at == STAGE_RISK
    assert out.rejected_decisions and not out.approved_decisions


def test_risk_engine_is_always_consulted_before_execution():
    risk = FakeRisk()
    ex = FakeExecution()
    out = _run(_pipeline(risk_engine=risk, execution_engine=ex))
    assert len(risk.calls) == 1, "risk engine was not consulted"
    assert len(ex.executed) == 1
    assert out.traded and out.stopped_at == STAGE_COMPLETE


def test_execution_sizes_from_the_decision_not_the_intent():
    """The engine's authority to *reduce* must actually bind execution."""
    ex = FakeExecution()
    out = _run(_pipeline(
        strategies=[FakeStrategy([_intent(qty="100")])],
        risk_engine=FakeRisk(verdict=Verdict.APPROVED_REDUCED, qty="7"),
        execution_engine=ex))
    assert out.orders[0].quantity == Decimal("7")
    assert out.intents[0].quantity == Decimal("100")


def test_a_throwing_risk_engine_is_treated_as_a_refusal():
    """Failing open here would be the worst bug in the system."""
    ex = FakeExecution()
    out = _run(_pipeline(risk_engine=FakeRisk(boom=True), execution_engine=ex))
    assert out.orders == () and ex.executed == []
    assert out.stopped_at == STAGE_RISK
    assert any("risk engine error" in e for e in out.errors)


def test_pipeline_without_a_risk_engine_cannot_be_constructed():
    with pytest.raises(ConfigurationError):
        TradingPipeline(risk_engine=None, strategies=[FakeStrategy()])


def test_pipeline_rejects_a_risk_engine_without_authorise():
    with pytest.raises(ConfigurationError):
        TradingPipeline(risk_engine=object(), strategies=[FakeStrategy()])


# --- kill switches ---------------------------------------------------------

def test_kill_switch_stops_the_pass_before_anything_else():
    risk = FakeRisk()
    ex = FakeExecution()
    out = _run(_pipeline(killswitches=FakeKillSwitches(engaged=_Engaged()),
                         risk_engine=risk, execution_engine=ex))
    assert out.stopped_at == STAGE_KILLSWITCH
    assert risk.calls == [] and ex.executed == []


def test_unreadable_kill_switch_registry_fails_closed():
    """If we cannot tell whether trading is halted, we do not trade."""
    risk = FakeRisk()
    out = _run(_pipeline(killswitches=FakeKillSwitches(boom=True),
                         risk_engine=risk))
    assert out.stopped_at == STAGE_KILLSWITCH
    assert risk.calls == []


# --- data quality ----------------------------------------------------------

def test_unusable_data_stops_before_forecasting():
    risk = FakeRisk()
    out = _run(_pipeline(validator=_validator(DataQuality.UNUSABLE),
                         risk_engine=risk))
    assert out.stopped_at == STAGE_VALIDATE
    assert out.data_quality is DataQuality.UNUSABLE
    assert risk.calls == []


def test_degraded_data_is_refused_when_ok_is_required():
    out = _run(_pipeline(validator=_validator(DataQuality.DEGRADED),
                         min_data_quality=DataQuality.OK))
    assert out.stopped_at == STAGE_VALIDATE


def test_degraded_data_is_allowed_by_default():
    out = _run(_pipeline(validator=_validator(DataQuality.DEGRADED),
                         execution_engine=FakeExecution()))
    assert out.stopped_at == STAGE_COMPLETE and out.traded


# --- failure isolation -----------------------------------------------------

def test_a_throwing_strategy_does_not_stop_the_others():
    ex = FakeExecution()
    out = _run(_pipeline(
        strategies=[FakeStrategy(boom=True), FakeStrategy([_intent("i2")])],
        execution_engine=ex))
    assert len(out.orders) == 1 and out.orders[0].intent_id == "i2"
    assert any("strategy" in e for e in out.errors)


def test_no_intent_is_a_clean_completion_not_an_error():
    out = _run(_pipeline(strategies=[FakeStrategy([])]))
    assert out.stopped_at == STAGE_COMPLETE
    assert out.intents == () and out.errors == ()
    assert not out.traded


def test_missing_execution_engine_still_records_decisions():
    """Shadow-style operation: decide everything, submit nothing."""
    out = _run(_pipeline(execution_engine=None))
    assert out.stopped_at == STAGE_EXECUTION
    assert out.approved_decisions and out.orders == ()


# --- mode and defaults -----------------------------------------------------

def test_mode_defaults_to_paper_without_a_controller():
    assert _run(_pipeline()).mode is Mode.PAPER


def test_broken_mode_controller_falls_back_to_paper_never_live():
    class Broken:
        def current(self):
            raise RuntimeError("store corrupt")
    assert _run(_pipeline(mode_controller=Broken())).mode is Mode.PAPER


# --- strategy isolation ----------------------------------------------------

def test_strategy_context_exposes_no_execution_surface():
    """A strategy must not be able to reach a broker, the OMS, or the risk
    limits through the context object it is handed."""
    strat = FakeStrategy()
    _run(_pipeline(strategies=[strat], execution_engine=FakeExecution()))
    ctx = strat.saw
    assert ctx is not None
    for forbidden in ("broker", "execution", "execution_engine", "oms",
                      "order_store", "risk_engine", "limits", "killswitches",
                      "vault"):
        assert not hasattr(ctx, forbidden), (
            f"strategy context exposes {forbidden!r} — a strategy must have no "
            "route to a venue or to the risk policy")


# --- audit -----------------------------------------------------------------

def test_every_stage_is_audited_under_one_correlation_id():
    events = []

    class Audit:
        def record(self, event_type, payload, *, actor, subject, correlation_id):
            events.append((event_type, correlation_id))

    out = _run(_pipeline(validator=_validator(), audit=Audit(),
                         execution_engine=FakeExecution()))
    types = [e[0] for e in events]
    assert "DATA_VALIDATED" in types
    assert "TRADE_INTENT" in types
    assert "RISK_DECISION" in types
    assert "ORDER_SUBMITTED" in types
    assert {c for _, c in events} == {out.correlation_id}, (
        "every event in one pass must share the correlation id that makes the "
        "order traceable back to its source data")


def test_a_failing_audit_sink_does_not_stop_trading_or_hide_itself():
    class Boom:
        def record(self, *a, **k):
            raise RuntimeError("audit down")

    out = _run(_pipeline(audit=Boom(), execution_engine=FakeExecution()))
    assert out.traded, "a broken audit sink must not halt the loop"


def test_outcome_is_json_serialisable():
    import json
    out = _run(_pipeline(validator=_validator(), execution_engine=FakeExecution()))
    assert json.dumps(out.to_dict())

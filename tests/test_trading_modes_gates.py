"""Operating modes and the deployment gates guarding live trading.

The property under test is that live is *unreachable* except through the one
door: every gate green, a valid operator token, a named operator, and an
immutable audit event. Each test removes exactly one of those and asserts the
door stays shut.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from olympus.trading.audit import AuditTrail, EventType
from olympus.trading.brokers import PaperBroker
from olympus.trading.clock import FixedClock
from olympus.trading.contracts import Instrument, Mode, OrderType
from olympus.trading.errors import GateFailure, ModeNotPermitted
from olympus.trading.killswitch import KillSwitchRegistry
from olympus.trading import modes as M
from olympus.trading import risk as R

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
INST = Instrument(symbol="AAPL", exchange="NASDAQ")
TOKEN = "operator-token-1"


class _Reconciler:
    def __init__(self, age=10.0):
        self._age = age

    def age_seconds(self, now=None):
        return self._age


class _Portfolio:
    def __init__(self, n=50):
        self.trades = list(range(n))


def _limits():
    return R.RiskLimits(
        approved_instruments=frozenset({INST.key}),
        approved_exchanges=frozenset({"NASDAQ"}),
        approved_order_types=frozenset({OrderType.MARKET}),
        allowed_modes=frozenset({Mode.PAPER, Mode.LIVE_RESTRICTED}),
        max_order_value=Decimal("1000"), max_daily_loss=Decimal("500"),
        require_broker_connected=True)


@pytest.fixture()
def ctx():
    clock = FixedClock(T0)
    audit = AuditTrail("modes", clock=clock)
    audit.record(EventType.MODE_CHANGED, {"bootstrap": True})
    broker = PaperBroker(clock=clock, instruments={INST.key: INST},
                         starting_cash=Decimal("100000"))
    broker.connect()
    return {
        "limits": _limits(),
        "broker": broker,
        "killswitches": KillSwitchRegistry(clock=clock),
        "reconciler": _Reconciler(),
        "audit": audit,
        "portfolio": _Portfolio(),
        "max_reconciliation_age_s": 3600.0,
        "_clock": clock,
    }


def _controller(ctx, token=TOKEN):
    return M.ModeController(clock=ctx["_clock"], audit=ctx["audit"],
                            operator_token=token)


# --- the default -----------------------------------------------------------

def test_a_fresh_install_is_in_paper_mode():
    assert M.ModeController(clock=FixedClock(T0)).current() is Mode.PAPER


def test_a_fresh_install_does_not_permit_live_orders():
    assert M.ModeController(clock=FixedClock(T0)).permits_live_orders() is False


def test_an_unreadable_mode_record_is_read_as_paper(ctx):
    """A corrupt store must never be interpreted as permission to trade."""
    controller = _controller(ctx)
    from olympus import store
    store.backend().put(M._NS, "current", b"{not json")
    assert controller.current() is Mode.PAPER


# --- safe modes need nothing ----------------------------------------------

@pytest.mark.parametrize("mode", [Mode.BACKTEST, Mode.PAPER, Mode.SHADOW])
def test_safe_modes_need_no_gates_or_token(ctx, mode):
    controller = M.ModeController(clock=ctx["_clock"], audit=ctx["audit"])
    assert controller.request(mode) is mode
    assert controller.current() is mode


# --- live is guarded -------------------------------------------------------

def test_live_is_granted_when_every_gate_passes(ctx):
    controller = _controller(ctx)
    result = controller.request(Mode.LIVE_RESTRICTED, operator="alice",
                                reason="go live", operator_token=TOKEN,
                                gate_context=ctx)
    assert result is Mode.LIVE_RESTRICTED
    assert controller.permits_live_orders() is True


def test_live_is_refused_without_an_operator_token(ctx):
    controller = M.ModeController(clock=ctx["_clock"], audit=ctx["audit"])
    with pytest.raises(ModeNotPermitted):
        controller.request(Mode.LIVE_RESTRICTED, operator="alice",
                           gate_context=ctx)
    assert controller.current() is Mode.PAPER


def test_live_is_refused_with_a_wrong_token(ctx):
    with pytest.raises(ModeNotPermitted):
        _controller(ctx).request(Mode.LIVE_RESTRICTED, operator="alice",
                                 operator_token="guess", gate_context=ctx)


def test_live_is_refused_without_a_named_operator(ctx):
    with pytest.raises(ModeNotPermitted):
        _controller(ctx).request(Mode.LIVE_RESTRICTED, operator="",
                                 operator_token=TOKEN, gate_context=ctx)


@pytest.mark.parametrize("removed", [
    "broker", "killswitches", "reconciler", "audit", "portfolio", "limits"])
def test_live_is_refused_when_any_single_gate_input_is_missing(ctx, removed):
    """Each collaborator backs at least one gate; losing any one shuts the door."""
    controller = _controller(ctx)
    broken = dict(ctx)
    broken[removed] = None
    with pytest.raises(GateFailure):
        controller.request(Mode.LIVE_RESTRICTED, operator="alice",
                           operator_token=TOKEN, gate_context=broken)
    assert controller.current() is Mode.PAPER


def test_connectivity_alone_is_not_enough(ctx):
    """The specific failure this design targets: 'the API works, so ship it.'"""
    broken = dict(ctx)
    broken["reconciler"] = _Reconciler(age=None)      # never reconciled
    assert M.BrokerConnectivityGate().check(broken).passed is True
    with pytest.raises(GateFailure) as excinfo:
        _controller(ctx).request(Mode.LIVE_RESTRICTED, operator="alice",
                                 operator_token=TOKEN, gate_context=broken)
    assert "reconciliation_recent" in excinfo.value.details["failed"]


def test_default_risk_limits_do_not_pass_the_gate(ctx):
    """Shipping the defaults unchanged is not a deliberate decision."""
    broken = dict(ctx)
    broken["limits"] = R.RiskLimits.conservative()
    assert M.RiskLimitsConfiguredGate().check(broken).passed is False


def test_stale_reconciliation_fails_its_gate(ctx):
    broken = dict(ctx)
    broken["reconciler"] = _Reconciler(age=99999.0)
    assert M.ReconciliationGate().check(broken).passed is False


def test_insufficient_paper_history_fails_its_gate(ctx):
    broken = dict(ctx)
    broken["portfolio"] = _Portfolio(n=2)
    assert M.PaperTradingHistoryGate(minimum_trades=20).check(broken).passed is False


def test_a_disconnected_broker_fails_its_gate(ctx):
    ctx["broker"].simulate_outage(True)
    assert M.BrokerConnectivityGate().check(ctx).passed is False


def test_an_unverifiable_audit_trail_fails_its_gate(ctx):
    class Broken:
        def verify(self):
            return {"ok": False, "problems": ["invalid signature at seq 3"]}
    broken = dict(ctx, audit=Broken())
    assert M.AuditTrailVerifiedGate().check(broken).passed is False


# --- the kill-switch gate actually exercises the switch -------------------

def test_the_kill_switch_gate_engages_and_disengages_for_real(ctx):
    from olympus.trading.killswitch import KillSwitchScope
    result = M.KillSwitchFunctionalGate().check(ctx)
    assert result.passed is True
    # and it cleaned up after itself
    assert ctx["killswitches"].is_engaged(
        KillSwitchScope.INSTRUMENT, "__gate_probe__") is False


def test_the_kill_switch_gate_fails_when_engaging_has_no_effect(ctx):
    class Inert:
        def is_engaged(self, *a, **k):
            return False
        def engage(self, *a, **k):
            return None
        def disengage(self, *a, **k):
            return None
        def check(self, *a, **k):
            return None
    assert M.KillSwitchFunctionalGate().check(
        dict(ctx, killswitches=Inert())).passed is False


# --- gate reporting --------------------------------------------------------

def test_gates_never_short_circuit(ctx):
    """An operator needs the whole picture, not the first failure."""
    results = M.run_gates(M.DEFAULT_GATES, dict(ctx, broker=None))
    assert len(results) == len(M.DEFAULT_GATES)
    assert any(not r.passed for r in results)


def test_a_raising_gate_is_a_failure_not_a_crash(ctx):
    class Exploding(M.Gate):
        name = "exploding"
        def check(self, c):
            raise RuntimeError("boom")
    results = M.run_gates([Exploding()], ctx)
    assert results[0].passed is False and "boom" in results[0].detail


# --- audit and downgrade ---------------------------------------------------

def test_going_live_writes_an_audit_event(ctx):
    _controller(ctx).request(Mode.LIVE_RESTRICTED, operator="alice",
                             reason="approved by risk committee",
                             operator_token=TOKEN, gate_context=ctx)
    events = ctx["audit"].events(type=EventType.MODE_CHANGED)
    latest = events[-1]
    assert latest.payload["mode"] == "live_restricted"
    assert latest.payload["operator"] == "alice"
    assert latest.payload["gates"], "the gate evidence must be recorded"


def test_stopping_is_never_gated(ctx):
    controller = _controller(ctx)
    controller.request(Mode.LIVE_RESTRICTED, operator="alice",
                       operator_token=TOKEN, gate_context=ctx)
    assert controller.disable_live() is Mode.PAPER
    assert controller.permits_live_orders() is False


def test_mode_survives_a_restart(ctx):
    _controller(ctx).request(Mode.SHADOW)
    fresh = M.ModeController(clock=FixedClock(T0 + timedelta(hours=1)))
    assert fresh.current() is Mode.SHADOW

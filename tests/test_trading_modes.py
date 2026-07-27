"""Operating modes: proving that live trading is hard to reach on purpose.

The interesting property is negative — that nothing *except* the intended door
opens live — so most of these tests are attempts to get live by other means: a
fresh install, a hand-written store record, a partial set of gate results, an
agent-supplied token, a restart. Each one must end in PAPER or an exception.

The positive path is tested too, because a safety mechanism nobody can satisfy
gets disabled rather than respected.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from olympus import store
from olympus.trading import modes as M
from olympus.trading import risk as R
from olympus.trading.audit import AuditTrail, EventType
from olympus.trading.brokers import PaperBroker
from olympus.trading.clock import FixedClock
from olympus.trading.contracts import Instrument, Mode, OrderType
from olympus.trading.errors import GateFailure, ModeNotPermitted
from olympus.trading.killswitch import KillSwitchRegistry, KillSwitchScope

T0 = datetime(2026, 5, 1, 13, 0, tzinfo=timezone.utc)
INST = Instrument(symbol="AAPL", exchange="NASDAQ")
TOKEN = "held-only-by-the-operator"


class _Reconciler:
    def __init__(self, age: float | None = 30.0):
        self._age = age

    def age_seconds(self, now=None):
        return self._age


class _Portfolio:
    def __init__(self, n: int = 40):
        self.trades = list(range(n))


def _limits():
    """Deliberately not the shipped defaults — the defaults fail their gate."""
    return R.RiskLimits(
        approved_instruments=frozenset({INST.key}),
        approved_exchanges=frozenset({"NASDAQ"}),
        approved_order_types=frozenset({OrderType.MARKET}),
        allowed_modes=frozenset({Mode.PAPER, Mode.LIVE_RESTRICTED}),
        max_order_value=Decimal("2500"), max_daily_loss=Decimal("400"),
        require_broker_connected=True)


@pytest.fixture()
def clock():
    return FixedClock(T0)


@pytest.fixture()
def ctx(clock):
    audit = AuditTrail("modes-run", clock=clock)
    audit.record(EventType.CONFIG_CHANGED, {"bootstrap": True})
    broker = PaperBroker(clock=clock, instruments={INST.key: INST},
                         starting_cash=Decimal("250000"))
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
    return M.ModeController(ctx["_clock"], ctx["audit"], operator_token=token)


def _go_live(controller, ctx, mode=Mode.LIVE_RESTRICTED, operator="alice"):
    return controller.request(mode, operator=operator, reason="risk sign-off",
                              operator_token=TOKEN, gate_context=ctx)


# --- the default is PAPER, in every reading of "default" --------------------

def test_a_fresh_store_is_paper(clock):
    assert M.ModeController(clock).current() is Mode.PAPER


def test_a_fresh_store_does_not_permit_live_orders(clock):
    assert M.ModeController(clock).permits_live_orders() is False


@pytest.mark.parametrize("blob", [
    b"", b"{not json", b"[]", b"null", b'{"mode": "ultra_live"}', b'{"x": 1}'])
def test_every_unreadable_mode_record_reads_as_paper(clock, blob):
    """There is no parse failure, missing field, or unknown enum value that
    resolves to permission to trade."""
    store.backend().put(M._NS, "current", blob)
    assert M.ModeController(clock).current() is Mode.PAPER


# --- live is not reachable by writing a file --------------------------------

def test_a_hand_written_live_record_is_ignored(clock):
    """Anything that can write a file — a config generator, a botched deploy,
    an agent with filesystem access — must not thereby be able to go live."""
    store.backend().put(M._NS, "current",
                        json.dumps({"mode": "live_bounded"}).encode("utf-8"))
    controller = M.ModeController(clock, operator_token=TOKEN)
    assert controller.current() is Mode.PAPER
    assert controller.permits_live_orders() is False


def test_a_forged_live_record_without_gate_evidence_is_ignored(clock):
    """Forging the *shape* of a real transition is not enough: the record must
    carry passing gate results, which only a real gate run produces."""
    store.backend().put(M._NS, "current", json.dumps({
        "mode": "live_restricted", "operator": "mallory", "gates": []},
    ).encode("utf-8"))
    assert M.ModeController(clock, operator_token=TOKEN).current() is Mode.PAPER


def test_a_forged_live_record_with_failing_gates_is_ignored(clock):
    store.backend().put(M._NS, "current", json.dumps({
        "mode": "live_restricted", "operator": "mallory",
        "gates": [{"name": "config_valid", "passed": False}]},
    ).encode("utf-8"))
    assert M.ModeController(clock, operator_token=TOKEN).current() is Mode.PAPER


# --- safe modes are free ----------------------------------------------------

@pytest.mark.parametrize("mode", [Mode.BACKTEST, Mode.PAPER, Mode.SHADOW])
def test_non_live_modes_need_no_gates_and_no_token(ctx, mode):
    controller = M.ModeController(ctx["_clock"], ctx["audit"])
    assert controller.request(mode) is mode
    assert controller.current() is mode
    assert controller.permits_live_orders() is False


# --- the one door that does open --------------------------------------------

def test_live_opens_when_every_condition_is_met(ctx):
    controller = _controller(ctx)
    assert _go_live(controller, ctx) is Mode.LIVE_RESTRICTED
    assert controller.permits_live_orders() is True


def test_the_transition_is_recorded_with_its_evidence(ctx):
    _go_live(_controller(ctx), ctx)
    event = ctx["audit"].events(type=EventType.MODE_CHANGED)[-1]
    assert event.payload["mode"] == "live_restricted"
    assert event.payload["previous"] == "paper"
    assert event.payload["operator"] == "alice"
    assert event.payload["reason"] == "risk sign-off"
    names = {g["name"] for g in event.payload["gates"]}
    assert names == {g.name for g in M.DEFAULT_GATES}
    assert all(g["passed"] for g in event.payload["gates"])


def test_the_recorded_transition_is_itself_tamper_evident(ctx):
    """The mode change lands in the immutable trail, not just a log line."""
    _go_live(_controller(ctx), ctx)
    assert ctx["audit"].verify()["ok"] is True


# --- and everything that must keep it shut ----------------------------------

def test_live_is_refused_when_no_operator_token_is_configured(ctx):
    """A process started without a token cannot go live however green the
    gates are — the token is constructor state, not configuration."""
    controller = M.ModeController(ctx["_clock"], ctx["audit"])
    with pytest.raises(ModeNotPermitted):
        controller.request(Mode.LIVE_RESTRICTED, operator="alice",
                           operator_token=TOKEN, gate_context=ctx)
    assert controller.current() is Mode.PAPER


def test_live_is_refused_when_the_presented_token_is_wrong(ctx):
    controller = _controller(ctx)
    with pytest.raises(ModeNotPermitted):
        controller.request(Mode.LIVE_RESTRICTED, operator="alice",
                           operator_token="guessed", gate_context=ctx)
    assert controller.current() is Mode.PAPER


def test_live_is_refused_when_no_token_is_presented_at_all(ctx):
    with pytest.raises(ModeNotPermitted):
        _controller(ctx).request(Mode.LIVE_RESTRICTED, operator="alice",
                                 gate_context=ctx)


def test_live_is_refused_without_a_named_operator(ctx):
    """Someone must be accountable by name; "system" going live is nobody."""
    with pytest.raises(ModeNotPermitted):
        _controller(ctx).request(Mode.LIVE_RESTRICTED, operator="",
                                 operator_token=TOKEN, gate_context=ctx)


def test_live_is_refused_without_an_audit_trail(ctx):
    """A transition that cannot be recorded must not happen: a system unable to
    say when it went live cannot be audited afterwards."""
    controller = M.ModeController(ctx["_clock"], None, operator_token=TOKEN)
    with pytest.raises(ModeNotPermitted):
        _go_live(controller, ctx)
    assert controller.current() is Mode.PAPER


@pytest.mark.parametrize("gate_name,removed", [
    ("config_valid", "limits"),
    ("account_verified", "broker"),
    ("kill_switch_functional", "killswitches"),
    ("reconciliation_recent", "reconciler"),
    ("audit_trail_verified", "audit"),
    ("paper_trading_history", "portfolio"),
])
def test_any_single_failing_gate_shuts_the_door(ctx, gate_name, removed):
    controller = _controller(ctx)
    broken = dict(ctx, **{removed: None})
    with pytest.raises(GateFailure) as excinfo:
        controller.request(Mode.LIVE_RESTRICTED, operator="alice",
                           operator_token=TOKEN, gate_context=broken)
    assert gate_name in excinfo.value.details["failed"]
    assert controller.current() is Mode.PAPER


def test_a_working_api_connection_is_not_sufficient(ctx):
    """The exact belief this module is built against: "the broker connects, so
    we're ready". Connectivity is one gate; being able to say what you hold is
    another, and it is the one that fails here."""
    broken = dict(ctx, reconciler=_Reconciler(age=None))
    assert M.BrokerConnectivityGate().check(broken).passed is True
    assert M.AccountVerifiedGate().check(broken).passed is True
    with pytest.raises(GateFailure) as excinfo:
        _controller(ctx).request(Mode.LIVE_RESTRICTED, operator="alice",
                                 operator_token=TOKEN, gate_context=broken)
    assert "reconciliation_recent" in excinfo.value.details["failed"]


# --- presented gate evidence ------------------------------------------------

def test_pre_run_gate_results_can_authorise_live(ctx):
    """Gates may be slow or need credentials the controller does not hold, so
    an operator can run them and present the output."""
    results = M.run_gates(M.DEFAULT_GATES, ctx)
    assert all(r.passed for r in results)
    controller = _controller(ctx)
    assert controller.request(Mode.LIVE_RESTRICTED, operator="alice",
                              operator_token=TOKEN,
                              gate_results=results) is Mode.LIVE_RESTRICTED


def test_partial_gate_evidence_is_refused(ctx):
    """Otherwise "submit only the gates that pass" would be a working strategy."""
    results = M.run_gates(M.DEFAULT_GATES, ctx)
    with pytest.raises(GateFailure) as excinfo:
        _controller(ctx).request(Mode.LIVE_RESTRICTED, operator="alice",
                                 operator_token=TOKEN,
                                 gate_results=results[:2])
    assert excinfo.value.details["failed"]


def test_fabricated_passing_results_do_not_cover_the_missing_gates(ctx):
    forged = [M.GateResult("config_valid", True, "trust me")]
    with pytest.raises(GateFailure):
        _controller(ctx).request(Mode.LIVE_RESTRICTED, operator="alice",
                                 operator_token=TOKEN, gate_results=forged)


def test_presented_results_containing_a_failure_are_refused(ctx):
    results = list(M.run_gates(M.DEFAULT_GATES, ctx))
    results[0] = M.GateResult(results[0].name, False, "regressed since")
    with pytest.raises(GateFailure):
        _controller(ctx).request(Mode.LIVE_RESTRICTED, operator="alice",
                                 operator_token=TOKEN, gate_results=results)


# --- the kill-switch gate must actually pull the lever ----------------------

class _RecordingRegistry:
    """Wraps a real registry and records what the gate genuinely did to it."""

    def __init__(self, inner):
        self.inner = inner
        self.calls: list[str] = []
        self.was_engaged_mid_check = False

    def is_engaged(self, scope, key):
        return self.inner.is_engaged(scope, key)

    def engage(self, scope, key, **kwargs):
        self.calls.append("engage")
        result = self.inner.engage(scope, key, **kwargs)
        self.was_engaged_mid_check = self.inner.is_engaged(scope, key)
        return result

    def disengage(self, scope, key, **kwargs):
        self.calls.append("disengage")
        return self.inner.disengage(scope, key, **kwargs)

    def check(self, **kwargs):
        self.calls.append("check")
        return self.inner.check(**kwargs)


def test_the_kill_switch_gate_really_engages_and_disengages(ctx):
    """A gate that only checks a switch is *configured* passes on a system that
    cannot stop itself, which is the whole failure it is meant to catch."""
    registry = _RecordingRegistry(ctx["killswitches"])
    result = M.KillSwitchFunctionalGate().check(dict(ctx, killswitches=registry))
    assert result.passed is True
    assert registry.calls.count("engage") == 1
    assert registry.calls.count("disengage") == 1
    assert registry.was_engaged_mid_check is True    # the switch really tripped


def test_the_kill_switch_gate_leaves_no_probe_engaged(ctx):
    """A gate with a side effect on the running system would be its own hazard."""
    M.KillSwitchFunctionalGate().check(ctx)
    assert ctx["killswitches"].is_engaged(
        KillSwitchScope.INSTRUMENT, "__gate_probe__") is False


def test_a_switch_that_does_nothing_fails_the_gate(ctx):
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


def test_a_switch_that_will_not_release_fails_the_gate(ctx):
    """Stuck-on is as broken as stuck-off: it would halt trading permanently."""
    class Sticky:
        def is_engaged(self, *a, **k):
            return True

        def engage(self, *a, **k):
            return None

        def disengage(self, *a, **k):
            return None

        def check(self, **k):
            return "engaged"

    assert M.KillSwitchFunctionalGate().check(
        dict(ctx, killswitches=Sticky())).passed is False


# --- gate reporting ---------------------------------------------------------

def test_gates_never_short_circuit(ctx):
    """An operator fixing a deployment needs the whole list, not the first
    failure followed by another hour-long round trip."""
    results = M.run_gates(M.DEFAULT_GATES, dict(ctx, broker=None, portfolio=None))
    assert [r.name for r in results] == [g.name for g in M.DEFAULT_GATES]
    assert sum(1 for r in results if not r.passed) >= 3


def test_a_gate_that_raises_is_a_failure_not_a_crash(ctx):
    class Exploding(M.Gate):
        name = "exploding"

        def check(self, c):
            raise RuntimeError("kaboom")

    result = M.run_gates([Exploding()], ctx)[0]
    assert result.passed is False
    assert "kaboom" in result.detail


def test_gate_results_serialise_for_the_audit_record(ctx):
    for result in M.run_gates(M.DEFAULT_GATES, ctx):
        json.dumps(result.to_dict())                 # must not raise


def test_paper_history_gate_requires_real_experience(ctx):
    assert M.PaperTradingHistoryGate(20).check(
        dict(ctx, portfolio=_Portfolio(3))).passed is False
    assert M.PaperTradingHistoryGate(20).check(ctx).passed is True


def test_shipped_default_limits_are_not_a_decision(ctx):
    assert M.RiskLimitsConfiguredGate().check(
        dict(ctx, limits=R.RiskLimits.conservative())).passed is False


# --- stopping, and persistence ---------------------------------------------

def test_stopping_is_never_gated(ctx):
    """Friction belongs on the way in. Needing green gates to *stop* trading
    would be a safety inversion."""
    controller = _controller(ctx)
    _go_live(controller, ctx)
    ctx["broker"].simulate_outage(True)              # everything is now broken
    assert controller.disable_live() is Mode.PAPER
    assert controller.permits_live_orders() is False


def test_a_downgrade_between_live_modes_needs_no_token(ctx):
    controller = _controller(ctx)
    _go_live(controller, ctx, mode=Mode.LIVE_BOUNDED)
    assert controller.request(Mode.LIVE_RESTRICTED,
                              operator="bob") is Mode.LIVE_RESTRICTED


def test_a_non_live_mode_survives_a_restart(ctx):
    _controller(ctx).request(Mode.SHADOW, operator="alice")
    fresh = M.ModeController(FixedClock(T0 + timedelta(hours=6)))
    assert fresh.current() is Mode.SHADOW


def test_live_survives_a_restart_that_still_holds_the_token(ctx):
    _go_live(_controller(ctx), ctx)
    restarted = M.ModeController(FixedClock(T0 + timedelta(hours=6)),
                                 ctx["audit"], operator_token=TOKEN)
    assert restarted.current() is Mode.LIVE_RESTRICTED


def test_live_does_not_survive_a_restart_without_the_token(ctx):
    """A process restarted without its operator token comes back up in PAPER.
    Erring towards not trading is the correct direction to be wrong in."""
    _go_live(_controller(ctx), ctx)
    restarted = M.ModeController(FixedClock(T0 + timedelta(hours=6)),
                                 ctx["audit"])
    assert restarted.current() is Mode.PAPER
    assert restarted.permits_live_orders() is False


def test_the_stored_record_explains_the_current_mode(ctx):
    controller = _controller(ctx)
    _go_live(controller, ctx)
    record = controller.record()
    assert record["operator"] == "alice"
    assert record["previous"] == "paper"
    assert record["at"] == T0.isoformat()

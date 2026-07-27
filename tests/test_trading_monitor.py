"""Tests for the monitor — written against the property that matters most.

The monitor is the component whose failure is invisible. A broker adapter that
raises stops trading; a risk engine that raises rejects an intent; both fail
closed and somebody notices. A monitor that raises fails *silent*: the system
keeps running with nothing watching it.

So the majority of these tests are hostile fakes. Every collaborator has a
mode where its method explodes, and the assertion is always the same shape —
`heartbeat()` returned, the failing probe is `DEGRADED` and says why, and the
*other* probes still reported. A monitor that aborts on its first failing probe
reports nothing about the nine measurements that would have halted the system.

The remaining tests cover the other half of the contract: measurement and
action are separate, and the automatic trips engage the right switch with
`auto=True` (which is what makes them need an operator override to clear).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from olympus.trading.clock import FixedClock
from olympus.trading.contracts import Mode
from olympus.trading.killswitch import (CODE_DAILY_LOSS, CODE_ORDER_RATE,
                                        CODE_PORTFOLIO_DRAWDOWN,
                                        CODE_STALE_DATA, KillSwitchRegistry,
                                        KillSwitchScope)
from olympus.trading.monitor import (Alert, AlertLevel, HealthStatus,
                                     MonitorReport, TradingMonitor, worst)
from olympus.trading.risk import RiskLimits

T0 = datetime(2026, 3, 2, 14, 0, tzinfo=timezone.utc)
INST = "NASDAQ:ACME"

BOOM = "deliberate probe explosion"


class Boom(Exception):
    pass


# --- hostile fakes ---------------------------------------------------------
# Each takes an `explode` set naming the methods that raise. That shape lets a
# single test parametrise over "which subsystem is broken today" rather than
# writing ten near-identical fakes.

class FakePortfolio:
    def __init__(self, *, daily_pnl=Decimal("50"), drawdown=Decimal("0"),
                 peak=Decimal("10000"), positions=(), explode=()):
        self._daily_pnl = daily_pnl
        self._drawdown = drawdown
        self._peak = peak
        self._positions = {k: object() for k in positions}
        self.explode = set(explode)
        # Read by AutoTripRules' portfolio fallbacks when a stat is absent.
        self.realised_pnl = Decimal("0")
        self.unrealised_pnl = Decimal("0")
        self.fees_paid = Decimal("0")

    def _check(self, name):
        if name in self.explode:
            raise Boom(BOOM)

    def daily_pnl(self, now=None):
        self._check("daily_pnl")
        return self._daily_pnl

    def drawdown(self):
        self._check("drawdown")
        return self._drawdown

    def drawdown_fraction(self):
        self._check("drawdown")
        return Decimal("0")

    def peak_equity(self):
        self._check("drawdown")
        return self._peak

    def open_positions(self):
        self._check("open_positions")
        return dict(self._positions)

    def strategy_pnl(self, strategy_id):
        self._check("strategy_pnl")
        return {"drawdown": Decimal("0")}

    def equity(self, ts=None):
        return self._peak


class FakeOrder:
    def __init__(self, created_at):
        self.created_at = created_at


class FakeOMS:
    def __init__(self, orders=(), *, explode=False):
        self._orders = list(orders)
        self.explode = explode

    def all_orders(self):
        if self.explode:
            raise Boom(BOOM)
        return list(self._orders)


class FakeBroker:
    def __init__(self, *, connected=True, explode=False):
        self.connected = connected
        self.explode = explode

    def health(self):
        if self.explode:
            raise Boom(BOOM)
        return {"ok": self.connected, "name": "fake"}

    def is_connected(self):
        return self.connected


class FakeReconciler:
    def __init__(self, *, age=30.0, ok=True, explode=False):
        self._age = age
        self._ok = ok
        self.explode = explode

    def age_seconds(self, now=None):
        if self.explode:
            raise Boom(BOOM)
        return self._age

    def last_report(self):
        return {"ok": self._ok}


class FakeAudit:
    def __init__(self, *, errors=0, explode=False):
        self._errors = errors
        self.explode = explode
        self.records: list[tuple] = []

    def events(self, *, type=None, since=None, correlation_id=None,
               subject=None):
        if self.explode:
            raise Boom(BOOM)
        return [object()] * self._errors

    def record(self, event_type, payload=None, *, actor="", subject="",
               correlation_id=""):
        self.records.append((event_type, subject))


class FakeModeController:
    def __init__(self, mode=Mode.PAPER, *, explode=False):
        self._mode = mode
        self.explode = explode

    def current(self):
        if self.explode:
            raise Boom(BOOM)
        return self._mode


def _limits(**overrides) -> RiskLimits:
    """Only the fields the probes read, so a failure points at one probe."""
    base = dict(allowed_modes=frozenset({Mode.PAPER}), require_stop_loss=False,
                max_daily_loss=Decimal("200"),
                max_portfolio_drawdown=Decimal("1000"),
                max_orders_per_minute=10, max_data_age_s=300.0,
                max_reconciliation_age_s=3600.0, require_reconciliation=False,
                max_open_positions=5)
    base.update(overrides)
    return RiskLimits(**base)


def _monitor(**overrides) -> TradingMonitor:
    clock = overrides.pop("clock", None) or FixedClock(T0)
    kw = dict(clock=clock, portfolio=FakePortfolio(), oms=FakeOMS(),
              broker=FakeBroker(), reconciler=FakeReconciler(),
              killswitches=KillSwitchRegistry(clock=clock),
              risk_limits=_limits(), audit=FakeAudit(),
              mode_controller=FakeModeController(),
              data_ts=lambda: T0 - timedelta(seconds=10))
    kw.update(overrides)
    return TradingMonitor(**kw)


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------

def test_healthy_system_reports_ok_with_every_probe_present():
    report = _monitor().heartbeat()
    assert report.status is HealthStatus.OK
    assert report.ok and not report.halted
    assert report.alerts == ()
    assert report.probe_errors == ()
    names = {c.name for c in report.checks}
    assert names == {"data_freshness", "broker_connectivity", "reconciliation",
                     "order_rate", "error_rate", "daily_pnl", "drawdown",
                     "open_positions", "kill_switches", "mode"}
    assert report.mode is Mode.PAPER


def test_report_is_json_serialisable_whole():
    import json
    json.dumps(_monitor().heartbeat().to_dict())


def test_metrics_use_the_key_names_the_trip_rules_read():
    """The contract between measuring and stopping. A probe that measures
    under a different name is a rule that silently never fires."""
    metrics = _monitor().heartbeat().metrics
    for key in ("data_age_s", "orders_last_minute", "reconciliation_age_s",
                "daily_pnl", "portfolio_drawdown", "open_positions"):
        assert key in metrics, key


# ---------------------------------------------------------------------------
# never raises
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("broken,probe", [
    ({"portfolio": FakePortfolio(explode={"daily_pnl"})}, "daily_pnl"),
    ({"portfolio": FakePortfolio(explode={"drawdown"})}, "drawdown"),
    ({"portfolio": FakePortfolio(explode={"open_positions"})}, "open_positions"),
    ({"oms": FakeOMS(explode=True)}, "order_rate"),
    ({"broker": FakeBroker(explode=True)}, "broker_connectivity"),
    ({"reconciler": FakeReconciler(explode=True)}, "reconciliation"),
    ({"audit": FakeAudit(explode=True)}, "error_rate"),
    ({"mode_controller": FakeModeController(explode=True)}, "mode"),
])
def test_a_probe_that_explodes_degrades_rather_than_raising(broken, probe):
    monitor = _monitor(**broken)
    report = monitor.heartbeat()                       # must not raise
    failed = report.check(probe)
    assert failed.status is HealthStatus.DEGRADED
    assert "probe failed" in failed.detail and BOOM in failed.detail
    assert any(probe in e for e in report.probe_errors)
    # The whole point: every OTHER probe still ran.
    assert len(report.checks) == 10
    assert report.status is not HealthStatus.OK


def test_a_data_source_that_explodes_degrades():
    def boom():
        raise Boom(BOOM)
    report = _monitor(data_ts=boom).heartbeat()
    assert report.check("data_freshness").status is HealthStatus.DEGRADED


def test_every_collaborator_broken_still_produces_a_report():
    """The worst case: nothing works. The monitor must still say so, because
    an exception here means nobody learns anything at all."""
    monitor = _monitor(
        portfolio=FakePortfolio(explode={"daily_pnl", "drawdown",
                                         "open_positions"}),
        oms=FakeOMS(explode=True), broker=FakeBroker(explode=True),
        reconciler=FakeReconciler(explode=True), audit=FakeAudit(explode=True),
        mode_controller=FakeModeController(explode=True))
    report = monitor.heartbeat()
    assert isinstance(report, MonitorReport)
    assert report.status is not HealthStatus.OK
    assert len(report.probe_errors) >= 7


def test_a_broken_clock_still_produces_a_report():
    class BrokenClock:
        def now(self):
            raise Boom(BOOM)

        def monotonic_ns(self):
            return 0

    report = TradingMonitor(clock=BrokenClock()).heartbeat()
    assert isinstance(report, MonitorReport)
    assert any("clock" in e for e in report.probe_errors)


def test_a_broken_limits_store_degrades_rather_than_raising():
    class BrokenLimits:
        def load(self):
            raise Boom(BOOM)

    report = _monitor(risk_limits=BrokenLimits()).heartbeat()
    assert report.status is not HealthStatus.OK
    assert any("risk_limits" in e for e in report.probe_errors)


def test_a_fully_unwired_monitor_reports_instead_of_raising():
    """A partially initialised system is exactly when a monitor is most
    needed, so a missing collaborator is a DEGRADED check, not a refusal."""
    report = TradingMonitor(clock=FixedClock(T0)).heartbeat()
    assert report.status is HealthStatus.FAILED      # no kill switch at all
    assert all(c.status is not HealthStatus.OK
               for c in report.checks if c.name != "mode")


def test_alerts_never_raise_even_with_everything_broken():
    monitor = _monitor(broker=FakeBroker(explode=True), oms=FakeOMS(explode=True))
    alerts = monitor.alerts()
    assert all(isinstance(a, Alert) for a in alerts)
    assert any(a.code == "MONITOR_PROBE_FAILED" for a in alerts)


def test_stats_never_raise():
    assert isinstance(_monitor(oms=FakeOMS(explode=True)).stats(), dict)


# ---------------------------------------------------------------------------
# what the probes actually judge
# ---------------------------------------------------------------------------

def test_stale_data_fails_the_freshness_probe():
    report = _monitor(data_ts=lambda: T0 - timedelta(seconds=900)).heartbeat()
    check = report.check("data_freshness")
    assert check.status is HealthStatus.FAILED
    assert check.observed == pytest.approx(900.0)
    assert report.metrics["data_age_s"] == pytest.approx(900.0)


def test_missing_data_is_degraded_not_ok():
    """'We could not measure it' and 'it was fine' must never be the same
    answer — the same rule the risk engine enforces."""
    report = _monitor(data_ts=lambda: None).heartbeat()
    assert report.check("data_freshness").status is HealthStatus.DEGRADED
    assert "data_age_s" not in report.metrics


def test_disconnected_broker_is_a_failure_not_a_warning():
    report = _monitor(broker=FakeBroker(connected=False)).heartbeat()
    assert report.check("broker_connectivity").status is HealthStatus.FAILED


def test_never_reconciled_is_failed_when_reconciliation_is_required():
    monitor = _monitor(reconciler=FakeReconciler(age=None),
                       risk_limits=_limits(require_reconciliation=True))
    check = monitor.heartbeat().check("reconciliation")
    assert check.status is HealthStatus.FAILED
    assert "never succeeded" in check.detail


def test_order_rate_counts_only_the_last_minute():
    orders = [FakeOrder(T0 - timedelta(seconds=5)),
              FakeOrder(T0 - timedelta(seconds=30)),
              FakeOrder(T0 - timedelta(seconds=600))]
    report = _monitor(oms=FakeOMS(orders)).heartbeat()
    assert report.metrics["orders_last_minute"] == 2
    assert report.check("order_rate").status is HealthStatus.OK


def test_daily_pnl_uses_a_signed_comparison():
    """Comparing the magnitude would flag a profitable day and train operators
    to ignore the alert."""
    good = _monitor(portfolio=FakePortfolio(daily_pnl=Decimal("900"))).heartbeat()
    assert good.check("daily_pnl").status is HealthStatus.OK
    bad = _monitor(portfolio=FakePortfolio(daily_pnl=Decimal("-900"))).heartbeat()
    assert bad.check("daily_pnl").status is HealthStatus.FAILED


def test_too_many_open_positions_degrades_rather_than_failing():
    """Halting here would block the closing trades that reduce the count."""
    monitor = _monitor(portfolio=FakePortfolio(
        positions=("a", "b", "c", "d", "e", "f")))
    assert monitor.heartbeat().check("open_positions").status is HealthStatus.DEGRADED


def test_error_rate_degrades_above_the_warning_threshold():
    report = _monitor(audit=FakeAudit(errors=20)).heartbeat()
    assert report.check("error_rate").status is HealthStatus.DEGRADED


def test_engaged_kill_switch_shows_up_as_failed_and_halted():
    clock = FixedClock(T0)
    registry = KillSwitchRegistry(clock=clock)
    registry.engage(KillSwitchScope.GLOBAL, reason="operator halt")
    report = _monitor(clock=clock, killswitches=registry).heartbeat()
    assert report.check("kill_switches").status is HealthStatus.FAILED
    assert report.halted
    assert report.status is HealthStatus.FAILED


def test_heartbeat_engages_nothing():
    """Measurement and action are separate: a monitoring bug must not be able
    to halt live trading, and a read-only dashboard must be safe to run."""
    clock = FixedClock(T0)
    registry = KillSwitchRegistry(clock=clock)
    monitor = _monitor(clock=clock, killswitches=registry,
                       portfolio=FakePortfolio(daily_pnl=Decimal("-5000")))
    report = monitor.heartbeat()
    assert report.check("daily_pnl").status is HealthStatus.FAILED
    assert KillSwitchRegistry(clock=clock).engaged() == []


# ---------------------------------------------------------------------------
# automatic trips
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("overrides,code,scope", [
    ({"portfolio": FakePortfolio(daily_pnl=Decimal("-5000"))},
     CODE_DAILY_LOSS, KillSwitchScope.GLOBAL),
    ({"portfolio": FakePortfolio(drawdown=Decimal("9000"))},
     CODE_PORTFOLIO_DRAWDOWN, KillSwitchScope.GLOBAL),
    ({"oms": FakeOMS([FakeOrder(T0)] * 50)},
     CODE_ORDER_RATE, KillSwitchScope.GLOBAL),
    ({"data_ts": (lambda: T0 - timedelta(hours=5))},
     CODE_STALE_DATA, KillSwitchScope.GLOBAL),
])
def test_auto_trip_engages_the_right_switch(overrides, code, scope):
    clock = FixedClock(T0)
    registry = KillSwitchRegistry(clock=clock)
    monitor = _monitor(clock=clock, killswitches=registry, **overrides)
    monitor.heartbeat()
    trips = monitor.evaluate_auto_trips()
    assert [t.code for t in trips] == [code]

    # Asserted through a DIFFERENT registry instance: an in-memory switch
    # passes any test that reuses the object that engaged it.
    engaged = KillSwitchRegistry(clock=clock).engaged()
    assert len(engaged) == 1
    assert engaged[0].scope is scope
    assert engaged[0].code == code
    # auto=True is what makes this need an explicit operator override to clear.
    assert engaged[0].auto is True


def test_a_healthy_system_trips_nothing():
    clock = FixedClock(T0)
    registry = KillSwitchRegistry(clock=clock)
    monitor = _monitor(clock=clock, killswitches=registry)
    monitor.heartbeat()
    assert monitor.evaluate_auto_trips() == []
    assert KillSwitchRegistry(clock=clock).engaged() == []


def test_evaluate_can_report_without_applying():
    clock = FixedClock(T0)
    registry = KillSwitchRegistry(clock=clock)
    monitor = _monitor(clock=clock, killswitches=registry,
                       portfolio=FakePortfolio(daily_pnl=Decimal("-5000")))
    monitor.heartbeat()
    trips = monitor.evaluate_auto_trips(apply=False)
    assert [t.code for t in trips] == [CODE_DAILY_LOSS]
    assert KillSwitchRegistry(clock=clock).engaged() == []


def test_evaluation_failure_does_not_raise():
    """A broken rule set must not take the monitor down; it degrades to 'no
    trip from that pass' and records the failure."""
    class BrokenRules:
        def evaluate(self, portfolio=None, stats=None, limits=None):
            raise Boom(BOOM)

    audit = FakeAudit()
    monitor = _monitor(auto_trip_rules=BrokenRules(), audit=audit)
    monitor.heartbeat()
    assert monitor.evaluate_auto_trips() == []
    assert any(subject == "auto_trip_evaluation" for _, subject in audit.records)


def test_a_failure_to_ENGAGE_propagates():
    """The one place the monitor is allowed to raise. Deciding to halt and
    then failing to halt, silently, is strictly worse than crashing."""
    clock = FixedClock(T0)

    class BrokenRegistry(KillSwitchRegistry):
        def engage(self, *a, **kw):
            raise Boom("store is unwritable")

    monitor = _monitor(clock=clock, killswitches=BrokenRegistry(clock=clock),
                       portfolio=FakePortfolio(daily_pnl=Decimal("-5000")))
    monitor.heartbeat()
    with pytest.raises(Boom):
        monitor.evaluate_auto_trips()


def test_trips_without_a_registry_are_reported_not_swallowed():
    audit = FakeAudit()
    monitor = _monitor(killswitches=None, audit=audit,
                       portfolio=FakePortfolio(daily_pnl=Decimal("-5000")))
    monitor.heartbeat()
    trips = monitor.evaluate_auto_trips()
    assert [t.code for t in trips] == [CODE_DAILY_LOSS]
    assert any(subject == "auto_trip_apply" for _, subject in audit.records)


# ---------------------------------------------------------------------------
# alerts and small pieces
# ---------------------------------------------------------------------------

def test_alerts_carry_a_level_matching_the_check_severity():
    report = _monitor(broker=FakeBroker(connected=False),
                      audit=FakeAudit(errors=99)).heartbeat()
    by_code = {a.code: a for a in report.alerts}
    assert by_code["HEALTH_BROKER_CONNECTIVITY"].level is AlertLevel.CRITICAL
    assert by_code["HEALTH_ERROR_RATE"].level is AlertLevel.WARNING
    assert all(a.ts == T0 for a in report.alerts)


def test_alerts_reuses_the_last_report():
    monitor = _monitor()
    report = monitor.heartbeat()
    assert monitor.alerts() == list(report.alerts)
    assert monitor.last_report is report


def test_worst_picks_the_most_severe():
    assert worst([]) is HealthStatus.OK
    assert worst([HealthStatus.OK, HealthStatus.DEGRADED]) is HealthStatus.DEGRADED
    assert worst([HealthStatus.DEGRADED, HealthStatus.FAILED,
                  HealthStatus.OK]) is HealthStatus.FAILED


def test_heartbeats_are_not_written_to_the_audit_trail_by_default():
    """Enabling this unconditionally would file every heartbeat as an ERROR
    event — feeding the very error-rate metric the monitor measures."""
    audit = FakeAudit()
    _monitor(audit=audit).heartbeat()
    assert audit.records == []


def test_heartbeats_are_written_when_opted_in_and_only_when_unhealthy():
    audit = FakeAudit()
    monitor = _monitor(audit=audit, audit_heartbeats=True)
    monitor.heartbeat()
    assert audit.records == []                       # healthy: nothing written
    monitor.broker = FakeBroker(connected=False)
    monitor.heartbeat()
    assert [s for _, s in audit.records] == ["heartbeat"]


def test_alert_requires_an_aware_timestamp():
    with pytest.raises(Exception):
        Alert(level=AlertLevel.INFO, code="X", message="m",
              ts=datetime(2026, 3, 2, 14, 0))

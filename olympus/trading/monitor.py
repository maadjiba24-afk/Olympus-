"""Continuous health monitoring — the component that must never be the one
that breaks.

Everything else in this package can fail loudly and be fine: a broker adapter
that raises stops trading, a risk engine that raises rejects an intent, a
forecaster that raises abstains. All of those fail *closed*. The monitor is
different, and the difference is the whole design constraint here.

The monitor is what notices that data went stale, that the broker went away,
that reconciliation stopped succeeding, that today's losses crossed the line —
and it is what engages the kill switches when they do. A monitor that raises
therefore does not fail closed; it fails *silent*. The system keeps running,
nobody is watching it, and the first evidence is the P&L. So:

**`heartbeat()` and `alerts()` never raise.** Every probe is individually
wrapped. A probe that explodes yields a `DEGRADED` check carrying the exception
text, and the rest of the report is still produced. "I could not measure this"
is reported as its own state, never conflated with "I measured it and it was
fine" — the same distinction `risk.py` enforces for missing measurements, for
the same reason.

The one deliberate exception
----------------------------
`evaluate_auto_trips()` swallows evaluation errors but **propagates a failure
to ENGAGE a switch**. Failing to measure is a degraded state; failing to *stop*
after deciding to stop is the emergency this entire subsystem exists for, and
swallowing it would leave a system that decided to halt and then carried on.
`killswitch.engage` has the same property for the same reason.

Separation of measurement from action
-------------------------------------
`heartbeat()` measures and reports; it engages nothing. `evaluate_auto_trips()`
acts. Fusing them would make the measurement path untestable without a store
and would mean a monitoring bug could halt live trading — a monitor should be
safe to run at any frequency, from anywhere, including a read-only dashboard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Mapping, Sequence

from .clock import Clock, default_clock
from .contracts import Mode, ensure_utc, jsonable
from .killswitch import AutoTripRules, KillSwitchRegistry, Trip

#: Window over which order and error rates are counted. One minute because that
#: is the unit `RiskLimits.max_orders_per_minute` is expressed in — a monitor
#: measuring over a different window than the limit is measured in produces
#: numbers that look like the limit and are not.
RATE_WINDOW_S = 60.0

#: Errors per `RATE_WINDOW_S` above which the system is considered degraded.
#: Not a kill-switch condition: errors are noisy, and halting on noise trains
#: operators to override the halt.
DEFAULT_ERROR_RATE_WARN = 5


class HealthStatus(str, Enum):
    """Three states, and the middle one carries the weight.

    `DEGRADED` means "this is not right, and it is also not proven wrong" —
    which covers both a genuine soft failure and a probe that could not run.
    Collapsing it into OK would hide outages; collapsing it into FAILED would
    make every transient hiccup look like an emergency, and an alert stream
    nobody believes is the same as no alert stream.
    """
    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"


_SEVERITY = {HealthStatus.OK: 0, HealthStatus.DEGRADED: 1, HealthStatus.FAILED: 2}


def worst(statuses: Sequence[HealthStatus]) -> HealthStatus:
    """The most severe status in a set. Empty set is OK (nothing to report)."""
    out = HealthStatus.OK
    for status in statuses:
        if _SEVERITY[status] > _SEVERITY[out]:
            out = status
    return out


class AlertLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


_LEVEL_FOR_STATUS = {
    HealthStatus.OK: AlertLevel.INFO,
    HealthStatus.DEGRADED: AlertLevel.WARNING,
    HealthStatus.FAILED: AlertLevel.CRITICAL,
}


@dataclass(frozen=True)
class HealthCheck:
    """One probe's verdict, with the numbers that produced it.

    `observed` and `limit` are carried separately from `detail` on purpose: an
    operator reading an alert at 3am needs the two numbers, and a prose string
    is where numbers go to become unparseable.
    """
    name: str
    status: HealthStatus
    detail: str = ""
    observed: Any = None
    limit: Any = None

    @property
    def ok(self) -> bool:
        return self.status is HealthStatus.OK

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status.value,
                "detail": self.detail, "observed": jsonable(self.observed),
                "limit": jsonable(self.limit)}


@dataclass(frozen=True)
class Alert:
    level: AlertLevel
    code: str
    message: str
    ts: datetime

    def __post_init__(self):
        object.__setattr__(self, "ts", ensure_utc(self.ts, field_name="ts"))
        object.__setattr__(self, "level", AlertLevel(self.level))

    def to_dict(self) -> dict:
        return {"level": self.level.value, "code": self.code,
                "message": self.message, "ts": jsonable(self.ts)}


@dataclass(frozen=True)
class MonitorReport:
    """One heartbeat, whole. Written to the audit trail as a single record.

    Deliberately a snapshot rather than a stream of separate metrics: an
    incident review needs to know what every probe said *at the same instant*,
    and separately-timestamped metrics cannot answer "was the broker already
    down when the data went stale?".
    """
    ts: datetime
    status: HealthStatus
    checks: tuple[HealthCheck, ...] = ()
    metrics: Mapping[str, Any] = field(default_factory=dict)
    alerts: tuple[Alert, ...] = ()
    kill_switches: tuple[Mapping[str, Any], ...] = ()
    mode: Mode | None = None
    #: Probe failures, verbatim. Kept separate from `checks` so "the monitor
    #: itself is unhealthy" is visible without reading every check.
    probe_errors: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "ts", ensure_utc(self.ts, field_name="ts"))
        object.__setattr__(self, "checks", tuple(self.checks))
        object.__setattr__(self, "alerts", tuple(self.alerts))
        object.__setattr__(self, "kill_switches", tuple(self.kill_switches))
        object.__setattr__(self, "probe_errors", tuple(self.probe_errors))
        object.__setattr__(self, "metrics", dict(self.metrics))

    @property
    def ok(self) -> bool:
        return self.status is HealthStatus.OK

    @property
    def halted(self) -> bool:
        """True when any kill switch is engaged — trading is stopped."""
        return any(s.get("engaged") for s in self.kill_switches)

    def check(self, name: str) -> HealthCheck | None:
        for item in self.checks:
            if item.name == name:
                return item
        return None

    def to_dict(self) -> dict:
        return {"ts": jsonable(self.ts), "status": self.status.value,
                "checks": [c.to_dict() for c in self.checks],
                "metrics": jsonable(dict(self.metrics)),
                "alerts": [a.to_dict() for a in self.alerts],
                "kill_switches": [dict(s) for s in self.kill_switches],
                "mode": self.mode.value if self.mode else None,
                "probe_errors": list(self.probe_errors)}


def _f(value: Any) -> float:
    """Best-effort float. Decimal-aware, because P&L arrives as Decimal and
    comparisons against float limits must not go through `str`."""
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


class TradingMonitor:
    """Observes the running system and, separately, stops it.

    Every collaborator is optional. A monitor that refuses to run because one
    subsystem is not wired is a monitor that is absent exactly when a partially
    initialised system most needs watching; a missing collaborator produces a
    `DEGRADED` check saying so.
    """

    def __init__(self, *, clock: Clock | None = None, portfolio=None, oms=None,
                 broker=None, reconciler=None,
                 killswitches: KillSwitchRegistry | None = None,
                 risk_limits=None, audit=None,
                 mode_controller=None,
                 data_ts: Callable[[], datetime | None] | None = None,
                 auto_trip_rules: AutoTripRules | None = None,
                 error_rate_warn: int = DEFAULT_ERROR_RATE_WARN,
                 strategy_ids: Sequence[str] = (),
                 audit_heartbeats: bool = False):
        self.clock = clock or default_clock()
        self.portfolio = portfolio
        self.oms = oms
        self.broker = broker
        self.reconciler = reconciler
        self.killswitches = killswitches
        #: Either a `RiskLimits` or a `LimitsStore`; resolved per heartbeat so
        #: a limit change made by an operator mid-session is picked up without
        #: restarting the monitor.
        self.risk_limits = risk_limits
        self.audit = audit
        self.mode_controller = mode_controller
        self.data_ts = data_ts
        self.auto_trip_rules = auto_trip_rules or AutoTripRules()
        self.error_rate_warn = int(error_rate_warn)
        self.strategy_ids = tuple(strategy_ids)
        self.audit_heartbeats = bool(audit_heartbeats)
        self._last_report: MonitorReport | None = None

    # -- infrastructure ---------------------------------------------------

    def _limits(self) -> Any:
        """Resolve limits from either a `RiskLimits` or a `LimitsStore`."""
        limits = self.risk_limits
        if limits is None:
            return None
        loader = getattr(limits, "load", None)
        if callable(loader):
            return loader()
        return limits

    @staticmethod
    def _limit(limits: Any, name: str) -> Any:
        return getattr(limits, name, None) if limits is not None else None

    def _run_probe(self, name: str, fn: Callable[[], HealthCheck],
                   errors: list[str]) -> HealthCheck:
        """Call one probe, converting any explosion into a DEGRADED check.

        This is the single most important five lines in the module. Probes read
        stores, sockets, and other processes' state; every one of them can
        raise, and a monitor whose first failing probe aborts the heartbeat
        reports nothing about the other nine — including the ones that would
        have said "engage the kill switch".
        """
        try:
            return fn()
        except BaseException as exc:                     # noqa: BLE001
            # BaseException, not Exception: a probe that raises KeyboardInterrupt
            # or a custom BaseException subclass must not take the safety system
            # down either. The monitor's job is to report, always.
            detail = f"{type(exc).__name__}: {exc}"[:400]
            errors.append(f"{name}: {detail}")
            return HealthCheck(name=name, status=HealthStatus.DEGRADED,
                               detail=f"probe failed: {detail}")

    # -- probes -----------------------------------------------------------
    # Each probe fills `metrics` with the measurement it took, using the exact
    # key names `killswitch.evaluate_auto_trips` reads. That is the contract
    # between measuring and stopping: a probe that measures under a different
    # name is a rule that silently never fires.

    def _probe_data_freshness(self, now: datetime, limits: Any,
                              metrics: dict) -> HealthCheck:
        if self.data_ts is None:
            return HealthCheck("data_freshness", HealthStatus.DEGRADED,
                               "no market-data timestamp source is wired")
        newest = self.data_ts()
        if newest is None:
            return HealthCheck("data_freshness", HealthStatus.DEGRADED,
                               "no market data has been received")
        age = (now - ensure_utc(newest, field_name="data_ts")).total_seconds()
        metrics["data_age_s"] = age
        tolerance = self._limit(limits, "max_data_age_s")
        if tolerance is None:
            return HealthCheck("data_freshness", HealthStatus.OK,
                               "no staleness tolerance configured", age, None)
        status = (HealthStatus.FAILED if age > _f(tolerance)
                  else HealthStatus.OK)
        return HealthCheck("data_freshness", status,
                           f"freshest bar is {age:.1f}s old", age, _f(tolerance))

    def _probe_broker(self, now: datetime, limits: Any,
                      metrics: dict) -> HealthCheck:
        if self.broker is None:
            return HealthCheck("broker_connectivity", HealthStatus.DEGRADED,
                               "no broker is wired")
        health = None
        probe = getattr(self.broker, "health", None)
        if callable(probe):
            health = probe()
        if isinstance(health, Mapping):
            connected = bool(health.get("ok"))
            detail = str(health.get("error", "") or "")
        else:
            connected = bool(self.broker.is_connected())
            detail = ""
        metrics["broker_connected"] = connected
        if connected:
            return HealthCheck("broker_connectivity", HealthStatus.OK,
                               "connected", True, True)
        # FAILED rather than DEGRADED: with no broker there is no way to exit a
        # position, which is the situation an operator must be woken for.
        return HealthCheck("broker_connectivity", HealthStatus.FAILED,
                           detail or "broker reports disconnected", False, True)

    def _probe_reconciliation(self, now: datetime, limits: Any,
                              metrics: dict) -> HealthCheck:
        if self.reconciler is None:
            return HealthCheck("reconciliation", HealthStatus.DEGRADED,
                               "no reconciler is wired")
        age = self.reconciler.age_seconds(now)
        tolerance = self._limit(limits, "max_reconciliation_age_s")
        required = bool(self._limit(limits, "require_reconciliation"))
        metrics["reconciliation_required"] = required
        last = self.reconciler.last_report()
        if isinstance(last, Mapping) and "ok" in last:
            metrics["reconciliation_ok"] = bool(last.get("ok"))
        if age is None:
            # None is not zero. A process that has never reconciled has never
            # verified its own book, and treating that as "just reconciled" is
            # how a freshly restarted system trades against a fictional
            # position.
            status = (HealthStatus.FAILED if required else HealthStatus.DEGRADED)
            return HealthCheck("reconciliation", status,
                               "reconciliation has never succeeded", None,
                               _f(tolerance) if tolerance is not None else None)
        metrics["reconciliation_age_s"] = age
        if tolerance is None:
            return HealthCheck("reconciliation", HealthStatus.OK,
                               f"last success {age:.0f}s ago", age, None)
        status = (HealthStatus.FAILED if age > _f(tolerance)
                  else HealthStatus.OK)
        return HealthCheck("reconciliation", status,
                           f"last success {age:.0f}s ago", age, _f(tolerance))

    def _probe_order_rate(self, now: datetime, limits: Any,
                          metrics: dict) -> HealthCheck:
        if self.oms is None:
            return HealthCheck("order_rate", HealthStatus.DEGRADED,
                               "no order store is wired")
        floor = now - timedelta(seconds=RATE_WINDOW_S)
        recent = 0
        for order in self.oms.all_orders():
            created = getattr(order, "created_at", None)
            if created is not None and ensure_utc(created) >= floor:
                recent += 1
        metrics["orders_last_minute"] = recent
        ceiling = self._limit(limits, "max_orders_per_minute")
        if ceiling is None:
            return HealthCheck("order_rate", HealthStatus.OK,
                               f"{recent} order(s) in the last minute",
                               recent, None)
        status = (HealthStatus.FAILED if recent > _f(ceiling)
                  else HealthStatus.OK)
        return HealthCheck("order_rate", status,
                           f"{recent} order(s) in the last minute",
                           recent, _f(ceiling))

    def _probe_error_rate(self, now: datetime, limits: Any,
                          metrics: dict) -> HealthCheck:
        if self.audit is None:
            return HealthCheck("error_rate", HealthStatus.DEGRADED,
                               "no audit trail is wired")
        from .audit import EventType
        floor = now - timedelta(seconds=RATE_WINDOW_S)
        count = len(self.audit.events(type=EventType.ERROR, since=floor))
        metrics["errors_last_minute"] = count
        status = (HealthStatus.DEGRADED if count > self.error_rate_warn
                  else HealthStatus.OK)
        return HealthCheck("error_rate", status,
                           f"{count} error event(s) in the last minute",
                           count, self.error_rate_warn)

    def _probe_daily_pnl(self, now: datetime, limits: Any,
                         metrics: dict) -> HealthCheck:
        if self.portfolio is None:
            return HealthCheck("daily_pnl", HealthStatus.DEGRADED,
                               "no portfolio is wired")
        pnl = self.portfolio.daily_pnl(now)
        metrics["daily_pnl"] = pnl
        ceiling = self._limit(limits, "max_daily_loss")
        if ceiling is None:
            return HealthCheck("daily_pnl", HealthStatus.OK,
                               f"session P&L {pnl}", pnl, None)
        # Signed comparison. Taking the magnitude would flag a profitable day.
        status = (HealthStatus.FAILED if _f(pnl) <= -_f(ceiling)
                  else HealthStatus.OK)
        return HealthCheck("daily_pnl", status, f"session P&L {pnl}",
                           pnl, ceiling)

    def _probe_drawdown(self, now: datetime, limits: Any,
                        metrics: dict) -> HealthCheck:
        if self.portfolio is None:
            return HealthCheck("drawdown", HealthStatus.DEGRADED,
                               "no portfolio is wired")
        drawdown = self.portfolio.drawdown()
        metrics["portfolio_drawdown"] = drawdown
        metrics["peak_equity"] = self.portfolio.peak_equity()
        fraction = getattr(self.portfolio, "drawdown_fraction", None)
        if callable(fraction):
            metrics["portfolio_drawdown_fraction"] = fraction()
        strategy_drawdowns: dict[str, Any] = {}
        for strategy_id in self.strategy_ids:
            stats = self.portfolio.strategy_pnl(strategy_id)
            if isinstance(stats, Mapping) and "drawdown" in stats:
                strategy_drawdowns[strategy_id] = stats["drawdown"]
        if strategy_drawdowns:
            metrics["strategy_drawdowns"] = strategy_drawdowns
        ceiling = self._limit(limits, "max_portfolio_drawdown")
        if ceiling is None:
            return HealthCheck("drawdown", HealthStatus.OK,
                               f"drawdown {drawdown} from peak", drawdown, None)
        status = (HealthStatus.FAILED if _f(drawdown) >= _f(ceiling)
                  else HealthStatus.OK)
        return HealthCheck("drawdown", status, f"drawdown {drawdown} from peak",
                           drawdown, ceiling)

    def _probe_positions(self, now: datetime, limits: Any,
                         metrics: dict) -> HealthCheck:
        if self.portfolio is None:
            return HealthCheck("open_positions", HealthStatus.DEGRADED,
                               "no portfolio is wired")
        open_positions = self.portfolio.open_positions()
        count = len(open_positions)
        metrics["open_positions"] = count
        metrics["open_position_keys"] = sorted(open_positions)
        ceiling = self._limit(limits, "max_open_positions")
        if ceiling is None:
            return HealthCheck("open_positions", HealthStatus.OK,
                               f"{count} open position(s)", count, None)
        # DEGRADED, not FAILED: the risk engine refuses the *next* opening
        # trade, so being at or over the ceiling is a state to notice rather
        # than an emergency to halt on. Halting here would also block the
        # closing trades that reduce the count.
        status = (HealthStatus.DEGRADED if count > int(ceiling)
                  else HealthStatus.OK)
        return HealthCheck("open_positions", status,
                           f"{count} open position(s)", count, int(ceiling))

    def _probe_kill_switches(self, now: datetime, limits: Any,
                             metrics: dict) -> HealthCheck:
        if self.killswitches is None:
            # Not having a kill switch at all is worse than having an engaged
            # one, hence FAILED: there is no way to stop.
            return HealthCheck("kill_switches", HealthStatus.FAILED,
                               "no kill-switch registry is wired")
        states = self.killswitches.all()
        engaged = [s for s in states if s.engaged]
        metrics["kill_switches_engaged"] = len(engaged)
        if not engaged:
            return HealthCheck("kill_switches", HealthStatus.OK,
                               "no switch engaged", 0, None)
        names = ", ".join(sorted(s.key for s in engaged))
        return HealthCheck("kill_switches", HealthStatus.FAILED,
                           f"engaged: {names}", len(engaged), 0)

    def _probe_mode(self, now: datetime, limits: Any,
                    metrics: dict) -> HealthCheck:
        if self.mode_controller is None:
            return HealthCheck("mode", HealthStatus.OK,
                               "no mode controller wired; assuming non-live")
        mode = self.mode_controller.current()
        metrics["mode"] = mode.value if isinstance(mode, Mode) else str(mode)
        return HealthCheck("mode", HealthStatus.OK, f"operating in {metrics['mode']}",
                           metrics["mode"], None)

    # -- the heartbeat ----------------------------------------------------

    def heartbeat(self) -> MonitorReport:
        """Take every measurement and report. Never raises.

        Safe to call at any frequency and from any thread: it reads, it does
        not write, and it engages nothing.
        """
        errors: list[str] = []
        metrics: dict[str, Any] = {}
        checks: list[HealthCheck] = []
        try:
            now = self.clock.now()
        except BaseException as exc:                     # noqa: BLE001
            # A broken clock is unrecoverable for a time-based report, but the
            # caller still gets a report object rather than an exception.
            from datetime import timezone
            now = datetime.now(timezone.utc)
            errors.append(f"clock: {type(exc).__name__}: {exc}")

        limits = None
        try:
            limits = self._limits()
        except BaseException as exc:                     # noqa: BLE001
            errors.append(f"risk_limits: {type(exc).__name__}: {exc}")

        probes: tuple[tuple[str, Callable[[], HealthCheck]], ...] = (
            ("data_freshness",
             lambda: self._probe_data_freshness(now, limits, metrics)),
            ("broker_connectivity",
             lambda: self._probe_broker(now, limits, metrics)),
            ("reconciliation",
             lambda: self._probe_reconciliation(now, limits, metrics)),
            ("order_rate", lambda: self._probe_order_rate(now, limits, metrics)),
            ("error_rate", lambda: self._probe_error_rate(now, limits, metrics)),
            ("daily_pnl", lambda: self._probe_daily_pnl(now, limits, metrics)),
            ("drawdown", lambda: self._probe_drawdown(now, limits, metrics)),
            ("open_positions",
             lambda: self._probe_positions(now, limits, metrics)),
            ("kill_switches",
             lambda: self._probe_kill_switches(now, limits, metrics)),
            ("mode", lambda: self._probe_mode(now, limits, metrics)),
        )
        for name, fn in probes:
            checks.append(self._run_probe(name, fn, errors))

        switches: tuple[Mapping[str, Any], ...] = ()
        if self.killswitches is not None:
            try:
                switches = tuple(s.to_dict() for s in self.killswitches.all())
            except BaseException as exc:                 # noqa: BLE001
                errors.append(f"kill_switch_dump: {type(exc).__name__}: {exc}")

        mode: Mode | None = None
        if self.mode_controller is not None:
            try:
                candidate = self.mode_controller.current()
                mode = candidate if isinstance(candidate, Mode) else Mode(str(candidate))
            except BaseException as exc:                 # noqa: BLE001
                errors.append(f"mode: {type(exc).__name__}: {exc}")

        status = worst([c.status for c in checks])
        if errors and status is HealthStatus.OK:
            # A report whose probes all "passed" but which could not read its
            # own limits is not healthy.
            status = HealthStatus.DEGRADED

        alerts = self._alerts_for(checks, now, errors)
        report = MonitorReport(ts=now, status=status, checks=tuple(checks),
                               metrics=metrics, alerts=tuple(alerts),
                               kill_switches=switches, mode=mode,
                               probe_errors=tuple(errors))
        self._last_report = report
        self._record(report)
        return report

    def _alerts_for(self, checks: Sequence[HealthCheck], now: datetime,
                    errors: Sequence[str]) -> list[Alert]:
        alerts: list[Alert] = []
        for check in checks:
            if check.status is HealthStatus.OK:
                continue
            alerts.append(Alert(
                level=_LEVEL_FOR_STATUS[check.status],
                code=f"HEALTH_{check.name.upper()}",
                message=f"{check.name}: {check.detail}", ts=now))
        for problem in errors:
            alerts.append(Alert(level=AlertLevel.WARNING, code="MONITOR_PROBE_FAILED",
                                message=problem, ts=now))
        return alerts

    def _record(self, report: MonitorReport) -> None:
        """Best-effort audit write, off by default.

        Two reasons it is opt-in rather than automatic. The trail is
        hash-chained and ledger-backed, so a per-second heartbeat would come to
        dominate it and make the records that matter — decisions, orders, fills
        — harder to find. And `EventType` has no heartbeat member, so an
        unrecognised label is filed as `ERROR`: enabling this unconditionally
        would make the monitor's own heartbeats feed the error-rate metric the
        monitor measures. When it is enabled, only non-OK reports are written,
        which is the subset an incident review actually reads.

        Swallowed by design — an unwritable audit trail must not stop the
        monitor from running; a missing record surfaces in the trail's own
        verification.
        """
        if self.audit is None or not self.audit_heartbeats:
            return
        if report.status is HealthStatus.OK:
            return
        try:
            self.audit.record("error", {"health_report": report.to_dict()},
                              actor="monitor", subject="heartbeat",
                              correlation_id="")
        except BaseException:                            # noqa: BLE001
            pass

    # -- reading ----------------------------------------------------------

    @property
    def last_report(self) -> MonitorReport | None:
        return self._last_report

    def alerts(self) -> list[Alert]:
        """Alerts from the most recent heartbeat, taking one if none exists.

        Never raises: `heartbeat()` does not, and a failure to produce even a
        report yields an empty list rather than an exception into whatever
        notification path called this.
        """
        try:
            report = self._last_report or self.heartbeat()
            return list(report.alerts)
        except BaseException:                            # noqa: BLE001
            return []

    def stats(self) -> dict:
        """The measurement dict `killswitch.evaluate_auto_trips` consumes."""
        try:
            report = self._last_report or self.heartbeat()
            return dict(report.metrics)
        except BaseException:                            # noqa: BLE001
            return {}

    # -- acting -----------------------------------------------------------

    def evaluate_auto_trips(self, *, report: MonitorReport | None = None,
                            apply: bool = True, by: str = "auto-monitor",
                            ) -> list[Trip]:
        """Decide which kill switches should fire, and (by default) fire them.

        Evaluation is defensive — a failure to compute a rule degrades to "no
        trip from that rule" rather than aborting the pass, because one broken
        rule must not stop the other five from halting the system.

        Engaging is NOT defensive. If `registry.engage` raises, that exception
        propagates: the system decided to stop and could not, which is strictly
        worse than the condition that triggered it and must never be a silent
        log line.
        """
        try:
            report = report or self._last_report or self.heartbeat()
            limits = self._limits()
            trips = self.auto_trip_rules.evaluate(
                portfolio=self.portfolio, stats=dict(report.metrics),
                limits=limits)
        except BaseException as exc:                     # noqa: BLE001
            self._note_error("auto_trip_evaluation", exc)
            return []
        if not trips or not apply:
            return trips
        if self.killswitches is None:
            # Nothing to engage with. Loud, because "we decided to halt and
            # have no halt mechanism" is a configuration emergency.
            self._note_error("auto_trip_apply",
                             RuntimeError("trips fired but no kill-switch "
                                          "registry is wired"))
            return trips
        self.auto_trip_rules.apply(self.killswitches, trips, by=by)
        return trips

    def _note_error(self, where: str, exc: BaseException) -> None:
        if self.audit is None:
            return
        try:
            self.audit.record("error",
                              {"where": f"monitor.{where}",
                               "error": f"{type(exc).__name__}: {exc}"},
                              actor="monitor", subject=where, correlation_id="")
        except BaseException:                            # noqa: BLE001
            pass


__all__ = ["HealthStatus", "HealthCheck", "AlertLevel", "Alert",
           "MonitorReport", "TradingMonitor", "worst", "RATE_WINDOW_S",
           "DEFAULT_ERROR_RATE_WARN"]

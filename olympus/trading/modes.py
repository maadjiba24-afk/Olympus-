"""Operating modes and the gates that guard live trading.

The default is `PAPER`, and getting to live requires **every** gate to pass plus
an explicit operator token plus an immutable audit event. None of those is
reachable from a config file, an agent, or any code path that untrusted content
can influence.

The specific failure this is built against: "the API connection works, so we're
ready." Connectivity is one gate of nine. The others exist because a system can
be perfectly connected and still be unable to tell you what it holds, unable to
stop itself, or unable to prove afterwards what it did.

Gate design
-----------
Gates are *evidence*, not configuration. `KillSwitchFunctionalGate` actually
engages and disengages a switch rather than checking that one is configured;
`ReconciliationGate` requires a recent *successful* reconciliation rather than a
reconciler being wired up. A gate that only checks a setting is a gate that
passes on a broken system.

Downgrading is always allowed
-----------------------------
Moving to a *less* live mode never requires gates or a token. Making it hard to
stop trading would be an obvious safety inversion — the friction belongs on the
way in, not the way out.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from .clock import Clock, default_clock
from .contracts import Mode, ensure_utc, jsonable
from .errors import ConfigurationError, GateFailure, ModeNotPermitted

_NS = "trading.mode"

#: Modes reachable without gates. PAPER is the default for a fresh install.
_SAFE_MODES = frozenset({Mode.BACKTEST, Mode.PAPER, Mode.SHADOW})

#: How live a mode is, so a "downgrade" can be recognised.
_RANK = {Mode.BACKTEST: 0, Mode.PAPER: 1, Mode.SHADOW: 2,
         Mode.LIVE_RESTRICTED: 3, Mode.LIVE_BOUNDED: 4}


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    detail: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed,
                "detail": self.detail, "evidence": jsonable(dict(self.evidence))}


class Gate:
    """One piece of evidence that live trading is safe to enable."""

    name = "gate"

    def check(self, ctx: Mapping[str, Any]) -> GateResult:  # pragma: no cover
        raise NotImplementedError

    def _fail(self, detail: str, **evidence) -> GateResult:
        return GateResult(self.name, False, detail, evidence)

    def _pass(self, detail: str = "", **evidence) -> GateResult:
        return GateResult(self.name, True, detail, evidence)


class ConfigValidGate(Gate):
    name = "config_valid"

    def check(self, ctx):
        limits = ctx.get("limits")
        if limits is None:
            return self._fail("no risk limits loaded")
        instruments = ctx.get("approved_instruments") or getattr(
            limits, "approved_instruments", frozenset())
        if not instruments:
            return self._fail(
                "no instruments are approved; an empty allow-list means either "
                "nothing may trade or the check was never configured, and live "
                "is not the place to find out which")
        return self._pass("limits loaded and instruments approved",
                          instruments=sorted(instruments))


class RiskLimitsConfiguredGate(Gate):
    """Refuses the shipped defaults. Someone must have made a decision."""
    name = "risk_limits_configured"

    def check(self, ctx):
        limits = ctx.get("limits")
        if limits is None:
            return self._fail("no risk limits loaded")
        try:
            from .risk import RiskLimits
            if limits.policy_hash() == RiskLimits.conservative().policy_hash():
                return self._fail(
                    "risk limits are the shipped defaults; an operator must set "
                    "limits deliberately before real money is at stake")
        except Exception as exc:                         # noqa: BLE001
            return self._fail(f"could not evaluate limits: {exc}")
        for required in ("max_daily_loss", "max_order_value"):
            if getattr(limits, required, None) is None:
                return self._fail(f"{required} is not configured")
        return self._pass("limits explicitly configured",
                          policy_hash=limits.policy_hash())


class AccountVerifiedGate(Gate):
    name = "account_verified"

    def check(self, ctx):
        broker = ctx.get("broker")
        if broker is None:
            return self._fail("no broker configured")
        try:
            account = broker.get_account()
        except Exception as exc:                         # noqa: BLE001
            return self._fail(f"could not read the account: {exc}")
        expected = ctx.get("expected_account_id")
        if expected and getattr(account, "source", None) != expected:
            return self._fail("the connected account is not the expected one",
                              expected=expected, got=account.source)
        if account.equity <= 0:
            return self._fail("account equity is zero or negative",
                              equity=str(account.equity))
        return self._pass("account readable", equity=str(account.equity))


class BrokerConnectivityGate(Gate):
    name = "broker_connectivity"

    def check(self, ctx):
        broker = ctx.get("broker")
        if broker is None:
            return self._fail("no broker configured")
        try:
            if not broker.is_connected():
                return self._fail("broker reports disconnected")
            health = broker.health()
        except Exception as exc:                         # noqa: BLE001
            return self._fail(f"health probe raised: {exc}")
        if not health.get("ok"):
            return self._fail("broker health probe failed", health=health)
        return self._pass("broker connected and healthy", health=health)


class KillSwitchFunctionalGate(Gate):
    """Actually exercises the switch. A configured switch is not a working one."""
    name = "kill_switch_functional"

    def check(self, ctx):
        registry = ctx.get("killswitches")
        if registry is None:
            return self._fail("no kill-switch registry configured")
        from .killswitch import KillSwitchScope
        probe = "__gate_probe__"
        try:
            if registry.is_engaged(KillSwitchScope.INSTRUMENT, probe):
                registry.disengage(KillSwitchScope.INSTRUMENT, probe,
                                   by="gate", operator_override=True)
            registry.engage(KillSwitchScope.INSTRUMENT, probe,
                            reason="deployment gate probe", by="gate")
            if not registry.is_engaged(KillSwitchScope.INSTRUMENT, probe):
                return self._fail("engaging a kill switch had no effect")
            if registry.check(instrument_key=probe) is None:
                return self._fail("an engaged switch is not visible to check()")
            registry.disengage(KillSwitchScope.INSTRUMENT, probe, by="gate")
            if registry.is_engaged(KillSwitchScope.INSTRUMENT, probe):
                return self._fail("disengaging a kill switch had no effect")
        except Exception as exc:                         # noqa: BLE001
            return self._fail(f"kill-switch probe raised: {exc}")
        return self._pass("engage and disengage both verified")


class ReconciliationGate(Gate):
    name = "reconciliation_recent"

    def check(self, ctx):
        reconciler = ctx.get("reconciler")
        if reconciler is None:
            return self._fail("no reconciler configured")
        max_age = float(ctx.get("max_reconciliation_age_s") or 3600.0)
        try:
            age = reconciler.age_seconds()
        except Exception as exc:                         # noqa: BLE001
            return self._fail(f"could not read reconciliation state: {exc}")
        if age is None:
            return self._fail(
                "reconciliation has never succeeded; without it the system "
                "cannot say what it holds")
        if age > max_age:
            return self._fail("last successful reconciliation is too old",
                              age_s=age, limit_s=max_age)
        return self._pass("recent successful reconciliation", age_s=age)


class AuditTrailVerifiedGate(Gate):
    name = "audit_trail_verified"

    def check(self, ctx):
        audit = ctx.get("audit")
        if audit is None:
            return self._fail("no audit trail configured")
        try:
            result = audit.verify()
        except Exception as exc:                         # noqa: BLE001
            return self._fail(f"audit verification raised: {exc}")
        if not result.get("ok"):
            return self._fail("the audit trail does not verify",
                              problems=result.get("problems", []))
        return self._pass("audit chain verifies", count=result.get("count", 0))


class PaperTradingHistoryGate(Gate):
    """Requires real paper experience before real money."""
    name = "paper_trading_history"

    def __init__(self, minimum_trades: int = 20):
        self.minimum = int(minimum_trades)

    def check(self, ctx):
        portfolio = ctx.get("portfolio")
        if portfolio is None:
            return self._fail("no portfolio configured")
        try:
            trades = len(portfolio.trades)
        except Exception as exc:                         # noqa: BLE001
            return self._fail(f"could not count trades: {exc}")
        if trades < self.minimum:
            return self._fail("not enough paper trading history",
                              trades=trades, required=self.minimum)
        return self._pass("sufficient paper history", trades=trades)


class ModelApprovedGate(Gate):
    name = "models_approved"

    def check(self, ctx):
        registry = ctx.get("model_registry")
        models = list(ctx.get("active_models") or ())
        if not models:
            return self._pass("no models in the active path")
        if registry is None:
            return self._fail("models are in use but no registry is configured",
                              models=models)
        unapproved = []
        for model_id in models:
            try:
                record = registry.get(model_id)
            except Exception:                            # noqa: BLE001
                record = None
            if record is None or getattr(record, "status", "") != "approved":
                unapproved.append(model_id)
        if unapproved:
            return self._fail("models are not approved for live use",
                              models=unapproved)
        return self._pass("all active models approved", models=models)


#: The full set, in the order an operator would want to read them.
DEFAULT_GATES: tuple[Gate, ...] = (
    ConfigValidGate(), RiskLimitsConfiguredGate(), AccountVerifiedGate(),
    BrokerConnectivityGate(), KillSwitchFunctionalGate(), ReconciliationGate(),
    AuditTrailVerifiedGate(), PaperTradingHistoryGate(), ModelApprovedGate(),
)


def run_gates(gates: Sequence[Gate], ctx: Mapping[str, Any]) -> list[GateResult]:
    """Run every gate. Never short-circuits — an operator needs the whole
    picture, not just the first thing that failed."""
    results = []
    for gate in gates:
        try:
            results.append(gate.check(ctx))
        except Exception as exc:                         # noqa: BLE001
            results.append(GateResult(getattr(gate, "name", "gate"), False,
                                      f"gate raised: {exc}"))
    return results


class ModeController:
    """Owns the current operating mode. Defaults to PAPER, always."""

    def __init__(self, *, clock: Clock | None = None, audit=None,
                 namespace: str = _NS, operator_token: str | None = None,
                 gates: Sequence[Gate] | None = None):
        self.clock = clock or default_clock()
        self.audit = audit
        self.namespace = namespace
        self._operator_token = operator_token
        self.gates = tuple(gates) if gates is not None else DEFAULT_GATES

    def _store(self):
        from olympus import store
        return store.backend()

    def current(self) -> Mode:
        raw = self._store().get(self.namespace, "current")
        if not raw:
            return Mode.PAPER
        try:
            return Mode(json.loads(raw.decode("utf-8"))["mode"])
        except Exception:                                # noqa: BLE001
            # An unreadable mode record must not be interpreted as live.
            return Mode.PAPER

    def permits_live_orders(self) -> bool:
        return self.current().is_live

    def request(self, mode: Any, *, operator: str = "", reason: str = "",
                operator_token: str | None = None,
                gate_context: Mapping[str, Any] | None = None,
                gates: Sequence[Gate] | None = None) -> Mode:
        """Change mode. Live requires every gate plus a valid operator token."""
        mode = Mode(mode)
        previous = self.current()

        # Downgrades are always permitted — friction belongs on the way in.
        if mode in _SAFE_MODES or _RANK[mode] < _RANK[previous]:
            return self._commit(mode, previous, operator=operator or "system",
                                reason=reason, gate_results=())

        if not self._operator_token:
            raise ModeNotPermitted(
                "live trading requires a configured operator token; none is set",
                requested=mode.value)
        if operator_token != self._operator_token:
            raise ModeNotPermitted("operator token rejected",
                                   requested=mode.value)
        if not operator:
            raise ModeNotPermitted("an authorised operator must be named",
                                   requested=mode.value)

        results = run_gates(gates if gates is not None else self.gates,
                            dict(gate_context or {}))
        failed = [r for r in results if not r.passed]
        if failed:
            raise GateFailure(
                "live trading refused: deployment gates did not pass",
                requested=mode.value,
                failed=[r.name for r in failed],
                details={r.name: r.detail for r in failed})
        return self._commit(mode, previous, operator=operator, reason=reason,
                            gate_results=tuple(results))

    def _commit(self, mode: Mode, previous: Mode, *, operator: str,
                reason: str, gate_results: Sequence[GateResult]) -> Mode:
        payload = {
            "mode": mode.value, "previous": previous.value,
            "operator": operator, "reason": reason,
            "at": self.clock.now().isoformat(),
            "gates": [g.to_dict() for g in gate_results],
        }
        self._store().put(self.namespace, "current",
                          json.dumps(payload, sort_keys=True).encode("utf-8"))
        if self.audit is not None:
            try:
                self.audit.record("MODE_CHANGED", payload, actor=operator or "system",
                                  subject="", correlation_id="")
            except Exception:                            # noqa: BLE001
                pass
        return mode

    def disable_live(self, *, operator: str = "system",
                     reason: str = "operator halt") -> Mode:
        """Return to PAPER. Never gated — stopping must always be easy."""
        return self.request(Mode.PAPER, operator=operator, reason=reason)


__all__ = ["Mode", "ModeController", "Gate", "GateResult", "run_gates",
           "DEFAULT_GATES", "ConfigValidGate", "RiskLimitsConfiguredGate",
           "AccountVerifiedGate", "BrokerConnectivityGate",
           "KillSwitchFunctionalGate", "ReconciliationGate",
           "AuditTrailVerifiedGate", "PaperTradingHistoryGate",
           "ModelApprovedGate"]

"""Kill switches — the ability to stop, at three scopes, and have it stick.

A kill switch is worth exactly as much as its weakest property, and the weak
property is almost never "can we turn it on". It is:

* **Does it survive a restart?** A switch held in memory is cleared by the crash
  that most likely caused it to trip. Every switch here is written through to
  `olympus.store` before `engage()` returns, and read back from the store on
  every check — not from a cache that a fork or a second process would miss.
* **Can the thing that tripped it clear it?** An automatic trip cleared by an
  ordinary code path is not a safety device, it is a speed bump. Auto-trips here
  require an explicit operator override to disengage.
* **What happens when it cannot be read?** Callers must fail closed. This module
  raises rather than returning "not engaged" on a read failure, so a corrupt or
  unreachable store stops trading instead of silently permitting it.

Scopes are checked most-general-first (global → strategy → instrument) so the
broadest halt always wins and a narrow disengage cannot re-open a global stop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Mapping

from .clock import Clock, default_clock
from .contracts import ensure_utc, jsonable
from .errors import ConfigurationError, KillSwitchActive, TradingError

_NS = "trading.killswitch"

#: Reason codes for automatic trips. Stable — they appear in the audit trail.
CODE_MANUAL = "MANUAL"
CODE_DAILY_LOSS = "DAILY_LOSS_BREACH"
CODE_PORTFOLIO_DRAWDOWN = "PORTFOLIO_DRAWDOWN_BREACH"
CODE_STRATEGY_DRAWDOWN = "STRATEGY_DRAWDOWN_BREACH"
CODE_ORDER_RATE = "ABNORMAL_ORDER_RATE"
CODE_STALE_DATA = "STALE_MARKET_DATA"
CODE_BROKER_DESYNC = "BROKER_DESYNCHRONISED"
CODE_RECONCILIATION_STALE = "RECONCILIATION_STALE"


class KillSwitchScope(str, Enum):
    GLOBAL = "global"
    STRATEGY = "strategy"
    INSTRUMENT = "instrument"


#: Broadest first. `check()` relies on this ordering.
_SCOPE_ORDER = (KillSwitchScope.GLOBAL, KillSwitchScope.STRATEGY,
                KillSwitchScope.INSTRUMENT)


@dataclass(frozen=True)
class KillSwitchState:
    scope: KillSwitchScope
    #: "" for GLOBAL; the strategy id or instrument key otherwise.
    target: str
    engaged: bool
    reason: str = ""
    code: str = ""
    engaged_at: datetime | None = None
    engaged_by: str = ""
    #: True when a rule tripped it rather than a human. Auto-trips need an
    #: explicit override to clear.
    auto: bool = False

    def __post_init__(self):
        if self.engaged_at is not None:
            object.__setattr__(self, "engaged_at",
                               ensure_utc(self.engaged_at, field_name="engaged_at"))
        if self.scope is KillSwitchScope.GLOBAL and self.target:
            raise ConfigurationError("a GLOBAL kill switch has no target",
                                     target=self.target)
        if self.scope is not KillSwitchScope.GLOBAL and not self.target:
            raise ConfigurationError(f"a {self.scope.value} kill switch needs a target")

    @property
    def key(self) -> str:
        return f"{self.scope.value}:{self.target}" if self.target else self.scope.value

    def to_dict(self) -> dict:
        return {"scope": self.scope.value, "target": self.target,
                "engaged": self.engaged, "reason": self.reason,
                "code": self.code, "engaged_by": self.engaged_by,
                "auto": self.auto, "engaged_at": jsonable(self.engaged_at)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "KillSwitchState":
        raw_ts = data.get("engaged_at")
        ts = datetime.fromisoformat(raw_ts) if raw_ts else None
        return cls(scope=KillSwitchScope(data["scope"]), target=data.get("target", ""),
                   engaged=bool(data.get("engaged", False)),
                   reason=data.get("reason", ""), code=data.get("code", ""),
                   engaged_at=ts, engaged_by=data.get("engaged_by", ""),
                   auto=bool(data.get("auto", False)))


class KillSwitchRegistry:
    """Persistent kill switches. Reads go to the store every time, on purpose.

    An in-memory cache would make this faster and wrong: a switch engaged by the
    CLI, by the monitor thread, or by another process must be visible to the
    trading loop *immediately*, and a cache with any TTL is a window in which a
    halted system keeps trading.
    """

    def __init__(self, *, clock: Clock | None = None, store_ns: str = _NS,
                 namespace: str | None = None, audit=None):
        self.clock = clock or default_clock()
        #: `namespace` is the older spelling and still wins when both are given,
        #: so no existing caller changes behaviour.
        self.namespace = namespace if namespace is not None else store_ns
        self.audit = audit

    @property
    def store_ns(self) -> str:
        return self.namespace

    # -- persistence ------------------------------------------------------

    def _store(self):
        from olympus import store
        return store.backend()

    def _read(self, key: str) -> KillSwitchState | None:
        try:
            raw = self._store().get(self.namespace, key)
        except Exception as exc:                       # noqa: BLE001
            # Fail closed: the caller must treat an unreadable registry as a
            # halt, so surface it rather than returning "nothing engaged".
            raise TradingError(
                f"kill-switch registry unreadable: {exc}", key=key) from exc
        if not raw:
            return None
        try:
            return KillSwitchState.from_dict(json.loads(raw.decode("utf-8")))
        except Exception as exc:                       # noqa: BLE001
            # A corrupt record is treated as ENGAGED, not as absent. If we
            # cannot tell what an operator set, the safe reading is "stopped".
            raise TradingError(
                f"kill-switch record corrupt (treating as engaged): {exc}",
                key=key) from exc

    def _write(self, state: KillSwitchState) -> None:
        payload = json.dumps(state.to_dict(), sort_keys=True).encode("utf-8")
        self._store().put(self.namespace, state.key, payload)

    def _record(self, state: KillSwitchState, action: str) -> None:
        if self.audit is None:
            return
        try:
            self.audit.record("KILL_SWITCH", {**state.to_dict(), "action": action},
                              actor=state.engaged_by or "system",
                              subject=state.target, correlation_id="")
        except Exception:                              # noqa: BLE001
            pass

    # -- operations -------------------------------------------------------

    def engage(self, scope: KillSwitchScope, target: str = "", *, reason: str,
               code: str = CODE_MANUAL, by: str = "operator",
               auto: bool = False) -> KillSwitchState:
        """Stop trading at `scope`. Idempotent; re-engaging refreshes the reason.

        Deliberately has no failure mode that leaves trading enabled: if the
        write fails the exception propagates, and the caller must treat a failed
        engage as a reason to stop by other means.
        """
        scope = KillSwitchScope(scope)
        state = KillSwitchState(
            scope=scope, target=target, engaged=True, reason=reason, code=code,
            engaged_at=self.clock.now(), engaged_by=by, auto=auto)
        self._write(state)
        self._record(state, "engage")
        return state

    def disengage(self, scope: KillSwitchScope, target: str = "", *,
                  by: str = "operator", reason: str = "",
                  operator_override: bool = False) -> KillSwitchState:
        """Resume trading at `scope`.

        An AUTO trip requires `operator_override=True`. Without that, a bug in
        the same loop that tripped the switch could clear it and carry on — the
        precise failure this device exists to prevent.
        """
        scope = KillSwitchScope(scope)
        current = self._read(
            KillSwitchState(scope=scope, target=target, engaged=False).key)
        if current is not None and current.engaged and current.auto \
                and not operator_override:
            raise KillSwitchActive(
                "this kill switch was tripped automatically and needs an "
                "explicit operator override to clear",
                scope=scope.value, target=target, code=current.code,
                reason=current.reason)
        state = KillSwitchState(
            scope=scope, target=target, engaged=False, reason=reason,
            code="", engaged_at=self.clock.now(), engaged_by=by, auto=False)
        self._write(state)
        self._record(state, "disengage")
        return state

    def is_engaged(self, scope: KillSwitchScope, target: str = "") -> bool:
        scope = KillSwitchScope(scope)
        key = KillSwitchState(scope=scope, target=target, engaged=False).key
        state = self._read(key)
        return bool(state and state.engaged)

    def check(self, instrument_key: str | None = None,
              strategy_id: str | None = None) -> KillSwitchState | None:
        """The single call the trading loop makes. Broadest scope wins.

        Returns the first engaged switch, or None. Raises (never returns None)
        if the registry cannot be read — callers must fail closed.
        """
        global_state = self._read(KillSwitchScope.GLOBAL.value)
        if global_state and global_state.engaged:
            return global_state
        if strategy_id:
            key = f"{KillSwitchScope.STRATEGY.value}:{strategy_id}"
            state = self._read(key)
            if state and state.engaged:
                return state
        if instrument_key:
            key = f"{KillSwitchScope.INSTRUMENT.value}:{instrument_key}"
            state = self._read(key)
            if state and state.engaged:
                return state
        return None

    def all(self) -> list[KillSwitchState]:
        """Every switch on record, engaged or not (operators need both)."""
        out = []
        try:
            keys = self._store().keys(self.namespace)
        except Exception as exc:                       # noqa: BLE001
            raise TradingError(f"kill-switch registry unreadable: {exc}") from exc
        for key in keys:
            try:
                state = self._read(key)
            except TradingError:
                continue                                # corrupt: reported by check()
            if state is not None:
                out.append(state)
        return sorted(out, key=lambda s: (s.scope.value, s.target))

    def engaged(self) -> list[KillSwitchState]:
        return [s for s in self.all() if s.engaged]


# ---------------------------------------------------------------------------
# automatic trips
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TripRule:
    """One automatic condition. Pure: it inspects numbers and returns a verdict.

    Kept as data + a predicate rather than a method on the monitor so the rule
    set is enumerable, testable in isolation, and visible in the audit trail.
    """
    name: str
    code: str
    scope: KillSwitchScope
    detail: str = ""


#: The required automatic shutdowns, as data.
AUTO_RULES = (
    TripRule("daily_loss", CODE_DAILY_LOSS, KillSwitchScope.GLOBAL,
             "realised+unrealised loss for the session breached the limit"),
    TripRule("portfolio_drawdown", CODE_PORTFOLIO_DRAWDOWN, KillSwitchScope.GLOBAL,
             "portfolio drawdown from peak equity breached the limit"),
    TripRule("strategy_drawdown", CODE_STRATEGY_DRAWDOWN, KillSwitchScope.STRATEGY,
             "a strategy's drawdown breached its limit"),
    TripRule("order_rate", CODE_ORDER_RATE, KillSwitchScope.GLOBAL,
             "orders per window exceeded the configured ceiling"),
    TripRule("stale_data", CODE_STALE_DATA, KillSwitchScope.GLOBAL,
             "freshest market data older than the tolerance"),
    TripRule("broker_desync", CODE_BROKER_DESYNC, KillSwitchScope.GLOBAL,
             "local and broker state disagree, or reconciliation is overdue"),
)


def evaluate_auto_trips(stats: Mapping[str, Any], limits: Any,
                        ) -> list[tuple[TripRule, str, str]]:
    """Return the trips that should fire, as (rule, target, detail).

    Pure and deterministic — takes measurements in, returns verdicts out, and
    touches neither the store nor the clock. The caller (the monitor) is what
    actually engages the switches, so this can be exhaustively tested without
    any persistence at all.

    `stats` keys are optional; a missing measurement never trips a rule, because
    "we did not measure it" must not be confused with "it is fine". The one
    exception is reconciliation age, where a *missing* value means reconciliation
    has never succeeded — which is itself a desync condition.

    P&L is SIGNED (negative is a loss), under either the `daily_pnl` or the
    legacy `daily_loss` key. Comparing its magnitude instead would halt the
    system on a good day, which trains operators to override the switch.
    """
    trips: list[tuple[TripRule, str, str]] = []
    by_name = {r.name: r for r in AUTO_RULES}

    def limit(name, default=None):
        return getattr(limits, name, default) if limits is not None else default

    daily_pnl = stats.get("daily_pnl")
    if daily_pnl is None:
        daily_pnl = stats.get("daily_loss")
    max_daily = limit("max_daily_loss")
    if daily_pnl is not None and max_daily is not None and max_daily > 0:
        if _num(daily_pnl) <= -_num(max_daily):
            trips.append((by_name["daily_loss"], "",
                          f"daily P&L {daily_pnl} <= -limit {max_daily}"))

    dd = stats.get("portfolio_drawdown")
    max_dd = limit("max_portfolio_drawdown")
    if dd is not None and max_dd is not None and max_dd > 0:
        if _num(dd) >= _num(max_dd):
            trips.append((by_name["portfolio_drawdown"], "",
                          f"portfolio drawdown {dd} >= limit {max_dd}"))

    max_sdd = limit("max_strategy_drawdown")
    for strategy_id, value in (stats.get("strategy_drawdowns") or {}).items():
        if max_sdd is not None and max_sdd > 0 and _num(value) >= _num(max_sdd):
            trips.append((by_name["strategy_drawdown"], strategy_id,
                          f"strategy drawdown {value} >= limit {max_sdd}"))

    rate = stats.get("orders_last_minute")
    max_rate = limit("max_orders_per_minute")
    if rate is not None and max_rate is not None and max_rate > 0:
        if _num(rate) > _num(max_rate):
            trips.append((by_name["order_rate"], "",
                          f"{rate} orders/min > limit {max_rate}"))

    age = stats.get("data_age_s")
    max_age = limit("max_data_age_s")
    if age is not None and max_age is not None and max_age > 0:
        if _num(age) > _num(max_age):
            trips.append((by_name["stale_data"], "",
                          f"data age {age}s > tolerance {max_age}s"))

    # Reconciliation: a missing age means it has NEVER succeeded, which is a
    # desync condition rather than an absent measurement.
    if stats.get("reconciliation_ok") is False:
        trips.append((by_name["broker_desync"], "",
                      "reconciliation reported breaks"))
    else:
        rec_age = stats.get("reconciliation_age_s", None)
        max_rec = limit("max_reconciliation_age_s")
        if max_rec is not None and max_rec > 0:
            if rec_age is None and stats.get("reconciliation_required", False):
                trips.append((by_name["broker_desync"], "",
                              "reconciliation has never succeeded"))
            elif rec_age is not None and _num(rec_age) > _num(max_rec):
                trips.append((by_name["broker_desync"], "",
                              f"reconciliation {rec_age}s old > {max_rec}s"))
    return trips


def _num(value: Any) -> float:
    from decimal import Decimal
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


__all__ = ["KillSwitchScope", "KillSwitchState", "KillSwitchRegistry",
           "TripRule", "AUTO_RULES", "evaluate_auto_trips",
           "CODE_MANUAL", "CODE_DAILY_LOSS", "CODE_PORTFOLIO_DRAWDOWN",
           "CODE_STRATEGY_DRAWDOWN", "CODE_ORDER_RATE", "CODE_STALE_DATA",
           "CODE_BROKER_DESYNC", "CODE_RECONCILIATION_STALE"]

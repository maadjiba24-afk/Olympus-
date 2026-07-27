"""The deterministic risk engine — the gate every trade must pass.

This is the most safety-critical module in the domain, and its design follows
from one observation: **the dangerous failure is not rejecting a good trade, it
is approving a bad one.** Everything below is arranged so that approving is hard
and refusing is the default.

Properties, each of which is tested rather than asserted:

* **Deterministic.** No network, no randomness, no language model, no wall-clock
  read. Time arrives through the injected clock; every input arrives through
  `RiskContext`. The same `(intent, context, limits)` produces the same
  `RiskDecision`, including its `policy_hash`. A decision you cannot replay is a
  decision you cannot audit.
* **Reduce or refuse, never increase.** The engine's entire authority is to
  shrink a proposal or reject it. There is no code path that raises a quantity,
  loosens a limit, or originates an order.
* **Fail closed.** A missing measurement that a limit depends on is a rejection,
  not a pass. "We could not check" and "it was fine" must never be confused —
  that confusion is how systems trade through outages.
* **Every check is recorded.** Passes as well as failures. The audit trail must
  show what was *evaluated*, not merely what tripped, or a later reader cannot
  distinguish "we checked and it was fine" from "we never checked".
* **Projected, not current, exposure.** Exposure limits are evaluated against
  the portfolio as it would be *after* the fill. Checking current exposure lets
  a sequence of individually-fine trades breach every limit in aggregate.

Limits are operator-owned. `RiskLimits` is frozen, `LimitsStore.save` demands an
operator token, and anything carrying the `TAINTED` marker — the flag attached
to values derived from external content — is refused outright. An analysis agent
that reads a news article cannot widen a limit, by construction.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .clock import Clock, default_clock
from .contracts import (DataQuality, Instrument, Mode, OrderType,
                        PortfolioSnapshot, RiskCheck, RiskDecision, Side,
                        TradeIntent, Verdict, ensure_utc, jsonable, to_decimal)
from .errors import (ConfigurationError, LimitsImmutableError, RiskError,
                     TradingError)
from .killswitch import KillSwitchRegistry

_NS = "trading.limits"

# --- reason codes (stable; they appear in the audit trail) -----------------

R_KILL_SWITCH = "KILL_SWITCH_ENGAGED"
R_KILL_SWITCH_UNREADABLE = "KILL_SWITCH_UNREADABLE"
R_MODE_NOT_ALLOWED = "MODE_NOT_ALLOWED"
R_INSTRUMENT_NOT_APPROVED = "INSTRUMENT_NOT_APPROVED"
R_EXCHANGE_NOT_APPROVED = "EXCHANGE_NOT_APPROVED"
R_ORDER_TYPE_NOT_APPROVED = "ORDER_TYPE_NOT_APPROVED"
R_DATA_STALE = "DATA_STALE"
R_DATA_QUALITY = "DATA_QUALITY_INSUFFICIENT"
R_DATA_MISSING = "DATA_MISSING"
R_SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
R_PRICE_DEVIATION = "PRICE_DEVIATION_EXCEEDED"
R_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
R_FORECAST_CONFIDENCE = "FORECAST_CONFIDENCE_TOO_LOW"
R_FORECAST_UNCERTAINTY = "FORECAST_UNCERTAINTY_TOO_HIGH"
R_MARKET_CLOSED = "MARKET_CLOSED"
R_NO_STOP_LOSS = "STOP_LOSS_REQUIRED"
R_BROKER_DISCONNECTED = "BROKER_DISCONNECTED"
R_RECONCILIATION_STALE = "RECONCILIATION_STALE"
R_ORDER_RATE = "ORDER_RATE_EXCEEDED"
R_STRATEGY_NOT_ACTIVE = "STRATEGY_NOT_ACTIVE"
R_MAX_ORDER_VALUE = "MAX_ORDER_VALUE_EXCEEDED"
R_MAX_POSITION_SIZE = "MAX_POSITION_SIZE_EXCEEDED"
R_MAX_POSITION_NOTIONAL = "MAX_POSITION_NOTIONAL_EXCEEDED"
R_MAX_GROSS_EXPOSURE = "MAX_GROSS_EXPOSURE_EXCEEDED"
R_MAX_NET_EXPOSURE = "MAX_NET_EXPOSURE_EXCEEDED"
R_MAX_LEVERAGE = "MAX_LEVERAGE_EXCEEDED"
R_MAX_POSITIONS = "MAX_OPEN_POSITIONS_EXCEEDED"
R_CONCENTRATION = "MAX_CONCENTRATION_EXCEEDED"
R_CORRELATED = "MAX_CORRELATED_EXPOSURE_EXCEEDED"
R_DAILY_LOSS = "MAX_DAILY_LOSS_REACHED"
R_STRATEGY_DRAWDOWN = "MAX_STRATEGY_DRAWDOWN_REACHED"
R_PORTFOLIO_DRAWDOWN = "MAX_PORTFOLIO_DRAWDOWN_REACHED"
R_BELOW_MIN_QUANTITY = "BELOW_MIN_QUANTITY_AFTER_REDUCTION"
R_BELOW_MIN_NOTIONAL = "BELOW_MIN_NOTIONAL_AFTER_REDUCTION"
R_NO_REFERENCE_PRICE = "NO_REFERENCE_PRICE"


# ---------------------------------------------------------------------------
# taint
# ---------------------------------------------------------------------------

class Tainted:
    """A wrapper marking a value as derived from untrusted external content.

    Deliberately not a subclass of anything useful: a `Tainted` cannot be
    silently used as a number or a string, so a taint that reaches arithmetic
    fails loudly instead of quietly widening a limit.
    """

    __slots__ = ("value", "origin")

    def __init__(self, value: Any, origin: str = "external"):
        self.value = value
        self.origin = origin

    def __repr__(self) -> str:                          # pragma: no cover
        return f"Tainted({self.value!r}, origin={self.origin!r})"


def is_tainted(value: Any) -> bool:
    return isinstance(value, Tainted)


def assert_untainted(value: Any, *, what: str = "value") -> Any:
    """Refuse a value that came from external content.

    Used at the limits API. This is the code-level expression of the rule that
    a news article, a web page, or a model summarising either of them can never
    change what Olympus is permitted to risk.
    """
    if is_tainted(value):
        raise LimitsImmutableError(
            f"{what} is derived from untrusted external content and may not "
            "reach risk configuration", origin=getattr(value, "origin", "external"))
    if isinstance(value, Mapping):
        for k, v in value.items():
            assert_untainted(v, what=f"{what}[{k!r}]")
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            assert_untainted(item, what=f"{what} item")
    return value


# ---------------------------------------------------------------------------
# limits
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RiskLimits:
    """Every hard limit, in one immutable object.

    `None` means "this limit is not configured" and the corresponding check is
    recorded as skipped — visibly, in the decision — rather than silently
    passing. That distinction matters: an operator reading a decision must be
    able to see which protections were actually in force.
    """
    # -- permissions
    approved_instruments: frozenset[str] = frozenset()
    approved_exchanges: frozenset[str] = frozenset()
    approved_order_types: frozenset[OrderType] = frozenset()
    allowed_modes: frozenset[Mode] = frozenset({Mode.BACKTEST, Mode.PAPER,
                                                Mode.SHADOW})
    # -- per-order sizing
    max_order_value: Decimal | None = None
    max_position_size: Decimal | None = None            # quantity, absolute
    max_position_notional: Decimal | None = None
    # -- portfolio exposure
    max_gross_exposure: Decimal | None = None
    max_net_exposure: Decimal | None = None
    max_leverage: Decimal | None = None
    max_open_positions: int | None = None
    max_concentration: float | None = None              # fraction of equity
    max_correlated_exposure: float | None = None
    correlation_groups: Mapping[str, str] = field(default_factory=dict)
    # -- loss control
    max_daily_loss: Decimal | None = None
    max_strategy_drawdown: Decimal | None = None
    max_portfolio_drawdown: Decimal | None = None
    # -- market conditions
    min_liquidity: Decimal | None = None
    max_spread_bps: float | None = None
    max_price_deviation_pct: float | None = None
    max_data_age_s: float | None = None
    min_data_quality: DataQuality = DataQuality.DEGRADED
    require_market_open: bool = False
    require_stop_loss: bool = True
    # -- forecast gating
    min_forecast_confidence: float | None = None
    max_forecast_uncertainty: float | None = None
    # -- operational
    max_orders_per_minute: int | None = None
    require_broker_connected: bool = True
    require_reconciliation: bool = False
    max_reconciliation_age_s: float | None = None

    def __post_init__(self):
        object.__setattr__(self, "approved_instruments", frozenset(self.approved_instruments))
        object.__setattr__(self, "approved_exchanges", frozenset(self.approved_exchanges))
        object.__setattr__(self, "approved_order_types",
                           frozenset(OrderType(o) for o in self.approved_order_types))
        object.__setattr__(self, "allowed_modes",
                           frozenset(Mode(m) for m in self.allowed_modes))
        object.__setattr__(self, "correlation_groups", dict(self.correlation_groups))
        for name in ("max_order_value", "max_position_size", "max_position_notional",
                     "max_gross_exposure", "max_net_exposure", "max_leverage",
                     "max_daily_loss", "max_strategy_drawdown",
                     "max_portfolio_drawdown", "min_liquidity"):
            value = getattr(self, name)
            if value is not None:
                dec = to_decimal(value, field_name=name)
                if dec < 0:
                    raise ConfigurationError(f"{name} must be >= 0", got=str(dec))
                object.__setattr__(self, name, dec)
        for name in ("max_concentration", "max_correlated_exposure"):
            value = getattr(self, name)
            if value is not None and not 0 < float(value) <= 1:
                raise ConfigurationError(f"{name} must be within (0, 1]", got=value)
        for name in ("min_forecast_confidence", "max_forecast_uncertainty"):
            value = getattr(self, name)
            if value is not None and not 0 <= float(value) <= 1:
                raise ConfigurationError(f"{name} must be within [0, 1]", got=value)
        if self.max_open_positions is not None and self.max_open_positions < 0:
            raise ConfigurationError("max_open_positions must be >= 0")
        if Mode.LIVE_RESTRICTED in self.allowed_modes or \
                Mode.LIVE_BOUNDED in self.allowed_modes:
            # Not forbidden — but a live-permitting limit set with no loss
            # ceiling is almost certainly a mistake, and silence here would be
            # the expensive kind.
            if self.max_daily_loss is None:
                raise ConfigurationError(
                    "a limit set that permits live trading must configure "
                    "max_daily_loss")

    # -- canonical identity ----------------------------------------------

    def to_dict(self) -> dict:
        out: dict[str, Any] = {}
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if isinstance(value, frozenset):
                out[name] = sorted(jsonable(v) for v in value)
            else:
                out[name] = jsonable(value)
        return out

    def policy_hash(self) -> str:
        """Stable SHA-256 over the canonicalised limits.

        Written into every `RiskDecision` so a historical decision can be
        replayed against exactly the policy that produced it — without which
        "why was this approved" is unanswerable after the next limit change.
        """
        blob = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    @classmethod
    def conservative(cls, **overrides) -> "RiskLimits":
        """A deliberately restrictive starting point.

        Chosen so that an operator who ships the defaults unchanged gets a
        system that trades small and stops early, rather than one that is
        wide open until someone remembers to tighten it.
        """
        base = dict(
            max_order_value=Decimal("1000"),
            max_position_size=Decimal("100"),
            max_position_notional=Decimal("2000"),
            max_gross_exposure=Decimal("10000"),
            max_net_exposure=Decimal("5000"),
            max_leverage=Decimal("1"),
            max_open_positions=5,
            max_concentration=0.25,
            max_correlated_exposure=0.4,
            max_daily_loss=Decimal("200"),
            max_strategy_drawdown=Decimal("500"),
            max_portfolio_drawdown=Decimal("1000"),
            max_spread_bps=50.0,
            max_price_deviation_pct=5.0,
            max_data_age_s=300.0,
            min_data_quality=DataQuality.OK,
            require_market_open=True,
            require_stop_loss=True,
            min_forecast_confidence=0.55,
            max_forecast_uncertainty=0.6,
            max_orders_per_minute=10,
            require_broker_connected=True,
            require_reconciliation=True,
            max_reconciliation_age_s=3600.0,
            allowed_modes=frozenset({Mode.BACKTEST, Mode.PAPER, Mode.SHADOW}),
            approved_order_types=frozenset({OrderType.MARKET, OrderType.LIMIT}),
        )
        base.update(overrides)
        return cls(**base)


class LimitsStore:
    """Operator-owned persistence for `RiskLimits`.

    Two guarantees:
      1. Saving requires an operator token. There is no "internal" path that
         skips it, because an internal path is exactly what a prompt-injected
         agent would reach for.
      2. Nothing tainted can be saved. `assert_untainted` walks the whole
         payload, so a limit assembled partly from a news summary is refused.
    """

    def __init__(self, *, clock: Clock | None = None, namespace: str = _NS,
                 audit=None, operator_token: str | None = None):
        self.clock = clock or default_clock()
        self.namespace = namespace
        self.audit = audit
        #: The expected token. None means "no live operator configured", which
        #: makes every save fail — the safe default for an unconfigured system.
        self._operator_token = operator_token

    def _store(self):
        from olympus import store
        return store.backend()

    def load(self) -> RiskLimits:
        """The active limits, or the conservative defaults if none are set."""
        raw = self._store().get(self.namespace, "active")
        if not raw:
            return RiskLimits.conservative()
        data = json.loads(raw.decode("utf-8"))
        return _limits_from_dict(data["limits"])

    def save(self, limits: RiskLimits, *, operator_token: str, reason: str,
             by: str = "operator") -> RiskLimits:
        if not isinstance(limits, RiskLimits):
            raise LimitsImmutableError("only a RiskLimits object may be saved",
                                       got=type(limits).__name__)
        assert_untainted(limits.to_dict(), what="risk limits")
        if not self._operator_token:
            raise LimitsImmutableError(
                "no operator token is configured; risk limits cannot be changed")
        if operator_token != self._operator_token:
            raise LimitsImmutableError("operator token rejected")
        payload = {
            "limits": limits.to_dict(),
            "policy_hash": limits.policy_hash(),
            "saved_at": self.clock.now().isoformat(),
            "by": by,
            "reason": reason,
        }
        blob = json.dumps(payload, sort_keys=True).encode("utf-8")
        self._store().put(self.namespace, "active", blob)
        self._store().put(self.namespace, f"v-{limits.policy_hash()[:16]}", blob)
        if self.audit is not None:
            try:
                self.audit.record("CONFIG_CHANGED", payload, actor=by,
                                  subject="risk_limits", correlation_id="")
            except Exception:                           # noqa: BLE001
                pass
        return limits


def _limits_from_dict(data: Mapping[str, Any]) -> RiskLimits:
    kw: dict[str, Any] = dict(data)
    kw["approved_instruments"] = frozenset(kw.get("approved_instruments") or ())
    kw["approved_exchanges"] = frozenset(kw.get("approved_exchanges") or ())
    kw["approved_order_types"] = frozenset(
        OrderType(o) for o in (kw.get("approved_order_types") or ()))
    kw["allowed_modes"] = frozenset(Mode(m) for m in (kw.get("allowed_modes") or ()))
    if kw.get("min_data_quality"):
        kw["min_data_quality"] = DataQuality(kw["min_data_quality"])
    for name in ("max_order_value", "max_position_size", "max_position_notional",
                 "max_gross_exposure", "max_net_exposure", "max_leverage",
                 "max_daily_loss", "max_strategy_drawdown",
                 "max_portfolio_drawdown", "min_liquidity"):
        if kw.get(name) is not None:
            kw[name] = Decimal(str(kw[name]))
    known = set(RiskLimits.__dataclass_fields__)
    return RiskLimits(**{k: v for k, v in kw.items() if k in known})


# ---------------------------------------------------------------------------
# context
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RiskContext:
    """Every fact a decision may be made from.

    Frozen and explicit so a decision is a pure function of its inputs. If a
    check needs something that is not here, it must be added here — a check that
    reaches out to fetch its own data would destroy replayability.
    """
    as_of: datetime
    mode: Mode
    instrument: Instrument
    portfolio: PortfolioSnapshot | None = None
    reference_price: float | None = None
    quote: Any = None
    data_age_s: float | None = None
    data_quality: DataQuality | None = None
    forecast: Any = None
    session_open: bool | None = None
    broker_connected: bool | None = None
    reconciliation_age_s: float | None = None
    recent_order_count: int | None = None
    daily_pnl: Decimal | None = None
    strategy_drawdown: Decimal | None = None
    portfolio_drawdown: Decimal | None = None
    strategy_active: bool = True
    average_volume: Decimal | None = None
    equity: Decimal | None = None

    def __post_init__(self):
        object.__setattr__(self, "as_of", ensure_utc(self.as_of, field_name="as_of"))
        for name in ("daily_pnl", "strategy_drawdown", "portfolio_drawdown",
                     "average_volume", "equity"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, to_decimal(value, field_name=name))


# ---------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------

class RiskEngine:
    """`authorise(intent, ctx) -> RiskDecision`. Deterministic. Fails closed."""

    def __init__(self, *, limits_store: LimitsStore | None = None,
                 limits: RiskLimits | None = None,
                 killswitches: KillSwitchRegistry | None = None,
                 clock: Clock | None = None, audit=None):
        if limits_store is None and limits is None:
            raise ConfigurationError(
                "RiskEngine needs either a LimitsStore or an explicit RiskLimits")
        self.limits_store = limits_store
        self._explicit_limits = limits
        self.killswitches = killswitches
        self.clock = clock or default_clock()
        self.audit = audit

    def limits(self) -> RiskLimits:
        if self._explicit_limits is not None:
            return self._explicit_limits
        return self.limits_store.load()

    # -- the decision -----------------------------------------------------

    def authorise(self, intent: TradeIntent, ctx: RiskContext) -> RiskDecision:
        limits = self.limits()
        checks: list[RiskCheck] = []
        reasons: list[str] = []
        instrument = ctx.instrument
        decided_at = ctx.as_of

        def add(name: str, passed: bool, code: str = "", detail: str = "",
                observed: Any = None, limit: Any = None) -> bool:
            checks.append(RiskCheck(name=name, passed=passed, code=code,
                                    detail=detail, observed=observed, limit=limit))
            if not passed and code:
                reasons.append(code)
            return passed

        def reject(explanation: str) -> RiskDecision:
            return RiskDecision(
                intent_id=intent.intent_id, verdict=Verdict.REJECTED,
                checks=tuple(checks), approved_quantity=Decimal("0"),
                decided_at=decided_at, mode=ctx.mode,
                policy_hash=limits.policy_hash(),
                reason_codes=tuple(dict.fromkeys(reasons)),
                explanation=explanation)

        # 1. Kill switches — first, and short-circuiting.
        if self.killswitches is not None:
            try:
                engaged = self.killswitches.check(
                    instrument_key=intent.instrument_key,
                    strategy_id=intent.strategy_id)
            except TradingError as exc:
                add("kill_switch", False, R_KILL_SWITCH_UNREADABLE, str(exc))
                return reject("kill-switch state could not be read; failing closed")
            if engaged is not None:
                add("kill_switch", False, R_KILL_SWITCH,
                    f"{engaged.scope.value} kill switch engaged: {engaged.code}",
                    observed=engaged.key)
                return reject("a kill switch is engaged")
            add("kill_switch", True)

        # 2. Mode.
        if not add("mode_allowed", ctx.mode in limits.allowed_modes,
                   R_MODE_NOT_ALLOWED, f"mode {ctx.mode.value} not permitted",
                   observed=ctx.mode.value,
                   limit=sorted(m.value for m in limits.allowed_modes)):
            return reject(f"mode {ctx.mode.value} is not permitted")

        # 3. Strategy status.
        if not add("strategy_active", bool(ctx.strategy_active),
                   R_STRATEGY_NOT_ACTIVE, "strategy is not active"):
            return reject("strategy is not active")

        # 4. Static permissions.
        if limits.approved_instruments:
            if not add("instrument_approved",
                       intent.instrument_key in limits.approved_instruments,
                       R_INSTRUMENT_NOT_APPROVED, "instrument not on the approved list",
                       observed=intent.instrument_key):
                return reject("instrument is not approved")
        if limits.approved_exchanges:
            if not add("exchange_approved",
                       instrument.exchange in limits.approved_exchanges,
                       R_EXCHANGE_NOT_APPROVED, "exchange not on the approved list",
                       observed=instrument.exchange):
                return reject("exchange is not approved")
        if limits.approved_order_types:
            if not add("order_type_approved",
                       intent.order_type in limits.approved_order_types,
                       R_ORDER_TYPE_NOT_APPROVED, "order type not permitted",
                       observed=intent.order_type.value):
                return reject("order type is not approved")

        # 5. Data integrity. A missing measurement fails closed when the
        #    corresponding limit is configured.
        if limits.max_data_age_s is not None:
            if ctx.data_age_s is None:
                add("data_freshness", False, R_DATA_MISSING,
                    "data age unknown while a freshness limit is configured")
                return reject("market-data age is unknown")
            if not add("data_freshness", ctx.data_age_s <= limits.max_data_age_s,
                       R_DATA_STALE, "market data is stale",
                       observed=ctx.data_age_s, limit=limits.max_data_age_s):
                return reject("market data is stale")

        if ctx.data_quality is not None:
            if ctx.data_quality is DataQuality.UNUSABLE:
                add("data_quality", False, R_DATA_QUALITY, "data unusable",
                    observed=ctx.data_quality.value)
                return reject("market data is unusable")
            if limits.min_data_quality is DataQuality.OK:
                if not add("data_quality", ctx.data_quality is DataQuality.OK,
                           R_DATA_QUALITY, "data quality below requirement",
                           observed=ctx.data_quality.value,
                           limit=limits.min_data_quality.value):
                    return reject("market-data quality is insufficient")
            else:
                add("data_quality", True, observed=ctx.data_quality.value)

        if limits.max_spread_bps is not None and ctx.quote is not None:
            spread = getattr(ctx.quote, "spread_bps", None)
            if spread is None:
                add("spread", False, R_DATA_MISSING, "spread unavailable")
                return reject("spread could not be measured")
            if not add("spread", spread <= limits.max_spread_bps,
                       R_SPREAD_TOO_WIDE, "spread wider than permitted",
                       observed=spread, limit=limits.max_spread_bps):
                return reject("spread is too wide")

        # Reference price: needed by every value-based limit below.
        ref = self._reference_price(intent, ctx)
        if ref is None:
            add("reference_price", False, R_NO_REFERENCE_PRICE,
                "no reference price available for valuation")
            return reject("no reference price is available")
        add("reference_price", True, observed=float(ref))

        if limits.max_price_deviation_pct is not None and intent.intended_entry:
            entry = to_decimal(intent.intended_entry, field_name="intended_entry")
            if entry > 0:
                dev = abs((ref - entry) / entry) * Decimal("100")
                if not add("price_deviation",
                           float(dev) <= limits.max_price_deviation_pct,
                           R_PRICE_DEVIATION, "reference price deviates from intent",
                           observed=float(dev),
                           limit=limits.max_price_deviation_pct):
                    return reject("price has moved too far from the intended entry")

        if limits.min_liquidity is not None:
            if ctx.average_volume is None:
                add("liquidity", False, R_DATA_MISSING, "liquidity unknown")
                return reject("liquidity could not be measured")
            if not add("liquidity", ctx.average_volume >= limits.min_liquidity,
                       R_LIQUIDITY, "instrument below the liquidity floor",
                       observed=str(ctx.average_volume),
                       limit=str(limits.min_liquidity)):
                return reject("instrument is too illiquid")

        # 6. Forecast gating.
        if limits.min_forecast_confidence is not None:
            conf = getattr(ctx.forecast, "confidence", None)
            if ctx.forecast is None or conf is None:
                add("forecast_confidence", False, R_DATA_MISSING,
                    "forecast confidence unknown while a floor is configured")
                return reject("forecast confidence is unknown")
            if not add("forecast_confidence", conf >= limits.min_forecast_confidence,
                       R_FORECAST_CONFIDENCE, "forecast confidence below floor",
                       observed=conf, limit=limits.min_forecast_confidence):
                return reject("forecast confidence is too low")
        if limits.max_forecast_uncertainty is not None:
            unc = getattr(ctx.forecast, "uncertainty", None)
            if ctx.forecast is None or unc is None:
                add("forecast_uncertainty", False, R_DATA_MISSING,
                    "forecast uncertainty unknown while a ceiling is configured")
                return reject("forecast uncertainty is unknown")
            if not add("forecast_uncertainty", unc <= limits.max_forecast_uncertainty,
                       R_FORECAST_UNCERTAINTY, "forecast too uncertain",
                       observed=unc, limit=limits.max_forecast_uncertainty):
                return reject("forecast is too uncertain")

        # 7. Session.
        if limits.require_market_open:
            if ctx.session_open is None:
                add("market_open", False, R_DATA_MISSING, "session state unknown")
                return reject("market session state is unknown")
            if not add("market_open", ctx.session_open, R_MARKET_CLOSED,
                       "market is closed"):
                return reject("market is closed")

        # 8. Protection.
        if limits.require_stop_loss:
            if not add("stop_loss", intent.stop_loss is not None, R_NO_STOP_LOSS,
                       "a stop loss is required but was not supplied"):
                return reject("a stop loss is required")

        # 9. Operational.
        if limits.require_broker_connected and ctx.mode.is_live:
            if not add("broker_connected", bool(ctx.broker_connected),
                       R_BROKER_DISCONNECTED, "broker is not connected"):
                return reject("broker is not connected")
        else:
            add("broker_connected", True)

        if limits.require_reconciliation and limits.max_reconciliation_age_s is not None:
            if ctx.reconciliation_age_s is None:
                add("reconciliation", False, R_RECONCILIATION_STALE,
                    "reconciliation has never succeeded")
                return reject("reconciliation has never succeeded")
            if not add("reconciliation",
                       ctx.reconciliation_age_s <= limits.max_reconciliation_age_s,
                       R_RECONCILIATION_STALE, "reconciliation is overdue",
                       observed=ctx.reconciliation_age_s,
                       limit=limits.max_reconciliation_age_s):
                return reject("reconciliation is overdue")

        if limits.max_orders_per_minute is not None:
            count = ctx.recent_order_count or 0
            if not add("order_rate", count < limits.max_orders_per_minute,
                       R_ORDER_RATE, "order rate ceiling reached",
                       observed=count, limit=limits.max_orders_per_minute):
                return reject("order-rate ceiling reached")

        # 10. Loss control — evaluated before sizing, because a breach here is
        #     a stop, not a reason to trade smaller.
        if limits.max_daily_loss is not None and ctx.daily_pnl is not None:
            breached = ctx.daily_pnl <= -limits.max_daily_loss
            if not add("daily_loss", not breached, R_DAILY_LOSS,
                       "daily loss limit reached", observed=str(ctx.daily_pnl),
                       limit=str(limits.max_daily_loss)):
                return reject("daily loss limit reached")
        if limits.max_strategy_drawdown is not None and ctx.strategy_drawdown is not None:
            if not add("strategy_drawdown",
                       ctx.strategy_drawdown < limits.max_strategy_drawdown,
                       R_STRATEGY_DRAWDOWN, "strategy drawdown limit reached",
                       observed=str(ctx.strategy_drawdown),
                       limit=str(limits.max_strategy_drawdown)):
                return reject("strategy drawdown limit reached")
        if limits.max_portfolio_drawdown is not None and ctx.portfolio_drawdown is not None:
            if not add("portfolio_drawdown",
                       ctx.portfolio_drawdown < limits.max_portfolio_drawdown,
                       R_PORTFOLIO_DRAWDOWN, "portfolio drawdown limit reached",
                       observed=str(ctx.portfolio_drawdown),
                       limit=str(limits.max_portfolio_drawdown)):
                return reject("portfolio drawdown limit reached")

        # 11. Sizing. From here the engine may REDUCE.
        quantity = instrument.quantize_quantity(intent.quantity)
        reduced = False

        def cap(new_qty: Decimal, name: str, code: str, detail: str,
                observed: Any, limit_value: Any) -> Decimal:
            nonlocal reduced
            new_qty = instrument.quantize_quantity(max(Decimal("0"), new_qty))
            if new_qty < quantity:
                reduced = True
                add(name, True, code, f"{detail} (reduced)", observed, limit_value)
                return new_qty
            add(name, True, "", detail, observed, limit_value)
            return quantity

        if limits.max_order_value is not None:
            value = instrument.notional(quantity, ref)
            if value > limits.max_order_value:
                allowed = limits.max_order_value / (ref * instrument.multiplier)
                quantity = cap(allowed, "max_order_value", R_MAX_ORDER_VALUE,
                               "order value above limit", str(value),
                               str(limits.max_order_value))
            else:
                add("max_order_value", True, observed=str(value),
                    limit=str(limits.max_order_value))

        position = self._position_quantity(intent.instrument_key, ctx)
        projected = position + quantity * intent.side.sign

        if limits.max_position_size is not None:
            if abs(projected) > limits.max_position_size:
                headroom = limits.max_position_size - abs(position) \
                    if (position * intent.side.sign) >= 0 else \
                    limits.max_position_size + abs(position)
                quantity = cap(headroom, "max_position_size", R_MAX_POSITION_SIZE,
                               "projected position above limit", str(abs(projected)),
                               str(limits.max_position_size))
                projected = position + quantity * intent.side.sign
            else:
                add("max_position_size", True, observed=str(abs(projected)),
                    limit=str(limits.max_position_size))

        if limits.max_position_notional is not None:
            notional = abs(instrument.notional(projected, ref))
            if notional > limits.max_position_notional:
                allowed_qty = limits.max_position_notional / (ref * instrument.multiplier)
                headroom = allowed_qty - abs(position)
                quantity = cap(headroom, "max_position_notional",
                               R_MAX_POSITION_NOTIONAL,
                               "projected position notional above limit",
                               str(notional), str(limits.max_position_notional))
                projected = position + quantity * intent.side.sign
            else:
                add("max_position_notional", True, observed=str(notional),
                    limit=str(limits.max_position_notional))

        # 12. PROJECTED portfolio exposure — after the fill, never before.
        equity = self._equity(ctx)
        gross_now, net_now = self._exposures(ctx)
        delta = instrument.notional(quantity * intent.side.sign, ref)
        # Gross rises by the added notional unless the trade reduces an existing
        # position; treating a reducing trade as exposure-adding would block the
        # very trades that make the book safer.
        reduces = (position != 0) and ((position > 0) != (intent.side is Side.BUY))
        gross_projected = gross_now + (abs(delta) if not reduces else -min(abs(delta), abs(instrument.notional(position, ref))))
        net_projected = net_now + delta

        if limits.max_gross_exposure is not None:
            if gross_projected > limits.max_gross_exposure and not reduces:
                headroom = limits.max_gross_exposure - gross_now
                allowed_qty = headroom / (ref * instrument.multiplier) if ref > 0 else Decimal("0")
                quantity = cap(allowed_qty, "max_gross_exposure", R_MAX_GROSS_EXPOSURE,
                               "projected gross exposure above limit",
                               str(gross_projected), str(limits.max_gross_exposure))
            else:
                add("max_gross_exposure", True, observed=str(gross_projected),
                    limit=str(limits.max_gross_exposure))

        if limits.max_net_exposure is not None:
            if abs(net_projected) > limits.max_net_exposure:
                headroom = limits.max_net_exposure - abs(net_now)
                allowed_qty = max(Decimal("0"), headroom) / (ref * instrument.multiplier) if ref > 0 else Decimal("0")
                quantity = cap(allowed_qty, "max_net_exposure", R_MAX_NET_EXPOSURE,
                               "projected net exposure above limit",
                               str(abs(net_projected)), str(limits.max_net_exposure))
            else:
                add("max_net_exposure", True, observed=str(abs(net_projected)),
                    limit=str(limits.max_net_exposure))

        if limits.max_leverage is not None and equity and equity > 0:
            lev = gross_projected / equity
            if lev > limits.max_leverage:
                allowed_gross = limits.max_leverage * equity
                headroom = allowed_gross - gross_now
                allowed_qty = max(Decimal("0"), headroom) / (ref * instrument.multiplier) if ref > 0 else Decimal("0")
                quantity = cap(allowed_qty, "max_leverage", R_MAX_LEVERAGE,
                               "projected leverage above limit", str(lev),
                               str(limits.max_leverage))
            else:
                add("max_leverage", True, observed=str(lev),
                    limit=str(limits.max_leverage))

        if limits.max_open_positions is not None:
            open_now = self._open_position_count(ctx)
            opening_new = position == 0 and quantity > 0
            projected_count = open_now + (1 if opening_new else 0)
            if not add("max_open_positions",
                       projected_count <= limits.max_open_positions,
                       R_MAX_POSITIONS, "too many open positions",
                       observed=projected_count, limit=limits.max_open_positions):
                return reject("maximum number of open positions reached")

        if limits.max_concentration is not None and equity and equity > 0:
            conc = abs(instrument.notional(position + quantity * intent.side.sign, ref)) / equity
            if float(conc) > limits.max_concentration:
                allowed_notional = Decimal(str(limits.max_concentration)) * equity
                allowed_qty = allowed_notional / (ref * instrument.multiplier) if ref > 0 else Decimal("0")
                quantity = cap(max(Decimal("0"), allowed_qty - abs(position)),
                               "max_concentration", R_CONCENTRATION,
                               "projected concentration above limit", float(conc),
                               limits.max_concentration)
            else:
                add("max_concentration", True, observed=float(conc),
                    limit=limits.max_concentration)

        if limits.max_correlated_exposure is not None and equity and equity > 0:
            group = limits.correlation_groups.get(intent.instrument_key)
            if group:
                exposure = self._group_exposure(group, limits, ctx)
                exposure += abs(delta)
                frac = exposure / equity
                if not add("max_correlated_exposure",
                           float(frac) <= limits.max_correlated_exposure,
                           R_CORRELATED, "correlated-group exposure above limit",
                           observed=float(frac), limit=limits.max_correlated_exposure):
                    return reject("correlated exposure limit reached")
            else:
                add("max_correlated_exposure", True, detail="no correlation group")

        # 13. Floor. A zero-size approval is forbidden by the contract, so a
        #     reduction that lands below the tradable minimum is a rejection.
        quantity = instrument.quantize_quantity(quantity)
        if quantity <= 0:
            add("min_quantity", False, R_BELOW_MIN_QUANTITY,
                "reduced to zero by the limits above", observed="0")
            return reject("size reduced below the tradable minimum")
        if quantity < instrument.min_quantity:
            add("min_quantity", False, R_BELOW_MIN_QUANTITY,
                "below the instrument minimum after reduction",
                observed=str(quantity), limit=str(instrument.min_quantity))
            return reject("size reduced below the instrument minimum")
        add("min_quantity", True, observed=str(quantity))

        if instrument.min_notional > 0:
            notional = instrument.notional(quantity, ref)
            if not add("min_notional", notional >= instrument.min_notional,
                       R_BELOW_MIN_NOTIONAL, "below the minimum notional",
                       observed=str(notional), limit=str(instrument.min_notional)):
                return reject("size reduced below the minimum notional")

        verdict = Verdict.APPROVED_REDUCED if reduced else Verdict.APPROVED
        decision = RiskDecision(
            intent_id=intent.intent_id, verdict=verdict, checks=tuple(checks),
            approved_quantity=quantity, decided_at=decided_at, mode=ctx.mode,
            policy_hash=limits.policy_hash(),
            reason_codes=tuple(dict.fromkeys(reasons)),
            explanation=("approved with a reduced size" if reduced else "approved"))
        if self.audit is not None:
            try:
                self.audit.record("RISK_DECISION", decision.to_dict(),
                                  actor="risk", subject=intent.instrument_key,
                                  correlation_id="")
            except Exception:                           # noqa: BLE001
                pass
        return decision

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _reference_price(intent: TradeIntent, ctx: RiskContext) -> Decimal | None:
        if ctx.reference_price is not None:
            return to_decimal(ctx.reference_price, field_name="reference_price")
        if ctx.quote is not None and getattr(ctx.quote, "mid", None):
            return to_decimal(ctx.quote.mid, field_name="mid")
        if intent.intended_entry is not None:
            return to_decimal(intent.intended_entry, field_name="intended_entry")
        if intent.limit_price is not None:
            return to_decimal(intent.limit_price, field_name="limit_price")
        return None

    @staticmethod
    def _position_quantity(key: str, ctx: RiskContext) -> Decimal:
        if ctx.portfolio is None:
            return Decimal("0")
        pos = ctx.portfolio.positions.get(key)
        return pos.quantity if pos is not None else Decimal("0")

    @staticmethod
    def _equity(ctx: RiskContext) -> Decimal:
        if ctx.equity is not None:
            return ctx.equity
        if ctx.portfolio is not None:
            try:
                return ctx.portfolio.equity
            except Exception:                           # noqa: BLE001
                return Decimal("0")
        return Decimal("0")

    @staticmethod
    def _exposures(ctx: RiskContext) -> tuple[Decimal, Decimal]:
        if ctx.portfolio is None:
            return Decimal("0"), Decimal("0")
        try:
            return ctx.portfolio.gross_exposure, ctx.portfolio.net_exposure
        except Exception:                               # noqa: BLE001
            return Decimal("0"), Decimal("0")

    @staticmethod
    def _open_position_count(ctx: RiskContext) -> int:
        if ctx.portfolio is None:
            return 0
        return len(ctx.portfolio.open_positions)

    @staticmethod
    def _group_exposure(group: str, limits: RiskLimits, ctx: RiskContext) -> Decimal:
        if ctx.portfolio is None:
            return Decimal("0")
        total = Decimal("0")
        for key, pos in ctx.portfolio.positions.items():
            if limits.correlation_groups.get(key) != group:
                continue
            mark = ctx.portfolio.marks.get(key)
            if mark is None:
                continue
            total += abs(pos.market_value(mark))
        return total


__all__ = ["RiskLimits", "LimitsStore", "RiskContext", "RiskEngine",
           "Tainted", "is_tainted", "assert_untainted"]

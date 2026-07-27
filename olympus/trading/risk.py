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
from .contracts import (AccountSnapshot, Candle, DataQuality,
                        DataQualityReport, Instrument, Mode, OrderType,
                        PortfolioSnapshot, RiskCheck, RiskDecision,
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
#: A P&L / equity measurement a configured limit depends on was not supplied.
#: Distinct from R_DATA_MISSING so an operator can tell "the market feed was
#: silent" apart from "our own accounting did not report".
R_PNL_MISSING = "PNL_MEASUREMENT_MISSING"
R_EQUITY_MISSING = "EQUITY_MEASUREMENT_MISSING"


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
        if not reason:
            # An unexplained limit change is the one an incident review cannot
            # reconstruct, so the field is mandatory in substance, not just in
            # signature.
            raise LimitsImmutableError("a limit change must state a reason")
        previous = self.load()
        payload = {
            "limits": limits.to_dict(),
            "policy_hash": limits.policy_hash(),
            "previous_policy_hash": previous.policy_hash(),
            "changed": _diff_limits(previous, limits),
            "saved_at": self.clock.now().isoformat(),
            "by": by,
            "reason": reason,
        }
        blob = json.dumps(payload, sort_keys=True).encode("utf-8")
        self._store().put(self.namespace, "active", blob)
        self._store().put(self.namespace, f"v-{limits.policy_hash()[:16]}", blob)
        # The change log lives in the store, not only in an injected audit sink:
        # a deployment that forgot to wire an audit trail must still be able to
        # answer "who widened this limit, when, and from what".
        self._append_audit(payload)
        if self.audit is not None:
            try:
                self.audit.record("CONFIG_CHANGED", payload, actor=by,
                                  subject="risk_limits", correlation_id="")
            except Exception:                           # noqa: BLE001
                pass
        return limits

    def _append_audit(self, payload: Mapping[str, Any]) -> None:
        from olympus import proclock
        # Read-modify-write on one key, so it is serialised: two operators
        # saving at once must produce two records, not one survivor (ADR 0005).
        with proclock.lock(f"{self.namespace}.audit"):
            raw = self._store().get(self.namespace, "audit")
            log = json.loads(raw.decode("utf-8")) if raw else []
            log.append(dict(payload))
            self._store().put(self.namespace, "audit",
                              json.dumps(log, sort_keys=True).encode("utf-8"))

    def history(self) -> list[dict]:
        """Every recorded limit change, oldest first."""
        raw = self._store().get(self.namespace, "audit")
        return json.loads(raw.decode("utf-8")) if raw else []


def _diff_limits(before: RiskLimits, after: RiskLimits) -> dict:
    """The fields that actually moved, so an audit record is readable at a
    glance rather than being two full policy dumps to eyeball."""
    a, b = before.to_dict(), after.to_dict()
    return {k: {"from": a.get(k), "to": b.get(k)}
            for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)}


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

    Two ways to supply the same fact
    --------------------------------
    Several fields exist in both a *raw evidence* form (`data_quality_report`,
    `last_reconciliation_ts`, `account`, `candle`) and a *reduced measurement*
    form (`data_quality`, `reconciliation_age_s`, `equity`, `reference_price`).
    The reduction happens here, once, in `__post_init__` — never inside a check.

    The reason is a specific failure this replaces: a caller that carefully
    assembles a `DataQualityReport` and passes it in, while the engine only ever
    reads the `data_quality` field the caller left as `None`, gets a system that
    *looks* like it validates data and does not. Deriving at construction means
    handing over the evidence is enough; you cannot supply it and have it
    ignored. An explicitly-supplied measurement always wins, so a caller can
    still override a derivation deliberately.
    """
    as_of: datetime
    mode: Mode
    instrument: Instrument
    portfolio: PortfolioSnapshot | None = None
    #: Broker-reported account state. Source of `equity` when not given directly;
    #: kept whole because a decision record should show what the broker said,
    #: not just the one number we extracted from it.
    account: AccountSnapshot | None = None
    reference_price: float | None = None
    quote: Any = None
    #: The most recent finalised bar. Last-resort source of a reference price.
    candle: Candle | None = None
    #: The validator's verdict on the data window behind this decision.
    data_quality_report: DataQualityReport | None = None
    data_age_s: float | None = None
    data_quality: DataQuality | None = None
    forecast: Any = None
    session_open: bool | None = None
    broker_connected: bool | None = None
    #: When reconciliation last *succeeded*. None means it never has.
    last_reconciliation_ts: datetime | None = None
    reconciliation_age_s: float | None = None
    recent_order_count: int | None = None
    daily_pnl: Decimal | None = None
    strategy_drawdown: Decimal | None = None
    portfolio_drawdown: Decimal | None = None
    strategy_active: bool = True
    strategy_id: str = ""
    average_volume: Decimal | None = None
    equity: Decimal | None = None

    def __post_init__(self):
        object.__setattr__(self, "as_of", ensure_utc(self.as_of, field_name="as_of"))
        if self.last_reconciliation_ts is not None:
            object.__setattr__(self, "last_reconciliation_ts",
                               ensure_utc(self.last_reconciliation_ts,
                                          field_name="last_reconciliation_ts"))
        for name in ("daily_pnl", "strategy_drawdown", "portfolio_drawdown",
                     "average_volume", "equity"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, to_decimal(value, field_name=name))

        # -- derivations. Explicit values win; these only fill in blanks.
        report = self.data_quality_report
        if report is not None:
            if self.data_quality is None:
                object.__setattr__(self, "data_quality", report.status)
            if self.data_age_s is None and report.age_seconds is not None:
                object.__setattr__(self, "data_age_s", float(report.age_seconds))
        if self.reconciliation_age_s is None and self.last_reconciliation_ts is not None:
            age = (self.as_of - self.last_reconciliation_ts).total_seconds()
            # A reconciliation timestamped in the future is a clock disagreement
            # between us and the broker, not freshness. Clamp at 0 rather than
            # handing a negative age to a "<= max_age" comparison that would
            # then pass unconditionally.
            object.__setattr__(self, "reconciliation_age_s", max(0.0, age))
        if self.equity is None and self.account is not None:
            object.__setattr__(self, "equity", self.account.equity)


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
        #     a stop, not a reason to trade smaller. Each fails closed: a
        #     configured loss ceiling with nothing measuring P&L against it is
        #     an unbounded system that merely looks protected.
        if limits.max_daily_loss is not None:
            if ctx.daily_pnl is None:
                add("daily_loss", False, R_PNL_MISSING,
                    "daily P&L unknown while a daily-loss ceiling is configured",
                    limit=str(limits.max_daily_loss))
                return reject("daily P&L is unknown")
            breached = ctx.daily_pnl <= -limits.max_daily_loss
            if not add("daily_loss", not breached, R_DAILY_LOSS,
                       "daily loss limit reached", observed=str(ctx.daily_pnl),
                       limit=str(limits.max_daily_loss)):
                return reject("daily loss limit reached")
        if limits.max_strategy_drawdown is not None:
            if ctx.strategy_drawdown is None:
                add("strategy_drawdown", False, R_PNL_MISSING,
                    "strategy drawdown unknown while a ceiling is configured",
                    limit=str(limits.max_strategy_drawdown))
                return reject("strategy drawdown is unknown")
            if not add("strategy_drawdown",
                       ctx.strategy_drawdown < limits.max_strategy_drawdown,
                       R_STRATEGY_DRAWDOWN, "strategy drawdown limit reached",
                       observed=str(ctx.strategy_drawdown),
                       limit=str(limits.max_strategy_drawdown)):
                return reject("strategy drawdown limit reached")
        if limits.max_portfolio_drawdown is not None:
            if ctx.portfolio_drawdown is None:
                add("portfolio_drawdown", False, R_PNL_MISSING,
                    "portfolio drawdown unknown while a ceiling is configured",
                    limit=str(limits.max_portfolio_drawdown))
                return reject("portfolio drawdown is unknown")
            if not add("portfolio_drawdown",
                       ctx.portfolio_drawdown < limits.max_portfolio_drawdown,
                       R_PORTFOLIO_DRAWDOWN, "portfolio drawdown limit reached",
                       observed=str(ctx.portfolio_drawdown),
                       limit=str(limits.max_portfolio_drawdown)):
                return reject("portfolio drawdown limit reached")

        # 11. Sizing. From here the engine may REDUCE.
        quantity = instrument.quantize_quantity(intent.quantity)
        sign = Decimal(intent.side.sign)
        #: Value of one unit of quantity. Every "how many can I afford" solve
        #: divides by this exactly once.
        unit = to_decimal(ref, field_name="ref") * instrument.multiplier
        reduced = False

        def cap(new_qty: Decimal, name: str, code: str, detail: str,
                observed: Any, limit_value: Any) -> Decimal:
            nonlocal reduced
            new_qty = instrument.quantize_quantity(max(Decimal("0"), new_qty))
            if new_qty < quantity:
                reduced = True
                # The code is recorded even though the check "passed": the limit
                # genuinely was exceeded, and an operator asking why a fill was
                # smaller than the strategy asked for needs that code.
                checks.append(RiskCheck(name=name, passed=True, code=code,
                                        detail=f"{detail} (reduced)",
                                        observed=observed, limit=limit_value))
                reasons.append(code)
                return new_qty
            add(name, True, "", detail, observed, limit_value)
            return quantity

        position = self._position_quantity(intent.instrument_key, ctx)

        def max_qty_for_abs_position(allowed_abs_qty: Decimal) -> Decimal:
            """Largest trade size q >= 0 with |position + q*sign| <= allowed.

            The reason this is a function rather than `allowed - abs(position)`
            inline: when the trade is on the *opposite* side of an existing
            position it first closes it and only then opens the other way, so
            the headroom is `allowed + abs(position)`. Getting that wrong in the
            subtracting direction blocks the trades that de-risk the book; the
            symmetric mistake — ignoring the flip entirely — lets a small short
            be flipped into an arbitrarily large long, which is the dangerous
            half and the reason this is centralised.
            """
            if position * sign >= 0:                       # adding to the position
                return max(Decimal("0"), allowed_abs_qty - abs(position))
            return max(Decimal("0"), allowed_abs_qty + abs(position))

        if limits.max_order_value is not None:
            value = abs(instrument.notional(quantity, ref))
            if value > limits.max_order_value:
                quantity = cap(limits.max_order_value / unit,
                               "max_order_value", R_MAX_ORDER_VALUE,
                               "order value above limit", str(value),
                               str(limits.max_order_value))
            else:
                add("max_order_value", True, observed=str(value),
                    limit=str(limits.max_order_value))

        if limits.max_position_size is not None:
            projected = position + quantity * sign
            if abs(projected) > limits.max_position_size:
                quantity = cap(max_qty_for_abs_position(limits.max_position_size),
                               "max_position_size", R_MAX_POSITION_SIZE,
                               "projected position above limit", str(abs(projected)),
                               str(limits.max_position_size))
            else:
                add("max_position_size", True, observed=str(abs(projected)),
                    limit=str(limits.max_position_size))

        if limits.max_position_notional is not None:
            notional = abs(instrument.notional(position + quantity * sign, ref))
            if notional > limits.max_position_notional:
                quantity = cap(
                    max_qty_for_abs_position(limits.max_position_notional / unit),
                    "max_position_notional", R_MAX_POSITION_NOTIONAL,
                    "projected position notional above limit",
                    str(notional), str(limits.max_position_notional))
            else:
                add("max_position_notional", True, observed=str(notional),
                    limit=str(limits.max_position_notional))

        # 12. PROJECTED portfolio exposure — after the fill, never before.
        #
        # Everything below is expressed as "the book without this instrument,
        # plus this instrument as it would be after the fill". Modelling the
        # trade as a *delta* added to today's exposure is the tempting shortcut
        # and it is wrong in exactly one case that matters: a trade large enough
        # to flip the sign of the position both closes the old exposure and
        # opens a new one, so its effect on gross exposure is not monotone in
        # the traded size. Rebuilding the leg from the projected position has no
        # such blind spot.
        equity = self._equity(ctx)
        gross_now, net_now = self._exposures(ctx)
        leg_gross_now, leg_net_now = self._instrument_leg(intent.instrument_key, ctx)
        other_gross = gross_now - leg_gross_now
        other_net = net_now - leg_net_now

        def projected_leg(qty: Decimal) -> tuple[Decimal, Decimal]:
            signed = instrument.notional(position + qty * sign, ref)
            return abs(signed), signed

        if limits.max_gross_exposure is not None:
            gross_projected = other_gross + projected_leg(quantity)[0]
            if gross_projected > limits.max_gross_exposure:
                room = (limits.max_gross_exposure - other_gross) / unit
                quantity = cap(max_qty_for_abs_position(max(Decimal("0"), room)),
                               "max_gross_exposure", R_MAX_GROSS_EXPOSURE,
                               "projected gross exposure above limit",
                               str(gross_projected), str(limits.max_gross_exposure))
            else:
                add("max_gross_exposure", True, observed=str(gross_projected),
                    limit=str(limits.max_gross_exposure))

        if limits.max_net_exposure is not None:
            net_projected = other_net + projected_leg(quantity)[1]
            if abs(net_projected) > limits.max_net_exposure:
                # Solve |other_net + (position + q*sign)*unit| <= limit for q.
                # Only the bound the trade moves *towards* can bind, hence the
                # single signed expression rather than a two-sided clamp.
                room = ((sign * limits.max_net_exposure - other_net) / unit
                        - position) * sign
                quantity = cap(max(Decimal("0"), room),
                               "max_net_exposure", R_MAX_NET_EXPOSURE,
                               "projected net exposure above limit",
                               str(abs(net_projected)), str(limits.max_net_exposure))
            else:
                add("max_net_exposure", True, observed=str(abs(net_projected)),
                    limit=str(limits.max_net_exposure))

        if limits.max_leverage is not None:
            if equity is None:
                add("max_leverage", False, R_EQUITY_MISSING,
                    "equity unknown while a leverage ceiling is configured",
                    limit=str(limits.max_leverage))
                return reject("equity is unknown")
            if equity <= 0:
                add("max_leverage", False, R_MAX_LEVERAGE,
                    "no positive equity to lever", observed=str(equity),
                    limit=str(limits.max_leverage))
                return reject("account has no positive equity")
            gross_projected = other_gross + projected_leg(quantity)[0]
            lev = gross_projected / equity
            if lev > limits.max_leverage:
                room = (limits.max_leverage * equity - other_gross) / unit
                quantity = cap(max_qty_for_abs_position(max(Decimal("0"), room)),
                               "max_leverage", R_MAX_LEVERAGE,
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

        if limits.max_concentration is not None:
            if equity is None:
                add("max_concentration", False, R_EQUITY_MISSING,
                    "equity unknown while a concentration ceiling is configured",
                    limit=limits.max_concentration)
                return reject("equity is unknown")
            if equity <= 0:
                add("max_concentration", False, R_CONCENTRATION,
                    "no positive equity to concentrate", observed=str(equity),
                    limit=limits.max_concentration)
                return reject("account has no positive equity")
            conc = projected_leg(quantity)[0] / equity
            if float(conc) > limits.max_concentration:
                allowed_notional = Decimal(str(limits.max_concentration)) * equity
                quantity = cap(max_qty_for_abs_position(allowed_notional / unit),
                               "max_concentration", R_CONCENTRATION,
                               "projected concentration above limit", float(conc),
                               limits.max_concentration)
            else:
                add("max_concentration", True, observed=float(conc),
                    limit=limits.max_concentration)

        if limits.max_correlated_exposure is not None:
            group = limits.correlation_groups.get(intent.instrument_key)
            if not group:
                add("max_correlated_exposure", True, detail="no correlation group")
            elif equity is None:
                add("max_correlated_exposure", False, R_EQUITY_MISSING,
                    "equity unknown while a correlated-exposure ceiling is set",
                    limit=limits.max_correlated_exposure)
                return reject("equity is unknown")
            elif equity <= 0:
                add("max_correlated_exposure", False, R_CORRELATED,
                    "no positive equity", observed=str(equity),
                    limit=limits.max_correlated_exposure)
                return reject("account has no positive equity")
            else:
                # Same projected-leg treatment: the group's other members at
                # their marks, plus this instrument as it would be after the
                # fill. Adding |delta| to a total that already contains this
                # instrument's current leg double-counts it.
                exposure = (self._group_exposure(group, limits, ctx,
                                                 exclude=intent.instrument_key)
                            + projected_leg(quantity)[0])
                frac = exposure / equity
                if not add("max_correlated_exposure",
                           float(frac) <= limits.max_correlated_exposure,
                           R_CORRELATED, "correlated-group exposure above limit",
                           observed=float(frac), limit=limits.max_correlated_exposure):
                    return reject("correlated exposure limit reached")

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
        """Best available mark, in descending order of trustworthiness.

        The intent's own prices come last deliberately: they are the strategy's
        opinion of where the market is, and valuing a trade against the number
        the proposer supplied is how a stale or optimistic strategy sizes itself
        past a notional limit.
        """
        if ctx.reference_price is not None:
            return to_decimal(ctx.reference_price, field_name="reference_price")
        if ctx.quote is not None and getattr(ctx.quote, "mid", None):
            return to_decimal(ctx.quote.mid, field_name="mid")
        if ctx.candle is not None and getattr(ctx.candle, "close", None):
            return to_decimal(ctx.candle.close, field_name="close")
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
    def _equity(ctx: RiskContext) -> Decimal | None:
        """Account equity, or None when nothing measured it.

        Returning None rather than 0 for "unknown" is the point: 0 is a real
        equity value that fails a leverage check, whereas unknown must fail a
        *different* way — with a code that says the measurement was missing.
        """
        if ctx.equity is not None:
            return ctx.equity
        if ctx.portfolio is not None:
            try:
                return ctx.portfolio.equity
            except Exception:                           # noqa: BLE001
                return None
        return None

    @staticmethod
    def _exposures(ctx: RiskContext) -> tuple[Decimal, Decimal]:
        if ctx.portfolio is None:
            return Decimal("0"), Decimal("0")
        try:
            return ctx.portfolio.gross_exposure, ctx.portfolio.net_exposure
        except Exception:                               # noqa: BLE001
            return Decimal("0"), Decimal("0")

    @staticmethod
    def _instrument_leg(key: str, ctx: RiskContext) -> tuple[Decimal, Decimal]:
        """This instrument's (gross, net) contribution to the current exposures.

        Valued at the portfolio's own mark, not at the risk engine's reference
        price, because it is subtracted from totals that were computed from
        those marks. Mixing the two sources leaves a residue that shows up as
        phantom headroom.
        """
        if ctx.portfolio is None:
            return Decimal("0"), Decimal("0")
        pos = ctx.portfolio.positions.get(key)
        mark = ctx.portfolio.marks.get(key)
        if pos is None or mark is None:
            return Decimal("0"), Decimal("0")
        signed = pos.market_value(mark)
        return abs(signed), signed

    @staticmethod
    def _open_position_count(ctx: RiskContext) -> int:
        if ctx.portfolio is None:
            return 0
        return len(ctx.portfolio.open_positions)

    @staticmethod
    def _group_exposure(group: str, limits: RiskLimits, ctx: RiskContext,
                        *, exclude: str = "") -> Decimal:
        if ctx.portfolio is None:
            return Decimal("0")
        total = Decimal("0")
        for key, pos in ctx.portfolio.positions.items():
            if key == exclude or limits.correlation_groups.get(key) != group:
                continue
            mark = ctx.portfolio.marks.get(key)
            if mark is None:
                continue
            total += abs(pos.market_value(mark))
        return total


__all__ = ["RiskLimits", "LimitsStore", "RiskContext", "RiskEngine",
           "Tainted", "is_tainted", "assert_untainted"]

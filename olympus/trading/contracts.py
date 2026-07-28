"""Data contracts for the Olympus trading domain.

Every value that crosses a module boundary in this package is one of the frozen
dataclasses below. Modules never pass each other bare dicts, tuples, or
DataFrames; that is what keeps the seven layers (analysis → forecast → signal →
strategy → risk → execution → reconciliation) genuinely separable and
independently testable.

Two numeric domains, deliberately not mixed
-------------------------------------------
* **Analytics — `float`.** Candles, forecasts, indicators, features. Market
  data arrives as float, the models are float, and exactness is meaningless for
  a predicted price.
* **Accounting and execution — `Decimal`.** Order quantities and prices, fills,
  positions, cash, exposure, P&L. Anything that reaches a broker or a ledger
  must be exact; binary floats silently produce quantities brokers reject and
  P&L that fails to reconcile to the cent.

The conversion happens in exactly one place — position sizing — and always via
`Instrument.quantize_quantity` / `quantize_price`, so the rounding rule is
declared once rather than re-improvised per call site.

Time
----
Every timestamp is timezone-aware UTC. `ensure_utc` rejects naive datetimes at
construction. A naive timestamp in a trading system is a latent correctness bug
(is that exchange-local? server-local? UTC?), so there is no permissive path.

Serialisation
-------------
Every contract exposes `to_dict()` producing a JSON-encodable mapping, because
every one of them ends up in the immutable audit trail. `Decimal` serialises to
a string (never a float — that would defeat the point), `datetime` to ISO-8601.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_EVEN, InvalidOperation
from enum import Enum
from typing import Any, Mapping, Sequence

from .errors import ConfigurationError, DataValidationError

#: Bumped when a contract changes shape incompatibly. Written into every
#: audit record so old trails stay interpretable.
SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def ensure_utc(value: datetime, *, field_name: str = "timestamp") -> datetime:
    """Return `value` as timezone-aware UTC, rejecting naive datetimes."""
    if not isinstance(value, datetime):
        raise DataValidationError(
            f"{field_name} must be a datetime", field=field_name,
            got=type(value).__name__)
    if value.tzinfo is None:
        raise DataValidationError(
            f"{field_name} must be timezone-aware (UTC); naive datetimes are "
            "ambiguous and are never accepted", field=field_name)
    return value.astimezone(timezone.utc)


def to_decimal(value: Any, *, field_name: str = "value") -> Decimal:
    """Coerce to Decimal without ever routing through binary float.

    `Decimal(0.1)` is 0.1000000000000000055511151231257827; `Decimal("0.1")` is
    exactly 0.1. Floats are therefore stringified first via `repr`, which gives
    the shortest round-tripping representation.
    """
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise DataValidationError(
                f"{field_name} must be finite", field=field_name, got=repr(value))
        result = Decimal(repr(value))
    elif isinstance(value, str):
        try:
            result = Decimal(value)
        except InvalidOperation:
            raise DataValidationError(
                f"{field_name} is not a valid decimal", field=field_name,
                got=value) from None
    else:
        raise DataValidationError(
            f"{field_name} must be a number", field=field_name,
            got=type(value).__name__)
    if not result.is_finite():
        raise DataValidationError(
            f"{field_name} must be finite", field=field_name, got=str(result))
    return result


def jsonable(value: Any) -> Any:
    """Recursively convert a contract value into something `json.dumps` accepts."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, float):
        # NaN/Inf are not valid JSON; surface them as null rather than emitting
        # a document that only Python's lenient parser will read back.
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


class _Contract:
    """Mixin giving every contract a uniform, JSON-safe `to_dict()`."""

    def to_dict(self) -> dict:
        return {k: jsonable(v) for k, v in asdict(self).items()}


# ---------------------------------------------------------------------------
# enumerations
# ---------------------------------------------------------------------------

class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        """+1 for BUY, -1 for SELL — the direction of the position delta."""
        return 1 if self is Side.BUY else -1

    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class TimeInForce(str, Enum):
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"


class OrderStatus(str, Enum):
    #: Created locally, not yet acknowledged by the broker. An order in this
    #: state may or may not exist upstream — recovery must query, never assume.
    PENDING_NEW = "pending_new"
    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    PENDING_CANCEL = "pending_cancel"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELED,
                        OrderStatus.REJECTED, OrderStatus.EXPIRED)

    @property
    def is_open(self) -> bool:
        return not self.is_terminal


class Mode(str, Enum):
    """Operating modes, ordered by how much real money they can move."""
    BACKTEST = "backtest"
    PAPER = "paper"
    SHADOW = "shadow"
    LIVE_RESTRICTED = "live_restricted"
    LIVE_BOUNDED = "live_bounded"

    @property
    def is_live(self) -> bool:
        """True when real orders reach a real venue."""
        return self in (Mode.LIVE_RESTRICTED, Mode.LIVE_BOUNDED)


class DataQuality(str, Enum):
    OK = "ok"
    #: Usable but flawed (a gap was interpolated, a bar was late). Strategies
    #: may trade on it; the risk engine may be configured not to.
    DEGRADED = "degraded"
    #: Not usable for trading decisions under any configuration.
    UNUSABLE = "unusable"


class Regime(str, Enum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGING = "ranging"
    HIGH_VOLATILITY = "high_volatility"
    CRISIS = "crisis"
    UNKNOWN = "unknown"


class SignalDirection(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class Verdict(str, Enum):
    APPROVED = "approved"
    #: Approved but with the quantity reduced to fit a limit.
    APPROVED_REDUCED = "approved_reduced"
    REJECTED = "rejected"


# ---------------------------------------------------------------------------
# instruments
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Instrument(_Contract):
    """A tradable instrument and its venue-imposed arithmetic.

    `tick_size` and `lot_size` are not cosmetic: submitting a price or quantity
    off-grid is one of the most common causes of broker rejection, so every
    quantity that reaches an order passes through the quantisers below.
    """
    symbol: str
    exchange: str
    asset_class: str = "equity"          # equity | crypto | fx | future | etf
    quote_currency: str = "USD"
    tick_size: Decimal = Decimal("0.01")
    lot_size: Decimal = Decimal("1")
    min_quantity: Decimal = Decimal("1")
    min_notional: Decimal = Decimal("0")
    #: Contract multiplier (futures/options). 1 for cash instruments.
    multiplier: Decimal = Decimal("1")
    #: Whether fractional quantities are permitted at all.
    allow_fractional: bool = False

    def __post_init__(self):
        if not self.symbol or not str(self.symbol).strip():
            raise ConfigurationError("instrument symbol must be non-empty")
        if not self.exchange or not str(self.exchange).strip():
            raise ConfigurationError("instrument exchange must be non-empty",
                                     symbol=self.symbol)
        for name in ("tick_size", "lot_size", "min_quantity", "multiplier"):
            object.__setattr__(self, name, to_decimal(getattr(self, name),
                                                      field_name=name))
        object.__setattr__(self, "min_notional",
                           to_decimal(self.min_notional, field_name="min_notional"))
        if self.tick_size <= 0:
            raise ConfigurationError("tick_size must be > 0", symbol=self.symbol)
        if self.lot_size <= 0:
            raise ConfigurationError("lot_size must be > 0", symbol=self.symbol)
        if self.multiplier <= 0:
            raise ConfigurationError("multiplier must be > 0", symbol=self.symbol)
        if self.min_notional < 0:
            raise ConfigurationError("min_notional must be >= 0", symbol=self.symbol)

    @property
    def key(self) -> str:
        """Stable identity used as a dict key and in audit records."""
        return f"{self.exchange}:{self.symbol}"

    def quantize_quantity(self, quantity: Any) -> Decimal:
        """Round a desired quantity DOWN onto the lot grid.

        Always down: rounding a size up can breach the very exposure limit that
        produced the number, so the conservative direction is the only safe
        default.
        """
        q = to_decimal(quantity, field_name="quantity")
        if q < 0:
            raise DataValidationError("quantity must be >= 0", got=str(q))
        steps = (q / self.lot_size).to_integral_value(rounding=ROUND_DOWN)
        return (steps * self.lot_size).normalize() + Decimal(0)

    def quantize_price(self, price: Any) -> Decimal:
        """Round a price onto the tick grid (half-even: unbiased over time)."""
        p = to_decimal(price, field_name="price")
        steps = (p / self.tick_size).to_integral_value(rounding=ROUND_HALF_EVEN)
        return (steps * self.tick_size).normalize() + Decimal(0)

    def notional(self, quantity: Any, price: Any) -> Decimal:
        """Cash value of `quantity` at `price`, including the multiplier."""
        return (to_decimal(quantity, field_name="quantity")
                * to_decimal(price, field_name="price") * self.multiplier)


# ---------------------------------------------------------------------------
# market data
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Candle(_Contract):
    """One OHLCV bar.

    `ts_open` is the bar's *start*; `ts_close` its end. Storing both removes the
    single most common source of off-by-one-bar look-ahead: code that reads a
    bar stamped with its open time and assumes the close price was knowable at
    that instant. Nothing in this package may act on a bar until the clock has
    passed `ts_close` and `is_final` is true.

    OHLC coherence is enforced here, at construction. The upstream Kronos model
    emits incoherent candles (teardown §12.7) — this is the boundary where that
    is caught rather than propagated into a portfolio.
    """
    instrument_key: str
    timeframe: str                       # "1m" | "5m" | "1h" | "1d" ...
    ts_open: datetime
    ts_close: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    amount: float = 0.0
    #: False for the in-progress bar being aggregated from live ticks.
    is_final: bool = True
    source: str = "unknown"

    def __post_init__(self):
        object.__setattr__(self, "ts_open", ensure_utc(self.ts_open, field_name="ts_open"))
        object.__setattr__(self, "ts_close", ensure_utc(self.ts_close, field_name="ts_close"))
        if self.ts_close <= self.ts_open:
            raise DataValidationError(
                "candle ts_close must be after ts_open",
                instrument=self.instrument_key,
                ts_open=self.ts_open.isoformat(), ts_close=self.ts_close.isoformat())
        for name in ("open", "high", "low", "close", "volume", "amount"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise DataValidationError(f"candle {name} must be numeric",
                                          field=name, got=type(value).__name__)
            value = float(value)
            if not math.isfinite(value):
                raise DataValidationError(f"candle {name} must be finite",
                                          field=name, got=repr(value))
            object.__setattr__(self, name, value)
        for name in ("open", "high", "low", "close"):
            if getattr(self, name) <= 0:
                raise DataValidationError(
                    f"candle {name} must be > 0", field=name,
                    instrument=self.instrument_key, got=getattr(self, name))
        for name in ("volume", "amount"):
            if getattr(self, name) < 0:
                raise DataValidationError(
                    f"candle {name} must be >= 0", field=name,
                    instrument=self.instrument_key, got=getattr(self, name))
        body_high = max(self.open, self.close)
        body_low = min(self.open, self.close)
        if self.high < body_high - 1e-9:
            raise DataValidationError(
                "candle high is below open/close", instrument=self.instrument_key,
                high=self.high, open=self.open, close=self.close)
        if self.low > body_low + 1e-9:
            raise DataValidationError(
                "candle low is above open/close", instrument=self.instrument_key,
                low=self.low, open=self.open, close=self.close)
        if self.high < self.low:
            raise DataValidationError(
                "candle high is below low", instrument=self.instrument_key,
                high=self.high, low=self.low)

    @property
    def typical_price(self) -> float:
        return (self.high + self.low + self.close) / 3.0

    @property
    def range(self) -> float:
        return self.high - self.low

    def with_final(self, is_final: bool) -> "Candle":
        return Candle(
            instrument_key=self.instrument_key, timeframe=self.timeframe,
            ts_open=self.ts_open, ts_close=self.ts_close, open=self.open,
            high=self.high, low=self.low, close=self.close, volume=self.volume,
            amount=self.amount, is_final=is_final, source=self.source)


@dataclass(frozen=True)
class Quote(_Contract):
    """Top-of-book snapshot. Drives spread and liquidity risk checks."""
    instrument_key: str
    ts: datetime
    bid: float
    ask: float
    bid_size: float = 0.0
    ask_size: float = 0.0
    source: str = "unknown"

    def __post_init__(self):
        object.__setattr__(self, "ts", ensure_utc(self.ts, field_name="ts"))
        for name in ("bid", "ask", "bid_size", "ask_size"):
            value = float(getattr(self, name))
            if not math.isfinite(value):
                raise DataValidationError(f"quote {name} must be finite", field=name)
            object.__setattr__(self, name, value)
        if self.bid <= 0 or self.ask <= 0:
            raise DataValidationError("quote bid/ask must be > 0",
                                      instrument=self.instrument_key,
                                      bid=self.bid, ask=self.ask)
        if self.ask < self.bid:
            raise DataValidationError(
                "crossed quote: ask below bid", instrument=self.instrument_key,
                bid=self.bid, ask=self.ask)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def spread_bps(self) -> float:
        mid = self.mid
        return (self.spread / mid) * 10_000.0 if mid > 0 else float("inf")


@dataclass(frozen=True)
class DataQualityReport(_Contract):
    """The verdict of `validate.py` on a window of market data."""
    instrument_key: str
    timeframe: str
    status: DataQuality
    bars_expected: int
    bars_present: int
    gaps: int = 0
    duplicates_removed: int = 0
    out_of_order_removed: int = 0
    invalid_dropped: int = 0
    newest_ts: datetime | None = None
    oldest_ts: datetime | None = None
    age_seconds: float | None = None
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def __post_init__(self):
        if self.newest_ts is not None:
            object.__setattr__(self, "newest_ts",
                               ensure_utc(self.newest_ts, field_name="newest_ts"))
        if self.oldest_ts is not None:
            object.__setattr__(self, "oldest_ts",
                               ensure_utc(self.oldest_ts, field_name="oldest_ts"))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "errors", tuple(self.errors))

    @property
    def usable(self) -> bool:
        return self.status is not DataQuality.UNUSABLE

    @property
    def completeness(self) -> float:
        if self.bars_expected <= 0:
            return 0.0
        return min(1.0, self.bars_present / self.bars_expected)


# ---------------------------------------------------------------------------
# forecasting
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ForecastPath(_Contract):
    """One sampled future. Kept individually — averaging is a *view*, not the
    result. The upstream Kronos implementation collapses samples to a mean
    inside its inference function (teardown §12.7), destroying exactly the
    dispersion information a risk engine needs."""
    closes: tuple[float, ...]
    opens: tuple[float, ...] = ()
    highs: tuple[float, ...] = ()
    lows: tuple[float, ...] = ()
    volumes: tuple[float, ...] = ()

    def __post_init__(self):
        for name in ("closes", "opens", "highs", "lows", "volumes"):
            object.__setattr__(self, name, tuple(float(v) for v in getattr(self, name)))
        if not self.closes:
            raise DataValidationError("forecast path must have at least one close")


@dataclass(frozen=True)
class ForecastResult(_Contract):
    """The forecasting layer's complete, self-describing output.

    Every field the caller needs to decide *whether to trust this* travels with
    the numbers: which model produced it, from which data window, how uncertain
    it is, what went wrong, and whether the forecaster abstained. A consumer
    that ignores `abstained` / `data_quality` and reads `mean_close` anyway is
    making a visible mistake rather than an invisible one.
    """
    instrument_key: str
    timeframe: str
    #: First and last timestamp of the INPUT window (not the forecast).
    input_start: datetime
    input_end: datetime
    #: When the forecast was produced.
    created_at: datetime
    horizon: int
    #: Timestamps the forecast refers to (len == horizon).
    forecast_timestamps: tuple[datetime, ...] = ()
    paths: tuple[ForecastPath, ...] = ()
    mean_close: tuple[float, ...] = ()
    median_close: tuple[float, ...] = ()
    #: {quantile_label: per-step values}, e.g. {"p05": (...), "p95": (...)}.
    quantiles: Mapping[str, tuple[float, ...]] = field(default_factory=dict)
    #: Expected simple return over the whole horizon, from the last input close.
    expected_return: float = 0.0
    #: Per-step expected returns (len == horizon).
    expected_return_path: tuple[float, ...] = ()
    #: Annualisation-free stdev of terminal returns across paths.
    forecast_volatility: float = 0.0
    #: Realised volatility of the input window, for comparison.
    realised_volatility: float = 0.0
    #: {"up": p, "down": p, "flat": p} — sums to 1.
    direction_probabilities: Mapping[str, float] = field(default_factory=dict)
    #: 0 = fully confident, 1 = no information. Monotone in path dispersion.
    uncertainty: float = 1.0
    model_version: str = "unknown"
    #: Checkpoint identity: repo id + revision + content hash where available.
    model_identity: Mapping[str, Any] = field(default_factory=dict)
    #: Sampling parameters actually used, so a run is reproducible from the record.
    inference_params: Mapping[str, Any] = field(default_factory=dict)
    data_quality: DataQuality = DataQuality.OK
    warnings: tuple[str, ...] = ()
    abstained: bool = False
    failure_reason: str | None = None
    failure_code: str | None = None
    latency_ms: float | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        for name in ("input_start", "input_end", "created_at"):
            object.__setattr__(self, name, ensure_utc(getattr(self, name), field_name=name))
        if self.input_end < self.input_start:
            raise DataValidationError("forecast input_end precedes input_start")
        if self.horizon < 0:
            raise DataValidationError("forecast horizon must be >= 0",
                                      got=self.horizon)
        object.__setattr__(self, "paths", tuple(self.paths))
        object.__setattr__(self, "forecast_timestamps",
                           tuple(ensure_utc(t, field_name="forecast_timestamp")
                                 for t in self.forecast_timestamps))
        for name in ("mean_close", "median_close", "expected_return_path"):
            object.__setattr__(self, name,
                               tuple(float(v) for v in getattr(self, name)))
        object.__setattr__(self, "quantiles", {
            str(k): tuple(float(x) for x in v) for k, v in dict(self.quantiles).items()})
        object.__setattr__(self, "direction_probabilities",
                           {str(k): float(v)
                            for k, v in dict(self.direction_probabilities).items()})
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "model_identity", dict(self.model_identity))
        object.__setattr__(self, "inference_params", dict(self.inference_params))
        if not 0.0 <= self.uncertainty <= 1.0:
            raise DataValidationError("uncertainty must be within [0, 1]",
                                      got=self.uncertainty)
        if self.abstained and not self.failure_reason:
            raise DataValidationError(
                "an abstaining forecast must carry a failure_reason")
        if not self.abstained:
            if self.horizon and len(self.mean_close) != self.horizon:
                raise DataValidationError(
                    "mean_close length must equal horizon",
                    horizon=self.horizon, got=len(self.mean_close))
            if self.forecast_timestamps and len(self.forecast_timestamps) != self.horizon:
                raise DataValidationError(
                    "forecast_timestamps length must equal horizon",
                    horizon=self.horizon, got=len(self.forecast_timestamps))

    @property
    def usable(self) -> bool:
        """Safe for a strategy to act on."""
        return (not self.abstained
                and self.data_quality is not DataQuality.UNUSABLE
                and bool(self.mean_close))

    @property
    def n_paths(self) -> int:
        return len(self.paths)

    @property
    def confidence(self) -> float:
        """Convenience inverse of uncertainty, for readability at call sites."""
        return 1.0 - self.uncertainty

    @classmethod
    def abstain(cls, *, instrument_key: str, timeframe: str,
                input_start: datetime, input_end: datetime, created_at: datetime,
                horizon: int, reason: str, code: str,
                data_quality: DataQuality = DataQuality.UNUSABLE,
                warnings: Sequence[str] = (), model_version: str = "unknown",
                model_identity: Mapping[str, Any] | None = None,
                inference_params: Mapping[str, Any] | None = None,
                ) -> "ForecastResult":
        """Build the canonical 'I decline to forecast' result.

        A constructor rather than an exception because abstention is the normal,
        expected outcome on bad data — callers should handle it in the main
        path, and it must be recorded in the audit trail like any other result.
        """
        return cls(
            instrument_key=instrument_key, timeframe=timeframe,
            input_start=input_start, input_end=input_end, created_at=created_at,
            horizon=horizon, abstained=True, failure_reason=reason,
            failure_code=code, data_quality=data_quality, uncertainty=1.0,
            warnings=tuple(warnings), model_version=model_version,
            model_identity=dict(model_identity or {}),
            inference_params=dict(inference_params or {}))


# ---------------------------------------------------------------------------
# signals and intents
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Signal(_Contract):
    """One analyst's opinion. Never an order; not even a proposal."""
    source: str                          # "forecast" | "ta.rsi" | "regime" ...
    instrument_key: str
    direction: SignalDirection
    #: Signed conviction in [-1, 1]; sign must agree with `direction`.
    strength: float
    confidence: float                    # [0, 1]
    ts: datetime
    horizon: int = 0
    rationale: str = ""
    features: Mapping[str, Any] = field(default_factory=dict)
    model_version: str = ""
    data_quality: DataQuality = DataQuality.OK

    def __post_init__(self):
        object.__setattr__(self, "ts", ensure_utc(self.ts, field_name="ts"))
        object.__setattr__(self, "strength", float(self.strength))
        object.__setattr__(self, "confidence", float(self.confidence))
        object.__setattr__(self, "features", dict(self.features))
        if not -1.0 <= self.strength <= 1.0:
            raise DataValidationError("signal strength must be within [-1, 1]",
                                      source=self.source, got=self.strength)
        if not 0.0 <= self.confidence <= 1.0:
            raise DataValidationError("signal confidence must be within [0, 1]",
                                      source=self.source, got=self.confidence)
        if self.direction is SignalDirection.LONG and self.strength < 0:
            raise DataValidationError("LONG signal cannot have negative strength",
                                      source=self.source, got=self.strength)
        if self.direction is SignalDirection.SHORT and self.strength > 0:
            raise DataValidationError("SHORT signal cannot have positive strength",
                                      source=self.source, got=self.strength)


@dataclass(frozen=True)
class TradeIntent(_Contract):
    """A strategy's *proposal*. It is not an order and cannot become one
    without passing the deterministic risk engine.

    The provenance fields (`data_ts_*`, `model_versions`, `supporting_signals`,
    `strategy_version`) are mandatory-by-convention rather than optional extras:
    they are what makes an executed order traceable back to the market data that
    caused it, which is the whole point of the audit requirement.
    """
    intent_id: str
    strategy_id: str
    strategy_version: str
    instrument_key: str
    side: Side
    order_type: OrderType
    #: Proposed size. The risk engine may reduce it; it may never increase it.
    quantity: Decimal
    created_at: datetime
    intended_entry: Decimal | None = None
    limit_price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.DAY
    max_holding_period_s: int | None = None
    confidence: float = 0.0
    supporting_signals: tuple[Signal, ...] = ()
    invalidating_conditions: tuple[str, ...] = ()
    #: Provenance of the market data this decision was made from.
    data_ts_start: datetime | None = None
    data_ts_end: datetime | None = None
    model_versions: Mapping[str, str] = field(default_factory=dict)
    explanation: str = ""
    reason_codes: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        object.__setattr__(self, "created_at",
                           ensure_utc(self.created_at, field_name="created_at"))
        for name in ("data_ts_start", "data_ts_end"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, ensure_utc(value, field_name=name))
        object.__setattr__(self, "quantity",
                           to_decimal(self.quantity, field_name="quantity"))
        for name in ("intended_entry", "limit_price", "stop_loss", "take_profit"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, to_decimal(value, field_name=name))
        object.__setattr__(self, "supporting_signals", tuple(self.supporting_signals))
        object.__setattr__(self, "invalidating_conditions",
                           tuple(self.invalidating_conditions))
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        object.__setattr__(self, "model_versions", dict(self.model_versions))
        object.__setattr__(self, "metadata", dict(self.metadata))
        object.__setattr__(self, "confidence", float(self.confidence))

        if not self.intent_id:
            raise DataValidationError("intent_id must be non-empty")
        if self.quantity <= 0:
            raise DataValidationError("intent quantity must be > 0",
                                      intent_id=self.intent_id,
                                      got=str(self.quantity))
        if not 0.0 <= self.confidence <= 1.0:
            raise DataValidationError("intent confidence must be within [0, 1]",
                                      intent_id=self.intent_id,
                                      got=self.confidence)
        if self.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) \
                and self.limit_price is None:
            raise DataValidationError(
                f"{self.order_type.value} intent requires a limit_price",
                intent_id=self.intent_id)
        if self.max_holding_period_s is not None and self.max_holding_period_s <= 0:
            raise DataValidationError("max_holding_period_s must be > 0",
                                      intent_id=self.intent_id)
        # A stop on the wrong side of the entry is not protection, it is an
        # instant exit. Catch it here rather than at the broker.
        if self.stop_loss is not None and self.intended_entry is not None:
            if self.side is Side.BUY and self.stop_loss >= self.intended_entry:
                raise DataValidationError(
                    "BUY stop_loss must be below intended_entry",
                    intent_id=self.intent_id, stop=str(self.stop_loss),
                    entry=str(self.intended_entry))
            if self.side is Side.SELL and self.stop_loss <= self.intended_entry:
                raise DataValidationError(
                    "SELL stop_loss must be above intended_entry",
                    intent_id=self.intent_id, stop=str(self.stop_loss),
                    entry=str(self.intended_entry))
        if self.take_profit is not None and self.intended_entry is not None:
            if self.side is Side.BUY and self.take_profit <= self.intended_entry:
                raise DataValidationError(
                    "BUY take_profit must be above intended_entry",
                    intent_id=self.intent_id)
            if self.side is Side.SELL and self.take_profit >= self.intended_entry:
                raise DataValidationError(
                    "SELL take_profit must be below intended_entry",
                    intent_id=self.intent_id)

    def with_quantity(self, quantity: Decimal) -> "TradeIntent":
        """Return a copy with a different size (used by risk down-sizing)."""
        data = {f: getattr(self, f) for f in self.__dataclass_fields__}
        data["quantity"] = to_decimal(quantity, field_name="quantity")
        return TradeIntent(**data)


# ---------------------------------------------------------------------------
# risk
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RiskCheck(_Contract):
    """The outcome of one named check. Every check that ran is recorded —
    passes as well as failures — so the audit trail proves what was evaluated,
    not merely what tripped."""
    name: str
    passed: bool
    #: Stable reason code, e.g. "MAX_ORDER_VALUE_EXCEEDED".
    code: str = ""
    detail: str = ""
    observed: Any = None
    limit: Any = None

    def to_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed, "code": self.code,
                "detail": self.detail, "observed": jsonable(self.observed),
                "limit": jsonable(self.limit)}


@dataclass(frozen=True)
class RiskDecision(_Contract):
    """The deterministic risk engine's authorisation record.

    `approved_quantity` may be smaller than the intent's quantity but never
    larger — the engine's authority is strictly to reduce or refuse.
    """
    intent_id: str
    verdict: Verdict
    checks: tuple[RiskCheck, ...]
    approved_quantity: Decimal
    decided_at: datetime
    mode: Mode
    #: Hash of the limit set in force, so a decision can be replayed against
    #: exactly the policy that produced it.
    policy_hash: str = ""
    reason_codes: tuple[str, ...] = ()
    explanation: str = ""
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        object.__setattr__(self, "decided_at",
                           ensure_utc(self.decided_at, field_name="decided_at"))
        object.__setattr__(self, "checks", tuple(self.checks))
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        object.__setattr__(self, "approved_quantity",
                           to_decimal(self.approved_quantity,
                                      field_name="approved_quantity"))
        if self.approved_quantity < 0:
            raise DataValidationError("approved_quantity must be >= 0")
        if self.verdict is Verdict.REJECTED and self.approved_quantity != 0:
            raise DataValidationError(
                "a rejected decision must approve zero quantity",
                intent_id=self.intent_id, got=str(self.approved_quantity))
        if self.verdict is not Verdict.REJECTED and self.approved_quantity <= 0:
            raise DataValidationError(
                "an approving decision must approve a positive quantity",
                intent_id=self.intent_id)

    @property
    def approved(self) -> bool:
        return self.verdict is not Verdict.REJECTED

    @property
    def failed_checks(self) -> tuple[RiskCheck, ...]:
        return tuple(c for c in self.checks if not c.passed)


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Order(_Contract):
    """An order as Olympus knows it.

    `client_order_id` is the idempotency key and the join key to the broker.
    It is derived deterministically from the authorising decision, so a retry
    after a timeout or a process restart produces the same key and cannot
    double-submit.
    """
    client_order_id: str
    instrument_key: str
    side: Side
    order_type: OrderType
    quantity: Decimal
    created_at: datetime
    status: OrderStatus = OrderStatus.PENDING_NEW
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.DAY
    filled_quantity: Decimal = Decimal("0")
    average_fill_price: Decimal | None = None
    broker_order_id: str | None = None
    updated_at: datetime | None = None
    #: Provenance — the chain back to the cause of this order.
    intent_id: str = ""
    decision_id: str = ""
    strategy_id: str = ""
    mode: Mode = Mode.PAPER
    reject_reason: str = ""
    fees: Decimal = Decimal("0")
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        object.__setattr__(self, "created_at",
                           ensure_utc(self.created_at, field_name="created_at"))
        if self.updated_at is not None:
            object.__setattr__(self, "updated_at",
                               ensure_utc(self.updated_at, field_name="updated_at"))
        for name in ("quantity", "filled_quantity", "fees"):
            object.__setattr__(self, name,
                               to_decimal(getattr(self, name), field_name=name))
        for name in ("limit_price", "stop_price", "average_fill_price"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, to_decimal(value, field_name=name))
        if not self.client_order_id:
            raise DataValidationError("client_order_id must be non-empty")
        if self.quantity <= 0:
            raise DataValidationError("order quantity must be > 0",
                                      client_order_id=self.client_order_id)
        if self.filled_quantity < 0 or self.filled_quantity > self.quantity:
            raise DataValidationError(
                "filled_quantity must be within [0, quantity]",
                client_order_id=self.client_order_id,
                filled=str(self.filled_quantity), quantity=str(self.quantity))
        if self.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) \
                and self.limit_price is None:
            raise DataValidationError(
                f"{self.order_type.value} order requires a limit_price",
                client_order_id=self.client_order_id)
        if self.order_type in (OrderType.STOP, OrderType.STOP_LIMIT) \
                and self.stop_price is None:
            raise DataValidationError(
                f"{self.order_type.value} order requires a stop_price",
                client_order_id=self.client_order_id)

    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity - self.filled_quantity

    @property
    def is_open(self) -> bool:
        return self.status.is_open

    def evolve(self, **changes) -> "Order":
        data = {f: getattr(self, f) for f in self.__dataclass_fields__}
        data.update(changes)
        return Order(**data)


@dataclass(frozen=True)
class Fill(_Contract):
    """An execution. Fills are the only thing that moves a position."""
    fill_id: str
    client_order_id: str
    instrument_key: str
    side: Side
    quantity: Decimal
    price: Decimal
    ts: datetime
    fee: Decimal = Decimal("0")
    fee_currency: str = "USD"
    broker_fill_id: str | None = None
    liquidity: str = ""                  # "maker" | "taker" | ""

    def __post_init__(self):
        object.__setattr__(self, "ts", ensure_utc(self.ts, field_name="ts"))
        for name in ("quantity", "price", "fee"):
            object.__setattr__(self, name,
                               to_decimal(getattr(self, name), field_name=name))
        if self.quantity <= 0:
            raise DataValidationError("fill quantity must be > 0",
                                      fill_id=self.fill_id)
        if self.price <= 0:
            raise DataValidationError("fill price must be > 0",
                                      fill_id=self.fill_id)
        if self.fee < 0:
            raise DataValidationError("fill fee must be >= 0", fill_id=self.fill_id)

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.price

    @property
    def signed_quantity(self) -> Decimal:
        return self.quantity * self.side.sign


@dataclass(frozen=True)
class Position(_Contract):
    """A net position with its cost basis and realised P&L."""
    instrument_key: str
    quantity: Decimal = Decimal("0")     # signed: negative = short
    average_price: Decimal = Decimal("0")
    realised_pnl: Decimal = Decimal("0")
    fees_paid: Decimal = Decimal("0")
    opened_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self):
        for name in ("quantity", "average_price", "realised_pnl", "fees_paid"):
            object.__setattr__(self, name,
                               to_decimal(getattr(self, name), field_name=name))
        for name in ("opened_at", "updated_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, ensure_utc(value, field_name=name))

    @property
    def is_flat(self) -> bool:
        return self.quantity == 0

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    @property
    def is_short(self) -> bool:
        return self.quantity < 0

    def market_value(self, price: Decimal | float) -> Decimal:
        return self.quantity * to_decimal(price, field_name="price")

    def unrealised_pnl(self, price: Decimal | float) -> Decimal:
        return (to_decimal(price, field_name="price") - self.average_price) * self.quantity


@dataclass(frozen=True)
class AccountSnapshot(_Contract):
    """Broker-reported account state, as of `ts`."""
    ts: datetime
    cash: Decimal
    equity: Decimal
    currency: str = "USD"
    buying_power: Decimal | None = None
    margin_used: Decimal = Decimal("0")
    source: str = "unknown"

    def __post_init__(self):
        object.__setattr__(self, "ts", ensure_utc(self.ts, field_name="ts"))
        for name in ("cash", "equity", "margin_used"):
            object.__setattr__(self, name,
                               to_decimal(getattr(self, name), field_name=name))
        if self.buying_power is not None:
            object.__setattr__(self, "buying_power",
                               to_decimal(self.buying_power, field_name="buying_power"))


@dataclass(frozen=True)
class PortfolioSnapshot(_Contract):
    """Olympus's own view of the portfolio at a point in time."""
    ts: datetime
    cash: Decimal
    positions: Mapping[str, Position] = field(default_factory=dict)
    marks: Mapping[str, float] = field(default_factory=dict)
    currency: str = "USD"
    realised_pnl: Decimal = Decimal("0")
    fees_paid: Decimal = Decimal("0")

    def __post_init__(self):
        object.__setattr__(self, "ts", ensure_utc(self.ts, field_name="ts"))
        for name in ("cash", "realised_pnl", "fees_paid"):
            object.__setattr__(self, name,
                               to_decimal(getattr(self, name), field_name=name))
        object.__setattr__(self, "positions", dict(self.positions))
        object.__setattr__(self, "marks", {k: float(v) for k, v in dict(self.marks).items()})

    @property
    def gross_exposure(self) -> Decimal:
        total = Decimal("0")
        for key, pos in self.positions.items():
            mark = self.marks.get(key)
            if mark is None:
                continue
            total += abs(pos.market_value(mark))
        return total

    @property
    def net_exposure(self) -> Decimal:
        total = Decimal("0")
        for key, pos in self.positions.items():
            mark = self.marks.get(key)
            if mark is None:
                continue
            total += pos.market_value(mark)
        return total

    @property
    def unrealised_pnl(self) -> Decimal:
        total = Decimal("0")
        for key, pos in self.positions.items():
            mark = self.marks.get(key)
            if mark is None:
                continue
            total += pos.unrealised_pnl(mark)
        return total

    @property
    def equity(self) -> Decimal:
        return self.cash + self.net_exposure

    @property
    def open_positions(self) -> dict[str, Position]:
        return {k: p for k, p in self.positions.items() if not p.is_flat}


__all__ = [
    "SCHEMA_VERSION", "ensure_utc", "to_decimal", "jsonable",
    "Side", "OrderType", "TimeInForce", "OrderStatus", "Mode", "DataQuality",
    "Regime", "SignalDirection", "Verdict",
    "Instrument", "Candle", "Quote", "DataQualityReport",
    "ForecastPath", "ForecastResult", "Signal", "TradeIntent",
    "RiskCheck", "RiskDecision", "Order", "Fill", "Position",
    "AccountSnapshot", "PortfolioSnapshot",
]

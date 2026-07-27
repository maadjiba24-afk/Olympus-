"""Strategies: the layer that turns opinions into *proposals*.

A strategy's entire output is a list of `TradeIntent`, which is an inert value
object. It cannot become an order without passing `risk.RiskEngine`, and this
module is structurally incapable of shortening that path: it imports no broker,
no OMS, no execution engine, and no kill-switch registry (the layer graph in
docs/TRADING_ARCHITECTURE.md §6 forbids it, and
tests/test_trading_boundaries.py fails the build if that ever changes).

The same discipline is applied to what a strategy can *see*. `StrategyContext`
is a closed set of fields — market data, features, a forecast, a fused signal,
regime, volatility, a portfolio snapshot, a quote, and the clock. There is no
`broker`, no `risk_engine`, no `limits` attribute to reach through, so "a model
cannot submit an order" is a property of the type rather than of reviewer
vigilance. `StrategyContext.coerce` deliberately *drops* any extra attribute it
finds on the object handed to it, so a caller cannot widen the surface by
passing a richer namespace.

Why a Kronos strategy and a non-Kronos twin
-------------------------------------------
`KronosMomentumStrategy` and `BaselineMomentumStrategy` inherit their sizing,
stop placement, take-profit placement, position-stacking rule and intent
construction from the same base class. The *only* difference between them is
where the directional view comes from: a `ForecastResult` versus arithmetic on
closes. That is not tidiness, it is the experimental design — `evaluate.py`
compares the two and answers "does Kronos add anything?", and the answer is
only meaningful if the signal source is the single free variable.

Sizing
------
Sizing is the one place where the float analytics domain meets the Decimal
accounting domain. Every quantity leaves this module as a `Decimal` that has
been through `Instrument.quantize_quantity` (which rounds DOWN), because a size
rounded up can breach the exposure limit that produced it.

Every intent carries a stop loss. `RiskLimits.require_stop_loss` defaults to
true, so an intent without one is rejected downstream anyway; producing one
here means the strategy states its own invalidation level rather than having a
generic one imposed on it.
"""

from __future__ import annotations

import hashlib
import json
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from .clock import Clock, default_clock
from .contracts import (Candle, DataQuality, Instrument, OrderType,
                        PortfolioSnapshot, Position, Quote, Side, Signal,
                        SignalDirection, TimeInForce, TradeIntent,
                        ForecastResult, ensure_utc, jsonable, to_decimal)
from .errors import ConfigurationError, RiskError
from .volatility import (CALENDARS, CONTINUOUS, Calendar, annualisation_factor,
                         average_true_range, realised_volatility,
                         vol_target_position_size)

#: Store namespace for strategy status + drawdown state
#: (docs/TRADING_ARCHITECTURE.md §10).
_NS = "trading.strategy"

_ZERO = Decimal("0")


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return low if value < low else (high if value > high else value)


# ---------------------------------------------------------------------------
# context
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StrategyContext:
    """Everything a strategy is allowed to see, and nothing else.

    A closed set on purpose. The fields below are the complete surface: there is
    no route from here to a venue, to the order store, or to the risk limits.
    `coerce` accepts the backtester's lightweight namespace, a mapping, or an
    already-built context, so `backtest.py` and `pipeline.py` can drive a
    strategy without either side importing the other — and anything they carry
    that is not a field below is discarded rather than smuggled through.
    """
    as_of: datetime
    instrument: Instrument
    timeframe: str = ""
    candles: tuple[Candle, ...] = ()
    features: Mapping[str, Any] = field(default_factory=dict)
    #: The forecast for THIS instrument, or None. Never a dict of forecasts —
    #: a strategy that has to pick one out of a mapping can pick the wrong one.
    forecast: ForecastResult | None = None
    #: A `signals.FusedSignal` or None. Duck-typed so this module does not have
    #: to import `signals` (and therefore `forecast`) to be usable.
    fused_signal: Any = None
    signals: tuple[Signal, ...] = ()
    #: A `regime.RegimeState`, a bare `Regime`, or None.
    regime: Any = None
    #: A `volatility.VolatilityState` or None.
    volatility: Any = None
    portfolio: PortfolioSnapshot | None = None
    quote: Quote | None = None
    data_quality: DataQuality = DataQuality.OK
    clock: Clock | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    #: The complete field list, used by `coerce`. Anything not named here is
    #: not visible to a strategy, by construction.
    _FIELDS = ("as_of", "instrument", "timeframe", "candles", "features",
               "forecast", "fused_signal", "signals", "regime", "volatility",
               "portfolio", "quote", "data_quality", "clock", "extra")

    def __post_init__(self):
        object.__setattr__(self, "as_of", ensure_utc(self.as_of, field_name="as_of"))
        object.__setattr__(self, "candles", tuple(self.candles or ()))
        object.__setattr__(self, "signals", tuple(self.signals or ()))
        object.__setattr__(self, "features", dict(self.features or {}))
        object.__setattr__(self, "extra", dict(self.extra or {}))
        if not isinstance(self.instrument, Instrument):
            raise ConfigurationError(
                "StrategyContext requires an Instrument; a strategy that has to "
                "guess tick and lot sizes cannot produce a tradable quantity",
                got=type(self.instrument).__name__)
        if not self.timeframe and self.candles:
            object.__setattr__(self, "timeframe", self.candles[-1].timeframe)

    # -- convenience -------------------------------------------------------

    @property
    def instrument_key(self) -> str:
        return self.instrument.key

    @property
    def last_candle(self) -> Candle | None:
        return self.candles[-1] if self.candles else None

    @property
    def last_close(self) -> float | None:
        return self.candles[-1].close if self.candles else None

    @property
    def closes(self) -> tuple[float, ...]:
        return tuple(c.close for c in self.candles)

    @property
    def equity(self) -> Decimal:
        """Account equity, or zero when no portfolio was supplied.

        Zero rather than a guessed default: sizing against an invented equity
        figure produces a real order, and there is no safe made-up number.
        """
        return self.portfolio.equity if self.portfolio is not None else _ZERO

    def position(self) -> Position:
        """This instrument's position — flat when unknown."""
        if self.portfolio is None:
            return Position(instrument_key=self.instrument_key)
        return self.portfolio.positions.get(
            self.instrument_key, Position(instrument_key=self.instrument_key))

    @classmethod
    def coerce(cls, source: Any) -> "StrategyContext":
        """Build a context from a namespace, mapping, or existing context.

        Two shapes are normalised because upstream layers legitimately produce
        them: `forecasts={key: ForecastResult}` (the pipeline holds several) is
        narrowed to this instrument's forecast, and `fused` is the pipeline's
        spelling of `fused_signal`.
        """
        if isinstance(source, cls):
            return source
        if isinstance(source, Mapping):
            def read(name, default=None):
                return source.get(name, default)
        else:
            def read(name, default=None):
                return getattr(source, name, default)

        instrument = read("instrument")
        data = {name: read(name) for name in cls._FIELDS}
        data = {k: v for k, v in data.items() if v is not None}
        data["instrument"] = instrument

        # A pipeline holds forecasts for the whole universe; a strategy gets
        # exactly one, chosen by instrument key rather than by position.
        if data.get("forecast") is None:
            forecasts = read("forecasts") or {}
            key = getattr(instrument, "key", "")
            if isinstance(forecasts, Mapping) and key:
                data["forecast"] = forecasts.get(key)
        if data.get("fused_signal") is None:
            data["fused_signal"] = read("fused")
        if not data.get("candles"):
            data["candles"] = read("candles") or ()
        if data.get("as_of") is None:
            clock = data.get("clock")
            if clock is not None:
                data["as_of"] = clock.now()
        kwargs = {k: v for k, v in data.items()
                  if k in cls._FIELDS and v is not None}
        kwargs["instrument"] = instrument
        kwargs["as_of"] = data.get("as_of")
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# the strategy interface
# ---------------------------------------------------------------------------

class Strategy(ABC):
    """Base class for everything that proposes trades.

    Subclasses receive a `StrategyContext` and return `TradeIntent`s. They are
    constructed with no reference to a broker, the OMS, the execution engine, or
    the limits store, and this module imports none of those, so there is nothing
    to reach through even by accident. A strategy's proposal is a request, and
    the deterministic risk engine is free to shrink it or refuse it.
    """

    #: Stable identity. Written into every intent and every audit record, so it
    #: must not change when the implementation is tuned — bump `version` for
    #: that.
    id: str = "strategy"
    version: str = "0"

    @property
    def warmup_bars(self) -> int:
        """Bars of history required before this strategy may propose anything."""
        return 0

    def describe(self) -> dict:
        """JSON-safe description of the strategy and its parameters.

        Recorded alongside intents so a decision can be re-read years later
        without the source that produced it.
        """
        return {"id": self.id, "version": self.version,
                "class": type(self).__name__,
                "warmup_bars": int(self.warmup_bars),
                "parameters": jsonable(self.parameters())}

    def parameters(self) -> Mapping[str, Any]:
        """Tunable parameters. Overridden by strategies that have any."""
        return {}

    @abstractmethod
    def on_bar(self, ctx: Any) -> list[TradeIntent]:
        """Propose zero or more trades for the bar just closed.

        Implementations must call `StrategyContext.coerce(ctx)` first: the
        backtester and the pipeline hand over their own namespace objects, and
        coercion is what narrows those to the permitted surface.
        """
        raise NotImplementedError

    def intent_id(self, ctx: StrategyContext, side: Side, salt: str = "") -> str:
        """Deterministic intent id.

        Derived from (strategy, version, instrument, bar time, side) rather than
        from a UUID so that replaying the same bar twice produces the same id —
        which is what lets the OMS treat a retry as a duplicate instead of a
        second trade. No randomness anywhere in this module.
        """
        payload = "|".join([self.id, self.version, ctx.instrument_key,
                            ctx.as_of.isoformat(), side.value, salt])
        return f"{self.id}-{hashlib.sha256(payload.encode()).hexdigest()[:16]}"

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"{type(self).__name__}(id={self.id!r}, version={self.version!r})"


class FlatStrategy(Strategy):
    """Never proposes anything. The control arm.

    Useful for two things that both matter: proving that a harness (backtester,
    pipeline, manager) does nothing on its own, and giving a comparison a
    genuine do-nothing baseline. A strategy that cannot beat this one after
    costs has no reason to exist.
    """

    id = "flat"
    version = "1"

    def __init__(self, *, id: str | None = None):
        if id:
            self.id = id
        self.calls = 0

    def on_bar(self, ctx: Any) -> list[TradeIntent]:
        self.calls += 1
        return []


# ---------------------------------------------------------------------------
# the shared momentum template
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DirectionalView:
    """A strategy's directional opinion, before it becomes a size.

    Split out so the Kronos and non-Kronos strategies can differ in *exactly*
    this object and share everything downstream of it.
    """
    direction: SignalDirection
    #: Expected simple return over the strategy's horizon, signed.
    expected_return: float
    #: [0, 1] — how much of the full risk budget this view justifies.
    conviction: float
    #: [0, 1] — how much the strategy trusts the view itself.
    confidence: float
    rationale: str = ""
    reason_codes: tuple[str, ...] = ()
    signals: tuple[Signal, ...] = ()
    model_versions: Mapping[str, str] = field(default_factory=dict)
    features: Mapping[str, Any] = field(default_factory=dict)


class VolatilityTargetedStrategy(Strategy):
    """Sizing, stops, take-profits and intent construction — shared.

    Everything here is deliberately *not* overridable by the concrete
    strategies below: `KronosMomentumStrategy` and `BaselineMomentumStrategy`
    implement `_view()` and nothing else, which is what makes a comparison
    between them a comparison of signal sources rather than of two whole
    trading systems.

    Position stacking is refused (one entry per instrument at a time). Not a
    tuning choice: without it, a persistent signal proposes a new full-size
    entry on every bar and the resulting exposure is a function of how often the
    loop runs rather than of conviction.
    """

    id = "vol-targeted"
    version = "1"

    def __init__(self, *, target_volatility: float = 0.15,
                 max_leverage: float = 1.0,
                 max_position_fraction: float = 0.25,
                 vol_window: int = 20, atr_window: int = 14,
                 stop_atr_multiple: float = 2.0,
                 take_profit_r: float = 2.0,
                 min_expected_return: float = 0.002,
                 full_conviction_return: float = 0.02,
                 min_confidence: float = 0.3,
                 max_holding_period_s: int | None = None,
                 calendar: Calendar | str = CONTINUOUS,
                 clock: Clock | None = None,
                 id: str | None = None, version: str | None = None):
        if target_volatility <= 0:
            raise ConfigurationError("target_volatility must be > 0",
                                     got=target_volatility)
        if max_leverage <= 0:
            raise ConfigurationError("max_leverage must be > 0", got=max_leverage)
        if not 0 < max_position_fraction <= 1:
            raise ConfigurationError(
                "max_position_fraction must be within (0, 1]",
                got=max_position_fraction)
        if stop_atr_multiple <= 0:
            raise ConfigurationError("stop_atr_multiple must be > 0",
                                     got=stop_atr_multiple)
        if take_profit_r <= 0:
            raise ConfigurationError("take_profit_r must be > 0",
                                     got=take_profit_r)
        if full_conviction_return <= 0:
            raise ConfigurationError("full_conviction_return must be > 0",
                                     got=full_conviction_return)
        if vol_window < 2 or atr_window < 1:
            raise ConfigurationError("vol_window >= 2 and atr_window >= 1",
                                     vol_window=vol_window, atr_window=atr_window)
        if id:
            self.id = id
        if version:
            self.version = version
        self.target_volatility = float(target_volatility)
        self.max_leverage = float(max_leverage)
        self.max_position_fraction = float(max_position_fraction)
        self.vol_window = int(vol_window)
        self.atr_window = int(atr_window)
        self.stop_atr_multiple = float(stop_atr_multiple)
        self.take_profit_r = float(take_profit_r)
        self.min_expected_return = float(min_expected_return)
        self.full_conviction_return = float(full_conviction_return)
        self.min_confidence = float(min_confidence)
        self.max_holding_period_s = max_holding_period_s
        self.calendar = (CALENDARS.get(calendar, CONTINUOUS)
                         if isinstance(calendar, str) else calendar)
        self.clock = clock or default_clock()
        #: Reasons the last call declined to trade. Read by tests and by the
        #: monitor; a strategy that silently returns [] is unexplainable.
        self.last_skip_reason: str = ""

    # -- the part subclasses replace ---------------------------------------

    @abstractmethod
    def _view(self, ctx: StrategyContext) -> DirectionalView | None:
        """Produce the directional opinion, or None to stand aside.

        Returning None (with `last_skip_reason` set) is the correct response to
        an unusable input. Producing a FLAT view with a made-up confidence would
        push the decision downstream into code that cannot tell the difference
        between "no opinion" and "opinion: nothing".
        """
        raise NotImplementedError

    def parameters(self) -> Mapping[str, Any]:
        return {"target_volatility": self.target_volatility,
                "max_leverage": self.max_leverage,
                "max_position_fraction": self.max_position_fraction,
                "vol_window": self.vol_window, "atr_window": self.atr_window,
                "stop_atr_multiple": self.stop_atr_multiple,
                "take_profit_r": self.take_profit_r,
                "min_expected_return": self.min_expected_return,
                "full_conviction_return": self.full_conviction_return,
                "min_confidence": self.min_confidence,
                "calendar": self.calendar.name}

    @property
    def warmup_bars(self) -> int:
        return max(self.vol_window, self.atr_window) + 2

    # -- the shared pipeline -----------------------------------------------

    def on_bar(self, ctx: Any) -> list[TradeIntent]:
        ctx = StrategyContext.coerce(ctx)
        self.last_skip_reason = ""

        if len(ctx.candles) < self.warmup_bars:
            self.last_skip_reason = "WARMUP"
            return []
        # Trading on data the validation layer called unusable is exactly the
        # failure the DataQuality enum exists to prevent, so it is refused here
        # as well as in the risk engine. Two independent refusals, on purpose.
        if ctx.data_quality is DataQuality.UNUSABLE:
            self.last_skip_reason = "DATA_UNUSABLE"
            return []
        if not ctx.candles[-1].is_final:
            self.last_skip_reason = "BAR_NOT_FINAL"
            return []

        view = self._view(ctx)
        if view is None or view.direction is SignalDirection.FLAT:
            if not self.last_skip_reason:
                self.last_skip_reason = "NO_VIEW"
            return []

        # One entry per instrument. An exit is the venue's stop/take doing its
        # job, not a second intent from here.
        if not ctx.position().is_flat:
            self.last_skip_reason = "ALREADY_POSITIONED"
            return []

        price = ctx.last_close
        if price is None or price <= 0:
            self.last_skip_reason = "NO_PRICE"
            return []

        side = Side.BUY if view.direction is SignalDirection.LONG else Side.SELL
        quantity = self._size(ctx, view, price)
        if quantity <= 0:
            self.last_skip_reason = self.last_skip_reason or "SIZE_ZERO"
            return []

        entry = ctx.instrument.quantize_price(price)
        stop, take = self._exits(ctx, side, entry)
        if stop is None:
            self.last_skip_reason = "NO_STOP"
            return []

        intent = TradeIntent(
            intent_id=self.intent_id(ctx, side),
            strategy_id=self.id, strategy_version=self.version,
            instrument_key=ctx.instrument_key, side=side,
            order_type=OrderType.MARKET, quantity=quantity,
            created_at=ctx.as_of, intended_entry=entry,
            stop_loss=stop, take_profit=take,
            time_in_force=TimeInForce.DAY,
            max_holding_period_s=self.max_holding_period_s,
            confidence=_clamp(view.confidence),
            supporting_signals=tuple(view.signals),
            invalidating_conditions=(
                f"price through stop {stop}",
                "forecast direction reverses",
                "data quality degrades to unusable"),
            data_ts_start=ctx.candles[0].ts_open,
            data_ts_end=ctx.candles[-1].ts_close,
            model_versions=dict(view.model_versions),
            explanation=view.rationale,
            reason_codes=tuple(view.reason_codes),
            metadata={"conviction": round(view.conviction, 6),
                      "expected_return": round(view.expected_return, 8),
                      "annualised_volatility": round(self._volatility(ctx), 8),
                      **{k: jsonable(v) for k, v in view.features.items()}})
        return [intent]

    # -- sizing and exits (shared by every subclass) -----------------------

    def _volatility(self, ctx: StrategyContext) -> float:
        """Annualised realised volatility of the window, 0.0 when undefined.

        Prefers an injected `VolatilityState` (the analysis layer's estimate,
        which may use a better estimator) and falls back to close-to-close.
        """
        state = ctx.volatility
        if state is not None:
            for name in ("ewma", "realised"):
                value = getattr(state, name, None)
                if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
                    return float(value)
        closes = ctx.closes
        if len(closes) <= self.vol_window:
            return 0.0
        factor = annualisation_factor(ctx.timeframe or "1d", self.calendar)
        series = realised_volatility(list(closes), self.vol_window, factor)
        latest = series[-1]
        return float(latest) if latest else 0.0

    def _size(self, ctx: StrategyContext, view: DirectionalView,
              price: float) -> Decimal:
        """Volatility-targeted quantity, quantised DOWN to a tradable lot.

        The risk budget is scaled by conviction, so a marginal view takes a
        proportionally smaller position rather than the same one. Two caps
        follow: `max_leverage` inside the vol-target formula and
        `max_position_fraction` of equity here, because a low-volatility
        instrument otherwise sizes into a position that is enormous in notional
        terms while being small in risk terms.
        """
        equity = ctx.equity
        if equity <= 0:
            self.last_skip_reason = "NO_EQUITY"
            return _ZERO
        vol = self._volatility(ctx)
        if vol <= 0:
            # A zero volatility estimate means a dead market or a broken
            # estimator; either way the formula's answer is "infinite size".
            self.last_skip_reason = "NO_VOLATILITY"
            return _ZERO

        instrument = ctx.instrument
        budget = self.target_volatility * _clamp(view.conviction)
        quantity = vol_target_position_size(
            budget, vol, equity, price, max_leverage=self.max_leverage,
            instrument=instrument)

        cap = instrument.quantize_quantity(
            (equity * to_decimal(self.max_position_fraction,
                                 field_name="max_position_fraction"))
            / to_decimal(price, field_name="price"))
        if cap < quantity:
            quantity = cap

        if quantity < instrument.min_quantity:
            self.last_skip_reason = "BELOW_MIN_QUANTITY"
            return _ZERO
        if instrument.min_notional > 0 and \
                instrument.notional(quantity, price) < instrument.min_notional:
            self.last_skip_reason = "BELOW_MIN_NOTIONAL"
            return _ZERO
        return quantity

    def _stop_distance(self, ctx: StrategyContext, price: float) -> float:
        """Stop distance in price units: ATR-based, volatility-based fallback.

        ATR first because a stop must survive normal intrabar noise, and ATR is
        the only estimator here that sees the intrabar range at all.
        """
        atr_series = average_true_range(list(ctx.candles), self.atr_window)
        atr = atr_series[-1] if atr_series else None
        if atr and atr > 0:
            return float(atr) * self.stop_atr_multiple
        vol = self._volatility(ctx)
        if vol > 0:
            factor = annualisation_factor(ctx.timeframe or "1d", self.calendar)
            per_bar = vol / factor if factor > 0 else vol
            return price * per_bar * self.stop_atr_multiple
        return 0.0

    def _exits(self, ctx: StrategyContext, side: Side,
               entry: Decimal) -> tuple[Decimal | None, Decimal | None]:
        """Stop loss and take profit, both quantised onto the tick grid.

        Returns `(None, None)` when no defensible stop distance exists — a
        trade whose invalidation level cannot be computed is one this layer
        declines to propose, rather than one it proposes unprotected.

        The post-quantisation nudge is not cosmetic: rounding a stop onto the
        tick grid can land it exactly on the entry, and `TradeIntent` rejects
        that (correctly — a stop at the entry is an instant exit, not
        protection).
        """
        instrument = ctx.instrument
        distance = self._stop_distance(ctx, float(entry))
        if distance <= 0:
            return None, None
        tick = instrument.tick_size
        d = to_decimal(distance, field_name="stop_distance")
        if side is Side.BUY:
            stop = instrument.quantize_price(entry - d)
            take = instrument.quantize_price(
                entry + d * to_decimal(self.take_profit_r,
                                       field_name="take_profit_r"))
            if stop >= entry:
                stop = entry - tick
            if take <= entry:
                take = entry + tick
            if stop <= 0:
                return None, None
        else:
            stop = instrument.quantize_price(entry + d)
            take = instrument.quantize_price(
                entry - d * to_decimal(self.take_profit_r,
                                       field_name="take_profit_r"))
            if stop <= entry:
                stop = entry + tick
            if take >= entry:
                take = entry - tick
            if take <= 0:
                return stop, None
        return stop, take

    def _conviction(self, expected_return: float, confidence: float) -> float:
        """Map an expected return and a confidence onto [0, 1] of the budget.

        Linear in |expected return| up to `full_conviction_return` and then
        flat: a forecast of +40% does not justify more risk than one of +2%,
        because at that magnitude the forecast is far more likely to be wrong
        than the market is to move that far.
        """
        magnitude = _clamp(abs(expected_return) / self.full_conviction_return)
        return magnitude * _clamp(confidence)


class KronosMomentumStrategy(VolatilityTargetedStrategy):
    """Trades the forecast's expected return, or stands aside.

    The refusal rules are the point of this class:

    * an **abstained** forecast produces no trade, ever. Abstention is the
      forecaster telling the truth about unreliable input; overriding it here
      would convert an honest "I don't know" into a position.
    * an **unusable** forecast (`DataQuality.UNUSABLE`, no mean path) is refused
      for the same reason.
    * a forecast for a **different instrument** is refused — a mis-keyed
      forecast is a silent way to trade the wrong thing.
    * a forecast whose **uncertainty** exceeds `max_uncertainty`, or whose
      confidence is below `min_confidence`, is refused rather than downweighted.
      There is a level of dispersion beyond which the central estimate carries
      no information, and sizing down does not fix that.

    Public evidence on Kronos's directional skill is currently negative
    (docs/KRONOS_TEARDOWN.md §16; upstream issues #354/#355), which is precisely
    why this strategy has a non-Kronos twin and why `evaluate.kronos_is_valuable`
    decides whether it earns its place.
    """

    id = "kronos-momentum"
    version = "1"

    def __init__(self, *, max_uncertainty: float = 0.6, **kwargs):
        super().__init__(**kwargs)
        if not 0.0 <= max_uncertainty <= 1.0:
            raise ConfigurationError("max_uncertainty must be within [0, 1]",
                                     got=max_uncertainty)
        self.max_uncertainty = float(max_uncertainty)

    def parameters(self) -> Mapping[str, Any]:
        return {**super().parameters(), "max_uncertainty": self.max_uncertainty,
                "signal_source": "kronos_forecast"}

    def _view(self, ctx: StrategyContext) -> DirectionalView | None:
        forecast = ctx.forecast
        if forecast is None:
            self.last_skip_reason = "NO_FORECAST"
            return None
        if forecast.abstained:
            self.last_skip_reason = "FORECAST_ABSTAINED"
            return None
        if not forecast.usable:
            self.last_skip_reason = "FORECAST_UNUSABLE"
            return None
        if forecast.instrument_key and forecast.instrument_key != ctx.instrument_key:
            self.last_skip_reason = "FORECAST_INSTRUMENT_MISMATCH"
            return None
        if forecast.uncertainty > self.max_uncertainty:
            self.last_skip_reason = "FORECAST_TOO_UNCERTAIN"
            return None

        confidence = _clamp(forecast.confidence)
        if confidence < self.min_confidence:
            self.last_skip_reason = "FORECAST_LOW_CONFIDENCE"
            return None

        expected = float(forecast.expected_return)
        if not math.isfinite(expected) or abs(expected) < self.min_expected_return:
            self.last_skip_reason = "EXPECTED_RETURN_BELOW_THRESHOLD"
            return None

        direction = (SignalDirection.LONG if expected > 0
                     else SignalDirection.SHORT)
        conviction = self._conviction(expected, confidence)
        strength = math.copysign(_clamp(conviction), expected)
        signal = Signal(
            source="kronos", instrument_key=ctx.instrument_key,
            direction=direction, strength=strength, confidence=confidence,
            ts=ctx.as_of, horizon=forecast.horizon,
            rationale=(f"forecast expected return {expected:+.4%} over "
                       f"{forecast.horizon} bars, uncertainty "
                       f"{forecast.uncertainty:.2f}"),
            features={"expected_return": expected,
                      "uncertainty": forecast.uncertainty,
                      "forecast_volatility": forecast.forecast_volatility,
                      "n_paths": forecast.n_paths},
            model_version=forecast.model_version,
            data_quality=forecast.data_quality)
        return DirectionalView(
            direction=direction, expected_return=expected,
            conviction=conviction, confidence=confidence,
            rationale=signal.rationale,
            reason_codes=("KRONOS_FORECAST_DIRECTIONAL",),
            signals=(signal,),
            model_versions={"forecast": forecast.model_version},
            features={"uncertainty": forecast.uncertainty,
                      "forecast_horizon": forecast.horizon})


class BaselineMomentumStrategy(VolatilityTargetedStrategy):
    """The control arm: identical machinery, no Kronos.

    The directional view is `close[-1] / close[-1-lookback] - 1` and the
    confidence is the fraction of bars in that window whose return agreed with
    the overall direction. Nothing here consults a model.

    This class exists so `evaluate.compare_to_baseline` and
    `evaluate.kronos_is_valuable` have something honest to compare against.
    Comparing a Kronos strategy against "buy and hold" or against a differently
    tuned system would confound the signal source with everything else; sharing
    `VolatilityTargetedStrategy` means the signal source is the only difference.
    """

    id = "baseline-momentum"
    version = "1"

    def __init__(self, *, lookback: int = 20, **kwargs):
        super().__init__(**kwargs)
        if lookback < 1:
            raise ConfigurationError("lookback must be >= 1", got=lookback)
        self.lookback = int(lookback)

    def parameters(self) -> Mapping[str, Any]:
        return {**super().parameters(), "lookback": self.lookback,
                "signal_source": "close_momentum"}

    @property
    def warmup_bars(self) -> int:
        return max(super().warmup_bars, self.lookback + 2)

    def _view(self, ctx: StrategyContext) -> DirectionalView | None:
        closes = ctx.closes
        if len(closes) < self.lookback + 1:
            self.last_skip_reason = "WARMUP"
            return None
        window = closes[-(self.lookback + 1):]
        base = window[0]
        if base <= 0:
            self.last_skip_reason = "NO_PRICE"
            return None
        expected = window[-1] / base - 1.0
        if not math.isfinite(expected) or abs(expected) < self.min_expected_return:
            self.last_skip_reason = "EXPECTED_RETURN_BELOW_THRESHOLD"
            return None

        direction = (SignalDirection.LONG if expected > 0
                     else SignalDirection.SHORT)
        steps = [window[i + 1] - window[i] for i in range(len(window) - 1)]
        agreeing = sum(1 for s in steps
                       if (s > 0) == (expected > 0) and s != 0)
        # Consistency as confidence: a move produced by one gap is a weaker
        # basis than the same move produced by a run of agreeing bars.
        confidence = _clamp(agreeing / len(steps)) if steps else 0.0
        if confidence < self.min_confidence:
            self.last_skip_reason = "MOMENTUM_LOW_CONFIDENCE"
            return None

        conviction = self._conviction(expected, confidence)
        strength = math.copysign(_clamp(conviction), expected)
        signal = Signal(
            source="momentum", instrument_key=ctx.instrument_key,
            direction=direction, strength=strength, confidence=confidence,
            ts=ctx.as_of, horizon=self.lookback,
            rationale=(f"{self.lookback}-bar momentum {expected:+.4%}, "
                       f"{agreeing}/{len(steps)} bars agreeing"),
            features={"expected_return": expected, "lookback": self.lookback,
                      "agreeing_bars": agreeing},
            model_version="momentum/1", data_quality=ctx.data_quality)
        return DirectionalView(
            direction=direction, expected_return=expected,
            conviction=conviction, confidence=confidence,
            rationale=signal.rationale,
            reason_codes=("BASELINE_MOMENTUM_DIRECTIONAL",),
            signals=(signal,), model_versions={"momentum": "1"},
            features={"lookback": self.lookback, "agreeing_bars": agreeing})


# ---------------------------------------------------------------------------
# lifecycle management
# ---------------------------------------------------------------------------

class StrategyStatus(str, Enum):
    """Operational state of one strategy.

    PAUSED and DISABLED are distinct because they mean different things to an
    operator: PAUSED is expected to end (a drawdown halt, a maintenance window),
    DISABLED is a decision to stop running this strategy. Collapsing them would
    make "why is this not trading?" unanswerable from the record.
    """
    ENABLED = "enabled"
    PAUSED = "paused"
    DISABLED = "disabled"


@dataclass(frozen=True)
class StrategyRecord:
    """Persisted status and drawdown state for one strategy.

    Equity figures are `Decimal` and serialise as strings — this record feeds
    a halt decision, and a float rounding error in a drawdown comparison is a
    halt that fires late or not at all.
    """
    strategy_id: str
    version: str = ""
    status: StrategyStatus = StrategyStatus.ENABLED
    updated_at: datetime | None = None
    updated_by: str = "system"
    reason: str = ""
    peak_equity: Decimal = _ZERO
    equity: Decimal = _ZERO
    #: Fractional drawdown limit (0.2 == 20%). None disables the halt.
    max_drawdown: float | None = None
    drawdown: float = 0.0
    halted_by_drawdown: bool = False

    def __post_init__(self):
        object.__setattr__(self, "status", StrategyStatus(self.status))
        for name in ("peak_equity", "equity"):
            object.__setattr__(self, name,
                               to_decimal(getattr(self, name), field_name=name))
        if self.updated_at is not None:
            object.__setattr__(self, "updated_at",
                               ensure_utc(self.updated_at, field_name="updated_at"))

    @property
    def active(self) -> bool:
        return self.status is StrategyStatus.ENABLED

    def to_dict(self) -> dict:
        return {"strategy_id": self.strategy_id, "version": self.version,
                "status": self.status.value,
                "updated_at": jsonable(self.updated_at),
                "updated_by": self.updated_by, "reason": self.reason,
                "peak_equity": str(self.peak_equity),
                "equity": str(self.equity),
                "max_drawdown": self.max_drawdown,
                "drawdown": self.drawdown,
                "halted_by_drawdown": self.halted_by_drawdown}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StrategyRecord":
        updated = data.get("updated_at")
        if isinstance(updated, str) and updated:
            updated = datetime.fromisoformat(updated)
        else:
            updated = None
        return cls(strategy_id=str(data.get("strategy_id", "")),
                   version=str(data.get("version", "")),
                   status=StrategyStatus(data.get("status", "enabled")),
                   updated_at=updated,
                   updated_by=str(data.get("updated_by", "system")),
                   reason=str(data.get("reason", "")),
                   peak_equity=data.get("peak_equity", 0),
                   equity=data.get("equity", 0),
                   max_drawdown=data.get("max_drawdown"),
                   drawdown=float(data.get("drawdown", 0.0)),
                   halted_by_drawdown=bool(data.get("halted_by_drawdown", False)))


class StrategyManager:
    """Registers strategies and decides which of them may run.

    Three responsibilities, all of which must survive a restart, so all of which
    are persisted to `olympus.store` under `trading.strategy`:

    1. **Status.** ENABLED / PAUSED / DISABLED per strategy. Re-registering a
       strategy after a restart does *not* reset its status — a disabled
       strategy that comes back enabled because the process bounced is the
       failure mode this exists to prevent.
    2. **Drawdown.** Peak-to-trough on that strategy's own equity attribution.
       Breaching the configured limit PAUSES it automatically, and clearing an
       automatic halt requires an explicit operator override, for the same
       reason `killswitch.disengage` does: the loop that lost the money must not
       be able to authorise itself to continue.
    3. **Kill switches.** The registry is *injected*, never imported — this
       module may not import `killswitch` (layer graph), and duck-typing keeps
       the structural guarantee intact while still letting a global or
       per-strategy switch stop a strategy.

    `active()` fails closed: any error reading a kill switch or a status record
    removes the strategy from the active set rather than defaulting it to
    tradable.
    """

    def __init__(self, *, clock: Clock | None = None, store_ns: str = _NS,
                 killswitches: Any = None, audit: Any = None):
        self.clock = clock or default_clock()
        self.namespace = store_ns
        #: Anything with `.check(strategy_id=...)` returning a truthy engaged
        #: state (or None). Injected so this module never imports killswitch.
        self.killswitches = killswitches
        self.audit = audit
        self._strategies: dict[str, Strategy] = {}

    # -- persistence -------------------------------------------------------

    def _store(self):
        from olympus import store
        return store.backend()

    def _read(self, strategy_id: str) -> StrategyRecord | None:
        raw = self._store().get(self.namespace, strategy_id)
        if not raw:
            return None
        return StrategyRecord.from_dict(json.loads(raw.decode("utf-8")))

    def _write(self, record: StrategyRecord) -> StrategyRecord:
        payload = json.dumps(record.to_dict(), sort_keys=True).encode("utf-8")
        self._store().put(self.namespace, record.strategy_id, payload)
        return record

    def _record_event(self, record: StrategyRecord, action: str) -> None:
        if self.audit is None:
            return
        try:
            self.audit.record("CONFIG_CHANGED",
                              {**record.to_dict(), "action": action},
                              actor=record.updated_by or "system",
                              subject=record.strategy_id)
        except Exception:                                # noqa: BLE001
            # An audit backend outage must not take the trading loop down, but
            # it must not be silent either — the caller still sees the state
            # change, and audit.verify() will show the gap.
            pass

    # -- registration ------------------------------------------------------

    def register(self, strategy: Strategy, *,
                 status: StrategyStatus = StrategyStatus.ENABLED,
                 max_drawdown: float | None = None,
                 by: str = "system") -> StrategyRecord:
        """Register a strategy, preserving any persisted status.

        `status` is the status for a *new* registration only. An existing record
        wins, because it may encode an operator's decision or an automatic halt.
        """
        if not getattr(strategy, "id", ""):
            raise ConfigurationError("a strategy must have a non-empty id")
        if not callable(getattr(strategy, "on_bar", None)):
            raise ConfigurationError("a strategy must implement on_bar(ctx)",
                                     strategy_id=strategy.id)
        if max_drawdown is not None and not 0 < max_drawdown < 1:
            raise ConfigurationError(
                "max_drawdown is a fraction in (0, 1)", got=max_drawdown)
        self._strategies[strategy.id] = strategy
        from olympus import proclock
        with proclock.lock(f"strategy-{strategy.id}"):
            existing = self._read(strategy.id)
            if existing is not None:
                record = StrategyRecord(
                    strategy_id=existing.strategy_id,
                    version=str(getattr(strategy, "version", "")),
                    status=existing.status, updated_at=self.clock.now(),
                    updated_by=existing.updated_by, reason=existing.reason,
                    peak_equity=existing.peak_equity, equity=existing.equity,
                    max_drawdown=(existing.max_drawdown if max_drawdown is None
                                  else max_drawdown),
                    drawdown=existing.drawdown,
                    halted_by_drawdown=existing.halted_by_drawdown)
            else:
                record = StrategyRecord(
                    strategy_id=strategy.id,
                    version=str(getattr(strategy, "version", "")),
                    status=StrategyStatus(status), updated_at=self.clock.now(),
                    updated_by=by, reason="registered",
                    max_drawdown=max_drawdown)
            self._write(record)
        self._record_event(record, "register")
        return record

    def strategies(self) -> dict[str, Strategy]:
        return dict(self._strategies)

    def get(self, strategy_id: str) -> Strategy | None:
        return self._strategies.get(strategy_id)

    def record(self, strategy_id: str) -> StrategyRecord | None:
        return self._read(strategy_id)

    def records(self) -> list[StrategyRecord]:
        out = []
        for key in sorted(self._store().keys(self.namespace)):
            record = self._read(key)
            if record is not None:
                out.append(record)
        return out

    def status(self, strategy_id: str) -> StrategyStatus:
        record = self._read(strategy_id)
        # Unknown means "not authorised to trade", not "enabled by default".
        return record.status if record else StrategyStatus.DISABLED

    # -- transitions -------------------------------------------------------

    def _transition(self, strategy_id: str, status: StrategyStatus, *,
                    by: str, reason: str, clear_halt: bool = False
                    ) -> StrategyRecord:
        from olympus import proclock
        with proclock.lock(f"strategy-{strategy_id}"):
            existing = self._read(strategy_id)
            if existing is None:
                raise ConfigurationError("unknown strategy", strategy_id=strategy_id)
            record = StrategyRecord(
                strategy_id=strategy_id, version=existing.version,
                status=status, updated_at=self.clock.now(), updated_by=by,
                reason=reason, peak_equity=existing.peak_equity,
                equity=existing.equity, max_drawdown=existing.max_drawdown,
                drawdown=existing.drawdown,
                halted_by_drawdown=(False if clear_halt
                                    else existing.halted_by_drawdown))
            self._write(record)
        self._record_event(record, status.value)
        return record

    def enable(self, strategy_id: str, *, by: str = "operator",
               reason: str = "", operator_override: bool = False
               ) -> StrategyRecord:
        """Allow a strategy to trade again.

        A strategy halted automatically by its drawdown limit needs
        `operator_override=True`. Without it, the same loop that lost the money
        could re-enable itself and carry on — the precise failure the halt
        exists to prevent.
        """
        existing = self._read(strategy_id)
        if existing is None:
            raise ConfigurationError("unknown strategy", strategy_id=strategy_id)
        if existing.halted_by_drawdown and not operator_override:
            raise RiskError(
                "this strategy was halted automatically on drawdown and needs "
                "an explicit operator override to resume",
                strategy_id=strategy_id, drawdown=existing.drawdown,
                limit=existing.max_drawdown)
        return self._transition(strategy_id, StrategyStatus.ENABLED, by=by,
                                reason=reason or "enabled", clear_halt=True)

    def pause(self, strategy_id: str, *, by: str = "operator",
              reason: str = "") -> StrategyRecord:
        return self._transition(strategy_id, StrategyStatus.PAUSED, by=by,
                                reason=reason or "paused")

    def disable(self, strategy_id: str, *, by: str = "operator",
                reason: str = "") -> StrategyRecord:
        return self._transition(strategy_id, StrategyStatus.DISABLED, by=by,
                                reason=reason or "disabled")

    # -- drawdown ----------------------------------------------------------

    def record_equity(self, strategy_id: str, equity: Decimal | float | str
                      ) -> StrategyRecord:
        """Update this strategy's equity, peak, and drawdown.

        Halts (PAUSES) the strategy when the drawdown reaches its configured
        limit. The comparison is `>=` deliberately: a limit that only fires on a
        strict breach lets a strategy sit exactly on its stated maximum loss.
        """
        value = to_decimal(equity, field_name="equity")
        from olympus import proclock
        with proclock.lock(f"strategy-{strategy_id}"):
            existing = self._read(strategy_id)
            if existing is None:
                raise ConfigurationError("unknown strategy",
                                         strategy_id=strategy_id)
            peak = existing.peak_equity if existing.peak_equity > value else value
            drawdown = 0.0
            if peak > 0:
                drawdown = float((peak - value) / peak)
            halted = existing.halted_by_drawdown
            status = existing.status
            reason = existing.reason
            by = existing.updated_by
            if (existing.max_drawdown is not None
                    and drawdown >= existing.max_drawdown
                    and status is StrategyStatus.ENABLED):
                halted = True
                status = StrategyStatus.PAUSED
                reason = (f"drawdown {drawdown:.4f} reached limit "
                          f"{existing.max_drawdown:.4f}")
                by = "system"
            record = StrategyRecord(
                strategy_id=strategy_id, version=existing.version,
                status=status, updated_at=self.clock.now(), updated_by=by,
                reason=reason, peak_equity=peak, equity=value,
                max_drawdown=existing.max_drawdown, drawdown=drawdown,
                halted_by_drawdown=halted)
            self._write(record)
        if halted and not existing.halted_by_drawdown:
            self._record_event(record, "drawdown_halt")
        return record

    def drawdown(self, strategy_id: str) -> float:
        record = self._read(strategy_id)
        return record.drawdown if record else 0.0

    # -- the question the trading loop asks --------------------------------

    def is_active(self, strategy_id: str) -> bool:
        """True only when the strategy is ENABLED and no kill switch applies."""
        try:
            record = self._read(strategy_id)
        except Exception:                                # noqa: BLE001
            return False                                 # fail closed
        if record is None or record.status is not StrategyStatus.ENABLED:
            return False
        return not self._killed(strategy_id)

    def _killed(self, strategy_id: str) -> bool:
        if self.killswitches is None:
            return False
        try:
            state = self.killswitches.check(strategy_id=strategy_id)
        except Exception:                                # noqa: BLE001
            # An unreadable kill-switch registry is treated as engaged. If we
            # cannot tell what an operator set, the safe reading is "stopped".
            return True
        return bool(state)

    def active(self) -> list[Strategy]:
        """Registered strategies that may propose trades right now."""
        return [self._strategies[key] for key in sorted(self._strategies)
                if self.is_active(key)]


__all__ = [
    "StrategyContext", "Strategy", "DirectionalView",
    "VolatilityTargetedStrategy", "KronosMomentumStrategy",
    "BaselineMomentumStrategy", "FlatStrategy",
    "StrategyStatus", "StrategyRecord", "StrategyManager",
]

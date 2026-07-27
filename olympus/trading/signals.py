"""Signal generation and fusion — opinions, and how to combine them.

A `Signal` is the weakest object in this package: it is one component's opinion,
carrying a direction, a signed conviction, and a self-assessed confidence. It
cannot size a trade, cannot reach a broker, and cannot alter a limit. That
weakness is deliberate and it is the containment boundary for the whole
"external content is untrusted" story: the very worst a poisoned news article,
a corrupted checkpoint, or a hallucinating agent can achieve through this layer
is *one advisory signal among several*, which fusion dilutes, a strategy turns
into a mere proposal, and the deterministic risk engine then judges on its own
terms.

Silence versus FLAT
-------------------
The single most important distinction in this module, and the one most systems
get wrong:

* **No signal** — "I have no basis for an opinion." An abstaining forecast,
  a window too short for the indicator, an unknown regime. The source stays out
  of the vote entirely and does not dilute the sources that *do* know something.
* **FLAT** — "I looked, and I see no direction." A forecast whose expected
  return is inside the noise band; a volatility reading so extreme that the
  right answer is to stand aside. This is real information, it counts in the
  vote, and it damps the fused strength.

Collapsing these two produces a system that either treats ignorance as a
bearish opinion or treats a considered "stand aside" as absence. Both are wrong
in ways that only show up in live P&L, so the rule is enforced structurally:
generators return an empty list for ignorance, never a FLAT signal.

Fusion
------
Fusion is deterministic, order-independent, and cannot invent a direction. Given
identical inputs it produces an identical `FusedSignal`, including its float
bits: signals are de-duplicated by source (newest wins) and then sorted by
source name before any arithmetic, so a different generator ordering cannot
change the last digit of the fused strength. If every input is FLAT or absent,
the output is FLAT with zero strength — there is no configuration under which
fusion manufactures a direction that no source held.

Three strategies, because "combine the opinions" has three defensible meanings
and the right one depends on what the desk is trying to do:

* `WeightedAverageFusion` — a weighted mean. Nuanced; a single dissenter reduces
  conviction proportionally rather than vetoing.
* `MajorityVoteFusion` — a vote. Robust to one source being wildly miscalibrated
  in magnitude, since only the sign of each opinion counts toward the winner.
* `UnanimousFusion` — a veto. Trades only when nobody disagrees, and then only
  as strongly as its least convinced member.

`agreement` is reported alongside every fused signal so a strategy can require
consensus without re-deriving it. Its definition is fixed for all three
strategies (see `SignalFusion._agreement`) so the number means the same thing
whichever fusion is configured.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from . import ta
from .clock import Clock, default_clock
from .contracts import (Candle, DataQuality, ForecastResult, Regime, Signal,
                        SignalDirection, ensure_utc, jsonable)
from .errors import ConfigurationError, DataValidationError

_LONG = SignalDirection.LONG
_SHORT = SignalDirection.SHORT
_FLAT = SignalDirection.FLAT

#: Below this a "strength" is indistinguishable from float noise and is treated
#: as no direction at all. One place, so every rule uses the same dead zone.
_EPS = 1e-12


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    """Clamp, mapping NaN to 0.0 rather than propagating it into a contract.

    NaN is how a divide-by-zero deep in an indicator becomes a `Signal` that
    passes every range check by accident (`nan <= 1.0` is False, but so is
    `nan >= -1.0`, so the validator's message would be misleading). Turning it
    into "no conviction" here keeps the failure legible.
    """
    if value is None or not math.isfinite(float(value)):
        return 0.0
    return max(low, min(high, float(value)))


def _sign(value: float) -> int:
    if value > _EPS:
        return 1
    if value < -_EPS:
        return -1
    return 0


def _direction_sign(direction: SignalDirection) -> int:
    if direction is _LONG:
        return 1
    if direction is _SHORT:
        return -1
    return 0


# ---------------------------------------------------------------------------
# context
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SignalContext:
    """Everything a generator is allowed to see.

    A closed set, deliberately: a generator that can only read this object
    cannot reach the portfolio, the broker, or the limits store, so "signals are
    advisory" is a property of the type rather than of reviewer vigilance.

    `coerce` accepts the pipeline's keyword-argument call style, a mapping, or
    any object with the right attribute names, so this module composes with
    `pipeline.TradingPipeline` without either side importing the other.
    """
    as_of: datetime
    instrument_key: str = ""
    timeframe: str = ""
    candles: tuple[Candle, ...] = ()
    forecasts: Mapping[str, ForecastResult] = field(default_factory=dict)
    quote: Any = None
    instrument: Any = None
    #: A `regime.RegimeState`, a `Regime`, or None. Duck-typed so `signals` does
    #: not have to import `regime` (and therefore `volatility`) to be usable.
    regime: Any = None
    #: A `volatility.VolatilityState` or None.
    volatility: Any = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    _FIELDS = ("as_of", "instrument_key", "timeframe", "candles", "forecasts",
               "quote", "instrument", "regime", "volatility", "extra")

    def __post_init__(self):
        object.__setattr__(self, "as_of", ensure_utc(self.as_of, field_name="as_of"))
        object.__setattr__(self, "candles", tuple(self.candles or ()))
        object.__setattr__(self, "forecasts", dict(self.forecasts or {}))
        object.__setattr__(self, "extra", dict(self.extra or {}))
        if not self.instrument_key:
            key = getattr(self.instrument, "key", "")
            if not key and self.candles:
                key = self.candles[-1].instrument_key
            object.__setattr__(self, "instrument_key", key or "")
        if not self.timeframe and self.candles:
            object.__setattr__(self, "timeframe", self.candles[-1].timeframe)

    @property
    def closes(self) -> list[float]:
        return [c.close for c in self.candles]

    @property
    def last(self) -> Candle | None:
        return self.candles[-1] if self.candles else None

    def forecast(self, name: str) -> ForecastResult | None:
        value = self.forecasts.get(name)
        return value if isinstance(value, ForecastResult) else None

    @classmethod
    def coerce(cls, context: Any = None, *, default_as_of: datetime | None = None,
               **kwargs) -> "SignalContext":
        if isinstance(context, SignalContext) and not kwargs:
            return context                   # already the right shape; keep it
        data: dict[str, Any] = {}
        if context is not None:
            if isinstance(context, SignalContext):
                data.update({f: getattr(context, f) for f in cls._FIELDS})
            elif isinstance(context, Mapping):
                data.update({k: v for k, v in context.items() if k in cls._FIELDS})
            else:
                data.update({f: getattr(context, f) for f in cls._FIELDS
                             if getattr(context, f, None) is not None})
        # Unknown keywords are folded into `extra` rather than rejected: the
        # pipeline may grow a new argument, and a signal generator refusing to
        # run because it was handed one more piece of context is a worse
        # failure than ignoring it.
        extra = dict(data.get("extra") or {})
        for key, value in kwargs.items():
            if key in cls._FIELDS:
                if value is not None:
                    data[key] = value
            else:
                extra[key] = value
        data["extra"] = extra
        if data.get("as_of") is None:
            if default_as_of is None:
                raise ConfigurationError(
                    "signal context needs an as_of; a generator must never "
                    "fall back to wall-clock time implicitly")
            data["as_of"] = default_as_of
        return cls(**data)

    def to_dict(self) -> dict:
        """Audit view. Candles are summarised, not dumped — a context record is
        provenance, not a second copy of the market data store."""
        return {
            "as_of": jsonable(self.as_of),
            "instrument_key": self.instrument_key,
            "timeframe": self.timeframe,
            "bars": len(self.candles),
            "forecasts": sorted(self.forecasts),
            "has_quote": self.quote is not None,
            "has_regime": self.regime is not None,
            "has_volatility": self.volatility is not None,
        }


# ---------------------------------------------------------------------------
# generators
# ---------------------------------------------------------------------------

class SignalGenerator(ABC):
    """Turns a `SignalContext` into zero or more `Signal`s.

    `generate` is concrete and does the context normalisation; subclasses
    implement `_generate`. That split is what lets the same generator be called
    as `generate(ctx)` by a strategy and as `generate(instrument=..., ...)` by
    the pipeline, without every subclass reimplementing the coercion — and
    without the ABC forcing one caller's shape onto the other.
    """

    #: Prefix used for the `Signal.source` of everything this generator emits.
    source = "generator"

    def __init__(self, *, source: str | None = None, clock: Clock | None = None):
        if source is not None:
            if not str(source).strip():
                raise ConfigurationError("signal source must be non-empty")
            self.source = str(source)
        self.clock = clock or default_clock()

    def generate(self, context: Any = None, **kwargs) -> list[Signal]:
        ctx = SignalContext.coerce(context, default_as_of=self.clock.now(),
                                   **kwargs)
        return list(self._generate(ctx) or ())

    @abstractmethod
    def _generate(self, ctx: SignalContext) -> Iterable[Signal]:
        """Produce signals, or nothing at all when there is no basis for one."""

    def describe(self) -> dict:
        return {"source": self.source, "class": type(self).__name__}

    def __repr__(self) -> str:               # pragma: no cover - trivial
        return f"{type(self).__name__}(source={self.source!r})"


class KronosSignalGenerator(SignalGenerator):
    """A `ForecastResult` becomes at most one `Signal`.

    Every rule here exists to stop a forecast being trusted further than it
    deserves:

    * **An abstaining or unusable forecast produces no signal at all.** Not a
      FLAT — a FLAT would enter the vote and dilute the sources that did have
      an opinion, silently converting "the model declined" into "the model saw
      nothing".
    * **Direction needs a threshold.** A predicted return of one basis point is
      not a bullish forecast, it is rounding. `min_expected_return` is the
      band inside which the honest answer is FLAT.
    * **Strength is a t-statistic, not a return.** `expected_return /
      forecast_volatility` — a 2% move predicted with 1% dispersion is a far
      stronger claim than a 2% move predicted with 8%. Scaled by `t_scale`
      (the t-value that earns full conviction) and clamped to [-1, 1], because
      the contract's strength is a conviction, not a magnitude.
    * **Confidence comes from the model's own dispersion, gated by data
      quality.** `ForecastResult.confidence` is `1 - uncertainty`; a DEGRADED
      window multiplies it down. A model cannot talk its way past bad data.

    Note the consequence for the deterministic baselines: they emit a single
    path, so `uncertainty` is 1.0 and confidence is 0.0. A baseline can state a
    direction but carries no weight in fusion — which is exactly the intended
    treatment of a null hypothesis.
    """

    source = "kronos"

    def __init__(self, *, forecast_name: str = "kronos",
                 min_expected_return: float = 0.001,
                 t_scale: float = 2.0,
                 max_uncertainty: float = 1.0,
                 min_confidence: float = 0.0,
                 min_data_quality: DataQuality = DataQuality.DEGRADED,
                 degraded_confidence_factor: float = 0.5,
                 undispersed_strength: float = 0.25,
                 horizon_in_signal: bool = True,
                 source: str | None = None, clock: Clock | None = None):
        super().__init__(source=source or forecast_name, clock=clock)
        for name, value in (("min_expected_return", min_expected_return),
                            ("t_scale", t_scale),
                            ("degraded_confidence_factor", degraded_confidence_factor),
                            ("undispersed_strength", undispersed_strength)):
            if not isinstance(value, (int, float)) or isinstance(value, bool) \
                    or not math.isfinite(float(value)) or value < 0:
                raise ConfigurationError(f"{name} must be a finite number >= 0",
                                         **{name: repr(value)})
        if t_scale <= 0:
            raise ConfigurationError("t_scale must be > 0", t_scale=t_scale)
        if not 0.0 <= max_uncertainty <= 1.0:
            raise ConfigurationError("max_uncertainty must be within [0, 1]",
                                     max_uncertainty=max_uncertainty)
        if not 0.0 <= min_confidence <= 1.0:
            raise ConfigurationError("min_confidence must be within [0, 1]",
                                     min_confidence=min_confidence)
        if not 0.0 <= degraded_confidence_factor <= 1.0:
            raise ConfigurationError(
                "degraded_confidence_factor must be within [0, 1]",
                degraded_confidence_factor=degraded_confidence_factor)
        if not 0.0 <= undispersed_strength <= 1.0:
            raise ConfigurationError("undispersed_strength must be within [0, 1]",
                                     undispersed_strength=undispersed_strength)
        self.forecast_name = forecast_name
        self.min_expected_return = float(min_expected_return)
        self.t_scale = float(t_scale)
        self.max_uncertainty = float(max_uncertainty)
        self.min_confidence = float(min_confidence)
        self.min_data_quality = min_data_quality
        self.degraded_confidence_factor = float(degraded_confidence_factor)
        self.undispersed_strength = float(undispersed_strength)
        self.horizon_in_signal = bool(horizon_in_signal)

    def describe(self) -> dict:
        return {**super().describe(), "forecast_name": self.forecast_name,
                "min_expected_return": self.min_expected_return,
                "t_scale": self.t_scale,
                "max_uncertainty": self.max_uncertainty,
                "min_data_quality": self.min_data_quality.value}

    def _generate(self, ctx: SignalContext) -> list[Signal]:
        raw = ctx.forecasts.get(self.forecast_name)
        if raw is None:
            return []                        # the model did not run: silence
        if not isinstance(raw, ForecastResult):
            # Wiring error, not a market condition. Loud, because a strategy
            # would otherwise silently trade with one fewer input forever.
            raise ConfigurationError(
                "forecasts[name] must be a ForecastResult",
                name=self.forecast_name, got=type(raw).__name__)
        forecast: ForecastResult = raw

        if ctx.instrument_key and forecast.instrument_key \
                and forecast.instrument_key != ctx.instrument_key:
            raise ConfigurationError(
                "forecast is for a different instrument than the context",
                expected=ctx.instrument_key, got=forecast.instrument_key)

        # Silence, in order of how badly wrong acting would be.
        if forecast.abstained or not forecast.usable:
            return []
        if forecast.data_quality is DataQuality.UNUSABLE:
            return []
        if (self.min_data_quality is DataQuality.OK
                and forecast.data_quality is not DataQuality.OK):
            return []
        if forecast.uncertainty > self.max_uncertainty:
            return []

        confidence = _clamp(forecast.confidence, 0.0, 1.0)
        if forecast.data_quality is DataQuality.DEGRADED:
            confidence *= self.degraded_confidence_factor
        if confidence < self.min_confidence:
            return []

        expected = float(forecast.expected_return)
        if not math.isfinite(expected):
            return []
        if expected > self.min_expected_return:
            direction = _LONG
        elif expected < -self.min_expected_return:
            direction = _SHORT
        else:
            direction = _FLAT

        # Dispersion for the t-statistic: the forecast's own, else the input
        # window's realised vol as a stand-in, else none at all.
        dispersion = float(forecast.forecast_volatility or 0.0)
        basis = "forecast_volatility"
        if dispersion <= 0:
            dispersion = float(forecast.realised_volatility or 0.0)
            basis = "realised_volatility"
        if dispersion > 0:
            t_stat = expected / dispersion
            strength = _clamp(t_stat / self.t_scale)
        else:
            # No dispersion estimate anywhere. We can state a direction but we
            # have not earned the right to state a size, so conviction is
            # pinned to a deliberately small configured value.
            basis = "none"
            t_stat = float("nan")
            strength = _sign(expected) * self.undispersed_strength

        if direction is _FLAT:
            strength = 0.0
        elif direction is _LONG:
            strength = max(0.0, strength)    # the contract forbids a negative LONG
        else:
            strength = min(0.0, strength)

        rationale = (f"expected return {expected:+.4%} over {forecast.horizon} "
                     f"bar(s) vs threshold {self.min_expected_return:.4%}; "
                     f"dispersion basis={basis}; uncertainty "
                     f"{forecast.uncertainty:.3f}")
        features = {
            "expected_return": expected,
            "forecast_volatility": float(forecast.forecast_volatility),
            "realised_volatility": float(forecast.realised_volatility),
            "uncertainty": float(forecast.uncertainty),
            "t_stat": None if math.isnan(t_stat) else t_stat,
            "dispersion_basis": basis,
            "n_paths": forecast.n_paths,
            "horizon": forecast.horizon,
            "direction_probabilities": dict(forecast.direction_probabilities),
        }
        return [Signal(
            source=self.source, instrument_key=ctx.instrument_key,
            direction=direction, strength=_clamp(strength),
            confidence=_clamp(confidence, 0.0, 1.0), ts=ctx.as_of,
            horizon=forecast.horizon if self.horizon_in_signal else 0,
            rationale=rationale, features=features,
            model_version=forecast.model_version,
            data_quality=forecast.data_quality)]


class TASignalGenerator(SignalGenerator):
    """Classical technical rules, each one documented and independently on/off.

    Three rule sets, chosen because they fail in *different* markets — which is
    the only reason to run more than one:

    * ``ma_cross`` (trend following). LONG while EMA(fast) is above EMA(slow).
      Conviction scales with the normalised gap between them; confidence grows
      with how long the cross has held, because a cross printed this bar is the
      one most likely to be noise. Fails in a range, where it whipsaws.
    * ``rsi_reversion`` (mean reversion). LONG below `oversold`, SHORT above
      `overbought`, FLAT in between — an explicit FLAT, because the rule *did*
      look and found no extreme. Fails in a trend, where oversold gets more so.
    * ``breakout`` (Donchian). LONG when the close exceeds the highest high of
      the previous `period` bars **excluding the current one** (including it
      would make the channel unbreakable), scaled by ATR so a breakout is
      measured in units of the instrument's own noise.

    These rules cannot assess their own reliability, so confidence is an
    operator-configured prior (`rule_confidence`) rather than a number invented
    per bar. Conviction lives in `strength`, where it belongs.
    """

    source = "ta"
    RULES = ("ma_cross", "rsi_reversion", "breakout")

    def __init__(self, *, rules: Sequence[str] = ("ma_cross", "rsi_reversion"),
                 fast: int = 12, slow: int = 26, rsi_period: int = 14,
                 oversold: float = 30.0, overbought: float = 70.0,
                 breakout_period: int = 20, atr_period: int = 14,
                 gap_scale: float = 0.01, confirm_bars: int = 3,
                 rule_confidence: float = 0.5,
                 source: str | None = None, clock: Clock | None = None):
        super().__init__(source=source, clock=clock)
        chosen = tuple(rules or ())
        if not chosen:
            raise ConfigurationError("at least one rule must be enabled")
        unknown = [r for r in chosen if r not in self.RULES]
        if unknown:
            raise ConfigurationError("unknown TA rule", unknown=unknown,
                                     known=list(self.RULES))
        if fast >= slow:
            raise ConfigurationError("fast period must be < slow period",
                                     fast=fast, slow=slow)
        if not 0.0 < oversold < overbought < 100.0:
            raise ConfigurationError(
                "require 0 < oversold < overbought < 100",
                oversold=oversold, overbought=overbought)
        if gap_scale <= 0:
            raise ConfigurationError("gap_scale must be > 0", gap_scale=gap_scale)
        if confirm_bars < 1:
            raise ConfigurationError("confirm_bars must be >= 1",
                                     confirm_bars=confirm_bars)
        if not 0.0 <= rule_confidence <= 1.0:
            raise ConfigurationError("rule_confidence must be within [0, 1]",
                                     rule_confidence=rule_confidence)
        self.rules = chosen
        self.fast, self.slow = int(fast), int(slow)
        self.rsi_period = int(rsi_period)
        self.oversold, self.overbought = float(oversold), float(overbought)
        self.breakout_period = int(breakout_period)
        self.atr_period = int(atr_period)
        self.gap_scale = float(gap_scale)
        self.confirm_bars = int(confirm_bars)
        self.rule_confidence = float(rule_confidence)

    def describe(self) -> dict:
        return {**super().describe(), "rules": list(self.rules),
                "fast": self.fast, "slow": self.slow,
                "rsi_period": self.rsi_period,
                "breakout_period": self.breakout_period}

    def _generate(self, ctx: SignalContext) -> list[Signal]:
        bars = [c for c in ctx.candles if c.is_final]
        if not bars:
            return []
        out: list[Signal] = []
        for rule in self.rules:
            produced = getattr(self, f"_rule_{rule}")(ctx, bars)
            if produced is not None:
                out.append(produced)
        return out

    # -- individual rules --------------------------------------------------

    def _emit(self, ctx: SignalContext, rule: str, direction: SignalDirection,
              strength: float, rationale: str, features: Mapping[str, Any],
              *, confidence: float | None = None) -> Signal:
        strength = _clamp(strength)
        if direction is _FLAT:
            strength = 0.0
        elif direction is _LONG:
            strength = max(0.0, strength)
        else:
            strength = min(0.0, strength)
        return Signal(
            source=f"{self.source}.{rule}", instrument_key=ctx.instrument_key,
            direction=direction, strength=strength,
            confidence=_clamp(self.rule_confidence if confidence is None
                              else confidence, 0.0, 1.0),
            ts=ctx.as_of, rationale=rationale,
            features={k: jsonable(v) for k, v in dict(features).items()},
            model_version=f"ta.{rule}-1")

    def _rule_ma_cross(self, ctx: SignalContext,
                       bars: Sequence[Candle]) -> Signal | None:
        closes = [c.close for c in bars]
        if len(closes) < self.slow + 1:
            return None                      # not enough history: silence
        fast = ta.ema(closes, self.fast)
        slow = ta.ema(closes, self.slow)
        f, s = fast[-1], slow[-1]
        if f is None or s is None or s == 0:
            return None
        gap = (f - s) / s
        direction = _LONG if _sign(gap) > 0 else (_SHORT if _sign(gap) < 0 else _FLAT)
        cross = ta.last_cross(fast, slow)
        # Age of the current cross, in bars. No cross inside the window means
        # the relationship has held for the whole window — maximal confirmation.
        held = len(closes) if cross is None else (len(closes) - 1 - cross[0])
        confidence = self.rule_confidence * min(1.0, (held + 1) / self.confirm_bars)
        return self._emit(
            ctx, "ma_cross", direction, gap / self.gap_scale,
            f"EMA{self.fast} {'above' if gap > 0 else 'below'} EMA{self.slow} "
            f"by {gap:+.3%}, held {held} bar(s)",
            {"fast": f, "slow": s, "gap": gap, "bars_since_cross": held},
            confidence=confidence)

    def _rule_rsi_reversion(self, ctx: SignalContext,
                            bars: Sequence[Candle]) -> Signal | None:
        closes = [c.close for c in bars]
        values = ta.rsi(closes, self.rsi_period)
        value = values[-1] if values else None
        if value is None:
            return None
        if value <= self.oversold:
            direction, strength = _LONG, (self.oversold - value) / self.oversold
        elif value >= self.overbought:
            direction = _SHORT
            strength = -(value - self.overbought) / (100.0 - self.overbought)
        else:
            direction, strength = _FLAT, 0.0
        return self._emit(
            ctx, "rsi_reversion", direction, strength,
            f"RSI{self.rsi_period}={value:.1f} vs band "
            f"[{self.oversold:.0f}, {self.overbought:.0f}]",
            {"rsi": value, "oversold": self.oversold,
             "overbought": self.overbought})

    def _rule_breakout(self, ctx: SignalContext,
                       bars: Sequence[Candle]) -> Signal | None:
        if len(bars) < self.breakout_period + 1:
            return None
        upper, _, lower = ta.donchian(bars, self.breakout_period)
        # Shift by one: the channel must exclude the bar being tested, or price
        # can never exceed a channel it is itself the boundary of.
        top, bottom = upper[-2], lower[-2]
        if top is None or bottom is None:
            return None
        band = ta.atr(bars, self.atr_period)
        width = band[-1] if band else None
        if not width or width <= 0:
            width = max(top - bottom, _EPS)
        close = bars[-1].close
        if close > top:
            direction, strength = _LONG, (close - top) / width
        elif close < bottom:
            direction, strength = _SHORT, -(bottom - close) / width
        else:
            direction, strength = _FLAT, 0.0
        return self._emit(
            ctx, "breakout", direction, strength,
            f"close {close:.4f} vs Donchian({self.breakout_period}) "
            f"[{bottom:.4f}, {top:.4f}], {width:.4f} per ATR unit",
            {"close": close, "upper": top, "lower": bottom, "atr": width})


class RegimeSignalGenerator(SignalGenerator):
    """The market's *state* as an opinion about direction.

    A regime is not a trade idea, so the mapping is deliberately conservative:
    a trending regime carries a directional opinion at the classifier's own
    confidence; ranging, high-volatility and crisis regimes map to **FLAT** —
    real information that damps whatever the other sources want to do — and an
    UNKNOWN regime produces no signal at all, because "we have not warmed up
    yet" is ignorance, not a view.

    Nothing here decides *not to trade*; that is the risk engine's job. This
    only makes the state visible to fusion.
    """

    source = "regime"

    #: Regime → (direction, does the classifier's confidence carry a direction?)
    _MAP = {
        Regime.TRENDING_UP: _LONG,
        Regime.TRENDING_DOWN: _SHORT,
        Regime.RANGING: _FLAT,
        Regime.HIGH_VOLATILITY: _FLAT,
        Regime.CRISIS: _FLAT,
    }

    def __init__(self, *, classifier: Any = None, strength_scale: float = 1.0,
                 min_confidence: float = 0.0,
                 source: str | None = None, clock: Clock | None = None):
        super().__init__(source=source, clock=clock)
        if not 0.0 < strength_scale <= 1.0:
            raise ConfigurationError("strength_scale must be within (0, 1]",
                                     strength_scale=strength_scale)
        if not 0.0 <= min_confidence <= 1.0:
            raise ConfigurationError("min_confidence must be within [0, 1]",
                                     min_confidence=min_confidence)
        self.classifier = classifier
        self.strength_scale = float(strength_scale)
        self.min_confidence = float(min_confidence)

    def _state(self, ctx: SignalContext) -> tuple[Regime, float] | None:
        """Read the regime from the context, or classify if given a classifier."""
        state = ctx.regime
        if state is None and self.classifier is not None and ctx.candles:
            try:
                state = self.classifier.classify(ctx.candles)
            except Exception:                # noqa: BLE001 - no history is not an error
                return None
        if state is None:
            return None
        if isinstance(state, Regime):
            return state, 1.0
        if isinstance(state, str):
            try:
                return Regime(state), 1.0
            except ValueError:
                return None
        regime = getattr(state, "regime", None)
        if not isinstance(regime, Regime):
            return None
        return regime, _clamp(getattr(state, "confidence", 1.0), 0.0, 1.0)

    def _generate(self, ctx: SignalContext) -> list[Signal]:
        found = self._state(ctx)
        if found is None:
            return []
        regime, confidence = found
        if regime is Regime.UNKNOWN:
            return []                        # no opinion, not a flat opinion
        if confidence < self.min_confidence:
            return []
        direction = self._MAP.get(regime, _FLAT)
        magnitude = confidence * self.strength_scale
        strength = {_LONG: magnitude, _SHORT: -magnitude}.get(direction, 0.0)
        return [Signal(
            source=self.source, instrument_key=ctx.instrument_key,
            direction=direction, strength=_clamp(strength),
            confidence=confidence, ts=ctx.as_of,
            rationale=f"regime={regime.value} at confidence {confidence:.2f}",
            features={"regime": regime.value, "regime_confidence": confidence},
            model_version="regime-1")]


class VolatilitySignalGenerator(SignalGenerator):
    """Risk-off when volatility is extreme, and silent otherwise.

    Single-purpose by design. When the percentile rank of realised volatility is
    at or above `extreme_rank`, this emits a FLAT signal whose confidence is the
    rank itself — so a 0.99 reading damps a fused position harder than a 0.95
    one. At any other level it emits **nothing**: silence here means "volatility
    has no view on direction", which is true, and emitting a permanent FLAT
    would quietly halve every fused strength the system ever produced.

    A FLAT signal is not a stop. Position sizing and hard limits belong to
    `risk.py`; this is one advisory input that argues for standing aside.
    """

    source = "volatility"

    def __init__(self, *, extreme_rank: float = 0.95, analyzer: Any = None,
                 min_confidence: float = 0.0,
                 source: str | None = None, clock: Clock | None = None):
        super().__init__(source=source, clock=clock)
        if not 0.0 < extreme_rank < 1.0:
            raise ConfigurationError("extreme_rank must be within (0, 1)",
                                     extreme_rank=extreme_rank)
        if not 0.0 <= min_confidence <= 1.0:
            raise ConfigurationError("min_confidence must be within [0, 1]",
                                     min_confidence=min_confidence)
        self.extreme_rank = float(extreme_rank)
        self.analyzer = analyzer
        self.min_confidence = float(min_confidence)

    def _state(self, ctx: SignalContext) -> Any:
        state = ctx.volatility
        if state is None and self.analyzer is not None and ctx.candles:
            try:
                state = self.analyzer.analyse(ctx.candles)
            except AttributeError:           # a differently-named analyser
                try:
                    state = self.analyzer.analyze(ctx.candles)
                except Exception:            # noqa: BLE001
                    return None
            except Exception:                # noqa: BLE001 - short history
                return None
        return state

    def _generate(self, ctx: SignalContext) -> list[Signal]:
        state = self._state(ctx)
        if state is None:
            return []
        rank = getattr(state, "percentile_rank", None)
        if rank is None or not isinstance(rank, (int, float)) \
                or isinstance(rank, bool) or not math.isfinite(float(rank)):
            return []
        rank = float(rank)
        if rank < self.extreme_rank:
            return []                        # silence is not endorsement
        confidence = _clamp(rank, 0.0, 1.0)
        if confidence < self.min_confidence:
            return []
        label = getattr(state, "regime_label", "extreme")
        return [Signal(
            source=self.source, instrument_key=ctx.instrument_key,
            direction=_FLAT, strength=0.0, confidence=confidence, ts=ctx.as_of,
            rationale=(f"realised volatility at percentile {rank:.2f} "
                       f"(label={label}); risk-off"),
            features={"percentile_rank": rank, "label": str(label),
                      "realised": float(getattr(state, "realised", 0.0) or 0.0)},
            model_version="volatility-1")]


# ---------------------------------------------------------------------------
# fusion
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FusedSignal:
    """The combined view of several sources, with its own workings attached.

    `contributions`, `agreement`, `sources` and `dropped` are not decoration:
    they are what lets an operator answer "why did we go long on 3 March" from
    the audit trail alone, without re-running anything. A fused signal that
    recorded only its direction and strength would be exactly as opaque as the
    black box this architecture exists to avoid.
    """
    instrument_key: str
    direction: SignalDirection
    strength: float
    confidence: float
    ts: datetime
    contributions: Mapping[str, float] = field(default_factory=dict)
    agreement: float = 0.0
    sources: tuple[str, ...] = ()
    #: "source:reason" for every input that was excluded (stale, foreign, …).
    dropped: tuple[str, ...] = ()
    rationale: str = ""

    def __post_init__(self):
        object.__setattr__(self, "ts", ensure_utc(self.ts, field_name="ts"))
        object.__setattr__(self, "strength", float(self.strength))
        object.__setattr__(self, "confidence", float(self.confidence))
        object.__setattr__(self, "agreement", float(self.agreement))
        object.__setattr__(self, "contributions",
                           {str(k): float(v)
                            for k, v in dict(self.contributions).items()})
        object.__setattr__(self, "sources", tuple(self.sources))
        object.__setattr__(self, "dropped", tuple(self.dropped))
        if not -1.0 <= self.strength <= 1.0:
            raise DataValidationError("fused strength must be within [-1, 1]",
                                      got=self.strength)
        if not 0.0 <= self.confidence <= 1.0:
            raise DataValidationError("fused confidence must be within [0, 1]",
                                      got=self.confidence)
        if not 0.0 <= self.agreement <= 1.0:
            raise DataValidationError("agreement must be within [0, 1]",
                                      got=self.agreement)
        # The structural half of "fusion never invents a direction": a FLAT
        # result cannot smuggle conviction through its strength field.
        if self.direction is _FLAT and self.strength != 0.0:
            raise DataValidationError("a FLAT fused signal must have zero "
                                      "strength", got=self.strength)
        if self.direction is _LONG and self.strength < 0:
            raise DataValidationError("LONG fused signal cannot be negative",
                                      got=self.strength)
        if self.direction is _SHORT and self.strength > 0:
            raise DataValidationError("SHORT fused signal cannot be positive",
                                      got=self.strength)

    @property
    def is_actionable(self) -> bool:
        """True when there is a direction with non-zero conviction behind it."""
        return self.direction is not _FLAT and abs(self.strength) > _EPS

    def to_dict(self) -> dict:
        return {k: jsonable(v) for k, v in asdict(self).items()}


class SignalFusion(ABC):
    """Base class: screening, weighting, agreement — everything but the vote.

    The screening rules are shared because they are safety rules, not tuning
    knobs, and every fusion strategy must apply them identically:

    * **Stale signals are dropped.** An opinion formed twenty bars ago is not
      evidence about now. `max_age_s` is measured against `as_of`, never wall
      time, so a backtest ages signals on backtest time.
    * **Signals from the future are dropped.** `ts > as_of` means a generator
      leaked the future; it must not reach a decision.
    * **Foreign instruments are dropped.** Fusing AAPL's momentum into MSFT's
      decision is a defect that produces plausible-looking numbers, which is the
      worst kind.
    * **One opinion per source.** A duplicated source would otherwise vote
      twice. The newest wins.

    Every exclusion is recorded in `FusedSignal.dropped`; nothing is discarded
    silently.

    Determinism: after screening, signals are sorted by source name before any
    arithmetic, so float addition happens in a fixed order regardless of how the
    generators were registered.
    """

    def __init__(self, *, weights: Mapping[str, float] | None = None,
                 default_weight: float = 1.0, max_age_s: float | None = None,
                 min_confidence: float = 0.0, min_strength: float = 0.0,
                 clock: Clock | None = None):
        cleaned: dict[str, float] = {}
        for source, weight in dict(weights or {}).items():
            if not isinstance(weight, (int, float)) or isinstance(weight, bool) \
                    or not math.isfinite(float(weight)) or weight < 0:
                raise ConfigurationError(
                    "signal weight must be a finite number >= 0; a negative "
                    "weight would invert a source's opinion rather than "
                    "discount it", source=source, weight=repr(weight))
            cleaned[str(source)] = float(weight)
        if default_weight < 0 or not math.isfinite(float(default_weight)):
            raise ConfigurationError("default_weight must be finite and >= 0",
                                     default_weight=default_weight)
        if max_age_s is not None and max_age_s <= 0:
            raise ConfigurationError("max_age_s must be > 0", max_age_s=max_age_s)
        if not 0.0 <= min_confidence <= 1.0:
            raise ConfigurationError("min_confidence must be within [0, 1]",
                                     min_confidence=min_confidence)
        if not 0.0 <= min_strength <= 1.0:
            raise ConfigurationError("min_strength must be within [0, 1]",
                                     min_strength=min_strength)
        self.weights = cleaned
        self.default_weight = float(default_weight)
        self.max_age_s = None if max_age_s is None else float(max_age_s)
        self.min_confidence = float(min_confidence)
        self.min_strength = float(min_strength)
        self.clock = clock or default_clock()

    # -- weighting ---------------------------------------------------------

    def weight_for(self, source: str) -> float:
        """Configured weight for a source.

        Exact match first, then the longest configured *prefix* ending in "."
        (so ``{"ta.": 0.5}`` halves every TA rule at once without naming them),
        then `default_weight`. Longest prefix wins, so a specific override beats
        a family default.
        """
        if source in self.weights:
            return self.weights[source]
        best, best_len = None, -1
        for key, weight in self.weights.items():
            if key.endswith(".") and source.startswith(key) and len(key) > best_len:
                best, best_len = weight, len(key)
        return self.default_weight if best is None else best

    def _normalised(self, signals: Sequence[Signal]) -> dict[str, float]:
        """Weights over the sources actually present, summing to 1.

        Normalising over *present* sources is what makes a missing source
        harmless: if the Kronos forecaster abstains, the remaining sources
        share the full weight rather than the fused strength being silently
        scaled down by the absent one's share.
        """
        raw = {s.source: self.weight_for(s.source) for s in signals}
        total = sum(raw.values())
        if total <= 0:
            # Everything is weighted zero. Rather than divide by zero, fall back
            # to equal weights — the caller asked to ignore every source, and
            # the honest answer is a FLAT built from a well-defined mean.
            return {k: 1.0 / len(raw) for k in raw} if raw else {}
        return {k: v / total for k, v in raw.items()}

    def _effective(self, signals: Sequence[Signal],
                   weights: Mapping[str, float]) -> dict[str, float]:
        """Weight × confidence: the weight that actually moves the result.

        Confidence belongs in the weight, not only in the output: a source that
        says "long, but I barely know" should move the fused strength less than
        one that says "long, and I am sure", and multiplying is the simplest
        rule that has that property and stays in [0, weight].
        """
        return {s.source: weights.get(s.source, 0.0) * s.confidence
                for s in signals}

    # -- agreement ---------------------------------------------------------

    def _agreement(self, signals: Sequence[Signal],
                   weights: Mapping[str, float]) -> float:
        """Cross-source consensus in [0, 1].

        Defined as the *weighted directional net over the weighted directional
        gross*::

            agreement = |Σ wᵢ·dᵢ| / Σ wᵢ·|dᵢ|,   dᵢ ∈ {+1, 0, −1}

        1.0 when every directional source points the same way, 0.0 when they are
        perfectly balanced, and in between for a partial dissent. Weights are
        weight × confidence, so a confident dissenter costs more agreement than
        a hesitant one.

        FLAT sources abstain from the direction vote: they neither agree nor
        disagree, so they appear in neither sum (they still damp `strength`
        through the fusion itself). When *no* source has a direction, the group
        unanimously has no view, and that is agreement 1.0 — an empty input, by
        contrast, has nothing to agree about and scores 0.0.
        """
        if not signals:
            return 0.0
        effective = self._effective(signals, weights)
        if sum(effective.values()) <= 0:
            # Nobody claims any confidence; fall back to configured weights so
            # agreement still describes the directions on the table.
            effective = {s.source: weights.get(s.source, 0.0) for s in signals}
        directional = [s for s in signals if s.direction is not _FLAT]
        gross = sum(effective.get(s.source, 0.0) for s in directional)
        if gross <= 0:
            return 1.0
        net = sum(effective.get(s.source, 0.0) * _direction_sign(s.direction)
                  for s in directional)
        return _clamp(abs(net) / gross, 0.0, 1.0)

    # -- screening ---------------------------------------------------------

    def _screen(self, signals: Iterable[Signal], *, as_of: datetime,
                instrument_key: str) -> tuple[list[Signal], list[str]]:
        kept: dict[str, Signal] = {}
        dropped: list[str] = []
        cutoff = None if self.max_age_s is None \
            else as_of - timedelta(seconds=self.max_age_s)
        for signal in signals or ():
            if not isinstance(signal, Signal):
                dropped.append(f"{type(signal).__name__}:not-a-signal")
                continue
            if instrument_key and signal.instrument_key \
                    and signal.instrument_key != instrument_key:
                dropped.append(f"{signal.source}:foreign-instrument")
                continue
            if signal.ts > as_of:
                dropped.append(f"{signal.source}:from-the-future")
                continue
            if cutoff is not None and signal.ts < cutoff:
                dropped.append(f"{signal.source}:stale")
                continue
            if signal.confidence < self.min_confidence:
                dropped.append(f"{signal.source}:below-min-confidence")
                continue
            existing = kept.get(signal.source)
            if existing is None or signal.ts >= existing.ts:
                if existing is not None:
                    dropped.append(f"{signal.source}:superseded")
                kept[signal.source] = signal
            else:
                dropped.append(f"{signal.source}:superseded")
        # Sorted by source: the arithmetic below must not depend on the order
        # generators happened to run in.
        return sorted(kept.values(), key=lambda s: s.source), dropped

    # -- the vote ----------------------------------------------------------

    @abstractmethod
    def _combine(self, signals: Sequence[Signal], weights: Mapping[str, float]
                 ) -> tuple[SignalDirection, float, float, dict[str, float], str]:
        """Return (direction, strength, confidence, contributions, rationale)."""

    def fuse(self, signals: Iterable[Signal], *, as_of: datetime | None = None,
             instrument_key: str = "") -> FusedSignal:
        now = ensure_utc(as_of, field_name="as_of") if as_of is not None \
            else self.clock.now()
        incoming = list(signals or ())
        key = instrument_key
        if not key:
            for signal in incoming:
                if isinstance(signal, Signal) and signal.instrument_key:
                    key = signal.instrument_key
                    break
        kept, dropped = self._screen(incoming, as_of=now, instrument_key=key)
        if not kept:
            # Absent inputs produce no direction. Ever.
            return FusedSignal(
                instrument_key=key, direction=_FLAT, strength=0.0,
                confidence=0.0, ts=now, contributions={}, agreement=0.0,
                sources=(), dropped=tuple(dropped),
                rationale="no usable signals")

        weights = self._normalised(kept)
        direction, strength, confidence, contributions, rationale = \
            self._combine(kept, weights)
        strength = _clamp(strength)
        if direction is _FLAT or abs(strength) <= max(self.min_strength, _EPS):
            direction, strength = _FLAT, 0.0
        elif direction is _LONG:
            strength = max(0.0, strength)
        else:
            strength = min(0.0, strength)
        return FusedSignal(
            instrument_key=key, direction=direction, strength=strength,
            confidence=_clamp(confidence, 0.0, 1.0), ts=now,
            contributions=contributions,
            agreement=self._agreement(kept, weights),
            sources=tuple(s.source for s in kept), dropped=tuple(dropped),
            rationale=rationale)

    def describe(self) -> dict:
        return {"class": type(self).__name__, "weights": dict(self.weights),
                "default_weight": self.default_weight,
                "max_age_s": self.max_age_s,
                "min_confidence": self.min_confidence,
                "min_strength": self.min_strength}

    def __repr__(self) -> str:               # pragma: no cover - trivial
        return f"{type(self).__name__}(weights={self.weights})"


class WeightedAverageFusion(SignalFusion):
    """Weighted mean of the members' strengths.

    `contributions[source]` is that source's signed share of the fused strength,
    and they sum to it exactly (before clamping) — so the audit record answers
    "who moved this" arithmetically rather than by inference.

    Confidence is the weight-averaged confidence of the members, *not* the
    agreement: a unanimous panel of unsure sources should not report high
    confidence, and a split panel of certain ones should not report low
    confidence. Those are different questions, so they get different fields.
    """

    def _combine(self, signals, weights):
        effective = self._effective(signals, weights)
        total = sum(effective.values())
        contributions: dict[str, float] = {}
        if total > 0:
            for signal in signals:
                share = effective.get(signal.source, 0.0) / total
                contributions[signal.source] = share * signal.strength
            strength = sum(contributions.values())
        else:
            # Every member has zero confidence (e.g. only deterministic
            # baselines ran). Nobody is claiming to know anything, so the fused
            # view is FLAT rather than an unweighted average of guesses.
            contributions = {s.source: 0.0 for s in signals}
            strength = 0.0
        confidence = sum(weights.get(s.source, 0.0) * s.confidence
                         for s in signals)
        direction = _LONG if _sign(strength) > 0 else (
            _SHORT if _sign(strength) < 0 else _FLAT)
        parts = ", ".join(f"{k}{v:+.3f}" for k, v in sorted(contributions.items()))
        return direction, strength, confidence, contributions, \
            f"weighted average of {len(signals)} source(s): {parts}"


class MajorityVoteFusion(SignalFusion):
    """One vote per source, weighted by weight × confidence.

    Only the *sign* of each opinion picks the winner, which makes the result
    immune to a single miscalibrated source shouting a strength of 1.0 into a
    weighted average. Magnitude re-enters only afterwards, through the winning
    side's mean conviction.

    FLAT voters count in the denominator: a source that looked and saw nothing
    genuinely reduces the majority's margin. A source with zero confidence casts
    no vote at all — it has told us it does not know.

        strength = sign · margin · (weighted mean |strength| of the winners)
        margin   = (winner − loser) / (all votes, including FLAT)

    A tie is FLAT. `min_margin` requires a minimum majority before any direction
    is reported at all.
    """

    def __init__(self, *, min_margin: float = 0.0, **kwargs):
        super().__init__(**kwargs)
        if not 0.0 <= min_margin <= 1.0:
            raise ConfigurationError("min_margin must be within [0, 1]",
                                     min_margin=min_margin)
        self.min_margin = float(min_margin)

    def _combine(self, signals, weights):
        effective = self._effective(signals, weights)
        total = sum(effective.values())
        contributions = {s.source: effective.get(s.source, 0.0)
                         * _direction_sign(s.direction) / total if total > 0
                         else 0.0 for s in signals}
        confidence = sum(weights.get(s.source, 0.0) * s.confidence
                         for s in signals)
        if total <= 0:
            return _FLAT, 0.0, confidence, contributions, \
                "no source cast a vote (all confidences zero)"
        long_w = sum(effective.get(s.source, 0.0)
                     for s in signals if s.direction is _LONG)
        short_w = sum(effective.get(s.source, 0.0)
                      for s in signals if s.direction is _SHORT)
        if abs(long_w - short_w) <= _EPS:
            return _FLAT, 0.0, confidence, contributions, \
                f"tied vote ({long_w:.3f} long vs {short_w:.3f} short)"
        winner = _LONG if long_w > short_w else _SHORT
        margin = (max(long_w, short_w) - min(long_w, short_w)) / total
        if margin < self.min_margin:
            return _FLAT, 0.0, confidence, contributions, \
                f"majority margin {margin:.3f} below minimum {self.min_margin:.3f}"
        winners = [s for s in signals if s.direction is winner]
        winner_weight = sum(effective.get(s.source, 0.0) for s in winners)
        conviction = (sum(effective.get(s.source, 0.0) * abs(s.strength)
                          for s in winners) / winner_weight
                      if winner_weight > 0 else 0.0)
        strength = _direction_sign(winner) * margin * conviction
        return winner, strength, confidence, contributions, \
            (f"{winner.value} majority: margin {margin:.3f}, winning "
             f"conviction {conviction:.3f}")


class UnanimousFusion(SignalFusion):
    """Trade only when nobody disagrees — and then only as much as the least
    convinced member allows.

    Strength is the *minimum* magnitude among the agreeing sources and
    confidence the minimum confidence, because a chain of agreement is only as
    strong as its weakest link; taking the mean would let two enthusiasts drag
    a sceptic into a position size nobody actually endorsed.

    `treat_flat_as_dissent` defaults to True: a source that looked and saw no
    direction has not agreed, and this fusion is the one chosen precisely when
    the desk wants a veto. Set it False to let FLAT sources abstain instead.
    `min_sources` (default 2) stops unanimity being trivially satisfied by a
    single source talking to itself.

    `contributions` here are the members' own signed strengths, not additive
    shares — the fused value is their minimum, so there is nothing to add up.
    """

    def __init__(self, *, treat_flat_as_dissent: bool = True,
                 min_sources: int = 2, **kwargs):
        super().__init__(**kwargs)
        if min_sources < 1:
            raise ConfigurationError("min_sources must be >= 1",
                                     min_sources=min_sources)
        self.treat_flat_as_dissent = bool(treat_flat_as_dissent)
        self.min_sources = int(min_sources)

    def _combine(self, signals, weights):
        contributions = {s.source: float(s.strength) for s in signals}
        directional = [s for s in signals if s.direction is not _FLAT]
        flats = [s for s in signals if s.direction is _FLAT]
        # Disagreement yields no view at all, so confidence is 0.0 rather than
        # an average of confidences nobody agreed on.
        if self.treat_flat_as_dissent and flats:
            return _FLAT, 0.0, 0.0, contributions, \
                (f"{len(flats)} source(s) flat; unanimity requires a direction "
                 "from every source")
        if len(directional) < self.min_sources:
            return _FLAT, 0.0, 0.0, contributions, \
                (f"only {len(directional)} directional source(s); "
                 f"{self.min_sources} required")
        first = directional[0].direction
        if any(s.direction is not first for s in directional):
            return _FLAT, 0.0, 0.0, contributions, "sources disagree"
        strength = _direction_sign(first) * min(abs(s.strength) for s in directional)
        confidence = min(s.confidence for s in directional)
        return first, strength, confidence, contributions, \
            (f"unanimous {first.value} across {len(directional)} source(s); "
             f"held to the weakest member")


__all__ = [
    "SignalContext", "SignalGenerator", "KronosSignalGenerator",
    "TASignalGenerator", "RegimeSignalGenerator", "VolatilitySignalGenerator",
    "FusedSignal", "SignalFusion", "WeightedAverageFusion",
    "MajorityVoteFusion", "UnanimousFusion",
]

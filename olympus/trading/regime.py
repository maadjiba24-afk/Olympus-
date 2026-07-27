"""Deterministic, rule-based market regime classification.

A strategy that is right in a trend and wrong in a range is not a bad strategy;
it is a strategy that needs to know which one it is in. That is the whole job
of this module: label the current market as trending, ranging, unusually
volatile, or in crisis, so the strategy layer can gate itself and the
backtester can attribute performance per regime.

Why this is not an LLM
----------------------
Regime is an input to sizing and to risk gating, and every input to a risk
decision in this package must be reproducible, auditable, and immune to
prompt injection from whatever text happened to be scraped that morning. A
language model asked "what regime are we in?" gives a different answer on
Tuesday. This classifier is a pure function of the candle history plus a frozen
config: same bars in, same label out, forever. Sentiment and narrative live in
`sentiment.py` and reach a decision only as a *signal*, never as a regime.

Evidence, deliberately multi-factor
-----------------------------------
Four orthogonal measurements, because each one alone has a well-known failure
mode:

* **Trend slope** (drift of a fast SMA, expressed in per-bar sigmas) — the
  direct measurement, but it cannot tell a strong trend from a random walk that
  happened to drift.
* **ADX** — Wilder's directional-movement strength, which does distinguish
  those two, but says nothing about direction (it is high in a crash and in a
  melt-up alike).
* **Volatility percentile and ratio** — against the instrument's *own*
  trailing history, because "high volatility" is only meaningful relative to
  what that instrument normally does. The ratio guard is what stops a
  uniformly-volatile instrument being permanently labelled HIGH_VOLATILITY
  purely because the last reading happened to land in its own top decile.
* **Drawdown from a rolling high** — distinguishes an ordinary volatility
  expansion from a crisis; a crisis is high volatility *plus* a collapsed
  price, not either alone.

Hysteresis
----------
The classification a strategy consumes must be *stable*. A label that flips
between RANGING and TRENDING_UP on alternate bars turns the strategy into a
transaction-cost generator. So a new label must persist for
`confirm_bars` consecutive bars before it is adopted, and the confidence of the
currently-held label decays as contrary evidence accumulates — which gives the
strategy an early warning without ever handing it a flapping label. Warm-up is
the exception: the first real classification is adopted immediately, since
there is no held label to flap against.

Insufficient history returns `Regime.UNKNOWN` with confidence 0.0. It never
guesses. A caller that treats UNKNOWN as "probably ranging" is making that
choice explicitly, in its own code, where it can be reviewed.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Sequence

from .clock import Clock, default_clock
from .contracts import Candle, Regime, ensure_utc, jsonable
from .errors import ConfigurationError, DataValidationError, InsufficientDataError
from .volatility import (
    CONTINUOUS, Calendar, VolatilityAnalyzer, annualisation_factor,
    log_returns, realised_volatility,
)


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RegimeConfig:
    """Every threshold the classifier uses, with its rationale.

    Frozen, and passed in explicitly, so that a backtest result can be tied to
    the exact thresholds that produced it: "the regime filter worked" is not a
    reproducible claim unless the numbers travel with it.

    Defaults are tuned for liquid 1h crypto bars and are deliberately
    conservative — they under-declare trends rather than over-declare them,
    because a false TRENDING label costs money in a range while a missed trend
    only costs opportunity.
    """
    #: Fast/slow simple moving averages. The fast one carries the slope
    #: measurement; the slow one is only used for sign confirmation, so the
    #: pair does not need to be a tuned crossover system.
    fast_ma: int = 10
    slow_ma: int = 30
    #: Bars over which the fast SMA's drift is measured.
    slope_window: int = 10
    #: Slope threshold in per-bar standard deviations. 0.10 means "the trend
    #: adds a tenth of a bar's typical move, every bar" — small in isolation,
    #: substantial compounded over `slope_window`. Expressed in sigma rather
    #: than in percent so the same number works for a stablecoin pair and a
    #: micro-cap.
    slope_sigma: float = 0.10
    #: Wilder ADX period and the strength above which a move counts as
    #: directional. 20-25 is the conventional band; 20 is the permissive end.
    adx_window: int = 14
    adx_threshold: float = 20.0
    #: Volatility measurement, delegated to `volatility.VolatilityAnalyzer`.
    vol_window: int = 20
    vol_lookback: int = 100
    calendar: Calendar = CONTINUOUS
    #: HIGH_VOLATILITY requires BOTH an extreme percentile against the
    #: instrument's own history AND an absolute ratio to its median — see the
    #: module docstring for why either alone misfires.
    high_vol_percentile: float = 0.85
    high_vol_ratio: float = 1.5
    #: CRISIS is high volatility *plus* a collapsed price. 15% off the rolling
    #: high with volatility at 2.5x its median is a genuine dislocation, not a
    #: pullback.
    crisis_vol_ratio: float = 2.5
    crisis_drawdown: float = 0.15
    #: Rolling window for the drawdown high-water mark.
    drawdown_window: int = 40
    #: Window for return dispersion (recent stdev vs the longer-run stdev).
    #: Reported as evidence for attribution; it is not itself a gate, because
    #: it is largely redundant with the volatility ratio.
    dispersion_window: int = 10
    #: Consecutive bars of contrary evidence required to switch label.
    confirm_bars: int = 3
    #: Absolute floor on history regardless of the windows above.
    min_bars: int = 80
    #: Confidence assigned to a classification that has only just crossed its
    #: threshold; it rises toward 1.0 as evidence strengthens. Never 0, because
    #: 0 is reserved for "no opinion at all" (UNKNOWN).
    base_confidence: float = 0.5

    def __post_init__(self):
        if self.fast_ma < 2 or self.slow_ma <= self.fast_ma:
            raise ConfigurationError(
                "require 2 <= fast_ma < slow_ma",
                fast_ma=self.fast_ma, slow_ma=self.slow_ma)
        for name in ("slope_window", "adx_window", "vol_window", "drawdown_window",
                     "dispersion_window", "confirm_bars", "min_bars"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ConfigurationError(f"{name} must be a positive int",
                                         **{name: value})
        if not 0.0 < self.high_vol_percentile < 1.0:
            raise ConfigurationError("high_vol_percentile must be in (0, 1)",
                                     high_vol_percentile=self.high_vol_percentile)
        if self.high_vol_ratio <= 1.0 or self.crisis_vol_ratio <= self.high_vol_ratio:
            raise ConfigurationError(
                "require 1 < high_vol_ratio < crisis_vol_ratio",
                high_vol_ratio=self.high_vol_ratio,
                crisis_vol_ratio=self.crisis_vol_ratio)
        if not 0.0 < self.crisis_drawdown < 1.0:
            raise ConfigurationError("crisis_drawdown must be in (0, 1)",
                                     crisis_drawdown=self.crisis_drawdown)
        if not 0.0 < self.base_confidence < 1.0:
            raise ConfigurationError("base_confidence must be in (0, 1)",
                                     base_confidence=self.base_confidence)
        if self.slope_sigma <= 0:
            raise ConfigurationError("slope_sigma must be > 0",
                                     slope_sigma=self.slope_sigma)


DEFAULT_CONFIG = RegimeConfig()


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RegimeState:
    """The classifier's output for one bar.

    `raw_regime` is what the evidence said *this* bar, before hysteresis;
    `regime` is the label actually in force. Publishing both is what makes the
    smoothing auditable — otherwise a reviewer cannot tell a stable market from
    a classifier that is suppressing a genuine change.
    """
    ts: datetime
    instrument_key: str
    regime: Regime
    confidence: float
    evidence: dict = field(default_factory=dict)
    raw_regime: Regime = Regime.UNKNOWN
    pending_bars: int = 0

    def __post_init__(self):
        object.__setattr__(self, "ts", ensure_utc(self.ts, field_name="ts"))
        if not isinstance(self.regime, Regime):
            raise ConfigurationError("regime must be a Regime",
                                     got=type(self.regime).__name__)
        if not 0.0 <= self.confidence <= 1.0:
            raise ConfigurationError("confidence must be in [0, 1]",
                                     got=self.confidence)
        # Evidence is an audit record, so pin it to plain floats now rather
        # than discovering a stray Decimal at serialisation time.
        cleaned = {str(k): float(v) for k, v in dict(self.evidence).items()}
        object.__setattr__(self, "evidence", cleaned)

    @property
    def is_known(self) -> bool:
        return self.regime is not Regime.UNKNOWN

    def to_dict(self) -> dict:
        return {k: jsonable(v) for k, v in asdict(self).items()}


class Hysteresis:
    """Requires N consecutive observations before changing the held label.

    Split out from the classifier so the smoothing rule can be tested on its
    own, without synthesising a price series that happens to produce the
    transition under test.

    Warm-up asymmetry: moving *off* UNKNOWN is immediate. UNKNOWN means "no
    opinion", and delaying the first opinion by `confirm_bars` would only
    withhold information, not stabilise anything.
    """

    def __init__(self, confirm_bars: int = 3):
        if not isinstance(confirm_bars, int) or confirm_bars < 1:
            raise ConfigurationError("confirm_bars must be a positive int",
                                     confirm_bars=confirm_bars)
        self.confirm_bars = confirm_bars
        self.current: Regime = Regime.UNKNOWN
        self.pending: Regime | None = None
        self.pending_count: int = 0

    def reset(self) -> None:
        self.current = Regime.UNKNOWN
        self.pending = None
        self.pending_count = 0

    def update(self, raw: Regime) -> Regime:
        """Feed one bar's raw label; return the label now in force."""
        if raw is Regime.UNKNOWN:
            # Evidence vanished (a gap, a truncated history). Hold the last
            # label rather than reverting to UNKNOWN — but stop counting
            # toward any pending switch, since we have no opinion this bar.
            self.pending, self.pending_count = None, 0
            return self.current
        if self.current is Regime.UNKNOWN:
            self.current = raw
            self.pending, self.pending_count = None, 0
            return self.current
        if raw is self.current:
            self.pending, self.pending_count = None, 0
            return self.current
        if raw is self.pending:
            self.pending_count += 1
        else:
            self.pending, self.pending_count = raw, 1
        if self.pending_count >= self.confirm_bars:
            self.current = raw
            self.pending, self.pending_count = None, 0
        return self.current

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (f"Hysteresis(current={self.current.value}, "
                f"pending={self.pending.value if self.pending else None}, "
                f"count={self.pending_count})")


# ---------------------------------------------------------------------------
# indicators used only here (kept local so this module stays self-contained)
# ---------------------------------------------------------------------------

def simple_moving_average(values: Sequence[float], window: int) -> list[float | None]:
    """SMA aligned with the input; None until `window` observations exist."""
    if window < 1:
        raise ConfigurationError("window must be >= 1", window=window)
    out: list[float | None] = [None] * len(values)
    if len(values) < window:
        return out
    running = math.fsum(values[:window])
    out[window - 1] = running / window
    for i in range(window, len(values)):
        # Incremental update; fsum on the whole window each bar would be O(n.w)
        # for no accuracy gain at these magnitudes.
        running += values[i] - values[i - window]
        out[i] = running / window
    return out


def adx(candles: Sequence[Candle], window: int = 14) -> list[float | None]:
    """Wilder's Average Directional Index, aligned with the input bars.

    Measures trend *strength* without direction: +DI and -DI are the smoothed
    up- and down-moves normalised by true range, DX is their normalised
    difference, and ADX is DX smoothed again. Needs roughly `2 * window + 1`
    bars before the first value exists, which is why `RegimeClassifier`
    includes that in its warm-up requirement.
    """
    n = len(candles)
    out: list[float | None] = [None] * n
    if n < 2 * window + 1:
        return out
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]

    tr: list[float] = [0.0]
    plus_dm: list[float] = [0.0]
    minus_dm: list[float] = [0.0]
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        # A bar counts toward at most one direction: an inside bar counts
        # toward neither, an outside bar toward the larger move only.
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                      abs(lows[i] - closes[i - 1])))

    smooth_tr = math.fsum(tr[1:window + 1])
    smooth_plus = math.fsum(plus_dm[1:window + 1])
    smooth_minus = math.fsum(minus_dm[1:window + 1])

    dx_values: list[tuple[int, float]] = []
    for i in range(window, n):
        if i > window:
            # Wilder's recursive smoothing: drop 1/window of the running sum
            # and add the new observation.
            smooth_tr = smooth_tr - smooth_tr / window + tr[i]
            smooth_plus = smooth_plus - smooth_plus / window + plus_dm[i]
            smooth_minus = smooth_minus - smooth_minus / window + minus_dm[i]
        if smooth_tr <= 0:
            dx_values.append((i, 0.0))
            continue
        di_plus = 100.0 * smooth_plus / smooth_tr
        di_minus = 100.0 * smooth_minus / smooth_tr
        total = di_plus + di_minus
        dx_values.append((i, 0.0 if total <= 0
                          else 100.0 * abs(di_plus - di_minus) / total))

    if len(dx_values) < window:
        return out
    running = math.fsum(v for _, v in dx_values[:window]) / window
    out[dx_values[window - 1][0]] = running
    for index, value in dx_values[window:]:
        running = (running * (window - 1) + value) / window
        out[index] = running
    return out


def rolling_drawdown(closes: Sequence[float], window: int) -> float:
    """Fraction below the highest close in the trailing `window` bars, in [0,1).

    Close-based rather than high-based: a wick that nobody could have exited at
    should not define the high-water mark a crisis is measured from.
    """
    recent = list(closes[-window:]) if window < len(closes) else list(closes)
    if not recent:
        return 0.0
    peak = max(recent)
    if peak <= 0:  # pragma: no cover - Candle forbids non-positive prices
        return 0.0
    return max(0.0, (peak - recent[-1]) / peak)


def _ramp(value: float, low: float, high: float) -> float:
    """Linear 0->1 ramp between `low` and `high`, clamped outside."""
    if high <= low:
        return 1.0 if value >= high else 0.0
    return min(1.0, max(0.0, (value - low) / (high - low)))


# ---------------------------------------------------------------------------
# classifier
# ---------------------------------------------------------------------------

class RegimeClassifier:
    """Rule-based regime classifier with hysteresis.

    Stateful by necessity — hysteresis is memory — but the state is only the
    held label and the pending counter, and it advances exactly once per
    *distinct* bar timestamp. Re-classifying the same bar returns the cached
    state instead of double-counting it toward a pending switch, which is what
    makes a live loop that polls twice per bar behave identically to one that
    polls once. Feeding a bar older than the last one seen is rejected: it
    means the caller is replaying history into a live classifier, and silently
    absorbing that would corrupt the counter.
    """

    def __init__(self, clock: Clock | None = None,
                 config: RegimeConfig | None = None):
        self.clock = clock or default_clock()
        self.config = config or DEFAULT_CONFIG
        self._hysteresis = Hysteresis(self.config.confirm_bars)
        self._vol = VolatilityAnalyzer(
            lookback=self.config.vol_lookback,
            clock=self.clock,
            window=self.config.vol_window,
            calendar=self.config.calendar,
        )
        self._last_ts: datetime | None = None
        self._last_state: RegimeState | None = None

    # -- warm-up -----------------------------------------------------------

    @property
    def required_bars(self) -> int:
        """Bars needed before any label but UNKNOWN can be produced.

        Derived from the configured windows rather than fixed, so raising
        `slow_ma` cannot leave the classifier reading a None SMA.
        """
        cfg = self.config
        return max(
            cfg.min_bars,
            cfg.slow_ma + cfg.slope_window,
            2 * cfg.adx_window + 1 + cfg.adx_window,
            cfg.drawdown_window,
            cfg.dispersion_window * 2 + 1,
            self._vol.required_bars,
        )

    # -- public API --------------------------------------------------------

    def reset(self) -> None:
        """Forget the held label. Used between backtest runs."""
        self._hysteresis.reset()
        self._last_ts = None
        self._last_state = None

    def classify(self, candles: Sequence[Candle]) -> RegimeState:
        """Classify the market as of the last candle."""
        if not candles:
            raise InsufficientDataError("classify requires at least one candle",
                                        have=0, need=self.required_bars)
        last = candles[-1]
        ts = ensure_utc(last.ts_close, field_name="ts_close")
        if self._last_ts is not None:
            if ts == self._last_ts and self._last_state is not None:
                return self._last_state
            if ts < self._last_ts:
                raise DataValidationError(
                    "regime classifier received an out-of-order bar; call "
                    "reset() before replaying an earlier history",
                    last_seen=self._last_ts.isoformat(), got=ts.isoformat())

        if len(candles) < self.required_bars:
            state = RegimeState(
                ts=ts, instrument_key=last.instrument_key,
                regime=Regime.UNKNOWN, confidence=0.0,
                evidence={"bars": float(len(candles)),
                          "bars_required": float(self.required_bars)},
                raw_regime=Regime.UNKNOWN, pending_bars=0)
            self._last_ts, self._last_state = ts, state
            return state

        evidence = self._evidence(candles)
        raw = self._raw_regime(evidence)
        held = self._hysteresis.update(raw)
        pending = self._hysteresis.pending_count
        confidence = self._confidence(held, evidence)
        if pending:
            # Contrary evidence is accumulating: decay confidence in the label
            # still in force, linearly with how close the switch is. The label
            # does not flap; the conviction does.
            confidence *= max(0.0, 1.0 - pending / self.config.confirm_bars)
        state = RegimeState(
            ts=ts, instrument_key=last.instrument_key, regime=held,
            confidence=round(min(1.0, max(0.0, confidence)), 6),
            evidence=evidence, raw_regime=raw, pending_bars=pending)
        self._last_ts, self._last_state = ts, state
        return state

    def history(self, candles: Sequence[Candle]) -> list[RegimeState]:
        """Walk the whole series bar by bar, one state per candle.

        Uses a private classifier seeded from this one's config, so calling it
        never disturbs the live hysteresis state, and the result is exactly
        what a live classifier would have produced had it seen the same bars in
        order — which is the property per-regime attribution in the backtester
        depends on.
        """
        walker = RegimeClassifier(clock=self.clock, config=self.config)
        return [walker.classify(candles[:i + 1]) for i in range(len(candles))]

    # -- evidence ----------------------------------------------------------

    def _evidence(self, candles: Sequence[Candle]) -> dict:
        cfg = self.config
        closes = [c.close for c in candles]
        timeframe = candles[-1].timeframe
        factor = annualisation_factor(timeframe, cfg.calendar)

        vol_state = self._vol.analyse(candles)
        # Back out per-bar sigma from the annualised number so the slope
        # threshold can be expressed in sigma units.
        per_bar_sigma = vol_state.realised / factor if factor > 0 else 0.0

        # Volatility relative to its own trailing median. Recomputed here (not
        # taken from VolatilityState) because the state carries the percentile,
        # not the distribution it was ranked against.
        window_slice = closes[-(cfg.vol_lookback + cfg.vol_window + 1):]
        series = [v for v in realised_volatility(window_slice, cfg.vol_window, factor)
                  if v is not None]
        median_vol = statistics.median(series[:-1]) if len(series) > 1 else 0.0
        vol_ratio = (vol_state.realised / median_vol) if median_vol > 0 else 1.0

        fast = simple_moving_average(closes, cfg.fast_ma)
        slow = simple_moving_average(closes, cfg.slow_ma)
        fast_now = fast[-1]
        fast_then = fast[-1 - cfg.slope_window]
        slow_now = slow[-1]
        if fast_now is None or fast_then is None or slow_now is None:
            raise InsufficientDataError(  # pragma: no cover - required_bars guards
                "moving averages unavailable", have=len(candles),
                need=self.required_bars)
        slope_per_bar = (fast_now - fast_then) / (fast_then * cfg.slope_window)
        slope_sigma = slope_per_bar / per_bar_sigma if per_bar_sigma > 0 else 0.0

        adx_series = adx(candles, cfg.adx_window)
        adx_now = adx_series[-1]

        drawdown = rolling_drawdown(closes, cfg.drawdown_window)

        rets = log_returns(closes)
        short = rets[-cfg.dispersion_window:]
        long = rets[-(cfg.dispersion_window * 4):]
        dispersion = 1.0
        if len(short) >= 2 and len(long) >= 2:
            long_sd = statistics.stdev(long)
            if long_sd > 0:
                dispersion = statistics.stdev(short) / long_sd

        return {
            "slope_sigma": slope_sigma,
            "slope_per_bar": slope_per_bar,
            "ma_separation": (fast_now - slow_now) / slow_now,
            "adx": float(adx_now) if adx_now is not None else -1.0,
            "vol_percentile": vol_state.percentile_rank,
            "vol_ratio": vol_ratio,
            "realised_vol": vol_state.realised,
            "atr_pct": vol_state.atr_pct,
            "drawdown": drawdown,
            "dispersion": dispersion,
        }

    # -- rules -------------------------------------------------------------

    def _raw_regime(self, ev: dict) -> Regime:
        """Ordered rules; the first match wins.

        Order encodes precedence: a crisis is still a crisis even if the trend
        filter would also fire (it usually does — a crash trends down), and a
        volatility explosion outranks a trend label because it changes how the
        position should be sized, not merely which way it points.
        """
        cfg = self.config
        if (ev["vol_ratio"] >= cfg.crisis_vol_ratio
                and ev["drawdown"] >= cfg.crisis_drawdown):
            return Regime.CRISIS
        if (ev["vol_percentile"] >= cfg.high_vol_percentile
                and ev["vol_ratio"] >= cfg.high_vol_ratio):
            return Regime.HIGH_VOLATILITY
        # ADX of -1.0 is the "not yet computable" sentinel; treat it as
        # non-blocking rather than as a failed directional test.
        directional = ev["adx"] < 0 or ev["adx"] >= cfg.adx_threshold
        if directional and ev["slope_sigma"] >= cfg.slope_sigma and ev["ma_separation"] > 0:
            return Regime.TRENDING_UP
        if directional and ev["slope_sigma"] <= -cfg.slope_sigma and ev["ma_separation"] < 0:
            return Regime.TRENDING_DOWN
        return Regime.RANGING

    def _confidence(self, regime: Regime, ev: dict) -> float:
        """Confidence for a label given the evidence, in (0, 1].

        Starts at `base_confidence` the instant a threshold is crossed and
        ramps toward 1.0 as the evidence goes further past it, so a caller can
        distinguish "just barely trending" from "unmistakably trending" without
        re-deriving the thresholds.
        """
        cfg = self.config
        base = cfg.base_confidence
        span = 1.0 - base
        if regime is Regime.UNKNOWN:
            return 0.0
        if regime is Regime.CRISIS:
            strength = 0.5 * _ramp(ev["vol_ratio"], cfg.crisis_vol_ratio,
                                   cfg.crisis_vol_ratio * 2.0)
            strength += 0.5 * _ramp(ev["drawdown"], cfg.crisis_drawdown,
                                    min(0.99, cfg.crisis_drawdown * 2.0))
            return base + span * strength
        if regime is Regime.HIGH_VOLATILITY:
            strength = 0.5 * _ramp(ev["vol_percentile"], cfg.high_vol_percentile, 1.0)
            strength += 0.5 * _ramp(ev["vol_ratio"], cfg.high_vol_ratio,
                                    cfg.high_vol_ratio * 2.0)
            return base + span * strength
        if regime in (Regime.TRENDING_UP, Regime.TRENDING_DOWN):
            slope = abs(ev["slope_sigma"])
            strength = 0.5 * _ramp(slope, cfg.slope_sigma, cfg.slope_sigma * 4.0)
            adx_now = ev["adx"]
            strength += 0.5 * (0.5 if adx_now < 0
                               else _ramp(adx_now, cfg.adx_threshold, 50.0))
            return base + span * strength
        # RANGING: confident exactly to the degree that nothing else fired.
        quiet = 1.0 - _ramp(abs(ev["slope_sigma"]), 0.0, cfg.slope_sigma)
        calm = 1.0 - _ramp(ev["vol_percentile"], 0.0, cfg.high_vol_percentile)
        return base + span * (0.6 * quiet + 0.4 * calm)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (f"RegimeClassifier(regime={self._hysteresis.current.value}, "
                f"confirm_bars={self.config.confirm_bars})")


__all__ = [
    "RegimeConfig", "DEFAULT_CONFIG", "RegimeState", "Hysteresis",
    "RegimeClassifier", "simple_moving_average", "adx", "rolling_drawdown",
]

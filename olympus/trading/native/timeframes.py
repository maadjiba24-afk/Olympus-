"""Multi-timeframe intelligence, and the bar states that make it safe.

The whole capability rests on one distinction the naive implementation does not
make: **a bar is closed, partial, or delayed, and those are three different
things.**

* **Closed** — its interval has ended and the venue has published it. Safe.
* **Partial** — its interval has not ended. Its high, low and close are still
  moving. Reading one is the single most common multi-timeframe leak, and it is
  attractive precisely because the number is *there*.
* **Delayed** — its interval ended but nothing has arrived. Not the same as
  partial: a partial bar has fresh data that is incomplete, a delayed one has
  no data at all, and a model that treats them alike will read a stale close as
  a current one.

`BarState` is carried on every observation, `TimeframeView.usable` is false for
anything but `CLOSED`, and `assert_no_partial_leak` is the independent check.
Three mechanisms because the failure is silent and the numbers look fine.

The ladder
----------
Eight scales from tick to weekly. They are *declared*, not assumed present:
`TimeframeLadder.available` reports which have data and `missing` reports which
do not, so a model configured for four scales and given two says so rather than
quietly modelling two.

Why alignment is by close and never by containment
--------------------------------------------------
Pairing a 09:15 hourly bar with "the daily bar it is inside" uses a bar that
has not closed. `dataset.align_timeframes` already implements the correct rule
— the newest context bar whose `ts_close <= base.ts_close` — and this module
consumes it rather than reimplementing it. A second implementation of that rule
is a second chance to get it wrong.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Mapping, Sequence

from ..contracts import Candle, ensure_utc, jsonable
from ..errors import ConfigurationError, DataValidationError
from ..instruments import parse_timeframe
from .dataset import Aligned, align_timeframes

#: The declared ladder, coarsest last. Tick is first and is the one scale that
#: is not a bar at all — it is declared so a caller can ask for it and be told
#: it is unavailable, rather than discovering the ladder simply stops at 1m.
LADDER: tuple[str, ...] = ("tick", "1m", "5m", "15m", "1h", "4h", "1d", "1w")

#: Multiples of a timeframe's own interval a bar may lag before it is DELAYED
#: rather than merely late. One full interval: a bar that has not arrived by the
#: time its successor's interval has elapsed is not coming soon.
DEFAULT_DELAY_TOLERANCE = 1.0


class BarState(str, Enum):
    """What is true about a bar right now. Carried on every observation."""

    #: Interval ended, data published. The only state a model may consume.
    CLOSED = "closed"
    #: Interval has not ended. High, low and close are still moving.
    PARTIAL = "partial"
    #: Interval ended and nothing has arrived. Distinct from PARTIAL: there is
    #: no fresh data at all, so the last close is stale rather than incomplete.
    DELAYED = "delayed"
    #: The scale is declared and has no data whatsoever.
    ABSENT = "absent"


def timeframe_seconds(timeframe: str) -> float:
    """Interval length. `tick` has none and raises rather than returning zero."""
    if timeframe == "tick":
        raise ConfigurationError(
            "tick data has no interval; a tick ladder rung is a stream, not a "
            "bar, and asking for its length is a category error")
    return parse_timeframe(timeframe).total_seconds()


def classify_bar(bar: Candle | None, *, as_of: datetime,
                 timeframe: str = "",
                 delay_tolerance: float = DEFAULT_DELAY_TOLERANCE) -> BarState:
    """Closed, partial, delayed or absent — from the bar and the clock alone.

    `is_final` is consulted *and* the timestamps are checked. Two mechanisms
    because a provider that sets `is_final` optimistically is a provider whose
    flag cannot be the only defence, and because a bar can be genuinely final
    and still be hours old.
    """
    moment = ensure_utc(as_of, field_name="as_of")
    if bar is None:
        return BarState.ABSENT
    if bar.ts_close > moment or not bar.is_final:
        return BarState.PARTIAL
    scale = timeframe or bar.timeframe
    try:
        interval = timeframe_seconds(scale)
    except ConfigurationError:
        return BarState.CLOSED
    lag = (moment - bar.ts_close).total_seconds()
    if lag > interval * (1.0 + delay_tolerance):
        return BarState.DELAYED
    return BarState.CLOSED


@dataclass(frozen=True)
class TimeframeObservation:
    """One scale's contribution, and whether it may be used."""

    timeframe: str
    state: BarState
    bar: Candle | None = None
    #: Seconds between the bar's close and `as_of`. `None` when absent.
    staleness_seconds: float | None = None
    #: How many of its own intervals stale. The scale-free version.
    staleness_bars: float | None = None
    n_bars: int = 0

    def __post_init__(self):
        object.__setattr__(self, "state", BarState(self.state))
        if self.state is BarState.ABSENT and self.bar is not None:
            raise ConfigurationError(
                "an absent scale cannot carry a bar", timeframe=self.timeframe)

    @property
    def usable(self) -> bool:
        """Only a closed bar. This is the property the model reads."""
        return self.state is BarState.CLOSED

    @property
    def last_close(self) -> float | None:
        """`None` unless the bar is closed.

        Deliberately gated on the state rather than on the bar's existence: a
        partial bar *has* a close attribute and returning it is the leak.
        """
        return self.bar.close if (self.usable and self.bar) else None

    def to_dict(self) -> dict:
        return {"timeframe": self.timeframe, "state": self.state.value,
                "usable": self.usable, "n_bars": self.n_bars,
                "staleness_seconds": self.staleness_seconds,
                "staleness_bars": self.staleness_bars,
                "ts_close": jsonable(self.bar.ts_close) if self.bar else None,
                "last_close": self.last_close}


@dataclass(frozen=True)
class TimeframeView:
    """Every declared scale at one instant, with each one's state."""

    instrument_key: str
    as_of: datetime
    base_timeframe: str
    observations: Mapping[str, TimeframeObservation]

    def __post_init__(self):
        object.__setattr__(self, "as_of",
                           ensure_utc(self.as_of, field_name="as_of"))
        object.__setattr__(self, "observations", dict(self.observations))
        if self.base_timeframe not in self.observations:
            raise ConfigurationError("the base timeframe is not observed",
                                     base_timeframe=self.base_timeframe,
                                     observed=sorted(self.observations))
        base = self.observations[self.base_timeframe]
        if not base.usable:
            raise DataValidationError(
                "the base timeframe's bar is not closed, so there is nothing "
                "to forecast from",
                base_timeframe=self.base_timeframe, state=base.state.value)

    @property
    def usable_timeframes(self) -> tuple[str, ...]:
        return tuple(tf for tf, o in self.observations.items() if o.usable)

    @property
    def unusable(self) -> Mapping[str, str]:
        """Scale → why it may not be used. What the explainer reports."""
        return {tf: o.state.value for tf, o in self.observations.items()
                if not o.usable}

    def observation(self, timeframe: str) -> TimeframeObservation | None:
        return self.observations.get(timeframe)

    def close(self, timeframe: str) -> float | None:
        """A scale's last close, or `None` when it may not be used."""
        observation = self.observations.get(timeframe)
        return observation.last_close if observation else None

    @property
    def coverage(self) -> float:
        return (len(self.usable_timeframes) / len(self.observations)
                if self.observations else 0.0)

    def to_dict(self) -> dict:
        return {"instrument": self.instrument_key, "as_of": jsonable(self.as_of),
                "base_timeframe": self.base_timeframe,
                "observations": {tf: o.to_dict()
                                 for tf, o in sorted(self.observations.items())},
                "usable": list(self.usable_timeframes),
                "unusable": dict(self.unusable),
                "coverage": round(self.coverage, 4)}


class TimeframeLadder:
    """The declared scales, and which of them actually have data.

    Declared rather than inferred. A ladder built from whatever series happened
    to be passed would silently narrow when a feed dropped; this one reports
    `missing` and the model sees a mask rather than a shorter input.
    """

    __slots__ = ("timeframes", "base", "delay_tolerance")

    def __init__(self, timeframes: Sequence[str] = LADDER, *,
                 base: str = "1h",
                 delay_tolerance: float = DEFAULT_DELAY_TOLERANCE):
        declared = tuple(timeframes)
        if not declared:
            raise ConfigurationError("a ladder needs at least one timeframe")
        unknown = [tf for tf in declared if tf not in LADDER]
        if unknown:
            raise ConfigurationError(
                "a ladder rung is not on the declared ladder; adding one is an "
                "edit to LADDER, not a runtime decision",
                unknown=unknown, ladder=list(LADDER))
        if base not in declared:
            raise ConfigurationError("the base timeframe is not on the ladder",
                                     base=base, ladder=list(declared))
        if base == "tick":
            raise ConfigurationError(
                "tick cannot be the base: a forecast horizon is measured in "
                "bars and a tick stream has none")
        if delay_tolerance < 0:
            raise ConfigurationError("delay_tolerance must be >= 0")
        # Coarsest last, by the declared ladder order rather than by parsing —
        # so the order is a property of LADDER and not of a sort key.
        self.timeframes = tuple(sorted(declared, key=LADDER.index))
        self.base = base
        self.delay_tolerance = float(delay_tolerance)

    @property
    def context(self) -> tuple[str, ...]:
        """Scales other than the base."""
        return tuple(tf for tf in self.timeframes if tf != self.base)

    @property
    def coarser_than_base(self) -> tuple[str, ...]:
        return tuple(tf for tf in self.timeframes
                     if LADDER.index(tf) > LADDER.index(self.base))

    def observe(self, series: Mapping[str, Sequence[Candle]], *,
                as_of: datetime, instrument_key: str = "") -> TimeframeView:
        """Build the view. Every declared scale gets an observation.

        A scale with no data gets `ABSENT` rather than being omitted, so a
        consumer iterating the view sees the same keys every time and a dropped
        feed changes a state rather than a shape.
        """
        moment = ensure_utc(as_of, field_name="as_of")
        observations: dict[str, TimeframeObservation] = {}
        key = instrument_key

        for timeframe in self.timeframes:
            bars = [b for b in series.get(timeframe) or ()]
            bars.sort(key=lambda b: b.ts_close)
            latest = bars[-1] if bars else None
            if latest is not None:
                key = key or latest.instrument_key
            state = classify_bar(latest, as_of=moment, timeframe=timeframe,
                                 delay_tolerance=self.delay_tolerance)
            lag = bars_late = None
            if latest is not None:
                lag = (moment - latest.ts_close).total_seconds()
                if timeframe != "tick":
                    bars_late = lag / timeframe_seconds(timeframe)
            observations[timeframe] = TimeframeObservation(
                timeframe=timeframe,
                state=state,
                bar=latest if state is not BarState.ABSENT else None,
                staleness_seconds=lag, staleness_bars=bars_late,
                n_bars=len(bars))

        return TimeframeView(instrument_key=key or "unknown", as_of=moment,
                             base_timeframe=self.base,
                             observations=observations)

    def align(self, base_bars: Sequence[Candle],
              context: Mapping[str, Sequence[Candle]], *,
              max_staleness: timedelta | None = None) -> list[Aligned]:
        """Causal alignment, delegated to `dataset.align_timeframes`.

        Delegated on purpose. A second implementation of "the newest context
        bar that had already closed" is a second chance to get it wrong, and
        that rule is the one this whole module exists to protect.
        """
        wanted = {tf: bars for tf, bars in context.items()
                  if tf in self.timeframes and tf != self.base and tf != "tick"}
        return align_timeframes(base_bars, wanted, max_staleness=max_staleness)

    def to_dict(self) -> dict:
        return {"timeframes": list(self.timeframes), "base": self.base,
                "context": list(self.context),
                "delay_tolerance": self.delay_tolerance}


def assert_no_partial_leak(view: TimeframeView) -> None:
    """Independent check that nothing unusable is being read.

    Independent of `TimeframeView.close`, which already gates on state — this
    re-derives the states from the bars and the clock, so it still works when
    the gating is the buggy part.
    """
    offenders = []
    for timeframe, observation in view.observations.items():
        if observation.bar is None:
            continue
        recomputed = classify_bar(observation.bar, as_of=view.as_of,
                                  timeframe=timeframe)
        if recomputed is not observation.state:
            offenders.append(
                f"{timeframe}: recorded {observation.state.value}, "
                f"recomputes as {recomputed.value}")
        if observation.usable and observation.bar.ts_close > view.as_of:
            offenders.append(
                f"{timeframe}: marked usable and closes after as_of")
    if offenders:
        raise DataValidationError(
            "a timeframe observation disagrees with the bars it was built from",
            offenders=offenders)


@dataclass(frozen=True)
class TimeframeFeatures:
    """Scale-free features from the usable scales only.

    Every value is `None` when its scale is unusable — never a carried-forward
    number from a coarser scale, and never zero. A trend feature that silently
    fell back to the hourly scale when the daily was delayed would report a
    daily trend that was never computed.
    """

    values: Mapping[str, float | None]
    usable: tuple[str, ...]
    unusable: Mapping[str, str]

    def __post_init__(self):
        object.__setattr__(self, "values", dict(self.values))
        object.__setattr__(self, "usable", tuple(self.usable))
        object.__setattr__(self, "unusable", dict(self.unusable))

    @property
    def mask(self) -> Mapping[str, bool]:
        return {k: v is not None for k, v in self.values.items()}

    @property
    def completeness(self) -> float:
        if not self.values:
            return 0.0
        return sum(1 for v in self.values.values() if v is not None) / len(self.values)

    def to_dict(self) -> dict:
        return {"values": dict(self.values), "mask": dict(self.mask),
                "usable": list(self.usable), "unusable": dict(self.unusable),
                "completeness": round(self.completeness, 4)}


def timeframe_features(view: TimeframeView,
                       series: Mapping[str, Sequence[Candle]], *,
                       window: int = 20) -> TimeframeFeatures:
    """Per-scale trend and dispersion, from closed bars only.

    The bars are re-filtered against `as_of` here rather than trusted from the
    caller: the series a caller holds may contain bars after the instant being
    modelled, and slicing them is this function's job rather than its
    precondition.
    """
    values: dict[str, float | None] = {}
    for timeframe in view.observations:
        observation = view.observations[timeframe]
        prefix = f"{timeframe}_"
        if not observation.usable or timeframe == "tick":
            values[prefix + "log_return"] = None
            values[prefix + "trend"] = None
            values[prefix + "dispersion"] = None
            continue
        closed = [b for b in series.get(timeframe) or ()
                  if b.is_final and b.ts_close <= view.as_of]
        closed.sort(key=lambda b: b.ts_close)
        closes = [b.close for b in closed][-window:]
        if len(closes) < 2:
            values[prefix + "log_return"] = None
            values[prefix + "trend"] = None
            values[prefix + "dispersion"] = None
            continue
        steps = [math.log(closes[i] / closes[i - 1])
                 for i in range(1, len(closes))]
        values[prefix + "log_return"] = steps[-1]
        values[prefix + "trend"] = math.log(closes[-1] / closes[0])
        values[prefix + "dispersion"] = (statistics.pstdev(steps)
                                         if len(steps) > 1 else None)
    return TimeframeFeatures(values=values, usable=view.usable_timeframes,
                             unusable=view.unusable)


__all__ = ["LADDER", "DEFAULT_DELAY_TOLERANCE", "BarState", "timeframe_seconds",
           "classify_bar", "TimeframeObservation", "TimeframeView",
           "TimeframeLadder", "assert_no_partial_leak", "TimeframeFeatures",
           "timeframe_features"]

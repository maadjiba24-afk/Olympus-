"""Tick → candle construction and timeframe resampling.

Live market data arrives as ticks; every layer above `validate.py` consumes
bars. This module is the only place that crosses that boundary, and it is built
around one rule: **a bar is emitted exactly once, when its bucket has provably
ended, and it is never touched again.**

Three consequences, each of which is a real production failure mode:

*Closed bars are immutable.* A tick that belongs to an already-closed bucket
(late feed, replayed message, a venue's out-of-order sequence) is counted in
`late_ticks` and discarded. Reopening a closed bar would mutate a value that
downstream code has already traded on — the strategy's entry price and the
audit record would disagree about what the market did.

*Empty buckets stay empty.* Crossing four idle minutes emits one candle, not
four. Synthesising flat "phantom" bars for buckets with no trades is the same
sin as gap-filling in `validate.py`: it invents prices, and it makes an illiquid
instrument look continuously tradable. `validate.py` counts the resulting hole
as a gap, which is the honest representation.

*The in-progress bar is marked.* `current()` returns `is_final=False`, and
`Candle`'s contract plus `validate.normalise_candles` mean such a bar can never
be mistaken for a settled one. Reading the forming bar is legitimate (a monitor
displaying live price); trading on it as if it had closed is not.

`resample` follows the same discipline: a target bucket is emitted only when
every source bar it needs is present, so a partial trailing bucket is withheld
rather than published as a short bar — publishing it is how a 5m strategy ends
up acting on 1 minute of data that it believes is 5.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

from .clock import Clock, default_clock
from .contracts import Candle, ensure_utc
from .errors import ConfigurationError, DataValidationError
# One bucketing rule for the whole package (see `instruments.floor_to_timeframe`)
# so the builder, the validator and the resampler cannot drift into three
# different opinions about where a bar starts.
from .instruments import floor_to_timeframe, parse_timeframe


@dataclass(frozen=True)
class Tick:
    """A single trade print.

    Deliberately minimal: price, size, time, instrument. Quote-driven state
    belongs in `Quote`; mixing the two produces bars whose "volume" is really a
    quote count.
    """
    instrument_key: str
    ts: datetime
    price: float
    size: float = 0.0

    def __post_init__(self):
        object.__setattr__(self, "ts", ensure_utc(self.ts, field_name="ts"))
        if not self.instrument_key or not str(self.instrument_key).strip():
            raise DataValidationError("tick instrument_key must be non-empty")
        for name in ("price", "size"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise DataValidationError(f"tick {name} must be numeric",
                                          field=name, got=type(value).__name__)
            value = float(value)
            if not math.isfinite(value):
                raise DataValidationError(f"tick {name} must be finite", field=name)
            object.__setattr__(self, name, value)
        if self.price <= 0:
            raise DataValidationError("tick price must be > 0",
                                      instrument=self.instrument_key, price=self.price)
        if self.size < 0:
            raise DataValidationError("tick size must be >= 0",
                                      instrument=self.instrument_key, size=self.size)

    @property
    def notional(self) -> float:
        return self.price * self.size


class CandleBuilder:
    """Aggregates one instrument's ticks into bars of one timeframe.

    Single-threaded by design. Feeding one builder from two threads would
    interleave `on_tick` updates and produce a bar whose OHLC never occurred;
    if a process has several feed threads, give each instrument its own builder
    and merge downstream (that is what `CandleAggregator` is for).
    """

    def __init__(self, instrument_key: str, timeframe: str,
                 clock: Clock | None = None, *, source: str = "ticks"):
        if not instrument_key or not str(instrument_key).strip():
            raise ConfigurationError("CandleBuilder needs an instrument_key")
        self.instrument_key = instrument_key
        self.timeframe = timeframe
        self.delta = parse_timeframe(timeframe)
        self.clock = clock or default_clock()
        self.source = source
        #: Ticks that arrived after their bucket had already been published.
        #: Exposed rather than logged: a rising count is a feed problem the
        #: operator must see, and it is the only trace of data we discarded.
        self.late_ticks = 0
        self.ticks_seen = 0
        self._bucket_start: datetime | None = None
        self._open = self._high = self._low = self._close = 0.0
        self._volume = 0.0
        self._amount = 0.0
        self._count = 0

    # -- internals ---------------------------------------------------------

    def _start_bucket(self, bucket: datetime, tick: Tick) -> None:
        self._bucket_start = bucket
        self._open = self._high = self._low = self._close = tick.price
        self._volume = tick.size
        self._amount = tick.notional
        self._count = 1

    def _build(self, is_final: bool) -> Candle:
        assert self._bucket_start is not None
        return Candle(
            instrument_key=self.instrument_key, timeframe=self.timeframe,
            ts_open=self._bucket_start, ts_close=self._bucket_start + self.delta,
            open=self._open, high=self._high, low=self._low, close=self._close,
            volume=self._volume, amount=self._amount, is_final=is_final,
            source=self.source)

    def _close_bucket(self) -> Candle | None:
        if self._bucket_start is None:
            return None
        candle = self._build(True)
        self._bucket_start = None
        self._count = 0
        return candle

    # -- public API --------------------------------------------------------

    def on_tick(self, tick: Tick) -> Candle | None:
        """Absorb a tick; return a FINALISED candle if it closed the previous bucket.

        Emission is edge-triggered by the first tick of the *next* bucket rather
        than by wall time, because that is the only moment the builder can know
        the previous bucket is over from the data alone. A quiet instrument's
        last bar therefore stays pending until either a later tick or an
        explicit `flush()` — which is correct: the alternative is publishing a
        bar before its close, which is look-ahead.
        """
        if not isinstance(tick, Tick):
            raise DataValidationError("on_tick expects a Tick",
                                      got=type(tick).__name__)
        if tick.instrument_key != self.instrument_key:
            raise DataValidationError(
                "tick routed to the wrong builder", expected=self.instrument_key,
                got=tick.instrument_key)
        self.ticks_seen += 1
        bucket = floor_to_timeframe(tick.ts, self.timeframe)

        if self._bucket_start is None:
            self._start_bucket(bucket, tick)
            return None
        if bucket == self._bucket_start:
            self._close = tick.price
            self._high = max(self._high, tick.price)
            self._low = min(self._low, tick.price)
            self._volume += tick.size
            self._amount += tick.notional
            self._count += 1
            return None
        if bucket < self._bucket_start:
            # Older than the bar currently forming: the bucket it belongs to has
            # already been published (or was skipped). Never merge it in.
            self.late_ticks += 1
            return None
        # A strictly later bucket: the pending bar is now provably complete.
        # Intervening empty buckets produce nothing at all — no phantom bars.
        finished = self._close_bucket()
        self._start_bucket(bucket, tick)
        return finished

    def on_ticks(self, ticks: Iterable[Tick]) -> list[Candle]:
        out: list[Candle] = []
        for tick in ticks:
            candle = self.on_tick(tick)
            if candle is not None:
                out.append(candle)
        return out

    def current(self) -> Candle | None:
        """The bar being formed, always `is_final=False`. None if idle."""
        if self._bucket_start is None:
            return None
        return self._build(False)

    def flush(self, now: datetime | None = None, *, force: bool = False) -> Candle | None:
        """Publish the pending bar if its bucket has ended by `now`.

        Default `force=False` keeps the invariant that a final bar is only ever
        emitted after its close time; `force=True` exists for end-of-stream in
        backtests and shutdown, where the caller knows no further ticks exist
        and accepts a possibly short final bar.
        """
        if self._bucket_start is None:
            return None
        if not force:
            when = ensure_utc(now if now is not None else self.clock.now(),
                              field_name="now")
            if when < self._bucket_start + self.delta:
                return None
        return self._close_bucket()

    @property
    def pending(self) -> bool:
        return self._bucket_start is not None

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (f"CandleBuilder({self.instrument_key}, {self.timeframe}, "
                f"pending={self.pending}, late_ticks={self.late_ticks})")


class CandleAggregator:
    """One builder per instrument, driven by a single mixed tick stream.

    Returns lists (never a bare candle) so callers write one loop that works
    whether a tick closed zero or one bar, and so `flush` — which can close many
    instruments at once — has the same shape.
    """

    def __init__(self, timeframe: str, clock: Clock | None = None, *,
                 source: str = "ticks"):
        self.timeframe = timeframe
        self.clock = clock or default_clock()
        self.source = source
        self._builders: dict[str, CandleBuilder] = {}

    def builder(self, instrument_key: str) -> CandleBuilder:
        builder = self._builders.get(instrument_key)
        if builder is None:
            builder = CandleBuilder(instrument_key, self.timeframe, self.clock,
                                    source=self.source)
            self._builders[instrument_key] = builder
        return builder

    def on_tick(self, tick: Tick) -> list[Candle]:
        candle = self.builder(tick.instrument_key).on_tick(tick)
        return [] if candle is None else [candle]

    def on_ticks(self, ticks: Iterable[Tick]) -> list[Candle]:
        out: list[Candle] = []
        for tick in ticks:
            out.extend(self.on_tick(tick))
        return out

    def current(self, instrument_key: str) -> Candle | None:
        builder = self._builders.get(instrument_key)
        return builder.current() if builder else None

    def flush(self, now: datetime | None = None, *, force: bool = False) -> list[Candle]:
        """Close every instrument whose bucket has ended, in a stable order.

        Sorted by `(ts_open, instrument_key)` so a replay of the same ticks
        produces byte-identical output — determinism here is what makes a
        backtest reproducible across dict-ordering changes.
        """
        out = [candle for builder in self._builders.values()
               if (candle := builder.flush(now, force=force)) is not None]
        out.sort(key=lambda c: (c.ts_open, c.instrument_key))
        return out

    @property
    def late_ticks(self) -> int:
        return sum(b.late_ticks for b in self._builders.values())

    @property
    def instruments(self) -> list[str]:
        return sorted(self._builders)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (f"CandleAggregator({self.timeframe}, "
                f"instruments={len(self._builders)})")


def resample(candles: Sequence[Candle], from_tf: str, to_tf: str, *,
             allow_partial: bool = False, source: str | None = None) -> list[Candle]:
    """Aggregate finer bars into coarser ones (`1m` → `5m`).

    Only whole target buckets are emitted. A bucket missing any of its source
    bars — the trailing one still forming, or one lost to a feed gap — is
    withheld rather than aggregated from what happens to be there: a "5m" bar
    built from two 1m bars is not a 5m bar, and nothing downstream could tell
    the difference. `allow_partial=True` opts into the loose behaviour for
    research/plotting, and is never used on a trading path.
    """
    from_delta = parse_timeframe(from_tf)
    to_delta = parse_timeframe(to_tf)
    if to_delta <= from_delta:
        raise ConfigurationError("resample target must be coarser than the source",
                                 from_tf=str(from_tf), to_tf=str(to_tf))
    ratio_seconds = to_delta.total_seconds() / from_delta.total_seconds()
    if abs(ratio_seconds - round(ratio_seconds)) > 1e-9:
        raise ConfigurationError(
            "resample target must be an exact multiple of the source timeframe",
            from_tf=str(from_tf), to_tf=str(to_tf))
    ratio = int(round(ratio_seconds))

    ordered = sorted(candles, key=lambda c: c.ts_open)
    buckets: dict[datetime, list[Candle]] = {}
    for index, candle in enumerate(ordered):
        if candle.timeframe != from_tf:
            raise DataValidationError("resample input has the wrong timeframe",
                                      expected=str(from_tf), got=candle.timeframe)
        if not candle.is_final:
            # An unfinished source bar would silently make the aggregate's close
            # a price that has not settled.
            raise DataValidationError(
                "resample requires final candles; validate first",
                instrument=candle.instrument_key,
                ts_open=candle.ts_open.isoformat())
        if index and candle.ts_open == ordered[index - 1].ts_open:
            raise DataValidationError(
                "resample input contains duplicate bars; deduplicate first",
                ts_open=candle.ts_open.isoformat())
        if index and candle.instrument_key != ordered[0].instrument_key:
            raise DataValidationError(
                "resample input mixes instruments",
                expected=ordered[0].instrument_key, got=candle.instrument_key)
        buckets.setdefault(floor_to_timeframe(candle.ts_open, to_tf), []).append(candle)

    out: list[Candle] = []
    for bucket_start in sorted(buckets):
        members = buckets[bucket_start]
        if len(members) != ratio and not allow_partial:
            continue
        first, last = members[0], members[-1]
        out.append(Candle(
            instrument_key=first.instrument_key, timeframe=to_tf,
            ts_open=bucket_start, ts_close=bucket_start + to_delta,
            open=first.open, high=max(c.high for c in members),
            low=min(c.low for c in members), close=last.close,
            volume=sum(c.volume for c in members),
            amount=sum(c.amount for c in members), is_final=True,
            source=source if source is not None else first.source))
    return out


def candles_from_ticks(ticks: Iterable[Tick], timeframe: str, *,
                       clock: Clock | None = None, source: str = "ticks",
                       force_last: bool = False) -> list[Candle]:
    """Convenience batch path: an entire tick history to bars, in order.

    `force_last` decides the fate of the final, possibly incomplete bucket. It
    defaults to False for the same reason `flush` does — a short bar published
    as final is indistinguishable from a real one.
    """
    aggregator = CandleAggregator(timeframe, clock, source=source)
    out = aggregator.on_ticks(ticks)
    out.extend(aggregator.flush(force=force_last))
    out.sort(key=lambda c: (c.ts_open, c.instrument_key))
    return out


__all__ = ["CandleAggregator", "CandleBuilder", "Tick", "candles_from_ticks",
           "resample"]

"""Datasets, provenance, alignment and the leakage checks that make them usable.

`data.py` is the minimal safe path: window, split, embargo. This module is what
a dataset needs *around* that before anyone should train on it — where the bars
came from, which instruments were in the universe at the time, what is missing,
what is duplicated, and a manifest that lets someone else rebuild the identical
thing.

The five failures this module exists to prevent
-----------------------------------------------
**Survivorship.** Training on today's index members and backtesting through
2018 tests a strategy on the companies that survived. `Universe` records
membership as intervals and `members_at` answers the question at a date, so a
dataset built from a universe cannot quietly use tomorrow's membership.

**Corporate actions applied from the future.** A back-adjusted price series is
correct in hindsight and a look-ahead in real time: applying a split ratio
before its ex-date restates prices nobody had. `adjust_for_actions` takes an
`as_of` and applies only what had happened by then.

**Misaligned timeframes.** The usual multi-timeframe bug: pairing a 1h bar with
the daily bar that *contains* it. That daily bar has not closed, so its high,
low and close are not yet knowable. `align_timeframes` pairs each base bar with
the most recent context bar that closed at or *before* it, and reports how stale
that is.

**Gaps read as flat markets.** A missing bar is not a bar with no movement.
`detect_gaps` finds them, `QualityReport` counts them, and nothing here fills
them — the mask says absent and the model is told.

**Statistics fitted across the split.** `ScalerPolicy` carries the split
boundary with it and refuses to fit on anything that crosses it. That is a
mechanism rather than a convention, because the convention is what fails.

Pure stdlib. A dataset is data about data about data.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Mapping, Sequence

from ..contracts import Candle, ensure_utc, jsonable
from ..errors import ConfigurationError, DataValidationError, InsufficientDataError
from ..instruments import parse_timeframe
from .data import (DatasetSpec, Window, data_version, make_windows,
                   temporal_split)
from .schema import MarketStateSchema, OLYMPUS_MARKET_SCHEMA

#: Bumped when the manifest's *shape* changes, so an old manifest is detectably
#: old rather than partially parsed.
MANIFEST_VERSION = 1


# ===========================================================================
# provenance
# ===========================================================================

@dataclass(frozen=True)
class SourceRecord:
    """Where a slice of the dataset came from, and under what terms.

    `licence` is not decoration. A dataset assembled from sources whose terms
    were never recorded cannot be redistributed, cannot be used to train a model
    anyone else may run, and the question surfaces at the worst possible moment.
    Blocker B4 is this repository's standing example of exactly that position,
    arrived at with a set of third-party weights rather than with data.
    """

    provider: str
    endpoint: str
    instrument_keys: tuple[str, ...]
    timeframe: str
    first_ts: datetime
    last_ts: datetime
    bars: int
    #: When Olympus fetched it. Distinct from the bar timestamps.
    retrieved_at: datetime | None = None
    licence: str = "unknown"
    #: True when the provider states the prices are corporate-action adjusted.
    #: `"unknown"` is a third state and is not the same as False.
    adjusted: str = "unknown"
    notes: str = ""

    def __post_init__(self):
        if not str(self.provider).strip():
            raise ConfigurationError("a source must name its provider")
        object.__setattr__(self, "instrument_keys", tuple(self.instrument_keys))
        if not self.instrument_keys:
            raise ConfigurationError("a source must name its instruments",
                                     provider=self.provider)
        for name in ("first_ts", "last_ts"):
            object.__setattr__(self, name,
                               ensure_utc(getattr(self, name), field_name=name))
        if self.retrieved_at is not None:
            object.__setattr__(self, "retrieved_at",
                               ensure_utc(self.retrieved_at,
                                          field_name="retrieved_at"))
        if self.last_ts < self.first_ts:
            raise ConfigurationError("source interval is inverted",
                                     provider=self.provider)
        if int(self.bars) < 0:
            raise ConfigurationError("bars must be >= 0", provider=self.provider)
        if str(self.adjusted) not in ("true", "false", "unknown"):
            raise ConfigurationError(
                "adjusted must be 'true', 'false' or 'unknown'; a boolean here "
                "would collapse 'the provider says no' and 'nobody asked'",
                provider=self.provider, adjusted=self.adjusted)

    def to_dict(self) -> dict:
        return {"provider": self.provider, "endpoint": self.endpoint,
                "instrument_keys": list(self.instrument_keys),
                "timeframe": self.timeframe,
                "first_ts": jsonable(self.first_ts),
                "last_ts": jsonable(self.last_ts), "bars": self.bars,
                "retrieved_at": jsonable(self.retrieved_at) if self.retrieved_at else None,
                "licence": self.licence, "adjusted": self.adjusted,
                "notes": self.notes}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SourceRecord":
        retrieved = data.get("retrieved_at")
        return cls(provider=str(data["provider"]),
                   endpoint=str(data.get("endpoint", "")),
                   instrument_keys=tuple(data.get("instrument_keys") or ()),
                   timeframe=str(data.get("timeframe", "")),
                   first_ts=datetime.fromisoformat(data["first_ts"]),
                   last_ts=datetime.fromisoformat(data["last_ts"]),
                   bars=int(data.get("bars", 0)),
                   retrieved_at=datetime.fromisoformat(retrieved) if retrieved else None,
                   licence=str(data.get("licence", "unknown")),
                   adjusted=str(data.get("adjusted", "unknown")),
                   notes=str(data.get("notes", "")))


# ===========================================================================
# universe membership through time
# ===========================================================================

@dataclass(frozen=True)
class MembershipInterval:
    """An instrument's tenure in a universe. `until=None` means still a member."""

    instrument_key: str
    since: datetime
    until: datetime | None = None
    reason_added: str = ""
    reason_removed: str = ""

    def __post_init__(self):
        if not str(self.instrument_key).strip():
            raise ConfigurationError("a membership needs an instrument")
        object.__setattr__(self, "since",
                           ensure_utc(self.since, field_name="since"))
        if self.until is not None:
            object.__setattr__(self, "until",
                               ensure_utc(self.until, field_name="until"))
            if self.until <= self.since:
                raise ConfigurationError("membership interval is inverted",
                                         instrument_key=self.instrument_key)

    def covers(self, when: datetime) -> bool:
        moment = ensure_utc(when, field_name="when")
        return self.since <= moment and (self.until is None or moment < self.until)

    def to_dict(self) -> dict:
        return {"instrument_key": self.instrument_key,
                "since": jsonable(self.since),
                "until": jsonable(self.until) if self.until else None,
                "reason_added": self.reason_added,
                "reason_removed": self.reason_removed}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MembershipInterval":
        until = data.get("until")
        return cls(instrument_key=str(data["instrument_key"]),
                   since=datetime.fromisoformat(data["since"]),
                   until=datetime.fromisoformat(until) if until else None,
                   reason_added=str(data.get("reason_added", "")),
                   reason_removed=str(data.get("reason_removed", "")))


class Universe:
    """Instrument membership as a function of time.

    The whole point is `members_at`. A dataset built from `universe.members_at(t)`
    for each `t` cannot use tomorrow's index constituents, which is the
    difference between a backtest and a backtest that only trades survivors.
    """

    __slots__ = ("name", "_intervals")

    def __init__(self, name: str, intervals: Sequence[MembershipInterval] = ()):
        if not str(name).strip():
            raise ConfigurationError("a universe must be named")
        self.name = str(name)
        self._intervals: list[MembershipInterval] = []
        for interval in intervals:
            self.add(interval)

    def add(self, interval: MembershipInterval) -> "Universe":
        for existing in self._intervals:
            if existing.instrument_key != interval.instrument_key:
                continue
            overlap = (existing.until is None or interval.since < existing.until) \
                and (interval.until is None or existing.since < interval.until)
            if overlap:
                raise ConfigurationError(
                    "overlapping membership intervals for one instrument",
                    instrument_key=interval.instrument_key,
                    universe=self.name)
        self._intervals.append(interval)
        self._intervals.sort(key=lambda i: (i.since, i.instrument_key))
        return self

    @property
    def intervals(self) -> tuple[MembershipInterval, ...]:
        return tuple(self._intervals)

    @property
    def all_members(self) -> tuple[str, ...]:
        """Every instrument that was *ever* a member.

        Named to be unattractive. Using this to build a dataset is precisely
        the survivorship error; `members_at` is the correct call.
        """
        return tuple(sorted({i.instrument_key for i in self._intervals}))

    def members_at(self, when: datetime) -> tuple[str, ...]:
        moment = ensure_utc(when, field_name="when")
        return tuple(sorted({i.instrument_key for i in self._intervals
                             if i.covers(moment)}))

    def was_member(self, instrument_key: str, when: datetime) -> bool:
        return instrument_key in self.members_at(when)

    def filter_bars(self, candles: Sequence[Candle]) -> list[Candle]:
        """Drop bars for instruments that were not members when they closed.

        This is the mechanical survivorship defence: a bar for a company that
        joined the index next year cannot enter a window stamped this year.
        """
        return [bar for bar in candles
                if self.was_member(bar.instrument_key, bar.ts_close)]

    def to_dict(self) -> dict:
        return {"name": self.name,
                "intervals": [i.to_dict() for i in self._intervals]}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Universe":
        return cls(str(data["name"]),
                   [MembershipInterval.from_dict(i)
                    for i in data.get("intervals") or ()])

    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(self.to_dict(), sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]


# ===========================================================================
# corporate actions
# ===========================================================================

class ActionType(str, Enum):
    SPLIT = "split"
    REVERSE_SPLIT = "reverse_split"
    CASH_DIVIDEND = "cash_dividend"
    SPIN_OFF = "spin_off"


@dataclass(frozen=True)
class CorporateAction:
    """A split or distribution, with the two dates that are not the same date.

    `announced_at` is when the ratio became knowable. `ex_date` is when prices
    changed units. Adjusting a series using an action whose `ex_date` has not
    arrived restates history with information from the future, which is why
    `adjust_for_actions` takes an `as_of` and why it is not optional.
    """

    instrument_key: str
    action: ActionType
    ex_date: datetime
    #: Split ratio (2.0 = two-for-one) or dividend amount in quote currency.
    value: float
    announced_at: datetime | None = None

    def __post_init__(self):
        object.__setattr__(self, "action", ActionType(self.action))
        object.__setattr__(self, "ex_date",
                           ensure_utc(self.ex_date, field_name="ex_date"))
        if self.announced_at is not None:
            object.__setattr__(self, "announced_at",
                               ensure_utc(self.announced_at,
                                          field_name="announced_at"))
            if self.announced_at > self.ex_date:
                raise ConfigurationError(
                    "an action announced after its ex-date cannot have been "
                    "acted on", instrument_key=self.instrument_key)
        value = float(self.value)
        if self.action in (ActionType.SPLIT, ActionType.REVERSE_SPLIT) and value <= 0:
            raise ConfigurationError("a split ratio must be positive",
                                     instrument_key=self.instrument_key,
                                     value=value)
        if self.action in (ActionType.CASH_DIVIDEND, ActionType.SPIN_OFF) and value < 0:
            raise ConfigurationError("a distribution must be non-negative",
                                     instrument_key=self.instrument_key,
                                     value=value)
        object.__setattr__(self, "value", value)

    def to_dict(self) -> dict:
        return {"instrument_key": self.instrument_key,
                "action": self.action.value, "ex_date": jsonable(self.ex_date),
                "value": self.value,
                "announced_at": jsonable(self.announced_at) if self.announced_at else None}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CorporateAction":
        announced = data.get("announced_at")
        return cls(instrument_key=str(data["instrument_key"]),
                   action=ActionType(data["action"]),
                   ex_date=datetime.fromisoformat(data["ex_date"]),
                   value=float(data["value"]),
                   announced_at=datetime.fromisoformat(announced) if announced else None)


def adjust_for_actions(candles: Sequence[Candle],
                       actions: Sequence[CorporateAction], *,
                       as_of: datetime) -> list[Candle]:
    """Back-adjust prices using only actions that had already happened.

    An action with `ex_date > as_of` is skipped. That is the whole difference
    between a series a backtest may read and a series that has been quietly
    restated with next month's split.

    Bars strictly before an applicable ex-date are divided by the split ratio
    (and reduced by the dividend); volume is multiplied by the ratio so that
    notional is preserved. Bars on or after the ex-date are already in the new
    units and are left alone.
    """
    moment = ensure_utc(as_of, field_name="as_of")
    applicable = sorted((a for a in actions if a.ex_date <= moment),
                        key=lambda a: a.ex_date)
    if not applicable:
        return list(candles)

    out: list[Candle] = []
    for bar in candles:
        price_factor = 1.0
        volume_factor = 1.0
        subtract = 0.0
        for action in applicable:
            if action.instrument_key != bar.instrument_key:
                continue
            if bar.ts_close >= action.ex_date:
                continue
            if action.action is ActionType.SPLIT:
                price_factor /= action.value
                volume_factor *= action.value
            elif action.action is ActionType.REVERSE_SPLIT:
                price_factor *= action.value
                volume_factor /= action.value
            else:
                subtract += action.value
        if price_factor == 1.0 and volume_factor == 1.0 and subtract == 0.0:
            out.append(bar)
            continue
        def adjust(price: float) -> float:
            value = price * price_factor - subtract
            if value <= 0:
                raise DataValidationError(
                    "corporate-action adjustment produced a non-positive "
                    "price; the action data is wrong for this instrument",
                    instrument_key=bar.instrument_key, ts=bar.ts_close,
                    price=price, factor=price_factor, subtract=subtract)
            return value
        out.append(Candle(
            instrument_key=bar.instrument_key, timeframe=bar.timeframe,
            ts_open=bar.ts_open, ts_close=bar.ts_close,
            open=adjust(bar.open), high=adjust(bar.high),
            low=adjust(bar.low), close=adjust(bar.close),
            volume=bar.volume * volume_factor, amount=bar.amount,
            is_final=bar.is_final, source=bar.source))
    return out


# ===========================================================================
# quality: duplicates, gaps, ordering
# ===========================================================================

@dataclass(frozen=True)
class Duplicate:
    instrument_key: str
    timeframe: str
    ts_open: datetime
    count: int
    #: True when the duplicated bars disagree on their values, which is a
    #: different and worse problem than a repeated identical row.
    conflicting: bool

    def to_dict(self) -> dict:
        return {"instrument_key": self.instrument_key,
                "timeframe": self.timeframe, "ts_open": jsonable(self.ts_open),
                "count": self.count, "conflicting": self.conflicting}


@dataclass(frozen=True)
class Gap:
    instrument_key: str
    timeframe: str
    after: datetime
    before: datetime
    missing_bars: int

    def to_dict(self) -> dict:
        return {"instrument_key": self.instrument_key,
                "timeframe": self.timeframe, "after": jsonable(self.after),
                "before": jsonable(self.before),
                "missing_bars": self.missing_bars}


def detect_duplicates(candles: Sequence[Candle]) -> list[Duplicate]:
    """Bars sharing an (instrument, timeframe, ts_open).

    Conflicting duplicates are flagged separately: two identical rows are a
    de-duplication problem, two rows disagreeing about the same minute are an
    ingestion problem, and treating them the same hides the second.
    """
    buckets: dict[tuple[str, str, datetime], list[Candle]] = {}
    for bar in candles:
        buckets.setdefault((bar.instrument_key, bar.timeframe, bar.ts_open),
                           []).append(bar)
    out: list[Duplicate] = []
    for (key, timeframe, ts_open), bars in sorted(buckets.items(),
                                                  key=lambda kv: kv[0][2]):
        if len(bars) < 2:
            continue
        signature = {(b.open, b.high, b.low, b.close, b.volume) for b in bars}
        out.append(Duplicate(instrument_key=key, timeframe=timeframe,
                             ts_open=ts_open, count=len(bars),
                             conflicting=len(signature) > 1))
    return out


def detect_gaps(candles: Sequence[Candle], *, timeframe: str = "",
                tolerance: float = 0.5) -> list[Gap]:
    """Missing bars, inferred from the timeframe's own step.

    Nothing is filled. A gap is reported so the manifest records it and the
    mask can carry it; a forward-filled bar is a fabricated observation with a
    real close price attached to it.
    """
    bars = sorted(candles, key=lambda b: (b.instrument_key, b.ts_open))
    if len(bars) < 2:
        return []
    out: list[Gap] = []
    for i in range(1, len(bars)):
        previous, current = bars[i - 1], bars[i]
        if previous.instrument_key != current.instrument_key:
            continue
        tf = timeframe or current.timeframe
        try:
            step = parse_timeframe(tf)
        except Exception:                                # noqa: BLE001
            continue
        if step <= timedelta(0):                         # pragma: no cover
            continue
        delta = current.ts_open - previous.ts_open
        expected = step
        if delta <= expected * (1.0 + tolerance):
            continue
        missing = int(round(delta / expected)) - 1
        if missing < 1:                                  # pragma: no cover
            continue
        out.append(Gap(instrument_key=current.instrument_key, timeframe=tf,
                       after=previous.ts_close, before=current.ts_open,
                       missing_bars=missing))
    return out


@dataclass(frozen=True)
class QualityReport:
    """What is wrong with the bars, counted rather than characterised.

    `usable` is a judgement and is deliberately conservative: conflicting
    duplicates or non-final bars make a dataset unusable outright, because both
    mean the numbers are not what they claim to be. Gaps do not — a market with
    a weekend has gaps and is still a market.
    """

    bars: int
    instruments: tuple[str, ...]
    first_ts: datetime | None
    last_ts: datetime | None
    duplicates: tuple[Duplicate, ...] = ()
    gaps: tuple[Gap, ...] = ()
    non_final: int = 0
    out_of_order: int = 0
    mixed_timeframes: tuple[str, ...] = ()
    #: Fraction of applicable schema channels actually observed, when a schema
    #: was supplied. `None` when no schema-level check was run.
    channel_completeness: float | None = None

    def __post_init__(self):
        for name in ("instruments", "duplicates", "gaps", "mixed_timeframes"):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    @property
    def conflicting_duplicates(self) -> int:
        return sum(1 for d in self.duplicates if d.conflicting)

    @property
    def missing_bars(self) -> int:
        return sum(g.missing_bars for g in self.gaps)

    @property
    def usable(self) -> bool:
        return (self.bars > 0 and self.non_final == 0
                and self.out_of_order == 0
                and self.conflicting_duplicates == 0
                and len(self.mixed_timeframes) <= 1)

    @property
    def blocking_reasons(self) -> tuple[str, ...]:
        reasons = []
        if self.bars == 0:
            reasons.append("no bars")
        if self.non_final:
            reasons.append(f"{self.non_final} non-final bars")
        if self.out_of_order:
            reasons.append(f"{self.out_of_order} out-of-order bars")
        if self.conflicting_duplicates:
            reasons.append(f"{self.conflicting_duplicates} conflicting duplicates")
        if len(self.mixed_timeframes) > 1:
            reasons.append(f"mixed timeframes {sorted(self.mixed_timeframes)}")
        return tuple(reasons)

    def to_dict(self) -> dict:
        return {"bars": self.bars, "instruments": list(self.instruments),
                "first_ts": jsonable(self.first_ts) if self.first_ts else None,
                "last_ts": jsonable(self.last_ts) if self.last_ts else None,
                "duplicates": [d.to_dict() for d in self.duplicates],
                "conflicting_duplicates": self.conflicting_duplicates,
                "gaps": [g.to_dict() for g in self.gaps],
                "missing_bars": self.missing_bars,
                "non_final": self.non_final, "out_of_order": self.out_of_order,
                "mixed_timeframes": list(self.mixed_timeframes),
                "channel_completeness": self.channel_completeness,
                "usable": self.usable,
                "blocking_reasons": list(self.blocking_reasons)}


def quality_report(candles: Sequence[Candle], *,
                   channel_completeness: float | None = None) -> QualityReport:
    """Everything checkable about a bar series, in one pass."""
    bars = list(candles)
    if not bars:
        return QualityReport(bars=0, instruments=(), first_ts=None, last_ts=None)

    instruments = tuple(sorted({b.instrument_key for b in bars}))
    timeframes = tuple(sorted({b.timeframe for b in bars}))
    non_final = sum(1 for b in bars if not b.is_final)

    out_of_order = 0
    by_instrument: dict[str, list[Candle]] = {}
    for bar in bars:
        by_instrument.setdefault(bar.instrument_key, []).append(bar)
    for series in by_instrument.values():
        for i in range(1, len(series)):
            if series[i].ts_open <= series[i - 1].ts_open:
                out_of_order += 1

    return QualityReport(
        bars=len(bars), instruments=instruments,
        first_ts=min(b.ts_open for b in bars),
        last_ts=max(b.ts_close for b in bars),
        duplicates=tuple(detect_duplicates(bars)),
        gaps=tuple(detect_gaps(bars)),
        non_final=non_final, out_of_order=out_of_order,
        mixed_timeframes=timeframes,
        channel_completeness=channel_completeness)


# ===========================================================================
# alignment
# ===========================================================================

@dataclass(frozen=True)
class Aligned:
    """One base bar and the context that had already closed beside it.

    `staleness` is in *bars of the context series*, and it is reported rather
    than hidden because a daily context bar paired with a 09:15 hourly bar is
    up to a day old, and a model that cannot see that treats yesterday's close
    as this morning's.
    """

    base: Candle
    context: Mapping[str, Candle | None]
    staleness_seconds: Mapping[str, float | None] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "context", dict(self.context))
        object.__setattr__(self, "staleness_seconds", dict(self.staleness_seconds))

    @property
    def complete(self) -> bool:
        return all(bar is not None for bar in self.context.values())

    def missing(self) -> tuple[str, ...]:
        return tuple(sorted(k for k, v in self.context.items() if v is None))


def assert_utc(candles: Sequence[Candle]) -> None:
    """Every timestamp is timezone-aware UTC.

    `Candle` already enforces this at construction, so this is a second,
    independent check for series that arrived by deserialisation or by a path
    that bypassed the contract. Two checks, because a naive datetime compares
    fine against another naive datetime and silently mis-orders against an
    aware one.
    """
    for bar in candles:
        for name in ("ts_open", "ts_close"):
            value = getattr(bar, name)
            if value.tzinfo is None or value.utcoffset() != timedelta(0):
                raise DataValidationError(
                    "dataset timestamps must be timezone-aware UTC",
                    instrument_key=bar.instrument_key, field=name,
                    value=repr(value))


def align_causally(base: Sequence[Candle],
                   context: Mapping[str, Sequence[Candle]], *,
                   max_staleness: timedelta | None = None) -> list[Aligned]:
    """Pair each base bar with the newest context bar that had already closed.

    The rule is `context.ts_close <= base.ts_close`, and it is the whole
    function. The tempting alternative — the context bar whose interval
    *contains* the base bar — is wrong, because that bar has not closed and its
    high, low and close are still moving.

    A context series with nothing old enough yields `None`, not the first
    available bar. `max_staleness` turns an over-aged pairing into `None` as
    well, so a channel declared `LAST_KNOWN` with a bounded staleness in the
    schema can be honoured here.
    """
    ordered_context: dict[str, list[Candle]] = {
        name: sorted(bars, key=lambda b: b.ts_close)
        for name, bars in context.items()}
    cursors = {name: 0 for name in ordered_context}

    out: list[Aligned] = []
    for bar in sorted(base, key=lambda b: b.ts_close):
        picked: dict[str, Candle | None] = {}
        ages: dict[str, float | None] = {}
        for name, series in ordered_context.items():
            index = cursors[name]
            while index < len(series) and series[index].ts_close <= bar.ts_close:
                index += 1
            cursors[name] = index
            chosen = series[index - 1] if index > 0 else None
            if chosen is not None:
                age = (bar.ts_close - chosen.ts_close).total_seconds()
                if max_staleness is not None and age > max_staleness.total_seconds():
                    chosen, age = None, None
            else:
                age = None
            picked[name] = chosen
            ages[name] = age
        out.append(Aligned(base=bar, context=picked, staleness_seconds=ages))
    return out


def align_timeframes(base: Sequence[Candle],
                     higher: Mapping[str, Sequence[Candle]], *,
                     max_staleness: timedelta | None = None) -> list[Aligned]:
    """Multi-timeframe alignment. `align_causally` keyed by timeframe.

    Refuses a context series whose step is *shorter* than the base's: pairing a
    1h bar with the most recent 1m bar discards 59 minutes of the very
    information the finer series was fetched for, and is almost always a
    transposed argument rather than an intention.
    """
    if base:
        base_step = parse_timeframe(base[0].timeframe)
        for name, series in higher.items():
            if not series:
                continue
            if parse_timeframe(series[0].timeframe) < base_step:
                raise ConfigurationError(
                    "context timeframe is finer than the base timeframe; "
                    "aligning it this way keeps only its last bar and discards "
                    "the rest",
                    base=base[0].timeframe, context=series[0].timeframe,
                    key=name)
    return align_causally(base, higher, max_staleness=max_staleness)


def align_cross_asset(base: Sequence[Candle],
                      references: Mapping[str, Sequence[Candle]], *,
                      max_staleness: timedelta | None = None) -> list[Aligned]:
    """Cross-asset alignment. Same causal rule, keyed by instrument.

    Separate from `align_timeframes` because the failure modes differ: here the
    common one is a reference trading on a different calendar, so the staleness
    report is what tells you a US equity is being paired with Friday's close on
    a Sunday crypto bar.
    """
    return align_causally(base, references, max_staleness=max_staleness)


def cross_asset_returns(aligned: Sequence[Aligned], reference: str
                        ) -> list[float | None]:
    """Log return of a reference between consecutive alignments.

    `None` where the reference was absent or unchanged-because-stale, rather
    than zero. A stale reference produces a zero return that is indistinguishable
    from a genuinely flat one, and that is the error worth not making.
    """
    out: list[float | None] = []
    previous: Candle | None = None
    for row in aligned:
        current = row.context.get(reference)
        if current is None or previous is None or current is previous:
            out.append(None)
        elif current.close <= 0 or previous.close <= 0:  # pragma: no cover
            out.append(None)
        else:
            out.append(math.log(current.close / previous.close))
        if current is not None:
            previous = current
    return out


# ===========================================================================
# splits: three-way and walk-forward
# ===========================================================================

@dataclass(frozen=True)
class ThreeWaySplit:
    """Train, validation and test, embargoed at both boundaries.

    Validation exists so that model selection — epochs, width, which candidate
    representation — happens without touching test. A hyper-parameter chosen on
    the test set has consumed it, and every number reported afterwards is an
    in-sample number wearing an out-of-sample label.
    """

    train: tuple[Window, ...]
    validation: tuple[Window, ...]
    test: tuple[Window, ...]
    train_boundary: datetime
    validation_boundary: datetime
    embargo_bars: int
    embargoed: int = 0

    def __post_init__(self):
        for name in ("train", "validation", "test"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        for name in ("train_boundary", "validation_boundary"):
            object.__setattr__(self, name,
                               ensure_utc(getattr(self, name), field_name=name))

    @property
    def sizes(self) -> tuple[int, int, int]:
        return (len(self.train), len(self.validation), len(self.test))

    def to_dict(self) -> dict:
        return {"train": len(self.train), "validation": len(self.validation),
                "test": len(self.test),
                "train_boundary": jsonable(self.train_boundary),
                "validation_boundary": jsonable(self.validation_boundary),
                "embargo_bars": self.embargo_bars, "embargoed": self.embargoed}


def three_way_split(windows: Sequence[Window], *, train_fraction: float = 0.6,
                    validation_fraction: float = 0.2,
                    embargo_bars: int | None = None) -> ThreeWaySplit:
    """Split in time, twice, embargoing each boundary.

    Implemented as two applications of `temporal_split` so that the embargo
    logic exists in exactly one place. A second implementation of "drop the
    windows that straddle the cut" is a second opportunity to get it wrong.
    """
    if not 0.0 < train_fraction < 1.0:
        raise ConfigurationError("train_fraction must be in (0, 1)",
                                 train_fraction=train_fraction)
    if not 0.0 < validation_fraction < 1.0:
        raise ConfigurationError("validation_fraction must be in (0, 1)",
                                 validation_fraction=validation_fraction)
    if train_fraction + validation_fraction >= 1.0:
        raise ConfigurationError(
            "train and validation leave no test set",
            train_fraction=train_fraction,
            validation_fraction=validation_fraction)

    first = temporal_split(windows, train_fraction=train_fraction,
                           embargo_bars=embargo_bars)
    remaining = first.test
    if not remaining:
        raise InsufficientDataError(
            "the embargo consumed everything after the training set; there is "
            "no validation or test data left",
            train=len(first.train), embargoed=first.embargoed)

    # Of what survives the first embargo, the validation share is measured
    # against the *original* total so the caller's fractions mean what they say.
    share = validation_fraction / (1.0 - train_fraction)
    share = min(max(share, 1e-6), 1.0 - 1e-6)
    second = temporal_split(remaining, train_fraction=share,
                            embargo_bars=embargo_bars)

    return ThreeWaySplit(
        train=first.train, validation=second.train, test=second.test,
        train_boundary=first.boundary, validation_boundary=second.boundary,
        embargo_bars=first.embargo_bars,
        embargoed=first.embargoed + second.embargoed)


@dataclass(frozen=True)
class WalkForwardFold:
    """One train/test pair in a walk-forward sequence."""

    index: int
    train: tuple[Window, ...]
    test: tuple[Window, ...]
    boundary: datetime
    embargoed: int = 0

    def __post_init__(self):
        object.__setattr__(self, "train", tuple(self.train))
        object.__setattr__(self, "test", tuple(self.test))
        object.__setattr__(self, "boundary",
                           ensure_utc(self.boundary, field_name="boundary"))

    def to_dict(self) -> dict:
        return {"index": self.index, "train": len(self.train),
                "test": len(self.test), "boundary": jsonable(self.boundary),
                "embargoed": self.embargoed}


def walk_forward_folds(windows: Sequence[Window], *, folds: int = 4,
                       min_train: int = 1, anchored: bool = True,
                       embargo_bars: int | None = None
                       ) -> list[WalkForwardFold]:
    """Expanding (anchored) or rolling walk-forward folds, each embargoed.

    Anchored by default. A rolling window that discards old data is a modelling
    choice about regime persistence, and defaulting to it would make that choice
    silently on the caller's behalf.

    Every fold's train/test boundary goes through the same embargo as a single
    split, so a fold cannot be the place where the leakage defence is skipped.
    """
    ordered = sorted(windows, key=lambda w: w.as_of)
    if folds < 1:
        raise ConfigurationError("folds must be >= 1", folds=folds)
    if min_train < 1:
        raise ConfigurationError("min_train must be >= 1", min_train=min_train)
    if len(ordered) < min_train + folds:
        raise InsufficientDataError(
            "not enough windows for the requested folds",
            have=len(ordered), need=min_train + folds, folds=folds)

    horizon = ordered[0].horizon
    if any(w.horizon != horizon for w in ordered):
        raise ConfigurationError("windows have mixed horizons")

    total = len(ordered)
    test_size = max(1, (total - min_train) // folds)
    out: list[WalkForwardFold] = []
    for i in range(folds):
        train_end = min_train + i * test_size
        test_end = min(total, train_end + test_size)
        if train_end >= total or test_end <= train_end:
            break
        train_start = 0 if anchored else train_end - min_train
        window_slice = ordered[train_start:test_end]
        # The embargo is applied by `temporal_split` rather than re-implemented,
        # so a fold cannot be the one place the defence is skipped. The fraction
        # is chosen so the split's own cut lands on this fold's boundary.
        fraction = (train_end - train_start) / len(window_slice)
        fraction = min(max(fraction, 1e-6), 1.0 - 1e-6)
        split = temporal_split(window_slice, train_fraction=fraction,
                               embargo_bars=embargo_bars)
        if not split.test:
            continue
        out.append(WalkForwardFold(index=i, train=split.train, test=split.test,
                                   boundary=split.boundary,
                                   embargoed=split.embargoed))
    if not out:
        raise InsufficientDataError(
            "every fold's test set was consumed by the embargo",
            windows=total, folds=folds, horizon=horizon)
    return out


# ===========================================================================
# leakage checks
# ===========================================================================

@dataclass(frozen=True)
class LeakageFinding:
    check: str
    detail: str
    context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "context", dict(self.context))

    def to_dict(self) -> dict:
        return {"check": self.check, "detail": self.detail,
                "context": jsonable(dict(self.context))}


def leakage_report(*, train: Sequence[Window], test: Sequence[Window],
                   validation: Sequence[Window] = (),
                   schema: MarketStateSchema | None = None
                   ) -> list[LeakageFinding]:
    """Independent audit of a split. Returns findings; empty means clean.

    Independent of the code that built the split, on purpose. A check that
    shares a helper with the thing it checks fails together with it, and the
    checks here are worth having precisely for the case where `temporal_split`
    is the buggy part.
    """
    findings: list[LeakageFinding] = []
    parts = {"train": list(train), "validation": list(validation),
             "test": list(test)}

    # 1. Identical windows in two parts.
    seen: dict[tuple, str] = {}
    for label, windows in parts.items():
        for window in windows:
            key = (window.instrument_key, window.timeframe,
                   window.inputs[0].ts_open, window.targets[-1].ts_close)
            previous = seen.get(key)
            if previous is not None and previous != label:
                findings.append(LeakageFinding(
                    check="window_overlap",
                    detail=f"a window appears in both {previous} and {label}",
                    context={"instrument_key": window.instrument_key,
                             "as_of": window.as_of.isoformat()}))
            seen[key] = label

    # 2. Ordering: every part must lie strictly after the previous one.
    ordered_labels = [k for k in ("train", "validation", "test") if parts[k]]
    for earlier, later in zip(ordered_labels, ordered_labels[1:]):
        last_target = max(w.targets[-1].ts_close for w in parts[earlier])
        first_input = min(w.inputs[0].ts_open for w in parts[later])
        if first_input < last_target:
            findings.append(LeakageFinding(
                check="embargo_violation",
                detail=(f"{later} inputs begin before {earlier} targets end, so "
                        f"{earlier} trained on {later}'s features"),
                context={"earlier": earlier, "later": later,
                         "last_target": last_target.isoformat(),
                         "first_input": first_input.isoformat()}))

    # 3. Within a window: the horizon must not be inside the inputs. `Window`
    #    enforces this at construction; re-checking here catches a Window built
    #    by a future code path that bypasses the constructor.
    for label, windows in parts.items():
        for window in windows:
            if window.targets[0].ts_open < window.inputs[-1].ts_close:
                findings.append(LeakageFinding(
                    check="horizon_inside_inputs",
                    detail=f"a {label} window's first target opens before its "
                           f"last input closes",
                    context={"as_of": window.as_of.isoformat()}))

    # 4. Channels that cannot be consumed without a vintage.
    #
    #    Scoped to *obtainable* channels. A REVISED channel that Olympus cannot
    #    fetch is not in the data and therefore cannot leak from it; flagging it
    #    here would mark every dataset unclean for a channel nobody has, and a
    #    finding that is always present is a finding nobody reads. The design
    #    concern is recorded where it belongs — in the schema, on the spec.
    if schema is not None:
        for spec in schema.needing_vintage():
            if not spec.obtainable:
                continue
            findings.append(LeakageFinding(
                check="revised_channel",
                detail=(f"channel {spec.name!r} is REVISED: the archived value "
                        f"is not the value that was published, so consuming it "
                        f"without a vintage is a look-ahead"),
                context={"channel": spec.name, "source": spec.source}))

    return findings


def assert_no_leakage(**kwargs: Any) -> None:
    """`leakage_report`, raising. For the training pipeline's own use."""
    findings = leakage_report(**kwargs)
    if findings:
        raise DataValidationError(
            "the split has leakage findings",
            findings=[f.to_dict() for f in findings])


# ===========================================================================
# scaler policy
# ===========================================================================

class ScalerPolicy:
    """Carries the split boundary so a scaler cannot be fitted across it.

    `features.fit_scaler` will happily fit on whatever it is given. This is the
    object that decides what it is given: rows at or after the boundary are
    refused, by mechanism rather than by the caller remembering.
    """

    __slots__ = ("boundary", "channels")

    def __init__(self, *, boundary: datetime,
                 channels: Sequence[str] = ()):
        self.boundary = ensure_utc(boundary, field_name="boundary")
        self.channels = tuple(channels)

    def fitting_rows(self, rows: Sequence[tuple[datetime, Mapping[str, float]]]
                     ) -> list[Mapping[str, float]]:
        """The rows a scaler may be fitted on. Everything else is dropped."""
        return [values for when, values in rows
                if ensure_utc(when, field_name="when") <= self.boundary]

    def check(self, rows: Sequence[tuple[datetime, Mapping[str, float]]]) -> None:
        """Raise if any row would take the fit past the boundary."""
        offenders = [when for when, _ in rows
                     if ensure_utc(when, field_name="when") > self.boundary]
        if offenders:
            raise DataValidationError(
                "scaler statistics would be fitted on data after the training "
                "boundary; every value the scaler produces afterwards carries "
                "information from the test period",
                boundary=self.boundary.isoformat(), offending_rows=len(offenders),
                first_offender=min(offenders).isoformat())

    def to_dict(self) -> dict:
        return {"boundary": jsonable(self.boundary),
                "channels": list(self.channels)}


# ===========================================================================
# the manifest
# ===========================================================================

@dataclass(frozen=True)
class DatasetManifest:
    """Everything needed to rebuild this dataset, and to prove it was not built
    from the future.

    Two hashes, deliberately. `content_hash` is over the bars themselves, so two
    machines can agree they hold the same data. `manifest_hash` is over the whole
    record including the spec, the schema fingerprint, the sources and the
    universe — so a dataset rebuilt from a *different* universe with the same
    bars is a different dataset, which it is.
    """

    dataset_id: str
    spec: DatasetSpec
    #: Hash of the exact bars. `data.data_version`.
    content_hash: str
    schema_fingerprint: str
    schema_name: str
    created_at: datetime
    sources: tuple[SourceRecord, ...] = ()
    quality: QualityReport | None = None
    split: Mapping[str, Any] = field(default_factory=dict)
    universe_name: str = ""
    universe_fingerprint: str = ""
    corporate_actions: int = 0
    adjustment_as_of: datetime | None = None
    leakage_findings: tuple[Mapping[str, Any], ...] = ()
    channels: tuple[str, ...] = ()
    notes: str = ""
    manifest_version: int = MANIFEST_VERSION

    def __post_init__(self):
        if not str(self.dataset_id).strip():
            raise ConfigurationError("a dataset must have an id")
        if not str(self.content_hash).strip():
            raise ConfigurationError("a manifest must carry a content hash",
                                     dataset_id=self.dataset_id)
        object.__setattr__(self, "created_at",
                           ensure_utc(self.created_at, field_name="created_at"))
        if self.adjustment_as_of is not None:
            object.__setattr__(self, "adjustment_as_of",
                               ensure_utc(self.adjustment_as_of,
                                          field_name="adjustment_as_of"))
        object.__setattr__(self, "sources", tuple(self.sources))
        object.__setattr__(self, "channels", tuple(self.channels))
        object.__setattr__(self, "leakage_findings",
                           tuple(dict(f) for f in self.leakage_findings))
        object.__setattr__(self, "split", dict(self.split))

    @property
    def clean(self) -> bool:
        """Nothing known to be wrong with this dataset — including unknowns.

        Empty `gaps` is part of the condition, not a separate axis. A dataset
        whose provenance was never recorded and whose universe is unknown may
        be perfectly well-formed and still be one nobody can defend: "we do not
        know whether this is survivorship-biased" is a defect, and a `clean`
        that ignored it would let the most common real failure pass as fine.
        """
        return (not self.leakage_findings and not self.gaps
                and self.quality is not None and self.quality.usable)

    @property
    def gaps(self) -> tuple[str, ...]:
        """What this manifest cannot tell a reader. Reported, not hidden.

        The same idea as `checkpoint.TrainingManifest.gaps`: a manifest that
        omits its provenance should say so in a field, not by the absence of one.
        """
        missing = []
        if not self.sources:
            missing.append("no source records: provenance is unknown")
        if not self.universe_name:
            missing.append("no universe: survivorship cannot be ruled out")
        if self.quality is None:
            missing.append("no quality report")
        if any(s.licence == "unknown" for s in self.sources):
            missing.append("at least one source has an unknown licence")
        if any(s.adjusted == "unknown" for s in self.sources):
            missing.append("at least one source's adjustment status is unknown")
        return tuple(missing)

    def to_dict(self) -> dict:
        return {"dataset_id": self.dataset_id, "spec": self.spec.to_dict(),
                "content_hash": self.content_hash,
                "schema_fingerprint": self.schema_fingerprint,
                "schema_name": self.schema_name,
                "created_at": jsonable(self.created_at),
                "sources": [s.to_dict() for s in self.sources],
                "quality": self.quality.to_dict() if self.quality else None,
                "split": jsonable(dict(self.split)),
                "universe_name": self.universe_name,
                "universe_fingerprint": self.universe_fingerprint,
                "corporate_actions": self.corporate_actions,
                "adjustment_as_of": (jsonable(self.adjustment_as_of)
                                     if self.adjustment_as_of else None),
                "leakage_findings": [dict(f) for f in self.leakage_findings],
                "channels": list(self.channels), "notes": self.notes,
                "manifest_version": self.manifest_version,
                "gaps": list(self.gaps)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DatasetManifest":
        adjustment = data.get("adjustment_as_of")
        quality = data.get("quality")
        return cls(
            dataset_id=str(data["dataset_id"]),
            spec=DatasetSpec.from_dict(data["spec"]),
            content_hash=str(data["content_hash"]),
            schema_fingerprint=str(data.get("schema_fingerprint", "")),
            schema_name=str(data.get("schema_name", "")),
            created_at=datetime.fromisoformat(data["created_at"]),
            sources=tuple(SourceRecord.from_dict(s)
                          for s in data.get("sources") or ()),
            quality=_quality_from_dict(quality) if quality else None,
            split=dict(data.get("split") or {}),
            universe_name=str(data.get("universe_name", "")),
            universe_fingerprint=str(data.get("universe_fingerprint", "")),
            corporate_actions=int(data.get("corporate_actions", 0)),
            adjustment_as_of=(datetime.fromisoformat(adjustment)
                              if adjustment else None),
            leakage_findings=tuple(data.get("leakage_findings") or ()),
            channels=tuple(data.get("channels") or ()),
            notes=str(data.get("notes", "")),
            manifest_version=int(data.get("manifest_version", MANIFEST_VERSION)))

    def manifest_hash(self) -> str:
        """Hash of the whole record except this hash itself."""
        payload = self.to_dict()
        payload.pop("gaps", None)
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def describe(self) -> str:                           # pragma: no cover
        return (f"{self.dataset_id} [{self.content_hash[:12]}] "
                f"{self.spec.timeframe} x{len(self.spec.instrument_keys)} "
                f"clean={self.clean} gaps={len(self.gaps)}")


def _quality_from_dict(data: Mapping[str, Any]) -> QualityReport:
    first, last = data.get("first_ts"), data.get("last_ts")
    return QualityReport(
        bars=int(data.get("bars", 0)),
        instruments=tuple(data.get("instruments") or ()),
        first_ts=datetime.fromisoformat(first) if first else None,
        last_ts=datetime.fromisoformat(last) if last else None,
        duplicates=tuple(Duplicate(instrument_key=d["instrument_key"],
                                   timeframe=d["timeframe"],
                                   ts_open=datetime.fromisoformat(d["ts_open"]),
                                   count=int(d["count"]),
                                   conflicting=bool(d["conflicting"]))
                         for d in data.get("duplicates") or ()),
        gaps=tuple(Gap(instrument_key=g["instrument_key"],
                       timeframe=g["timeframe"],
                       after=datetime.fromisoformat(g["after"]),
                       before=datetime.fromisoformat(g["before"]),
                       missing_bars=int(g["missing_bars"]))
                   for g in data.get("gaps") or ()),
        non_final=int(data.get("non_final", 0)),
        out_of_order=int(data.get("out_of_order", 0)),
        mixed_timeframes=tuple(data.get("mixed_timeframes") or ()),
        channel_completeness=data.get("channel_completeness"))


# ===========================================================================
# the one entry point
# ===========================================================================

@dataclass(frozen=True)
class BuiltDataset:
    """Windows, splits and the manifest that describes them."""

    manifest: DatasetManifest
    split: ThreeWaySplit
    windows: tuple[Window, ...]

    def __post_init__(self):
        object.__setattr__(self, "windows", tuple(self.windows))

    @property
    def usable(self) -> bool:
        return self.manifest.clean and bool(self.split.train)

    def to_dict(self) -> dict:
        return {"manifest": self.manifest.to_dict(),
                "manifest_hash": self.manifest.manifest_hash(),
                "split": self.split.to_dict(), "windows": len(self.windows),
                "usable": self.usable}


def build_dataset(candles: Sequence[Candle], spec: DatasetSpec, *,
                  dataset_id: str, created_at: datetime,
                  schema: MarketStateSchema | None = None,
                  sources: Sequence[SourceRecord] = (),
                  universe: Universe | None = None,
                  actions: Sequence[CorporateAction] = (),
                  adjustment_as_of: datetime | None = None,
                  train_fraction: float = 0.6,
                  validation_fraction: float = 0.2,
                  notes: str = "") -> BuiltDataset:
    """Bars in, manifest and embargoed three-way split out.

    Deliberately one function, in one order, for the same reason
    `data.build_dataset` is: every step between "raw bars" and "training rows"
    is a place a scaler could be fitted, a universe could be applied too late,
    or an action could be applied from the future. Exposing them as separate
    public steps would make each of those a caller's decision, and the caller is
    not who should be deciding.

    The order is: adjust (as of a stated instant) → filter to universe members
    → quality → window → split → audit → manifest. Auditing *after* splitting
    rather than trusting the splitter is the point of `leakage_report`.
    """
    if not str(dataset_id).strip():
        raise ConfigurationError("a dataset build must name its dataset")
    stamp = ensure_utc(created_at, field_name="created_at")
    active_schema = schema or OLYMPUS_MARKET_SCHEMA

    bars = list(candles)
    assert_utc(bars)

    if actions:
        if adjustment_as_of is None:
            raise ConfigurationError(
                "corporate actions need an adjustment_as_of; applying them "
                "without one restates history using whatever the action list "
                "happens to contain today")
        bars = adjust_for_actions(bars, actions, as_of=adjustment_as_of)

    if universe is not None:
        bars = universe.filter_bars(bars)
        if not bars:
            raise InsufficientDataError(
                "no bars survived the universe filter; every instrument was "
                "outside its membership interval at its own bar times",
                universe=universe.name)

    report = quality_report(bars)
    if not report.usable:
        raise DataValidationError("dataset is not usable",
                                  reasons=list(report.blocking_reasons))

    windows = make_windows(bars, lookback=spec.lookback, horizon=spec.horizon,
                           stride=spec.stride)
    split = three_way_split(windows, train_fraction=train_fraction,
                            validation_fraction=validation_fraction,
                            embargo_bars=spec.embargo_bars)
    findings = leakage_report(train=split.train, validation=split.validation,
                              test=split.test, schema=active_schema)

    manifest = DatasetManifest(
        dataset_id=dataset_id, spec=spec, content_hash=data_version(bars),
        schema_fingerprint=active_schema.fingerprint(),
        schema_name=active_schema.name, created_at=stamp,
        sources=tuple(sources), quality=report, split=split.to_dict(),
        universe_name=universe.name if universe else "",
        universe_fingerprint=universe.fingerprint() if universe else "",
        corporate_actions=len(actions), adjustment_as_of=adjustment_as_of,
        leakage_findings=tuple(f.to_dict() for f in findings),
        channels=active_schema.names, notes=notes)

    return BuiltDataset(manifest=manifest, split=split, windows=tuple(windows))


__all__ = ["MANIFEST_VERSION", "SourceRecord", "MembershipInterval", "Universe",
           "ActionType", "CorporateAction", "adjust_for_actions", "Duplicate",
           "Gap", "detect_duplicates", "detect_gaps", "QualityReport",
           "quality_report", "Aligned", "assert_utc", "align_causally",
           "align_timeframes", "align_cross_asset", "cross_asset_returns",
           "ThreeWaySplit", "three_way_split", "WalkForwardFold",
           "walk_forward_folds", "LeakageFinding", "leakage_report",
           "assert_no_leakage", "ScalerPolicy", "DatasetManifest",
           "BuiltDataset", "build_dataset"]

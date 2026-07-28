"""Market-data quality gate — the boundary every downstream layer trusts.

Features, forecasts, signals, strategies, risk sizing and the backtester all
assume the bars they receive are final, in order, on the timeframe grid, and
recent enough to act on. This module is the single place where that assumption
is *established*, so it is written as a safety component rather than a
convenience:

**It never invents data.** A gap is counted and reported, never filled;
an outlier is flagged, never smoothed away; a rejected row is recorded with its
reason, never silently dropped. Interpolating one missing bar is how a
backtester ends up trading a price that never existed, and how a live strategy
ends up sizing off a fabricated return. The only mutations performed here are
*removals* of data that cannot be true (non-final bars, wrong instrument,
duplicates, bars from the future) plus a stable sort.

**It reports rather than raises.** `normalise_candles` always returns a
`(candles, DataQualityReport)` pair, because "how bad is this window" is a
decision for the caller's policy, not for the parser: a research job may happily
run on 92%-complete history that a live strategy must refuse. Callers that
require good data call `require_usable(report)`, which converts the report's
recorded reasons into the correctly typed error. That split is what lets the
same validation code serve backtest, paper, and live with different tolerances.

**Look-ahead is a separate, loud check.** `assert_no_lookahead` exists because
the most expensive bug in this domain is invisible: a backtest that reads a bar
whose close had not happened yet does not crash, it just prints an excellent
Sharpe ratio. Every component that selects a data window by time — the
backtester and the forecast adapter — calls it, and it raises rather than
warns.

Timeframe arithmetic (`parse_timeframe`, `floor_to_timeframe`) is re-exported
from `instruments` rather than reimplemented, so validation, candle building and
the venue calendars can never disagree by one bar about where a bucket starts.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from .clock import Clock
from .contracts import Candle, DataQuality, DataQualityReport, Instrument, ensure_utc
from .errors import (
    DataValidationError,
    InsufficientDataError,
    MarketDataError,
    StaleDataError,
)
# Timeframe arithmetic has exactly one implementation, in `instruments`. Two
# modules disagreeing by one bar about where a bucket starts is the kind of
# defect that only shows up as an unexplained backtest/live divergence, so the
# bucketing rule is imported, never re-derived.
from .instruments import floor_to_timeframe, parse_timeframe, timeframe_seconds

#: A window with less than this fraction of its expected bars is UNUSABLE.
#: 0.95 is deliberately strict: at 1m bars that is 3 missing minutes an hour,
#: which is already enough to distort a volatility estimate.
DEFAULT_MIN_COMPLETENESS = 0.95

#: Robust-sigma multiple beyond which a return is *flagged* (never dropped).
#: 10 is intentionally far out — this is a "that price is probably a bad tick or
#: an unadjusted split" detector, not a volatility filter. Real market crashes
#: must survive it, because refusing to see a crash is worse than seeing one.
DEFAULT_OUTLIER_SIGMA = 10.0

#: Rolling window for the robust return statistics.
_OUTLIER_WINDOW = 30
_OUTLIER_MIN_SAMPLES = 8

# Error codes recorded in DataQualityReport.errors as "CODE: detail". The
# report is a data contract, not an exception, so the *reason* it is unusable
# has to survive as data; `require_usable` reads these prefixes back and raises
# the matching typed error. Prose can be reworded, these cannot.
ERR_EMPTY = "DATA_EMPTY"
ERR_STALE = "DATA_STALE"
ERR_INCOMPLETE = "DATA_INCOMPLETE"
ERR_INSUFFICIENT = "DATA_INSUFFICIENT"

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# timeframe helpers
# ---------------------------------------------------------------------------

def is_on_grid(ts: datetime, timeframe: str) -> bool:
    return floor_to_timeframe(ts, timeframe) == ensure_utc(ts, field_name="ts")


# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------

def _coerce_max_age(max_age: Any) -> float | None:
    if max_age is None:
        return None
    if isinstance(max_age, timedelta):
        return max_age.total_seconds()
    return float(max_age)


def _instrument_key(instrument: Any) -> str | None:
    if instrument is None:
        return None
    if isinstance(instrument, Instrument):
        return instrument.key
    return str(instrument)


def _robust_outliers(candles: Sequence[Candle], sigma: float) -> list[int]:
    """Indices whose close-to-close return is an extreme relative to its recent
    neighbourhood, using median/MAD rather than mean/stdev.

    Mean and standard deviation are themselves destroyed by the outlier being
    looked for (one 1000x bad tick inflates sigma enough to hide itself), so the
    estimator has to be robust or the detector is decorative.
    """
    if sigma <= 0 or len(candles) < _OUTLIER_MIN_SAMPLES + 1:
        return []
    returns: list[float] = []
    for i in range(1, len(candles)):
        previous = candles[i - 1].close
        returns.append((candles[i].close - previous) / previous if previous > 0 else 0.0)
    flagged: list[int] = []
    for i, value in enumerate(returns):
        window = returns[max(0, i - _OUTLIER_WINDOW):i]
        if len(window) < _OUTLIER_MIN_SAMPLES:
            continue
        centre = _median(window)
        deviations = [abs(v - centre) for v in window]
        # 1.4826 rescales a MAD to a standard deviation for normal data.
        scale = 1.4826 * _median(deviations)
        if scale <= 0:
            # A degenerate MAD (more than half the window identical — a pegged
            # or thinly-traded instrument) would otherwise switch the detector
            # off exactly where a bad print is most likely. Fall back to the
            # mean absolute deviation, which is only zero for a genuinely
            # constant series, where "outlier" has no meaning.
            scale = sum(deviations) / len(deviations)
        if scale <= 0:
            continue
        if abs(value - centre) > sigma * scale:
            flagged.append(i + 1)                # returns[i] describes candles[i+1]
    return flagged


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def normalise_candles(
    raw: Sequence[Candle],
    *,
    timeframe: str,
    clock: Clock,
    expected_start: datetime | None = None,
    expected_end: datetime | None = None,
    max_age: timedelta | float | None = None,
    instrument: Instrument | str | None = None,
    min_completeness: float = DEFAULT_MIN_COMPLETENESS,
    outlier_sigma: float = DEFAULT_OUTLIER_SIGMA,
    drop_future: bool = True,
    _prior_invalid: int = 0,
    _prior_warnings: Sequence[str] = (),
) -> tuple[list[Candle], DataQualityReport]:
    """Clean a candle window and describe exactly what was wrong with it.

    The steps run in a fixed order and each one is counted, because the report
    is an audit record: a year later "why did we not trade" has to be answerable
    from the report alone.

    1. drop non-final bars (an in-progress bar's close can still move)
    2. drop bars for another instrument or timeframe
    3. drop bars from the future relative to `clock` (`drop_future`)
    4. deduplicate on `ts_open`, keeping the LAST occurrence — feeds re-send a
       bar to *correct* it, so the newest copy is the authoritative one
    5. sort ascending, counting how many arrived out of order
    6. count grid slots with no bar. Gaps are COUNTED, NEVER FILLED
    7. measure staleness against `clock.now()`
    8. flag (never drop) extreme close-to-close moves

    Returns `(candles, report)`; it does not raise on bad data — see
    `require_usable`.
    """
    key = _instrument_key(instrument)
    delta = parse_timeframe(timeframe)
    now = ensure_utc(clock.now(), field_name="clock.now()")
    warnings: list[str] = list(_prior_warnings)
    errors: list[str] = []
    invalid_dropped = int(_prior_invalid)

    kept: list[Candle] = []
    non_final = mismatched = future = 0
    for candle in raw:
        if not isinstance(candle, Candle):
            raise DataValidationError(
                "normalise_candles takes Candle objects; use validate_raw() for "
                "untrusted rows", got=type(candle).__name__)
        if not candle.is_final:
            non_final += 1
            continue
        if candle.timeframe != timeframe or (key is not None
                                             and candle.instrument_key != key):
            mismatched += 1
            continue
        if drop_future and candle.ts_close > now:
            future += 1
            continue
        kept.append(candle)
    if key is None and kept:
        key = kept[0].instrument_key
        # A single window must describe a single instrument; anything else is a
        # caller bug that would otherwise produce a meaningless report.
        foreign = [c for c in kept if c.instrument_key != key]
        if foreign:
            mismatched += len(foreign)
            kept = [c for c in kept if c.instrument_key == key]
    invalid_dropped += non_final + mismatched + future
    if non_final:
        warnings.append(f"dropped {non_final} non-final bar(s)")
    if mismatched:
        warnings.append(f"dropped {mismatched} bar(s) for another instrument/timeframe")
    if future:
        warnings.append(f"dropped {future} bar(s) closing after {now.isoformat()}")

    # 4. dedupe, last wins.
    by_open: dict[datetime, Candle] = {}
    duplicates = 0
    for candle in kept:
        if candle.ts_open in by_open:
            duplicates += 1
        by_open[candle.ts_open] = candle
    if duplicates:
        warnings.append(f"removed {duplicates} duplicate bar(s) (kept last seen)")

    # 5. sort; count arrivals that were out of sequence. They are REORDERED,
    #    not discarded — a late-arriving bar is still true, it was just slow.
    arrival = [c for c in kept if by_open.get(c.ts_open) is c]
    out_of_order = sum(1 for i in range(1, len(arrival))
                       if arrival[i].ts_open < arrival[i - 1].ts_open)
    clean = sorted(by_open.values(), key=lambda c: c.ts_open)
    if out_of_order:
        warnings.append(f"reordered {out_of_order} out-of-order bar(s)")

    # 6. grid + gaps. The grid is anchored on `expected_start` when the caller
    #    states one, otherwise on the oldest observed bar — daily equity bars
    #    are stamped at the session open, not at UTC midnight, so an absolute
    #    grid would misclassify every one of them.
    anchor = ensure_utc(expected_start, field_name="expected_start") if expected_start else None
    last_slot = ensure_utc(expected_end, field_name="expected_end") if expected_end else None
    if anchor is None and clean:
        anchor = clean[0].ts_open
    if last_slot is None and clean:
        last_slot = clean[-1].ts_open
    bars_expected = 0
    gaps = 0
    misaligned = 0
    if anchor is not None and last_slot is not None and last_slot >= anchor:
        span = (last_slot - anchor).total_seconds()
        bars_expected = int(span // delta.total_seconds()) + 1
        step = delta.total_seconds()
        present_slots = set()
        aligned: list[Candle] = []
        for candle in clean:
            offset = (candle.ts_open - anchor).total_seconds()
            if offset < 0 or abs(offset % step) > 1e-6:
                misaligned += 1
                continue
            present_slots.add(int(offset // step))
            aligned.append(candle)
        if misaligned:
            # Off-grid bars are removed rather than kept: they cannot be placed
            # on the timeframe grid, so every gap count and every index-based
            # feature computed downstream would be wrong.
            invalid_dropped += misaligned
            warnings.append(f"dropped {misaligned} bar(s) off the {timeframe} grid")
            clean = aligned
        gaps = max(0, bars_expected - len(present_slots))

    bars_present = len(clean)

    # 7. staleness, measured from ts_close: a bar is only knowable once closed.
    newest = clean[-1] if clean else None
    oldest = clean[0] if clean else None
    age_seconds = (now - newest.ts_close).total_seconds() if newest else None
    limit = _coerce_max_age(max_age)
    stale = bool(limit is not None and age_seconds is not None and age_seconds > limit)
    if stale:
        errors.append(
            f"{ERR_STALE}: newest bar closed {age_seconds:.1f}s ago, tolerance {limit:.1f}s")

    # 8. outliers — flagged only.
    outliers = _robust_outliers(clean, outlier_sigma)
    for index in outliers:
        candle = clean[index]
        warnings.append(
            f"outlier close {candle.close} at {candle.ts_open.isoformat()} "
            f"(> {outlier_sigma}σ vs rolling median return)")

    # status
    completeness = min(1.0, bars_present / bars_expected) if bars_expected > 0 else 0.0
    if not clean:
        errors.append(f"{ERR_EMPTY}: no usable bars after validation")
        status = DataQuality.UNUSABLE
    elif stale:
        status = DataQuality.UNUSABLE
    elif completeness < min_completeness:
        errors.append(
            f"{ERR_INCOMPLETE}: completeness {completeness:.4f} < {min_completeness:.4f} "
            f"({bars_present}/{bars_expected} bars)")
        status = DataQuality.UNUSABLE
    elif gaps or duplicates or outliers or out_of_order or invalid_dropped:
        status = DataQuality.DEGRADED
    else:
        status = DataQuality.OK

    report = DataQualityReport(
        instrument_key=key or "", timeframe=timeframe, status=status,
        bars_expected=bars_expected, bars_present=bars_present, gaps=gaps,
        duplicates_removed=duplicates, out_of_order_removed=out_of_order,
        invalid_dropped=invalid_dropped,
        newest_ts=newest.ts_open if newest else None,
        oldest_ts=oldest.ts_open if oldest else None,
        age_seconds=age_seconds, warnings=tuple(warnings), errors=tuple(errors))
    return clean, report


# ---------------------------------------------------------------------------
# tolerant parsing of untrusted feeds
# ---------------------------------------------------------------------------

_TS_OPEN_KEYS = ("ts_open", "open_time", "start", "start_time", "timestamp",
                 "time", "t", "date", "datetime")
_TS_CLOSE_KEYS = ("ts_close", "close_time", "end", "end_time")
_FIELD_KEYS = {
    "open": ("open", "o", "open_price"),
    "high": ("high", "h", "high_price"),
    "low": ("low", "l", "low_price"),
    "close": ("close", "c", "close_price"),
    "volume": ("volume", "v", "vol", "base_volume"),
    "amount": ("amount", "turnover", "quote_volume", "notional"),
}
_FINAL_KEYS = ("is_final", "final", "closed", "is_closed", "confirmed")


def _pick(row: Mapping, keys: Sequence[str]) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return None


def _parse_ts(value: Any, *, field_name: str) -> datetime:
    """Accept datetime, ISO-8601 string, or epoch seconds/millis/micros/nanos.

    Epoch magnitude is disambiguated by range rather than by configuration:
    a feed that switches from seconds to millis mid-stream (they do) otherwise
    lands bars in 1970 or the year 55000 without anyone noticing.
    """
    if isinstance(value, datetime):
        return ensure_utc(value, field_name=field_name)
    if isinstance(value, bool):
        raise DataValidationError(f"{field_name} must be a timestamp", field=field_name)
    if isinstance(value, (int, float)):
        magnitude = abs(float(value))
        if magnitude >= 1e17:
            seconds = float(value) / 1e9
        elif magnitude >= 1e14:
            seconds = float(value) / 1e6
        elif magnitude >= 1e11:
            seconds = float(value) / 1e3
        else:
            seconds = float(value)
        if not math.isfinite(seconds):
            raise DataValidationError(f"{field_name} is not finite", field=field_name)
        return _EPOCH + timedelta(seconds=seconds)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            return _parse_ts(int(text), field_name=field_name)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            raise DataValidationError(f"{field_name} is not an ISO-8601 timestamp",
                                      field=field_name, got=text) from None
        if parsed.tzinfo is None:
            # A naive string from an external feed is assumed UTC *and said so*
            # in the report; guessing local time would shift every bar.
            parsed = parsed.replace(tzinfo=timezone.utc)
        return ensure_utc(parsed, field_name=field_name)
    raise DataValidationError(f"{field_name} has an unsupported type",
                              field=field_name, got=type(value).__name__)


def _parse_float(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or value is None:
        raise DataValidationError(f"{field_name} is missing", field=field_name)
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise DataValidationError(f"{field_name} is not numeric", field=field_name,
                                  got=repr(value)) from None
    if not math.isfinite(result):
        raise DataValidationError(f"{field_name} is not finite", field=field_name)
    return result


def validate_raw(
    rows: Sequence[Mapping],
    *,
    timeframe: str,
    clock: Clock,
    instrument: Instrument | str | None = None,
    source: str = "unknown",
    expected_start: datetime | None = None,
    expected_end: datetime | None = None,
    max_age: timedelta | float | None = None,
    min_completeness: float = DEFAULT_MIN_COMPLETENESS,
    outlier_sigma: float = DEFAULT_OUTLIER_SIGMA,
    drop_future: bool = True,
    max_reported_rejections: int = 20,
) -> tuple[list[Candle], DataQualityReport]:
    """Parse untrusted feed rows into `Candle`s, RECORDING every rejection.

    `Candle` validates hard and raises — correct for internal code, useless for
    a REST page where one malformed row would discard 499 good ones. So this is
    the tolerant boundary: each bad row is counted in `invalid_dropped` and
    described in `warnings`, and what survives goes through exactly the same
    `normalise_candles` pipeline as trusted data. The first
    `max_reported_rejections` reasons are quoted verbatim so the failure is
    diagnosable without re-fetching, then only the count grows (an entirely
    malformed 100k-row page must not produce a 100k-line report).
    """
    key = _instrument_key(instrument)
    delta = parse_timeframe(timeframe)
    parsed: list[Candle] = []
    rejections: list[str] = []
    rejected = 0
    for index, row in enumerate(rows):
        try:
            if not isinstance(row, Mapping):
                raise DataValidationError("row is not a mapping",
                                          got=type(row).__name__)
            ts_raw = _pick(row, _TS_OPEN_KEYS)
            if ts_raw is None:
                raise DataValidationError("row has no open timestamp")
            ts_open = _parse_ts(ts_raw, field_name="ts_open")
            close_raw = _pick(row, _TS_CLOSE_KEYS)
            ts_close = (_parse_ts(close_raw, field_name="ts_close")
                        if close_raw is not None else ts_open + delta)
            values = {name: _parse_float(_pick(row, keys), field_name=name)
                      for name, keys in _FIELD_KEYS.items()
                      if name in ("open", "high", "low", "close")}
            for name in ("volume", "amount"):
                raw_value = _pick(row, _FIELD_KEYS[name])
                values[name] = (0.0 if raw_value is None
                                else _parse_float(raw_value, field_name=name))
            final_raw = _pick(row, _FINAL_KEYS)
            row_key = key or str(_pick(row, ("instrument_key", "symbol", "ticker"))
                                 or "UNKNOWN:UNKNOWN")
            parsed.append(Candle(
                instrument_key=row_key, timeframe=timeframe, ts_open=ts_open,
                ts_close=ts_close, is_final=True if final_raw is None else bool(final_raw),
                source=str(row.get("source", source)), **values))
        except (MarketDataError, ValueError, TypeError, KeyError) as exc:
            rejected += 1
            if len(rejections) < max_reported_rejections:
                rejections.append(f"row {index} rejected: {exc}")
    if rejected > len(rejections):
        rejections.append(f"... and {rejected - len(rejections)} further rejected row(s)")

    return normalise_candles(
        parsed, timeframe=timeframe, clock=clock, expected_start=expected_start,
        expected_end=expected_end, max_age=max_age, instrument=instrument,
        min_completeness=min_completeness, outlier_sigma=outlier_sigma,
        drop_future=drop_future, _prior_invalid=rejected,
        _prior_warnings=rejections)


# ---------------------------------------------------------------------------
# policy gates
# ---------------------------------------------------------------------------

def require_usable(report: DataQualityReport, *, min_bars: int | None = None,
                   allow_degraded: bool = True) -> DataQualityReport:
    """Turn a report into an exception when the data may not be traded on.

    The typed error matters: `StaleDataError` means "try again later", while
    `InsufficientDataError` means "this window will never be good enough, ask
    for more history". Callers and the audit trail distinguish them by class and
    by `.code`, so the reason is recovered from the report's recorded prefixes
    rather than re-derived by re-inspecting the bars.
    """
    detail = {"instrument": report.instrument_key, "timeframe": report.timeframe,
              "bars_present": report.bars_present,
              "bars_expected": report.bars_expected,
              "completeness": round(report.completeness, 4),
              "gaps": report.gaps, "status": report.status.value}
    for entry in report.errors:
        if entry.startswith(ERR_STALE):
            raise StaleDataError(entry, age_seconds=report.age_seconds, **detail)
    for entry in report.errors:
        if entry.startswith(ERR_EMPTY) or entry.startswith(ERR_INCOMPLETE):
            raise InsufficientDataError(entry, **detail)
    if report.status is DataQuality.UNUSABLE:
        raise DataValidationError(
            "; ".join(report.errors) or "data quality is unusable", **detail)
    if not allow_degraded and report.status is DataQuality.DEGRADED:
        raise DataValidationError(
            "degraded data rejected by policy",
            warnings=list(report.warnings), **detail)
    if min_bars is not None and report.bars_present < min_bars:
        raise InsufficientDataError(
            f"{ERR_INSUFFICIENT}: {report.bars_present} bars, {min_bars} required",
            required=min_bars, **detail)
    return report


def assert_no_lookahead(candles: Iterable[Candle], as_of: datetime) -> None:
    """Raise if any bar could not have been known at `as_of`.

    The single most important check for backtest integrity. A bar is knowable
    only once it has *closed*, so the test is `ts_close > as_of` — not
    `ts_open`, which is the off-by-one-bar mistake that makes a backtest look
    prophetic. Non-final bars fail by construction, which is intended: an
    in-progress bar's close has not happened yet.
    """
    as_of = ensure_utc(as_of, field_name="as_of")
    for candle in candles:
        if candle.ts_close > as_of:
            raise DataValidationError(
                "look-ahead: candle closes after the decision time",
                instrument=candle.instrument_key, timeframe=candle.timeframe,
                ts_open=candle.ts_open.isoformat(),
                ts_close=candle.ts_close.isoformat(), as_of=as_of.isoformat(),
                is_final=candle.is_final)


def find_gaps(candles: Sequence[Candle], timeframe: str) -> list[tuple[datetime, datetime]]:
    """Missing `[start, end)` grid ranges between consecutive bars.

    Reported, never filled — callers that need continuous history must fetch
    the missing range, not interpolate across it.
    """
    delta = parse_timeframe(timeframe)
    ordered = sorted(candles, key=lambda c: c.ts_open)
    gaps: list[tuple[datetime, datetime]] = []
    for previous, current in zip(ordered, ordered[1:]):
        expected = previous.ts_open + delta
        if current.ts_open > expected:
            gaps.append((expected, current.ts_open))
    return gaps


__all__ = [
    "DEFAULT_MIN_COMPLETENESS", "DEFAULT_OUTLIER_SIGMA",
    "ERR_EMPTY", "ERR_INCOMPLETE", "ERR_INSUFFICIENT", "ERR_STALE",
    "assert_no_lookahead", "find_gaps", "floor_to_timeframe", "is_on_grid",
    "normalise_candles", "parse_timeframe", "require_usable",
    "timeframe_seconds", "validate_raw",
]

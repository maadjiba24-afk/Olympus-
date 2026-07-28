"""Append-only OHLCV history on top of the Olympus key-value store.

Why a file of newline-delimited JSON rather than a table, a binary format, or a
DataFrame on disk:

* **Auditable.** A backtest that produced a trade must be reproducible from the
  bytes that were on disk at the time. NDJSON is greppable, diffable, and
  readable a year later by a human with no Olympus installed — which is the
  same reason the ledger and trace files are JSON.
* **Zero dependencies.** Olympus has three required dependencies and none of
  them is a storage engine. Parquet would be faster and would also make
  `pip install olympus` drag in Arrow.
* **Line-granular damage.** A truncated write corrupts one line, not the file.
  `get_candles` skips a bad line and counts it; it never raises and never
  silently substitutes a plausible bar. Losing one bar loudly is recoverable;
  a store that refuses to open at all takes the whole system down with it.

Correctness properties this module owns
---------------------------------------
**Exact float round-trip.** `json.dumps` writes floats with `repr`, the
shortest representation that reads back bit-identical, so a close price
survives a store/load cycle unchanged. Anything else silently perturbs every
indicator computed downstream.

**Idempotent writes.** Bars are keyed by `ts_open`; re-putting a range
overwrites in place rather than duplicating. Backfills overlap by design (a
vendor's last bar is often revised), so an ingest loop that re-fetches the last
day must be a no-op, not a corruption.

**Only final bars are read back.** The in-progress bar is *stored* — its final
version replaces it later under the same `ts_open` — but `get_candles` filters
it out. A strategy that acts on a forming bar is a strategy with look-ahead
bias, and the safest place to block that is the boundary everything reads
through.

**Cross-process safety.** The KV store defines a blind `put` as whole-value
replace (ADR 0005), so every read-modify-write here is wrapped in
`proclock.lock()`. Without it, a heartbeat ingesting 1m bars and a CLI backfill
running concurrently would each write a merge of the file they read, and one of
them would lose everything the other added.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Iterable, Iterator, Sequence

from .. import proclock, store
from .contracts import SCHEMA_VERSION, Candle, ensure_utc
from .errors import DataValidationError

#: KV namespace prefix. One namespace per (instrument, timeframe) pair keeps a
#: 1m stream from ever being merged with a 1d stream by a typo'd key.
NAMESPACE_PREFIX = "trading.candles"
#: The single blob inside each pair's namespace.
_BLOB_KEY = "candles.ndjson"
#: `instruments()` needs its own catalogue namespace: the KV interface can list
#: keys within a namespace but has no way to enumerate namespaces.

_SLUG_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _pair_id(instrument_key: str, timeframe: str) -> str:
    """Filesystem-safe identity for a (instrument, timeframe) pair.

    A readable slug for humans browsing MEMORY_DIR plus a hash suffix for
    correctness: the store sanitises and truncates keys, so two long or
    similar instrument keys could otherwise collide and silently share a
    history file.
    """
    raw = f"{instrument_key}|{timeframe}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    slug = _SLUG_RE.sub("_", raw)[:40].strip("_") or "pair"
    return f"{slug}.{digest}"


class CandleStore:
    """Persistent, append-only OHLCV history.

    Reads are sorted-ascending, deduplicated, and final-bars-only regardless of
    what is physically on disk, so a caller never has to defend against a
    partially-merged or out-of-order file.
    """

    def __init__(self, *, namespace: str = NAMESPACE_PREFIX, backend=None):
        self._prefix = str(namespace)
        # Held only when injected (tests / an alternative store). Otherwise the
        # backend is resolved per call so a process that reconfigures
        # MEMORY_DIR — every test does, via conftest — is not talking to a
        # backend pinned to the old directory.
        self._backend = backend
        #: Cumulative count of unparseable lines skipped by this instance.
        #: Surfaced rather than swallowed: a store quietly dropping lines is a
        #: silent data-loss bug, and this is the number that makes it visible.
        self.corrupt_lines = 0

    # -- plumbing ----------------------------------------------------------
    def _store(self):
        return self._backend if self._backend is not None else store.backend()

    def _ns(self, instrument_key: str, timeframe: str) -> str:
        return f"{self._prefix}.{_pair_id(instrument_key, timeframe)}"

    def _lock_name(self, instrument_key: str, timeframe: str) -> str:
        return f"trading-candles-{_pair_id(instrument_key, timeframe)}"

    def _index_ns(self) -> str:
        return f"{self._prefix}.index"

    # -- serialisation -----------------------------------------------------
    @staticmethod
    def _encode(candle: Candle) -> str:
        """One NDJSON record. `Candle.to_dict()` already renders timestamps as
        ISO-8601 UTC and floats as floats; `json.dumps` then writes each float
        via `repr`, which round-trips exactly."""
        record = candle.to_dict()
        record["v"] = SCHEMA_VERSION      # so old files stay interpretable
        return json.dumps(record, sort_keys=True)

    @staticmethod
    def _decode(line: str) -> Candle:
        """Rebuild a Candle from a record. Raises on anything malformed; the
        caller turns that into a skipped line, never a crash."""
        record = json.loads(line)
        if not isinstance(record, dict):
            raise ValueError("candle record is not an object")
        record.pop("v", None)
        # Unknown keys are dropped rather than fatal: a file written by a newer
        # schema should degrade to "the fields we understand", not to an
        # unreadable history.
        fields = {k: v for k, v in record.items()
                  if k in Candle.__dataclass_fields__}
        for name in ("ts_open", "ts_close"):
            value = fields.get(name)
            if not isinstance(value, str):
                raise ValueError(f"candle record missing {name}")
            # fromisoformat here (not a parser lib) accepts exactly what
            # `datetime.isoformat()` emits, which is exactly what we wrote.
            fields[name] = datetime.fromisoformat(value)
        return Candle(**fields)

    def _read(self, instrument_key: str, timeframe: str) -> dict[str, Candle]:
        """Load the pair's history as ``{iso ts_open: Candle}``, keeping the
        LAST occurrence of each timestamp (later lines are revisions) and
        skipping damaged ones."""
        blob = self._store().get(self._ns(instrument_key, timeframe), _BLOB_KEY)
        if not blob:
            return {}
        rows: dict[str, Candle] = {}
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError:
            # The whole blob is unreadable bytes: count it once and start over
            # rather than propagating a decode error into a trading loop.
            self.corrupt_lines += 1
            return {}
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                candle = self._decode(line)
            except (ValueError, TypeError, KeyError, DataValidationError):
                # Truncated tail, hand-edited file, partially-written line:
                # skip it, count it, keep the rest of the history usable.
                self.corrupt_lines += 1
                continue
            rows[candle.ts_open.isoformat()] = candle
        return rows

    def _write(self, instrument_key: str, timeframe: str,
               rows: dict[str, Candle]) -> None:
        """Serialise the whole history back, sorted by open time.

        A full rewrite rather than a true append because the KV contract is
        whole-value replace; NDJSON keeps the merge O(n) in lines and the file
        readable, which is the trade this module explicitly accepts.
        """
        ordered = [rows[k] for k in sorted(rows)]
        payload = "".join(self._encode(c) + "\n" for c in ordered)
        self._store().put(self._ns(instrument_key, timeframe), _BLOB_KEY,
                          payload.encode("utf-8"))

    def _index(self, instrument_key: str, timeframe: str) -> None:
        pair = _pair_id(instrument_key, timeframe)
        self._store().put(
            self._index_ns(), pair,
            json.dumps({"instrument_key": instrument_key,
                        "timeframe": timeframe}).encode("utf-8"))

    # -- writing -----------------------------------------------------------
    def put_candles(self, candles: Sequence[Candle]) -> int:
        """Persist bars, deduplicating on ``ts_open`` (last one wins).

        Returns the number of rows this call actually added or changed — a
        re-put of identical bars returns 0, which is what makes idempotency
        observable to an ingest loop rather than merely claimed.

        Mixed instruments and timeframes in one call are fine; they are grouped
        and each group is merged under its own lock.
        """
        if candles is None:
            return 0
        grouped: dict[tuple[str, str], list[Candle]] = {}
        for candle in candles:
            if not isinstance(candle, Candle):
                raise DataValidationError(
                    "put_candles expects Candle contracts",
                    got=type(candle).__name__)
            grouped.setdefault((candle.instrument_key, candle.timeframe),
                               []).append(candle)

        written = 0
        for (instrument_key, timeframe), batch in grouped.items():
            # The lock spans read→merge→write: two processes backfilling the
            # same series must serialise, or the loser's bars vanish.
            with proclock.lock(self._lock_name(instrument_key, timeframe)):
                rows = self._read(instrument_key, timeframe)
                changed = False
                for candle in batch:
                    stamp = candle.ts_open.isoformat()
                    existing = rows.get(stamp)
                    if existing is not None and existing == candle:
                        continue          # byte-identical: not a change
                    rows[stamp] = candle
                    written += 1
                    changed = True
                if changed:
                    self._write(instrument_key, timeframe, rows)
                # Indexed even when nothing changed, so a lost or hand-deleted
                # index entry heals on the next write attempt.
                self._index(instrument_key, timeframe)
        return written

    def drop(self, instrument_key: str, timeframe: str) -> None:
        """Delete a series outright.

        The write path is append-only; this is the operator/retention escape
        hatch (and how tests reset), kept explicit so no ingest code can reach
        it by accident.
        """
        with proclock.lock(self._lock_name(instrument_key, timeframe)):
            self._store().delete(self._ns(instrument_key, timeframe), _BLOB_KEY)
            self._store().delete(self._index_ns(),
                                 _pair_id(instrument_key, timeframe))

    # -- reading -----------------------------------------------------------
    def get_candles(self, instrument_key: str, timeframe: str,
                    start: datetime | None = None,
                    end: datetime | None = None,
                    limit: int | None = None) -> list[Candle]:
        """Final bars for a series, ascending by ``ts_open``.

        `start`/`end` bound ``ts_open`` half-open — ``[start, end)`` — the same
        convention as the bars themselves, so consecutive queries tile the
        timeline without overlapping or dropping a bar.

        `limit` returns the **most recent** N bars in the range (still
        ascending), because every caller that limits wants "the last N bars of
        context", never the oldest N.
        """
        if start is not None:
            start = ensure_utc(start, field_name="start")
        if end is not None:
            end = ensure_utc(end, field_name="end")
        if start is not None and end is not None and end < start:
            raise DataValidationError("end must not precede start",
                                      start=start.isoformat(),
                                      end=end.isoformat())
        rows = self._read(instrument_key, timeframe)
        out: list[Candle] = []
        for key in sorted(rows):
            candle = rows[key]
            if not candle.is_final:
                continue              # never hand out a forming bar
            if start is not None and candle.ts_open < start:
                continue
            if end is not None and candle.ts_open >= end:
                continue
            out.append(candle)
        if limit is not None:
            if int(limit) < 0:
                raise DataValidationError("limit must be >= 0", limit=limit)
            out = out[-int(limit):] if int(limit) else []
        return out

    def latest(self, instrument_key: str, timeframe: str) -> Candle | None:
        """The newest final bar, or None when the series is empty."""
        rows = [c for c in self._read(instrument_key, timeframe).values()
                if c.is_final]
        if not rows:
            return None
        return max(rows, key=lambda c: c.ts_open)

    def count(self, instrument_key: str, timeframe: str) -> int:
        """Number of final bars stored — consistent with `get_candles`, so
        `count() == len(get_candles())` always holds."""
        return sum(1 for c in self._read(instrument_key, timeframe).values()
                   if c.is_final)

    def instruments(self) -> list[tuple[str, str]]:
        """Every ``(instrument_key, timeframe)`` series known to this store."""
        backend = self._store()
        index_ns = self._index_ns()
        pairs: list[tuple[str, str]] = []
        for key in backend.keys(index_ns):
            blob = backend.get(index_ns, key)
            if not blob:
                continue
            try:
                record = json.loads(blob.decode("utf-8"))
                pairs.append((str(record["instrument_key"]),
                              str(record["timeframe"])))
            except (ValueError, KeyError, UnicodeDecodeError):
                self.corrupt_lines += 1     # a damaged index entry, not a crash
                continue
        return sorted(set(pairs))

    def integrity(self, instrument_key: str, timeframe: str) -> dict:
        """Cheap health report for a series: line counts, damage, and span.

        Exists so an operator can answer "is this history intact?" without
        parsing MEMORY_DIR by hand, and so ingest can alarm on `corrupt` > 0.
        """
        before = self.corrupt_lines
        rows = self._read(instrument_key, timeframe)
        final = [c for c in rows.values() if c.is_final]
        opens = sorted(c.ts_open for c in final)
        return {
            "instrument_key": instrument_key,
            "timeframe": timeframe,
            "rows": len(rows),
            "final": len(final),
            "corrupt": self.corrupt_lines - before,
            "oldest": opens[0].isoformat() if opens else None,
            "newest": opens[-1].isoformat() if opens else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"CandleStore(namespace={self._prefix!r})"


_DEFAULT: CandleStore | None = None


def default_store() -> CandleStore:
    """Process-wide store instance. Stateless apart from its damage counter, so
    sharing one is safe; callers that want an isolated counter build their own.
    """
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = CandleStore()
    return _DEFAULT


def iter_candles(candles: Iterable[Candle]) -> Iterator[Candle]:
    """Yield candles ascending by ``ts_open``, deduplicated (last wins).

    The same normalisation `get_candles` applies, exposed for callers holding
    in-memory bars from a feed so live and stored paths agree bar-for-bar.
    """
    rows: dict[datetime, Candle] = {}
    for candle in candles:
        rows[candle.ts_open] = candle
    for stamp in sorted(rows):
        yield rows[stamp]


__all__ = ["CandleStore", "NAMESPACE_PREFIX", "default_store", "iter_candles"]

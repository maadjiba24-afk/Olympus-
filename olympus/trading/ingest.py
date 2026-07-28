"""Market-data ingestion.

The layer between a real venue's feed and the validated `Candle`s the rest of
the domain consumes. Everything downstream — features, forecasts, risk — trusts
that what reaches it actually happened, at the time it says it happened, so this
module's job is to be paranoid on their behalf.

Three timestamps, not one
-------------------------
Every ingested bar carries the venue's `ts_open`/`ts_close` *and* a local
`received_at`. They answer different questions: the venue's clock says when the
bar happened, ours says when we learned about it, and the difference is the only
way to distinguish "the market is quiet" from "our feed died twenty minutes ago".
A system that keeps only the venue timestamp cannot tell those apart, and will
happily trade a twenty-minute-old book.

Never fabricate
---------------
No gap is ever filled, no bar is ever interpolated, no price is ever carried
forward. A hole stays a hole and is reported; `validate.normalise_candles` then
decides whether what survived is usable, and the pipeline abstains if it is not.
The temptation to forward-fill is strong precisely because it makes downstream
code simpler — and it is how a stale price becomes a trade.

Streaming and reconnection
--------------------------
A stream that drops is normal. What must not happen is resuming as though
nothing was missed, so `IngestionService` records the last bar it saw per
series, and on reconnect backfills from there via REST before resuming the
stream. The joint is deduplicated on `ts_open`, keeping the later (REST) copy,
because a bar that arrived twice is a bar we can check.

Dependencies
------------
Stdlib only for REST (`urllib.request`). Streaming uses `websockets`, which is
already a declared optional extra; it is imported lazily and its absence raises
`DependencyMissing` rather than `ImportError`. The core of this module —
sequencing, gap detection, dedupe, persistence, quality — has no third-party
dependency at all and is exercised by the replay tests.
"""

from __future__ import annotations

import json
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from .clock import Clock, default_clock
from .contracts import (Candle, DataQuality, DataQualityReport, Quote,
                        ensure_utc, jsonable)
from .errors import (ConfigurationError, DataValidationError, DependencyMissing,
                     MarketDataError, StaleDataError)
from .instruments import floor_to_timeframe, parse_timeframe, timeframe_seconds

_RAW_NS = "trading.ingest.raw"
_STATE_NS = "trading.ingest.state"


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IngestedCandle:
    """A candle plus the provenance the venue does not give us.

    `received_at` is ours; `source_ts` is whatever the venue stamped the message
    with. Keeping both is what makes feed latency measurable instead of
    guessable.
    """
    candle: Candle
    received_at: datetime
    source: str
    source_ts: datetime | None = None
    #: Venue sequence/update id when the protocol has one. Gap detection on a
    #: sequence number is exact; gap detection on timestamps is inference.
    sequence: int | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "received_at",
                           ensure_utc(self.received_at, field_name="received_at"))
        if self.source_ts is not None:
            object.__setattr__(self, "source_ts",
                               ensure_utc(self.source_ts, field_name="source_ts"))
        object.__setattr__(self, "raw", dict(self.raw))

    @property
    def latency_s(self) -> float | None:
        """How long after the bar closed we learned about it."""
        return (self.received_at - self.candle.ts_close).total_seconds()

    def to_dict(self) -> dict:
        return {"candle": self.candle.to_dict(),
                "received_at": jsonable(self.received_at),
                "source": self.source, "source_ts": jsonable(self.source_ts),
                "sequence": self.sequence, "raw": jsonable(self.raw)}


@dataclass(frozen=True)
class FeedHealth:
    """What an operator needs to decide whether the feed can be trusted."""
    series: str
    connected: bool
    last_message_at: datetime | None = None
    last_bar_close: datetime | None = None
    age_s: float | None = None
    gaps_detected: int = 0
    duplicates_dropped: int = 0
    out_of_order_dropped: int = 0
    reconnects: int = 0
    messages: int = 0
    errors: tuple[str, ...] = ()
    quality: DataQuality = DataQuality.UNUSABLE

    def to_dict(self) -> dict:
        return {"series": self.series, "connected": self.connected,
                "last_message_at": jsonable(self.last_message_at),
                "last_bar_close": jsonable(self.last_bar_close),
                "age_s": self.age_s, "gaps_detected": self.gaps_detected,
                "duplicates_dropped": self.duplicates_dropped,
                "out_of_order_dropped": self.out_of_order_dropped,
                "reconnects": self.reconnects, "messages": self.messages,
                "errors": list(self.errors), "quality": self.quality.value}


# ---------------------------------------------------------------------------
# the provider contract
# ---------------------------------------------------------------------------

class MarketDataProvider(ABC):
    """One venue's market data, normalised to Olympus contracts.

    Two capabilities, deliberately separate: `backfill` is a pull with an exact
    range (used for history and for closing a gap after a disconnect), `stream`
    is a push that runs until stopped. A provider may implement only backfill;
    `supports_streaming` says which.
    """

    name: str = "abstract"
    venue: str = "none"
    supports_streaming: bool = False

    @abstractmethod
    def backfill(self, instrument_key: str, timeframe: str, *,
                 start: datetime, end: datetime,
                 limit: int | None = None) -> list[IngestedCandle]:
        """Closed bars in `[start, end)`, ascending, no gaps invented."""

    def stream(self, instrument_key: str, timeframe: str, *,
               on_candle: Callable[[IngestedCandle], None],
               stop: threading.Event | None = None) -> None:
        raise DependencyMissing(
            f"{self.name} does not implement streaming", provider=self.name)

    def get_quote(self, instrument_key: str) -> Quote | None:
        return None

    def health(self) -> dict:
        return {"name": self.name, "venue": self.venue}

    # -- helpers for subclasses ------------------------------------------

    @staticmethod
    def _http_json(url: str, *, timeout: float = 15.0,
                   headers: Mapping[str, str] | None = None) -> Any:
        """GET JSON over the stdlib. No third-party HTTP dependency.

        Deliberately minimal: providers here only need unauthenticated GETs, and
        pulling in a client library for that would add a required dependency to
        a project whose whole footprint is three.
        """
        import urllib.error
        import urllib.request
        request = urllib.request.Request(
            url, headers={"Accept": "application/json",
                          "User-Agent": "olympus-trading/1.0",
                          **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise MarketDataError(
                "market-data provider returned an HTTP error",
                url=url, status=exc.code,
                body=exc.read()[:400].decode("utf-8", "replace")) from exc
        except Exception as exc:                         # noqa: BLE001
            # Includes the proxy's CONNECT-level policy denial, which surfaces
            # as a URLError rather than an HTTPError.
            raise MarketDataError("market-data provider unreachable",
                                  url=url, error=str(exc)) from exc


class ReplayProvider(MarketDataProvider):
    """Serves recorded bars. The deterministic half of every ingestion test.

    Exists so the whole ingestion pipeline — sequencing, gaps, dedupe,
    reconnection, persistence — can be tested exactly, offline, without a
    network. It is not a simulator: it replays bytes that a real venue produced
    (or that a fixture defines), so a test failure means the pipeline is wrong
    rather than the fake being unrealistic.
    """

    name = "replay"
    venue = "replay"
    supports_streaming = True

    def __init__(self, candles: Sequence[IngestedCandle] | Sequence[Candle], *,
                 clock: Clock | None = None, source: str = "replay",
                 drop_after: int | None = None):
        self.clock = clock or default_clock()
        self.source = source
        #: Simulate a mid-stream disconnect after N pushes, so reconnect and
        #: gap-backfill are testable rather than assumed.
        self.drop_after = drop_after
        self._records: list[IngestedCandle] = []
        for item in candles:
            if isinstance(item, IngestedCandle):
                self._records.append(item)
            else:
                self._records.append(IngestedCandle(
                    candle=item, received_at=item.ts_close, source=source))
        self._records.sort(key=lambda r: r.candle.ts_open)
        self.stream_calls = 0

    def backfill(self, instrument_key, timeframe, *, start, end, limit=None):
        start = ensure_utc(start, field_name="start")
        end = ensure_utc(end, field_name="end")
        out = [r for r in self._records
               if r.candle.instrument_key == instrument_key
               and r.candle.timeframe == timeframe
               and start <= r.candle.ts_open < end]
        return out[:limit] if limit else out

    def stream(self, instrument_key, timeframe, *, on_candle, stop=None):
        self.stream_calls += 1
        pushed = 0
        for record in self._records:
            if stop is not None and stop.is_set():
                return
            if record.candle.instrument_key != instrument_key:
                continue
            if record.candle.timeframe != timeframe:
                continue
            if self.drop_after is not None and pushed >= self.drop_after:
                raise MarketDataError("simulated stream disconnect",
                                      provider=self.name)
            on_candle(record)
            pushed += 1


# ---------------------------------------------------------------------------
# Binance spot
# ---------------------------------------------------------------------------

_BINANCE_INTERVALS = {
    60: "1m", 180: "3m", 300: "5m", 900: "15m", 1800: "30m",
    3600: "1h", 7200: "2h", 14400: "4h", 21600: "6h", 28800: "8h",
    43200: "12h", 86400: "1d", 259200: "3d", 604800: "1w",
}


class BinanceSpotProvider(MarketDataProvider):
    """Binance spot market data.

    Chosen as the first real vertical because its market-data endpoints require
    **no credentials at all** — so the only thing standing between this code and
    live data is network reachability, not key provisioning — and because spot
    crypto trades 24/7, which removes the session-calendar edge cases that make
    an equity feed's first version wrong.

    ⚠️ **This class has never successfully connected in this environment.**
    Every Binance host is refused by the egress policy with HTTP 403 at CONNECT
    (see docs/TRADING_STATUS.md). It is written against Binance's documented
    REST/WS contract and exercised by fixture-replay tests that assert the
    parsing, not by an integration test against the venue. Treat it as
    unverified against the real API until someone runs it with egress.
    """

    name = "binance-spot"
    venue = "BINANCE"
    supports_streaming = True

    REST_BASE = "https://api.binance.com"
    WS_BASE = "wss://stream.binance.com:9443/ws"
    #: Binance caps a klines page at 1000 rows.
    MAX_PAGE = 1000

    def __init__(self, *, clock: Clock | None = None,
                 rest_base: str | None = None, ws_base: str | None = None,
                 request_timeout: float = 15.0):
        self.clock = clock or default_clock()
        self.rest_base = (rest_base or self.REST_BASE).rstrip("/")
        self.ws_base = (ws_base or self.WS_BASE).rstrip("/")
        self.request_timeout = request_timeout

    # -- symbol / interval mapping ---------------------------------------

    @staticmethod
    def to_symbol(instrument_key: str) -> str:
        """`BINANCE:BTCUSDT` -> `BTCUSDT`."""
        return instrument_key.split(":", 1)[-1].replace("-", "").upper()

    @classmethod
    def to_interval(cls, timeframe: str) -> str:
        seconds = timeframe_seconds(timeframe)
        interval = _BINANCE_INTERVALS.get(seconds)
        if interval is None:
            raise ConfigurationError(
                "Binance does not offer this timeframe",
                timeframe=timeframe,
                supported=sorted(_BINANCE_INTERVALS.values()))
        return interval

    # -- parsing ----------------------------------------------------------

    def parse_kline_row(self, row: Sequence[Any], instrument_key: str,
                        timeframe: str, *, received_at: datetime) -> IngestedCandle:
        """One REST kline row -> `IngestedCandle`.

        Binance's `closeTime` is the last *millisecond* of the bar
        (open + interval − 1ms), not the next bar's open. Using it verbatim as
        `ts_close` would make every bar one millisecond short and put it off the
        timeframe grid, so it is normalised to the exclusive close.
        """
        open_ms = int(row[0])
        ts_open = datetime.fromtimestamp(open_ms / 1000, tz=timezone.utc)
        ts_close = ts_open + parse_timeframe(timeframe)
        return IngestedCandle(
            candle=Candle(
                instrument_key=instrument_key, timeframe=timeframe,
                ts_open=ts_open, ts_close=ts_close,
                open=float(row[1]), high=float(row[2]), low=float(row[3]),
                close=float(row[4]), volume=float(row[5]),
                amount=float(row[7]) if len(row) > 7 else 0.0,
                is_final=True, source=self.name),
            received_at=received_at, source=self.name,
            source_ts=datetime.fromtimestamp(int(row[6]) / 1000, tz=timezone.utc),
            raw={"open_time": open_ms,
                 "trades": int(row[8]) if len(row) > 8 else None})

    def parse_ws_kline(self, message: Mapping[str, Any], *,
                       received_at: datetime) -> IngestedCandle | None:
        """One websocket kline event -> `IngestedCandle`, or None.

        Returns None for an in-progress bar (`k.x` false). Emitting unclosed
        bars would hand the strategy a close that has not happened yet — the
        streaming form of look-ahead.
        """
        k = message.get("k") or {}
        if not k.get("x"):
            return None
        symbol = str(k.get("s", "")).upper()
        interval = str(k.get("i", ""))
        timeframe = next((tf for sec, tf in _BINANCE_INTERVALS.items()
                          if tf == interval), interval)
        ts_open = datetime.fromtimestamp(int(k["t"]) / 1000, tz=timezone.utc)
        return IngestedCandle(
            candle=Candle(
                instrument_key=f"{self.venue}:{symbol}", timeframe=timeframe,
                ts_open=ts_open, ts_close=ts_open + parse_timeframe(timeframe),
                open=float(k["o"]), high=float(k["h"]), low=float(k["l"]),
                close=float(k["c"]), volume=float(k["v"]),
                amount=float(k.get("q", 0.0)), is_final=True, source=self.name),
            received_at=received_at, source=self.name,
            source_ts=datetime.fromtimestamp(int(message["E"]) / 1000,
                                             tz=timezone.utc)
            if message.get("E") else None,
            sequence=int(k["L"]) if k.get("L") is not None else None,
            raw={"symbol": symbol, "interval": interval})

    # -- provider API ------------------------------------------------------

    def backfill(self, instrument_key, timeframe, *, start, end, limit=None):
        start = ensure_utc(start, field_name="start")
        end = ensure_utc(end, field_name="end")
        if end <= start:
            raise ConfigurationError("backfill end must be after start")
        symbol = self.to_symbol(instrument_key)
        interval = self.to_interval(timeframe)
        step = parse_timeframe(timeframe)

        out: list[IngestedCandle] = []
        cursor = start
        while cursor < end:
            page = min(self.MAX_PAGE, limit - len(out) if limit else self.MAX_PAGE)
            if page <= 0:
                break
            url = (f"{self.rest_base}/api/v3/klines?symbol={symbol}"
                   f"&interval={interval}"
                   f"&startTime={int(cursor.timestamp() * 1000)}"
                   f"&endTime={int(end.timestamp() * 1000) - 1}"
                   f"&limit={page}")
            rows = self._http_json(url, timeout=self.request_timeout)
            if not rows:
                break                                    # genuinely no data
            received = self.clock.now()
            for row in rows:
                record = self.parse_kline_row(row, instrument_key, timeframe,
                                              received_at=received)
                if record.candle.ts_open >= end:
                    continue
                out.append(record)
            last_open = datetime.fromtimestamp(int(rows[-1][0]) / 1000,
                                               tz=timezone.utc)
            nxt = last_open + step
            if nxt <= cursor:                            # no forward progress
                break
            cursor = nxt
            if limit and len(out) >= limit:
                break
        out.sort(key=lambda r: r.candle.ts_open)
        return out[:limit] if limit else out

    def stream(self, instrument_key, timeframe, *, on_candle, stop=None):
        """Consume the venue's kline websocket until `stop` is set.

        `websockets` is a declared optional extra, imported here so the rest of
        ingestion works without it.
        """
        try:
            import websockets  # noqa: F401
            from websockets.sync.client import connect
        except Exception as exc:                         # noqa: BLE001
            raise DependencyMissing(
                "streaming market data needs the 'websockets' extra",
                package="websockets", extra="browser") from exc

        symbol = self.to_symbol(instrument_key).lower()
        interval = self.to_interval(timeframe)
        url = f"{self.ws_base}/{symbol}@kline_{interval}"
        with connect(url, open_timeout=self.request_timeout) as socket:
            while stop is None or not stop.is_set():
                try:
                    raw = socket.recv(timeout=self.request_timeout)
                except TimeoutError:
                    continue
                message = json.loads(raw)
                record = self.parse_ws_kline(message, received_at=self.clock.now())
                if record is not None:
                    on_candle(record)

    def get_quote(self, instrument_key):
        symbol = self.to_symbol(instrument_key)
        data = self._http_json(
            f"{self.rest_base}/api/v3/ticker/bookTicker?symbol={symbol}",
            timeout=self.request_timeout)
        return Quote(instrument_key=instrument_key, ts=self.clock.now(),
                     bid=float(data["bidPrice"]), ask=float(data["askPrice"]),
                     bid_size=float(data.get("bidQty", 0.0)),
                     ask_size=float(data.get("askQty", 0.0)), source=self.name)

    def health(self):
        try:
            self._http_json(f"{self.rest_base}/api/v3/ping",
                            timeout=self.request_timeout)
            return {"name": self.name, "venue": self.venue, "ok": True}
        except MarketDataError as exc:
            return {"name": self.name, "venue": self.venue, "ok": False,
                    "error": exc.message, "details": exc.details}


# ---------------------------------------------------------------------------
# the service
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IngestionConfig:
    timeframe: str = "1m"
    #: A series older than this is not tradeable. Passed to the quality report;
    #: the risk engine enforces its own limit independently.
    max_age_s: float = 300.0
    backfill_bars: int = 500
    max_reconnects: int = 10
    reconnect_backoff_s: float = 1.0
    max_reconnect_backoff_s: float = 60.0
    #: Persist the raw provider payloads alongside the normalised bars. Costs
    #: disk; buys the ability to re-derive history after a parsing bug, which
    #: is otherwise unrecoverable.
    persist_raw: bool = True


class IngestionService:
    """Turns a provider's output into validated, persisted, gap-aware history.

    Owns the state a feed needs to be trustworthy across a restart: the last bar
    seen per series, the counters, and the raw record. The service is the only
    thing that writes to the candle store, so there is one place where "what we
    believe the market did" is decided.
    """

    def __init__(self, *, provider: MarketDataProvider, store=None,
                 config: IngestionConfig | None = None,
                 clock: Clock | None = None, audit=None,
                 namespace: str = _STATE_NS, raw_namespace: str = _RAW_NS):
        self.provider = provider
        self.config = config or IngestionConfig()
        self.clock = clock or default_clock()
        self.audit = audit
        self.namespace = namespace
        self.raw_namespace = raw_namespace
        if store is None:
            from .storage import CandleStore
            store = CandleStore()
        self.store = store
        self._stats: dict[str, dict[str, Any]] = {}
        self._stop = threading.Event()

    # -- state ------------------------------------------------------------

    def _series_key(self, instrument_key: str, timeframe: str) -> str:
        return f"{instrument_key}|{timeframe}"

    def _stat(self, series: str) -> dict[str, Any]:
        return self._stats.setdefault(series, {
            "connected": False, "messages": 0, "gaps": 0, "duplicates": 0,
            "out_of_order": 0, "reconnects": 0, "errors": [],
            "last_message_at": None, "last_bar_close": None,
            "last_bar_open": None, "last_sequence": None})

    def _kv(self):
        from olympus import store
        return store.backend()

    def _save_state(self, series: str) -> None:
        stat = self._stat(series)
        payload = {"last_bar_open": jsonable(stat["last_bar_open"]),
                   "last_bar_close": jsonable(stat["last_bar_close"]),
                   "last_sequence": stat["last_sequence"],
                   "gaps": stat["gaps"], "duplicates": stat["duplicates"],
                   "out_of_order": stat["out_of_order"],
                   "reconnects": stat["reconnects"]}
        self._kv().put(self.namespace, series,
                       json.dumps(payload, sort_keys=True).encode("utf-8"))

    def load_state(self, instrument_key: str, timeframe: str) -> dict | None:
        """Restore counters and the resume point after a restart.

        Without this a restarted process would either re-request everything or,
        worse, resume from *now* and leave a silent hole where the downtime was.
        """
        series = self._series_key(instrument_key, timeframe)
        raw = self._kv().get(self.namespace, series)
        if not raw:
            return None
        data = json.loads(raw.decode("utf-8"))
        stat = self._stat(series)
        for key in ("gaps", "duplicates", "out_of_order", "reconnects"):
            stat[key] = int(data.get(key, 0))
        stat["last_sequence"] = data.get("last_sequence")
        for key in ("last_bar_open", "last_bar_close"):
            value = data.get(key)
            stat[key] = datetime.fromisoformat(value) if value else None
        return data

    # -- ingestion --------------------------------------------------------

    def backfill(self, instrument_key: str, timeframe: str, *,
                 start: datetime | None = None,
                 end: datetime | None = None,
                 bars: int | None = None) -> list[Candle]:
        """Pull history and persist it. Returns the accepted bars."""
        step = parse_timeframe(timeframe)
        end = ensure_utc(end or self.clock.now(), field_name="end")
        if start is None:
            count = bars or self.config.backfill_bars
            start = floor_to_timeframe(end, timeframe) - step * count
        start = ensure_utc(start, field_name="start")
        records = self.provider.backfill(instrument_key, timeframe,
                                         start=start, end=end, limit=bars)
        return self._accept(instrument_key, timeframe, records)

    def _accept(self, instrument_key: str, timeframe: str,
                records: Sequence[IngestedCandle]) -> list[Candle]:
        """Sequence, dedupe, gap-check and persist. The single write path."""
        series = self._series_key(instrument_key, timeframe)
        stat = self._stat(series)
        step = parse_timeframe(timeframe)
        accepted: list[Candle] = []

        for record in sorted(records, key=lambda r: r.candle.ts_open):
            candle = record.candle
            stat["messages"] += 1
            stat["last_message_at"] = record.received_at

            if candle.instrument_key != instrument_key or candle.timeframe != timeframe:
                continue
            if not candle.is_final:
                continue                                  # never emit a live bar

            last_open = stat["last_bar_open"]
            if last_open is not None:
                if candle.ts_open == last_open:
                    stat["duplicates"] += 1
                    continue
                if candle.ts_open < last_open:
                    # Late arrival for a bar we already published. Counted and
                    # dropped: a published bar is immutable, and silently
                    # rewriting history is worse than a visible late count.
                    stat["out_of_order"] += 1
                    continue
                missing = int((candle.ts_open - last_open) / step) - 1
                if missing > 0:
                    # A hole. Recorded, never filled.
                    stat["gaps"] += missing

            if record.sequence is not None and stat["last_sequence"] is not None:
                if record.sequence <= stat["last_sequence"]:
                    stat["duplicates"] += 1
                    continue
            if record.sequence is not None:
                stat["last_sequence"] = record.sequence

            stat["last_bar_open"] = candle.ts_open
            stat["last_bar_close"] = candle.ts_close
            accepted.append(candle)
            if self.config.persist_raw and record.raw:
                self._persist_raw(series, record)

        if accepted:
            self.store.put_candles(accepted)
            self._save_state(series)
            self._record_audit(instrument_key, timeframe, accepted)
        return accepted

    def _persist_raw(self, series: str, record: IngestedCandle) -> None:
        key = f"{series}|{int(record.candle.ts_open.timestamp())}"
        try:
            self._kv().put(self.raw_namespace, key,
                           json.dumps(record.to_dict(), sort_keys=True).encode())
        except Exception:                                # noqa: BLE001
            pass                                          # raw record is a bonus

    def _record_audit(self, instrument_key, timeframe, accepted) -> None:
        if self.audit is None:
            return
        try:
            self.audit.record(
                "MARKET_DATA_RECEIVED",
                {"instrument": instrument_key, "timeframe": timeframe,
                 "bars": len(accepted), "provider": self.provider.name,
                 "first": jsonable(accepted[0].ts_open),
                 "last": jsonable(accepted[-1].ts_close)},
                actor="ingest", subject=instrument_key, correlation_id="")
        except Exception:                                # noqa: BLE001
            pass

    # -- streaming --------------------------------------------------------

    def run_stream(self, instrument_key: str, timeframe: str, *,
                   stop: threading.Event | None = None,
                   sleeper: Callable[[float], None] | None = None) -> None:
        """Stream with reconnect-and-backfill. Blocks until stopped.

        On every reconnection the gap since the last bar we actually stored is
        closed by REST *before* the stream resumes. Resuming without that is how
        a feed silently loses the exact window in which something happened.
        """
        stop = stop or self._stop
        series = self._series_key(instrument_key, timeframe)
        stat = self._stat(series)
        sleep = sleeper or time.sleep
        backoff = self.config.reconnect_backoff_s

        while not stop.is_set():
            try:
                stat["connected"] = True
                self.provider.stream(
                    instrument_key, timeframe,
                    on_candle=lambda rec: self._accept(instrument_key,
                                                       timeframe, [rec]),
                    stop=stop)
                stat["connected"] = False
                return                                    # clean end of stream
            except Exception as exc:                      # noqa: BLE001
                stat["connected"] = False
                stat["errors"].append(str(exc)[:200])
                stat["reconnects"] += 1
                self._save_state(series)
                if stat["reconnects"] > self.config.max_reconnects:
                    raise MarketDataError(
                        "market-data stream exceeded its reconnect budget",
                        series=series, reconnects=stat["reconnects"],
                        last_error=str(exc)[:200]) from exc
                if stop.is_set():
                    return
                sleep(min(backoff, self.config.max_reconnect_backoff_s))
                backoff = min(backoff * 2, self.config.max_reconnect_backoff_s)
                self._backfill_gap(instrument_key, timeframe)

    def _backfill_gap(self, instrument_key: str, timeframe: str) -> None:
        series = self._series_key(instrument_key, timeframe)
        last_close = self._stat(series)["last_bar_close"]
        if last_close is None:
            return
        try:
            self.backfill(instrument_key, timeframe, start=last_close,
                          end=self.clock.now())
        except MarketDataError as exc:
            self._stat(series)["errors"].append(f"gap backfill failed: {exc}")

    def stop(self) -> None:
        """Ask a running stream to finish. Idempotent and safe from any thread."""
        self._stop.set()

    # -- health -----------------------------------------------------------

    def health(self, instrument_key: str, timeframe: str) -> FeedHealth:
        series = self._series_key(instrument_key, timeframe)
        stat = self._stat(series)
        last_close = stat["last_bar_close"]
        age = ((self.clock.now() - last_close).total_seconds()
               if last_close else None)
        if last_close is None:
            quality = DataQuality.UNUSABLE
        elif age is not None and age > self.config.max_age_s:
            quality = DataQuality.UNUSABLE
        elif stat["gaps"] or stat["out_of_order"]:
            quality = DataQuality.DEGRADED
        else:
            quality = DataQuality.OK
        return FeedHealth(
            series=series, connected=bool(stat["connected"]),
            last_message_at=stat["last_message_at"], last_bar_close=last_close,
            age_s=age, gaps_detected=stat["gaps"],
            duplicates_dropped=stat["duplicates"],
            out_of_order_dropped=stat["out_of_order"],
            reconnects=stat["reconnects"], messages=stat["messages"],
            errors=tuple(stat["errors"][-5:]), quality=quality)

    def require_fresh(self, instrument_key: str, timeframe: str) -> FeedHealth:
        """Raise unless the feed is fresh enough to trade on."""
        health = self.health(instrument_key, timeframe)
        if health.quality is DataQuality.UNUSABLE:
            raise StaleDataError(
                "market data is not fresh enough to trade on",
                series=health.series, age_s=health.age_s,
                max_age_s=self.config.max_age_s)
        return health


__all__ = ["IngestedCandle", "FeedHealth", "MarketDataProvider",
           "ReplayProvider", "BinanceSpotProvider", "IngestionConfig",
           "IngestionService"]

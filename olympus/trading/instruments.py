"""Instrument catalogue, trading sessions, and timeframe arithmetic.

Three concerns live here because every other layer depends on all three and
none of them may disagree with the others.

**The catalogue is a lookup, not a source of truth about the market.** An
`Instrument` carries the venue's arithmetic (tick size, lot size, minimum
notional); getting it wrong produces broker rejections, not bad P&L, which is
why the registry refuses to guess: an unknown symbol raises rather than
fabricating a default instrument with plausible-looking tick sizes.

**Sessions decide whether a timestamp is tradable at all.** A strategy that
computes a signal on a bar stamped 02:00 UTC Saturday and sizes a US equity
position from it has produced an order that cannot fill. Session logic is
timezone-real (`zoneinfo`, stdlib): "09:30 New York" is 13:30 UTC in summer and
14:30 UTC in winter, and code that hardcodes either one is wrong for half the
year. All boundaries are computed in local time and returned in UTC.

**Timeframe helpers are used everywhere**, so they are exact rather than
convenient. `floor_to_timeframe` uses integer-microsecond arithmetic against a
fixed epoch anchor — no float seconds, no drift — because bar alignment is the
join key between market data, features, forecasts, and fills. Two components
that disagree by one bar produce a look-ahead bug that no test of either
component alone can catch.

Mutation discipline
-------------------
`default_registry()` returns a *sealed* registry. Sealing is not stylistic:
instrument definitions are an execution parameter (they set the minimum order
size and the price grid), so nothing downstream of external content — a news
summariser, an LLM analysis pass, a strategy reading a config it fetched — may
edit the shared catalogue. Code that legitimately needs a different catalogue
calls `.copy()` and gets its own unsealed one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Iterator, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import Instrument, ensure_utc
from .errors import ConfigurationError

#: Anchor for sub-weekly bar alignment. Sub-daily and daily bars are aligned to
#: the Unix epoch, which is midnight UTC — the convention every venue and data
#: vendor uses for "the 00:00 daily bar".
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
#: Anchor for weekly bars: 1970-01-05 is a **Monday**. Aligning weeks to the
#: epoch itself would produce Thursday-opening weeks, which matches no venue.
_WEEK_EPOCH = datetime(1970, 1, 5, tzinfo=timezone.utc)

#: How far `next_open`/`next_close` will search before declaring the session
#: unsatisfiable. A session with no reachable trading day inside two years is a
#: misconfiguration (empty weekday set, holidays covering everything), and
#: reporting that loudly beats looping forever.
_SEARCH_HORIZON_DAYS = 732


# ---------------------------------------------------------------------------
# timeframes
# ---------------------------------------------------------------------------

_TIMEFRAME_RE = re.compile(r"^\s*(\d+)\s*([a-zA-Z]+)\s*$")

#: Suffix aliases. Deliberately *not* including months or years: their length
#: varies, so "1mo" has no fixed `timedelta` and silently approximating it with
#: 30 days would corrupt every bar boundary derived from it.
_UNIT_ALIASES = {
    "s": "s", "sec": "s", "secs": "s", "second": "s", "seconds": "s",
    "m": "m", "min": "m", "mins": "m", "minute": "m", "minutes": "m",
    "h": "h", "hr": "h", "hrs": "h", "hour": "h", "hours": "h",
    "d": "d", "day": "d", "days": "d",
    "w": "w", "wk": "w", "wks": "w", "week": "w", "weeks": "w",
}
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
#: Spelled out so the error says "unsupported" rather than "unparseable" — a
#: caller asking for monthly bars has a modelling problem, not a typo.
_CALENDAR_UNITS = {"mo", "mon", "month", "months", "y", "yr", "year", "years"}


def _split_timeframe(timeframe: str) -> tuple[int, str]:
    """``"5m"`` → ``(5, "m")``. The single parse point for every helper here,
    so an accepted spelling can never mean two different things."""
    if not isinstance(timeframe, str):
        raise ConfigurationError("timeframe must be a string",
                                 got=type(timeframe).__name__)
    match = _TIMEFRAME_RE.match(timeframe)
    if match is None:
        raise ConfigurationError(
            "timeframe must look like '<count><unit>', e.g. '5m', '1h', '1d'",
            timeframe=timeframe)
    count = int(match.group(1))
    raw_unit = match.group(2).lower()
    if count <= 0:
        raise ConfigurationError("timeframe count must be > 0",
                                 timeframe=timeframe)
    if raw_unit in _CALENDAR_UNITS:
        raise ConfigurationError(
            "calendar timeframes (months/years) have no fixed duration and are "
            "not supported; use days or weeks", timeframe=timeframe)
    unit = _UNIT_ALIASES.get(raw_unit)
    if unit is None:
        raise ConfigurationError(
            "unknown timeframe unit; expected one of s/m/h/d/w",
            timeframe=timeframe, unit=match.group(2))
    return count, unit


def parse_timeframe(timeframe: str) -> timedelta:
    """Parse ``"5m"`` / ``"1h"`` / ``"1d"`` / ``"30s"`` / ``"2w"`` to a timedelta.

    Case-insensitive; ``"M"`` therefore means *minutes*, never months. Calendar
    units are rejected explicitly (see `_CALENDAR_UNITS`).
    """
    count, unit = _split_timeframe(timeframe)
    return timedelta(seconds=count * _UNIT_SECONDS[unit])


def timeframe_seconds(timeframe: str) -> int:
    """Duration of one bar in whole seconds."""
    return int(parse_timeframe(timeframe).total_seconds())


def is_valid_timeframe(timeframe: Any) -> bool:
    """True when `parse_timeframe` would accept this string. Never raises."""
    try:
        parse_timeframe(timeframe)
    except ConfigurationError:
        return False
    return True


def floor_to_timeframe(when: datetime, timeframe: str) -> datetime:
    """The open time of the bar containing `when`.

    Exact: the arithmetic runs on `timedelta`'s integer microsecond counter, so
    there is no float rounding and no accumulated drift over long histories.
    Weeks anchor to Monday 00:00 UTC; everything else to the Unix epoch.
    """
    count, unit = _split_timeframe(timeframe)
    step = timedelta(seconds=count * _UNIT_SECONDS[unit])
    moment = ensure_utc(when, field_name="when")
    anchor = _WEEK_EPOCH if unit == "w" else _EPOCH
    # `timedelta // timedelta` is integer division that FLOORS, which is what
    # makes pre-epoch timestamps land on a bar open rather than one bar late.
    steps = (moment - anchor) // step
    return anchor + steps * step


def next_bar_open(when: datetime, timeframe: str) -> datetime:
    """Open time of the bar *after* the one containing `when`."""
    return floor_to_timeframe(when, timeframe) + parse_timeframe(timeframe)


def timeframe_bounds(when: datetime, timeframe: str) -> tuple[datetime, datetime]:
    """``(ts_open, ts_close)`` of the bar containing `when` — half-open."""
    opened = floor_to_timeframe(when, timeframe)
    return opened, opened + parse_timeframe(timeframe)


# ---------------------------------------------------------------------------
# trading sessions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MarketSession:
    """When a venue accepts orders, expressed in the venue's own timezone.

    Conventions, chosen so one dataclass covers every shape we need:

    * ``close_time > open_time`` — an ordinary intraday session.
    * ``close_time < open_time`` — a session that wraps midnight (futures-style
      overnight). It belongs to the weekday of its *open*.
    * ``close_time == open_time`` — a continuous 24-hour session. Combined with
      all seven weekdays and no holidays this is the crypto 24/7 case; its
      "closes" are the instantaneous day boundaries between back-to-back
      sessions, which is why `is_open` is true across them.

    Boundaries are computed by attaching the venue timezone to a local wall
    time and converting to UTC, so DST transitions move the UTC boundary rather
    than silently shifting the session.
    """

    name: str
    tz_name: str = "UTC"
    open_time: time = time(0, 0)
    close_time: time = time(0, 0)
    #: Monday=0 … Sunday=6, matching `date.weekday()`.
    weekdays: frozenset[int] = frozenset({0, 1, 2, 3, 4})
    #: Local calendar dates on which the session does not run at all. Half days
    #: are not modelled: an early close that is treated as a full day only ever
    #: causes an order to be *rejected*, never an unintended fill.
    holidays: frozenset[date] = frozenset()

    def __post_init__(self):
        if not self.name or not str(self.name).strip():
            raise ConfigurationError("session name must be non-empty")
        for attr in ("open_time", "close_time"):
            value = getattr(self, attr)
            if not isinstance(value, time):
                raise ConfigurationError(f"session {attr} must be a datetime.time",
                                         session=self.name,
                                         got=type(value).__name__)
            if value.tzinfo is not None:
                raise ConfigurationError(
                    f"session {attr} must be a naive local wall time; the "
                    "timezone comes from tz_name", session=self.name)
        try:
            ZoneInfo(self.tz_name)
        except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
            # Distinguish "this zone does not exist" from "this machine has no
            # zone database at all". Windows ships none, so every named zone
            # fails there — including UTC — and the sessions below are built at
            # module scope, which turned a missing package into `import
            # olympus.trading` failing outright. `tzdata` is declared as a
            # win32-scoped dependency; this message is for the installation
            # that somehow lacks it.
            try:
                ZoneInfo("UTC")
                database = ""
            except Exception:                            # noqa: BLE001
                database = ("this machine has no IANA time-zone database, so "
                            "no named zone resolves; install `tzdata`")
            raise ConfigurationError(
                database or "unknown timezone", session=self.name,
                tz_name=str(self.tz_name), reason=str(exc),
                tz_database_missing=bool(database)) from None
        days = frozenset(int(d) for d in self.weekdays)
        if not days:
            raise ConfigurationError("session must trade on at least one weekday",
                                     session=self.name)
        if any(d < 0 or d > 6 for d in days):
            raise ConfigurationError("weekdays must be 0 (Mon) … 6 (Sun)",
                                     session=self.name, weekdays=sorted(days))
        object.__setattr__(self, "weekdays", days)
        holidays = []
        for value in self.holidays:
            if isinstance(value, datetime):
                # A datetime here is ambiguous (which timezone's date?), so
                # collapse it explicitly rather than comparing a datetime to a
                # date and never matching.
                value = value.date()
            if not isinstance(value, date):
                raise ConfigurationError("holidays must be datetime.date values",
                                         session=self.name,
                                         got=type(value).__name__)
            holidays.append(value)
        object.__setattr__(self, "holidays", frozenset(holidays))

    # -- shape ------------------------------------------------------------
    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.tz_name)

    @property
    def is_continuous(self) -> bool:
        """True for a 24-hour session (open == close)."""
        return self.open_time == self.close_time

    @property
    def wraps_midnight(self) -> bool:
        return self.close_time < self.open_time

    def is_trading_day(self, day: date) -> bool:
        """Does a session *start* on this local calendar date?"""
        if isinstance(day, datetime):
            day = day.date()
        return day.weekday() in self.weekdays and day not in self.holidays

    # -- boundaries --------------------------------------------------------
    def bounds_for(self, day: date) -> tuple[datetime, datetime] | None:
        """UTC ``(open, close)`` of the session starting on local date `day`,
        or None when no session starts that day. The interval is half-open:
        `open` is tradable, `close` is not."""
        if not self.is_trading_day(day):
            return None
        tz = self.tz
        start = datetime.combine(day, self.open_time, tzinfo=tz)
        end_day = day if (not self.is_continuous and not self.wraps_midnight) \
            else day + timedelta(days=1)
        end = datetime.combine(end_day, self.close_time, tzinfo=tz)
        return start.astimezone(timezone.utc), end.astimezone(timezone.utc)

    def _candidate_days(self, when: datetime) -> Iterator[date]:
        """Local dates whose session could still be running at `when`.

        Only today and yesterday: no session here exceeds 24 hours, so a
        session that started two days ago has necessarily ended.
        """
        local_date = when.astimezone(self.tz).date()
        yield local_date - timedelta(days=1)
        yield local_date

    def is_open(self, when: datetime) -> bool:
        """Is the venue accepting orders at this instant?"""
        moment = ensure_utc(when, field_name="when")
        for day in self._candidate_days(moment):
            window = self.bounds_for(day)
            if window is not None and window[0] <= moment < window[1]:
                return True
        return False

    def next_open(self, when: datetime) -> datetime:
        """The first session open strictly after `when` (UTC).

        Strictly after, so calling it repeatedly walks forward through the
        calendar instead of returning the same boundary forever.
        """
        moment = ensure_utc(when, field_name="when")
        for start, _ in self._walk(moment):
            if start > moment:
                return start
        raise ConfigurationError(
            "session has no open within the search horizon",
            session=self.name, horizon_days=_SEARCH_HORIZON_DAYS)

    def next_close(self, when: datetime) -> datetime:
        """The first session close strictly after `when` (UTC).

        When the venue is currently open this is the current session's close,
        which is what holding-period and end-of-day flattening logic needs.
        """
        moment = ensure_utc(when, field_name="when")
        for _, end in self._walk(moment):
            if end > moment:
                return end
        raise ConfigurationError(
            "session has no close within the search horizon",
            session=self.name, horizon_days=_SEARCH_HORIZON_DAYS)

    def _walk(self, moment: datetime) -> Iterator[tuple[datetime, datetime]]:
        """Yield session windows in chronological order, starting one local day
        before `moment` so a session already in progress is not missed."""
        day = moment.astimezone(self.tz).date() - timedelta(days=1)
        for offset in range(_SEARCH_HORIZON_DAYS):
            window = self.bounds_for(day + timedelta(days=offset))
            if window is not None:
                yield window

    def with_holidays(self, holidays: Iterable[date]) -> "MarketSession":
        """A copy with `holidays` added — sessions are frozen, so calendars are
        updated by replacement, never in place."""
        return MarketSession(
            name=self.name, tz_name=self.tz_name, open_time=self.open_time,
            close_time=self.close_time, weekdays=self.weekdays,
            holidays=frozenset(self.holidays) | frozenset(holidays))

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "tz_name": self.tz_name,
            "open_time": self.open_time.isoformat(),
            "close_time": self.close_time.isoformat(),
            "weekdays": sorted(self.weekdays),
            "holidays": [d.isoformat() for d in sorted(self.holidays)],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MarketSession":
        try:
            return cls(
                name=str(data["name"]),
                tz_name=str(data.get("tz_name", "UTC")),
                open_time=time.fromisoformat(str(data.get("open_time", "00:00"))),
                close_time=time.fromisoformat(str(data.get("close_time", "00:00"))),
                weekdays=frozenset(int(d) for d in
                                   data.get("weekdays", (0, 1, 2, 3, 4))),
                holidays=frozenset(date.fromisoformat(str(d))
                                   for d in data.get("holidays", ())),
            )
        except KeyError as exc:
            raise ConfigurationError("session definition is missing a field",
                                     missing=str(exc)) from None
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("session definition is malformed",
                                     reason=str(exc)) from None


#: 24/7, no holidays — crypto venues. Continuous by the open == close rule.
CRYPTO_24_7 = MarketSession(
    name="crypto_24_7", tz_name="UTC",
    open_time=time(0, 0), close_time=time(0, 0),
    weekdays=frozenset({0, 1, 2, 3, 4, 5, 6}))

#: US equity regular trading hours. Pre/post market is deliberately excluded:
#: liquidity there is not what the risk model assumes.
US_EQUITY_RTH = MarketSession(
    name="us_equity_rth", tz_name="America/New_York",
    open_time=time(9, 30), close_time=time(16, 0),
    weekdays=frozenset({0, 1, 2, 3, 4}))

#: Round-the-clock weekday session (FX-style: opens Sunday evening, closes
#: Friday evening — modelled here as Mon-Fri UTC days for the default set).
FX_24_5 = MarketSession(
    name="fx_24_5", tz_name="UTC",
    open_time=time(0, 0), close_time=time(0, 0),
    weekdays=frozenset({0, 1, 2, 3, 4}))

_BUILTIN_SESSIONS: dict[str, MarketSession] = {
    s.name: s for s in (CRYPTO_24_7, US_EQUITY_RTH, FX_24_5)
}


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

def _norm(text: Any) -> str:
    return str(text).strip().upper()


class InstrumentRegistry:
    """Catalogue of tradable instruments and the sessions they trade in.

    Lookups are by `Instrument.key` (``"EXCHANGE:SYMBOL"``) or by bare symbol.
    A bare symbol listed on two venues is *ambiguous* and raises rather than
    picking one: silently resolving ``"AAPL"`` to the wrong venue produces an
    order routed to a book that does not exist.

    Reads return frozen contracts or fresh containers, so a caller can never
    reach in and mutate registry state through a returned value.
    """

    def __init__(self, *, sessions: Mapping[str, MarketSession] | None = None):
        self._by_key: dict[str, Instrument] = {}
        self._session_of: dict[str, str] = {}
        # The built-ins are always present so that a registry loaded from a
        # partial config still resolves "us_equity_rth" rather than failing at
        # the first order.
        self._sessions: dict[str, MarketSession] = dict(_BUILTIN_SESSIONS)
        self._sealed = False
        for session in (sessions or {}).values():
            self.add_session(session, replace=True)

    # -- mutation ----------------------------------------------------------
    @property
    def sealed(self) -> bool:
        return self._sealed

    def seal(self) -> "InstrumentRegistry":
        """Make this registry permanently read-only and return it.

        The shared default registry is sealed at import. Anything holding a
        reference — including code paths fed by untrusted external content —
        can then only read it; a legitimate customiser takes `.copy()`.
        """
        self._sealed = True
        return self

    def _check_mutable(self) -> None:
        if self._sealed:
            raise ConfigurationError(
                "instrument registry is sealed; call .copy() to derive a "
                "mutable registry instead of mutating the shared catalogue")

    def register(self, instrument: Instrument, *, session: str | None = None,
                 replace: bool = False) -> Instrument:
        """Add an instrument. Duplicate keys raise unless `replace=True`.

        Silent replacement is the wrong default: two definitions of the same
        symbol with different lot sizes is a configuration bug, and the one
        that wins would depend on import order.
        """
        self._check_mutable()
        if not isinstance(instrument, Instrument):
            raise ConfigurationError("register expects an Instrument",
                                     got=type(instrument).__name__)
        key = instrument.key
        if key in self._by_key and not replace:
            raise ConfigurationError("instrument already registered", key=key)
        if session is not None and session not in self._sessions:
            raise ConfigurationError("unknown session", key=key, session=session)
        self._by_key[key] = instrument
        self._session_of[key] = session or self._default_session_name(instrument)
        return instrument

    def add_session(self, session: MarketSession, *, replace: bool = False
                    ) -> MarketSession:
        self._check_mutable()
        if not isinstance(session, MarketSession):
            raise ConfigurationError("add_session expects a MarketSession",
                                     got=type(session).__name__)
        if session.name in self._sessions and not replace:
            raise ConfigurationError("session already registered",
                                     session=session.name)
        self._sessions[session.name] = session
        return session

    @staticmethod
    def _default_session_name(instrument: Instrument) -> str:
        """Fallback session by asset class. Conservative: anything unrecognised
        gets the *narrowest* calendar (weekday RTH-less 24/5), never 24/7,
        because over-stating tradability is the failure that puts orders into a
        closed book."""
        asset_class = str(instrument.asset_class).lower()
        if asset_class == "crypto":
            return CRYPTO_24_7.name
        if asset_class in ("equity", "etf"):
            return US_EQUITY_RTH.name
        return FX_24_5.name

    def copy(self) -> "InstrumentRegistry":
        """An unsealed, independent clone."""
        clone = InstrumentRegistry()
        clone._sessions = dict(self._sessions)
        clone._by_key = dict(self._by_key)
        clone._session_of = dict(self._session_of)
        return clone

    # -- lookup ------------------------------------------------------------
    def find(self, reference: str) -> Instrument | None:
        """Resolve ``"EXCHANGE:SYMBOL"`` or a bare symbol; None when unknown.

        Raises only for the genuinely undecidable case (an ambiguous bare
        symbol), never for a plain miss.
        """
        text = _norm(reference)
        if not text:
            return None
        for key, instrument in self._by_key.items():
            if _norm(key) == text:
                return instrument
        matches = [i for i in self._by_key.values() if _norm(i.symbol) == text]
        if len(matches) > 1:
            raise ConfigurationError(
                "symbol is listed on more than one exchange; qualify it as "
                "EXCHANGE:SYMBOL", symbol=text,
                candidates=sorted(i.key for i in matches))
        return matches[0] if matches else None

    def get(self, reference: str) -> Instrument:
        """Like `find`, but an unknown instrument raises.

        There is no "make one up" path. A default `Instrument` would carry
        invented tick and lot sizes, and orders sized from invented arithmetic
        are rejected by the venue at best and mis-sized at worst.
        """
        instrument = self.find(reference)
        if instrument is None:
            raise ConfigurationError("unknown instrument", reference=str(reference),
                                     known=len(self._by_key))
        return instrument

    def lookup(self, symbol: str, exchange: str | None = None) -> Instrument:
        """Resolve by symbol, optionally disambiguated by exchange."""
        if exchange:
            return self.get(f"{_norm(exchange)}:{_norm(symbol)}")
        return self.get(symbol)

    def __contains__(self, reference: object) -> bool:
        try:
            return self.find(str(reference)) is not None
        except ConfigurationError:
            return True          # ambiguous means "several of them are present"

    def __len__(self) -> int:
        return len(self._by_key)

    def __iter__(self) -> Iterator[Instrument]:
        return iter(self.all())

    def keys(self) -> list[str]:
        return sorted(self._by_key)

    def symbols(self) -> list[str]:
        return sorted({i.symbol for i in self._by_key.values()})

    def all(self) -> tuple[Instrument, ...]:
        """Every instrument, ordered by key — a fresh tuple of frozen values."""
        return tuple(self._by_key[k] for k in sorted(self._by_key))

    def by_asset_class(self, asset_class: str) -> tuple[Instrument, ...]:
        wanted = str(asset_class).lower()
        return tuple(i for i in self.all()
                     if str(i.asset_class).lower() == wanted)

    # -- sessions ----------------------------------------------------------
    def sessions(self) -> dict[str, MarketSession]:
        """A copy of the session table (sessions themselves are frozen)."""
        return dict(self._sessions)

    def session(self, name: str) -> MarketSession:
        try:
            return self._sessions[name]
        except KeyError:
            raise ConfigurationError("unknown session", session=str(name),
                                     known=sorted(self._sessions)) from None

    def session_for(self, reference: str) -> MarketSession:
        """The trading session of an instrument."""
        instrument = self.get(reference)
        return self.session(self._session_of[instrument.key])

    def session_name_for(self, reference: str) -> str:
        return self._session_of[self.get(reference).key]

    def is_open(self, reference: str, when: datetime) -> bool:
        """Is this instrument's venue open at `when`?"""
        return self.session_for(reference).is_open(when)

    # -- serialisation -----------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "sessions": [s.to_dict() for _, s in sorted(self._sessions.items())],
            "instruments": [
                dict(i.to_dict(), session=self._session_of[i.key])
                for i in self.all()
            ],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "InstrumentRegistry":
        """Build a registry from a plain mapping (config file, JSON payload).

        Note the trust boundary: loading a catalogue is an *operator* action.
        This constructor is not a hook for content fetched at runtime — that is
        why it returns a new registry instead of updating the shared one.
        """
        if not isinstance(data, Mapping):
            raise ConfigurationError("registry definition must be a mapping",
                                     got=type(data).__name__)
        registry = cls()
        for raw in data.get("sessions", ()) or ():
            registry.add_session(MarketSession.from_dict(raw), replace=True)
        for raw in data.get("instruments", ()) or ():
            if not isinstance(raw, Mapping):
                raise ConfigurationError("instrument definition must be a mapping",
                                         got=type(raw).__name__)
            fields = dict(raw)
            session = fields.pop("session", None)
            try:
                instrument = Instrument(**fields)
            except TypeError as exc:
                raise ConfigurationError("instrument definition has unexpected "
                                         "fields", reason=str(exc)) from None
            registry.register(instrument, session=session, replace=True)
        return registry

    @classmethod
    def from_json(cls, text: str) -> "InstrumentRegistry":
        try:
            data = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("registry JSON is malformed",
                                     reason=str(exc)) from None
        return cls.from_dict(data)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (f"InstrumentRegistry({len(self._by_key)} instruments, "
                f"{len(self._sessions)} sessions"
                f"{', sealed' if self._sealed else ''})")


# ---------------------------------------------------------------------------
# built-in catalogue
# ---------------------------------------------------------------------------

#: A handful of real, liquid instruments so tests, demos, and the paper broker
#: have something concrete without a config file. Values are the venues' actual
#: published increments at time of writing; they are a *starting point*, not an
#: authority — a live deployment loads its broker's own instrument feed.
_DEFAULT_INSTRUMENTS: tuple[tuple[Instrument, str], ...] = (
    (Instrument(symbol="AAPL", exchange="NASDAQ", asset_class="equity",
                quote_currency="USD", tick_size=Decimal("0.01"),
                lot_size=Decimal("1"), min_quantity=Decimal("1")),
     US_EQUITY_RTH.name),
    (Instrument(symbol="MSFT", exchange="NASDAQ", asset_class="equity",
                quote_currency="USD", tick_size=Decimal("0.01"),
                lot_size=Decimal("1"), min_quantity=Decimal("1")),
     US_EQUITY_RTH.name),
    (Instrument(symbol="NVDA", exchange="NASDAQ", asset_class="equity",
                quote_currency="USD", tick_size=Decimal("0.01"),
                lot_size=Decimal("1"), min_quantity=Decimal("1")),
     US_EQUITY_RTH.name),
    (Instrument(symbol="SPY", exchange="ARCA", asset_class="etf",
                quote_currency="USD", tick_size=Decimal("0.01"),
                lot_size=Decimal("1"), min_quantity=Decimal("1")),
     US_EQUITY_RTH.name),
    # Crypto: fractional, tiny lot grid, and a venue minimum notional — the
    # combination that most often produces "order too small" rejections when a
    # sizing model is allowed to round however it likes.
    (Instrument(symbol="BTCUSDT", exchange="BINANCE", asset_class="crypto",
                quote_currency="USDT", tick_size=Decimal("0.01"),
                lot_size=Decimal("0.00001"), min_quantity=Decimal("0.00001"),
                min_notional=Decimal("10"), allow_fractional=True),
     CRYPTO_24_7.name),
    (Instrument(symbol="ETHUSDT", exchange="BINANCE", asset_class="crypto",
                quote_currency="USDT", tick_size=Decimal("0.01"),
                lot_size=Decimal("0.0001"), min_quantity=Decimal("0.0001"),
                min_notional=Decimal("10"), allow_fractional=True),
     CRYPTO_24_7.name),
    (Instrument(symbol="BTC-USD", exchange="COINBASE", asset_class="crypto",
                quote_currency="USD", tick_size=Decimal("0.01"),
                lot_size=Decimal("0.00000001"),
                min_quantity=Decimal("0.00000001"),
                min_notional=Decimal("1"), allow_fractional=True),
     CRYPTO_24_7.name),
)


def build_default_registry() -> InstrumentRegistry:
    """A fresh, **unsealed** registry holding the built-in catalogue."""
    registry = InstrumentRegistry()
    for instrument, session in _DEFAULT_INSTRUMENTS:
        registry.register(instrument, session=session)
    return registry


_DEFAULT: InstrumentRegistry | None = None


def default_registry() -> InstrumentRegistry:
    """The process-wide catalogue, sealed against mutation.

    Sealed on first construction and shared thereafter, so no caller — however
    deep in a chain that started with untrusted content — can add or reshape an
    instrument other code will size orders from.
    """
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = build_default_registry().seal()
    return _DEFAULT


__all__ = [
    "CRYPTO_24_7", "FX_24_5", "US_EQUITY_RTH", "InstrumentRegistry",
    "MarketSession", "build_default_registry", "default_registry",
    "floor_to_timeframe", "is_valid_timeframe", "next_bar_open",
    "parse_timeframe", "timeframe_bounds", "timeframe_seconds",
]

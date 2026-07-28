"""The safe event interface: timing is trusted, text never is.

Two rules, and the second is a safety boundary rather than a modelling choice.

**External text is untrusted.** Every string that arrives from outside — a
headline, a release title, an exchange notice — goes through
`sentiment.sanitise_external_text`, which already neutralises instruction text
and fenced system blocks and is already tested against injection probes. Nothing
here re-implements that; it consumes it, and `MarketEvent` refuses to construct
from raw text that was not sanitised.

**An event may change a feature and may never change a control.** This is the
line the whole module exists to draw:

    may affect:  features, forecasts, abstention, scenario weights
    may NEVER:   risk limits, broker credentials, permissions, live-mode
                 status, safety controls, deployment gates

`assert_event_boundary` is the mechanical check, and the structural one is that
this module imports none of those subsystems — `kernel.audit_evolution_modules`
covers it, and `tests/test_trading_native_capabilities.py` asserts the import
set directly. A module that cannot reach a control cannot be talked into
changing one by a headline.

What is trusted, precisely
--------------------------
The *timing* of a scheduled event is `KNOWN_IN_ADVANCE` (see
`schema.TimestampSemantics`) and is the useful part: "a central-bank decision
lands in two bars" is actionable and needs no text at all. The *content* is
`ANNOUNCED` at best and `REVISED` at worst, and this module deliberately offers
no numeric surprise field — `schema.event_surprise` is marked `UNREACHABLE`
because using it without vintages is a look-ahead of days.

So `EventContext` gives a model: how close the next event is, how important it
was declared to be, what kind it is, and whether text was tainted. Not what the
release said.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Iterator, Mapping, Sequence

from ..contracts import ensure_utc, jsonable
from ..errors import ConfigurationError

#: Bumped when an event kind's meaning changes.
EVENT_SCHEMA_VERSION = 1

#: Subsystems an event may never influence. Names rather than objects, because
#: this is checked against parsed source that is never executed.
FORBIDDEN_TARGETS: frozenset[str] = frozenset({
    "risk_limits", "broker_credentials", "permissions", "live_mode",
    "safety_controls", "deployment_gates",
})

#: What an event *may* influence. Enumerated so the permitted set is as
#: reviewable as the forbidden one.
PERMITTED_TARGETS: frozenset[str] = frozenset({
    "features", "forecasts", "abstention", "scenario_weights", "explanations",
})


class EventKind(str, Enum):
    ECONOMIC_ANNOUNCEMENT = "economic_announcement"
    EARNINGS = "earnings"
    CORPORATE_DISCLOSURE = "corporate_disclosure"
    CENTRAL_BANK_DECISION = "central_bank_decision"
    EXCHANGE_INCIDENT = "exchange_incident"
    REGULATORY_ANNOUNCEMENT = "regulatory_announcement"
    SCHEDULED_MARKET_EVENT = "scheduled_market_event"


class EventImportance(str, Enum):
    UNKNOWN = "unknown"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


#: Kinds whose timing is genuinely known ahead. The rest arrive when they
#: arrive, and a model conditioning on "an exchange incident in two bars" would
#: be conditioning on the future.
SCHEDULED_KINDS: frozenset[EventKind] = frozenset({
    EventKind.ECONOMIC_ANNOUNCEMENT, EventKind.EARNINGS,
    EventKind.CENTRAL_BANK_DECISION, EventKind.SCHEDULED_MARKET_EVENT,
})


@dataclass(frozen=True)
class MarketEvent:
    """One event. Text is sanitised at construction and marked if it was dirty.

    `title` is stored *only* in its sanitised form. There is no field holding
    the original, because a field holding the original is a field something will
    eventually read.
    """

    event_id: str
    kind: EventKind
    #: When the event occurs (scheduled) or occurred (unscheduled).
    at: datetime
    #: When Olympus learned of it. For an unscheduled event this is what makes
    #: the timing usable at all — a model may condition on an incident only
    #: after it was known.
    known_at: datetime
    instrument_keys: tuple[str, ...] = ()
    importance: EventImportance = EventImportance.UNKNOWN
    #: Sanitised. `from_external` is the only way to supply raw text.
    title: str = ""
    source: str = "unknown"
    #: True when sanitisation changed the text — i.e. it contained something
    #: that looked like an instruction.
    text_tainted: bool = False
    #: Digest of the raw text, so an audit can tie a record to its source
    #: without storing the source.
    raw_digest: str = ""

    def __post_init__(self):
        if not str(self.event_id).strip():
            raise ConfigurationError("an event must have an id")
        object.__setattr__(self, "kind", EventKind(self.kind))
        object.__setattr__(self, "importance", EventImportance(self.importance))
        for name in ("at", "known_at"):
            object.__setattr__(self, name,
                               ensure_utc(getattr(self, name), field_name=name))
        object.__setattr__(self, "instrument_keys", tuple(self.instrument_keys))
        if self.kind in SCHEDULED_KINDS and self.known_at > self.at:
            raise ConfigurationError(
                "a scheduled event known only after it happened is not "
                "scheduled; use an unscheduled kind or fix known_at",
                event_id=self.event_id, kind=self.kind.value)

    @property
    def scheduled(self) -> bool:
        return self.kind in SCHEDULED_KINDS

    def knowable_at(self, when: datetime) -> bool:
        """Was this event's *existence* known at `when`?

        The gate every feature passes. For a scheduled release this is true
        well before it happens; for an exchange incident it is true only after.
        """
        return ensure_utc(when, field_name="when") >= self.known_at

    def bars_until(self, when: datetime, *, bar_seconds: float) -> float | None:
        """Bars from `when` to the event. Negative once it has passed.

        `None` when the event is not yet knowable — not a large number, because
        a large number is a value a model will condition on.
        """
        if not self.knowable_at(when) or bar_seconds <= 0:
            return None
        moment = ensure_utc(when, field_name="when")
        return (self.at - moment).total_seconds() / bar_seconds

    def to_dict(self) -> dict:
        return {"event_id": self.event_id, "kind": self.kind.value,
                "at": jsonable(self.at), "known_at": jsonable(self.known_at),
                "instrument_keys": list(self.instrument_keys),
                "importance": self.importance.value, "title": self.title,
                "source": self.source, "scheduled": self.scheduled,
                "text_tainted": self.text_tainted, "raw_digest": self.raw_digest}

    @classmethod
    def from_external(cls, *, event_id: str, kind: EventKind, at: datetime,
                      known_at: datetime, raw_title: Any = "",
                      instrument_keys: Sequence[str] = (),
                      importance: EventImportance = EventImportance.UNKNOWN,
                      source: str = "unknown") -> "MarketEvent":
        """Build from untrusted text. The only path that accepts raw strings.

        Delegates to `sentiment.sanitise_external_text_report`, which already
        neutralises instruction text and fenced system blocks and is already
        probed by `tests/test_trading_sentiment.py`. Re-implementing that here
        would be a second sanitiser to keep correct.
        """
        from ..sentiment import sanitise_external_text_report
        report = sanitise_external_text_report(raw_title)
        raw = "" if raw_title is None else str(raw_title)
        return cls(
            event_id=event_id, kind=kind, at=at, known_at=known_at,
            instrument_keys=tuple(instrument_keys), importance=importance,
            title=report.text, source=source,
            text_tainted=bool(getattr(report, "modified", report.text != raw)),
            raw_digest=hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16])


class EventCalendar:
    """Events, queryable as of an instant. Never returns one not yet knowable."""

    __slots__ = ("_events",)

    def __init__(self, events: Sequence[MarketEvent] = ()):
        self._events: list[MarketEvent] = []
        for event in events:
            self.add(event)

    def add(self, event: MarketEvent) -> "EventCalendar":
        if any(e.event_id == event.event_id for e in self._events):
            raise ConfigurationError("duplicate event id",
                                     event_id=event.event_id)
        self._events.append(event)
        self._events.sort(key=lambda e: (e.at, e.event_id))
        return self

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self) -> Iterator[MarketEvent]:
        return iter(self._events)

    def knowable(self, when: datetime, *,
                 instrument_key: str = "") -> tuple[MarketEvent, ...]:
        """Every event whose existence was known at `when`."""
        moment = ensure_utc(when, field_name="when")
        return tuple(e for e in self._events
                     if e.knowable_at(moment)
                     and (not instrument_key or not e.instrument_keys
                          or instrument_key in e.instrument_keys))

    def next_after(self, when: datetime, *, instrument_key: str = "",
                   kinds: Sequence[EventKind] = ()) -> MarketEvent | None:
        """The next event at or after `when` that was already knowable."""
        moment = ensure_utc(when, field_name="when")
        wanted = set(kinds) if kinds else None
        for event in self.knowable(moment, instrument_key=instrument_key):
            if event.at < moment:
                continue
            if wanted is not None and event.kind not in wanted:
                continue
            return event
        return None

    def to_dict(self) -> dict:
        return {"schema_version": EVENT_SCHEMA_VERSION,
                "events": [e.to_dict() for e in self._events]}


@dataclass(frozen=True)
class EventContext:
    """What a model may know about events at one instant.

    Timing and declared importance. **No content.** There is deliberately no
    numeric surprise field: using one without vintages is a look-ahead of days,
    and `schema.event_surprise` is marked `UNREACHABLE` for that reason.
    """

    as_of: datetime
    instrument_key: str
    #: Bars until the next knowable scheduled event. `None` when there is none.
    bars_until_next: float | None = None
    next_kind: EventKind | None = None
    next_importance: EventImportance = EventImportance.UNKNOWN
    #: Events that became knowable within the trailing window.
    recent_count: int = 0
    #: True when any contributing event's text was altered by sanitisation.
    any_text_tainted: bool = False
    #: True when an exchange incident is in force.
    exchange_incident: bool = False

    def __post_init__(self):
        object.__setattr__(self, "as_of",
                           ensure_utc(self.as_of, field_name="as_of"))
        if self.next_kind is not None:
            object.__setattr__(self, "next_kind", EventKind(self.next_kind))
        object.__setattr__(self, "next_importance",
                           EventImportance(self.next_importance))

    @property
    def in_event_window(self) -> bool:
        """Within two bars of a declared high-importance event.

        Two bars either side. A forecast made into a central-bank decision is a
        forecast about a coin flip, whatever the model's confidence says.
        """
        if self.bars_until_next is None:
            return False
        return (abs(self.bars_until_next) <= 2.0
                and self.next_importance is EventImportance.HIGH)

    @property
    def elevated_risk(self) -> bool:
        return self.in_event_window or self.exchange_incident

    def features(self) -> Mapping[str, float | None]:
        """The numeric surface a model consumes. Timing only.

        `None` rather than a sentinel when there is no next event: a large
        number is a value a model will condition on, and "no event scheduled"
        is not "an event a very long way away".
        """
        return {
            "event_bars_until": self.bars_until_next,
            "event_importance_high":
                1.0 if self.next_importance is EventImportance.HIGH else 0.0,
            "event_recent_count": float(self.recent_count),
            "event_exchange_incident": 1.0 if self.exchange_incident else 0.0,
        }

    def to_dict(self) -> dict:
        return {"as_of": jsonable(self.as_of),
                "instrument": self.instrument_key,
                "bars_until_next": self.bars_until_next,
                "next_kind": self.next_kind.value if self.next_kind else None,
                "next_importance": self.next_importance.value,
                "recent_count": self.recent_count,
                "any_text_tainted": self.any_text_tainted,
                "exchange_incident": self.exchange_incident,
                "in_event_window": self.in_event_window,
                "elevated_risk": self.elevated_risk,
                "features": dict(self.features())}


def build_event_context(calendar: EventCalendar, *, as_of: datetime,
                        instrument_key: str, bar_seconds: float,
                        lookback_bars: float = 5.0) -> EventContext:
    """Event features as of `as_of`, from knowable events only."""
    moment = ensure_utc(as_of, field_name="as_of")
    if bar_seconds <= 0:
        raise ConfigurationError("bar_seconds must be positive",
                                 bar_seconds=bar_seconds)
    knowable = calendar.knowable(moment, instrument_key=instrument_key)

    upcoming = calendar.next_after(moment, instrument_key=instrument_key,
                                  kinds=tuple(SCHEDULED_KINDS))
    bars_until = (upcoming.bars_until(moment, bar_seconds=bar_seconds)
                  if upcoming else None)

    window_start = moment - timedelta(seconds=bar_seconds * lookback_bars)
    recent = [e for e in knowable if window_start <= e.known_at <= moment]
    incident = any(e.kind is EventKind.EXCHANGE_INCIDENT for e in recent)

    return EventContext(
        as_of=moment, instrument_key=instrument_key,
        bars_until_next=bars_until,
        next_kind=upcoming.kind if upcoming else None,
        next_importance=(upcoming.importance if upcoming
                         else EventImportance.UNKNOWN),
        recent_count=len(recent),
        any_text_tainted=any(e.text_tainted for e in recent),
        exchange_incident=incident)


#: Modules permitted to name a forbidden target, and why. One entry: this file
#: declares `FORBIDDEN_TARGETS`, so it must contain every name in it. Same
#: enumerated-with-a-reason pattern as `originality.MARKER_EXEMPT`, and
#: `boundary_exemption_is_needed` fails if it outlives its reason.
BOUNDARY_EXEMPT: Mapping[str, str] = {
    "events.py": "declares FORBIDDEN_TARGETS; must contain every name in it",
}


def assert_event_boundary(module_source: str, *, name: str = "<module>") -> None:
    """No event-handling **consumer** names a control it may never change.

    Mechanical and blunt on purpose: the boundary is not a subtlety, and a
    check that tried to distinguish "mentions" from "modifies" would be a check
    with a judgement call in it. That bluntness is why `BOUNDARY_EXEMPT` exists
    and why it has exactly one entry.
    """
    if name in BOUNDARY_EXEMPT:
        return
    lowered = module_source.lower()
    offenders = sorted(
        target for target in FORBIDDEN_TARGETS
        if target.replace("_", "") in lowered.replace("_", "").replace(" ", ""))
    if offenders:
        raise ConfigurationError(
            "event-handling code names a control an event may never change",
            module=name, offenders=offenders,
            permitted=sorted(PERMITTED_TARGETS))


def boundary_exemption_is_needed() -> tuple[str, ...]:
    """Exemptions that have outlived their reason. Empty is the good state."""
    from pathlib import Path
    stale = []
    for name, reason in BOUNDARY_EXEMPT.items():
        path = Path(__file__).resolve().parent / name
        if not path.exists():
            stale.append(f"{name}: exempted and does not exist")
            continue
        text = path.read_text(encoding="utf-8").lower().replace("_", "")
        if not any(t.replace("_", "") in text for t in FORBIDDEN_TARGETS):
            stale.append(f"{name}: exempted and names no forbidden target "
                         f"({reason})")
    return tuple(stale)


def event_boundary_report() -> dict:
    """The boundary, as data. Feeds the model card and the capability report."""
    return {"schema_version": EVENT_SCHEMA_VERSION,
            "may_affect": sorted(PERMITTED_TARGETS),
            "may_never_affect": sorted(FORBIDDEN_TARGETS),
            "text_policy": "every external string is sanitised by "
                           "sentiment.sanitise_external_text before it is "
                           "stored; the original is never retained, only its "
                           "digest",
            "content_policy": "timing and declared importance only. No numeric "
                              "surprise field exists, because using one without "
                              "vintages is a look-ahead of days",
            "scheduled_kinds": sorted(k.value for k in SCHEDULED_KINDS),
            "boundary_exemptions": dict(BOUNDARY_EXEMPT),
            "stale_exemptions": list(boundary_exemption_is_needed())}


__all__ = ["EVENT_SCHEMA_VERSION", "FORBIDDEN_TARGETS", "PERMITTED_TARGETS",
           "EventKind", "EventImportance", "SCHEDULED_KINDS", "MarketEvent",
           "EventCalendar", "EventContext", "build_event_context",
           "BOUNDARY_EXEMPT", "assert_event_boundary",
           "boundary_exemption_is_needed", "event_boundary_report"]

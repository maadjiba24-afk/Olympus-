"""The immutable trading event trail.

Built on `olympus.ledger`, which already provides the hard part: an append-only
chain where each node is content-addressed, carries a `parent` link to the
previous node's hash, and is Ed25519-signed. `verify_ledger` walks that chain
and names the first break, so tampering with a historical record — editing a
risk decision, deleting an order, reordering fills — is detectable rather than
merely discouraged.

Reusing it rather than inventing a trading-specific log is deliberate: an audit
trail written for this domain alone would be a second, less-tested
implementation of the same primitive, and the primitive is the whole value.

What this module adds on top:

* **A trading event vocabulary** — every event type the design requires, so the
  set is enumerable and a missing emission is visible.
* **Correlation tracing.** One `correlation_id` threads market data → forecast →
  intent → decision → order → fill → position change, which is what turns "why
  did this order exist" from an archaeology exercise into a query.
* **Redaction.** Payloads are scanned for credential-shaped material before they
  are written. An audit trail that captures a broker key is a liability, and
  the trail is the one place you can never go back and edit.

Degradation, deliberately partial: if the crypto backend is unavailable the
chain is unsigned but events are **still recorded**. Losing tamper-evidence is
bad; losing the record entirely is worse, and silently skipping the write would
be worst of all.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Sequence

from .clock import Clock, default_clock
from .contracts import SCHEMA_VERSION, ensure_utc, jsonable
from .errors import TradingError


class EventType(str, Enum):
    """Every recordable trading event. Enumerable on purpose — a required
    event that nothing emits should be findable by inspection."""
    MARKET_DATA_RECEIVED = "market_data_received"
    DATA_VALIDATED = "data_validated"
    FORECAST_PRODUCED = "forecast_produced"
    AGENT_OUTPUT = "agent_output"
    TRADE_INTENT = "trade_intent"
    RISK_DECISION = "risk_decision"
    ORDER_SUBMITTED = "order_submitted"
    BROKER_RESPONSE = "broker_response"
    FILL = "fill"
    POSITION_CHANGED = "position_changed"
    CONFIG_CHANGED = "config_changed"
    MODEL_CHANGED = "model_changed"
    KILL_SWITCH = "kill_switch"
    MODE_CHANGED = "mode_changed"
    RECONCILIATION = "reconciliation"
    ERROR = "error"
    HUMAN_INTERVENTION = "human_intervention"


#: The causal chain an order must be traceable along. Used by `trace()` to
#: order events and by tests to assert nothing in the chain is missing.
CAUSAL_CHAIN = (
    EventType.MARKET_DATA_RECEIVED,
    EventType.DATA_VALIDATED,
    EventType.FORECAST_PRODUCED,
    EventType.TRADE_INTENT,
    EventType.RISK_DECISION,
    EventType.ORDER_SUBMITTED,
    EventType.BROKER_RESPONSE,
    EventType.FILL,
    EventType.POSITION_CHANGED,
)


# --- redaction -------------------------------------------------------------
# Patterns for material that must never be written to an immutable log. Kept
# conservative and additive: a false positive costs a redacted string in an
# audit record, a false negative costs a leaked credential that cannot be
# retracted.
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
               re.S),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+"),
    re.compile(r"\b(?:sk|pk|ghp|gho|xox[baprs]|AKIA)[-_A-Za-z0-9]{8,}\b"),
    re.compile(r"https?://[^\s/@]+:[^\s/@]+@"),
)

#: Keys whose *values* are redacted regardless of shape.
_SECRET_KEYS = frozenset({
    "api_key", "apikey", "secret", "api_secret", "password", "passwd",
    "token", "access_token", "refresh_token", "private_key", "credential",
    "credentials", "authorization", "auth", "signature_key", "seed",
})

REDACTED = "[redacted]"


def redact(value: Any) -> Any:
    """Strip credential-shaped material from a payload, recursively."""
    if isinstance(value, Mapping):
        out = {}
        for key, item in value.items():
            if str(key).lower() in _SECRET_KEYS:
                out[str(key)] = REDACTED
            else:
                out[str(key)] = redact(item)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        text = value
        for pattern in _SECRET_PATTERNS:
            text = pattern.sub(REDACTED, text)
        return text
    return value


def new_correlation_id(prefix: str = "corr") -> str:
    """Mint the id that threads one decision's whole chain.

    Callers are free to supply their own (a backtest may prefer a deterministic
    one derived from the bar timestamp); this exists so the common case has an
    obvious right answer instead of every emitter inventing a scheme.
    """
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


@dataclass(frozen=True)
class TradingEvent:
    event_id: str
    ts: datetime
    type: EventType
    actor: str
    subject: str
    correlation_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        object.__setattr__(self, "ts", ensure_utc(self.ts, field_name="ts"))
        object.__setattr__(self, "payload", dict(self.payload))

    def to_dict(self) -> dict:
        return {"event_id": self.event_id, "ts": jsonable(self.ts),
                "type": self.type.value, "actor": self.actor,
                "subject": self.subject, "correlation_id": self.correlation_id,
                "payload": jsonable(self.payload),
                "schema_version": self.schema_version}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TradingEvent":
        return cls(event_id=data["event_id"],
                   ts=datetime.fromisoformat(data["ts"]),
                   type=EventType(data["type"]), actor=data.get("actor", ""),
                   subject=data.get("subject", ""),
                   correlation_id=data.get("correlation_id", ""),
                   payload=data.get("payload") or {},
                   schema_version=data.get("schema_version", SCHEMA_VERSION))


class AuditTrail:
    """Append-only, hash-chained, signed trading events for one run."""

    def __init__(self, run_id: str, clock: Clock | None = None, *,
                 ledger_backed: bool = True):
        self.run_id = str(run_id)
        self.clock = clock or default_clock()
        self.ledger_backed = ledger_backed
        self._memory: list[TradingEvent] = []

    # -- writing ----------------------------------------------------------

    def record(self, event_type: Any, payload: Mapping[str, Any] | None = None,
               *, actor: str = "system", subject: str = "",
               correlation_id: str = "") -> TradingEvent:
        """Append one event. Never raises for an unknown event type — an
        unrecognised event is still worth recording, and refusing to log
        something because its label is unfamiliar is the wrong failure."""
        if not isinstance(event_type, EventType):
            label = str(event_type)
            try:
                event_type = EventType(label.lower())
            except ValueError:
                # Keep the label the caller used: downgrading to ERROR loses the
                # only evidence of what was meant, and "an event we could not
                # classify" is itself worth knowing about later.
                event_type = EventType.ERROR
                payload = {**(payload or {}), "unrecognised_event_type": label}
        event = TradingEvent(
            event_id=uuid.uuid4().hex, ts=self.clock.now(), type=event_type,
            actor=actor, subject=subject, correlation_id=correlation_id,
            payload=redact(jsonable(dict(payload or {}))))
        self._memory.append(event)
        if self.ledger_backed:
            self._append_to_ledger(event)
        return event

    def _append_to_ledger(self, event: TradingEvent) -> None:
        from olympus import ledger
        # `step` is the compact, replay-hashable description; `state` carries
        # the full payload. ledger.replay_hash() therefore digests the sequence
        # of event kinds and subjects independently of payload volume.
        step = {"type": event.type.value, "subject": event.subject,
                "correlation_id": event.correlation_id}
        try:
            ledger.checkpoint(self._ledger_run_id(), step, event.to_dict())
        except Exception as exc:                         # noqa: BLE001
            # A durable-write failure is LOUD. The one degradation this module
            # accepts is an unsigned chain when the crypto backend is missing —
            # `ledger._seal_node` already handles that and still commits, and
            # `verify()` then reports the chain unattested. Anything else (an
            # unwritable ledger, a malformed run id) means the record does not
            # exist, and a caller that believes it recorded something it did not
            # is worse than one that fails.
            raise TradingError(
                f"audit event could not be committed to the ledger: {exc}",
                run_id=self.run_id, event_type=event.type.value,
                event_id=event.event_id) from exc

    def _ledger_run_id(self) -> str:
        return f"trading-{self.run_id}"

    # -- reading ----------------------------------------------------------

    def events(self, *, type: Any = None, since: datetime | None = None,
               correlation_id: str | None = None,
               subject: str | None = None) -> list[TradingEvent]:
        out = list(self._load())
        if type is not None:
            wanted = type if isinstance(type, EventType) else EventType(str(type).lower())
            out = [e for e in out if e.type is wanted]
        if since is not None:
            floor = ensure_utc(since, field_name="since")
            out = [e for e in out if e.ts >= floor]
        if correlation_id is not None:
            out = [e for e in out if e.correlation_id == correlation_id]
        if subject is not None:
            out = [e for e in out if e.subject == subject]
        return out

    def _load(self) -> list[TradingEvent]:
        if not self.ledger_backed:
            return list(self._memory)
        from olympus import ledger
        try:
            run = ledger.open_run(self._ledger_run_id())
        except Exception:                                # noqa: BLE001
            return list(self._memory)
        out: list[TradingEvent] = []
        for node in run.nodes:
            state = node.get("state")
            if isinstance(state, Mapping) and "event_id" in state:
                try:
                    out.append(TradingEvent.from_dict(state))
                except Exception:                        # noqa: BLE001
                    continue
        return out or list(self._memory)

    #: Payload keys that identify the entity an event is *about*. `trace()`
    #: resolves any of them to the correlation id, so an operator holding only
    #: a broker's order id can still ask "why did this order exist".
    IDENTITY_KEYS = ("client_order_id", "broker_order_id", "order_id",
                     "intent_id", "decision_id", "fill_id")

    def trace(self, identifier: str) -> list[TradingEvent]:
        """The full causal chain behind one order, in causal order.

        `identifier` is whatever the operator has to hand: a correlation id, or
        a client order id (or any other id in `IDENTITY_KEYS`). Both resolve to
        the same chain, because the question being asked — "why does this order
        exist" — is the same question whichever end you start from.

        Sorted by chain position first and timestamp second, so the story reads
        in the order it happened even when two events share a timestamp (which
        every FixedClock-driven backtest produces).
        """
        events = self._load()
        correlation_id = self._resolve_correlation_id(identifier, events)
        if correlation_id is None:
            # No thread to pull, but an event naming this id directly is still
            # a truthful (if lonely) answer — better than pretending the order
            # was never mentioned. An emitter that forgot its correlation id is
            # a bug in the emitter, not grounds for hiding the record.
            return [e for e in events if self._names(e, identifier)]
        order = {t: i for i, t in enumerate(CAUSAL_CHAIN)}
        chain = [e for e in events if e.correlation_id == correlation_id]
        return sorted(chain, key=lambda e: (order.get(e.type, len(order)), e.ts))

    def _resolve_correlation_id(self, identifier: str,
                                events: Sequence[TradingEvent]) -> str | None:
        """Map an id of any kind onto the correlation id that threads its chain.

        A correlation id wins outright; only if nothing correlates under that
        value do we go looking for an entity id in the payloads. That ordering
        matters: correlating is the cheap, exact answer, and falling through to
        payload search first would make an unrelated event that merely mentions
        the string hijack the trace.
        """
        if not identifier:
            return None
        if any(e.correlation_id == identifier for e in events):
            return identifier
        for event in events:
            if self._names(event, identifier):
                return event.correlation_id or None
        return None

    @staticmethod
    def _names(event: TradingEvent, identifier: str) -> bool:
        """True when the event's payload identifies this exact entity."""
        payload = event.payload or {}
        return any(payload.get(key) == identifier
                   for key in AuditTrail.IDENTITY_KEYS)

    def trace_order(self, client_order_id: str) -> list[TradingEvent]:
        """Alias kept for callers that want the intent spelled out."""
        return self.trace(client_order_id)

    # -- integrity --------------------------------------------------------

    def verify(self) -> dict:
        """Verify the chain. Tampering must show up here, not in a postmortem.

        Returns the ledger's verdict: `ok` (every node content-addressed,
        contiguous, parent-linked and signed), `verified` (the length of the
        trustworthy prefix, so a break is located rather than just announced),
        and `attested` (signed under a real, non-dev key). Without a working
        crypto backend the events are still on disk and still readable, but
        `ok` is False — an unsigned chain is a record, not evidence, and
        reporting it as verified would be the lie that matters.
        """
        if not self.ledger_backed:
            return {"ok": True, "count": len(self._memory), "verified": len(self._memory),
                    "attested": False, "problems": ["not ledger-backed"]}
        from olympus import ledger
        return ledger.verify_ledger(self._ledger_run_id())

    def replay_hash(self) -> str:
        from olympus import ledger
        return ledger.replay_hash(self._ledger_run_id())


__all__ = ["EventType", "TradingEvent", "AuditTrail", "CAUSAL_CHAIN",
           "redact", "REDACTED", "new_correlation_id"]

"""The immutable trading event trail.

Two things must be true for this to be worth anything: an order must be
traceable back to the market data that caused it, and someone editing the record
afterwards must be caught. Both get direct tests, including an actual forgery
attempt rather than a claim that signing is enabled.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from olympus import ledger
from olympus.trading.audit import (CAUSAL_CHAIN, REDACTED, AuditTrail,
                                   EventType, TradingEvent, redact)
from olympus.trading.clock import FixedClock

T0 = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)
SUBJ = "NASDAQ:AAPL"


@pytest.fixture()
def trail():
    return AuditTrail("test-run", clock=FixedClock(T0))


def _full_chain(trail, cid="corr-1"):
    payloads = {
        EventType.MARKET_DATA_RECEIVED: {"bars": 400},
        EventType.DATA_VALIDATED: {"status": "ok"},
        EventType.FORECAST_PRODUCED: {"model_version": "kronos-small@abc123"},
        EventType.TRADE_INTENT: {"intent_id": "i1"},
        EventType.RISK_DECISION: {"verdict": "approved", "policy_hash": "ph"},
        EventType.ORDER_SUBMITTED: {"client_order_id": "olymp-xyz"},
        EventType.BROKER_RESPONSE: {"status": "new"},
        EventType.FILL: {"quantity": "10"},
        EventType.POSITION_CHANGED: {"quantity": "10"},
    }
    for etype, payload in payloads.items():
        trail.record(etype, payload, actor="test", subject=SUBJ,
                     correlation_id=cid)
    return cid


# --- recording and retrieval ----------------------------------------------

def test_events_are_recorded_and_retrievable(trail):
    trail.record(EventType.TRADE_INTENT, {"intent_id": "i1"}, subject=SUBJ)
    events = trail.events()
    assert len(events) == 1
    assert events[0].type is EventType.TRADE_INTENT
    assert events[0].subject == SUBJ


def test_events_can_be_filtered(trail):
    cid = _full_chain(trail)
    trail.record(EventType.ERROR, {"x": 1}, subject="other", correlation_id="c2")
    assert len(trail.events(type=EventType.FILL)) == 1
    assert len(trail.events(correlation_id=cid)) == len(CAUSAL_CHAIN)
    assert len(trail.events(subject="other")) == 1


def test_an_unknown_event_type_is_still_recorded(trail):
    """Refusing to log something because its label is unfamiliar is the wrong
    failure — the record matters more than the taxonomy."""
    event = trail.record("something_new", {"x": 1})
    assert event.type is EventType.ERROR
    assert trail.events()


# --- traceability ----------------------------------------------------------

def test_an_order_traces_back_to_its_source_market_data(trail):
    """The completion-standard requirement, stated as a test."""
    cid = _full_chain(trail)
    chain = trail.trace(cid)
    assert [e.type for e in chain] == list(CAUSAL_CHAIN)


def test_trace_by_client_order_id(trail):
    _full_chain(trail)
    chain = trail.trace_order("olymp-xyz")
    types = {e.type for e in chain}
    assert EventType.MARKET_DATA_RECEIVED in types
    assert EventType.RISK_DECISION in types
    assert EventType.FILL in types


def test_trace_is_causally_ordered_even_with_identical_timestamps(trail):
    """A FixedClock gives every event the same ts; the chain must still read
    in the order it happened."""
    cid = _full_chain(trail)
    chain = trail.trace(cid)
    assert len({e.ts for e in chain}) == 1
    assert [e.type for e in chain] == list(CAUSAL_CHAIN)


def test_trace_of_an_unknown_order_is_empty(trail):
    assert trail.trace_order("nope") == []


# --- tamper evidence -------------------------------------------------------

@pytest.mark.requires_crypto
def test_a_clean_trail_verifies(trail):
    _full_chain(trail)
    assert trail.verify()["ok"] is True


@pytest.mark.requires_crypto
def test_forging_a_recorded_decision_is_detected():
    """Actually rewrite history on disk and confirm verification fails.

    This is the test that makes 'immutable' a claim rather than a hope: it
    forges a REJECTED risk decision into an APPROVED one, which is precisely
    the edit someone covering their tracks would make.
    """
    trail = AuditTrail("forgery-run", clock=FixedClock(T0))
    trail.record(EventType.RISK_DECISION, {"verdict": "rejected"},
                 subject=SUBJ, correlation_id="c")
    trail.record(EventType.ORDER_SUBMITTED, {"client_order_id": "o1"},
                 subject=SUBJ, correlation_id="c")
    assert trail.verify()["ok"] is True

    path = ledger._path("trading-forgery-run")
    raw = path.read_text(encoding="utf-8")
    assert "rejected" in raw
    path.write_text(raw.replace("rejected", "approved"), encoding="utf-8")

    result = AuditTrail("forgery-run", clock=FixedClock(T0)).verify()
    assert result["ok"] is False
    assert result["verified"] == 0
    assert any("signature" in p for p in result["problems"])


@pytest.mark.requires_crypto
def test_deleting_an_event_is_detected():
    """Removing a middle record breaks the chain's seq/parent linkage."""
    trail = AuditTrail("deletion-run", clock=FixedClock(T0))
    for i in range(4):
        trail.record(EventType.FILL, {"n": i}, subject=SUBJ, correlation_id="c")
    assert trail.verify()["ok"] is True

    path = ledger._path("trading-deletion-run")
    lines = path.read_text(encoding="utf-8").splitlines()
    del lines[1]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert AuditTrail("deletion-run", clock=FixedClock(T0)).verify()["ok"] is False


# --- redaction -------------------------------------------------------------

def test_credential_shaped_material_is_redacted(trail):
    """The trail is the one place you can never go back and edit."""
    event = trail.record(EventType.CONFIG_CHANGED, {
        "api_key": "sk-abcdefghijklmnop",
        "note": "bearer eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.sig",
        "nested": [{"password": "hunter2"}],
        "url": "https://user:secret@broker.example.com/api",
        "safe": "max_order_value=1000",
    })
    payload = event.payload
    assert payload["api_key"] == REDACTED
    assert payload["nested"][0]["password"] == REDACTED
    assert REDACTED in payload["note"]
    assert REDACTED in payload["url"]
    assert payload["safe"] == "max_order_value=1000"


def test_redaction_is_recursive_and_type_preserving():
    out = redact({"a": {"b": [{"token": "x"}, "plain"]}, "n": 5})
    assert out["a"]["b"][0]["token"] == REDACTED
    assert out["a"]["b"][1] == "plain"
    assert out["n"] == 5


def test_a_recorded_event_round_trips(trail):
    original = trail.record(EventType.FILL, {"quantity": "10"}, subject=SUBJ,
                            correlation_id="c")
    restored = TradingEvent.from_dict(original.to_dict())
    assert restored.event_id == original.event_id
    assert restored.type is original.type
    assert restored.payload == original.payload


def test_events_survive_a_new_trail_instance():
    """The trail is on disk, not in the process that wrote it."""
    AuditTrail("persist-run", clock=FixedClock(T0)).record(
        EventType.FILL, {"quantity": "1"}, subject=SUBJ, correlation_id="c")
    reopened = AuditTrail("persist-run", clock=FixedClock(T0 + timedelta(hours=1)))
    assert len(reopened.events()) == 1

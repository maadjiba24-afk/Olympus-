"""The audit trail as an *evidence* system, not a logging system.

These tests are written against the properties an investigator would need a
year after the fact: the record survives the process that wrote it, it says
which market data caused which order, editing it is detectable, and it never
contains a credential. Where a property could be faked by a cooperative test
(signing "is enabled", secrets "are handled") the test does the hostile thing
instead — rewrites the file on disk, feeds in real credential shapes, breaks the
crypto backend.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from olympus import ledger, witness
from olympus.trading.audit import (CAUSAL_CHAIN, REDACTED, AuditTrail,
                                   EventType, TradingEvent,
                                   new_correlation_id, redact)
from olympus.trading.clock import FixedClock
from olympus.trading.contracts import RiskCheck, Side, Verdict
from olympus.trading.errors import TradingError

T0 = datetime(2026, 3, 14, 9, 30, tzinfo=timezone.utc)
SUBJ = "NASDAQ:AAPL"


@pytest.fixture()
def clock():
    return FixedClock(T0)


@pytest.fixture()
def trail(clock):
    return AuditTrail("audit-run", clock=clock)


def _record_chain(trail, cid="corr-a", order_id="olymp-1"):
    """Emit one complete market-data-to-position chain under one correlation."""
    steps = (
        (EventType.MARKET_DATA_RECEIVED, {"bars": 512, "source": "test"}),
        (EventType.DATA_VALIDATED, {"status": "ok", "gaps": 0}),
        (EventType.FORECAST_PRODUCED, {"model_version": "kronos-small@ff01"}),
        (EventType.TRADE_INTENT, {"intent_id": "intent-9"}),
        (EventType.RISK_DECISION, {"verdict": "approved", "intent_id": "intent-9"}),
        (EventType.ORDER_SUBMITTED, {"client_order_id": order_id}),
        (EventType.BROKER_RESPONSE, {"client_order_id": order_id, "status": "new"}),
        (EventType.FILL, {"client_order_id": order_id, "quantity": "10"}),
        (EventType.POSITION_CHANGED, {"quantity": "10"}),
    )
    for etype, payload in steps:
        trail.record(etype, payload, actor="pipeline", subject=SUBJ,
                     correlation_id=cid)
    return cid


# --- the vocabulary is complete -------------------------------------------

def test_every_required_event_type_exists():
    """The set is enumerable on purpose: a required event that nothing can
    express is a hole you only discover during an incident."""
    required = {
        "MARKET_DATA_RECEIVED", "DATA_VALIDATED", "FORECAST_PRODUCED",
        "AGENT_OUTPUT", "TRADE_INTENT", "RISK_DECISION", "ORDER_SUBMITTED",
        "BROKER_RESPONSE", "FILL", "POSITION_CHANGED", "CONFIG_CHANGED",
        "MODEL_CHANGED", "KILL_SWITCH", "ERROR", "HUMAN_INTERVENTION",
        "MODE_CHANGED", "RECONCILIATION",
    }
    assert required <= {member.name for member in EventType}


# --- round trip and persistence -------------------------------------------

def test_an_event_round_trips_through_its_dict_form(trail):
    original = trail.record(EventType.RISK_DECISION,
                            {"verdict": "approved", "quantity": "10"},
                            actor="risk", subject=SUBJ, correlation_id="c")
    restored = TradingEvent.from_dict(original.to_dict())
    assert restored == original


def test_a_recorded_event_is_json_serialisable(trail):
    """Payloads go through `jsonable`, so a Decimal or an enum in a payload is
    a formatting detail rather than an unwritable record."""
    event = trail.record(EventType.FILL, {
        "quantity": Decimal("2.5"), "side": Side.BUY, "at": T0,
        "check": RiskCheck(name="max_order_value", passed=True),
        "verdict": Verdict.APPROVED,
    }, subject=SUBJ)
    encoded = json.dumps(event.to_dict())            # must not raise
    decoded = json.loads(encoded)["payload"]
    assert decoded["quantity"] == "2.5"
    assert decoded["side"] == "buy"
    assert decoded["at"].startswith("2026-03-14T09:30")
    assert decoded["check"]["passed"] is True


def test_the_trail_outlives_the_process_that_wrote_it(clock):
    """The record is on disk. A crash after recording still leaves evidence."""
    AuditTrail("survives", clock=clock).record(
        EventType.ORDER_SUBMITTED, {"client_order_id": "o-1"}, subject=SUBJ,
        correlation_id="c")
    reopened = AuditTrail("survives", clock=FixedClock(T0 + timedelta(days=1)))
    events = reopened.events()
    assert len(events) == 1
    assert events[0].payload["client_order_id"] == "o-1"
    assert events[0].ts == T0                        # the *recorded* time, not now


# --- ordering and filtering ------------------------------------------------

def test_events_come_back_in_the_order_they_happened(trail, clock):
    for i in range(5):
        trail.record(EventType.FILL, {"n": i}, subject=SUBJ)
        clock.advance(timedelta(seconds=30))
    events = trail.events()
    assert [e.payload["n"] for e in events] == [0, 1, 2, 3, 4]
    assert [e.ts for e in events] == sorted(e.ts for e in events)


def test_filtering_by_type_correlation_and_time(trail, clock):
    cid = _record_chain(trail)
    clock.advance(timedelta(minutes=5))
    late = trail.record(EventType.ERROR, {"boom": True}, subject="other",
                        correlation_id="corr-b")

    assert [e.type for e in trail.events(type=EventType.FILL)] == [EventType.FILL]
    assert len(trail.events(correlation_id=cid)) == len(CAUSAL_CHAIN)
    assert trail.events(since=T0 + timedelta(minutes=1)) == [late]
    assert trail.events(correlation_id="nobody") == []


def test_filters_compose(trail, clock):
    _record_chain(trail, cid="corr-a")
    clock.advance(timedelta(minutes=1))
    _record_chain(trail, cid="corr-b", order_id="olymp-2")
    fills = trail.events(type=EventType.FILL, correlation_id="corr-b")
    assert len(fills) == 1
    assert fills[0].payload["client_order_id"] == "olymp-2"


# --- traceability: the question the trail exists to answer ------------------

def test_an_order_traces_back_to_the_market_data_that_caused_it(trail):
    """`trace(client_order_id)` must reconstruct the whole causal chain from
    the record alone — no live objects, no other system consulted."""
    _record_chain(trail)
    chain = trail.trace("olymp-1")
    assert [e.type for e in chain] == list(CAUSAL_CHAIN)
    assert chain[0].payload["bars"] == 512
    assert chain[2].payload["model_version"] == "kronos-small@ff01"


def test_a_correlation_id_traces_the_same_chain_as_an_order_id(trail):
    cid = _record_chain(trail)
    assert trail.trace(cid) == trail.trace("olymp-1")


def test_tracing_works_from_any_identifier_in_the_chain(trail):
    _record_chain(trail)
    for identifier in ("intent-9", "olymp-1"):
        assert [e.type for e in trail.trace(identifier)] == list(CAUSAL_CHAIN)


def test_a_trace_is_causally_ordered_despite_identical_timestamps(trail):
    """A FixedClock stamps every event the same second; the story must still
    read in the order it happened rather than in whatever order ties break."""
    _record_chain(trail)
    chain = trail.trace("olymp-1")
    assert len({e.ts for e in chain}) == 1
    assert [e.type for e in chain] == list(CAUSAL_CHAIN)


def test_two_concurrent_decisions_do_not_bleed_into_each_others_traces(trail):
    _record_chain(trail, cid="corr-a", order_id="olymp-1")
    _record_chain(trail, cid="corr-b", order_id="olymp-2")
    chain = trail.trace("olymp-2")
    assert len(chain) == len(CAUSAL_CHAIN)
    assert {e.correlation_id for e in chain} == {"corr-b"}


def test_tracing_an_unknown_order_returns_nothing(trail):
    _record_chain(trail)
    assert trail.trace("never-existed") == []
    assert trail.trace("") == []


def test_correlation_ids_are_unique(trail):
    ids = {new_correlation_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(i.startswith("corr-") for i in ids)


# --- tamper evidence -------------------------------------------------------

@pytest.mark.requires_crypto
def test_an_untouched_trail_verifies(trail):
    _record_chain(trail)
    result = trail.verify()
    assert result["ok"] is True
    assert result["count"] == len(CAUSAL_CHAIN)
    assert result["verified"] == len(CAUSAL_CHAIN)


@pytest.mark.requires_crypto
def test_editing_a_recorded_payload_is_detected(clock):
    """The edit under test is the one a person covering their tracks makes:
    turn the rejection that stopped a trade into an approval."""
    trail = AuditTrail("tamper", clock=clock)
    trail.record(EventType.RISK_DECISION, {"verdict": "rejected"},
                 subject=SUBJ, correlation_id="c")
    assert trail.verify()["ok"] is True

    path = ledger._path("trading-tamper")
    path.write_text(path.read_text(encoding="utf-8").replace("rejected", "approved"),
                    encoding="utf-8")

    result = AuditTrail("tamper", clock=clock).verify()
    assert result["ok"] is False
    assert result["verified"] == 0


@pytest.mark.requires_crypto
def test_reordering_events_is_detected(clock):
    """Each node commits to its parent's hash, so swapping two events breaks
    the chain even though every individual record is untouched."""
    trail = AuditTrail("reorder", clock=clock)
    for i in range(4):
        trail.record(EventType.FILL, {"n": i}, subject=SUBJ, correlation_id="c")

    path = ledger._path("trading-reorder")
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1], lines[2] = lines[2], lines[1]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert AuditTrail("reorder", clock=clock).verify()["ok"] is False


@pytest.mark.requires_crypto
def test_appending_a_forged_event_is_detected(clock):
    """Someone who understands the format still cannot mint a valid node
    without the signing key."""
    trail = AuditTrail("append", clock=clock)
    trail.record(EventType.FILL, {"n": 0}, subject=SUBJ, correlation_id="c")

    path = ledger._path("trading-append")
    genuine = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    forged = dict(genuine, seq=1, parent=genuine["node_hash"])
    forged["state"] = dict(genuine["state"], event_id="forged")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(forged) + "\n")

    result = AuditTrail("append", clock=clock).verify()
    assert result["ok"] is False
    assert result["verified"] == 1                   # the honest prefix is located


# --- degradation, without ever losing the record ---------------------------

def test_events_are_still_recorded_when_signing_is_unavailable(clock, monkeypatch):
    """Losing tamper-evidence is bad; losing the record is worse. The trail
    keeps writing, and `verify()` stops claiming the chain is trustworthy."""
    def broken(*args, **kwargs):
        raise witness.WitnessError("no crypto backend")

    monkeypatch.setattr(witness, "sub_public_key_hex", broken)
    trail = AuditTrail("degraded", clock=clock)
    trail.record(EventType.FILL, {"n": 1}, subject=SUBJ, correlation_id="c")

    assert len(trail.events()) == 1                  # recorded anyway
    assert trail.verify()["ok"] is False             # but not attested


def test_a_failed_durable_write_is_loud(clock):
    """Silently dropping an event would leave a caller believing it recorded
    something it did not — the one failure mode worse than crashing."""
    trail = AuditTrail("../escape", clock=clock)
    with pytest.raises(TradingError):
        trail.record(EventType.FILL, {"n": 1}, subject=SUBJ)


def test_an_unrecognised_event_type_is_recorded_with_its_label(trail):
    """Refusing to log something because its label is unfamiliar is the wrong
    failure, and downgrading it must not erase what was meant."""
    event = trail.record("teleported_to_mars", {"x": 1})
    assert event.type is EventType.ERROR
    assert event.payload["unrecognised_event_type"] == "teleported_to_mars"


def test_an_unbacked_trail_still_records_and_admits_it_is_not_evidence():
    trail = AuditTrail("memory-only", clock=FixedClock(T0), ledger_backed=False)
    trail.record(EventType.FILL, {"n": 1}, subject=SUBJ, correlation_id="c")
    assert len(trail.events()) == 1
    assert trail.verify()["problems"] == ["not ledger-backed"]


# --- redaction -------------------------------------------------------------

def test_credential_shaped_values_never_reach_the_trail(trail):
    """The trail is the one place you cannot go back and edit, so a leaked
    secret there is permanent. Both shapes are covered: a key whose *name*
    means secret, and a value whose *form* does."""
    event = trail.record(EventType.CONFIG_CHANGED, {
        "api_key": "not-even-key-shaped",
        "broker": {"credentials": {"password": "hunter2"}},
        "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhZG1pbiJ9.c2ln",
        "aws": "AKIAIOSFODNN7EXAMPLE",
        "dsn": "https://admin:s3cret@broker.example.com/stream",
        "pem": "-----BEGIN PRIVATE KEY-----\nMIIBVg==\n-----END PRIVATE KEY-----",
        "max_order_value": "1000",
    })
    payload = event.payload
    assert payload["api_key"] == REDACTED
    assert payload["broker"]["credentials"] == REDACTED
    assert REDACTED in payload["jwt"] and "eyJzdWIiOiJhZG1pbiJ9" not in payload["jwt"]
    assert REDACTED in payload["aws"]
    assert "s3cret" not in payload["dsn"]
    assert "MIIBVg==" not in payload["pem"]
    assert payload["max_order_value"] == "1000"      # ordinary config survives


def test_redaction_reaches_the_durable_record_not_just_the_return_value(trail):
    """The returned event being clean would prove nothing if the bytes on disk
    still held the secret."""
    trail.record(EventType.CONFIG_CHANGED, {"api_secret": "sk-live-abcdefghijkl"},
                 subject=SUBJ)
    raw = ledger._path("trading-audit-run").read_text(encoding="utf-8")
    assert "sk-live-abcdefghijkl" not in raw
    assert REDACTED in raw


def test_redaction_is_recursive_and_leaves_everything_else_alone():
    out = redact({"a": [{"token": "x"}, "plain", 5], "n": 5, "ok": True,
                  "note": "spread is 3bps"})
    assert out["a"][0]["token"] == REDACTED
    assert out["a"][1] == "plain"
    assert out["a"][2] == 5
    assert out["n"] == 5 and out["ok"] is True
    assert out["note"] == "spread is 3bps"


def test_redaction_is_case_insensitive_about_key_names():
    assert redact({"API_KEY": "x", "Authorization": "Bearer y"}) == {
        "API_KEY": REDACTED, "Authorization": REDACTED}

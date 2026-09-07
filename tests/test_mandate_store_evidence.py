"""P2S: mandate replay evidence fails closed across corruption and races."""

from __future__ import annotations

import concurrent.futures
import json

import pytest

from olympus import actions, mandate, mandate_store, memory, store, witness
from olympus import builtin_actions  # noqa: F401  (registers built-ins)

pytestmark = pytest.mark.skipif(not witness.available(),
                                reason="cryptography backend required")


def _pair(user: str, suffix: str = "one"):
    intent = mandate.create_intent(
        user,
        amount_cap=15_000,
        currency="USD",
        merchants=["acme"],
        item="running shoes",
        trusted=True,
        nonce=f"intent-{suffix}",
        now=100.0,
        expires_in=10_000.0,
    )
    cart = mandate.create_cart(
        intent,
        amount=12_000,
        currency="USD",
        merchant="acme",
        items=["Shoe X"],
        nonce=f"cart-{suffix}",
        now=101.0,
    )
    return mandate.sign(intent), mandate.co_sign(mandate.sign(cart))


@pytest.mark.parametrize("namespace, raw", [
    ("mandates", b"{not-json"),
    ("mandates", b"{}"),
    ("mandates", b"\xff\xfe"),
    ("mandate_nonces", b"{not-json"),
    ("mandate_nonces", b"{}"),
    ("mandate_nonces", b'["", 7]'),
])
def test_existing_invalid_blob_blocks_both_views_and_preserves_bytes(
        namespace, raw):
    user = "corrupt-owner"
    key = memory.storage_key(user)
    store.backend().put(namespace, key, raw)

    with pytest.raises(mandate_store.MandateStateError):
        mandate_store.records(user)
    with pytest.raises(mandate_store.MandateStateError):
        mandate_store.consumed_nonces(user)

    assert store.backend().get(namespace, key) == raw
    status = mandate_store.evidence_status(user)
    assert status["state"] == "unavailable"
    assert status["records"] is None
    assert status["consumed_nonces"] is None
    assert raw.decode("utf-8", "replace") not in json.dumps(status)


def test_record_without_its_nonce_evidence_blocks_both_views():
    user = "missing-nonce"
    signed_intent, signed_cart = _pair(user)
    mandate_store.record(user, signed_intent, signed_cart)
    key = memory.storage_key(user)
    store.backend().put("mandate_nonces", key, b"[]")

    with pytest.raises(mandate_store.MandateStateError,
                       match="missing consumed-nonce evidence"):
        mandate_store.records(user)
    with pytest.raises(mandate_store.MandateStateError,
                       match="missing consumed-nonce evidence"):
        mandate_store.consumed_nonces(user)


def test_tampered_record_payload_blocks_both_views_and_is_preserved():
    user = "tampered-record"
    mandate_store.record(user, *_pair(user))
    key = memory.storage_key(user)
    blob = store.backend().get("mandates", key)
    rows = json.loads(blob)
    rows[0]["signed_cart"]["payload"]["amount"] = 1
    tampered = json.dumps(rows).encode("utf-8")
    store.backend().put("mandates", key, tampered)

    with pytest.raises(mandate_store.MandateStateError,
                       match="mandates row 0 is malformed"):
        mandate_store.records(user)
    with pytest.raises(mandate_store.MandateStateError):
        mandate_store.consumed_nonces(user)

    assert store.backend().get("mandates", key) == tampered


def test_corrupt_nonce_evidence_fails_the_real_authorization_action():
    user = "u"
    key = memory.storage_key(user)
    raw = b'["known",'
    store.backend().put("mandate_nonces", key, raw)
    actions.grant_scope(user, "payment.authorize")
    prepared = actions.prepare(user, "authorize_payment", {
        "_user": user,
        "amount": 12_000,
        "currency": "USD",
        "merchant": "acme",
        "items": ["Shoe X"],
        "amount_cap": 15_000,
        "merchants": ["acme"],
        "item": "running shoes",
        "expires_in": 3600,
    })

    done = actions.approve(user, prepared.id)

    assert done.status == actions.FAILED
    assert "mandate replay evidence" in done.error.lower()
    assert store.backend().get("mandate_nonces", key) == raw
    assert store.backend().get("mandates", key) is None


def test_colliding_safe_ids_have_independent_records_and_nonces():
    first = "tg-a.b"
    second = "tg-a-b"
    assert memory.safe_id(first) == memory.safe_id(second)
    assert memory.storage_key(first) != memory.storage_key(second)

    first_pair = _pair(first, "first")
    second_pair = _pair(second, "second")
    mandate_store.record(first, *first_pair)

    assert [row["user"] for row in mandate_store.records(first)] == [first]
    assert mandate_store.records(second) == []
    assert mandate_store.consumed_nonces(second) == set()

    mandate_store.record(second, *second_pair)
    assert [row["user"] for row in mandate_store.records(second)] == [second]
    assert "cart-first" not in mandate_store.consumed_nonces(second)
    assert "cart-second" not in mandate_store.consumed_nonces(first)


@pytest.mark.parametrize("namespace", ["mandates", "mandate_nonces"])
def test_unattributed_legacy_safe_id_blob_is_never_claimed(namespace):
    user = "tg-a.b"
    legacy_key = memory.safe_id(user)
    raw = b"[]"
    store.backend().put(namespace, legacy_key, raw)

    with pytest.raises(mandate_store.MandateStateError,
                       match="unattributed legacy safe-id"):
        mandate_store.records(user)
    with pytest.raises(mandate_store.MandateStateError):
        mandate_store.record(user, *_pair(user))

    assert store.backend().get(namespace, legacy_key) == raw
    assert store.backend().get(namespace, memory.storage_key(user)) is None


def test_second_record_of_same_cart_is_refused_inside_store_lock():
    user = "replay-owner"
    signed_intent, signed_cart = _pair(user)
    mandate_store.record(user, signed_intent, signed_cart)

    with pytest.raises(mandate_store.MandateReplayError,
                       match="already consumed"):
        mandate_store.record(user, signed_intent, signed_cart)

    assert len(mandate_store.records(user)) == 1


def test_concurrent_same_nonce_has_one_winner_and_one_replay():
    user = "race-owner"
    signed_intent, signed_cart = _pair(user)

    def attempt():
        try:
            mandate_store.record(user, signed_intent, signed_cart)
            return "recorded"
        except mandate_store.MandateReplayError:
            return "replay"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: attempt(), range(2)))

    assert sorted(results) == ["recorded", "replay"]
    assert len(mandate_store.records(user)) == 1


def test_record_publish_failure_burns_nonce_and_blocks_retry(monkeypatch):
    user = "interrupted-owner"
    signed_intent, signed_cart = _pair(user)
    real = store.backend()

    class FailRecordPublish:
        def get(self, namespace, key):
            return real.get(namespace, key)

        def put(self, namespace, key, value):
            if namespace == "mandates":
                raise OSError("simulated record publish failure")
            return real.put(namespace, key, value)

    monkeypatch.setattr(store, "backend", lambda: FailRecordPublish())

    with pytest.raises(OSError, match="simulated record publish failure"):
        mandate_store.record(user, signed_intent, signed_cart)

    assert "cart-one" in mandate_store.consumed_nonces(user)
    assert mandate_store.records(user) == []
    with pytest.raises(mandate_store.MandateReplayError):
        mandate_store.record(user, signed_intent, signed_cart)


def test_full_nonce_ledger_refuses_instead_of_evicting(monkeypatch):
    user = "bounded-owner"
    monkeypatch.setattr(mandate_store, "_MAX_NONCES", 2)
    mandate_store.record(user, *_pair(user, "first"))
    before = mandate_store.consumed_nonces(user)

    with pytest.raises(mandate_store.MandateStateError,
                       match="replay evidence was not evicted"):
        mandate_store.record(user, *_pair(user, "second"))

    assert mandate_store.consumed_nonces(user) == before
    assert len(mandate_store.records(user)) == 1


def test_system_owner_keeps_its_literal_compatible_key():
    mandate_store.record("shared", *_pair("shared"))

    assert store.backend().get("mandates", "shared") is not None
    assert store.backend().get("mandate_nonces", "shared") is not None
    assert len(mandate_store.records("shared")) == 1

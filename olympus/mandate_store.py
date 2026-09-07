"""Durable AP2 authorization records and single-use nonce evidence.

This is the persistence behind the user-facing mandate flow (ADR 0004). It
stores signed, verified authorization artifacts and the nonces they consume.
It records authorizations only -- **no money moves here**.

Security contract:

* a missing blob is valid first-use state; an existing invalid blob is not;
* tenant state is keyed by the exact durable principal, never ``safe_id``;
* legacy lossy-key state is preserved but cannot be claimed implicitly;
* read/modify/write is serialized across processes; and
* nonce consumption is published before the record. If the second publish
  fails, the nonce is burned and retry is refused rather than replay-enabled.
"""

from __future__ import annotations

import json
import math
import threading
import time
from typing import Any

from . import memory, proclock, store

_NS = "mandates"
_NONCE_NS = "mandate_nonces"
_MAX_RECORDS = 500          # rolling per-user authorization history
_MAX_NONCES = 10_000        # refuse when full; NEVER evict replay evidence
_LOCK = threading.Lock()


class MandateStateError(RuntimeError):
    """Stored mandate/replay evidence is missing integrity.

    This error is deliberately distinct from first use. Turning malformed or
    unattributed state into empty collections would erase the only fact that a
    nonce was already consumed and can authorize a replay.
    """

    def __init__(self, user: str, reason: str) -> None:
        self.user = memory.canonical_owner(user)
        self.reason = str(reason)
        super().__init__(
            f"Mandate replay evidence for '{self.user}' is unavailable "
            f"({self.reason}). Authorization reads and writes are refused; "
            "preserve the stored blobs for operator inspection.")


class MandateReplayError(MandateStateError):
    """The cart nonce is already durably consumed."""

    def __init__(self, user: str) -> None:
        super().__init__(user, "cart nonce is already consumed")


def _key(user: str) -> str:
    """Collision-resistant key for one exact owner."""
    return memory.storage_key(user)


def _read_blob(user: str, namespace: str, key: str) -> bytes | None:
    try:
        return store.backend().get(namespace, key)
    except Exception as err:
        raise MandateStateError(
            user,
            f"{namespace} storage read failed: {type(err).__name__}",
        ) from err


def _legacy_namespaces(user: str) -> list[str]:
    """Lossy-key blobs which cannot be attributed to this tenant.

    Reserved system owners intentionally retain their literal historical key.
    Every tenant moved from ``safe_id`` to ``storage_key``; a legacy blob may
    belong to any member of the collision group and is therefore a stop state,
    even when an exact-owner blob has subsequently been created.
    """
    exact = memory.canonical_owner(user)
    if memory.is_system_owner(exact):
        return []
    legacy_key = memory.safe_id(exact)
    return [namespace for namespace in (_NS, _NONCE_NS)
            if _read_blob(exact, namespace, legacy_key) is not None]


def _decode_list(user: str, namespace: str, blob: bytes | None) -> list:
    if blob is None:
        return []
    try:
        text = blob.decode("utf-8")
    except (AttributeError, UnicodeDecodeError) as err:
        raise MandateStateError(user, f"{namespace} is not valid UTF-8") from err
    try:
        data = json.loads(text)
    except json.JSONDecodeError as err:
        raise MandateStateError(user, f"{namespace} is malformed JSON") from err
    if not isinstance(data, list):
        raise MandateStateError(user, f"{namespace} root is not an array")
    return data


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _record_nonces(user: str, row: object, index: int) -> tuple[str, str]:
    """Validate the persisted record shape and return intent/cart nonces."""
    bad = f"mandates row {index} is malformed"
    if not isinstance(row, dict):
        raise MandateStateError(user, bad)

    exact = memory.canonical_owner(user)
    if row.get("user") != exact or row.get("moved_money") is not False:
        raise MandateStateError(user, bad)
    stamp = row.get("recorded_at")
    if (isinstance(stamp, bool) or not isinstance(stamp, (int, float))
            or not math.isfinite(stamp)):
        raise MandateStateError(user, bad)

    signed_intent = row.get("signed_intent")
    signed_cart = row.get("signed_cart")
    if not isinstance(signed_intent, dict) or not isinstance(signed_cart, dict):
        raise MandateStateError(user, bad)
    intent = signed_intent.get("payload")
    cart = signed_cart.get("payload")
    if not isinstance(intent, dict) or not isinstance(cart, dict):
        raise MandateStateError(user, bad)

    intent_nonce = intent.get("nonce")
    cart_nonce = cart.get("nonce")
    required = (
        signed_intent.get("kind") == "intent",
        signed_cart.get("kind") == "cart",
        intent.get("kind") == "intent",
        cart.get("kind") == "cart",
        intent.get("user") == exact,
        cart.get("user") == exact,
        _nonempty_string(intent.get("id")),
        _nonempty_string(cart.get("id")),
        _nonempty_string(intent_nonce),
        _nonempty_string(cart_nonce),
        _nonempty_string(signed_intent.get("public_key")),
        _nonempty_string(signed_intent.get("signature")),
        _nonempty_string(signed_cart.get("public_key")),
        _nonempty_string(signed_cart.get("signature")),
        _nonempty_string(signed_cart.get("user_public_key")),
        _nonempty_string(signed_cart.get("user_signature")),
        row.get("nonce") == cart_nonce,
        row.get("id") == cart.get("id"),
        row.get("intent_id") == intent.get("id"),
        cart.get("intent_id") == intent.get("id"),
        row.get("amount") == cart.get("amount"),
        row.get("currency") == cart.get("currency") == intent.get("currency"),
        row.get("merchant") == cart.get("merchant"),
        row.get("amount_cap") == intent.get("amount_cap"),
        intent.get("trusted") is True,
    )
    if not all(required):
        raise MandateStateError(user, bad)
    return intent_nonce, cart_nonce


def _state(user: str) -> tuple[list[dict], list[str]]:
    """Load and cross-check both halves of one owner's replay state."""
    exact = memory.canonical_owner(user)
    legacy = _legacy_namespaces(exact)
    if legacy:
        raise MandateStateError(
            exact,
            "unattributed legacy safe-id blobs exist in " + ", ".join(legacy),
        )

    key = _key(exact)
    rows = _decode_list(exact, _NS, _read_blob(exact, _NS, key))
    nonce_values = _decode_list(
        exact, _NONCE_NS, _read_blob(exact, _NONCE_NS, key))

    if len(rows) > _MAX_RECORDS:
        raise MandateStateError(exact, "mandate history exceeds its bound")
    if len(nonce_values) > _MAX_NONCES:
        raise MandateStateError(exact, "nonce ledger exceeds its bound")
    if any(not _nonempty_string(value) for value in nonce_values):
        raise MandateStateError(exact, "nonce ledger contains an invalid nonce")
    if len(set(nonce_values)) != len(nonce_values):
        raise MandateStateError(exact, "nonce ledger contains duplicates")

    required_nonces: set[str] = set()
    for index, row in enumerate(rows):
        required_nonces.update(_record_nonces(exact, row, index))
    if not required_nonces.issubset(set(nonce_values)):
        raise MandateStateError(
            exact, "an authorization record has missing consumed-nonce evidence")
    return rows, nonce_values


def consumed_nonces(user: str) -> set[str]:
    """The nonces already used by this exact owner's recorded mandates.

    Orphan nonces from an interrupted nonce-first write are returned too. They
    are intentional fail-safe tombstones and continue to block retry.
    """
    _, nonces = _state(user)
    return set(nonces)


def records(user: str) -> list[dict]:
    """Validated authorization records for one exact owner."""
    rows, _ = _state(user)
    return rows


def _put(user: str, namespace: str, data: list) -> None:
    encoded = json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    store.backend().put(namespace, _key(user), encoded)


def record(user: str, signed_intent: Any, signed_cart: Any) -> dict:
    """Append one already-verified mandate and atomically consume its nonce.

    The caller must still run ``mandate.enforce_commit``. This persistence
    boundary independently re-checks durable freshness under the same lock as
    the writes, closing the time-of-check/time-of-use race between concurrent
    approvals.
    """
    exact = memory.canonical_owner(user)
    intent_payload = getattr(signed_intent, "payload", {}) or {}
    cart_payload = getattr(signed_cart, "payload", {}) or {}
    rec = {
        "id": cart_payload.get("id", ""),
        "intent_id": intent_payload.get("id", ""),
        "user": exact,
        "amount": cart_payload.get("amount"),
        "currency": cart_payload.get("currency"),
        "merchant": cart_payload.get("merchant"),
        "amount_cap": intent_payload.get("amount_cap"),
        "nonce": cart_payload.get("nonce", ""),
        "signed_intent": getattr(signed_intent, "to_dict", dict)()
        if hasattr(signed_intent, "to_dict") else {},
        "signed_cart": getattr(signed_cart, "to_dict", dict)()
        if hasattr(signed_cart, "to_dict") else {},
        "recorded_at": time.time(),
        "moved_money": False,
    }
    intent_nonce, cart_nonce = _record_nonces(exact, rec, 0)

    # The module lock gives one predictable lock order in-process. Proclock is
    # the machine-wide serialization point for the FileStore deployment model.
    with _LOCK:
        with proclock.lock(f"mandate-{_key(exact)}"):
            rows, nonces = _state(exact)
            if cart_nonce in nonces:
                raise MandateReplayError(exact)

            updated_nonces = list(nonces)
            for nonce in (intent_nonce, cart_nonce):
                if nonce not in updated_nonces:
                    updated_nonces.append(nonce)
            if len(updated_nonces) > _MAX_NONCES:
                raise MandateStateError(
                    exact, "nonce ledger is full; replay evidence was not evicted")

            # Publish the replay tombstone FIRST. If the record publish fails,
            # the authorization may be absent but its nonce remains consumed.
            _put(exact, _NONCE_NS, updated_nonces)
            updated_rows = [*rows, rec][-_MAX_RECORDS:]
            _put(exact, _NS, updated_rows)
    return rec


def evidence_status(user: str) -> dict:
    """Non-sensitive operator view of mandate/replay evidence health."""
    exact = memory.canonical_owner(user)
    try:
        rows, nonces = _state(exact)
    except MandateStateError as err:
        return {
            "owner": exact,
            "state": "unavailable",
            "reason": err.reason,
            "records": None,
            "consumed_nonces": None,
        }
    key = _key(exact)
    present = any(_read_blob(exact, namespace, key) is not None
                  for namespace in (_NS, _NONCE_NS))
    return {
        "owner": exact,
        "state": "valid" if present else "missing",
        "reason": None,
        "records": len(rows),
        "consumed_nonces": len(nonces),
    }

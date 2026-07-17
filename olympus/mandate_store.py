"""Append-only record of issued AP2 payment mandates + consumed nonces.

This is the persistence behind the user-facing mandate flow (ADR 0004). It
stores the signed, verified authorization artifacts and the set of nonces that
have been consumed, so a mandate can never be recorded twice (replay defense).
It records authorizations only — **no money moves here**.
"""

from __future__ import annotations

import json
import time
from typing import Any

from . import memory, store

_NS = "mandates"
_NONCE_NS = "mandate_nonces"
_MAX_RECORDS = 500          # bounded per-user history


def _key(user: str) -> str:
    return memory.safe_id(user)


def consumed_nonces(user: str) -> set[str]:
    """The nonces already used by this user's recorded mandates."""
    blob = store.backend().get(_NONCE_NS, _key(user))
    if not blob:
        return set()
    try:
        data = json.loads(blob)
    except (ValueError, json.JSONDecodeError):
        return set()
    return set(data) if isinstance(data, list) else set()


def _save_nonces(user: str, nonces: set[str]) -> None:
    # bound the set the same way as records — keep the most recent
    trimmed = list(nonces)[-_MAX_RECORDS * 2:]
    store.backend().put(_NONCE_NS, _key(user),
                        json.dumps(trimmed).encode("utf-8"))


def records(user: str) -> list[dict]:
    blob = store.backend().get(_NS, _key(user))
    if not blob:
        return []
    try:
        data = json.loads(blob)
    except (ValueError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def record(user: str, signed_intent: Any, signed_cart: Any) -> dict:
    """Append a verified mandate authorization. The caller MUST have already run
    `mandate.enforce_commit` (the payment.mandate ABC contract) — this only
    persists. Consumes the cart's nonce so it can't be recorded again."""
    intent_payload = getattr(signed_intent, "payload", {}) or {}
    cart_payload = getattr(signed_cart, "payload", {}) or {}
    rec = {
        "id": cart_payload.get("id", ""),
        "intent_id": intent_payload.get("id", ""),
        "user": user,
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
    cur = records(user)
    cur.append(rec)
    store.backend().put(_NS, _key(user),
                        json.dumps(cur[-_MAX_RECORDS:]).encode("utf-8"))
    nonces = consumed_nonces(user)
    for n in (cart_payload.get("nonce"), intent_payload.get("nonce")):
        if n:
            nonces.add(str(n))
    _save_nonces(user, nonces)
    return rec

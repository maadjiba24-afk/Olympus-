"""Secrets vault — encrypted-at-rest storage for OAuth tokens and credentials.

OAuth tokens are the keys to a user's email and calendar; they must never sit
in plaintext on disk. This module encrypts them with a key derived from
OLYMPUS_SECRET_KEY (any passphrase) using Fernet (AES-128-CBC + HMAC).

Graceful degradation: if `cryptography` isn't installed or no secret key is
set, the vault refuses to store secrets rather than writing them in the clear,
and tells the caller exactly what's missing. So a misconfiguration fails safe.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os

from . import config, memory, store  # noqa: F401  (config used elsewhere)

try:
    from cryptography.fernet import Fernet, InvalidToken
    Fernet(Fernet.generate_key())          # prove the backend actually works
    _HAVE_CRYPTO = True
except BaseException:  # ImportError, or a broken/panicking native backend
    _HAVE_CRYPTO = False
    InvalidToken = Exception               # fallback so references don't NameError


class VaultError(RuntimeError):
    pass


def _fernet() -> "Fernet":
    if not _HAVE_CRYPTO:
        raise VaultError(
            "the 'cryptography' package is required to store credentials "
            "securely — `pip install cryptography`.")
    passphrase = os.environ.get("OLYMPUS_SECRET_KEY")
    if not passphrase:
        raise VaultError(
            "OLYMPUS_SECRET_KEY is not set — required to encrypt stored "
            "credentials. Set it to any strong, stable secret string.")
    # Derive a 32-byte Fernet key deterministically from the passphrase.
    key = base64.urlsafe_b64encode(hashlib.sha256(passphrase.encode()).digest())
    return Fernet(key)


def available() -> bool:
    """True if the vault can actually encrypt/decrypt (deps + key present)."""
    try:
        _fernet()
        return True
    except VaultError:
        return False


_NS = "vault"


def _load(user: str) -> dict:
    blob = store.backend().get(_NS, memory.safe_id(user))
    if not blob:
        return {}
    try:
        return json.loads(_fernet().decrypt(blob).decode())
    except (InvalidToken, ValueError, json.JSONDecodeError):
        raise VaultError("could not decrypt vault — wrong OLYMPUS_SECRET_KEY?")


def _store(user: str, data: dict) -> None:
    blob = _fernet().encrypt(json.dumps(data).encode())
    store.backend().put(_NS, memory.safe_id(user), blob)


def put(user: str, name: str, value: dict | str) -> None:
    """Store a secret (e.g. a token bundle) for a user, encrypted."""
    data = _load(user)
    data[name] = value
    _store(user, data)


def get(user: str, name: str) -> dict | str | None:
    return _load(user).get(name)


def delete(user: str, name: str) -> None:
    data = _load(user)
    if name in data:
        del data[name]
        _store(user, data)


def names(user: str) -> list[str]:
    return sorted(_load(user).keys())

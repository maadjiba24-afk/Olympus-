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


def encrypt_bytes(data: bytes) -> bytes:
    """Encrypt arbitrary bytes with the vault key (Fernet: AES-128-CBC + HMAC).
    Used for at-rest encryption of backup archives. Raises VaultError if no key
    or crypto backend is available, so a misconfiguration fails safe rather than
    writing plaintext."""
    return _fernet().encrypt(data)


def decrypt_bytes(token: bytes) -> bytes:
    """Inverse of `encrypt_bytes`. Raises VaultError on a wrong key or tampered
    ciphertext (Fernet authenticates, so corruption is detected, not silently
    returned)."""
    try:
        return _fernet().decrypt(token)
    except (InvalidToken, ValueError):
        raise VaultError("could not decrypt — wrong OLYMPUS_SECRET_KEY or the "
                         "data was corrupted/tampered.")


_NS = "vault"

#: Bumped when the vault key changed from `safe_id(user)` to
#: `memory.storage_key(user)`. See `legacy_tenants` for the migration path.
VAULT_KEY_VERSION = 2


def _key(user: str) -> str:
    """The vault key for a principal.

    THE CENTRAL FIX. This used to be `memory.safe_id(user)`, which collapses
    every run of non-`[A-Za-z0-9_-]` to a single `-` and truncates at 64
    characters. `tg-a.b`, `tg-a@b`, `tg-a b` and `tg-a-b` therefore shared ONE
    encrypted vault, as did any two ids agreeing on their first 64 sanitized
    characters — so `google_oauth.access_token` handed one principal another's
    OAuth bundle and `gmail` read another person's mailbox with it. Saved site
    passwords and browser session cookies merged the same way.

    `memory.storage_key` keys a tenant on the exact principal plus its complete
    SHA-256 digest, and keeps the reserved installation namespaces (`shared`,
    `operator` — the `opconfig`/`secretref` config-secret store) on their
    literal names so installation code that hardcodes them is unaffected.
    """
    return memory.storage_key(user)


def _load(user: str) -> dict:
    # A pre-v2 gateway principal was derived with safe_id and may represent
    # several external accounts. Never expose credentials under an identity
    # that the authenticated transport can no longer prove.
    if memory.is_ambiguous_gateway_owner(user):
        return {}
    blob = store.backend().get(_NS, _key(user))
    if not blob:
        return {}
    try:
        return json.loads(_fernet().decrypt(blob).decode())
    except (InvalidToken, ValueError, json.JSONDecodeError):
        raise VaultError("could not decrypt vault — wrong OLYMPUS_SECRET_KEY?")


def _store(user: str, data: dict) -> None:
    if memory.is_ambiguous_gateway_owner(user):
        raise VaultError(
            "cannot write credentials for an ambiguous pre-v2 gateway owner; "
            "map the account to its verified v2 principal first")
    blob = _fernet().encrypt(json.dumps(data).encode())
    store.backend().put(_NS, _key(user), blob)


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


# --- pre-v2 vaults: quarantined, never implicitly claimed -----------------
#
# A vault written before `VAULT_KEY_VERSION` 2 is keyed by a `safe_id` value
# that MORE THAN ONE principal maps to, so nothing in the record says which of
# them owns it. It is therefore unreachable through `get`/`names`/`put`: there
# is no fallback, not even for a principal whose exact identity happens to
# equal that normalized string. Equalling a lossy value is not proof of
# ownership — it is the one thing every collision victim also does.
#
# The consequence is deliberate and must be stated: an affected tenant is
# simply NOT CONNECTED any more (`google_oauth.connected` is False, saved site
# logins and cookies are gone) and must reconnect, or an operator must claim
# the vault explicitly with `migrate_legacy` after establishing out of band
# whose it is.


def legacy_tenants() -> list[str]:
    """Vault keys left over from the pre-v2 `safe_id` layout.

    A key is legacy when it is neither a reserved installation namespace nor a
    well-formed `owner_key` (a label plus a 64-character SHA-256 digest).
    Operator inspection only — reading one still requires `migrate_legacy`.
    """
    import re as _re
    out = []
    for key in store.backend().keys(_NS):
        if key in memory.SYSTEM_OWNERS:
            continue
        if _re.fullmatch(r".*-[0-9a-f]{64}", key):
            continue
        out.append(key)
    return sorted(out)


def legacy_names(legacy_key: str) -> list[str]:
    """The entry names inside a quarantined vault, so an operator can see what
    would be migrated. Never returns the secret values themselves."""
    blob = store.backend().get(_NS, legacy_key)
    if not blob:
        return []
    try:
        return sorted(json.loads(_fernet().decrypt(blob).decode()).keys())
    except (InvalidToken, ValueError, json.JSONDecodeError):
        raise VaultError("could not decrypt vault — wrong OLYMPUS_SECRET_KEY?")


def migrate_legacy(legacy_key: str, owner: str, *, overwrite: bool = False) -> int:
    """Claim a quarantined vault for an EXACT principal. Operator action.

    This is the explicit decision the code refuses to make: the operator states
    out of band which principal owned `legacy_key`. Entries are merged into
    that principal's vault (existing entries win unless `overwrite`), and the
    legacy key is removed so it cannot be claimed twice.

    Returns the number of entries migrated.
    """
    exact = memory.assert_not_system_owner(owner)
    if memory.is_ambiguous_gateway_owner(exact):
        raise VaultError(
            "legacy vaults may only be migrated to a verified v2 principal")
    if legacy_key not in legacy_tenants():
        raise VaultError(f"'{legacy_key}' is not a quarantined legacy vault")
    blob = store.backend().get(_NS, legacy_key)
    if not blob:
        return 0
    try:
        legacy = json.loads(_fernet().decrypt(blob).decode())
    except (InvalidToken, ValueError, json.JSONDecodeError):
        raise VaultError("could not decrypt vault — wrong OLYMPUS_SECRET_KEY?")
    data = _load(exact)
    moved = 0
    for name, value in legacy.items():
        if name in data and not overwrite:
            continue
        data[name] = value
        moved += 1
    _store(exact, data)
    store.backend().delete(_NS, legacy_key)
    return moved


def discard_legacy(legacy_key: str) -> bool:
    """Delete a quarantined vault outright — the other operator resolution when
    nobody can establish who owned it. Returns False if it was not quarantined.
    """
    if legacy_key not in legacy_tenants():
        return False
    store.backend().delete(_NS, legacy_key)
    return True


# --- outbound scanning of a quarantined vault ----------------------------
#
# Quarantine removes a legacy vault from every CREDENTIAL path: nothing may
# authenticate with a secret whose owner is unknown. But the always-on outbound
# secret floor is the opposite kind of check — it decides what must NOT LEAVE,
# and there the conservative answer is to scan MORE, not less. An earlier
# revision let quarantined secrets drop out of that scan entirely, which meant a
# legacy OAuth token could be emitted verbatim by any principal in the collision
# group. Scanning them back in can only ever cause a refusal, never a
# disclosure.
#
# Values never leave this module. `legacy_scan` takes a predicate, runs it on
# the decrypted leaves in-process, and returns only a GENERIC reason string —
# no secret, no label, no entry name, no vault key, no ciphertext. There is
# deliberately nothing here for a caller to log.

_LEGACY_MATCH_REASON = (
    "outbound content matches a stored secret from an unmigrated credential "
    "vault that may belong to this identity. An operator must migrate or "
    "discard it before this can be sent.")

_LEGACY_UNREADABLE_REASON = (
    "an unmigrated credential vault that may belong to this identity cannot be "
    "read, so outbound content cannot be checked against it. An operator must "
    "migrate or discard it before this can be sent.")


def _legacy_key_for(user: str) -> str | None:
    """The quarantined vault key for this principal's collision group, if any.

    The group, not the individual: `tg-a.b` and `tg-a-b` both normalize to
    `tg-a-b`, and a legacy vault names only that normalized value, so either
    could be its author and both must be checked against it.
    """
    exact = memory.canonical_owner(user)
    if memory.is_system_owner(exact):
        return None                    # reserved namespaces were never ambiguous
    key = memory.safe_id(exact)
    return key if key in legacy_tenants() else None


def _leaves(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _leaves(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _leaves(v)]
    return []


def legacy_scan(user: str, matches) -> str | None:
    """Check outbound content against this collision group's quarantined vault.

    `matches(secret_value) -> bool` is applied to each decrypted leaf INSIDE
    this function; the caller supplies the encoding-aware comparison and never
    receives a value.

    Returns None when there is nothing to check, and otherwise a generic reason
    string. FAIL CLOSED: if such a vault exists but cannot be decrypted or
    parsed, every send for this collision group is refused until an operator
    resolves it — an unreadable vault is precisely the case where we cannot
    prove the content is safe.
    """
    key = _legacy_key_for(user)
    if key is None:
        return None
    blob = store.backend().get(_NS, key)
    if not blob:
        return None
    try:
        data = json.loads(_fernet().decrypt(blob).decode())
    except (InvalidToken, ValueError, json.JSONDecodeError, VaultError):
        return _LEGACY_UNREADABLE_REASON
    if not isinstance(data, dict):
        return _LEGACY_UNREADABLE_REASON
    for entry in data.values():
        for leaf in _leaves(entry):
            try:
                if matches(leaf):
                    return _LEGACY_MATCH_REASON
            except Exception:
                return _LEGACY_UNREADABLE_REASON
    return None

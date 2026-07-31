"""User accounts — the prerequisite for opening Olympus to untrusted users.

Until now a hosted instance was safe for a *team behind a shared access token*
(unguessable per-browser sessions, but no real identity). This adds real
accounts: register / log in / log out, password hashing with stdlib PBKDF2
(no new dependency), and server-side session tokens. When logged in, a user's
memory/actions/everything is namespaced to their account id, not a cookie.

Accounts are OPT-IN. Local single-user runs need nothing. Set
OLYMPUS_REQUIRE_LOGIN=1 on a shared/public instance to require authentication
for the web API. Stored on the same backend as everything else.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

from . import store

_ACCTS, _SESSIONS = "accounts", "sessions"
_LOCK = threading.Lock()
_PBKDF2_ROUNDS = 200_000
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,64}$")
_MIN_PASSWORD = 8


def require_login() -> bool:
    return os.environ.get("OLYMPUS_REQUIRE_LOGIN", "").lower() in (
        "1", "true", "yes", "on")


def _session_ttl() -> float:
    return float(os.environ.get("OLYMPUS_SESSION_DAYS", "30")) * 86400


# --- password hashing (stdlib, no deps) ----------------------------------

def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)


def _norm(username: str) -> str:
    return (username or "").strip().lower()


# --- account storage -----------------------------------------------------

def _load_acct(username: str) -> dict | None:
    blob = store.backend().get(_ACCTS, _norm(username))
    if not blob:
        return None
    try:
        return json.loads(blob)
    except (ValueError, json.JSONDecodeError):
        return None


def exists(username: str) -> bool:
    return _load_acct(username) is not None


def register(username: str, password: str) -> str:
    """Create an account; returns a fresh session token. Raises ValueError on
    bad input or a taken username."""
    username = (username or "").strip()
    if not _USERNAME_RE.match(username):
        raise ValueError("username must be 3–64 chars: letters, digits, . _ -")
    if len(password or "") < _MIN_PASSWORD:
        raise ValueError(f"password must be at least {_MIN_PASSWORD} characters")
    with _LOCK:
        if store.backend().get(_ACCTS, _norm(username)):
            raise ValueError("that username is taken")
        salt = secrets.token_bytes(16)
        acct = {"id": secrets.token_hex(8), "username": username,
                "salt": salt.hex(), "hash": _hash(password, salt).hex(),
                "created_at": time.time()}
        store.backend().put(_ACCTS, _norm(username),
                            json.dumps(acct).encode())
    return _issue_session(acct)


def authenticate(username: str, password: str) -> str | None:
    """Verify credentials; returns a session token, or None on failure.
    Constant-time comparison; same cost whether or not the user exists."""
    acct = _load_acct(username)
    salt = bytes.fromhex(acct["salt"]) if acct else secrets.token_bytes(16)
    expected = bytes.fromhex(acct["hash"]) if acct else _hash("x", salt)
    got = _hash(password or "", salt)
    if acct and hmac.compare_digest(got, expected):
        return _issue_session(acct)
    return None


# --- session tokens ------------------------------------------------------

def _issue_session(acct: dict) -> str:
    token = secrets.token_urlsafe(32)
    store.backend().put(_SESSIONS, token, json.dumps(
        {"account_id": acct["id"], "username": acct["username"],
         "created_at": time.time()}).encode())
    return token


def account_for_token(token: str) -> dict | None:
    """Resolve a session token to {account_id, username}, honoring expiry."""
    if not token:
        return None
    blob = store.backend().get(_SESSIONS, token)
    if not blob:
        return None
    try:
        sess = json.loads(blob)
    except (ValueError, json.JSONDecodeError):
        return None
    if time.time() - sess.get("created_at", 0) >= _session_ttl():
        store.backend().delete(_SESSIONS, token)      # expired
        return None
    return sess


def logout(token: str) -> None:
    if token:
        store.backend().delete(_SESSIONS, token)


def namespace_for_token(token: str) -> str | None:
    """The per-user memory namespace for a logged-in token, or None."""
    sess = account_for_token(token)
    return f"u:{sess['account_id']}" if sess else None

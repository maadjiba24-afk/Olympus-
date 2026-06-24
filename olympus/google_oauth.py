"""Google OAuth connect flow — per-user, self-serve account linking.

Replaces "paste a token into an env var" with a real flow: the user clicks
Connect, authorizes Google, and Olympus stores the resulting tokens in the
encrypted vault, refreshing them automatically. This is what makes Gmail +
Calendar usable by someone who isn't running shell commands.

Setup (operator, one time): create an OAuth client in Google Cloud Console and
set OLYMPUS_GOOGLE_CLIENT_ID / OLYMPUS_GOOGLE_CLIENT_SECRET, plus a redirect URI
that points at this app's /oauth/google/callback.

Tokens are per user, encrypted at rest (vault.py). Reading them back yields a
live access token, refreshed on demand.
"""

from __future__ import annotations

import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from . import store, vault

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

# Gmail (read/send/modify) + Calendar (events). Least privilege for the MVP.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
    "openid", "email",
]


class OAuthError(RuntimeError):
    pass


def _client() -> tuple[str, str, str]:
    cid = os.environ.get("OLYMPUS_GOOGLE_CLIENT_ID")
    secret = os.environ.get("OLYMPUS_GOOGLE_CLIENT_SECRET")
    redirect = os.environ.get(
        "OLYMPUS_GOOGLE_REDIRECT",
        "http://localhost:8484/oauth/google/callback")
    if not cid or not secret:
        raise OAuthError(
            "Google OAuth not configured — set OLYMPUS_GOOGLE_CLIENT_ID and "
            "OLYMPUS_GOOGLE_CLIENT_SECRET (create an OAuth client in Google "
            "Cloud Console).")
    return cid, secret, redirect


def configured() -> bool:
    try:
        _client()
        return True
    except OAuthError:
        return False


def authorize_url(state: str) -> str:
    """The Google consent URL to send the user to. `state` is an opaque,
    single-use nonce minted by issue_state() and validated on the callback —
    it is NOT trusted as identity on return."""
    cid, _, redirect = _client()
    params = {
        "client_id": cid,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",      # request a refresh token
        "prompt": "consent",
        "state": state,
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


# --- CSRF state (single-use, server-side, short-lived) -------------------

_STATE_NS = "oauth_state"
_STATE_TTL = 600          # seconds; a consent round-trip is quick


def issue_state(user: str, sid: str) -> str:
    """Mint an unguessable, single-use `state` and bind it server-side to the
    user/browser that started the flow. The returned `state` carries no
    identity itself — the callback resolves identity from this record, so a
    forged or replayed `state` cannot bind a connection to the wrong user."""
    token = secrets.token_urlsafe(32)
    store.backend().put(_STATE_NS, token, json.dumps(
        {"user": user, "sid": sid, "created_at": time.time()}).encode())
    return token


def consume_state(state: str) -> dict | None:
    """Validate a returned `state`; return its {user, sid} record or None if
    it's unknown, already used, or expired. Single-use: a valid state is
    deleted on consumption."""
    if not state:
        return None
    blob = store.backend().get(_STATE_NS, state)
    if not blob:
        return None
    store.backend().delete(_STATE_NS, state)      # single-use, even if expired
    try:
        rec = json.loads(blob)
    except (ValueError, json.JSONDecodeError):
        return None
    if time.time() - rec.get("created_at", 0) > _STATE_TTL:
        return None
    return rec


def _post_token(data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    try:
        with urllib.request.urlopen(
                urllib.request.Request(TOKEN_URL, data=body), timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as err:
        detail = err.read().decode(errors="replace")[:300]
        raise OAuthError(f"token exchange failed: {detail}") from None
    except urllib.error.URLError as err:
        raise OAuthError(f"Google unreachable: {err}") from None


def exchange_code(user: str, code: str) -> str:
    """Exchange an auth code for tokens and store them (encrypted) for the user."""
    cid, secret, redirect = _client()
    tok = _post_token({
        "code": code, "client_id": cid, "client_secret": secret,
        "redirect_uri": redirect, "grant_type": "authorization_code",
    })
    bundle = {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token", ""),
        "expires_at": time.time() + int(tok.get("expires_in", 3600)),
    }
    vault.put(user, "google", bundle)
    return "Google account connected."


def connected(user: str) -> bool:
    try:
        return bool(vault.get(user, "google"))
    except vault.VaultError:
        return False


def disconnect(user: str) -> str:
    vault.delete(user, "google")
    return "Google account disconnected."


def access_token(user: str) -> str:
    """Return a valid access token for the user, refreshing if expired."""
    bundle = vault.get(user, "google")
    if not bundle:
        raise OAuthError("no Google account connected for this user")
    if time.time() < bundle.get("expires_at", 0) - 60:
        return bundle["access_token"]
    # refresh
    refresh = bundle.get("refresh_token")
    if not refresh:
        raise OAuthError("access token expired and no refresh token — reconnect")
    cid, secret, _ = _client()
    tok = _post_token({
        "refresh_token": refresh, "client_id": cid, "client_secret": secret,
        "grant_type": "refresh_token",
    })
    bundle["access_token"] = tok["access_token"]
    bundle["expires_at"] = time.time() + int(tok.get("expires_in", 3600))
    vault.put(user, "google", bundle)
    return bundle["access_token"]

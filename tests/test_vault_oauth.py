"""Tests for the secrets vault, storage abstraction, and Google OAuth flow."""

import time

import pytest

from olympus import store, vault, google_oauth

# Encryption needs a working `cryptography` backend. It's a real dependency
# (installed in CI and on real machines); skip the crypto-dependent tests where
# the native backend is unavailable so the rest of the suite still runs.
needs_crypto = pytest.mark.skipif(
    not vault._HAVE_CRYPTO, reason="cryptography backend unavailable here")


@pytest.fixture(autouse=True)
def fresh_store():
    store.reset()
    yield
    store.reset()


# --- storage abstraction -------------------------------------------------

def test_file_store_roundtrip():
    s = store.FileStore()
    s.put("ns", "k", b"hello")
    assert s.get("ns", "k") == b"hello"
    assert "k" in s.keys("ns")
    s.delete("ns", "k")
    assert s.get("ns", "k") is None


def test_backend_defaults_to_file(monkeypatch):
    monkeypatch.delenv("OLYMPUS_DATABASE_URL", raising=False)
    store.reset()
    assert isinstance(store.backend(), store.FileStore)


# --- the vault (encryption) ---------------------------------------------

@needs_crypto
def test_vault_requires_secret_key(monkeypatch):
    monkeypatch.delenv("OLYMPUS_SECRET_KEY", raising=False)
    assert not vault.available()
    with pytest.raises(vault.VaultError, match="OLYMPUS_SECRET_KEY"):
        vault.put("u", "google", {"x": 1})


@needs_crypto
def test_vault_encrypts_at_rest(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "correct horse battery staple")
    assert vault.available()
    secret = {"access_token": "ya29.SENSITIVE", "refresh_token": "1//RT"}
    vault.put("u", "google", secret)
    # round-trips for the owner
    assert vault.get("u", "google") == secret
    # but the raw stored bytes do NOT contain the plaintext token
    raw = store.backend().get("vault", "u")
    assert b"ya29.SENSITIVE" not in raw and b"1//RT" not in raw


@needs_crypto
def test_vault_wrong_key_fails_closed(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "key-one")
    vault.put("u", "google", {"t": "secret"})
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "key-two")
    with pytest.raises(vault.VaultError, match="could not decrypt"):
        vault.get("u", "google")


@needs_crypto
def test_vault_is_per_user(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "k")
    vault.put("alice", "google", {"t": "a"})
    assert vault.get("bob", "google") is None


# --- Google OAuth flow ---------------------------------------------------

def test_oauth_not_configured(monkeypatch):
    for k in ("OLYMPUS_GOOGLE_CLIENT_ID", "OLYMPUS_GOOGLE_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    assert not google_oauth.configured()
    with pytest.raises(google_oauth.OAuthError, match="not configured"):
        google_oauth.authorize_url("sess")


def test_authorize_url_has_offline_and_scopes(monkeypatch):
    monkeypatch.setenv("OLYMPUS_GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("OLYMPUS_GOOGLE_CLIENT_SECRET", "sec")
    url = google_oauth.authorize_url("sess123")
    assert "accounts.google.com" in url
    assert "access_type=offline" in url and "state=sess123" in url
    assert "gmail.send" in url and "calendar.events" in url


@needs_crypto
def test_exchange_code_stores_encrypted_tokens(monkeypatch):
    monkeypatch.setenv("OLYMPUS_GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("OLYMPUS_GOOGLE_CLIENT_SECRET", "sec")
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "k")
    monkeypatch.setattr(google_oauth, "_post_token",
                        lambda data: {"access_token": "AT", "refresh_token": "RT",
                                      "expires_in": 3600})
    assert not google_oauth.connected("u")
    google_oauth.exchange_code("u", "auth-code")
    assert google_oauth.connected("u")
    assert google_oauth.access_token("u") == "AT"   # valid, no refresh needed


@needs_crypto
def test_access_token_refreshes_when_expired(monkeypatch):
    monkeypatch.setenv("OLYMPUS_GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("OLYMPUS_GOOGLE_CLIENT_SECRET", "sec")
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "k")
    vault.put("u", "google", {"access_token": "OLD", "refresh_token": "RT",
                              "expires_at": time.time() - 10})  # expired
    monkeypatch.setattr(google_oauth, "_post_token",
                        lambda data: {"access_token": "NEW", "expires_in": 3600})
    assert google_oauth.access_token("u") == "NEW"
    # the refreshed token is persisted
    assert vault.get("u", "google")["access_token"] == "NEW"


@needs_crypto
def test_disconnect_clears_tokens(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "k")
    vault.put("u", "google", {"access_token": "AT"})
    google_oauth.disconnect("u")
    assert not google_oauth.connected("u")


# --- web connect endpoints ----------------------------------------------

@needs_crypto
def test_web_connect_status_and_callback(monkeypatch):
    import json, threading, urllib.request
    from http.server import ThreadingHTTPServer
    from olympus import web

    monkeypatch.setenv("OLYMPUS_GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("OLYMPUS_GOOGLE_CLIENT_SECRET", "sec")
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "k")
    monkeypatch.setattr(google_oauth, "_post_token",
                        lambda data: {"access_token": "AT", "refresh_token": "RT",
                                      "expires_in": 3600})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        st = json.loads(urllib.request.urlopen(
            base + "/api/connected?session=s9").read())
        assert st["configured"] is True and st["connected"] is False
        # simulate Google redirecting back with a code
        urllib.request.urlopen(
            base + "/oauth/google/callback?state=s9&code=abc").read()
        st2 = json.loads(urllib.request.urlopen(
            base + "/api/connected?session=s9").read())
        assert st2["connected"] is True
    finally:
        srv.shutdown()

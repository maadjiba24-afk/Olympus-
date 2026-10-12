"""Tests for the secrets vault, storage abstraction, and Google OAuth flow."""

import os
import time

import pytest

from olympus import store, vault, google_oauth

# Encryption needs a working `cryptography` backend. It's a real dependency
# (installed in CI and on real machines); skip the crypto-dependent tests where
# the native backend is unavailable so the rest of the suite still runs.
needs_crypto = pytest.mark.skipif(
    not vault._HAVE_CRYPTO, reason="cryptography backend unavailable here")


@pytest.fixture(autouse=True)
def fresh_store(monkeypatch):
    monkeypatch.delenv("OLYMPUS_SECRET_KEY", raising=False)
    monkeypatch.delenv("OLYMPUS_SECRET_KEY_FILE", raising=False)
    monkeypatch.delenv("OLYMPUS_ENV", raising=False)
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
def test_vault_key_file_roundtrip(monkeypatch, tmp_path):
    key_file = tmp_path / "vault-key"
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.write(fd, b"file-held-vault-secret-0123456789abcdef\n")
    finally:
        os.close(fd)
    monkeypatch.setenv("OLYMPUS_SECRET_KEY_FILE", str(key_file))
    assert vault.configured() and vault.available()
    vault.put("u", "google", {"token": "private"})
    assert vault.get("u", "google") == {"token": "private"}


@needs_crypto
def test_vault_key_sources_are_mutually_exclusive(monkeypatch, tmp_path):
    key_file = tmp_path / "vault-key"
    key_file.write_text("file-secret", encoding="utf-8")
    key_file.chmod(0o600)
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "env-secret")
    monkeypatch.setenv("OLYMPUS_SECRET_KEY_FILE", str(key_file))
    assert vault.configured() is True
    assert vault.available() is False
    with pytest.raises(vault.VaultError, match="unavailable or invalid"):
        vault.check_ready()


@needs_crypto
def test_configured_missing_vault_key_file_is_not_unconfigured(monkeypatch,
                                                                tmp_path):
    missing = tmp_path / "missing"
    monkeypatch.setenv("OLYMPUS_SECRET_KEY_FILE", str(missing))
    assert vault.configured() is True
    assert vault.available() is False
    with pytest.raises(vault.VaultError, match="unavailable or invalid"):
        vault.check_ready()


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
@needs_crypto
def test_vault_key_file_permissions_are_enforced(monkeypatch, tmp_path):
    key_file = tmp_path / "vault-key"
    key_file.write_text("file-secret", encoding="utf-8")
    key_file.chmod(0o644)
    monkeypatch.setenv("OLYMPUS_SECRET_KEY_FILE", str(key_file))
    with pytest.raises(vault.VaultError, match="unavailable or invalid"):
        vault.check_ready()


@needs_crypto
def test_malformed_vault_key_file_fails_closed(monkeypatch, tmp_path):
    key_file = tmp_path / "vault-key"
    key_file.write_bytes(b"\xff\xfe\x00")
    key_file.chmod(0o600)
    monkeypatch.setenv("OLYMPUS_SECRET_KEY_FILE", str(key_file))
    assert vault.configured() is True
    assert vault.available() is False
    with pytest.raises(vault.VaultError, match="unavailable or invalid"):
        vault.check_ready()


@needs_crypto
@pytest.mark.parametrize("secret", [
    "REPLACE_WITH_RANDOM_HEX",
    "short-production-key",
])
def test_named_deployments_reject_placeholder_or_short_vault_keys(
        monkeypatch, secret):
    monkeypatch.setenv("OLYMPUS_ENV", "production")
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", secret)
    assert vault.available() is False
    with pytest.raises(vault.VaultError, match="unavailable or invalid"):
        vault.check_ready()


@needs_crypto
def test_vault_encrypts_at_rest(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "correct horse battery staple")
    assert vault.available()
    secret = {"access_token": "ya29.SENSITIVE", "refresh_token": "1//RT"}
    vault.put("u", "google", secret)
    # round-trips for the owner
    assert vault.get("u", "google") == secret
    # but the raw stored bytes do NOT contain the plaintext token
    # The vault is owner-keyed now (exact principal + SHA-256 digest), so ask
    # the module for the key instead of hardcoding the old safe_id layout.
    raw = store.backend().get("vault", vault._key("u"))
    assert b"ya29.SENSITIVE" not in raw and b"1//RT" not in raw


@needs_crypto
def test_vault_wrong_key_fails_closed(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "key-one")
    vault.put("u", "google", {"t": "secret"})
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "key-two")
    with pytest.raises(vault.VaultError, match="could not decrypt or authenticate the bounded vault record"):
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
                                      "expires_in": 3600, "token_type": "Bearer"})
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
                        lambda data: {"access_token": "NEW", "expires_in": 3600, "token_type": "Bearer"})
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
def test_web_connect_flow_validates_state(monkeypatch):
    import json, threading, time, urllib.error, urllib.parse, urllib.request
    from http.server import ThreadingHTTPServer
    from socketserver import TCPServer
    from olympus import native_cleanup, web

    monkeypatch.setenv("OLYMPUS_GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("OLYMPUS_GOOGLE_CLIENT_SECRET", "sec")
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "k")
    provider_calls = []
    def provider(data):
        provider_calls.append(1)
        return {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}
    monkeypatch.setattr(google_oauth, "_post_token", provider)

    serving = threading.Event()
    class OwnedServer(ThreadingHTTPServer):
        daemon_threads = False
        block_on_close = True
        workers_unclosed = False

        def server_bind(self):
            # HTTPServer.server_bind performs reverse hostname lookup. This
            # exact literal loopback fixture needs no DNS or ambient proxy.
            TCPServer.server_bind(self)
            self.server_name = 'localhost'
            self.server_port = self.server_address[1]

        def service_actions(self):
            # This callback runs from inside the real serve_forever loop.
            serving.set()

        def server_close(self):
            # Close the acquired listener even if start failed. Preserve the
            # mixin's request-thread inventory, but bound every owned join.
            workers = getattr(self, '_threads', None)
            workers = list(workers) if isinstance(workers, list) else []
            deadline = time.monotonic() + 5
            def join_one(worker):
                if worker.ident is not None:
                    worker.join(max(0, deadline-time.monotonic()))
            def join_workers():
                try:
                    native_cleanup.drain([lambda worker=worker: join_one(worker) for worker in workers])
                finally:
                    self.workers_unclosed = any(worker.is_alive() for worker in workers)
            native_cleanup.drain([lambda: TCPServer.server_close(self), join_workers])

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    srv = thread = primary = None

    def connected():
        with native_cleanup.closing(local.open(base+"/api/connected?session=cookiesid9", timeout=5)) as response:
            return json.loads(response.read())["connected"]

    def start():
        request = urllib.request.Request(base+"/oauth/google/start",
            headers={"Cookie": "olympus_sid=cookiesid9"})
        try:
            with native_cleanup.closing(opener.open(request, timeout=5)) as response:
                location = response.headers["Location"]
        except urllib.error.HTTPError as error:
            try:
                assert error.code == 302
                location = error.headers["Location"]
            except BaseException as original:
                native_cleanup.drain([error.close], original)
                raise
            if not native_cleanup.drain([error.close], error):
                raise
        return urllib.parse.parse_qs(urllib.parse.urlparse(location).query)["state"][0]

    def callback(state, cookie="olympus_sid=cookiesid9"):
        request = urllib.request.Request(base+f"/oauth/google/callback?state={state}&code=abc",
                                         headers={"Cookie": cookie})
        try:
            with native_cleanup.closing(local.open(request, timeout=5)) as response:
                response.read()
                return response.status
        except urllib.error.HTTPError as error:
            status = error.code
            if not native_cleanup.drain([error.close], error):
                raise
            return status

    def joined():
        if thread is not None and thread.ident is not None:
            thread.join(5)
        if srv is not None:
            assert srv.socket.fileno() == -1

    try:
        srv = OwnedServer(("127.0.0.1", 0), web.Handler)
        thread = threading.Thread(target=srv.serve_forever, kwargs={'poll_interval': 0.05},
                                  name='owned-oauth-http-server', daemon=True)
        thread.start()
        if not serving.wait(5):
            pytest.exit('Owned OAuth server loop start was not confirmed; stopping validation', returncode=2)
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        assert connected() is False
        state = start()
        assert state and state != "cookiesid9"
        assert callback("forged-never-issued") == 400
        assert connected() is False and provider_calls == []
        assert callback(state, cookie="olympus_sid=someoneelse9") == 400
        assert provider_calls == []
        state = start()
        assert callback(state) == 200
        assert connected() is True
        assert callback(state) == 400 and provider_calls == [1]
        bundle = vault.get("web-cookiesid9", "google")
        assert bundle["access_token"] == "AT" and bundle["refresh_token"] == "RT"
        assert vault.get("web-someoneelse9", "google") is None
        raw = store.backend().get("vault", vault._key("web-cookiesid9"))
        assert raw and raw != json.dumps(bundle).encode()
    except BaseException as error:
        primary = error
        raise
    finally:
        # start() can report an interrupt after a thread has actually begun.
        # Observe the real loop before choosing shutdown; never shutdown a
        # server that has not entered serve_forever.
        if thread is not None and thread.is_alive() and not serving.is_set():
            serving.wait(5)
        actions = []
        if srv is not None:
            if serving.is_set():
                actions.append(srv.shutdown)
            actions.append(srv.server_close)
        actions.append(joined)
        cleanup_failure = None
        try:
            native_cleanup.drain(actions, primary)
        except BaseException as error:
            cleanup_failure = error
        if ((thread is not None and thread.is_alive())
                or (srv is not None and srv.workers_unclosed)):
            pytest.exit('Owned OAuth thread remains live; stopping validation', returncode=2)
        if cleanup_failure is not None:
            raise cleanup_failure


def test_consume_state_is_single_use_and_expiring(monkeypatch):
    from olympus import store
    store.reset()
    state = google_oauth.issue_state("web-abc", "abc")
    rec = google_oauth.consume_state(state)
    assert rec and rec["user"] == "web-abc" and rec["sid"] == "abc"
    assert google_oauth.consume_state(state) is None        # single-use
    assert google_oauth.consume_state("never-issued") is None
    # expiry
    monkeypatch.setattr(google_oauth, "_STATE_TTL", -1)
    stale = google_oauth.issue_state("web-xyz", "xyz")
    assert google_oauth.consume_state(stale) is None

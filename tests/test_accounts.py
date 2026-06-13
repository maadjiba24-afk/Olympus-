"""Tests for user accounts: registration, auth, sessions, and web gating."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from olympus import accounts, config, store, usermem


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    store.reset()
    monkeypatch.delenv("OLYMPUS_REQUIRE_LOGIN", raising=False)
    yield
    store.reset()


# --- accounts core -------------------------------------------------------

def test_register_and_authenticate():
    token = accounts.register("alice", "hunter2pass")
    assert accounts.account_for_token(token)["username"] == "alice"
    again = accounts.authenticate("alice", "hunter2pass")
    assert accounts.account_for_token(again)["username"] == "alice"


def test_password_never_stored_plaintext():
    accounts.register("bob", "supersecret123")
    raw = store.backend().get("accounts", "bob")
    assert b"supersecret123" not in raw          # only the PBKDF2 hash is stored


def test_wrong_password_rejected():
    accounts.register("carol", "rightpassword")
    assert accounts.authenticate("carol", "wrongpassword") is None
    assert accounts.authenticate("nobody", "whatever") is None


def test_username_rules_and_uniqueness():
    accounts.register("dave", "password123")
    with pytest.raises(ValueError, match="taken"):
        accounts.register("dave", "password123")
    with pytest.raises(ValueError):
        accounts.register("ab", "password123")          # too short
    with pytest.raises(ValueError):
        accounts.register("bad name!", "password123")   # illegal chars
    with pytest.raises(ValueError, match="at least"):
        accounts.register("eve", "short")               # weak password


def test_logout_invalidates_token():
    token = accounts.register("frank", "password123")
    accounts.logout(token)
    assert accounts.account_for_token(token) is None


def test_namespace_isolation_between_accounts():
    t1 = accounts.register("user1", "password123")
    t2 = accounts.register("user2", "password123")
    n1, n2 = accounts.namespace_for_token(t1), accounts.namespace_for_token(t2)
    assert n1 != n2 and n1.startswith("u:")


def test_expired_session(monkeypatch):
    token = accounts.register("grace", "password123")
    monkeypatch.setenv("OLYMPUS_SESSION_DAYS", "0")     # everything is expired
    assert accounts.account_for_token(token) is None


# --- web gating ----------------------------------------------------------

@pytest.fixture()
def server(monkeypatch):
    from olympus import web
    monkeypatch.setattr(web, "_SESSIONS", {})
    monkeypatch.setattr(web, "_HITS", {})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", web
    srv.shutdown()


def _post(url, path, payload, cookie=None):
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = "olympus_sid=" + cookie
    req = urllib.request.Request(url + path, data=json.dumps(payload).encode(),
                                 headers=headers, method="POST")
    resp = urllib.request.urlopen(req)
    return json.loads(resp.read()), resp.headers.get("Set-Cookie", "")


def test_require_login_blocks_api(server, monkeypatch):
    base, web = server
    monkeypatch.setenv("OLYMPUS_REQUIRE_LOGIN", "1")
    req = urllib.request.Request(base + "/api/memory")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 401                          # no login → blocked


def test_register_then_access(server, monkeypatch):
    base, web = server
    monkeypatch.setenv("OLYMPUS_REQUIRE_LOGIN", "1")
    out, setcookie = _post(base, "/api/register",
                           {"username": "heidi", "password": "password123"})
    assert out["logged_in"] is True and "olympus_sid=" in setcookie
    token = setcookie.split("olympus_sid=")[1].split(";")[0]
    # the token cookie now grants access, scoped to heidi's namespace
    usermem.add_memory(accounts.namespace_for_token(token), type="preference",
                       content="heidi likes tea", confidence=0.9)
    req = urllib.request.Request(base + "/api/memory",
                                 headers={"Cookie": "olympus_sid=" + token})
    d = json.loads(urllib.request.urlopen(req).read())
    assert any(m["content"] == "heidi likes tea" for m in d["memories"])


def test_me_reports_status(server, monkeypatch):
    base, web = server
    monkeypatch.setenv("OLYMPUS_REQUIRE_LOGIN", "1")
    d = json.loads(urllib.request.urlopen(base + "/api/me").read())
    assert d["login_required"] is True and d["logged_in"] is False

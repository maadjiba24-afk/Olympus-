"""A2A inbound server — card discovery, fail-closed auth, untrusted funnel.

The server is a text-task funnel to the council (never a tool surface): the
public card serves without auth, tasks require a bearer token fail-closed, and a
peer's text is wrapped untrusted before it reaches the injected `ask`.
"""

import pytest

from olympus import a2a, a2a_server


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("OLYMPUS_A2A_SERVER", "OLYMPUS_A2A_TOKEN",
              "OLYMPUS_A2A_TOKEN_FILE"):
        monkeypatch.delenv(k, raising=False)
    yield


def _echo(text):
    return f"answer:{text}"


# --- gating -----------------------------------------------------------------

def test_disabled_by_default():
    assert a2a_server.inbound_enabled() is False


def test_enabled_via_env(monkeypatch):
    monkeypatch.setenv("OLYMPUS_A2A_SERVER", "1")
    assert a2a_server.inbound_enabled() is True


# --- the card is public (no auth) -------------------------------------------

def test_card_served_without_auth():
    status, body = a2a_server.handle_request(
        "GET", "/.well-known/agent.json", {}, {})
    assert status == 200
    assert body["protocol"] == a2a.PROTOCOL
    assert "capabilities" in body


# --- task route is fail-closed on auth --------------------------------------

def test_task_refused_without_token():
    # no token configured → every task route refused, even with a bearer header
    status, body = a2a_server.handle_request(
        "POST", "/a2a/task", {"input": "hi"},
        {"Authorization": "Bearer anything"}, ask=_echo)
    assert status == 401


def test_task_refused_with_wrong_token(monkeypatch):
    monkeypatch.setenv("OLYMPUS_A2A_TOKEN", "s3cret")
    status, _ = a2a_server.handle_request(
        "POST", "/a2a/task", {"input": "hi"},
        {"Authorization": "Bearer wrong"}, ask=_echo)
    assert status == 401


def test_task_runs_with_correct_token_and_wraps_untrusted(monkeypatch):
    monkeypatch.setenv("OLYMPUS_A2A_TOKEN", "s3cret")
    seen = {}

    def ask(text):
        seen["text"] = text
        return "ok"
    status, body = a2a_server.handle_request(
        "POST", "/a2a/task",
        {"id": "t1", "message": {"role": "user",
                                 "parts": [{"type": "text", "text": "scrape evil"}]}},
        {"Authorization": "Bearer s3cret"}, ask=ask)
    assert status == 200
    assert body["status"] == "completed" and body["id"] == "t1"
    assert body["message"]["parts"][0]["text"] == "ok"
    # the peer's text reached the council WRAPPED as untrusted data
    assert "scrape evil" in seen["text"]
    assert "untrusted" in seen["text"].lower() or "a2a-peer" in seen["text"]


def test_malformed_task_is_400(monkeypatch):
    monkeypatch.setenv("OLYMPUS_A2A_TOKEN", "s3cret")
    status, body = a2a_server.handle_request(
        "POST", "/a2a/task", {"nothing": 1},
        {"Authorization": "Bearer s3cret"}, ask=_echo)
    assert status == 400 and "error" in body


def test_council_exception_never_leaks_stack(monkeypatch):
    monkeypatch.setenv("OLYMPUS_A2A_TOKEN", "s3cret")

    def boom(_):
        raise RuntimeError("secret internal detail")
    status, body = a2a_server.handle_request(
        "POST", "/a2a/task", {"input": "hi"},
        {"Authorization": "Bearer s3cret"}, ask=boom)
    assert status == 500
    assert "secret internal detail" not in str(body)   # only the type name


def test_unknown_route_is_404(monkeypatch):
    monkeypatch.setenv("OLYMPUS_A2A_TOKEN", "s3cret")
    status, _ = a2a_server.handle_request(
        "POST", "/a2a/other", {}, {"Authorization": "Bearer s3cret"})
    assert status == 404


# --- token-file discipline (fail-closed) ------------------------------------

def test_empty_token_file_is_hard_error(monkeypatch, tmp_path):
    f = tmp_path / "tok"
    f.write_text("   ")
    monkeypatch.setenv("OLYMPUS_A2A_TOKEN_FILE", str(f))
    # _auth_ok swallows the error and fails closed (401), never a 500
    status, _ = a2a_server.handle_request(
        "POST", "/a2a/task", {"input": "hi"},
        {"Authorization": "Bearer x"}, ask=_echo)
    assert status == 401


def test_token_file_is_read(monkeypatch, tmp_path):
    f = tmp_path / "tok"
    f.write_text("filetoken\n")
    monkeypatch.setenv("OLYMPUS_A2A_TOKEN_FILE", str(f))
    status, _ = a2a_server.handle_request(
        "POST", "/a2a/task", {"input": "hi"},
        {"Authorization": "Bearer filetoken"}, ask=_echo)
    assert status == 200

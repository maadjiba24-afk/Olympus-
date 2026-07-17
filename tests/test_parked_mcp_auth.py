"""Parked-item 2 — MCP server authentication.

The MCP server is stdio-local by default (the OS process boundary is its trust
boundary). This adds a fail-closed bearer-token auth layer so it can NEVER be
network-exposed without authentication: an exposed server with no token refuses
to serve, and when auth is on every tool call must present the token. Building
the auth is the milestone; actually exposing the server stays the operator's
act (default off).
"""

import pytest

from olympus import mcp_server as mcp


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("OLYMPUS_MCP_AUTH_TOKEN", "OLYMPUS_MCP_AUTH_TOKEN_FILE",
              "OLYMPUS_MCP_EXPOSED"):
        monkeypatch.delenv(k, raising=False)


def _call(name, args=None, params_extra=None, session=None):
    params = {"name": name, "arguments": args or {}}
    if params_extra:
        params.update(params_extra)
    return mcp.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params},
        ask=lambda m: "answer", session=session)


# --- default: stdio-local, no auth (backward compatible) -------------------

def test_no_auth_by_default():
    assert not mcp.require_auth() and not mcp.exposed()
    res = _call("olympus_goals")
    assert "result" in res and not res["result"].get("isError")


# --- token configured → auth enforced --------------------------------------

def test_tool_call_refused_without_token(monkeypatch):
    monkeypatch.setenv("OLYMPUS_MCP_AUTH_TOKEN", "s3cret")
    assert mcp.require_auth()
    res = _call("olympus_goals")
    assert res["error"]["code"] == -32001 and "unauthorized" in res["error"]["message"]


def test_tool_call_allowed_with_correct_token(monkeypatch):
    monkeypatch.setenv("OLYMPUS_MCP_AUTH_TOKEN", "s3cret")
    res = _call("olympus_goals", params_extra={"authToken": "s3cret"})
    assert "result" in res and not res["result"].get("isError")


def test_tool_call_refused_with_wrong_token(monkeypatch):
    monkeypatch.setenv("OLYMPUS_MCP_AUTH_TOKEN", "s3cret")
    res = _call("olympus_goals", params_extra={"authToken": "guess"})
    assert res["error"]["code"] == -32001


def test_session_token_from_initialize_authorizes_later_calls(monkeypatch):
    monkeypatch.setenv("OLYMPUS_MCP_AUTH_TOKEN", "s3cret")
    session: dict = {}
    init = mcp.handle_message(
        {"jsonrpc": "2.0", "id": 0, "method": "initialize",
         "params": {"authToken": "s3cret"}}, session=session)
    assert init["result"]["authRequired"] is True
    assert session.get("token") == "s3cret"
    # A later call with no per-call token rides the authenticated session.
    res = _call("olympus_goals", session=session)
    assert "result" in res and not res["result"].get("isError")


def test_initialize_with_wrong_token_does_not_authenticate(monkeypatch):
    monkeypatch.setenv("OLYMPUS_MCP_AUTH_TOKEN", "s3cret")
    session: dict = {}
    mcp.handle_message(
        {"jsonrpc": "2.0", "id": 0, "method": "initialize",
         "params": {"authToken": "wrong"}}, session=session)
    assert "token" not in session
    assert _call("olympus_goals", session=session)["error"]["code"] == -32001


# --- handshake / liveness are always open ----------------------------------

def test_initialize_and_ping_open_even_when_auth_required(monkeypatch):
    monkeypatch.setenv("OLYMPUS_MCP_AUTH_TOKEN", "s3cret")
    init = mcp.handle_message({"jsonrpc": "2.0", "id": 1,
                               "method": "initialize", "params": {}})
    assert "result" in init and init["result"]["authRequired"] is True
    ping = mcp.handle_message({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    assert ping["result"] == {}


# --- fail-closed: exposed without a token refuses to serve -----------------

def test_exposed_without_token_refuses_to_serve(monkeypatch):
    monkeypatch.setenv("OLYMPUS_MCP_EXPOSED", "1")
    assert mcp.require_auth()                    # exposure alone requires auth
    with pytest.raises(mcp.MCPAuthError):
        mcp.assert_serveable()
    # And with no token configured, NO credential is ever accepted.
    assert not mcp.credential_ok("anything")
    assert _call("olympus_goals")["error"]["code"] == -32001


def test_exposed_with_token_serves(monkeypatch):
    monkeypatch.setenv("OLYMPUS_MCP_EXPOSED", "1")
    monkeypatch.setenv("OLYMPUS_MCP_AUTH_TOKEN", "tok")
    mcp.assert_serveable()                       # no raise
    assert _call("olympus_goals",
                 params_extra={"authToken": "tok"})["result"]


# --- token file custody (preferred; fail-closed) ---------------------------

def test_token_file_is_read_and_enforced(monkeypatch, tmp_path):
    f = tmp_path / "mcp_token"
    f.write_text("file-token\n", encoding="utf-8")
    monkeypatch.setenv("OLYMPUS_MCP_AUTH_TOKEN_FILE", str(f))
    assert mcp.require_auth()
    assert _call("olympus_goals", params_extra={"authToken": "file-token"})["result"]
    assert _call("olympus_goals")["error"]["code"] == -32001


def test_unreadable_or_empty_token_file_is_hard_error(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_MCP_AUTH_TOKEN_FILE", str(tmp_path / "nope"))
    with pytest.raises(mcp.MCPAuthError):
        mcp.require_auth()                       # never a silent 'no auth'
    empty = tmp_path / "empty"
    empty.write_text("   \n", encoding="utf-8")
    monkeypatch.setenv("OLYMPUS_MCP_AUTH_TOKEN_FILE", str(empty))
    with pytest.raises(mcp.MCPAuthError):
        mcp.require_auth()


def test_credential_ok_rejects_missing_and_mismatch(monkeypatch):
    assert not mcp.credential_ok("x")            # no token configured
    monkeypatch.setenv("OLYMPUS_MCP_AUTH_TOKEN", "right")
    assert mcp.credential_ok("right")
    assert not mcp.credential_ok("wrong")
    assert not mcp.credential_ok(None)

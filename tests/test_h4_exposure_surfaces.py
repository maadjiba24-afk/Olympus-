"""H4 — exposure-surface hardening: the MCP auth boundary and OTLP telemetry.

Two outward-facing surfaces:
  * mcp_server — an exposed server's auth check must not CRASH on a hostile
    credential, and one malformed message must not take the loop down.
  * otel — the OTLP export must funnel through the same sovereign egress choke
    every other outbound call uses, so run metadata can't leak past the
    allowlist under sovereign mode.
"""

import io
import json

import pytest

from olympus import mcp_server as mcp, otel, security


# --- MCP: the auth check must not crash on a hostile credential --------------

@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("OLYMPUS_MCP_AUTH_TOKEN", "OLYMPUS_MCP_AUTH_TOKEN_FILE",
              "OLYMPUS_MCP_EXPOSED", "OLYMPUS_OTLP_ENDPOINT",
              "OLYMPUS_OTLP_HEADERS", "OLYMPUS_SOVEREIGN",
              "OLYMPUS_EGRESS_ALLOWLIST", "OLYMPUS_REPLAY"):
        monkeypatch.delenv(k, raising=False)


def test_credential_ok_non_ascii_returns_false_not_raises(monkeypatch):
    monkeypatch.setenv("OLYMPUS_MCP_AUTH_TOKEN", "s3cret-ascii")
    # A non-ASCII credential must be rejected, never raise a TypeError.
    assert mcp.credential_ok("tökén-with-ü") is False
    assert mcp.credential_ok("正しくない") is False
    assert mcp.credential_ok("s3cret-ascii") is True         # the real one


def test_handle_message_non_ascii_token_is_unauthorized_not_a_crash(monkeypatch):
    monkeypatch.setenv("OLYMPUS_MCP_EXPOSED", "1")
    monkeypatch.setenv("OLYMPUS_MCP_AUTH_TOKEN", "s3cret-ascii")
    resp = mcp.handle_message(
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
         "params": {"name": "olympus_goals", "authToken": "hostile-ü-token"}},
        ask=lambda m: "x")
    assert resp["error"]["code"] == -32001                   # unauthorized, clean


def test_serve_stdio_survives_a_non_object_message(monkeypatch):
    # A JSON value that isn't an object (a list) must get an invalid-request
    # error and the loop must continue to serve the NEXT message.
    stdin = io.StringIO('[1, 2, 3]\n'
                        '{"jsonrpc":"2.0","id":9,"method":"ping"}\n')
    out = io.StringIO()
    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr("sys.stdout", out)
    mcp.serve_stdio()
    lines = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
    assert lines[0]["error"]["code"] == -32600               # invalid request
    assert lines[1]["id"] == 9 and "result" in lines[1]      # loop survived


def test_serve_stdio_survives_a_faulty_handler(monkeypatch):
    # If a handler raises, the loop turns it into an internal-error response and
    # keeps serving — it never dies on one bad message.
    def _boom(msg, **kw):
        raise RuntimeError("kaboom with secret detail")
    monkeypatch.setattr(mcp, "handle_message", _boom)
    stdin = io.StringIO('{"jsonrpc":"2.0","id":3,"method":"ping"}\n')
    out = io.StringIO()
    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr("sys.stdout", out)
    mcp.serve_stdio()
    resp = json.loads(out.getvalue().strip())
    assert resp["error"]["code"] == -32603
    assert "secret detail" not in resp["error"]["message"]   # detail not leaked


# --- OTLP: export funnels through the sovereign egress choke ------------------

def _record():
    return {"id": "run1", "kind": "chat", "total_secs": 1.0,
            "decisions": [{"record_id": "d0", "decision_type": "route",
                           "agent": {"name": "Zeus"}, "outcome": {"status": "ok"},
                           "cost": 0.0, "duration_ms": 10, "ts": 0.0}]}


def test_export_proceeds_when_sovereign_off(monkeypatch):
    monkeypatch.setenv("OLYMPUS_OTLP_ENDPOINT", "http://collector.example.com:4318")
    calls = []
    ok = otel.export_run(_record(), poster=lambda u, p: calls.append(u))
    assert ok is True and calls                              # sovereign off → sent


def test_export_blocked_under_sovereign_to_nonallowlisted_host(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SOVEREIGN", "1")
    monkeypatch.setenv("OLYMPUS_OTLP_ENDPOINT", "http://collector.example.com:4318")
    calls = []
    ok = otel.export_run(_record(), poster=lambda u, p: calls.append(u))
    # Refused by the egress choke — the poster is NEVER called, no metadata left.
    assert ok is False and calls == []


def test_export_allowed_under_sovereign_to_loopback_collector(monkeypatch):
    # Loopback is on the sovereign allowlist, so a LOCAL collector still works
    # even under sovereign mode (the common deployment).
    monkeypatch.setenv("OLYMPUS_SOVEREIGN", "1")
    monkeypatch.setenv("OLYMPUS_OTLP_ENDPOINT", "http://127.0.0.1:4318")
    calls = []
    ok = otel.export_run(_record(), poster=lambda u, p: calls.append(u))
    assert ok is True and calls


def test_export_allowed_under_sovereign_when_host_allowlisted(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SOVEREIGN", "1")
    monkeypatch.setenv("OLYMPUS_EGRESS_ALLOWLIST", "collector.example.com")
    monkeypatch.setenv("OLYMPUS_OTLP_ENDPOINT", "http://collector.example.com:4318")
    assert security.egress_allowed("collector.example.com")   # sanity
    calls = []
    ok = otel.export_run(_record(), poster=lambda u, p: calls.append(u))
    assert ok is True and calls

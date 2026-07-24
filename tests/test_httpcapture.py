"""Opt-in HTTP capture / replay store (the safe Caido form).

Records the ALREADY-governed `tools._http_get` fetch path for operator
inspection/replay — off by default, secret-redacted, size-capped, and opening no
new socket. Tests run fully offline.
"""

from __future__ import annotations

import pytest

from olympus import config, httpcapture, security, tools

# A secret assembled from fragments so no committed source line holds a
# contiguous secret literal (push-protection scanning).
_STRIPE = "sk" + "_live_" + "abcdef1234567890ABCDEF"


@pytest.fixture(autouse=True)
def _off(monkeypatch):
    monkeypatch.delenv("OLYMPUS_HTTP_CAPTURE", raising=False)
    yield


def _on(monkeypatch):
    monkeypatch.setenv("OLYMPUS_HTTP_CAPTURE", "1")


# --- gating ------------------------------------------------------------------

def test_disabled_by_default_records_nothing(tmp_path):
    assert httpcapture.enabled() is False
    assert httpcapture.record("GET", "https://x/", None, 200, "hi") is None
    assert httpcapture.records() == []


def test_enabled_records_and_reads_back(monkeypatch):
    _on(monkeypatch)
    rid = httpcapture.record("GET", "https://ex.com/a", {"Content-Type": "text/html"},
                             200, "<html>hello</html>", elapsed_ms=12.3)
    assert rid
    recs = httpcapture.records()
    assert len(recs) == 1
    r = recs[0]
    assert r["url"] == "https://ex.com/a" and r["status"] == 200
    assert r["method"] == "GET" and r["bytes"] == len("<html>hello</html>")
    assert httpcapture.get(rid)["id"] == rid


# --- security: secrets never persisted ---------------------------------------

def test_secret_in_body_is_redacted_in_store(monkeypatch):
    _on(monkeypatch)
    httpcapture.record("GET", "https://ex.com/", None, 200, f"token={_STRIPE} end")
    r = httpcapture.records()[0]
    assert _STRIPE not in r["body"]
    assert "[redacted key]" in r["body"]
    # the integrity hash is over the ORIGINAL body, so replay-diff still works
    assert r["body_sha256"]


def test_sensitive_request_headers_are_dropped(monkeypatch):
    _on(monkeypatch)
    httpcapture.record("GET", "https://ex.com/",
                       {"Authorization": "Bearer " + _STRIPE, "Content-Type": "text/html"},
                       200, "ok")
    r = httpcapture.records()[0]
    # only the safe subset is kept; Authorization is never persisted
    assert "authorization" not in {k.lower() for k in r["req_headers"]}
    assert r["req_headers"].get("content-type") == "text/html"


def test_body_is_size_capped(monkeypatch):
    _on(monkeypatch)
    big = "x" * (httpcapture._BODY_CAP + 5000)
    httpcapture.record("GET", "https://ex.com/big", None, 200, big)
    r = httpcapture.records()[0]
    assert r["truncated"] is True
    assert len(r["body"]) <= httpcapture._BODY_CAP
    assert r["bytes"] == len(big)          # true size preserved in metadata


# --- replay ------------------------------------------------------------------

def test_replay_detects_change_via_governed_fetch(monkeypatch):
    _on(monkeypatch)
    rid = httpcapture.record("GET", "https://ex.com/p", None, 200, "v1")
    # unchanged
    same = httpcapture.replay(rid, http_get=lambda url: "v1")
    assert same["ok"] and same["changed"] is False
    # changed
    diff = httpcapture.replay(rid, http_get=lambda url: "v2 now")
    assert diff["ok"] and diff["changed"] is True and diff["now_bytes"] == 6


def test_replay_unknown_id_is_safe(monkeypatch):
    _on(monkeypatch)
    out = httpcapture.replay("nope-000000000000", http_get=lambda url: "x")
    assert out["ok"] is False


def test_clear_removes_files(monkeypatch):
    _on(monkeypatch)
    httpcapture.record("GET", "https://ex.com/", None, 200, "a")
    assert httpcapture.records()
    assert httpcapture.clear() >= 1
    assert httpcapture.records() == []


# --- integration: the _http_get hook captures a governed fetch ---------------

class _FakeResp:
    def __init__(self, body, status=200, headers=None):
        self._b = body.encode()
        self.status = status
        self.headers = headers or {"Content-Type": "text/html"}

    def read(self, n=-1):
        return self._b[:n] if (n and n > 0) else self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_http_get_hook_captures_when_enabled(monkeypatch):
    _on(monkeypatch)
    monkeypatch.setattr(security, "secret_exfil_reason", lambda *a, **k: None)
    monkeypatch.setattr(tools, "_egress_confinement_reason", lambda u: None)
    monkeypatch.setattr(security, "url_block_reason", lambda *a, **k: None)
    monkeypatch.setattr(tools, "_proxied", lambda u: False)
    monkeypatch.setattr(tools, "_pinned_opener",
                        lambda: type("O", (), {"open": lambda self, req, timeout=None:
                                               _FakeResp("<html>hi</html>")})())
    out = tools._http_get("https://ex.com/page")
    assert out == "<html>hi</html>"
    recs = httpcapture.records()
    assert len(recs) == 1 and recs[0]["url"] == "https://ex.com/page"
    assert recs[0]["status"] == 200 and recs[0]["elapsed_ms"] is not None


def test_http_get_does_not_capture_when_disabled(monkeypatch):
    # capture off (default) → the governed fetch still works, nothing stored
    monkeypatch.setattr(security, "secret_exfil_reason", lambda *a, **k: None)
    monkeypatch.setattr(tools, "_egress_confinement_reason", lambda u: None)
    monkeypatch.setattr(security, "url_block_reason", lambda *a, **k: None)
    monkeypatch.setattr(tools, "_proxied", lambda u: False)
    monkeypatch.setattr(tools, "_pinned_opener",
                        lambda: type("O", (), {"open": lambda self, req, timeout=None:
                                               _FakeResp("hi")})())
    assert tools._http_get("https://ex.com/") == "hi"
    assert httpcapture.records() == []

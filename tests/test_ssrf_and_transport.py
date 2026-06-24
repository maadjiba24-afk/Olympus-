"""Tests for the SSRF guard on outbound fetches and the web transport hardening
(constant-time access-token check, Secure session cookie behind TLS)."""

import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from olympus import config, security, store, tools


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    store.reset()
    yield
    store.reset()


# --- SSRF guard ----------------------------------------------------------

@pytest.mark.parametrize("url", [
    "file:///etc/passwd",                       # non-http(s) scheme
    "ftp://example.com/x",                       # non-http(s) scheme
    "gopher://127.0.0.1:11211/",                 # non-http(s) scheme
    "http://127.0.0.1/",                         # loopback
    "http://127.0.0.1:8484/api/metrics",         # loopback w/ port
    "http://[::1]/",                             # IPv6 loopback
    "http://169.254.169.254/latest/meta-data/",  # AWS/Azure metadata (link-local)
    "http://10.0.0.5/internal",                  # private 10/8
    "http://192.168.1.1/",                       # private 192.168/16
    "http://172.16.5.4/",                        # private 172.16/12
    "http://0.0.0.0/",                           # unspecified
    "http://localhost/admin",                    # internal name
    "http://metadata.google.internal/",          # GCP metadata name
    "not-a-url",                                 # malformed / no scheme
])
def test_url_block_reason_blocks_dangerous(url):
    assert security.url_block_reason(url) is not None


@pytest.mark.parametrize("url", [
    "http://1.1.1.1/",       # public IP literal (no DNS lookup needed)
    "https://8.8.8.8/path",
])
def test_url_block_reason_allows_public(url):
    assert security.url_block_reason(url) is None


def test_web_fetch_refuses_internal_without_network():
    # The tool must reject before any socket call to a real host.
    out = tools._web_fetch("http://169.254.169.254/latest/meta-data/")
    assert out.startswith("Error:") and "non-public" in out
    out2 = tools._web_fetch("file:///etc/passwd")
    assert out2.startswith("Error:") and "http(s)" in out2


def test_public_hostname_pointing_internal_is_blocked(monkeypatch):
    # A public-looking name that resolves to an internal IP must still fail.
    def fake_getaddrinfo(host, port, *a, **k):
        return [(2, 1, 6, "", ("10.1.2.3", port))]
    monkeypatch.setattr(security.socket, "getaddrinfo", fake_getaddrinfo)
    assert security.url_block_reason("http://totally-legit.example/") is not None


# --- transport: constant-time access token -------------------------------

class _FakeHandler:
    def __init__(self, headers):
        self.headers = headers


def test_authorized_open_when_no_token(monkeypatch):
    from olympus import web
    monkeypatch.delenv("OLYMPUS_ACCESS_TOKEN", raising=False)
    assert web._authorized(_FakeHandler({})) is True


def test_authorized_requires_matching_token(monkeypatch):
    from olympus import web
    monkeypatch.setenv("OLYMPUS_ACCESS_TOKEN", "s3cr3t-token")
    assert web._authorized(_FakeHandler({"X-Olympus-Token": "s3cr3t-token"})) is True
    assert web._authorized(_FakeHandler({"X-Olympus-Token": "wrong"})) is False
    assert web._authorized(_FakeHandler({})) is False


def test_https_request_detection(monkeypatch):
    from olympus import web
    monkeypatch.delenv("OLYMPUS_SECURE_COOKIES", raising=False)
    assert web._https_request(_FakeHandler({"X-Forwarded-Proto": "https"})) is True
    assert web._https_request(_FakeHandler({"X-Forwarded-Proto": "http"})) is False
    assert web._https_request(_FakeHandler({})) is False
    monkeypatch.setenv("OLYMPUS_SECURE_COOKIES", "1")
    assert web._https_request(_FakeHandler({})) is True


# --- transport: Secure cookie behind TLS ---------------------------------

@pytest.fixture()
def server(monkeypatch):
    from olympus import web
    monkeypatch.setattr(web, "_SESSIONS", {})
    monkeypatch.setattr(web, "_HITS", {})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_cookie_secure_only_under_https(server):
    # Plain HTTP request: no Secure (so local http:// dev still works).
    plain = urllib.request.urlopen(server + "/")
    assert "Secure" not in plain.headers.get("Set-Cookie", "")

    # Simulated proxied HTTPS request: cookie must be Secure.
    req = urllib.request.Request(server + "/",
                                 headers={"X-Forwarded-Proto": "https"})
    tls = urllib.request.urlopen(req)
    sc = tls.headers.get("Set-Cookie", "")
    assert "Secure" in sc and "HttpOnly" in sc and "SameSite=Lax" in sc

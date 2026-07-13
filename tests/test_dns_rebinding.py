"""DNS rebinding: the fetch path must connect to the exact IP the SSRF gate
validated, never re-resolve the hostname. An attacker who answers the first
lookup with a public address and the second with 169.254.169.254 must find no
second lookup to poison — the socket dials the pinned address, and the
connect-time re-validation refuses a flipped record outright.
"""

import pytest

from olympus import security, tools

PUBLIC_IP = "93.184.216.34"
INTERNAL_IP = "169.254.169.254"


def _addrinfo(ip: str):
    import socket
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
             (ip, 80))]


# --- resolve_pinned_ip: the validating resolver ---------------------------

def test_resolver_returns_validated_ip(monkeypatch):
    monkeypatch.setattr(security.socket, "getaddrinfo",
                        lambda *a, **k: _addrinfo(PUBLIC_IP))
    assert security.resolve_pinned_ip("example.com", 80) == PUBLIC_IP


def test_resolver_refuses_internal_address(monkeypatch):
    monkeypatch.setattr(security.socket, "getaddrinfo",
                        lambda *a, **k: _addrinfo(INTERNAL_IP))
    with pytest.raises(ValueError):
        security.resolve_pinned_ip("rebind.example.com", 80)


def test_resolver_refuses_mixed_records(monkeypatch):
    """One poisoned A record among clean ones poisons the whole answer."""
    monkeypatch.setattr(
        security.socket, "getaddrinfo",
        lambda *a, **k: _addrinfo(PUBLIC_IP) + _addrinfo(INTERNAL_IP))
    with pytest.raises(ValueError):
        security.resolve_pinned_ip("rebind.example.com", 80)


def test_resolver_refuses_blocked_hostname_without_dns(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not resolve a name-blocked host")
    monkeypatch.setattr(security.socket, "getaddrinfo", boom)
    with pytest.raises(ValueError):
        security.resolve_pinned_ip("metadata.google.internal", 80)


# --- the pinned connection: dials the IP, not the name --------------------

def test_connection_dials_pinned_ip_not_hostname(monkeypatch):
    monkeypatch.setattr(security.socket, "getaddrinfo",
                        lambda *a, **k: _addrinfo(PUBLIC_IP))
    dialed = {}

    def fake_create_connection(addr, *a, **k):
        dialed["addr"] = addr
        raise OSError("stop before real I/O")

    monkeypatch.setattr(tools._socket, "create_connection",
                        fake_create_connection)
    conn = tools._PinnedHTTPConnection("example.com", 80, timeout=5)
    with pytest.raises(OSError):
        conn.connect()
    assert dialed["addr"] == (PUBLIC_IP, 80)


def test_connection_refuses_record_flipped_to_internal(monkeypatch):
    """The rebinding scenario proper: by connect time the record points at the
    metadata service — the connection must refuse, and must never dial."""
    monkeypatch.setattr(security.socket, "getaddrinfo",
                        lambda *a, **k: _addrinfo(INTERNAL_IP))

    def boom(*a, **k):
        raise AssertionError("must not open a socket to a refused host")

    monkeypatch.setattr(tools._socket, "create_connection", boom)
    conn = tools._PinnedHTTPConnection("rebind.example.com", 80, timeout=5)
    with pytest.raises(tools._urlerr.URLError):
        conn.connect()


def test_https_connection_refuses_flip_too(monkeypatch):
    monkeypatch.setattr(security.socket, "getaddrinfo",
                        lambda *a, **k: _addrinfo(INTERNAL_IP))
    monkeypatch.setattr(tools._socket, "create_connection",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not dial")))
    conn = tools._PinnedHTTPSConnection("rebind.example.com", 443, timeout=5)
    with pytest.raises(tools._urlerr.URLError):
        conn.connect()


# --- the opener wires the pinned classes in -------------------------------

def test_pinned_opener_uses_pinned_handlers():
    opener = tools._pinned_opener()
    kinds = {type(h).__name__ for h in opener.handlers}
    assert "_PinnedHTTPHandler" in kinds
    assert "_PinnedHTTPSHandler" in kinds
    assert "_SafeRedirectHandler" in kinds


# --- proxy-aware fetch (regression: pinning must not bypass an HTTP proxy) ---

def test_proxied_detects_configured_proxy(monkeypatch):
    monkeypatch.setattr(tools._urlreq, "getproxies", lambda: {"https": "http://127.0.0.1:8080"})
    monkeypatch.setattr(tools._urlreq, "proxy_bypass", lambda h: False)
    assert tools._proxied("https://example.com") is True
    monkeypatch.setattr(tools._urlreq, "getproxies", lambda: {})
    assert tools._proxied("https://example.com") is False


def test_proxy_bypass_is_honored(monkeypatch):
    monkeypatch.setattr(tools._urlreq, "getproxies", lambda: {"https": "http://127.0.0.1:8080"})
    monkeypatch.setattr(tools._urlreq, "proxy_bypass", lambda h: True)   # NO_PROXY match
    assert tools._proxied("https://internal.example") is False


def test_http_get_uses_proxy_opener_when_proxied(monkeypatch):
    """Proxied fetch must go through the proxy opener (which respects the
    proxy), NOT the pinning opener that dials the target IP directly."""
    monkeypatch.setattr(tools, "_proxied", lambda url: True)
    used = {}
    monkeypatch.setattr(tools, "_proxy_opener",
                        lambda: _fake_opener(used, "proxy"))
    monkeypatch.setattr(tools, "_pinned_opener",
                        lambda: _fake_opener(used, "pinned"))
    # In proxy mode the SSRF check must not resolve (target resolves to the
    # proxy's loopback locally); a benign public URL should pass.
    tools._http_get("https://example.com")
    assert used["opener"] == "proxy"


def test_http_get_uses_pinned_opener_when_direct(monkeypatch):
    monkeypatch.setattr(tools, "_proxied", lambda url: False)
    monkeypatch.setattr(security, "url_block_reason", lambda url, **kw: None)
    used = {}
    monkeypatch.setattr(tools, "_proxy_opener", lambda: _fake_opener(used, "proxy"))
    monkeypatch.setattr(tools, "_pinned_opener", lambda: _fake_opener(used, "pinned"))
    tools._http_get("https://example.com")
    assert used["opener"] == "pinned"


class _FakeResp:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return b"ok"


def _fake_opener(used, tag):
    class _O:
        def open(self, req, timeout=0):
            used["opener"] = tag
            return _FakeResp()
    return _O()

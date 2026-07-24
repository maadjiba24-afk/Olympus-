"""Self-assessment: Olympus attacks YOUR OWN local app to find its vulns.

Two layers under test: (A) the SSRF loopback allowance that makes local self-
testing possible while remaining STRUCTURALLY incapable of reaching a routable
target, and (B) the end-to-end orchestration (crawl → benign active validation)
against a loopback app, refusing anything non-local.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlparse

import pytest

from olympus import assess, security, selfassess, tools


# =====================================================================
# A. SSRF loopback allowance — the safety spine
# =====================================================================

def test_allowance_refuses_routable_hosts():
    for bad in ("example.com", "8.8.8.8", "169.254.169.254", "10.0.0.5",
                "metadata.google.internal"):
        with pytest.raises(ValueError):
            with security.allow_local_target(bad, 80):
                pass


def test_allowance_accepts_loopback_forms():
    for good in ("127.0.0.1", "localhost", "::1", "127.5.5.5"):
        with security.allow_local_target(good, 8000):
            pass                                     # no raise


def test_loopback_blocked_without_allowance():
    # Default posture: loopback is an SSRF target and is refused (resolve path
    # validates the IP; the name "localhost" is blocked by name too).
    assert security.url_block_reason("http://127.0.0.1:8000/", resolve=True)
    assert security.url_block_reason("http://localhost:8000/", resolve=False)


def test_allowance_permits_only_the_exact_target():
    with security.allow_local_target("127.0.0.1", 8000):
        assert security.url_block_reason("http://127.0.0.1:8000/x", resolve=True) is None
        # exact PORT: a different port stays blocked
        assert security.url_block_reason("http://127.0.0.1:9999/", resolve=True)
        # the allowance NEVER widens to other internal targets (metadata / RFC1918
        # stay blocked even while a loopback allowance is active)
        assert security.url_block_reason("http://169.254.169.254/", resolve=True)
        assert security.url_block_reason("http://10.0.0.5:8000/", resolve=True)


def test_allowance_resets_after_context():
    with security.allow_local_target("127.0.0.1", 8000):
        pass
    assert security.url_block_reason("http://127.0.0.1:8000/", resolve=True)


def test_allowance_still_refuses_rebind_to_public(monkeypatch):
    # A permitted loopback NAME that resolves to a PUBLIC ip is still refused —
    # the pin requires an actual loopback resolution.
    def _public(host, port, *a, **k):
        return [(2, 1, 6, "", ("93.184.216.34", port))]     # a public IP
    monkeypatch.setattr(security.socket, "getaddrinfo", _public)
    with security.allow_local_target("localhost", 8000):
        with pytest.raises(ValueError):
            security.resolve_pinned_ip("localhost", 8000)


def test_resolve_pins_loopback_when_allowed(monkeypatch):
    def _lb(host, port, *a, **k):
        return [(2, 1, 6, "", ("127.0.0.1", port))]
    monkeypatch.setattr(security.socket, "getaddrinfo", _lb)
    with security.allow_local_target("localhost", 8000):
        assert security.resolve_pinned_ip("localhost", 8000) == "127.0.0.1"
    # outside the allowance, the same loopback resolution is refused
    with pytest.raises(ValueError):
        security.resolve_pinned_ip("localhost", 8000)


# =====================================================================
# B. Orchestration — loopback-only, crawl → validate
# =====================================================================

def test_is_local_target():
    assert selfassess.is_local_target("http://127.0.0.1:8000/") is True
    assert selfassess.is_local_target("http://localhost:3000/app") is True
    assert selfassess.is_local_target("https://example.com/") is False
    assert selfassess.is_local_target("ftp://127.0.0.1/") is False


def test_selfassess_refuses_non_local():
    out = selfassess.selfassess("https://example.com/")
    assert out.get("refused") is True
    assert "local" in out["error"].lower()
    assert assess.list_findings() == []              # nothing assessed


def _vuln_app_probe():
    """A tiny fake local app: the index links to /search?q=, and any reflected
    query value is echoed UNESCAPED (a real XSS surface)."""
    def _probe(url, max_bytes=400_000, follow_redirects=True, extra_headers=None):
        p = urlparse(url)
        q = dict(parse_qsl(p.query, keep_blank_values=True))
        if p.path in ("", "/"):
            body = '<html><body><a href="/search?q=hi">search</a></body></html>'
        else:
            body = "<html>" + " ".join(f"{v}" for v in q.values()) + "</html>"
        return {"status": 200, "headers": {"content-type": "text/html"},
                "body": body, "url": url}
    return _probe


def test_selfassess_finds_xss_in_local_app(monkeypatch):
    monkeypatch.setattr(tools, "_http_probe", _vuln_app_probe())
    out = selfassess.selfassess("http://127.0.0.1:8000/")
    assert out["total_findings"] > 0
    cwes = {f["cwe"] for f in out["findings"]}
    assert "CWE-79" in cwes                           # reflected XSS confirmed
    assert out["crawled_param_urls"] >= 1             # crawl found /search?q=
    assert any(ph.startswith("validate") for ph in out["phases"])


def test_selfassess_grants_scope_for_the_local_target(monkeypatch):
    monkeypatch.setattr(tools, "_http_probe", _vuln_app_probe())
    selfassess.selfassess("http://127.0.0.1:8000/")
    assert assess.in_scope("127.0.0.1") is True       # auto-authorized the app


def test_crawl_stays_same_origin_including_port():
    # A crafted link to ANOTHER local port is not same-origin → not crawled
    # (and the allowance would refuse it anyway). Host+port must both match.
    base = "http://127.0.0.1:8000/"
    assert selfassess._same_origin("http://127.0.0.1:8000/x", base) is True
    assert selfassess._same_origin("http://127.0.0.1:9999/x", base) is False
    assert selfassess._same_origin("http://localhost:8000/x", base) is False
    assert selfassess._same_origin("https://127.0.0.1:8000/x", base) is False


def test_selfassess_threads_cookie_to_probes(monkeypatch):
    # An authenticated self-assessment: the cookie reaches the crawl + validation
    # probes so behind-login pages are tested.
    seen = []
    def _probe(url, max_bytes=400_000, follow_redirects=True, extra_headers=None):
        seen.append((extra_headers or {}).get("Cookie"))
        return {"status": 200, "headers": {"content-type": "text/html"},
                "body": '<a href="/x?q=1">x</a>', "url": url}
    monkeypatch.setattr(tools, "_http_probe", _probe)
    out = selfassess.selfassess("http://127.0.0.1:8000/", cookie="session=abc")
    assert "authenticated" in out["phases"]
    assert any(c == "session=abc" for c in seen)      # cookie reached the probes


# --- CSRF-token-absence detection (CWE-352) at the crawl level ---------------

def _form_app_probe(with_token: bool):
    """A local app whose index has a state-changing POST form; optionally it
    carries a hidden anti-CSRF token field."""
    token = '<input type="hidden" name="csrf_token" value="abc">' if with_token else ""
    html = (f'<html><form method="POST" action="/transfer">{token}'
            '<input name="amount"><input name="to"></form></html>')
    def _probe(url, max_bytes=400_000, follow_redirects=True, extra_headers=None):
        return {"status": 200, "headers": {"content-type": "text/html"},
                "body": html, "url": url}
    return _probe


def test_selfassess_flags_csrf_when_no_token(monkeypatch):
    monkeypatch.setattr(tools, "_http_probe", _form_app_probe(with_token=False))
    out = selfassess.selfassess("http://127.0.0.1:8000/")
    csrf = [f for f in out["findings"] if f["cwe"] == "CWE-352"]
    assert len(csrf) == 1 and csrf[0]["source"] == "active_validation"
    assert any(ph.startswith("csrf") for ph in out["phases"])


def test_selfassess_no_csrf_flag_when_token_present(monkeypatch):
    monkeypatch.setattr(tools, "_http_probe", _form_app_probe(with_token=True))
    out = selfassess.selfassess("http://127.0.0.1:8000/")
    assert [f for f in out["findings"] if f["cwe"] == "CWE-352"] == []


def test_get_form_is_not_a_csrf_finding(monkeypatch):
    # A GET form is not state-changing → no CSRF finding (it's a param source).
    html = '<html><form method="GET" action="/search"><input name="q"></form></html>'
    monkeypatch.setattr(tools, "_http_probe", lambda url, **k: {
        "status": 200, "headers": {"content-type": "text/html"},
        "body": html, "url": url})
    out = selfassess.selfassess("http://127.0.0.1:8000/")
    assert [f for f in out["findings"] if f["cwe"] == "CWE-352"] == []

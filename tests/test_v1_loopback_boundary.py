"""Adversarial tests for the /v1/* loopback boundary (SPEC-01 item 1).

The guarantee under review: when OLYMPUS_API_KEYS is UNSET, the OpenAI-compatible
routes (/v1/chat/completions, /v1/models) must be reachable ONLY from loopback,
and must NEVER act as an open relay to whatever provider key Olympus holds.

The subtle failure these tests hunt for: deciding "is this client remote?" from
something the client controls (a Host header, X-Forwarded-For, X-Real-IP) instead
of from the kernel-reported peer address. A header-based check is spoofable, which
makes the "loopback-only" guarantee fake — and behind a reverse proxy (this repo
ships a Caddyfile) the peer address is the proxy's loopback address, so a naive
"peer == 127.0.0.1" check exposes /v1/* to the whole internet while believing it
is safe.

These tests are written against the BEHAVIOR CONTRACT, not against exact internal
names, because the precise predicate may differ in the merged code. Each test
marked `WIRE:` has a one-line spot to point at the real handler/function. Where a
test encodes the *correct* defensive behavior that may not be implemented yet, it
is marked with `pytest.mark.xfail(strict=False)` so it documents the intended
contract without breaking CI today — flip it to a hard assertion once item 1 is
confirmed/fixed.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from olympus import config, web


# --------------------------------------------------------------------------
# Harness (mirrors tests/test_web_and_settings.py)
# --------------------------------------------------------------------------
@pytest.fixture()
def server(monkeypatch):
    # No API keys configured: this is the regime where loopback-only must hold.
    monkeypatch.delenv("OLYMPUS_API_KEYS", raising=False)
    monkeypatch.setattr(web, "_SESSIONS", {}, raising=False)
    monkeypatch.setattr(web, "_HITS", {}, raising=False)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _req(url, path, *, method="POST", headers=None, payload=None):
    data = json.dumps(payload or {"model": "olympus-council",
                                  "messages": [{"role": "user",
                                                "content": "hi"}]}).encode()
    h = {"Content-Type": "application/json", **(headers or {})}
    return urllib.request.Request(url + path, data=data if method == "POST" else None,
                                  headers=h, method=method)


def _status(url, path, **kw):
    try:
        with urllib.request.urlopen(_req(url, path, **kw)) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


# --------------------------------------------------------------------------
# Group A — a header's VALUE must never decide remoteness (anti-spoofing).
#
# Two sub-cases, reconciled with Group C:
#   * A header whose value merely *claims* a remote origin but is not a
#     reverse-proxy forwarding header (e.g. Host) must be IGNORED — a loopback
#     connection stays served (200). The decision isn't driven by header values.
#   * A reverse-proxy *forwarding* header (X-Forwarded-For / X-Real-IP /
#     Forwarded) is treated as proof the loopback peer is a proxy relaying an
#     off-box client (the Caddy trap, see Group C). With no keys configured that
#     is refused (key required) rather than served — closing the open relay.
#
# So the contract is: header *values* never grant/deny; the *presence* of a
# forwarding header denies (requires a key) when no keys are set. Both are
# header-independent in the dangerous direction — nothing a client sends can
# turn a refusal into an *allow*.
# --------------------------------------------------------------------------
# (header, expected_status_when_no_keys)
SPOOF_REMOTE_HEADERS = [
    ({"X-Forwarded-For": "8.8.8.8"}, 401),               # forwarding header → key required
    ({"X-Forwarded-For": "203.0.113.7, 127.0.0.1"}, 401),
    ({"X-Real-IP": "8.8.8.8"}, 401),
    ({"Host": "evil.example.com"}, 200),                 # not a forwarding header → ignored
    ({"Host": "8.8.8.8:8484"}, 200),
    ({"Forwarded": "for=8.8.8.8;host=evil.example.com;proto=https"}, 401),
]


@pytest.mark.parametrize("hdr,expected", SPOOF_REMOTE_HEADERS)
def test_loopback_request_header_contract(server, hdr, expected):
    """A non-forwarding header claiming a remote origin is ignored (loopback
    stays served); a reverse-proxy forwarding header requires a key when none is
    configured. Either way the outcome is decided by the peer socket + the
    header's presence, never by a (spoofable) header value."""
    code = _status(server, "/v1/models", method="GET", headers=hdr)
    assert code == expected, (
        f"header {hdr} produced {code}, expected {expected}; remoteness must "
        "come from the peer socket and a forwarding header must require a key")


# --------------------------------------------------------------------------
# Group B — the dangerous direction. A REMOTE peer that lies about being
# loopback (or sits behind a proxy) must still be refused when no keys are set.
# We can't open a non-loopback socket in a unit test, so we drive the
# remoteness predicate directly with a faked peer address. This is the core
# of item 1.
#
# WIRE: set REMOTENESS to the actual predicate the merged code uses. It is one
# of these shapes — uncomment/point at whichever exists:
#   - a module function:        web._is_loopback(client_address)  -> bool
#   - a module function:        web._v1_allowed(handler)          -> bool
#   - a Handler method:         Handler._v1_local_ok()            -> bool
# The test asserts: given a non-loopback peer and NO keys, access is denied.
# --------------------------------------------------------------------------
def _resolve_remoteness_predicate():
    """Best-effort discovery of the loopback/allow predicate so this test wires
    to the real code. Returns a callable(peer_ip:str, headers:dict)->allowed:bool
    or None if it can't be found (test then xfails as a contract reminder)."""
    # WIRED: web._v1_allowed(peer_ip, headers) is the real /v1/* allow decision
    # used by Handler._v1_authorized when OLYMPUS_API_KEYS is unset (it composes
    # the same _is_loopback + _forwarding_headers_present primitives the handler
    # branches on). It reflects both the loopback-peer and the proxy-header rule.
    _v1 = getattr(web, "_v1_allowed", None)
    if callable(_v1):
        return (lambda ip, h, _fn=_v1: bool(_fn(ip, h))), "_v1_allowed"
    # Common explicit helpers:
    for name in ("_is_loopback", "is_loopback", "_loopback", "_peer_is_local"):
        fn = getattr(web, name, None)
        if callable(fn):
            # these take an address/ip; treat True(loopback) -> allowed
            def adapter(ip, _h, _fn=fn):
                try:
                    return bool(_fn(ip))
                except TypeError:
                    return bool(_fn((ip, 12345)))
            return adapter, name
    for name in ("_v1_allowed", "_v1_local_ok", "_openai_allowed"):
        fn = getattr(web, name, None)
        if callable(fn):
            return (lambda ip, h, _fn=fn: bool(_fn(ip, h))), name
    return None, None


@pytest.mark.parametrize("peer_ip,headers", [
    ("8.8.8.8", {}),                                   # plain remote
    ("8.8.8.8", {"X-Forwarded-For": "127.0.0.1"}),     # remote lying via XFF
    ("8.8.8.8", {"X-Real-IP": "127.0.0.1"}),           # remote lying via X-Real-IP
    ("8.8.8.8", {"Host": "localhost"}),                # remote lying via Host
    ("203.0.113.9", {"Forwarded": "for=127.0.0.1"}),   # remote lying via Forwarded
    ("::ffff:8.8.8.8", {}),                            # v4-mapped-v6 remote
    ("10.0.0.5", {}),                                  # RFC1918 is NOT loopback
    ("169.254.1.1", {}),                               # link-local is NOT loopback
])
def test_remote_peer_is_refused_without_keys(peer_ip, headers):
    predicate, name = _resolve_remoteness_predicate()
    if predicate is None:
        pytest.xfail("WIRE: point _resolve_remoteness_predicate at the real "
                     "loopback check; could not auto-discover it. The contract "
                     "this asserts: a non-loopback peer with no keys is denied.")
    allowed = predicate(peer_ip, headers)
    assert allowed is False, (
        f"peer {peer_ip} with headers {headers} was ALLOWED by {name}() while "
        "OLYMPUS_API_KEYS is unset — this is an open-relay / spoofable boundary")


# --------------------------------------------------------------------------
# Group C — the reverse-proxy trap, asserted as a property of the predicate.
# Behind Caddy, the kernel peer is the proxy (127.0.0.1) but the request is
# really remote. The ONLY safe behavior when no keys are set is: do not rely on
# the loopback peer to imply safety for proxied requests. The defensible rule is
# "require a key whenever a forwarding header is present" (i.e. you appear to be
# behind a proxy). This rule is now implemented (web._v1_allowed), so the
# assertion below is HARD — no xfail.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("headers", [
    {"X-Forwarded-For": "8.8.8.8"},
    {"X-Real-IP": "8.8.8.8"},
    {"Forwarded": "for=8.8.8.8"},
])
def test_proxied_loopback_requires_key(headers):
    predicate, name = _resolve_remoteness_predicate()
    if predicate is None:
        pytest.xfail("WIRE: predicate not discoverable; see Group B note.")
    # Peer is loopback (the proxy), but forwarding headers reveal a real remote
    # client. With no keys configured this must be refused.
    allowed = predicate("127.0.0.1", headers)
    assert allowed is False, (
        f"{name}() trusts a loopback peer that is forwarding a remote client "
        f"({headers}); behind a reverse proxy this exposes /v1/* publicly")


# --------------------------------------------------------------------------
# Group D — positive control: with keys SET, a valid bearer works and a missing
# bearer is 401 even on loopback (so the loopback path doesn't silently bypass
# auth once auth exists).
# --------------------------------------------------------------------------
def test_keys_set_loopback_still_requires_bearer(monkeypatch):
    monkeypatch.setenv("OLYMPUS_API_KEYS", "realkey")
    monkeypatch.setattr(web, "_SESSIONS", {}, raising=False)
    monkeypatch.setattr(web, "_HITS", {}, raising=False)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        # No bearer → 401 even though we're on loopback.
        assert _status(base, "/v1/models", method="GET") == 401
        # Wrong bearer → 401.
        assert _status(base, "/v1/models", method="GET",
                       headers={"Authorization": "Bearer nope"}) == 401
        # Right bearer → 200.
        assert _status(base, "/v1/models", method="GET",
                       headers={"Authorization": "Bearer realkey"}) == 200
    finally:
        srv.shutdown()

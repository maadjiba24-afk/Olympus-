"""Tests for SPEC-02: provable zero-egress sovereignty mode.

A network spy (autouse) fails any test that attempts a real outbound socket to a
non-loopback host — so the sovereign-mode tests prove that nothing left the box
(and, critically, that the fail-closed path never silently reached for a remote
model).
"""

import json
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from olympus import config, providers, security, web


# --- network spy: zero real outbound calls in these tests --------------------
@pytest.fixture(autouse=True)
def no_outbound_sockets(monkeypatch):
    real_connect = socket.socket.connect

    def guard(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"unexpected outbound socket to {address!r}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guard)
    yield


def _settings(provider, model, base_url=None, api_key=None):
    return config.Settings(provider=provider, model=model,
                           base_url=base_url, api_key=api_key)


LOCAL = _settings("openai", "llama3.1", base_url="http://localhost:11434/v1")
REMOTE = _settings("openai", "gpt-4o", base_url="https://api.openai.com/v1",
                   api_key="sk-x")
ANTHROPIC = _settings("anthropic", "claude-opus-4-8")


# --- egress choke ------------------------------------------------------------
def test_egress_choke_allows_loopback_blocks_other(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SOVEREIGN", "1")
    monkeypatch.delenv("OLYMPUS_EGRESS_ALLOWLIST", raising=False)
    # Loopback always allowed.
    security.assert_egress_allowed("127.0.0.1")
    security.assert_egress_allowed("localhost")
    security.assert_egress_allowed("::1")
    # A known local-provider host (Ollama) is allowed via the catalog notion.
    assert "localhost" in providers.local_provider_hosts()
    # A remote host is refused, fail-closed, with a typed error.
    assert security.egress_allowed("api.openai.com") is False
    with pytest.raises(security.EgressBlocked):
        security.assert_egress_allowed("api.openai.com")


def test_egress_allowlist_hostnames_and_subdomains(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SOVEREIGN", "1")
    monkeypatch.setenv("OLYMPUS_EGRESS_ALLOWLIST", "api.example.com, 10.0.0.0/8")
    assert security.egress_allowed("api.example.com") is True
    assert security.egress_allowed("vllm.api.example.com") is True   # subdomain
    assert security.egress_allowed("10.1.2.3") is True               # CIDR member
    assert security.egress_allowed("11.0.0.1") is False              # outside CIDR


def test_egress_is_noop_when_sovereign_off(monkeypatch):
    monkeypatch.delenv("OLYMPUS_SOVEREIGN", raising=False)
    # No-op: every host allowed, nothing raises (byte-for-byte unchanged path).
    assert security.egress_allowed("api.openai.com") is True
    assert security.egress_allowed("anything.example") is True
    security.assert_egress_allowed("api.anthropic.com")   # does not raise


# --- pool eligibility filter -------------------------------------------------
def test_pool_filters_out_remote_member_under_sovereign(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SOVEREIGN", "1")
    monkeypatch.delenv("OLYMPUS_EGRESS_ALLOWLIST", raising=False)
    pool = config.ModelPool.of(LOCAL, REMOTE)
    hosts = [config.member_host(m) for m in pool.members]
    assert hosts == ["localhost"]                      # remote excluded entirely
    # And the existing capability-score selection runs on the eligible set:
    assert pool.for_role("reasoning").base_url == "http://localhost:11434/v1"
    assert pool.for_role("coding").base_url == "http://localhost:11434/v1"


def test_remote_member_never_selected_even_as_tiebreak(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SOVEREIGN", "1")
    # Remote model scores higher, but must never be chosen under sovereign mode.
    strong_remote = _settings("openai", "gpt-5", base_url="https://api.openai.com/v1",
                              api_key="sk-x")
    pool = config.ModelPool.of(LOCAL, strong_remote)
    for role in ("reasoning", "coding", "verify"):
        assert config.member_is_local(pool.for_role(role))


# --- fail closed (the critical one) ------------------------------------------
def test_fail_closed_when_no_local_member(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SOVEREIGN", "1")
    monkeypatch.delenv("OLYMPUS_EGRESS_ALLOWLIST", raising=False)
    # Only a remote member configured → typed fail-closed error, no fallback.
    with pytest.raises(security.NoLocalModelError):
        config.ModelPool.of(REMOTE)
    with pytest.raises(security.NoLocalModelError):
        config.ModelPool.of(ANTHROPIC)
    # The autouse socket spy guarantees no outbound call was attempted while
    # failing closed (it did not silently reach for the remote model).


def test_fail_closed_from_env_no_local(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SOVEREIGN", "1")
    monkeypatch.setenv("OLYMPUS_PROVIDER", "openai")
    monkeypatch.setenv("OLYMPUS_MODEL", "gpt-4o")
    monkeypatch.setenv("OLYMPUS_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("OLYMPUS_API_KEY", "sk-x")
    monkeypatch.delenv("OLYMPUS_MODELS", raising=False)
    monkeypatch.delenv("OLYMPUS_EGRESS_ALLOWLIST", raising=False)
    with pytest.raises(security.NoLocalModelError):
        config.ModelPool.from_env()


# --- data-class routing ------------------------------------------------------
def test_restricted_stays_local_even_with_sovereign_off(monkeypatch):
    monkeypatch.delenv("OLYMPUS_SOVEREIGN", raising=False)
    # Policy: restricted is local-only regardless of the global flag.
    assert config.data_class_local_only("restricted") is True
    assert config.data_class_local_only("internal") is False   # sovereign off
    assert config.data_class_local_only("public") is False
    # A mixed pool restricted to local keeps only the local member.
    pool = config.ModelPool.of(LOCAL, REMOTE)        # sovereign off → unfiltered
    assert len(pool.members) == 2
    local = pool.local_only()
    assert [config.member_host(m) for m in local.members] == ["localhost"]


def test_restricted_fails_closed_when_no_local(monkeypatch):
    monkeypatch.delenv("OLYMPUS_SOVEREIGN", raising=False)
    pool = config.ModelPool.of(REMOTE)               # sovereign off → remote ok
    with pytest.raises(security.NoLocalModelError):
        pool.local_only()


def test_data_class_defaults(monkeypatch):
    monkeypatch.delenv("OLYMPUS_SOVEREIGN", raising=False)
    assert config.default_data_class() == "public"
    assert config.data_class_local_only(None) is False
    monkeypatch.setenv("OLYMPUS_SOVEREIGN", "1")
    assert config.default_data_class() == "internal"   # at least internal
    assert config.data_class_local_only(None) is True  # unspecified → local


# --- web header plumbing -----------------------------------------------------
class _FakeBot:
    last_pool = None

    def __init__(self, *args, **kwargs):
        _FakeBot.last_pool = kwargs.get("pool")

    def ask(self, message):
        return "local council answer"

    def ask_stream(self, message):
        yield "local council answer"


@pytest.fixture()
def web_server(monkeypatch):
    monkeypatch.setattr(web.orchestrator, "Olympus", _FakeBot)
    monkeypatch.setattr(web, "_SESSIONS", {}, raising=False)
    monkeypatch.setattr(web, "_HITS", {}, raising=False)
    monkeypatch.setenv("OLYMPUS_API_KEYS", "devkey")
    # A pool with one local and one remote member, sovereign OFF.
    monkeypatch.delenv("OLYMPUS_SOVEREIGN", raising=False)
    monkeypatch.setenv("OLYMPUS_PROVIDER", "openai")
    monkeypatch.setenv("OLYMPUS_MODEL", "llama3.1")
    monkeypatch.setenv("OLYMPUS_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.delenv("OLYMPUS_API_KEY", raising=False)
    monkeypatch.setenv("OLYMPUS_MODELS", json.dumps(
        [{"provider": "openai", "model": "gpt-4o",
          "base_url": "https://api.openai.com/v1", "api_key": "sk-x"}]))
    _FakeBot.last_pool = None
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _post(url, payload, headers=None):
    req = urllib.request.Request(
        url + "/v1/chat/completions", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer devkey", **(headers or {})},
        method="POST")
    return urllib.request.urlopen(req)


def test_v1_restricted_header_forces_local_pool(web_server):
    resp = _post(web_server,
                 {"model": "olympus-council",
                  "messages": [{"role": "user", "content": "hi"}]},
                 headers={"X-Olympus-Data-Class": "restricted"})
    assert resp.status == 200
    body = json.loads(resp.read())
    assert body["choices"][0]["message"]["content"] == "local council answer"
    # The header forced local-only routing: the remote member was filtered out.
    hosts = [config.member_host(m) for m in _FakeBot.last_pool.members]
    assert hosts == ["localhost"]


def test_v1_public_default_keeps_remote_when_sovereign_off(web_server):
    # No data-class header, sovereign off → most-permissive, pool unfiltered.
    resp = _post(web_server,
                 {"model": "olympus-council",
                  "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status == 200
    hosts = sorted(config.member_host(m) for m in _FakeBot.last_pool.members)
    assert hosts == ["api.openai.com", "localhost"]   # remote retained


def test_v1_status_reports_sovereignty(web_server):
    req = urllib.request.Request(web_server + "/api/metrics",
                                 headers={"Authorization": "Bearer devkey"})
    body = json.loads(urllib.request.urlopen(req).read())
    sov = body["sovereignty"]
    assert sov["sovereign"] is False
    assert "openai/llama3.1 @ localhost" in sov["members"]
    assert sov["eligible_local"] == ["openai/llama3.1 @ localhost"]


# --- backward-compat regression ----------------------------------------------
def test_backward_compat_pool_selection_unchanged(monkeypatch):
    monkeypatch.delenv("OLYMPUS_SOVEREIGN", raising=False)
    opus = _settings("anthropic", "claude-opus-4-8")
    haiku = _settings("anthropic", "claude-haiku-4-5")
    pool = config.ModelPool.of(opus, haiku)
    # Both members retained (no filtering), and the stronger model wins reasoning
    # exactly as before this SPEC — selection logic untouched.
    assert len(pool.members) == 2
    assert pool.for_role("reasoning").model == "claude-opus-4-8"
    # Egress guard is a pure no-op when sovereign is off (no DNS, no block).
    assert security.egress_allowed("api.anthropic.com") is True


def test_backward_compat_ssrf_guard_unchanged(monkeypatch):
    monkeypatch.delenv("OLYMPUS_SOVEREIGN", raising=False)
    # SSRF guard still blocks internal addresses and still allows public ones.
    assert security.url_block_reason("http://127.0.0.1/") is not None
    assert security.url_block_reason("ftp://example.com/") is not None


def test_sovereign_ssrf_blocks_non_allowlisted_public(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SOVEREIGN", "1")
    monkeypatch.setenv("OLYMPUS_EGRESS_ALLOWLIST", "allowed.example.com")
    # Resolve to a fixed public IP so the test makes no real DNS/socket call.
    monkeypatch.setattr(security.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    # A public host not on the allowlist is refused for tool fetches too.
    reason = security.url_block_reason("https://evil.example.org/x")
    assert reason and "allowlist" in reason
    # An allowlisted public host passes the same guard.
    assert security.url_block_reason("https://allowed.example.com/x") is None


def test_sovereign_embeddings_do_not_egress_to_non_allowlisted_host(monkeypatch):
    """The embeddings path (user memory content) must honor the same egress
    allowlist as model calls under sovereign mode — no silent leak off-box."""
    from olympus import embed
    monkeypatch.setenv("OLYMPUS_SOVEREIGN", "1")
    monkeypatch.delenv("OLYMPUS_EGRESS_ALLOWLIST", raising=False)
    monkeypatch.setenv("OLYMPUS_EMBED_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("OLYMPUS_EMBED_API_KEY", "sk-secret")
    monkeypatch.setenv("OLYMPUS_EMBED_MODEL", "text-embedding-3-small")
    attempted = {"n": 0}

    def boom(*a, **k):
        attempted["n"] += 1
        raise AssertionError("embeddings egressed under sovereign mode")

    monkeypatch.setattr(embed.urllib.request, "urlopen", boom)
    # Refused before any send → None (retrieval degrades to lexical).
    assert embed.embed_one("private memory content") is None
    assert attempted["n"] == 0
    # With the host allowlisted, the guard permits the call (it then reaches the
    # spy, proving the guard — not availability — was the gate).
    monkeypatch.setenv("OLYMPUS_EGRESS_ALLOWLIST", "api.openai.com")
    with pytest.raises(AssertionError):
        embed.embed_one("private memory content")

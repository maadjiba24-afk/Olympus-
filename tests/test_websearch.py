"""The pluggable search-provider layer: provider order from env, fallthrough
on failure, cooldown on rate limits, TTL caching, and normalized results.
Network is fully stubbed via websearch._request / the DDG seam.
"""

import pytest

from olympus import websearch


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setattr(websearch, "_cache", {})
    monkeypatch.setattr(websearch, "_cooling", {})
    for var in ("OLYMPUS_SEARCH_PROVIDERS", "OLYMPUS_SEARXNG_URL",
                "BRAVE_SEARCH_API_KEY", "TAVILY_API_KEY", "SERPER_API_KEY",
                "GOOGLE_PSE_KEY", "GOOGLE_PSE_CX"):
        monkeypatch.delenv(var, raising=False)


def test_default_order_is_configured_then_ddg(monkeypatch):
    assert websearch.configured() == ["ddg"]
    monkeypatch.setenv("TAVILY_API_KEY", "t")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "b")
    assert websearch.configured() == ["brave", "tavily", "ddg"]


def test_explicit_order_wins(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SEARCH_PROVIDERS", "tavily, ddg, bogus")
    assert websearch.configured() == ["tavily", "ddg"]


def test_searxng_parse(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SEARXNG_URL", "http://localhost:8888")
    seen = {}

    def fake_request(url, payload=None, headers=None):
        seen["url"] = url
        return {"results": [{"title": "T", "url": "https://a.example/x",
                             "content": "snippet"},
                            {"title": "bad", "url": "ftp://nope"}]}

    monkeypatch.setattr(websearch, "_request", fake_request)
    out = websearch.results("hello world")
    assert out == [{"title": "T", "url": "https://a.example/x",
                    "snippet": "snippet"}]
    assert seen["url"].startswith("http://localhost:8888/search")
    assert "hello%20world" in seen["url"]


def test_fallthrough_on_provider_error(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "t")

    def fake_request(url, payload=None, headers=None):
        raise RuntimeError("tavily down")

    monkeypatch.setattr(websearch, "_request", fake_request)
    monkeypatch.setattr(websearch, "_ddg", lambda q, n: [
        {"title": "D", "url": "https://d.example", "snippet": ""}])
    # rebuild the registry entry to pick up the patched _ddg
    monkeypatch.setitem(websearch._PROVIDERS, "ddg",
                        ((), lambda q, n: [{"title": "D",
                                            "url": "https://d.example",
                                            "snippet": ""}]))
    out = websearch.results("q")
    assert out[0]["url"] == "https://d.example"


def test_rate_limited_provider_cools_down(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "t")
    calls = {"tavily": 0}

    def fake_request(url, payload=None, headers=None):
        calls["tavily"] += 1
        raise websearch.RateLimited(url)

    monkeypatch.setattr(websearch, "_request", fake_request)
    monkeypatch.setitem(websearch._PROVIDERS, "ddg",
                        ((), lambda q, n: [{"title": "D",
                                            "url": "https://d.example",
                                            "snippet": ""}]))
    websearch.results("q1")
    websearch.results("q2")
    assert calls["tavily"] == 1          # second query skipped the cooling provider
    assert websearch._cooling.get("tavily", 0) > 0


def test_results_are_cached(monkeypatch):
    calls = {"n": 0}

    def fake_ddg(q, n):
        calls["n"] += 1
        return [{"title": "D", "url": "https://d.example", "snippet": ""}]

    monkeypatch.setitem(websearch._PROVIDERS, "ddg", ((), fake_ddg))
    a = websearch.results("same query")
    b = websearch.results("same query")
    assert a == b and calls["n"] == 1


def test_egress_choke_is_applied(monkeypatch):
    checked = {}
    from olympus import security

    def fake_assert(host):
        checked["host"] = host

    monkeypatch.setattr(security, "assert_egress_allowed", fake_assert)

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"results": []}'

    monkeypatch.setattr(websearch.urllib.request, "urlopen",
                        lambda req, timeout=0: _Resp())
    websearch._request("https://api.example.com/search?q=x")
    assert checked["host"] == "api.example.com"


def test_search_text_renders_blocks(monkeypatch):
    monkeypatch.setitem(websearch._PROVIDERS, "ddg", ((), lambda q, n: [
        {"title": "T", "url": "https://a.example", "snippet": "s"}]))
    assert websearch.search_text("q") == "T\nhttps://a.example\ns"
    monkeypatch.setattr(websearch, "_cache", {})
    monkeypatch.setitem(websearch._PROVIDERS, "ddg", ((), lambda q, n: []))
    assert websearch.search_text("q2") == "No results found."

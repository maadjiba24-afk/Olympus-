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
                "SERPAPI_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_default_order_is_configured_then_ddg(monkeypatch):
    assert websearch.configured() == ["ddg"]
    monkeypatch.setenv("TAVILY_API_KEY", "t")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "b")
    assert websearch.configured() == ["brave", "tavily", "ddg"]


def test_explicit_order_wins(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SEARCH_PROVIDERS", "tavily, ddg, bogus")
    assert websearch.configured() == ["tavily", "ddg"]


def test_retired_google_pse_cannot_be_reenabled(monkeypatch):
    monkeypatch.setenv("GOOGLE_PSE_KEY", "legacy")
    monkeypatch.setenv("GOOGLE_PSE_CX", "legacy")

    assert "google_pse" not in websearch._PROVIDERS
    assert "google_pse" not in websearch.configured()


def test_bing_serpapi_parse(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "secret")
    monkeypatch.setenv("OLYMPUS_SEARCH_PROVIDERS", "bing")
    seen = {}

    def fake_request(url, payload=None, headers=None):
        seen["url"] = url
        return {
            "organic_results": [
                {
                    "title": "Bing result",
                    "link": "https://example.com/result",
                    "snippet": "A result returned by Bing.",
                },
                {
                    "title": "Invalid",
                    "link": "ftp://example.com/invalid",
                    "snippet": "Must be discarded.",
                },
            ]
        }

    monkeypatch.setattr(websearch, "_request", fake_request)

    assert websearch.results("olympus search", limit=5) == [
        {
            "title": "Bing result",
            "url": "https://example.com/result",
            "snippet": "A result returned by Bing.",
        }
    ]
    assert "https://serpapi.com/search?" in seen["url"]
    assert "engine=bing" in seen["url"]
    assert "q=olympus%20search" in seen["url"]
    assert "api_key=secret" in seen["url"]


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


# --- provider diagnostics --------------------------------------------------


def test_diagnostics_config_only_never_contacts_provider(monkeypatch):
    calls = []

    monkeypatch.setitem(
        websearch._PROVIDERS,
        "ddg",
        ((), lambda query, limit: calls.append((query, limit)) or []),
    )

    report = websearch.diagnostics(live=False)

    assert report["live"] is False
    assert report["ok"] is None
    assert report["providers"]["ddg"]["configured"] is True
    assert report["providers"]["ddg"]["status"] == "unverified"
    assert report["providers"]["tavily"]["configured"] is False
    assert report["providers"]["tavily"]["status"] == "unconfigured"
    assert calls == []


def test_diagnostics_live_probes_every_configured_provider(monkeypatch):
    secret = "top-secret-provider-key"
    monkeypatch.setenv("TAVILY_API_KEY", secret)

    monkeypatch.setitem(
        websearch._PROVIDERS,
        "tavily",
        (
            ("TAVILY_API_KEY",),
            lambda query, limit: [
                {
                    "title": "Live result",
                    "url": "https://example.com/live",
                    "snippet": "Provider answered.",
                }
            ],
        ),
    )

    def dead_ddg(query, limit):
        raise RuntimeError("provider offline")

    monkeypatch.setitem(websearch._PROVIDERS, "ddg", ((), dead_ddg))

    report = websearch.diagnostics(live=True)

    assert report["live"] is True
    assert report["ok"] is True
    assert report["providers"]["tavily"]["status"] == "ok"
    assert report["providers"]["tavily"]["result_count"] == 1
    assert report["providers"]["ddg"]["status"] == "down"
    assert secret not in repr(report)


def test_diagnostics_distinguishes_rate_limiting(monkeypatch):
    def limited(query, limit):
        raise websearch.RateLimited("https://provider.example/search")

    monkeypatch.setitem(websearch._PROVIDERS, "ddg", ((), limited))

    report = websearch.diagnostics(live=True)

    assert report["ok"] is False
    assert report["providers"]["ddg"]["status"] == "rate_limited"

def test_retired_google_pse_explicit_config_fails_loudly(monkeypatch):
    monkeypatch.setenv(
        "OLYMPUS_SEARCH_PROVIDERS",
        "google_pse",
    )

    with pytest.raises(
        websearch.SearchConfigurationError,
        match="google_pse",
    ):
        websearch.configured()

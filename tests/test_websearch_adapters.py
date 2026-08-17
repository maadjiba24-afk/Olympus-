"""Deterministic provider-adapter parsing — the PR-path replacement for the
live search gate (W2-CI).

WHY THIS EXISTS. `search-live` was a merge gate that ran a real SearXNG against
real upstream engines from a GitHub runner. That cannot work: SearXNG is a
metasearch PROXY, and the engines it proxies deliberately block datacenter IP
ranges. Run 31975823235 failed with every engine refusing at once — DuckDuckGo
CAPTCHA on every retry, Brave "Too many request (suspended_time=180)", Google
CSE "unusual traffic from your network", Startpage CAPTCHA (suspended 3600s),
Wikidata HTTP 403 — so the proxy had nothing to serve and never became
operational. The runs that passed were the ones that drew an unblocked IP. That
is not flake; it is a gate whose outcome depends on which IP the runner got.

What the PR path actually needs to know is whether OUR PARSING is correct: that
each adapter reads the right container and the right field names out of its
provider's response shape. That is assertable against a recorded response with
NO NETWORK, and it is the half that is a real defect when it breaks. An engine
blocking a runner is not a defect in this repository.

The live probe still has value — it is how you learn a provider changed its
response shape — so it moved to a scheduled workflow rather than being deleted.
Deleting it would trade a bad signal for no signal.

THE FIXTURES ARE DELIBERATELY DISTINCT PER PROVIDER. Every adapter reads a
different container and different field names:

    searxng  results[]         title / url  / content
    brave    web.results[]     title / url  / description
    tavily   results[]         title / url  / content
    serper   organic[]         title / link / snippet
    bing     organic_results[] title / link / snippet

so a copy-paste between adapters, or a provider renaming a field, changes the
parsed output. Each fixture carries values unique to its provider, which is what
makes a mix-up detectable rather than coincidentally equal.
"""

from __future__ import annotations

import pytest

from olympus import websearch

_QUERY = "Olympus agent framework GitHub"
_LIMIT = 3


# Recorded response bodies, one per provider, in that provider's own shape.
# `ddg` is absent on purpose and asserted separately: it delegates to
# `tools.ddg_results` and has no JSON body of its own to parse here.
_RECORDED: dict[str, dict] = {
    "searxng": {
        "payload": {"results": [
            {"title": "searxng-title", "url": "https://searxng.example/a",
             "content": "searxng-snippet"},
            {"title": "searxng-two", "url": "https://searxng.example/b",
             "content": "searxng-snippet-2"},
        ]},
        "expect": {"title": "searxng-title", "url": "https://searxng.example/a",
                   "snippet": "searxng-snippet"},
        "env": {"OLYMPUS_SEARXNG_URL": "http://127.0.0.1:8888"},
    },
    "brave": {
        # NESTED under web.results -- the one adapter that is not top level.
        "payload": {"web": {"results": [
            {"title": "brave-title", "url": "https://brave.example/a",
             "description": "brave-snippet"},
        ]}},
        "expect": {"title": "brave-title", "url": "https://brave.example/a",
                   "snippet": "brave-snippet"},
        "env": {"BRAVE_SEARCH_API_KEY": "k"},
    },
    "tavily": {
        "payload": {"results": [
            {"title": "tavily-title", "url": "https://tavily.example/a",
             "content": "tavily-snippet"},
        ]},
        "expect": {"title": "tavily-title", "url": "https://tavily.example/a",
                   "snippet": "tavily-snippet"},
        "env": {"TAVILY_API_KEY": "k"},
    },
    "serper": {
        # `organic`, and `link` rather than `url`.
        "payload": {"organic": [
            {"title": "serper-title", "link": "https://serper.example/a",
             "snippet": "serper-snippet"},
        ]},
        "expect": {"title": "serper-title", "url": "https://serper.example/a",
                   "snippet": "serper-snippet"},
        "env": {"SERPER_API_KEY": "k"},
    },
    "bing": {
        # SerpApi's Bing engine: `organic_results`, and `link`.
        "payload": {"organic_results": [
            {"title": "bing-title", "link": "https://bing.example/a",
             "snippet": "bing-snippet"},
        ]},
        "expect": {"title": "bing-title", "url": "https://bing.example/a",
                   "snippet": "bing-snippet"},
        "env": {"SERPAPI_API_KEY": "k"},
    },
}


@pytest.fixture()
def recorded(monkeypatch):
    """Serve one recorded body to `_request`, and forbid real network."""
    def make(payload):
        def fake_request(url, payload=None, headers=None):
            return make.body
        make.body = payload
        monkeypatch.setattr(websearch, "_request", fake_request)
    return make


def test_every_provider_has_a_recorded_fixture():
    """Derived from the registry, so a new provider cannot ship unparsed.

    Same guarantee as the authz matrix: the list is not hand-maintained, so
    adding an adapter without a fixture fails here rather than being silently
    uncovered.
    """
    registry = set(websearch._PROVIDERS)
    covered = set(_RECORDED) | {"ddg"}      # ddg is asserted separately
    missing = sorted(registry - covered)
    assert not missing, (
        f"providers with no recorded-response fixture: {missing}. Add one, so "
        f"a parsing regression in the new adapter is caught on the PR path.")
    stale = sorted(covered - registry - {"ddg"})
    assert not stale, f"fixtures for providers that no longer exist: {stale}"


@pytest.mark.parametrize("provider", sorted(_RECORDED))
def test_adapter_parses_its_own_response_shape(provider, recorded, monkeypatch):
    """Each adapter reads its provider's container and field names."""
    spec = _RECORDED[provider]
    for key, value in spec["env"].items():
        monkeypatch.setenv(key, value)
    recorded(spec["payload"])

    _required, fetch = websearch._PROVIDERS[provider]
    results = fetch(_QUERY, _LIMIT)

    assert isinstance(results, list) and results, (
        f"{provider} parsed no results out of a well-formed recorded response "
        f"-- the adapter is reading the wrong container or field names")
    assert results[0] == spec["expect"], (
        f"{provider} normalized its response to {results[0]!r}, expected "
        f"{spec['expect']!r}")
    for item in results:
        assert set(item) == {"title", "url", "snippet"}


@pytest.mark.parametrize("provider", sorted(_RECORDED))
def test_adapter_honours_the_limit(provider, recorded, monkeypatch):
    """A provider returning more than asked must still be truncated."""
    spec = _RECORDED[provider]
    for key, value in spec["env"].items():
        monkeypatch.setenv(key, value)
    row = dict(spec["payload"] and {})       # build an oversized body
    payload = spec["payload"]

    def _inflate(obj):
        if isinstance(obj, dict):
            return {k: _inflate(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return obj * 10
        return obj

    recorded(_inflate(payload))
    _required, fetch = websearch._PROVIDERS[provider]
    results = fetch(_QUERY, 2)
    assert len(results) <= 2, (
        f"{provider} returned {len(results)} results for a limit of 2")
    assert row == {}                          # keep the linter honest


@pytest.mark.parametrize("provider", sorted(_RECORDED))
def test_adapter_survives_an_empty_or_malformed_response(
        provider, recorded, monkeypatch):
    """An empty body is a legitimate 'nothing found', not a crash. A malformed
    one must not raise either -- production falls through to the next provider,
    and an exception here would take the whole chain down."""
    spec = _RECORDED[provider]
    for key, value in spec["env"].items():
        monkeypatch.setenv(key, value)
    _required, fetch = websearch._PROVIDERS[provider]

    for body in ({}, {"results": []}, {"web": {}}, {"organic": None},
                 {"organic_results": []}):
        recorded(body)
        assert fetch(_QUERY, _LIMIT) == []


@pytest.mark.parametrize("provider", sorted(_RECORDED))
def test_adapter_drops_rows_without_a_usable_url(
        provider, recorded, monkeypatch):
    """`_norm` refuses a non-http(s) url; the adapter must drop that row rather
    than emit a result the contract says is impossible."""
    spec = _RECORDED[provider]
    for key, value in spec["env"].items():
        monkeypatch.setenv(key, value)

    def _blank_urls(obj):
        if isinstance(obj, dict):
            return {k: ("javascript:alert(1)" if k in ("url", "link")
                        else _blank_urls(v)) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_blank_urls(v) for v in obj]
        return obj

    recorded(_blank_urls(spec["payload"]))
    _required, fetch = websearch._PROVIDERS[provider]
    assert fetch(_QUERY, _LIMIT) == []


def test_ddg_delegates_rather_than_parsing(monkeypatch):
    """`ddg` has no JSON body of its own -- it delegates to tools.ddg_results,
    so what this pins is the delegation, not a response shape."""
    from olympus import tools
    seen = {}

    def fake(query, limit):
        seen["args"] = (query, limit)
        return [{"title": "t", "url": "https://d.example", "snippet": "s"}]

    monkeypatch.setattr(tools, "ddg_results", fake)
    _required, fetch = websearch._PROVIDERS["ddg"]
    assert fetch(_QUERY, _LIMIT)[0]["url"] == "https://d.example"
    assert seen["args"] == (_QUERY, _LIMIT)


def test_no_adapter_reaches_the_network_in_this_module(recorded):
    """The whole point: this file asserts parsing with zero network. If an
    adapter ever bypassed `_request`, these tests would start depending on the
    very upstream availability that made the live gate unmergeable."""
    import inspect
    for name in _RECORDED:
        src = inspect.getsource(websearch._PROVIDERS[name][1])
        assert "_request(" in src, (
            f"{name} does not go through websearch._request, so it cannot be "
            f"tested without network -- and would reintroduce the dependency "
            f"that took search-live off the merge path")

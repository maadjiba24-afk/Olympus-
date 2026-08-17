"""The pluggable search-provider layer: provider order from env, fallthrough
on failure, cooldown on rate limits, TTL caching, and normalized results.
Network is fully stubbed via websearch._request / the DDG seam.
"""

import urllib.error

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


# --- Step 1F: the Tavily request/response contract, offline ------------------
#
# tests/test_websearch_live.py no longer fails the build when a third-party
# index legitimately returns zero hits, because `websearch.results()` does not
# either — it falls through to the next provider. That relaxation is only safe
# if a BROKEN Tavily adapter still fails loudly somewhere, because a broken
# parser also returns []. This is that somewhere: no network, no credential, no
# timing. `_serper`, `_bing` and `_searxng` already had parse coverage above;
# Tavily — the provider whose live emptiness triggered this work — did not.


def test_tavily_request_and_response_contract(monkeypatch):
    """Exercise the real `_tavily` adapter through the public `results()` path.

    Pins, in one pass: the endpoint, that the key travels in the POST body
    (Tavily's contract) and not the URL, the query, the `max_results` field,
    the field mapping including content -> snippet, rejection of non-http(s)
    URLs, and that slicing to the limit happens on the raw response.
    """
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-secret-key")
    monkeypatch.setenv("OLYMPUS_SEARCH_PROVIDERS", "tavily")
    seen = {}

    def fake_request(url, payload=None, headers=None):
        seen["url"] = url
        seen["payload"] = payload
        seen["headers"] = headers
        return {
            "results": [
                {"title": "First hit", "url": "https://a.example/1",
                 "content": "Body one."},
                # Inside the requested slice, but not an http(s) URL: must be
                # dropped without dropping the results around it.
                {"title": "Rejected scheme", "url": "ftp://b.example/2",
                 "content": "Body two."},
                {"title": "Third hit", "url": "http://c.example/3",
                 "content": "Body three."},
                # Beyond the requested slice: must never appear.
                {"title": "Beyond the limit", "url": "https://d.example/4",
                 "content": "Body four."},
                {"title": "Also beyond", "url": "https://e.example/5",
                 "content": "Body five."},
            ]
        }

    monkeypatch.setattr(websearch, "_request", fake_request)

    out = websearch.results("olympus agent framework", limit=3)

    # --- the request Olympus actually sends ---------------------------------
    assert seen["url"] == "https://api.tavily.com/search"
    assert seen["headers"] is None, (
        "the Tavily adapter sends no custom headers; the key belongs in the "
        "POST body")
    assert seen["payload"] == {
        "api_key": "tavily-secret-key",
        "query": "olympus agent framework",
        "max_results": 3,
    }
    # The credential must never end up in the URL, where `_request` would hand
    # it to the egress choke and it could be logged.
    assert "tavily-secret-key" not in seen["url"]

    # --- the response Olympus produces from it ------------------------------
    assert out == [
        {"title": "First hit", "url": "https://a.example/1",
         "snippet": "Body one."},
        {"title": "Third hit", "url": "http://c.example/3",
         "snippet": "Body three."},
    ]
    for result in out:
        assert set(result) == {"title", "url", "snippet"}


def test_tavily_respects_the_requested_result_limit(monkeypatch):
    """A generous provider must not be able to overrun the caller's limit."""
    monkeypatch.setenv("TAVILY_API_KEY", "t")
    monkeypatch.setenv("OLYMPUS_SEARCH_PROVIDERS", "tavily")

    def fake_request(url, payload=None, headers=None):
        return {"results": [
            {"title": f"Hit {i}", "url": f"https://x.example/{i}",
             "content": f"Body {i}."}
            for i in range(10)
        ]}

    monkeypatch.setattr(websearch, "_request", fake_request)

    out = websearch.results("q", limit=2)
    assert len(out) == 2
    assert [r["url"] for r in out] == ["https://x.example/0",
                                       "https://x.example/1"]


def test_tavily_empty_response_yields_no_results(monkeypatch):
    """The exact live shape that started Step 1F, reproduced offline.

    A well-formed Tavily reply carrying no hits must produce `[]` — not an
    exception, not a fabricated row. `results()` then falls through, which the
    next test proves.
    """
    monkeypatch.setenv("TAVILY_API_KEY", "t")
    monkeypatch.setattr(websearch, "_request",
                        lambda url, payload=None, headers=None: {"results": []})
    assert websearch._tavily("q", 3) == []


# --- Step 1F: empty is a fall-through, not a failure -------------------------


def test_empty_provider_falls_through_to_the_next_in_order(monkeypatch):
    """The production semantics the live gate was contradicting.

    A provider that completes successfully with zero results must be treated
    exactly like one that errored: move on. This pins that the first provider
    really is invoked, really does return empty, the second really is invoked
    after it, its result is what comes back, and the configured order is
    honoured.
    """
    monkeypatch.setenv("TAVILY_API_KEY", "t")
    monkeypatch.setenv("OLYMPUS_SEARXNG_URL", "http://127.0.0.1:8888")
    monkeypatch.setenv("OLYMPUS_SEARCH_PROVIDERS", "tavily, searxng")

    calls = []
    anchor_row = {"title": "Anchor", "url": "https://anchor.example/x",
                  "snippet": "from the second provider"}

    def empty_first(query, limit):
        out = []
        calls.append(("tavily", query, limit, list(out)))
        return out

    def working_second(query, limit):
        out = [dict(anchor_row)]
        calls.append(("searxng", query, limit, list(out)))
        return out

    monkeypatch.setitem(websearch._PROVIDERS, "tavily",
                        (("TAVILY_API_KEY",), empty_first))
    monkeypatch.setitem(websearch._PROVIDERS, "searxng",
                        (("OLYMPUS_SEARXNG_URL",), working_second))

    assert websearch.configured() == ["tavily", "searxng"]

    out = websearch.results("fallback probe", limit=5)

    # both invoked, in the configured order
    assert [c[0] for c in calls] == ["tavily", "searxng"], calls
    # the first really did return empty
    assert calls[0][3] == []
    # both received the caller's query and limit unchanged
    assert calls[0][1] == calls[1][1] == "fallback probe"
    assert calls[0][2] == calls[1][2] == 5
    # the second provider's result is what the caller gets — nothing fabricated
    assert out == [anchor_row]
    assert calls[1][3] == [anchor_row]


def test_every_provider_empty_returns_empty_without_fabricating(monkeypatch):
    """Exhausting the chain yields `[]`, never an invented row.

    The counterpart to the fall-through above: 'empty is tolerated' must not
    quietly become 'empty is filled in'.
    """
    monkeypatch.setenv("TAVILY_API_KEY", "t")
    monkeypatch.setenv("OLYMPUS_SEARCH_PROVIDERS", "tavily, ddg")
    calls = []

    def empty(name):
        def fetch(query, limit):
            calls.append(name)
            return []
        return fetch

    monkeypatch.setitem(websearch._PROVIDERS, "tavily",
                        (("TAVILY_API_KEY",), empty("tavily")))
    monkeypatch.setitem(websearch._PROVIDERS, "ddg", ((), empty("ddg")))

    assert websearch.results("nothing anywhere") == []
    assert calls == ["tavily", "ddg"], calls
    # an empty answer is never cached as if it were a hit
    assert websearch._cache == {}


# --- Step 1F: the live gate's own contract, proven offline -------------------
#
# tests/test_websearch_live.py is skipped unless OLYMPUS_SEARCH_LIVE is set, so
# its validator and its exception policy would otherwise ship unexercised. If
# the strict checks it applies to NON-EMPTY results were broken, the relaxation
# of the empty case would leave the gate with no teeth at all. These load that
# module by path — under a distinct name, so pytest's own import of it is
# untouched — and prove the two decisions it makes.


def _live_module():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent / "test_websearch_live.py"
    spec = importlib.util.spec_from_file_location(
        "olympus_live_search_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_live_validator_accepts_a_well_formed_result_set():
    live = _live_module()
    live.assert_live_result_contract("probe", [
        {"title": "T", "url": "https://a.example", "snippet": "s"},
        {"title": "U", "url": "http://b.example", "snippet": ""},
    ], 3)


def test_live_validator_accepts_a_genuinely_empty_list():
    """`[]` is the ONE empty value that is structurally valid.

    It means the provider completed and found nothing, which production treats
    as a fall-through. Every other falsey value is rejected by the test below.
    """
    live = _live_module()
    live.assert_live_result_contract("probe", [], 3)


@pytest.mark.parametrize("bad, why", [
    (None, "None is not a list"),
    ({}, "empty dict is falsey but not a list"),
    ("", "empty string is falsey but not a list"),
    ((), "empty tuple is falsey but not a list"),
    (0, "zero is falsey but not a list"),
    (set(), "empty set is falsey but not a list"),
    ({"results": []}, "a raw provider envelope, not a normalized list"),
    ("not a list", "a non-empty string"),
    ([{"title": "T", "url": "https://a.example"}], "missing snippet"),
    ([{"title": "T", "url": "https://a.example", "snippet": "s",
       "extra": 1}], "extra key"),
    ([{"title": "", "url": "https://a.example", "snippet": "s"}],
     "empty title"),
    ([{"title": "T", "url": "ftp://a.example", "snippet": "s"}],
     "non-http(s) url"),
    ([{"title": "T", "url": "https://a.example", "snippet": None}],
     "non-string snippet"),
    ([{"title": "T", "url": "https://a.example", "snippet": "s"}] * 4,
     "more results than the requested limit"),
])
def test_live_validator_rejects_malformed_or_falsey_results(bad, why):
    """Structure is never tolerated, empty or not.

    Emptiness is tolerated for third-party indexes ONLY in the exact shape
    `[]`. A falsey non-list — the shape a broken adapter returns on error — is
    a hard failure, not a quiet 'the index had nothing today'.
    """
    live = _live_module()
    with pytest.raises(AssertionError):
        live.assert_live_result_contract("probe", bad, 3)


# --- the live probe's CONTROL FLOW, not just its validator -------------------
#
# Proving the validator rejects `None` is not enough: the caller could still
# check `if not results` first and never invoke it, which is exactly the bug
# this section exists to prevent regressing. These call the real live probe
# function body with an injected provider and assert on what it actually does.


@pytest.mark.parametrize("junk", [None, {}, "", (), 0, set(), {"results": []},
                                  [{"title": "T", "url": "ftp://x",
                                    "snippet": "s"}]])
def test_live_probe_hard_fails_on_falsey_or_malformed_provider_output(
        monkeypatch, junk):
    """A falsey non-list from a provider must fail, never report `empty`.

    `ddg` needs no credential, so the probe runs its full body: fetch, then
    validate, then classify. If the order ever regresses to classify-then-
    validate, `None`/`{}`/`""` reach the `empty` branch and this passes
    silently — so these cases are the regression, not the validator unit test.
    """
    live = _live_module()
    monkeypatch.setitem(websearch._PROVIDERS, "ddg", ((), lambda q, n: junk))

    with pytest.raises(AssertionError):
        live.test_live_search_provider("ddg")


def test_live_probe_accepts_an_empty_list_from_a_non_anchor_provider(
        monkeypatch, capsys):
    """A third-party index that genuinely found nothing is a health state."""
    live = _live_module()
    monkeypatch.setitem(websearch._PROVIDERS, "ddg", ((), lambda q, n: []))

    live.test_live_search_provider("ddg")          # must not raise

    assert ("SEARCH_LIVE provider=ddg status=empty count=0"
            in capsys.readouterr().out)


def test_live_probe_hard_fails_when_the_anchor_returns_an_empty_list(
        monkeypatch):
    """The anchor's emptiness stays a hard failure, by policy.

    Same input as the test above — a valid, structurally correct `[]` — and the
    opposite verdict, because the anchor is the provider the gate requires a
    real end-to-end result from.
    """
    live = _live_module()
    monkeypatch.setenv("OLYMPUS_SEARXNG_URL", "http://127.0.0.1:8888")
    monkeypatch.setitem(websearch._PROVIDERS, live._ANCHOR,
                        (("OLYMPUS_SEARXNG_URL",), lambda q, n: []))

    with pytest.raises(AssertionError, match="anchor"):
        live.test_live_search_provider(live._ANCHOR)


def test_live_probe_passes_a_well_formed_nonempty_result(monkeypatch, capsys):
    """The happy path still passes, so the tests above are not vacuous."""
    live = _live_module()
    monkeypatch.setitem(websearch._PROVIDERS, "ddg", ((), lambda q, n: [
        {"title": "T", "url": "https://a.example", "snippet": "s"}]))

    live.test_live_search_provider("ddg")          # must not raise

    assert ("SEARCH_LIVE provider=ddg status=ok count=1"
            in capsys.readouterr().out)


@pytest.mark.parametrize("code, rejected", [
    (401, True), (402, True), (403, True),
    (400, False), (404, False), (429, False), (418, False),
    (500, False), (502, False), (503, False),
])
def test_live_gate_only_skips_on_credential_rejection(code, rejected):
    """The skip boundary must stay narrow.

    Only 'rotate the key' statuses become skips. Everything else — a bad
    request, a missing endpoint, any 5xx — is an integration or protocol
    problem and must reach the runner as a failure rather than a quiet pass.
    Widening this set is the cheapest way to turn the live gate into a no-op.
    """
    live = _live_module()
    err = urllib.error.HTTPError(
        "https://provider.example/search", code, "reason", {}, None)
    assert live.is_credential_rejection(err) is rejected
    assert live._CREDENTIAL_REJECTED == {401, 402, 403}


@pytest.mark.parametrize("code, exhausted", [
    # Tavily: 432 Plan Limit Exceeded, 433 PayGo Limit Exceeded.
    (432, True), (433, True),
    # NOT quota: these are the credential set, and they mean rotate the key.
    (401, False), (402, False), (403, False),
    # NOT quota: 429 is "slow down" and never reaches here as an HTTPError —
    # `_request` converts it to RateLimited — but pin it anyway so a future
    # change to that conversion cannot silently reclassify it.
    (429, False),
    # NOT quota: integration and protocol problems, which must still FAIL.
    (400, False), (404, False), (431, False), (434, False),
    (500, False), (502, False), (503, False),
])
def test_live_gate_quota_boundary_is_narrow(code, exhausted):
    """Quota exhaustion is a THIRD category, and it must stay a narrow one.

    432/433 mean the account's allowance is spent: the key is valid and the
    integration works, so the skip has to say "upgrade the plan or wait for the
    billing cycle", not "rotate the key". Rotating a key against an exhausted
    plan changes nothing, and a skip that gives an instruction which cannot work
    is worse than the failure it replaced.

    The neighbours 431 and 434 are pinned as failures on purpose: this is an
    allowlist of two documented codes, not a 43x range.
    """
    live = _live_module()
    err = urllib.error.HTTPError(
        "https://provider.example/search", code, "reason", {}, None)
    assert live.is_quota_exhausted(err) is exhausted
    assert live._QUOTA_EXHAUSTED == {432, 433}


def test_quota_and_credential_categories_are_disjoint():
    """The two skip categories must never overlap.

    If a code were in both, the branch order would silently decide which advice
    the operator got — and the wrong one is unactionable.
    """
    live = _live_module()
    assert not (live._QUOTA_EXHAUSTED & live._CREDENTIAL_REJECTED)
    assert 429 not in live._QUOTA_EXHAUSTED, "429 is RateLimited, not quota"


def test_live_status_lines_have_the_exact_required_shape():
    """The CI health line must never carry a secret, a URL or a query."""
    live = _live_module()

    assert (live.emit_status("tavily", "empty", 0)
            == "SEARCH_LIVE provider=tavily status=empty count=0")
    assert (live.emit_status("searxng", "ok", 3)
            == "SEARCH_LIVE provider=searxng status=ok count=3")
    assert (live.emit_status("results-chain", "ok", 2)
            == "SEARCH_LIVE provider=results-chain status=ok count=2")
    # count is optional — the statuses that have nothing to count omit it
    assert (live.emit_status("brave", "unconfigured")
            == "SEARCH_LIVE provider=brave status=unconfigured")

    for forbidden in ("http://", "https://", "api_key", "TAVILY_API_KEY"):
        assert forbidden not in live.emit_status("tavily", "down")


def test_live_status_emitter_cannot_become_a_generic_formatter():
    """The sanitization guarantee must be enforced, not merely documented.

    An earlier revision accepted `**fields` and interpolated whatever it was
    given, so one `emit_status(p, "down", url=endpoint)` would have printed a
    credential — `_bing` builds its endpoint with `api_key=` in the query
    string. There is now no free-form channel at all, and each of the three
    parameters is constrained.
    """
    live = _live_module()

    # No free-form field exists.
    with pytest.raises(TypeError):
        live.emit_status("tavily", "down", url="https://x.example?api_key=SEC")
    with pytest.raises(TypeError):
        live.emit_status("tavily", "down", 0, "extra")

    # The status word is a closed vocabulary, so a message cannot ride in it.
    assert live._STATUSES == {"ok", "empty", "rate_limited", "quota_exhausted",
                              "upstream_blocked", "down", "unconfigured"}
    for bad_status in ("weird", "", "down https://x.example?api_key=SEC"):
        with pytest.raises(ValueError):
            live.emit_status("tavily", bad_status)

    # The count is a real non-negative int, not a string carrying anything.
    for bad_count in ("0", 1.5, True, -1, b"0"):
        with pytest.raises((TypeError, ValueError)):
            live.emit_status("tavily", "ok", bad_count)


def test_live_status_subjects_are_a_closed_allowlist_not_a_pattern():
    """The subject must be allowlisted, not merely character-filtered.

    An earlier revision validated the subject with
    `\\A[a-z0-9][a-z0-9_-]{0,31}\\Z` and claimed a secret could not match it.
    That was false. A lowercase hex token such as `a8f31c92e57b44d1`, or an
    `sk_live_...` style key, satisfies that pattern exactly and would have been
    printed verbatim into the CI log. No character class can separate a
    credential from a provider name, because a credential is free to look like
    one — so the policy is now the set of names that actually exist.

    The token cases below are the load-bearing ones: they are syntactically
    indistinguishable from a legal identifier and fail only because they are
    not in the registry.
    """
    live = _live_module()

    assert live._SUBJECTS == frozenset(websearch._PROVIDERS) | {"results-chain"}
    assert live._SUBJECTS == {"searxng", "brave", "tavily", "serper", "bing",
                              "ddg", "results-chain"}

    # every real provider, plus the one synthetic subject, is emittable
    for subject in sorted(live._SUBJECTS):
        assert live.emit_status(subject, "ok", 1) == (
            f"SEARCH_LIVE provider={subject} status=ok count=1")

    for bad_subject in (
            # obviously unsafe
            "https://x.example",
            "TAVILY_API_KEY=abc",
            "has space",
            "Upper",
            "",
            7,
            None,
            # THE POINT: these all satisfy the old regex and are rejected only
            # because they are not real provider names.
            "a8f31c92e57b44d1",
            "sk_live_123456789",
            "some-random-lowercase-token",
    ):
        with pytest.raises(ValueError):
            live.emit_status(bad_subject, "down")


def test_rejected_status_values_never_reach_stdout_or_the_exception(capsys):
    """Refusing to PRINT a value is worthless if the exception echoes it.

    `emit_status` guards the log line, but pytest renders exception text into
    the same CI log — so an earlier revision that raised
    `f"unsafe SEARCH_LIVE subject: {subject!r}"` leaked a mistakenly-passed
    credential through the failure path instead of the success path. Both
    exits are checked here, with credential-shaped inputs that are otherwise
    syntactically unremarkable.
    """
    live = _live_module()
    secret_subject = "a8f31c92e57b44d1"
    secret_status = "secretcredential123456"

    with pytest.raises(ValueError) as exc:
        live.emit_status(secret_subject, "ok", 1)
    assert secret_subject not in str(exc.value)
    assert secret_subject not in repr(exc.value)

    with pytest.raises(ValueError) as exc:
        live.emit_status("tavily", secret_status, 1)
    assert secret_status not in str(exc.value)
    assert secret_status not in repr(exc.value)

    # ...and neither reached the log on the way to being rejected.
    captured = capsys.readouterr()
    assert secret_subject not in captured.out
    assert secret_subject not in captured.err
    assert secret_status not in captured.out
    assert secret_status not in captured.err

    # The messages stay useful: they name the allowlist, which is registry
    # data rather than caller input, so an operator can still fix the call.
    with pytest.raises(ValueError, match="expected one of"):
        live.emit_status(secret_subject, "ok")
    with pytest.raises(ValueError, match="expected one of"):
        live.emit_status("tavily", secret_status)


def test_rejected_count_values_never_reach_the_exception(capsys):
    """The count branches report a type NAME or a constant, never a value."""
    live = _live_module()
    secret_count = "a8f31c92e57b44d1"

    with pytest.raises(TypeError) as type_exc:
        live.emit_status("tavily", "ok", secret_count)
    assert secret_count not in str(type_exc.value)
    assert "str" in str(type_exc.value)          # the type name is safe context

    with pytest.raises(ValueError) as value_exc:
        live.emit_status("tavily", "ok", -12345)
    assert "-12345" not in str(value_exc.value)

    captured = capsys.readouterr()
    assert secret_count not in captured.out
    assert secret_count not in captured.err


def test_live_anchor_is_the_repository_pinned_searxng():
    """The anchor must remain the provider this repo pins, starts and configures.

    Pointing it at a third-party index would silently remove the one hard
    guarantee that stops an all-empty run from going green. Note the anchor is
    pinned, not deterministic — SearXNG proxies live upstream engines — so its
    hard-failure status is a policy choice, not a claim that emptiness proves
    local breakage.
    """
    live = _live_module()
    assert live._ANCHOR == "searxng"
    assert live._ANCHOR_ENV == "OLYMPUS_SEARXNG_URL"
    assert live._ANCHOR in websearch._PROVIDERS

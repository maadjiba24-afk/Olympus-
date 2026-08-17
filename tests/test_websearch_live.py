"""Live end-to-end tests for every Olympus search provider.

These tests contact real external services. They run only when explicitly
enabled and skip providers whose required environment variables are absent,
or whose credential the provider itself rejects — an expired key is an
operator problem, not a defect in the code under test, and must not be able
to hold the merge queue shut. The skip reason names the credential to rotate.

WHAT AN EMPTY RESULT SET MEANS (Step 1F correction). This file used to assert
`results` for every provider individually, which made the live gate STRONGER
than the production contract it is supposed to guard. `websearch.results()`
treats a provider that errors, rate-limits, OR returns `[]` identically: it
falls through to the next provider in the configured order. A third-party
index legitimately returning zero hits for one query on one day is therefore
not an Olympus integration failure, and failing the build on it made a green
run depend on an external service's recall. That is exactly what happened:
push run 31219271668 failed only `test_live_search_provider[tavily]` with
`assert []`, on a commit whose PR run passed everything, with a valid key, no
HTTP error, and no rate limit.

The correction is NOT to delete the assertion — a permanently broken Tavily
parser also returns `[]`, and deleting it would make that invisible. The
guarantee is split into the two things it was conflating:

  * PARSER/PROTOCOL correctness is pinned deterministically, offline, in
    tests/test_websearch.py (`test_tavily_*`) — a broken adapter fails there
    with no network and no credential;
  * LIVE health of a third-party index is reported here as a status word, and
    only the things Olympus actually promises are asserted.

What still HARD FAILS here, so the gate keeps its teeth:

  * the pinned local SearXNG anchor returning nothing. Olympus pins, starts and
    configures that container, which makes it the repository-pinned hard
    integration anchor — but its retrieval still goes out to live upstream
    engines, so it is NOT deterministic. An empty anchor response is a hard
    failure by POLICY (the job requires at least one real end-to-end result
    before it may go green), not because emptiness proves the container, its
    committed settings or the adapter is broken; upstream search or network
    state can produce it just as well;
  * the live gate running at all without that anchor configured;
  * any structurally invalid result collection — including a falsey one such as
    None, {} or "", which must never be mistaken for an empty list;
  * any NON-EMPTY result set that is malformed;
  * `websearch.results()` — the real user-facing fallback chain — failing to
    deliver a valid result;
  * any unexpected protocol/integration exception, which propagates unchanged.

Run with:

    $env:OLYMPUS_SEARCH_LIVE="1"
    python -m pytest -q -s tests/test_websearch_live.py
"""

from __future__ import annotations

import os
import urllib.error

import pytest

from olympus import websearch

# Provider responses that mean "your credential is bad or spent", not "the
# integration is broken". `_request` turns 429 into RateLimited before it ever
# surfaces as an HTTPError, so both shapes have to be caught.
_CREDENTIAL_REJECTED = {401, 402, 403}

# Provider responses that mean "this account's allowance is used up". A THIRD
# category, deliberately kept out of `_CREDENTIAL_REJECTED` above.
#
# Tavily documents 432 as Plan Limit Exceeded ("exceeds your plan's set usage
# limit") and 433 as PayGo Limit Exceeded. The key is valid, the integration is
# fine, and the plan is spent — so the remedy is to upgrade the plan or wait for
# the billing cycle to roll over. Folding these into `_CREDENTIAL_REJECTED`
# would produce a skip telling the operator to ROTATE THE KEY, which is a
# plausible-sounding instruction that cannot possibly work: a fresh key on an
# exhausted plan is still an exhausted plan. A skip that reports the wrong
# reason is worse than the failure it replaces, because it looks handled.
#
# Distinct from `RateLimited` (429) too: 429 means "you are going too fast, try
# again shortly" and the provider is put on cooldown. This means "you have no
# allowance left", and waiting minutes will not help.
_QUOTA_EXHAUSTED = {432, 433}

# The repository-pinned hard integration anchor: a SearXNG container this repo
# pins by digest, starts, and configures from committed settings. That control
# is why its emptiness is treated as a build failure while a third-party
# index's is not — it is the one provider the job is entitled to demand a real
# end-to-end result from before going green.
#
# It is NOT deterministic, and nothing here should claim otherwise. SearXNG is
# a metasearch proxy: its retrieval goes out to live upstream engines, so an
# empty response can come from upstream search or network state just as much as
# from a broken container, broken settings or a broken adapter. The hard
# failure is a POLICY choice — at least one real result must be observed — not
# a diagnosis of local breakage.
_ANCHOR = "searxng"
_ANCHOR_ENV = "OLYMPUS_SEARXNG_URL"

# The complete SEARCH_LIVE status vocabulary. Mostly shared with
# websearch.diagnostics(), which additionally has "unverified" (a state only the
# non-live report can be in) and does not distinguish quota exhaustion.
#
# `upstream_blocked` is the environment condition that took this job off the
# merge path (W2-CI): a metasearch proxy whose upstream engines refused the
# runner -- CAPTCHA, rate-limit, 403. It is NOT `down` (nothing here is
# broken) and NOT `rate_limited` (that is one provider throttling us, and it
# recovers on its own). It means the probe could not observe anything from
# this network, so the operator should read the result as 'no data', not as
# 'Olympus is failing'. Emitted by the scheduled workflow itself.
#
# `quota_exhausted` is deliberately its OWN word rather than reuse of
# `rate_limited` or `down`. The entire purpose of this line is that an operator
# reading the CI log can tell WHY a provider is not being covered, and those
# three states call for three different actions: `rate_limited` means back off
# and it will recover on its own, `down` means investigate the provider or the
# integration, `quota_exhausted` means someone has to pay or wait for the
# billing cycle. Reusing a word here would make the log actively misleading at
# exactly the moment it is the only thing standing between an uncovered
# provider and a green build.
_STATUSES = frozenset({"ok", "empty", "rate_limited", "quota_exhausted",
                       "upstream_blocked", "down", "unconfigured"})

# The complete set of subjects that may ever appear in a SEARCH_LIVE line: the
# real provider registry, plus the one synthetic subject for the end-to-end
# chain. A CLOSED ALLOWLIST, deliberately, not a character pattern.
#
# An earlier revision matched subjects against `\A[a-z0-9][a-z0-9_-]{0,31}\Z`
# and claimed a secret could not match it. That was false: `a8f31c92e57b44d1`
# and `sk_live_123456789` satisfy it perfectly, so a caller passing a token
# would have printed it. Character filtering cannot distinguish a credential
# from a provider name — only an allowlist of the names that actually exist
# can. A provider legitimately added to `websearch._PROVIDERS` is admitted
# automatically; nothing else ever is.
_SUBJECTS = frozenset(websearch._PROVIDERS) | {"results-chain"}

_QUERY = "Olympus agent framework GitHub"
_LIMIT = 3

_LIVE = os.environ.get("OLYMPUS_SEARCH_LIVE", "").strip().lower() in {
    "1",
    "true",
    "yes",
}

pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason="set OLYMPUS_SEARCH_LIVE=1 to run real search-provider tests",
)


def emit_status(subject: str, status: str, count: int | None = None) -> str:
    """Print one sanitized health line per live probe, and return it.

    The emitted line is exactly:

        SEARCH_LIVE provider=<subject> status=<status>[ count=<n>]

    SANITIZATION IS ENFORCED, NOT DOCUMENTED. An earlier revision took
    `**fields` and interpolated whatever a caller passed, so the promise that a
    credential, URL or payload can never be printed rested entirely on every
    present and future caller remembering it. One `emit_status(p, "down",
    url=endpoint)` would have leaked a key — `_bing` builds an endpoint with
    `api_key=` in the query string. The interface is therefore the smallest one
    the gate actually needs, and each of the three fields is constrained:

      * `subject` must be a member of `_SUBJECTS` — the actual provider
        registry plus the literal "results-chain". A closed allowlist, because
        a character pattern cannot tell a credential from a provider name: a
        lowercase token like `a8f31c92e57b44d1` looks exactly like a legal
        identifier and would have passed one;
      * `status` must be one of `_STATUSES`, the same vocabulary
        `websearch.diagnostics()` reports;
      * `count` must be a non-negative int, or omitted. Not a string, not a
        bool, not a float.

    A rejected value is never echoed back, on either path: the raised message
    names the allowlist but withholds what was passed. Guarding only the print
    would be theatre, since pytest renders exception text into the same CI log.

    There is no free-form channel, so this cannot casually become a generic
    formatter. Context that is genuinely useful but unsafe or verbose — HTTP
    status codes, missing env-var names, the provider order — stays in the
    pytest skip/failure message, which is only shown for skipped or failing
    tests and is not a machine-parsed log line.

    Printed rather than logged so it lands in the CI job log next to the test
    that produced it. The search-live job runs pytest with `-s` for exactly
    this reason: without it pytest captures stdout and only replays it for
    FAILING tests, so a passing-but-empty provider — the state this whole
    correction exists to make visible — would print nothing at all.
    """
    # THE REJECTED VALUE IS NEVER ECHOED. Refusing to PRINT a subject while
    # interpolating it into the exception leaks it just the same: pytest
    # renders the message into the CI log, so `{subject!r}` would have put a
    # mistakenly-passed credential straight where the print guard was meant to
    # keep it out. Only constants and the allowlists themselves — which are
    # provider names from the registry, not caller input — appear below.
    if not isinstance(subject, str) or subject not in _SUBJECTS:
        raise ValueError(
            f"unsafe SEARCH_LIVE subject (value withheld); expected one of "
            f"{sorted(_SUBJECTS)}")
    if status not in _STATUSES:
        raise ValueError(
            f"unknown SEARCH_LIVE status (value withheld); expected one of "
            f"{sorted(_STATUSES)}")
    line = f"SEARCH_LIVE provider={subject} status={status}"
    if count is not None:
        # A type NAME cannot carry a secret; a value can, so neither branch
        # reports one.
        if isinstance(count, bool) or not isinstance(count, int):
            raise TypeError(
                f"SEARCH_LIVE count must be an int, got {type(count).__name__}")
        if count < 0:
            raise ValueError("SEARCH_LIVE count must not be negative")
        line += f" count={count}"
    print(line, flush=True)
    return line


def is_credential_rejection(err: urllib.error.HTTPError) -> bool:
    """True only for the statuses that mean 'rotate the key'.

    Deliberately a narrow allowlist rather than a range. Everything else —
    400, 404, 5xx, a malformed response, a JSON decode failure, a DNS error —
    is an integration or protocol problem and must reach the test runner as a
    failure. Widening this set is the easiest way to accidentally turn the
    live gate into a no-op, so it is pinned by a deterministic test in
    tests/test_websearch.py.
    """
    return getattr(err, "code", None) in _CREDENTIAL_REJECTED


def is_quota_exhausted(err: urllib.error.HTTPError) -> bool:
    """True only for the statuses that mean 'the allowance is spent'.

    Narrow for the same reason `is_credential_rejection` is narrow, and kept
    separate from it because the two call for different actions. This one must
    never grow to cover a generic 4xx: 400 is a malformed request and 404 is a
    missing endpoint, and both are integration problems that have to fail.
    Pinned by a deterministic test in tests/test_websearch.py alongside the
    credential set.
    """
    return getattr(err, "code", None) in _QUOTA_EXHAUSTED


def assert_live_result_contract(provider: str, results, limit: int) -> None:
    """Strict structural validation of a live result collection, empty or not.

    This runs BEFORE any emptiness decision, and that ordering is the point.
    An earlier revision tested `if not results` first, which meant `None`,
    `{}`, `""`, `()` and `0` — all falsey — were reported as the tolerated
    health state `empty` and passed without ever reaching this function. A
    provider adapter that had started returning `None` on error, or a dict
    instead of a list, would have looked exactly like a quiet day at a search
    index. Only a genuine `[]` may be called empty.

    So: the collection must be a `list` (an empty one is valid and means the
    provider genuinely found nothing), no longer than the requested limit, and
    every element present must satisfy the normalized shape `_norm` promises —
    exactly {title, url, snippet}, a non-empty string title, an http(s) URL, a
    string snippet. A malformed collection is always a hard failure, on every
    provider including the ones whose emptiness is tolerated.
    """
    assert isinstance(results, list), (
        f"{provider} returned {type(results).__name__}, not a list")
    assert len(results) <= limit, (
        f"{provider} returned {len(results)} results for a limit of {limit}")
    for index, result in enumerate(results):
        where = f"{provider} result {index}"
        assert isinstance(result, dict), f"{where} is not a dict: {type(result)}"
        assert set(result) == {"title", "url", "snippet"}, (
            f"{where} has keys {sorted(result)}, expected "
            f"['snippet', 'title', 'url']")
        assert isinstance(result["title"], str) and result["title"], (
            f"{where} has an empty or non-string title")
        assert isinstance(result["url"], str) and result["url"].startswith(
            ("http://", "https://")), f"{where} has a non-http(s) url"
        assert isinstance(result["snippet"], str), (
            f"{where} has a non-string snippet: {type(result['snippet'])}")


def test_live_gate_requires_the_pinned_searxng_anchor():
    """Running the live gate without its anchor must not be able to go green.

    Every third-party provider in this file is allowed to be empty, so without
    the anchor a run in which literally every external index returned nothing
    would report success and prove nothing. The anchor is the one provider the
    job requires a real end-to-end result from, so its presence is mandatory
    whenever the gate is enabled.
    """
    anchor_url = os.environ.get(_ANCHOR_ENV, "").strip()
    assert anchor_url, (
        f"OLYMPUS_SEARCH_LIVE is enabled but {_ANCHOR_ENV} is unset, so the "
        f"pinned {_ANCHOR} anchor cannot run. Without it every remaining "
        f"provider is permitted to report 'empty' and the job would go green "
        f"having verified nothing.")
    assert _ANCHOR in websearch.configured(), (
        f"{_ANCHOR} is not in the configured provider order "
        f"{websearch.configured()}; the live anchor must actually be reachable "
        f"through the production configuration path")


@pytest.mark.parametrize("provider", tuple(websearch._PROVIDERS))
def test_live_search_provider(provider):
    """Probe one provider directly, with no fallback and no cache.

    Bypassing `results()` is intentional: this is per-provider health, and a
    working provider must not be able to mask a broken neighbour. The
    user-facing chain is asserted separately, below.
    """
    required_env, fetch = websearch._PROVIDERS[provider]
    missing = [name for name in required_env if not os.environ.get(name)]

    if missing:
        # The missing variable NAMES go in the skip reason, not the status
        # line: the emitter takes no free-form field by design.
        emit_status(provider, "unconfigured")
        pytest.skip(
            f"{provider} requires: {', '.join(missing)}"
        )

    # Only the three documented credential/rate/quota shapes are converted to
    # skips. Every other exception — HTTP 4xx/5xx, JSON decode, DNS, timeout, a
    # provider changing its response schema — propagates and fails the gate.
    #
    # EVERY branch emits its status line BEFORE it skips. Once a provider
    # skips, the live gate silently stops covering it, and a check that is not
    # running cannot fail — so the SEARCH_LIVE line is the only thing that makes
    # the loss of coverage visible in the CI log. A skip that emitted nothing,
    # or emitted after `pytest.skip` raised, would be indistinguishable from a
    # pass. (The job runs pytest with `-s` so these lines survive; see
    # `emit_status`.)
    try:
        results = fetch(_QUERY, _LIMIT)
    except websearch.RateLimited:
        emit_status(provider, "rate_limited")
        pytest.skip(f"{provider} rate-limited the live probe (HTTP 429)")
    except urllib.error.HTTPError as err:
        # Quota BEFORE credential: both are 4xx and both skip, but they demand
        # different actions, and exactly one status line may be emitted per
        # probe. Checking quota first is what keeps the reported reason true.
        if is_quota_exhausted(err):
            emit_status(provider, "quota_exhausted")
            # ASCII only: this reason is rendered into the CI log, whose
            # encoding is not ours to choose. An em-dash here came back as
            # mojibake the first time it was captured.
            pytest.skip(
                f"{provider} has exhausted its plan allowance "
                f"(HTTP {err.code} {err.reason}). The credential is VALID and "
                f"the integration is fine, so rotating "
                f"{', '.join(required_env)} will NOT help. Upgrade the plan, "
                f"or wait for the billing cycle to reset, to re-enable this "
                f"probe."
            )
        emit_status(provider, "down")
        if not is_credential_rejection(err):
            raise
        pytest.skip(
            f"{provider} rejected the configured credential "
            f"(HTTP {err.code} {err.reason}); rotate "
            f"{', '.join(required_env)} to re-enable this probe"
        )

    # STRUCTURE FIRST, EMPTINESS SECOND. Deciding `if not results` before
    # validating would let None / {} / "" / () / 0 be reported as the tolerated
    # `empty` health state and pass. Only a real list reaches the branch below,
    # and only a real `[]` can be called empty.
    assert_live_result_contract(provider, results, _LIMIT)

    if not results:
        emit_status(provider, "empty", 0)
        # Policy, not diagnosis: this repo pins, starts and configures the
        # anchor container, so the job is entitled to require a real
        # end-to-end result from it before going green. That is why an empty
        # anchor fails while an empty third-party index does not — NOT because
        # emptiness here proves local breakage. SearXNG proxies live upstream
        # engines, so upstream search or network state can produce this too.
        assert provider != _ANCHOR, (
            f"the pinned local {_ANCHOR} anchor returned no results for a "
            f"fixed query. The live gate requires at least one real "
            f"end-to-end result before it may pass, and this is the provider "
            f"that guarantee rests on, so this is a hard failure by policy. "
            f"It does not by itself prove the container, its committed "
            f"settings or the adapter is broken — {_ANCHOR} proxies live "
            f"upstream engines, so upstream search or network state can cause "
            f"it too. Check the container logs and the upstream engines "
            f"before assuming a code defect.")
        return

    emit_status(provider, "ok", len(results))


def test_live_production_fallback_chain_delivers_results(monkeypatch):
    """The actual user-facing guarantee: `websearch.results()` delivers.

    This is the promise Olympus makes — not "every provider answers", but "the
    configured chain answers". Tavily is placed FIRST when it is configured, so
    the run exercises one of the two outcomes that matter, and both are a pass
    only if production behaves correctly:

      * Tavily returns results, and the chain returns them; or
      * Tavily returns empty / errors / rate-limits, and the real production
        fall-through in `results()` must reach the SearXNG anchor and deliver.

    Either way the assertion below is the same and is never relaxed. The
    anchor is last, so if the chain produces nothing at all, this fails.

    `_cache` and `_cooling` are module-level and process-wide: a cached entry
    from an earlier test could satisfy this without any provider being called,
    and a cooldown left by the rate-limit probe above could silently remove
    Tavily from the chain. Both are reset here so the result is genuinely
    produced by this call, and `monkeypatch` restores them afterwards.
    """
    anchor_url = os.environ.get(_ANCHOR_ENV, "").strip()
    assert anchor_url, (
        f"{_ANCHOR_ENV} must be configured for the live fallback test — the "
        f"chain needs its hard anchor at the end")

    order = []
    if os.environ.get("TAVILY_API_KEY"):
        order.append("tavily")
    order.append(_ANCHOR)

    monkeypatch.setenv("OLYMPUS_SEARCH_PROVIDERS", ",".join(order))
    monkeypatch.setattr(websearch, "_cache", {})
    monkeypatch.setattr(websearch, "_cooling", {})
    assert websearch.configured() == order, (
        f"the chain under test is {websearch.configured()}, not {order}")

    out = websearch.results(_QUERY, limit=_LIMIT)

    # Structure first here too, for the same reason as the per-provider probe.
    assert_live_result_contract("results-chain", out, _LIMIT)
    emit_status("results-chain", "ok" if out else "empty", len(out))
    assert out, (
        f"websearch.results() returned nothing from the chain {order}. The "
        f"final provider is the repository-pinned {_ANCHOR} anchor, so the "
        f"gate requires a real result here: either the fall-through in "
        f"results() is broken, or the anchor produced nothing (which may be "
        f"the container, its settings, the adapter, or the live upstream "
        f"engines it proxies).")

"""Live end-to-end tests for every Olympus search provider.

These tests contact real external services. They run only when explicitly
enabled and skip providers whose required environment variables are absent,
or whose credential the provider itself rejects — an expired key is an
operator problem, not a defect in the code under test, and must not be able
to hold the merge queue shut. The skip reason names the credential to rotate.

Run with:

    $env:OLYMPUS_SEARCH_LIVE="1"
    python -m pytest -q tests/test_websearch_live.py
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


_LIVE = os.environ.get("OLYMPUS_SEARCH_LIVE", "").strip().lower() in {
    "1",
    "true",
    "yes",
}

pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason="set OLYMPUS_SEARCH_LIVE=1 to run real search-provider tests",
)


@pytest.mark.parametrize("provider", tuple(websearch._PROVIDERS))
def test_live_search_provider(provider):
    required_env, fetch = websearch._PROVIDERS[provider]
    missing = [name for name in required_env if not os.environ.get(name)]

    if missing:
        pytest.skip(
            f"{provider} requires: {', '.join(missing)}"
        )

    try:
        results = fetch("Olympus agent framework GitHub", 3)
    except websearch.RateLimited:
        pytest.skip(f"{provider} rate-limited the live probe (HTTP 429)")
    except urllib.error.HTTPError as err:
        if err.code not in _CREDENTIAL_REJECTED:
            raise
        pytest.skip(
            f"{provider} rejected the configured credential "
            f"(HTTP {err.code} {err.reason}); rotate "
            f"{', '.join(required_env)} to re-enable this probe"
        )

    assert results, f"{provider} returned no live results"
    assert len(results) <= 3

    for result in results:
        assert set(result) == {"title", "url", "snippet"}
        assert result["title"]
        assert result["url"].startswith(("http://", "https://"))
        assert isinstance(result["snippet"], str)

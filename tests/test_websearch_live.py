"""Live end-to-end tests for every Olympus search provider.

These tests contact real external services. They run only when explicitly
enabled and skip providers whose required environment variables are absent.

Run with:

    $env:OLYMPUS_SEARCH_LIVE="1"
    python -m pytest -q tests/test_websearch_live.py
"""

from __future__ import annotations

import os

import pytest

from olympus import websearch


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

    results = fetch("Olympus agent framework GitHub", 3)

    assert results, f"{provider} returned no live results"
    assert len(results) <= 3

    for result in results:
        assert set(result) == {"title", "url", "snippet"}
        assert result["title"]
        assert result["url"].startswith(("http://", "https://"))
        assert isinstance(result["snippet"], str)

"""Pluggable web-search providers — one seam, many backends.

Adopted from Odysseus's search-provider layer: client-side web search should
not live or die with one engine. Providers are tried in order until one
returns results; a provider that rate-limits (429) is put on cooldown and the
next takes over; identical queries within the TTL are served from a small
in-process cache instead of hammering anyone.

Providers (each active only when its configuration is present):

  searxng     OLYMPUS_SEARXNG_URL          self-hosted metasearch — the
                                           sovereignty-friendly option
  brave       BRAVE_SEARCH_API_KEY         api.search.brave.com
  tavily      TAVILY_API_KEY               api.tavily.com (search-for-LLMs)
  serper      SERPER_API_KEY               google.serper.dev (Google SERP)
  google_pse  GOOGLE_PSE_KEY+GOOGLE_PSE_CX Google Programmable Search
  bing        SERPAPI_API_KEY               Bing results through SerpApi
  ddg         (always available)           DuckDuckGo HTML, no key

Order defaults to the list above filtered to what's configured;
OLYMPUS_SEARCH_PROVIDERS="tavily,ddg" pins an explicit order.

Trust posture: provider *endpoints* are operator configuration (like a model
base URL), so requests go out under security.assert_egress_allowed — the
sovereign-mode choke — rather than the model-URL SSRF guard, which would
wrongly refuse a loopback SearXNG. Result *content* (titles/snippets/pages)
remains untrusted data; callers wrap it before it reaches a prompt.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from . import security

_TIMEOUT = 20
_CACHE_TTL = 15 * 60
_COOLDOWN = 5 * 60

_cache: dict[str, tuple[float, list[dict]]] = {}
_cooling: dict[str, float] = {}       # provider name -> retry-not-before


class RateLimited(RuntimeError):
    pass


def _request(url: str, payload: dict | None = None,
             headers: dict | None = None) -> Any:
    """JSON request to an operator-configured search endpoint (GET, or POST
    when `payload` is given), gated by the sovereign egress choke."""
    host = urllib.parse.urlparse(url).hostname or ""
    security.assert_egress_allowed(host)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"User-Agent": "olympus-search",
                 **({"Content-Type": "application/json"} if payload is not None
                    else {}),
                 **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as err:
        if err.code == 429:
            raise RateLimited(url)
        raise


def _norm(title: Any, url: Any, snippet: Any) -> dict | None:
    u = str(url or "").strip()
    if not u.startswith(("http://", "https://")):
        return None
    return {"title": str(title or "").strip()[:300], "url": u,
            "snippet": str(snippet or "").strip()[:500]}


# --- providers -------------------------------------------------------------

def _searxng(query: str, limit: int) -> list[dict]:
    base = os.environ["OLYMPUS_SEARXNG_URL"].rstrip("/")
    data = _request(f"{base}/search?format=json&q="
                    + urllib.parse.quote(query))
    out = [_norm(r.get("title"), r.get("url"), r.get("content"))
           for r in (data.get("results") or [])[:limit]]
    return [r for r in out if r]


def _brave(query: str, limit: int) -> list[dict]:
    data = _request(
        "https://api.search.brave.com/res/v1/web/search?q="
        + urllib.parse.quote(query),
        headers={"X-Subscription-Token":
                 os.environ["BRAVE_SEARCH_API_KEY"],
                 "Accept": "application/json"})
    web = (data.get("web") or {}).get("results") or []
    out = [_norm(r.get("title"), r.get("url"), r.get("description"))
           for r in web[:limit]]
    return [r for r in out if r]


def _tavily(query: str, limit: int) -> list[dict]:
    data = _request("https://api.tavily.com/search",
                    payload={"api_key": os.environ["TAVILY_API_KEY"],
                             "query": query, "max_results": limit})
    out = [_norm(r.get("title"), r.get("url"), r.get("content"))
           for r in (data.get("results") or [])[:limit]]
    return [r for r in out if r]


def _serper(query: str, limit: int) -> list[dict]:
    data = _request("https://google.serper.dev/search",
                    payload={"q": query, "num": limit},
                    headers={"X-API-KEY": os.environ["SERPER_API_KEY"]})
    out = [_norm(r.get("title"), r.get("link"), r.get("snippet"))
           for r in (data.get("organic") or [])[:limit]]
    return [r for r in out if r]


def _google_pse(query: str, limit: int) -> list[dict]:
    data = _request(
        "https://www.googleapis.com/customsearch/v1?key="
        + urllib.parse.quote(os.environ["GOOGLE_PSE_KEY"])
        + "&cx=" + urllib.parse.quote(os.environ["GOOGLE_PSE_CX"])
        + "&q=" + urllib.parse.quote(query))
    out = [_norm(r.get("title"), r.get("link"), r.get("snippet"))
           for r in (data.get("items") or [])[:limit]]
    return [r for r in out if r]


def _bing(query: str, limit: int) -> list[dict]:
    """Bing web results through SerpApi.

    Microsoft's standalone Bing Search APIs are retired, so this provider uses
    SerpApi's maintained Bing engine while preserving Olympus's normalized
    search-result shape and provider-fallback behavior.
    """
    data = _request(
        "https://serpapi.com/search?engine=bing"
        + "&q=" + urllib.parse.quote(query)
        + "&count=" + str(max(1, limit))
        + "&api_key=" + urllib.parse.quote(os.environ["SERPAPI_API_KEY"]))
    out = [_norm(r.get("title"), r.get("link"), r.get("snippet"))
           for r in (data.get("organic_results") or [])[:limit]]
    return [r for r in out if r]


def _ddg(query: str, limit: int) -> list[dict]:
    from . import tools
    return tools.ddg_results(query, limit)


_PROVIDERS: dict[str, tuple[tuple[str, ...], Callable[[str, int], list[dict]]]] = {
    # name: (required env vars, fetcher)
    "searxng": (("OLYMPUS_SEARXNG_URL",), _searxng),
    "brave": (("BRAVE_SEARCH_API_KEY",), _brave),
    "tavily": (("TAVILY_API_KEY",), _tavily),
    "serper": (("SERPER_API_KEY",), _serper),
    "google_pse": (("GOOGLE_PSE_KEY", "GOOGLE_PSE_CX"), _google_pse),
    "bing": (("SERPAPI_API_KEY",), _bing),
    "ddg": ((), _ddg),
}


def configured() -> list[str]:
    """Provider order: explicit OLYMPUS_SEARCH_PROVIDERS, else every
    configured provider in preference order, DDG always last-resort."""
    explicit = os.environ.get("OLYMPUS_SEARCH_PROVIDERS", "")
    if explicit.strip():
        names = [n.strip().lower() for n in explicit.split(",") if n.strip()]
        return [n for n in names if n in _PROVIDERS]
    active = [name for name, (env, _) in _PROVIDERS.items()
              if name != "ddg" and all(os.environ.get(v) for v in env)]
    return active + ["ddg"]


def results(query: str, limit: int = 8) -> list[dict]:
    """Search `query` across the configured providers. Returns
    [{title, url, snippet}] from the first provider that delivers; a failing
    or rate-limited provider falls through to the next."""
    key = f"{query}\x00{limit}"
    hit = _cache.get(key)
    now = time.time()
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    for name in configured():
        env, fetch = _PROVIDERS[name]
        if not all(os.environ.get(v) for v in env):
            continue
        if _cooling.get(name, 0) > now:
            continue
        try:
            out = fetch(query, limit)
        except RateLimited:
            _cooling[name] = now + _COOLDOWN
            continue
        except Exception:
            continue
        if out:
            _cache[key] = (now, out)
            if len(_cache) > 256:                    # bound the cache
                _cache.pop(next(iter(_cache)))
            return out
    return []


def search_text(query: str) -> str:
    """The web_search tool's text rendering."""
    return "\n\n".join(f"{r['title']}\n{r['url']}\n{r['snippet']}"
                       for r in results(query)) or "No results found."

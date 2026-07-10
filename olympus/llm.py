"""Anthropic backend — Claude via the official SDK.

All calls stream (timeout protection on long outputs), use adaptive thinking,
and cache the system prompt so repeated agent calls are cheap.
"""

from __future__ import annotations

import json
import time
from typing import Any

import anthropic

from . import config, replaystore, security, usage

_clients: dict[tuple[str | None, str | None], anthropic.Anthropic] = {}
# Bound the cache: on a public BYOK instance each distinct visitor key/base is a
# new entry (with its own connection pool), so an unbounded dict grows without
# limit under untrusted input. FIFO-evict the oldest when over the cap.
_CLIENTS_MAX = 256


def _cache_control() -> dict[str, str]:
    """Cache-breakpoint marker honouring the configured TTL. The 5-minute
    default needs no extra fields; the 1-hour tier is requested per-block
    (and needs the extended-TTL beta header, added in complete())."""
    if config.prompt_cache_ttl() == "1h":
        return {"type": "ephemeral", "ttl": "1h"}
    return {"type": "ephemeral"}


# Beta header for the 1-hour prompt-cache tier.
_EXTENDED_TTL_BETA = "extended-cache-ttl-2025-04-11"


def _cache_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Add a cache breakpoint to the tool list so the (often large) tool
    schemas are billed once and then read from cache on every later turn of
    the agent loop. The breakpoint on the last tool caches the whole array;
    we copy rather than mutate the caller's dicts."""
    if not tools:
        return tools
    out = [dict(t) for t in tools]
    out[-1] = {**out[-1], "cache_control": _cache_control()}
    return out


# Numeric/size *bounds* keywords Anthropic's structured-output format rejects
# ("... is not supported" 400s). Verified live: maxItems and minimum are
# rejected; minItems/maxLength/pattern/format are ACCEPTED, so we must NOT strip
# those (doing so would silently change existing schemas). We drop only the
# bounds keywords — they are advisory caps and every call site slices/validates
# defensively regardless.
_UNSUPPORTED_SCHEMA_KEYS = frozenset({
    "maxItems", "minimum", "maximum",
    "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "maxProperties", "minProperties",
})


def server_web_search(settings: config.Settings, query: str,
                      max_results: int = 8) -> list[dict[str, Any]]:
    """Anthropic SERVER-SIDE web search → [{title, url, snippet}]. The search
    runs on Anthropic's servers, so it works regardless of this host's outbound
    egress policy (a locked-down proxy that blocks general web still lets the
    model search). Returns [] on non-Anthropic providers or any failure — the
    caller falls back to the client-side provider layer."""
    if settings.provider != "anthropic":
        return []
    try:
        resp = client(settings).messages.create(
            model=config.require_model(settings), max_tokens=1024,
            tools=[{"type": "web_search_20250305", "name": "web_search",
                    "max_uses": 3}],
            messages=[{"role": "user",
                       "content": f"Search the web for: {query}"}])
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", "") != "web_search_tool_result":
            continue
        for item in getattr(block, "content", None) or []:
            url = getattr(item, "url", None)
            if not url:
                continue
            out.append({"title": getattr(item, "title", "") or "",
                        "url": url, "snippet": getattr(item, "page_age", "") or ""})
            if len(out) >= max_results:
                return out
    return out


def _walk_text(node: Any, acc: list[str], budget: int = 40) -> None:
    """Collect string-ish `text`/`data` fields from a nested SDK block tree."""
    if budget <= 0:
        return
    if isinstance(node, str):
        return
    for attr in ("text", "data"):
        v = getattr(node, attr, None)
        if isinstance(v, str) and v.strip():
            acc.append(v)
    inner = getattr(node, "content", None)
    if isinstance(inner, list):
        for child in inner:
            _walk_text(child, acc, budget - 1)
    elif inner is not None and not isinstance(inner, str):
        _walk_text(inner, acc, budget - 1)
    src = getattr(node, "source", None)
    if src is not None:
        _walk_text(src, acc, budget - 1)


def server_web_fetch(settings: config.Settings, url: str,
                     max_chars: int = 12_000) -> str:
    """Anthropic SERVER-SIDE fetch of `url` → readable text. Runs on Anthropic's
    servers (egress-policy independent). Returns "" on non-Anthropic providers
    or failure so the caller falls back to the client-side pinned fetch."""
    if settings.provider != "anthropic":
        return ""
    try:
        resp = client(settings).messages.create(
            model=config.require_model(settings), max_tokens=4096,
            tools=[{"type": "web_fetch_20250910", "name": "web_fetch",
                    "max_uses": 1}],
            extra_headers={"anthropic-beta": "web-fetch-2025-09-10"},
            messages=[{"role": "user", "content":
                       f"Fetch {url} and reproduce its main textual content "
                       "verbatim (no commentary)."}])
    except Exception:
        return ""
    # Prefer the raw fetched document from the tool-result block; fall back to
    # the model's reproduced text.
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", "") == "web_fetch_tool_result":
            acc: list[str] = []
            _walk_text(block, acc)
            doc = " ".join(acc).strip()
            if len(doc) > 80:
                return doc[:max_chars]
    text = " ".join(getattr(b, "text", "") for b in getattr(resp, "content", [])
                    or [] if getattr(b, "type", "") == "text").strip()
    return text[:max_chars]


def _strict_object_schema(schema: Any) -> Any:
    """Return a copy of `schema` normalized for Anthropic's structured-output
    API: `additionalProperties: false` on every object node (REQUIRED — it 400s
    otherwise), and unsupported validation keywords (maxItems, pattern, …)
    stripped. Applying this centrally means no individual schema can forget it —
    a whole class of live-only failures removed."""
    if isinstance(schema, dict):
        out = {}
        for k, v in schema.items():
            if k in _UNSUPPORTED_SCHEMA_KEYS:
                continue
            out[k] = _strict_object_schema(v)
        is_object = out.get("type") == "object" or "properties" in out
        if is_object and "additionalProperties" not in out:
            out["additionalProperties"] = False
        return out
    if isinstance(schema, list):
        return [_strict_object_schema(v) for v in schema]
    return schema


def client(settings: config.Settings | None = None) -> anthropic.Anthropic:
    key = settings.api_key if settings else None
    base = settings.base_url if settings else None
    cache_key = (key, base)
    if cache_key not in _clients:
        if len(_clients) >= _CLIENTS_MAX:
            _clients.pop(next(iter(_clients)))     # evict the oldest entry
        kwargs: dict[str, Any] = {}
        if key:
            kwargs["api_key"] = key
        elif base:
            # A custom endpoint with no explicit key: do NOT let the SDK fall
            # back to the operator's ANTHROPIC_API_KEY env var and ship it to a
            # user-supplied base_url. Pass an empty key so it fails closed (401)
            # instead of leaking the operator's credential to a third party.
            kwargs["api_key"] = ""
        if base:
            kwargs["base_url"] = base
        _clients[cache_key] = anthropic.Anthropic(**kwargs)
    return _clients[cache_key]


def complete(
    system: str,
    messages: list[dict[str, Any]],
    *,
    settings: config.Settings | None = None,
    tools: list[dict[str, Any]] | None = None,
    mcp_servers: list[dict[str, Any]] | None = None,
    container: str | None = None,
    effort: str = "high",
    max_tokens: int | None = None,
    output_schema: dict[str, Any] | None = None,
) -> anthropic.types.Message:
    """One streamed Messages API call; returns the final Message."""
    settings = settings or config.Settings.from_env()
    params: dict[str, Any] = {
        "model": config.require_model(settings),
        "max_tokens": max_tokens or config.MAX_TOKENS,
        "system": [
            {
                "type": "text",
                "text": system,
                "cache_control": _cache_control(),
            }
        ],
        "messages": messages,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort},
    }
    # Reuse the server-side container across turns (web search's dynamic
    # filtering runs server-side code execution, which the API requires us to
    # reference by container id when continuing the conversation).
    if container:
        params["container"] = container
    if tools:
        params["tools"] = _cache_tools(tools)
    if output_schema:
        params["output_config"]["format"] = {
            "type": "json_schema",
            "schema": _strict_object_schema(output_schema),
        }
    # Native MCP connector support — routes through the beta endpoint.
    use_beta = bool(mcp_servers)
    if mcp_servers:
        params["mcp_servers"] = mcp_servers
        params["betas"] = ["mcp-client-2025-11-20"]
    # The 1-hour cache tier also rides the beta endpoint.
    if config.prompt_cache_ttl() == "1h":
        params.setdefault("betas", []).append(_EXTENDED_TTL_BETA)
        use_beta = True

    # Re-executable replay: hash this exact request. In replay mode return the
    # frozen response with NO network; a missing hash means the orchestration
    # diverged from the recorded run. In record mode we store the response below.
    req_hash = replaystore.request_hash(params)
    if replaystore.replaying():
        message = replaystore.get(req_hash)
        if message is None:
            raise replaystore.ReplayDivergence(req_hash, params)
        replaystore.note_call(req_hash)
        return message

    # Sovereign egress choke (after replay, which makes no network call): a
    # remote Anthropic host fails closed here under sovereign mode. No-op when
    # sovereign is off, so the normal path is unchanged.
    security.assert_egress_allowed(config.member_host(settings))

    # Observe-only plugin hooks (telemetry/guardrails); mutation is not
    # honoured here — it would silently diverge the replay hash.
    from . import connectors
    connectors.emit("pre_llm_call", params)

    # Key-rotation ring: on a rate-limited or credit-exhausted key, move to the
    # member's next configured key immediately instead of burning the remaining
    # retries on a key that will keep saying no (mirrors openai_compat._post).
    keys = settings.all_keys()
    key_idx = 0

    def _active() -> config.Settings:
        if key_idx == 0 or not keys:
            return settings
        import dataclasses
        return dataclasses.replace(settings, api_key=keys[key_idx % len(keys)])

    def _quotaish(err: Exception) -> bool:
        low = str(err).lower()
        return any(m in low for m in ("credit", "quota", "billing",
                                      "insufficient", "balance"))

    last_err: Exception | None = None
    for attempt in range(4):
        s = _active()
        try:
            endpoint = client(s).beta.messages if use_beta \
                else client(s).messages
            with usage.slot():
                with endpoint.stream(**params) as stream:
                    message = stream.get_final_message()
            u = getattr(message, "usage", None)
            if u is not None:
                usage.record(
                    params["model"],
                    getattr(u, "input_tokens", 0)
                    + getattr(u, "cache_read_input_tokens", 0)
                    + getattr(u, "cache_creation_input_tokens", 0),
                    getattr(u, "output_tokens", 0),
                )
            replaystore.put(req_hash, message)   # freeze for re-executable replay
            replaystore.note_call(req_hash)
            connectors.emit("post_llm_call", params, message)
            return message
        except (anthropic.RateLimitError, anthropic.InternalServerError,
                anthropic.APIConnectionError) as err:
            last_err = err
            if isinstance(err, anthropic.RateLimitError) \
                    and key_idx + 1 < len(keys):
                key_idx += 1                # next key now — no backoff needed
                continue
            if attempt < 3:                 # no point sleeping after the last try
                time.sleep(2 ** attempt)
        except anthropic.APIStatusError as err:
            # Anthropic reports an exhausted balance as a 400-class error
            # ("credit balance is too low"): rotate if another key exists;
            # anything else 4xx is a real request problem — surface it (the
            # pool-level failover in backend.py may still switch members).
            if _quotaish(err) and key_idx + 1 < len(keys):
                last_err = err
                key_idx += 1
                continue
            raise
    raise last_err  # type: ignore[misc]


def stream_text(
    system: str,
    messages: list[dict[str, Any]],
    *,
    settings: config.Settings | None = None,
    effort: str = "high",
    max_tokens: int | None = None,
):
    """Yield text deltas of a streamed Anthropic completion (no tools)."""
    settings = settings or config.Settings.from_env()
    params: dict[str, Any] = {
        "model": config.require_model(settings),
        "max_tokens": max_tokens or config.MAX_TOKENS,
        "system": [{"type": "text", "text": system,
                    "cache_control": _cache_control()}],
        "messages": messages,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort},
    }
    endpoint = client(settings).messages
    if config.prompt_cache_ttl() == "1h":
        params["betas"] = [_EXTENDED_TTL_BETA]
        endpoint = client(settings).beta.messages
    security.assert_egress_allowed(config.member_host(settings))
    with usage.slot():
        with endpoint.stream(**params) as stream:
            for text in stream.text_stream:
                yield text
            final = stream.get_final_message()
    u = getattr(final, "usage", None)
    if u is not None:
        usage.record(params["model"],
                     getattr(u, "input_tokens", 0)
                     + getattr(u, "cache_read_input_tokens", 0)
                     + getattr(u, "cache_creation_input_tokens", 0),
                     getattr(u, "output_tokens", 0))


def text_of(message: anthropic.types.Message) -> str:
    """Concatenate the text blocks of a response."""
    return "\n".join(b.text for b in message.content if b.type == "text").strip()


def json_of(message: anthropic.types.Message) -> dict[str, Any]:
    """Parse the (schema-constrained) text of a response as JSON."""
    return json.loads(text_of(message))

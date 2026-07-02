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
        "model": settings.model or config.default_model(),
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
            "schema": output_schema,
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

    last_err: Exception | None = None
    for attempt in range(4):
        try:
            endpoint = client(settings).beta.messages if use_beta \
                else client(settings).messages
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
            return message
        except (anthropic.RateLimitError, anthropic.InternalServerError,
                anthropic.APIConnectionError) as err:
            last_err = err
            if attempt < 3:                 # no point sleeping after the last try
                time.sleep(2 ** attempt)
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
        "model": settings.model or config.default_model(),
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

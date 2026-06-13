"""Anthropic backend — Claude via the official SDK.

All calls stream (timeout protection on long outputs), use adaptive thinking,
and cache the system prompt so repeated agent calls are cheap.
"""

from __future__ import annotations

import json
import time
from typing import Any

import anthropic

from . import config, usage

_clients: dict[tuple[str | None, str | None], anthropic.Anthropic] = {}


def _cache_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Add a cache breakpoint to the tool list so the (often large) tool
    schemas are billed once and then read from cache on every later turn of
    the agent loop. The breakpoint on the last tool caches the whole array;
    we copy rather than mutate the caller's dicts."""
    if not tools:
        return tools
    out = [dict(t) for t in tools]
    out[-1] = {**out[-1], "cache_control": {"type": "ephemeral"}}
    return out


def client(settings: config.Settings | None = None) -> anthropic.Anthropic:
    key = settings.api_key if settings else None
    base = settings.base_url if settings else None
    cache_key = (key, base)
    if cache_key not in _clients:
        kwargs: dict[str, Any] = {}
        if key:
            kwargs["api_key"] = key
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
        "model": settings.model or config.MODEL,
        "max_tokens": max_tokens or config.MAX_TOKENS,
        "system": [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
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
            return message
        except (anthropic.RateLimitError, anthropic.InternalServerError,
                anthropic.APIConnectionError) as err:
            last_err = err
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
        "model": settings.model or config.MODEL,
        "max_tokens": max_tokens or config.MAX_TOKENS,
        "system": [{"type": "text", "text": system,
                    "cache_control": {"type": "ephemeral"}}],
        "messages": messages,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort},
    }
    with usage.slot():
        with client(settings).messages.stream(**params) as stream:
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

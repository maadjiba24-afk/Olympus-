"""OpenAI-compatible backend — any /chat/completions endpoint, zero deps.

Covers OpenAI, Google Gemini (OpenAI-compat endpoint), DeepSeek, Groq, Mistral,
OpenRouter, Ollama, LM Studio, vLLM... Tool calling uses standard function
calling; structured output uses prompt-enforced JSON with lenient parsing so
it works even on providers without response_format support.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from . import config, security, tools, usage

DEFAULT_BASE_URL = "https://api.openai.com/v1"

# Per-endpoint credential rotation state. When a key hits a rate limit or quota
# wall we mark it exhausted and advance a shared cursor, so subsequent calls
# start from the next healthy key instead of re-hitting the dead one. Keyed by
# base_url so different providers rotate independently.
_key_cursor: dict[str, int] = {}
_key_stats: dict[str, dict[str, int]] = {}   # base -> {"<masked>": success_count}
_exhausted: dict[str, set[str]] = {}         # base -> {masked keys seen exhausted}
# Rotation state is read/written from concurrent gateway worker threads
# (Telegram, Discord, web, …). A lock keeps cursor advances and the exhausted
# set from racing — without it two threads can skip or re-hit the same key.
import threading as _threading
_ROT_LOCK = _threading.RLock()

# Status codes / body markers that mean "this key is spent — try another".
_QUOTA_CODES = frozenset({402, 429})
_QUOTA_MARKERS = ("insufficient", "quota", "exceeded your current",
                  "billing", "rate limit", "out of credit", "balance")


def _is_quota_error(code: int, detail: str) -> bool:
    if code in _QUOTA_CODES:
        return True
    low = (detail or "").lower()
    return any(m in low for m in _QUOTA_MARKERS)


# Azure OpenAI speaks the same request/response body as OpenAI, but on a
# deployment-scoped URL with an `api-key` header instead of `Authorization:
# Bearer`. Detecting it by endpoint host lets Azure ride the whole openai_compat
# rotation/failover/usage machinery unchanged (provider stays "openai").
_AZURE_DEFAULT_API_VERSION = "2024-10-21"


def _is_azure(base: str) -> bool:
    # Match only the canonical Azure OpenAI host. A broader `.azure.com` catch
    # would misroute an ordinary OpenAI-compatible proxy that merely happens to
    # be Azure-hosted (it expects Bearer auth, not the api-key header).
    host = (urlparse(base).hostname or "").lower()
    return host.endswith(".openai.azure.com")


def _endpoint_url(settings: config.Settings, base: str) -> str:
    """The /chat/completions URL for this endpoint — deployment-scoped +
    api-version query for Azure, plain for every other OpenAI-compatible host."""
    if not _is_azure(base):
        return f"{base}/chat/completions"
    import os
    from urllib.parse import quote
    deployment = (os.environ.get("OLYMPUS_AZURE_DEPLOYMENT")
                  or settings.model or "").strip()
    if not deployment:
        raise ValueError(
            "Azure OpenAI needs a deployment name — set the model to your "
            "Azure deployment, or set OLYMPUS_AZURE_DEPLOYMENT.")
    api_version = os.environ.get("OLYMPUS_AZURE_API_VERSION",
                                 _AZURE_DEFAULT_API_VERSION)
    return (f"{base}/openai/deployments/{quote(deployment, safe='')}"
            f"/chat/completions?api-version={quote(api_version, safe='')}")


def _auth_headers(base: str, key: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if key:
        # Azure authenticates with an `api-key` header; everyone else with a
        # standard OpenAI-style bearer token.
        if _is_azure(base):
            headers["api-key"] = key
        else:
            headers["Authorization"] = f"Bearer {key}"
    return headers


def _record_key_use(base: str, masked: str) -> None:
    with _ROT_LOCK:
        _key_stats.setdefault(base, {})
        _key_stats[base][masked] = _key_stats[base].get(masked, 0) + 1


def rotation_report(settings: config.Settings) -> str:
    """Human-readable provenance of key rotation for `olympus models`. Never
    prints a secret — only masked tails and per-key success counts."""
    keys = settings.all_keys()
    if len(keys) <= 1:
        return ""
    base = (settings.base_url or DEFAULT_BASE_URL).rstrip("/")
    stats = _key_stats.get(base, {})
    spent = _exhausted.get(base, set())
    lines = [f"Credential rotation ({len(keys)} keys for {settings.provider}):"]
    for i, k in enumerate(keys):
        masked = config.mask_key(k)
        flags = []
        if masked in spent:
            flags.append("exhausted")
        if i == _key_cursor.get(base, 0) % len(keys):
            flags.append("active")
        used = stats.get(masked, 0)
        tag = f" [{', '.join(flags)}]" if flags else ""
        lines.append(f"  {masked}: {used} calls{tag}")
    return "\n".join(lines)


def _post(settings: config.Settings, payload: dict[str, Any]) -> dict[str, Any]:
    base = (settings.base_url or DEFAULT_BASE_URL).rstrip("/")
    url = _endpoint_url(settings, base)
    # Sovereign egress choke: under sovereign mode a model call to a
    # non-allowlisted host fails closed here (no-op when sovereign is off).
    security.assert_egress_allowed(urlparse(url).hostname or "")
    body = json.dumps(payload).encode()

    keys = list(settings.all_keys())
    # No credentials (e.g. a keyless local endpoint) — one unauthenticated pass
    # through the same retry logic.
    key_ring: list[str | None] = keys or [None]
    n = len(key_ring)
    with _ROT_LOCK:
        start = _key_cursor.get(base, 0) if keys else 0

    last_err: Exception | None = None
    # Try each key in turn; each key gets the full transient-error backoff loop.
    for k_off in range(n):
        idx = (start + k_off) % n
        key = key_ring[idx]
        headers = _auth_headers(base, key)
        masked = config.mask_key(key) if key else "(no key)"

        for attempt in range(4):
            req = urllib.request.Request(url, data=body, headers=headers)
            try:
                with usage.slot():
                    with urllib.request.urlopen(req, timeout=600) as resp:
                        data = json.loads(resp.read())
                u = data.get("usage") or {}
                usage.record(payload.get("model", "unknown"),
                             int(u.get("prompt_tokens", 0)),
                             int(u.get("completion_tokens", 0)))
                if keys:
                    with _ROT_LOCK:
                        _key_cursor[base] = idx    # remember the healthy key
                    _record_key_use(base, masked)
                return data
            except urllib.error.HTTPError as err:
                detail = err.read().decode(errors="replace")[:500]
                # Quota/rate-limit on THIS key with another available → rotate
                # to the next key immediately instead of burning backoff on a
                # key that's already spent.
                if keys and n > 1 and _is_quota_error(err.code, detail):
                    with _ROT_LOCK:
                        _exhausted.setdefault(base, set()).add(masked)
                    last_err = RuntimeError(f"HTTP {err.code} (key {masked}): "
                                            f"{detail}")
                    break                          # advance to the next key
                if err.code in (408, 429, 500, 502, 503, 529) and attempt < 3:
                    last_err = RuntimeError(f"HTTP {err.code}: {detail}")
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(
                    f"Provider error HTTP {err.code} from {url}: {detail}"
                ) from None
            except (urllib.error.URLError, TimeoutError, OSError) as err:
                last_err = err
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Provider unreachable at {url}: {last_err}")


def _to_openai_tools(tool_defs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Anthropic-style tool defs to OpenAI function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": d["name"],
                "description": d.get("description", ""),
                "parameters": d.get("input_schema", {"type": "object"}),
            },
        }
        for d in tool_defs
    ]


def extract_json(text: str) -> dict[str, Any]:
    """Lenient JSON extraction: tolerate code fences and surrounding prose."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


def complete_text(settings: config.Settings, system: str,
                  messages: list[dict[str, Any]], effort: str = "high") -> str:
    # NOTE: `effort` is accepted for parity with the Anthropic backend's
    # interface but is NOT applied here — OpenAI-compatible endpoints express
    # reasoning effort only on specific reasoning models (via `reasoning_effort`),
    # and blindly sending it would 400 the many models that don't support it. So
    # on this path effort is a deliberate no-op rather than a silent control.
    resp = _post(settings, {
        "model": settings.model,
        "messages": [{"role": "system", "content": system}, *messages],
        # Bound the response — without a cap, reasoning models can generate
        # unboundedly (a major latency/cost sink). config.MAX_TOKENS is generous.
        "max_tokens": config.MAX_TOKENS,
    })
    return (resp["choices"][0]["message"].get("content") or "").strip()


def complete_json(settings: config.Settings, system: str,
                  messages: list[dict[str, Any]], schema: dict[str, Any],
                  effort: str = "high") -> dict[str, Any]:
    instruction = (
        "\n\nRespond with ONLY a single JSON object matching this JSON schema "
        "— no prose, no code fences:\n" + json.dumps(schema)
    )
    text = complete_text(settings, system + instruction, messages, effort)
    return extract_json(text)


def run_agent(settings: config.Settings, system: str, task: str,
              tool_defs: list[dict[str, Any]] | None, effort: str = "high",
              max_iterations: int = config.MAX_AGENT_ITERATIONS) -> str:
    """Agentic tool loop over the OpenAI function-calling protocol."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": task},
    ]
    payload_tools = _to_openai_tools(tool_defs) if tool_defs else None

    for _ in range(max_iterations):
        payload: dict[str, Any] = {"model": settings.model, "messages": messages}
        if payload_tools:
            payload["tools"] = payload_tools
        message = _post(settings, payload)["choices"][0]["message"]
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            return (message.get("content") or "").strip()

        messages.append({
            "role": "assistant",
            "content": message.get("content"),
            "tool_calls": tool_calls,
        })
        for call in tool_calls:
            name = call["function"]["name"]
            handler = tools.resolve_handler(name)
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
                result = handler(**args) if handler else f"Error: unknown tool {name}"
            except Exception as err:
                result = f"Error: {err}"
            text = str(result)[:40_000]
            if security.should_wrap(name):
                text = security.wrap_untrusted(text, source=name)
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", name),
                "content": text,
            })

    return ("[Agent stopped: tool-use iteration limit reached. Partial work "
            "above may be incomplete.]")

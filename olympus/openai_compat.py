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

from . import config, security, tools, usage

DEFAULT_BASE_URL = "https://api.openai.com/v1"


def _post(settings: config.Settings, payload: dict[str, Any]) -> dict[str, Any]:
    base = (settings.base_url or DEFAULT_BASE_URL).rstrip("/")
    url = f"{base}/chat/completions"
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if settings.api_key:
        headers["Authorization"] = f"Bearer {settings.api_key}"

    last_err: Exception | None = None
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
            return data
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="replace")[:500]
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
    resp = _post(settings, {
        "model": settings.model,
        "messages": [{"role": "system", "content": system}, *messages],
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
            handler = tools.HANDLERS.get(name)
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
                result = handler(**args) if handler else f"Error: unknown tool {name}"
            except Exception as err:
                result = f"Error: {err}"
            text = str(result)[:40_000]
            if name in security.INGESTION_TOOLS:
                text = security.wrap_untrusted(text, source=name)
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", name),
                "content": text,
            })

    return ("[Agent stopped: tool-use iteration limit reached. Partial work "
            "above may be incomplete.]")

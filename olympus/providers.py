"""Provider catalog + model discovery — the data behind the setup wizard.

A small, curated catalog of the providers Olympus speaks to, each with its base
URL, auth style, and (where the provider exposes one) a `/models` endpoint so the
wizard can **list the real model IDs** for the user's key instead of making them
guess (the exact friction that bites people: wrong base URL / wrong model name).

Auth styles:
  - ``api_key``      — paste a key (most providers).
  - ``subscription`` — no API key; runs on a local login (e.g. the Claude Code
                       CLI on a Claude Pro/Max subscription).
  - ``local``        — a local server (Ollama); usually no key.

`fetch_models()` queries the provider for its model list (best-effort, never
raises). `build_pool_config()` turns the chosen members into the env config
Olympus reads (primary settings + the `OLYMPUS_MODELS` pool JSON).
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    backend: str                 # "anthropic" | "openai" | "claude-code"
    auth: str = "api_key"        # "api_key" | "subscription" | "local"
    base_url: str = ""
    key_env: str = ""            # the env var users usually hold this key in
    sample_models: tuple[str, ...] = field(default_factory=tuple)
    note: str = ""


# Ordered for display. Subscription/login options first (no API spend), then the
# common API-key providers, then local and custom.
CATALOG: tuple[Provider, ...] = (
    Provider("claude-code", "Claude — via Claude Code subscription (no API key)",
             "claude-code", auth="subscription",
             sample_models=("claude-opus-4-8", "claude-sonnet-4-6"),
             note="Runs on your Claude Pro/Max login via the `claude` CLI — "
                  "personal use; no web tools (single-turn)."),
    Provider("anthropic", "Anthropic (Claude) — API key",
             "anthropic", base_url="https://api.anthropic.com",
             key_env="ANTHROPIC_API_KEY",
             sample_models=("claude-opus-4-8", "claude-sonnet-4-6")),
    Provider("openai", "OpenAI (GPT) — API key",
             "openai", base_url="https://api.openai.com/v1",
             key_env="OPENAI_API_KEY",
             sample_models=("gpt-4o", "gpt-4o-mini")),
    Provider("deepseek", "DeepSeek — API key",
             "openai", base_url="https://api.deepseek.com",
             key_env="DEEPSEEK_API_KEY",
             sample_models=("deepseek-chat", "deepseek-reasoner")),
    Provider("glm", "Z.AI / GLM (Zhipu) — API key",
             "openai", base_url="https://open.bigmodel.cn/api/paas/v4",
             key_env="GLM_API_KEY",
             sample_models=("glm-4.5-flash", "glm-4.5-air", "glm-4.6")),
    Provider("kimi", "Kimi / Moonshot — API key",
             "openai", base_url="https://api.moonshot.ai/v1",
             key_env="MOONSHOT_API_KEY",
             sample_models=("moonshot-v1-128k", "moonshot-v1-8k", "kimi-k2.5")),
    Provider("groq", "Groq — API key (very fast)",
             "openai", base_url="https://api.groq.com/openai/v1",
             key_env="GROQ_API_KEY",
             sample_models=("llama-3.3-70b-versatile",)),
    Provider("openrouter", "OpenRouter (100+ models) — API key",
             "openai", base_url="https://openrouter.ai/api/v1",
             key_env="OPENROUTER_API_KEY",
             sample_models=("openai/gpt-4o", "anthropic/claude-sonnet-4")),
    Provider("gemini", "Google Gemini (OpenAI-compatible) — API key",
             "openai",
             base_url="https://generativelanguage.googleapis.com/v1beta/openai",
             key_env="GEMINI_API_KEY",
             sample_models=("gemini-2.5-flash", "gemini-2.5-pro")),
    Provider("mistral", "Mistral — API key",
             "openai", base_url="https://api.mistral.ai/v1",
             key_env="MISTRAL_API_KEY", sample_models=("mistral-large-latest",)),
    Provider("ollama", "Ollama (local models) — no key",
             "openai", auth="local", base_url="http://localhost:11434/v1",
             sample_models=("llama3.1", "qwen2.5")),
    Provider("custom", "Custom OpenAI-compatible endpoint",
             "openai", auth="api_key",
             note="Enter the base URL and model yourself."),
)

_BY_KEY = {p.key: p for p in CATALOG}


def get(key: str) -> Provider | None:
    return _BY_KEY.get(key)


def _http_json(url: str, headers: dict, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def fetch_models(provider: Provider, api_key: str = "",
                 base_url: str = "") -> list[str]:
    """Return the model IDs the provider exposes for this key. Best-effort:
    returns [] on any error (the wizard then falls back to sample/typed names)."""
    base = (base_url or provider.base_url).rstrip("/")
    try:
        if provider.backend == "anthropic":
            data = _http_json(base + "/v1/models",
                              {"x-api-key": api_key,
                               "anthropic-version": "2023-06-01"})
            return [m["id"] for m in data.get("data", []) if m.get("id")]
        if provider.backend == "openai":
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            data = _http_json(base + "/models", headers)
            items = data.get("data", data if isinstance(data, list) else [])
            return [m.get("id") for m in items if isinstance(m, dict) and m.get("id")]
    except Exception:
        return []
    return []


@dataclass
class Member:
    """One chosen provider entry, ready to become pool config."""
    backend: str
    model: str = ""
    api_key: str = ""
    base_url: str = ""


def build_pool_config(members: list[Member]) -> dict[str, str]:
    """Turn chosen members into the env config Olympus reads: the first member
    becomes the primary (OLYMPUS_PROVIDER/MODEL/API_KEY/BASE_URL); the rest go
    into the OLYMPUS_MODELS pool JSON. Anthropic keys live in ANTHROPIC_API_KEY."""
    if not members:
        return {}
    out: dict[str, str] = {}
    primary = members[0]
    out["OLYMPUS_PROVIDER"] = primary.backend
    if primary.model:
        out["OLYMPUS_MODEL"] = primary.model
    if primary.backend == "anthropic":
        if primary.api_key:
            out["ANTHROPIC_API_KEY"] = primary.api_key
    elif primary.backend == "openai":
        if primary.base_url:
            out["OLYMPUS_BASE_URL"] = primary.base_url
        if primary.api_key:
            out["OLYMPUS_API_KEY"] = primary.api_key
    # claude-code needs neither key nor base_url.

    extras = []
    for m in members[1:]:
        entry: dict[str, str] = {"provider": m.backend}
        if m.model:
            entry["model"] = m.model
        if m.api_key:
            entry["api_key"] = m.api_key
        if m.base_url:
            entry["base_url"] = m.base_url
        extras.append(entry)
    if extras:
        out["OLYMPUS_MODELS"] = json.dumps(extras, separators=(",", ":"))
    return out

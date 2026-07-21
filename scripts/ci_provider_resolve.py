#!/usr/bin/env python3
"""Resolve the CI eval provider/model from whichever provider secret exists.

The answer-quality gate makes real model calls, and different operators hold
keys for different providers. This script checks a priority chain of provider
key envs, takes the FIRST one present, discovers a usable model from that
account's own `GET /models` inventory (availability differs per account/tier),
and exports the resolved `OLYMPUS_*` config for the gate step.

Priority (first present wins):
  ANTHROPIC_API_KEY  -> native Anthropic provider (claude-opus-4-8)
  OPENAI_API_KEY, GEMINI_API_KEY, DEEPSEEK_API_KEY, GROQ_API_KEY,
  MISTRAL_API_KEY, XAI_API_KEY, OPENROUTER_API_KEY, KIMI_API_KEY
                     -> Olympus's OpenAI-compatible provider against that
                        vendor's endpoint, model resolved from /models

With no key at all it exports nothing — the gate script then skips cleanly.

Output: appends KEY=VALUE lines to $GITHUB_ENV (so later workflow steps see
them); when $GITHUB_ENV is unset (local use) it prints `export KEY=VALUE`
lines to stdout for `eval "$(python scripts/ci_provider_resolve.py)"`.
Inventory and the chosen model are logged to stderr, never the key.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

# (secret env, base_url, preferred model ids in order, fallback id prefix).
# Preference lists favor current strong non-reasoning chat models; whatever an
# account actually offers wins. KIMI keeps the order that produced the
# committed baseline (moonshot-v1-32k on that account) so gating stays
# apples-to-apples.
OPENAI_COMPAT = [
    ("OPENAI_API_KEY", "https://api.openai.com/v1",
     ["gpt-5.1", "gpt-5", "gpt-4.1", "gpt-4o"], "gpt"),
    ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai",
     ["gemini-3-pro-preview", "gemini-2.5-pro", "gemini-2.5-flash"], "gemini"),
    ("DEEPSEEK_API_KEY", "https://api.deepseek.com/v1",
     ["deepseek-chat", "deepseek-reasoner"], "deepseek"),
    ("GROQ_API_KEY", "https://api.groq.com/openai/v1",
     ["llama-3.3-70b-versatile", "moonshotai/kimi-k2-instruct",
      "openai/gpt-oss-120b"], "llama"),
    ("MISTRAL_API_KEY", "https://api.mistral.ai/v1",
     ["mistral-large-latest", "mistral-medium-latest"], "mistral"),
    ("XAI_API_KEY", "https://api.x.ai/v1",
     ["grok-4", "grok-4-fast", "grok-3"], "grok"),
    ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1",
     ["openrouter/auto"], ""),
    ("KIMI_API_KEY", "https://api.moonshot.ai/v1",
     ["kimi-k2-0905-preview", "kimi-k2-0711-preview", "kimi-k2-turbo-preview",
      "kimi-latest", "moonshot-v1-32k", "moonshot-v1-8k"], "kimi"),
]


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def list_models(base_url: str, key: str) -> list[str]:
    req = urllib.request.Request(
        base_url.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    return [m["id"] for m in data.get("data", []) if m.get("id")]


def pick_model(ids: list[str], preferred: list[str], prefix: str) -> str | None:
    for cand in preferred:
        if cand in ids:
            return cand
    for mid in ids:
        if prefix and mid.startswith(prefix):
            return mid
    return ids[0] if ids else None


def emit(pairs: dict[str, str]) -> None:
    dest = os.environ.get("GITHUB_ENV")
    if dest:
        with open(dest, "a", encoding="utf-8") as f:
            for k, v in pairs.items():
                f.write(f"{k}={v}\n")
    else:
        for k, v in pairs.items():
            print(f"export {k}={v}")


def main() -> int:
    if os.environ.get("ANTHROPIC_API_KEY"):
        log("Provider: anthropic (ANTHROPIC_API_KEY) — model claude-opus-4-8")
        emit({"OLYMPUS_PROVIDER": "anthropic",
              "OLYMPUS_MODEL": "claude-opus-4-8",
              "ANTHROPIC_API_KEY": os.environ["ANTHROPIC_API_KEY"]})
        return 0
    for env_name, base_url, preferred, prefix in OPENAI_COMPAT:
        key = os.environ.get(env_name)
        if not key:
            continue
        log(f"Provider: openai-compatible ({env_name}) at {base_url}")
        try:
            ids = list_models(base_url, key)
            log("Available models:\n" + "\n".join(f"  {m}" for m in ids))
        except Exception as err:
            # Discovery is best-effort — some gateways gate /models. Fall back
            # to the first preferred id and let the eval call surface errors.
            log(f"Model discovery failed ({err}); using preferred pin.")
            ids = []
        model = pick_model(ids, preferred, prefix) or preferred[0]
        log(f"Using eval model: {model}")
        emit({"OLYMPUS_PROVIDER": "openai",
              "OPENAI_API_KEY": key,
              "OLYMPUS_BASE_URL": base_url,
              "OLYMPUS_MODEL": model})
        return 0
    log("No provider key found — the gate will skip cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

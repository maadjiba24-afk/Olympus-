#!/usr/bin/env python3
"""Bind an explicitly authorized provider/model; ambient credentials are not consent.

No automatic provider/model fallback. Inventory verification, when requested,
occurs only after authorization. Output contains configuration identifiers only;
credentials are never copied to stdout or GITHUB_ENV. The gate binds the same
explicit provider directly. See docs/LIVE_QUALITY_AUTHORIZATION.md.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from olympus import live_quality_authorization as auth

# Retained pure compatibility helper data; live execution never auto-selects.
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



def list_models(base_url: str, key: str) -> list[str]:
    req = urllib.request.Request(
        base_url.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read(1048577)
    if len(raw) > 1048576:
        raise ValueError("oversized provider inventory")
    data = json.loads(raw)
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not all(
        isinstance(row, dict) and isinstance(row.get("id"), str) for row in rows
    ):
        raise ValueError("invalid provider inventory")
    return [row["id"] for row in rows]


def pick_model(ids: list[str], preferred: list[str], prefix: str) -> str | None:
    """Legacy pure utility only; not an authorization or live selection path."""
    for cand in preferred:
        if cand in ids:
            return cand
    for mid in ids:
        if prefix and mid.startswith(prefix):
            return mid
    return ids[0] if ids else None


def emit(pairs: dict[str, str]) -> None:
    if any(any(ch in value for ch in "\r\n\0") for value in pairs.values()):
        raise ValueError("invalid configuration output")
    dest = os.environ.get("GITHUB_ENV")
    if dest:
        with open(dest, "a", encoding="utf-8") as stream:
            stream.write("".join(f"{key}={value}\n" for key, value in pairs.items()))
    print(json.dumps(pairs, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    auth.add_arguments(parser)
    parser.add_argument("--check-authorization", action="store_true",
                        help="check consent/source only; no credential access or provider calls")
    parser.add_argument("--verify-model", action="store_true",
                        help="verify this model in the selected compatible provider's inventory")
    args = parser.parse_args(argv)
    try:
        authorization = auth.authorize(args, ROOT)
        if args.check_authorization:
            print("Live quality authorization and reviewed source verified; no provider contacted.")
            return 0
        settings = auth.provider_configuration(authorization)
    except (auth.AuthorizationRequired, OSError) as error:
        print(f"UNAVAILABLE: {error}; no provider contacted.", file=sys.stderr)
        return 3
    if args.verify_model and settings["provider"] == "openai":
        try:
            ids = list_models(settings["base_url"], settings["api_key"])
            if settings["model"] not in ids:
                raise ValueError("approved model is absent from provider inventory")
        except Exception as error:
            # Provider exceptions may include credentials or response content.
            print("UNAVAILABLE: model inventory verification failed ("
                  + type(error).__name__ + "); no fallback selected.", file=sys.stderr)
            return 3
    emit({"OLYMPUS_PROVIDER": settings["provider"],
          "OLYMPUS_MODEL": settings["model"],
          "OLYMPUS_BASE_URL": settings["base_url"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Explicit authorization for the live quality CLI and CI provider resolver.

Credentials are capability, never consent. This is an operator/runner boundary,
not a sandbox against an operator who can replace the source or environment.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess


class AuthorizationRequired(ValueError):
    """No provider discovery, benchmark, or baseline write is authorized."""


PROVIDERS = {
    "anthropic": ("ANTHROPIC_API_KEY", "anthropic", ""),
    "openai": ("OPENAI_API_KEY", "openai", "https://api.openai.com/v1"),
    "gemini": ("GEMINI_API_KEY", "openai", "https://generativelanguage.googleapis.com/v1beta/openai"),
    "deepseek": ("DEEPSEEK_API_KEY", "openai", "https://api.deepseek.com/v1"),
    "groq": ("GROQ_API_KEY", "openai", "https://api.groq.com/openai/v1"),
    "mistral": ("MISTRAL_API_KEY", "openai", "https://api.mistral.ai/v1"),
    "xai": ("XAI_API_KEY", "openai", "https://api.x.ai/v1"),
    "openrouter": ("OPENROUTER_API_KEY", "openai", "https://openrouter.ai/api/v1"),
    "kimi": ("KIMI_API_KEY", "openai", "https://api.moonshot.ai/v1"),
}


def add_arguments(parser):
    parser.add_argument("--allow-live", action="store_true",
                        help="explicitly authorize this invocation's provider calls")
    parser.add_argument("--expected-commit", help="exact reviewed 40-character commit")
    parser.add_argument("--authorization-reason", help="operator's reason for this live run")
    parser.add_argument("--provider", choices=sorted(PROVIDERS))
    parser.add_argument("--model", help="explicit approved model; never selected from ambient keys")


def _require(condition, message):
    if not condition:
        raise AuthorizationRequired(message)


def _unique(pairs):
    value = {}
    for key, item in pairs:
        _require(key not in value, "duplicate authorization event field")
        value[key] = item
    return value


def reviewed_head(root):
    """Read Git without hooks, optional index refresh, or custom Git directories."""
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                "GIT_NAMESPACE", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"):
        _require(key not in os.environ, "custom Git environment cannot authorize a live run")
    def read(*args):
        result = subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", *args],
            cwd=root, capture_output=True, timeout=15,
        )
        _require(result.returncode == 0, "reviewed Git checkout is unavailable")
        return result.stdout.decode("utf-8").strip()
    _require(Path(read("rev-parse", "--show-toplevel")).resolve() == Path(root).resolve(),
             "live invocation must use the reviewed repository root")
    _require(not read("status", "--porcelain", "--untracked-files=no"),
             "tracked checkout differs from the reviewed commit")
    return read("rev-parse", "HEAD")


def authorize(args, root, *, environ=None):
    """Refuse before reading credentials, opening a socket, or changing a file."""
    env = os.environ if environ is None else environ
    _require(args.allow_live is True, "live quality evaluation requires --allow-live")
    _require(isinstance(args.expected_commit, str)
             and re.fullmatch(r"[0-9a-f]{40}", args.expected_commit),
             "an exact reviewed --expected-commit is required")
    _require(isinstance(args.authorization_reason, str)
             and 12 <= len(args.authorization_reason) <= 512
             and all(ch.isprintable() for ch in args.authorization_reason),
             "a printable operator authorization reason (12-512 characters) is required")
    _require(args.provider in PROVIDERS, "an explicit provider is required")
    _require(isinstance(args.model, str)
             and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}", args.model),
             "an explicit safe model identifier is required")
    if any(env.get(key) for key in ("CI", "GITHUB_ACTIONS", "GITHUB_EVENT_NAME",
                                    "GITHUB_EVENT_PATH", "GITHUB_WORKFLOW_REF")):
        _require(env.get("GITHUB_ACTIONS") == "true"
                 and env.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
                 and env.get("GITHUB_REF") == "refs/heads/main"
                 and env.get("GITHUB_REF_PROTECTED") == "true"
                 and env.get("GITHUB_SHA") == args.expected_commit
                 and env.get("OLYMPUS_LIVE_QUALITY_ENABLED") == "1",
                 "CI live evaluation requires an enabled manual dispatch on protected main")
        repository = env.get("GITHUB_REPOSITORY", "")
        _require(bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository))
                 and env.get("GITHUB_WORKFLOW_REF") ==
                 repository + "/.github/workflows/live-quality-gate.yml@refs/heads/main",
                 "unexpected live authorization workflow")
        event_path = Path(env.get("GITHUB_EVENT_PATH", ""))
        _require(event_path.is_file() and not event_path.is_symlink(),
                 "manual authorization event is unavailable")
        with event_path.open("rb") as stream:
            raw = stream.read(65537)
        _require(len(raw) <= 65536, "oversized authorization event")
        try:
            event = json.loads(raw, object_pairs_hook=_unique)
        except (UnicodeError, ValueError) as error:
            raise AuthorizationRequired("invalid authorization event") from error
        inputs = event.get("inputs") if isinstance(event, dict) else None
        _require(isinstance(inputs, dict), "manual authorization inputs are unavailable")
        _require(inputs.get("allow_live") is True
                 or (type(inputs.get("allow_live")) is str
                     and inputs["allow_live"] == "true"),
                 "dispatch did not explicitly authorize live calls")
        for name in ("expected_commit", "provider", "model", "authorization_reason"):
            _require(inputs.get(name) == getattr(args, name),
                     "dispatch authorization does not match " + name)
    try:
        actual = reviewed_head(root)
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise AuthorizationRequired("reviewed checkout could not be verified") from error
    _require(actual == args.expected_commit, "reviewed commit changed")
    return {key: getattr(args, key) for key in
            ("expected_commit", "authorization_reason", "provider", "model")}


def provider_configuration(authorization, *, environ=None):
    """Bind the explicitly selected credential and endpoint; no ambient fallback."""
    env = os.environ if environ is None else environ
    secret, provider, endpoint = PROVIDERS[authorization["provider"]]
    key = env.get(secret, "")
    _require(bool(key) and not any(ch in key for ch in "\r\n\0"),
             "the explicitly selected provider credential is unavailable or invalid")
    return {"provider": provider, "model": authorization["model"],
            "api_key": key, "base_url": endpoint}

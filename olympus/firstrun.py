"""First-run experience — saved keys + an interactive setup wizard.

The goal: after the one-line install, a user types `olympus`, answers two
questions (which provider, paste the key), and is chatting in plain English.
No bash, no pip, no environment variables.

Keys are saved to ~/.olympus/config.env (chmod 600) and loaded automatically
on every later run. Real environment variables always win over the saved file,
so power users and CI keep full control.
"""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("OLYMPUS_HOME", Path.home() / ".olympus"))
CONFIG_ENV = CONFIG_DIR / "config.env"

# any of these present (env or saved file) means Olympus has a model to talk to
_KEY_VARS = ("ANTHROPIC_API_KEY", "OLYMPUS_API_KEY", "OPENAI_API_KEY",
             "OLYMPUS_BASE_URL")


def load_env_file(path: Path | None = None) -> int:
    """Load saved KEY=VALUE lines into the environment (env vars win).
    Returns how many values were applied."""
    path = path or CONFIG_ENV
    if not path.exists():
        return 0
    applied = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value
            applied += 1
    return applied


def configured() -> bool:
    """True if Olympus has at least one way to reach a model."""
    return any(os.environ.get(v) for v in _KEY_VARS)


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or default


def _ask_secret(prompt: str) -> str:
    try:
        value = getpass.getpass(f"{prompt} (input hidden): ").strip()
    except Exception:                       # no tty echo control available
        value = input(f"{prompt}: ").strip()
    return value


def _save(values: dict[str, str]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["# OLYMPUS saved configuration — created by `olympus setup`.",
             "# Environment variables override anything here.", ""]
    lines += [f"{k}={v}" for k, v in values.items() if v]
    CONFIG_ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        CONFIG_ENV.chmod(0o600)             # the file holds API keys
    except OSError:
        pass
    for k, v in values.items():             # take effect immediately
        if v:
            os.environ[k] = v


def _read_saved() -> dict[str, str]:
    """Current KEY=VALUE pairs in the saved config (empty if none)."""
    out: dict[str, str] = {}
    if not CONFIG_ENV.exists():
        return out
    for line in CONFIG_ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip("'\"")
    return out


def save_env_value(key: str, value: str) -> None:
    """Set one saved config value without clobbering the others."""
    values = _read_saved()
    values[key] = value
    _save(values)


def wizard() -> bool:
    """Interactive setup: choose a provider, paste a key, done.
    Returns True if a configuration was saved."""
    print()
    print("  ⚡ Welcome to OLYMPUS — one-time setup (takes ~30 seconds)")
    print()
    print("  Which AI provider key are you bringing?")
    print("    1) Anthropic (Claude) — recommended, full capability")
    print("    2) OpenAI (GPT)")
    print("    3) Other OpenAI-compatible (Groq, OpenRouter, Gemini, Ollama…)")
    print()
    choice = _ask("  Choose 1, 2 or 3", "1")

    values: dict[str, str] = {}
    if choice == "2":
        key = _ask_secret("  Paste your OpenAI API key")
        if not key:
            print("  No key entered — setup cancelled.")
            return False
        values["OLYMPUS_PROVIDER"] = "openai"
        values["OLYMPUS_API_KEY"] = key
        values["OLYMPUS_MODEL"] = _ask("  Model to use", "gpt-4o")
    elif choice == "3":
        values["OLYMPUS_PROVIDER"] = "openai"
        values["OLYMPUS_BASE_URL"] = _ask(
            "  Provider base URL (e.g. https://api.groq.com/openai/v1)")
        values["OLYMPUS_MODEL"] = _ask("  Model name (as the provider calls it)")
        key = _ask_secret("  Paste the API key (Enter to skip for local servers)")
        if key:
            values["OLYMPUS_API_KEY"] = key
        if not values["OLYMPUS_BASE_URL"] or not values["OLYMPUS_MODEL"]:
            print("  Base URL and model are required — setup cancelled.")
            return False
    else:
        key = _ask_secret("  Paste your Anthropic API key (sk-ant-…)")
        if not key:
            print("  No key entered — setup cancelled.")
            return False
        values["ANTHROPIC_API_KEY"] = key

    _save(values)
    print()
    print(f"  ✓ Saved to {CONFIG_ENV} (only you can read it).")
    print("  ✓ You're set. Just type what you want — plain English works.")
    print("    Tip: add more keys anytime with `olympus setup`;")
    print("    Olympus composes multiple models into one brain.")
    print()
    return True


def ensure_ready() -> bool:
    """Load saved config; if still unconfigured and we're interactive, run the
    wizard. Returns True when a model is reachable."""
    load_env_file()
    if configured():
        return True
    if sys.stdin.isatty() and sys.stdout.isatty():
        return wizard() and configured()
    print("No API key configured. Run `olympus setup`, or set "
          "ANTHROPIC_API_KEY / OLYMPUS_API_KEY.", file=sys.stderr)
    return False

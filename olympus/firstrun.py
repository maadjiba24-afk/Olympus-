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


# Keys whose values are secrets — masked when shown, never printed in full.
_SECRET_HINT = ("KEY", "TOKEN", "SECRET", "PASSWORD", "SEED")


def _is_secret(key: str) -> bool:
    return any(h in key.upper() for h in _SECRET_HINT)


def show_config() -> str:
    """Render the saved config with any secret values masked. Read-only."""
    from . import config as cfg
    saved = _read_saved()
    if not saved:
        return (f"No saved config at {CONFIG_ENV}.\n"
                "Run `olympus setup` to create one.")
    lines = [f"Saved config ({CONFIG_ENV}, owner-only):", ""]
    for k in sorted(saved):
        v = saved[k]
        shown = cfg.mask_key(v) if _is_secret(k) else v
        overridden = k in os.environ and os.environ[k] != v
        tag = "   ← overridden by an env var" if overridden else ""
        lines.append(f"  {k}={shown}{tag}")
    return "\n".join(lines)


def config_set(key: str, value: str) -> str:
    """Set one config value (owner-only file). Never echoes a secret back."""
    key = key.strip()
    if not key:
        return "Usage: olympus config set <KEY> <VALUE>"
    # A genuine external override is an env var that differs from our saved
    # value (env loaded from our own file matches, so it isn't an override).
    external = key in os.environ and os.environ[key] != _read_saved().get(key)
    save_env_value(key, value)
    from . import config as cfg
    shown = cfg.mask_key(value) if _is_secret(key) else value
    note = ("  (a real environment variable of the same name is set and will "
            "still override this)") if external else ""
    return f"Saved {key}={shown} to {CONFIG_ENV}.{note}"


def open_in_editor() -> str:
    """Open the saved config in $EDITOR (nano fallback). Returns a status line."""
    import shutil
    import subprocess
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_ENV.exists():
        _save({})
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if not editor:
        editor = "nano" if shutil.which("nano") else "vi"
    try:
        subprocess.call([editor, str(CONFIG_ENV)])
    except Exception as err:
        return (f"Could not launch '{editor}': {err}\n"
                f"Edit it yourself: {CONFIG_ENV}")
    return f"Edited {CONFIG_ENV}."


def _configure_providers() -> list:
    """The provider-composition loop, shared by the full wizard and
    `setup model`. Returns the chosen Members (possibly empty)."""
    from . import providers
    members = []
    while True:
        print("  Add a provider:")
        prov = providers.CATALOG[_choose(
            "Choose a provider", [p.label for p in providers.CATALOG], 1) - 1]
        member = _configure_member(prov)
        if member:
            members.append(member)
            print(f"  ✓ Added {prov.label.split(' —')[0]}"
                  + (f"  ({member.model})" if member.model else ""))
        if not _yes("Add another provider to compose a pool?", default_yes=False):
            break
    return members


def setup_section(section: str) -> bool:
    """Edit ONE part of the config without re-running the whole wizard —
    mirrors `olympus setup model|terminal|gateway|tools`. Returns True if saved."""
    section = (section or "").strip().lower()
    values = _read_saved()

    if section in ("model", "models", "provider"):
        members = _configure_providers()
        if not members:
            print("  Nothing chosen — no changes.")
            return False
        from . import providers
        for k in ("OLYMPUS_PROVIDER", "OLYMPUS_MODEL", "OLYMPUS_API_KEY",
                  "OLYMPUS_BASE_URL", "OLYMPUS_MODELS", "ANTHROPIC_API_KEY"):
            values.pop(k, None)
        values.update(providers.build_pool_config(members))
        _save(values)
        print(f"  ✓ Pool updated ({len(members)} provider(s)).")
        return True

    if section in ("gateway", "gateways", "messaging"):
        _configure_gateway(values)
        _save(values)
        return True

    if section in ("terminal", "exec", "sandbox", "tools"):
        if _yes("Allow Olympus to run commands/edit files in a sandbox?", True):
            be = _choose("Execution backend",
                         ["local (this machine)", "docker (isolated container)"], 1)
            values["OLYMPUS_EXEC_BACKEND"] = "docker" if be == 2 else "local"
        modes = ["enforce (block catastrophic commands — recommended)",
                 "paranoid (also block risky commands)",
                 "audit (classify only, never block)",
                 "off (no screening — not recommended)"]
        pick = _choose("Command security gate", modes, 1)
        values["OLYMPUS_EXEC_SECURITY"] = ["enforce", "paranoid", "audit",
                                           "off"][pick - 1]
        if section == "tools":
            key = _ask_secret("  Media/vision API key (OPENAI_API_KEY) — "
                              "enables image gen, TTS, analyze_image (blank to skip)")
            if key:
                values["OPENAI_API_KEY"] = key
        _save(values)
        print("  ✓ Saved.")
        return True

    print(f"  Unknown section '{section}'. "
          "Try: model | terminal | gateway | tools  (or `olympus setup` for all).")
    return False


def _choose(prompt: str, labels: list[str], default: int = 1) -> int:
    """Numbered picker (1-based). Robust everywhere — SSH, WSL, dumb terminals."""
    for i, label in enumerate(labels, 1):
        print(f"    {i:2}) {label}")
    while True:
        raw = _ask(f"  {prompt}", str(default))
        try:
            n = int(raw)
            if 1 <= n <= len(labels):
                return n
        except ValueError:
            pass
        print("  Please enter a number from the list.")


def _yes(prompt: str, default_yes: bool = False) -> bool:
    d = "Y/n" if default_yes else "y/N"
    ans = _ask(f"  {prompt} ({d})", "").strip().lower()
    if not ans:
        return default_yes
    return ans in ("y", "yes")


def _pick_model(prov, api_key: str, base_url: str) -> str:
    """Let the user pick a model — listing the provider's real model IDs when we
    can fetch them, so they never have to guess the exact name."""
    from . import providers
    models = providers.fetch_models(prov, api_key, base_url)
    if models:
        models = sorted(models)[:30]
        print(f"  Found {len(models)} models for your key:")
        labels = models + ["(type a different model name)"]
        idx = _choose("Pick a model", labels, default=1)
        if idx <= len(models):
            return models[idx - 1]
        return _ask("  Model name", prov.sample_models[0] if prov.sample_models else "")
    # Couldn't list — offer the known samples, else free text.
    if prov.sample_models:
        print("  (couldn't list models automatically — pick a common one)")
        labels = list(prov.sample_models) + ["(type a different model name)"]
        idx = _choose("Pick a model", labels, default=1)
        if idx <= len(prov.sample_models):
            return prov.sample_models[idx - 1]
    return _ask("  Model name (as the provider calls it)")


def _configure_member(prov):
    """Interactively build one pool Member for the chosen provider, or None."""
    from . import providers
    if prov.note:
        print(f"  · {prov.note}")
    if prov.auth == "subscription":            # e.g. Claude Code
        from . import claude_code
        if not claude_code.available():
            print("  ⚠ The `claude` CLI isn't installed/logged in on this "
                  "machine, so this option won't work yet. Install Claude Code "
                  "and run `claude` once to log in, then re-run setup.")
            if not _yes("Add it anyway?"):
                return None
        model = _pick_model(prov, "", "") if prov.sample_models else ""
        return providers.Member(backend=prov.backend, model=model)

    base_url = prov.base_url
    if prov.key == "custom":
        base_url = _ask("  Provider base URL (e.g. https://api.x.ai/v1)")
        if not base_url:
            return None
    api_key = ""
    if prov.auth != "local":
        api_key = _ask_secret(f"  Paste your {prov.label.split(' —')[0]} API key")
        if not api_key and prov.key != "custom":
            print("  No key entered — skipping this provider.")
            return None
    model = _pick_model(prov, api_key, base_url)
    if not model:
        print("  No model chosen — skipping this provider.")
        return None
    return providers.Member(backend=prov.backend, model=model,
                            api_key=api_key, base_url=base_url)


_GATEWAYS = {
    "telegram": [("TELEGRAM_BOT_TOKEN", "Bot token from @BotFather", True)],
    "discord": [("DISCORD_WEBHOOK_URL", "Channel webhook URL (outbound)", False),
                ("DISCORD_PUBLIC_KEY", "App public key (for inbound)", False)],
    "slack": [("SLACK_BOT_TOKEN", "xoxb- bot token", True),
              ("SLACK_SIGNING_SECRET", "App signing secret", True)],
    "signal": [("SIGNAL_CLI_REST_URL", "signal-cli REST URL", False),
               ("SIGNAL_NUMBER", "Your registered number (+1…)", False)],
}


def _configure_gateway(values: dict) -> None:
    names = list(_GATEWAYS)
    idx = _choose("Which platform?", [n.title() for n in names] + ["(none)"], 1)
    if idx > len(names):
        return
    name = names[idx - 1]
    for env, desc, secret in _GATEWAYS[name]:
        val = _ask_secret(f"  {env} — {desc}") if secret else _ask(f"  {env} — {desc}")
        if val:
            values[env] = val
    print(f"  ✓ {name.title()} configured. Run it with: olympus {name}")


def wizard() -> bool:
    """Guided setup: compose one or more providers (incl. a Claude subscription)
    into Olympus's model pool, with model auto-discovery; optionally set up a
    messaging gateway, execution backend, and fast mode. Returns True if saved."""
    from . import providers
    print()
    print("  ⚡ Welcome to OLYMPUS — guided setup")
    print("  Bring API keys and/or a Claude subscription; Olympus composes them")
    print("  into one brain (each model used where it's strongest).")
    print()

    members = _configure_providers()

    if not members:
        print("  Nothing configured — setup cancelled.")
        return False

    values = providers.build_pool_config(members)

    if len(members) > 1 or _yes("Enable fast mode (lower latency)?", True):
        values["OLYMPUS_FAST"] = "1"
    if _yes("Allow Olympus to run commands/edit files in a sandbox?"):
        be = _choose("Execution backend",
                     ["local (this machine)", "docker (isolated container)"], 1)
        values["OLYMPUS_EXEC_BACKEND"] = "docker" if be == 2 else "local"
    if _yes("Connect a messaging platform now (Telegram/Discord/Slack/Signal)?"):
        _configure_gateway(values)

    # Merge: replace provider-related keys, keep any unrelated saved settings.
    existing = _read_saved()
    for k in ("OLYMPUS_PROVIDER", "OLYMPUS_MODEL", "OLYMPUS_API_KEY",
              "OLYMPUS_BASE_URL", "OLYMPUS_MODELS", "ANTHROPIC_API_KEY"):
        existing.pop(k, None)
    existing.update(values)
    _save(existing)

    print()
    print(f"  ✓ Saved to {CONFIG_ENV} (owner-only).")
    print(f"  ✓ Pool: {len(members)} provider(s) composed into one brain.")
    print("  ✓ You're set — type `olympus` to chat. `olympus models` shows the pool.")
    print()
    # End on the same readiness picture `olympus doctor` shows.
    try:
        from . import doctor
        print(doctor.render())
        print()
    except Exception:
        pass
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

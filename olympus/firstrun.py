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


def _offer_detected(members: list) -> None:
    """If provider keys are already in the environment, offer to use them
    without making the user paste anything (auto-detection, #9)."""
    from . import providers
    for prov, key in _env_detected():
        name = prov.label.split(' —')[0]
        if _yes(f"Found {prov.key_env} in your environment — use {name}?", True):
            model = _pick_model(prov, key, prov.base_url)
            if model:
                members.append(providers.Member(
                    backend=prov.backend, model=model, api_key=key,
                    base_url=prov.base_url))
                print(f"  ✓ Using {name} from your environment  ({model})")


def _configure_providers(scan_env: bool = True) -> list:
    """The provider-composition loop, shared by the full wizard and
    `setup model`. Returns the chosen Members (possibly empty)."""
    members: list = []
    if scan_env:
        _offer_detected(members)
    entries = _picker_entries()
    labels = [f"{lbl}" + (f"   ({trade})" if trade else "")
              for (lbl, trade, _res) in entries]
    while True:
        # Once we have members (from env-scan or a prior pick), ask before
        # forcing another manual pick — so a detected key doesn't require one.
        if members and not _yes("Add another provider to compose a pool?", False):
            break
        print("  Add a provider:")
        idx = _choose("Choose a provider", labels, 1)
        prov = entries[idx - 1][2]()          # resolver() → Provider
        if prov is None:
            continue
        member = _configure_member(prov)
        if member:
            members.append(member)
            print(f"  ✓ Added {prov.label.split(' —')[0]}"
                  + (f"  ({member.model})" if member.model else "")
                  + f"  — {len(members)} in the pool now")
        elif not members:
            # First attempt produced nothing — offer a clean way out.
            if not _yes("Add a provider to compose a pool?", False):
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


def _env_detected() -> list[tuple]:
    """Provider keys already present in the environment — for auto-detection
    ('Found DEEPSEEK_API_KEY — use it?'). Returns (Provider, key) pairs."""
    from . import providers
    found = []
    for prov in providers.CATALOG:
        if prov.key_env and os.environ.get(prov.key_env):
            found.append((prov, os.environ[prov.key_env]))
    return found


def _picker_entries():
    """Provider choices for the wizard. The two Anthropic rows (subscription vs
    API key) are merged into ONE 'Anthropic (Claude)' entry that asks auth mode
    when chosen — auth-mode-first, so users don't puzzle over two Claude rows.
    Returns a list of (label, tradeoff, resolver) where resolver() -> Provider."""
    from . import providers

    def resolve_anthropic():
        mode = _choose("How do you want to authenticate Claude?",
                       ["Claude Pro/Max subscription — no API key",
                        "Anthropic API key"], 1)
        return providers.get("claude-code" if mode == 1 else "anthropic")

    entries = [("Anthropic (Claude)", "subscription or API key",
                resolve_anthropic)]
    for prov in providers.CATALOG:
        if prov.key in ("claude-code", "anthropic"):
            continue                       # folded into the merged entry above
        entries.append((prov.label, prov.tradeoff,
                        (lambda p=prov: p)))
    return entries


def _filter_models(models: list[str], query: str) -> list[str]:
    """Case-insensitive substring filter for large discovered model lists."""
    q = (query or "").strip().lower()
    if not q:
        return models
    return [m for m in models if q in m.lower()]


def _pick_model(prov, api_key: str, base_url: str) -> str:
    """Let the user pick a model — listing the provider's real model IDs when we
    can fetch them, so they never have to guess the exact name. Big lists
    (OpenRouter-scale) get a type-to-filter step before the numbered pick."""
    from . import providers
    models = providers.fetch_models(prov, api_key, base_url)
    if models:
        models = sorted(models)
        # Aggregators expose hundreds of models — filter before listing.
        while len(models) > 20:
            print(f"  Found {len(models)} models for your key.")
            query = _ask("  Filter (part of a name, blank to list the first 30)")
            if not query:
                models = models[:30]
                break
            hits = _filter_models(models, query)
            if hits:
                models = hits[:30]
                break
            print(f"  Nothing matches '{query}' — try again.")
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
        if prov.key_url:
            print(f"  Get one at: {prov.key_url}")
        # Providers show a key exactly once — nudge the user to save it now.
        print("  (tip: your provider shows the key only once — save it now; "
              "Olympus stores it in the owner-only config.env)")
        api_key = _ask_secret(f"  Paste your {prov.label.split(' —')[0]} API key "
                              "(blank to skip)")
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
    "email": [("GMAIL_REFRESH_TOKEN", "Gmail OAuth refresh token "
               "(connect Gmail via the web UI OAuth flow)", True),
              ("OLYMPUS_EMAIL_ALLOW", "Only reply to these senders "
               "(comma-separated; blank = anyone)", False)],
    "webhook": [("OLYMPUS_WEBHOOK_SECRET", "Shared secret callers must send "
                 "(blank = open — LAN only!)", True)],
}


def _gateway_configured(name: str, values: dict | None = None) -> bool:
    """Whether a platform has at least one of its env vars set (saved or env)."""
    values = values or {}
    return any(os.environ.get(env) or values.get(env)
               for env, _desc, _secret in _GATEWAYS[name])


def _configure_gateway(values: dict) -> None:
    """One checklist of every channel with its configured status; pick several
    numbers to set up in one pass (Hermes-style multi-select, numbered for
    SSH/WSL robustness)."""
    names = list(_GATEWAYS)
    print("  Messaging channels:")
    for i, name in enumerate(names, 1):
        state = "configured" if _gateway_configured(name, values) \
            else "not configured"
        print(f"    {i:2}) [{'x' if state == 'configured' else ' '}] "
              f"{name.title():9s} ({state})")
    raw = _ask("  Configure which? (numbers, e.g. 1 3 — blank for none)")
    picked: list[str] = []
    for tok in raw.replace(",", " ").split():
        if tok.isdigit() and 1 <= int(tok) <= len(names):
            picked.append(names[int(tok) - 1])
    for name in picked:
        print(f"  — {name.title()}:")
        for env, desc, secret in _GATEWAYS[name]:
            val = (_ask_secret(f"  {env} — {desc}") if secret
                   else _ask(f"  {env} — {desc}"))
            if val:
                values[env] = val
        print(f"  ✓ {name.title()} configured. Run it with: olympus {name}")


def _doctor_gaps() -> list:
    """The ✗/⚠ readiness items delta-setup can actually fix, mapped to a
    section: [(check, section)]. Purely informational checks are skipped."""
    from . import doctor
    fixable = {"provider": "model", "command gate": "terminal",
               "media / vision": "tools", "gateways": "gateway"}
    out = []
    for c in doctor.run_checks():
        if c.status in (doctor.FAIL, doctor.WARN) and c.name in fixable:
            out.append((c, fixable[c.name]))
    return out


def setup_missing() -> bool:
    """Delta-setup: fix ONLY what `olympus doctor` flags, instead of re-running
    the whole wizard. Walks each gap, offers the matching section, and ends on
    the doctor summary so the loop closes: doctor finds it → setup fixes it →
    doctor confirms it. Returns True if anything was configured."""
    gaps = _doctor_gaps()
    if not gaps:
        print("  ✓ Nothing missing — doctor reports all systems go.")
        return False
    changed = False
    for check, section in gaps:
        icon = "✗" if check.status == "fail" else "⚠"
        print(f"\n  {icon} {check.name}: {check.detail}")
        if _yes(f"Fix this now? (opens the '{section}' setup)",
                check.status == "fail"):
            changed = bool(setup_section(section)) or changed
    try:
        from . import doctor
        print()
        print(doctor.render(with_caps=False))
    except Exception:
        pass
    return changed


def _current_pool_line() -> str:
    """One-line view of the currently-configured pool, for a re-run."""
    from . import config as cfg
    try:
        pool = cfg.ModelPool.from_env()
        members = ", ".join(f"{m.provider}/{m.model or 'default'}"
                            for m in pool.members)
        return f"  Currently configured: {members}"
    except Exception:
        return ""


def wizard() -> bool:
    """Guided setup: compose one or more providers (incl. a Claude subscription)
    into Olympus's model pool, with model auto-discovery; optionally set up a
    messaging gateway, execution backend, and fast mode. Returns True if saved."""
    from . import providers
    print()
    print("  ⚡ Welcome to OLYMPUS — guided setup")
    print("  Bring API keys and/or a Claude subscription; Olympus composes them")
    print("  into one brain (each model used where it's strongest).")
    # Config-location block up front, so users always know where keys land.
    print(f"  Config is saved to: {CONFIG_ENV} (owner-only; env vars override).")
    rerun = configured()
    if rerun:
        line = _current_pool_line()
        if line:
            print(line)
    print()

    # On a configured install with readiness gaps, offer the delta first:
    # fix ONLY what doctor flags, instead of re-answering everything.
    if rerun:
        gaps = _doctor_gaps()
        if gaps:
            styles = [f"Fix what's missing ({len(gaps)} item(s) doctor flagged)",
                      "Quick — one provider, recommended defaults",
                      "Full — reconfigure the pool, sandbox, security, messaging"]
            pick = _choose("Setup style", styles, 1)
            if pick == 1:
                return setup_missing()
            quick = (pick == 2)
        else:
            quick = (_choose(
                "Setup style",
                ["Quick — one provider, recommended defaults (free/no-setup "
                 "where possible)",
                 "Full — configure the pool, sandbox, security, and messaging"],
                1) == 1)
    else:
        # Quick / Full fork — most people want one provider and sane defaults.
        quick = (_choose(
            "Setup style",
            ["Quick — one provider, recommended defaults (free/no-setup where "
             "possible)",
             "Full — configure the pool, sandbox, security, and messaging"],
            1) == 1)

    members = _configure_providers()
    if not members:
        print("  Nothing configured — setup cancelled.")
        return False

    values = providers.build_pool_config(members)

    if quick:
        # Recommended defaults, no further questions.
        values["OLYMPUS_FAST"] = "1"
        values.setdefault("OLYMPUS_EXEC_SECURITY", "enforce")
        print("  ✓ Quick defaults: fast mode on, command security = enforce, "
              "sandbox off (enable later with `olympus setup terminal`).")
    else:
        cur = _read_saved()
        fast_default = cur.get("OLYMPUS_FAST", "1" if len(members) > 1 else "") == "1"
        if len(members) > 1:
            values["OLYMPUS_FAST"] = "1"
            print("  ✓ Fast mode on (a pool routes light stages to the quickest model).")
        elif _yes("Enable fast mode? (lower latency; skips one optional review)",
                  fast_default):
            values["OLYMPUS_FAST"] = "1"
        if _yes("Allow Olympus to run commands/edit files in a sandbox? "
                "(it still needs your approval per action)", False):
            be = _choose("Execution backend  (local = this machine; "
                         "docker = isolated, safer for untrusted code)",
                         ["local (this machine)", "docker (isolated container)"], 1)
            values["OLYMPUS_EXEC_BACKEND"] = "docker" if be == 2 else "local"
            # Only ask about the command gate when the sandbox is enabled.
            modes = ["enforce — block catastrophic commands (recommended)",
                     "paranoid — also block risky commands (sudo, curl|sh)",
                     "audit — classify only, never block",
                     "off — no screening (not recommended)"]
            pick = _choose("Command security gate", modes, 1)
            values["OLYMPUS_EXEC_SECURITY"] = ["enforce", "paranoid", "audit",
                                               "off"][pick - 1]
        else:
            values.setdefault("OLYMPUS_EXEC_SECURITY", "enforce")
        if _yes("Connect a messaging platform now "
                "(Telegram/Discord/Slack/Signal)?", False):
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

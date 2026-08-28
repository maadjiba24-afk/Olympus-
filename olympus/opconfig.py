"""Operator-editable configuration — the admin panel's Phase 3 backend.

Olympus configuration is environment variables, loaded at every process
start from `~/.olympus/config.env` (the setup wizard's file; real env vars
always win). This module makes a **strict allowlist** of those settings
editable from the browser, with three hard rules:

1. **Allowlist, never arbitrary env.** Only the registry below can be set.
   Keys that could weaponize the process (OLYMPUS_PLUGINS_DIR,
   OLYMPUS_EXEC_BACKEND, OLYMPUS_MEMORY_DIR, PATH, ...) and the panel's own
   auth (OLYMPUS_ACCESS_TOKEN, OLYMPUS_API_KEYS — changing the lock from
   inside the room is a lockout/escalation surface) are deliberately
   absent and stay CLI-only.
2. **Secrets go to the vault, not to disk in the clear.** A secret-classed
   value is Fernet-encrypted via vault.py (requires exactly one of
   OLYMPUS_SECRET_KEY or OLYMPUS_SECRET_KEY_FILE; without a usable source we
   refuse and say exactly what to set — never a silent plaintext fallback).
   Non-secret values go to config.env like the wizard's.
3. **Honest effect reporting.** Every set/unset says where it took effect:
   applied live in THIS process immediately; other Olympus processes
   (heartbeat, gateways) read it at their next start — each key lists its
   consumers. A real environment variable that shadows a saved value is
   reported as such, never papered over.

Boot hydration: cli.main() calls `apply_secrets()` right after
firstrun.load_env_file(), so vault-stored settings reach every `olympus
<command>` process the same way config.env values do (env still wins).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

from . import firstrun

VAULT_USER = "operator"          # vault namespace for config secrets
VAULT_PREFIX = "config:"         # vault entry name = config:<ENV_KEY>

_LOOPBACK_HOSTS = ("localhost", "127.", "0.0.0.0", "::1", "[::1]")


# --- validators (reason string when invalid, None when fine) ---------------

def _v_any(value: str) -> str | None:
    return None if value.strip() else "value must not be empty"


def _v_int(value: str) -> str | None:
    try:
        int(value)
        return None
    except ValueError:
        return "must be an integer"


def _v_bool(value: str) -> str | None:
    return None if value.strip().lower() in (
        "0", "1", "true", "false", "yes", "no", "on", "off") else \
        "must be a boolean (1/0)"


def _v_provider(value: str) -> str | None:
    return None if value.strip().lower() in (
        "anthropic", "openai", "claude-code", "moa") else \
        "must be one of: anthropic, openai, claude-code, moa"


def _v_ttl(value: str) -> str | None:
    return None if value.strip().lower() in ("5m", "1h") else \
        "must be 5m or 1h"


def _v_url(value: str) -> str | None:
    try:
        p = urlparse(value.strip())
    except ValueError:
        return "malformed URL"
    if p.scheme not in ("http", "https") or not p.hostname:
        return "must be an http(s) URL"
    if p.scheme == "http" and not any(
            p.hostname.startswith(h.rstrip(".")) or p.hostname == h
            for h in _LOOPBACK_HOSTS):
        return "plain http is only allowed for local hosts — use https"
    return None


def _v_models_json(value: str) -> str | None:
    try:
        data = json.loads(value)
    except json.JSONDecodeError as err:
        return f"must be a JSON array (line {err.lineno}: {err.msg})"
    if not isinstance(data, list):
        return "must be a JSON array of member objects"
    for i, m in enumerate(data):
        if not isinstance(m, dict):
            return f"member {i + 1} must be an object"
        unknown = set(m) - {"provider", "model", "api_key", "base_url"}
        if unknown:
            return f"member {i + 1} has unknown keys: {', '.join(sorted(unknown))}"
        bad = _v_provider(str(m.get("provider", "anthropic")))
        if bad:
            return f"member {i + 1}: {bad}"
    return None


def _v_webhooks(value: str) -> str | None:
    for pair in value.split(","):
        pair = pair.strip()
        if not pair:
            continue
        name, _, url = pair.partition("=")
        if not name.strip() or not url.startswith(("http://", "https://")):
            return ("must be comma-separated name=https://... pairs "
                    f"(bad entry: '{pair[:40]}')")
    return None


# --- the registry -----------------------------------------------------------

@dataclass(frozen=True)
class Setting:
    key: str
    section: str                       # channels | email | models | connectors
    label: str
    secret: bool = False
    validate: Callable[[str], str | None] = _v_any
    # Other Olympus PROCESSES that consume this key (they read env at their
    # own start/calls, so they see a change on their next restart). The web
    # process itself always applies live.
    consumers: tuple[str, ...] = ()


_CHAN = "channels"
_MAIL = "email"
_MODL = "models"
_CONN = "connectors"

REGISTRY: dict[str, Setting] = {s.key: s for s in (
    # Channels — inbound gateways run as their own `olympus <name>` process.
    Setting("TELEGRAM_BOT_TOKEN", _CHAN, "Telegram bot token", secret=True,
            consumers=("telegram gateway", "heartbeat (notify)")),
    Setting("TELEGRAM_NOTIFY_CHAT_ID", _CHAN, "Telegram notify chat id",
            validate=_v_any, consumers=("heartbeat (notify)",)),
    Setting("TELEGRAM_ALLOWED_CHAT_IDS", _CHAN, "Telegram allowed chat ids",
            consumers=("telegram gateway",)),
    Setting("DISCORD_PUBLIC_KEY", _CHAN, "Discord app public key",
            consumers=("discord gateway",)),
    Setting("DISCORD_WEBHOOK_URL", _CHAN, "Discord notify webhook URL",
            secret=True, validate=_v_url,
            consumers=("heartbeat (notify)",)),
    Setting("SLACK_SIGNING_SECRET", _CHAN, "Slack signing secret", secret=True,
            consumers=("slack gateway",)),
    Setting("SLACK_BOT_TOKEN", _CHAN, "Slack bot token", secret=True,
            consumers=("slack gateway", "heartbeat (notify)")),
    Setting("SLACK_NOTIFY_CHANNEL", _CHAN, "Slack notify channel",
            consumers=("heartbeat (notify)",)),
    Setting("WHATSAPP_ACCESS_TOKEN", _CHAN, "WhatsApp access token",
            secret=True, consumers=("whatsapp gateway",)),
    Setting("WHATSAPP_PHONE_NUMBER_ID", _CHAN, "WhatsApp phone number id",
            consumers=("whatsapp gateway",)),
    Setting("WHATSAPP_VERIFY_TOKEN", _CHAN, "WhatsApp webhook verify token",
            secret=True, consumers=("whatsapp gateway",)),
    Setting("WHATSAPP_APP_SECRET", _CHAN, "WhatsApp app secret", secret=True,
            consumers=("whatsapp gateway",)),
    Setting("WHATSAPP_ALLOWED_NUMBERS", _CHAN, "WhatsApp allowed numbers",
            consumers=("whatsapp gateway",)),
    Setting("SIGNAL_CLI_REST_URL", _CHAN, "signal-cli REST URL",
            validate=_v_url, consumers=("signal gateway",)),
    Setting("SIGNAL_NUMBER", _CHAN, "Signal registered number",
            consumers=("signal gateway",)),
    Setting("SIGNAL_NOTIFY_RECIPIENT", _CHAN, "Signal notify recipient",
            consumers=("heartbeat (notify)",)),
    # Email (outbound SMTP; read per call in whichever process sends).
    Setting("SMTP_HOST", _MAIL, "SMTP host", consumers=("heartbeat",)),
    Setting("SMTP_PORT", _MAIL, "SMTP port", validate=_v_int,
            consumers=("heartbeat",)),
    Setting("SMTP_USER", _MAIL, "SMTP username", consumers=("heartbeat",)),
    Setting("SMTP_PASS", _MAIL, "SMTP password", secret=True,
            consumers=("heartbeat",)),
    Setting("SMTP_FROM", _MAIL, "SMTP from address", consumers=("heartbeat",)),
    Setting("OLYMPUS_EMAIL_ALLOWLIST", _MAIL, "Email recipient allowlist",
            consumers=("heartbeat",)),
    Setting("OLYMPUS_WEBHOOKS", _MAIL, "Named webhooks (name=url,...)",
            secret=True,  # webhook paths commonly contain bearer credentials
            validate=_v_webhooks, consumers=("heartbeat",)),
    # Model pool — the web process re-keys live (session bots rebuild on a
    # pool fingerprint change); other processes at their next start.
    Setting("OLYMPUS_PROVIDER", _MODL, "Primary provider",
            validate=_v_provider, consumers=("heartbeat", "gateways")),
    Setting("OLYMPUS_MODEL", _MODL, "Primary model",
            consumers=("heartbeat", "gateways")),
    Setting("ANTHROPIC_API_KEY", _MODL, "Anthropic API key", secret=True,
            consumers=("heartbeat", "gateways")),
    Setting("OLYMPUS_API_KEY", _MODL, "Provider API key (OpenAI-compatible)",
            secret=True, consumers=("heartbeat", "gateways")),
    Setting("OLYMPUS_BASE_URL", _MODL, "Provider base URL", validate=_v_url,
            consumers=("heartbeat", "gateways")),
    Setting("OLYMPUS_MODELS", _MODL, "Extra pool members (JSON array)",
            secret=True,               # members may carry api_key values
            validate=_v_models_json, consumers=("heartbeat", "gateways")),
    Setting("OLYMPUS_FAST", _MODL, "Fast mode", validate=_v_bool,
            consumers=("heartbeat", "gateways")),
    Setting("OLYMPUS_FALLBACK", _MODL, "Provider failover", validate=_v_bool,
            consumers=("heartbeat", "gateways")),
    Setting("OLYMPUS_CACHE_TTL", _MODL, "Prompt-cache TTL", validate=_v_ttl,
            consumers=("heartbeat", "gateways")),
    # Connectors — the MCP action allowlist (definitions themselves live in
    # connectors.json and are edited via mcp_add/mcp_remove, live everywhere).
    Setting("OLYMPUS_MCP_ACTION_ALLOWLIST", _CONN,
            "MCP action-server allowlist (names)",
            consumers=("heartbeat", "gateways")),
)}

SECTIONS = (_CHAN, _MAIL, _MODL, _CONN)


# --- storage ----------------------------------------------------------------

def _vault_name(key: str) -> str:
    return VAULT_PREFIX + key


def _saved_env() -> dict[str, str]:
    return firstrun._read_saved()


def _vault_keys() -> set[str]:
    """Registry keys that currently have a vault-stored value."""
    from . import vault
    try:
        return {n[len(VAULT_PREFIX):] for n in vault.names(VAULT_USER)
                if n.startswith(VAULT_PREFIX)}
    except Exception:
        return set()


def apply_secrets() -> int:
    """Hydrate vault-stored config secrets into the environment (env wins —
    same precedence as config.env). Called at process start by cli.main();
    returns how many values were applied. Never raises: a missing secret key
    or crypto backend simply means nothing to hydrate."""
    from . import vault
    applied = 0
    try:
        names = vault.names(VAULT_USER)
    except Exception:
        return 0
    for name in names:
        if not name.startswith(VAULT_PREFIX):
            continue
        key = name[len(VAULT_PREFIX):]
        if key not in REGISTRY or key in os.environ:
            continue
        try:
            value = vault.get(VAULT_USER, name)
        except Exception:
            continue
        if isinstance(value, str) and value:
            os.environ[key] = value
            applied += 1
    return applied


def set_value(key: str, value: str) -> str:
    """Validate and persist one setting; apply it to THIS process now.
    Returns the honest effect message; raises ValueError on refusal."""
    setting = REGISTRY.get(key)
    if setting is None:
        raise ValueError(
            f"'{key}' is not operator-configurable from the panel "
            "(strict allowlist; see docs/ADMIN_PANEL.md)")
    value = (value or "").strip()
    reason = setting.validate(value)
    if reason:
        raise ValueError(f"{setting.label}: {reason}")

    if setting.secret:
        from . import vault
        try:
            vault.put(VAULT_USER, _vault_name(key), value)
        except Exception as err:
            raise ValueError(
                f"cannot store '{setting.label}' encrypted: {err} — set "
                "OLYMPUS_SECRET_KEY or OLYMPUS_SECRET_KEY_FILE and retry; "
                "secrets are never written to disk in the clear") from err
        # Belt and braces: if an old plaintext copy exists in config.env
        # (e.g. from the setup wizard), remove it — the vault owns it now.
        saved = _saved_env()
        if key in saved:
            saved.pop(key)
            firstrun._save(saved)
    else:
        try:
            firstrun.save_env_value(key, value)
        except firstrun.ConfigFileError as err:
            raise ValueError(
                f"cannot save '{setting.label}': {err}") from err

    os.environ[key] = value            # live in this process immediately
    where = "the vault (encrypted)" if setting.secret else "config.env"
    note = f"{setting.label} saved to {where} and applied here now."
    if setting.consumers:
        note += (" Running processes pick it up on their next restart: "
                 + ", ".join(setting.consumers) + ".")
    return note


def unset(key: str) -> str:
    """Remove a setting from the saved stores and this process's env."""
    setting = REGISTRY.get(key)
    if setting is None:
        raise ValueError(f"'{key}' is not operator-configurable from the panel")
    removed = []
    saved = _saved_env()
    if key in saved:
        saved.pop(key)
        firstrun._save(saved)
        removed.append("config.env")
    from . import vault
    try:
        if vault.get(VAULT_USER, _vault_name(key)) is not None:
            vault.delete(VAULT_USER, _vault_name(key))
            removed.append("vault")
    except Exception:
        pass
    had_env = os.environ.pop(key, None) is not None
    if not removed and not had_env:
        return f"{setting.label} was not set."
    note = f"{setting.label} cleared" + \
        (f" from {' and '.join(removed)}" if removed else "") + "."
    note += (" If the real process environment sets it (systemd/docker), "
             "that value returns at the next restart — the panel cannot "
             "remove it there.")
    return note


def status() -> list[dict]:
    """One honest row per registry key — presence and provenance, never a
    secret value. `source` is where the effective value comes from; a saved
    value shadowed by a differing real env var is called out."""
    saved = _saved_env()
    vaulted = _vault_keys()
    out = []
    for key, s in REGISTRY.items():
        env_val = os.environ.get(key)
        in_saved = key in saved
        in_vault = key in vaulted
        if env_val is not None:
            if in_saved and saved[key] == env_val:
                source = "config.env"
            elif in_vault or in_saved:
                # We can't distinguish "hydrated from vault" from "set by the
                # operator's environment" post-hoc; both read as env. What
                # matters: a DIFFERENT saved value would be shadowed.
                source = "vault" if in_vault else "env (shadows config.env)"
            else:
                source = "env"
        elif in_vault:
            source = "vault (applies at next start)"
        elif in_saved:
            source = "config.env (applies at next start)"
        else:
            source = None
        out.append({
            "key": key, "section": s.section, "label": s.label,
            "secret": s.secret, "set": bool(env_val or in_saved or in_vault),
            "source": source,
            "value": (None if s.secret or env_val is None else env_val),
            "consumers": list(s.consumers),
        })
    return out


def vault_ready() -> bool:
    """Whether secret settings can be stored (crypto + usable vault key)."""
    from . import vault
    try:
        vault._fernet()
        return True
    except Exception:
        return False

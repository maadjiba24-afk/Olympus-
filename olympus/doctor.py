"""`olympus doctor` — a readiness check + capability summary.

Answers the question a new user actually has after setup: "is this thing
actually configured, and what can it do right now?" Runs a series of cheap,
offline checks (no model calls unless asked) and prints a ✓/⚠/✗ summary, then a
one-glance capability readiness view. Reused at the end of `olympus setup` so
the wizard ends by showing the same readiness picture.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from . import config

OK, WARN, FAIL = "ok", "warn", "fail"
_ICON = {OK: "✓", WARN: "⚠", FAIL: "✗"}


@dataclass(frozen=True)
class Check:
    name: str
    status: str          # OK | WARN | FAIL
    detail: str

    def render(self) -> str:
        return f"  {_ICON.get(self.status, '•')} {self.name:22s} {self.detail}"


def _provider_checks() -> list[Check]:
    from . import firstrun
    out: list[Check] = []
    pool = config.ModelPool.from_env()
    primary = pool.primary()
    if firstrun.configured():
        model = primary.model or "(provider default)"
        out.append(Check("provider", OK, f"{primary.provider} / {model}"))
    else:
        out.append(Check("provider", FAIL,
                         "no key/endpoint — run `olympus setup`"))
    # Pool composition
    if pool.is_multi():
        out.append(Check("model pool", OK,
                         f"{len(pool.members)} models composed "
                         "(best-of-each per role)"))
    # Credential rotation
    keys = primary.all_keys()
    if len(keys) > 1:
        out.append(Check("key rotation", OK,
                         f"{len(keys)} keys — rotates on rate-limit/quota"))
    return out


def _sandbox_checks() -> list[Check]:
    from . import cmdguard, sandbox
    out: list[Check] = []
    be = sandbox.backend()
    out.append(Check("sandbox backend", OK, f"{be}"
                     + ("  (host-side; commands need approval)"
                        if be == "local" else "  (isolated container)")))
    m = cmdguard.mode()
    status = OK if m in ("enforce", "paranoid") else WARN
    note = {"enforce": "blocks catastrophic commands (fail-closed)",
            "paranoid": "blocks catastrophic + risky commands",
            "audit": "classifies but does NOT block — audit only",
            "off": "DISABLED — commands are not screened"}.get(m, m)
    out.append(Check("command gate", status, f"{m} — {note}"))
    # Workspace writable
    try:
        wd = sandbox.workdir()
        writable = os.access(wd, os.W_OK)
        out.append(Check("workspace", OK if writable else WARN,
                         f"{wd}" + ("" if writable else "  (not writable!)")))
    except Exception as err:
        out.append(Check("workspace", WARN, f"unavailable: {str(err)[:60]}"))
    return out


def _memory_checks() -> list[Check]:
    out: list[Check] = []
    try:
        config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        writable = os.access(config.MEMORY_DIR, os.W_OK)
        out.append(Check("memory dir", OK if writable else FAIL,
                         f"{config.MEMORY_DIR}"
                         + ("" if writable else "  (not writable!)")))
    except Exception as err:
        out.append(Check("memory dir", FAIL, f"{str(err)[:60]}"))
    return out


def _optional_checks() -> list[Check]:
    """Optional capabilities — WARN (not FAIL) when unconfigured; they're extras."""
    out: list[Check] = []
    media_key = (os.environ.get("OPENAI_API_KEY")
                 or os.environ.get("OLYMPUS_MEDIA_API_KEY"))
    out.append(Check("media / vision", OK if media_key else WARN,
                     "image gen, TTS, analyze_image ready" if media_key
                     else "no media key — image/vision tools will decline"))
    gateways = {
        "telegram": os.environ.get("TELEGRAM_BOT_TOKEN"),
        "discord": os.environ.get("DISCORD_WEBHOOK_URL")
        or os.environ.get("DISCORD_PUBLIC_KEY"),
        "slack": os.environ.get("SLACK_BOT_TOKEN"),
        "signal": os.environ.get("SIGNAL_CLI_REST_URL"),
    }
    live = [n for n, v in gateways.items() if v]
    out.append(Check("gateways", OK if live else WARN,
                     ", ".join(live) if live else "none connected (optional)"))
    # Web-search providers: DuckDuckGo always works keyless; report any keyed
    # or self-hosted providers the operator has configured so they're
    # discoverable (see docs/ODYSSEUS_TRACKING.md §7).
    from . import websearch
    providers = websearch.configured()
    extra = [p for p in providers if p != "ddg"]
    out.append(Check("web search", OK,
                     (f"{', '.join(providers)} (order)" if extra
                      else "DuckDuckGo (keyless; add SearXNG/Brave/Tavily/"
                           "Serper/PSE for better results)")))
    return out


def run_checks() -> list[Check]:
    checks: list[Check] = []
    checks += _provider_checks()
    checks += _memory_checks()
    checks += _sandbox_checks()
    checks += _optional_checks()
    return checks


def capability_summary() -> str:
    """A compact 'what I can do now' overview drawn from the live manifest —
    the launch capability dashboard, grouped and counted."""
    from . import capabilities
    from .specialists import SPECIALISTS
    lines = ["Capabilities"]
    try:
        m = capabilities.manifest()
        lines.append(f"  {m['agents']['count']} specialists · "
                     f"{m['tools']['count']} tools · "
                     f"{m['commands']['count']} commands")
    except Exception:
        pass
    roster = ", ".join(SPECIALISTS[k].name for k in sorted(SPECIALISTS))
    lines.append(f"  council: {roster}")
    return "\n".join(lines)


def render(checks: list[Check] | None = None, *, with_caps: bool = True) -> str:
    checks = checks if checks is not None else run_checks()
    fails = sum(1 for c in checks if c.status == FAIL)
    warns = sum(1 for c in checks if c.status == WARN)
    lines = ["OLYMPUS doctor — readiness check", ""]
    lines += [c.render() for c in checks]
    lines.append("")
    if fails:
        lines.append(f"  {_ICON[FAIL]} {fails} blocking issue(s) — "
                     "Olympus isn't ready. Fix the ✗ items above.")
    elif warns:
        lines.append(f"  {_ICON[OK]} Ready. {warns} optional item(s) "
                     "unconfigured (⚠) — extras you can add later.")
    else:
        lines.append(f"  {_ICON[OK]} All systems go.")
    if with_caps:
        lines += ["", capability_summary()]
    return "\n".join(lines)


def is_ready(checks: list[Check] | None = None) -> bool:
    checks = checks if checks is not None else run_checks()
    return not any(c.status == FAIL for c in checks)

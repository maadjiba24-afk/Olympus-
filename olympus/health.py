"""Runtime health / degraded-state reporting.

`doctor` answers "is this install *set up* correctly?" — a one-shot readiness
check. This answers a different question: "what is degraded *right now*?" — the
live state of the moving parts (model pool, memory store, gateway channels,
search, connected accounts, push targets), suitable for polling from a status
page, an uptime monitor, or `olympus health`.

Each component reports **ok** / **degraded** / **down**:
  - down     — broken or unusable; the feature won't work at all
  - degraded — running but impaired, or an optional piece is missing
  - ok       — healthy

`report()` returns a structured dict (JSON-friendly) with an overall `ok` that is
False only when something is genuinely **down** — an optional-but-absent piece
(no push channel, no connected Google account) is `degraded`, not a failure, so
a minimal install still reports healthy.
"""

from __future__ import annotations

import os
import time

OK, DEGRADED, DOWN = "ok", "degraded", "down"


def _models() -> dict:
    from . import config
    try:
        pool = config.ModelPool.from_env()
        primary = pool.primary()
        if not primary.usable():
            return {"status": DOWN, "detail": "no usable model configured"}
        n = len(pool.members)
        note = f"{n} model{'s' if n != 1 else ''} in pool"
        return {"status": OK, "detail": f"{primary.provider}/"
                f"{primary.model or 'default'} ({note})"}
    except Exception as err:
        return {"status": DOWN, "detail": f"pool error: {str(err)[:120]}"}


def _memory() -> dict:
    from . import config
    try:
        d = config.MEMORY_DIR
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".health_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return {"status": OK, "detail": f"writable at {d}"}
    except Exception as err:
        return {"status": DOWN, "detail": f"not writable: {str(err)[:120]}"}


def _gateway() -> dict:
    """Cross-process view of the running daemon (if any), from the status file."""
    from . import gateway
    try:
        st = gateway.read_status()
    except Exception:
        st = {}
    channels = (st or {}).get("channels") or {}
    if not channels:
        return {"status": OK, "detail": "no gateway daemon running",
                "channels": {}}
    bad = {n: c for n, c in channels.items()
           if str(c.get("status", "")).lower() not in ("running", "ok", "up")}
    if bad:
        return {"status": DEGRADED,
                "detail": "impaired: " + ", ".join(sorted(bad)),
                "channels": channels}
    return {"status": OK,
            "detail": f"{len(channels)} channel(s) running",
            "channels": channels}


def _search() -> dict:
    """DuckDuckGo always works keyless, so search is never 'down'; report any
    keyed providers as an upgrade."""
    keyed = [n for n, v in {
        "brave": os.environ.get("BRAVE_SEARCH_API_KEY"),
        "tavily": os.environ.get("TAVILY_API_KEY"),
        "serper": os.environ.get("SERPER_API_KEY"),
        "google-pse": os.environ.get("GOOGLE_PSE_KEY"),
        "searxng": os.environ.get("OLYMPUS_SEARXNG_URL"),
    }.items() if v]
    detail = ("keyed: " + ", ".join(keyed)) if keyed else "keyless (DuckDuckGo)"
    return {"status": OK, "detail": detail}


def _notifications() -> dict:
    live = [n for n, v in {
        "telegram": os.environ.get("TELEGRAM_BOT_TOKEN"),
        "discord": os.environ.get("DISCORD_WEBHOOK_URL")
        or os.environ.get("DISCORD_PUBLIC_KEY"),
        "slack": os.environ.get("SLACK_BOT_TOKEN"),
        "signal": os.environ.get("SIGNAL_CLI_REST_URL"),
        "ntfy": os.environ.get("NTFY_TOPIC"),
    }.items() if v]
    if live:
        return {"status": OK, "detail": ", ".join(live)}
    return {"status": DEGRADED, "detail": "no push channel configured"}


def _connections() -> dict:
    from . import google_oauth
    try:
        configured = google_oauth.configured()
    except Exception:
        configured = False
    if not configured:
        return {"status": DEGRADED,
                "detail": "no Google connection configured (email/calendar off)"}
    return {"status": OK, "detail": "Google OAuth configured"}


# Ordered component names; each maps to a module-level `_<name>()` probe,
# resolved by name at call time so probes stay individually patchable/testable.
_COMPONENTS = ("models", "memory", "gateway", "search", "notifications",
               "connections")


def report() -> dict:
    """Full structured health report. `ok` is False only if something is DOWN."""
    components = {}
    for name in _COMPONENTS:
        try:
            components[name] = globals()[f"_{name}"]()
        except Exception as err:            # a probe must never crash the report
            components[name] = {"status": DEGRADED,
                                "detail": f"probe error: {str(err)[:120]}"}
    down = [n for n, c in components.items() if c["status"] == DOWN]
    degraded = [n for n, c in components.items() if c["status"] == DEGRADED]
    return {"ok": not down, "down": down, "degraded": degraded,
            "components": components}


def render(rep: dict | None = None) -> str:
    rep = rep or report()
    icon = {OK: "✓", DEGRADED: "⚠", DOWN: "✗"}
    head = "Healthy" if rep["ok"] else "DEGRADED — " + ", ".join(rep["down"])
    lines = [f"Olympus health: {head}"]
    for name, c in rep["components"].items():
        lines.append(f"  {icon.get(c['status'], '•')} {name:14s} {c['detail']}")
    return "\n".join(lines)

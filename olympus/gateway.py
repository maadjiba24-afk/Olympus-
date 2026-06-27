"""Shared chat-gateway logic, reused by every messaging platform.

The Telegram gateway grew its own copy of the command routing (/help, /good,
/scan, …) and the per-user Olympus bot bookkeeping. As we add Discord, Slack,
and Signal, that logic lives here once so a platform module only has to handle
*transport* (auth, parse an incoming message, send an outgoing one).

A platform calls `reply_for(bots, user_key, text)` and gets back the list of
reply chunks to send. `bots` is the platform's own dict of per-user Olympus
instances (private memory + persisted history per user).
"""

from __future__ import annotations

from . import memory, orchestrator

CHUNK = 3500

HELP = (
    "⚡ OLYMPUS — your council of AI specialists.\n\n"
    "Ask anything: finance, marketing, code, security, social, coaching, "
    "scheduling, opportunities. Every answer is fact-checked before you see it.\n\n"
    "/scan — scan the web for opportunities now\n"
    "/watch <youtube-url> — watch a video and learn from it\n"
    "/queue <youtube-url> — queue a video for the autonomous loop\n"
    "/audit — Olympus audits and upgrades itself\n"
    "/good or /bad [comment] — rate the last answer\n"
    "/lang <language> — reply in your language\n"
    "/contribute on|off — share anonymized insights to improve Olympus\n"
    "/growth — see how Olympus has adapted to you over time\n"
)


def chunk(text: str, size: int = CHUNK) -> list[str]:
    text = (text or "").strip() or "(empty reply)"
    return [text[i:i + size] for i in range(0, len(text), size)]


def reply_for(bots: dict, user_key: str, text: str,
              prefix: str = "ol") -> list[str]:
    """Resolve a user's message to reply chunks, handling slash commands and
    otherwise running the full Zeus → Athena → Aletheia pipeline. `user_key`
    namespaces that user's private memory and persisted conversation."""
    text = (text or "").strip()
    if not text:
        return chunk("(say something and I'll help)")
    cmd, _, arg = text.partition(" ")
    cmd = cmd.lower()

    if cmd in ("/start", "/help"):
        return chunk(HELP)
    if cmd == "/scan":
        return chunk(orchestrator.opportunity_scan())
    if cmd == "/audit":
        return chunk(orchestrator.evolution_audit())
    if cmd == "/watch":
        if not arg:
            return chunk("Usage: /watch <youtube-url>")
        return chunk(orchestrator.watch_and_learn(arg))
    if cmd == "/queue":
        if not arg:
            return chunk("Usage: /queue <youtube-url>")
        memory.watchlist_add(arg)
        return chunk("Queued — the heartbeat will watch it on its next pass.")

    uid = f"{prefix}-{memory.safe_id(user_key)}"
    bot = bots.get(uid)
    if bot is None:
        bot = bots[uid] = orchestrator.Olympus(user=uid, conversation_id=uid)

    if cmd in ("/good", "/bad"):
        return chunk(bot.feedback("up" if cmd == "/good" else "down", arg))
    if cmd == "/lang":
        if not arg:
            return chunk("Usage: /lang <language> (e.g. /lang French, /lang auto)")
        return chunk(bot.set_language(arg))
    if cmd == "/contribute":
        on = arg.strip().lower() in ("on", "yes", "true", "1", "enable")
        return chunk(bot.set_contribute(on))
    if cmd == "/growth":
        from . import companion
        return chunk(companion.summary(uid))

    return chunk(bot.ask(text))

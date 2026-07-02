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

import queue
import threading
import traceback
from collections import OrderedDict

from . import memory, orchestrator, steering

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
    "/steer <note> — nudge the task that's currently running\n"
    "/undo [N] — remove the last N exchanges from the conversation\n"
    "/goal <text> [:: done-means] — set a standing goal the heartbeat works\n"
    "/goal list · /goal drop <id> — review or retire standing goals\n"
    "/lang <language> — reply in your language\n"
    "/contribute on|off — share anonymized insights to improve Olympus\n"
    "/growth — see how Olympus has adapted to you over time\n"
)


def chunk(text: str, size: int = CHUNK) -> list[str]:
    text = (text or "").strip() or "(empty reply)"
    return [text[i:i + size] for i in range(0, len(text), size)]


# Every chat platform that exposes an ambient notify() for proactive pushes.
NOTIFY_CHANNELS = ("telegram", "discord", "slack", "signal")


def notify_all(text: str) -> list[str]:
    """Push a proactive message to EVERY configured chat channel and return the
    channels that accepted it. Each gateway's notify() is a no-op returning
    False when that platform isn't configured, so this fans out only where
    credentials exist — the heartbeat/backup no longer reach Telegram alone."""
    from . import discord, signal as signal_gw, slack, telegram
    fns = {"telegram": telegram.notify, "discord": discord.notify,
           "slack": slack.notify, "signal": signal_gw.notify}
    delivered = []
    for name in NOTIFY_CHANNELS:
        try:
            if fns[name](text):
                delivered.append(name)
        except Exception:
            pass
    return delivered


def try_steer(user_key: str, text: str, prefix: str = "ol") -> list[str] | None:
    """Fast-path `/steer`: handle it synchronously (BEFORE the per-user serial
    worker queue) so the note reaches a pipeline that is already mid-run —
    queued behind the running task it would only be seen after the run ends.
    Returns reply chunks when the message was a /steer, else None."""
    cmd, _, arg = (text or "").strip().partition(" ")
    if cmd.lower() != "/steer":
        return None
    if not arg.strip():
        return chunk("Usage: /steer <note> — nudge the task that's running")
    uid = f"{prefix}-{memory.safe_id(user_key)}"
    if steering.put(uid, arg):
        return chunk("Noted — the running task will see this after its "
                     "next tool call.")
    return chunk("Steering queue is full; note dropped.")


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
    if cmd == "/steer":
        # Fallback for transports that didn't fast-path it; same behavior.
        return try_steer(user_key, text, prefix)  # type: ignore[return-value]
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
    if cmd == "/goal":
        from . import goals
        sub, _, rest = arg.strip().partition(" ")
        if not arg.strip() or sub == "list":
            return chunk(goals.summary(uid))
        if sub == "drop":
            return chunk(goals.set_status(rest.strip(), "dropped"))
        if sub == "done":
            return chunk(goals.set_status(rest.strip(), "done",
                                          evidence="closed manually"))
        text, _, contract = arg.partition("::")
        return chunk(goals.add(uid, text.strip(), contract.strip()))

    bot = bots.get(uid)
    if bot is None:
        bot = bots[uid] = orchestrator.Olympus(user=uid, conversation_id=uid)

    if cmd == "/undo":
        try:
            n = int(arg) if arg.strip() else 1
        except ValueError:
            return chunk("Usage: /undo [N]")
        return chunk(bot.undo(n))
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


# --- background dispatch for webhook gateways -----------------------------
#
# Webhook platforms (Slack, Discord) require a fast acknowledgement — Slack
# retries an event if the endpoint doesn't answer within ~3s; Discord marks an
# interaction failed on the same window. The Olympus pipeline is far slower than
# that, so the HTTP handler must ack immediately and run the pipeline in the
# background. This shared dispatcher gives every webhook gateway the same
# proven shape the WhatsApp gateway uses: one serial worker per user (ordered
# within a conversation, concurrent across users) plus event de-duplication so a
# platform retry is dropped instead of answered twice.


class _Worker(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.q: queue.Queue = queue.Queue()

    def run(self) -> None:
        while True:
            fn = self.q.get()
            try:
                fn()
            except Exception:
                traceback.print_exc()


class Dispatcher:
    """Per-key serial background workers + event de-dup for webhook gateways."""

    def __init__(self, max_seen: int = 2000) -> None:
        self._workers: dict[str, _Worker] = {}
        # Ordered so we can evict the OLDEST id when full (a bounded FIFO),
        # rather than clearing the whole set — which would let a retry arriving
        # right after the cap be treated as new and processed twice.
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self._lock = threading.Lock()
        self._max_seen = max_seen

    def seen(self, event_id: str | None) -> bool:
        """Record `event_id` and report whether it was already seen (a retry).
        Empty/None ids are never treated as duplicates (nothing to key on)."""
        if not event_id:
            return False
        with self._lock:
            if event_id in self._seen:
                return True
            self._seen[event_id] = None
            if len(self._seen) > self._max_seen:
                self._seen.popitem(last=False)      # drop only the oldest id
            return False

    def submit(self, key: str, fn) -> None:
        """Run `fn()` on the background worker serial to `key`."""
        with self._lock:
            worker = self._workers.get(key)
            if worker is None:
                worker = self._workers[key] = _Worker()
                worker.start()
        worker.q.put(fn)

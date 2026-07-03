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

import os
import queue
import threading
import traceback
from collections import OrderedDict

from . import config, memory, orchestrator, steering

CHUNK = 3500

# Idle gateway sessions are distilled-and-cleared after this many seconds so a
# long-running gateway process doesn't accumulate unbounded per-user history
# (cost + context drift). 0 disables the sweep. The heartbeat calls
# reset_idle_sessions() on a cadence; the distilled state is preserved.
GATEWAY_SESSION_MAX_AGE = int(
    os.environ.get("OLYMPUS_GATEWAY_SESSION_MAX_AGE", str(6 * 3600)))

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
    "/goal list · /goal drop <id> · /goal wait <id> <pid> — manage goals\n"
    "/heartbeat add <every> <prompt> — a periodic check that only pings you "
    "when something needs attention (list · drop <id>)\n"
    "/onexit <pid> <prompt> — run a task once when that process exits\n"
    "/learn <url or workflow> — distill a reusable skill from it\n"
    "/journey — the timeline of everything Olympus has learned\n"
    "/wiki [show <page>] — the concept pages Olympus maintains about "
    "your world\n"
    "/moa <question> — one-shot mixture-of-agents across the model pool\n"
    "/reasoning — how the last answer was produced (pipeline trace)\n"
    "/lang <language> — reply in your language\n"
    "/contribute on|off — share anonymized insights to improve Olympus\n"
    "/growth — see how Olympus has adapted to you over time\n"
    "/approvals — commands held for your approval\n"
    "/approve <id> · /deny <id> — decide a held command from chat\n"
    "/usage — tokens and cost for this session and today\n"
    "/model [name|auto] — pin this conversation to a model (opus/sonnet/gpt…)\n"
    "/reset — start fresh (keeps a distilled summary of what we covered)\n"
)


def chunk(text: str, size: int = CHUNK) -> list[str]:
    text = (text or "").strip() or "(empty reply)"
    return [text[i:i + size] for i in range(0, len(text), size)]


# Every chat platform that exposes an ambient notify() for proactive pushes.
NOTIFY_CHANNELS = ("telegram", "discord", "slack", "signal")


# --- in-flight work journal (session auto-resume) --------------------------
#
# Conversation *history* already survives a restart (persisted per turn); the
# request being processed WHEN the gateway died did not — it vanished
# silently. Each long-poll gateway marks the message it is working on and
# clears the mark on completion; on boot it takes the stale marks back and
# re-runs them, telling the user. An entry is retried at most once (a message
# that kills the gateway twice must not become a crash loop).

_INFLIGHT_MAX_ATTEMPTS = 2
_INFLIGHT_MAX_AGE = 24 * 3600


def _inflight_dir():
    d = config.MEMORY_DIR / "inflight"
    d.mkdir(parents=True, exist_ok=True)
    return d


def inflight_mark(uid: str, deliver_key, text: str) -> None:
    """Record that `text` is being processed for `uid` (best-effort).
    `deliver_key` is whatever the platform needs to send a message back
    (chat id, sender number)."""
    import json as _json
    import time as _time
    try:
        path = _inflight_dir() / f"{memory.safe_id(uid)}.json"
        attempts = 1
        if path.exists():
            try:
                prior = _json.loads(path.read_text(encoding="utf-8"))
                if prior.get("text") == text:
                    attempts = int(prior.get("attempts", 1)) + 1
            except (ValueError, OSError):
                pass
        path.write_text(_json.dumps(
            {"uid": uid, "key": deliver_key, "text": text,
             "attempts": attempts, "ts": _time.time()}), encoding="utf-8")
    except OSError:
        pass


def inflight_clear(uid: str) -> None:
    try:
        (_inflight_dir() / f"{memory.safe_id(uid)}.json").unlink(
            missing_ok=True)
    except OSError:
        pass


def inflight_take(prefix: str) -> list[dict]:
    """Pop and return the resumable entries whose uid starts with `prefix`.
    Entries that are too old or already retried are dropped, not returned."""
    import json as _json
    import time as _time
    out = []
    for path in sorted(_inflight_dir().glob("*.json")):
        try:
            entry = _json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            path.unlink(missing_ok=True)
            continue
        if not str(entry.get("uid", "")).startswith(prefix):
            continue
        path.unlink(missing_ok=True)      # taken either way
        fresh = _time.time() - float(entry.get("ts", 0)) <= _INFLIGHT_MAX_AGE
        if fresh and int(entry.get("attempts", 1)) < _INFLIGHT_MAX_ATTEMPTS \
                and entry.get("text"):
            out.append(entry)
    return out


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
              prefix: str = "ol", uid: str | None = None) -> list[str]:
    """Resolve a user's message to reply chunks, handling slash commands and
    otherwise running the full Zeus → Athena → Aletheia pipeline. `user_key`
    namespaces that user's private memory and persisted conversation. A
    transport may pass an explicit `uid` when its historical session keys
    predate this router (Telegram's raw `tg-<chat id>`, where safe_id would
    mangle negative group ids)."""
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

    uid = uid or f"{prefix}-{memory.safe_id(user_key)}"
    if cmd in ("/approvals", "/pending", "/approve", "/deny", "/reject"):
        from . import approvals
        handled = approvals.handle_command(uid, cmd, arg)
        if handled is not None:
            return chunk(handled)
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
        if sub == "wait":
            parts = rest.split()
            if len(parts) != 2 or not parts[1].isdigit():
                return chunk("Usage: /goal wait <goal id> <pid>")
            return chunk(goals.wait_on(parts[0], int(parts[1])))
        text, _, contract = arg.partition("::")
        return chunk(goals.add(uid, text.strip(), contract.strip()))
    if cmd == "/profile":
        # View-only: a conversation can see its boundary, never widen it
        # (assignment is operator-side via `olympus restrict`).
        from . import capprofile
        return chunk(capprofile.summary(uid))
    if cmd == "/onexit":
        from . import scheduler
        pid_str, _, prompt = arg.strip().partition(" ")
        if not pid_str.isdigit() or not prompt.strip():
            return chunk("Usage: /onexit <pid> <what to do when it exits>\n"
                         "e.g. /onexit 4242 summarize the training log")
        # Deliver the wake-up result back to the requesting channel's owner
        # chat (the closest thing a scheduled run has to "this conversation").
        channel = {"tg": "telegram", "dc": "discord",
                   "sl": "slack", "sg": "signal"}.get(uid.split("-", 1)[0], "")
        try:
            job = scheduler.add_on_exit(
                f"onexit-{pid_str}", int(pid_str), prompt.strip(), user=uid,
                deliver_to=channel)
        except ValueError as err:
            return chunk(str(err))
        return chunk(f"⏳ Watching pid {job.watch_pid} — when it exits I'll "
                     f"run: {prompt.strip()}")
    if cmd == "/heartbeat":
        from . import agentbeat
        sub, _, rest = arg.strip().partition(" ")
        if not arg.strip() or sub == "list":
            return chunk(agentbeat.summary(uid))
        if sub == "drop":
            return chunk("Dropped." if agentbeat.remove(uid, rest.strip())
                         else "No heartbeat with that id.")
        if sub == "add":
            every, _, prompt = rest.partition(" ")
            if not prompt.strip():
                return chunk("Usage: /heartbeat add <every> <what to check>\n"
                             "e.g. /heartbeat add 2h anything urgent in my goals?")
            beat = agentbeat.add(uid, every, prompt)
            return chunk(f"💓 Heartbeat #{beat.id} set — every "
                         f"{beat.every // 60} minutes I'll check: "
                         f"{beat.prompt}\nI'll only message you when "
                         f"something needs attention.")
        return chunk("Usage: /heartbeat [list] · add <every> <prompt> · "
                     "drop <id>")
    if cmd == "/learn":
        from . import learn
        # Chat users must never read server paths — URLs/workflows only.
        return chunk(learn.distill(arg, allow_paths=False))
    if cmd == "/wiki":
        from . import wiki
        sub, _, ref = arg.strip().partition(" ")
        if sub == "show" and ref.strip():
            return chunk(wiki.read(uid, ref.strip()))
        return chunk(wiki.summary(uid))
    if cmd == "/journey":
        from . import journey
        sub, _, ref = arg.strip().partition(" ")
        if sub == "show":
            return chunk(journey.show(ref.strip(), uid))
        if sub == "rm":
            return chunk(journey.remove(ref.strip(), uid))
        return chunk(journey.timeline(uid))
    if cmd == "/moa":
        from . import moa
        if not arg.strip():
            return chunk("Usage: /moa <question>")
        try:
            return chunk(moa.one_shot(arg.strip()))
        except Exception as err:
            return chunk(f"moa failed: {err}")

    bot = bots.get(uid)
    if bot is None:
        bot = bots[uid] = orchestrator.Olympus(user=uid, conversation_id=uid)
    import time
    bot._last_active = time.time()   # for idle-session reset sweeps

    if cmd == "/undo":
        try:
            n = int(arg) if arg.strip() else 1
        except ValueError:
            return chunk("Usage: /undo [N]")
        return chunk(bot.undo(n))
    if cmd == "/reasoning":
        from . import tui
        return chunk(tui.reasoning_view(bot))
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
    if cmd == "/reset":
        return chunk(bot.reset())

    reply = bot.ask(text)
    # A turn can leave irreversible actions held by the approval spine; tell
    # the person in-channel so the decision happens where the ask happened.
    from . import approvals
    return chunk(reply + approvals.footer(uid))


def reset_idle_sessions(bots: dict, max_age_secs: int | None = None) -> int:
    """Distill-and-clear gateway sessions that have gone quiet, so a long-lived
    gateway process doesn't carry unbounded per-user history (cost + drift).
    Returns how many sessions were reset. Called on a cadence by the heartbeat;
    the distilled state is preserved (see Olympus.reset)."""
    import time
    max_age = max_age_secs if max_age_secs is not None else GATEWAY_SESSION_MAX_AGE
    if max_age <= 0:
        return 0
    now = time.time()
    reset = 0
    for uid, bot in list(bots.items()):
        last = getattr(bot, "_last_active", None)
        if last is not None and (now - last) >= max_age and bot.history:
            try:
                bot.reset()
                bot._last_active = now   # distilled now → fresh; don't re-sweep
                reset += 1
            except Exception:
                pass
    return reset


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

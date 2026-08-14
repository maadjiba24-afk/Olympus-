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

import json
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
    "/onchange <cmd> :: <prompt> — run a task whenever a command's output "
    "changes\n"
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
    "/fast [on|off|auto] — latency mode; auto decides per message\n"
    "/reset — start fresh (keeps a distilled summary of what we covered)\n"
)


def chunk(text: str, size: int = CHUNK) -> list[str]:
    text = (text or "").strip() or "(empty reply)"
    return [text[i:i + size] for i in range(0, len(text), size)]


# Every chat platform that exposes an ambient notify() for proactive pushes.
NOTIFY_CHANNELS = ("telegram", "discord", "slack", "signal", "ntfy", "matrix",
                   "mattermost", "googlechat", "sms")


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
    credentials exist — the heartbeat/backup no longer reach Telegram alone.

    Egress Phase C (docs/DESIGN_BOUNDARY_LAYER.md): a proactive broadcast is a
    C0-only channel — a payload carrying the user's PII or a stored secret (e.g.
    a heartbeat that echoed private working state to a Telegram group) is a leak.
    When the egress guard is enabled, `guard(..., BROADCAST)` HOLDs such content
    and it is not broadcast (returns [] — delivered nowhere)."""
    from . import config, egress
    if config.egress_guard_enabled():
        d = egress.guard(text, egress.ChannelKind.BROADCAST, user="shared")
        if d.verdict is egress.Verdict.HOLD:
            return []
    from . import (discord, googlechat, matrix, mattermost, ntfy,
                   signal as signal_gw, slack, sms, telegram)
    fns = {"telegram": telegram.notify, "discord": discord.notify,
           "slack": slack.notify, "signal": signal_gw.notify,
           "ntfy": ntfy.notify, "matrix": matrix.notify,
           "mattermost": mattermost.notify, "googlechat": googlechat.notify,
           "sms": sms.notify}
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


def _set_fast(arg: str) -> str:
    """Handle `/fast [on|off|auto]`. Instance-wide (fast mode is a process
    setting, like it is in the CLI/TUI), persisted so it survives a restart.
    'auto' lets Olympus decide per message: quick asks stay fast, substantial
    ones get the full council + review."""
    val = (arg or "").strip().lower()
    if not val:
        cur = config.fast_setting()
        note = {"on": "every reply skips the optional review for lower latency",
                "off": "every reply gets the full council + review",
                "auto": "I decide per message — quick asks fast, hard ones full"}
        return (f"Fast mode is **{cur}** — {note[cur]}.\n"
                "Change it with /fast on · /fast off · /fast auto")
    mapping = {"on": "1", "1": "1", "yes": "1", "true": "1",
               "off": "0", "0": "0", "no": "0", "false": "0",
               "auto": "auto"}
    if val not in mapping:
        return "Usage: /fast [on|off|auto]"
    stored = mapping[val]
    os.environ["OLYMPUS_FAST"] = stored
    try:
        from . import firstrun
        firstrun.save_env_value("OLYMPUS_FAST", stored)
    except Exception:
        pass                        # env is set for this process regardless
    setting = config.fast_setting()
    blurb = {"on": "Fast mode ON — I'll skip the optional review round-trip.",
             "off": "Fast mode OFF — every reply gets the full council + review.",
             "auto": "Fast mode AUTO — quick asks stay fast; substantial ones "
                     "get the full council + review."}
    return blurb[setting]


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
        from . import onboarding
        wid = uid or f"{prefix}-{memory.safe_id(user_key)}"
        if cmd == "/start" and onboarding.is_new(wid):
            onboarding.mark_seen(wid)
            return chunk(onboarding.welcome() + "\n\n" + HELP)
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
    if cmd == "/usage":
        from . import prefs, usage
        sub = arg.strip().lower()
        if sub in ("on", "off"):
            prefs.set(uid, "usage_footer", True if sub == "on" else None)
            return chunk("Usage footer on — every reply ends with its "
                         "token/cost line." if sub == "on"
                         else "Usage footer off.")
        session = usage.session_totals(uid)
        lines = [
            f"This session: {session['calls']} model call(s), "
            f"{usage._fmt_tokens(session['in'])} in / "
            f"{usage._fmt_tokens(session['out'])} out, "
            f"~${session['cost']:.4f}",
            f"Today (all users): ${usage.today_spend():.2f}",
        ]
        budget = usage.budget_status()
        if budget.get("budget"):
            lines.append(f"Daily budget: ${budget['budget']:.2f} "
                         f"({budget.get('status', '')})")
        lines.append("Per-reply footers: /usage on · /usage off")
        return chunk("\n".join(lines))
    if cmd == "/model":
        from . import modelpin
        if not arg.strip():
            return chunk(modelpin.status(uid))
        reply = modelpin.set_pin(uid, arg)
        # The session's pool is chosen at construction; rebuild it so the
        # pin (or unpin) takes effect on the next message, not next restart.
        bots.pop(uid, None)
        return chunk(reply)
    if cmd == "/fast":
        return chunk(_set_fast(arg))
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
    if cmd == "/onchange":
        from . import scheduler
        watch, _, prompt = arg.strip().partition("::")
        if not watch.strip() or not prompt.strip():
            return chunk("Usage: /onchange <command> :: <what to do on change>\n"
                         "e.g. /onchange git -C ~/repo log -1 :: summarize the "
                         "new commit")
        channel = {"tg": "telegram", "dc": "discord",
                   "sl": "slack", "sg": "signal"}.get(uid.split("-", 1)[0], "")
        try:
            job = scheduler.add_on_change(
                f"onchange-{memory.safe_id(watch)[:16]}", watch.strip(),
                prompt.strip(), user=uid, deliver_to=channel)
        except ValueError as err:
            return chunk(str(err))
        return chunk(f"👁 Watching `{job.watch_cmd}` — when its output changes "
                     f"I'll run: {prompt.strip()}")
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

    from . import prefs, usage
    before = usage.session_totals(uid)
    reply = bot.ask(text)
    # A turn can leave irreversible actions held by the approval spine; tell
    # the person in-channel so the decision happens where the ask happened.
    from . import approvals
    reply += approvals.footer(uid)
    # Opt-in per-reply cost accounting (/usage on).
    if prefs.get(uid, "usage_footer"):
        spent = usage.delta(before, usage.session_totals(uid))
        reply += "\n\n" + usage.footer(spent, uid)
    return chunk(reply)


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


# --- unified gateway daemon (the surveyed gateway-style: one process, all channels) ----
#
# Historically each channel was its own `olympus <channel>` process. This runs
# every CONFIGURED channel together in one long-lived daemon, each in its own
# thread with error isolation, so a single deployment is reachable everywhere
# the user already chats. Channel detection is env-based (same signals the
# individual commands and `olympus doctor` use).

# HTTP-serving channels need distinct ports; polling channels take none.
_CHANNEL_PORTS = {"whatsapp": 8485, "discord": 8486, "slack": 8487,
                  "webhook": 8488}


def _channel_registry() -> "dict[str, tuple]":
    """name -> (is_configured: ()->bool, start: ()->None). Imported lazily so
    importing gateway never drags in every channel module."""
    from . import (discord, email_gateway, gmail, signal as signal_gw, slack,
                   telegram, webhook_gateway, whatsapp)

    def env(k: str) -> bool:
        return bool(os.environ.get(k))

    return {
        "telegram": (lambda: env("TELEGRAM_BOT_TOKEN"), telegram.run_bot),
        "signal": (lambda: env("SIGNAL_CLI_REST_URL"), signal_gw.run_bot),
        "email": (gmail.configured, email_gateway.run),
        "discord": (lambda: env("DISCORD_PUBLIC_KEY"),
                    lambda: discord.run_server(port=_CHANNEL_PORTS["discord"])),
        "slack": (lambda: env("SLACK_SIGNING_SECRET"),
                  lambda: slack.run_server(port=_CHANNEL_PORTS["slack"])),
        "whatsapp": (lambda: env("WHATSAPP_VERIFY_TOKEN"),
                     lambda: whatsapp.run_server(
                         port=_CHANNEL_PORTS["whatsapp"])),
        "webhook": (lambda: env("OLYMPUS_WEBHOOK_SECRET"),
                    lambda: webhook_gateway.run_server(
                        port=_CHANNEL_PORTS["webhook"])),
    }


def _is_configured(pred) -> bool:
    try:
        return bool(pred())
    except Exception:
        return False


def configured_channels(registry: "dict | None" = None) -> "list[str]":
    """The channels whose credentials/config are present, in a stable order."""
    reg = registry or _channel_registry()
    return [name for name, (is_cfg, _start) in reg.items()
            if _is_configured(is_cfg)]


def _max_restarts() -> int:
    """How many times a channel may be auto-restarted before the supervisor
    gives up on it (0 = never restart; -1 = unbounded). Default 20."""
    try:
        return int(os.environ.get("OLYMPUS_GATEWAY_MAX_RESTARTS", "20"))
    except ValueError:
        return 20


class _Supervisor:
    """Keeps configured channels alive: each runs in its own thread, and when
    one exits or crashes it is restarted with exponential backoff — so a
    transient upstream blip or a dropped long-poll doesn't silently take a
    channel offline for the life of the daemon. Per-channel liveness is tracked
    for `channel_health()`."""

    def __init__(self, report=print, *, max_restarts: int | None = None,
                 base_delay: float = 1.0, max_delay: float = 60.0,
                 persist: bool = False):
        self.report = report
        self.max_restarts = _max_restarts() if max_restarts is None \
            else max_restarts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.state: dict[str, dict] = {}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._persist = persist            # write status to disk (the CLI daemon)
        self._lock = threading.Lock()
        self._started_at = 0.0

    def _write_status(self) -> None:
        """Publish liveness to a status file so `olympus gateway status` (a
        SEPARATE process) can read a running daemon's health. Best-effort.
        Several supervisor threads call this concurrently, so the state is
        snapshotted under the lock and each write uses a UNIQUE temp file —
        the final rename is atomic, so a reader never sees a partial file and
        writers never clobber each other's temp."""
        if not self._persist:
            return
        import threading as _t
        import time
        with self._lock:
            snapshot = {k: dict(v) for k, v in self.state.items()}
            started = self._started_at
        try:
            path = _status_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({
                "pid": os.getpid(),
                "started_at": started,
                "heartbeat": time.time(),
                "channels": snapshot,
            }, indent=2)
            tmp = path.with_name(f"{path.name}.{os.getpid()}.{_t.get_ident()}.tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(path)
        except Exception:
            pass

    def _set_state(self, name: str, **fields) -> None:
        with self._lock:
            self.state[name] = fields
        self._write_status()

    def _supervise(self, name: str, start) -> None:
        restarts = 0
        while not self._stop.is_set():
            self._set_state(name, status="running", restarts=restarts,
                            last_error="")
            self.report(f"▶ {name} channel started"
                        + (f" (:{_CHANNEL_PORTS[name]})"
                           if name in _CHANNEL_PORTS else ""))
            try:
                start()                    # blocks until the channel loop ends
                reason = "exited"
            except Exception as err:
                reason = str(err)[:160] or type(err).__name__
            if self._stop.is_set():
                break
            restarts += 1
            if self.max_restarts >= 0 and restarts > self.max_restarts:
                self._set_state(name, status="failed", restarts=restarts - 1,
                                last_error=reason)
                self.report(f"✗ {name} channel gave up after "
                            f"{restarts - 1} restart(s): {reason}")
                return
            delay = min(self.max_delay,
                        self.base_delay * (2 ** (restarts - 1)))
            self._set_state(name, status="restarting", restarts=restarts,
                            last_error=reason)
            self.report(f"↻ {name} channel {reason}; restart #{restarts} "
                        f"in {delay:.0f}s")
            self._stop.wait(delay)         # interruptible backoff
        self._set_state(name, status="stopped", restarts=restarts,
                        last_error="")

    def start(self, names: "list[str]", reg: dict) -> None:
        import time
        self._started_at = time.time()
        for name in names:
            _is_cfg, start = reg[name]
            t = threading.Thread(target=self._supervise, args=(name, start),
                                 name=f"gw-{name}", daemon=True)
            t.start()
            self._threads.append(t)
        self._write_status()

    def stop(self) -> None:
        self._stop.set()

    def wait(self) -> None:
        """Block until interrupted, refreshing the status heartbeat so a stale
        file is distinguishable from a live one, then signal channels to stop."""
        try:
            while not self._stop.is_set():
                self._write_status()          # heartbeat
                self._stop.wait(30)
        except KeyboardInterrupt:
            pass
        self.stop()
        self._write_status()


def _status_path() -> "config.Path":  # type: ignore[name-defined]
    return config.MEMORY_DIR / "gateway_status.json"


def read_status(stale_after: float = 90.0) -> dict:
    """Read the running daemon's status file (written by the CLI daemon), from
    ANY process. Adds `running` (heartbeat fresh AND pid alive) and `stale`.
    Returns {"running": False, "reason": ...} when no live daemon is found."""
    import time
    path = _status_path()
    if not path.exists():
        return {"running": False, "reason": "no gateway daemon status file"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"running": False, "reason": "unreadable status file"}
    age = time.time() - float(data.get("heartbeat", 0))
    pid = int(data.get("pid", 0))
    alive = _pid_alive(pid)
    data["age_secs"] = round(age, 1)
    data["stale"] = age > stale_after
    data["running"] = alive and not data["stale"]
    if not alive:
        data["reason"] = f"pid {pid} not running"
    elif data["stale"]:
        data["reason"] = f"heartbeat {age:.0f}s old (> {stale_after:.0f}s)"
    return data


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False

    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        error_access_denied = 5

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if handle:
            kernel32.CloseHandle(handle)
            return True

        return ctypes.get_last_error() == error_access_denied

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def channel_health(supervisor: "_Supervisor | None" = None) -> dict:
    """Per-channel liveness of the currently-running daemon (status, restart
    count, last error). Empty when no daemon is running in this process."""
    sup = supervisor if supervisor is not None else _RUNNING
    return dict(sup.state) if sup is not None else {}


_RUNNING: "_Supervisor | None" = None       # the daemon running in this process


def run_all(only: "list[str] | None" = None, registry: "dict | None" = None,
            block: bool = True, report=print,
            supervisor: "_Supervisor | None" = None) -> "list[str]":
    """Run every configured channel together under a supervisor that
    auto-restarts a channel that exits or crashes (exponential backoff, capped
    by OLYMPUS_GATEWAY_MAX_RESTARTS). Returns the channels started. With
    block=True (the CLI path) runs until interrupted; block=False returns after
    launching (tests / embedding)."""
    global _RUNNING
    reg = registry or _channel_registry()
    names = configured_channels(reg)
    if only:
        want = {c.strip().lower() for c in only}
        for u in want - set(reg):
            report(f"⚠ unknown channel '{u}' — known: {', '.join(reg)}")
        names = [n for n in names if n in want]
    if not names:
        report("No channels configured. Set a channel's env vars (e.g. "
               "TELEGRAM_BOT_TOKEN) — see docs/GATEWAY.md — then rerun "
               "`olympus gateway`.")
        return []

    sup = supervisor or _Supervisor(report=report, persist=True)
    sup.start(names, reg)
    _RUNNING = sup
    report(f"Gateway daemon live — {len(names)} channel(s): "
           f"{', '.join(names)}. Auto-restart on crash; Ctrl-C to stop.")
    if block:
        sup.wait()
        report("Shutting down gateway daemon.")
        _RUNNING = None
    return names

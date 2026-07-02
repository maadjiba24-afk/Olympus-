"""Olympus Telegram gateway — zero dependencies, raw Bot API over urllib.

Setup:
  1. Create a bot with @BotFather, copy the token.
  2. export TELEGRAM_BOT_TOKEN=123456:ABC...
  3. python -m olympus telegram

Optional:
  TELEGRAM_ALLOWED_CHAT_IDS=12345,67890   restrict who may talk to the bot
  TELEGRAM_NOTIFY_CHAT_ID=12345           heartbeat pushes reports here

Commands: /start /scan /audit /watch <url> /queue <url> — anything else goes
through the full Zeus → Athena → Aletheia pipeline.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import traceback
import urllib.error
import urllib.request

from . import memory, orchestrator

CHUNK = 4000  # Telegram message hard limit is 4096 chars

HELP = (
    "⚡ OLYMPUS — your council of AI specialists.\n\n"
    "Just ask anything: finance, marketing, code, security, social media, "
    "coaching, scheduling, opportunities. Every answer is fact-checked by "
    "the hallucination controller before you see it.\n\n"
    "Commands:\n"
    "/scan — scan the web for opportunities & world events now\n"
    "/watch <youtube-url> — watch a video and learn from it\n"
    "/queue <youtube-url> — queue a video for the autonomous loop\n"
    "/audit — Olympus audits and upgrades itself\n"
    "/good or /bad [comment] — rate the last answer (Olympus learns from it)\n"
    "/lang <language> — reply in your language (e.g. /lang Spanish, /lang auto)\n"
    "/contribute on|off — share anonymized insights to improve Olympus for all\n"
)


def _token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN (get one from @BotFather).")
    return token


def _call(token: str, method: str, **params) -> dict:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=70) as resp:
        return json.loads(resp.read())


def _send(token: str, chat_id: int | str, text: str) -> None:
    text = text.strip() or "(empty reply)"
    for i in range(0, len(text), CHUNK):
        _call(token, "sendMessage", chat_id=chat_id, text=text[i : i + CHUNK])


def notify(text: str) -> bool:
    """Push a message to the owner chat if configured (used by the heartbeat).

    Best-effort: returns False instead of raising when unconfigured/offline.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_NOTIFY_CHAT_ID")
    if not token or not chat_id:
        return False
    try:
        _send(token, chat_id, text)
        return True
    except Exception as err:
        import logging
        logging.getLogger("olympus.telegram").warning("notify failed: %s", err)
        return False


def _allowed(chat_id: int) -> bool:
    raw = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    if not raw:
        return True  # open to everyone unless restricted
    return str(chat_id) in {x.strip() for x in raw.split(",")}


def _handle(token: str, bots: dict[int, orchestrator.Olympus],
            chat_id: int, text: str) -> None:
    if not _allowed(chat_id):
        _send(token, chat_id, "This Olympus instance is private.")
        return

    cmd, _, arg = text.strip().partition(" ")
    cmd = cmd.lower()

    if cmd in ("/start", "/help"):
        _send(token, chat_id, HELP)
        return
    if cmd == "/scan":
        _send(token, chat_id, "🌐 Argus is scanning the world — give me a few minutes...")
        _send(token, chat_id, orchestrator.opportunity_scan())
        return
    if cmd == "/audit":
        _send(token, chat_id, "🔧 Prometheus is auditing Olympus...")
        _send(token, chat_id, orchestrator.evolution_audit())
        return
    if cmd == "/watch":
        if not arg:
            _send(token, chat_id, "Usage: /watch <youtube-url>")
            return
        _send(token, chat_id, "🎥 Mnemosyne is watching the video...")
        _send(token, chat_id, orchestrator.watch_and_learn(arg))
        return
    if cmd == "/queue":
        if not arg:
            _send(token, chat_id, "Usage: /queue <youtube-url>")
            return
        memory.watchlist_add(arg)
        _send(token, chat_id, "Queued — the heartbeat will watch it on its next pass.")
        return

    # Per-chat identity: private memory namespace + history that survives
    # restarts (conversation persisted to disk under the chat id).
    bot = bots.setdefault(chat_id, orchestrator.Olympus(
        user=f"tg-{chat_id}", conversation_id=f"tg-{chat_id}"))

    if cmd == "/undo":
        try:
            n = int(arg) if arg.strip() else 1
        except ValueError:
            _send(token, chat_id, "Usage: /undo [N]")
            return
        _send(token, chat_id, bot.undo(n))
        return
    if cmd in ("/good", "/bad"):
        _send(token, chat_id,
              bot.feedback("up" if cmd == "/good" else "down", arg))
        return
    if cmd == "/lang":
        if not arg:
            _send(token, chat_id, "Usage: /lang <language>  (e.g. /lang French, "
                                  "/lang auto)")
            return
        _send(token, chat_id, bot.set_language(arg))
        return
    if cmd == "/contribute":
        on = arg.strip().lower() in ("on", "yes", "true", "1", "enable")
        _send(token, chat_id, bot.set_contribute(on))
        return

    _call(token, "sendChatAction", chat_id=chat_id, action="typing")
    _send(token, chat_id, bot.ask(text))


class _ChatWorker(threading.Thread):
    """One worker per chat: serial within a chat, concurrent across chats so
    a slow pipeline for one user never blocks everyone else."""

    def __init__(self, token: str, bots: dict, chat_id: int):
        super().__init__(daemon=True)
        self.token, self.bots, self.chat_id = token, bots, chat_id
        self.q: queue.Queue[str] = queue.Queue()

    def run(self) -> None:
        while True:
            text = self.q.get()
            try:
                _handle(self.token, self.bots, self.chat_id, text)
            except Exception:
                traceback.print_exc()
                try:
                    _send(self.token, self.chat_id,
                          "Something went wrong — try again.")
                except Exception:
                    pass


def run_bot() -> None:
    token = _token()
    me = _call(token, "getMe")["result"]
    print(f"⚡ Olympus is live on Telegram as @{me['username']}  (Ctrl-C to stop)")

    bots: dict[int, orchestrator.Olympus] = {}
    workers: dict[int, _ChatWorker] = {}
    offset = 0
    while True:
        try:
            updates = _call(token, "getUpdates", offset=offset, timeout=50)
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(5)
            continue
        for update in updates.get("result", []):
            offset = update["update_id"] + 1
            message = update.get("message") or {}
            text = message.get("text")
            chat = message.get("chat") or {}
            if not text or "id" not in chat:
                continue
            chat_id = chat["id"]
            # /steer is answered here, on the polling thread, so the note can
            # reach a pipeline already running on this chat's serial worker.
            # Keyed exactly like the bot's conversation_id (raw chat id — group
            # ids are negative, which safe_id would mangle).
            if text.split(" ", 1)[0].lower() == "/steer":
                from . import steering
                note = text.partition(" ")[2].strip()
                if not note:
                    _send(token, chat_id, "Usage: /steer <note>")
                elif steering.put(f"tg-{chat_id}", note):
                    _send(token, chat_id, "Noted — the running task will see "
                                          "this after its next tool call.")
                else:
                    _send(token, chat_id, "Steering queue is full; note dropped.")
                continue
            worker = workers.get(chat_id)
            if worker is None:
                worker = workers[chat_id] = _ChatWorker(token, bots, chat_id)
                worker.start()
            worker.q.put(text)

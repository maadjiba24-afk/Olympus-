"""Olympus Telegram gateway — zero dependencies, raw Bot API over urllib.

Setup:
  1. Create a bot with @BotFather, copy the token.
  2. export TELEGRAM_BOT_TOKEN=123456:ABC...
  3. python -m olympus telegram
  4. Run `olympus pair telegram` on the machine, then send the code to the
     bot as `/pair <code>` — DMs are untrusted by default.

Optional:
  TELEGRAM_DM_POLICY=pairing|allowlist|open   who may talk (default: pairing)
  TELEGRAM_ALLOWED_CHAT_IDS=12345,67890       always-allowed chat ids
  TELEGRAM_NOTIFY_CHAT_ID=12345               heartbeat pushes reports here
  TELEGRAM_PROGRESS=0                         disable live progress updates

Command routing is shared with every other chat platform (gateway.reply_for);
this module only handles transport: long-polling, pairing, voice notes,
reply quoting, and streaming progress edits.
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

from . import orchestrator, pairing

CHUNK = 4000  # Telegram message hard limit is 4096 chars
PROGRESS_EVERY = 6  # seconds between progress-message edits

PAIR_HINT = (
    "🔒 This Olympus instance is private.\n"
    "If it's yours: run `olympus pair telegram` on the machine it lives on, "
    "then send me /pair <code>."
)


def _token() -> str:
    from . import secretref
    token = secretref.getenv("TELEGRAM_BOT_TOKEN")
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


def _send(token: str, chat_id: int | str, text: str,
          reply_to: int | None = None) -> None:
    """Send `text`, chunked to the API limit. Only the first chunk quotes the
    triggering message so the answer stays visually attached to its question
    in busy chats without turning every chunk into a quote."""
    text = text.strip() or "(empty reply)"
    first = True
    for i in range(0, len(text), CHUNK):
        params: dict = {"chat_id": chat_id, "text": text[i: i + CHUNK]}
        if first and reply_to:
            params["reply_to_message_id"] = reply_to
            params["allow_sending_without_reply"] = True
        _call(token, "sendMessage", **params)
        first = False


def notify(text: str) -> bool:
    """Push a message to the owner chat if configured (used by the heartbeat).

    Best-effort: returns False instead of raising when unconfigured/offline.
    """
    from . import secretref
    token = secretref.getenv("TELEGRAM_BOT_TOKEN")
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


MAX_VOICE_BYTES = 20 * 1024 * 1024      # Bot API getFile download ceiling


def _voice_text(token: str, message: dict) -> str | None:
    """Transcribe a voice note / audio message to text, or None when the
    message carries no audio (or transcription is unavailable). The
    transcript rides the normal text pipeline, prefixed so the agent knows
    it heard, not read."""
    media = message.get("voice") or message.get("audio")
    if not media or not media.get("file_id"):
        return None
    if int(media.get("file_size") or 0) > MAX_VOICE_BYTES:
        return None
    try:
        info = _call(token, "getFile", file_id=media["file_id"])
        file_path = (info.get("result") or {}).get("file_path")
        if not file_path:
            return None
        with urllib.request.urlopen(
                f"https://api.telegram.org/file/bot{token}/{file_path}",
                timeout=60) as resp:
            blob = resp.read(MAX_VOICE_BYTES + 1)
        if len(blob) > MAX_VOICE_BYTES:
            return None
        from . import media as media_tools
        transcript = media_tools.transcribe_bytes(
            blob, filename=file_path.rsplit("/", 1)[-1] or "voice.oga")
    except Exception:
        return None
    if transcript.startswith("Error"):
        return None
    return f"[voice note] {transcript}"


# --- access control ---------------------------------------------------------

def _policy() -> str:
    p = os.environ.get("TELEGRAM_DM_POLICY", "pairing").strip().lower()
    return p if p in ("pairing", "allowlist", "open") else "pairing"


def _env_allowed_ids() -> set[str]:
    raw = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    ids = {x.strip() for x in raw.split(",") if x.strip()} if raw else set()
    owner = os.environ.get("TELEGRAM_NOTIFY_CHAT_ID", "").strip()
    if owner:
        ids.add(owner)
    return ids


def _allowed(chat_id: int) -> bool:
    """Untrusted by default: a chat may talk only when explicitly allowed by
    env, paired with a one-time code, or the policy was deliberately opened."""
    policy = _policy()
    if policy == "open":
        return True
    if str(chat_id) in _env_allowed_ids():
        return True
    if policy == "pairing":
        return pairing.is_paired("telegram", chat_id)
    return False   # allowlist policy and not on the list


_DENIED_AT: dict[int, float] = {}
_DENY_REPLY_EVERY = 3600


def _deny(token: str, chat_id: int) -> None:
    """Tell a stranger how pairing works — once an hour, not per message, so
    an unpaired scraper can't turn the bot into a reply machine."""
    now = time.time()
    if now - _DENIED_AT.get(chat_id, 0) >= _DENY_REPLY_EVERY:
        _DENIED_AT[chat_id] = now
        try:
            _send(token, chat_id, PAIR_HINT)
        except Exception:
            pass


# --- live progress ----------------------------------------------------------

class _Progress:
    """A placeholder message that shows Olympus is working, edited in place
    with elapsed time, then replaced by the final answer. Best-effort: any
    API failure degrades to the plain send-at-the-end behavior."""

    def __init__(self, token: str, chat_id: int, reply_to: int | None):
        self.token, self.chat_id, self.reply_to = token, chat_id, reply_to
        self.message_id: int | None = None
        self._stop = threading.Event()
        self._started = time.time()

    def start(self) -> None:
        if os.environ.get("TELEGRAM_PROGRESS", "1").strip().lower() in (
                "0", "false", "off", "no"):
            return
        try:
            params: dict = {"chat_id": self.chat_id, "text": "⏳ Working…"}
            if self.reply_to:
                params["reply_to_message_id"] = self.reply_to
                params["allow_sending_without_reply"] = True
            out = _call(self.token, "sendMessage", **params)
            self.message_id = (out.get("result") or {}).get("message_id")
        except Exception:
            self.message_id = None
            return
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while not self._stop.wait(PROGRESS_EVERY):
            if self.message_id is None:
                return
            elapsed = int(time.time() - self._started)
            try:
                _call(self.token, "editMessageText", chat_id=self.chat_id,
                      message_id=self.message_id,
                      text=f"⏳ Working — {elapsed}s. Use /steer <note> to nudge me.")
            except Exception:
                return   # message deleted / rate-limited: stop editing quietly

    def finish(self, text: str) -> None:
        """Replace the placeholder with the answer (first chunk in place, the
        rest as follow-ups); fall back to plain sends without a placeholder."""
        self._stop.set()
        text = (text or "").strip() or "(empty reply)"
        if self.message_id is None:
            _send(self.token, self.chat_id, text, reply_to=self.reply_to)
            return
        head, tail = text[:CHUNK], text[CHUNK:]
        try:
            _call(self.token, "editMessageText", chat_id=self.chat_id,
                  message_id=self.message_id, text=head)
        except Exception:
            _send(self.token, self.chat_id, head, reply_to=self.reply_to)
        if tail:
            _send(self.token, self.chat_id, tail)

    def fail(self, text: str) -> None:
        self.finish(text)


# --- message handling --------------------------------------------------------

def _handle(token: str, bots: dict[str, orchestrator.Olympus],
            chat_id: int, text: str, message_id: int | None = None) -> None:
    cmd, _, arg = text.strip().partition(" ")
    cmd = cmd.lower()

    if cmd == "/pair":
        if pairing.redeem("telegram", arg, chat_id):
            _send(token, chat_id,
                  "✓ Paired. This chat can now talk to Olympus — /help to start.",
                  reply_to=message_id)
        else:
            _send(token, chat_id,
                  "That code didn't match (codes expire after a few minutes). "
                  "Run `olympus pair telegram` again.", reply_to=message_id)
        return

    if not _allowed(chat_id):
        _deny(token, chat_id)
        return

    uid = f"tg-{chat_id}"
    from . import gateway

    # Slash commands answer fast — no placeholder theater for them.
    if cmd.startswith("/"):
        for part in gateway.reply_for(bots, str(chat_id), text,
                                      prefix="tg", uid=uid):
            _send(token, chat_id, part, reply_to=message_id)
        return

    _call(token, "sendChatAction", chat_id=chat_id, action="typing")
    progress = _Progress(token, chat_id, message_id)
    progress.start()
    # Journal the in-flight request so a gateway restart resumes it instead
    # of losing it silently.
    gateway.inflight_mark(uid, chat_id, text)
    try:
        parts = gateway.reply_for(bots, str(chat_id), text,
                                  prefix="tg", uid=uid)
        progress.finish("\n".join(parts) if parts else "")
    except Exception:
        progress.fail("Something went wrong — try again.")
        raise
    finally:
        gateway.inflight_clear(uid)


class _ChatWorker(threading.Thread):
    """One worker per chat: serial within a chat, concurrent across chats so
    a slow pipeline for one user never blocks everyone else."""

    def __init__(self, token: str, bots: dict, chat_id: int):
        super().__init__(daemon=True)
        self.token, self.bots, self.chat_id = token, bots, chat_id
        self.q: queue.Queue = queue.Queue()

    def run(self) -> None:
        while True:
            item = self.q.get()
            text, message_id = item if isinstance(item, tuple) else (item, None)
            try:
                _handle(self.token, self.bots, self.chat_id, text, message_id)
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
    if _policy() == "pairing":
        print("   DM policy: pairing (untrusted by default). "
              "Run `olympus pair telegram` to let a chat in.")

    from . import gateway
    bots: dict[str, orchestrator.Olympus] = {}
    workers: dict[int, _ChatWorker] = {}

    # Session auto-resume: re-run whatever a previous gateway process was
    # working on when it died (at most one retry per message).
    for entry in gateway.inflight_take("tg-"):
        try:
            chat_id = int(entry["key"])
        except (TypeError, ValueError):
            continue
        try:
            _send(token, chat_id,
                  "⚡ I was restarted while working on your last request — "
                  "picking it back up now.")
        except Exception:
            pass
        worker = workers.get(chat_id)
        if worker is None:
            worker = workers[chat_id] = _ChatWorker(token, bots, chat_id)
            worker.start()
        worker.q.put((entry["text"], None))

    offset = 0
    last_sweep = time.time()
    while True:
        # Periodically distill-and-clear idle sessions so a long-lived gateway
        # doesn't carry unbounded per-user history.
        if time.time() - last_sweep >= 900:
            gateway.reset_idle_sessions(bots)
            last_sweep = time.time()
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
            if not text:
                # A voice note becomes text via transcription (needs a media
                # API key; silently skipped otherwise, as before).
                text = _voice_text(token, message)
            if not text or "id" not in chat:
                continue
            chat_id = chat["id"]
            message_id = message.get("message_id")
            # /steer is answered here, on the polling thread, so the note can
            # reach a pipeline already running on this chat's serial worker.
            # Keyed exactly like the bot's conversation_id (raw chat id — group
            # ids are negative, which safe_id would mangle).
            if text.split(" ", 1)[0].lower() == "/steer":
                if not _allowed(chat_id):
                    _deny(token, chat_id)
                    continue
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
            worker.q.put((text, message_id))

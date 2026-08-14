"""Olympus WhatsApp gateway — zero dependencies, raw WhatsApp Cloud API.

WhatsApp's official Cloud API is webhook-based (Meta POSTs messages to you),
so unlike the Telegram gateway this runs a small HTTP server to receive them —
the same dependency-free style as the rest of Olympus (raw urllib + http.server).

Setup:
  1. Create a Meta app + WhatsApp product, get a phone-number ID and a token.
  2. Choose any secret string as your verify token.
  3. export WHATSAPP_ACCESS_TOKEN=...        (Graph API token)
     export WHATSAPP_PHONE_NUMBER_ID=...     (the "from" number's ID)
     export WHATSAPP_VERIFY_TOKEN=...         (the secret you chose)
  4. python -m olympus whatsapp               (serves the webhook on :8485)
  5. Point Meta's webhook at https://your-host/webhook (behind HTTPS — see README).

Optional:
  WHATSAPP_ALLOWED_NUMBERS=15551234567,...   restrict who may talk to it
  WHATSAPP_APP_SECRET=...                     verify inbound payload signatures

Commands mirror the Telegram gateway (/scan /audit /watch /queue /good /lang …);
anything else goes through the full Zeus → Athena → Aletheia pipeline.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import queue
import threading
import traceback
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import gateway, memory, orchestrator

GRAPH = "https://graph.facebook.com/v21.0"
CHUNK = 4000  # WhatsApp text body limit is 4096 chars


# --- configuration -------------------------------------------------------

def _access_token() -> str:
    token = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
    if not token:
        raise SystemExit("Set WHATSAPP_ACCESS_TOKEN (from your Meta app).")
    return token


def _phone_number_id() -> str:
    pid = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
    if not pid:
        raise SystemExit("Set WHATSAPP_PHONE_NUMBER_ID (your WhatsApp number's ID).")
    return pid


def _allowed(sender: str) -> bool:
    """Whether this sender may use the council. CLOSED by default.

    Every other channel treats an unknown sender as untrusted (chanbase's
    default policy is `pairing`); WhatsApp answered everyone unless an
    allowlist was set, which is the opposite default and contradicts
    docs/GATEWAY.md. Set WHATSAPP_ALLOWED_NUMBERS to your own number(s), or
    WHATSAPP_DM_POLICY=open to deliberately restore the old behavior."""
    if os.environ.get("WHATSAPP_DM_POLICY", "").strip().lower() == "open":
        return True
    raw = os.environ.get("WHATSAPP_ALLOWED_NUMBERS", "").strip()
    if not raw:
        return False
    return sender in {x.strip() for x in raw.split(",")}


# --- sending -------------------------------------------------------------

def _send(to: str, text: str) -> None:
    """Send a text message back to a WhatsApp user via the Cloud API."""
    text = text.strip() or "(empty reply)"
    url = f"{GRAPH}/{_phone_number_id()}/messages"
    for i in range(0, len(text), CHUNK):
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text[i:i + CHUNK]},
        }
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {_access_token()}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()


# --- webhook verification + parsing --------------------------------------

def verify_challenge(mode: str, token: str, challenge: str) -> str | None:
    """Meta's GET handshake: echo the challenge iff the verify token matches."""
    expected = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
    if mode == "subscribe" and expected and hmac.compare_digest(token, expected):
        return challenge
    return None


def valid_signature(raw_body: bytes, header: str | None) -> bool:
    """Verify Meta's X-Hub-Signature-256. FAIL CLOSED without an app secret.

    This used to return True when `WHATSAPP_APP_SECRET` was unset, so a default
    install accepted *any* POST to the webhook URL as genuine. The verify token
    guards only Meta's GET handshake, not message delivery — so anyone who
    learned the URL could forge a payload, spoof any sender number, and drive
    the council on the operator's key. An unverified payload is not a WhatsApp
    payload; refuse it rather than trust it."""
    secret = os.environ.get("WHATSAPP_APP_SECRET", "")
    if not secret:
        return False
    if not header or not header.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, header.split("=", 1)[1])


MAX_VOICE_BYTES = 20 * 1024 * 1024


def _voice_text(media_id: str) -> str | None:
    """Download a WhatsApp audio/voice message via the Graph API and
    transcribe it. None when unavailable (no media key, too big, errors)."""
    try:
        req = urllib.request.Request(
            f"{GRAPH}/{media_id}",
            headers={"Authorization": f"Bearer {_access_token()}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            info = json.loads(resp.read())
        url = info.get("url")
        if not url:
            return None
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {_access_token()}"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            blob = resp.read(MAX_VOICE_BYTES + 1)
        if len(blob) > MAX_VOICE_BYTES:
            return None
        from . import media as media_tools
        transcript = media_tools.transcribe_bytes(blob, filename="voice.ogg")
    except Exception:
        return None
    if transcript.startswith("Error"):
        return None
    return f"[voice note] {transcript}"


def extract_messages(payload: dict,
                     transcribe=None) -> list[tuple[str, str, str]]:
    """Pull (sender, text, message_id) tuples from a webhook payload, ignoring
    everything else (delivery/read status callbacks, unsupported types).
    Voice/audio messages become text via transcription when a media API key
    is configured (injectable via `transcribe` for tests). The message id
    lets the server drop Meta's webhook retries."""
    transcribe = transcribe or _voice_text
    out: list[tuple[str, str, str]] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []):
                sender = msg.get("from", "")
                body = ""
                if msg.get("type") == "text":
                    body = (msg.get("text") or {}).get("body", "")
                elif msg.get("type") in ("audio", "voice"):
                    media_id = (msg.get("audio") or msg.get("voice")
                                or {}).get("id", "")
                    if media_id:
                        body = transcribe(media_id) or ""
                if sender and body:
                    out.append((sender, body, msg.get("id", "")))
    return out


# --- message handling (mirrors the Telegram command set) -----------------

def _process(bots: dict, sender: str, text: str) -> None:
    if not _allowed(sender):
        _send(sender, "This Olympus instance is private.")
        return

    cmd, _, arg = text.strip().partition(" ")
    cmd = cmd.lower()

    if cmd in ("/start", "/help"):
        _send(sender, gateway.HELP)
        return
    if cmd == "/scan":
        _send(sender, "🌐 Argus is scanning the world — give me a few minutes...")
        _send(sender, orchestrator.opportunity_scan())
        return
    if cmd == "/audit":
        _send(sender, "🔧 Prometheus is auditing Olympus...")
        _send(sender, orchestrator.evolution_audit())
        return
    if cmd == "/watch":
        if not arg:
            _send(sender, "Usage: /watch <youtube-url>")
            return
        _send(sender, "🎥 Mnemosyne is watching the video...")
        _send(sender, orchestrator.watch_and_learn(arg))
        return
    if cmd == "/queue":
        if not arg:
            _send(sender, "Usage: /queue <youtube-url>")
            return
        memory.watchlist_add(arg)
        _send(sender, "Queued — the heartbeat will watch it on its next pass.")
        return

    # Per-sender identity: private memory namespace + persisted history.
    bot = bots.setdefault(sender, orchestrator.Olympus(
        user=f"wa-{sender}", conversation_id=f"wa-{sender}"))

    if cmd == "/undo":
        try:
            n = int(arg) if arg.strip() else 1
        except ValueError:
            _send(sender, "Usage: /undo [N]")
            return
        _send(sender, bot.undo(n))
        return
    if cmd in ("/good", "/bad"):
        _send(sender, bot.feedback("up" if cmd == "/good" else "down", arg))
        return
    if cmd == "/lang":
        if not arg:
            _send(sender, "Usage: /lang <language>  (e.g. /lang Spanish, /lang auto)")
            return
        _send(sender, bot.set_language(arg))
        return
    if cmd == "/contribute":
        on = arg.strip().lower() in ("on", "yes", "true", "1", "enable")
        _send(sender, bot.set_contribute(on))
        return

    # Journal the in-flight request so a gateway restart resumes it.
    from . import gateway
    gateway.inflight_mark(f"wa-{sender}", sender, text)
    try:
        _send(sender, bot.ask(text))
    finally:
        gateway.inflight_clear(f"wa-{sender}")


class _SenderWorker(threading.Thread):
    """One worker per sender: serial within a conversation, concurrent across
    senders — and crucially lets the webhook return 200 immediately while the
    (slow) pipeline runs in the background, so Meta doesn't retry/duplicate."""

    def __init__(self, bots: dict, sender: str):
        super().__init__(daemon=True)
        self.bots, self.sender = bots, sender
        self.q: queue.Queue[str] = queue.Queue()

    def run(self) -> None:
        while True:
            text = self.q.get()
            try:
                _process(self.bots, self.sender, text)
            except Exception:
                traceback.print_exc()
                try:
                    _send(self.sender, "Something went wrong — try again.")
                except Exception:
                    pass


# --- the webhook HTTP server ---------------------------------------------

class Handler(BaseHTTPRequestHandler):
    # shared state is attached to the server instance in run_server()
    def log_message(self, *args):  # silence default request logging
        pass

    def _text(self, code: int, body: str = "") -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self) -> None:  # noqa: N802 — Meta's verification handshake
        params = parse_qs(urlparse(self.path).query)
        challenge = verify_challenge(
            params.get("hub.mode", [""])[0],
            params.get("hub.verify_token", [""])[0],
            params.get("hub.challenge", [""])[0])
        if challenge is not None:
            self._text(200, challenge)
        else:
            self._text(403, "verification failed")

    def do_POST(self) -> None:  # noqa: N802 — inbound messages
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        if not valid_signature(raw, self.headers.get("X-Hub-Signature-256")):
            self._text(403, "bad signature")
            return
        # Acknowledge fast; process in the background so Meta doesn't retry.
        self._text(200, "ok")
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return
        seen, bots, workers, lock = (self.server._seen, self.server._bots,
                                     self.server._workers, self.server._lock)
        for sender, text, mid in extract_messages(payload):
            with lock:
                if mid and mid in seen:
                    continue            # Meta retried a webhook — already handled
                if mid:
                    if len(seen) > 1000:
                        seen.clear()
                    seen.add(mid)
                worker = workers.get(sender)
                if worker is None:
                    worker = workers[sender] = _SenderWorker(bots, sender)
                    worker.start()
            # /steer is answered on the webhook thread (not queued) so the
            # note reaches a pipeline already running on this sender's worker.
            if text.split(" ", 1)[0].lower() == "/steer":
                from . import steering
                note = text.partition(" ")[2].strip()
                if not note:
                    _send(sender, "Usage: /steer <note>")
                elif steering.put(f"wa-{sender}", note):
                    _send(sender, "Noted — the running task will see this "
                                  "after its next tool call.")
                else:
                    _send(sender, "Steering queue is full; note dropped.")
                continue
            worker.q.put(text)


def run_server(host: str = "127.0.0.1", port: int = 8485) -> None:
    _access_token(); _phone_number_id()    # fail fast if misconfigured
    if not os.environ.get("WHATSAPP_VERIFY_TOKEN"):
        raise SystemExit("Set WHATSAPP_VERIFY_TOKEN (any secret; used for "
                         "Meta's webhook verification).")
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd._bots, httpd._workers = {}, {}
    httpd._seen = set()
    httpd._lock = threading.Lock()
    # Session auto-resume: re-run what a previous process died holding.
    from . import gateway as _gw
    for entry in _gw.inflight_take("wa-"):
        sender = str(entry.get("key") or "")
        if not sender:
            continue
        try:
            _send(sender, "⚡ I was restarted while working on your last "
                          "request — picking it back up now.")
        except Exception:
            pass
        worker = httpd._workers.get(sender)
        if worker is None:
            worker = httpd._workers[sender] = _SenderWorker(httpd._bots, sender)
            worker.start()
        worker.q.put(entry["text"])
    print(f"⚡ Olympus WhatsApp webhook on http://{host}:{port}/webhook "
          f"(put it behind HTTPS; Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass

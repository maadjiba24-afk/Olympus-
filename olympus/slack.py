"""Olympus Slack gateway — zero dependencies, raw HTTP over urllib.

  * notify()  — post to a channel via chat.postMessage (SLACK_BOT_TOKEN +
                SLACK_NOTIFY_CHANNEL), used by the heartbeat/scheduler.
  * Events API — Slack POSTs message events to your public URL.
                `handle_event(payload)` answers the url_verification handshake
                and routes message events through the Olympus pipeline.

Inbound requests are authenticated with Slack's signing secret (HMAC-SHA256
over the raw body, stdlib only): set SLACK_SIGNING_SECRET.

Setup:
  export SLACK_BOT_TOKEN=xoxb-...
  export SLACK_SIGNING_SECRET=...
  export SLACK_NOTIFY_CHANNEL=C0123        # optional, for notify()
  python -m olympus slack
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import gateway

_BOTS: dict = {}
_DISPATCH = gateway.Dispatcher()


def _post(method: str, payload: dict) -> dict:
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    req = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def send(channel: str, text: str) -> None:
    for part in gateway.chunk(text):
        _post("chat.postMessage", {"channel": channel, "text": part})


def notify(text: str) -> bool:
    channel = os.environ.get("SLACK_NOTIFY_CHANNEL", "").strip()
    if not channel or not os.environ.get("SLACK_BOT_TOKEN"):
        return False
    try:
        send(channel, text)
        return True
    except Exception:
        return False


def verify_signature(secret: str, timestamp: str, body: bytes,
                     signature: str, now: float | None = None) -> bool:
    """Verify Slack's v0 HMAC-SHA256 request signature (stdlib hmac)."""
    if not secret or not timestamp or not signature:
        return False
    try:
        if abs((now or time.time()) - int(timestamp)) > 60 * 5:
            return False                              # stale → replay guard
    except ValueError:
        return False
    base = f"v0:{timestamp}:".encode() + body
    digest = hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"v0={digest}", signature)


def handle_event(payload: dict) -> dict:
    """The *fast* synchronous body Slack expects. For the url_verification
    handshake returns {challenge}; for everything else returns {ok: True}
    immediately WITHOUT running the (slow) pipeline — that happens in the
    background via `process_event`, so Slack's ~3s deadline is always met and it
    never retries into a duplicate reply."""
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}
    return {"ok": True}


def process_event(payload: dict, bots: dict | None = None) -> None:
    """Run the pipeline for a Slack message event and post the reply. Called on
    a background worker after the webhook has already acked. Ignores the bot's
    own messages and non-message events to avoid reply loops."""
    bots = _BOTS if bots is None else bots
    event = payload.get("event") or {}
    if event.get("type") not in ("message", "app_mention") or event.get("bot_id"):
        return
    text = event.get("text", "")
    channel = event.get("channel", "")
    user_key = event.get("user", "anon")
    reply = "\n".join(gateway.reply_for(bots, user_key, text, prefix="sl"))
    if channel:
        try:
            send(channel, reply)
        except Exception:
            pass


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        secret = os.environ.get("SLACK_SIGNING_SECRET", "")
        ts = self.headers.get("X-Slack-Request-Timestamp", "")
        sig = self.headers.get("X-Slack-Signature", "")
        if not verify_signature(secret, ts, body, sig):
            self.send_response(401)
            self.end_headers()
            return
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self.send_response(400)
            self.end_headers()
            return
        # Ack fast (handshake or {ok:true}), then run the pipeline off-thread.
        resp = handle_event(payload)
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

        if payload.get("type") == "event_callback":
            # Slack retries carry the same event_id — drop the duplicate.
            if _DISPATCH.seen(payload.get("event_id")):
                return
            event = payload.get("event") or {}
            user_key = event.get("user", "anon")
            # /steer is handled here, OUTSIDE the serial worker, so the note
            # can reach a pipeline that is already running for this user.
            steer = gateway.try_steer(user_key, event.get("text", ""),
                                      prefix="sl")
            if steer is not None:
                if event.get("channel"):
                    try:
                        send(event["channel"], "\n".join(steer))
                    except Exception:
                        pass
                return
            _DISPATCH.submit(user_key, lambda p=payload: process_event(p))


def run_server(host: str = "0.0.0.0", port: int = 8487) -> None:
    if not os.environ.get("SLACK_SIGNING_SECRET"):
        raise SystemExit("Set SLACK_SIGNING_SECRET (from your Slack app config).")
    print(f"⚡ Olympus Slack events endpoint on {host}:{port}")
    ThreadingHTTPServer((host, port), _Handler).serve_forever()

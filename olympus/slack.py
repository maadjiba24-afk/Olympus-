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
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import gateway

_BOTS: dict = {}


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
    """Return the JSON body Slack expects. For the url_verification handshake
    returns {challenge}; for a user message runs the pipeline and posts the
    reply, returning {ok: True}."""
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}
    event = payload.get("event") or {}
    # Ignore bot's own messages / non-message events to avoid loops.
    if event.get("type") not in ("message", "app_mention") or event.get("bot_id"):
        return {"ok": True}
    text = event.get("text", "")
    channel = event.get("channel", "")
    user_key = event.get("user", "anon")
    reply = "\n".join(gateway.reply_for(_BOTS, user_key, text, prefix="sl"))
    if channel:
        try:
            send(channel, reply)
        except Exception:
            pass
    return {"ok": True}


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
            resp = handle_event(json.loads(body))
        except Exception:
            self.send_response(400)
            self.end_headers()
            return
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)


def run_server(host: str = "0.0.0.0", port: int = 8487) -> None:
    if not os.environ.get("SLACK_SIGNING_SECRET"):
        raise SystemExit("Set SLACK_SIGNING_SECRET (from your Slack app config).")
    print(f"⚡ Olympus Slack events endpoint on {host}:{port}")
    HTTPServer((host, port), _Handler).serve_forever()

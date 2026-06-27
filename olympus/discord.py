"""Olympus Discord gateway — zero dependencies, raw HTTP over urllib.

Two integration points, both standard Discord:

  * notify()  — push a message to a channel via an Incoming Webhook URL
                (DISCORD_WEBHOOK_URL). Used by the heartbeat/scheduler.
  * Interactions endpoint — Discord POSTs slash-command interactions to your
                public URL. `handle_interaction(payload)` returns the JSON
                response; PING (type 1) is answered with PONG (type 1), and a
                slash command (type 2) runs through the Olympus pipeline.

Discord requires Ed25519 request-signature verification on the interactions
endpoint; `verify_signature()` does it with the `cryptography` dependency
Olympus already ships. Set DISCORD_PUBLIC_KEY to your app's public key.

Setup:
  export DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...   (outbound)
  export DISCORD_PUBLIC_KEY=...                                     (inbound)
  python -m olympus discord            # serve the interactions endpoint
"""

from __future__ import annotations

import json
import os
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import gateway

PING, PONG = 1, 1
APPLICATION_COMMAND = 2
CHANNEL_MESSAGE = 4

_BOTS: dict = {}


def notify(text: str) -> bool:
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        return False
    try:
        for part in gateway.chunk(text, 1900):       # Discord limit is 2000
            req = urllib.request.Request(
                url, data=json.dumps({"content": part}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=30).read()
        return True
    except Exception:
        return False


def verify_signature(public_key: str, signature: str, timestamp: str,
                     body: bytes) -> bool:
    """Verify Discord's Ed25519 request signature. Lazy-imports cryptography so
    importing this module never fails on a host without it."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey)
    except BaseException:
        # No usable crypto backend → we cannot verify, so we must NOT trust the
        # request. Reject rather than crash the endpoint.
        return False
    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key))
        key.verify(bytes.fromhex(signature), timestamp.encode() + body)
        return True
    except BaseException:
        return False


def _command_text(data: dict) -> str:
    """Flatten a slash-command interaction into the text the pipeline expects."""
    name = data.get("name", "")
    opts = data.get("options") or []
    arg = " ".join(str(o.get("value", "")) for o in opts)
    # /ask <prompt> → just the prompt; other commands map to /name <arg>
    if name == "ask":
        return arg
    return f"/{name} {arg}".strip()


def handle_interaction(payload: dict) -> dict:
    """Produce the JSON response for a Discord interaction payload."""
    if payload.get("type") == PING:
        return {"type": PONG}
    if payload.get("type") == APPLICATION_COMMAND:
        user = (((payload.get("member") or {}).get("user")) or
                payload.get("user") or {})
        user_key = str(user.get("id", "anon"))
        text = _command_text(payload.get("data") or {})
        reply = "\n".join(gateway.reply_for(_BOTS, user_key, text, prefix="dc"))
        return {"type": CHANNEL_MESSAGE, "data": {"content": reply[:1990]}}
    return {"type": PONG}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):           # quiet
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        pub = os.environ.get("DISCORD_PUBLIC_KEY", "")
        sig = self.headers.get("X-Signature-Ed25519", "")
        ts = self.headers.get("X-Signature-Timestamp", "")
        if not pub or not verify_signature(pub, sig, ts, body):
            self.send_response(401)
            self.end_headers()
            return
        try:
            resp = handle_interaction(json.loads(body))
        except Exception:
            self.send_response(400)
            self.end_headers()
            return
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)


def run_server(host: str = "0.0.0.0", port: int = 8486) -> None:
    if not os.environ.get("DISCORD_PUBLIC_KEY"):
        raise SystemExit("Set DISCORD_PUBLIC_KEY (your Discord app's public key).")
    print(f"⚡ Olympus Discord interactions endpoint on {host}:{port}")
    HTTPServer((host, port), _Handler).serve_forever()

"""Olympus Discord gateway — zero dependencies, raw HTTP over urllib.

Two integration points, both standard Discord:

  * notify()  — push a message to a channel via an Incoming Webhook URL
                (DISCORD_WEBHOOK_URL). Used by the heartbeat/scheduler.
  * Interactions endpoint — Discord POSTs slash-command interactions to your
                public URL. PING (type 1) is answered synchronously with PONG;
                a slash command (type 2) is acked with a DEFERRED response
                (type 5) within Discord's 3s deadline, then the Olympus pipeline
                runs in the background and `process_command` delivers the real
                reply via the interaction follow-up webhook.

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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import gateway

PING, PONG = 1, 1
APPLICATION_COMMAND = 2
# Interaction response type 5: "acknowledge, a reply will follow." Discord shows
# a thinking state and gives us up to 15 minutes to PATCH in the real content —
# the only way to run a pipeline slower than Discord's 3s interaction deadline.
DEFERRED_CHANNEL_MESSAGE = 5

API = "https://discord.com/api/v10"

_BOTS: dict = {}
_DISPATCH = gateway.Dispatcher()


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


def _user_key(payload: dict) -> str:
    user = (((payload.get("member") or {}).get("user")) or
            payload.get("user") or {})
    return str(user.get("id", "anon"))


def sync_response(payload: dict) -> dict:
    """The *immediate* JSON response for an interaction. PING is answered with
    PONG; a slash command is acked with a DEFERRED response so Discord's 3s
    deadline is met, and the real content follows via `process_command`."""
    if payload.get("type") == PING:
        return {"type": PONG}
    if payload.get("type") == APPLICATION_COMMAND:
        return {"type": DEFERRED_CHANNEL_MESSAGE}
    return {"type": PONG}


def _followup(application_id: str, token: str, text: str) -> None:
    """Deliver the real reply after a deferred ack. The first chunk edits the
    deferred placeholder (@original); any further chunks are posted as follow-up
    messages so a long reply isn't truncated at Discord's 2000-char limit."""
    if not application_id or not token:
        return
    parts = gateway.chunk(text, 1900) or ["(empty reply)"]
    for i, part in enumerate(parts):
        if i == 0:
            url = f"{API}/webhooks/{application_id}/{token}/messages/@original"
            method = "PATCH"
        else:
            url = f"{API}/webhooks/{application_id}/{token}"
            method = "POST"
        req = urllib.request.Request(
            url, data=json.dumps({"content": part}).encode(),
            headers={"Content-Type": "application/json"}, method=method)
        try:
            urllib.request.urlopen(req, timeout=30).read()
        except Exception:
            break


def process_command(payload: dict, bots: dict | None = None) -> None:
    """Run the pipeline for a slash command and deliver the reply via the
    interaction follow-up webhook. Runs on a background worker after the
    deferred ack has already been sent."""
    bots = _BOTS if bots is None else bots
    text = _command_text(payload.get("data") or {})
    reply = "\n".join(gateway.reply_for(bots, _user_key(payload), text,
                                        prefix="dc"))
    _followup(payload.get("application_id", ""), payload.get("token", ""), reply)


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
            payload = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self.send_response(400)
            self.end_headers()
            return
        # Ack immediately (PONG, or a DEFERRED for a command), then run the
        # pipeline off-thread and deliver via the follow-up webhook.
        resp = sync_response(payload)
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

        if payload.get("type") == APPLICATION_COMMAND:
            # Discord retries an interaction with the same id — drop duplicates.
            if _DISPATCH.seen(payload.get("id")):
                return
            # /steer bypasses the serial worker so it lands mid-run.
            steer = gateway.try_steer(
                _user_key(payload), _command_text(payload.get("data") or {}),
                prefix="dc")
            if steer is not None:
                _followup(payload.get("application_id", ""),
                          payload.get("token", ""), "\n".join(steer))
                return
            _DISPATCH.submit(_user_key(payload),
                             lambda p=payload: process_command(p))


def run_server(host: str = "0.0.0.0", port: int = 8486) -> None:
    if not os.environ.get("DISCORD_PUBLIC_KEY"):
        raise SystemExit("Set DISCORD_PUBLIC_KEY (your Discord app's public key).")
    print(f"⚡ Olympus Discord interactions endpoint on {host}:{port}")
    ThreadingHTTPServer((host, port), _Handler).serve_forever()

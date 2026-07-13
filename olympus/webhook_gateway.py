"""Inbound webhook gateway — a generic HTTP entry point into Olympus.

Any system that can POST JSON can talk to the council: a form, a cron on another
host, a Zapier/n8n step, a custom app. It POSTs ``{"user": "...", "text": "..."}``
and gets back ``{"reply": "..."}`` — routed through the SAME shared gateway
pipeline (per-user memory, slash commands, verified answers) as every messaging
platform.

Auth: set OLYMPUS_WEBHOOK_SECRET and callers must send it in the
``X-Olympus-Secret`` header (or ``?secret=``). Unset = open (fine behind your
own network boundary, not for the public internet).
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import gateway

_MAX_BODY = 100_000


def _secret_ok(supplied: str) -> bool:
    want = os.environ.get("OLYMPUS_WEBHOOK_SECRET", "")
    if not want:
        return True
    import hmac
    return hmac.compare_digest(want, supplied or "")


def handle_payload(bots: dict, payload: dict) -> dict:
    """Core: turn a {user, text} payload into a {reply} dict. Pure/testable —
    no HTTP. Raises ValueError on a malformed payload."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    text = (payload.get("text") or "").strip()
    if not text:
        raise ValueError("missing 'text'")
    user = (payload.get("user") or "anonymous").strip() or "anonymous"
    chunks = gateway.reply_for(bots, user, text, prefix="hook")
    return {"reply": "\n\n".join(chunks)}


def _make_handler(bots: dict):
    class _Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, obj: dict) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802 (stdlib naming)
            supplied = (self.headers.get("X-Olympus-Secret", "")
                        or self.path.partition("secret=")[2])
            if not _secret_ok(supplied):
                return self._send(401, {"error": "unauthorized"})
            try:
                length = min(int(self.headers.get("Content-Length", 0)), _MAX_BODY)
                payload = json.loads(self.rfile.read(length) or b"{}")
                result = handle_payload(bots, payload)
            except ValueError as err:
                return self._send(400, {"error": str(err)})
            except Exception as err:  # never leak a stack trace
                return self._send(500, {"error": str(err)[:200]})
            self._send(200, result)

        def log_message(self, *a):   # keep stdout quiet
            pass

    return _Handler


def run_server(host: str = "0.0.0.0", port: int = 8487) -> None:
    bots: dict = {}
    auth = "on" if os.environ.get("OLYMPUS_WEBHOOK_SECRET") else "OFF (open)"
    print(f"⚡ Olympus webhook gateway on {host}:{port}  (auth: {auth})")
    print('   POST {"user":"...","text":"..."}  →  {"reply":"..."}')
    HTTPServer((host, port), _make_handler(bots)).serve_forever()

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
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import gateway

_MAX_BODY = 100_000

# Per-caller sliding-window rate limit. A webhook is a public entry point that
# runs the FULL council on the operator's key — without a limit, a leaked (or
# unset) secret is a key-burn DoS. Keyed by client IP; window is 60s.
_HITS: dict[str, deque] = {}
_HITS_LOCK = threading.Lock()


def _rate_limit() -> int:
    try:
        return int(os.environ.get("OLYMPUS_WEBHOOK_RATE_LIMIT", "20"))
    except ValueError:
        return 20


def _rate_limited(key: str, limit: int | None = None) -> bool:
    limit = _rate_limit() if limit is None else limit
    if limit <= 0:
        return False
    now = time.time()
    with _HITS_LOCK:
        if len(_HITS) > 5000:            # bound the limiter's own memory
            _HITS.clear()
        hits = _HITS.setdefault(key, deque())
        while hits and now - hits[0] > 60:
            hits.popleft()
        if len(hits) >= limit:
            return True
        hits.append(now)
    return False


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
            client = self.client_address[0] if self.client_address else "?"
            if _rate_limited(client):
                return self._send(429, {"error": "rate limit exceeded — "
                                        "slow down"})
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

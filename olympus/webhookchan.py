"""Generic webhook-channel HTTP server — the shared transport for chat
platforms that PUSH inbound messages to an HTTP endpoint and expect the reply
in the HTTP response (Mattermost outgoing webhooks, Google Chat apps, LINE,
Slack-style outgoing hooks).

A platform adapter supplies three small callables and gets a hardened server:

  * verify(headers, body_bytes) -> bool   — authenticate the caller (shared
        token / HMAC). Fail-closed: a False return is a 401.
  * parse(body_obj) -> (sender_id, text) | None  — pull the sender + message
        text from the platform's payload shape (None = ignore, e.g. the bot's
        own echo or a non-message event).
  * respond(reply_text) -> dict           — wrap the reply in the platform's
        expected JSON response shape.

The server itself owns the security invariants every webhook channel needs and
that OpenClaw repeatedly got wrong: a bounded request body, a per-source rate
limit, fail-closed auth, no stack traces in responses, and routing through the
untrusted-by-default access spine (chanbase.collect_reply). The adapter never
re-implements any of that.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import chanbase

_MAX_BODY = 200_000


def make_handler(channel: str, prefix: str, verify, parse, respond,
                 bots: dict | None = None,
                 limiter: "chanbase.RateLimiter | None" = None):
    bots = {} if bots is None else bots
    limiter = limiter or chanbase.RateLimiter(per_minute=12)

    class _Handler(BaseHTTPRequestHandler):
        def _json(self, code: int, obj: dict) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802 (stdlib naming)
            try:
                length = min(int(self.headers.get("Content-Length", 0) or 0),
                             _MAX_BODY)
            except ValueError:
                return self._json(400, {"error": "bad length"})
            raw = self.rfile.read(length) if length else b""
            # Fail-closed auth FIRST, before parsing anything.
            try:
                if not verify(self.headers, raw):
                    return self._json(401, {"error": "unauthorized"})
            except Exception:
                return self._json(401, {"error": "unauthorized"})
            client = self.client_address[0] if self.client_address else "?"
            if limiter.hit(f"ip:{client}"):
                return self._json(429, {"error": "rate limit exceeded"})
            try:
                payload = json.loads(raw or b"{}")
            except ValueError:
                # Some platforms post form-encoded; let parse handle raw too.
                payload = {"_raw": raw.decode("utf-8", "replace")}
            try:
                parsed = parse(payload)
            except Exception:
                parsed = None
            if not parsed:
                return self._json(200, respond(""))   # ignore silently
            sender_id, text = parsed
            try:
                reply = chanbase.collect_reply(channel, prefix, sender_id,
                                               text, bots, limiter=limiter)
            except Exception as err:            # never leak a stack trace
                return self._json(200, respond(f"error: {str(err)[:150]}"))
            self._json(200, respond(reply or ""))

        def log_message(self, *a):              # keep stdout quiet
            pass

    return _Handler


def serve(channel: str, prefix: str, verify, parse, respond,
          host: str, port: int) -> None:
    handler = make_handler(channel, prefix, verify, parse, respond)
    print(f"⚡ Olympus {channel} channel on {host}:{port}  "
          f"(untrusted by default — pair with `olympus pair {channel}`)")
    HTTPServer((host, port), handler).serve_forever()

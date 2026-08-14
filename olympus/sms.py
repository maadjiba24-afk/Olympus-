"""Olympus SMS channel — inbound texts in, replies out, over Twilio's webhook.

This is the *buildable* slice of the surveyed gateway's telephony surface. A live voice call
needs an audio/media stack (streaming speech in and out) that Olympus
deliberately doesn't carry; an SMS, by contrast, is plain text — so it drops
straight onto the same untrusted-by-default access spine every other channel
uses, with no new dependencies.

Flow (Twilio-compatible, works with any provider that speaks the same shape):
  * Twilio POSTs a form-encoded message (From, Body, …) to this endpoint,
    signed with `X-Twilio-Signature` (HMAC-SHA1 over the exact request URL plus
    the sorted POST params). We verify it in constant time and fail closed when
    no auth token is configured — a public SMS endpoint with no signature check
    would run the council for any spoofed number.
  * The reply goes back as TwiML `<Response><Message>…</Message></Response>`.
  * Proactive pushes go out through Twilio's REST API (`notify`).

Setup:
  export TWILIO_AUTH_TOKEN=…                 # verifies inbound (SecretRef ok)
  export OLYMPUS_SMS_PUBLIC_URL=https://you.example/sms   # the exact webhook URL
  # for outbound notify:
  export TWILIO_ACCOUNT_SID=AC…
  export TWILIO_FROM=+15551234567
  export OLYMPUS_SMS_NOTIFY_TO=+15557654321
  python -m olympus sms --port 8491
  # then pair: `olympus pair sms`, and text `/pair <code>` from your phone.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from xml.sax.saxutils import escape

from . import chanbase

PREFIX = "sms"
_MAX_BODY = 32_000          # an SMS webhook body is tiny; cap hard


def _auth_token() -> str:
    from . import secretref
    return secretref.getenv("TWILIO_AUTH_TOKEN")


def _request_url(headers, path: str) -> str:
    """The exact URL Twilio signed. Behind a proxy the handler can't see the
    public scheme/host, so an operator pins it with OLYMPUS_SMS_PUBLIC_URL;
    otherwise reconstruct a best-effort URL from the Host header."""
    pinned = os.environ.get("OLYMPUS_SMS_PUBLIC_URL", "").strip()
    if pinned:
        # Twilio signs the full URL it POSTed to, query string included. If the
        # operator pinned only the base path, honour the request path when the
        # pin has none of its own.
        return pinned
    host = headers.get("Host", "").strip()
    proto = headers.get("X-Forwarded-Proto", "https").split(",")[0].strip()
    return f"{proto}://{host}{path}"


def _expected_signature(token: str, url: str, params: dict[str, str]) -> str:
    """Twilio's scheme: URL + each sorted key immediately followed by its value,
    HMAC-SHA1 with the auth token, base64-encoded."""
    data = url
    for key in sorted(params):
        data += key + params[key]
    mac = hmac.new(token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1)
    return base64.b64encode(mac.digest()).decode("ascii")


def _flat(qs: dict[str, list[str]]) -> dict[str, str]:
    return {k: (v[0] if v else "") for k, v in qs.items()}


def verify(headers, url: str, params: dict[str, str]) -> bool:
    """Constant-time Twilio signature check. Fail-closed without a token."""
    token = _auth_token()
    if not token:
        return False
    got = headers.get("X-Twilio-Signature", "")
    if not got:
        return False
    want = _expected_signature(token, url, params)
    return hmac.compare_digest(want, got)


def parse(params: dict[str, str]):
    """(sender, text) from the form fields, or None to ignore."""
    sender = (params.get("From") or "").strip()
    text = (params.get("Body") or "").strip()
    if not sender or not text:
        return None
    return sender, text


def twiml(reply: str) -> bytes:
    """Wrap a reply as TwiML. Empty reply → an empty Response (say nothing)."""
    if reply:
        body = f"<Message>{escape(reply)}</Message>"
    else:
        body = ""
    doc = ('<?xml version="1.0" encoding="UTF-8"?>'
           f"<Response>{body}</Response>")
    return doc.encode("utf-8")


def make_handler(bots: dict | None = None,
                 limiter: "chanbase.RateLimiter | None" = None):
    bots = {} if bots is None else bots
    limiter = limiter or chanbase.RateLimiter(per_minute=8)

    class _Handler(BaseHTTPRequestHandler):
        def _xml(self, code: int, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "text/xml; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802 (stdlib naming)
            try:
                length = min(int(self.headers.get("Content-Length", 0) or 0),
                             _MAX_BODY)
            except ValueError:
                return self._xml(400, twiml(""))
            raw = self.rfile.read(length) if length else b""
            params = _flat(urllib.parse.parse_qs(raw.decode("utf-8", "replace")))
            url = _request_url(self.headers, self.path)
            # Fail-closed auth FIRST, before doing anything with the body.
            try:
                if not verify(self.headers, url, params):
                    return self._xml(403, twiml(""))
            except Exception:
                return self._xml(403, twiml(""))
            client = self.client_address[0] if self.client_address else "?"
            if limiter.hit(f"ip:{client}"):
                return self._xml(429, twiml(""))
            parsed = parse(params)
            if not parsed:
                return self._xml(200, twiml(""))
            sender, text = parsed
            try:
                reply = chanbase.collect_reply("sms", PREFIX, sender, text,
                                               bots, limiter=limiter)
            except Exception:
                # Never leak internals over SMS — an exception string can carry
                # a path or a value. Log it server-side, reply generically.
                import logging
                logging.getLogger("olympus.sms").exception("sms turn failed")
                return self._xml(200, twiml(
                    "Something went wrong handling that — please try again."))
            return self._xml(200, twiml(reply or ""))

        def log_message(self, *a):
            pass

    return _Handler


def notify(text: str, to: str | None = None) -> bool:
    """Proactive outbound SMS via Twilio's REST API. Best-effort; returns False
    (a no-op) when the account/from/recipient credentials aren't configured, so
    it fans out cleanly from `gateway.notify_all`."""
    from . import secretref
    sid = secretref.getenv("TWILIO_ACCOUNT_SID")
    token = _auth_token()
    frm = os.environ.get("TWILIO_FROM", "").strip()
    to = (to or os.environ.get("OLYMPUS_SMS_NOTIFY_TO", "")).strip()
    if not (sid and token and frm and to):
        return False
    try:
        endpoint = (f"https://api.twilio.com/2010-04-01/Accounts/"
                    f"{urllib.parse.quote(sid)}/Messages.json")
        data = urllib.parse.urlencode({"From": frm, "To": to,
                                       "Body": text}).encode()
        req = urllib.request.Request(endpoint, data=data)
        basic = base64.b64encode(f"{sid}:{token}".encode()).decode("ascii")
        req.add_header("Authorization", f"Basic {basic}")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        urllib.request.urlopen(req, timeout=20).read()
        return True
    except Exception as err:
        import logging
        logging.getLogger("olympus.sms").warning("notify failed: %s", err)
        return False


def run_server(host: str = "127.0.0.1", port: int = 8491) -> None:
    if not _auth_token():
        raise SystemExit(
            "Set TWILIO_AUTH_TOKEN — inbound SMS is rejected without a "
            "signature-verification token (a public endpoint with no check "
            "would answer any spoofed number).")
    handler = make_handler()
    print(f"⚡ Olympus SMS channel on {host}:{port}  "
          f"(untrusted by default — pair with `olympus pair sms`)")
    HTTPServer((host, port), handler).serve_forever()

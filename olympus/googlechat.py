"""Olympus Google Chat channel — Chat app webhook in, response out.

A Google Chat **app** receives message events as HTTP POSTs and replies in the
HTTP response. Olympus verifies a shared bearer token on the endpoint (set it
as a static header on the Chat app configuration, or front the endpoint with
one), parses the event, and answers through the shared council pipeline.

Setup:
  1. Create a Google Chat app; point its endpoint at this server.
  2. export GOOGLECHAT_VERIFY_TOKEN=xxxxx   # a secret you also send as a header
  3. python -m olympus googlechat --port 8490
  4. Pair: `olympus pair googlechat`, then `/pair <code>` in the space —
     senders are untrusted by default.

Note: production Google Chat apps should additionally verify the Google-signed
Bearer JWT (audience = your project number). This adapter verifies a shared
token, which is sufficient behind a trusted ingress; the JWT check is a
documented hardening step, not silently assumed.
"""

from __future__ import annotations

import hmac
import os

from . import webhookchan

PREFIX = "gc"


def _verify_token() -> str:
    from . import secretref
    return secretref.getenv("GOOGLECHAT_VERIFY_TOKEN")


def _verify(headers, raw: bytes) -> bool:
    want = _verify_token()
    if not want:
        return False                        # fail closed
    got = (headers.get("Authorization", "").removeprefix("Bearer ").strip()
           or headers.get("X-Olympus-Token", "").strip())
    return hmac.compare_digest(want, got or "")


def _parse(payload: dict):
    """Extract (sender, text) from a Google Chat MESSAGE event."""
    if payload.get("type") not in (None, "MESSAGE"):
        return None
    msg = payload.get("message") or {}
    text = (msg.get("text") or "").strip()
    sender = ((msg.get("sender") or {}).get("name")
              or (payload.get("user") or {}).get("name") or "")
    sender = str(sender).strip()
    if not sender or not text:
        return None
    return sender, text


def _respond(reply: str) -> dict:
    return {"text": reply} if reply else {}


def notify(text: str) -> bool:
    """Proactive push via an incoming webhook URL (space webhook)."""
    from . import secretref
    url = secretref.getenv("GOOGLECHAT_WEBHOOK_URL")
    if not url:
        return False
    try:
        import json
        import urllib.request
        req = urllib.request.Request(
            url, data=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json; charset=UTF-8"})
        urllib.request.urlopen(req, timeout=20).read()
        return True
    except Exception as err:
        import logging
        logging.getLogger("olympus.googlechat").warning("notify failed: %s", err)
        return False


def run_server(host: str = "0.0.0.0", port: int = 8490) -> None:
    if not _verify_token():
        raise SystemExit("Set GOOGLECHAT_VERIFY_TOKEN — inbound calls are "
                         "rejected without it.")
    webhookchan.serve("googlechat", PREFIX, _verify, _parse, _respond,
                      host, port)

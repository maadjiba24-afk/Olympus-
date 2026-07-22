"""Olympus Mattermost channel — outgoing-webhook in, incoming-webhook out.

Mattermost (self-hosted Slack alternative) can POST an **outgoing webhook** to
Olympus when a message matches a trigger, and Olympus replies in the HTTP
response. Proactive pushes go out through an **incoming webhook** URL. Both are
plain HTTP — no SDK.

Setup:
  1. In Mattermost, add an Outgoing Webhook pointing at this server's URL; copy
     its token.
  2. export MATTERMOST_OUTGOING_TOKEN=xxxxx        # verifies inbound calls
     export MATTERMOST_WEBHOOK_URL=https://mm.example/hooks/abc  # for notify
  3. python -m olympus mattermost --port 8489
  4. Pair: run `olympus pair mattermost`, then send `/pair <code>` in the
     watched channel — senders are untrusted by default.

Access control, rate limiting, and routing are shared (chanbase / webhookchan).
"""

from __future__ import annotations

import hmac
import json
import os
import urllib.parse
import urllib.request

from . import webhookchan

PREFIX = "mm"


def _outgoing_token() -> str:
    from . import secretref
    return secretref.getenv("MATTERMOST_OUTGOING_TOKEN")


def _verify(headers, raw: bytes) -> bool:
    """Mattermost outgoing webhooks carry a `token` field. Compare it to the
    configured one in constant time. If none is configured, fail closed (a
    public endpoint with no token would run the council for anyone)."""
    want = _outgoing_token()
    if not want:
        return False
    got = ""
    body = raw.decode("utf-8", "replace")
    ctype = headers.get("Content-Type", "")
    if "application/json" in ctype:
        try:
            got = (json.loads(body or "{}") or {}).get("token", "")
        except ValueError:
            got = ""
    else:                                   # form-encoded outgoing webhook
        got = urllib.parse.parse_qs(body).get("token", [""])[0]
    return hmac.compare_digest(want, got or "")


def _parse(payload: dict):
    """Extract (user_id, text) from a Mattermost outgoing-webhook payload.
    Handles both JSON and the form-encoded `_raw` fallback."""
    if "_raw" in payload:
        q = urllib.parse.parse_qs(payload["_raw"])
        user = q.get("user_name", q.get("user_id", [""]))[0]
        text = q.get("text", [""])[0]
    else:
        user = payload.get("user_name") or payload.get("user_id") or ""
        text = payload.get("text") or ""
    user, text = str(user).strip(), str(text).strip()
    if not user or not text:
        return None
    return user, text


def _respond(reply: str) -> dict:
    # Empty text tells Mattermost to post nothing (used for ignored events).
    return {"text": reply} if reply else {}


def notify(text: str) -> bool:
    """Proactive push via an incoming webhook URL. Best-effort."""
    from . import secretref
    url = secretref.getenv("MATTERMOST_WEBHOOK_URL")
    if not url:
        return False
    try:
        req = urllib.request.Request(
            url, data=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20).read()
        return True
    except Exception as err:
        import logging
        logging.getLogger("olympus.mattermost").warning("notify failed: %s", err)
        return False


def run_server(host: str = "0.0.0.0", port: int = 8489) -> None:
    if not _outgoing_token():
        raise SystemExit("Set MATTERMOST_OUTGOING_TOKEN (from your Mattermost "
                         "outgoing webhook) — inbound calls are rejected "
                         "without it.")
    webhookchan.serve("mattermost", PREFIX, _verify, _parse, _respond,
                      host, port)

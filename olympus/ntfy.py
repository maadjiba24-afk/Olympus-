"""ntfy push channel — one more delivery target for scheduled jobs and alerts.

[ntfy](https://ntfy.sh) is dead-simple pub-sub push: POST a message to a topic
URL and every subscribed phone/desktop gets a notification. No app registration,
no bot token — just a topic name. That makes it the lightest possible "tell me
when it's done" target, alongside the existing Telegram/Discord/Slack/Signal
gateways.

Configuration (all optional):
  NTFY_TOPIC    the topic to publish to (required to be "configured")
  NTFY_SERVER   base URL, default https://ntfy.sh (set for a self-hosted server)
  NTFY_TOKEN    bearer token for a protected topic (optional)

`notify()` is best-effort: it returns False rather than raising when unconfigured
or offline, so a delivery failure never breaks the job that produced the result.
"""

from __future__ import annotations

import logging
import os
import urllib.error
import urllib.request

_DEFAULT_SERVER = "https://ntfy.sh"
_MAX_BYTES = 32 * 1024          # ntfy caps message size; stay well under


def _server() -> str:
    return (os.environ.get("NTFY_SERVER") or _DEFAULT_SERVER).rstrip("/")


def _topic() -> str:
    return (os.environ.get("NTFY_TOPIC") or "").strip().strip("/")


def configured() -> bool:
    return bool(_topic())


def notify(text: str, title: str = "Olympus") -> bool:
    """Publish `text` to the configured ntfy topic. Best-effort → False on any
    failure or when unconfigured."""
    topic = _topic()
    if not topic:
        return False
    from . import secretref
    body = (text or "").encode("utf-8")[:_MAX_BYTES]
    url = f"{_server()}/{topic}"
    headers = {"Content-Type": "text/plain; charset=utf-8"}
    if title:
        # Header values must be latin-1 clean; drop non-latin-1 chars rather
        # than raising (the body still carries the full unicode message).
        headers["Title"] = title.encode("latin-1", "ignore").decode("latin-1")
    token = secretref.getenv("NTFY_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(url, data=body, headers=headers,
                                     method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError) as err:
        logging.getLogger("olympus.ntfy").warning("notify failed: %s", err)
        return False

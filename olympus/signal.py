"""Olympus Signal gateway — zero dependencies, over signal-cli's REST API.

Signal has no official bot API; the community-standard bridge is the
`signal-cli-rest-api` service (run it in Docker next to Olympus). Olympus talks
to it over plain HTTP:

  * notify()/send() — POST /v2/send
  * run_bot()       — poll GET /v1/receive/<number> and route each message
                      through the Olympus pipeline.

Setup:
  # run the bridge, register your number, then:
  export SIGNAL_CLI_REST_URL=http://localhost:8080
  export SIGNAL_NUMBER=+15551234567
  export SIGNAL_NOTIFY_RECIPIENT=+15557654321   # optional, for notify()
  python -m olympus signal
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

from . import chanbase, gateway


_limiter = chanbase.RateLimiter(per_minute=12)


def _base() -> str:
    return os.environ.get("SIGNAL_CLI_REST_URL", "http://localhost:8080").rstrip("/")


def send(recipient: str, text: str,
         number: str | None = None) -> bool:
    number = number or os.environ.get("SIGNAL_NUMBER", "")
    if not number or not recipient:
        return False
    try:
        for part in gateway.chunk(text):
            payload = {"message": part, "number": number,
                       "recipients": [recipient]}
            req = urllib.request.Request(
                f"{_base()}/v2/send", data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=30).read()
        return True
    except Exception:
        return False


def notify(text: str) -> bool:
    recipient = os.environ.get("SIGNAL_NOTIFY_RECIPIENT", "").strip()
    if not recipient:
        return False
    return send(recipient, text)


MAX_VOICE_BYTES = 20 * 1024 * 1024


def _voice_text(attachment: dict) -> str | None:
    """Download a signal-cli attachment and transcribe it. None when it isn't
    audio, is too big, or transcription is unavailable."""
    if not str(attachment.get("contentType", "")).startswith("audio/"):
        return None
    att_id = attachment.get("id")
    if not att_id or int(attachment.get("size") or 0) > MAX_VOICE_BYTES:
        return None
    try:
        req = urllib.request.Request(f"{_base()}/v1/attachments/{att_id}")
        with urllib.request.urlopen(req, timeout=60) as resp:
            blob = resp.read(MAX_VOICE_BYTES + 1)
        if len(blob) > MAX_VOICE_BYTES:
            return None
        from . import media
        transcript = media.transcribe_bytes(
            blob, filename=str(attachment.get("filename") or "voice.aac"))
    except Exception:
        return None
    if transcript.startswith("Error"):
        return None
    return f"[voice note] {transcript}"


def parse_messages(raw: list, transcribe=None) -> list[tuple[str, str]]:
    """Extract (source_number, text) pairs from a signal-cli /v1/receive list.
    Voice notes become text via transcription when a media API key is
    configured (injectable via `transcribe` for tests)."""
    transcribe = transcribe or _voice_text
    out = []
    for item in raw or []:
        env = item.get("envelope") if isinstance(item, dict) else None
        if not env:
            continue
        msg = env.get("dataMessage") or {}
        text = msg.get("message")
        source = env.get("source") or env.get("sourceNumber")
        if not text:
            for att in msg.get("attachments") or []:
                text = transcribe(att) if isinstance(att, dict) else None
                if text:
                    break
        if text and source:
            out.append((source, text))
    return out


def _receive(number: str) -> list:
    req = urllib.request.Request(f"{_base()}/v1/receive/{number}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def run_bot() -> None:
    number = os.environ.get("SIGNAL_NUMBER", "")
    if not number:
        raise SystemExit("Set SIGNAL_NUMBER (the registered signal-cli number).")
    print(f"⚡ Olympus is live on Signal as {number}  (Ctrl-C to stop)")
    bots: dict = {}
    while True:
        try:
            raw = _receive(number)
        except Exception:
            time.sleep(5)
            continue
        for source, text in parse_messages(raw):
            try:
                # Routed through chanbase (pairing → allowlist → rate limit →
                # council), like matrix/sms/mattermost. Signal previously fed
                # every sender straight into `gateway.reply_for`: no pairing,
                # no allowlist, no limiter — so anyone who knew the number ran
                # the full council on the operator's key, and the codes minted
                # by `olympus pair signal` were never consulted by anything.
                chanbase.route(
                    "signal", "sg", source, text, bots,
                    lambda part, _s=source: send(_s, part, number=number),
                    limiter=_limiter)
            except Exception:
                pass
        time.sleep(1)

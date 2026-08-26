"""Email gateway — Olympus answers by email, reusing the Gmail adapter.

Polls the inbox for unread mail and runs each message through the SAME shared
gateway pipeline as Telegram/Discord/Slack/Signal (per-user memory, slash
commands, the full verified council), then replies to the sender and marks the
message read so it isn't answered twice.

Safe by construction:
  * It only ever REPLIES to the sender — it never initiates mail.
  * Email is untrusted content, so the pipeline's capability separation keeps an
    inbound message from ever triggering an action tool.
  * A sender allowlist (OLYMPUS_EMAIL_ALLOW, comma-separated addresses) can lock
    it to known correspondents; empty = reply to anyone who writes.
"""

from __future__ import annotations

import os
import time

from . import gateway, gmail


def _allowed(sender: str) -> bool:
    allow = [a.strip().lower() for a in
             os.environ.get("OLYMPUS_EMAIL_ALLOW", "").split(",") if a.strip()]
    return not allow or (sender or "").lower() in allow


def _reply_subject(subject: str) -> str:
    s = (subject or "").strip() or "your message"
    return s if s.lower().startswith("re:") else f"Re: {s}"


def handle_message(bots: dict, msg: dict) -> str | None:
    """Process one email dict ({sender, subject, body, authenticated}) and
    return the reply text that was sent (or None if the sender wasn't allowed).
    Pure except for the gmail.send call — the poll loop handles fetch/mark-read.

    Spoof-guard: when an allowlist is in force, sender identity is load-bearing
    — so the message must also carry Gmail's DMARC/SPF+DKIM pass verdict
    (msg["authenticated"]). A From: header alone proves nothing; without this
    check anyone could impersonate an allowlisted sender with one forged
    header. No allowlist = identity isn't trusted anyway, so no auth demand."""
    sender = msg.get("sender", "")
    if not _allowed(sender):
        return None
    allowlist_active = bool(os.environ.get("OLYMPUS_EMAIL_ALLOW", "").strip())
    if allowlist_active and not msg.get("authenticated", False):
        return None
    subject = msg.get("subject", "")
    body = (msg.get("body", "") or "").strip()
    # Give the pipeline the subject as context plus the body as the request.
    text = f"{subject}\n\n{body}".strip() if subject else body
    # The allowlist already defines mailbox equality case-insensitively. Bind
    # the owner with the same canonical form so one authenticated mailbox
    # cannot mint multiple quota/preference identities via header casing.
    sender_key = sender.strip().lower()
    chunks = gateway.reply_for(bots, sender_key, text, prefix="email")
    reply = "\n\n".join(chunks)
    gmail.send(sender, _reply_subject(subject), reply)
    return reply


def poll_once(bots: dict, max_messages: int = 10) -> list[str]:
    """Fetch unread mail, answer each, mark it read. Returns a short log."""
    log: list[str] = []
    for msg in gmail.list_unread(max_results=max_messages):
        try:
            sent = handle_message(bots, msg)
            gmail.mark_read(msg["id"])
            if sent is None:
                log.append(f"skipped {msg.get('sender', '?')} (not allowed)")
            else:
                log.append(f"replied to {msg.get('sender', '?')}")
        except Exception as err:
            log.append(f"error on {msg.get('id', '?')}: {str(err)[:100]}")
    return log


def run(poll_seconds: int = 60) -> None:
    """Long-running poll loop. Requires the Gmail adapter to be configured."""
    if not gmail.configured():
        raise SystemExit("Gmail isn't connected. Set GMAIL_ACCESS_TOKEN / "
                         "GMAIL_REFRESH_TOKEN (see the OAuth setup).")
    print(f"⚡ Olympus email gateway is live — polling every {poll_seconds}s "
          "(Ctrl-C to stop)")
    bots: dict = {}
    while True:
        try:
            for line in poll_once(bots):
                print(f"  {line}")
        except Exception as err:
            print(f"  [poll error] {str(err)[:150]}")
        time.sleep(max(10, poll_seconds))

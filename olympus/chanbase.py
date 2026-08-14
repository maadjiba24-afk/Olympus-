"""Shared, security-hardened base for chat channels.

Every messaging platform Olympus speaks to (Telegram, Matrix, Mattermost, …)
faces the same access-control problem, and it is exactly the problem that
produced the surveyed gateway's worst CVEs: an allowlist or pairing check that silently
grants *authority* instead of merely *access*. This module keeps those two
concerns strictly apart:

  * ACCESS (here) — may this sender talk to the assistant at all? Untrusted by
    default; opened only by an explicit env allowlist, a one-time pairing code
    (via `pairing`), or a deliberately-`open` policy.
  * AUTHORITY (elsewhere) — what may an allowed conversation actually do? That
    stays with the capability-profile + action/autonomy spine, which a channel
    can never widen. So being on a channel allowlist can NEVER escalate to
    owner powers — the failure mode that broke the surveyed gateway.

A channel module only handles transport (auth to the platform, parse an
incoming message, send an outgoing one) and calls into here for the access
decision, per-sender rate limiting, and the throttled "you're not paired"
hint. Routing to the council itself goes through `gateway.reply_for`, the same
single pipeline every channel shares.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque

from . import pairing


def _env(channel: str, suffix: str, default: str = "") -> str:
    return os.environ.get(f"OLYMPUS_{channel.upper()}_{suffix}", default)


def policy(channel: str) -> str:
    """Access policy for `channel`: pairing (default, untrusted) | allowlist |
    open. A per-channel override wins over the global OLYMPUS_CHANNEL_DM_POLICY;
    an unrecognized value falls back to the safe default (pairing)."""
    val = (_env(channel, "DM_POLICY")
           or os.environ.get("OLYMPUS_CHANNEL_DM_POLICY", "")).strip().lower()
    return val if val in ("pairing", "allowlist", "open") else "pairing"


def env_allowed_ids(channel: str) -> set[str]:
    """Sender ids always allowed for `channel` (OLYMPUS_<CHANNEL>_ALLOWED_IDS,
    comma-separated), plus the owner-notify id if one is configured."""
    raw = _env(channel, "ALLOWED_IDS").strip()
    ids = {x.strip() for x in raw.split(",") if x.strip()} if raw else set()
    owner = _env(channel, "NOTIFY_ID").strip()
    if owner:
        ids.add(owner)
    return ids


def allowed(channel: str, sender_id) -> bool:
    """Untrusted by default: a sender may talk only when explicitly allowlisted
    by env, paired with a one-time code, or the policy was deliberately opened.
    This decides ACCESS only — never authority."""
    pol = policy(channel)
    if pol == "open":
        return True
    if str(sender_id) in env_allowed_ids(channel):
        return True
    if pol == "pairing":
        return pairing.is_paired(channel, sender_id)
    return False        # allowlist policy and not on the list


class RateLimiter:
    """Per-sender sliding-window limiter. A chat channel runs the FULL council
    on the operator's key; without a limit, one chatty or hostile sender is a
    key-burn DoS. Window is 60s; the limiter bounds its own memory."""

    def __init__(self, per_minute: int = 12):
        self.per_minute = per_minute
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> bool:
        """Record a request for `key`; return True if it is over the limit."""
        if self.per_minute <= 0:
            return False
        now = time.time()
        with self._lock:
            if len(self._hits) > 5000:
                self._hits.clear()
            q = self._hits.setdefault(str(key), deque())
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return True
            q.append(now)
        return False


_DENIED_AT: dict[str, float] = {}
_DENY_EVERY = 3600


def pair_hint(channel: str) -> str:
    return (f"🔒 This Olympus instance is private.\n"
            f"If it's yours: run `olympus pair {channel}` on the machine it "
            f"lives on, then send /pair <code>.")


def deny_once(channel: str, sender_id, send_fn) -> None:
    """Tell an unpaired sender how pairing works — at most once per hour per
    sender, so a scraper can't turn the bot into a reply machine. `send_fn`
    takes the hint text; any error is swallowed."""
    key = f"{channel}:{sender_id}"
    now = time.time()
    if now - _DENIED_AT.get(key, 0) >= _DENY_EVERY:
        _DENIED_AT[key] = now
        try:
            send_fn(pair_hint(channel))
        except Exception:
            pass


def try_pair(channel: str, sender_id, text: str) -> str | None:
    """If `text` is a `/pair <code>` command, redeem it and return the reply;
    otherwise None (not a pairing command). Lets every channel share one
    pairing entry point."""
    parts = (text or "").strip().split(None, 1)
    if not parts or parts[0].lower() != "/pair":
        return None
    code = parts[1] if len(parts) > 1 else ""
    if pairing.redeem(channel, code, sender_id):
        return ("✓ Paired. This conversation can now talk to Olympus — "
                "/help to start.")
    return ("That code didn't match (codes expire after a few minutes). "
            "Run `olympus pair " + channel + "` again.")


def route(channel: str, prefix: str, sender_id, text: str, bots: dict,
          send_fn, limiter: "RateLimiter | None" = None) -> bool:
    """The shared inbound flow for a simple (non-streaming) channel:

      pairing command → access check → rate limit → gateway.reply_for → send

    `send_fn(text)` delivers one reply string on this channel. `uid` is the
    per-conversation namespace `<prefix>-<sender_id>`. Returns True when a
    reply (or pairing/deny response) was produced, False when the message was
    dropped (rate-limited). Never raises for control-flow reasons."""
    from . import gateway
    paired = try_pair(channel, sender_id, text)
    if paired is not None:
        send_fn(paired)
        return True
    if not allowed(channel, sender_id):
        deny_once(channel, sender_id, send_fn)
        return False
    if limiter is not None and limiter.hit(str(sender_id)):
        send_fn("You're sending requests faster than I can safely run them — "
                "give it a moment.")
        return False
    uid = f"{prefix}-{sender_id}"
    for chunk in gateway.reply_for(bots, str(sender_id), text,
                                   prefix=prefix, uid=uid):
        send_fn(chunk)
    return True


def collect_reply(channel: str, prefix: str, sender_id, text: str, bots: dict,
                  limiter: "RateLimiter | None" = None) -> str | None:
    """Reply-RETURNING variant of `route`, for webhook channels that answer in
    the HTTP response instead of via a separate send call. Returns the reply
    text (pairing/deny messages included), or None when the message was dropped
    (rate-limited). Same access spine as `route` — untrusted by default."""
    paired = try_pair(channel, sender_id, text)
    if paired is not None:
        return paired
    if not allowed(channel, sender_id):
        # A webhook can't throttle a per-sender hint across requests as cleanly
        # as a poller, but the access decision is identical: deny.
        return pair_hint(channel)
    if limiter is not None and limiter.hit(str(sender_id)):
        return ("You're sending requests faster than I can safely run them — "
                "give it a moment.")
    from . import gateway
    uid = f"{prefix}-{sender_id}"
    chunks = gateway.reply_for(bots, str(sender_id), text, prefix=prefix,
                               uid=uid)
    return "\n\n".join(chunks)

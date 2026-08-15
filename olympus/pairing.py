"""Channel pairing — untrusted-by-default DM access with one-time codes.

A chat channel (Telegram today; any transport tomorrow) should not answer
strangers. Instead of the old "open unless TELEGRAM_ALLOWED_CHAT_IDS is set"
posture, a channel asks this module whether a sender is paired. The owner runs
`olympus pair telegram` on the machine (proof of machine access), gets a short
one-time code, and sends it to the bot as `/pair <code>`. The sender id is
then remembered — no secrets travel through the chat besides the short-lived
code itself.

Codes are single-use, expire after PAIR_TTL seconds, and are stored hashed
(SHA-256) so a leaked pairing file never reveals a live code. Paired ids are
plain — they are identifiers, not secrets.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time

from . import config

PAIR_TTL = int(os.environ.get("OLYMPUS_PAIR_TTL", "600"))  # seconds a code lives
_CODE_LEN = 8


def _path():
    d = config.MEMORY_DIR / "pairing"
    d.mkdir(parents=True, exist_ok=True)
    return d / "pairing.json"


def _load() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("codes", {})    # channel -> [{hash, ts}]
    data.setdefault("paired", {})   # channel -> {sender_id: {ts}}
    return data


def _save(data: dict) -> None:
    path = _path()
    tmp = path.with_suffix(".tmp")
    from . import atomicio
    atomicio.publish(tmp, path, json.dumps(data, indent=1))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _hash(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


def issue_code(channel: str) -> str:
    """Mint a one-time pairing code for `channel` (call from the CLI, on the
    machine). Alphabet avoids 0/O and 1/I lookalikes."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    code = "".join(secrets.choice(alphabet) for _ in range(_CODE_LEN))
    data = _load()
    now = time.time()
    live = [c for c in data["codes"].get(channel, [])
            if now - c.get("ts", 0) <= PAIR_TTL]
    live.append({"hash": _hash(code), "ts": now})
    data["codes"][channel] = live[-5:]      # at most a handful outstanding
    _save(data)
    return code


def redeem(channel: str, code: str, sender_id: str) -> bool:
    """Pair `sender_id` when `code` matches a live one-time code. The code is
    consumed whether or not anything else goes right afterwards."""
    if not code or not code.strip():
        return False
    data = _load()
    now = time.time()
    target = _hash(code)
    live = [c for c in data["codes"].get(channel, [])
            if now - c.get("ts", 0) <= PAIR_TTL]
    match = next((c for c in live if secrets.compare_digest(
        c.get("hash", ""), target)), None)
    data["codes"][channel] = [c for c in live if c is not match]
    if match is None:
        _save(data)                          # persist expiry cleanup anyway
        return False
    data["paired"].setdefault(channel, {})[str(sender_id)] = {"ts": now}
    _save(data)
    return True


def is_paired(channel: str, sender_id) -> bool:
    return str(sender_id) in _load()["paired"].get(channel, {})


def paired(channel: str) -> list[str]:
    return sorted(_load()["paired"].get(channel, {}))


def unpair(channel: str, sender_id) -> bool:
    data = _load()
    entry = data["paired"].get(channel, {}).pop(str(sender_id), None)
    _save(data)
    return entry is not None

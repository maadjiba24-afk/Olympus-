"""Conversational onboarding — a warm first-contact for chat channels.

The surveyed gateway greets a brand-new user with a guided setup conversation. Olympus is
already provisioned server-side (the operator brings the keys), so the goal
here is different: the *first* time a given chat identity talks to Olympus,
introduce what it is and how to get the most out of it — once, and never again
for that identity.

State is a tiny JSON set of the user keys we've already welcomed, under
`MEMORY_DIR/onboarded.json`. Everything is best-effort: onboarding never blocks
or breaks a reply, so any I/O error just means the user isn't marked seen yet.
"""

from __future__ import annotations

import json
import threading

from . import config

_LOCK = threading.Lock()


def _path():
    return config.MEMORY_DIR / "onboarded.json"


def _load() -> set[str]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return set(data) if isinstance(data, list) else set()
    except Exception:
        return set()


def is_new(uid: str) -> bool:
    """True if this chat identity has never been welcomed."""
    if not uid:
        return False
    with _LOCK:
        return uid not in _load()


def mark_seen(uid: str) -> None:
    """Record that this identity has been welcomed (idempotent, best-effort)."""
    if not uid:
        return
    with _LOCK:
        seen = _load()
        if uid in seen:
            return
        seen.add(uid)
        try:
            config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
            _path().write_text(json.dumps(sorted(seen)), encoding="utf-8")
        except Exception:
            pass


def reset(uid: str) -> None:
    """Forget one identity (so it will be welcomed again). Test/support helper."""
    with _LOCK:
        seen = _load()
        if uid in seen:
            seen.discard(uid)
            try:
                _path().write_text(json.dumps(sorted(seen)), encoding="utf-8")
            except Exception:
                pass


def _nspec() -> int:
    try:
        from .specialists import SPECIALISTS
        return len(SPECIALISTS)
    except Exception:
        return 0


def welcome() -> str:
    """The full first-contact introduction (shown on a new user's /start)."""
    n = _nspec()
    council = (f"a verified council of {n} AI specialists"
               if n else "a verified council of AI specialists")
    return (
        f"👋 Welcome — I'm Olympus, {council} that debate, cross-check, and "
        "verify each other before I answer, so you get the strongest reply "
        "rather than the first one.\n\n"
        "You can just talk to me in plain language. A few things to try:\n"
        "• Ask a hard question — I'll route it through the full council.\n"
        "• “Watch this video and learn from it” — /watch <url>\n"
        "• “Keep an eye on X and ping me” — /goal <what you want>\n"
        "• /usage to see tokens and cost, /model to pick a brain, "
        "/lang for your language.\n\n"
        "I reply carefully by default and can go quicker on simple asks "
        "(/fast auto). Type /help any time for the full list. What would you "
        "like to do first?"
    )

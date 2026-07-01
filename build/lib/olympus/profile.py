"""Per-user profile card — the always-loaded, cached identity/preference seed.

The cheapest, highest-leverage memory: a tiny, stable block of "who this user
is and how they like things done" that rides on every Zeus turn. Because it's
small and changes rarely, it sits inside the cached system prefix — so it costs
almost nothing per call after the first, yet means the user stops re-explaining
themselves. This is the first, deliberately minimal slice of the larger memory
architecture: durable, inspectable, user-editable, and never auto-written from
untrusted content.

Stored under the user's `prefs` (a small JSON doc); derived facts (language,
autonomy level, granted scopes) are read live from the same place so the card
never drifts from the real settings.
"""

from __future__ import annotations

from . import prefs

_AUTONOMY_NAMES = {0: "suggest only", 1: "prepare, wait for approval",
                   2: "approve to act", 3: "auto safe actions",
                   4: "standing authority"}


def get(user: str) -> dict:
    """The user's editable profile doc: {about, facts{...}}."""
    return dict(prefs.get(user, "profile", {}) or {})


def set_about(user: str, text: str) -> str:
    """Set the freeform 'about me' note (empty string clears it)."""
    p = get(user)
    text = (text or "").strip()
    if text:
        p["about"] = text
    else:
        p.pop("about", None)
    prefs.set(user, "profile", p)
    return "Cleared your profile note." if not text else \
        "Saved. Olympus will remember this across conversations."


def set_fact(user: str, key: str, value: str) -> str:
    """Set or clear one structured fact (e.g. role, company, timezone)."""
    p = get(user)
    facts = dict(p.get("facts", {}))
    key = key.strip().lower()
    value = (value or "").strip()
    if value:
        facts[key] = value
    else:
        facts.pop(key, None)
    p["facts"] = facts
    prefs.set(user, "profile", p)
    return f"Updated '{key}'." if value else f"Cleared '{key}'."


def clear(user: str) -> str:
    prefs.set(user, "profile", {})
    return "Cleared your whole profile (Olympus's saved settings still apply)."


def card(user: str) -> str:
    """Build the compact, cache-friendly profile block. Returns '' when there's
    nothing to say, so an empty profile costs zero tokens — only non-empty
    lines are emitted (no padding)."""
    p = get(user)
    lines: list[str] = []

    about = p.get("about")
    if about:
        lines.append(f"- About: {about}")

    facts = p.get("facts") or {}
    for key in sorted(facts):
        lines.append(f"- {key.capitalize()}: {facts[key]}")

    lang = prefs.get(user, "language", None)
    if lang and str(lang).lower() != "auto":
        lines.append(f"- Preferred language: {lang}")

    # Autonomy/permissions only when the user has moved them off the defaults,
    # so we never spend tokens stating the obvious.
    autonomy = prefs.get(user, "autonomy", 1)
    try:
        autonomy = int(autonomy)
    except (TypeError, ValueError):
        autonomy = 1
    if autonomy != 1:
        name = _AUTONOMY_NAMES.get(autonomy, f"L{autonomy}")
        lines.append(f"- Autonomy preference: L{autonomy} ({name})")

    scopes = prefs.get(user, "scopes", []) or []
    if scopes:
        lines.append(f"- Has granted these action permissions: {', '.join(scopes)}")

    if not lines:
        return ""
    return ("\n\n## What you know about this user (remember it; ask only if "
            "something's missing)\n" + "\n".join(lines))

"""Per-conversation and per-specialist model pins.

The pool normally assigns each pipeline role to its strongest configured
member. A pin overrides that for one conversation ("/model haiku" — this
chat runs cheap) or one council member (OLYMPUS_SPECIALIST_MODELS) without
touching how anyone else runs.

Pins select among the models the operator ALREADY configured — a shorthand
like "opus"/"sonnet"/"gpt" matches a pool member's provider/model name. A
pin can never invent credentials or switch a BYOK visitor onto the
operator's keys (it only applies to default env-pool sessions).
"""

from __future__ import annotations

from . import config, memory, prefs

_PREF_KEY = "model_pin"


def match(pool: config.ModelPool, token: str) -> config.Settings | None:
    """The pool member whose provider/model matches `token` (case-insensitive
    substring), e.g. 'opus', 'gpt', 'openai/gpt-5', 'deepseek'."""
    token = (token or "").strip().lower()
    if not token:
        return None
    for member in pool.members:
        if token in f"{member.provider}/{member.model}".lower():
            return member
    return None


def get_pin(user: str) -> str | None:
    # EXACT principal: prefs are per-owner settings, and `safe_id` here pinned
    # every colliding principal's conversations to one model.
    return prefs.get(memory.canonical_owner(user), _PREF_KEY)


def set_pin(user: str, token: str) -> str:
    """Pin `user`'s conversations to a configured model, or clear with
    'auto'. Validates against the operator pool so a typo can't strand the
    conversation on nothing."""
    uid = memory.canonical_owner(user)
    token = (token or "").strip().lower()
    if token in ("", "auto", "off", "none", "clear"):
        prefs.set(uid, _PREF_KEY, None)
        return ("Model pin cleared — the pool picks the best model per task "
                "again.")
    pool = config.ModelPool.from_env()
    member = match(pool, token)
    if member is None:
        options = ", ".join(f"{m.provider}/{m.model or '(default)'}"
                            for m in pool.members)
        return (f"No configured model matches '{token}'. "
                f"Configured: {options}. Use /model auto to unpin.")
    prefs.set(uid, _PREF_KEY, token)
    return (f"📌 This conversation is pinned to {member.provider}/"
            f"{member.model or '(default)'} — /model auto to unpin.")


def resolve(user: str) -> config.Settings | None:
    """The Settings a pinned conversation should run on (None = no pin or the
    pinned model is no longer configured — fail open to normal selection)."""
    token = get_pin(user)
    if not token:
        return None
    try:
        return match(config.ModelPool.from_env(), token)
    except Exception:
        return None


def status(user: str) -> str:
    pool = config.ModelPool.from_env()
    token = get_pin(user)
    if token:
        member = match(pool, token)
        if member:
            head = (f"📌 Pinned to {member.provider}/"
                    f"{member.model or '(default)'} (/model auto to unpin)")
        else:
            head = (f"Pin '{token}' no longer matches a configured model — "
                    f"using normal selection. /model auto clears it.")
    else:
        head = "No pin — the pool picks the best model per task."
    return head + "\n" + pool.assignment()

"""Per-conversation capability profiles — scoped tool/access boundaries.

The security gate (cmdguard) decides what a *command* may do; capability
separation (security.filter_tools) keeps ingestion and action apart within a
run. This layer scopes what a *conversation* may reach at all: a Telegram
guest, a shared group chat, and the owner's terminal should not see the same
loadout, even though they run the same pipeline.

A profile is a named boundary: tools denied (or an explicit whitelist) plus a
cap on the autonomy level actions can reach. Assignment is operator-side only
(CLI `olympus restrict`, or OLYMPUS_CHANNEL_PROFILE as the default for chat
channels) — a conversation can inspect its own boundary but never widen it.

Built-ins:
  full    — no restriction (the owner's own surfaces)
  reader  — research allowed; anything that acts on the world, mutates
            Olympus, or executes code is out; autonomy capped at L1
  guest   — Q&A only: reader minus local files, email/calendar, memory
            writes and subagents; autonomy capped at L0 (suggest only)
"""

from __future__ import annotations

import json

from . import config, memory, prefs, security

_READER_DENY = frozenset(security.ACTION_TOOLS) | {
    "prepare_action", "schedule_task", "code_execution",
    "create_skill", "gate_skills", "gate_prompt",
    "operator_authorize_site", "operator_forget_site",
    "operator_remember_login", "operator_review", "operator_schedule",
    "propose_playbook", "set_advanced_mode",
    "site_profile_record", "site_template_record", "browser_skill_record",
}

_GUEST_DENY = _READER_DENY | {
    "read_file", "list_dir", "read_source_file", "list_source_files",
    "read_email", "read_inbox", "read_calendar",
    "save_lesson", "search_sessions", "spawn_subagent",
    "generate_image", "text_to_speech",
}

BUILTIN: dict[str, dict] = {
    "full":   {"deny": [], "allow": [], "max_autonomy": 4},
    "reader": {"deny": sorted(_READER_DENY), "allow": [], "max_autonomy": 1},
    "guest":  {"deny": sorted(_GUEST_DENY), "allow": [], "max_autonomy": 0},
}

# Conversations arriving over a chat transport (vs. the owner's terminal).
CHANNEL_PREFIXES = ("tg-", "dc-", "sl-", "sg-", "wa-", "email-", "hook-")

_PREF_KEY = "capability_profile"

# If a restriction was explicitly selected but its definition can no longer be
# resolved, the only safe interpretation is the most restrictive built-in
# profile.  Falling back to ``full`` would turn a missing/corrupt policy file
# into an authorization widening.
_FAIL_CLOSED_PROFILE = "guest"


def _custom() -> dict[str, dict]:
    """Operator-defined profiles from capability_profiles.json (same shape as
    BUILTIN values); malformed files are ignored, never fatal."""
    path = config.MEMORY_DIR / "capability_profiles.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out = {}
    if isinstance(data, dict):
        for name, spec in data.items():
            if not isinstance(name, str) or not name.strip() \
                    or not isinstance(spec, dict):
                continue
            deny = spec.get("deny", []) or []
            allow = spec.get("allow", []) or []
            cap = spec.get("max_autonomy", 4)
            if (not isinstance(deny, list)
                    or not all(isinstance(t, str) and t.strip() for t in deny)
                    or not isinstance(allow, list)
                    or not all(isinstance(t, str) and t.strip() for t in allow)
                    or not isinstance(cap, int) or isinstance(cap, bool)
                    or not 0 <= cap <= 4):
                # Ignore this definition as unresolved.  An assignment that
                # names it will be clamped to the fail-closed profile below.
                continue
            out[name] = {
                "deny": list(deny),
                "allow": list(allow),
                "max_autonomy": cap,
            }
    return out


def profiles() -> dict[str, dict]:
    # Built-ins are security constants.  A custom file may add profiles, but it
    # must never shadow ``guest`` (the quarantine/fail-closed posture), or relax
    # ``reader``/``full`` under a familiar name.
    return {**_custom(), **BUILTIN}


def get(name: str) -> dict | None:
    if not isinstance(name, str):
        return None
    return profiles().get(name.strip().lower() or None)


def _resolve_name(value) -> str | None:
    """Return a normalized, currently defined profile name, else ``None``."""
    if not isinstance(value, str):
        return None
    name = value.strip().lower()
    return name if name and get(name) is not None else None


def assign(user: str, name: str) -> str:
    # EXACT principal. A capability profile carries `max_autonomy`, so
    # normalizing here scoped every colliding principal to one profile:
    # restricting `tg-a.b` to "guest" restricted `tg-a-b` too, and widening one
    # widened them all.
    if get(name) is None:
        known = ", ".join(sorted(profiles()))
        return f"No profile '{name}'. Known profiles: {known}."
    uid = memory.canonical_owner(user)
    prefs.set(uid, _PREF_KEY, name.strip().lower())
    return f"'{uid}' is now scoped to the '{name}' profile."


def clear(user: str) -> str:
    uid = memory.canonical_owner(user)
    prefs.set(uid, _PREF_KEY, None)
    return f"'{uid}' restriction cleared."


def of_user(user: str | None = None) -> str:
    """The profile governing `user` (default: the active conversation).
    Explicit assignment wins; otherwise chat-channel conversations get the
    OLYMPUS_CHANNEL_PROFILE default (if set); otherwise full."""
    import os
    uid = memory.canonical_owner(user) if user else memory.current_owner()
    assigned = prefs.get(uid, _PREF_KEY)
    if assigned is not None:
        # An explicit restriction is a security decision.  If its custom
        # definition disappears, becomes corrupt, or the stored name is
        # malformed, retain a restrictive posture instead of silently widening
        # the conversation to ``full``.
        return _resolve_name(assigned) or _FAIL_CLOSED_PROFILE
    if uid.startswith(CHANNEL_PREFIXES):
        configured = os.environ.get("OLYMPUS_CHANNEL_PROFILE", "").strip()
        if configured:
            # A non-empty but invalid operator setting is evidence that a
            # restriction was intended.  Treat loss/corruption of that policy
            # the same way as an unresolved explicit assignment.
            return _resolve_name(configured) or _FAIL_CLOSED_PROFILE
    return "full"


def _blocked(name: str, spec: dict) -> bool:
    allow = spec.get("allow") or []
    if allow:
        return name not in allow
    return name in (spec.get("deny") or [])


def filter_tools(tool_defs, user: str | None = None):
    """Strip tools outside the conversation's boundary. Matches on a def's
    name, falling back to its server-side type (code_execution, web_search)."""
    spec = get(of_user(user))
    if not spec or (not spec.get("deny") and not spec.get("allow")):
        return tool_defs
    out = []
    for d in tool_defs:
        if isinstance(d, dict):
            name = d.get("name") or ""
            typ = str(d.get("type") or "")
            base = typ.split("_2")[0] if typ else ""   # code_execution_20250522
            if _blocked(name, spec) or (base and _blocked(base, spec)):
                continue
        out.append(d)
    return out


def autonomy_cap(user: str | None = None) -> int:
    spec = get(of_user(user))
    return int(spec.get("max_autonomy", 4)) if spec else 4


def summary(user: str | None = None) -> str:
    uid = memory.canonical_owner(user) if user else memory.current_owner()
    name = of_user(uid)
    spec = get(name) or {}
    lines = [f"Capability profile for {uid}: {name}"]
    if spec.get("allow"):
        lines.append("allowed tools: " + ", ".join(sorted(spec["allow"])))
    elif spec.get("deny"):
        lines.append(f"denied tools: {len(spec['deny'])} "
                     f"(acting/self-modifying capabilities)")
    else:
        lines.append("no tool restrictions")
    lines.append(f"autonomy cap: L{spec.get('max_autonomy', 4)}")
    return "\n".join(lines)

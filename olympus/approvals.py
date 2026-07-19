"""Surface approval-gated actions for plain-English confirmation.

A normal person should be able to say "yes" to confirm an irreversible action
instead of running `olympus approve <id>`. This thin layer lists the actions
awaiting approval and maps a yes/no to the existing spine (`actions.approve` /
`actions.reject`) — the governance is unchanged; only the interface is friendly.

Beyond the terminal, the same spine is now reachable from any chat channel:
`summary()` renders the queue, `footer()` appends a compact card to a reply
when a turn left something held for approval, and `handle_command()` maps
`/approvals`, `/approve <id>` and `/deny <id>` onto the spine. Every execution
still runs through the security gate (cmdguard via the sandbox) — chat only
changes where the yes/no happens, never what is allowed.
"""

from __future__ import annotations

from . import actions


def pending(user: str) -> list:
    """Actions awaiting the user's approval (PREPARED only — APPROVED already ran)."""
    return [a for a in actions.pending(user) if a.status == actions.PREPARED]


def approve(user: str, action_id: str):
    return actions.approve(user, action_id)


def reject(user: str, action_id: str, reason: str = "declined"):
    return actions.reject(user, action_id, reason)


# --- chat-facing rendering & command handling -------------------------------

def _card(action) -> str:
    what = (action.preview or action.title or "").strip()
    if len(what) > 400:
        what = what[:400] + "…"
    return f"#{action.id} — {what}"


def summary(user: str) -> str:
    """Human list of everything currently held for approval."""
    held = pending(user)
    if not held:
        return "Nothing is waiting for approval."
    lines = ["⏸ Waiting for your approval:"]
    lines += [_card(a) for a in held]
    lines.append("Reply /approve <id> or /deny <id> [reason].")
    return "\n".join(lines)


def footer(user: str) -> str:
    """Compact card appended to a chat reply when the turn held actions for
    approval — so the person is told in-channel instead of finding out later.
    Empty string when nothing is pending."""
    held = pending(user)
    if not held:
        return ""
    lines = ["", "⏸ Held for approval:"]
    lines += [_card(a) for a in held]
    lines.append("Reply /approve <id> or /deny <id>.")
    return "\n".join(lines)


def handle_command(user: str, cmd: str, arg: str) -> str | None:
    """Map a chat slash-command onto the approval spine. Returns the reply
    text, or None when `cmd` is not an approval command."""
    cmd = (cmd or "").lower()
    if cmd in ("/approvals", "/pending"):
        return summary(user)
    if cmd == "/approve":
        action_id = (arg or "").strip().lstrip("#")
        if not action_id:
            return "Usage: /approve <id> — see /approvals for the queue."
        try:
            done = approve(user, action_id)
        except Exception as err:
            return f"Could not approve #{action_id}: {err}"
        if getattr(done, "status", "") == actions.EXECUTED:
            return f"✓ #{action_id} approved and executed."
        return (f"#{action_id}: {getattr(done, 'status', 'unknown')}"
                f"{': ' + done.error if getattr(done, 'error', '') else ''}")
    if cmd in ("/deny", "/reject"):
        action_id, _, reason = (arg or "").strip().partition(" ")
        action_id = action_id.lstrip("#")
        if not action_id:
            return "Usage: /deny <id> [reason]"
        try:
            reject(user, action_id, reason.strip() or "declined in chat")
        except Exception as err:
            return f"Could not deny #{action_id}: {err}"
        return f"✗ #{action_id} denied."
    return None

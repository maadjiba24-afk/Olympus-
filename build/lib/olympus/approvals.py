"""Surface approval-gated actions for plain-English confirmation.

A normal person should be able to say "yes" to confirm an irreversible action
instead of running `olympus approve <id>`. This thin layer lists the actions
awaiting approval and maps a yes/no to the existing spine (`actions.approve` /
`actions.reject`) — the governance is unchanged; only the interface is friendly.
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

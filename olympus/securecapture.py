"""Out-of-band credential capture for the operator's 'remember' mode.

The model must never see a password. The flow: a tool records a *pending
request* (domain only, in the user's prefs); the interactive layer later prompts
the user through a private input (getpass) the model never observes, and stores
the result in the encrypted vault. The secret is born and dies outside the model
loop. See docs/DESIGN_OPERATOR_UX.md.
"""

from __future__ import annotations

from . import prefs

_PENDING_KEY = "pending_secure_login"


def request(user: str, domain: str) -> None:
    """Record that a private credential prompt is owed for `domain`."""
    prefs.set(user, _PENDING_KEY, (domain or "").strip().lower())


def pending(user: str) -> str | None:
    """The domain awaiting a secure prompt, if any."""
    return prefs.get(user, _PENDING_KEY) or None


def clear(user: str) -> None:
    prefs.set(user, _PENDING_KEY, None)


def fulfill(user: str, domain: str, username: str, password: str) -> str:
    """Store the captured credentials in the vault and clear the request. Only
    ever called by the interactive layer with values from a private prompt."""
    from . import operator
    operator.remember_credentials(user, domain, username, password)
    clear(user)
    return (domain or "").strip().lower()

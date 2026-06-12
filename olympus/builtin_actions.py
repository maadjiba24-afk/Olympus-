"""Built-in action types registered on the Action spine.

These prove the spine end-to-end and are the first real "things Olympus can do":
  - save_note   : trivial + reversible (demonstrates auto-execute + undo)
  - send_email  : irreversible, scope-gated, always needs explicit approval
  - call_webhook: irreversible, scope-gated

Sending an email or hitting a webhook reuses the existing allowlist-gated
handlers in tools.py, so the operating-assistant layer inherits the same
safety as before. New capabilities (calendar, files, payments-prep) are just
new ActionTypes registered here.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import actions, config, memory, tools


# --- save_note: trivial, reversible -------------------------------------

def _notes_dir(user: str) -> Path:
    d = config.MEMORY_DIR / "notes" / memory.safe_id(user)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _note_preview(p: dict) -> str:
    return f"Save a note titled '{p.get('title', 'note')}':\n{p.get('body', '')[:500]}"


def _note_execute(p: dict) -> dict:
    user = p.get("_user", "shared")
    fname = f"{int(time.time())}-{memory.safe_id(p.get('title', 'note'))}.md"
    path = _notes_dir(user) / fname
    path.write_text(f"# {p.get('title', 'note')}\n\n{p.get('body', '')}\n",
                    encoding="utf-8")
    return {"path": str(path)}


def _note_undo(result: dict) -> str:
    path = Path(result.get("path", ""))
    if path.exists():
        path.unlink()
    return "note deleted"


# --- send_email: irreversible, scope-gated ------------------------------

def _email_preview(p: dict) -> str:
    return (f"Send email\n  To: {p.get('to', '?')}\n"
            f"  Subject: {p.get('subject', '?')}\n\n{p.get('body', '')[:1000]}")


def _email_execute(p: dict) -> dict:
    msg = tools._send_email(p.get("to", ""), p.get("subject", ""),
                            p.get("body", ""))
    if msg.lower().startswith("error"):
        raise RuntimeError(msg)
    return {"status": msg}


# --- call_webhook: irreversible, scope-gated ----------------------------

def _webhook_preview(p: dict) -> str:
    return f"POST to webhook '{p.get('name', '?')}' with payload:\n{p.get('payload', {})}"


def _webhook_execute(p: dict) -> dict:
    msg = tools._call_webhook(p.get("name", ""), p.get("payload", {}))
    if msg.lower().startswith("error"):
        raise RuntimeError(msg)
    return {"status": msg}


def register_builtins() -> None:
    actions.register(actions.ActionType(
        name="save_note", risk_class=actions.TRIVIAL, scope="notes",
        preview=_note_preview, execute=_note_execute, undo=_note_undo,
        description="Save a note to the user's notes (reversible)."))
    actions.register(actions.ActionType(
        name="send_email", risk_class=actions.IRREVERSIBLE, scope="email",
        preview=_email_preview, execute=_email_execute,
        description="Send an email via the configured SMTP account."))
    actions.register(actions.ActionType(
        name="call_webhook", risk_class=actions.IRREVERSIBLE, scope="webhook",
        preview=_webhook_preview, execute=_webhook_execute,
        description="POST a payload to an operator-configured webhook."))


register_builtins()

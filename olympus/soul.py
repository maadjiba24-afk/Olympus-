"""Soul — an editable personality file the owner injects into Zeus.

`~/.olympus/soul.md` is the owner's own voice-and-values directive: tone,
priorities, house style, things to always/never do. It is first-party, trusted
content (the operator wrote it), so it is injected directly into Zeus's system
prompt — unlike web/tool content, which is untrusted and enveloped.

Kept small and optional: no file means no injection (Olympus's built-in
personality stands). `olympus soul` shows it; `olympus soul edit` opens it.
"""

from __future__ import annotations

import os
from pathlib import Path

# Cap the injected size so a runaway soul file can't dominate the context.
_MAX_CHARS = 4000

_TEMPLATE = """\
# Olympus — Soul

This file is your personal directive to Olympus. Whatever you write here is
woven into how Zeus (the voice you talk to) thinks and replies. Delete the
examples and make it yours.

## Voice & tone
- (e.g. Direct and warm. Skip preamble. No corporate filler.)

## Priorities
- (e.g. Optimize for my time. Lead with the decision, then the why.)

## Always / never
- Always: (e.g. show trade-offs when you recommend something)
- Never: (e.g. pad answers to sound thorough)
"""


def soul_path() -> Path:
    home = Path(os.environ.get("OLYMPUS_HOME", Path.home() / ".olympus"))
    return home / "soul.md"


def read() -> str:
    """The soul text, or '' when there is no soul file."""
    p = soul_path()
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8").strip()[:_MAX_CHARS]
    except OSError:
        return ""


def block() -> str:
    """A system-prompt injection block, or '' when no soul is configured.

    Trusted (owner-authored) — but still fenced with a clear heading so it reads
    as a standing directive, not as user-turn content."""
    text = read()
    if not text:
        return ""
    return ("\n\n## Owner's soul (personal directive — honor this in every "
            "reply)\n" + text + "\n")


def ensure_scaffold() -> Path:
    """Create the soul file from a template if it doesn't exist yet."""
    p = soul_path()
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_TEMPLATE, encoding="utf-8")
    return p


def edit() -> str:
    """Open the soul file in $EDITOR (nano/vi fallback). Returns a status line."""
    import shutil
    import subprocess
    p = ensure_scaffold()
    editor = (os.environ.get("EDITOR") or os.environ.get("VISUAL")
              or ("nano" if shutil.which("nano") else "vi"))
    try:
        subprocess.call([editor, str(p)])
    except Exception as err:
        return f"Could not launch '{editor}': {err}\nEdit it yourself: {p}"
    return f"Edited {p}. Its voice now shapes every reply."


def show() -> str:
    text = read()
    if not text:
        return (f"No soul configured. Create one with `olympus soul edit` "
                f"(saved to {soul_path()}).")
    return f"Olympus soul ({soul_path()}):\n\n{text}"

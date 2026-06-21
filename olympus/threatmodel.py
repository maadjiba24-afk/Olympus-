"""Bind the threat model to the live tool surface.

Olympus exposes a finite, *named* set of tools (`tools.HANDLERS`) — unlike a
harness that auto-registers hundreds. That makes a real threat model tractable:
every exposed tool must have an entry in `docs/THREAT_MODEL.md` (capability,
trust boundary, deny-first default, abuse case), and no entry may describe a
tool that doesn't exist. This module enforces that binding, the same way
`capabilities.py` binds the README numbers to the code.
"""

from __future__ import annotations

import re
from pathlib import Path

# A documented tool is a table row whose first cell is a `backtick` name.
_ROW_RE = re.compile(r"^\|\s*`([a-z0-9_]+)`\s*\|")


def doc_path() -> Path:
    return Path(__file__).resolve().parent.parent / "docs" / "THREAT_MODEL.md"


def exposed_tools() -> set[str]:
    """The live tool surface the model can actually call."""
    from . import tools
    return set(tools.HANDLERS)


def documented_tools(text: str) -> set[str]:
    """Tool names with a row in the threat-model table."""
    return {m for line in text.splitlines() for m in _ROW_RE.findall(line)}


def check(text: str) -> list[str]:
    """Return problems binding the threat model to the tool surface; empty=OK."""
    exposed, documented = exposed_tools(), documented_tools(text)
    problems = []
    for tool in sorted(exposed - documented):
        problems.append(f"tool '{tool}' is exposed but has no THREAT_MODEL.md "
                        "entry — every capability must be threat-modeled.")
    for tool in sorted(documented - exposed):
        problems.append(f"THREAT_MODEL.md documents '{tool}', which is not an "
                        "exposed tool (stale entry — remove it).")
    return problems


def check_repo() -> list[str]:
    path = doc_path()
    if not path.exists():
        return ["docs/THREAT_MODEL.md is missing."]
    return check(path.read_text(encoding="utf-8"))

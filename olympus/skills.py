"""The skill library Olympus builds for itself.

A skill is a reusable how-to document distilled from experience: lessons,
corrections, feedback, and watched videos get synthesized into named skills
by Metis (daily) and any specialist who learns something procedural. Every
specialist sees the skill index and loads relevant skills on demand — so
knowledge gained once is applied by the whole council forever.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from . import config


def _dir() -> Path:
    d = config.MEMORY_DIR / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "skill"


def create(name: str, description: str, instructions: str) -> str:
    """Create or overwrite a skill. Returns a confirmation message."""
    path = _dir() / f"{_slug(name)}.md"
    existed = path.exists()
    path.write_text(
        f"# {name.strip()}\n\n"
        f"> {description.strip()}\n\n"
        f"{instructions.strip()}\n\n"
        f"_Last updated: {time.strftime('%Y-%m-%d %H:%M')}_\n",
        encoding="utf-8",
    )
    verb = "updated" if existed else "created"
    return f"Skill '{name}' {verb} ({path.name}). It is now visible to every specialist."


def read(name: str) -> str:
    path = _dir() / f"{_slug(name)}.md"
    if not path.exists():
        available = ", ".join(p.stem for p in sorted(_dir().glob("*.md"))) or "none"
        return f"No skill named '{name}'. Available skills: {available}"
    return path.read_text(encoding="utf-8", errors="replace")[:20_000]


def index() -> str:
    """One line per skill: shown to every specialist in its system prompt."""
    lines = []
    for path in sorted(_dir().glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        name = text.splitlines()[0].lstrip("# ").strip() if text else path.stem
        desc = ""
        for line in text.splitlines():
            if line.startswith("> "):
                desc = line[2:].strip()
                break
        lines.append(f"- {name}: {desc}")
    return "\n".join(lines) if lines else "(no skills built yet)"


def count() -> int:
    return len(list(_dir().glob("*.md")))

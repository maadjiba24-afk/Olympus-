"""The skill library Olympus builds for itself.

A skill is a reusable how-to document distilled from experience: lessons,
corrections, feedback, and watched videos get synthesized into named skills
by Metis (daily) and any specialist who learns something procedural. Every
specialist sees the skill index and loads relevant skills on demand — so
knowledge gained once is applied by the whole council forever.

Autonomously-created skills are written **provisional** and proven by a
benchmark before they become permanent (see orchestrator.gate_skills): if a
new/changed skill doesn't measurably raise the affected specialist's score, it is
reverted. That makes the autonomous self-improvement path safe enough to run
without a human in the loop.
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


def _backup_dir() -> Path:
    d = config.MEMORY_DIR / "skill_backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "skill"


def _meta(text: str, key: str) -> str | None:
    m = re.search(rf"<!--\s*{key}:\s*(.*?)\s*-->", text)
    return m.group(1) if m else None


def _title(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def create(name: str, description: str, instructions: str, *,
           specialist: str | None = None, provisional: bool = False) -> str:
    """Create or overwrite a skill. Returns a confirmation message.

    `specialist` tags which specialist the skill primarily serves (used to
    pick which benchmark gates it). `provisional` marks it unproven until a
    benchmark promotes it.
    """
    path = _dir() / f"{_slug(name)}.md"
    existed = path.exists()
    if existed:  # back up the prior version so a revert can restore it
        (_backup_dir() / path.name).write_text(
            path.read_text(encoding="utf-8"), encoding="utf-8")
    elif (_backup_dir() / path.name).exists():
        # brand-new skill: clear any stale backup so revert means "delete"
        (_backup_dir() / path.name).unlink()

    header = ""
    if specialist:
        header += f"<!-- specialist: {specialist} -->\n"
    if provisional:
        header += "<!-- provisional -->\n"
    path.write_text(
        f"{header}# {name.strip()}\n\n"
        f"> {description.strip()}\n\n"
        f"{instructions.strip()}\n\n"
        f"_Last updated: {time.strftime('%Y-%m-%d %H:%M')}_\n",
        encoding="utf-8",
    )
    verb = "updated" if existed else "created"
    tag = " (provisional — pending benchmark)" if provisional else ""
    return (f"Skill '{name}' {verb}{tag}. It is visible to every specialist.")


def read(name: str) -> str:
    path = _dir() / f"{_slug(name)}.md"
    if not path.exists():
        available = ", ".join(p.stem for p in sorted(_dir().glob("*.md"))) or "none"
        return f"No skill named '{name}'. Available skills: {available}"
    return path.read_text(encoding="utf-8", errors="replace")[:20_000]


GLOBAL_SPECIALIST = "all"   # tag a skill `specialist: all` to share it with everyone


def _visible_to(owner: str | None, specialist: str | None) -> bool:
    """Whether a skill owned by `owner` should appear for `specialist`.

    `specialist=None` means "show everything" (the human `olympus skills` view).
    A skill with no owner, or owner 'all', is global. A skill tagged for one
    specialist is shown ONLY to that specialist — so a benchmark-gated skill
    can't silently degrade other specialists that merely see it in a shared
    index (the cross-contamination the gate couldn't catch)."""
    if specialist is None:
        return True
    if not owner or owner == GLOBAL_SPECIALIST:
        return True
    return owner == specialist


def index(specialist: str | None = None) -> str:
    """One line per skill. With `specialist`, scope to the skills that
    specialist should actually see (its own + global); without it, list all."""
    lines = []
    for path in sorted(_dir().glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if not _visible_to(_meta(text, "specialist"), specialist):
            continue
        name = _title(text) or path.stem
        desc = ""
        for line in text.splitlines():
            if line.startswith("> "):
                desc = line[2:].strip()
                break
        prov = " (provisional)" if _meta(text, "provisional") is not None \
            or "<!-- provisional -->" in text else ""
        lines.append(f"- {name}: {desc}{prov}")
    return "\n".join(lines) if lines else "(no skills built yet)"


def count() -> int:
    return len(list(_dir().glob("*.md")))


# --- provisional lifecycle (benchmark gating) ----------------------------

def list_provisional() -> list[tuple[str, str | None]]:
    """Return (skill_name, specialist) for every provisional skill."""
    out = []
    for path in _dir().glob("*.md"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "<!-- provisional -->" in text:
            out.append((_title(text) or path.stem, _meta(text, "specialist")))
    return out


def set_hidden(name: str, hidden: bool) -> None:
    """Hide/unhide a skill from the index so before/after benchmarks isolate
    its effect. Hidden skills are renamed .md -> .hidden."""
    base = _dir() / f"{_slug(name)}.md"
    hide = _dir() / f"{_slug(name)}.hidden"
    if hidden and base.exists():
        base.rename(hide)
    elif not hidden and hide.exists():
        hide.rename(base)


def promote(name: str) -> None:
    """Clear the provisional flag — the skill has proven itself."""
    path = _dir() / f"{_slug(name)}.md"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    path.write_text(re.sub(r"<!--\s*provisional\s*-->\n?", "", text),
                    encoding="utf-8")


def revert(name: str) -> str:
    """Undo a provisional skill: restore its prior version, or delete it if it
    was brand new."""
    path = _dir() / f"{_slug(name)}.md"
    backup = _backup_dir() / f"{_slug(name)}.md"
    if backup.exists():
        path.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")
        backup.unlink()
        return f"reverted '{name}' to previous version"
    if path.exists():
        path.unlink()
        return f"removed new skill '{name}'"
    return f"nothing to revert for '{name}'"

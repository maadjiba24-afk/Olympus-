"""`/journey` — the browsable timeline of what Olympus has learned.

`/learned` answers "what happened recently"; this answers "how did the
system get here": every skill built (and whether it proved out), every
lesson, correction and prompt upgrade, in one chronological view — with the
ability to inspect any entry in full and to delete the ones that shouldn't
shape future behaviour (a wrong lesson keeps doing damage until removed).

Entries get a short stable ref (content-addressed from their path) so
`journey show <ref>` / `journey rm <ref>` work across renders. Deleting a
skill archives it recoverably (skills.archive); deleting a memory note
removes the file — both are the operator curating the system's memory,
which is exactly the surface the learning loop expects to be curated.
"""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

from . import config, memory, skills

# Memory categories that represent LEARNING (not operational logs).
_CATEGORIES = ("lessons", "corrections", "feedback", "upgrades")
_KIND_ICON = {"skill": "🛠", "lessons": "📖", "corrections": "✏️",
              "feedback": "👍", "upgrades": "⬆️"}


def _ref(path: Path) -> str:
    return hashlib.sha256(str(path).encode()).hexdigest()[:8]


def _note_entries(user: str) -> list[dict]:
    out = []
    for cat in _CATEGORIES:
        dirs = [memory._dir(cat)]
        if cat in memory.USER_SCOPED and user != "shared":
            dirs.append(memory._dir(cat, user))
        for d in dirs:
            for p in d.glob("*.md"):
                # filenames: YYYYMMDD-HHMMSS[-pid]-slug.md (the pid segment
                # was added by ADR 0005's collision-proof memory.save; old
                # notes don't have it — tolerate both)
                try:
                    ts = time.mktime(time.strptime(p.name[:15],
                                                   "%Y%m%d-%H%M%S"))
                except ValueError:
                    ts = p.stat().st_mtime
                try:                     # the real title lives in the note
                    title = memory.note_title(
                        p.read_text(encoding="utf-8"))
                except OSError:
                    title = ""
                if not title:            # fallback: derive from the stem
                    m = re.match(r"\d{8}-\d{6}-(?:\d+-)?(.*)", p.stem)
                    title = (m.group(1) if m else p.stem).replace("-", " ") \
                        or p.stem
                out.append({"ref": _ref(p), "ts": ts, "kind": cat,
                            "title": title, "path": p})
    return out


def _skill_entries() -> list[dict]:
    out = []
    for p in skills._dir().glob("*.md"):
        text = p.read_text(encoding="utf-8", errors="replace")
        title = skills._title(text) or p.stem
        prov = "<!-- provisional -->" in text
        out.append({"ref": _ref(p), "ts": p.stat().st_mtime, "kind": "skill",
                    "title": title + (" (provisional)" if prov else ""),
                    "path": p})
    return out


def entries(user: str = "shared") -> list[dict]:
    """Every learning event, oldest first."""
    return sorted(_note_entries(memory.safe_id(user)) + _skill_entries(),
                  key=lambda e: e["ts"])


def timeline(user: str = "shared", limit: int = 30) -> str:
    """Human-readable journey, newest last (reads bottom-up like a log)."""
    evs = entries(user)
    if not evs:
        return ("Nothing learned yet — the journey starts once skills, "
                "lessons, or corrections accumulate.")
    lines = [f"Learning journey — {len(evs)} event(s), showing the last "
             f"{min(limit, len(evs))}. `journey show <ref>` for detail, "
             "`journey rm <ref>` to remove one."]
    for e in evs[-limit:]:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(e["ts"]))
        icon = _KIND_ICON.get(e["kind"], "·")
        lines.append(f"  {when}  {icon} [{e['ref']}] {e['kind']}: {e['title']}")
    return "\n".join(lines)


def _find(ref: str, user: str = "shared") -> dict | None:
    return next((e for e in entries(user) if e["ref"] == ref), None)


def show(ref: str, user: str = "shared") -> str:
    e = _find(ref, user)
    if e is None:
        return f"No journey entry '{ref}' — see `journey` for refs."
    body = e["path"].read_text(encoding="utf-8", errors="replace")[:8000]
    return f"[{e['ref']}] {e['kind']}: {e['title']}\n\n{body}"


def remove(ref: str, user: str = "shared") -> str:
    """Delete one learning event. Skills archive recoverably; notes unlink."""
    e = _find(ref, user)
    if e is None:
        return f"No journey entry '{ref}' — see `journey` for refs."
    if e["kind"] == "skill":
        name = skills._title(e["path"].read_text(encoding="utf-8",
                                                 errors="replace")) \
            or e["path"].stem
        return "Removed: " + skills.archive(name, "removed via journey")
    try:
        e["path"].unlink()
    except OSError as err:
        return f"Could not remove [{ref}]: {err}"
    return (f"Removed {e['kind']} entry '{e['title']}' — it will no longer "
            "shape future answers.")

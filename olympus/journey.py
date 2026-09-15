"""Owner-bound learning timeline with content-bound destructive references."""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

from . import config, memory, skills, note_evidence as notes

_CATEGORIES = ("lessons", "corrections", "feedback", "upgrades")
_KIND_ICON = {"skill": "🛠", "lessons": "📖", "corrections": "✏️", "feedback": "👍", "upgrades": "⬆️"}


def _ref(path: Path, raw: bytes | None = None) -> str:
    raw = notes.read_raw(path) if raw is None else raw
    if raw is None:
        raise notes.NoteStateError("journey entry disappeared")
    return hashlib.sha256(str(notes.logical(path)).encode() + b"\0" + raw).hexdigest()


def _note_entries(user: str) -> list[dict]:
    out = []
    for category in _CATEGORIES:
        for row in memory._note_rows(category, user, include_shared=True):
            created = row["meta"].get("created")
            try:
                stamp = time.mktime(time.strptime(created or row["path"].name[:15], "%Y%m%d-%H%M%S"))
            except (ValueError, OverflowError):
                stamp = notes.io(row["path"]).stat().st_mtime
            out.append({"ref": _ref(row["path"], row["raw"]), "ts": stamp,
                        "kind": category, "title": memory.note_title(row["body"]),
                        "path": row["path"], "sha256": row["sha256"], "owner": row["owner"]})
    return out


def _skill_entries() -> list[dict]:
    out = []
    for path in notes.inventory(config.MEMORY_DIR / "skills"):
        if path.suffix != ".md":
            continue
        raw = notes.read_raw(path)
        if raw is None:
            raise notes.NoteStateError("skill disappeared during journey inspection")
        try:
            text = raw.decode("utf-8")
        except UnicodeError as err:
            raise notes.NoteStateError("invalid UTF-8 skill in journey") from err
        title = skills._title(text) or path.stem
        out.append({"ref": _ref(path, raw), "ts": notes.io(path).stat().st_mtime,
                    "kind": "skill", "title": title + (" (provisional)" if "<!-- provisional -->" in text else ""),
                    "path": path, "sha256": notes.digest(raw), "owner": "shared"})
    return out


def entries(user: str | None = None) -> list[dict]:
    exact = memory.current_owner() if user is None else memory.canonical_owner(user)
    with notes.guard():
        return sorted(_note_entries(exact) + _skill_entries(), key=lambda entry: (entry["ts"], entry["ref"]))


def timeline(user: str | None = None, limit: int = 30) -> str:
    notes.evidence.integer(limit, minimum=1, maximum=notes.MAX_FILES)
    rows = entries(user)
    if not rows:
        return "Nothing learned yet — the journey starts once skills, lessons, or corrections accumulate."
    lines = [f"Learning journey — {len(rows)} event(s), showing the last {min(limit, len(rows))}. "
             "`journey show <ref>` for detail, `journey rm <ref>` to remove one."]
    for entry in rows[-limit:]:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry["ts"]))
        lines.append(f"  {when}  {_KIND_ICON.get(entry['kind'], '·')} [{entry['ref']}] "
                     f"{entry['kind']}: {entry['title']}")
    return "\n".join(lines)


def _find(ref: str, user: str | None = None) -> dict | None:
    matches = [entry for entry in entries(user) if entry["ref"] == ref]
    if len(matches) > 1:
        raise notes.NoteStateError("ambiguous journey reference")
    return matches[0] if matches else None


def show(ref: str, user: str | None = None) -> str:
    with notes.guard():
        entry = _find(ref, user)
        if entry is None:
            return f"No journey entry '{ref}' — see `journey` for current refs."
        raw = notes.read_raw(entry["path"])
        if notes.digest(raw) != entry["sha256"]:
            raise notes.NoteStateError("journey entry changed during read")
        return f"[{entry['ref']}] {entry['kind']}: {entry['title']}\n\n{raw.decode('utf-8')[:8000]}"


def remove(ref: str, user: str | None = None) -> str:
    exact = memory.current_owner() if user is None else memory.canonical_owner(user)
    with notes.guard():
        entry = _find(ref, exact)
        if entry is None:
            return f"No journey entry '{ref}' — see `journey` for current refs."
        if entry["owner"] != exact and exact not in memory.SYSTEM_OWNERS | {"cli"}:
            return "Shared system entries can be removed only by the installation operator."
        raw = notes.read_raw(entry["path"])
        if notes.digest(raw) != entry["sha256"]:
            raise notes.NoteStateError("journey entry changed before removal")
        if entry["kind"] == "skill":
            # Same recovery protocol preserves the actual inspected skill bytes;
            # never derive another source path from its possibly different title.
            backup = config.MEMORY_DIR / "skill_backups" / ("pruned-journey-" + ref + ".md")
            prior = notes.read_raw(backup)
            if prior is not None and prior != raw:
                raise notes.NoteStateError("skill recovery destination conflicts")
            changes = {notes.relative(entry["path"]): None, notes.relative(backup): raw}
            expected = {notes.relative(entry["path"]): entry["sha256"], notes.relative(backup): notes.digest(prior)}
            notes.transact(changes, expected)
            return "Removed: archived '" + entry["title"] + "' → skill_backups/" + backup.name
        notes.delete_rows([entry])
        return f"Removed {entry['kind']} entry '{entry['title']}' — it will no longer shape future answers."

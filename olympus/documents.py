"""User documents — a small, per-user Markdown workspace.

The first slice of the Phase-3 "workspace": a place for the user to keep
documents that the assistant can also read and (with approval) write. Storage
mirrors the memory/prefs convention — one directory per user under
`memory/users/<safe_id>/documents/`, one Markdown file per document — so it is
dependency-free, inspectable on disk, and namespaced per user.

Writes go through the approval spine (a NOTABLE, reversible `write_document`
action), never directly from a tool: the assistant proposes a document and the
user confirms, and every change is reversible. Reads are side-effect-free.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from . import config, memory

_MAX_BYTES = 1_000_000            # a single document cap (1 MB of Markdown)


def _dir(user: str) -> Path:
    safe = memory.safe_id(user)
    base = (config.MEMORY_DIR / "users" / safe) if safe != "shared" \
        else config.MEMORY_DIR
    d = base / "documents"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:80] \
        or "document"


def _backup_dir(user: str) -> Path:
    d = _dir(user).parent / "document_backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _title(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def path_for(user: str, name: str) -> Path:
    return _dir(user) / f"{_slug(name)}.md"


def exists(user: str, name: str) -> bool:
    return path_for(user, name).is_file()


def save(user: str, name: str, content: str) -> dict:
    """Create or overwrite a document. Returns a result dict carrying the prior
    content (so the write can be undone). Enforces the size cap."""
    content = content or ""
    if len(content.encode("utf-8")) > _MAX_BYTES:
        raise ValueError(f"document exceeds the {_MAX_BYTES // 1000}KB limit")
    target = path_for(user, name)
    existed = target.is_file()
    prior = target.read_text(encoding="utf-8", errors="replace") if existed \
        else None
    if existed:                                  # keep a backup for undo
        (_backup_dir(user) / target.name).write_text(prior, encoding="utf-8")
    target.write_text(content, encoding="utf-8")
    return {"user": user, "name": name, "path": str(target),
            "existed": existed, "prior": prior,
            "bytes": len(content.encode("utf-8"))}


def undo_save(result: dict) -> str:
    """Reverse a save: restore prior content, or delete a newly-created doc."""
    path = Path(result.get("path", ""))
    if result.get("existed") and result.get("prior") is not None:
        path.write_text(result["prior"], encoding="utf-8")
        return f"restored previous contents of '{result.get('name')}'"
    if path.exists():
        path.unlink()
        return f"deleted new document '{result.get('name')}'"
    return "nothing to undo"


def read(user: str, name: str) -> str | None:
    target = path_for(user, name)
    if not target.is_file():
        return None
    return target.read_text(encoding="utf-8", errors="replace")


def delete(user: str, name: str) -> bool:
    target = path_for(user, name)
    if not target.is_file():
        return False
    # back the file up before removing, so a delete is recoverable by hand
    (_backup_dir(user) / f"deleted-{int(time.time())}-{target.name}").write_text(
        target.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
    target.unlink()
    return True


def listing(user: str) -> list[dict]:
    """Metadata for every document, newest-updated first."""
    out = []
    for p in _dir(user).glob("*.md"):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            st = p.stat()
        except OSError:
            continue
        out.append({"name": _title(text) or p.stem, "slug": p.stem,
                    "title": _title(text), "chars": len(text),
                    "updated": st.st_mtime})
    out.sort(key=lambda d: d["updated"], reverse=True)
    return out


def render_list(user: str) -> str:
    docs = listing(user)
    if not docs:
        return "No documents yet."
    lines = []
    for d in docs:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(d["updated"]))
        lines.append(f"- {d['name']}  ({d['chars']} chars, updated {when})")
    return "\n".join(lines)

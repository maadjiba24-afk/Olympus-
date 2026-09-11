"""User documents — a small, per-user Markdown workspace.

The first slice of the Phase-3 "workspace": a place for the user to keep
documents that the assistant can also read and (with approval) write. Storage
uses `owners/<full-owner-key>/workspace-v2/documents/`, one Markdown file per
document. Normalized legacy paths remain preserved and unclaimed.

Writes go through the approval spine (a NOTABLE, reversible `write_document`
action), never directly from a tool: the assistant proposes a document and the
user confirms, and every change is reversible. Reads are side-effect-free.
"""

from __future__ import annotations

import re
import hashlib
import stat
import uuid
import time
from pathlib import Path

from . import memory, owner_evidence as evidence

_MAX_BYTES = 1_000_000            # a single document cap (1 MB of Markdown)


def _dir(user: str) -> Path:
    return evidence.workspace(user) / "documents"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:80] \
        or "document"


def _backup_dir(user: str) -> Path:
    return evidence.workspace(user) / "document_backups"


def _title(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def path_for(user: str, name: str) -> Path:
    return _dir(user) / f"{_slug(name)}.md"


def exists(user: str, name: str) -> bool:
    return read(user, name) is not None


def save(user: str, name: str, content: str) -> dict:
    """Publish owned text after preserving the exact previous bytes."""
    user = evidence.exact(user)
    if not isinstance(content, str):
        raise ValueError("document content must be text")
    raw = content.encode("utf-8")
    if len(raw) > _MAX_BYTES:
        raise ValueError(f"document exceeds the {_MAX_BYTES // 1000}KB limit")
    with evidence.guard(user):
        target = path_for(user, name)
        prior = _read_raw(target)
        if prior is None and len(_listing(user)) >= 1000:
            raise evidence.OwnerEvidenceStateError("documents", "document-count bound exceeded")
        if prior is not None:
            backup = _backup_dir(user) / ("saved-" + uuid.uuid4().hex + "-" + target.name)
            evidence.publish(backup, prior, "documents")
        evidence.publish(target, raw, "documents")
    return {"user": user, "name": name, "path": str(target),
            "existed": prior is not None,
            "prior": None if prior is None else prior.decode("utf-8"),
            "content_sha256": hashlib.sha256(raw).hexdigest(),
            "prior_sha256": None if prior is None else hashlib.sha256(prior).hexdigest(),
            "bytes": len(raw)}


def undo_save(result: dict, *, user: str | None = None) -> str:
    """Undo only the authenticated owner's unchanged, recorded publication."""
    owner = evidence.exact(memory.current_owner() if user is None else user)
    if not isinstance(result, dict) or result.get("user") != owner:
        raise PermissionError("document undo owner mismatch")
    target = path_for(owner, result.get("name", ""))
    if result.get("path") != str(target):
        raise PermissionError("document undo path mismatch")
    with evidence.guard(owner):
        current = _read_raw(target)
        if (current is None or hashlib.sha256(current).hexdigest()
                != result.get("content_sha256")):
            raise evidence.OwnerEvidenceStateError("documents", "stale or unverified undo receipt")
        if result.get("existed") is True:
            prior = result.get("prior")
            if (not isinstance(prior, str) or len(prior.encode("utf-8")) > _MAX_BYTES
                    or hashlib.sha256(prior.encode("utf-8")).hexdigest()
                    != result.get("prior_sha256")):
                raise evidence.OwnerEvidenceStateError("documents", "invalid undo evidence")
            backup = _backup_dir(owner) / ("undo-" + uuid.uuid4().hex + "-" + target.name)
            evidence.publish(backup, current, "documents")
            evidence.publish(target, prior.encode("utf-8"), "documents")
            return f"restored previous contents of '{result.get('name')}'"
        if result.get("existed") is not False or result.get("prior") is not None:
            raise evidence.OwnerEvidenceStateError("documents", "invalid undo evidence")
        evidence.unlink(target, "documents")
        return f"deleted new document '{result.get('name')}'"


def _read_raw(path):
    raw = evidence.read_bytes(path, _MAX_BYTES, "documents")
    if raw is not None:
        try:
            raw.decode("utf-8")
        except UnicodeError as err:
            raise evidence.OwnerEvidenceStateError("documents", "invalid UTF-8") from err
    return raw


def read(user: str, name: str) -> str | None:
    raw = _read_raw(path_for(user, name))
    return None if raw is None else raw.decode("utf-8")


def delete(user: str, name: str) -> bool:
    with evidence.guard(user):
        target = path_for(user, name)
        raw = _read_raw(target)
        if raw is None:
            return False
        backup = _backup_dir(user) / ("deleted-" + uuid.uuid4().hex + "-" + target.name)
        evidence.publish(backup, raw, "documents")
        evidence.unlink(target, "documents")
        return True


def listing(user: str) -> list[dict]:
    """Bounded metadata; unavailable documents never disappear from a report."""
    with evidence.guard(user):
        return _listing(user)


def _listing(user):
    out = []
    directory = _dir(user)
    try:
        evidence._parents(directory, "documents")
        try:
            info = evidence._io(directory).lstat()
        except FileNotFoundError:
            return []
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise evidence.OwnerEvidenceStateError("documents", "invalid document directory")
        paths = []
        for path in evidence._io(directory).glob("*.md"):
            paths.append(path)
            if len(paths) > 1000:
                raise evidence.OwnerEvidenceStateError("documents", "document-count bound exceeded")
        for path in paths:
            # Use the ordinary namespace spelling for the parent checks.
            original = directory / path.name
            raw = _read_raw(original)
            if raw is None:
                raise evidence.OwnerEvidenceStateError("documents", "document disappeared during listing")
            body = raw.decode("utf-8")
            updated = path.stat().st_mtime
            out.append({"name": _title(body) or path.stem, "slug": path.stem,
                        "title": _title(body), "chars": len(body), "updated": updated})
    except OSError as err:
        raise evidence.OwnerEvidenceStateError("documents", "listing unavailable") from err
    out.sort(key=lambda row: row["updated"], reverse=True)
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

"""Scoped file-memory archives; validation precedes journaled publication."""
from __future__ import annotations

import io
import gzip
import json
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import time

from . import config, memory, note_evidence as notes

MAX_ARCHIVE = notes.MAX_BATCH + 8 * 1024 * 1024


def roots(user=None, all_users=False):
    base = config.MEMORY_DIR
    exact = memory.canonical_owner(user)
    if all_users:
        return [base / name for name in
                (*memory.CATEGORIES, "users", "owners", "conversations", "notes")]
    result = []
    for category in (*memory.CATEGORIES, notes.ACTION_CATEGORY):
        if exact != "shared" and category not in (
            memory.USER_SCOPED | memory.PRIVATE_CATEGORIES | {notes.ACTION_CATEGORY}
        ):
            continue
        result.append(notes.directory(exact, category))
    return list(dict.fromkeys(result))


def collect(roots):
    found = set()
    for root in roots:
        found.update(notes.inventory(root, recursive=True))
        if len(found) > notes.MAX_FILES:
            raise notes.NoteStateError("archive file inventory bound exceeded")
    return sorted(found)


def external_read(path, cap):
    path = notes.io(path)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400):
        raise ValueError("archive is not a regular file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("archive changed during read")
        raw = stream.read(cap + 1)
    if len(raw) > cap:
        raise ValueError("archive size bound exceeded")
    return raw


def external_publish(path, raw):
    """Publish a caller-selected export/mirror without following a link."""
    path = notes.logical(path)
    for parent in path.parents:
        if parent.exists():
            info = notes.io(parent).lstat()
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & 0x400):
                raise ValueError("linked or non-directory export parent")
    if notes.io(path).exists() or notes.io(path).is_symlink():
        external_read(path, MAX_ARCHIVE)
    notes.io(path.parent).mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".note-export-", dir=notes.io(path.parent))
    staging = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, notes.io(path))
        notes.sync_dir(path.parent)
    finally:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass  # Preserve inaccessible staging without masking the failure.


def _validate_scope(scope):
    if not isinstance(scope, dict):
        raise ValueError("archive must declare its owner scope")
    if set(scope) == {"all"} and scope["all"] is True:
        return
    if (set(scope) != {"user"} or not isinstance(scope["user"], str)
            or scope["user"] != memory.canonical_owner(scope["user"])):
        raise ValueError("invalid archive owner scope")
    notes.evidence.exact(scope["user"])


def _path_scope(rel, scope):
    _validate_scope(scope)
    path = notes.checked_relative(rel)
    parts = Path(rel).parts
    allowed = {*memory.CATEGORIES, "users", "owners", "conversations", "notes"}
    if parts[0] not in allowed or len(parts) < 2:
        raise ValueError("archive target is not a file-memory namespace")
    if scope == {"all": True}:
        return path
    if set(scope) != {"user"} or not isinstance(scope["user"], str):
        raise ValueError("invalid archive owner scope")
    exact = memory.canonical_owner(scope["user"])
    if scope["user"] != exact:
        raise ValueError("noncanonical archive owner")
    if parts[0] == "owners":
        if len(parts) < 4 or parts[1] != memory.owner_key(exact):
            raise ValueError("archive owner/path mismatch")
    elif parts[0] == "users":
        if len(parts) < 4 or parts[1] != memory.safe_id(exact):
            raise ValueError("legacy archive scope mismatch")
    elif parts[0] == "notes":
        if len(parts) < 3 or parts[1] != memory.safe_id(exact):
            raise ValueError("legacy action-note scope mismatch")
    elif exact != "shared" or parts[0] not in memory.CATEGORIES:
        raise ValueError("archive file escapes the requested owner scope")
    return path


def _validate_file(rel, raw, scope):
    parts = Path(rel).parts
    category, owner = None, None
    if len(parts) == 2 and parts[0] in memory.CATEGORIES:
        category, owner = parts[0], "shared"
    elif len(parts) == 4 and parts[0] == "owners" and parts[2] in (
        *memory.CATEGORIES, notes.ACTION_CATEGORY
    ):
        category = parts[2]
        if parts[3] == ".initialized-v2.json":
            value = notes._json(raw, 65536)
            owner = value.get("owner")
            if (not isinstance(owner, str)
                    or memory.owner_key(owner) != parts[1]
                    or value != {"version": 2, "owner": owner,
                                 "category": category, "legacy": "preserved-unclaimed"}):
                raise ValueError("invalid archived initialization marker")
            return
        try:
            meta, _ = memory.parse_note(raw.decode("utf-8").replace("\r\n", "\n"))
            owner = json.loads(meta["owner_json"]) if "owner_json" in meta else None
        except (UnicodeError, ValueError) as err:
            raise ValueError("invalid archived note metadata") from err
        if owner is None:
            if category != "job_reports":
                raise ValueError("archived private note lacks exact attribution")
            # Existing job reports were already addressed by full owner digest.
            # An all-user administrative restore preserves that address, without
            # inferring a principal from its readable prefix.
            owner = scope.get("user", "shared")
        elif not isinstance(owner, str) or memory.owner_key(owner) != parts[1]:
            raise ValueError("archived note owner does not match its directory")
    if category is not None:
        if parts[-1].endswith(".md"):
            notes.validate_note(raw, owner, category)
        elif parts[-1] == ".initialized-v2.json":
            value = notes._json(raw, 65536)
            if value != {"version": 2, "owner": owner, "category": category,
                         "legacy": "preserved-unclaimed"} or type(value["version"]) is not int:
                raise ValueError("invalid archived initialization marker")
        else:
            raise ValueError("unexpected file in archived note category; preserve it for inspection")


def export(out_path, *, user=None, all_users=False, encrypt=False):
    with notes.guard():
        exact = memory.canonical_owner(user)
        if not all_users:
            # Scoped export refuses unclaimed legacy rather than certifying
            # an incomplete private archive as an empty successful history.
            for category in (*memory.USER_SCOPED, notes.ACTION_CATEGORY):
                notes._check_scope(exact, category)
        scope = {"all": True} if all_users else {"user": exact}
        files, total = [], 0
        for path in collect(roots(exact, all_users)):
            rel = notes.relative(path)
            raw = notes.read_raw(path)
            if raw is None:
                raise notes.NoteStateError("archive source disappeared")
            _path_scope(rel, scope)
            _validate_file(rel, raw, scope)
            total += len(raw)
            if total > notes.MAX_BATCH:
                raise ValueError("archive aggregate bound exceeded")
            files.append((rel, raw))
        manifest = {
            "schema_version": memory.ARCHIVE_SCHEMA_VERSION,
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "scope": scope,
            "coverage": "file-memory roots" if all_users else "exact-owner file notes",
            "complete_principal_erasure_or_backup": False,
            "files": [{"path": rel, "sha256": notes.digest(raw), "bytes": len(raw)}
                      for rel, raw in files],
        }
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for rel, raw in [*(("data/" + rel, raw) for rel, raw in files),
                             ("manifest.json", json.dumps(manifest, sort_keys=True).encode())]:
                item = tarfile.TarInfo(rel)
                item.size, item.mtime, item.mode = len(raw), 0, 0o600
                archive.addfile(item, io.BytesIO(raw))
        raw = buffer.getvalue()
        if encrypt:
            from . import vault
            raw = vault._fernet().encrypt(raw)
        if len(raw) > MAX_ARCHIVE:
            raise ValueError("encoded archive bound exceeded")
        output = notes.logical(out_path)
        state = notes.logical(config.MEMORY_DIR)
        if output.is_relative_to(state) and not (
            output.parent == state and output.name.endswith((".tar.gz", ".tgz", ".enc"))
        ):
            raise ValueError("export destination overlaps managed memory state")
        external_publish(output, raw)
        return manifest


def load_archive(path):
    raw = memory._maybe_decrypt(external_read(path, MAX_ARCHIVE * 2))
    if len(raw) > MAX_ARCHIVE:
        raise ValueError("decrypted archive bound exceeded")
    payloads, total, folded = {}, 0, set()
    try:
        # Bound decompression before tarfile parses PAX/long-name headers;
        # otherwise an enormous metadata header can bypass member-size checks.
        with gzip.GzipFile(fileobj=io.BytesIO(raw), mode="rb") as compressed:
            expanded = compressed.read(MAX_ARCHIVE + 1)
        if len(expanded) > MAX_ARCHIVE:
            raise ValueError("expanded archive bound exceeded")
        with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as archive:
            for item in archive:
                name = item.name
                if (len(payloads) >= notes.MAX_FILES + 1 or not item.isfile()
                        or name.casefold() in folded
                        or item.size < 0 or item.size > 4 * notes.MAX_NOTE):
                    raise ValueError("invalid, duplicate or oversized archive member")
                total += item.size
                if total > MAX_ARCHIVE:
                    raise ValueError("expanded archive bound exceeded")
                stream = archive.extractfile(item)
                if stream is None:
                    raise ValueError("missing archive payload")
                data = stream.read(item.size + 1)
                if len(data) != item.size:
                    raise ValueError("truncated archive member")
                payloads[name] = data
                folded.add(name.casefold())
    except (tarfile.TarError, EOFError, OSError, OverflowError) as err:
        raise ValueError("invalid memory archive") from err
    if "manifest.json" not in payloads:
        raise ValueError("not an Olympus memory export: no manifest.json inside")
    manifest = notes._json(payloads.pop("manifest.json"), 4 * notes.MAX_NOTE)
    if not isinstance(manifest, dict):
        raise ValueError("invalid archive manifest")
    version = manifest.get("schema_version")
    if type(version) is not int or version not in memory.SUPPORTED_ARCHIVE_VERSIONS:
        raise ValueError(f"refusing to import unknown schema_version {version!r}")
    memory._gate_import(manifest, str(path))
    scope = manifest.get("scope")
    _validate_scope(scope)
    rows = manifest.get("files")
    if not isinstance(rows, list) or len(rows) > notes.MAX_FILES:
        raise ValueError("invalid archive inventory")
    changes, seen = {}, set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "bytes", "sha256"}:
            raise ValueError("invalid archive file declaration")
        rel = row["path"]
        _path_scope(rel, scope)
        if rel.casefold() in seen:
            raise ValueError("duplicate or case-colliding manifest path")
        seen.add(rel.casefold())
        data = payloads.get("data/" + rel)
        if (data is None or type(row["bytes"]) is not int
                or row["bytes"] != len(data) or len(data) > notes.MAX_NOTE
                or not isinstance(row["sha256"], str)
                or not notes._HASH.fullmatch(row["sha256"])
                or notes.digest(data) != row["sha256"]):
            raise ValueError("archive checksum, size or payload mismatch")
        _validate_file(rel, data, scope)
        changes[rel] = data
    if set(payloads) != {"data/" + name for name in changes}:
        raise ValueError("archive payload and manifest inventories differ")
    return manifest, changes


def restore(path, *, overwrite=True, user=None, all_users=None):
    manifest, changes = load_archive(path)
    scope = manifest["scope"]
    if user is not None and scope != {"user": memory.canonical_owner(user)}:
        raise ValueError("archive does not match the explicitly selected owner")
    if all_users is not None and (scope == {"all": True}) != all_users:
        raise ValueError("archive scope requires an explicit matching --user or --all")
    with notes.guard():
        expected, skipped = {}, []
        for rel in list(changes):
            before = notes.read_raw(notes.checked_relative(rel))
            if before is not None and not overwrite:
                skipped.append(rel)
                del changes[rel]
            else:
                expected[rel] = notes.digest(before)
        result = notes.transact(changes, expected)
        for rel, raw in changes.items():
            if notes.read_raw(notes.checked_relative(rel)) != raw:
                raise notes.NoteStateError("restored bytes could not be verified")
        return {"schema_version": manifest["schema_version"],
                "restored": sorted(changes), "count": len(changes),
                "verified": len(changes), "skipped_existing": sorted(skipped),
                "legacy_unclaimed": sorted(name for name in changes
                                           if name.startswith(("users/", "notes/"))),
                "transaction": result.get("transaction"),
                "complete_principal_restore": False}


def delete_preview(user=None, *, category=None, note_id=None):
    exact = memory.canonical_owner(user)
    if category is not None and category not in (*memory.CATEGORIES, notes.ACTION_CATEGORY):
        raise ValueError("unknown memory category: " + str(category))
    if (category is not None and exact != "shared" and category not in
            memory.USER_SCOPED | memory.PRIVATE_CATEGORIES | {notes.ACTION_CATEGORY}):
        raise ValueError("shared system-note deletion requires the explicit shared scope")
    if note_id is not None and (not isinstance(note_id, str) or len(note_id) > 256):
        raise ValueError("invalid note identifier")
    with notes.guard():
        categories = [category] if category else [
            cat for cat in (*memory.CATEGORIES, notes.ACTION_CATEGORY)
            if exact == "shared" or cat in
            memory.USER_SCOPED | memory.PRIVATE_CATEGORIES | {notes.ACTION_CATEGORY}
        ]
        rows = []
        for cat in categories:
            for row in notes.notes(exact, cat):
                path = row["path"]
                if note_id is None or note_id in (path.name, path.stem):
                    rows.append({"path": notes.relative(path), "sha256": row["sha256"]})
        return {"owner": exact, "category": category, "note_id": note_id, "files": rows}


def delete(user=None, *, category=None, note_id=None, preview=None):
    with notes.guard():
        actual = delete_preview(user, category=category, note_id=note_id)
        if preview is not None and preview != actual:
            raise notes.NoteStateError("delete preview is stale; inspect a fresh preview")
        return notes.delete_rows([
            {"path": notes.checked_relative(item["path"]), "sha256": item["sha256"]}
            for item in actual["files"]
        ])


def migrate():
    """Upgrade supported attributable notes with preserved, resumable before bytes."""
    with notes.guard():
        changes, expected, scanned, unclaimed = {}, {}, 0, []
        for path in collect(roots(all_users=True)):
            if path.suffix != ".md":
                continue
            rel = notes.relative(path)
            parts = Path(rel).parts
            if parts[0] in ("users", "notes"):
                unclaimed.append(rel)
                continue
            category, owner = None, None
            if len(parts) == 2 and parts[0] in memory.CATEGORIES:
                category, owner = parts[0], "shared"
            elif len(parts) == 4 and parts[0] == "owners" and parts[2] in memory.CATEGORIES:
                category = parts[2]
            if category is None:
                continue
            raw = notes.read_raw(path)
            text = raw.decode("utf-8").replace("\r\n", "\n")
            meta, body = memory.parse_note(text)
            if owner is None:
                if "owner_json" not in meta:
                    unclaimed.append(rel)
                    continue
                owner = json.loads(meta["owner_json"])
            notes.validate_note(raw, owner, category)
            scanned += 1
            if meta.get("schema_version") == "2":
                continue
            created = meta.get("created") or memory._created_from_name(path.name)
            # Keep the entire existing body, including its title, verbatim.
            header = (
                "---\nschema_version: 2\ncreated: " + created
                + "\nowner_json: " + json.dumps(owner, ensure_ascii=True)
                + "\ncategory: " + category + "\nbody_sha256: "
                + notes.digest(body.encode("utf-8")) + "\noperation: \n---\n"
            )
            changes[rel] = (header + body).encode("utf-8")
            expected[rel] = notes.digest(raw)
        result = notes.transact(changes, expected)
        return {"scanned": scanned, "migrated": len(changes),
                "legacy_unclaimed": unclaimed, "transaction": result.get("transaction")}

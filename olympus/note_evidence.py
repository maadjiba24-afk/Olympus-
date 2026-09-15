"""Bounded file-note evidence and durable, explicitly recoverable mutations.

The state administrator remains trusted. Windows retains the documented
single-process topology; POSIX uses the established process lock. Every note
reader participates in the same state-root lock and refuses an active journal.
Recovery validates ALL current target bytes before changing any target.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import time
import uuid
import warnings

from . import config, memory, owner_evidence as evidence, proclock

MAX_NOTE = 1024 * 1024
MAX_FILES = 10000
MAX_BATCH = 64 * 1024 * 1024
MAX_PLAN = 192 * 1024 * 1024
ACTION_CATEGORY = "action_notes"
_ID = re.compile(r"(?:[0-9a-f]{32}|[0-9a-f]{64})\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class NoteStateError(evidence.OwnerEvidenceStateError):
    def __init__(self, reason):
        super().__init__("file notes", reason)


def digest(raw):
    return None if raw is None else hashlib.sha256(raw).hexdigest()


def logical(path):
    value = str(path)
    if os.name == "nt" and value.startswith("\\\\?\\"):
        value = ("\\\\" + value[8:]) if value.startswith("\\\\?\\UNC\\") else value[4:]
    return Path(value).absolute()


def relative(path):
    return logical(path).relative_to(logical(config.MEMORY_DIR)).as_posix()


def checked_relative(value):
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("invalid note path")
    path = PurePosixPath(value)
    parts = value.split("/")
    if path.is_absolute() or "\\" in value or any(
        part in ("", ".", "..") or part[-1:] in (".", " ")
        or any(ord(c) < 32 for c in part) or ":" in part
        or part.split(".")[0].upper() in
        {"CON", "PRN", "AUX", "NUL", *("COM" + str(i) for i in range(1, 10)),
         *("LPT" + str(i) for i in range(1, 10))}
        for part in parts
    ):
        raise ValueError("unsafe note path")
    return config.MEMORY_DIR.joinpath(*parts)


def io(path):
    return evidence._io(logical(path))


def _check_dir(path):
    evidence._parents(logical(path) / "entry", "file notes")
    try:
        info = io(path).lstat()
    except FileNotFoundError:
        return False
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400):
        raise NoteStateError("non-directory or linked note directory")
    return True


def sync_dir(path):
    if not hasattr(os, "O_DIRECTORY"):
        return  # Native Windows directory fsync is not a supported guarantee.
    fd = os.open(io(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def mkdir(path):
    path = logical(path)
    root = logical(config.MEMORY_DIR)
    if not path.is_relative_to(root):
        raise NoteStateError("directory outside configured note state")
    if not io(root).exists():
        io(root).mkdir(parents=True, mode=0o700)
        sync_dir(root.parent)
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        if not _check_dir(current):
            io(current).mkdir(mode=0o700)
            sync_dir(current.parent)


def read_raw(path, cap=MAX_NOTE):
    return evidence.read_bytes(logical(path), cap, "file notes")


def publish(path, raw):
    """Unique staging, strict POSIX directory barriers, no ignored failures."""
    path = logical(path)
    evidence._parents(path, "file notes")
    mkdir(path.parent)
    read_raw(path, MAX_PLAN)  # Refuse nonregular/reparse destinations.
    fd, name = tempfile.mkstemp(prefix=".note-", suffix=".tmp", dir=io(path.parent))
    staging = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, io(path))
        sync_dir(path.parent)
    finally:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass  # Inaccessible private staging is preserved; never masks failure.


def remove(path):
    raw = read_raw(path, MAX_PLAN)
    if raw is not None:
        io(path).unlink()
    sync_dir(logical(path).parent)


def _journal_root():
    return config.MEMORY_DIR / "note-transactions-v2"


def _active():
    return _journal_root() / "active.json"


@contextmanager
def guard(*, recovery=False):
    key = hashlib.sha256(str(logical(config.MEMORY_DIR)).encode()).hexdigest()
    try:
        _check_dir(config.MEMORY_DIR / "locks")
        with proclock.lock("note-v2-" + key):
            if not recovery and read_raw(_active(), 4096) is not None:
                raise NoteStateError(
                    "an interrupted mutation requires memory notes-recover; "
                    "inspect memory notes-status before retrying")
            yield
    except OSError as err:
        raise NoteStateError("I/O or durability unconfirmed; inspect notes-status") from err


def _json(raw, cap=MAX_PLAN):
    return evidence.decode(raw, "file notes", cap)


def _pack(raw):
    return None if raw is None else base64.b64encode(raw).decode("ascii")


def _unpack(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid journal bytes")
    return base64.b64decode(value, validate=True)


def _load_plan(pointer):
    evidence.fields(pointer, ("version", "id", "sha256"))
    if type(pointer["version"]) is not int or pointer["version"] != 2:
        raise ValueError("invalid journal version")
    if not isinstance(pointer["id"], str) or not _ID.fullmatch(pointer["id"]):
        raise ValueError("invalid transaction identity")
    path = _journal_root() / pointer["id"] / "plan.json"
    raw = read_raw(path, MAX_PLAN)
    if raw is None or digest(raw) != pointer["sha256"]:
        raise NoteStateError("recovery plan missing or changed; preserve all evidence")
    plan = _json(raw)
    evidence.fields(plan, ("version", "id", "changes"))
    if type(plan["version"]) is not int or plan["version"] != 2 or plan["id"] != pointer["id"]:
        raise ValueError("journal identity mismatch")
    evidence.records(plan["changes"], MAX_FILES)
    seen, total = set(), 0
    for item in plan["changes"]:
        evidence.fields(item, ("path", "before", "after"))
        path = checked_relative(item["path"])
        if path.is_relative_to(_journal_root()):
            raise ValueError("journal cannot modify its own evidence")
        folded = item["path"].casefold()
        if folded in seen:
            raise ValueError("duplicate journal target")
        seen.add(folded)
        for key in ("before", "after"):
            data = _unpack(item[key])
            if data is not None:
                total += len(data)
                if len(data) > MAX_NOTE:
                    raise ValueError("journal file bound exceeded")
    if total > 2 * MAX_BATCH:
        raise ValueError("journal aggregate bound exceeded")
    return plan


def _finish(pointer, decision):
    plan = _load_plan(pointer)
    terminal = _journal_root() / pointer["id"] / "result.json"
    terminal_raw = read_raw(terminal, 4096)
    if terminal_raw is not None:
        result = _json(terminal_raw, 4096)
        if result != {"version": 2, "id": pointer["id"],
                      "sha256": pointer["sha256"], "decision": decision}:
            raise NoteStateError("a terminal recovery decision already exists")
    # Check every target before the first mutation, including already changed
    # targets. A stale/corrupt target is never overwritten to force recovery.
    for item in plan["changes"]:
        current = read_raw(checked_relative(item["path"]))
        allowed = (_unpack(item["before"]), _unpack(item["after"]))
        if current not in allowed:
            raise NoteStateError("recovery target changed: " + item["path"])
    selected = "after" if decision == "resume" else "before"
    for item in plan["changes"]:
        path, target = checked_relative(item["path"]), _unpack(item[selected])
        if target is None:
            if io(path.parent).exists():
                remove(path)
        else:
            # Re-publish even matching bytes to confirm the durability barrier.
            publish(path, target)
        if read_raw(path) != target:
            raise NoteStateError("recovery publication could not be verified")
    result = {"version": 2, "id": pointer["id"],
              "sha256": pointer["sha256"], "decision": decision}
    publish(terminal, json.dumps(result, sort_keys=True).encode())
    remove(_active())
    return {"transaction": pointer["id"], "decision": decision,
            "changed": [item["path"] for item in plan["changes"]],
            "state": "available", "verified": True}


def transact(changes, expected, *, operation=None):
    """Durably preserve before/after bytes before a multi-file mutation."""
    with guard():
        if len(changes) > MAX_FILES or set(changes) != set(expected):
            raise ValueError("invalid transaction inventory")
        items, total, seen = [], 0, set()
        for relative_name, after in sorted(changes.items()):
            path = checked_relative(relative_name)
            if path.is_relative_to(_journal_root()):
                raise ValueError("invalid journal target")
            folded = relative_name.casefold()
            if folded in seen:
                raise ValueError("case-colliding transaction target")
            seen.add(folded)
            before = read_raw(path)
            if digest(before) != expected[relative_name]:
                raise NoteStateError("stale mutation target: " + relative_name)
            if after is not None and (not isinstance(after, bytes) or len(after) > MAX_NOTE):
                raise ValueError("invalid note transaction bytes")
            total += len(before or b"") + len(after or b"")
            if before != after:
                items.append({"path": relative_name, "before": _pack(before),
                              "after": _pack(after)})
        if total > 2 * MAX_BATCH:
            raise ValueError("note transaction bound exceeded")
        if not items:
            return {"state": "available", "verified": True, "changed": []}
        identity = operation or uuid.uuid4().hex
        if not _ID.fullmatch(identity):
            raise ValueError("invalid transaction id")
        directory = _journal_root() / identity
        if io(directory).exists():
            raise NoteStateError("transaction identity already exists; inspect notes-status")
        plan = {"version": 2, "id": identity, "changes": items}
        raw = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
        if len(raw) > MAX_PLAN:
            raise ValueError("recovery plan bound exceeded")
        publish(directory / "plan.json", raw)
        pointer = {"version": 2, "id": identity, "sha256": digest(raw)}
        try:
            publish(_active(), json.dumps(pointer, sort_keys=True).encode())
            return _finish(pointer, "resume")
        except (OSError, evidence.OwnerEvidenceStateError) as err:
            raise NoteStateError(
                "mutation unconfirmed; preserved transaction " + identity
                + "; inspect memory notes-status and notes-recover") from err


def recovery_status():
    with guard(recovery=True):
        raw = read_raw(_active(), 4096)
        if raw is None:
            return {"state": "available", "active_transaction": None}
        try:
            pointer = _json(raw, 4096)
            plan = _load_plan(pointer)
            return {"state": "unavailable", "active_transaction": pointer["id"],
                    "targets": [item["path"] for item in plan["changes"]],
                    "recovery": "memory notes-recover TRANSACTION --decision resume|rollback"}
        except (ValueError, evidence.OwnerEvidenceStateError) as err:
            raise NoteStateError("recovery evidence damaged; preserve journal") from err


def recover(identity, decision):
    if not isinstance(identity, str) or not _ID.fullmatch(identity):
        raise ValueError("invalid recovery transaction")
    if decision not in ("resume", "rollback"):
        raise ValueError("recovery decision must be resume or rollback")
    with guard(recovery=True):
        raw = read_raw(_active(), 4096)
        if raw is None:
            path = _journal_root() / identity / "result.json"
            result = _json(read_raw(path, 4096), 4096)
            if result.get("id") != identity or result.get("decision") != decision:
                raise NoteStateError("no matching completed recovery receipt")
            pointer = {key: result[key] for key in ("version", "id", "sha256")}
            plan = _load_plan(pointer)
            selected = "after" if decision == "resume" else "before"
            for item in plan["changes"]:
                if read_raw(checked_relative(item["path"])) != _unpack(item[selected]):
                    raise NoteStateError("completed recovery target has since changed")
            return {"state": "available", "transaction": identity,
                    "decision": decision, "verified": True, "already_completed": True}
        pointer = _json(raw, 4096)
        if pointer.get("id") != identity:
            raise NoteStateError("different active transaction; preserve both")
        return _finish(pointer, decision)


def category_owner(owner, category):
    if category not in (*memory.CATEGORIES, ACTION_CATEGORY):
        raise ValueError("unknown memory category: " + str(category))
    exact = evidence.exact(owner)
    return exact if category in memory.USER_SCOPED | memory.PRIVATE_CATEGORIES | {ACTION_CATEGORY} else "shared"


def directory(owner, category):
    exact = category_owner(owner, category)
    if category in memory.PRIVATE_CATEGORIES or category == ACTION_CATEGORY or (
        category in memory.USER_SCOPED and exact != "shared"
    ):
        return config.MEMORY_DIR / "owners" / memory.owner_key(exact) / category
    return config.MEMORY_DIR / category


def inventory(root, *, recursive=False):
    """Bounded and non-link-following, including hidden attribution markers."""
    root = logical(root)
    if not _check_dir(root):
        return []
    found, pending = [], [root]
    visited = 0
    while pending:
        directory_path = pending.pop()
        _check_dir(directory_path)
        with os.scandir(io(directory_path)) as entries:
            for entry in entries:
                visited += 1
                if visited > MAX_FILES:
                    raise NoteStateError("file inventory bound exceeded")
                path = directory_path / entry.name
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise NoteStateError("linked note inventory entry")
                if stat.S_ISDIR(info.st_mode):
                    if recursive:
                        pending.append(path)
                    else:
                        raise NoteStateError("unexpected nested note directory")
                elif stat.S_ISREG(info.st_mode):
                    found.append(path)
                else:
                    raise NoteStateError("nonregular note inventory entry")
    return sorted(found)


def legacy(owner, category):
    exact = category_owner(owner, category)
    if category in memory.USER_SCOPED and exact != "shared":
        root = config.MEMORY_DIR / "users" / memory.safe_id(exact) / category
    elif category == ACTION_CATEGORY:
        root = config.MEMORY_DIR / "notes" / memory.safe_id(exact)
    else:
        return []
    return [{"path": relative(path), "sha256": digest(read_raw(path))}
            for path in inventory(root, recursive=True)]


def _check_scope(owner, category):
    exact = category_owner(owner, category)
    root = directory(exact, category)
    marker = read_raw(root / ".initialized-v2.json", 65536)
    if marker is not None:
        value = _json(marker, 65536)
        if value != {"version": 2, "owner": exact, "category": category,
                     "legacy": "preserved-unclaimed"}:
            raise NoteStateError("invalid exact-owner initialization marker")
    elif legacy(exact, category):
        raise NoteStateError(
            "unclaimed legacy " + category
            + "; explicit notes-initialize is required to start separate empty notes")
    return root


def initialize(owner, category, *, acknowledge_legacy=False):
    if category not in memory.USER_SCOPED | memory.PRIVATE_CATEGORIES | {ACTION_CATEGORY}:
        raise ValueError("notes-initialize is for private or user-scoped categories")
    exact = category_owner(owner, category)
    with guard():
        root = directory(exact, category)
        existing = inventory(root)
        marker = root / ".initialized-v2.json"
        if read_raw(marker, 65536) is not None:
            _check_scope(exact, category)
            rows = notes(exact, category)
            return {"state": "available", "owner": exact, "category": category,
                    "legacy_unclaimed": legacy(exact, category), "already_initialized": True,
                    "initialized_empty": not rows}
        if existing:
            raise NoteStateError("exact-owner note directory is not empty; preserve it")
        prior = legacy(exact, category)
        if prior and acknowledge_legacy is not True:
            raise ValueError("explicit acknowledgement of unclaimed legacy notes is required")
        raw = json.dumps({"version": 2, "owner": exact, "category": category,
                          "legacy": "preserved-unclaimed"}, sort_keys=True).encode()
        path = root / ".initialized-v2.json"
        transact({relative(path): raw}, {relative(path): None})
        return {"state": "available", "owner": exact, "category": category,
                "legacy_unclaimed": prior, "initialized_empty": True}


def status(owner, category=None):
    exact = evidence.exact(owner)
    recovery = recovery_status()
    if recovery["state"] != "available":
        return recovery
    categories = [category] if category else [
        *sorted(memory.USER_SCOPED | memory.PRIVATE_CATEGORIES), ACTION_CATEGORY]
    result = {"state": "available", "owner": exact, "categories": []}
    with guard():
        for cat in categories:
            row = {"category": cat}
            try:
                row.update(state="available", notes=len(notes(exact, cat)),
                           legacy_unclaimed=legacy(exact, cat))
            except evidence.OwnerEvidenceStateError as err:
                row.update(state="unavailable", reason=err.reason)
                result["state"] = "unavailable"
            result["categories"].append(row)
        mirror_rows = []
        for path in inventory(config.MEMORY_DIR / "note-mirror-v2" / memory.owner_key(exact)):
            value = _json(read_raw(path, 65536), 65536)
            if (not isinstance(value, dict) or value.get("version") != 2
                    or value.get("owner") != exact
                    or value.get("state") not in ("available", "unavailable")):
                raise NoteStateError("invalid optional-mirror receipt")
            if value["state"] == "unavailable" or value.get("receipt_state"):
                mirror_rows.append(value)
        result["optional_mirror_unavailable"] = mirror_rows
    return result


def validate_note(raw, owner, category):
    if raw is None:
        raise NoteStateError("note disappeared during read")
    if len(raw) > MAX_NOTE:
        raise NoteStateError("note size bound exceeded")
    try:
        text = raw.decode("utf-8").replace("\r\n", "\n")
        meta, body = memory.parse_note(text)
        exact = category_owner(owner, category)
        if text.startswith("---") and not meta:
            raise ValueError("malformed frontmatter")
        if text.startswith("---\n"):
            header = text[4:text.find("\n---\n", 4)].splitlines()
            keys = [line.partition(":")[0].strip() for line in header]
            if len(keys) != len(set(keys)) or any(":" not in line for line in header):
                raise ValueError("duplicate or malformed note metadata")
        version = int(meta.get("schema_version", 0))
        if version not in (0, 1, 2):
            raise ValueError("unknown note version")
        if version == 2:
            if set(meta) != {"schema_version", "created", "owner_json", "category",
                             "body_sha256", "operation"}:
                raise ValueError("invalid note fields")
            if json.loads(meta["owner_json"]) != exact or meta["category"] != category:
                raise ValueError("note owner/category mismatch")
            if digest(body.encode("utf-8")) != meta["body_sha256"]:
                raise ValueError("note body checksum mismatch")
            if meta["operation"] and not _ID.fullmatch(meta["operation"]):
                raise ValueError("invalid operation identity")
        elif exact != "shared" and category != "job_reports":
            raise ValueError("private note lacks exact-owner attribution")
        if "created" in meta:
            time.strptime(meta["created"], "%Y%m%d-%H%M%S")
        if not body.strip():
            raise ValueError("empty note")
        return meta, body
    except (ValueError, TypeError, UnicodeError, OverflowError) as err:
        raise NoteStateError("invalid note metadata, ownership or content") from err


def render(owner, category, title, content, *, created=None, operation=""):
    from . import security
    evidence.text(title, 512)
    evidence.text(content, 500000, empty=True)
    exact = category_owner(owner, category)
    title = security.sanitize_for_memory(title)
    content = security.sanitize_for_memory(content)
    body = f"# {title}\n\n{content.strip()}\n"
    created = created or time.strftime("%Y%m%d-%H%M%S")
    text = (
        "---\nschema_version: 2\ncreated: " + created
        + "\nowner_json: " + json.dumps(exact, ensure_ascii=True)
        + "\ncategory: " + category + "\nbody_sha256: " + digest(body.encode())
        + "\noperation: " + operation + "\n---\n" + body
    )
    raw = text.encode("utf-8")
    validate_note(raw, exact, category)
    return raw


def notes(owner, category):
    with guard():
        root = _check_scope(owner, category)
        rows = []
        for path in inventory(root):
            if path.name == ".initialized-v2.json":
                continue  # Already validated by _check_scope.
            if re.fullmatch(r"\.note-[A-Za-z0-9_-]+\.tmp", path.name):
                read_raw(path)  # Bounded, preserved private publication staging.
                continue
            if path.name.startswith("."):
                raise NoteStateError("unrecognized hidden note evidence")
            if path.suffix != ".md":
                raise NoteStateError("unexpected non-note file in note category")
            raw = read_raw(path)
            meta, body = validate_note(raw, owner, category)
            rows.append({"path": path, "sha256": digest(raw), "raw": raw,
                         "meta": meta, "body": body, "category": category,
                         "owner": category_owner(owner, category)})
        return rows


def create(owner, category, title, content, *, action_id=None, commit=None):
    exact = category_owner(owner, category)
    with guard():
        root = _check_scope(exact, category)
        operation = (hashlib.sha256(("note-action\0" + exact + "\0" + action_id).encode()).hexdigest()
                     if action_id is not None else uuid.uuid4().hex)
        if action_id is not None:
            evidence.text(action_id, 128)
            filename = operation + ".md"
        else:
            filename = time.strftime("%Y%m%d-%H%M%S") + "-" + operation + ".md"
        path = root / filename
        raw = render(exact, category, title, content, operation=operation)
        prior = read_raw(path)
        if prior is not None:
            meta, body = validate_note(prior, exact, category)
            _, desired = validate_note(raw, exact, category)
            if action_id is None or meta.get("operation") != operation or body != desired:
                raise NoteStateError("existing note conflicts with action identity")
            raw = prior
        elif len(notes(exact, category)) >= MAX_FILES:
            raise NoteStateError("note count bound exceeded")
        changes, expected = {relative(path): raw}, {relative(path): digest(prior)}
        if commit is not None:
            extra, original = commit(result_for_bytes(path, raw, exact, category))
            if set(extra) & set(changes) or set(extra) != set(original):
                raise NoteStateError("invalid action transaction targets")
            changes.update(extra)
            expected.update(original)
        transact(changes, expected)
        if prior == raw:
            publish(path, raw)  # Confirm durability on an idempotent retry.
        return io(path)


def delete_rows(rows):
    with guard():
        changes = {relative(row["path"]): None for row in rows}
        expected = {relative(row["path"]): row["sha256"] for row in rows}
        result = transact(changes, expected)
        return sorted(result["changed"])


def note_result(path, owner, category):
    with guard():
        return result_for_bytes(path, read_raw(path), owner, category)


def result_for_bytes(path, raw, owner, category):
    meta, _ = validate_note(raw, owner, category)
    return {"version": 2, "owner": category_owner(owner, category),
            "category": category, "note": logical(path).name,
            "sha256": digest(raw), "operation": meta.get("operation", ""),
            "path": str(io(path))}


def undo_note(result, owner, *, action_id, commit=None):
    exact = evidence.exact(owner)
    with guard():
        try:
            evidence.fields(result, ("version", "owner", "category", "note",
                                     "sha256", "operation", "path"))
            if (result["version"] != 2 or result["owner"] != exact
                    or result["category"] != ACTION_CATEGORY
                    or not _HASH.fullmatch(result["sha256"])
                    or not _HASH.fullmatch(result["operation"])
                    or result["note"] != result["operation"] + ".md"):
                raise ValueError("invalid undo authority")
            path = directory(exact, ACTION_CATEGORY) / result["note"]
            if logical(result["path"]) != logical(path):
                raise ValueError("undo path mismatch")
        except (ValueError, TypeError, KeyError) as err:
            raise NoteStateError("untrusted note undo result") from err
        evidence.text(action_id, 128)
        expected_operation = hashlib.sha256(("note-action\0" + exact + "\0" + action_id).encode()).hexdigest()
        if result["operation"] != expected_operation:
            raise NoteStateError("undo result belongs to another action")
        raw = read_raw(path)
        tombstone = config.MEMORY_DIR / "note-undo-v2" / memory.owner_key(exact) / (result["operation"] + ".json")
        authority = {key: result[key] for key in ("owner", "note", "sha256", "operation")}
        if raw is None:
            prior = read_raw(tombstone, 65536)
            if prior is None or _json(prior, 65536) != authority:
                raise NoteStateError("note is absent without a matching undo receipt")
            sync_dir(path.parent)
            if commit is not None:
                changes, expected = commit(result)
                transact(changes, expected)
            return "note deleted"
        validate_note(raw, exact, ACTION_CATEGORY)
        if digest(raw) != result["sha256"]:
            raise NoteStateError("note changed since execution; undo refused")
        if read_raw(tombstone, 65536) is not None:
            raise NoteStateError("a completed undo receipt conflicts with a restored note")
        changes = {relative(path): None, relative(tombstone): json.dumps(authority, sort_keys=True).encode()}
        expected = {relative(path): result["sha256"], relative(tombstone): None}
        if commit is not None:
            extra, original = commit(result)
            if set(extra) & set(changes) or set(extra) != set(original):
                raise NoteStateError("invalid undo transaction targets")
            changes.update(extra)
            expected.update(original)
        transact(changes, expected)
        return "note deleted"


def mirror_warning(message):
    # Warning filters configured as "error" must not convert a successful
    # canonical save into a failed action which a caller could repeat.
    try:
        warnings.warn(message, RuntimeWarning, stacklevel=3)
    except RuntimeWarning:
        pass

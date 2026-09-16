"""Exact-owner wiki snapshot, published through the existing recovery journal."""
from copy import deepcopy
import json

from . import config, memory, note_evidence as notes, owner_evidence as oe


def empty():
    return {"pages": [], "last_dream": 0.0, "memory_ids": [], "seen": {}}


def validate(data):
    from . import wiki
    oe.fields(data, ("pages", "last_dream", "memory_ids", "seen"))
    if not isinstance(data["seen"], dict) or len(data["seen"]) > 32000:
        raise ValueError("invalid dream source inventory")
    for key, value in data["seen"].items():
        oe.text(key, 2048)
        from .sleeptime_evidence import digest
        digest(value)
    oe.number(data["last_dream"])
    oe.records(data["memory_ids"], 1500)
    if len(set(data["memory_ids"])) != len(data["memory_ids"]):
        raise ValueError("duplicate dream memory")
    for ident in data["memory_ids"]:
        oe.text(ident, 128)
    oe.records(data["pages"], wiki.MAX_PAGES)
    seen = set()
    for page in data["pages"]:
        oe.fields(page, ("slug", "title", "body", "created", "updated",
                         "review_after_days", "durable", "sources"))
        oe.text(page["title"], 512)
        oe.text(page["body"], wiki.MAX_PAGE_CHARS)
        oe.text(page["sources"], 4096, empty=True)
        if page["slug"] != wiki.slugify(page["title"]) or page["slug"] in seen:
            raise ValueError("wiki page collision or identity mismatch")
        seen.add(page["slug"])
        oe.number(page["created"])
        oe.number(page["updated"], minimum=page["created"])
        oe.integer(page["review_after_days"], minimum=1, maximum=36500)
        if type(page["durable"]) is not bool:
            raise ValueError("invalid durability claim")


def path(user):
    return oe.workspace(user) / "wiki" / "state.json"


def document(user):
    return oe.JsonStore(oe.exact(user), "wiki", validate, empty=empty,
                        path=path(user), max_bytes=notes.MAX_NOTE)


def legacy(user):
    owner = oe.exact(user)
    root = (config.MEMORY_DIR / "wiki" if owner == "shared" else
            config.MEMORY_DIR / "users" / memory.safe_id(owner) / "wiki")
    result = {}
    for p in notes.inventory(root):
        raw = notes.read_raw(p)
        if raw is None:
            raise oe.OwnerEvidenceStateError("wiki", "legacy inventory changed")
        result[notes.relative(p)] = {"sha256": notes.digest(raw), "bytes": len(raw)}
    return result


def read(user):
    with notes.guard():
        doc = document(user)
        raw = doc._raw()
        if raw is None and legacy(user):
            raise oe.OwnerEvidenceStateError("wiki", "unclaimed legacy pages; inspect wiki state-status")
        return doc._value(raw), raw


def encode(user, data):
    validate(data)
    raw = json.dumps({"version": 2, "owner": oe.exact(user), "data": data},
                     sort_keys=True, allow_nan=False).encode()
    if len(raw) > notes.MAX_NOTE:
        raise ValueError("wiki snapshot byte capacity exceeded; original preserved")
    document(user)._value(raw)
    return raw


def publish(user, data, before, *, extra=None):
    with notes.guard():
        read(user)
        target = notes.relative(path(user))
        changes = {target: encode(user, data)}
        expected = {target: notes.digest(before)}
        for name, (old, new) in (extra or {}).items():
            if name in changes:
                raise ValueError("duplicate wiki transaction target")
            changes[name] = new
            expected[name] = notes.digest(old)
        return notes.transact(changes, expected)


def mutate(user, fn):
    with notes.guard():
        data, before = read(user)
        result = fn(data)
        publish(user, data, before)
        return deepcopy(result)


def status(user):
    result = {"store": "wiki", "owner": oe.exact(user)}
    try:
        result["legacy_unclaimed"] = legacy(user)
        data, raw = read(user)
        result.update(state="missing" if raw is None else "valid", pages=len(data["pages"]))
    except oe.OwnerEvidenceStateError as err:
        result.update(state="unavailable", pages=None, reason=err.reason)
    return result


def initialize(user, *, acknowledge_legacy=False):
    if acknowledge_legacy is not True:
        raise ValueError("explicit preservation acknowledgement required")
    with notes.guard():
        if document(user)._raw() is not None:
            raise ValueError("exact wiki already exists; initialization refused")
        before = legacy(user)
        target = notes.relative(path(user))
        notes.transact({target: encode(user, empty())}, {target: None})
        if legacy(user) != before:
            raise oe.OwnerEvidenceStateError("wiki", "legacy inventory changed")
    return status(user)


def owners():
    """Validated principals only; directory spellings are never identities."""
    from . import usermem
    result = set(usermem.owners()) | {"shared"}
    # Keep the typed-memory -> note lock ordering used by dream publication.
    with notes.guard():
        try:
            return _note_owners(result)
        except (ValueError, TypeError, KeyError, AttributeError) as err:
            raise oe.OwnerEvidenceStateError("wiki", "owner inventory unavailable") from err


def _note_owners(result):
    root = config.MEMORY_DIR / "owners"
    if not notes._check_dir(root):
        return sorted(result)
    count = 0
    for directory in notes.io(root).iterdir():
        count += 1
        if count > 1000 or not notes._check_dir(directory):
            raise oe.OwnerEvidenceStateError("wiki", "owner inventory unavailable or too large")
        state_path = notes.logical(directory) / "workspace-v2" / "wiki" / "state.json"
        raw = notes.read_raw(state_path)
        if raw is not None:
            value = oe.decode(raw, "wiki", notes.MAX_NOTE)
            owner = value.get("owner") if isinstance(value, dict) else None
            oe.text(owner, 8192)
            if memory.owner_key(owner) != directory.name:
                raise oe.OwnerEvidenceStateError("wiki", "owner path mismatch")
            document(owner)._value(raw)
            result.add(owner)
        for category in ("lessons", "corrections", "feedback"):
            for entry in notes.inventory(notes.logical(directory) / category):
                if entry.name == ".initialized-v2.json":
                    marker = oe.decode(notes.read_raw(entry), "wiki owner marker", 4096)
                    owner = marker.get("owner") if isinstance(marker, dict) else None
                    oe.text(owner, 8192)
                    if memory.owner_key(owner) != directory.name:
                        raise oe.OwnerEvidenceStateError("wiki", "note initialization owner mismatch")
                    notes._check_scope(owner, category)
                    result.add(owner)
                    continue
                if entry.name.startswith(".note-") and entry.name.endswith(".tmp"):
                    notes.read_raw(entry)  # Private staging is bounded and preserved.
                    continue
                if entry.name.startswith(".") or entry.suffix != ".md":
                    raise oe.OwnerEvidenceStateError("wiki", "unexpected note inventory entry")
                note_raw = notes.read_raw(entry)
                try:
                    meta, _ = memory.parse_note(note_raw.decode("utf-8"))
                    owner = json.loads(meta["owner_json"])
                    if memory.owner_key(owner) != directory.name:
                        raise ValueError("note owner path mismatch")
                    notes.validate_note(note_raw, owner, category)
                except (ValueError, KeyError, TypeError, UnicodeError) as err:
                    raise oe.OwnerEvidenceStateError("wiki", "note principal unavailable") from err
                notes._check_scope(owner, category)
                result.add(owner)
    return sorted(result)

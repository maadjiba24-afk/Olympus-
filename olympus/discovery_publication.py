"""Recoverable owner/gap-bound publication, including its acknowledgement.

Research results are durably prepared once. Retrying a prepared operation does
not call a provider; the note journal publishes its destination, gap status and
receipt together. Recovery uses the existing notes-recover operator route.
"""
from copy import deepcopy
import hashlib
import json

from . import config, memory, note_evidence as notes, owner_evidence as oe, proclock


def identity(user, gap_id):
    oe.text(gap_id, 128)
    return hashlib.sha256((oe.exact(user) + "\0" + gap_id).encode()).hexdigest()


def path(user, gap_id):
    return oe.workspace(user) / "discovery" / "operations" / (identity(user, gap_id) + ".json")


def guard(user, gap_id):
    key = hashlib.sha256((str(config.MEMORY_DIR.absolute()) + "\0" + identity(user, gap_id)).encode()).hexdigest()
    return proclock.lock("discovery-" + key)


def gap_identity(gap):
    return {k: gap[k] for k in ("id", "kind", "topic", "created", "source", "evidence")}


def read(user, gap_id):
    raw = notes.read_raw(path(user, gap_id))
    if raw is None:
        return None, None
    data = oe.decode(raw, "discovery publication", notes.MAX_NOTE)
    try:
        oe.fields(data, ("version", "owner", "id", "gap", "state", "body", "title",
                         "destination", "before_sha256", "after_sha256"))
        if (type(data["version"]) is not int or data["version"] != 1
                or data["owner"] != oe.exact(user) or data["id"] != identity(user, gap_id)
                or data["state"] not in ("prepared", "published") or data["gap"]["id"] != gap_id):
            raise ValueError("publication identity differs")
        oe.fields(data["gap"], ("id", "kind", "topic", "created", "source", "evidence"))
        from . import discovery, wiki
        discovery._validate([{**data["gap"], "status": "open", "hits": 1,
                              "last_seen": data["gap"]["created"], "resolved_ref": ""}])
        oe.text(data["body"], 64000)
        oe.text(data["title"], 512)
        oe.text(data["destination"], 4096)
        expected_title = (data["gap"]["topic"].capitalize() if data["gap"]["kind"] == "knowledge"
                          else "Discovery: " + data["gap"]["topic"])
        expected_destination = (wiki.slugify(expected_title) if data["gap"]["kind"] == "knowledge"
            else notes.relative(notes.directory("shared", "upgrades") / (identity(user, gap_id) + ".md")))
        if data["title"] != expected_title or data["destination"] != expected_destination:
            raise ValueError("publication destination or title differs from the bound gap")
        from .sleeptime_evidence import digest
        for key in ("before_sha256", "after_sha256"):
            if data[key] is not None:
                digest(data[key])
        if data["state"] == "published" and data["after_sha256"] is None:
            raise ValueError("published receipt lacks content evidence")
    except (ValueError, KeyError, TypeError) as err:
        raise oe.OwnerEvidenceStateError("discovery publication", "invalid operation") from err
    return data, raw


def _encode(data):
    return json.dumps(data, sort_keys=True, allow_nan=False).encode()


def _page_hash(page):
    return notes.digest(_encode(page)) if page is not None else None


def _gap(user, requested, kind):
    from . import discovery
    gaps = discovery._load_gaps(user)
    row = next((g for g in gaps if g["id"] == requested.get("id")), None)
    if row is None or row["kind"] != kind or gap_identity(row) != gap_identity(requested):
        raise ValueError("gap identity or ownership differs from persisted evidence")
    return gaps, row


def publish(user, requested, kind, research=None):
    from . import discovery, wiki, wiki_evidence as we
    if discovery._replaying():
        return "(skipped: replay)"
    user = oe.exact(user)
    with guard(user, requested.get("id")):
        with notes.guard():
            gaps, gap = _gap(user, requested, kind)
            operation, before_operation = read(user, gap["id"])
            if operation is not None and operation["gap"] != gap_identity(gap):
                raise ValueError("gap changed after preparation; preserve the operation")
            if operation is not None and operation["state"] == "published":
                expected_status = "acquired" if kind == "knowledge" else "proposed"
                if gap["status"] != expected_status or gap["resolved_ref"] != operation["destination"]:
                    raise oe.OwnerEvidenceStateError("discovery", "publication acknowledgement differs")
                if kind == "knowledge":
                    rows = we.read(user)[0]["pages"]
                    actual = _page_hash(next((p for p in rows if p["slug"] == operation["destination"]), None))
                else:
                    actual = notes.digest(notes.read_raw(notes.checked_relative(operation["destination"])))
                if actual != operation["after_sha256"]:
                    return "(publication completed previously; destination has since changed; preserved without replay)"
                return _message(operation)
            if gap["status"] != "open":
                raise ValueError("resolved gap has no matching publication receipt; inspect existing evidence")
            original = deepcopy(gap)
            if operation is None:
                title = gap["topic"].capitalize() if kind == "knowledge" else "Discovery: " + gap["topic"]
                if kind == "knowledge":
                    wiki_data, _ = we.read(user)
                    destination = wiki.slugify(title)
                    page = next((p for p in wiki_data["pages"] if p["slug"] == destination), None)
                    before_hash = _page_hash(page)
                else:
                    # Upgrade proposals remain explicitly installation-shared.
                    destination = notes.relative(notes.directory("shared", "upgrades") /
                                                 (identity(user, gap["id"]) + ".md"))
                    notes.notes("shared", "upgrades")
                    before_hash = notes.digest(notes.read_raw(notes.checked_relative(destination)))
                    if before_hash is not None:
                        raise ValueError("unclaimed upgrade destination exists; preserve it")
        if operation is None:
            if kind == "knowledge":
                with memory.user_context(user):
                    body = research("Give a clear, current, well-sourced explanation in at most "
                                    + str(wiki.MAX_PAGE_CHARS) + " characters of: " + original["topic"])
                if not isinstance(body, str) or not discovery._is_substantive(body):
                    return "(queued: no substantive research result)"
            else:
                body = ("Operator proposal only; nothing has been built or enabled.\n\n"
                        f"Gap: {original['topic']}\nEvidence: {original['evidence']}\n"
                        f"Source: {original['source']}\nSeen: {original['hits']} time(s).")
            oe.text(body, 64000)
            operation = dict(version=1, owner=user, id=identity(user, original["id"]),
                gap=gap_identity(original), state="prepared", body=body, title=title,
                destination=destination, before_sha256=before_hash, after_sha256=None)
            with notes.guard():
                # Preserve completed research even if a concurrent edit makes
                # final publication stale. No provider call is repeated on retry.
                target = notes.relative(path(user, original["id"]))
                notes.transact({target: _encode(operation)}, {target: None})
                operation, before_operation = read(user, original["id"])
        with notes.guard():
            gaps, gap = _gap(user, original, kind)
            if gap["status"] != "open":
                raise ValueError("gap changed before acknowledgement")
            before_gaps = discovery._evidence(user)._raw()
            operation = deepcopy(operation)
            changes, expected = {}, {}
            if kind == "knowledge":
                data, before_wiki = we.read(user)
                page = next((p for p in data["pages"] if p["slug"] == operation["destination"]), None)
                if _page_hash(page) != operation["before_sha256"]:
                    raise ValueError("wiki destination changed since research; preserved result needs review")
                wiki._put(data, operation["title"], operation["body"], sources="discovery: research")
                page = next(p for p in data["pages"] if p["slug"] == operation["destination"])
                operation["after_sha256"] = _page_hash(page)
                target = notes.relative(we.path(user))
                changes[target], expected[target] = we.encode(user, data), notes.digest(before_wiki)
                gap["status"] = "acquired"
            else:
                target = operation["destination"]
                current = notes.read_raw(notes.checked_relative(target))
                if notes.digest(current) != operation["before_sha256"]:
                    raise ValueError("upgrade destination changed; preserve it")
                raw = notes.render("shared", "upgrades", operation["title"], operation["body"],
                                   operation=operation["id"])
                changes[target], expected[target] = raw, notes.digest(current)
                operation["after_sha256"] = notes.digest(raw)
                gap["status"] = "proposed"
            gap["resolved_ref"] = operation["destination"]
            operation["state"] = "published"
            target = notes.relative(discovery._gaps_path(user))
            changes[target], expected[target] = discovery._encode_gaps(user, gaps), notes.digest(before_gaps)
            target = notes.relative(path(user, gap["id"]))
            changes[target], expected[target] = _encode(operation), notes.digest(before_operation)
            notes.transact(changes, expected)
            return _message(operation)


def _message(operation):
    word = "learned" if operation["gap"]["kind"] == "knowledge" else "proposed feature"
    return f"{word} '{operation['gap']['topic']}' → {operation['destination']}"

"""Consolidation evidence in the same atomic snapshot as its typed memories.

The existing v3 envelope remains readable without this optional collection.
Normalized legacy collections are fingerprinted, never attributed or rewritten.
"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import time
import uuid

from . import config, memory, memory_evidence as me, owner_evidence as oe, store, usermem

COLLECTION = "sleeptime.v1"
LEGACY = ("sleeptime.proposals", "sleeptime.quarantine", "sleeptime.snapshots")
CAP = 500


def empty():
    return {"proposals": [], "quarantine": [], "snapshots": []}


def _proposal(row):
    oe.fields(row, ("id", "user", "type", "source_ids", "source_contents",
                   "source_records", "rewrite", "provenance", "sensitivity",
                   "verified", "unsupported", "applied", "created_at", "confidence"))
    oe.text(row["id"], 128)
    if oe.exact(row["user"]) != row["user"] or row["type"] not in usermem.TYPES:
        raise ValueError("proposal owner or type")
    me.strings(row["source_ids"], 5)
    me.strings(row["source_contents"], 5)
    if not row["source_ids"] or len(set(row["source_ids"])) != len(row["source_ids"]):
        raise ValueError("invalid proposal sources")
    me.rows(row["source_records"], 5)
    if (row["source_ids"] != [m["id"] for m in row["source_records"]]
            or row["source_contents"] != [m["content"] for m in row["source_records"]]):
        raise ValueError("source binding differs")
    usermem._validate_state({usermem._EVENTS: [], usermem._CANDS: [],
                            usermem._MEMS: row["source_records"]})
    oe.text(row["rewrite"], usermem._MAX_CONTENT)
    me.strings(row["provenance"])
    me.strings(row["unsupported"], 100)
    if row["sensitivity"] not in ("normal", "high"):
        raise ValueError("invalid sensitivity")
    if type(row["verified"]) is not bool or type(row["applied"]) is not bool:
        raise ValueError("invalid proposal decision")
    if row["verified"] and row["unsupported"]:
        raise ValueError("contradictory verification")
    if any(s["type"] != row["type"] for s in row["source_records"]):
        raise ValueError("source type differs")
    if row["applied"] and not row["verified"]:
        raise ValueError("unverified applied proposal")
    me.timestamp(row["created_at"])
    me.probability(row["confidence"])


def validate(data):
    oe.fields(data, ("proposals", "quarantine", "snapshots"))
    for row in me.rows(data["proposals"], CAP):
        _proposal(row)
    for row in me.rows(data["quarantine"], CAP):
        oe.fields(row, ("id", "proposal", "reason", "evidence_hash"))
        _proposal(row["proposal"])
        if row["id"] != row["proposal"]["id"]:
            raise ValueError("quarantine identity")
        oe.text(row["reason"], 4096)
        if row["evidence_hash"] is not None:
            digest(row["evidence_hash"])
    for row in me.rows(data["snapshots"], CAP):
        oe.fields(row, ("id", "ts", "proposal_id", "rewrite_id", "sources",
                       "after_sources", "rewrite", "reverted"))
        oe.text(row["proposal_id"], 128)
        oe.text(row["rewrite_id"], 128)
        me.timestamp(row["ts"])
        if type(row["reverted"]) is not bool:
            raise ValueError("invalid revert decision")
        for key in ("sources", "after_sources"):
            me.rows(row[key], 5)
        usermem._validate_state({usermem._EVENTS: [], usermem._CANDS: [],
            usermem._MEMS: row["sources"] + [row["rewrite"]]})
        usermem._validate_state({usermem._EVENTS: [], usermem._CANDS: [],
            usermem._MEMS: row["after_sources"]})
        if (row["rewrite"]["id"] != row["rewrite_id"]
                or [s["id"] for s in row["sources"]] !=
                   [s["id"] for s in row["after_sources"]]):
            raise ValueError("snapshot binding differs")
        proposal = next((p for p in data["proposals"] if p["id"] == row["proposal_id"]), None)
        if (proposal is None or row["id"] != row["proposal_id"] or not proposal["applied"]
                or row["sources"] != proposal["source_records"]
                or row["rewrite"]["content"] != proposal["rewrite"]):
            raise ValueError("snapshot/proposal binding differs")
        for before, after in zip(row["sources"], row["after_sources"]):
            if after != {**before, "status": usermem.SUPERSEDED, "superseded_by": row["rewrite_id"]}:
                raise ValueError("invalid supersession evidence")
    for proposal in data["proposals"]:
        if proposal["applied"] != any(s["proposal_id"] == proposal["id"] for s in data["snapshots"]):
            raise ValueError("applied proposal lacks atomic rewrite evidence")


def digest(value):
    import re
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("invalid evidence digest")


def legacy(user):
    key = memory.safe_id(oe.exact(user))
    backend = store.backend()
    result = {}
    for namespace in LEGACY:
        try:
            raw = (oe.read_bytes(config.MEMORY_DIR / "store" / namespace / key,
                                 8 * 1024 * 1024, "sleeptime")
                   if type(backend) is store.FileStore else backend.get(namespace, key))
            if raw is not None:
                if not isinstance(raw, bytes) or len(raw) > 8 * 1024 * 1024:
                    raise ValueError("invalid legacy bytes")
                result[namespace] = {"sha256": hashlib.sha256(raw).hexdigest(),
                                     "bytes": len(raw)}
        except oe.OwnerEvidenceStateError:
            raise
        except Exception as err:
            raise oe.OwnerEvidenceStateError("sleeptime", "legacy inventory unavailable") from err
    return result


@contextmanager
def transaction(user, *, initialize=False):
    user = oe.exact(user)
    with usermem._guard(user) as state:
        if COLLECTION not in state:
            if legacy(user) and not initialize:
                raise oe.OwnerEvidenceStateError("sleeptime", "unclaimed legacy state; inspect state-status")
            current = empty()
        else:
            current = deepcopy(state[COLLECTION])
        for row in current["proposals"]:
            if row["user"] != user:
                raise oe.OwnerEvidenceStateError("sleeptime", "proposal owner mismatch")
        for row in current["quarantine"]:
            if row["proposal"]["user"] != user:
                raise oe.OwnerEvidenceStateError("sleeptime", "quarantine owner mismatch")
        before = deepcopy(current)
        yield current
        if initialize or before != current:
            validate(current)
            state[COLLECTION] = current


def status(user):
    result = {"owner": oe.exact(user), "store": "sleeptime"}
    try:
        result["legacy_unclaimed"] = legacy(user)
        with usermem._guard(user) as state, transaction(user) as data:
            result.update(state="valid" if COLLECTION in state else "missing",
                          counts={key: len(v) for key, v in data.items()})
    except oe.OwnerEvidenceStateError as err:
        result.update(state="unavailable", counts=None, reason=err.reason)
    return result


def initialize(user, *, acknowledge_legacy=False):
    if acknowledge_legacy is not True:
        raise ValueError("explicit legacy-preservation acknowledgement required")
    with usermem._guard(user) as state:
        if COLLECTION in state:
            raise ValueError("sleeptime state already initialized")
        before = legacy(user)
        with transaction(user, initialize=True):
            pass
        if before != legacy(user):
            raise oe.OwnerEvidenceStateError("sleeptime", "legacy inventory changed")
    return status(user)


def load(user, key):
    with transaction(user) as data:
        return deepcopy(data[key])


def add(user, proposal):
    _proposal(proposal)
    if proposal["user"] != oe.exact(user):
        raise ValueError("proposal belongs to another owner")
    with transaction(user) as data:
        return deepcopy(_add(data, proposal))
    return proposal


def _add(data, proposal):
    existing = next((p for p in data["proposals"] if p["id"] == proposal["id"]), None)
    if existing is not None:
        if {**existing, "applied": False} != {**proposal, "applied": False}:
            raise ValueError("proposal retry differs")
        return existing
    if len(data["proposals"]) >= CAP:
        raise oe.OwnerEvidenceStateError("sleeptime", "proposal capacity reached; history preserved")
    data["proposals"].append(deepcopy(proposal))
    return data["proposals"][-1]


def commit(user, proposal, check_contract):
    _proposal(proposal)
    if proposal["user"] != oe.exact(user):
        raise ValueError("proposal belongs to another owner")
    with transaction(user) as data:
        existing = _add(data, proposal)
        prior = next((s for s in data["snapshots"] if s["proposal_id"] == proposal["id"]), None)
        if prior is not None:
            if prior["reverted"]:
                raise ValueError("proposal already reverted; create a new reviewed proposal")
            _confirm_durable(user)
            return prior["rewrite_id"]
        sources = [usermem.get_memory(user, key) for key in proposal["source_ids"]]
        if sources != proposal["source_records"] or any(m["status"] != usermem.ACTIVE for m in sources):
            raise ValueError("proposal sources changed; no rewrite published")
        reason = check_contract(sources)
        if reason:
            if len(data["quarantine"]) >= CAP:
                raise ValueError("quarantine capacity reached; history preserved")
            if not any(p["id"] == proposal["id"] for p in data["quarantine"]):
                data["quarantine"].append({"id": proposal["id"],
                    "proposal": deepcopy(proposal), "reason": reason, "evidence_hash": None})
            return None
        if len(data["snapshots"]) >= CAP:
            raise ValueError("snapshot capacity reached; history preserved")
        now = time.time()
        new = {"id": uuid.uuid4().hex[:12], "type": proposal["type"], "key": None,
               "content": proposal["rewrite"],
               "confidence": max(m["confidence"] for m in sources),
               "importance": max(m["importance"] for m in sources),
               "sensitivity": proposal["sensitivity"], "provenance": proposal["provenance"],
               "half_life_days": usermem.HALF_LIFE[proposal["type"]],
               "status": usermem.ACTIVE, "superseded_by": None,
               "created_at": now, "last_used_at": now, "use_count": 0}
        mems = usermem.all_memories(user)
        if len(mems) >= usermem._MAX_MEMORIES * 3:
            raise ValueError("memory capacity reached; rewrite history preserved")
        for row in mems:
            if row["id"] in proposal["source_ids"]:
                row.update(status=usermem.SUPERSEDED, superseded_by=new["id"])
        mems.append(new)
        usermem._save(usermem._MEMS, user, mems)
        data["snapshots"].append({"id": proposal["id"], "ts": now,
            "proposal_id": proposal["id"], "rewrite_id": new["id"],
            "sources": deepcopy(sources), "after_sources": [deepcopy(next(m for m in mems
                if m["id"] == key)) for key in proposal["source_ids"]],
            "rewrite": deepcopy(new), "reverted": False})
        existing["applied"] = True
        return new["id"]


def revert(user, identifier):
    with transaction(user) as data:
        snap = next((s for s in data["snapshots"] if s["id"] == identifier), None)
        if snap is None:
            return False
        if snap["reverted"]:
            _confirm_durable(user)
            return True
        expected = snap["after_sources"] + [snap["rewrite"]]
        if any(usermem.get_memory(user, row["id"]) != row for row in expected):
            raise ValueError("rewrite or sources changed; stale revert refused")
        originals = {row["id"]: row for row in snap["sources"]}
        mems = usermem.all_memories(user)
        for index, row in enumerate(mems):
            if row["id"] in originals:
                mems[index] = deepcopy(originals[row["id"]])
            elif row["id"] == snap["rewrite_id"]:
                row["status"] = usermem.TOMBSTONED
        usermem._save(usermem._MEMS, user, mems)
        snap["reverted"] = True
        return True


def _confirm_durable(user):
    from . import note_evidence
    document = usermem._state(user).document
    path = document._file()
    if path is not None:
        raw = document._raw()
        document._value(raw)
        note_evidence.publish(path, raw)


def quarantine_target(user):
    return "quarantine:" + memory.storage_key(oe.exact(user))


def flush_quarantine(user):
    """Retry the durable outbox, without regenerating or re-verifying a rewrite.

    The owner transaction contains the pending record before any signing work.
    A lost local/PG acknowledgement can only repeat an idempotent signed append.
    """
    from . import deltas
    with transaction(user) as data:
        for row in data["quarantine"]:
            if row["evidence_hash"] is not None:
                continue
            snap = deltas.record_snapshot(quarantine_target(user), kind="quarantine",
                state={"proposal": row["proposal"], "reason": row["reason"]},
                provenance=deltas.Provenance(source="sleeptime", trust="user"),
                operation_id=row["id"], require_signature=True)
            row["evidence_hash"] = snap["snapshot_hash"]
    return {"pending": 0}

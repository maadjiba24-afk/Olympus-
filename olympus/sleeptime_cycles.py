"""The signed cycle record is the only authority for graduation counters.

Legacy counters and the old scoreboard remain unclaimed. A stable cycle ID
replays its receipt, never its model work; publication/anchor failure stays
unavailable until the exact persisted operation is retried.
"""
from copy import deepcopy
from functools import wraps
import hashlib
import json
import time
import uuid

from . import config, deltas, memory, note_evidence as notes, owner_evidence as oe, store, witness

TARGET = "sleeptime:scoreboard:v2"


def serialized(fn):
    """Serialize preparation as well as publication across both cycle callers.

    A second cycle must not generate work against counters whose first cycle
    may still need recording recovery. POSIX uses the existing process lock;
    Windows retains the documented single-process state-directory contract.
    """
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            with deltas._guard(TARGET):
                return fn(*args, **kwargs)
        except TimeoutError as err:
            raise oe.OwnerEvidenceStateError("sleeptime cycles", "another cycle is still in progress") from err
    return wrapped


def _draft_root():
    return config.MEMORY_DIR / "sleeptime-cycles-v2"


def _draft_path(cycle_id):
    oe.text(cycle_id, 128)
    return _draft_root() / hashlib.sha256(cycle_id.encode()).hexdigest()


def _pending():
    raw = notes.read_raw(_draft_root() / "active.json", 4096)
    if raw is None:
        return None
    value = oe.decode(raw, "cycle preparation", 4096)
    oe.fields(value, ("id",))
    oe.text(value["id"], 128)
    return value["id"]


def zero():
    return dict(clean_cycles=0, runs=0, last_run=0.0, committed=0, proposed=0)


def fingerprint(proposal):
    row = {key: value for key, value in proposal.items() if key != "applied"}
    return hashlib.sha256(json.dumps(row, sort_keys=True, allow_nan=False,
                                   separators=(",", ":")).encode()).hexdigest()


def legacy():
    backend = store.backend()
    path = config.MEMORY_DIR / "store" / "sleeptime" / "state"
    try:
        raw = (oe.read_bytes(path, 1024 * 1024, "sleeptime cycles")
               if type(backend) is store.FileStore else backend.get("sleeptime", "state"))
        old = oe.read_bytes(deltas._path("sleeptime:scoreboard"), 16 * 1024 * 1024,
                            "sleeptime cycles")
        return {key: {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
                for key, value in (("counter", raw), ("scoreboard", old)) if value is not None}
    except Exception as err:
        raise oe.OwnerEvidenceStateError("sleeptime cycles", "legacy inventory unavailable") from err


def _validate(row):
    data = row["state"]
    oe.fields(data, ("version", "cycle_id", "grade", "reasons", "proposal_refs",
                     "counters", "report", "legacy_unclaimed", "request_sha256"))
    if data["version"] != 2 or type(data["version"]) is not int:
        raise ValueError("cycle schema differs")
    oe.text(data["cycle_id"], 128)
    if data["grade"] not in ("CLEAN", "DIRTY", "INITIALIZED"):
        raise ValueError("invalid cycle grade")
    oe.records(data["reasons"], 1000)
    for reason in data["reasons"]:
        oe.text(reason, 4096)
    oe.records(data["proposal_refs"], 20000)
    seen = set()
    for ref in data["proposal_refs"]:
        oe.fields(ref, ("owner", "id", "sha256"))
        oe.exact(ref["owner"])
        oe.text(ref["id"], 128)
        from .sleeptime_evidence import digest
        digest(ref["sha256"])
        identity = ref["owner"], ref["id"]
        if identity in seen:
            raise ValueError("duplicate proposal evidence")
        seen.add(identity)
    if data["grade"] == "CLEAN" and (not seen or data["reasons"]):
        raise ValueError("empty or contradicted clean qualification")
    st = data["counters"]
    oe.fields(st, tuple(zero()))
    for key in ("clean_cycles", "runs", "committed", "proposed"):
        oe.integer(st[key])
    oe.number(st["last_run"])
    if st["clean_cycles"] > st["runs"]:
        raise ValueError("cycle counters differ")
    if not isinstance(data["report"], dict) or not isinstance(data["legacy_unclaimed"], dict):
        raise ValueError("invalid cycle report")
    from .sleeptime_evidence import digest
    digest(data["request_sha256"])
    if (row["delta"] != {"operation_id": data["cycle_id"], "value": None}
            or row["kind"] != "supervision"):
        raise ValueError("cycle identity differs")
    return data


def records(*, qualified=True):
    try:
        with notes.guard():
            pending = _pending()
            if qualified and pending is not None:
                raise ValueError("cycle recording unconfirmed; retry cycle " + pending)
        rows = (deltas.qualified_snapshots(TARGET) if qualified else deltas.snapshots(TARGET))
        if not rows:
            if legacy():
                raise ValueError("unclaimed legacy state; explicit initialize-empty required")
            return []
        for row in rows:
            if not deltas._snapshot_ok(row, TARGET):
                raise ValueError("unverifiable cycle")
            _validate(row)
        return rows
    except (ValueError, TypeError, KeyError) as err:
        raise oe.OwnerEvidenceStateError("sleeptime cycles", str(err)) from err


def state():
    rows = records()
    return deepcopy(rows[-1]["state"]["counters"]) if rows else zero()


def status():
    try:
        rows = records()
        return {"state": "valid" if rows else "missing", "counters": state(),
                "legacy_unclaimed": legacy(), "attested": not witness.is_default_seed()}
    except (oe.OwnerEvidenceStateError, witness.WitnessError) as err:
        return {"state": "unavailable", "counters": None, "reason": str(err)}


def initialize(*, acknowledge_legacy=False):
    if acknowledge_legacy is not True:
        raise ValueError("explicit legacy-preservation acknowledgement required")
    with deltas._guard(TARGET):
        if deltas.snapshots(TARGET):
            raise ValueError("cycle authority already exists")
        data = dict(version=2, cycle_id=uuid.uuid4().hex, grade="INITIALIZED",
                    reasons=[], proposal_refs=[], counters=zero(), report={},
                    legacy_unclaimed=legacy(), request_sha256=hashlib.sha256(b"initialize").hexdigest())
        _publish(data)
    return status()


def _publish(data):
    with notes.guard():
        return _publish_locked(data)


def _publish_locked(data):
    cycle_id = data["cycle_id"]
    path = _draft_path(cycle_id)
    raw = json.dumps(data, sort_keys=True, allow_nan=False).encode()
    if len(raw) > notes.MAX_NOTE:
        raise ValueError("cycle report exceeds evidence bound")
    existing = notes.read_raw(path / "draft.json")
    if _pending() not in (None, cycle_id):
        raise oe.OwnerEvidenceStateError("sleeptime cycles", "another cycle requires recording recovery")
    if existing is None:
        if len(notes.inventory(_draft_root(), recursive=True)) >= 2000:
            raise ValueError("cycle recovery history capacity reached; preserve records")
        targets = {notes.relative(path / "draft.json"): raw,
                   notes.relative(_draft_root() / "active.json"): json.dumps({"id": cycle_id}).encode()}
        notes.transact(targets, {key: None for key in targets})
    elif existing != raw:
        raise ValueError("cycle retry differs from the durable prepared report")
    snap = deltas.record_snapshot(TARGET, kind="supervision", state=data,
        operation_id=data["cycle_id"], require_signature=True,
        provenance=deltas.Provenance(source="supervise", trust="operator"))
    notes.publish(path / "result.json", json.dumps(snap, sort_keys=True, allow_nan=False).encode())
    notes.remove(_draft_root() / "active.json")
    return snap


def replay(cycle_id):
    """Confirm an interrupted append/anchor without running providers again."""
    oe.text(cycle_id, 128)
    with deltas._guard(TARGET):
        path = _draft_path(cycle_id)
        raw = notes.read_raw(path / "draft.json")
        if raw is not None:
            data = oe.decode(raw, "cycle draft", notes.MAX_NOTE)
            _validate({"state": data, "delta": {"operation_id": cycle_id, "value": None}, "kind": "supervision"})
            if data["cycle_id"] != cycle_id:
                raise ValueError("cycle draft identity differs")
            result = notes.read_raw(path / "result.json")
            if result is not None and _pending() != cycle_id:
                snap = oe.decode(result, "cycle receipt", notes.MAX_NOTE)
                if snap["state"] != data or not deltas._snapshot_ok(snap, TARGET):
                    raise ValueError("cycle receipt signature or identity differs")
                records()  # Current qualification must still be available.
                return snap
            return _publish(data)
        for row in records(qualified=False):
            if row["state"]["cycle_id"] == cycle_id:
                _publish(row["state"])
                records()  # Require the current anchor, when configured.
                return deepcopy(row)
    return None


def record(clean, proposed, committed, *, proposal_refs=(), cycle_id=None, report=None,
           reasons=()):
    from . import sleeptime_evidence as se
    cycle_id = cycle_id or uuid.uuid4().hex
    proposal_refs, reasons = list(proposal_refs), list(reasons)
    request = dict(clean=clean, proposed=proposed, committed=committed,
                   proposal_refs=proposal_refs, reasons=reasons, report=report)
    request_hash = hashlib.sha256(json.dumps(request, sort_keys=True,
                                  allow_nan=False).encode()).hexdigest()
    def same_request(prior):
        if prior["state"]["request_sha256"] != request_hash:
            raise ValueError("cycle ID reused with different evidence")
        return prior
    prior = replay(cycle_id)
    if prior is not None:
        return same_request(prior)
    oe.integer(proposed)
    oe.integer(committed)
    if type(clean) is not bool or committed > proposed:
        raise ValueError("invalid cycle summary")
    refs, problems = [], list(reasons)
    supported = applied = 0
    seen = set()
    for user, identifier in proposal_refs:
        user = oe.exact(user)
        if (user, identifier) in seen:
            raise ValueError("duplicate proposal reference")
        seen.add((user, identifier))
        row = next((r for r in se.load(user, "proposals") if r["id"] == identifier), None)
        if row is None:
            raise oe.OwnerEvidenceStateError("sleeptime cycles", "referenced proposal missing")
        refs.append({"owner": user, "id": identifier, "sha256": fingerprint(row)})
        supported += row["verified"]
        applied += row["applied"]
        if not row["verified"] or row["unsupported"] or row["confidence"] < config.SLEEPTIME_CONFIDENCE_MIN:
            problems.append("proposal is rejected or below the confidence floor: " + identifier)
    if not refs:
        problems.append("no persisted proposals; empty work does not qualify")
    if (supported, applied) != (proposed, committed):
        problems.append("summary does not match persisted proposals")
    if not clean:
        problems.append("cycle did not provide affirmative complete evidence")
    grade = "DIRTY" if problems else "CLEAN"
    with deltas._guard(TARGET):
        prior = replay(cycle_id)
        if prior is not None:
            return same_request(prior)
        st = state()
        st.update(runs=st["runs"] + 1, last_run=time.time(),
                  proposed=st["proposed"] + supported,
                  committed=st["committed"] + applied,
                  clean_cycles=st["clean_cycles"] + 1 if grade == "CLEAN" else 0)
        data = dict(version=2, cycle_id=cycle_id, grade=grade, reasons=problems,
                    proposal_refs=refs, counters=st, report=report or {}, legacy_unclaimed={},
                    request_sha256=request_hash)
        return _publish(data)

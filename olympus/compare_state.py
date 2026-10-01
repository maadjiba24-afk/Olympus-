"""Bounded exact-owner comparison snapshot and durable vote receipts.

One atomic snapshot contains answers, decisions and the calibration outbox.
Tallies are derived from validated decisions, never separately incremented.
The POSIX lock is machine-wide; Windows retains proclock's one-process scope.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import re
import string

from . import config, memory, note_evidence as files, owner_evidence as ev
from . import compare_execution as execution

NAME = "comparisons"
MAX_BYTES = 64 * 1024 * 1024
MAX_RECORDS = 50
MAX_RECEIPTS = 10000
MAX_TEXT = 32768
ID = re.compile(r"[0-9a-f]{32}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")
FAILED = "[This answer is unavailable; no substitute model was called.]"
INDETERMINATE = "[No durable answer is available. Recovery did not repeat this model call.]"


class CompareError(RuntimeError):
    def __init__(self, code, message, status=503, *, cid=None):
        self.code, self.status, self.cid = code, status, cid
        super().__init__(message)

    def payload(self):
        out = {"error": str(self), "code": self.code}
        if self.cid is not None:
            out.update(id=self.cid, recovery="recover")
        return out


def owner(value):
    # canonical_owner(None/"") means shared elsewhere; never infer it here.
    ev.text(value, 8192)
    value.encode("utf-8", "strict")
    return value


def text(value, *, empty=False):
    ev.text(value, MAX_TEXT, empty=empty)
    if len(value.encode("utf-8")) > MAX_TEXT:
        raise ValueError("text byte bound exceeded")


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError("comparison id must be 32 lowercase hexadecimal characters")
    return value


def _hash(value):
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise ValueError("invalid digest")


def path(uid):
    return config.MEMORY_DIR / "owners" / memory.owner_key(owner(uid)) / "compare-v2" / "state.json"


def legacy(uid):
    safe = memory.safe_id(owner(uid))
    base = config.MEMORY_DIR if safe == "shared" else config.MEMORY_DIR / "users" / safe
    candidate = base / "compares"
    ev._parents(candidate, NAME)
    try:
        ev._io(candidate).lstat()
        return True
    except FileNotFoundError:
        return False


@contextmanager
def guard(uid):
    try:
        owner(uid)
        files._check_dir(config.MEMORY_DIR / "locks")
        with ev.guard(uid, "comparisons-v2"):
            yield
    except CompareError:
        raise
    except (ev.OwnerEvidenceStateError, OSError, TimeoutError) as err:
        raise CompareError("unavailable", "Comparison evidence is unavailable; preserve it and retry recovery.") from err
    except (ValueError, TypeError, OverflowError, RecursionError) as err:
        raise CompareError("invalid", "Invalid comparison input or evidence; nothing was repaired.", 400) from err


def empty(uid):
    return {"version": 2, "owner": owner(uid), "records": {}, "archive": {}}


def request_hash(prompt, system_hash, effort):
    return execution.digest({"prompt": prompt, "system_hash": system_hash, "effort": effort})


def model(member):
    cfg = member["configured"]
    return cfg["provider"] + ("/" + cfg["model"] if cfg["model"] else "")


def _provenance(value, root_id):
    ev.fields(value, ("responses", "calls", "overflow"))
    if type(value["overflow"]) is not bool:
        raise ValueError("invalid overflow flag")
    ev.records(value["responses"], execution.MAX_RESPONSES)
    ev.records(value["calls"], execution.MAX_RESPONSES)
    calls = set()
    for call in value["calls"]:
        ev.fields(call, ("run_id", "parent_run_id", "provider", "requested_model", "endpoint_sha256", "state"))
        identifier(call["run_id"])
        if call["run_id"] in calls or (not calls and call["run_id"] != root_id):
            raise ValueError("invalid child run identity")
        if call["parent_run_id"] is not None and call["parent_run_id"] not in calls:
            raise ValueError("missing parent run")
        calls.add(call["run_id"])
        ev.text(call["provider"], 128)
        ev.text(call["requested_model"], 1024, empty=True)
        _hash(call["endpoint_sha256"])
        if call["state"] not in ("started", "failed", "returned"):
            raise ValueError("invalid dispatch state")
    for response in value["responses"]:
        ev.fields(response, ("run_id", "provider", "model", "response_id", "revision", "endpoint_sha256", "replay"))
        if response["run_id"] not in calls | {root_id}:
            raise ValueError("unknown response run")
        for key in ("provider", "model", "response_id", "revision"):
            if response[key] is not None:
                ev.text(response[key], 1024)
        if response["endpoint_sha256"] is not None:
            _hash(response["endpoint_sha256"])
        if type(response["replay"]) is not bool:
            raise ValueError("invalid replay flag")


def _record(cid, rec):
    ev.fields(rec, ("id", "request_hash", "prompt", "system_hash", "effort", "created",
                    "phase", "members", "choice", "calibration", "calibration_receipt", "decision_at"))
    if rec["id"] != cid:
        raise ValueError("comparison id mismatch")
    text(rec["prompt"])
    _hash(rec["system_hash"])
    ev.text(rec["effort"], 32)
    ev.number(rec["created"])
    if rec["request_hash"] != request_hash(rec["prompt"], rec["system_hash"], rec["effort"]):
        raise ValueError("request digest mismatch")
    if rec["phase"] not in ("running", "complete", "revealed"):
        raise ValueError("invalid phase")
    if not isinstance(rec["members"], list) or not 2 <= len(rec["members"]) <= 26:
        raise ValueError("invalid members")
    run_ids = set()
    for i, member in enumerate(rec["members"]):
        ev.fields(member, ("label", "run_id", "configured", "state", "text", "provenance", "identity"))
        if member["label"] != string.ascii_uppercase[i]:
            raise ValueError("invalid blind labels")
        identifier(member["run_id"])
        if member["run_id"] in run_ids:
            raise ValueError("duplicate run identity")
        run_ids.add(member["run_id"])
        cfg = member["configured"]
        ev.fields(cfg, ("provider", "model", "endpoint_sha256", "effort"))
        ev.text(cfg["provider"], 128)
        ev.text(cfg["model"], 1024, empty=True)
        _hash(cfg["endpoint_sha256"])
        if cfg["effort"] != rec["effort"]:
            raise ValueError("effort mismatch")
        _provenance(member["provenance"], member["run_id"])
        if member["identity"] != execution.identity(cfg, member["provenance"]):
            raise ValueError("execution identity mismatch")
        state = member["state"]
        if state not in ("not_started", "started", "answered", "failed", "indeterminate"):
            raise ValueError("invalid execution state")
        text(member["text"], empty=state in ("not_started", "started"))
        if state in ("not_started", "started") and member["text"] != "":
            raise ValueError("premature answer")
        if state == "failed" and member["text"] != FAILED:
            raise ValueError("invalid failure text")
        if state == "indeterminate" and member["text"] != INDETERMINATE:
            raise ValueError("invalid recovery text")
        if rec["phase"] != "running" and state in ("not_started", "started"):
            raise ValueError("unfinished comparison")
    choice = rec["choice"]
    if choice is not None:
        successful = [m["label"] for m in rec["members"] if m["state"] == "answered"]
        if rec["phase"] != "revealed" or len(successful) < 2 or choice not in successful:
            raise ValueError("ineligible vote")
    if rec["phase"] == "revealed":
        ev.text(rec["decision_at"], 64)
    elif rec["decision_at"] is not None or choice is not None:
        raise ValueError("premature decision")
    if rec["calibration"] not in ("disabled", "waiting", "pending", "recorded", "excluded"):
        raise ValueError("invalid calibration status")
    if rec["phase"] != "revealed" and rec["calibration"] not in ("disabled", "waiting"):
        raise ValueError("premature calibration")
    if rec["calibration"] == "recorded":
        _hash(rec["calibration_receipt"])
    elif rec["calibration_receipt"] is not None:
        raise ValueError("unexpected receipt")


def validate(uid, state):
    ev.fields(state, ("version", "owner", "records", "archive"))
    if type(state["version"]) is not int or state["version"] != 2 or state["owner"] != owner(uid):
        raise ValueError("owner/version mismatch")
    for key, cap in (("records", MAX_RECORDS), ("archive", MAX_RECEIPTS)):
        if not isinstance(state[key], dict) or len(state[key]) > cap:
            raise ValueError("collection bound")
        for cid in state[key]:
            identifier(cid)
    if set(state["records"]) & set(state["archive"]):
        raise ValueError("duplicate comparison")
    if len(state["records"]) + len(state["archive"]) > MAX_RECEIPTS:
        raise ValueError("receipt bound exceeded")
    for cid, rec in state["records"].items():
        _record(cid, rec)
    for rec in state["archive"].values():
        ev.fields(rec, ("request_hash", "revealed", "winner"))
        _hash(rec["request_hash"])
        if type(rec["revealed"]) is not bool:
            raise ValueError("invalid archived decision")
        if rec["winner"] is not None:
            if not rec["revealed"]:
                raise ValueError("premature archived vote")
            ev.fields(rec["winner"], ("identity", "model"))
            _hash(rec["winner"]["identity"])
            ev.text(rec["winner"]["model"], 1200)


def load(uid, *, allow_legacy=False):
    raw = ev.read_bytes(path(uid), MAX_BYTES, NAME)
    if raw is None:
        if not allow_legacy and legacy(uid):
            raise CompareError("unclaimed_legacy", "Legacy comparison evidence is unclaimed. Explicit empty initialization is required; legacy bytes will be preserved.", 409)
        return empty(uid)
    try:
        state = ev.decode(raw, NAME, MAX_BYTES)
        validate(uid, state)
        return state
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as err:
        raise CompareError("unavailable", "Comparison evidence failed validation. Preserve the bytes; no repair was performed.") from err


def save(uid, state, *, cid=None):
    try:
        validate(uid, state)
        raw = json.dumps(state, sort_keys=True, ensure_ascii=True, allow_nan=False,
                         separators=(",", ":")).encode()
        if len(raw) > MAX_BYTES:
            raise CompareError("capacity", "Comparison storage capacity reached; preserve and export the evidence.", 409)
        files.publish(path(uid), raw)
    except (ev.OwnerEvidenceStateError, OSError) as err:
        raise CompareError("publication_unconfirmed", "Comparison publication is unconfirmed. Recover this id before continuing; model calls will not be repeated.", cid=cid) from err


def winner(rec):
    if rec["choice"] is None:
        return None
    member = next(m for m in rec["members"] if m["label"] == rec["choice"])
    return {"identity": member["identity"], "model": model(member)}


def tallies(state):
    rows = {}
    votes = [winner(r) for r in state["records"].values()]
    votes.extend(r["winner"] for r in state["archive"].values())
    for vote in votes:
        if vote is not None:
            row = rows.setdefault(vote["identity"], {**vote, "count": 0})
            if row["model"] != vote["model"]:
                raise CompareError("unavailable", "Conflicting comparison identities; preserve the evidence.")
            row["count"] += 1
    return sorted(rows.values(), key=lambda r: (-r["count"], r["identity"]))


def reserve(state, limit=MAX_RECORDS, *, members=26):
    # Reserve the receipt before calling a model. No pending publication is
    # silently pruned, and the finite lifetime receipt budget is never reset.
    if len(state["records"]) + len(state["archive"]) >= MAX_RECEIPTS:
        raise CompareError("capacity", "Comparison receipt capacity reached; export and review retention.", 409)
    # Worst-case JSON escaping, bounded receipts and answer bytes. Reserve
    # before ANY provider call, so an oversized existing snapshot cannot cause
    # new paid work whose bounded result has no space in the snapshot.
    needed = 6 * (MAX_TEXT + members * (MAX_TEXT + execution.MAX_RESPONSES * 7000 + 6000))
    def full():
        return (len(state["records"]) >= limit or
                len(json.dumps(state, ensure_ascii=True).encode()) + needed > MAX_BYTES)
    while full():
        candidates = [r for r in state["records"].values()
                      if r["phase"] != "running" and r["calibration"] != "pending"]
        if not candidates:
            raise CompareError("recovery_required", "Recover pending comparisons before starting another.", 409)
        rec = min(candidates, key=lambda r: (r["created"], r["id"]))
        state["archive"][rec["id"]] = {"request_hash": rec["request_hash"],
            "revealed": rec["phase"] == "revealed", "winner": winner(rec)}
        del state["records"][rec["id"]]

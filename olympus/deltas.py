"""Delta substrate (Plane 3.1) — the shared, non-destructive learning substrate
under the reflection engine's two write targets (prompts/skills, and memory).

The reflection engine learns by DELTA, never by destructive rewrite. Two halves
of that substrate already exist:

  * `ace` — the delta model: non-destructive bullets with helpful/harmful
    counters, signature dedup, and pin-preserving pruning.
  * `sleeptime` — append-only snapshots + union provenance + revert, for memory.

This module unifies the second half into one tamper-evident primitive both
targets can stand on: an APPEND-ONLY, content-addressed, hash-chained, witness-
SIGNED snapshot log with FULL PROVENANCE. Where sleeptime's snapshots are plain
store records, these are signed and chained (as the execution ledger is), so a
learned-state history cannot be silently rewritten.

It also owns the one sanctioned way learned content may enter a prompt:
`enveloped()` — sanitize the text, then wrap it in the fail-closed untrusted-data
envelope. Learned content is DATA; it never enters a prompt as trusted text.

Integrity scope (by design): `verify_history` proves the retained window is
internally consistent and signed — reorder, edit, forgery, and cross-target
transplant are all rejected. Like any append-only log with no out-of-band head
anchor (and exactly as the execution ledger), it cannot by itself detect
WHOLESALE TRUNCATION of the newest records; detecting that would require pinning
the expected head hash in a separate trust store, which is out of scope here.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field

from . import config, security, witness, proclock, owner_evidence as oe, note_evidence as notes

SNAP_SCHEMA = "olympus-delta-snapshot/1"
SNAP_LABEL = "delta-snapshot/v1"        # witness subkey (domain separation)

_MAX_SNAPSHOTS = 200                     # rolling window per target (append-only)

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class DeltaError(ValueError):
    """A snapshot could not be minted, or a history is malformed/untrusted."""


# --- provenance -----------------------------------------------------------
#
# A trust label ranks a piece of learned content by how sensitive its ORIGIN is.
# When deltas from several origins merge, the RESULT inherits the strongest
# (most sensitive) label — you can never launder operator-trust content down to
# web-trust, nor silently promote web content up. Mirrors sleeptime's
# _rank_sensitivity / _merged_provenance, made reusable.

_TRUST_ORDER = ("web", "tool", "conversation", "user", "operator")
_TRUST_RANK = {label: i for i, label in enumerate(_TRUST_ORDER)}
_DEFAULT_TRUST = "conversation"


def rank_trust(label: str) -> int:
    """Sensitivity rank of a trust label; an unknown label is treated as the
    LOWEST trust (fail-safe — an unrecognized origin is not privileged)."""
    return _TRUST_RANK.get(str(label), -1)


def strongest_trust(labels) -> str:
    """The most-sensitive RECOGNIZED trust label in `labels`. Falls back to the
    default only when none is recognized — an unknown label is never returned
    (fail-safe) and, crucially, never spuriously promotes weaker content."""
    best, best_rank = None, -2
    for label in labels or ():
        r = rank_trust(label)
        if r > best_rank:
            best, best_rank = str(label), r
    return best if best is not None and best_rank >= 0 else _DEFAULT_TRUST


@dataclass(frozen=True)
class Provenance:
    """Where a learned delta came from — a first-class, preserved attribution."""
    source: str = "unknown"        # "ace" | "sleeptime" | "prometheus" | ...
    run_id: str = ""               # the trace/run that produced the signal
    trust: str = _DEFAULT_TRUST    # sensitivity label of the content's origin
    detail: str = ""

    def to_dict(self) -> dict:
        return {"source": self.source, "run_id": self.run_id,
                "trust": self.trust, "detail": self.detail}

    @classmethod
    def from_dict(cls, d) -> "Provenance":
        if not isinstance(d, dict):
            return cls()
        return cls(source=str(d.get("source", "unknown")),
                   run_id=str(d.get("run_id", "")),
                   trust=str(d.get("trust", _DEFAULT_TRUST)),
                   detail=str(d.get("detail", "")))


def merge_provenance(provs) -> Provenance:
    """Union several provenances into one: sources and run_ids are joined, and
    the RESULT carries the strongest (most sensitive) trust label — content
    never launders down to a weaker trust than any of its inputs."""
    provs = [p if isinstance(p, Provenance) else Provenance.from_dict(p)
             for p in (provs or [])]
    if not provs:
        return Provenance()
    sources = sorted({p.source for p in provs if p.source})
    runs = sorted({p.run_id for p in provs if p.run_id})
    trust = strongest_trust([p.trust for p in provs])
    return Provenance(source="+".join(sources) or "unknown",
                      run_id=",".join(runs), trust=trust,
                      detail="; ".join(p.detail for p in provs if p.detail))


# --- the one sanctioned learned-content → prompt path ---------------------

def enveloped(text: str, *, source: str) -> str:
    """The ONLY sanctioned way learned content enters a prompt: sanitize it (so a
    stored injection is defanged) and wrap it in the fail-closed untrusted-data
    envelope. Learned content is DATA — never trusted instructions."""
    clean = security.sanitize_for_memory(str(text))
    return security.wrap_untrusted(clean, source=source)


# --- append-only signed snapshot log --------------------------------------

def _content_hash(core: dict) -> str:
    # A null-PRESERVING canonical serialization: witness.canonical_json drops
    # None-valued keys, which would leave a null-valued field in `state`/`delta`
    # outside the digest (and thus outside the signature). json keeps nulls, so
    # every field — including explicit nulls — is covered.
    return hashlib.sha256(json.dumps(
        core, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        default=str).encode("utf-8")).hexdigest()


def _lock_for(target_id: str) -> threading.Lock:
    with _locks_guard:
        lk = _locks.get(target_id)
        if lk is None:
            lk = _locks[target_id] = threading.Lock()
        return lk


_ALLOWED = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:")


def _safe_target(target_id: str) -> str:
    tid = str(target_id)
    if not tid or tid in (".", "..") or any(c not in _ALLOWED for c in tid):
        raise DeltaError(f"invalid target_id {target_id!r}")
    return tid


def _filename(target_id: str) -> str:
    """A collision-free filename for a target. A readable stem (":" → "_") plus a
    hash of the FULL id, so distinct targets that sanitize to the same stem (e.g.
    'a:b' and 'a_b') never share one history file — the mapping is injective."""
    tid = _safe_target(target_id)
    stem = tid.replace(":", "_")[:64]
    digest = hashlib.sha256(tid.encode("utf-8")).hexdigest()[:12]
    return f"{stem}.{digest}.jsonl"


def _path(target_id: str):
    return config.MEMORY_DIR / "deltas" / _filename(target_id)


def _read(target_id: str) -> list[dict]:
    try:
        raw = notes.read_raw(_path(target_id), 16 * 1024 * 1024)
        if raw is None:
            return []
        if not raw.endswith(b"\n"):
            raise DeltaError("corrupt snapshot history: incomplete snapshot tail; preserve history")
        out = []
        for line_number, line in enumerate(raw.splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = oe.decode(line, "delta", 1024 * 1024)
                oe.fields(row, ("schema", "target_id", "seq", "prev", "kind", "state",
                               "delta", "provenance", "snapshot_hash", "publicKey", "signature"))
                oe.integer(row["seq"])
                if row["schema"] != SNAP_SCHEMA or row["target_id"] != target_id:
                    raise ValueError("snapshot identity differs")
            except (oe.OwnerEvidenceStateError, ValueError, TypeError) as err:
                raise DeltaError(
                    f"corrupt snapshot record at line {line_number}; preserve history: {err}"
                ) from err
            out.append(row)
            if len(out) > _MAX_SNAPSHOTS:
                raise ValueError("snapshot count exceeds bound")
        return out
    except (oe.OwnerEvidenceStateError, ValueError, TypeError) as err:
        raise DeltaError("snapshot history unavailable: " + str(err)) from err


def _guard(target_id):
    key = hashlib.sha256((str(config.MEMORY_DIR.absolute()) + "\0" + _safe_target(target_id)).encode()).hexdigest()
    return proclock.lock("delta-" + key)


def _core(target_id, seq, prev, kind, state, delta, provenance) -> dict:
    return {"schema": SNAP_SCHEMA, "target_id": target_id, "seq": int(seq),
            "prev": prev, "kind": str(kind), "state": state,
            "delta": delta, "provenance": provenance}


def record_snapshot(target_id: str, *, kind: str, state,
                    delta=None, provenance: Provenance | dict | None = None,
                    operation_id=None, require_signature=False) -> dict:
    """Durable serialized append; idempotency is covered by the signed delta.

    A failed external anchor is distinct from the locally published record.
    Retrying the same operation reuses that record and retries only its anchor.
    """
    prov = (provenance if isinstance(provenance, Provenance)
            else Provenance.from_dict(provenance)).to_dict()
    if operation_id is not None:
        oe.text(operation_id, 128)
        delta = {"operation_id": operation_id, "value": delta}
    with _guard(target_id):
        existing = _read(target_id)
        previous = None
        for row in existing:
            core = {k: row[k] for k in ("schema", "target_id", "seq", "prev", "kind", "state", "delta", "provenance")}
            if _content_hash(core) != row["snapshot_hash"]:
                raise DeltaError("cannot extend a damaged history")
            if previous is not None and (row["prev"] != previous["snapshot_hash"] or row["seq"] != previous["seq"] + 1):
                raise DeltaError("cannot extend a broken chain")
            if (require_signature or row["signature"]) and not _snapshot_ok(row, target_id):
                raise DeltaError("cannot extend unverifiable evidence")
            previous = row
        snap = None
        if operation_id is not None:
            for row in existing:
                if isinstance(row["delta"], dict) and row["delta"].get("operation_id") == operation_id:
                    if (row["kind"], row["state"], row["delta"], row["provenance"]) != (kind, state, delta, prov):
                        raise DeltaError("operation retry differs from persisted evidence")
                    snap = row
                    break
        if snap is None:
            core = _core(target_id, existing[-1]["seq"] + 1 if existing else 0,
                         existing[-1]["snapshot_hash"] if existing else None,
                         kind, state, delta, prov)
            # Reject unsupported/nonfinite JSON before signing or writing.
            json.dumps(core, allow_nan=False)
            snap_hash = _content_hash(core)
            snap = dict(core, snapshot_hash=snap_hash)
            try:
                snap["publicKey"] = witness.sub_public_key_hex(SNAP_LABEL)
                snap["signature"] = witness.sign_with(SNAP_LABEL, snap_hash.encode("utf-8"))
            except witness.WitnessError:
                if require_signature:
                    raise DeltaError("signing unavailable; qualification not recorded")
                snap["publicKey"] = snap["signature"] = ""
            kept = (existing + [snap])[-_MAX_SNAPSHOTS:]
            raw = "".join(json.dumps(s, ensure_ascii=False, allow_nan=False) + "\n" for s in kept).encode()
            if len(raw) > 16 * 1024 * 1024 or any(len(json.dumps(s).encode()) > 1024 * 1024 for s in kept):
                raise DeltaError("snapshot history bound exceeded")
            try:
                notes.publish(_path(target_id), raw)
            except (OSError, oe.OwnerEvidenceStateError) as err:
                raise DeltaError("snapshot publication unconfirmed; reread before retry") from err
        else:
            # An earlier rename may have succeeded before its directory fsync
            # failed. Seeing matching bytes is not a durability acknowledgement.
            try:
                notes.publish(_path(target_id), notes.read_raw(_path(target_id), 16 * 1024 * 1024))
            except (OSError, oe.OwnerEvidenceStateError) as err:
                raise DeltaError("snapshot durability still unconfirmed") from err
        # Keep append and anchor publication ordered. Retrying an older
        # operation must never move an external head backwards.
        from . import anchor
        head = existing[-1] if existing and existing[-1]["seq"] > snap["seq"] else snap
        anchored = anchor.publish_head("delta", target_id, head["seq"], head["snapshot_hash"])
        if require_signature and anchor.enabled() and not anchored:
            raise DeltaError("local evidence persisted; configured anchor unavailable; retry the same operation")
    return snap


def qualified_snapshots(target_id):
    """Read qualifying evidence; absent is distinct from unreadable/unsigned.

    A configured anchor must match this exact head. When anchoring is off the
    retained signed window is the explicit local-custody boundary.
    """
    with _guard(target_id):
        rows = _read(target_id)
        if not rows:
            return []
        verdict = verify_history(target_id)
        if not verdict["ok"]:
            raise DeltaError("qualification history unavailable: " + "; ".join(verdict["problems"]))
        from . import anchor
        if anchor.enabled():
            try:
                record = anchor._sink().read("delta", target_id)
                if (not anchor.record_ok(record) or record["head"] != rows[-1]["snapshot_hash"]
                        or record["seq"] != rows[-1]["seq"]):
                    raise DeltaError("configured anchor does not confirm the current evidence head")
            except Exception as err:
                raise DeltaError("qualification anchor unavailable") from err
        return rows


def snapshots(target_id: str) -> list[dict]:
    return _read(target_id)


def latest(target_id: str) -> dict | None:
    recs = _read(target_id)
    return recs[-1] if recs else None


def restore(target_id: str, snapshot_hash: str):
    """Non-destructive rollback: return the `state` captured by a prior snapshot
    (does NOT mutate the target — the caller reloads that state). Raises if the
    snapshot is absent OR fails verification (never restore untrusted state)."""
    for rec in _read(target_id):
        if rec.get("snapshot_hash") == snapshot_hash:
            if not _snapshot_ok(rec, target_id):
                raise DeltaError("refusing to restore an unverifiable snapshot")
            return rec.get("state")
    raise DeltaError(f"no snapshot {snapshot_hash!r} for target {target_id!r}")


def _snapshot_ok(rec: dict, expected_target: str) -> bool:
    if not isinstance(rec, dict) or rec.get("schema") != SNAP_SCHEMA:
        return False
    # Bind the record to the target being verified: a genuinely-signed snapshot
    # transplanted from ANOTHER target's file must not validate here (it self-
    # references its own target_id, so without this the signature would pass).
    if rec.get("target_id") != expected_target:
        return False
    core = _core(rec.get("target_id"), rec.get("seq", -1), rec.get("prev"),
                 rec.get("kind"), rec.get("state"), rec.get("delta"),
                 rec.get("provenance"))
    if _content_hash(core) != rec.get("snapshot_hash"):
        return False
    try:
        expected = witness.sub_public_key_hex(SNAP_LABEL)
    except witness.WitnessError:
        return False
    if str(rec.get("publicKey", "")).lower() != expected.lower():
        return False
    return witness.verify_signature(expected,
                                    str(rec["snapshot_hash"]).encode("utf-8"),
                                    str(rec.get("signature", "")))


def verify_history(target_id: str) -> dict:
    """Verify a target's snapshot history: every record signed and content-
    addressed, seq contiguous within the retained window, and each `prev` links
    to the prior retained snapshot. Fail closed.

    Returns {ok, found, count, verified, attested, problems}. Because the window
    rolls, seq may start above 0 (older snapshots trimmed) — contiguity is
    checked from the first retained record, not from genesis.

    A corrupt (unparseable) record is a verification FAILURE reported here as
    ok=False — never an exception escaping into the caller, so the documented
    fail-closed verdict contract holds even against a garbage-injecting
    attacker."""
    try:
        recs = _read(target_id)
    except DeltaError as err:
        return {"ok": False, "found": True, "count": 0, "verified": 0,
                "attested": False, "problems": [str(err)]}
    out = {"ok": False, "found": bool(recs), "count": len(recs),
           "verified": 0, "attested": False, "problems": []}
    if not recs:
        out["problems"].append("no history for target")
        return out
    prev_hash = recs[0].get("prev")
    prev_seq = None
    verified = 0
    for i, rec in enumerate(recs):
        if prev_seq is not None and rec.get("seq") != prev_seq + 1:
            out["problems"].append(f"seq gap at index {i}")
            break
        if rec.get("prev") != prev_hash:
            out["problems"].append(f"broken chain link at index {i}")
            break
        if not _snapshot_ok(rec, target_id):
            out["problems"].append(f"invalid signature at index {i}")
            break
        prev_hash = rec["snapshot_hash"]
        prev_seq = rec.get("seq")
        verified = i + 1
    out["verified"] = verified
    out["ok"] = verified == len(recs) and not out["problems"]
    if out["ok"]:
        try:
            out["attested"] = witness.available() and not witness.is_default_seed()
        except Exception:
            out["attested"] = False
    return out

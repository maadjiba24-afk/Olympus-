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

from . import config, security, witness

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
    path = _path(target_id)
    if not path.exists():
        return []
    out: list[dict] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except (ValueError, UnicodeDecodeError) as err:
            # A syntactically corrupt line (FS corruption, or a hostile writer —
            # these logs are signed precisely against an attacker with FS write
            # access). Raise a TYPED error so verify_history reports it as a
            # fail-closed verdict and record_snapshot refuses to extend a
            # corrupt history, rather than a bare JSONDecodeError escaping into
            # every consumer. The raw line is NOT echoed (it may carry state).
            raise DeltaError(
                f"corrupt snapshot record at line {i} for target "
                f"{target_id!r}: {err.__class__.__name__}") from err
    return out


def _core(target_id, seq, prev, kind, state, delta, provenance) -> dict:
    return {"schema": SNAP_SCHEMA, "target_id": target_id, "seq": int(seq),
            "prev": prev, "kind": str(kind), "state": state,
            "delta": delta, "provenance": provenance}


def record_snapshot(target_id: str, *, kind: str, state,
                    delta=None, provenance: Provenance | dict | None = None) -> dict:
    """Append one signed, content-addressed, hash-chained snapshot of a learned
    target's state. Append-only and non-destructive: prior versions are never
    overwritten. `state` is the full post-delta state (e.g. a playbook dict);
    `delta` is the change that produced it; `provenance` attributes it.

    A rolling window keeps the newest `_MAX_SNAPSHOTS`; trimming drops the oldest
    while the retained window stays internally chain-verifiable."""
    prov = (provenance if isinstance(provenance, Provenance)
            else Provenance.from_dict(provenance)).to_dict()
    with _lock_for(target_id):
        existing = _read(target_id)
        seq = (existing[-1]["seq"] + 1) if existing else 0
        prev = existing[-1]["snapshot_hash"] if existing else None
        core = _core(target_id, seq, prev, kind, state,
                     delta if delta is not None else None, prov)
        snap_hash = _content_hash(core)
        snap = dict(core)
        snap["snapshot_hash"] = snap_hash
        try:
            snap["publicKey"] = witness.sub_public_key_hex(SNAP_LABEL)
            snap["signature"] = witness.sign_with(SNAP_LABEL,
                                                  snap_hash.encode("utf-8"))
        except witness.WitnessError:
            snap["publicKey"] = ""
            snap["signature"] = ""
        kept = (existing + [snap])[-_MAX_SNAPSHOTS:]
        path = _path(target_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(json.dumps(s, ensure_ascii=False) + "\n"
                               for s in kept), encoding="utf-8")
        tmp.replace(path)
    # Anchor the new head OUT of the host's write domain so truncation is
    # detectable (no-op when anchoring is off; fail-LOUD when on).
    from . import anchor
    anchor.publish_head("delta", target_id, snap["seq"], snap["snapshot_hash"])
    return snap


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

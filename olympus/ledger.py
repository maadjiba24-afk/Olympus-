"""Checkpointed execution ledger (Plane 2.1) — every step a signed,
content-addressed checkpoint node; a linear run is a hash chain with branching
factor 1.

The decision log (`trace.py`) records a whole run and signs it once, at flush —
so a run killed mid-flight leaves *nothing* durable. This ledger commits each
step as it completes: an append-only, per-run chain of checkpoint nodes, each

  * content-addressed — `node_hash = sha256(canonical(core))` over
    {schema, run_id, seq, parent, step, state};
  * chained — `parent` is the previous node's `node_hash`, so any reordering,
    insertion, or edit of an earlier node breaks every node after it;
  * signed — under a domain-separated witness subkey (as `mandate`/`identity`),
    so the chain verifies under the root of trust (the "0.1 key").

That makes a run resumable and tamper-evident. `drive()` executes a plan against
a ledger and SKIPS steps already committed, so a killed run re-driven with the
same plan resumes from its last checkpoint and completes — and, because steps
are content-addressed, its `replay_hash` is byte-identical to an uninterrupted
control run of the same plan.

Speculative *branches* (Plane 2.2) will fork this same chain; here every run is
linear (branching factor 1).
"""

from __future__ import annotations

import hashlib
import json
import threading

from . import config, witness

SCHEMA = "olympus-checkpoint/1"
LABEL = "checkpoint/v1"          # witness subkey (domain separation)

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class LedgerError(ValueError):
    """A checkpoint could not be minted, or a ledger is malformed/untrusted."""


# --- content addressing + signing -----------------------------------------

def _content_hash(core: dict) -> str:
    return hashlib.sha256(witness.canonical_json(core)).hexdigest()


def _core(run_id: str, seq: int, parent: str | None, step, state) -> dict:
    return {"schema": SCHEMA, "run_id": run_id, "seq": int(seq),
            "parent": parent, "step": step, "state": state}


def _seal_node(run_id: str, seq: int, parent: str | None, step, state) -> dict:
    """Mint a signed, content-addressed checkpoint node. The signature is over
    the content address, so it simultaneously attests the node's contents and
    its position in the chain (`parent` is part of the hashed core)."""
    core = _core(run_id, seq, parent, step, state)
    node_hash = _content_hash(core)
    node = dict(core)
    node["node_hash"] = node_hash
    try:
        node["publicKey"] = witness.sub_public_key_hex(LABEL)
        node["signature"] = witness.sign_with(LABEL, node_hash.encode("utf-8"))
    except witness.WitnessError:
        # Crypto unavailable: still commit an (unsigned) checkpoint so a run can
        # resume; verify_ledger() will report it unverified (fail closed).
        node["publicKey"] = ""
        node["signature"] = ""
    return node


def _node_signature_ok(node: dict) -> bool:
    """The node's content address recomputes AND its signature validates under
    our own checkpoint subkey (fail closed on any mismatch)."""
    if not isinstance(node, dict) or node.get("schema") != SCHEMA:
        return False
    core = _core(node.get("run_id"), node.get("seq", -1), node.get("parent"),
                 node.get("step"), node.get("state"))
    recomputed = _content_hash(core)
    if recomputed != node.get("node_hash"):
        return False        # tampered contents (or wrong position in the chain)
    try:
        expected = witness.sub_public_key_hex(LABEL)
    except witness.WitnessError:
        return False
    if str(node.get("publicKey", "")).lower() != expected.lower():
        return False        # unsigned or foreign / cross-artifact key
    return witness.verify_signature(expected, recomputed.encode("utf-8"),
                                    str(node.get("signature", "")))


# --- per-run ledger file ---------------------------------------------------

_ALLOWED_ID = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")


def _safe_id(run_id: str) -> str:
    rid = str(run_id)
    # Reject (never silently mangle) a run_id that isn't a clean filename — a
    # path separator or traversal segment must not reach the filesystem.
    if not rid or rid in (".", "..") or any(c not in _ALLOWED_ID for c in rid):
        raise LedgerError(f"invalid run_id {run_id!r}")
    return rid


def _path(run_id: str):
    return config.MEMORY_DIR / "ledger" / f"{_safe_id(run_id)}.jsonl"


def _lock_for(run_id: str) -> threading.Lock:
    with _locks_guard:
        lk = _locks.get(run_id)
        if lk is None:
            lk = _locks[run_id] = threading.Lock()
        return lk


def _heal_tail(path) -> None:
    """Ensure the file ends on a clean record boundary before an append. A kill
    can leave the final line either (a) complete JSON missing only its trailing
    newline, or (b) genuinely torn (partial bytes). Case (a) is a committed
    checkpoint — exactly as `_read_nodes` treats it — so terminate it with a
    newline rather than dropping it; case (b) is truncated so the next record
    can't concatenate onto partial bytes. Keeping this consistent with
    `_read_nodes` is what prevents a committed node from being lost on resume.
    The last-byte check is O(1); the full read runs only on the rare heal path."""
    if not path.exists():
        return
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        if size == 0:
            return
        f.seek(size - 1)
        if f.read(1) == b"\n":
            return
    raw = path.read_bytes()
    nl = raw.rfind(b"\n")
    tail = raw[nl + 1:]
    try:
        json.loads(tail.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        with path.open("wb") as f:      # (b) torn partial bytes — drop them
            f.write(raw[:nl + 1] if nl >= 0 else b"")
    else:
        with path.open("ab") as f:      # (a) committed node — just terminate it
            f.write(b"\n")


def _read_nodes(run_id: str) -> list[dict]:
    """Load a run's committed nodes, tolerating a torn trailing line (a write
    interrupted by a kill) by dropping only that last, unparseable line."""
    path = _path(run_id)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    nodes: list[dict] = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            nodes.append(json.loads(line))
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                break               # torn final write — safe to drop
            raise LedgerError(f"corrupt ledger line {i} for run {run_id!r}")
    return nodes


class Ledger:
    """A single run's append-only checkpoint chain (branching factor 1)."""

    def __init__(self, run_id: str, nodes: list[dict] | None = None):
        self.run_id = str(run_id)
        self._nodes = nodes if nodes is not None else _read_nodes(run_id)

    # -- reads --
    @property
    def nodes(self) -> list[dict]:
        return list(self._nodes)

    def head(self) -> dict | None:
        return self._nodes[-1] if self._nodes else None

    def next_seq(self) -> int:
        return len(self._nodes)

    def steps(self) -> list:
        return [n.get("step") for n in self._nodes]

    def state(self) -> object:
        """The resumable state after the last committed step (None if empty)."""
        h = self.head()
        return h.get("state") if h else None

    def replay_hash(self) -> str:
        """A byte-stable digest of the committed STEP chain — the reasoning path,
        invariant to wall-clock and to whether the run was resumed. Two runs of
        the same plan (one uninterrupted, one killed-and-resumed) share it."""
        return hashlib.sha256(witness.canonical_json(
            {"steps": self.steps()})).hexdigest()

    # -- writes --
    def append(self, step, state) -> dict:
        """Commit one checkpoint: mint the signed node, then atomically append a
        single line. Serialized per run so concurrent appends can't interleave."""
        with _lock_for(self.run_id):
            # Re-read the head under the lock so seq/parent reflect any node a
            # concurrent writer committed since this Ledger was constructed.
            on_disk = _read_nodes(self.run_id)
            self._nodes = on_disk
            parent = on_disk[-1]["node_hash"] if on_disk else None
            node = _seal_node(self.run_id, len(on_disk), parent, step, state)
            path = _path(self.run_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            _heal_tail(path)     # truncate any torn final line before appending
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(node, ensure_ascii=False) + "\n")
                f.flush()
            self._nodes.append(node)
        # Anchor the new head OUT of the host's write domain so truncation is
        # detectable (no-op when anchoring is off; fail-LOUD when on).
        from . import anchor
        anchor.publish_head("checkpoint", self.run_id, node["seq"],
                            node["node_hash"])
        return node


def open_run(run_id: str) -> Ledger:
    """Open (or resume) a run's ledger — loads any already-committed chain."""
    return Ledger(run_id)


def checkpoint(run_id: str, step, state) -> dict:
    return open_run(run_id).append(step, state)


# --- verification (under the witness / 0.1 key) ---------------------------

def verify_ledger(run_id: str) -> dict:
    """Verify a run's whole chain: every node signed under our checkpoint key,
    content-addresses intact, seq contiguous from 0, and each `parent` links to
    the prior node's `node_hash`. Fail closed — any break makes `ok` False and
    names the first offending seq.

    Returns {ok, found, count, verified, attested, problems}. `verified` is the
    count of leading nodes that verify (the safe resume prefix); `attested` is
    True only when the chain is signed under a configured (non-dev) seed. A
    corrupt (unparseable) middle record is reported as ok=False, never raised."""
    try:
        nodes = _read_nodes(run_id)
    except LedgerError as err:
        return {"ok": False, "found": True, "count": 0, "verified": 0,
                "attested": False, "problems": [str(err)]}
    out = {"ok": False, "found": bool(nodes), "count": len(nodes),
           "verified": 0, "attested": False, "problems": []}
    if not nodes:
        out["problems"].append("no ledger for run")
        return out
    parent = None
    verified = 0
    for i, node in enumerate(nodes):
        if node.get("seq") != i:
            out["problems"].append(f"seq {node.get('seq')!r} out of order at {i}")
            break
        if node.get("parent") != parent:
            out["problems"].append(f"broken parent link at seq {i}")
            break
        if not _node_signature_ok(node):
            out["problems"].append(f"invalid signature at seq {i}")
            break
        parent = node["node_hash"]
        verified = i + 1
    out["verified"] = verified
    out["ok"] = verified == len(nodes) and not out["problems"]
    if out["ok"]:
        try:
            out["attested"] = witness.available() and not witness.is_default_seed()
        except Exception:
            out["attested"] = False
    return out


def replay_hash(run_id: str) -> str:
    return open_run(run_id).replay_hash()


# --- resume-aware driver ---------------------------------------------------

def drive(run_id: str, plan, executor, *, stop_before: int | None = None):
    """Execute `plan` (a sequence of step descriptors) against the run's ledger,
    committing a checkpoint per step and SKIPPING steps already committed — so a
    killed run re-driven with the same plan resumes from its last checkpoint and
    completes.

      executor(prev_state, step, index) -> new_state   (deterministic)

    `stop_before` (test hook) simulates a mid-flight kill: stop cleanly before
    committing the step at that index, leaving a valid, resumable prefix.
    Returns the final committed state.

    A resume checks that the already-committed steps still MATCH the plan: if
    the plan diverged from what was recorded, that is a real divergence and we
    refuse rather than silently continue on a mismatched chain (fail closed)."""
    plan = list(plan)
    led = open_run(run_id)
    committed = led.steps()
    for i, step in enumerate(committed):
        if i >= len(plan):
            break
        if witness.canonical_json(step) != witness.canonical_json(plan[i]):
            raise LedgerError(
                f"resume divergence at step {i}: recorded step differs from plan")
    state = led.state()
    for i in range(led.next_seq(), len(plan)):
        if stop_before is not None and i >= stop_before:
            break
        state = executor(state, plan[i], i)
        led.append(plan[i], state)
    return state


# --- speculation records (Plane 2.2 audit trail) ---------------------------
#
# Exploration (tree search) forks the committed chain into UNCOMMITTED branches.
# Those branches never enter the linear chain, but the fact that a branch was
# explored — and that a branch was pruned, and why — is part of the run's
# tamper-evident record. Each such record is content-addressed and signed under
# its own subkey, kept in a per-run side log so it can never be confused with a
# committed checkpoint.

SPEC_SCHEMA = "olympus-speculation/1"
SPEC_LABEL = "speculation/v1"       # witness subkey (distinct from checkpoints)


def _spec_path(run_id: str):
    return config.MEMORY_DIR / "ledger" / f"{_safe_id(run_id)}.spec.jsonl"


def _read_spec(run_id: str) -> list[dict]:
    path = _spec_path(run_id)
    if not path.exists():
        return []
    out: list[dict] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except (ValueError, UnicodeDecodeError) as err:
            # Typed error (matches _read_nodes) so verify_speculation reports a
            # fail-closed verdict instead of leaking a bare JSONDecodeError.
            raise LedgerError(
                f"corrupt speculation record at line {i} for run {run_id!r}: "
                f"{err.__class__.__name__}") from err
    return out


def _spec_core(run_id: str, seq: int, kind: str, info) -> dict:
    return {"schema": SPEC_SCHEMA, "run_id": run_id, "seq": int(seq),
            "kind": str(kind), "info": info}


def record_speculation(run_id: str, kind: str, info) -> dict:
    """Append a signed, content-addressed speculation record (`kind` is
    'branch' | 'prune' | 'halt'). These audit UNCOMMITTED exploration and never
    enter the committed chain. Serialized per run so seqs can't collide."""
    if kind not in ("branch", "prune", "halt"):
        raise LedgerError(f"unknown speculation kind {kind!r}")
    with _lock_for(run_id + ".spec"):
        seq = len(_read_spec(run_id))
        core = _spec_core(run_id, seq, kind, info)
        rec_hash = _content_hash(core)
        rec = dict(core)
        rec["record_hash"] = rec_hash
        try:
            rec["publicKey"] = witness.sub_public_key_hex(SPEC_LABEL)
            rec["signature"] = witness.sign_with(SPEC_LABEL,
                                                 rec_hash.encode("utf-8"))
        except witness.WitnessError:
            rec["publicKey"] = ""
            rec["signature"] = ""
        path = _spec_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
    from . import anchor
    anchor.publish_head("speculation", run_id, rec["seq"], rec["record_hash"])
    return rec


def speculation_records(run_id: str) -> list[dict]:
    return _read_spec(run_id)


def _spec_record_ok(rec: dict) -> bool:
    if not isinstance(rec, dict) or rec.get("schema") != SPEC_SCHEMA:
        return False
    core = _spec_core(rec.get("run_id"), rec.get("seq", -1),
                      rec.get("kind"), rec.get("info"))
    if _content_hash(core) != rec.get("record_hash"):
        return False
    try:
        expected = witness.sub_public_key_hex(SPEC_LABEL)
    except witness.WitnessError:
        return False
    if str(rec.get("publicKey", "")).lower() != expected.lower():
        return False
    return witness.verify_signature(expected, str(rec["record_hash"]).encode("utf-8"),
                                    str(rec.get("signature", "")))


def verify_speculation(run_id: str) -> dict:
    """Verify a run's speculation side log: every record content-addressed,
    contiguous seq from 0, and signed under the speculation key. Fail closed —
    a corrupt (unparseable) record is reported as ok=False, never raised."""
    try:
        recs = _read_spec(run_id)
    except LedgerError as err:
        return {"ok": False, "found": True, "count": 0, "verified": 0,
                "attested": False, "problems": [str(err)]}
    out = {"ok": False, "found": bool(recs), "count": len(recs),
           "verified": 0, "attested": False, "problems": []}
    verified = 0
    for i, rec in enumerate(recs):
        if rec.get("seq") != i:
            out["problems"].append(f"seq {rec.get('seq')!r} out of order at {i}")
            break
        if not _spec_record_ok(rec):
            out["problems"].append(f"invalid speculation record at {i}")
            break
        verified = i + 1
    out["verified"] = verified
    out["ok"] = bool(recs) and verified == len(recs) and not out["problems"]
    if out["ok"]:
        try:
            out["attested"] = witness.available() and not witness.is_default_seed()
        except Exception:
            out["attested"] = False
    return out

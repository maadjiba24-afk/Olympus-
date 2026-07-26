"""Sealed session journal — append-only, hash-linked persistence per session.

Wave-1 C1 (docs/absorption/WAVE1_IMPLEMENTATION_SPEC.md): the conversation
snapshot is a whole-file rewrite per turn, and a corrupt snapshot used to load
as `[]` — silent history loss. Each turn now also appends one sealed JSONL
record to `MEMORY_DIR/sessions/<safe_id>.journal.jsonl`; on snapshot
corruption, `memory.load_conversation` rebuilds history from the journal's
verified prefix instead.

Record: `{"v","seq","ts","kind","conversation_id","payload","prev","sha"}`.
`sha` seals the canonical serialization (sort_keys, separators (",",":"),
ensure_ascii=False) of the record with `sha` blanked; `prev` is the previous
record's sha ("" for the first) — hash-linked. Commit = a verifying line
terminated by "\n", written in ONE write() call, flushed every append; fsync
per record when OLYMPUS_SESSION_FSYNC=always, else once at turn-end (auto,
default). Reject-never-repair: a torn/unverifiable FINAL line is truncated
(the only permitted mutation, I-J6); anything bad earlier quarantines the file
by copy to `sessions/quarantine/` and reads stop at the verified boundary —
never silently past it. `compact()` is the sanctioned rewrite (tmp+replace).
"""

from __future__ import annotations

import hashlib
import json
import os
import time

from . import config

SCHEMA_VERSION = "1.0"


def enabled() -> bool:
    """OLYMPUS_SESSION_JOURNAL — default on (the journal is purely additive;
    the snapshot stays the source of truth and default read path)."""
    return os.environ.get("OLYMPUS_SESSION_JOURNAL", "on").strip().lower() \
        not in ("0", "off", "false", "no")


def _fsync_always() -> bool:
    return os.environ.get("OLYMPUS_SESSION_FSYNC", "auto").strip().lower() \
        == "always"


def _max_bytes() -> int:
    try:
        mb = float(os.environ.get("OLYMPUS_SESSION_JOURNAL_MAX_MB", "64"))
    except ValueError:
        mb = 64.0
    return max(1, int(mb * 1024 * 1024))


def _sid(conversation_id: str) -> str:
    from . import memory
    return memory.safe_id(conversation_id)


def _journal_path(sid: str):
    d = config.MEMORY_DIR / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{sid}.journal.jsonl"


def _quarantine_dir():
    d = config.MEMORY_DIR / "sessions" / "quarantine"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _locked(sid: str):
    from . import proclock
    return proclock.lock(f"session-journal-{sid}")


def _canonical(rec: dict) -> str:
    return json.dumps(rec, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _seal(rec: dict) -> str:
    return hashlib.sha256(
        _canonical(dict(rec, sha="")).encode("utf-8")).hexdigest()


def _check(line: bytes, prev_sha: str, prev_seq: int, first: bool, sid: str):
    """Verify one journal line. Returns (record|None, reason, fatal). A
    non-fatal failure (unparseable bytes, seal mismatch) on the FINAL line is
    a torn write; a fatal one (chain/seq/version violation on a self-sealed
    record) is corruption wherever it sits — a torn write can't produce it."""
    try:
        rec = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "unparseable line", False
    if not isinstance(rec, dict) or not isinstance(rec.get("sha"), str):
        return None, "malformed record", False
    if rec["sha"] != _seal(rec):
        return None, "seal (sha) mismatch", False
    major = str(rec.get("v", "")).partition(".")[0]
    if major != "1":                    # unknown MAJOR ⇒ refuse (I-J7);
        return None, f"unknown version {rec.get('v')!r}", True
    seq = rec.get("seq")                # unknown minor reads fine above.
    if not isinstance(seq, int) or seq < 1:
        return None, "bad seq", True
    if rec.get("conversation_id") != sid:
        return None, "conversation_id mismatch", True
    if first:
        # seq > 1 with any prev is the surviving suffix of a compaction;
        # seq 1 must be a true chain head.
        if seq == 1 and rec.get("prev") != "":
            return None, "seq 1 with non-empty prev", True
    else:
        if seq != prev_seq + 1:
            return None, f"seq {seq} after {prev_seq}", True
        if rec.get("prev") != prev_sha:
            return None, "prev-link mismatch", True
    return rec, "", False


def _quarantine(sid: str, data: bytes, reason: str) -> None:
    from . import errors
    try:
        digest = hashlib.sha256(data).hexdigest()
        qdir = _quarantine_dir()
        if not any(hashlib.sha256(p.read_bytes()).hexdigest() == digest
                   for p in qdir.glob(f"{sid}.*.journal")):
            stamp = time.strftime("%Y%m%d-%H%M%S")
            for n in range(100):
                q = qdir / (f"{sid}.{stamp}.journal" if n == 0
                            else f"{sid}.{stamp}-{n}.journal")
                if not q.exists():
                    q.write_bytes(data)
                    break
    except OSError:
        pass
    errors.capture(
        "sessionlog",
        ValueError(f"journal corruption in session {sid}: {reason}"),
        context="quarantined by copy; reads stop at the verified prefix")


def _scan(sid: str):
    """Parse + verify the journal. Caller holds the session lock. Returns
    (verified records, status) and applies the one permitted mutation:
    truncating a torn tail back to the last verified record."""
    path = _journal_path(sid)
    if not path.exists():
        return [], "absent"
    if path.stat().st_size > _max_bytes():
        return [], "oversize"
    data = path.read_bytes()
    records: list[dict] = []
    pos = verified_end = 0
    prev_sha, prev_seq = "", 0
    torn, corrupt = False, ""
    n = len(data)
    while pos < n:
        nl = data.find(b"\n", pos)
        if nl == -1:                    # unterminated tail: never committed
            torn = True
            break
        rec, reason, fatal = _check(data[pos:nl], prev_sha, prev_seq,
                                    not records, sid)
        if rec is None:
            if nl + 1 >= n and not fatal:
                torn = True             # byte-damaged final line = torn write
            else:
                corrupt = reason
            break
        records.append(rec)
        prev_sha, prev_seq = rec["sha"], rec["seq"]
        pos = verified_end = nl + 1
    if corrupt:
        _quarantine(sid, data, corrupt)
        return records, "quarantined"
    if torn:
        with open(path, "r+b") as f:    # I-J6: the ONLY permitted mutation
            f.truncate(verified_end)
        return records, "torn_tail_truncated"
    return records, "ok"


def _open_append(path):
    return open(path, "ab")             # seam for fault injection in tests


def _append_records(sid: str, entries, records=None) -> int:
    """Seal and append (kind, payload) entries. Caller holds the lock.
    Returns the last seq written; raises on a dead (quarantined/oversize)
    journal or an I/O failure — public wrappers capture, never propagate."""
    if records is None:
        records, status = _scan(sid)
        if status in ("quarantined", "oversize"):
            raise OSError(
                f"journal for {sid} is {status}; compact() or delete_session()")
    prev_sha = records[-1]["sha"] if records else ""
    seq = records[-1]["seq"] if records else 0
    always = _fsync_always()
    f = _open_append(_journal_path(sid))
    try:
        for kind, payload in entries:
            seq += 1
            rec = {"v": SCHEMA_VERSION, "seq": seq, "ts": time.time(),
                   "kind": kind, "conversation_id": sid, "payload": payload,
                   "prev": prev_sha, "sha": ""}
            rec["sha"] = _seal(rec)
            # One write() per record ending "\n" — commit = the line verifies.
            f.write((_canonical(rec) + "\n").encode("utf-8"))
            f.flush()
            if always:
                os.fsync(f.fileno())
            prev_sha = rec["sha"]
        if not always:                  # auto: fsync once at turn-end/close
            os.fsync(f.fileno())
    finally:
        f.close()
    return seq


def append_turn(conversation_id: str, messages) -> int:
    """Append one sealed turn record (the messages added this turn). Returns
    its seq, or 0 when journaling is off or the append failed — a journal
    failure is captured and never blocks the reply."""
    if not enabled():
        return 0
    sid = _sid(conversation_id)
    try:
        with _locked(sid):
            return _append_records(
                sid, [("turn", {"messages": list(messages)})])
    except Exception as err:
        from . import errors
        errors.capture("sessionlog.append_turn", err, context=sid)
        return 0


def append_tombstone(conversation_id: str, from_seq: int,
                     through_seq: int) -> int:
    """Logically delete the turn records in [from_seq, through_seq]: recovery
    skips them immediately; compact() through them removes the bytes."""
    if not enabled():
        return 0
    sid = _sid(conversation_id)
    try:
        with _locked(sid):
            return _append_records(
                sid, [("tombstone", {"from_seq": int(from_seq),
                                     "through_seq": int(through_seq)})])
    except Exception as err:
        from . import errors
        errors.capture("sessionlog.append_tombstone", err, context=sid)
        return 0


def sync(conversation_id: str, history) -> int:
    """Journal the delta between the reconstructed journal history and the
    snapshot just written (the memory.save_conversation hook). A shrunk or
    rewritten history (compaction, /clear) is recorded as a reset followed by
    the full new history. Never raises; returns the last seq written (0 =
    nothing new, journaling off, or failure)."""
    if not enabled():
        return 0
    sid = _sid(conversation_id)
    try:
        with _locked(sid):
            records, status = _scan(sid)
            if status in ("quarantined", "oversize"):
                raise OSError(f"journal for {sid} is {status}; "
                              "compact() or delete_session()")
            current = _replay(records)
            history = list(history)
            if current == history:
                return 0
            if len(history) > len(current) and \
                    history[:len(current)] == current:
                entries = [("turn", {"messages": history[len(current):]})]
            else:
                entries = [("reset", {})]
                if history:
                    entries.append(("turn", {"messages": history}))
            return _append_records(sid, entries, records)
    except Exception as err:
        from . import errors
        errors.capture("sessionlog.sync", err, context=sid)
        return 0


def _replay(records) -> list[dict]:
    """Reconstruct the message history from verified records: turns extend,
    resets clear, tombstoned turn seqs are skipped, marks are boundaries."""
    dead = [(r["payload"].get("from_seq", 0), r["payload"].get("through_seq", 0))
            for r in records
            if r.get("kind") == "tombstone" and isinstance(r.get("payload"), dict)]
    out: list[dict] = []
    for rec in records:
        kind = rec.get("kind")
        if kind == "reset":
            out = []
        elif kind == "turn":
            if any(a <= rec["seq"] <= b for a, b in dead):
                continue
            payload = rec.get("payload")
            if isinstance(payload, dict):
                out.extend(payload.get("messages") or [])
    return out


def read_verified(conversation_id: str):
    """All verified records plus the journal status
    (ok|torn_tail_truncated|quarantined|absent|oversize)."""
    sid = _sid(conversation_id)
    with _locked(sid):
        return _scan(sid)


def journal_status(conversation_id: str) -> str:
    sid = _sid(conversation_id)
    if not _journal_path(sid).exists():
        return "absent"
    with _locked(sid):
        return _scan(sid)[1]


def recover_history(conversation_id: str):
    """Rebuild the message history from the journal's verified prefix — the
    snapshot-corruption recovery path. None when there is nothing to recover
    (absent, empty, or oversize journal — I-J10: recovery refuses beyond
    OLYMPUS_SESSION_JOURNAL_MAX_MB; compact() or delete_session() first)."""
    sid = _sid(conversation_id)
    with _locked(sid):
        records, status = _scan(sid)
    if status in ("absent", "oversize") or not records:
        return None
    return _replay(records)


def compact(conversation_id: str, through_seq: int) -> None:
    """Atomically rewrite the journal dropping the prefix seq <= through_seq
    (physically deleting compacted and tombstoned-then-passed records) and
    seal a snapshot_mark on the tail. Interruption-safe: tmp + os.replace, the
    old journal stays valid until the switch. Also the sanctioned repair for a
    quarantined journal — only the verified prefix survives the rewrite."""
    sid = _sid(conversation_id)
    with _locked(sid):
        records, _status = _scan(sid)
        if not records:
            return
        through_seq = int(through_seq)
        snap = config.MEMORY_DIR / "conversations" / f"{sid}.json"
        snap_sha = hashlib.sha256(snap.read_bytes()).hexdigest() \
            if snap.exists() else ""
        tail = records[-1]
        mark = {"v": SCHEMA_VERSION, "seq": tail["seq"] + 1, "ts": time.time(),
                "kind": "snapshot_mark", "conversation_id": sid,
                "payload": {"snapshot_sha": snap_sha,
                            "through_seq": through_seq},
                "prev": tail["sha"], "sha": ""}
        mark["sha"] = _seal(mark)
        kept = [r for r in records if r["seq"] > through_seq] + [mark]
        path = _journal_path(sid)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with open(tmp, "wb") as f:
                for rec in kept:
                    f.write((_canonical(rec) + "\n").encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass


def delete_session(conversation_id: str) -> None:
    """Hard-delete a session's journal, quarantine copies, and snapshot."""
    sid = _sid(conversation_id)
    with _locked(sid):
        paths = [_journal_path(sid),
                 config.MEMORY_DIR / "conversations" / f"{sid}.json"]
        paths += list(_quarantine_dir().glob(f"{sid}.*.journal"))
        for p in paths:
            try:
                p.unlink()
            except OSError:
                pass

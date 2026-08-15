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
import threading
import time

from . import config

SCHEMA_VERSION = "1.0"

# Hot-path cache for `sync` — see `_cache_get`. Bounded; insertion-ordered so
# the oldest session is the one evicted.
_CACHE_MAX = 64
_CACHE: "dict[str, dict]" = {}
_CACHE_LOCK = threading.Lock()


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


def _stat_key(path):
    """The file identity a cache entry is bound to. `None` when the file is
    gone — an absent journal can never validate a cache entry."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_ino, st.st_dev, st.st_size, st.st_mtime_ns)


def _tail_sha(path) -> str:
    """The `sha` of the journal's last complete line, or "" if there is none.

    Cheap (reads the final block, not the file) and cryptographic: an entry
    whose recorded tail sha still matches cannot have had its chain head
    swapped, so the cached (seq, sha) is safe to extend from."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            back = min(size, 8192)
            f.seek(size - back)
            block = f.read(back)
    except OSError:
        return ""
    if not block.endswith(b"\n"):       # torn tail: force the full path
        return ""
    lines = block.rstrip(b"\n").rsplit(b"\n", 1)
    try:
        rec = json.loads(lines[-1].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ""
    return rec.get("sha", "") if isinstance(rec, dict) else ""


def _cache_get(sid: str, path):
    """The cached scan result for this journal, or None.

    `sync` runs on every turn and used to re-read and re-verify the WHOLE
    journal each time, so a session's journaling cost grew with its own depth:
    measured 15.99 us/record of slope, i.e. a projected 162 ms append at 10k
    records and 801 ms at 50k (Phase-4 Stage-D D1). The verification itself is
    not the problem — repeating it once per turn is.

    An entry is honoured only when BOTH hold: the file's (inode, device, size,
    mtime_ns) is byte-identical to what this process left behind, AND the last
    line's seal still matches the tail sha recorded then. The first catches any
    other process appending or replacing the file; the second catches a rewrite
    that happened to preserve the stat tuple. The caller holds the session lock
    across the check and the append, so nothing can slip in between.

    What is deliberately NOT weakened: every READ path (`read_verified`,
    `recover_history`, `journal_status`, `compact`) still calls `_scan`
    unconditionally and verifies the full chain. This cache only spares the
    write path from re-proving what it proved on the way in, and it is
    per-process — a fresh process always re-verifies from scratch.

    Keyed on the absolute journal PATH, not the bare session id: two different
    MEMORY_DIRs (a relocated home, a test tmp dir) hold different journals for
    the same `sid`, and keying on the id alone would make them alias."""
    with _CACHE_LOCK:
        entry = _CACHE.get(str(path))
    if entry is None or entry["stat"] != _stat_key(path):
        return None
    if entry["tail_sha"] != _tail_sha(path):
        return None                     # rewritten under an identical stat
    return entry


def _cache_put(sid: str, path, *, history, seq: int, sha: str) -> None:
    key = _stat_key(path)
    if key is None:
        return
    entry = {"stat": key, "tail_sha": sha, "seq": seq,
             "history": [dict(m) for m in history]}
    with _CACHE_LOCK:
        _CACHE.pop(str(path), None)
        _CACHE[str(path)] = entry
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))


def _cache_drop(sid: str) -> None:
    with _CACHE_LOCK:
        _CACHE.pop(str(_journal_path(sid)), None)


def _open_append(path):
    return open(path, "ab")             # seam for fault injection in tests


def _append_records(sid: str, entries, tail=None) -> tuple[int, str]:
    """Seal and append (kind, payload) entries. Caller holds the lock.
    `tail` is an already-verified (last_seq, last_sha) pair; None means scan.
    Returns the new (last seq, last sha); raises on a dead (quarantined/
    oversize) journal or an I/O failure — public wrappers capture, never
    propagate."""
    if tail is None:
        records, status = _scan(sid)
        if status in ("quarantined", "oversize"):
            raise OSError(
                f"journal for {sid} is {status}; compact() or delete_session()")
        tail = ((records[-1]["seq"], records[-1]["sha"]) if records else (0, ""))
    seq, prev_sha = tail
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
    return seq, prev_sha


def append_turn(conversation_id: str, messages) -> int:
    """Append one sealed turn record (the messages added this turn). Returns
    its seq, or 0 when journaling is off or the append failed — a journal
    failure is captured and never blocks the reply."""
    if not enabled():
        return 0
    sid = _sid(conversation_id)
    try:
        with _locked(sid):
            seq, _sha = _append_records(
                sid, [("turn", {"messages": list(messages)})])
            _cache_drop(sid)    # this path does not track history; re-scan next
            return seq
    except Exception as err:
        from . import errors
        errors.capture("sessionlog.append_turn", err, context=sid)
        _cache_drop(sid)
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
            seq, _sha = _append_records(
                sid, [("tombstone", {"from_seq": int(from_seq),
                                     "through_seq": int(through_seq)})])
            _cache_drop(sid)    # a tombstone changes what _replay returns
            return seq
    except Exception as err:
        from . import errors
        errors.capture("sessionlog.append_tombstone", err, context=sid)
        _cache_drop(sid)
        return 0


def sync(conversation_id: str, history) -> int:
    """Journal the delta between the reconstructed journal history and the
    snapshot just written (the memory.save_conversation hook). A shrunk or
    rewritten history (compaction, /clear) is recorded as a reset followed by
    the full new history. Never raises; returns the last seq written (0 =
    nothing new, journaling off, or failure).

    Hot path: `_cache_get` supplies the previous turn's already-verified replay
    and chain tail when the file is provably untouched since this process wrote
    it, so the per-turn cost stops scaling with session depth (Stage-D D1). On
    any miss — first turn of a process, a concurrent writer, a rewritten file —
    it falls through to the full `_scan`, which is also what every read path
    does unconditionally."""
    if not enabled():
        return 0
    sid = _sid(conversation_id)
    path = _journal_path(sid)
    try:
        with _locked(sid):
            entry = _cache_get(sid, path)
            if entry is not None:
                current, tail = entry["history"], (entry["seq"],
                                                   entry["tail_sha"])
            else:
                records, status = _scan(sid)
                if status in ("quarantined", "oversize"):
                    raise OSError(f"journal for {sid} is {status}; "
                                  "compact() or delete_session()")
                current = _replay(records)
                tail = ((records[-1]["seq"], records[-1]["sha"])
                        if records else (0, ""))
            history = list(history)
            if current == history:
                # Still re-arm the cache: a miss caused by another process's
                # append must not force a rescan on every subsequent turn.
                _cache_put(sid, path, history=history, seq=tail[0],
                           sha=tail[1])
                return 0
            if len(history) > len(current) and \
                    history[:len(current)] == current:
                entries = [("turn", {"messages": history[len(current):]})]
            else:
                entries = [("reset", {})]
                if history:
                    entries.append(("turn", {"messages": history}))
            seq, sha = _append_records(sid, entries, tail)
            _cache_put(sid, path, history=history, seq=seq, sha=sha)
            return seq
    except Exception as err:
        from . import errors
        errors.capture("sessionlog.sync", err, context=sid)
        _cache_drop(sid)        # never extend from a tail we failed to write
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
        _cache_drop(sid)                # the rewrite invalidates any tail state
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


#: Compact a journal once it passes this fraction of the hard ceiling
#: (`OLYMPUS_SESSION_JOURNAL_MAX_MB`, default 64 MB → 16 MB here). Deliberately
#: not AT the ceiling: at the ceiling appends and `recover_history` have already
#: stopped for that conversation (I-J10), so a threshold there would only ever
#: run after the damage. A quarter leaves 3x headroom for a day's growth between
#: maintenance ticks.
COMPACT_AT_FRACTION = 0.25


def safe_compact_bound(records) -> int:
    """The largest `through_seq` whose removal provably does not change the
    replayed history. 0 when nothing can be dropped.

    THE INVARIANT. `compact()` physically deletes records at or below
    `through_seq`, and `recover_history` rebuilds history from the JOURNAL
    ALONE — it never reads the snapshot. So a bound chosen from "what the
    snapshot covers" would silently destroy the crash-recovery data this
    subsystem exists to provide, and destroy it invisibly, because nothing
    reads it until the next crash.

    The bound is therefore derived from `_replay` itself, which is the only
    thing that defines what a record contributes:

      * `reset` CLEARS the accumulated history;
      * `turn` EXTENDS it;
      * tombstoned turns are skipped, marks are boundaries.

    Everything strictly before the most recent `reset` is therefore dead by
    construction — `_replay` discards it regardless. That prefix, and only that
    prefix, is safe to delete. `sync()` writes exactly such a `reset` + full
    -history pair whenever the history is rewritten rather than extended
    (`/clear`, ABC/in-run compaction), which is precisely what happens on the
    long conversations whose journals actually approach the ceiling.

    The bound is then VERIFIED by replay before it is returned, so safety rests
    on a comparison rather than on the reasoning above being right."""
    if not records:
        return 0
    resets = [r["seq"] for r in records if r.get("kind") == "reset"]
    if not resets:
        return 0                      # append-only journal: nothing is dead
    bound = max(resets) - 1
    if bound < 1:
        return 0
    kept = [r for r in records if r["seq"] > bound]
    if _replay(kept) != _replay(records):
        return 0                      # verification failed — never compact
    return bound


def compact_due(*, threshold_bytes: int | None = None) -> dict:
    """Compact every journal past the size threshold, dropping only records
    that `safe_compact_bound` proves are dead.

    The scheduled counterpart to `compact()`, which takes an explicit
    `through_seq` and so cannot be called by a scheduler on its own. Never
    raises: this runs inside the heartbeat's maintenance job, where one bad
    journal must not stop the sweep for the rest.

    A conversation that is over the threshold but has nothing safe to drop is
    reported in `skipped`, not silently ignored — a journal that keeps growing
    with no compactable prefix is an operator-visible condition."""
    out = {"scanned": 0, "compacted": [], "skipped": [], "bytes_before": 0,
           "bytes_after": 0}
    limit = _max_bytes() if threshold_bytes is None else int(threshold_bytes)
    if threshold_bytes is None:
        limit = int(limit * COMPACT_AT_FRACTION)
    d = config.MEMORY_DIR / "sessions"
    if not d.exists():
        return out
    for path in sorted(d.glob("*.journal.jsonl")):
        sid = path.name[: -len(".journal.jsonl")]
        try:
            size = path.stat().st_size
        except OSError:
            continue
        out["scanned"] += 1
        if size < limit:
            continue
        try:
            with _locked(sid):
                records, status = _scan(sid)
            if status == "absent" or not records:
                continue
            bound = safe_compact_bound(records)
            if bound <= 0:
                out["skipped"].append({"sid": sid, "bytes": size,
                                       "reason": "no dead prefix to drop"})
                continue
            compact(sid, bound)
            out["bytes_before"] += size
            out["bytes_after"] += path.stat().st_size
            out["compacted"].append({"sid": sid, "through_seq": bound})
        except Exception as err:            # noqa: BLE001 — never stop the sweep
            from . import errors
            errors.capture("sessionlog.compact_due", err, context=sid)
            out["skipped"].append({"sid": sid, "bytes": size,
                                   "reason": f"error: {type(err).__name__}"})
    return out


def delete_session(conversation_id: str) -> None:
    """Hard-delete a session's journal, quarantine copies, and snapshot."""
    sid = _sid(conversation_id)
    with _locked(sid):
        _cache_drop(sid)
        paths = [_journal_path(sid),
                 config.MEMORY_DIR / "conversations" / f"{sid}.json"]
        paths += list(_quarantine_dir().glob(f"{sid}.*.journal"))
        for p in paths:
            try:
                p.unlink()
            except OSError:
                pass

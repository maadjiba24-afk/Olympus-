"""Independent ADVERSARIAL audit of Wave-1 C1 (sealed session journal).

These probes deliberately attack angles the author's own suites
(test_sessionlog.py / test_sessionlog_faults.py) do NOT cover:

  1. fsync/flush policy actually matches OLYMPUS_SESSION_FSYNC (counted, not asserted-by-doc)
  2. a self-sealed record with a forged `prev` is caught at the exact chain boundary
  3. compaction interrupted at additional points; seq continuity through snapshot_mark
  4. seal is SHA-256 integrity ONLY (no HMAC / signing) — accepted-debt, layerable later
  5. version edge cases beyond the plain 1.9/2.0 already covered
  6. remove the proclock serialization and PROVE the corruption it prevents
  7. tombstone a MIDDLE range; physical erasure only lands on compaction THROUGH it
  8. hostile conversation_ids cannot escape the sessions dir

The auditor REPORTS; it never patches. No edits to olympus/sessionlog.py or
olympus/memory.py are made by this file.
"""

import contextlib
import hashlib
import json
import threading
import time
from pathlib import Path

import pytest

from olympus import config, memory, sessionlog

MSG = {"role": "user", "content": "hello world"}


def _path(cid="s1"):
    return (config.MEMORY_DIR / "sessions"
            / f"{memory.safe_id(cid)}.journal.jsonl")


def _lines(cid="s1"):
    return _path(cid).read_bytes().decode("utf-8").splitlines()


def _rewrite(lines, cid="s1"):
    _path(cid).write_bytes(("\n".join(lines) + "\n").encode("utf-8"))


def _seed(n=3, cid="s1"):
    for i in range(n):
        assert sessionlog.append_turn(
            cid, [{"role": "user", "content": f"msg-{i}"}]) == i + 1


def _quarantined(cid="s1"):
    d = config.MEMORY_DIR / "sessions" / "quarantine"
    return sorted(d.glob(f"{memory.safe_id(cid)}.*.journal")) if d.exists() else []


def _errors_text() -> str:
    p = config.MEMORY_DIR / "errors" / "errors.jsonl"
    return p.read_text(encoding="utf-8") if p.exists() else ""


# ===========================================================================
# CLAIM 1 — DURABILITY: fsync/flush policy matches OLYMPUS_SESSION_FSYNC
# ===========================================================================

class _CountingFile:
    """Wraps the real journal file object, counting write()/flush() while
    delegating fileno() so the module's os.fsync still targets real bytes."""

    def __init__(self, real):
        self._real = real
        self.writes = 0
        self.flushes = 0

    def write(self, b):
        self.writes += 1
        return self._real.write(b)

    def flush(self):
        self.flushes += 1
        return self._real.flush()

    def fileno(self):
        return self._real.fileno()

    def close(self):
        return self._real.close()


def _install_counters(monkeypatch):
    """Return (counter_holder, fsync_calls) after wiring seams so a single
    append can be inspected for its flush + fsync behaviour."""
    holder = {}
    real_open = sessionlog._open_append

    def wrapped_open(path):
        cf = _CountingFile(real_open(path))
        holder["cf"] = cf
        return cf

    fsyncs = {"n": 0}
    real_fsync = sessionlog.os.fsync

    def counting_fsync(fd):
        fsyncs["n"] += 1
        return real_fsync(fd)

    monkeypatch.setattr(sessionlog, "_open_append", wrapped_open)
    monkeypatch.setattr(sessionlog.os, "fsync", counting_fsync)
    return holder, fsyncs


def test_1_fsync_always_syncs_every_record(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SESSION_FSYNC", "always")
    holder, fsyncs = _install_counters(monkeypatch)
    # three records in ONE append call so the policy is observable
    entries = [("turn", {"messages": [MSG]}) for _ in range(3)]
    with sessionlog._locked("s1"):
        sessionlog._append_records("s1", entries)
    cf = holder["cf"]
    assert cf.writes == 3          # one write() per record (I-J5)
    assert cf.flushes == 3         # flush() after EVERY append (I-J8)
    assert fsyncs["n"] == 3        # fsync=always ⇒ one fsync per record


def test_1_fsync_auto_syncs_once_at_turn_end(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SESSION_FSYNC", "auto")
    holder, fsyncs = _install_counters(monkeypatch)
    entries = [("turn", {"messages": [MSG]}) for _ in range(3)]
    with sessionlog._locked("s1"):
        sessionlog._append_records("s1", entries)
    cf = holder["cf"]
    assert cf.writes == 3
    assert cf.flushes == 3         # flush is STILL per-append under auto
    assert fsyncs["n"] == 1        # auto ⇒ exactly one fsync at close/turn-end


def test_1_committed_record_survives_reopen_as_durable_bytes(monkeypatch):
    """Simulated crash: with fsync=always the append returns only after the
    bytes are on disk. A brand-new reader (no shared in-memory state) must
    recover the last record and it must still self-verify."""
    monkeypatch.setenv("OLYMPUS_SESSION_FSYNC", "always")
    sessionlog.append_turn("crash", [{"role": "user", "content": "before crash"}])
    seq2 = sessionlog.append_turn(
        "crash", [{"role": "assistant", "content": "durable payload"}])
    assert seq2 == 2
    # Re-read straight from disk bytes, independent of any module state.
    raw = _path("crash").read_bytes()
    last = json.loads(raw.decode("utf-8").splitlines()[-1])
    assert last["seq"] == 2
    assert last["payload"]["messages"][0]["content"] == "durable payload"
    assert last["sha"] == sessionlog._seal(last)     # seal intact on disk
    # And the public recovery path reconstructs it after "restart".
    assert sessionlog.recover_history("crash")[-1]["content"] == "durable payload"


# ===========================================================================
# CLAIM 2 — HASH-CHAIN: a self-sealed record with a forged prev is caught
#           at the EXACT boundary (isolates the prev-link check from the seal)
# ===========================================================================

def test_2_selfsealed_record_with_broken_prev_quarantines_at_boundary():
    _seed(3)
    lines = _lines()
    rec = json.loads(lines[1])                 # seq 2
    assert rec["seq"] == 2
    rec["prev"] = "f" * 64                      # break the chain link only
    rec["sha"] = sessionlog._seal(rec)          # ...but re-seal so sha is VALID
    # sanity: the forged record verifies its OWN seal — only the link is wrong
    assert rec["sha"] == sessionlog._seal(rec)
    lines[1] = sessionlog._canonical(rec)
    _rewrite(lines)
    records, status = sessionlog.read_verified("s1")
    assert status == "quarantined"
    assert [r["seq"] for r in records] == [1]   # stops EXACTLY at the break
    assert _quarantined()
    assert "prev-link mismatch" in _errors_text() or "sessionlog" in _errors_text()
    # recovery never reads past the boundary
    assert sessionlog.recover_history("s1") == [
        {"role": "user", "content": "msg-0"}]


def test_2_selfsealed_record_with_broken_prev_but_wrong_seq_still_caught():
    """Belt-and-braces: change prev AND leave seq correct — the seq check
    passes, so the prev-link check must be the one that fires."""
    _seed(2)
    lines = _lines()
    rec = json.loads(lines[1])                 # seq 2, correct seq
    rec["prev"] = json.loads(lines[0])["prev"]  # point at "" (seq-1's prev)
    rec["sha"] = sessionlog._seal(rec)
    lines[1] = sessionlog._canonical(rec)
    _rewrite(lines)
    records, status = sessionlog.read_verified("s1")
    assert status == "quarantined"
    assert [r["seq"] for r in records] == [1]


def test_2_LIMITATION_committed_suffix_truncation_is_not_detected():
    """Documented residual (spec C1.8/C1.15): the hash chain makes INTERNAL
    splices detectable, but truncating a committed suffix (rollback) yields a
    shorter, fully-valid journal — NOT flagged. Recording it as a limitation,
    not a failure."""
    _seed(3)
    lines = _lines()
    _rewrite(lines[:2])                          # drop the committed seq-3 record
    records, status = sessionlog.read_verified("s1")
    assert status == "ok"                        # looks pristine — the residual
    assert [r["seq"] for r in records] == [1, 2]


# ===========================================================================
# CLAIM 3 — COMPACTION SAFETY: extra interruption points; seq/chain
#           continuity across the snapshot_mark boundary
# ===========================================================================

def test_3_interrupt_at_fsync_before_replace_leaves_original_intact(monkeypatch):
    """A NEW interruption point vs the existing os.replace-raises test: die at
    the fsync AFTER the tmp bytes are written but BEFORE the swap."""
    _seed(4)
    before = _path().read_bytes()
    real_fsync = sessionlog.os.fsync

    def boom(fd):
        raise OSError("killed at fsync, tmp written, not yet replaced")

    monkeypatch.setattr(sessionlog.os, "fsync", boom)
    with pytest.raises(OSError):
        sessionlog.compact("s1", 2)
    monkeypatch.setattr(sessionlog.os, "fsync", real_fsync)
    assert _path().read_bytes() == before                  # no data loss
    assert not list(_path().parent.glob(".*.tmp"))         # tmp cleaned up
    assert sessionlog.read_verified("s1")[1] == "ok"


def test_3_stale_tmp_from_crashed_compaction_is_ignored_by_reads():
    """An interrupted compaction can leave a `.<name>.<pid>.tmp` sibling. A
    reader must ignore it entirely (globs only the real journal) and a fresh
    compaction must still succeed."""
    _seed(3)
    good = _path().read_bytes()
    stale = _path().with_name(f".{_path().name}.999999.tmp")
    stale.write_bytes(b"garbage not json\n{broken\n")
    records, status = sessionlog.read_verified("s1")
    assert status == "ok"
    assert [r["seq"] for r in records] == [1, 2, 3]        # tmp not read
    assert _path().read_bytes() == good
    sessionlog.compact("s1", 1)                            # still works
    assert sessionlog.read_verified("s1")[1] == "ok"


def test_3_seq_and_chain_stay_continuous_across_snapshot_mark():
    _seed(5)
    pre_tail_sha = sessionlog.read_verified("s1")[0][-1]["sha"]
    sessionlog.compact("s1", 2)
    records, status = sessionlog.read_verified("s1")
    assert status == "ok"
    # surviving suffix + mark: 3,4,5 then mark=6
    assert [r["seq"] for r in records] == [3, 4, 5, 6]
    mark = records[-1]
    assert mark["kind"] == "snapshot_mark"
    assert mark["prev"] == records[-2]["sha"]              # mark links the tail
    assert records[-2]["sha"] == pre_tail_sha             # tail unchanged by compact
    # full chain re-walk across the compaction boundary
    prev = records[0]["prev"]
    for r in records:
        assert r["prev"] == prev
        assert r["sha"] == sessionlog._seal(r)
        prev = r["sha"]
    # appends AFTER the mark keep the seq dense and the chain intact
    assert sessionlog.append_turn("s1", [MSG]) == 7
    recs2, st2 = sessionlog.read_verified("s1")
    assert st2 == "ok"
    assert [r["seq"] for r in recs2] == [3, 4, 5, 6, 7]
    assert recs2[-1]["prev"] == mark["sha"]               # new turn chains onto mark


# ===========================================================================
# CLAIM 4 — KEY ROTATION / SIGNING: ACCEPTED-DEBT. SHA-256 integrity only,
#           no HMAC/signature; witness.sign_log layerable as a sidecar.
# ===========================================================================

def test_4_records_carry_sha256_only_no_keyed_mac_or_signature():
    sessionlog.append_turn("s1", [MSG])
    rec = sessionlog.read_verified("s1")[0][0]
    # closed schema: exactly the 8 spec fields, none of them a signature/MAC
    assert set(rec) == {"v", "seq", "ts", "kind", "conversation_id",
                        "payload", "prev", "sha"}
    # the seal is a bare SHA-256 over the canonical record — reproducible with
    # no key material whatsoever
    canon = sessionlog._canonical(dict(rec, sha=""))
    assert rec["sha"] == hashlib.sha256(canon.encode("utf-8")).hexdigest()
    # the module imports/uses no signing or keyed-hash primitive
    src = Path(sessionlog.__file__).read_text(encoding="utf-8").lower()
    for token in ("hmac", "witness", "ed25519", "fernet", "signing_key",
                  "private_key"):
        assert token not in src, f"unexpected crypto token in sessionlog: {token}"


@pytest.mark.requires_crypto
def test_4_witness_sign_log_layers_over_records_without_format_change():
    """Confirms the Wave-2 path exists and is DETACHED: witness.sign_log
    produces a sidecar signature over a list of records and does not mutate
    them or require a new in-record field — so signing could be layered later
    without any journal-format change."""
    from olympus import witness
    sessionlog.append_turn("s1", [MSG])
    sessionlog.append_turn("s1", [MSG])
    records = sessionlog.read_verified("s1")[0]
    snapshot = json.dumps(records, sort_keys=True)
    sig = witness.sign_log(records)
    assert {"publicKey", "signature"} <= set(sig)          # detached artifact
    assert json.dumps(records, sort_keys=True) == snapshot  # records untouched
    # the signature lives outside the record; the on-disk schema is unchanged
    assert "signature" not in records[0]


# ===========================================================================
# CLAIM 5 — SCHEMA MIGRATION: version edge cases beyond plain 1.9 / 2.0
# ===========================================================================

def _append_forged(cid, seq, version, prev_sha):
    rec = {"v": version, "seq": seq, "ts": 0.0, "kind": "turn",
           "conversation_id": memory.safe_id(cid),
           "payload": {"messages": [MSG]}, "prev": prev_sha, "sha": ""}
    rec["sha"] = sessionlog._seal(rec)
    with _path(cid).open("a", encoding="utf-8") as f:
        f.write(sessionlog._canonical(rec) + "\n")
    return rec


@pytest.mark.parametrize("version", ["1", "1.0", "1.9", "1.99", "1.0.3"])
def test_5_known_major_variants_are_tolerated(version):
    _seed(1)
    tail = sessionlog.read_verified("s1")[0][-1]
    _append_forged("s1", 2, version, tail["sha"])
    records, status = sessionlog.read_verified("s1")
    assert status == "ok", version
    assert [r["seq"] for r in records] == [1, 2], version


@pytest.mark.parametrize("version", ["2.0", "0.9", "10.0", "x", ""])
def test_5_unknown_major_variants_are_refused(version):
    _seed(1)
    tail = sessionlog.read_verified("s1")[0][-1]
    _append_forged("s1", 2, version, tail["sha"])
    records, status = sessionlog.read_verified("s1")
    assert status == "quarantined", version
    assert [r["seq"] for r in records] == [1], version     # stop at the bad record


def test_5_multidigit_major_10_is_not_confused_with_1():
    """`str(v).partition('.')[0]` must treat '10.0' as major 10 (unknown),
    NOT a prefix-match of '1' — a classic version-parsing trap."""
    _seed(1)
    tail = sessionlog.read_verified("s1")[0][-1]
    _append_forged("s1", 2, "10.0", tail["sha"])
    assert sessionlog.read_verified("s1")[1] == "quarantined"


# ===========================================================================
# CLAIM 6 — CONCURRENCY: PROVE the corruption the proclock prevents
# ===========================================================================

def test_6_stale_scan_without_serialization_duplicates_seq():
    """Deterministic model of two lock-free writers that scanned the same
    state then each appended: both compute the same next seq ⇒ duplicate ⇒
    the journal quarantines and reads stop at the dup boundary. This is
    exactly the outcome proclock exists to prevent."""
    sessionlog.append_turn("s1", [MSG])                    # seq 1
    stale, _ = sessionlog._scan("s1")                      # both writers hold this
    tail = (stale[-1]["seq"], stale[-1]["sha"])
    with sessionlog._locked("s1"):
        sessionlog._append_records("s1", [("turn", {"messages": [MSG]})],
                                   tail)                    # writer A ⇒ seq 2
        sessionlog._append_records("s1", [("turn", {"messages": [MSG]})],
                                   tail)                    # writer B ⇒ seq 2 (dup!)
    # raw file really does contain a duplicate seq
    seqs = [json.loads(l)["seq"] for l in _lines()]
    assert seqs == [1, 2, 2]
    records, status = sessionlog.read_verified("s1")
    assert status == "quarantined"
    assert [r["seq"] for r in records] == [1, 2]           # stops at the dup


def test_6_threaded_appends_without_lock_corrupt_the_journal(monkeypatch):
    """Remove the serialization (proclock -> no-op) and widen the scan->write
    window; concurrent appenders then produce duplicate seqs / gaps. Contrast
    with test_sessionlog_faults.test_concurrent_appends_*_stay_dense, which
    keeps the lock and stays perfectly dense."""
    monkeypatch.setattr(sessionlog, "_locked",
                        lambda sid: contextlib.nullcontext())
    real_open = sessionlog._open_append

    def slow_open(path):
        f = real_open(path)
        time.sleep(0.003)                                  # widen scan->write race
        return f

    monkeypatch.setattr(sessionlog, "_open_append", slow_open)

    def worker():
        for _ in range(10):
            sessionlog.append_turn("s1", [MSG])            # returns 0 on capture

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    seqs = [json.loads(l)["seq"] for l in _lines() if l.strip()]
    dense_unique = (seqs == list(range(1, len(seqs) + 1)))
    status = sessionlog.read_verified("s1")[1]
    print(f"\n[claim6] no-lock seqs={seqs} status={status}")
    # Without the lock the journal is NOT a clean dense 1..N 'ok' journal.
    assert not (dense_unique and status == "ok"), (
        "expected lock-free concurrency to corrupt the journal, but it "
        f"stayed clean: seqs={seqs}")


# ===========================================================================
# CLAIM 7 — RETENTION / TOMBSTONE: erasure of a MIDDLE range; physical
#           deletion lands only on compaction THROUGH the tombstoned seq
# ===========================================================================

def test_7_tombstone_middle_range_erased_only_after_compaction_through_it():
    secret = "MIDDLE-SECRET-XYZ-9876"
    sessionlog.append_turn("s1", [{"role": "user", "content": "keep-first"}])   # 1
    sessionlog.append_turn("s1", [{"role": "user", "content": secret}])         # 2
    sessionlog.append_turn("s1", [{"role": "user", "content": "keep-last"}])    # 3
    sessionlog.append_tombstone("s1", 2, 2)                                     # 4

    # logical deletion is immediate; bytes are still physically present
    assert sessionlog.recover_history("s1") == [
        {"role": "user", "content": "keep-first"},
        {"role": "user", "content": "keep-last"}]
    assert secret.encode() in _path().read_bytes()

    # compacting SHORT of the tombstoned seq must NOT erase the bytes yet
    sessionlog.compact("s1", 1)
    assert secret.encode() in _path().read_bytes(), \
        "compaction that did not reach the tombstoned seq erased data early"

    # compaction THROUGH the tombstoned seq physically removes the payload
    sessionlog.compact("s1", 2)
    raw = _path().read_bytes()
    assert secret.encode() not in raw                       # GDPR-style erasure
    assert b"keep-first" not in raw                         # compacted prefix gone
    assert b"keep-last" in raw                              # survivor retained
    assert sessionlog.read_verified("s1")[1] == "ok"        # chain still valid
    assert sessionlog.recover_history("s1") == [
        {"role": "user", "content": "keep-last"}]


def test_7_no_quarantine_copy_retains_the_erased_secret():
    """Defence in depth: erasure must not survive in a stray quarantine copy."""
    secret = "NO-LINGER-SECRET-4242"
    sessionlog.append_turn("s1", [{"role": "user", "content": secret}])
    sessionlog.append_turn("s1", [{"role": "user", "content": "keep"}])
    sessionlog.append_tombstone("s1", 1, 1)
    sessionlog.compact("s1", 1)
    for q in _quarantined():
        assert secret.encode() not in q.read_bytes()
    assert secret.encode() not in _path().read_bytes()


# ===========================================================================
# CLAIM 8 — CROSS-SESSION ISOLATION: hostile ids cannot escape sessions dir
# ===========================================================================

@pytest.mark.parametrize("hostile", [
    "../../etc/passwd",
    "..\\..\\windows\\system32",
    "/etc/shadow",
    "....//....//secret",
    "a/../../b",
    "con:aux:nul",
    "  ../../.. ",
    "\x00../evil",
    "..%2f..%2fetc",
])
def test_8_hostile_conversation_ids_stay_inside_sessions_dir(hostile):
    sessions = (config.MEMORY_DIR / "sessions").resolve()
    sessionlog.append_turn(hostile, [MSG])
    sid = memory.safe_id(hostile)
    jp = _path(hostile).resolve()
    # the derived path is a direct child of sessions/, nothing escapes
    assert jp.parent == sessions, hostile
    assert jp.exists(), hostile
    assert "/" not in sid and "\\" not in sid and ".." not in sid, hostile
    assert config.MEMORY_DIR.resolve() in jp.parents, hostile


def test_8_no_files_are_created_outside_memory_dir_by_hostile_ids(tmp_path):
    """After hammering hostile ids, no journal/file leaked above MEMORY_DIR."""
    mem = config.MEMORY_DIR.resolve()
    for hostile in ("../../../oops", "/tmp/olympus-escape-test", "../sibling"):
        sessionlog.append_turn(hostile, [MSG])
    # every *.journal.jsonl that now exists is under MEMORY_DIR/sessions
    for jp in mem.rglob("*.journal.jsonl"):
        assert (mem / "sessions") in jp.parents or jp.parent == mem / "sessions"
    # the traversal targets outside MEMORY_DIR were never created
    assert not (mem.parent / "oops").exists()
    assert not Path("/tmp/olympus-escape-test").exists()
    assert not (mem.parent / "sibling").exists()


def test_8_distinct_hostile_ids_that_collapse_alike_share_one_journal():
    """LIMITATION note: safe_id is intentionally lossy — two different hostile
    strings can map to the same sid and therefore the same journal. Isolation
    holds (still inside sessions/), but callers must treat safe_id as a
    sanitiser, not a unique key."""
    a, b = "../../etc", "..\\..\\etc"
    assert memory.safe_id(a) == memory.safe_id(b) == "etc"
    sessionlog.append_turn(a, [{"role": "user", "content": "A"}])
    sessionlog.append_turn(b, [{"role": "user", "content": "B"}])
    # both writes landed in the SAME journal (documented lossiness)
    hist = sessionlog.recover_history(a)
    assert hist == [{"role": "user", "content": "A"},
                    {"role": "user", "content": "B"}]

"""C1 fault-injection suite (spec C1.12): every corruption class is either a
torn tail (truncated back to the verified prefix — the one permitted
mutation) or a quarantine-and-stop — never a silent read past the damage."""

import errno
import json
import threading

import pytest

from olympus import config, memory, sessionlog

MSG = {"role": "user", "content": "hello world"}


def _path(cid="s1"):
    return (config.MEMORY_DIR / "sessions"
            / f"{memory.safe_id(cid)}.journal.jsonl")


def _seed(n=3, cid="s1"):
    for i in range(n):
        assert sessionlog.append_turn(
            cid, [{"role": "user", "content": f"msg-{i}"}]) == i + 1
    return _path(cid).read_bytes()


def _lines(data):
    return data.decode("utf-8").splitlines()


def _rewrite(lines, cid="s1"):
    _path(cid).write_bytes(("\n".join(lines) + "\n").encode("utf-8"))


def _errors_logged() -> str:
    p = config.MEMORY_DIR / "errors" / "errors.jsonl"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _quarantined(cid="s1"):
    d = config.MEMORY_DIR / "sessions" / "quarantine"
    return sorted(d.glob(f"{memory.safe_id(cid)}.*.journal")) if d.exists() else []


# --- torn tail -------------------------------------------------------------

def test_torn_tail_at_every_byte_offset_of_final_record():
    data = _seed(3)
    last_start = data.rstrip(b"\n").rfind(b"\n") + 1
    for cut in range(last_start + 1, len(data)):
        _path().write_bytes(data[:cut])
        records, status = sessionlog.read_verified("s1")
        assert status == "torn_tail_truncated", cut
        assert [r["seq"] for r in records] == [1, 2], cut   # exactly one lost
        assert _path().read_bytes() == data[:last_start], cut
        assert sessionlog.journal_status("s1") == "ok", cut
    # a cut exactly at the record boundary is simply a clean shorter journal
    _path().write_bytes(data[:last_start])
    assert sessionlog.read_verified("s1")[1] == "ok"


def test_partial_write_of_next_record_is_torn():
    data = _seed(2)
    _path().write_bytes(data + b'{"v":"1.0","seq":3,"kind":"tu')
    records, status = sessionlog.read_verified("s1")
    assert status == "torn_tail_truncated"
    assert [r["seq"] for r in records] == [1, 2]
    # the torn write is gone; the journal appends cleanly again
    assert sessionlog.append_turn("s1", [MSG]) == 3


# --- mid-file corruption ⇒ quarantine, stop at the boundary -----------------

def test_midfile_payload_mutation_quarantines():
    data = _seed(3)
    lines = _lines(data)
    rec = json.loads(lines[1])
    rec["payload"]["messages"][0]["content"] = "TAMPERED"
    lines[1] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    _rewrite(lines)
    records, status = sessionlog.read_verified("s1")
    assert status == "quarantined"
    assert [r["seq"] for r in records] == [1]           # never reads past it
    copies = _quarantined()
    assert copies and copies[0].read_bytes() == _path().read_bytes()
    assert "sessionlog" in _errors_logged()
    assert sessionlog.recover_history("s1") == [
        {"role": "user", "content": "msg-0"}]


def test_midfile_sha_mutation_quarantines():
    lines = _lines(_seed(3))
    rec = json.loads(lines[1])
    rec["sha"] = "0" * 64
    lines[1] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    _rewrite(lines)
    records, status = sessionlog.read_verified("s1")
    assert status == "quarantined"
    assert [r["seq"] for r in records] == [1]
    assert _quarantined()


def test_reordered_records_refused_at_boundary():
    lines = _lines(_seed(3))
    lines[1], lines[2] = lines[2], lines[1]
    _rewrite(lines)
    records, status = sessionlog.read_verified("s1")
    assert status == "quarantined"
    assert [r["seq"] for r in records] == [1]


def test_duplicated_seq_refused():
    lines = _lines(_seed(3))
    lines.insert(2, lines[1])
    _rewrite(lines)
    records, status = sessionlog.read_verified("s1")
    assert status == "quarantined"
    assert [r["seq"] for r in records] == [1, 2]


def test_unknown_major_version_refused_at_that_record():
    _seed(2)
    tail = sessionlog.read_verified("s1")[0][-1]
    rec = {"v": "2.0", "seq": 3, "ts": 0.0, "kind": "turn",
           "conversation_id": "s1", "payload": {"messages": [MSG]},
           "prev": tail["sha"], "sha": ""}
    rec["sha"] = sessionlog._seal(rec)          # self-consistent, wrong major
    with _path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n")
    records, status = sessionlog.read_verified("s1")
    assert status == "quarantined"
    assert [r["seq"] for r in records] == [1, 2]


def test_unknown_minor_version_reads_fine():
    _seed(1)
    tail = sessionlog.read_verified("s1")[0][-1]
    rec = {"v": "1.9", "seq": 2, "ts": 0.0, "kind": "turn",
           "conversation_id": "s1", "payload": {"messages": [MSG]},
           "prev": tail["sha"], "sha": ""}
    rec["sha"] = sessionlog._seal(rec)
    with _path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n")
    records, status = sessionlog.read_verified("s1")
    assert status == "ok" and len(records) == 2


def test_quarantined_journal_refuses_appends_until_compacted():
    lines = _lines(_seed(3))
    rec = json.loads(lines[1])
    rec["sha"] = "f" * 64
    lines[1] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    _rewrite(lines)
    assert sessionlog.append_turn("s1", [MSG]) == 0     # captured, not raised
    sessionlog.compact("s1", 0)                         # sanctioned repair
    assert sessionlog.read_verified("s1")[1] == "ok"
    assert sessionlog.append_turn("s1", [MSG]) > 0


# --- concurrency -------------------------------------------------------------

def test_concurrent_appends_from_two_threads_stay_dense():
    def worker():
        for _ in range(20):
            assert sessionlog.append_turn("s1", [MSG]) > 0
    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    records, status = sessionlog.read_verified("s1")
    assert status == "ok"
    assert [r["seq"] for r in records] == list(range(1, 41))


# --- I/O failures never reach the caller ------------------------------------

class _FullDisk:
    def write(self, b):
        raise OSError(errno.ENOSPC, "No space left on device")

    def flush(self):
        pass

    def close(self):
        pass


def test_disk_full_append_never_raises(monkeypatch):
    _seed(1)
    monkeypatch.setattr(sessionlog, "_open_append", lambda path: _FullDisk())
    assert sessionlog.append_turn("s1", [MSG]) == 0
    assert "No space left" in _errors_logged()
    # the reply path is unaffected: the snapshot still saves and loads
    memory.save_conversation("s1", [MSG])
    assert memory.load_conversation("s1") == [MSG]
    # journal untouched beyond the verified prefix
    assert sessionlog.read_verified("s1")[1] == "ok"


def test_permission_failure_never_raises(monkeypatch):
    # the container runs as root, so chmod 0 is a no-op — inject EACCES instead
    def denied(path):
        raise PermissionError(errno.EACCES, "Permission denied")
    monkeypatch.setattr(sessionlog, "_open_append", denied)
    assert sessionlog.append_turn("s1", [MSG]) == 0
    memory.save_conversation("s1", [MSG])
    assert memory.load_conversation("s1") == [MSG]
    assert "Permission denied" in _errors_logged()


# --- compaction & deletion ---------------------------------------------------

def test_compaction_interruption_leaves_original_intact(monkeypatch):
    data = _seed(4)

    def boom(src, dst):
        raise OSError("killed between tmp write and replace")
    with monkeypatch.context() as m:
        m.setattr(sessionlog.os, "replace", boom)
        with pytest.raises(OSError):
            sessionlog.compact("s1", 2)
        assert _path().read_bytes() == data             # old journal intact
        assert not list(_path().parent.glob(".*.tmp"))  # tmp cleaned up
    assert sessionlog.read_verified("s1")[1] == "ok"


def test_tombstone_then_compact_removes_payload_bytes():
    secret = "SECRET-TO-FORGET-1234"
    sessionlog.append_turn("s1", [{"role": "user", "content": secret}])
    sessionlog.append_turn("s1", [{"role": "user", "content": "keep me"}])
    sessionlog.append_tombstone("s1", 1, 1)
    # logical deletion is immediate ...
    assert sessionlog.recover_history("s1") == [
        {"role": "user", "content": "keep me"}]
    assert secret.encode() in _path().read_bytes()
    # ... physical deletion lands with compaction through the tombstoned seq
    sessionlog.compact("s1", 1)
    assert secret.encode() not in _path().read_bytes()
    assert sessionlog.read_verified("s1")[1] == "ok"
    assert sessionlog.recover_history("s1") == [
        {"role": "user", "content": "keep me"}]


# --- size bound ---------------------------------------------------------------

def test_oversize_journal_refuses_recovery(monkeypatch):
    _seed(3)
    monkeypatch.setenv("OLYMPUS_SESSION_JOURNAL_MAX_MB", "0.0001")  # ~100 B
    assert sessionlog.journal_status("s1") == "oversize"
    assert sessionlog.recover_history("s1") is None
    assert sessionlog.append_turn("s1", [MSG]) == 0     # refuses to grow it
    monkeypatch.delenv("OLYMPUS_SESSION_JOURNAL_MAX_MB")
    assert sessionlog.journal_status("s1") == "ok"      # data never touched


# --- the silent-[] bug becomes recovery --------------------------------------

def test_snapshot_corruption_recovers_from_journal():
    h = []
    for i in range(3):
        h = h + [{"role": "user", "content": f"q{i}"},
                 {"role": "assistant", "content": f"a{i}"}]
        memory.save_conversation("web-x", h)
    snap = config.MEMORY_DIR / "conversations" / "web-x.json"
    snap.write_text("{ definitely not json", encoding="utf-8")
    assert memory.load_conversation("web-x") == h
    assert "recovered 6 messages" in _errors_logged()


def test_snapshot_corruption_with_journal_off_stays_legacy(monkeypatch):
    memory.save_conversation("web-x", [MSG])
    snap = config.MEMORY_DIR / "conversations" / "web-x.json"
    snap.write_text("][", encoding="utf-8")
    monkeypatch.setenv("OLYMPUS_SESSION_JOURNAL", "0")
    assert memory.load_conversation("web-x") == []

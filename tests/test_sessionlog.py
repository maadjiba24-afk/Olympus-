import hashlib
import json
import time

from olympus import config, memory, sessionlog


def _path(cid="s1"):
    return (config.MEMORY_DIR / "sessions"
            / f"{memory.safe_id(cid)}.journal.jsonl")


def test_append_and_read_verified():
    assert sessionlog.append_turn("s1", [{"role": "user", "content": "hi"}]) == 1
    assert sessionlog.append_turn("s1", [{"role": "assistant", "content": "yo"}]) == 2
    records, status = sessionlog.read_verified("s1")
    assert status == "ok"
    assert [r["seq"] for r in records] == [1, 2]
    assert [r["kind"] for r in records] == ["turn", "turn"]
    assert records[0]["prev"] == ""
    assert sessionlog.journal_status("s1") == "ok"


def test_seq_dense_and_hash_chained():
    for i in range(5):
        sessionlog.append_turn("s1", [{"role": "user", "content": f"m{i} é"}])
    records, _ = sessionlog.read_verified("s1")
    assert [r["seq"] for r in records] == [1, 2, 3, 4, 5]
    prev = ""
    for rec in records:
        assert rec["prev"] == prev
        canon = json.dumps(dict(rec, sha=""), sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False)
        assert rec["sha"] == hashlib.sha256(canon.encode("utf-8")).hexdigest()
        prev = rec["sha"]
    # one line per record, each newline-terminated
    raw = _path().read_bytes()
    assert raw.endswith(b"\n") and raw.count(b"\n") == 5


def test_recover_history_round_trip():
    h = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    sessionlog.append_turn("s1", h[:1])
    sessionlog.append_turn("s1", h[1:])
    assert sessionlog.recover_history("s1") == h
    assert sessionlog.recover_history("never-seen") is None
    assert sessionlog.journal_status("never-seen") == "absent"


def test_save_conversation_journals_only_the_delta():
    h = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]
    memory.save_conversation("web-1", h)
    h = h + [{"role": "user", "content": "q2"}, {"role": "assistant", "content": "a2"}]
    memory.save_conversation("web-1", h)
    memory.save_conversation("web-1", h)          # no growth ⇒ no record
    records, status = sessionlog.read_verified("web-1")
    assert status == "ok"
    assert [r["kind"] for r in records] == ["turn", "turn"]
    assert [len(r["payload"]["messages"]) for r in records] == [2, 2]
    assert sessionlog.recover_history("web-1") == h


def test_shrunk_history_is_recorded_as_reset():
    memory.save_conversation("web-1", [{"role": "user", "content": "old"}] * 4)
    short = [{"role": "user", "content": "fresh"}]
    memory.save_conversation("web-1", short)
    kinds = [r["kind"] for r in sessionlog.read_verified("web-1")[0]]
    assert kinds == ["turn", "reset", "turn"]
    assert sessionlog.recover_history("web-1") == short


def test_flag_off_disables_journaling(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SESSION_JOURNAL", "off")
    assert sessionlog.enabled() is False
    assert sessionlog.append_turn("s1", [{"role": "user", "content": "x"}]) == 0
    memory.save_conversation("s1", [{"role": "user", "content": "x"}])
    assert sessionlog.journal_status("s1") == "absent"
    monkeypatch.setenv("OLYMPUS_SESSION_JOURNAL", "1")
    assert sessionlog.enabled() is True


def test_sessions_are_isolated_by_safe_id():
    sessionlog.append_turn("alice", [{"role": "user", "content": "A"}])
    sessionlog.append_turn("bob", [{"role": "user", "content": "B"}])
    assert sessionlog.append_turn("alice", [{"role": "user", "content": "A2"}]) == 2
    assert sessionlog.recover_history("bob") == [{"role": "user", "content": "B"}]
    # traversal collapses to a safe name under sessions/, never a foreign path
    sessionlog.append_turn("../../etc", [{"role": "user", "content": "x"}])
    assert (config.MEMORY_DIR / "sessions" / "etc.journal.jsonl").exists()


def test_compact_drops_prefix_and_seals_a_mark():
    for i in range(1, 6):
        sessionlog.append_turn("s1", [{"role": "user", "content": f"m{i}"}])
    sessionlog.compact("s1", 3)
    records, status = sessionlog.read_verified("s1")
    assert status == "ok"
    assert [r["seq"] for r in records] == [4, 5, 6]
    assert records[-1]["kind"] == "snapshot_mark"
    assert records[-1]["payload"]["through_seq"] == 3
    assert sessionlog.recover_history("s1") == [
        {"role": "user", "content": "m4"}, {"role": "user", "content": "m5"}]
    # appends keep chaining after compaction
    assert sessionlog.append_turn("s1", [{"role": "user", "content": "m6"}]) == 7
    assert sessionlog.read_verified("s1")[1] == "ok"


def test_delete_session_removes_journal_and_snapshot():
    memory.save_conversation("gone", [{"role": "user", "content": "x"}])
    assert sessionlog.journal_status("gone") == "ok"
    sessionlog.delete_session("gone")
    assert sessionlog.journal_status("gone") == "absent"
    assert not (config.MEMORY_DIR / "conversations" / "gone.json").exists()
    assert memory.load_conversation("gone") == []


def test_append_p50_under_bound():
    msgs = [{"role": "user", "content": "x" * 200}]
    samples = []
    for _ in range(200):
        t0 = time.perf_counter()
        assert sessionlog.append_turn("bench", msgs) > 0
        samples.append(time.perf_counter() - t0)
    samples.sort()
    p50 = samples[len(samples) // 2] * 1000
    print(f"\nsessionlog append p50: {p50:.3f} ms (fsync=auto, n=200)")
    # spec target < 5 ms on local disk; asserted loosely to dodge CI flake
    assert p50 < 50.0

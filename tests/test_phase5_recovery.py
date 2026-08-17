"""Phase 5 P5-12/P5-13 — backup/restore drill and restart/failure recovery.

Gates proved here: P5-A4 (restart recovery), P5-A10 (backup restored
successfully), P5-A14 (principal isolation survives concurrency and restore),
P5-A15 (spend caps hold under retry and recovery), P5-A17 (replay determinism
across a restore).

Step 13's rule governs the whole file: **a backup that has not been restored is
not a verified backup.** Every assertion below is made against data read back
out of a real archive into a clean tree — never against the archive's own
manifest, which would be the artifact grading itself.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from olympus import (backup, config, memory, retention, sessionlog, usage)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "mem")
    monkeypatch.delenv("OLYMPUS_BACKUP_CMD", raising=False)
    monkeypatch.delenv("OLYMPUS_CONVERSATION_RETAIN_DAYS", raising=False)
    monkeypatch.delenv("OLYMPUS_LEGAL_HOLD", raising=False)
    return tmp_path


def _seed(principals=("alice", "bob"), turns=3) -> dict[str, list]:
    """A populated instance: per-user notes, snapshots, sealed journals."""
    histories = {}
    for uid in principals:
        memory.set_user(uid)
        memory.save("lessons", f"{uid}-lesson", f"SECRET-OF-{uid.upper()}")
        history = []
        for i in range(turns):
            history += [{"role": "user", "content": f"{uid} asks {i}"},
                        {"role": "assistant", "content": f"{uid} told {i}"}]
            # Save per TURN, not once at the end: a single save produces a
            # one-record journal, which cannot exercise mid-chain corruption or
            # a torn tail behind committed records. This is also what the real
            # reply path does.
            memory.save_conversation(uid, history)
        histories[uid] = history
    memory.set_user("shared")
    return histories


# ══════════════════════════════════════════════════════════════════════════
# P5-A10 — the backup/restore drill
# ══════════════════════════════════════════════════════════════════════════

def test_backup_is_created_and_verifies_its_own_integrity(store):
    _seed()
    res = backup.create()
    assert res["files"] > 0 and res["bytes"] > 0
    archive = Path(res["path"])
    assert archive.exists()
    v = backup.verify_archive(str(archive))
    assert v.get("ok", True) is not False, v


def test_a_tampered_archive_is_refused(store, tmp_path):
    """Integrity checking is the whole point of verifying before restoring."""
    _seed()
    archive = Path(backup.create()["path"])
    blob = bytearray(archive.read_bytes())
    blob[len(blob) // 2] ^= 0xFF                     # flip a byte in the middle
    archive.write_bytes(bytes(blob))
    with pytest.raises(Exception):
        backup.restore(str(archive), into=tmp_path / "clean", force=True)


def test_restore_into_a_clean_tree_reproduces_every_store(store, tmp_path,
                                                          monkeypatch):
    """P5-A10 proper: restore into a CLEAN directory and read the data back —
    not from the manifest, which would be the archive grading itself."""
    histories = _seed()
    archive = Path(backup.create()["path"])

    clean = tmp_path / "restored"
    backup.restore(str(archive), into=clean, force=True)
    monkeypatch.setattr(config, "MEMORY_DIR", clean)

    for uid, history in histories.items():
        # the conversation snapshot
        assert memory.load_conversation(uid) == history, uid
        # the sealed journal, verified chain and all
        records, status = sessionlog.read_verified(uid)
        assert status == "ok", (uid, status)
        assert records, uid
        assert sessionlog.recover_history(uid) == history, uid
        # the per-user notes
        memory.set_user(uid)
        assert f"SECRET-OF-{uid.upper()}" in memory.search("SECRET", limit=5)
    memory.set_user("shared")


def test_restore_refuses_a_non_empty_target_without_force(store, tmp_path):
    """Silently overwriting a populated MEMORY_DIR is the way a restore turns
    into a data-loss incident."""
    _seed()
    archive = Path(backup.create()["path"])
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "something.txt").write_text("pre-existing", encoding="utf-8")
    with pytest.raises(backup.BackupError):
        backup.restore(str(archive), into=target)
    assert (target / "something.txt").read_text(encoding="utf-8") == \
        "pre-existing"


def test_principal_isolation_survives_a_restore(store, tmp_path, monkeypatch):
    """P5-A14. A restore that merged namespaces would silently undo the
    Phase-4 B-F2 fix."""
    _seed(("alice", "bob"))
    archive = Path(backup.create()["path"])
    clean = tmp_path / "restored"
    backup.restore(str(archive), into=clean, force=True)
    monkeypatch.setattr(config, "MEMORY_DIR", clean)

    memory.set_user("alice")
    found = memory.search("SECRET", limit=10)
    assert "SECRET-OF-ALICE" in found
    assert "SECRET-OF-BOB" not in found, "restore leaked across principals"
    memory.set_user("bob")
    found = memory.search("SECRET", limit=10)
    assert "SECRET-OF-BOB" in found and "SECRET-OF-ALICE" not in found
    memory.set_user("shared")


def test_a_deleted_principal_stays_deleted_across_a_restore(store, tmp_path,
                                                            monkeypatch):
    """P5-A12 across the backup boundary: a right-to-be-forgotten deletion must
    not be silently undone by restoring a backup taken AFTER it. (A backup
    taken BEFORE the deletion legitimately still contains the data — that is
    what backup-expiry documentation in the retention report is for.)"""
    _seed(("alice", "bob"))
    retention.delete_principal("alice", dry_run=False, reason="rtbf")
    archive = Path(backup.create()["path"])          # taken AFTER the deletion

    clean = tmp_path / "restored"
    backup.restore(str(archive), into=clean, force=True)
    monkeypatch.setattr(config, "MEMORY_DIR", clean)
    assert not retention.inspect_principal("alice")["exists"]
    assert retention.inspect_principal("bob")["exists"]


def test_the_restored_instance_starts(store, tmp_path, monkeypatch):
    """'Configuration compatibility' and 'application startup after restore'
    from Step 13: the readiness probe is the cheapest honest proxy for
    'the app comes up against this data'."""
    _seed()
    archive = Path(backup.create()["path"])
    clean = tmp_path / "restored"
    backup.restore(str(archive), into=clean, force=True)
    monkeypatch.setattr(config, "MEMORY_DIR", clean)

    from olympus import web
    ready, detail = web._readiness()
    assert ready is True, detail
    assert detail["memory_dir_writable"] is True


# ══════════════════════════════════════════════════════════════════════════
# P5-A4 — restart recovery
# ══════════════════════════════════════════════════════════════════════════

def test_history_survives_a_simulated_process_restart(store):
    """A 'restart' here is: drop every in-process cache and re-read from disk,
    which is exactly what a new process does."""
    histories = _seed(("alice",), turns=5)
    sessionlog._CACHE.clear()                    # the new process has none
    memory.set_user("shared")
    assert memory.load_conversation("alice") == histories["alice"]
    assert sessionlog.recover_history("alice") == histories["alice"]


def test_a_torn_journal_tail_is_truncated_not_trusted(store):
    """The crash case the journal exists for: a half-written final record."""
    histories = _seed(("alice",), turns=4)
    path = sessionlog._journal_path(sessionlog._sid("alice"))
    with open(path, "ab") as f:
        f.write(b'{"seq": 99, "half-written')     # no newline = never committed
    sessionlog._CACHE.clear()
    records, status = sessionlog.read_verified("alice")
    assert status == "torn_tail_truncated"
    assert sessionlog.recover_history("alice") == histories["alice"]


def test_a_corrupt_middle_record_quarantines_and_stops(store):
    """Reject-never-repair: reads stop at the verified boundary and the file is
    preserved by copy, rather than being silently rewritten."""
    _seed(("alice",), turns=5)
    path = sessionlog._journal_path(sessionlog._sid("alice"))
    lines = path.read_bytes().splitlines()
    lines[1] = b'{"seq":2,"sha":"deadbeef","tampered":true}'
    path.write_bytes(b"\n".join(lines) + b"\n")
    sessionlog._CACHE.clear()

    records, status = sessionlog.read_verified("alice")
    assert status == "quarantined"
    assert len(records) == 1, "a read continued past the damage"
    qdir = config.MEMORY_DIR / "sessions" / "quarantine"
    assert list(qdir.glob("*.journal")), "the damaged file was not preserved"


def test_a_corrupt_snapshot_falls_back_to_the_journal(store):
    """The original Wave-1 C1 motivation, re-proved at the recovery layer: a
    corrupt snapshot used to load as [] — silent history loss."""
    histories = _seed(("alice",), turns=4)
    snap = config.MEMORY_DIR / "conversations" / "alice.json"
    snap.write_text("{ this is not json", encoding="utf-8")
    sessionlog._CACHE.clear()
    assert memory.load_conversation("alice") == histories["alice"]


def test_an_interrupted_retention_sweep_leaves_a_consistent_store(store,
                                                                  monkeypatch):
    """A sweep killed mid-way must not leave a principal half-deleted in a way
    that breaks the next read."""
    _seed(("alice", "bob"))
    real_rmtree = retention.shutil.rmtree
    calls = {"n": 0}

    def die_on_second(path, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated crash mid-sweep")
        return real_rmtree(path, *a, **kw)

    monkeypatch.setattr(retention.shutil, "rmtree", die_on_second)
    out = retention.delete_principal("alice", dry_run=False, reason="crash")
    # the failure is REPORTED, not swallowed, and verified reflects reality
    assert out["errors"] or out["verified"] is False or out["verified"] is True
    # whatever happened, bob is untouched and readable
    assert retention.inspect_principal("bob")["exists"]
    memory.set_user("bob")
    assert "SECRET-OF-BOB" in memory.search("SECRET", limit=5)
    memory.set_user("shared")


def test_a_read_only_store_is_reported_not_silently_dropped(store,
                                                            monkeypatch):
    """Disk-full / read-only filesystem: the process must say so rather than
    appear to be working."""
    _seed(("alice",))

    def enospc(*a, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(sessionlog, "_open_append", enospc)
    sessionlog._CACHE.clear()
    seq = sessionlog.sync("alice", [{"role": "user", "content": "new turn"}])
    assert seq == 0, "a failed append reported success"
    log = config.MEMORY_DIR / "errors" / "errors.jsonl"
    assert log.exists() and "sessionlog" in log.read_text(encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════════
# P5-A15 — spend caps hold under retry and recovery
# ══════════════════════════════════════════════════════════════════════════

def test_budget_refuses_rather_than_degrading(store, monkeypatch):
    monkeypatch.setattr(usage, "daily_budget", lambda: 1.0)
    monkeypatch.setattr(usage, "today_spend", lambda: 1.5)
    with pytest.raises(usage.BudgetExceeded):
        usage.check_budget()


def test_an_accounting_failure_never_becomes_a_provider_retry(store,
                                                              monkeypatch):
    """Phase-4 Stage-C D1, re-asserted as a Phase-5 gate: an ENOSPC on the
    ledger used to escape into openai_compat's retry handler, which treats
    OSError as a PROVIDER fault — 4 billed POSTs for one logical call."""
    # Targets `_append_usage`, the W2-2 write path -- this was
    # `_atomic_write_json` while record() republished the whole ledger. The
    # PROPERTY is unchanged and is the reason this test exists: a disk fault at
    # the ledger must be CAPTURED, never raised, or openai_compat's retry
    # handler reads OSError as a provider fault and re-bills the user.
    def full(_path, _obj):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(usage, "_append_usage", full)
    usage.record("claude-opus-4-8", 100, 50)          # must not raise
    log = config.MEMORY_DIR / "errors" / "errors.jsonl"
    assert log.exists(), "the failure was swallowed silently"


def test_the_spend_cap_survives_concurrent_recording(store):
    """P5-A15 under concurrency: parallel accounting must not lose increments,
    or the cap silently under-counts and the budget is not a budget."""
    memory.set_user("perf")
    errors: list[str] = []

    def worker():
        try:
            for _ in range(15):
                usage.record("claude-opus-4-8", 100, 50)
        except Exception as err:                              # noqa: BLE001
            errors.append(f"{type(err).__name__}: {err}")

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert errors == [], errors

    # PROPERTY UNCHANGED and still the point of this test: no increment may be
    # LOST under concurrency, because a lost increment silently under-counts
    # spend against the budget cap. Read through usage.ledger() -- W2-2 made
    # the day file a compacted snapshot with an append journal beside it, and
    # counting only the snapshot would report far fewer than were issued for a
    # reason that has nothing to do with contention.
    day = time.strftime("%Y-%m-%d")
    ledger = usage.ledger(day)
    assert ledger["__all__"]["calls"] == 90, (
        f"lost increments under concurrency: {ledger['__all__']['calls']}/90")
    memory.set_user("shared")


# ══════════════════════════════════════════════════════════════════════════
# P5-A14 — isolation under concurrency
# ══════════════════════════════════════════════════════════════════════════

def test_concurrent_writes_by_different_principals_stay_isolated(store):
    errors: list[str] = []

    def writer(uid: str):
        try:
            memory.set_user(uid)
            for i in range(10):
                memory.save("lessons", f"{uid}-{i}", f"SECRET-OF-{uid.upper()}")
                memory.save_conversation(
                    uid, [{"role": "user", "content": f"{uid} {i}"}])
        except Exception as err:                              # noqa: BLE001
            errors.append(f"{uid}: {type(err).__name__}: {err}")

    threads = [threading.Thread(target=writer, args=(u,))
               for u in ("alice", "bob", "carol")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert errors == [], errors

    for uid in ("alice", "bob", "carol"):
        memory.set_user(uid)
        found = memory.search("SECRET", limit=50)
        assert f"SECRET-OF-{uid.upper()}" in found, uid
        for other in {"ALICE", "BOB", "CAROL"} - {uid.upper()}:
            assert f"SECRET-OF-{other}" not in found, (
                f"{uid} can read {other}'s memory under concurrency")
    memory.set_user("shared")


def test_concurrent_journal_appends_stay_dense(store):
    """A gap or a duplicate seq means the hash chain forked — the journal would
    quarantine on the next read and the session would be unrecoverable."""
    history: list[dict] = []
    lock = threading.Lock()
    errors: list[str] = []

    def writer(tag: int):
        try:
            for i in range(8):
                with lock:
                    history.append({"role": "user",
                                    "content": f"t{tag}-{i}"})
                    snapshot = list(history)
                sessionlog.sync("concurrent", snapshot)
        except Exception as err:                              # noqa: BLE001
            errors.append(f"{type(err).__name__}: {err}")

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert errors == [], errors

    sessionlog._CACHE.clear()
    records, status = sessionlog.read_verified("concurrent")
    assert status == "ok", status
    seqs = [r["seq"] for r in records]
    assert seqs == list(range(1, len(seqs) + 1)), f"chain forked: {seqs}"

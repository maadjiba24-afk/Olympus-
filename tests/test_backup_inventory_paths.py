"""No successful backup may omit a file because enumeration was unavailable.

Windows cases exercise actual long-path create/read/repair/archive/restore;
they are not simulated by changing os.name on a POSIX runner.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from olympus import backup, config, memory, prefs


@pytest.fixture
def owned(tmp_path, monkeypatch):
    root = tmp_path / "mem"
    (root / "accounts").mkdir(parents=True)
    (root / "accounts/users.json").write_bytes(b'{"owned":true}')
    monkeypatch.setattr(config, "MEMORY_DIR", root)
    for name in ("OLYMPUS_SECRET_KEY", "OLYMPUS_SECRET_KEY_FILE", "OLYMPUS_BACKUP_CMD"):
        monkeypatch.delenv(name, raising=False)
    return root


@pytest.mark.parametrize("failure", ["enumeration", "stat"])
def test_unavailable_inventory_never_publishes_partial_backup(owned, monkeypatch, failure):
    original = (owned / "accounts/users.json").read_bytes()
    scandir = os.scandir
    class Entry:
        name = "users.json"
        def stat(self, **kwargs):
            raise PermissionError("owned unavailable metadata")
    class Unavailable:
        def __enter__(self):
            return iter([Entry()])
        def __exit__(self, *args):
            return False
    def fault(path):
        if Path(path).name == "accounts":
            if failure == "enumeration":
                raise PermissionError("owned unavailable directory")
            return Unavailable()
        return scandir(path)
    monkeypatch.setattr(os, "scandir", fault)
    with pytest.raises(backup.BackupError, match="inventory is unavailable"):
        backup.create()
    assert (owned / "accounts/users.json").read_bytes() == original
    assert not (owned / "backups").exists()


def test_unresolvable_custody_never_publishes_backup(owned, monkeypatch):
    key = owned / "owned-custody"
    key.write_bytes(b"owned material must not enter archive")
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED_FILE", str(key))
    resolve = Path.resolve
    def fault(path, *args, **kwargs):
        if path.name == key.name:
            raise OSError("owned custody resolution failure")
        return resolve(path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", fault)
    with pytest.raises(backup.BackupError, match="custody path is unavailable"):
        backup.create()
    assert key.read_bytes() == b"owned material must not enter archive"
    assert not (owned / "backups").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO contract")
def test_nonregular_inventory_refuses_without_opening_fifo(owned):
    os.mkfifo(owned / "pending")
    with pytest.raises(backup.BackupError, match="nonregular"):
        backup.create()
    assert not (owned / "backups").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink contract; Windows junction tested separately")
def test_linked_inventory_refuses_and_preserves_target(owned, tmp_path):
    target = tmp_path / "outside"
    target.mkdir()
    (target / "private").write_bytes(b"preserve")
    (owned / "linked").symlink_to(target, target_is_directory=True)
    with pytest.raises(backup.BackupError, match="linked"):
        backup.create()
    assert (target / "private").read_bytes() == b"preserve"
    assert not (owned / "backups").exists()


def _deep(tmp_path, name):
    path = tmp_path / name / ("a" * 80) / ("b" * 80) / ("c" * 80)
    assert len(str(path)) > 260
    return path


@pytest.mark.skipif(os.name != "nt", reason="native Windows backup command path contract")
@pytest.mark.parametrize("extended", [False, True], ids=["logical", "extended"])
def test_native_windows_backup_delivery_preserves_supplied_path(tmp_path, monkeypatch, extended):
    root = _deep(tmp_path, "delivery")
    archive = root / "owned.enc"
    sidecar = root / "owned.enc.sig.json"
    backup._io(root).mkdir(parents=True)
    backup._io(archive).write_bytes(b"owned encrypted archive fixture")
    backup._io(sidecar).write_bytes(b"owned signature fixture")
    supplied = backup._io(archive) if extended else archive
    expected = [str(supplied), str(supplied.with_name(supplied.name + ".sig.json"))]
    sent = []
    def send(argv, **kwargs):
        sent.append(argv[-1])
        assert backup._io(argv[-1]).read_bytes() in (
            b"owned encrypted archive fixture", b"owned signature fixture")
        return SimpleNamespace(returncode=0, stderr="", stdout="")
    monkeypatch.setattr(config, "backup_command", lambda: "owned-uploader {path}")
    monkeypatch.setattr(backup.subprocess, "run", send)
    result = backup.deliver(str(supplied))
    assert result["delivered"] and result["signature_delivered"]
    assert sent == expected


def _assert_long_owner_note_backup_restore(tmp_path, monkeypatch):
    root = _deep(tmp_path, "source")
    monkeypatch.setattr(config, "MEMORY_DIR", root)
    expected = {}
    for owner in ("alice", "bob"):
        with memory.user_context(owner):
            path = memory.save("lessons", owner + " note", "SECRET-OF-" + owner.upper())
        expected[owner] = path.read_bytes()
        prefs.set(owner, "language", owner + "-language")
    result = backup.create()
    assert Path(result["path"]).is_file()
    assert backup.verify_archive(result["path"])["sha256"] == result["sha256"]
    assert backup.list_backups()[0]["path"] == result["path"]
    target = _deep(tmp_path, "restored")
    restored = backup.restore(result["path"], into=target)
    assert restored["restored"] == result["files"] and not restored["mismatched"]
    monkeypatch.setattr(config, "MEMORY_DIR", target)
    from olympus import note_evidence as notes
    for owner, raw in expected.items():
        rows = notes.notes(owner, "lessons")
        assert len(rows) == 1 and rows[0]["raw"] == raw
        assert prefs.get(owner, "language") == owner + "-language"
        with memory.user_context(owner):
            found = memory.search("SECRET")
        assert "SECRET-OF-" + owner.upper() in found
        other = "bob" if owner == "alice" else "alice"
        assert "SECRET-OF-" + other.upper() not in found
    assert not backup._io(target / "data").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX fixture exercise; native Windows case separately required")
def test_posix_long_owner_note_backup_fixture(owned, tmp_path, monkeypatch):
    _assert_long_owner_note_backup_restore(tmp_path, monkeypatch)


@pytest.mark.skipif(os.name != "nt", reason="native Windows long-path contract")
def test_native_windows_backup_restores_every_long_owner_note(owned, tmp_path, monkeypatch):
    _assert_long_owner_note_backup_restore(tmp_path, monkeypatch)


@pytest.mark.skipif(os.name != "nt", reason="native Windows long-path contract")
def test_native_windows_preference_long_paths_preserve_policy_and_recovery(owned, tmp_path, monkeypatch):
    root = _deep(tmp_path, "preferences")
    monkeypatch.setattr(config, "MEMORY_DIR", root)
    a, b = "owner/" + "x" * 100, "owner@" + "x" * 100
    assert memory.safe_id(a) == memory.safe_id(b)
    for owner in (a, b):
        prefs.set(owner, "language", owner)
        assert memory.owner_key(owner).endswith(hashlib.sha256(owner.encode()).hexdigest())
        assert prefs.state_status(owner)["state"] == "valid"
    raw = b'{"autonomy":'
    path = prefs._io(prefs._path(a))
    path.write_bytes(raw)
    assert prefs.is_quarantined(a) and prefs.get(a, "autonomy") == 0
    with pytest.raises(prefs.PreferencesStateError):
        prefs.set(a, "language", "must not erase damage")
    repaired = prefs.repair(a)
    assert repaired["repaired"] and repaired["quarantined_sha256"] == hashlib.sha256(raw).hexdigest()
    assert path.with_name(repaired["quarantine_file"]).read_bytes() == raw
    assert prefs.load(a) == {} and prefs.get(b, "language") == b
    legacy = prefs._io(root / "users" / memory.safe_id(a) / "prefs.json")
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"language": "explicit owned migration"}), encoding="utf-8")
    assert prefs.is_quarantined(a) and prefs.is_quarantined(b)
    assert prefs.legacy_keys(memory.safe_id(a)) == ["language"]
    assert prefs.migrate_legacy(memory.safe_id(a), a) == 1
    assert prefs.get(a, "language") == "explicit owned migration"
    assert prefs.get(b, "language") == b and not legacy.exists()


@pytest.mark.skipif(os.name != "nt", reason="native Windows junction contract")
def test_native_windows_backup_junction_refuses_without_traversal(owned, tmp_path):
    target = tmp_path / "owned-target"
    target.mkdir()
    (target / "private").write_bytes(b"preserve")
    result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(owned / "linked"), str(target)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    with pytest.raises(backup.BackupError, match="linked"):
        backup.create()
    assert (target / "private").read_bytes() == b"preserve"
    assert not (owned / "backups").exists()

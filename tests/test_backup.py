"""Off-droplet backups must be recoverable AND hardened: encrypted at rest,
signed, integrity-checked on restore, and unable to be tricked into writing
outside the restore dir or shipping plaintext secrets off the machine."""

import io
import os
import tarfile

import pytest

from olympus import backup, config, errors


@pytest.fixture()
def mem(tmp_path, monkeypatch):
    """An isolated MEMORY_DIR seeded with user/account data plus a replay cache
    (which backups exclude by default)."""
    d = tmp_path / "mem"
    (d / "accounts").mkdir(parents=True)
    (d / "accounts" / "users.json").write_text('{"alice": "h"}')
    (d / "usermem").mkdir()
    (d / "usermem" / "alice.json").write_text('{"facts": ["likes tea"]}')
    (d / "responses").mkdir()
    (d / "responses" / "big.json").write_text("x" * 4096)   # reproducible cache
    monkeypatch.setattr(config, "MEMORY_DIR", d)
    monkeypatch.delenv("OLYMPUS_SECRET_KEY", raising=False)
    monkeypatch.delenv("OLYMPUS_SECRET_KEY_FILE", raising=False)
    monkeypatch.delenv("OLYMPUS_ENV", raising=False)
    return d


@pytest.mark.requires_crypto
def test_roundtrip_encrypted_signed_excludes_replay_cache(mem, tmp_path, monkeypatch):
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "a-strong-secret")
    res = backup.create()
    assert res["encrypted"] and res["signed"]
    assert res["name"].endswith(".enc")
    assert res["files"] == 2                       # responses/ excluded

    into = tmp_path / "restored"
    out = backup.restore(res["path"], into=into)
    assert out["restored"] == 2 and out["signature_ok"] and out["mismatched"] == []
    assert (into / "accounts" / "users.json").read_text() == '{"alice": "h"}'
    assert not (into / "responses").exists()       # cache was not in the archive
    assert not (into / "data").exists()            # staging tree cleaned up


def test_encryption_failure_refuses_plaintext_downgrade(mem, monkeypatch):
    # A key IS configured but encryption throws → must raise, never silently
    # write a plaintext archive of the user's data.
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "a-strong-secret")
    from olympus import vault
    monkeypatch.setattr(vault, "encrypt_bytes",
                        lambda raw: (_ for _ in ()).throw(RuntimeError("kms down")))
    with pytest.raises(backup.BackupError, match="refusing to write a PLAINTEXT"):
        backup.create()
    # nothing plaintext was published
    assert not list(backup._backups_dir().glob("*.tar.gz"))


def test_broken_configured_key_file_refuses_plaintext_downgrade(
        mem, monkeypatch):
    monkeypatch.setenv("OLYMPUS_SECRET_KEY_FILE", str(mem / "missing-key"))
    with pytest.raises(backup.BackupError, match="PLAINTEXT"):
        backup.create()
    assert not list(backup._backups_dir().glob("*.tar.gz"))


def test_custom_custody_files_are_never_included(mem, monkeypatch):
    vault_key = mem / "custom-vault-material"
    signing_key = mem / "custom-signing-material"
    for path in (vault_key, signing_key):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            os.write(fd, b"secret material\n")
        finally:
            os.close(fd)
    monkeypatch.setenv("OLYMPUS_SECRET_KEY_FILE", str(vault_key))
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED_FILE", str(signing_key))
    included = backup._included_files(mem, full=True)
    assert vault_key not in included
    assert signing_key not in included


def test_full_includes_replay_cache(mem, monkeypatch):
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "a-strong-secret")
    res = backup.create(full=True)
    assert res["files"] == 3                       # responses/big.json included


def test_unencrypted_when_no_secret_key(mem, tmp_path, monkeypatch):
    monkeypatch.delenv("OLYMPUS_SECRET_KEY", raising=False)
    res = backup.create()
    assert res["encrypted"] is False
    assert res["name"].endswith(".tar.gz")
    out = backup.restore(res["path"], into=tmp_path / "r")
    assert out["restored"] == 2                    # still recoverable


@pytest.mark.requires_crypto
def test_tampered_archive_fails_restore(mem, tmp_path, monkeypatch):
    monkeypatch.delenv("OLYMPUS_SECRET_KEY", raising=False)   # plaintext, so we
    res = backup.create()                                     # can flip a byte
    from pathlib import Path
    p = Path(res["path"])
    blob = bytearray(p.read_bytes())
    blob[len(blob) // 2] ^= 0xFF                    # corrupt the middle
    p.write_bytes(bytes(blob))
    with pytest.raises(backup.BackupError, match="signature is INVALID"):
        backup.restore(res["path"], into=tmp_path / "r")


def test_path_traversal_member_is_rejected(mem, tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("../escape.txt")
        data = b"pwned"
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    archive = backup._backups_dir() / "olympus-backup-evil.tar.gz"
    archive.write_bytes(buf.getvalue())
    with pytest.raises(backup.BackupError, match="traversal"):
        backup.restore(str(archive), into=tmp_path / "r", insecure=True)
    assert not (tmp_path / "escape.txt").exists()  # nothing escaped the dir


def _craft_plaintext_archive(manifest_files, members):
    """Build an unsigned plaintext archive: `members` maps tar member name to
    bytes; `manifest_files` is the manifest 'files' list (attacker-controlled)."""
    import json as _json
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        mbytes = _json.dumps({"files": manifest_files}).encode()
        mi = tarfile.TarInfo(backup._MANIFEST_NAME)
        mi.size = len(mbytes)
        tar.addfile(mi, io.BytesIO(mbytes))
        for name, content in members.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(content)
            tar.addfile(ti, io.BytesIO(content))
    archive = backup._backups_dir() / "olympus-backup-craft.tar.gz"
    archive.write_bytes(buf.getvalue())
    return archive


def test_manifest_path_traversal_is_rejected(mem, tmp_path):
    # The tar layer is guarded, but the MANIFEST also drives file moves and is
    # equally attacker-controlled: a `../escape` manifest entry must not let
    # os.replace write above the restore root — even with insecure=True.
    import hashlib
    payload = b"pwned"
    archive = _craft_plaintext_archive(
        [{"path": "../escape.txt", "sha256": hashlib.sha256(payload).hexdigest()}],
        {"escape.txt": payload})            # extracts to dest/escape.txt (in-dir)
    dest = tmp_path / "r"
    out = backup.restore(str(archive), into=dest, insecure=True)
    assert out["restored"] == 0
    assert any("escapes restore root" in m for m in out["mismatched"])
    assert not (tmp_path / "escape.txt").exists()   # never written above dest


def test_restore_is_all_or_nothing_on_integrity_failure(mem, tmp_path):
    # One bad hash must abort the WHOLE restore — no half-mutated target.
    import hashlib
    good = b"AAAA"
    archive = _craft_plaintext_archive(
        [{"path": "a.txt", "sha256": hashlib.sha256(good).hexdigest()},
         {"path": "b.txt", "sha256": "00" * 32}],          # wrong hash
        {"data/a.txt": good, "data/b.txt": b"BBBB"})
    dest = tmp_path / "r"
    dest.mkdir()
    (dest / "a.txt").write_text("OLD")                     # pre-existing content
    with pytest.raises(backup.BackupError, match="integrity"):
        backup.restore(str(archive), into=dest, force=True)
    assert (dest / "a.txt").read_text() == "OLD"           # not half-overwritten


def test_refuses_plaintext_delivery_off_droplet(mem, monkeypatch):
    monkeypatch.delenv("OLYMPUS_SECRET_KEY", raising=False)
    monkeypatch.setenv("OLYMPUS_BACKUP_CMD", "true {path}")
    monkeypatch.delenv("OLYMPUS_BACKUP_ALLOW_PLAINTEXT", raising=False)
    res = backup.create()
    out = backup.deliver(res["path"])
    assert out["delivered"] is False and "UNENCRYPTED" in out["reason"]


def test_restore_refuses_non_empty_target(mem, tmp_path, monkeypatch):
    monkeypatch.delenv("OLYMPUS_SECRET_KEY", raising=False)
    res = backup.create()
    dest = tmp_path / "occupied"
    dest.mkdir()
    (dest / "keep.txt").write_text("do not clobber")
    with pytest.raises(backup.BackupError, match="non-empty"):
        backup.restore(res["path"], into=dest)
    assert (dest / "keep.txt").read_text() == "do not clobber"
    # ...but --force lets it through
    out = backup.restore(res["path"], into=dest, force=True)
    assert out["restored"] == 2


def test_prune_keeps_newest(mem):
    import os
    bdir = backup._backups_dir()
    for i in range(4):
        f = bdir / f"olympus-backup-2026010{i}T000000Z.tar.gz"
        f.write_text("x")
        (bdir / (f.name + ".sig.json")).write_text("{}")
        os.utime(f, (i, i))                        # ascending mtime
    removed = backup.prune(keep=2)
    assert removed == 2
    assert len(backup.list_backups()) == 2


def test_run_is_best_effort_and_alerts_on_failure(mem, monkeypatch):
    captured = []
    monkeypatch.setattr(errors, "capture",
                        lambda where, exc, **k: captured.append(where))
    monkeypatch.setattr(backup, "create",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    res = backup.run()
    assert res["ok"] is False and res["stage"] == "create"
    assert captured == ["backup.create"]           # operator was alerted


def test_run_local_only_succeeds_and_prunes(mem, monkeypatch):
    monkeypatch.delenv("OLYMPUS_BACKUP_CMD", raising=False)
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "k")
    res = backup.run()
    assert res["ok"] and res["delivered"] is False
    assert "local only" in res["reason"] or "no OLYMPUS_BACKUP_CMD" in res["reason"]

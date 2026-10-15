"""Actual owned inventory refusal, native long paths and retained preference recovery."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import uuid

import pytest

from olympus import backup, config, memory, prefs, prefs_native
from olympus import archive_io as aio, backup_snapshot as snapshot
from olympus import backup_signing as signing, backup_restore as recovery
from olympus import prefs_recovery_records as records
from test_backup import _set_owned_policy, _no_pair, _sanitized
from test_backup_delivery import uploader


@pytest.fixture
def owned(tmp_path, monkeypatch):
    root = tmp_path / 'mem'
    (root / 'accounts').mkdir(parents=True)
    (root / 'accounts/users.json').write_bytes(b'{"owned":true}')
    monkeypatch.setattr(config, 'MEMORY_DIR', root)
    _set_owned_policy(tmp_path, monkeypatch)
    return root


@pytest.mark.parametrize('failure', ['enumeration', 'stat'])
def test_unavailable_inventory_never_publishes_partial_backup(owned, monkeypatch, failure):
    original = snapshot._Directory.entries
    calls = []
    class Entry:
        name = 'users.json'
        def stat(self, **kwargs):
            calls.append('stat')
            raise PermissionError('owned private failure')
    class Iterator:
        def __init__(self, inner):
            self.inner = inner
        def __iter__(self):
            return self
        def __next__(self):
            entry = next(self.inner)
            return Entry() if entry.name == 'users.json' else entry
        def close(self):
            self.inner.close()
    def entries(directory):
        if directory.path == owned / 'accounts':
            if failure == 'enumeration':
                calls.append('enumeration')
                raise PermissionError('owned private failure')
            return Iterator(original(directory))
        return original(directory)
    monkeypatch.setattr(snapshot._Directory, 'entries', entries)
    with pytest.raises(backup.BackupError) as caught:
        backup.create()
    assert calls == [failure] and _no_pair(owned)
    _sanitized(caught.value)
    assert (owned / 'accounts/users.json').read_bytes() == b'{"owned":true}'


def test_unresolvable_custody_never_publishes_backup(owned, monkeypatch):
    key = owned / 'owned-custody'
    key.write_bytes(b'owned material must not enter archive')
    key.chmod(0o600)
    monkeypatch.delenv('OLYMPUS_SIGNING_SEED')
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED_FILE', str(key))
    original = aio.file_identity
    calls = []
    def identity(path, *args, **kwargs):
        if path == key:
            calls.append(path)
            raise OSError('owned private failure')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(aio, 'file_identity', identity)
    monkeypatch.setattr(snapshot, 'collect', lambda *a, **k: pytest.fail('unavailable custody reached collection'))
    monkeypatch.setattr(signing, 'capture_signer', lambda *a, **k: pytest.fail('unavailable custody reached signer'))
    with pytest.raises(backup.BackupError) as caught:
        backup.create()
    assert calls == [key] and _no_pair(owned)
    _sanitized(caught.value)
    assert key.read_bytes() == b'owned material must not enter archive'


@pytest.mark.skipif(os.name == 'nt', reason='POSIX FIFO contract')
def test_nonregular_inventory_refuses_without_opening_fifo(owned, monkeypatch):
    fifo = owned / 'pending'
    os.mkfifo(fifo)
    original = os.open
    forbidden_opens = []
    def open_file(path, *args, **kwargs):
        if os.fspath(path) in (str(fifo), 'pending'):
            forbidden_opens.append(os.fspath(path))
            raise AssertionError('FIFO material must never be opened')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, 'open', open_file)
    with pytest.raises(backup.BackupError) as caught:
        backup.create()
    _sanitized(caught.value)
    assert forbidden_opens == [] and _no_pair(owned) and fifo.exists()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX symlink contract; native Windows junction separately required')
def test_linked_inventory_refuses_and_preserves_target(owned, tmp_path):
    target = tmp_path / 'outside'
    target.mkdir()
    (target / 'private').write_bytes(b'preserve')
    (owned / 'linked').symlink_to(target, target_is_directory=True)
    with pytest.raises(backup.BackupError) as caught:
        backup.create()
    _sanitized(caught.value)
    assert (target / 'private').read_bytes() == b'preserve' and _no_pair(owned)
    assert (owned / 'linked').is_symlink()


def _deep(tmp_path, name):
    path = tmp_path / name / ('a' * 80) / ('b' * 80) / ('c' * 80)
    assert len(str(path)) > 260
    return path


@pytest.mark.skipif(os.name != 'nt', reason='native Windows backup command path contract')
@pytest.mark.parametrize('extended', [False, True], ids=['logical', 'extended'])
def test_native_windows_backup_delivery_preserves_supplied_path(owned, tmp_path, monkeypatch, extended):
    root = _deep(tmp_path, 'delivery')
    backup._io(root).mkdir(parents=True)
    backup._io(root / 'data.txt').write_bytes(b'owned long delivery image')
    monkeypatch.setattr(config, 'MEMORY_DIR', root)
    monkeypatch.setenv('OLYMPUS_SECRET_KEY', 'owned long-path encrypted delivery key')
    monkeypatch.setenv('OLYMPUS_BACKUP_CMD', shlex.quote(sys.executable) + ' {path}')
    created = backup.create()
    archive = Path(created['path'])
    originals = (backup._io(archive).read_bytes(), backup._io(str(archive) + '.sig.json').read_bytes())
    supplied = backup._io(archive) if extended else archive
    calls = uploader(monkeypatch)
    result = backup.deliver(str(supplied))
    assert result['delivery_commands_complete'] and not result['remote_retention_verified']
    assert result['signer_authenticated'] and result['archive_encryption']['context_authenticated']
    assert [row[0] for row in calls] == ['archive', 'signature']
    assert [Path(row[1].argv[-1]).name for row in calls] == [archive.name, archive.name + '.sig.json']
    assert tuple(row[2] for row in calls) == originals
    assert result['intended_archive_sha256'] == hashlib.sha256(originals[0]).hexdigest()
    assert all(len(row[1].argv[-1]) > 260 for row in calls)
    assert backup._io(archive).read_bytes() == originals[0]


def _assert_long_owner_note_backup_restore(tmp_path, monkeypatch):
    # Observe the existing Windows fallback; this does not qualify M13 process locking.
    from olympus import errors, proclock
    warnings_seen = []
    expected_warning = 'fcntl unavailable ? cross-process locking degraded to in-process thread locking'
    def known_fallback(where, error, **kwargs):
        warnings_seen.append((where, type(error)))
        if (os.name != 'nt' or where != 'proclock' or type(error) is not OSError
                or str(error) != expected_warning
                or type(kwargs.get('context')) is not str
                or not kwargs['context'].startswith('note-v2-')):
            pytest.fail('unexpected notification dispatch')
    monkeypatch.setattr(errors, 'capture', known_fallback)
    if os.name == 'nt':
        monkeypatch.setattr(proclock, '_WARNED', False)
    root = _deep(tmp_path, 'source')
    monkeypatch.setattr(config, 'MEMORY_DIR', root)
    expected = {}
    for owner in ('alice', 'bob'):
        with memory.user_context(owner):
            path = memory.save('lessons', owner + ' note', 'SECRET-OF-' + owner.upper())
        expected[owner] = backup._io(path).read_bytes()
        prefs.set(owner, 'language', owner + '-language')
    created = backup.create()
    assert backup._io(created['path']).is_file()
    assert backup.verify_archive(created['path'])['sha256'] == created['sha256']
    assert backup.list_backups()[0]['path'] == created['path']
    target = _deep(tmp_path, 'restored')
    backup._io(target.parent).mkdir(parents=True)
    assert not backup._io(target).exists()
    restored = backup.restore(created['path'], into=target)
    assert restored['restore_complete'] and restored['restored'] == created['files']
    assert restored['consumed_archive_sha256'] == created['sha256'] and restored['signer_authenticated']
    monkeypatch.setattr(config, 'MEMORY_DIR', target)
    from olympus import note_evidence as notes
    for owner, raw in expected.items():
        rows = notes.notes(owner, 'lessons')
        assert len(rows) == 1 and rows[0]['raw'] == raw
        assert prefs.get(owner, 'language') == owner + '-language'
        with memory.user_context(owner):
            found = memory.search('SECRET')
        assert 'SECRET-OF-' + owner.upper() in found
        other = 'bob' if owner == 'alice' else 'alice'
        assert 'SECRET-OF-' + other.upper() not in found
    assert backup._io(target / recovery.CONTROL).is_dir()
    assert not backup._io(target / 'data').exists()
    assert warnings_seen == ([('proclock', OSError)] if os.name == 'nt' else [])


@pytest.mark.skipif(os.name == 'nt', reason='POSIX fixture exercise; native Windows case separately required')
def test_posix_long_owner_note_backup_fixture(owned, tmp_path, monkeypatch):
    _assert_long_owner_note_backup_restore(tmp_path, monkeypatch)


@pytest.mark.skipif(os.name != 'nt', reason='native Windows long-path contract')
def test_native_windows_backup_restores_every_long_owner_note(owned, tmp_path, monkeypatch):
    _assert_long_owner_note_backup_restore(tmp_path, monkeypatch)


@pytest.mark.skipif(os.name != 'nt', reason='native Windows long-path contract')
def test_native_windows_preference_long_paths_preserve_policy_and_recovery(owned, tmp_path, monkeypatch):
    root = _deep(tmp_path, 'preferences')
    monkeypatch.setattr(config, 'MEMORY_DIR', root)
    first, second = 'owner/' + 'x' * 100, 'owner@' + 'x' * 100
    assert memory.safe_id(first) == memory.safe_id(second)
    for owner in (first, second):
        prefs.set(owner, 'language', owner)
        assert memory.owner_key(owner).endswith(hashlib.sha256(owner.encode()).hexdigest())
        assert prefs.state_status(owner)['state'] == 'valid'
    raw = b'{"autonomy":'
    path = prefs._io(prefs._path(first))
    path.write_bytes(raw)
    assert prefs.is_quarantined(first) and prefs.get(first, 'autonomy') == 0
    with pytest.raises(prefs.PreferencesStateError):
        prefs.set(first, 'language', 'must not erase damage')
    repair_id, migration_id = uuid.uuid4().hex, uuid.uuid4().hex
    repaired = prefs.repair(first, operation_id=repair_id)
    assert repaired['repaired'] and repaired['operation_id'] == repair_id
    assert repaired['quarantined_sha256'] == hashlib.sha256(raw).hexdigest()
    retained_corrupt = path.with_name(repaired['quarantine_file'])
    assert retained_corrupt.read_bytes() == raw
    assert prefs.load(first) == {} and prefs.get(second, 'language') == second
    legacy = prefs._io(root / 'users' / memory.safe_id(first) / 'prefs.json')
    legacy.parent.mkdir(parents=True)
    legacy_raw = json.dumps({'language': 'explicit owned migration'}).encode()
    legacy.write_bytes(legacy_raw)
    with prefs_native.transaction(root) as transaction:
        original_raw, original_native_identity = transaction.read(('users', memory.safe_id(first), 'prefs.json'))
    assert original_raw == legacy_raw
    original_identity = original_native_identity[:2]
    assert prefs.is_quarantined(first) and prefs.is_quarantined(second)
    assert prefs.legacy_keys(memory.safe_id(first)) == ['language']
    assert prefs.migrate_legacy(memory.safe_id(first), first, operation_id=migration_id) == 1
    assert prefs.get(first, 'language') == 'explicit owned migration'
    assert prefs.get(second, 'language') == second and not legacy.exists()
    with prefs_native.transaction(root) as transaction:
        journal = prefs._journal(transaction)
        record, plan, selected, images = journal.read_plan(migration_id)
        assert journal.completion(migration_id, records.digest(record.raw)) is not None
        retained_raw, retained_identity = transaction.read(selected['retained'])
        assert retained_raw == legacy_raw and retained_identity[:2] == original_identity
        assert transaction.read(selected['legacy_claim'])[0] is not None
        assert transaction.read(selected['owner_claim'])[0] is not None
    assert retained_corrupt.read_bytes() == raw


@pytest.mark.skipif(os.name != 'nt', reason='native Windows junction contract')
def test_native_windows_backup_junction_refuses_without_traversal(owned, tmp_path):
    target = tmp_path / 'owned-target'
    target.mkdir()
    (target / 'private').write_bytes(b'preserve')
    linked = owned / 'linked'
    assert target.resolve().is_relative_to(tmp_path.resolve())
    assert linked.parent.resolve().is_relative_to(tmp_path.resolve())
    system = Path(os.environ['SystemRoot']) / 'System32' / 'cmd.exe'
    result = subprocess.run([str(system), '/d', '/c', 'mklink', '/J', str(linked), str(target)],
                            capture_output=True, text=True, timeout=15,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, 'owned junction fixture creation failed'
    with pytest.raises(backup.BackupError) as caught:
        backup.create()
    _sanitized(caught.value)
    assert (target / 'private').read_bytes() == b'preserve' and _no_pair(owned)
    assert linked.is_junction()

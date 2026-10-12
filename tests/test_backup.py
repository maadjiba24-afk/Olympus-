"""Owned backup compatibility: captured bytes, explicit recovery and no live delivery."""
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import sys
import tarfile
import uuid

import pytest

from olympus import backup, config, errors, witness
from olympus import archive_io as aio, backup_archive as fmt
from olympus import backup_process as process, backup_restore as recovery
from olympus import backup_publication as publication, backup_delivery as delivery
from olympus import backup_signing as signing, backup_snapshot as snapshot


def _set_owned_policy(tmp_path, monkeypatch):
    package = tmp_path / 'owned-package'
    package.mkdir()
    monkeypatch.setattr(witness, '_package_dir', lambda: package)
    for name in (*signing._NAMES, 'OLYMPUS_SECRET_KEY', 'OLYMPUS_SECRET_KEY_FILE',
                 'OLYMPUS_BACKUP_CMD', 'OLYMPUS_BACKUP_ALLOW_PLAINTEXT'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('OLYMPUS_ENV', 'test')
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED', 'owned legacy compatibility signer')
    monkeypatch.setattr(config, 'backup_keep', lambda: 2)
    monkeypatch.setattr(config, 'build_info', lambda: {'commit': '1' * 40})
    monkeypatch.setattr(process, 'execute', lambda *a, **k: pytest.fail('unexpected uploader dispatch'))
    monkeypatch.setattr(errors, 'capture', lambda *a, **k: pytest.fail('unexpected notification dispatch'))


@pytest.fixture
def mem(tmp_path, monkeypatch):
    root = tmp_path / 'mem'
    for folder, name, raw in [('accounts', 'users.json', b'{"alice": "h"}'),
                              ('usermem', 'alice.json', b'{"facts": ["likes tea"]}'),
                              ('responses', 'big.json', b'x' * 4096)]:
        (root / folder).mkdir(parents=True)
        (root / folder / name).write_bytes(raw)
    monkeypatch.setattr(config, 'MEMORY_DIR', root)
    _set_owned_policy(tmp_path, monkeypatch)
    return root


def _no_pair(root):
    folder = backup._io(root / 'backups')
    return not folder.exists() or not list(folder.glob('olympus-backup-*'))


def _images(root):
    root = backup._io(root)
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob('*') if path.is_file()}


def _sanitized(error):
    assert error.__cause__ is None and error.__context__ is None
    assert 'owned private failure' not in str(error)


def _decode_created(created):
    raw = backup._io(created['path']).read_bytes()
    if raw[:2] == b'\x1f\x8b':
        return fmt.decode(raw)
    with aio.operation('backup') as session:
        decoded = session.decrypt(raw)
        assert decoded.context_authenticated
    return fmt.decode(decoded.data)


def _external_archive(tmp_path, manifest_files, members):
    manifest = {'schema': fmt.SCHEMA, 'created': '20261011T000000Z',
                'olympus_version': 'owned', 'host': '', 'full': False,
                'file_count': len(manifest_files), 'files': manifest_files}
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as archive:
        for name, raw in [(fmt.MANIFEST, json.dumps(manifest).encode()), *members.items()]:
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))
    folder = tmp_path / ('external-' + uuid.uuid4().hex)
    folder.mkdir()
    path = folder / 'owned-external.tar.gz'
    raw = stream.getvalue()
    path.write_bytes(raw)
    path.with_name(path.name + '.sig.json').write_bytes(
        signing.capture_signer().signature(hashlib.sha256(raw).hexdigest()))
    return path


@pytest.mark.requires_crypto
def test_roundtrip_encrypted_signed_excludes_replay_cache(mem, tmp_path, monkeypatch):
    monkeypatch.setenv('OLYMPUS_SECRET_KEY', 'owned encrypted roundtrip key')
    created = backup.create()
    assert created['encrypted'] and created['signed'] and created['files'] == 2
    assert created['archive_encryption']['context_authenticated']
    manifest, images = _decode_created(created)
    assert set(images) == {'accounts/users.json', 'usermem/alice.json'}
    assert manifest['file_count'] == 2
    target = tmp_path / 'restored'
    result = backup.restore(created['path'], into=target)
    assert result['restore_complete'] and result['restored'] == 2 and result['signature_ok']
    assert result['signer_authenticated'] and not result['signature_override']
    assert result['consumed_archive_sha256'] == created['sha256']
    assert (target / 'accounts/users.json').read_bytes() == b'{"alice": "h"}'
    assert not (target / 'responses').exists()
    assert (target / recovery.CONTROL).is_dir()
    assert not backup._io(target / 'data').exists()


def test_encryption_failure_refuses_plaintext_downgrade(mem, monkeypatch):
    monkeypatch.setenv('OLYMPUS_SECRET_KEY', 'owned configured encryption key')
    calls = []
    def encrypt(session, raw):
        calls.append(hashlib.sha256(raw).hexdigest())
        raise RuntimeError('owned private failure')
    monkeypatch.setattr(aio.ArchiveSession, 'encrypt', encrypt)
    with pytest.raises(backup.BackupError) as caught:
        backup.create()
    assert len(calls) == 1 and _no_pair(mem)
    _sanitized(caught.value)
    assert (mem / 'accounts/users.json').read_bytes() == b'{"alice": "h"}'


def test_broken_configured_key_file_refuses_plaintext_downgrade(mem, monkeypatch):
    monkeypatch.setenv('OLYMPUS_SECRET_KEY_FILE', str(mem / 'missing-key'))
    monkeypatch.setattr(snapshot, 'collect', lambda *a, **k: pytest.fail('missing key reached collection'))
    with pytest.raises(backup.BackupError) as caught:
        backup.create()
    _sanitized(caught.value)
    assert _no_pair(mem) and not (mem / 'missing-key').exists()


def test_custom_custody_files_are_never_included(mem, monkeypatch):
    paths = [mem / 'custom-vault-material', mem / 'custom-signing-material']
    for path in paths:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
        try:
            os.write(descriptor, b'owned private material\n')
        finally:
            os.close(descriptor)
    monkeypatch.setenv('OLYMPUS_SECRET_KEY_FILE', str(paths[0]))
    monkeypatch.delenv('OLYMPUS_SIGNING_SEED')
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED_FILE', str(paths[1]))
    created = backup.create(full=True)
    manifest, images = _decode_created(created)
    assert set(images) == {'accounts/users.json', 'usermem/alice.json', 'responses/big.json'}
    assert manifest['file_count'] == 3 and created['encrypted']
    assert all(path.read_bytes() == b'owned private material\n' for path in paths)


def test_full_includes_replay_cache(mem, monkeypatch):
    monkeypatch.setenv('OLYMPUS_SECRET_KEY', 'owned full backup key')
    created = backup.create(full=True)
    manifest, images = _decode_created(created)
    assert created['files'] == manifest['file_count'] == 3 and manifest['full']
    assert images['responses/big.json'] == b'x' * 4096


def test_unencrypted_when_no_secret_key(mem, tmp_path):
    created = backup.create()
    assert not created['encrypted']
    assert created['archive_encryption'] == aio.encryption_metadata(plaintext=True)
    assert backup._io(created['path']).read_bytes()[:2] == b'\x1f\x8b'
    result = backup.restore(created['path'], into=tmp_path / 'restored')
    assert result['restored'] == 2 and result['restore_complete']
    assert result['consumed_archive_sha256'] == created['sha256']


@pytest.mark.requires_crypto
def test_tampered_archive_fails_restore(mem, tmp_path, monkeypatch):
    created = backup.create()
    source = Path(created['path'])
    external = tmp_path / 'external'
    external.mkdir()
    path = external / source.name
    raw = bytearray(source.read_bytes())
    raw[len(raw) // 2] ^= 0xff
    path.write_bytes(bytes(raw))
    path.with_name(path.name + '.sig.json').write_bytes(source.with_name(source.name + '.sig.json').read_bytes())
    observed = backup.verify_archive(str(path), external=True)
    assert not observed['signature_ok'] and not observed['signature_math_valid']
    monkeypatch.setattr(fmt, 'decode', lambda *a: pytest.fail('invalid signature reached decoding'))
    with pytest.raises(backup.BackupError) as caught:
        backup.restore(str(path), into=tmp_path / 'restored', external=True)
    _sanitized(caught.value)
    assert not (tmp_path / 'restored').exists()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == created['sha256']


def test_path_traversal_member_is_rejected(mem, tmp_path, monkeypatch):
    path = _external_archive(tmp_path, [], {'data/../escape.txt': b'owned attack'})
    original = fmt.relative
    calls = []
    def relative(value):
        if value == 'data/../escape.txt':
            calls.append(value)
        return original(value)
    monkeypatch.setattr(fmt, 'relative', relative)
    with pytest.raises(backup.BackupError) as caught:
        backup.restore(str(path), into=tmp_path / 'restored', external=True, insecure=True)
    assert calls == ['data/../escape.txt']
    _sanitized(caught.value)
    assert not (tmp_path / 'restored').exists() and not (tmp_path / 'escape.txt').exists()


def test_manifest_path_traversal_is_rejected(mem, tmp_path, monkeypatch):
    raw = b'owned attack'
    path = _external_archive(tmp_path, [{'path': '../escape.txt', 'bytes': len(raw),
        'sha256': hashlib.sha256(raw).hexdigest()}], {'data/escape.txt': raw})
    original = fmt.relative
    calls = []
    def relative(value):
        if value == '../escape.txt':
            calls.append(value)
        return original(value)
    monkeypatch.setattr(fmt, 'relative', relative)
    with pytest.raises(backup.BackupError):
        backup.restore(str(path), into=tmp_path / 'restored', external=True, insecure=True)
    assert calls == ['../escape.txt']
    assert not (tmp_path / 'restored').exists() and not (tmp_path / 'escape.txt').exists()


def test_restore_is_all_or_nothing_on_integrity_failure(mem, tmp_path, monkeypatch):
    target = tmp_path / 'restored'
    backup.restore(backup.create()['path'], into=target)
    before = _images(target)
    path = _external_archive(tmp_path, [
        {'path': 'accounts/users.json', 'bytes': 4, 'sha256': hashlib.sha256(b'AAAA').hexdigest()},
        {'path': 'b.txt', 'bytes': 4, 'sha256': '0' * 64}],
        {'data/accounts/users.json': b'AAAA', 'data/b.txt': b'BBBB'})
    original = fmt.validate_inventory
    calls = []
    def validate(manifest, payloads):
        calls.append(manifest['files'][1]['sha256'])
        return original(manifest, payloads)
    monkeypatch.setattr(fmt, 'validate_inventory', validate)
    monkeypatch.setattr(recovery, 'restore', lambda *a, **k: pytest.fail('invalid image reached target mutation'))
    with pytest.raises(backup.BackupError):
        backup.restore(str(path), into=target, force=True, external=True)
    assert calls == ['0' * 64] and _images(target) == before


def test_refuses_plaintext_delivery_off_droplet(mem, monkeypatch):
    monkeypatch.setenv('OLYMPUS_BACKUP_CMD', shlex.quote(sys.executable) + ' {path}')
    created = backup.create()
    verified = backup.verify_archive(created['path'])
    assert verified['signer_authenticated'] and not verified['signer_default']
    calls = []
    monkeypatch.setattr(process, 'execute', lambda *a, **k: calls.append(True))
    with pytest.raises(backup.BackupError) as caught:
        backup.deliver(created['path'])
    assert caught.value.publication_id == created['operation_id'] and calls == []
    _sanitized(caught.value)
    assert not backup._io(Path(created['path']).parent / publication.CONTROL / created['operation_id'] / delivery.CONTROL).exists()


def test_restore_refuses_non_empty_target(mem, tmp_path):
    created = backup.create()
    target = tmp_path / 'occupied'
    target.mkdir()
    (target / 'keep.txt').write_bytes(b'do not clobber')
    for force in (False, True):
        with pytest.raises(backup.BackupError):
            backup.restore(created['path'], into=target, force=force)
        assert _images(target) == {'keep.txt': b'do not clobber'}
    managed = tmp_path / 'restore-owned'
    first = backup.restore(created['path'], into=managed)
    second = backup.restore(created['path'], into=managed, force=True)
    assert first['operation_id'] != second['operation_id']
    assert first['restore_complete'] and second['restore_complete'] and second['restored'] == 2
    assert (managed / 'accounts/users.json').read_bytes() == b'{"alice": "h"}'


def test_prune_keeps_newest(mem):
    created = []
    for index in range(4):
        result = backup.create()
        os.utime(result['path'], (1000 + index, 1000 + index))
        created.append(result)
    before = _images(mem)
    result = backup.prune(keep=2)
    assert result['batch_complete'] and result['retired_count'] == 2
    assert set(result['retired_operations']) == {row['operation_id'] for row in created[:2]}
    assert result['files_deleted'] == result['bytes_reclaimed'] == result['history_capacity_reclaimed'] == 0
    after = _images(mem)
    assert all(after[name] == raw for name, raw in before.items())
    rows = backup.list_backups()
    assert len(rows) == 4
    assert [row['operation_id'] for row in rows if row['selection_candidate']] == [row['operation_id'] for row in reversed(created[2:])]


def test_run_preserves_failure_facts_without_retry_or_notification(mem, monkeypatch):
    requested = uuid.uuid4().hex
    observed = uuid.uuid4().hex
    calls = []
    def create(**kwargs):
        calls.append(kwargs['operation_id'])
        failure = RuntimeError('owned private failure')
        failure.operation_id = observed
        raise failure
    monkeypatch.setattr(backup, '_create', create)
    result = backup.run(operation_id=requested, deliver_off=False)
    assert calls == [requested] and not result['ok'] and result['stage'] == 'create'
    assert result['requested_operation_id'] == requested
    assert result['failure']['observed_operation_id'] == observed
    assert result['failure']['stage'] == 'create' and result['confirmed_stages'] == []
    assert not result['publication_complete'] and not result['evidence_recorded'] and _no_pair(mem)
    assert 'owned private failure' not in json.dumps(result)


def test_run_local_only_succeeds_and_prunes(mem, monkeypatch):
    monkeypatch.setenv('OLYMPUS_SECRET_KEY', 'owned local workflow key')
    monkeypatch.setattr(config, 'backup_keep', lambda: 0)
    result = backup.run(deliver_off=False)
    assert result['ok'] and result['stage'] == 'complete' and result['status'] == 'local_only'
    assert result['publication_complete'] and result['retirement_batch_complete'] and result['evidence_recorded']
    assert not result['delivery_commands_complete'] and not result['remote_retention_verified']
    assert result['requested_delivery_id'] is None
    assert result['delivery']['status'] == 'explicitly_skipped'
    assert result['retirement']['retired_count'] == 1 and result['retirement']['files_deleted'] == 0
    assert Path(result['path']).is_file() and Path(result['path'] + '.sig.json').is_file()
    assert [row['kind'] for row in result['confirmed_stages']] == ['publication']

"""Actual captured adapters on owned roots; uploader execution stays mocked."""
import hashlib
import json
import os
from pathlib import Path
import uuid

import pytest
from olympus import backup, backup_authority as authority, backup_delivery as delivery
from olympus import backup_management as management, backup_publication as publication
from olympus import backup_restore as recovery, backup_signing as signing, archive_io as aio, config
from olympus.vault_storage import _Directory
from test_backup_delivery import owned, uploader, files, native_io as native


def test_create_deliver_retire_borrow_original_open_roots_and_policy(owned, monkeypatch):
    calls = uploader(monkeypatch); opened = []
    original = publication._Root.__init__
    with authority.capture('run', include_delivery=True, retention=0) as captured:
        def initialize(root, directory, **kwargs):
            assert type(directory) is _Directory and directory is captured._backups
            opened.append(directory)
            return original(root, directory, **kwargs)
        monkeypatch.setattr(publication._Root, '__init__', initialize)
        monkeypatch.setattr(signing, 'capture_policy', lambda **k: pytest.fail('stage recaptured public policy'))
        monkeypatch.setattr(config, 'backup_keep', lambda: pytest.fail('stage recaptured retention policy'))
        monkeypatch.setattr(delivery, '_capture_command', lambda *a: pytest.fail('stage recaptured command'))
        with captured.stage('create'):
            created = backup._create(operation_id=uuid.uuid4().hex, _authority=captured)
        captured.memory.check(); captured._backups.check(); captured.working.check()
        with captured.stage('deliver'):
            sent = delivery._deliver(created['path'], delivery_id=uuid.uuid4().hex, _authority=captured)
        captured.memory.check(); captured._backups.check(); captured.working.check()
        with captured.stage('retire'):
            retired = management._retire_backups(_authority=captured)
        captured.memory.check(); captured._backups.check(); captured.working.check()
        assert sent['delivery_commands_complete'] is True and sent['remote_retention_verified'] is False
        assert retired['batch_complete'] is True and retired['retired_count'] == 1
        assert retired['retired_operations'] == [created['operation_id']]
        receipt = json.loads(native(owned / 'backups' / publication.CONTROL / created['operation_id'] / 'retired.json').read_bytes())
        assert receipt['operation_id'] == created['operation_id'] and receipt['retired'] is True
        assert receipt['files_deleted'] == 0 and receipt['retained_images'] is True
        assert hashlib.sha256(native(created['path']).read_bytes()).hexdigest() == created['sha256']
        assert hashlib.sha256(native(created['path'] + '.sig.json').read_bytes()).hexdigest() == created['signature_sha256']
        assert len(calls) == 2 and len(opened) == 3 and all(value is captured._backups for value in opened)
    assert native(created['path']).is_file() and not captured.active


def test_catalog_restore_borrow_original_live_and_input_authorities(owned, tmp_path, monkeypatch):
    created = backup.create(); target = tmp_path / 'offline'; observed = []
    original = recovery._Root.__init__
    with authority.capture('drill') as captured:
        def initialize(root, destination, **kwargs):
            assert kwargs['live_root'] is captured.memory
            assert kwargs['input_lease'].root is captured._backups
            observed.append(destination)
            return original(root, destination, **kwargs)
        monkeypatch.setattr(recovery._Root, '__init__', initialize)
        monkeypatch.setattr(signing, 'capture_policy', lambda **k: pytest.fail('restore recaptured public policy'))
        with captured.stage('catalog'):
            rows = management._list_backups(_authority=captured)
        assert rows[0]['operation_id'] == created['operation_id'] and rows[0]['selection_candidate'] is True
        with captured.stage('restore'):
            restored = backup._restore(created['path'], into=target, _authority=captured)
        captured.memory.check(); captured._backups.check()
        assert observed == [target]
        assert restored['restore_complete'] is True and restored['consumed_archive_sha256'] == created['sha256']
    assert native(target / 'data.txt').read_bytes() == b'owned delivery image'


def test_create_uses_frozen_absolute_private_selectors_during_transient_callbacks(owned, tmp_path, monkeypatch):
    original_working, other_working = tmp_path / 'a', tmp_path / 'b'
    foreign = tmp_path / 'foreign'
    for path in (original_working, other_working, foreign): native(path).mkdir()
    for parent, value in ((original_working, b'owned original material'), (other_working, b'owned other material')):
        for name in ('seed', 'vault'):
            native(parent / name).write_bytes(value); native(parent / name).chmod(0o600)
    native(foreign / 'sentinel').write_bytes(b'preserve foreign root')
    monkeypatch.chdir(original_working)
    monkeypatch.delenv('OLYMPUS_SIGNING_SEED')
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED_FILE', 'seed')
    monkeypatch.setenv('OLYMPUS_SECRET_KEY_FILE', 'vault')
    original_key, original_vault = signing._key, aio.custody.capture
    callbacks = []
    with authority.capture('run') as captured:
        def key(selected):
            assert selected['OLYMPUS_SIGNING_SEED_FILE'] == str(original_working / 'seed')
            callbacks.append('signing')
            monkeypatch.chdir(other_working); monkeypatch.setattr(config, 'MEMORY_DIR', foreign)
            try: return original_key(selected)
            finally:
                monkeypatch.chdir(original_working); monkeypatch.setattr(config, 'MEMORY_DIR', owned)
        def vault(selected):
            assert selected['OLYMPUS_SECRET_KEY_FILE'] == str(original_working / 'vault')
            callbacks.append('vault')
            monkeypatch.chdir(other_working); monkeypatch.setattr(config, 'MEMORY_DIR', foreign)
            try: return original_vault(selected)
            finally:
                monkeypatch.chdir(original_working); monkeypatch.setattr(config, 'MEMORY_DIR', owned)
        monkeypatch.setattr(signing, '_key', key); monkeypatch.setattr(aio.custody, 'capture', vault)
        with captured.stage('create'):
            created = backup._create(_authority=captured)
        assert callbacks == ['signing', 'vault']
        expected = signing.witness.ed25519.Ed25519PrivateKey.from_private_bytes(hashlib.sha256(b'owned original material').digest())
        assert created['publicKey'] == signing._public(expected) and created['encrypted'] is True
        assert Path(created['path']).parent == owned / 'backups'
    assert files(foreign) == {'sentinel': b'preserve foreign root'}
    assert files(other_working) == {'seed': b'owned other material', 'vault': b'owned other material'}


def test_frozen_command_environment_survives_transient_resolution_callback(owned, tmp_path, monkeypatch):
    foreign = tmp_path / 'foreign'; native(foreign).mkdir()
    native(foreign / 'sentinel').write_bytes(b'preserve')
    original = delivery._capture_command; callbacks = []
    original_cwd = os.getcwd()
    def command(selected, cwd, environment):
        callbacks.append(cwd)
        monkeypatch.chdir(foreign); monkeypatch.setattr(config, 'MEMORY_DIR', foreign)
        try: return original(selected, cwd, environment)
        finally:
            monkeypatch.chdir(original_cwd); monkeypatch.setattr(config, 'MEMORY_DIR', owned)
    monkeypatch.setattr(delivery, '_capture_command', command)
    calls = uploader(monkeypatch)
    with authority.capture('run', include_delivery=True) as captured:
        frozen = dict(captured.environment)
        monkeypatch.setattr(delivery, '_capture_command', lambda *a: pytest.fail('command was recaptured in stage'))
        monkeypatch.setenv('OWNED_LATE_ENVIRONMENT_SENTINEL', 'not in command snapshot')
        with captured.stage('create'): created = backup._create(_authority=captured)
        with captured.stage('deliver'): sent = delivery._deliver(created['path'], _authority=captured)
    assert callbacks == [original_cwd] and sent['delivery_commands_complete'] is True and len(calls) == 2
    assert all(dict(row[1].environment) == frozen for row in calls)
    assert files(foreign) == {'sentinel': b'preserve'}


def test_restore_input_handoff_retains_original_parent_during_transient_config_change(owned, tmp_path, monkeypatch):
    created = backup.create(); target = tmp_path / 'offline'
    foreign = tmp_path / 'foreign'; native(foreign).mkdir(); native(foreign / 'sentinel').write_bytes(b'preserve')
    original = publication.read_completed; callbacks = []
    with authority.capture('drill') as captured:
        def read(root, name):
            assert root.root is captured._backups
            callbacks.append(name)
            monkeypatch.setattr(config, 'MEMORY_DIR', foreign)
            try: return original(root, name)
            finally: monkeypatch.setattr(config, 'MEMORY_DIR', owned)
        monkeypatch.setattr(publication, 'read_completed', read)
        with captured.stage('restore'):
            restored = backup._restore(created['path'], into=target, _authority=captured)
        captured._backups.check(); captured.memory.check()
    assert callbacks == [Path(created['path']).name]
    assert restored['consumed_archive_sha256'] == created['sha256']
    assert files(foreign) == {'sentinel': b'preserve'}


@pytest.mark.parametrize('stage', ['create', 'deliver', 'catalog', 'retire', 'restore'])
def test_transient_changed_root_at_actual_stage_entry_cannot_write_foreign_root(owned, tmp_path, monkeypatch, stage):
    created = None if stage == 'create' else backup.create()
    foreign = tmp_path / 'foreign'; native(foreign).mkdir(); native(foreign / 'sentinel').write_bytes(b'preserve')
    before = files(owned); called = []
    mode = 'drill' if stage in ('catalog', 'restore') else 'run'
    with authority.capture(mode, include_delivery=mode == 'run') as captured:
        with captured.stage(stage):
            monkeypatch.setattr(config, 'MEMORY_DIR', foreign)
            try:
                with pytest.raises(Exception):
                    called.append(stage)
                    if stage == 'create': backup._create(_authority=captured)
                    elif stage == 'deliver': delivery._deliver(created['path'], _authority=captured)
                    elif stage == 'catalog': management._list_backups(_authority=captured)
                    elif stage == 'retire': management._retire_backups(_authority=captured)
                    else: backup._restore(created['path'], into=tmp_path / 'offline', _authority=captured)
            finally: monkeypatch.setattr(config, 'MEMORY_DIR', owned)
    assert called == [stage] and files(owned) == before
    assert files(foreign) == {'sentinel': b'preserve'}
    assert not native(tmp_path / 'offline').exists()


def test_late_backup_directory_is_not_adopted_by_actual_creation(owned):
    with authority.capture('run') as captured:
        native(owned / 'backups').mkdir(); native(owned / 'backups' / 'foreign').write_bytes(b'preserve')
        before = files(owned)
        with captured.stage('create'):
            with pytest.raises(backup.BackupError): backup._create(_authority=captured)
        assert captured._backups is None
    assert files(owned) == before


@pytest.mark.parametrize('mode,stage,call', [
    ('inspect', 'catalog', lambda context: management._list_backups(_authority=context)),
    ('manage', 'retire', lambda context: management._retire_backups(_authority=context))])
def test_key_free_adapters_pass_no_ambient_private_selectors(owned, monkeypatch, mode, stage, call):
    original = aio.freeze_selectors; selections = []
    monkeypatch.setenv('OLYMPUS_SECRET_KEY_FILE', '.')
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED_FILE', '.')
    def freeze(selectors=None, **kwargs):
        selections.append(selectors)
        assert selectors == {}
        return original(selectors, **kwargs)
    with authority.capture(mode) as captured:
        monkeypatch.setattr(aio, 'freeze_selectors', freeze)
        with captured.stage(stage): call(captured)
        captured.memory.check()
    assert selections == [{}]


def test_private_adapters_reject_inactive_or_wrong_stage_authority(owned, tmp_path):
    with authority.capture('run') as captured:
        with captured.stage('retire'):
            with pytest.raises(backup.BackupError): backup._create(_authority=captured)
            with pytest.raises(authority.AuthorityError): management._list_backups(_authority=captured)
    with pytest.raises(backup.BackupError): backup._create(_authority=captured)
    with pytest.raises(authority.AuthorityError): backup._restore('absent', into=tmp_path / 'offline', _authority=captured)
    assert not native(owned / 'backups').exists() and not native(tmp_path / 'offline').exists()



@pytest.mark.parametrize('which', ['memory', 'backups'])
def test_actual_retirement_cannot_adopt_a_root_replaced_after_creation(owned, which):
    observation = None; created = None; retained = None
    selected = owned if which == 'memory' else owned / 'backups'
    try:
        with authority.capture('run', retention=1) as captured:
            with captured.stage('create'): created = backup._create(_authority=captured)
            retained = selected.with_name(selected.name + '-retained')
            try:
                native(selected).rename(native(retained))
            except OSError as error:
                assert os.name == 'nt' and error.winerror in (5, 32, 33)
                observation = 'native-refusal'
                with captured.stage('retire'):
                    retired = management._retire_backups(_authority=captured)
                assert retired['batch_complete'] is True and retired['retired_count'] == 0
            else:
                observation = 'path-replaced'
                native(selected).mkdir(); native(selected / 'foreign').write_bytes(b'preserve replacement')
                with pytest.raises(Exception):
                    with captured.stage('retire'):
                        management._retire_backups(_authority=captured)
    except Exception:
        assert observation == 'path-replaced'
    assert created is not None and observation in ('native-refusal', 'path-replaced')
    if observation == 'path-replaced':
        assert files(selected) == {'foreign': b'preserve replacement'}
        archive = retained / Path(created['path']).relative_to(selected)
    else:
        archive = Path(created['path'])
    assert hashlib.sha256(native(archive).read_bytes()).hexdigest() == created['sha256']

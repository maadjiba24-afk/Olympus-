"""Owned captured-workflow controls; no uploader or full workflow execution."""
import hashlib
import os
from pathlib import Path
import threading

import pytest
from olympus import archive_io as aio, backup, backup_authority as authority
from olympus import backup_delivery as delivery, backup_signing as signing, config

native = backup._io


@pytest.fixture
def owned(tmp_path, monkeypatch):
    memory, working = tmp_path / 'm', tmp_path / 'w'
    native(memory).mkdir(); native(working).mkdir()
    monkeypatch.setattr(config, 'MEMORY_DIR', memory)
    monkeypatch.chdir(working)
    for name in (*signing._NAMES, 'OLYMPUS_SECRET_KEY', 'OLYMPUS_SECRET_KEY_FILE',
                 'OLYMPUS_BACKUP_CMD', 'OLYMPUS_BACKUP_ALLOW_PLAINTEXT', 'OLYMPUS_BACKUP_KEEP'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('OLYMPUS_ENV', 'test')
    return memory, working


def forbidden(*args, **kwargs):
    pytest.fail('an irrelevant private/policy/command dependency was acquired')


@pytest.mark.parametrize('mode,stage', [
    ('create', 'create'), ('deliver', 'deliver'), ('restore', 'restore'),
    ('run', 'create'), ('drill', 'catalog'), ('manage', 'retire'), ('inspect', 'catalog')])
def test_mode_borrows_original_roots_and_requires_one_active_stage(owned, mode, stage):
    memory, working = owned
    with authority.capture(mode) as captured:
        assert captured.memory.path == memory and captured.working.path == working
        with pytest.raises(authority.AuthorityError):
            captured.require_stage(stage)
        with captured.stage(stage):
            captured.require_stage(stage)
            with pytest.raises(authority.AuthorityError):
                with captured.stage(stage):
                    pytest.fail('a nested stage was admitted')
        assert captured._stage is None
        assert not native(memory / 'backups').exists()
    assert not captured.active
    assert captured.memory is None and captured.working is None and captured._backups is None


@pytest.mark.parametrize('mode', ['manage', 'inspect'])
def test_key_free_actions_ignore_unrelated_invalid_settings(owned, monkeypatch, mode):
    monkeypatch.setenv('OLYMPUS_SECRET_KEY_FILE', '.')
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED_FILE', '.')
    monkeypatch.setenv('OLYMPUS_PINNED_PUBKEY', 'not a pin')
    monkeypatch.setenv('OLYMPUS_BACKUP_CMD', 'not a command')
    monkeypatch.setattr(signing, 'capture_policy', forbidden)
    monkeypatch.setattr(signing, 'capture_signer', forbidden)
    monkeypatch.setattr(delivery, '_capture_environment', forbidden)
    monkeypatch.setattr(delivery, '_capture_command', forbidden)
    monkeypatch.setattr(aio.custody, 'capture', forbidden)
    with authority.capture(mode) as captured:
        assert captured.policy is None and captured.vault_selectors is None
        assert captured.command is None and captured.environment is None
        captured.check()


@pytest.mark.parametrize('enabled', [False, True])
def test_run_delivery_permission_matches_explicit_option(owned, enabled):
    with authority.capture('run', include_delivery=enabled) as captured:
        if enabled:
            with captured.stage('deliver'):
                captured.require_stage('deliver')
        else:
            with pytest.raises(authority.AuthorityError):
                with captured.stage('deliver'):
                    pytest.fail('disabled delivery was admitted')


def test_unconfigured_delivery_defers_unused_private_paths_and_public_policy(owned, monkeypatch):
    monkeypatch.setenv('OLYMPUS_SECRET_KEY_FILE', '.')
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED_FILE', '.')
    monkeypatch.setenv('OLYMPUS_PINNED_PUBKEY', 'invalid unused pin')
    monkeypatch.setattr(signing, 'capture_policy', forbidden)
    monkeypatch.setattr(aio.custody, 'capture', forbidden)
    with authority.capture('deliver') as captured:
        assert captured.command is None and captured.policy is None
        assert captured.excluded == frozenset()


def test_configured_delivery_resolves_command_before_public_policy_without_private_reads(owned, monkeypatch):
    calls = []
    monkeypatch.setenv('OLYMPUS_SECRET_KEY_FILE', '.')
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED_FILE', '.')
    original = signing.capture_policy
    def command(selected, cwd, environment):
        calls.append('command')
        assert not any(name.upper() in delivery._PRIVATE_ENV for name in environment)
        return ('owned command sentinel',)
    def policy(**kwargs):
        calls.append('policy')
        return original(**kwargs)
    monkeypatch.setattr(delivery, '_capture_command', command)
    monkeypatch.setattr(signing, 'capture_policy', policy)
    monkeypatch.setattr(signing, '_read_seed', forbidden)
    monkeypatch.setattr(aio.custody, '_read_file', forbidden)
    with authority.capture('deliver') as captured:
        assert captured.policy is not None and captured.excluded == frozenset()
    assert calls == ['command', 'policy']


def test_independently_appearing_backups_child_is_never_adopted(owned):
    memory, _ = owned
    with authority.capture('create') as captured:
        native(memory / 'backups').mkdir()
        witness = memory / 'backups' / 'foreign'
        native(witness).write_bytes(b'preserve separately owned bytes')
        with captured.stage('create'):
            with pytest.raises(FileExistsError):
                captured.backups(create=True)
            assert captured._backups is None and captured._backups_absent is True
        assert native(witness).read_bytes() == b'preserve separately owned bytes'
        assert [p.name for p in native(memory / 'backups').iterdir()] == ['foreign']


def test_only_creation_can_create_the_absent_child(owned):
    memory, _ = owned
    with authority.capture('run') as captured:
        with captured.stage('retire'):
            with pytest.raises(authority.AuthorityError):
                captured.backups(create=True)
        assert not native(memory / 'backups').exists()
        with captured.stage('create'):
            first = captured.backups(create=True)
            assert captured.backups(create=True) is first
        with captured.stage('retire'):
            assert captured.backups() is first


@pytest.mark.parametrize('which', ['memory', 'backups'])
def test_original_native_root_or_child_is_retained_between_stages(owned, which):
    memory, _ = owned
    child = memory / 'backups'; native(child).mkdir()
    selected = memory if which == 'memory' else child
    retained = selected.with_name(selected.name + '-retained')
    observation = None
    try:
        with authority.capture('run') as captured:
            with captured.stage('create'):
                assert captured.backups() is captured._backups
            try:
                native(selected).rename(native(retained))
            except OSError as error:
                assert os.name == 'nt' and error.winerror in (5, 32, 33)
                observation = 'native-refusal'
                with captured.stage('retire'):
                    captured.require_stage('retire')
            else:
                observation = 'path-replaced'
                native(selected).mkdir()
                native(selected / 'foreign').write_bytes(b'foreign')
                with pytest.raises(Exception):
                    with captured.stage('retire'):
                        pytest.fail('a replacement root reached the next stage')
    except Exception:
        assert observation == 'path-replaced'
    assert observation in ('native-refusal', 'path-replaced')
    if observation == 'path-replaced':
        assert native(selected / 'foreign').read_bytes() == b'foreign'
        assert native(retained).is_dir()


def test_relative_selectors_are_frozen_before_callbacks(owned, monkeypatch):
    _, working = owned
    key = working / 'key'; native(key).write_bytes(b'owned custody'); native(key).chmod(0o600)
    monkeypatch.setenv('OLYMPUS_SECRET_KEY_FILE', 'key')
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED_FILE', 'key')
    with authority.capture('run') as captured:
        assert captured.vault_selectors['OLYMPUS_SECRET_KEY_FILE'] == str(key)
        assert captured.excluded == frozenset((key,))
        assert captured.policy.cwd == str(working)
        with captured.stage('create'):
            assert captured.bind_file('archive') == working / 'archive'


@pytest.mark.parametrize('value', ['', '.', os.path.abspath(os.sep)])
def test_file_binding_rejects_original_empty_leaf(owned, value):
    with authority.capture('inspect') as captured:
        with pytest.raises(aio.ArchiveError):
            captured.bind_file(value)


def test_changed_selection_refuses_stage_and_never_rebinds_authority(owned, tmp_path, monkeypatch):
    memory, _ = owned
    foreign = tmp_path / 'foreign'; native(foreign).mkdir()
    with authority.capture('run') as captured:
        monkeypatch.setattr(config, 'MEMORY_DIR', foreign)
        with pytest.raises(authority.AuthorityError):
            with captured.stage('create'):
                pytest.fail('changed selection reached stage')
        assert captured.memory.path == memory
        monkeypatch.setattr(config, 'MEMORY_DIR', memory)
        with captured.stage('create'):
            assert captured.memory.path == memory
    assert list(native(foreign).iterdir()) == []


def test_foreign_thread_exit_cannot_clear_or_close_owner_stage(owned):
    failures = []
    with authority.capture('run') as captured:
        stage = captured.stage('create'); stage.__enter__()
        def foreign():
            for action in (captured.check, lambda: stage.__exit__(None, None, None), captured.close):
                try:
                    action()
                except BaseException as error:
                    failures.append(error)
        thread = threading.Thread(target=foreign)
        thread.start(); thread.join(timeout=10)
        assert not thread.is_alive()
        assert len(failures) == 3 and all(type(error) is authority.AuthorityError for error in failures)
        assert captured._stage == 'create' and captured.active
        with pytest.raises(authority.AuthorityError):
            with captured.stage('retire'):
                pytest.fail('foreign exit released another thread stage')
    assert captured._stage is None and not captured.active


def test_inactive_context_and_selectors_cannot_escape_cleanup(owned, monkeypatch):
    monkeypatch.setenv('OLYMPUS_SECRET_KEY', 'owned private value')
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED', 'owned private signing value')
    with authority.capture('run', include_delivery=True) as captured:
        assert 'owned private' not in repr(captured)
    for name in ('vault_selectors', 'raw_vault_selection', 'signing_selection', 'environment', 'policy', 'command'):
        assert getattr(captured, name) is None
    with pytest.raises(authority.AuthorityError):
        captured.check()


def test_cleanup_drains_all_handles_without_replacing_primary_interrupt(owned, monkeypatch):
    memory, _ = owned; native(memory / 'backups').mkdir()
    original = authority._Directory.close
    closed = []
    stop = KeyboardInterrupt('owned interruption')
    def close(directory):
        original(directory)
        closed.append(directory.path)
        raise OSError('owned secondary private failure')
    with pytest.raises(KeyboardInterrupt) as caught:
        with authority.capture('run') as captured:
            monkeypatch.setattr(authority._Directory, 'close', close)
            raise stop
    assert caught.value is stop
    assert closed == [memory / 'backups', memory, owned[1]]
    assert not captured.active and captured.memory is None


def test_creation_detects_private_file_identity_change_between_stages(owned, monkeypatch):
    memory, working = owned
    key = working / 'key'; native(key).write_bytes(b'owned key'); native(key).chmod(0o600)
    monkeypatch.setenv('OLYMPUS_SECRET_KEY_FILE', str(key))
    replaced = False
    with pytest.raises(Exception):
        with authority.capture('run') as captured:
            native(key).rename(native(working / 'retained-key'))
            native(key).write_bytes(b'changed key'); native(key).chmod(0o600)
            replaced = True
            with captured.stage('create'):
                pytest.fail('changed private file reached creation')
    assert replaced
    assert native(working / 'retained-key').read_bytes() == b'owned key'
    assert native(key).read_bytes() == b'changed key'
    assert not native(memory / 'backups').exists()


def test_invalid_dot_selector_stays_invalid_after_its_old_directory_becomes_a_file(owned, monkeypatch):
    _, working = owned
    captured = aio.freeze_selectors({'OLYMPUS_SECRET_KEY_FILE': '.', 'OLYMPUS_ENV': 'test'}, cwd=str(working))
    assert type(captured['OLYMPUS_SECRET_KEY_FILE']) is not str
    monkeypatch.chdir(working.parent)
    native(working).rename(native(working.with_name('retained-working')))
    native(working).write_bytes(b'owned private file replacement'); native(working).chmod(0o600)
    monkeypatch.setattr(aio.custody, '_read_file', forbidden)
    with aio.operation('backup', selectors=captured, cwd=str(working), capture_key=False) as session:
        assert not session.key_selected
        with pytest.raises(Exception):
            session.material()
        with pytest.raises(aio.ArchiveError, match='remains unavailable'):
            session.material()


def test_explicit_signer_uses_captured_cwd_and_public_pin_snapshot(owned, tmp_path, monkeypatch):
    _, working = owned
    native(working / 'seed').write_bytes(b'original owned signing seed'); native(working / 'seed').chmod(0o600)
    other = tmp_path / 'other'; native(other).mkdir()
    native(other / 'seed').write_bytes(b'other owned signing seed'); native(other / 'seed').chmod(0o600)
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED_FILE', 'seed')
    selected = signing._selection()
    policy = signing.capture_policy(selection=selected, cwd=str(working))
    monkeypatch.chdir(other)
    monkeypatch.setattr(signing, '_pins', forbidden)
    signer = signing.capture_signer(selection=selected, cwd=str(working), policy=policy)
    expected = signing.witness.ed25519.Ed25519PrivateKey.from_private_bytes(hashlib.sha256(b'original owned signing seed').digest())
    assert signer.public_key == signing._public(expected)
    digest = '1' * 64
    assert signer.verify(digest, signer.signature(digest))['signer_authenticated'] is True


def test_signer_rejects_mismatched_policy_before_private_capture(owned, tmp_path, monkeypatch):
    selected = signing._selection()
    policy = signing.capture_policy(selection=selected, cwd=str(owned[1]))
    monkeypatch.setattr(signing, '_key', forbidden)
    with pytest.raises(signing.SignatureError):
        signing.capture_signer(selection=selected, cwd=str(tmp_path), policy=policy)


def test_signing_dot_selector_refuses_before_private_file_read(owned, monkeypatch):
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED_FILE', '.')
    monkeypatch.setattr(signing, '_read_seed', forbidden)
    with pytest.raises(signing.SignatureError):
        signing.capture_signer(selection=signing._selection(), cwd=str(owned[1]))

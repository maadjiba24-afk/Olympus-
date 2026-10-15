"""Owned delivery journal controls; uploader dispatch is always mocked."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
import uuid

import pytest

from olympus import backup, backup_delivery as delivery, backup_process as process
from olympus import backup_publication as publication, backup_signing as signing
from olympus import archive_io as aio, config, witness
from olympus.vault_storage import _Directory


@pytest.fixture
def owned(tmp_path, monkeypatch):
    live = tmp_path / 'memory'
    live.mkdir()
    (native_io(live / 'data.txt')).write_bytes(b'owned delivery image')
    package = tmp_path / 'package'
    package.mkdir()
    monkeypatch.setattr(config, 'MEMORY_DIR', live)
    monkeypatch.setattr(witness, '_package_dir', lambda: package)
    for name in (*signing._NAMES, 'OLYMPUS_SECRET_KEY', 'OLYMPUS_SECRET_KEY_FILE',
                 'OLYMPUS_BACKUP_CMD', 'OLYMPUS_BACKUP_ALLOW_PLAINTEXT'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('OLYMPUS_ENV', 'test')
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED', 'owned delivery signing material')
    monkeypatch.setenv('OLYMPUS_BACKUP_ALLOW_PLAINTEXT', '1')
    monkeypatch.setenv('OLYMPUS_BACKUP_CMD', shlex.quote(sys.executable) + ' {path}')
    monkeypatch.setattr(process, 'execute', lambda *a, **k: pytest.fail('unconfigured uploader dispatch'))
    return live


def native_io(path):
    """Use extended Win32 names only for owned fixture I/O, never product inputs."""
    path = Path(path)
    if os.name == 'nt':
        return backup._io(path)
    return path


def files(root):
    root = native_io(root)
    return {path.relative_to(root).as_posix(): native_io(path).read_bytes()
            for path in native_io(root).rglob('*') if native_io(path).is_file()}


def operation(created):
    return Path(created['path']).parent / publication.CONTROL / created['operation_id']


def outcome(**overrides):
    return {'exit_code': 0, 'timed_out': False, 'natural_tree_exit': True,
        'cleanup_confirmed': True, 'handles_closed': True, 'reconciliation_required': False,
        'pid': 12345, 'release_attempted': True, 'release_confirmed': True, **overrides}


def native():
    if os.name != 'nt':
        return {'pid': 12345, 'pgid': 12345, 'sid': 12345, 'start_ticks': 101,
            'gate_before_exec': True, 'exclusive_waiter_required': True}
    return {'pid': 12345, 'created_suspended': True, 'job_assigned_before_resume': True,
        'kill_on_job_close': True, 'breakaway_enabled': False, 'only_owned_standard_handles_inherited': True}


def uploader(monkeypatch, callback=None):
    calls = []
    def execute(command, *, record_started, check_authority):
        check_authority()
        path = Path(command.argv[-1])
        component = 'signature' if path.name.endswith('.sig.json') else 'archive'
        control = path.parent
        assert control.name == delivery.CONTROL
        assert (native_io(control / (component + '-intent.json'))).is_file()
        if component == 'signature':
            assert json.loads((native_io(control / 'archive-outcome.json')).read_bytes())['outcome'] == outcome()
        record_started(native())
        check_authority()
        calls.append((component, command, native_io(path).read_bytes()))
        return outcome() if callback is None else callback(component, command, calls)
    monkeypatch.setattr(process, 'execute', execute)
    return calls


@pytest.mark.parametrize('encrypted', [False, True])
def test_completed_pair_preserves_basenames_bytes_and_truthful_scope(owned, monkeypatch, encrypted):
    if encrypted:
        monkeypatch.setenv('OLYMPUS_SECRET_KEY', 'owned encryption material for delivery')
        monkeypatch.delenv('OLYMPUS_BACKUP_ALLOW_PLAINTEXT')
    created = backup.create()
    path = Path(created['path'])
    original = (native_io(path).read_bytes(), native_io(path.with_name(path.name + '.sig.json')).read_bytes())
    calls = uploader(monkeypatch)
    did = uuid.uuid4().hex
    result = backup.deliver(str(path), delivery_id=did)
    assert result['delivery_commands_complete'] is True
    assert result['remote_retention_verified'] is False and 'delivered' not in result
    assert result['publication_id'] == created['operation_id'] and result['delivery_id'] == did
    assert result['archive_encryption']['encrypted'] is encrypted
    assert result['archive_encryption']['context_authenticated'] is encrypted
    assert result['signer_authenticated'] and not result['signer_default']
    assert [row[0] for row in calls] == ['archive', 'signature']
    assert [Path(row[1].argv[-1]).name for row in calls] == [path.name, path.name + '.sig.json']
    assert tuple(row[2] for row in calls) == original
    assert result['intended_archive_sha256'] == hashlib.sha256(original[0]).hexdigest()
    for _, command, _ in calls:
        assert not (set(key.upper() for key in command.environment) & delivery._PRIVATE_ENV)
    before = files(owned)
    monkeypatch.setattr(signing, 'capture_policy', lambda **k: pytest.fail('inspection acquired signer'))
    monkeypatch.setattr(aio.custody, 'capture', lambda *a, **k: pytest.fail('inspection acquired vault key'))
    monkeypatch.setattr(aio.ArchiveSession, 'decrypt', lambda *a, **k: pytest.fail('inspection decrypted payload'))
    observed = backup.inspect_delivery(str(path))
    assert observed['status'] == 'completion_visible' and observed['delivery_id'] == did
    assert observed['delivery_commands_complete'] is None
    assert observed['completion_barriers_reconfirmed'] is False
    assert observed['remote_retention_verified'] is False and observed['observational_only']
    assert observed['staged_bytes_compared_with_publication_images'] and not observed['verification_reperformed']
    assert files(owned) == before and len(calls) == 2


def test_not_configured_does_not_allocate_attempt_evidence(owned, monkeypatch):
    created = backup.create()
    monkeypatch.delenv('OLYMPUS_BACKUP_CMD')
    before = files(owned)
    result = backup.deliver(created['path'])
    assert result['status'] == 'not_configured' and not result['delivery_commands_complete']
    assert result['delivery_id'] is None and not result['remote_retention_verified']
    assert files(owned) == before


@pytest.mark.parametrize('policy', ['plaintext_unapproved', 'public_default', 'untrusted_signer'])
def test_policy_refuses_before_fixed_attempt_directory(owned, monkeypatch, policy):
    if policy == 'public_default':
        monkeypatch.delenv('OLYMPUS_SIGNING_SEED')
    created = backup.create()
    if policy == 'plaintext_unapproved':
        monkeypatch.delenv('OLYMPUS_BACKUP_ALLOW_PLAINTEXT')
    elif policy == 'untrusted_signer':
        monkeypatch.setenv('OLYMPUS_SIGNING_SEED', 'different owned signing identity')
    with pytest.raises(backup.BackupError) as caught:
        backup.deliver(created['path'])
    assert caught.value.publication_id == created['operation_id']
    assert not (native_io(operation(created) / delivery.CONTROL)).exists()
    assert caught.value.__cause__ is None and caught.value.__context__ is None


@pytest.mark.parametrize('name,expected_calls', [
    ('plan.json', 0), ('delivery-claim.json', 0), ('archive-intent.json', 0),
    ('archive-started.json', 0), ('archive-outcome.json', 1),
    ('signature-intent.json', 1), ('signature-started.json', 1),
    ('signature-outcome.json', 2), ('complete.json', 2),
])
@pytest.mark.parametrize('after_write', [False, True])
def test_failed_receipt_ack_keeps_one_attempt_and_never_retries(owned, monkeypatch, name, expected_calls, after_write):
    created = backup.create()
    calls = uploader(monkeypatch)
    original = _Directory.write_new
    fired = []
    def write(directory, leaf, raw):
        relevant = directory.path.name == delivery.CONTROL or leaf == delivery.CLAIM
        if relevant and leaf == name and not fired:
            fired.append(True)
            if after_write:
                original(directory, leaf, raw)
            raise OSError('owned secret marker must not escape public failure')
        return original(directory, leaf, raw)
    monkeypatch.setattr(_Directory, 'write_new', write)
    did = uuid.uuid4().hex
    with pytest.raises(backup.BackupError) as caught:
        backup.deliver(created['path'], delivery_id=did)
    assert fired == [True] and len(calls) == expected_calls
    error = caught.value
    assert error.publication_id == created['operation_id'] and error.delivery_id == did
    assert error.delivery_progress['completion_receipt_confirmed'] is False
    assert 'secret marker' not in str(error) and error.__context__ is None and error.__cause__ is None
    assert (native_io(operation(created) / delivery.CONTROL)).is_dir()
    monkeypatch.setattr(_Directory, 'write_new', original)
    before = files(owned)
    for requested in (did, uuid.uuid4().hex):
        with pytest.raises(backup.BackupError):
            backup.deliver(created['path'], delivery_id=requested)
    assert files(owned) == before and len(calls) == expected_calls
    observed = backup.inspect_delivery(created['path'])
    assert observed['attempt_evidence_visible'] and not observed['automatic_retry_permitted']
    assert observed['delivery_commands_complete'] is None
    if name == 'complete.json' and after_write:
        assert observed['status'] == 'completion_visible'
    else:
        assert observed['status'] != 'completion_visible'
        second = backup.create()
        with pytest.raises(backup.BackupError) as blocked:
            backup.deliver(second['path'])
        assert blocked.value.delivery_progress['blocking_publication_id_observed'] == created['operation_id']
        assert len(calls) == expected_calls and not (native_io(operation(second) / delivery.CONTROL)).exists()


@pytest.mark.parametrize('change', [
    {'exit_code': 7},
    {'cleanup_confirmed': False, 'reconciliation_required': True},
    {'handles_closed': False, 'reconciliation_required': True},
    {'exit_code': None, 'natural_tree_exit': False, 'timed_out': True, 'reconciliation_required': True},
])
def test_archive_failure_or_unknown_cleanup_stops_signature_and_future_publications(owned, monkeypatch, change):
    created = backup.create()
    calls = uploader(monkeypatch, lambda *_: outcome(**change))
    with pytest.raises(backup.BackupError) as caught:
        backup.deliver(created['path'])
    assert len(calls) == 1
    progress = caught.value.delivery_progress['components']['archive']
    assert progress['observed_outcome'] == outcome(**change) and progress['outcome_receipt_confirmed']
    assert not (native_io(operation(created) / delivery.CONTROL / 'signature-intent.json')).exists()
    second = backup.create()
    with pytest.raises(backup.BackupError):
        backup.deliver(second['path'])
    assert len(calls) == 1


def test_unknown_process_exception_retains_bounded_outcome_without_provider_text(owned, monkeypatch):
    created = backup.create()
    expected = outcome(exit_code=None, natural_tree_exit=False, cleanup_confirmed=False,
        handles_closed=False, reconciliation_required=True)
    def fail(*_):
        error = RuntimeError('private provider error')
        error.process_outcome = expected
        raise error
    calls = uploader(monkeypatch, fail)
    with pytest.raises(backup.BackupError) as caught:
        backup.deliver(created['path'])
    assert len(calls) == 1 and 'private provider' not in str(caught.value)
    assert caught.value.delivery_progress['components']['archive']['observed_outcome'] == expected
    assert caught.value.__context__ is None and caught.value.__cause__ is None


def test_control_flow_exception_identity_and_partial_outcome_are_preserved(owned, monkeypatch):
    created = backup.create()
    error = KeyboardInterrupt()
    error.process_outcome = outcome(exit_code=None, natural_tree_exit=False, timed_out=True)
    def fail(*_):
        raise error
    calls = uploader(monkeypatch, fail)
    with pytest.raises(KeyboardInterrupt) as caught:
        backup.deliver(created['path'])
    assert caught.value is error and len(calls) == 1
    assert error.publication_id == created['operation_id'] and len(error.delivery_id) == 32
    assert error.delivery_progress['components']['archive']['observed_outcome'] == error.process_outcome


@pytest.mark.parametrize('witness_name', [delivery.CLAIM, delivery.CONTROL])
def test_single_missing_witness_never_reopens_dispatch(owned, monkeypatch, witness_name):
    created = backup.create()
    calls = uploader(monkeypatch)
    backup.deliver(created['path'])
    selected = operation(created) / witness_name
    native_io(selected).rename(native_io(selected.with_name('retained-owned-' + witness_name)))
    before = files(owned)
    with pytest.raises(backup.BackupError):
        backup.deliver(created['path'])
    with pytest.raises(backup.BackupError):
        backup.inspect_delivery(created['path'])
    assert len(calls) == 2 and files(owned) == before


def test_completed_historical_and_retired_pairs_admit_next_publication_without_payload_reads(owned, monkeypatch):
    first = backup.create()
    calls = uploader(monkeypatch)
    backup.deliver(first['path'])
    assert backup.prune(keep=0)['retired_count'] == 1
    second = backup.create()
    previous = operation(first)
    original_read, original_io = _Directory.read_with_identity, aio.read_file
    def read(directory, name, cap):
        if previous in directory.path.parents or directory.path == previous:
            assert name.endswith('.json') and name != Path(first['path']).name + '.sig.json'
        return original_read(directory, name, cap)
    def read_file(path, *args, **kwargs):
        assert previous not in Path(path).parents
        return original_io(path, *args, **kwargs)
    monkeypatch.setattr(_Directory, 'read_with_identity', read)
    monkeypatch.setattr(aio, 'read_file', read_file)
    result = backup.deliver(second['path'])
    assert result['delivery_commands_complete'] and len(calls) == 4


def test_historical_metadata_budget_refuses_before_new_slot_or_dispatch(owned, monkeypatch):
    first = backup.create()
    calls = uploader(monkeypatch)
    backup.deliver(first['path'])
    second = backup.create()
    monkeypatch.setattr(delivery, 'MAX_SCAN_BYTES', 1)
    with pytest.raises(backup.BackupError):
        backup.deliver(second['path'])
    assert len(calls) == 2 and not (native_io(operation(second) / delivery.CONTROL)).exists()


def test_selected_plaintext_permission_cannot_be_granted_by_executable_resolution_callback(owned, monkeypatch):
    created = backup.create()
    monkeypatch.delenv('OLYMPUS_BACKUP_ALLOW_PLAINTEXT')
    original = delivery._capture_command
    def capture(selected, cwd, environment):
        monkeypatch.setenv('OLYMPUS_BACKUP_ALLOW_PLAINTEXT', '1')
        result = original(selected, cwd, environment)
        monkeypatch.delenv('OLYMPUS_BACKUP_ALLOW_PLAINTEXT')
        return result
    monkeypatch.setattr(delivery, '_capture_command', capture)
    with pytest.raises(backup.BackupError):
        backup.deliver(created['path'])
    assert not (native_io(operation(created) / delivery.CONTROL)).exists()


def test_configuration_drift_before_dispatch_refuses_without_new_slot(owned, monkeypatch, tmp_path):
    created = backup.create()
    alternate = tmp_path / 'alternate'
    alternate.mkdir()
    before = files(alternate)
    original = delivery._capture_command
    def capture(selected, cwd, environment):
        result = original(selected, cwd, environment)
        monkeypatch.setattr(config, 'MEMORY_DIR', alternate)
        return result
    monkeypatch.setattr(delivery, '_capture_command', capture)
    with pytest.raises(backup.BackupError):
        backup.deliver(created['path'])
    assert not (native_io(operation(created) / delivery.CONTROL)).exists() and files(alternate) == before


def test_changed_stage_bytes_stop_signature_after_archive_observation(owned, monkeypatch):
    created = backup.create()
    def change(component, command, calls):
        path = Path(command.argv[-1])
        native_io(path).write_bytes(b'owned replacement staged image')
        return outcome()
    calls = uploader(monkeypatch, change)
    with pytest.raises(backup.BackupError):
        backup.deliver(created['path'])
    assert len(calls) == 1 and not (native_io(operation(created) / delivery.CONTROL / 'signature-intent.json')).exists()


def test_publication_retirement_cannot_enter_while_command_pair_holds_authority(owned, monkeypatch):
    created = backup.create()
    attempts = []
    refused_locks = []
    original_lock = publication._try_lock
    def acquire(fd):
        result = original_lock(fd)
        if not result:
            refused_locks.append(True)
        return result
    monkeypatch.setattr(publication, '_try_lock', acquire)
    def contend(component, command, calls):
        with pytest.raises(publication.PublicationError):
            publication.retire(Path(created['path']).parent, operation_id=created['operation_id'])
        attempts.append(component)
        return outcome()
    calls = uploader(monkeypatch, contend)
    assert backup.deliver(created['path'])['delivery_commands_complete']
    assert attempts == ['archive', 'signature'] and len(calls) == 2
    assert refused_locks == [True, True]
    assert not (native_io(operation(created) / 'retirement-intent.json')).exists()


@pytest.mark.parametrize('move', ['uppercase', 'outside_control', 'renamed_child'])
def test_unresolved_history_cannot_be_hidden_while_final_pair_remains(owned, monkeypatch, tmp_path, move):
    first = backup.create(operation_id='a' * 32)
    calls = uploader(monkeypatch, lambda *_: outcome(exit_code=2))
    with pytest.raises(backup.BackupError):
        backup.deliver(first['path'])
    second = backup.create()
    prior = operation(first)
    if move == 'uppercase':
        native_io(prior).rename(native_io(prior.with_name(prior.name.upper())))
    elif move == 'outside_control':
        native_io(prior).rename(native_io(tmp_path / 'retained-owned-operation'))
    else:
        (native_io(prior / delivery.CONTROL)).rename(native_io(prior / 'retained-owned-delivery'))
        (native_io(prior / delivery.CLAIM)).rename(native_io(prior / 'retained-owned-claim.json'))
    before = files(owned)
    with pytest.raises(backup.BackupError):
        backup.deliver(second['path'])
    assert len(calls) == 1 and files(owned) == before
    assert not (native_io(operation(second) / delivery.CONTROL)).exists()


def test_retirement_does_not_hide_unresolved_delivery(owned, monkeypatch):
    first = backup.create()
    calls = uploader(monkeypatch, lambda *_: outcome(exit_code=3))
    with pytest.raises(backup.BackupError):
        backup.deliver(first['path'])
    assert backup.prune(keep=0)['retired_count'] == 1
    second = backup.create()
    with pytest.raises(backup.BackupError) as caught:
        backup.deliver(second['path'])
    assert caught.value.delivery_progress['blocking_publication_id_observed'] == first['operation_id']
    assert len(calls) == 1 and not (native_io(operation(second) / delivery.CONTROL)).exists()


def test_invalid_writer_metadata_refuses_before_intent_or_dispatch(owned, monkeypatch):
    created = backup.create()
    monkeypatch.setattr(delivery.time, 'time', lambda: -1)
    with pytest.raises(backup.BackupError):
        backup.deliver(created['path'])
    control = operation(created) / delivery.CONTROL
    assert native_io(control).is_dir() and not (native_io(control / 'plan.json')).exists()
    assert not (native_io(control / 'archive-intent.json')).exists()


@pytest.mark.parametrize('invalid', ['no_started_receipt', 'different_pid', 'boolean_exit_code'])
def test_invalid_native_outcome_never_authorizes_signature(owned, monkeypatch, invalid):
    created = backup.create()
    calls = []
    def execute(command, *, record_started, check_authority):
        calls.append(True)
        check_authority()
        if invalid != 'no_started_receipt':
            record_started(native())
        return outcome(**({'pid': 54321} if invalid == 'different_pid' else
                          {'exit_code': False} if invalid == 'boolean_exit_code' else {}))
    monkeypatch.setattr(process, 'execute', execute)
    with pytest.raises(backup.BackupError):
        backup.deliver(created['path'])
    assert calls == [True]
    assert not (native_io(operation(created) / delivery.CONTROL / 'signature-intent.json')).exists()


@pytest.mark.parametrize('leaf', ['head.json', 'progress.json'])
def test_resumed_publication_keeps_bounded_staging_and_can_deliver(owned, monkeypatch, leaf):
    # Fail the actual publication after the native stage has been fsynced,
    # while retaining the original head/operation evidence for same-ID resume.
    op = uuid.uuid4().hex
    fired = []
    if os.name == 'nt':
        original = aio._windows_publish_rename
        def rename(directory, fd, name, expected):
            # head.json is first published as active intent. Fail its second
            # publication (barrier confirmation), so the ID remains resumable.
            if name == leaf:
                fired.append(name)
                if leaf != 'head.json' or len(fired) == 2:
                    raise OSError('owned native publication boundary failure')
            return original(directory, fd, name, expected)
        monkeypatch.setattr(aio, '_windows_publish_rename', rename)
    else:
        original = aio.os.replace
        def rename(src, dst, *args, **kwargs):
            if dst == leaf and 'src_dir_fd' in kwargs:
                fired.append(dst)
                if leaf != 'head.json' or len(fired) == 2:
                    raise OSError('owned native publication boundary failure')
            return original(src, dst, *args, **kwargs)
        monkeypatch.setattr(aio.os, 'replace', rename)
    with pytest.raises(backup.BackupError):
        backup.create(operation_id=op)
    assert fired == [leaf] * (2 if leaf == 'head.json' else 1)
    retained = {path: native_io(path).read_bytes() for path in (native_io(owned / 'backups')).rglob('.archive-*.tmp')}
    assert len(retained) == 1
    if os.name == 'nt':
        monkeypatch.setattr(aio, '_windows_publish_rename', original)
    else:
        monkeypatch.setattr(aio.os, 'replace', original)
    resumed = publication.resume(owned / 'backups', operation_id=op)
    calls = uploader(monkeypatch)
    assert backup.deliver(resumed['path'])['delivery_commands_complete']
    second = backup.create()
    assert backup.deliver(second['path'])['delivery_commands_complete']
    assert len(calls) == 4 and all(native_io(path).read_bytes() == raw for path, raw in retained.items())


def test_uploader_environment_is_captured_before_executable_resolution_callbacks(owned, monkeypatch):
    created = backup.create()
    monkeypatch.setenv('OWNED_UPLOAD_ROUTE', 'entry-selected-owned-route')
    original = delivery._capture_command
    def capture(selected, cwd, environment):
        monkeypatch.setenv('OWNED_UPLOAD_ROUTE', 'temporarily-changed-owned-route')
        result = original(selected, cwd, environment)
        monkeypatch.setenv('OWNED_UPLOAD_ROUTE', 'entry-selected-owned-route')
        return result
    monkeypatch.setattr(delivery, '_capture_command', capture)
    calls = uploader(monkeypatch)
    assert backup.deliver(created['path'])['delivery_commands_complete']
    assert len(calls) == 2
    assert all(command.environment['OWNED_UPLOAD_ROUTE'] == 'entry-selected-owned-route'
               for _, command, _ in calls)

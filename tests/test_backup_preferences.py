"""Owned preference/backup authority controls; no delivery or live credentials."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import threading
import os
import subprocess
import sys
import time

import pytest

from olympus import archive_io as io, backup, backup_archive as fmt
from olympus import backup_authority as authority, backup_commands as commands
from olympus import backup_facts as facts, backup_publication as publication
from olympus import backup_restore as restore, backup_signing as signing
from olympus import backup_snapshot as snapshot, backup_workflow as workflow
from olympus import config, prefs, prefs_native as native
from olympus.vault_storage import _Directory

OWNER = 'owned-backup-preferences'
OP = '1' * 32


@pytest.fixture
def owned(tmp_path, monkeypatch):
    root = tmp_path / 'owned-memory'
    root.mkdir()
    monkeypatch.setattr(config, 'MEMORY_DIR', root)
    monkeypatch.setattr(native, '_GATE', threading.RLock())
    monkeypatch.setattr(native, '_LOCAL', threading.local())
    monkeypatch.setattr(native, '_IN_FLIGHT', False)
    monkeypatch.setattr(native, '_POISONED', False)
    monkeypatch.setattr(prefs, '_TRANSFORM', threading.local())
    for name in (*signing._NAMES, 'OLYMPUS_SECRET_KEY', 'OLYMPUS_SECRET_KEY_FILE'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('OLYMPUS_ENV', 'test')
    return root


def _collect(root, *, full=False):
    with io.operation('backup', selectors={}, capture_key=False):
        with closing(_Directory(root)) as held:
            return snapshot.collect(held, full=full, excluded=frozenset())


def _no_pair(root):
    return not (root / 'backups').exists() or not list((root / 'backups').glob('olympus-backup-v2-*'))


RESERVED = ('prefs-recovery', 'Prefs-Recovery', 'prefs.claim.json',
            'prefs/owners/owned/PREFS.CLAIM.JSON', 'users/owned/prefs.claim.json')


@pytest.mark.parametrize('name', RESERVED)
@pytest.mark.parametrize('shape', ['empty-file', 'malformed-file', 'directory'])
@pytest.mark.parametrize('full', [False, True])
def test_collection_refuses_recovery_authority_without_reading_or_stripping_it(owned, monkeypatch, name, shape, full):
    path = owned.joinpath(*name.split('/'))
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = b'' if shape == 'empty-file' else b'{owned-private-invalid-authority'
    if shape == 'directory':
        path.mkdir()
    else:
        path.write_bytes(raw)
    read = io.read_file
    def guarded(selected, *args, **kwargs):
        assert selected != path, 'collection attempted to read reserved recovery authority'
        return read(selected, *args, **kwargs)
    monkeypatch.setattr(io, 'read_file', guarded)
    with pytest.raises(ValueError, match='not portable'):
        _collect(owned, full=full)
    assert path.is_dir() if shape == 'directory' else path.read_bytes() == raw
    assert _no_pair(owned)


@pytest.mark.parametrize('name', (*RESERVED, 'prefs-recovery/operations/owned/source.bin',
                                 'locks/preferences.native.lock', 'LOCKS/PREFERENCES.NATIVE.LOCK'))
def test_restore_refuses_native_authority_before_any_destination_creation(owned, tmp_path, name):
    destination = tmp_path / 'offline'
    evidence = {'archive_sha256': hashlib.sha256(b'owned fixture').hexdigest(),
        'signature_sha256': None, 'signed': False, 'signature_ok': False,
        'signature_override': False, 'archive_encryption': io.encryption_metadata(plaintext=True)}
    with pytest.raises(ValueError, match='cannot transplant'):
        restore.restore(destination, {name: b'owned retained bytes'}, evidence,
                        operation_id=OP, live_root=owned)
    assert not destination.exists() and list(owned.iterdir()) == []


def test_collection_never_reads_held_coordination_file_and_preserves_preference_image(owned, monkeypatch):
    prefs.set(OWNER, 'language', 'owned-language')
    parts = prefs._relative_paths(OWNER)[0]
    expected = owned.joinpath(*parts).read_bytes()
    lock = owned.joinpath(*native.coordination_path())
    read = io.read_file
    def guarded(path, *args, **kwargs):
        assert path != lock, 'archive attempted to read its held preference mutex'
        return read(path, *args, **kwargs)
    monkeypatch.setattr(io, 'read_file', guarded)
    assert _collect(owned) == {'/'.join(parts): expected}
    assert lock.is_file()


@pytest.mark.parametrize('entry', ['create', 'run', 'collect'])
def test_nested_public_entry_refuses_before_authority_or_key_callbacks(owned, monkeypatch, entry):
    def forbidden(*args, **kwargs):
        pytest.fail('nested backup reached authority or key capture')
    monkeypatch.setattr(authority, 'capture', forbidden)
    monkeypatch.setattr(signing, 'capture_signer', forbidden)
    with native.transaction(owned):
        if entry == 'run':
            result = backup.run(operation_id=OP, deliver_off=False)
            assert result['ok'] is False and result['stage'] == 'validation'
            assert not result['publication_complete'] and not result['confirmed_stages']
        else:
            with pytest.raises(backup.BackupError if entry == 'create' else native.PreferenceStorageError):
                backup.create(operation_id=OP) if entry == 'create' else _collect(owned)
    assert _no_pair(owned)


def test_collection_guard_refuses_nested_preference_mutation_from_read_callback(owned, monkeypatch):
    prefs.set(OWNER, 'language', 'before')
    target = owned.joinpath(*prefs._relative_paths(OWNER)[0])
    before = target.read_bytes()
    read = io.read_file
    observed = []
    def callback(path, *args, **kwargs):
        if path == target:
            with pytest.raises(prefs.PreferenceOperationError):
                prefs.set(OWNER, 'language', 'unexpected')
            observed.append(True)
        return read(path, *args, **kwargs)
    monkeypatch.setattr(io, 'read_file', callback)
    images = _collect(owned)
    assert observed == [True] and images['/'.join(prefs._relative_paths(OWNER)[0])] == before
    assert target.read_bytes() == before


def test_root_replacement_before_guard_cannot_initialize_replacement_coordination(owned, monkeypatch):
    (owned / 'original').write_bytes(b'owned original')
    retained = owned.with_name('retained-original')
    constructor = native.Transaction
    switched = []
    def replace_root(selected, binding=None, **kwargs):
        try:
            owned.rename(retained)
        except OSError as error:
            import os
            assert os.name == 'nt' and error.winerror in (5, 32)
            switched.append('native-refusal')
            raise
        switched.append('replaced')
        owned.mkdir()
        return constructor(selected, binding, **kwargs)
    monkeypatch.setattr(native, 'Transaction', replace_root)
    with pytest.raises((native.PreferenceStorageError, OSError)):
        _collect(owned)
    assert len(switched) == 1
    if switched == ['replaced']:
        assert list(owned.iterdir()) == []
        assert (retained / 'original').read_bytes() == b'owned original'
    else:
        assert (owned / 'original').read_bytes() == b'owned original'
        assert not (owned / 'locks').exists()


@pytest.mark.parametrize('entry', ['create', 'run'])
@pytest.mark.parametrize('fault_type', [OSError, KeyboardInterrupt, SystemExit])
def test_cleanup_uncertainty_survives_public_result_and_command_projection(owned, monkeypatch, entry, fault_type):
    (owned / 'data').write_bytes(b'owned source')
    close = native.Transaction.close
    fault = fault_type('owned-private-cleanup')
    fault.__notes__ = 123
    closed, collected = [], []
    collect_locked = snapshot._collect_locked
    def completed_collection(*args, **kwargs):
        result = collect_locked(*args, **kwargs)
        tx = native._LOCAL.current
        assert tx.held and not tx.closed
        collected.append(result)
        return result
    monkeypatch.setattr(snapshot, '_collect_locked', completed_collection)
    def failed_ack(tx):
        reached_collection = tx.held and not tx.closed and collected == [{'data': b'owned source'}]
        close(tx)  # Resources really close before the injected lost acknowledgment.
        assert reached_collection and tx.closed and not tx.held
        closed.append(True)
        raise fault
    monkeypatch.setattr(native.Transaction, 'close', failed_ack)
    monkeypatch.setattr(publication, 'publish', lambda *a, **k: pytest.fail('cleanup failure reached publication'))
    if entry == 'run' and fault_type is OSError:
        result = backup.run(operation_id=OP, deliver_off=False)
        assert result['ok'] is False and not result['publication_complete']
        assert result['failure']['preference_cleanup_unconfirmed'] is True
        projected = commands._workflow_report(result)
        assert projected['failure']['preference_cleanup_unconfirmed'] is True
        rendered = commands.scheduled_status(result)
        assert rendered['ok'] is False and rendered['preference_cleanup_unconfirmed'] is True
        fresh = workflow.WorkflowError(result)
        assert commands._failure(fresh, OP)['preference_cleanup_unconfirmed'] is True
    else:
        expected = backup.BackupError if fault_type is OSError else fault_type
        with pytest.raises(expected) as caught:
            backup.create(operation_id=OP) if entry == 'create' else backup.run(operation_id=OP, deliver_off=False)
        if fault_type is not OSError:
            assert caught.value is fault
        assert native.cleanup_unknown(caught.value)
        projected = commands._failure(caught.value, OP)
        assert projected['preference_cleanup_unconfirmed'] is True
        if entry == 'run':
            assert projected['workflow']['failure']['preference_cleanup_unconfirmed'] is True
    assert closed == [True] and native._POISONED and _no_pair(owned)
    with pytest.raises(native.PreferenceStorageError):
        native.require_outside_transaction()


@pytest.mark.parametrize('flag', [0, 1, False, 'true', None])
def test_status_never_accepts_malformed_cleanup_flags(flag):
    result = workflow._initial('backup')
    result.update(ok=True, stage='complete', requested_operation_id=OP,
                  publication_complete=True, retirement_batch_complete=True, evidence_recorded=True)
    result['failure'] = {'preference_cleanup_unconfirmed': flag}
    assert commands.scheduled_status(result) == {
        'ok': False, 'message': 'Backup status unavailable; inspect retained operation evidence before retrying.'}


def test_status_never_accepts_success_with_cleanup_uncertainty():
    result = workflow._initial('backup')
    result.update(ok=True, stage='complete', requested_operation_id=OP,
                  publication_complete=True, retirement_batch_complete=True, evidence_recorded=True)
    result['failure'] = {'preference_cleanup_unconfirmed': True}
    assert commands.scheduled_status(result)['ok'] is False


def test_public_backup_releases_preference_guard_before_encoding_and_publication(owned, monkeypatch):
    prefs.set(OWNER, 'language', 'before')
    encode, publish = io.encode_tar, publication.publish
    observed = []
    def encoded(*args, **kwargs):
        native.require_outside_transaction()
        observed.append('encoding')
        return encode(*args, **kwargs)
    def published(*args, **kwargs):
        native.require_outside_transaction()
        observed.append('publication')
        return publish(*args, **kwargs)
    monkeypatch.setattr(io, 'encode_tar', encoded)
    monkeypatch.setattr(publication, 'publish', published)
    result = backup.create(operation_id=OP)
    _manifest, images = fmt.decode(Path(result['path']).read_bytes())
    assert observed == ['encoding', 'publication'] and result['publication_complete']
    assert '/'.join(native.coordination_path()) not in images
    assert json.loads(images['/'.join(prefs._relative_paths(OWNER)[0])])['language'] == 'before'



def _corrupt_current(root):
    parts = prefs._relative_paths(OWNER)[0]
    raw = b'{owned-corrupt-before-backup'
    with native.transaction(root) as tx:
        tx.publish(parts, raw)
    return parts, raw


def test_recovery_thread_waits_for_collection_and_runs_after_guard_release(owned, monkeypatch, request):
    parts, before = _corrupt_current(owned)
    target = owned.joinpath(*parts)
    gate = native._GATE
    blocked, finished = threading.Event(), threading.Event()
    results, errors, observed = [], [], []
    class ObservedGate:
        def acquire(self, *, timeout):
            if threading.current_thread().name == 'owned-backup-recovery':
                if gate.acquire(blocking=False):
                    gate.release()
                    raise AssertionError('recovery thread did not contend on collector gate')
                blocked.set()
            return gate.acquire(timeout=timeout)
        def release(self):
            return gate.release()
    def recover():
        try:
            results.append(prefs.repair(OWNER, operation_id=OP))
        except BaseException as error:
            errors.append(type(error).__name__)
        finally:
            finished.set()
    thread = threading.Thread(target=recover, name='owned-backup-recovery', daemon=True)
    read = io.read_file
    def captured(path, *args, **kwargs):
        if path == target:
            assert native._LOCAL.current.held
            thread.start()
            assert blocked.wait(3) and not finished.is_set()
            assert not (owned / 'prefs-recovery').exists()
            observed.append(True)
        return read(path, *args, **kwargs)
    primary = None
    with monkeypatch.context() as patch:
        patch.setattr(native, '_GATE', ObservedGate())
        patch.setattr(io, 'read_file', captured)
        try:
            images = _collect(owned)
        except BaseException as error:
            primary = error
            raise
        finally:
            cleanup_primary = None
            if thread.ident is not None:
                try:
                    thread.join(timeout=20)
                except BaseException as error:
                    cleanup_primary = error
                if thread.is_alive():
                    try:
                        thread.join(timeout=3)
                    except BaseException as error:
                        if cleanup_primary is None:
                            cleanup_primary = error
            if thread.is_alive():
                request.session.shouldstop = 'owned recovery thread closure unconfirmed'
                preserved = primary if primary is not None else cleanup_primary
                if isinstance(preserved, (KeyboardInterrupt, SystemExit)):
                    BaseException.__getattribute__(preserved, '__dict__')['_owned_recovery_thread_cleanup_unconfirmed'] = True
                    raise preserved
                pytest.exit('owned recovery thread closure unconfirmed; outer process containment required', returncode=3)
            if primary is None and cleanup_primary is not None:
                raise cleanup_primary
    assert observed == [True] and not errors and finished.is_set()
    assert len(results) == 1 and results[0]['repaired'] is True
    assert images == {'/'.join(parts): before}
    assert json.loads(target.read_bytes()) == {}
    assert (owned / 'prefs-recovery').is_dir()
    with pytest.raises(ValueError, match='not portable'):
        _collect(owned)
    assert not native._IN_FLIGHT and not native._POISONED


_RECOVERY_CHILD = r"""import json,socket,sys
from pathlib import Path
def denied(*args,**kwargs):raise AssertionError('owned recovery child network disabled')
socket.socket.connect=denied;socket.socket.connect_ex=denied
socket.socket.bind=denied;socket.create_connection=denied
sys.path.insert(0,sys.argv[1])
from olympus import config,prefs,prefs_native as native
config.MEMORY_DIR=Path(sys.argv[2])
signals=Path(sys.argv[3]);owner=sys.argv[4];operation_id=sys.argv[5]
real_probe=native.storage._try_lock
seen={'denied':False,'acquired':False}
def probe(fd):
    result=real_probe(fd)
    if not result and not seen['denied']:
        with (signals/'denied').open('xb') as stream:stream.write(b'owned-denied')
    seen['acquired' if result else 'denied']=True
    return result
native.storage._try_lock=probe
with (signals/'ready').open('xb') as stream:stream.write(b'owned-ready')
assert sys.stdin.buffer.read(1)==b'g'
result=prefs.repair(owner,operation_id=operation_id)
assert result['repaired'] is True
assert not native._IN_FLIGHT and not native._POISONED
print(json.dumps(seen))
"""


def _await_signal(path, child):
    deadline = time.monotonic() + 4
    while not path.exists():
        assert child.poll() is None, 'owned recovery child exited before its boundary'
        if time.monotonic() >= deadline:
            raise TimeoutError('owned recovery child boundary not observed')
        time.sleep(0.01)


def test_fresh_recovery_process_contends_on_collector_native_lock(owned, tmp_path, monkeypatch):
    parts, before = _corrupt_current(owned)
    target = owned.joinpath(*parts)
    signals = tmp_path / 'owned-process-signals'
    signals.mkdir()
    child, primary = None, None
    cleanup, observed, cleanup_first = [], [], []
    def record_cleanup(error):
        cleanup.append(type(error).__name__)
        if not cleanup_first:
            cleanup_first.append(error)
    read = io.read_file
    def captured(path, *args, **kwargs):
        if path == target:
            assert native._LOCAL.current.held
            child.stdin.write(b'g'); child.stdin.flush()
            _await_signal(signals / 'denied', child)
            assert child.poll() is None and not (owned / 'prefs-recovery').exists()
            observed.append(True)
        return read(path, *args, **kwargs)
    try:
        child = subprocess.Popen([sys.executable, '-I', '-B', '-c', _RECOVERY_CHILD,
            str(Path(native.__file__).resolve().parents[1]), str(owned), str(signals), OWNER, OP],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        _await_signal(signals / 'ready', child)
        with monkeypatch.context() as patch:
            patch.setattr(io, 'read_file', captured)
            images = _collect(owned)
        stdout, stderr = child.communicate(timeout=20)
        assert child.returncode == 0 and stderr == b'' and len(stdout) < 200
        assert json.loads(stdout) == {'denied': True, 'acquired': True}
        assert observed == [True] and images == {'/'.join(parts): before}
        assert json.loads(target.read_bytes()) == {}
        assert (owned / 'prefs-recovery').is_dir()
        with pytest.raises(ValueError, match='not portable'):
            _collect(owned)
    except BaseException as error:
        primary = error
        raise
    finally:
        if child is not None:
            running = None
            try:
                running = child.poll() is None
            except BaseException as error:
                record_cleanup(error)
            if running is not False:
                try:
                    child.kill()  # Retained handle / direct unreaped owned child only.
                except BaseException as error:
                    record_cleanup(error)
            try:
                child.wait(timeout=3)
            except BaseException as error:
                record_cleanup(error)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except BaseException as error:
                        record_cleanup(error)
        if cleanup:
            if primary is not None:
                BaseException.__getattribute__(primary, '__dict__')['_owned_recovery_child_cleanup_unconfirmed'] = True
            elif isinstance(cleanup_first[0], (KeyboardInterrupt, SystemExit)):
                preserved = cleanup_first[0]
                BaseException.__getattribute__(preserved, '__dict__')['_owned_recovery_child_cleanup_unconfirmed'] = True
                raise preserved
            else:
                raise AssertionError('owned recovery child cleanup unconfirmed')


@pytest.mark.parametrize('replacement', ['lock-file', 'locks-directory'])
def test_collection_refuses_lock_identity_replacement_before_return(owned, monkeypatch, replacement):
    prefs.set(OWNER, 'language', 'before')
    target = owned.joinpath(*prefs._relative_paths(OWNER)[0])
    before = target.read_bytes()
    selected = owned.joinpath(*native.coordination_path()) if replacement == 'lock-file' else owned / 'locks'
    retained = selected.with_name('retained-original-coordination')
    read = io.read_file
    observed = []
    def changed(path, *args, **kwargs):
        raw = read(path, *args, **kwargs)
        if path == target:
            assert native._LOCAL.current.held
            try:
                selected.rename(retained)
            except OSError as error:
                assert os.name == 'nt' and error.winerror in (5, 32)
                observed.append('native-refusal')
                raise
            observed.append('replaced')
            if replacement == 'lock-file':
                selected.write_bytes(b'owned replacement coordination')
            else:
                selected.mkdir()
                (selected / native.coordination_path()[-1]).write_bytes(b'owned replacement coordination')
        return raw
    with monkeypatch.context() as patch:
        patch.setattr(io, 'read_file', changed)
        with pytest.raises((native.PreferenceStorageError, native.storage.StorageError, OSError, ValueError)):
            _collect(owned)
    assert len(observed) == 1 and target.read_bytes() == before and _no_pair(owned)
    if observed == ['replaced']:
        assert retained.exists()
        replacement_file = selected if replacement == 'lock-file' else selected / native.coordination_path()[-1]
        assert replacement_file.read_bytes() == b'owned replacement coordination'
    else:
        assert selected.exists() and not retained.exists()
    assert not native._IN_FLIGHT and not native._POISONED



@pytest.mark.parametrize('reserved', ['prefs-recovery/head.json', 'prefs/owners/owned/prefs.claim.json',
                                     'locks/preferences.native.lock'])
def test_resume_rechecks_reserved_target_in_authenticated_retained_plan(owned, tmp_path, monkeypatch, reserved):
    destination = tmp_path / 'owned-offline-restore'
    evidence = {'archive_sha256': hashlib.sha256(b'owned fixture').hexdigest(),
        'signature_sha256': None, 'signed': False, 'signature_ok': False,
        'signature_override': False, 'archive_encryption': io.encryption_metadata(plaintext=True)}
    reached = []
    def stop_before_apply(*args):
        reached.append(True)
        raise OSError('owned retained restore plan before application')
    with monkeypatch.context() as patch:
        patch.setattr(restore, '_apply', stop_before_apply)
        with pytest.raises(restore.RestoreError):
            restore.restore(destination, {'owned-safe': b'owned after'}, evidence,
                            operation_id=OP, live_root=owned)
    assert reached == [True] and not (destination / 'owned-safe').exists()
    control = destination / restore.CONTROL
    plan_path, head_path = control / OP / 'plan.json', control / 'head.json'
    plan = json.loads(plan_path.read_bytes())
    assert len(plan['files']) == 1
    # Construct an owned, internally hash-consistent preexisting plan that a
    # previous binary could accept. New resume must recheck path semantics.
    plan['files'][0]['path'] = reserved
    plan_raw = restore._json(plan)
    plan_path.write_bytes(plan_raw)
    head = json.loads(head_path.read_bytes())
    head['plan_sha256'] = hashlib.sha256(plan_raw).hexdigest()
    head_raw = restore._json(head)
    head_path.write_bytes(head_raw)
    before = {p.relative_to(destination).as_posix(): p.read_bytes()
              for p in destination.rglob('*') if p.is_file()}
    validated = []
    target_name = restore._target_name
    def checked(name):
        if name == reserved:
            validated.append(True)
        return target_name(name)
    with monkeypatch.context() as patch:
        patch.setattr(restore, '_target_name', checked)
        with pytest.raises(restore.RestoreError):
            restore.resume(destination, operation_id=OP, live_root=owned)
    assert validated == [True]
    assert not destination.joinpath(*reserved.split('/')).exists()
    assert {p.relative_to(destination).as_posix(): p.read_bytes()
            for p in destination.rglob('*') if p.is_file()} == before
    assert list(owned.iterdir()) == []

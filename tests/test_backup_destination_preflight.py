"""Owned creation-destination refusal and native cleanup controls; no uploader."""
import json
from types import SimpleNamespace

import pytest

from olympus import archive_io as aio, backup, backup_authority as authority
from olympus import backup_commands as commands, backup_facts as facts
from olympus import backup_signing as signing, backup_snapshot as snapshot
from olympus import backup_workflow as workflow, native_cleanup
from test_backup_delivery import owned, files, native_io as native

OP = '8' * 32


@pytest.mark.parametrize('name,kind', [('backups','directory'),('backups','file'),
                                     ('BACKUPS','file'),('Backups','directory')])
def test_late_destination_refuses_before_any_private_input_or_collection(owned, monkeypatch, name, kind):
    calls = []
    def forbidden(*args, **kwargs):
        calls.append('private-or-collection')
        raise AssertionError('known foreign destination reached private input or collection')
    with authority.capture('run') as captured:
        path = native(owned / name)
        path.mkdir() if kind == 'directory' else path.write_bytes(b'owned foreign evidence')
        before = files(owned)
        monkeypatch.setattr(signing, 'capture_signer', forbidden)
        monkeypatch.setattr(aio.ArchiveSession, 'select_key', forbidden)
        monkeypatch.setattr(snapshot, 'collect', forbidden)
        with captured.stage('create'):
            with pytest.raises(backup.BackupError) as caught:
                backup._create(operation_id=OP, _authority=captured)
            assert caught.value.operation_id == OP
            assert captured._backups is None
        assert calls == [] and files(owned) == before
    assert files(owned) == before


@pytest.mark.parametrize('callback', ['signer','vault-key'])
def test_callback_created_destination_refuses_before_preference_guard(owned, monkeypatch, callback):
    calls = []
    def appear():
        target = native(owned / 'backups')
        target.mkdir()
        native(target / 'foreign').write_bytes(b'owned callback evidence')
        calls.append(callback)
    def no_collection(*args, **kwargs):
        calls.append('collection')
        raise AssertionError('late callback destination reached preference collection')
    with authority.capture('run') as captured:
        if callback == 'signer':
            original = signing.capture_signer
            def signer(**kwargs):
                result = original(**kwargs)
                appear()
                return result
            monkeypatch.setattr(signing, 'capture_signer', signer)
        else:
            original = aio.ArchiveSession.select_key
            def key(session):
                result = original(session)
                appear()
                return result
            monkeypatch.setattr(aio.ArchiveSession, 'select_key', key)
        monkeypatch.setattr(snapshot, 'collect', no_collection)
        with captured.stage('create'):
            with pytest.raises(backup.BackupError):
                backup._create(operation_id=OP, _authority=captured)
            assert captured._backups is None
        assert calls == [callback]
        assert not native(owned / 'locks').exists()
        assert native(owned / 'backups' / 'foreign').read_bytes() == b'owned callback evidence'


def _faulting_scan(monkeypatch, *, body=None, close=None):
    original = authority._creation_entries
    observed = {'opened':0,'closed':0,'roots':[]}
    def acquire(memory):
        raw = original(memory)
        observed['opened'] += 1
        observed['roots'].append(memory)
        class Scan:
            def __iter__(self):
                return self
            def __next__(self):
                if body is not None:
                    raise body
                return next(raw)
            def close(self):
                observed['closed'] += 1
                raw.close()  # The owned native iterator really drains before lost acknowledgment.
                if close is not None:
                    raise close
        return Scan()
    monkeypatch.setattr(authority, '_creation_entries', acquire)
    return observed


@pytest.mark.parametrize('fault_type', [OSError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('close_fails', [False, True])
def test_scan_body_failure_preserves_primary_and_closes_once(owned, monkeypatch, fault_type, close_fails):
    fault = fault_type('owned-private-scan')
    fault.__notes__ = 7  # Cleanup must not use add_note or replace this object.
    close_fault = OSError('owned-private-close') if close_fails else None
    observed = _faulting_scan(monkeypatch, body=fault, close=close_fault)
    before = files(owned)
    held = []
    with pytest.raises(fault_type) as caught:
        with authority.capture('run') as captured:
            held.extend([captured.memory,captured.working])
            with captured.stage('create'):
                with pytest.raises(fault_type) as primary:
                    captured.preflight_creation()
                assert primary.value is fault
                assert native_cleanup.unknown(fault) is close_fails
                assert observed['closed'] == observed['opened'] == 1
                if close_fails:
                    with pytest.raises(authority.AuthorityError) as refused:
                        captured.preflight_creation()
                    assert native_cleanup.unknown(refused.value)
                    assert observed['opened'] == 1
                raise fault
    assert caught.value is fault and observed['closed'] == 1
    assert all(directory._closed for directory in held)
    assert files(owned) == before


@pytest.mark.parametrize('fault_type', [OSError, KeyboardInterrupt, SystemExit])
def test_cleanup_only_failure_poison_is_local_and_roots_still_drain(owned, monkeypatch, fault_type):
    fault = fault_type('owned-private-close')
    observed = _faulting_scan(monkeypatch, close=fault)
    held = []
    with pytest.raises(fault_type) as caught:
        with authority.capture('run') as captured:
            held.extend([captured.memory,captured.working])
            with captured.stage('create'):
                with pytest.raises(fault_type) as primary:
                    captured.preflight_creation()
                assert primary.value is fault and native_cleanup.unknown(fault)
                with pytest.raises(authority.AuthorityError) as refused:
                    captured.check()
                assert native_cleanup.unknown(refused.value)
                raise fault
    assert caught.value is fault
    assert observed['opened'] == observed['closed'] == 1
    assert all(directory._closed for directory in held)
    assert not native(owned / 'locks').exists()


def test_enumeration_overflow_refuses_without_custody_and_closes_once(owned, monkeypatch):
    closed = []
    class Oversize:
        def __init__(self): self.position = 0
        def __iter__(self): return self
        def __next__(self):
            self.position += 1
            if self.position > snapshot.MAX_DIRECTORY_ENTRIES + 1:
                raise AssertionError('scan failed to enforce its bound')
            return SimpleNamespace(name='owned-' + str(self.position))
        def close(self): closed.append(True)
    monkeypatch.setattr(snapshot, 'MAX_DIRECTORY_ENTRIES', 3)
    iterator = Oversize()
    with authority.capture('run') as captured:
        monkeypatch.setattr(authority, '_creation_entries', lambda memory: iterator)
        with captured.stage('create'):
            with pytest.raises(authority.AuthorityError, match='bound'):
                captured.preflight_creation()
            captured.check()
    assert iterator.position == snapshot.MAX_DIRECTORY_ENTRIES + 1 == 4
    assert closed == [True] and not native(owned / 'locks').exists()


def test_preflight_observes_each_call_and_requires_creation_stage(owned, monkeypatch):
    calls = []
    original = authority._creation_entries
    def acquire(memory):
        calls.append('scan')
        return original(memory)
    with authority.capture('run') as captured:
        monkeypatch.setattr(authority, '_creation_entries', acquire)
        with pytest.raises(authority.AuthorityError): captured.preflight_creation()
        assert calls == []
        with captured.stage('create'):
            captured.preflight_creation()
            native(owned / 'backups').mkdir()
            with pytest.raises(authority.AuthorityError): captured.preflight_creation()
        assert calls == ['scan','scan'] and captured._backups is None


@pytest.mark.parametrize('fault_type', [OSError, KeyboardInterrupt, SystemExit])
def test_native_cleanup_fact_survives_real_workflow_and_command_projection(owned, monkeypatch, fault_type):
    fault = fault_type('owned-private-close')
    observed = _faulting_scan(monkeypatch, close=fault)
    if fault_type is OSError:
        result = backup.run(operation_id=OP, deliver_off=False)
        assert result['ok'] is False and not result['publication_complete']
        assert result['failure']['native_cleanup_unconfirmed'] is True
        assert 'preference_cleanup_unconfirmed' not in result['failure']
        rendered = commands.scheduled_status(result)
        assert rendered['ok'] is False and rendered['native_cleanup_unconfirmed'] is True
        assert commands._workflow_report(result)['failure']['native_cleanup_unconfirmed'] is True
        projected = commands._failure(workflow.WorkflowError(result), OP)
    else:
        with pytest.raises(fault_type) as caught:
            backup.run(operation_id=OP, deliver_off=False)
        assert caught.value is fault and native_cleanup.unknown(fault)
        projected = commands._failure(caught.value, OP)
    assert projected['native_cleanup_unconfirmed'] is True
    assert projected['workflow']['failure']['native_cleanup_unconfirmed'] is True
    assert 'preference_cleanup_unconfirmed' not in projected
    assert 'owned-private' not in json.dumps(projected)
    assert observed['opened'] == observed['closed'] == 1
    assert all(root._closed for root in observed['roots'])
    assert not native(owned / 'locks').exists()


@pytest.mark.parametrize('flag', [0, 1, False, 'true', None])
def test_public_native_cleanup_flag_must_be_exact_true(flag):
    result = workflow._initial('backup')
    result.update(stage='create',requested_operation_id=OP)
    result['failure']={'stage':'create','status':'completion_unconfirmed','native_cleanup_unconfirmed':flag}
    assert commands._workflow_report(result) is None
    assert commands.scheduled_status(result)=={'ok':False,
        'message':'Backup status unavailable; inspect retained operation evidence before retrying.'}


@pytest.mark.parametrize('flag', ['native_cleanup_unconfirmed','preference_cleanup_unconfirmed'])
def test_success_cannot_coexist_with_either_cleanup_uncertainty(flag):
    result=workflow._initial('backup')
    result.update(ok=True,stage='complete',requested_operation_id=OP,
        publication_complete=True,retirement_batch_complete=True,evidence_recorded=True)
    result['failure']={flag:True}
    assert commands.scheduled_status(result)['ok'] is False


def test_native_cleanup_transport_keeps_typed_acknowledgments_and_operation_id():
    source=OSError('owned-private')
    source.operation_id=OP
    native_cleanup.mark(source)
    confirmed=facts.ConfirmedStage('publication',OP,'1'*64,'2'*64,'3'*64)
    target=backup.BackupError('owned public failure')
    facts.carry(source,target,(confirmed,))
    facts.retain_id(target,source.operation_id)
    assert facts.observed(target)==(confirmed,) and target.operation_id==OP
    assert native_cleanup.unknown(target)
    output=commands._failure(target,'9'*32)
    assert output['native_cleanup_unconfirmed'] is True
    assert 'owned-private' not in json.dumps(output)


@pytest.mark.parametrize('already_held', [False, True])
def test_captured_creation_succeeds_with_absent_or_already_held_destination(owned, already_held):
    if already_held:
        native(owned / 'backups').mkdir()
    with authority.capture('run') as captured:
        previous = captured._backups
        assert (previous is not None) is already_held
        with captured.stage('create'):
            result = backup._create(operation_id=OP, _authority=captured)
        assert result['publication_complete'] is True and result['operation_id'] == OP
        assert captured._backups is not None
        if already_held:
            assert captured._backups is previous
        captured.check()
    assert native(result['path']).is_file()
    assert native(owned / 'data.txt').read_bytes() == b'owned delivery image'

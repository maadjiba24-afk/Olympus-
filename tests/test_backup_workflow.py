"""Actual workflow stages on owned roots; no uploader process or notifications."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import uuid

import pytest
from olympus import backup, backup_authority as authority, backup_workflow as workflow
from olympus import backup_evidence as evidence, backup_delivery as delivery
from olympus import backup_management as management, backup_publication as publication
from olympus import backup_restore as recovery, backup_facts as facts, config
from test_backup_delivery import owned, uploader, outcome, files, native_io as native


@pytest.fixture(autouse=True)
def fixed_local_policy(monkeypatch):
    monkeypatch.setattr(config, 'backup_keep', lambda: 2)
    monkeypatch.setattr(config, 'build_info', lambda: {'commit': '1' * 40})


def test_run_and_drill_record_exact_images_without_claiming_remote_or_current_proof(owned, tmp_path, monkeypatch):
    calls = uploader(monkeypatch)
    requested, attempt, restore_id = (uuid.uuid4().hex for _ in range(3))
    result = backup.run(operation_id=requested, delivery_id=attempt)
    assert result['ok'] is True and result['stage'] == 'complete'
    assert result['publication_complete'] and result['delivery_commands_complete']
    assert result['retirement_batch_complete'] and result['evidence_recorded']
    assert result['requested_operation_id'] == requested and result['requested_delivery_id'] == attempt
    assert result['remote_retention_verified'] is False and len(calls) == 2
    assert [row['kind'] for row in result['confirmed_stages']] == ['publication', 'delivery']
    monkeypatch.setattr(backup, 'verify_archive', lambda *a, **k: pytest.fail('drill separately reread archive'))
    drilled = backup.drill(into=tmp_path / 'offline', operation_id=restore_id)
    assert drilled['ok'] and drilled['restore_complete'] and drilled['evidence_recorded']
    assert drilled['sha256'] == result['sha256'] == drilled['restore']['consumed_archive_sha256']
    assert native(drilled['into']).is_dir()
    assert native(Path(drilled['into']) / 'data.txt').read_bytes() == b'owned delivery image'
    before = files(owned)
    observed = evidence.inspect()
    assert len(observed['receipts']) == 2 and observed['observational_only']
    for row in observed['receipts']:
        assert row['receipt_visible'] is True and row['write_acknowledgment_observed'] is None
        receipt = row['receipt']
        assert receipt['enclosing_workflow_complete'] is False
        assert receipt['current_archive_availability_verified'] is False
        assert receipt['remote_retention_verified'] is False and receipt['database_included'] is False
    assert files(owned) == before and len(calls) == 2


@pytest.mark.parametrize('stage', ['create', 'deliver', 'retire', 'evidence'])
def test_returned_stages_survive_later_stage_exit_failure(owned, monkeypatch, stage):
    calls = uploader(monkeypatch)
    original = authority._WorkflowAuthority.stage
    exits = []
    @contextmanager
    def failed_exit(self, name):
        with original(self, name):
            yield self
        if name == stage:
            exits.append(name)
            raise RuntimeError('owned private stage-exit sentinel')
    monkeypatch.setattr(authority._WorkflowAuthority, 'stage', failed_exit)
    result = backup.run()
    assert exits == [stage] and result['ok'] is False and result['failure']['stage'] == stage
    assert result['publication_complete'] is True
    assert result['delivery_commands_complete'] is (stage != 'create')
    assert result['retirement_batch_complete'] is (stage in ('retire', 'evidence'))
    assert result['evidence_recorded'] is (stage == 'evidence')
    assert len(calls) == (0 if stage == 'create' else 2)
    assert len(result['confirmed_stages']) == (1 if stage == 'create' else 2)
    assert 'owned private stage-exit sentinel' not in json.dumps(result)
    assert native(result['path']).is_file()


def test_authority_cleanup_failure_preserves_every_returned_stage(owned, monkeypatch):
    calls = uploader(monkeypatch)
    original = authority._WorkflowAuthority.close
    closed = []
    def close(self):
        original(self)
        closed.append(self)
        raise RuntimeError('owned private cleanup sentinel')
    monkeypatch.setattr(authority._WorkflowAuthority, 'close', close)
    result = backup.run()
    assert result['ok'] is False and result['failure']['stage'] == 'release_authority'
    assert result['publication_complete'] and result['delivery_commands_complete']
    assert result['retirement_batch_complete'] and result['evidence_recorded'] and len(calls) == 2
    assert len(closed) == 1 and not closed[0].active
    assert closed[0].memory is None and closed[0]._backups is None and closed[0].working is None
    assert 'owned private cleanup sentinel' not in json.dumps(result)


@pytest.mark.parametrize('stage', ['restore', 'evidence'])
def test_drill_stage_exit_failure_retains_completed_restore_and_offline_bytes(owned, tmp_path, monkeypatch, stage):
    created = backup.create()
    original = authority._WorkflowAuthority.stage
    @contextmanager
    def failed_exit(self, name):
        with original(self, name):
            yield self
        if name == stage:
            raise RuntimeError('owned private drill stage-exit sentinel')
    monkeypatch.setattr(authority._WorkflowAuthority, 'stage', failed_exit)
    with pytest.raises(workflow.WorkflowError) as raised:
        backup.drill(created['path'], into=tmp_path / 'offline')
    result = raised.value.workflow_facts
    assert not result['ok'] and result['restore_complete']
    assert result['sha256'] == created['sha256']
    assert result['evidence_recorded'] is (stage == 'evidence')
    assert result['failure']['stage'] == stage
    assert native(Path(result['into']) / 'data.txt').read_bytes() == b'owned delivery image'
    assert [row['kind'] for row in result['confirmed_stages']] == ['restore']
    assert 'owned private drill stage-exit sentinel' not in json.dumps(result)


def test_drill_allocates_absent_child_and_retains_parent_after_failure(owned, tmp_path, monkeypatch):
    created = backup.create()
    parent = tmp_path / 'retained-parent'
    calls = []
    def make_parent(**kwargs):
        assert kwargs == {'prefix': 'olympus-restore-drill-'}
        native(parent).mkdir()
        return str(parent)
    original = backup._restore
    def restore(path, **kwargs):
        assert kwargs['into'] == parent / 'restored' and not native(kwargs['into']).exists()
        calls.append(kwargs['operation_id'])
        return original(path, **kwargs)
    monkeypatch.setattr(workflow.tempfile, 'mkdtemp', make_parent)
    monkeypatch.setattr(backup, '_restore', restore)
    monkeypatch.setattr(evidence, '_record_restore', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('owned evidence failure')))
    with pytest.raises(workflow.WorkflowError) as raised:
        backup.drill(created['path'])
    result = raised.value.workflow_facts
    assert result['retained_parent'] == str(parent) and result['restore_complete']
    assert len(calls) == 1 and native(parent / 'restored' / recovery.CONTROL).is_dir()
    assert native(parent / 'restored' / 'data.txt').read_bytes() == b'owned delivery image'


def test_run_retains_partial_publication_ack_without_stage_return_or_retry(owned, monkeypatch):
    original = publication._Root.close
    closes = []
    def close(self):
        acknowledged = self.confirmed_stage_facts
        original(self)
        if acknowledged:
            closes.append(acknowledged)
            raise RuntimeError('owned later publication cleanup failure')
    monkeypatch.setattr(publication._Root, 'close', close)
    requested = uuid.uuid4().hex
    result = backup.run(operation_id=requested)
    assert not result['ok'] and not result['publication_complete']
    assert result['failure']['stage'] == 'create' and len(closes) == 1
    assert len(result['confirmed_stages']) == 1
    item = result['confirmed_stages'][0]
    assert item['kind'] == 'publication' and item['operation_id'] == requested
    assert item['completion_acknowledgment_observed'] and not item['enclosing_operation_complete']
    assert native(owned / 'backups' / publication.archive_name(requested, False)).is_file()
    assert not native(owned / evidence.CONTROL).exists()


def test_actual_pending_old_publication_id_is_not_relabelled_as_new_request(owned, monkeypatch):
    old, requested = uuid.uuid4().hex, uuid.uuid4().hex
    original = publication._Root.publish
    def interrupt(self, path, raw):
        if Path(path).name == 'complete.json':
            raise KeyboardInterrupt('owned interrupted old publication')
        return original(self, path, raw)
    with monkeypatch.context() as patch:
        patch.setattr(publication._Root, 'publish', interrupt)
        with pytest.raises(KeyboardInterrupt):
            backup.create(operation_id=old)
    before = files(owned)
    result = backup.run(operation_id=requested)
    assert not result['ok'] and result['requested_operation_id'] == requested
    assert result['failure']['observed_operation_id'] == old
    assert result['confirmed_stages'] == [] and files(owned) == before


def test_interrupt_identity_and_observed_id_survive_secondary_authority_close(owned, monkeypatch):
    requested, observed_id = uuid.uuid4().hex, uuid.uuid4().hex
    interrupted = KeyboardInterrupt('owned original interruption')
    interrupted.operation_id = observed_id
    sentinel = facts.ConfirmedStage('publication', observed_id, 'a' * 64, 'b' * 64, 'c' * 64)
    interrupted._backup_confirmed_stages = (sentinel,)
    def create(**kwargs):
        assert kwargs['operation_id'] == requested
        raise interrupted
    original = authority._WorkflowAuthority.close
    def close(self):
        original(self)
        raise RuntimeError('owned secondary cleanup')
    monkeypatch.setattr(backup, '_create', create)
    monkeypatch.setattr(authority._WorkflowAuthority, 'close', close)
    with pytest.raises(KeyboardInterrupt) as raised:
        backup.run(operation_id=requested)
    assert raised.value is interrupted and raised.value.operation_id == observed_id
    result = interrupted.workflow_facts
    assert result['requested_operation_id'] == requested
    assert result['failure']['observed_operation_id'] == observed_id
    assert result['confirmed_stages'] == [sentinel.report()] and not result['ok']


def test_keep_zero_records_historical_creation_and_retirement_without_current_availability(owned, monkeypatch):
    monkeypatch.setattr(config, 'backup_keep', lambda: 0)
    result = backup.run(deliver_off=False)
    assert result['ok'] and result['retirement']['retired_operations'] == [result['requested_operation_id']]
    recorded = result['evidence']['receipt']
    assert recorded['retirement']['retired_operations'] == [recorded['operation_id']]
    assert recorded['current_archive_availability_verified'] is False
    assert recorded['remote_retention_verified'] is False
    assert hashlib.sha256(native(result['path']).read_bytes()).hexdigest() == result['sha256']
    assert backup.list_backups()[0]['selection_candidate'] is False


@pytest.mark.parametrize('configured', [False, True])
def test_explicit_skip_and_unconfigured_delivery_remain_local_only(owned, monkeypatch, configured):
    if not configured:
        monkeypatch.delenv('OLYMPUS_BACKUP_CMD')
    result = backup.run(deliver_off=not configured)
    assert result['ok'] and result['status'] == 'local_only'
    assert not result['delivery_commands_complete'] and result['delivery']['delivery_id'] is None
    assert result['delivery']['status'] == ('explicitly_skipped' if configured else 'not_configured')
    assert not result['remote_retention_verified'] and result['evidence_recorded']



def test_partial_delivery_and_later_history_refusal_keep_actual_old_ids_and_never_retry(owned, monkeypatch):
    def unknown_signature(component, _command, _calls):
        return outcome() if component == 'archive' else outcome(exit_code=None,
            natural_tree_exit=False, cleanup_confirmed=False, handles_closed=False,
            reconciliation_required=True)
    calls = uploader(monkeypatch, unknown_signature)
    old_operation, old_attempt = uuid.uuid4().hex, uuid.uuid4().hex
    first = backup.run(operation_id=old_operation, delivery_id=old_attempt)
    assert not first['ok'] and first['publication_complete'] and not first['delivery_commands_complete']
    progress = first['failure']['delivery_progress']
    assert progress['components']['archive']['observed_outcome']['natural_tree_exit'] is True
    assert progress['components']['archive']['outcome_receipt_confirmed'] is True
    signature = progress['components']['signature']['observed_outcome']
    assert signature['exit_code'] is None and signature['reconciliation_required'] is True
    assert not progress['completion_receipt_confirmed'] and len(calls) == 2
    assert first['failure']['observed_delivery_id'] == old_attempt
    retained = files(owned / 'backups' / publication.CONTROL / old_operation)
    new_operation, new_attempt = uuid.uuid4().hex, uuid.uuid4().hex
    second = backup.run(operation_id=new_operation, delivery_id=new_attempt)
    assert not second['ok'] and second['publication_complete'] and second['failure']['stage'] == 'deliver'
    failure = second['failure']
    assert second['requested_delivery_id'] == new_attempt
    assert failure['attempt_delivery_id'] == new_attempt
    assert failure['observed_delivery_id'] == old_attempt
    assert failure['observed_blocking_publication_id'] == old_operation
    assert failure['delivery_progress']['components'] == {} and len(calls) == 2
    assert files(owned / 'backups' / publication.CONTROL / old_operation) == retained


def test_existing_attempt_projection_through_run_distinguishes_requested_and_retained_ids(owned, monkeypatch):
    created = backup.create()
    calls = uploader(monkeypatch)
    old_attempt, requested = uuid.uuid4().hex, uuid.uuid4().hex
    assert backup.deliver(created['path'], delivery_id=old_attempt)['delivery_commands_complete']
    # The narrow coordinator handoff injects a genuinely completed existing
    # publication result; the delivery adapter and retained claim are actual.
    # Ordinary run does not resume or recreate a requested old publication ID.
    def existing(**kwargs):
        assert kwargs['_authority'].memory.path == owned
        assert kwargs['operation_id'] == created['operation_id']
        return created
    monkeypatch.setattr(backup, '_create', existing)
    before = files(owned)
    result = backup.run(operation_id=created['operation_id'], delivery_id=requested)
    assert not result['ok'] and result['failure']['stage'] == 'deliver'
    failure = result['failure']
    assert failure['attempt_delivery_id'] == requested
    assert failure['observed_delivery_id'] == old_attempt
    assert failure['delivery_progress']['phase'] == 'existing_attempt_refused'
    assert failure['delivery_progress']['components'] == {} and len(calls) == 2
    assert files(owned) == before


@pytest.mark.parametrize('stage', ['create', 'deliver', 'retire'])
def test_token_sealing_failure_does_not_erase_valid_returned_stage(owned, monkeypatch, stage):
    calls = uploader(monkeypatch)
    original = evidence._seal
    def seal(captured, name, selected):
        if name == stage:
            raise ValueError('owned token sealing failure')
        return original(captured, name, selected)
    monkeypatch.setattr(evidence, '_seal', seal)
    result = backup.run()
    assert not result['ok'] and result['failure']['stage'] == stage
    assert result['publication_complete'] is True
    assert result['delivery_commands_complete'] is (stage != 'create')
    assert result['retirement_batch_complete'] is (stage == 'retire')
    assert len(calls) == (0 if stage == 'create' else 2)
    assert not result['evidence_recorded']


def test_restore_token_sealing_failure_keeps_returned_restore_and_bytes(owned, tmp_path, monkeypatch):
    created = backup.create()
    monkeypatch.setattr(evidence, '_seal', lambda *a: (_ for _ in ()).throw(ValueError('owned seal failure')))
    with pytest.raises(workflow.WorkflowError) as raised:
        backup.drill(created['path'], into=tmp_path / 'offline')
    result = raised.value.workflow_facts
    assert not result['ok'] and result['restore_complete'] and not result['evidence_recorded']
    assert result['failure']['stage'] == 'restore'
    assert native(Path(result['into']) / 'data.txt').read_bytes() == b'owned delivery image'
    assert result['confirmed_stages'][0]['archive_sha256'] == created['sha256']


def test_retirement_failure_reports_successful_earlier_member_without_deletion(owned, monkeypatch):
    earlier = [backup.create(), backup.create()]
    monkeypatch.setattr(config, 'backup_keep', lambda: 0)
    original = publication._retire_held
    completed = []
    def retire(root, operation_id):
        if completed:
            raise RuntimeError('owned second retirement failure')
        receipt = original(root, operation_id)
        completed.append(operation_id)
        return receipt
    monkeypatch.setattr(publication, '_retire_held', retire)
    result = backup.run(deliver_off=False)
    assert not result['ok'] and result['publication_complete'] and not result['retirement_batch_complete']
    assert result['failure']['stage'] == 'retire' and len(completed) == 1
    retired = [row for row in result['confirmed_stages'] if row['kind'] == 'retirement']
    assert len(retired) == 1 and retired[0]['operation_id'] == completed[0]
    for created in [*earlier, result['publication']]:
        assert hashlib.sha256(native(created['path']).read_bytes()).hexdigest() == created['sha256']



def test_later_damaged_history_does_not_inherit_previous_delivery_id(owned, monkeypatch):
    calls = uploader(monkeypatch)
    first = backup.create(operation_id='1' * 32)
    first_attempt = 'a' * 32
    assert backup.deliver(first['path'], delivery_id=first_attempt)['delivery_commands_complete']
    second = backup.create(operation_id='2' * 32)
    second_attempt = 'b' * 32
    assert backup.deliver(second['path'], delivery_id=second_attempt)['delivery_commands_complete']
    # Keep the active publication healthy while an older operation's metadata
    # is damaged before delivery-claim inspection.
    backup.create(operation_id='3' * 32)
    retained_root = owned / 'backups' / publication.CONTROL
    native(retained_root / second['operation_id'] / 'plan.json').write_bytes(b'{}')
    before_first, before_second = files(retained_root / first['operation_id']), files(retained_root / second['operation_id'])
    result = backup.run(operation_id='4' * 32, delivery_id='c' * 32)
    assert not result['ok'] and result['publication_complete'] and result['failure']['stage'] == 'deliver'
    failure = result['failure']
    assert failure['observed_blocking_publication_id'] == second['operation_id']
    assert failure['delivery_progress']['blocking_publication_id_observed'] == second['operation_id']
    assert 'existing_delivery_id_observed' not in failure['delivery_progress']
    assert 'observed_delivery_id' not in failure and len(calls) == 4
    assert files(retained_root / first['operation_id']) == before_first
    assert files(retained_root / second['operation_id']) == before_second

"""Acknowledgment loss and later cleanup failure controls on owned state."""
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import uuid

import pytest
from olympus import backup, backup_facts as facts, backup_publication as publication
from olympus import backup_restore as recovery, backup_delivery as delivery
from olympus.vault_storage import _Directory
from test_backup_delivery import owned, uploader, native_io as native


def fact(number=1, **overrides):
    return facts.ConfirmedStage(**{'kind': 'retirement', 'operation_id': f'{number:032x}',
        'archive_sha256': 'a' * 64, 'signature_sha256': 'b' * 64,
        'plan_sha256': 'c' * 64, **overrides})


@pytest.mark.parametrize('case', ['identical', 'overlapping_limit', 'conflict', 'unique_overflow'])
def test_merge_keeps_bounded_unique_acknowledgments(case):
    source, target = RuntimeError('owned original failure'), RuntimeError('owned public failure')
    if case == 'identical':
        left = tuple(fact(i) for i in range(facts.MAX_FACTS)); right = left
        expected = left
    elif case == 'overlapping_limit':
        left = tuple(fact(i) for i in range(800)); right = tuple(fact(i) for i in range(400, facts.MAX_FACTS))
        expected = tuple(fact(i) for i in range(facts.MAX_FACTS))
    elif case == 'conflict':
        left = (fact(),); right = (fact(archive_sha256='d' * 64),); expected = ()
    else:
        left = tuple(fact(i) for i in range(700)); right = tuple(fact(i) for i in range(700, 1400))
        expected = ()
    source._backup_confirmed_stages = left
    facts.carry(source, target, right)
    assert facts.observed(target) == expected
    assert str(source) == 'owned original failure' and str(target) == 'owned public failure'


@pytest.mark.parametrize('bad', [None, [], {'secret': 'owned sentinel'}, (object(),), tuple(range(1029))])
def test_malformed_transport_is_ignored_without_replacing_failure(bad):
    source = KeyboardInterrupt('owned original interruption')
    source._backup_confirmed_stages = bad
    target = RuntimeError('generic public failure')
    facts.carry(source, target, (fact(),))
    assert facts.observed(source) == () and facts.observed(target) == (fact(),)
    assert str(source) == 'owned original interruption'


def test_malformed_exact_fact_and_hostile_attribute_getter_cannot_mask_primary():
    class PrivateFailure(RuntimeError):
        def __getattribute__(self, name):
            if name == '_backup_confirmed_stages':
                raise ValueError('owned private getter must not run')
            return super().__getattribute__(name)
    malformed = object.__new__(facts.ConfirmedStage)
    source = PrivateFailure('owned primary')
    source._backup_confirmed_stages = (malformed,)
    target = RuntimeError('generic public failure')
    facts.carry(source, target)
    assert facts.observed(target) == ()
    assert str(source) == 'owned primary'


def test_acknowledgment_report_never_upgrades_enclosing_or_remote_success():
    item = fact()
    with pytest.raises(FrozenInstanceError): item.kind = 'publication'
    report = item.report()
    assert report['completion_acknowledgment_observed'] is True
    assert report['enclosing_operation_complete'] is False
    assert report['current_state_revalidated'] is False and report['remote_retention_verified'] is False
    with pytest.raises(ValueError): fact(kind='evidence')


@pytest.mark.parametrize('boundary', ['publication', 'creation'])
def test_confirmed_publication_survives_later_inner_or_outer_native_close_failure(owned, monkeypatch, boundary):
    op = uuid.uuid4().hex; raised = []
    if boundary == 'publication':
        original = publication._Root.close
        def close(root):
            original(root)
            if root.confirmed_stage_facts and not raised:
                raised.append(True)
                raise OSError('owned private close sentinel')
        monkeypatch.setattr(publication._Root, 'close', close)
    else:
        original = _Directory.close
        def close(directory):
            original(directory)
            if (directory.path == owned and not raised
                    and native(owned / 'backups' / publication.CONTROL / op / 'complete.json').is_file()):
                raised.append(True)
                raise OSError('owned private close sentinel')
        monkeypatch.setattr(_Directory, 'close', close)
    with pytest.raises(backup.BackupError) as caught:
        backup.create(operation_id=op)
    assert raised == [True]
    observed = facts.observed(caught.value)
    assert len(observed) == 1 and observed[0].kind == 'publication' and observed[0].operation_id == op
    assert caught.value.operation_id == op and 'sentinel' not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    journal = owned / 'backups' / publication.CONTROL / op
    receipt = json.loads(native(journal / 'complete.json').read_bytes())
    assert observed[0].archive_sha256 == receipt['archive_sha256']
    assert observed[0].signature_sha256 == receipt['signature_sha256']


@pytest.mark.parametrize('boundary', ['restore', 'live_root'])
def test_confirmed_restore_survives_later_inner_or_outer_native_close_failure(owned, tmp_path, monkeypatch, boundary):
    created = backup.create()
    target = tmp_path / 'offline'; op = uuid.uuid4().hex; raised = []
    if boundary == 'restore':
        original = recovery._Root.close
        def close(root):
            original(root)
            if root.confirmed_stage_facts and not raised:
                raised.append(True)
                raise OSError('owned private cleanup sentinel')
        monkeypatch.setattr(recovery._Root, 'close', close)
    else:
        original = _Directory.close
        def close(directory):
            original(directory)
            if directory.path == owned and native(target / 'data.txt').is_file() and not raised:
                raised.append(True)
                raise OSError('owned private cleanup sentinel')
        monkeypatch.setattr(_Directory, 'close', close)
    with pytest.raises(backup.BackupError) as caught:
        backup.restore(created['path'], into=target, operation_id=op)
    assert raised == [True]
    observed = facts.observed(caught.value)
    assert len(observed) == 1 and observed[0].kind == 'restore' and observed[0].operation_id == op
    assert observed[0].archive_sha256 == created['sha256']
    assert native(target / 'data.txt').read_bytes() == b'owned delivery image'
    assert caught.value.__cause__ is None and caught.value.__context__ is None


@pytest.mark.parametrize('kind', ['publication', 'restore'])
def test_visible_completion_with_lost_acknowledgment_mints_no_confirmed_fact(owned, tmp_path, monkeypatch, kind):
    created = backup.create() if kind == 'restore' else None
    module = recovery if kind == 'restore' else publication
    op = uuid.uuid4().hex; visible = []
    original = module._Root.publish
    def publish(root, path, raw):
        original(root, path, raw)
        if Path(path).name == 'complete.json':
            visible.append(Path(path))
            raise OSError('owned acknowledgment lost after visible receipt')
    monkeypatch.setattr(module._Root, 'publish', publish)
    with pytest.raises(backup.BackupError) as caught:
        if kind == 'publication': backup.create(operation_id=op)
        else: backup.restore(created['path'], into=tmp_path / 'offline', operation_id=op)
    assert len(visible) == 1 and native(visible[0]).is_file()
    assert facts.observed(caught.value) == () and caught.value.operation_id == op


def test_retirement_batch_reports_acknowledged_member_before_next_lost_ack(owned, monkeypatch):
    created = [backup.create(), backup.create()]
    original = publication._Root.publish; acknowledgments = []
    def publish(root, path, raw):
        original(root, path, raw)
        if Path(path).name == 'retired.json':
            acknowledgments.append(Path(path))
            if len(acknowledgments) == 2:
                raise OSError('owned second retirement acknowledgment lost')
    monkeypatch.setattr(publication._Root, 'publish', publish)
    with pytest.raises(backup.BackupError) as caught: backup.prune(keep=0)
    assert len(acknowledgments) == 2 and all(native(path).is_file() for path in acknowledgments)
    observed = facts.observed(caught.value)
    assert [(item.kind, item.operation_id) for item in observed] == [('retirement', acknowledgments[0].parent.name)]
    assert caught.value.completed_retirement_ids == (acknowledgments[0].parent.name,)
    assert caught.value.files_deleted == 0
    assert all(native(row['path']).is_file() and native(row['path'] + '.sig.json').is_file() for row in created)


def test_retirement_inner_close_failure_keeps_acknowledged_member(owned, monkeypatch):
    created = backup.create(); roots = []; raised = []
    original_init, original_close = publication._Root.__init__, _Directory.close
    def initialize(root, *args, **kwargs):
        original_init(root, *args, **kwargs); roots.append(root)
    def close(directory):
        original_close(directory)
        if (directory.path.name == created['operation_id'] and roots
                and any(item.kind == 'retirement' for item in roots[-1].confirmed_stage_facts) and not raised):
            raised.append(True)
            raise OSError('owned retirement directory close failure')
    monkeypatch.setattr(publication._Root, '__init__', initialize)
    monkeypatch.setattr(_Directory, 'close', close)
    with pytest.raises(backup.BackupError) as caught: backup.prune(keep=0)
    assert raised == [True]
    assert caught.value.completed_retirement_ids == (created['operation_id'],)
    assert [(item.kind, item.operation_id) for item in facts.observed(caught.value)] == [('retirement', created['operation_id'])]


def test_confirmed_delivery_survives_publication_lease_cleanup_failure_without_retry(owned, monkeypatch):
    created = backup.create(); calls = uploader(monkeypatch)
    original = publication._Root.close; raised = []
    def close(root):
        original(root)
        if len(calls) == 2 and not raised:
            raised.append(True)
            raise OSError('owned delivery lease cleanup failure')
    monkeypatch.setattr(publication._Root, 'close', close)
    did = uuid.uuid4().hex
    with pytest.raises(backup.BackupError) as caught: backup.deliver(created['path'], delivery_id=did)
    assert raised == [True] and len(calls) == 2
    observed = facts.observed(caught.value)
    assert len(observed) == 1 and observed[0].kind == 'delivery'
    assert observed[0].operation_id == did and observed[0].publication_id == created['operation_id']
    assert observed[0].archive_sha256 == created['sha256']
    assert caught.value.delivery_progress['completion_receipt_confirmed'] is True
    with pytest.raises(backup.BackupError): backup.deliver(created['path'])
    assert len(calls) == 2


def test_delivery_lost_completion_ack_has_no_confirmed_fact(owned, monkeypatch):
    created = backup.create(); calls = uploader(monkeypatch)
    original = _Directory.write_new; visible = []
    def write(directory, name, raw):
        original(directory, name, raw)
        if directory.path.name == delivery.CONTROL and name == 'complete.json':
            visible.append(directory.path / name)
            raise OSError('owned delivery completion acknowledgment lost')
    monkeypatch.setattr(_Directory, 'write_new', write)
    with pytest.raises(backup.BackupError) as caught: backup.deliver(created['path'])
    assert len(calls) == 2 and len(visible) == 1 and native(visible[0]).is_file()
    assert caught.value.delivery_progress['completion_receipt_confirmed'] is False
    assert facts.observed(caught.value) == ()


@pytest.mark.parametrize('module', [publication, recovery], ids=['publication', 'restore'])
@pytest.mark.parametrize('interruption', [KeyboardInterrupt, SystemExit])
def test_native_lease_preserves_observed_recovery_id_on_interrupt(owned, tmp_path, module, interruption):
    requested, observed = uuid.uuid4().hex, uuid.uuid4().hex
    stop = interruption('owned interruption'); stop.operation_id = observed
    if module is publication:
        target = tmp_path / 'journal'; native(target).mkdir()
        context = module.locked(target, initialize=True, operation_id=requested)
    else:
        context = module._authority(tmp_path / 'offline', owned, (), requested, initialize=True)
    with pytest.raises(interruption) as caught:
        with context as root: raise stop
    assert caught.value is stop and stop.operation_id == observed
    assert root.closed and root.fd is None and root.directories == []


def test_facade_restore_boundary_preserves_actual_interrupt_id_and_confirmed_facts():
    requested, observed = uuid.uuid4().hex, uuid.uuid4().hex
    stop = KeyboardInterrupt('owned interruption'); stop.operation_id = observed
    item = fact(kind='restore', operation_id=observed)
    with pytest.raises(KeyboardInterrupt) as caught:
        with backup._restore_boundary(requested) as confirmed:
            confirmed.append(item)
            raise stop
    assert caught.value is stop and stop.operation_id == observed
    assert facts.observed(stop) == (item,)



def test_creation_preserves_confirmed_inner_failure_through_both_outer_close_failures(owned, monkeypatch):
    op = uuid.uuid4().hex; primary_seen = []; secondary = []
    original_root, original_directory = publication._Root.close, _Directory.close
    def root_close(root):
        original_root(root)
        if root.confirmed_stage_facts and not primary_seen:
            primary_seen.append(True)
            raise OSError('owned primary publication cleanup failure')
    def directory_close(directory):
        original_directory(directory)
        if primary_seen and directory.path in (owned, owned / 'backups'):
            secondary.append(directory.path)
            raise OSError('owned secondary creation cleanup failure')
    monkeypatch.setattr(publication._Root, 'close', root_close)
    monkeypatch.setattr(_Directory, 'close', directory_close)
    with pytest.raises(backup.BackupError) as caught: backup.create(operation_id=op)
    assert primary_seen == [True] and secondary == [owned / 'backups', owned]
    assert caught.value.operation_id == op
    assert [(item.kind, item.operation_id) for item in facts.observed(caught.value)] == [('publication', op)]
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_actual_unfinished_publication_id_survives_new_request_and_secondary_creation_cleanup(owned, monkeypatch):
    old, requested = uuid.uuid4().hex, uuid.uuid4().hex
    original_publish = publication._Root.publish; heads = []
    def publish(root, path, raw):
        original_publish(root, path, raw)
        if Path(path).name == 'head.json':
            heads.append(Path(path))
            raise OSError('owned original intent acknowledgment lost')
    with monkeypatch.context() as temporary:
        temporary.setattr(publication._Root, 'publish', publish)
        with pytest.raises(backup.BackupError) as first: backup.create(operation_id=old)
    assert len(heads) == 1 and first.value.operation_id == old
    assert json.loads(native(heads[0]).read_bytes())['operation_id'] == old
    original_create, original_close = publication.publish, _Directory.close
    observed = []; secondary = []
    def create(*args, **kwargs):
        try: return original_create(*args, **kwargs)
        except publication.PublicationError as error:
            observed.append(error.operation_id)
            raise
    def close(directory):
        original_close(directory)
        if observed and directory.path in (owned / 'backups', owned):
            secondary.append(directory.path)
            raise OSError('owned secondary native cleanup failure')
    monkeypatch.setattr(publication, 'publish', create)
    monkeypatch.setattr(_Directory, 'close', close)
    with pytest.raises(backup.BackupError) as caught: backup.create(operation_id=requested)
    assert observed == [old] and secondary == [owned / 'backups', owned]
    assert caught.value.operation_id == old and facts.observed(caught.value) == ()
    assert not native(owned / 'backups' / publication.CONTROL / requested).exists()


@pytest.mark.parametrize('kind', ['publication', 'restore'])
def test_historical_resume_acknowledgment_survives_later_cleanup_failure(owned, tmp_path, monkeypatch, kind):
    created = backup.create()
    if kind == 'restore':
        destination = tmp_path / 'offline'
        restored = backup.restore(created['path'], into=destination)
        op = restored['operation_id']; module = recovery
    else:
        op = created['operation_id']; module = publication
    original = module._Root.close; confirmed = []
    def close(root):
        original(root)
        if root.confirmed_stage_facts and not confirmed:
            confirmed.extend(root.confirmed_stage_facts)
            raise OSError('owned historical completion cleanup failure')
    monkeypatch.setattr(module._Root, 'close', close)
    error_type = backup.BackupError if kind == 'restore' else publication.PublicationError
    with pytest.raises(error_type) as caught:
        if kind == 'restore': backup.resume_restore(destination, operation_id=op)
        else: publication.resume(Path(created['path']).parent, operation_id=op)
    assert len(confirmed) == 1 and facts.observed(caught.value) == tuple(confirmed)
    assert confirmed[0].kind == kind and confirmed[0].operation_id == op
    assert confirmed[0].archive_sha256 == created['sha256']
    assert caught.value.operation_id == op



def test_borrowed_root_accumulator_deduplicates_and_preserves_prior_tuple_on_refusal():
    current = ()
    for _ in range(facts.MAX_FACTS + 10): current = facts.accumulate(current, fact())
    assert current == (fact(),)
    full = tuple(fact(i) for i in range(facts.MAX_FACTS))
    with pytest.raises(ValueError): facts.accumulate(full, fact(facts.MAX_FACTS))
    assert len(full) == facts.MAX_FACTS
    assert full[0] == fact(0) and full[-1] == fact(facts.MAX_FACTS - 1)
    assert facts.accumulate(full, full[0]) == full

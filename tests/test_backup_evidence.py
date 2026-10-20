"""Retained local evidence, lost acknowledgments and native-root provenance."""
from contextlib import contextmanager
import copy
import hashlib
import json
import os
import errno
from pathlib import Path
import shutil
import uuid

import pytest
from olympus import backup, backup_evidence as evidence, backup_authority as authority
from olympus import backup_management as management, backup_restore as recovery
from olympus import archive_io as aio, backup_archive as fmt, config
from olympus.vault_storage import _Directory
from test_backup_delivery import owned, files, native_io as native
from test_backup_restore_recovery import _evidence as restore_fixture_evidence


@pytest.fixture(autouse=True)
def fixed_local_policy(monkeypatch):
    monkeypatch.setattr(config, 'backup_keep', lambda: 2)
    monkeypatch.setattr(config, 'build_info', lambda: {'commit': '1' * 40})


@contextmanager
def completed_stages():
    with authority.capture('run') as captured:
        with captured.stage('create'):
            created = backup._create(_authority=captured)
            evidence._capture_returned(captured, 'create', created)
        evidence._capture_skipped_delivery(captured)
        with captured.stage('retire'):
            retired = management._retire_backups(_authority=captured)
            evidence._capture_returned(captured, 'retire', retired)
        yield captured, created, retired


def test_visible_receipt_after_lost_write_ack_is_not_reported_as_acknowledged(owned, monkeypatch):
    original = _Directory.write_new
    written = []
    def write(self, name, data):
        original(self, name, data)
        if self.path.name == evidence.CONTROL and name.startswith('backup-'):
            written.append(name)
            raise OSError('owned lost receipt acknowledgment')
    with monkeypatch.context() as patch:
        patch.setattr(_Directory, 'write_new', write)
        result = backup.run(deliver_off=False)
    assert not result['ok'] and result['publication_complete'] and result['retirement_batch_complete']
    assert result['failure']['stage'] == 'evidence' and not result['evidence_recorded']
    assert 'evidence_acknowledgment' not in result and len(written) == 1
    before = files(owned)
    observed = evidence.inspect()
    assert len(observed['receipts']) == 1
    assert observed['receipts'][0]['write_acknowledgment_observed'] is None
    assert observed['completion_barriers_reconfirmed'] is False and files(owned) == before


@pytest.mark.parametrize('interrupt', [False, True])
def test_evidence_ack_survives_store_cleanup_failure(owned, monkeypatch, interrupt):
    original = evidence._Store.close
    failure = KeyboardInterrupt('owned store interruption') if interrupt else RuntimeError('owned store cleanup')
    def close(self):
        original(self)
        raise failure
    with monkeypatch.context() as patch:
        patch.setattr(evidence._Store, 'close', close)
        if interrupt:
            with pytest.raises(KeyboardInterrupt) as raised:
                backup.run(deliver_off=False)
            assert raised.value is failure
            result = raised.value.workflow_facts
        else:
            result = backup.run(deliver_off=False)
    assert not result['ok'] and not result['evidence_recorded']
    assert result['publication_complete'] and result['retirement_batch_complete']
    acknowledgment = result['evidence_acknowledgment']
    assert acknowledgment['receipt_write_acknowledgment_observed'] is True
    assert acknowledgment['enclosing_workflow_complete'] is False
    row = evidence.inspect()['receipts'][0]
    assert row['receipt_sha256'] == acknowledgment['receipt_sha256']
    assert row['write_acknowledgment_observed'] is None


def test_same_evidence_id_never_overwrites_prior_receipt(owned):
    with completed_stages() as (captured, created, _retired):
        with captured.stage('evidence'):
            first = evidence._record_backup(_authority=captured)
        before = files(owned)
        with captured.stage('evidence'):
            with pytest.raises(evidence.EvidenceError) as raised:
                evidence._record_backup(_authority=captured)
        assert raised.value.operation_id == created['operation_id']
        assert evidence.observed(raised.value) is None
        assert files(owned) == before
        assert first['acknowledgment']['receipt_write_acknowledgment_observed']


@pytest.mark.parametrize('damage', ['marker', 'extra_entry', 'case_alias', 'malformed_receipt'])
def test_damaged_namespace_is_preserved_and_not_reinitialized(owned, monkeypatch, damage):
    initial = backup.run(deliver_off=False)
    assert initial['ok']
    control = owned / evidence.CONTROL
    receipt = control / ('backup-' + initial['requested_operation_id'] + '.json')
    if damage == 'marker':
        native(control / 'initialization.json').write_bytes(b'{}')
    elif damage == 'extra_entry':
        native(control / 'unexplained.json').write_bytes(b'owned retained bytes')
    elif damage == 'case_alias':
        # Rename the sole owned leaf; no two-name assumption on Windows.
        native(receipt).rename(native(receipt.with_name(receipt.name.upper())))
    else:
        native(receipt).write_bytes(b'{"schema":1,"schema":2}')
    before = files(owned)
    with pytest.raises(evidence.EvidenceError):
        evidence.inspect()
    assert files(owned) == before
    retained = files(control)
    with completed_stages() as (captured, _created, _retired):
        with captured.stage('evidence'):
            with pytest.raises(evidence.EvidenceError):
                evidence._record_backup(_authority=captured)
    assert files(control) == retained


def test_capacity_refusal_retains_receipts_and_completed_publication(owned, monkeypatch):
    first = backup.run(deliver_off=False)
    assert first['ok']
    retained = files(owned / evidence.CONTROL)
    monkeypatch.setattr(evidence, 'MAX_RECEIPTS', 1)
    second = backup.run(deliver_off=False)
    assert not second['ok'] and second['failure']['stage'] == 'evidence'
    assert second['publication_complete'] and not second['evidence_recorded']
    assert files(owned / evidence.CONTROL) == retained
    assert native(second['path']).is_file()


def test_copied_namespace_cannot_be_adopted_as_another_roots_evidence(owned, tmp_path, monkeypatch):
    result = backup.run(deliver_off=False)
    assert result['ok']
    other = tmp_path / 'other-memory'
    native(other).mkdir()
    shutil.copytree(native(owned / evidence.CONTROL), native(other / evidence.CONTROL))
    before = files(other)
    monkeypatch.setattr(config, 'MEMORY_DIR', other)
    with pytest.raises(evidence.EvidenceError):
        evidence.inspect()
    assert files(other) == before


def test_raw_foreign_result_and_foreign_tokens_cannot_be_recorded_in_current_root(owned, tmp_path, monkeypatch):
    with completed_stages() as (first, created, _retired):
        tokens = dict(first._returned_evidence_tokens)
    assert not first._returned_evidence_tokens
    other = tmp_path / 'other-memory'
    native(other).mkdir()
    native(other / 'sentinel').write_bytes(b'preserve other root')
    monkeypatch.setattr(config, 'MEMORY_DIR', other)
    with authority.capture('run') as second:
        with second.stage('create'):
            with pytest.raises(ValueError):
                evidence._capture_returned(second, 'create', created)
        second._returned_evidence_tokens.update(tokens)
        with second.stage('evidence'):
            with pytest.raises(ValueError):
                evidence._record_backup(_authority=second)
    assert files(other) == {'sentinel': b'preserve other root'}


def test_token_freezes_result_bytes_and_cannot_cross_modes_or_replace(owned):
    with completed_stages() as (captured, created, _retired):
        selected = evidence._token(captured, 'create')
        created['sha256'] = '0' * 64
        assert evidence._token(captured, 'create') == selected
        with captured.stage('evidence'):
            with pytest.raises(ValueError):
                evidence._record_restore(_authority=captured)
            recorded = evidence._record_backup(_authority=captured)
        assert recorded['receipt']['publication']['archive_sha256'] == selected['archive_sha256']
        with captured.stage('create'):
            with pytest.raises(ValueError):
                evidence._seal(captured, 'create', selected)


@pytest.mark.parametrize('full', [False, True])
def test_later_backup_excludes_evidence_namespace_and_restore_refuses_reserved_paths(owned, tmp_path, full):
    first = backup.run(deliver_off=False)
    assert first['ok']
    created = backup.create(full=full)
    raw = native(created['path']).read_bytes()
    manifest, changes = fmt.decode(raw)
    assert changes and all(not name.casefold().startswith(evidence.CONTROL) for name in changes)
    assert not any(row['path'].casefold().startswith(evidence.CONTROL) for row in manifest['files'])
    before = files(owned)
    for number, spelling in enumerate((evidence.CONTROL, evidence.CONTROL.upper())):
        destination = tmp_path / ('reserved-target-' + str(number))
        with pytest.raises(ValueError, match='^restore target overlaps reserved recovery or signing custody$') as raised:
            recovery.restore(destination, {spelling + '/initialization.json': b'foreign authority'},
                restore_fixture_evidence(), operation_id=uuid.uuid4().hex, live_root=owned)
        assert type(raised.value) is ValueError
        assert not native(destination).exists()
    assert files(owned) == before


@pytest.mark.parametrize('mutation', ['bool_count', 'string_boolean', 'wrong_digest', 'wrong_signature', 'bad_encryption', 'unknown_field'])
def test_strict_receipt_parser_rejects_coercion_and_cross_stage_mismatches(owned, mutation):
    result = backup.run(deliver_off=False)
    assert result['ok']
    payload = copy.deepcopy(result['evidence']['receipt'])
    if mutation == 'bool_count':
        payload['publication']['files'] = True
    elif mutation == 'string_boolean':
        payload['publication']['trust']['signer_authenticated'] = 'true'
    elif mutation == 'wrong_digest':
        payload['delivery']['archive_sha256'] = 'd' * 64
    elif mutation == 'wrong_signature':
        payload['delivery']['signature_sha256'] = 'e' * 64
    elif mutation == 'bad_encryption':
        payload['publication']['encryption']['context_authenticated'] = 1
    else:
        payload['raw_private_error'] = 'owned private sentinel'
    with pytest.raises(ValueError):
        evidence._payload(payload)


def test_inspection_is_key_free_and_does_not_create_missing_namespace(owned, monkeypatch):
    before = files(owned)
    monkeypatch.setattr(aio.custody, 'capture', lambda *a, **k: pytest.fail('inspection acquired private custody'))
    observed = evidence.inspect()
    assert observed['receipts'] == [] and observed['observational_only']
    assert files(owned) == before



def test_real_native_evidence_lock_contention_refuses_and_drains_only_its_handles(owned, monkeypatch):
    with completed_stages() as (captured, _created, _retired):
        with captured.stage('evidence'):
            with evidence._store(captured.memory, initialize=True):
                pass
        before = files(owned / evidence.CONTROL)
        closed, attempts = [], []
        original_try_lock = evidence._try_lock
        def try_lock(descriptor):
            acquired = original_try_lock(descriptor)
            attempts.append(acquired)
            return acquired
        monkeypatch.setattr(evidence, '_try_lock', try_lock)
        original = evidence._Store.close
        def close(self):
            descriptor, directory = self.fd, self.control
            original(self)
            if descriptor is not None:
                with pytest.raises(OSError) as raised:
                    os.fstat(descriptor)
                assert raised.value.errno == errno.EBADF
            if directory is not None:
                assert directory._closed
            self.memory.check()
            closed.append(self)
        monkeypatch.setattr(evidence._Store, 'close', close)
        with captured.stage('evidence'):
            with evidence._store(captured.memory, initialize=False) as held:
                with pytest.raises(evidence.EvidenceError):
                    evidence._record_backup(_authority=captured)
                assert len(closed) == 1 and closed[0] is not held
                assert attempts == [True, False]
                held.check()
            assert len(closed) == 2 and held.closed
        assert files(owned / evidence.CONTROL) == before
        with captured.stage('evidence'):
            completed = evidence._record_backup(_authority=captured)
        assert attempts == [True, False, True]
        assert completed['acknowledgment']['receipt_write_acknowledgment_observed'] is True
        captured.memory.check()


@pytest.mark.parametrize('boundary', ['initialization_file', 'receipt_file', 'receipt_directory'])
def test_underlying_evidence_barrier_failure_retains_bytes_and_no_ack(owned, monkeypatch, boundary):
    with completed_stages() as (captured, created, _retired):
        original_open, original_sync = _Directory._open, _Directory.sync
        original_fsync, original_close = os.fsync, os.close
        original_store_close = evidence._Store.close
        names, failures, drained = {}, [], []
        receipt_name = 'backup-' + created['operation_id'] + '.json'
        def open_file(self, name, flags):
            descriptor = original_open(self, name, flags)
            if self.path.name == evidence.CONTROL:
                names[descriptor] = name
            return descriptor
        def close_file(descriptor):
            names.pop(descriptor, None)
            return original_close(descriptor)
        def fsync(descriptor):
            name = names.get(descriptor)
            expected = 'initialization.json' if boundary == 'initialization_file' else receipt_name
            if boundary != 'receipt_directory' and name == expected and not failures:
                failures.append(boundary)
                raise OSError(errno.EIO, 'owned injected native evidence barrier failure')
            return original_fsync(descriptor)
        def sync(self):
            if (boundary == 'receipt_directory' and self.path.name == evidence.CONTROL
                    and native(self.path / receipt_name).is_file() and not failures):
                failures.append(boundary)
                raise OSError(errno.EIO, 'owned injected evidence directory barrier failure')
            return original_sync(self)
        def close_store(self):
            descriptor, child = self.fd, self.control
            original_store_close(self)
            assert self.fd is None and self.control is None and self.closed
            if descriptor is not None:
                with pytest.raises(OSError):
                    os.fstat(descriptor)
            if child is not None:
                assert child._closed
            self.memory.check()
            drained.append(self)
        with monkeypatch.context() as patch:
            patch.setattr(_Directory, '_open', open_file)
            patch.setattr(_Directory, 'sync', sync)
            patch.setattr(os, 'fsync', fsync)
            patch.setattr(os, 'close', close_file)
            patch.setattr(evidence._Store, 'close', close_store)
            with captured.stage('evidence'):
                with pytest.raises(evidence.EvidenceError) as raised:
                    evidence._record_backup(_authority=captured)
        assert failures == [boundary] and len(drained) == 1
        assert evidence.observed(raised.value) is None
        assert native(owned / evidence.CONTROL / 'initialization.json').is_file()
        assert native(owned / evidence.CONTROL / receipt_name).is_file() is (boundary != 'initialization_file')
        assert hashlib.sha256(native(created['path']).read_bytes()).hexdigest() == created['sha256']
        before = files(owned / evidence.CONTROL)
        observed = evidence.inspect()
        assert len(observed['receipts']) == (0 if boundary == 'initialization_file' else 1)
        assert all(row['write_acknowledgment_observed'] is None for row in observed['receipts'])
        assert files(owned / evidence.CONTROL) == before

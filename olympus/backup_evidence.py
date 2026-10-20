"""Bounded append-only local stage receipts, never remote-storage proof.

Receipts describe earlier returned stages. Visibility does not reconstruct a
lost write acknowledgment, establish current archive availability, or prove
that an enclosing workflow survived cleanup. Copied namespaces are rejected
by their native initialization identities.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
import re
import threading
import time

from . import archive_io as aio, backup_archive as fmt, backup_facts as facts
from . import backup_publication as publication, config
from .backup_authority import require
from .backup_restore import _exclusive_child, _raise_public_error
from .vault_storage import (_Directory, _directory_identity, _file_information,
                            _file_identity, _try_lock, _unlock)
from . import native_cleanup

SCHEMA = 'olympus-backup-local-evidence/1'
CONTROL = '.olympus-backup-evidence'
MAX_RECEIPT = 64 * 1024
MAX_RECEIPTS = 2048
MAX_TOTAL = 8 * 1024 * 1024
_NAME = re.compile(r'(backup|restore)-([0-9a-f]{32})\.json\Z')
_COMMIT = re.compile(r'[0-9a-f]{40}\Z')
_ATTRIBUTE = '_backup_recorded_evidence'


class EvidenceError(RuntimeError):
    def __init__(self, operation_id=None):
        self.operation_id = operation_id if facts.operation_id(operation_id) else None
        super().__init__('backup evidence did not complete; retain its ID and all existing receipts')


@dataclass(frozen=True)
class RecordedEvidence:
    kind: str
    operation_id: str
    archive_sha256: str
    receipt_sha256: str

    def __post_init__(self):
        if not _valid_ack(self):
            raise ValueError('invalid backup evidence acknowledgment')

    def report(self):
        if not _valid_ack(self):
            raise ValueError('invalid backup evidence acknowledgment')
        return {'schema': SCHEMA, 'kind': self.kind, 'operation_id': self.operation_id,
            'archive_sha256': self.archive_sha256, 'receipt_sha256': self.receipt_sha256,
            'receipt_write_acknowledgment_observed': True, 'enclosing_workflow_complete': False,
            'current_archive_availability_verified': False, 'remote_retention_verified': False}


def _valid_ack(value):
    if type(value) is not RecordedEvidence:
        return False
    try:
        return (type(value.kind) is str and value.kind in ('backup', 'restore')
                and facts.operation_id(value.operation_id) and publication._digest(value.archive_sha256)
                and publication._digest(value.receipt_sha256))
    except AttributeError:
        return False


def observed(error):
    attributes = facts._attributes(error)
    value = None if attributes is None else attributes.get(_ATTRIBUTE)
    return value if _valid_ack(value) else None


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _object(value):
    if type(value) is not dict:
        raise ValueError('backup evidence requires an exact object')
    return value


def _bool(value):
    if type(value) is not bool:
        raise ValueError('backup evidence requires explicit booleans')
    return value


def _integer(value, maximum, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('backup evidence integer is outside its bound')
    return value


def _digest(value, *, optional=False):
    if not (optional and value is None) and not publication._digest(value):
        raise ValueError('backup evidence digest is invalid')
    return value


def _encryption(value):
    value = _object(value)
    encrypted = _bool(value.get('encrypted'))
    if encrypted:
        if (set(value) != {'encrypted', 'format_version', 'context_authenticated', 'legacy_unbound'}
                or type(value['format_version']) is not int or value['format_version'] not in (0, 1)
                or _bool(value['context_authenticated']) is not (value['format_version'] == 1)
                or _bool(value['legacy_unbound']) is not (value['format_version'] == 0)):
            raise ValueError('backup encryption metadata is inconsistent')
    elif (set(value) != {'encrypted', 'format_version', 'context_authenticated'}
            or value['format_version'] is not None or value['context_authenticated'] is not False):
        raise ValueError('backup plaintext metadata is inconsistent')
    return dict(value)


def _trust(result):
    names = ('signed', 'signature_ok', 'signature_math_valid', 'signer_trusted',
             'signer_authenticated', 'signer_default')
    trust = {name: _bool(result.get(name)) for name in names}
    key, decision = result.get('publicKey'), result.get('signer_trust')
    if type(decision) is not str or decision not in (
            'unsigned', 'pinned', 'local', 'unknown', 'public_default_local_integrity'):
        raise ValueError('backup signer decision is invalid')
    if trust['signed']:
        _digest(key)
    elif key is not None or any(trust[name] for name in names[1:]) or decision != 'unsigned':
        raise ValueError('unsigned backup cannot carry signer authority')
    if (trust['signature_ok'] and not (trust['signed'] and trust['signature_math_valid'] and trust['signer_trusted'])
            or trust['signer_authenticated'] is not (trust['signature_ok'] and not trust['signer_default'])
            or trust['signer_trusted'] is not (decision in ('pinned', 'local', 'public_default_local_integrity'))
            or decision == 'public_default_local_integrity' and not trust['signer_default']):
        raise ValueError('backup signer facts are inconsistent')
    return {**trust, 'publicKey': key, 'signer_trust': decision}


def _scope(result):
    if (result.get('backup_scope') != 'filesystem_memory_only' or result.get('database_included') is not False
            or result.get('application_consistent_snapshot') is not False):
        raise ValueError('backup receipt scope is invalid')


def publication_result(result):
    result = _object(result)
    _scope(result)
    operation = publication._opid(result.get('operation_id'))
    digest, signature = _digest(result.get('archive_sha256')), _digest(result.get('signature_sha256'))
    encryption, trust = _encryption(result.get('archive_encryption')), _trust(result)
    encrypted = _bool(result.get('encrypted'))
    name = publication.archive_name(operation, encrypted)
    if (result.get('schema') != publication.SCHEMA or result.get('publication_complete') is not True
            or result.get('name') != name or result.get('sha256') != digest
            or encryption['encrypted'] is not encrypted or not trust['signature_ok']):
        raise ValueError('backup publication result is inconsistent')
    return {'operation_id': operation, 'archive_name': name, 'archive_sha256': digest,
        'signature_sha256': signature, 'plan_sha256': _digest(result.get('plan_sha256')),
        'files': _integer(result.get('files'), fmt.MAX_FILES),
        'bytes': _integer(result.get('bytes'), aio.MAX_CIPHERTEXT, 1),
        'publication_returned_complete': True, 'encryption': encryption, 'trust': trust}


def delivery_result(result, published):
    result, published = _object(result), _object(published)
    status = result.get('status')
    if (type(status) is not str or status not in ('command_pair_completed', 'not_configured', 'explicitly_skipped')
            or result.get('publication_id') != published['operation_id']
            or result.get('remote_retention_verified') is not False):
        raise ValueError('backup delivery result is inconsistent')
    complete = _bool(result.get('delivery_commands_complete'))
    if status == 'command_pair_completed':
        operation = publication._opid(result.get('delivery_id'))
        plan = _digest(result.get('delivery_plan_sha256', result.get('plan_sha256')))
        digest = result.get('intended_archive_sha256', result.get('archive_sha256'))
        signature = result.get('intended_signature_sha256', result.get('signature_sha256'))
        if (not complete or digest != published['archive_sha256'] or signature != published['signature_sha256']
                or result.get('archive_sha256') != digest or result.get('signature_sha256') != signature):
            raise ValueError('backup delivery does not bind both publication images')
    else:
        if complete or result.get('delivery_id') is not None:
            raise ValueError('skipped backup delivery cannot claim command completion')
        operation, plan = None, None
    return {'status': status, 'publication_id': published['operation_id'], 'delivery_id': operation,
        'plan_sha256': plan, 'archive_sha256': published['archive_sha256'],
        'signature_sha256': published['signature_sha256'], 'delivery_commands_complete': complete,
        'remote_retention_verified': False}


def restore_result(result, *, archive_name):
    result = _object(result)
    _scope(result)
    aio._filename(archive_name)
    operation = publication._opid(result.get('operation_id'))
    digest = _digest(result.get('consumed_archive_sha256'))
    signature = _digest(result.get('signature_sha256'), optional=True)
    trust = _trust(result)
    override = _bool(result.get('signature_override'))
    if (result.get('restore_complete') is not True or result.get('sha256') != digest
            or trust['signed'] is not (signature is not None)
            or override is not (not trust['signature_ok'])):
        raise ValueError('backup restore result is inconsistent')
    local = _object(result.get('local_publication'))
    if local.get('kind') == 'local_completed':
        if (set(local) != {'kind', 'operation_id', 'plan_sha256', 'archive_sha256', 'signature_sha256'}
                or local['archive_sha256'] != digest or local['signature_sha256'] != signature):
            raise ValueError('restore local publication differs from the consumed image')
        publication._opid(local['operation_id'])
        _digest(local['plan_sha256'])
    elif local not in ({'kind': 'external_pair'}, {'kind': 'legacy_local'}):
        raise ValueError('restore publication provenance is invalid')
    return {'operation_id': operation, 'archive_name': archive_name, 'archive_sha256': digest,
        'signature_sha256': signature, 'plan_sha256': _digest(result.get('plan_sha256')),
        'restored_files': _integer(result.get('restored'), fmt.MAX_FILES),
        'restore_returned_complete': True, 'signature_override': override,
        'encryption': _encryption(result.get('archive_encryption')), 'trust': trust,
        'local_publication': dict(local)}


def _json(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')
    if len(raw) > MAX_RECEIPT:
        raise ValueError('backup evidence exceeds its receipt bound')
    return raw


def _publication_summary(value):
    value = _object(value)
    expected = {'operation_id', 'archive_name', 'archive_sha256', 'signature_sha256', 'plan_sha256',
                'files', 'bytes', 'publication_returned_complete', 'encryption', 'trust'}
    if set(value) != expected or value['publication_returned_complete'] is not True:
        raise ValueError('invalid publication summary')
    original = {'schema': publication.SCHEMA, 'operation_id': value['operation_id'],
        'name': value['archive_name'], 'archive_sha256': value['archive_sha256'],
        'signature_sha256': value['signature_sha256'], 'plan_sha256': value['plan_sha256'],
        'files': value['files'], 'bytes': value['bytes'], 'publication_complete': True,
        'sha256': value['archive_sha256'], 'archive_encryption': value['encryption'],
        'encrypted': _object(value['encryption']).get('encrypted'), **_object(value['trust']),
        'backup_scope': 'filesystem_memory_only', 'database_included': False,
        'application_consistent_snapshot': False}
    selected = publication_result(original)
    if selected != value or set(value['trust']) != set(selected['trust']):
        raise ValueError('noncanonical publication summary')
    return selected


def _delivery_summary(value, published):
    value = _object(value)
    keys = {'status', 'publication_id', 'delivery_id', 'plan_sha256', 'archive_sha256',
            'signature_sha256', 'delivery_commands_complete', 'remote_retention_verified'}
    if set(value) != keys:
        raise ValueError('invalid delivery summary')
    selected = delivery_result(value, published)
    if selected != value:
        raise ValueError('noncanonical delivery summary')
    return selected


def _retirement_summary(value):
    value = _object(value)
    expected = {'retired_operations', 'pending_retirement_ids', 'observed_retired_ids',
                'batch_returned_complete', 'files_deleted', 'bytes_reclaimed', 'history_capacity_reclaimed'}
    if (set(value) != expected or value['batch_returned_complete'] is not True
            or any(_integer(value[name], 0) != 0 for name in
                   ('files_deleted', 'bytes_reclaimed', 'history_capacity_reclaimed'))):
        raise ValueError('invalid retirement summary')
    for name in ('retired_operations', 'pending_retirement_ids', 'observed_retired_ids'):
        rows = value[name]
        if (type(rows) is not list or len(rows) > publication.MAX_OPERATIONS
                or any(not facts.operation_id(row) for row in rows) or len(rows) != len(set(rows))):
            raise ValueError('invalid bounded retirement IDs')
    return {name: list(item) if type(item) is list else item for name, item in value.items()}


def _restore_summary(value):
    value = _object(value)
    expected = {'operation_id', 'archive_name', 'archive_sha256', 'signature_sha256', 'plan_sha256',
        'restored_files', 'restore_returned_complete', 'signature_override', 'encryption', 'trust', 'local_publication'}
    if set(value) != expected or value['restore_returned_complete'] is not True:
        raise ValueError('invalid restore summary')
    original = {'operation_id': value['operation_id'], 'consumed_archive_sha256': value['archive_sha256'],
        'sha256': value['archive_sha256'], 'signature_sha256': value['signature_sha256'],
        'plan_sha256': value['plan_sha256'], 'restored': value['restored_files'], 'restore_complete': True,
        'signature_override': value['signature_override'], 'archive_encryption': value['encryption'],
        'local_publication': value['local_publication'], **_object(value['trust']),
        'backup_scope': 'filesystem_memory_only', 'database_included': False,
        'application_consistent_snapshot': False}
    selected = restore_result(original, archive_name=value['archive_name'])
    if selected != value or set(value['trust']) != set(selected['trust']):
        raise ValueError('noncanonical restore summary')
    return selected


def _identity(value):
    if type(value) is not list or len(value) != 2:
        raise ValueError('invalid native evidence identity')
    return [_integer(item, (1 << 128) - 1) for item in value]


def _payload(value):
    value = _object(value)
    common = {'schema', 'kind', 'operation_id', 'recorded_at', 'commit', 'memory_directory',
        'memory_identity', 'backup_scope', 'database_included', 'application_consistent_snapshot',
        'enclosing_workflow_complete', 'current_archive_availability_verified', 'remote_retention_verified'}
    kind = value.get('kind')
    if type(kind) is not str or kind not in ('backup', 'restore'):
        raise ValueError('invalid evidence kind')
    expected = common | ({'publication', 'delivery', 'retirement'} if kind == 'backup' else {'restore'})
    if (set(value) != expected or value['schema'] != SCHEMA
            or any(value[name] is not False for name in ('enclosing_workflow_complete',
                'current_archive_availability_verified', 'remote_retention_verified'))):
        raise ValueError('invalid evidence receipt scope or fields')
    _scope(value)
    operation = publication._opid(value['operation_id'])
    _integer(value['recorded_at'], 253402300799, 1)
    if (type(value['commit']) is not str or not (value['commit'] == 'unknown' or _COMMIT.fullmatch(value['commit']))
            or type(value['memory_directory']) is not str or len(value['memory_directory']) > 8192):
        raise ValueError('invalid evidence build or directory binding')
    _identity(value['memory_identity'])
    if kind == 'backup':
        selected = _publication_summary(value['publication'])
        _delivery_summary(value['delivery'], selected)
        _retirement_summary(value['retirement'])
    else:
        selected = _restore_summary(value['restore'])
    if selected['operation_id'] != operation:
        raise ValueError('evidence operation ID differs from its stage')
    return value


class _Store:
    """Own only the native child and its lock; borrow the caller's memory root."""
    def __init__(self, memory, *, initialize):
        if type(memory) is not _Directory or type(initialize) is not bool:
            raise ValueError('backup evidence requires exact native authority')
        self.memory, self.control, self.fd = memory, None, None
        self.pid, self.thread = os.getpid(), threading.get_ident()
        self.closed, self.held = False, False
        self._cleanup_unconfirmed = False
        self.marker, self.marker_identity = None, None
        failure = None
        try:
            memory.check()
            memory._case_spelling(CONTROL)
            created = False
            try:
                self.control = memory.child(CONTROL)
            except FileNotFoundError as error:
                if native_cleanup.unknown(error): raise
                memory.check()
                if not initialize:
                    raise
                self.control = _exclusive_child(memory, CONTROL)
                created = True
            self.fd = self.control._open('evidence.lock', os.O_RDWR | (os.O_CREAT | os.O_EXCL if created else 0))
            self.lock_identity = list(_file_identity(_file_information(self.fd))[:2])
            if not _try_lock(self.fd):
                raise ValueError('backup evidence namespace is busy')
            self.held = True
            expected = {'schema': SCHEMA, 'memory_directory': str(memory.path),
                'memory_identity': list(_directory_identity(memory)),
                'control_identity': list(_directory_identity(self.control)), 'lock_identity': self.lock_identity}
            if created:
                os.fsync(self.fd)
                self.control.sync()
                self.control.write_new('initialization.json', _json(expected))
            raw, identity = self.control.read_with_identity('initialization.json', MAX_RECEIPT)
            if fmt.strict_json(raw, MAX_RECEIPT) != expected:
                raise ValueError('backup evidence initialization differs from original authority')
            self.marker, self.marker_identity = raw, [identity['device'], identity['inode']]
            self.check()
        except BaseException as error:
            failure = error
        if failure is not None:
            native_cleanup.drain_owned(self, [self.close], failure)
            raise failure

    def check(self):
        if self._cleanup_unconfirmed:
            error = ValueError('backup evidence cleanup requires reconciliation')
            native_cleanup.mark(error)
            raise error
        if (self.closed or not self.held or self.fd is None
                or (self.pid, self.thread) != (os.getpid(), threading.get_ident())):
            raise ValueError('backup evidence authority is inactive or foreign')
        self.memory.check()
        self.control.check()
        if list(_file_identity(_file_information(self.fd))[:2]) != self.lock_identity:
            raise ValueError('backup evidence lock identity changed')
        if self.control.win is None:
            info = os.stat('evidence.lock', dir_fd=self.control.fd, follow_symlinks=False)
            if list(_file_identity(info)[:2]) != self.lock_identity:
                raise ValueError('backup evidence lock path changed')
        if self.marker is not None:
            raw, identity = self.control.read_with_identity('initialization.json', MAX_RECEIPT)
            if raw != self.marker or [identity['device'], identity['inode']] != self.marker_identity:
                raise ValueError('backup evidence initialization changed')

    def rows(self):
        self.check()
        names = publication._names(self.control, MAX_RECEIPTS + 2)
        if len(set(name.casefold() for name in names)) != len(names):
            raise ValueError('backup evidence spelling is ambiguous')
        if not {'initialization.json', 'evidence.lock'} <= set(names):
            raise ValueError('backup evidence control files are missing')
        rows, total = {}, 0
        for name in sorted(set(names) - {'initialization.json', 'evidence.lock'}):
            match = _NAME.fullmatch(name)
            if match is None:
                raise ValueError('backup evidence has an unknown retained entry')
            raw, _identity_info = self.control.read_with_identity(name, min(MAX_RECEIPT, MAX_TOTAL - total))
            total += len(raw)
            value = _payload(fmt.strict_json(raw, MAX_RECEIPT))
            if (value['kind'] != match[1] or value['operation_id'] != match[2]
                    or value['memory_directory'] != str(self.memory.path)
                    or value['memory_identity'] != list(_directory_identity(self.memory))):
                raise ValueError('backup evidence receipt binding differs')
            rows[name] = (value, raw)
        self.check()
        return rows, total

    def put(self, value):
        value = _payload(value)
        name = value['kind'] + '-' + value['operation_id'] + '.json'
        raw = _json(value)
        rows, total = self.rows()
        if name in rows or len(rows) >= MAX_RECEIPTS or len(raw) > MAX_TOTAL - total:
            raise ValueError('backup evidence ID exists or retained capacity is exhausted')
        # Repeat initialization barriers before relying on a visible marker
        # whose first writer may have lost an acknowledgment.
        marker_fd = self.control._open('initialization.json', os.O_RDWR)
        primary = None
        try:
            if (list(_file_identity(_file_information(marker_fd))[:2]) != self.marker_identity
                    or os.read(marker_fd, len(self.marker) + 1) != self.marker):
                raise ValueError('backup evidence initialization changed before confirmation')
            os.fsync(marker_fd)
            self.check()
        except BaseException as error:
            primary = error
            raise
        finally:
            owned, marker_fd = marker_fd, None
            native_cleanup.drain_owned(self, [lambda: os.close(owned)], primary)
        os.fsync(self.fd)
        self.control.sync()
        self.memory.sync()
        self.check()
        self.control.write_new(name, raw)
        observed_raw, _identity_info = self.control.read_with_identity(name, len(raw))
        if observed_raw != raw:
            raise ValueError('backup evidence verification differs from its written bytes')
        self.check()
        return _hash(raw)

    def close(self):
        if (self.pid, self.thread) != (os.getpid(), threading.get_ident()):
            raise ValueError('backup evidence cleanup belongs to another process or thread')
        if self.closed:
            if self._cleanup_unconfirmed:
                error = ValueError('backup evidence cleanup remains unconfirmed')
                native_cleanup.mark(error)
                raise error
            return
        self.closed = True
        fd, held, self.fd, self.held = self.fd, self.held, None, False
        child, self.control = self.control, None
        actions = []
        if fd is not None:
            if held:actions.append(lambda: _unlock(fd))
            actions.append(lambda: os.close(fd))
        if child is not None:actions.append(child.close)
        native_cleanup.drain_owned(self, actions)
        if self._cleanup_unconfirmed:
            error = ValueError('backup evidence cleanup remains unconfirmed')
            native_cleanup.mark(error)
            raise error


@contextmanager
def _store(memory, *, initialize):
    store, failure = None, None
    try:
        store = _Store(memory, initialize=initialize)
        yield store
    except BaseException as error:
        failure = error
    finally:
        if store is not None:
            try:
                native_cleanup.drain_owned(store, [store.close], failure)
            except BaseException as error:
                failure = error
    if failure is not None:
        raise failure


def _commit():
    try:
        build = config.build_info()
        value = build.get('commit') if type(build) is dict else None
    except Exception:
        value = None
    return value if type(value) is str and _COMMIT.fullmatch(value) else 'unknown'


def _record(kind, operation, stages, captured):
    captured = require(captured, 'evidence')
    receipt = {'schema': SCHEMA, 'kind': kind, 'operation_id': operation,
        'recorded_at': time.time_ns() // 1_000_000_000, 'commit': _commit(),
        'memory_directory': str(captured.memory.path),
        'memory_identity': list(_directory_identity(captured.memory)),
        'backup_scope': 'filesystem_memory_only', 'database_included': False,
        'application_consistent_snapshot': False, 'enclosing_workflow_complete': False,
        'current_archive_availability_verified': False, 'remote_retention_verified': False, **stages}
    _payload(receipt)
    digest = stages['publication' if kind == 'backup' else 'restore']['archive_sha256']
    acknowledgment, result, failure = None, None, None
    try:
        require(captured, 'evidence')
        with aio.operation('backup', selectors={}, capture_key=False):
            with _store(captured.memory, initialize=True) as store:
                require(captured, 'evidence')
                checksum = store.put(receipt)
                # This write+readback+barrier returned. A later authority or
                # cleanup failure must retain this narrower acknowledgment.
                acknowledgment = RecordedEvidence(kind, operation, digest, checksum)
                require(captured, 'evidence')
                result = {'receipt': receipt, 'acknowledgment': acknowledgment.report()}
    except BaseException as error:
        failure = error
    if failure is not None:
        if isinstance(failure, Exception):
            public = EvidenceError(operation)
            facts.carry(failure, public)
            if acknowledgment is not None:
                setattr(public, _ATTRIBUTE, acknowledgment)
            failure = None
            _raise_public_error(public)
        if acknowledgment is not None:
            attributes = facts._attributes(failure)
            if attributes is not None:
                attributes[_ATTRIBUTE] = acknowledgment
        facts.retain_id(failure, operation)
        raise failure
    return result


@dataclass(frozen=True, repr=False)
class _ReturnedStage:
    owner: object
    stage: str
    image: bytes


def _token(captured, stage):
    token = captured._returned_evidence_tokens.get(stage)
    if (type(token) is not _ReturnedStage or token.owner is not captured or token.stage != stage
            or type(token.image) is not bytes or len(token.image) > MAX_RECEIPT):
        raise ValueError('backup returned-stage token belongs to another authority or is invalid')
    return fmt.strict_json(token.image, MAX_RECEIPT)


def _seal(captured, stage, selected):
    if stage in captured._returned_evidence_tokens:
        raise ValueError('backup returned-stage token cannot be replaced')
    token = _ReturnedStage(captured, stage, _json(selected))
    captured._returned_evidence_tokens[stage] = token
    return selected


def _capture_returned(captured, stage, result, *, archive_name=None, destination=None):
    """Trusted coordinator boundary immediately after its actual adapter returns.

    Structural parsing alone is not proof of execution. Only that boundary
    calls this private function; recorders accept its same-authority tokens,
    never arbitrary result dictionaries, paths or observational disk receipts.
    """
    require(captured, stage)
    if captured.mode == 'run' and stage == 'create':
        selected = publication_result(result)
        from pathlib import Path
        child = captured.backups()
        if (child is None or type(result.get('path')) is not str or Path(result['path']) !=
                child.path / selected['archive_name']):
            raise ValueError('returned publication path belongs to another captured root')
    elif captured.mode == 'run' and stage == 'deliver':
        selected = delivery_result(result, _publication_summary(_token(captured, 'create')))
    elif captured.mode == 'run' and stage == 'retire':
        result = _object(result)
        if result.get('batch_complete') is not True:
            raise ValueError('retirement batch has no returned completion')
        selected = _retirement_summary({name: result.get(name) for name in (
            'retired_operations', 'pending_retirement_ids', 'observed_retired_ids',
            'files_deleted', 'bytes_reclaimed', 'history_capacity_reclaimed')} |
            {'batch_returned_complete': True})
        if _integer(result.get('retired_count'), publication.MAX_OPERATIONS) != len(selected['retired_operations']):
            raise ValueError('retirement count differs from its IDs')
    elif captured.mode == 'drill' and stage == 'restore':
        selected = restore_result(result, archive_name=archive_name)
        if (destination is None or type(result.get('into')) is not str
                or captured.bind_directory(result['into']) != destination):
            raise ValueError('returned restore target differs from the coordinator request')
    else:
        raise ValueError('returned-stage token does not match the captured mode')
    return _seal(captured, stage, selected)


def _capture_skipped_delivery(captured):
    captured.check()
    if captured.mode != 'run' or 'deliver' in captured.allowed_stages or captured._stage is not None:
        raise ValueError('delivery was not explicitly disabled in captured authority')
    published = _publication_summary(_token(captured, 'create'))
    selected = delivery_result({'status': 'explicitly_skipped', 'publication_id': published['operation_id'],
        'delivery_id': None, 'delivery_commands_complete': False, 'remote_retention_verified': False}, published)
    return _seal(captured, 'deliver', selected)


def _record_backup(*, _authority):
    captured = require(_authority, 'evidence')
    if captured.mode != 'run':
        raise ValueError('backup evidence requires the captured backup workflow')
    selected = _publication_summary(_token(captured, 'create'))
    return _record('backup', selected['operation_id'], {'publication': selected,
        'delivery': _delivery_summary(_token(captured, 'deliver'), selected),
        'retirement': _retirement_summary(_token(captured, 'retire'))}, captured)


def _record_restore(*, _authority):
    captured = require(_authority, 'evidence')
    if captured.mode != 'drill':
        raise ValueError('restore evidence requires the captured restore workflow')
    selected = _restore_summary(_token(captured, 'restore'))
    return _record('restore', selected['operation_id'], {'restore': selected}, captured)


def inspect():
    """Observe bounded retained receipts; do not reconstruct lost acknowledgments.

    This reads only public metadata. Missing evidence is an empty observation;
    damaged, copied, oversize or busy evidence is an error. No files are added,
    overwritten, removed, chmodded or settled by inspection.
    """
    from .backup import _held_backup_directory
    selected = aio.selected_directory(config.MEMORY_DIR)
    failure, result = None, None
    try:
        with aio.operation('backup', selectors={}, capture_key=False):
            with _held_backup_directory(selected) as memory:
                memory._case_spelling(CONTROL)
                try:
                    control = memory.child(CONTROL)
                except FileNotFoundError as error:
                    if native_cleanup.unknown(error): raise
                    memory.check()
                    rows = []
                else:
                    # First lookup establishes presence only; _Store captures
                    # and validates the current namespace at read admission.
                    owned, control = control, None
                    native_cleanup.drain([owned.close])
                    with _store(memory, initialize=False) as store:
                        retained, _total = store.rows()
                        rows = [{'receipt': value, 'receipt_sha256': _hash(raw),
                                 'receipt_visible': True, 'write_acknowledgment_observed': None}
                                for value, raw in retained.values()]
                memory.check()
                if aio.selected_directory(config.MEMORY_DIR) != selected:
                    raise ValueError('backup evidence memory selection changed')
                result = {'schema': SCHEMA, 'observational_only': True, 'receipts': rows,
                    'enclosing_workflow_complete': False, 'current_archive_availability_verified': False,
                    'remote_retention_verified': False, 'completion_barriers_reconfirmed': False}
    except BaseException as error:
        failure = error
    if failure is not None:
        if isinstance(failure, Exception):
            public = EvidenceError()
            facts.carry(failure, public)
            failure = None
            _raise_public_error(public)
        raise failure
    return result

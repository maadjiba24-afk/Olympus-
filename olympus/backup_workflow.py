"""Captured backup workflows with distinct acknowledged and returned outcomes.

All stage adapters borrow one original native authority. Returned success is
recorded before a stage exit or final authority cleanup can fail. A retained
acknowledgment alone never upgrades an unsuccessful enclosing operation.
"""
from __future__ import annotations

from pathlib import Path
import tempfile
import uuid

from . import backup_authority as authority, backup_facts as acknowledgments
from . import backup_publication as publication

SCHEMA = 'olympus-backup-workflow/1'


class WorkflowError(RuntimeError):
    def __init__(self, facts):
        self.workflow_facts = facts
        self.operation_id = facts['failure'].get('observed_operation_id') or facts['requested_operation_id']
        self.into = facts.get('into')
        for name in ('preference_cleanup_unconfirmed', 'native_cleanup_unconfirmed'):
            if facts['failure'].get(name) is True:
                BaseException.__getattribute__(self, '__dict__')['_' + name] = True
        super().__init__('backup workflow did not complete; retain its IDs and recovery evidence')


def _initial(kind):
    return {'schema': SCHEMA, 'kind': kind, 'ok': False, 'stage': 'validation',
        'requested_operation_id': None, 'requested_delivery_id': None,
        'publication_complete': False, 'delivery_commands_complete': False,
        'restore_complete': False, 'retirement_batch_complete': False,
        'evidence_recorded': False, 'confirmed_stages': [],
        'remote_retention_verified': False, 'backup_scope': 'filesystem_memory_only',
        'database_included': False, 'application_consistent_snapshot': False}


def _fact(facts, item):
    """Keep already acknowledged facts if a later private result conflicts."""
    report = item.report()
    key = item.kind, item.operation_id
    previous = next((row for row in facts['confirmed_stages']
                     if (row['kind'], row['operation_id']) == key), None)
    if previous is not None:
        if previous != report:
            raise ValueError('backup stage acknowledgments conflict')
        return
    if len(facts['confirmed_stages']) >= acknowledgments.MAX_FACTS:
        raise ValueError('backup stage acknowledgment capacity reached')
    facts['confirmed_stages'].append(report)


def _delivery_progress(value):
    """Project only bounded known delivery fields, with no exception strings."""
    from . import backup_delivery
    phases = {'validation', 'existing_attempt_refused', 'preparation', 'completion_evidence'} | {
        component + '_' + suffix for component in ('archive', 'signature')
        for suffix in ('intent', 'started_evidence', 'command', 'outcome_evidence')}
    required = {'phase', 'components', 'completion_receipt_confirmed'}
    allowed = required | {'existing_delivery_id_observed', 'blocking_publication_id_observed'}
    if (type(value) is not dict or not required <= set(value) <= allowed
            or type(value['phase']) is not str or value['phase'] not in phases
            or type(value['completion_receipt_confirmed']) is not bool
            or type(value['components']) is not dict or not set(value['components']) <= {'archive', 'signature'}):
        return None
    selected = {'phase': value['phase'], 'completion_receipt_confirmed': value['completion_receipt_confirmed'],
                'components': {}}
    for name in ('existing_delivery_id_observed', 'blocking_publication_id_observed'):
        if name in value:
            if not acknowledgments.operation_id(value[name]):
                return None
            selected[name] = value[name]
    for name, row in value['components'].items():
        if (type(row) is not dict or set(row) != {'intent_confirmed', 'observed_outcome', 'outcome_receipt_confirmed'}
                or type(row['intent_confirmed']) is not bool or type(row['outcome_receipt_confirmed']) is not bool):
            return None
        outcome = row['observed_outcome']
        if outcome is not None:
            try:
                outcome = backup_delivery._outcome(outcome)
            except ValueError:
                return None
        if (outcome is not None and not row['intent_confirmed']
                or row['outcome_receipt_confirmed'] and outcome is None):
            return None
        selected['components'][name] = {**row, 'observed_outcome': outcome}
    return selected


def _failure(facts, error):
    # Read the actual exception dictionary, without invoking user properties.
    attributes = acknowledgments._attributes(error)
    if attributes is None:
        attributes = {}
    observed = {'stage': facts['stage'], 'status': 'completion_unconfirmed'}
    for name in ('preference_cleanup_unconfirmed', 'native_cleanup_unconfirmed'):
        if attributes.get('_' + name) is True:
            observed[name] = True
    value = attributes.get('operation_id')
    if acknowledgments.operation_id(value):
        observed['observed_operation_id'] = value
    for name in ('publication_id', 'delivery_id'):
        value = attributes.get(name)
        if acknowledgments.operation_id(value):
            observed['attempt_' + name] = value
    progress = _delivery_progress(attributes.get('delivery_progress'))
    if progress is not None:
        observed['delivery_progress'] = progress
        old = progress.get('existing_delivery_id_observed')
        if old is not None:
            observed['observed_delivery_id'] = old
        elif any(row['intent_confirmed'] for row in progress['components'].values()):
            value = attributes.get('delivery_id')
            if acknowledgments.operation_id(value):
                observed['observed_delivery_id'] = value
        blocked = progress.get('blocking_publication_id_observed')
        if blocked is not None:
            observed['observed_blocking_publication_id'] = blocked
    for item in acknowledgments.observed(error):
        try:
            _fact(facts, item)
        except ValueError:
            observed['acknowledgment_conflict_or_capacity'] = True
    # The evidence module accepts only its exact, bounded acknowledgment type.
    from . import backup_evidence
    evidence = backup_evidence.observed(error)
    if evidence is not None:
        facts['evidence_acknowledgment'] = evidence.report()
    facts['failure'] = observed
    if not isinstance(error, Exception):
        attributes['workflow_facts'] = facts
        acknowledgments.retain_id(error, facts['requested_operation_id'])
        raise error


def _returned_publication(facts, result, operation_id):
    from . import backup_evidence
    selected = backup_evidence.publication_result(result)
    if selected['operation_id'] != operation_id:
        raise ValueError('backup publication returned another operation ID')
    _fact(facts, acknowledgments.ConfirmedStage('publication', operation_id,
        selected['archive_sha256'], selected['signature_sha256'], selected['plan_sha256']))
    facts['publication'] = result
    facts['publication_complete'] = True
    # Keep only these compatibility fields for local CLI/heartbeat callers.
    for key in ('name', 'path', 'files', 'bytes', 'sha256', 'encrypted', 'signed',
                'signer_authenticated', 'signer_default', 'archive_encryption'):
        facts[key] = result[key]


def _returned_retirement(facts, result):
    keys = {'retired_count', 'retired_operations', 'pending_retirement_ids', 'observed_retired_ids',
            'files_deleted', 'bytes_reclaimed', 'history_capacity_reclaimed', 'batch_complete'}
    if (type(result) is not dict or not keys <= set(result)
            or result['batch_complete'] is not True
            or any(type(result[name]) is not int or result[name] != 0 for name in
                   ('files_deleted', 'bytes_reclaimed', 'history_capacity_reclaimed'))):
        raise ValueError('backup retirement did not confirm its retained-byte batch')
    for name in ('retired_operations', 'pending_retirement_ids', 'observed_retired_ids'):
        values = result[name]
        if (type(values) is not list or len(values) > publication.MAX_OPERATIONS
                or any(not acknowledgments.operation_id(value) for value in values)
                or len(values) != len(set(values))):
            raise ValueError('backup retirement returned invalid operation IDs')
    if (type(result['retired_count']) is not int
            or result['retired_count'] != len(result['retired_operations'])):
        raise ValueError('backup retirement count differs from its returned IDs')
    facts['retirement'] = result
    facts['retirement_batch_complete'] = True


def run(*, full=False, deliver_off=True, operation_id=None, delivery_id=None):
    """Run each captured stage once; ordinary failures return bounded facts."""
    from . import backup, backup_delivery, backup_management, backup_evidence
    facts = _initial('backup')
    failure = None
    try:
        if type(full) is not bool or type(deliver_off) is not bool:
            raise ValueError('backup workflow options require booleans')
        if not deliver_off and delivery_id is not None:
            raise ValueError('a skipped delivery cannot have an attempt ID')
        operation = uuid.uuid4().hex if operation_id is None else publication._opid(operation_id)
        attempt = (uuid.uuid4().hex if delivery_id is None else publication._opid(delivery_id)) if deliver_off else None
        facts.update(requested_operation_id=operation, requested_delivery_id=attempt)
        from . import prefs
        prefs._backup_collection_preflight()
        facts['stage'] = 'capture_authority'
        with authority.capture('run', include_delivery=deliver_off) as captured:
            facts['stage'] = 'create'
            with captured.stage('create'):
                created = backup._create(full=full, operation_id=operation, _authority=captured)
                _returned_publication(facts, created, operation)
                backup_evidence._capture_returned(captured, 'create', created)
            facts['stage'] = 'deliver'
            if deliver_off:
                with captured.stage('deliver'):
                    sent = backup_delivery._deliver(created['path'], delivery_id=attempt, _authority=captured)
                    selected = backup_evidence.delivery_result(sent, backup_evidence.publication_result(created))
                    if selected['delivery_id'] not in (None, attempt):
                        raise ValueError('backup delivery returned another attempt ID')
                    if selected['delivery_commands_complete']:
                        _fact(facts, acknowledgments.ConfirmedStage('delivery', attempt,
                            selected['archive_sha256'], selected['signature_sha256'],
                            selected['plan_sha256'], operation))
                    facts['delivery'] = selected
                    facts['delivery_commands_complete'] = selected['delivery_commands_complete']
                    backup_evidence._capture_returned(captured, 'deliver', sent)
            else:
                facts['delivery'] = backup_evidence._capture_skipped_delivery(captured)
            facts['stage'] = 'retire'
            with captured.stage('retire'):
                retired = backup_management._retire_backups(_authority=captured)
                _returned_retirement(facts, retired)
                backup_evidence._capture_returned(captured, 'retire', retired)
            facts['stage'] = 'evidence'
            with captured.stage('evidence'):
                receipt = backup_evidence._record_backup(_authority=captured)
                facts['evidence'] = receipt
                facts['evidence_recorded'] = True
            facts['stage'] = 'release_authority'
        facts.update(ok=True, stage='complete',
            status='commands_completed' if facts['delivery_commands_complete'] else 'local_only')
    except BaseException as error:
        failure = error
    if failure is not None:
        _failure(facts, failure)
    return facts


def drill(archive_path=None, *, into=None, external=False, operation_id=None):
    """Restore one consumed image into an offline target and retain recovery."""
    from . import backup, backup_management, backup_evidence
    from .backup_restore import _raise_public_error
    facts = _initial('restore_drill')
    failure = None
    try:
        if type(external) is not bool:
            raise ValueError('external restore selection requires a boolean')
        operation = uuid.uuid4().hex if operation_id is None else publication._opid(operation_id)
        facts['requested_operation_id'] = operation
        facts['stage'] = 'capture_authority'
        with authority.capture('drill') as captured:
            facts['stage'] = 'catalog'
            if archive_path is None:
                if external:
                    raise ValueError('external drill requires an explicit archive')
                with captured.stage('catalog'):
                    candidates = backup_management._list_backups(_authority=captured)
                    selected = next((row for row in candidates if row.get('selection_candidate') is True), None)
                    if selected is None:
                        raise ValueError('no managed completed archive is eligible for a drill')
                    archive = captured.bind_file(selected['path'])
            else:
                archive = captured.bind_file(archive_path)
            facts['stage'] = 'prepare_offline_destination'
            if into is None:
                parent = Path(tempfile.mkdtemp(prefix='olympus-restore-drill-'))
                facts['retained_parent'] = str(parent)
                destination = parent / 'restored'
            else:
                destination = captured.bind_directory(into)
            facts.update(into=str(destination), archive=str(archive), archive_name=archive.name)
            facts['stage'] = 'restore'
            with captured.stage('restore'):
                restored = backup._restore(str(archive), into=destination, external=external,
                                           operation_id=operation, _authority=captured)
                selected = backup_evidence.restore_result(restored, archive_name=archive.name)
                if selected['operation_id'] != operation:
                    raise ValueError('restore returned another operation ID')
                _fact(facts, acknowledgments.ConfirmedStage('restore', operation,
                    selected['archive_sha256'], selected['signature_sha256'], selected['plan_sha256']))
                facts.update(restore=restored, restore_complete=True,
                             sha256=selected['archive_sha256'])
                backup_evidence._capture_returned(captured, 'restore', restored,
                    archive_name=archive.name, destination=destination)
            facts['stage'] = 'evidence'
            with captured.stage('evidence'):
                receipt = backup_evidence._record_restore(_authority=captured)
                facts.update(receipt=receipt, evidence_recorded=True)
            facts['stage'] = 'release_authority'
        facts.update(ok=True, stage='complete')
    except BaseException as error:
        failure = error
    if failure is not None:
        _failure(facts, failure)
        failure = None
        _raise_public_error(WorkflowError(facts))
    return facts

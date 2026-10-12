"""Explicit local backup recovery and truthful command-stage reporting.

Local inspection/recovery is reachable before encrypted configuration
hydration. Ordinary backup creation and uploader dispatch retain service boot
checks. This module never changes custody, activates providers or retries a
delivery. Recovery IDs are flushed before any mutating action.
"""
from __future__ import annotations

import json
import sys
import uuid


class CommandRefused(ValueError):
    pass


def add_backup_parser(sub):
    parser = sub.add_parser('backup', help='create a signed filesystem archive and optionally run its uploader commands')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--list', action='store_true', help='observe retained local backup states')
    action.add_argument('--drill', nargs='?', const='', metavar='ARCHIVE',
        help='restore this archive or the newest eligible local archive offline; retain the recovery directory')
    parser.add_argument('--full', action='store_true', help='include reproducible replay caches')
    parser.add_argument('--no-deliver', action='store_true', help='create locally without invoking uploader commands')
    parser.add_argument('--operation-id', help='lowercase UUID32 for creation or the offline restore drill')
    parser.add_argument('--delivery-id', help='lowercase UUID32 for a new uploader attempt')
    parser.add_argument('--into', help='explicit offline drill target; otherwise allocate and retain a new parent')
    parser.add_argument('--external', action='store_true', help='drill an explicitly named external pair without a local publication journal')
    return parser


def add_recovery_parsers(sub):
    restore = sub.add_parser('restore', help='verify one archive image and restore into an explicit offline target')
    restore.add_argument('archive', help='archive whose exact consumed bytes will be verified')
    restore.add_argument('--into', required=True, help='absent offline target, or an intact restore-owned target with --force')
    restore.add_argument('--operation-id', help='lowercase UUID32 for this offline restore')
    restore.add_argument('--force', action='store_true', help='permit another operation only in an intact restore-owned root')
    restore.add_argument('--insecure', action='store_true',
        help='override a well-formed signature decision; custody, archive bounds and file integrity remain mandatory')
    restore.add_argument('--external', action='store_true', help='explicit external pair; do not require a local publication journal')
    recovery = sub.add_parser('backup-evidence', help='inspect retained backup evidence or explicitly settle one local recovery operation')
    recovery.add_argument('action', choices=('list', 'receipts', 'publication-inspect', 'publication-resume',
        'publication-retire', 'delivery-inspect', 'restore-inspect', 'restore-resume'))
    recovery.add_argument('--operation-id', help='retained lowercase UUID32 for publication or restore recovery')
    recovery.add_argument('--archive', help='managed archive for delivery evidence inspection')
    recovery.add_argument('--into', help='retained offline restore root for restore inspection or resumption')
    return restore, recovery


def is_local(args):
    return (args.command in ('backup-evidence', 'restore')
            or args.command == 'backup' and (args.list or args.drill is not None))


def _emit(value):
    print(json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False), flush=True)


def _report_failure(output):
    """A broken output stream must not repeat work or expose raw error text."""
    try:
        _emit(output)
    except BaseException:
        try:
            print(json.dumps(output, ensure_ascii=True, sort_keys=True, allow_nan=False),
                  file=sys.stderr, flush=True)
        except BaseException:
            pass  # Preserve the primary failure/interruption if both streams fail.


def _id(value, *, required=False, allocate=False):
    from . import backup_facts
    if value is None:
        if required:
            raise CommandRefused('this action requires its retained --operation-id')
        return uuid.uuid4().hex if allocate else None
    if not backup_facts.operation_id(value):
        raise CommandRefused('operation identities must be 32 lowercase hexadecimal characters')
    return value


def _publication(action, operation_id):
    from . import archive_io as aio, backup_management as management
    from . import backup_publication as publication, backup_facts as facts
    confirmed, result, failure = (), None, None
    try:
        with aio.operation('backup', selectors={}, capture_key=False):
            with management._directories() as (_memory, directory):
                if directory is None:
                    raise CommandRefused('the selected local backup directory is absent')
                if action == 'publication-inspect':
                    result = publication.inspect(directory, operation_id=operation_id)
                else:
                    result = (publication.resume(directory, operation_id=operation_id)
                        if action == 'publication-resume' else publication.retire(directory, operation_id=operation_id))
                    confirmed = (facts.ConfirmedStage('publication' if action == 'publication-resume' else 'retirement',
                        result['operation_id'], result['archive_sha256'], result['signature_sha256'], result['plan_sha256']),)
    except BaseException as error:
        failure = error
    if failure is not None:
        facts.carry(failure, failure, confirmed)
        facts.retain_id(failure, operation_id)
        raise failure
    return {**result, 'current_signer_verification_performed': False,
            'current_decryption_performed': False, 'remote_retention_verified': False}




def _evidence_report(row):
    from . import backup_evidence as evidence
    if type(row) is not dict:
        raise ValueError('invalid evidence acknowledgment report')
    report = evidence.RecordedEvidence(*(row.get(name) for name in
        ('kind', 'operation_id', 'archive_sha256', 'receipt_sha256'))).report()
    if (any(type(row.get(name)) is not bool for name in ('receipt_write_acknowledgment_observed',
            'enclosing_workflow_complete', 'current_archive_availability_verified', 'remote_retention_verified'))
            or row != report):
        raise ValueError('invalid evidence acknowledgment report')
    return report


def _workflow_report(value):
    """Transport known returned/acknowledged facts, without error text or I/O.

    Parsing transports a trusted workflow's report; it does not establish
    execution, settle interrupted work, or revalidate present disk state.
    """
    from . import backup_facts as facts, backup_workflow as workflow, backup_evidence as evidence
    if type(value) is not dict or value.get('schema') != workflow.SCHEMA:
        return None
    kind, stage = value.get('kind'), value.get('stage')
    stages = {'validation', 'capture_authority', 'create', 'deliver', 'retire', 'catalog',
              'prepare_offline_destination', 'restore', 'evidence', 'release_authority'}
    if type(kind) is not str or kind not in ('backup', 'restore_drill') or type(stage) is not str or stage not in stages:
        return None
    flags = ('ok', 'publication_complete', 'delivery_commands_complete', 'restore_complete',
             'retirement_batch_complete', 'evidence_recorded', 'remote_retention_verified',
             'database_included', 'application_consistent_snapshot')
    if (any(type(value.get(name)) is not bool for name in flags) or value['ok']
            or any(value[name] for name in ('remote_retention_verified', 'database_included', 'application_consistent_snapshot'))
            or value.get('backup_scope') != 'filesystem_memory_only'):
        return None
    selected = {'schema': workflow.SCHEMA, 'kind': kind, 'stage': stage,
                'backup_scope': 'filesystem_memory_only', **{name: value[name] for name in flags}}
    for name in ('requested_operation_id', 'requested_delivery_id'):
        item = value.get(name)
        if item is not None and not facts.operation_id(item):
            return None
        selected[name] = item
    try:
        rows = value.get('confirmed_stages')
        if type(rows) is not list or len(rows) > facts.MAX_FACTS:
            return None
        reports = []
        for row in rows:
            if type(row) is not dict:
                return None
            item = facts.ConfirmedStage(*(row.get(name) for name in
                ('kind', 'operation_id', 'archive_sha256', 'signature_sha256', 'plan_sha256', 'publication_id')))
            report = item.report()
            if (any(type(row.get(name)) is not bool for name in ('completion_acknowledgment_observed',
                    'enclosing_operation_complete', 'current_state_revalidated', 'remote_retention_verified'))
                    or row != report):
                return None
            reports.append(report)
        selected['confirmed_stages'] = reports
        if value['publication_complete']:
            selected['publication'] = evidence.publication_result(value.get('publication'))
        if 'delivery' in value:
            if 'publication' not in selected:
                return None
            selected['delivery'] = evidence._delivery_summary(value['delivery'], selected['publication'])
            if selected['delivery']['delivery_commands_complete'] is not value['delivery_commands_complete']:
                return None
        elif value['delivery_commands_complete']:
            return None
        if value['restore_complete']:
            selected['restore'] = evidence.restore_result(value.get('restore'), archive_name=value.get('archive_name'))
        if value['retirement_batch_complete']:
            retired = {}
            workflow._returned_retirement(retired, value.get('retirement'))
            selected['retirement'] = {name: retired['retirement'][name] for name in
                ('retired_count', 'retired_operations', 'pending_retirement_ids', 'observed_retired_ids',
                 'files_deleted', 'bytes_reclaimed', 'history_capacity_reclaimed', 'batch_complete')}
        if 'sha256' in value:
            if not facts._digest(value['sha256']):
                return None
            selected['sha256'] = value['sha256']
        for name in ('into', 'retained_parent'):
            if name in value:
                if type(value[name]) is not str or len(value[name]) > 8192:
                    return None
                selected[name] = value[name]
        failure = value.get('failure')
        if type(failure) is not dict or failure.get('stage') != stage or failure.get('status') != 'completion_unconfirmed':
            return None
        selected['failure'] = {'stage': stage, 'status': 'completion_unconfirmed'}
        for name in ('preference_cleanup_unconfirmed', 'native_cleanup_unconfirmed'):
            if name in failure:
                if failure[name] is not True:
                    return None
                selected['failure'][name] = True
        for name in ('observed_operation_id', 'observed_delivery_id', 'observed_blocking_publication_id',
                     'attempt_publication_id', 'attempt_delivery_id'):
            if name in failure:
                if not facts.operation_id(failure[name]):
                    return None
                selected['failure'][name] = failure[name]
        if 'delivery_progress' in failure:
            progress = workflow._delivery_progress(failure['delivery_progress'])
            if progress is None:
                return None
            selected['failure']['delivery_progress'] = progress
        if failure.get('acknowledgment_conflict_or_capacity') is True:
            selected['failure']['acknowledgment_conflict_or_capacity'] = True
        if 'evidence_acknowledgment' in value:
            selected['evidence_acknowledgment'] = _evidence_report(value['evidence_acknowledgment'])
        if value['evidence_recorded']:
            returned = value.get('evidence' if kind == 'backup' else 'receipt')
            if type(returned) is not dict or set(returned) != {'receipt', 'acknowledgment'}:
                return None
            report = _evidence_report(returned['acknowledgment'])
            expected_kind = 'backup' if kind == 'backup' else 'restore'
            stage_result = selected.get('publication' if kind == 'backup' else 'restore')
            if (type(stage_result) is not dict or report['kind'] != expected_kind
                    or report['operation_id'] != selected['requested_operation_id']
                    or report['archive_sha256'] != stage_result['archive_sha256']):
                return None
            selected['returned_evidence_acknowledgment'] = report
    except (ValueError, TypeError, KeyError):
        return None
    return selected


def _failure(error, requested):
    from . import backup_facts as facts, backup_workflow as workflow, backup_evidence
    attributes = facts._attributes(error)
    attributes = {} if attributes is None else attributes
    output = {'state': 'completion_unconfirmed', 'requested_operation_id': requested,
              'reason': 'inspect the retained operation before retrying; automatic delivery retry is forbidden',
              'remote_retention_verified': False}
    for name in ('preference_cleanup_unconfirmed', 'native_cleanup_unconfirmed'):
        if attributes.get('_' + name) is True:
            output[name] = True
    if type(error) is CommandRefused:
        output.update(state='refused', reason=str(error))
    retained = _workflow_report(attributes.get('workflow_facts'))
    if retained is not None:
        output['workflow'] = retained
    for name in ('operation_id', 'publication_id', 'delivery_id'):
        value = attributes.get(name)
        if facts.operation_id(value):
            output[('observed_' if name == 'operation_id' else 'attempt_') + name] = value
    confirmed = facts.observed(error)
    if confirmed:
        output['confirmed_stages'] = [item.report() for item in confirmed]
    progress = workflow._delivery_progress(attributes.get('delivery_progress'))
    if progress is not None:
        output['delivery_progress'] = progress
    acknowledgment = backup_evidence.observed(error)
    if acknowledgment is not None:
        output['evidence_acknowledgment'] = acknowledgment.report()
    return output


def scheduled_status(result):
    """Render only fixed text and bounded typed facts for heartbeat callers."""
    from . import backup_facts as facts, backup_workflow
    fallback = {'ok': False, 'message': 'Backup status unavailable; inspect retained operation evidence before retrying.'}
    if type(result) is not dict or result.get('schema') != backup_workflow.SCHEMA or result.get('kind') != 'backup':
        return fallback
    operation = result.get('requested_operation_id')
    if not facts.operation_id(operation):
        return fallback
    flags = ('ok', 'publication_complete', 'delivery_commands_complete', 'retirement_batch_complete',
             'evidence_recorded', 'remote_retention_verified')
    if any(type(result.get(name)) is not bool for name in flags) or result['remote_retention_verified']:
        return fallback
    known_stages = {'validation', 'capture_authority', 'create', 'deliver', 'retire', 'evidence', 'release_authority', 'complete'}
    if type(result.get('stage')) is not str or result['stage'] not in known_stages:
        return fallback
    observed_failure = result.get('failure')
    cleanup_flags = ('preference_cleanup_unconfirmed', 'native_cleanup_unconfirmed')
    if (type(observed_failure) is dict and any(name in observed_failure
            and observed_failure[name] is not True for name in cleanup_flags)):
        return fallback
    cleanup_unknown = (type(observed_failure) is dict
                       and any(observed_failure.get(name) is True for name in cleanup_flags))
    if cleanup_unknown and result['ok']:
        return fallback
    if result['ok']:
        if (result['stage'] != 'complete' or not result['publication_complete']
                or not result['retirement_batch_complete'] or not result['evidence_recorded']):
            return fallback
        delivery = 'uploader command pair completed' if result['delivery_commands_complete'] else 'local only'
        return {'ok': True, 'message': 'Backup ' + operation + ': local publication, logical retention and evidence completed; '
                + delivery + '; remote retention remains unverified.'}
    if result['stage'] == 'complete':
        return fallback
    publication = 'publication returned complete' if result['publication_complete'] else 'publication completion unconfirmed'
    delivery = 'uploader commands returned complete' if result['delivery_commands_complete'] else 'uploader completion unconfirmed or not attempted'
    observed = result.get('failure')
    ids = []
    if type(observed) is dict:
        for field, label in (('observed_operation_id', 'operation'),
                             ('observed_delivery_id', 'delivery attempt'),
                             ('observed_blocking_publication_id', 'blocking publication')):
            value = observed.get(field)
            if facts.operation_id(value):
                ids.append(label + ' ' + value)
    recovery = (' Observed recovery IDs: ' + '; '.join(ids) + '.') if ids else ''
    output = {'ok': False, 'message': 'Backup ' + operation + ' did not complete at ' + result['stage'] + '; '
            + publication + '; ' + delivery + '.' + recovery + ' Retain evidence and inspect IDs before retrying.'}
    if cleanup_unknown:
        for name in cleanup_flags:
            if observed_failure.get(name) is True:
                output[name] = True
        output['message'] += ' Native resource cleanup is unconfirmed; reconcile the process before retrying.'
    return output


def command(args):
    from . import backup, backup_evidence
    requested, delivery_id, returned = None, None, None
    try:
        if args.command == 'backup':
            if args.list:
                if any((args.full, args.no_deliver, args.operation_id, args.delivery_id, args.into, args.external)):
                    raise CommandRefused('listing does not accept creation, delivery or restore options')
                returned = {'state': 'observed', 'backups': backup.list_backups(), 'observational_only': True}
                _emit(returned)
                return 0
            if args.drill is not None:
                if args.full or args.no_deliver or args.delivery_id:
                    raise CommandRefused('a restore drill does not accept creation or delivery options')
                if args.external and not args.drill:
                    raise CommandRefused('an external drill requires an explicit archive path')
                requested = _id(args.operation_id, allocate=True)
                _emit({'state': 'requested', 'action': 'restore_drill', 'operation_id': requested,
                       'retains_offline_recovery': True})
                result = backup.drill(args.drill or None, into=args.into, external=args.external, operation_id=requested)
            else:
                if args.into or args.external:
                    raise CommandRefused('--into and --external apply only to a restore drill')
                if args.no_deliver and args.delivery_id is not None:
                    raise CommandRefused('a skipped delivery cannot have an attempt ID')
                requested = _id(args.operation_id, allocate=True)
                delivery_id = None if args.no_deliver else _id(args.delivery_id, allocate=True)
                _emit({'state': 'requested', 'action': 'backup', 'operation_id': requested,
                       'delivery_id': delivery_id, 'remote_retention_verified': False})
                result = backup.run(full=args.full, deliver_off=not args.no_deliver,
                                    operation_id=requested, delivery_id=delivery_id)
            returned = {'state': 'complete' if result.get('ok') is True else 'completion_unconfirmed', 'workflow': result}
            _emit(returned)
            return 0 if result.get('ok') is True else 1
        if args.command == 'restore':
            requested = _id(args.operation_id, allocate=True)
            _emit({'state': 'requested', 'action': 'restore', 'operation_id': requested, 'offline_target': args.into})
            result = backup.restore(args.archive, into=args.into, force=args.force, insecure=args.insecure,
                                    external=args.external, operation_id=requested)
            returned = {'state': 'complete', 'restore': result, 'remote_retention_verified': False}
            _emit(returned)
            return 0
        if args.command != 'backup-evidence':
            raise CommandRefused('unsupported backup command')
        action = args.action
        publication_action = action in ('publication-inspect', 'publication-resume', 'publication-retire')
        restore_action = action in ('restore-inspect', 'restore-resume')
        if (args.archive is not None and action != 'delivery-inspect'
                or args.into is not None and not restore_action
                or args.operation_id is not None and not (publication_action or restore_action)):
            raise CommandRefused('recovery options must belong to the selected action')
        if restore_action and not args.into:
            raise CommandRefused('restore recovery requires its retained offline --into root')
        if action == 'delivery-inspect' and not args.archive:
            raise CommandRefused('delivery inspection requires --archive')
        requested = _id(args.operation_id, required=publication_action or restore_action)
        if action in ('publication-resume', 'publication-retire', 'restore-resume'):
            _emit({'state': 'requested', 'action': action, 'operation_id': requested})
        if publication_action:
            result = _publication(action, requested)
        elif restore_action:
            result = (backup.inspect_restore(args.into, operation_id=requested) if action == 'restore-inspect'
                      else backup.resume_restore(args.into, operation_id=requested))
        elif action == 'delivery-inspect':
            result = backup.inspect_delivery(args.archive)
        elif action == 'list':
            result = {'backups': backup.list_backups(), 'observational_only': True}
        else:
            result = backup_evidence.inspect()
        returned = {'state': 'complete' if action in ('publication-resume', 'publication-retire', 'restore-resume') else 'observed',
                    'action': action, 'result': result, 'remote_retention_verified': False}
        _emit(returned)
        return 0
    except BaseException as error:
        output = _failure(error, requested)
        if delivery_id is not None:
            output['requested_delivery_id'] = delivery_id
        if returned is not None:
            output.update(state='output_unconfirmed', returned_result=returned,
                reason='the operation returned a result but command output was not confirmed; inspect retained evidence',
                operation_repeated=False)
        from . import backup_facts
        attributes = backup_facts._attributes(error)
        if attributes is not None:
            attributes['backup_command_report'] = output
        if not isinstance(error, Exception):
            _report_failure(output)
            raise
    _report_failure(output)
    return 1

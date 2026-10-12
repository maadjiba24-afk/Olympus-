"""Explicit local preference inspection and retained recovery operations."""
from contextlib import contextmanager
import json


class CommandRefused(ValueError):
    """Fixed, credential-free operator input explanations only."""


def add_parser(sub):
    parser = sub.add_parser('prefs-evidence',
        help='inspect preference evidence or explicitly recover a retained operation')
    parser.add_argument('owner', nargs='?', default=None,
        help="exact owner; defaults to cli for inspection/repair, required for migration")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--repair', action='store_true',
        help='preserve corrupt bytes, then reset the exact owner to safe empty state')
    actions.add_argument('--migrate-legacy', metavar='LEGACY_ID',
        help='explicitly assign one quarantined legacy group to the exact owner')
    actions.add_argument('--discard-legacy', metavar='LEGACY_ID',
        help='explicitly retire one quarantined legacy group without deleting its bytes')
    parser.add_argument('--operation-id',
        help='original 32-character lowercase hexadecimal recovery ID for an exact retry')
    parser.add_argument('--operator-reviewed', action='store_true',
        help='confirm explicit review of the selected legacy ownership/discard action')
    parser.add_argument('--overwrite', action='store_true',
        help='explicitly replace conflicting target entries during legacy migration')
    return parser


def _selection(args):
    from . import prefs
    owner = 'cli' if args.owner is None else args.owner
    if args.overwrite and args.migrate_legacy is None:
        raise CommandRefused('--overwrite requires --migrate-legacy')
    legacy = args.migrate_legacy is not None or args.discard_legacy is not None
    if legacy and not args.operator_reviewed:
        raise CommandRefused('legacy recovery requires --operator-reviewed')
    if args.operator_reviewed and not legacy:
        raise CommandRefused('--operator-reviewed is valid only for legacy recovery')
    if args.discard_legacy is not None:
        if args.owner is not None:
            raise CommandRefused('legacy discard does not select an owner; omit the owner argument')
        return prefs._recovery_request('discard', None, args.discard_legacy, False, args.operation_id)
    if args.migrate_legacy is not None:
        if args.owner is None:
            raise CommandRefused('legacy migration requires an explicit exact owner')
        return prefs._recovery_request('migrate', args.owner, args.migrate_legacy,
                                       args.overwrite, args.operation_id)
    if args.repair:
        return prefs._recovery_request('repair', owner, None, False, args.operation_id)
    if args.operation_id is not None:
        raise CommandRefused('--operation-id requires the original recovery action and selection')
    prefs._selected_owner(owner)
    return None


@contextmanager
def _existing_root():
    """Hold read-only ancestors across output; never create a missing root."""
    from . import archive_io, config, prefs, prefs_native as native
    prefs._assert_not_transform()
    native.require_outside_transaction()
    selected = archive_io.selected_directory(config.MEMORY_DIR)
    directory = native._delegated(native.storage._Directory, selected, create=False)
    primary = None
    try:
        identity = native._delegated(native.storage._directory_identity, directory)
        yield selected, identity, directory
    except BaseException as error:
        primary = error
        raise
    finally:
        native._drain([directory.close], primary)


def _emit(value):
    print(json.dumps(value, ensure_ascii=True, sort_keys=True), flush=True)


def _result(request, result):
    from . import prefs
    result = dict(result)
    if request['kind'] == 'repair':
        output = prefs._status_observed(result.pop('_snapshot'))
        output.update({key: value for key, value in result.items() if key != 'count'})
        output['repaired'] = result['completed']
    else:
        output = {'state': 'complete' if result['completed'] else 'absent',
                  'operation_id': result['operation_id'], 'completed': result['completed'],
                  'historical': result['historical']}
        if request['kind'] == 'migrate':
            output['entries_changed'] = result['count']
        else:
            output['retired'] = bool(result['count'])
    output['requested_operation_id'] = request['operation']
    return output


def _valid_id(value):
    return (type(value) is str and len(value) == 32
            and all(ch in '0123456789abcdef' for ch in value))


def _returned(request, result):
    """Bounded facts only; never retain the preference snapshot or its values."""
    if (type(result) is not dict or type(result.get('completed')) is not bool
            or type(result.get('historical')) is not bool
            or type(result.get('count')) is not int or not 0 <= result['count'] <= 8192):
        raise ValueError('invalid preference recovery completion facts')
    identifier = result.get('operation_id')
    if (identifier is not None and identifier != request['operation']
            or result['completed'] and identifier is None
            or not result['completed'] and identifier is not None):
        raise ValueError('invalid preference recovery completion identity')
    return {key: result[key] for key in ('operation_id', 'completed', 'historical', 'count')}


def _observed_ids(error):
    attributes = BaseException.__getattribute__(error, '__dict__')
    found = []
    if type(attributes) is dict:
        for key in ('operation_id', '_preference_recovery_operation'):
            value = attributes.get(key)
            if _valid_id(value) and value not in found:
                found.append(value)
    return found


def _retain(error, requested, returned, dispatch_started):
    attributes = BaseException.__getattribute__(error, '__dict__')
    if type(attributes) is not dict:
        return
    attributes['_preference_recovery_dispatch_started'] = dispatch_started
    if requested is not None:
        observed = _observed_ids(error)
        attributes['_preference_recovery_requested_operation'] = requested
        if not _valid_id(attributes.get('_preference_recovery_operation')):
            attributes['_preference_recovery_operation'] = observed[0] if observed else requested
    if returned is not None:
        attributes['_preference_recovery_returned'] = dict(returned)


def command(args):
    """Emit an ID before effects; preserve returned facts across later failures."""
    from . import native_cleanup, prefs, prefs_native as native
    identifier, returned = None, None
    dispatch_started = False
    try:
        request = _selection(args)
        if request is None:
            output = prefs.state_status('cli' if args.owner is None else args.owner)
        else:
            identifier = request['operation']
            with _existing_root() as (root, identity, directory):
                _emit({'state': 'requested', 'operation_id': identifier,
                       'retry': 'repeat the same action and selection with this --operation-id'})
                # Output is a callback boundary. Check the original native
                # owner before the transaction can create coordination state.
                native._delegated(directory.check)
                dispatch_started = True
                result = prefs._run_recovery(request, root=root, root_identity=identity)
                returned = _returned(request, result)
                native._delegated(directory.check)
            # Enclosing success is reported only after all held roots close.
            output = _result(request, result)
        _emit(output)
        return 1 if output.get('state') in ('unavailable', 'recovery-pending') or output.get('quarantined') else 0
    except Exception as error:
        _retain(error, identifier, returned, dispatch_started)
        observed = _observed_ids(error)
        output = {'state': 'recovery_required' if dispatch_started else 'refused' if identifier is None else 'unavailable',
                  'operation_id': observed[0] if observed else identifier,
                  'requested_operation_id': identifier,
                  'dispatch_started': dispatch_started,
                  'reason': ('preference command did not confirm completion; preserve the original '
                             'operation ID and repeat only its original action and selection'
                             if dispatch_started else 'preference request or selected root is unavailable; no recovery was dispatched')}
        if len(observed) > 1:
            output['observed_operation_ids'] = observed
        if returned is not None:
            output['recovery_returned'] = dict(returned)
        if isinstance(error, CommandRefused):
            output['reason'] = str(error)
        if native.cleanup_unknown(error) or native_cleanup.unknown(error):
            output['native_cleanup_unconfirmed'] = True
        try:
            _emit(output)
        except BaseException:
            # The primary has already been translated to a fixed refusal.
            # A second broken output stream must not reveal or replace it,
            # even when the reporting callback raises another interrupt.
            attributes = BaseException.__getattribute__(error, '__dict__')
            if type(attributes) is dict:
                attributes['_preference_reporting_unconfirmed'] = True
        return 1
    except BaseException as error:
        # Never mask actual KeyboardInterrupt/SystemExit with diagnostics.
        # Cleanup drained against this same exception; keep bounded facts.
        _retain(error, identifier, returned, dispatch_started)
        raise

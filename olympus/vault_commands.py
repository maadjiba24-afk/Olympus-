"""Explicit local vault format/custody recovery. No provider calls or key writes."""
from __future__ import annotations

from . import native_cleanup
import json
import os
import re
import sys
import uuid


class CommandRefused(ValueError):
    """Only fixed, credential-free operator input explanations."""


def add_parser(sub):
    parser = sub.add_parser('vault-evidence', help='inspect or explicitly recover encrypted vault records')
    parser.add_argument('action', choices=['legacy-list', 'migrate-format', 'rotate',
        'claim', 'discard', 'inspect', 'recover'])
    parser.add_argument('--owner', help='exact owner; required for format migration, rotation and claim')
    parser.add_argument('--operation-id', help='retained 32-character lowercase hexadecimal operation identity')
    parser.add_argument('--legacy-key', help='explicit quarantined source address for claim/discard')
    parser.add_argument('--operator-reviewed', action='store_true', help='confirm explicit legacy ownership/discard review')
    parser.add_argument('--overwrite', action='store_true', help='explicitly replace conflicting target entries during claim')
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--replacement-key-file', help='read an already prepared private replacement passphrase file')
    selection.add_argument('--prompt-replacement-key', action='store_true', help='read replacement custody from a hidden terminal prompt')
    return parser


def _replacement(args):
    from . import vault_custody
    selected = {'OLYMPUS_ENV': os.environ.get('OLYMPUS_ENV', '')}
    if args.replacement_key_file:
        selected['OLYMPUS_SECRET_KEY_FILE'] = args.replacement_key_file
    elif args.prompt_replacement_key:
        import getpass
        import warnings
        if not sys.stdin.isatty():
            raise CommandRefused('replacement custody requires a private terminal or a private key file')
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            value = getpass.getpass('Replacement vault passphrase (hidden): ')
            if args.action == 'rotate' and value != getpass.getpass('Repeat replacement passphrase (hidden): '):
                raise CommandRefused('replacement passphrases differ')
        selected['OLYMPUS_SECRET_KEY'] = value
    else:
        return None
    captured = vault_custody.capture(selected)
    if captured is None:
        raise CommandRefused('replacement custody is empty')
    return captured


def command(args):
    """Print operation identity before mutation; errors never expose key data."""
    from . import vault
    operation_id = args.operation_id
    action = args.action
    cleanup_unconfirmed = False
    try:
        replacing = bool(args.replacement_key_file or args.prompt_replacement_key)
        if args.overwrite and action != 'claim':
            raise CommandRefused('--overwrite is valid only for an explicit legacy claim')
        if replacing and action not in ('rotate', 'inspect', 'recover'):
            raise CommandRefused('replacement custody is only valid for rotation and its inspection/recovery')
        if action == 'rotate' and not replacing:
            raise CommandRefused('rotation requires explicit replacement custody; use migrate-format to keep the current key')
        if action in ('migrate-format', 'rotate', 'claim') and args.owner is None:
            raise CommandRefused('this action requires an explicit exact --owner')
        if action in ('claim', 'discard') and (not args.operator_reviewed or not args.legacy_key):
            raise CommandRefused('claim/discard requires --legacy-key and --operator-reviewed')
        if action in ('inspect', 'recover') and not operation_id:
            raise CommandRefused('inspection/recovery requires the retained --operation-id')
        if action == 'discard' and args.owner is not None:
            raise CommandRefused('discard preserves unassigned legacy bytes; omit --owner')
        if operation_id is not None and re.fullmatch('[0-9a-f]{32}', operation_id) is None:
            raise CommandRefused('invalid operation identity')
        replacement = _replacement(args)
        if action == 'legacy-list':
            output = {'state':'available', 'legacy_keys':vault.legacy_tenants()}
        elif action == 'inspect':
            output = {'state':'available', **vault.inspect_change(operation_id,
                owner=args.owner, replacement_custody=replacement)}
        else:
            operation_id = operation_id or uuid.uuid4().hex
            # Output loss here prevents mutation; retain this ID for inspection
            # and recovery, never blindly launch a new operation on uncertainty.
            print(json.dumps({'state':'requested', 'operation_id':operation_id}), flush=True)
            if action == 'recover':
                output = vault.resume_change(operation_id, owner=args.owner, replacement_custody=replacement)
            elif action in ('migrate-format', 'rotate'):
                output = vault.rotate(args.owner, operation_id=operation_id, replacement_custody=replacement)
            elif action == 'claim':
                count = vault.migrate_legacy(args.legacy_key, args.owner,
                    operation_id=operation_id, overwrite=args.overwrite)
                output = {'operation_id':operation_id, 'committed':True, 'entries_changed':count}
            else:
                changed = vault.discard_legacy(args.legacy_key, operation_id=operation_id)
                output = {'operation_id':operation_id, 'committed':changed,
                          'state':'complete' if changed else 'absent'}
            output = {'state':'complete', **output,
                'custody_source_changed':False,
                'historical_ciphertext_retained':bool(output.get('committed'))}
        print(json.dumps(output, ensure_ascii=True, sort_keys=True), flush=True)
        return 0
    except vault.VaultRecoveryError as exc:
        cleanup_unconfirmed = native_cleanup.unknown(exc)
        output = {'state':'recovery_required', 'operation_id':exc.operation_id,
                  'reason':'retained vault change requires explicit inspection/recovery'}
    except CommandRefused as exc:
        cleanup_unconfirmed = native_cleanup.unknown(exc)
        output = {'state':'refused', 'operation_id':operation_id, 'reason':str(exc)}
    except (vault.VaultError, ValueError) as exc:
        cleanup_unconfirmed = native_cleanup.unknown(exc)
        output = {'state':'refused', 'operation_id':operation_id,
                  'reason':'vault request or custody is invalid or unavailable; inspect a retained operation before retrying'}
    except Exception as exc:
        cleanup_unconfirmed = native_cleanup.unknown(exc)
        output = {'state':'unavailable', 'operation_id':operation_id,
                  'reason':'vault command did not confirm completion; inspect the retained operation before retrying'}
    output['cleanup_unconfirmed'] = cleanup_unconfirmed
    try:
        print(json.dumps(output, ensure_ascii=True, sort_keys=True), flush=True)
    except BaseException as error:
        if cleanup_unconfirmed:
            native_cleanup.mark(error)
        raise
    return 1

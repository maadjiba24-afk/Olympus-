"""One retained, non-retrying command-pair attempt for a managed local backup.

The local journal records intended input images and observed command outcomes.
It never proves remote receipt, retention, or restore availability. A fixed
directory and a separate parent claim consume the publication's one-attempt
slot; partial evidence is retained and never reset. Coordinated removal of all
witnesses by an administrator is outside this cooperative-writer contract.
Only original archive/signature bytes are staged, never decrypted payloads.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import time
import uuid

from . import archive_io as aio, backup_archive as fmt, backup_management as management
from . import backup_process as process, backup_publication as publication, backup_signing as signing
from . import config, backup_facts as stage_facts
from .backup_restore import _exclusive_child, _held_directory, _raise_public_error
from . import native_cleanup

SCHEMA = 'olympus-backup-delivery/1'
CONTROL = 'delivery'
CLAIM = 'delivery-claim.json'
MAX_EVIDENCE = 64 * 1024
MAX_ENTRIES = 32
MAX_SCAN_BYTES = 16 * 1024 * 1024
_OUTCOME_KEYS = frozenset(('exit_code', 'timed_out', 'natural_tree_exit', 'cleanup_confirmed',
    'handles_closed', 'reconciliation_required', 'pid', 'release_attempted', 'release_confirmed'))
_PRIVATE_ENV = frozenset(('OLYMPUS_SECRET_KEY', 'OLYMPUS_SECRET_KEY_FILE',
    'OLYMPUS_SIGNING_SEED', 'OLYMPUS_SIGNING_SEED_FILE'))


class DeliveryError(RuntimeError):
    def __init__(self, publication_id, delivery_id, progress):
        self.publication_id = publication_id
        self.delivery_id = delivery_id
        self.delivery_progress = dict(progress)
        super().__init__('backup delivery did not complete; retain evidence and inspect its publication and delivery IDs; no automatic retry')


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('ascii')
    if len(raw) > MAX_EVIDENCE:
        raise ValueError('backup delivery evidence exceeds its bound')
    return raw


def _outcome(value):
    if type(value) is not dict or set(value) != _OUTCOME_KEYS:
        raise ValueError('backup command outcome has an invalid shape')
    for key in _OUTCOME_KEYS - {'exit_code', 'pid'}:
        if type(value[key]) is not bool:
            raise ValueError('backup command outcome requires exact boolean facts')
    code, pid = value['exit_code'], value['pid']
    if code is not None and (type(code) is not int or not -(2 ** 31) <= code < 2 ** 32):
        raise ValueError('backup command exit status exceeds its bound')
    if pid is not None and (type(pid) is not int or not 0 < pid < 2 ** 32):
        raise ValueError('backup command process identity exceeds its bound')
    if value['release_confirmed'] and not value['release_attempted']:
        raise ValueError('backup command release evidence is contradictory')
    if value['natural_tree_exit'] and (code is None or not value['release_confirmed'] or value['timed_out']):
        raise ValueError('backup command exit evidence is contradictory')
    return dict(value)


def _successful(value):
    return (value['exit_code'] == 0 and value['natural_tree_exit'] and not value['timed_out']
        and value['cleanup_confirmed'] and value['handles_closed'] and not value['reconciliation_required'])


def _native_identity(value):
    return (type(value) is list and len(value) == 2
        and all(type(part) is int and 0 <= part < 2 ** 128 for part in value))


def _native_file_identity(value):
    return (type(value) is list and len(value) == 5
        and all(type(part) is int and 0 <= part < 2 ** 128 for part in value[:4])
        and (value[4] is None or (type(value[4]) is int and 0 <= value[4] < 2 ** 128)))


def _claim(value, publication_id):
    keys = {'schema', 'publication_id', 'delivery_id', 'publication_plan_sha256',
            'delivery_plan_sha256', 'delivery_directory_identity'}
    if (type(value) is not dict or set(value) != keys or value['schema'] != SCHEMA
            or value['publication_id'] != publication_id
            or not publication._digest(value['publication_plan_sha256'])
            or not publication._digest(value['delivery_plan_sha256'])
            or not _native_identity(value['delivery_directory_identity'])):
        raise ValueError('backup delivery claim is invalid')
    publication._opid(value['delivery_id'])
    return value


def _plan(value, root, operation, directory, published_plan, publication_hash):
    keys = {'schema', 'publication_id', 'delivery_id', 'publication_plan_sha256',
        'publication_initialization_sha256', 'publication_operation_identity', 'delivery_directory_identity',
        'archive_name', 'archive_sha256', 'archive_bytes', 'archive_identity',
        'signature_sha256', 'signature_bytes', 'signature_identity',
        'signer_authenticated', 'signer_default', 'publicKey', 'archive_encryption', 'plaintext_allowed', 'created_unix'}
    if (type(value) is not dict or set(value) != keys or value['schema'] != SCHEMA
            or value['publication_id'] != published_plan['operation_id']
            or value['publication_plan_sha256'] != publication_hash
            or value['publication_initialization_sha256'] != _hash(root.marker)
            or not _native_identity(value['publication_operation_identity'])
            or not _native_identity(value['delivery_directory_identity'])
            or value['publication_operation_identity'] != publication._ident(operation)
            or value['delivery_directory_identity'] != publication._ident(directory)
            or value['archive_name'] != published_plan['name']
            or value['signer_authenticated'] is not True or value['signer_default'] is not False
            or type(value['publicKey']) is not str or not signing._KEY.fullmatch(value['publicKey'])
            or type(value['plaintext_allowed']) is not bool or type(value['created_unix']) is not int
            or not 0 <= value['created_unix'] < 2 ** 63):
        raise ValueError('backup delivery plan identity or policy is invalid')
    publication._opid(value['delivery_id'])
    for component in ('archive', 'signature'):
        for suffix in ('sha256', 'bytes'):
            if value[component + '_' + suffix] != published_plan[component + '_' + suffix]:
                raise ValueError('backup delivery image does not match its publication')
        if type(value[component + '_bytes']) is not int:
            raise ValueError('backup delivery image size requires an integer')
        if (not _native_file_identity(value[component + '_identity'])
                or value[component + '_identity'][2] != value[component + '_bytes']):
            raise ValueError('backup delivery image identity is invalid')
    encryption = value['archive_encryption']
    if type(encryption) is not dict:
        raise ValueError('backup delivery encryption metadata is invalid')
    if encryption == aio.encryption_metadata(plaintext=True):
        if (set(encryption) != {'encrypted', 'format_version', 'context_authenticated'}
                or encryption['encrypted'] is not False or encryption['context_authenticated'] is not False
                or encryption['format_version'] is not None or not value['plaintext_allowed']):
            raise ValueError('backup delivery plaintext evidence is invalid')
    elif (set(encryption) != {'encrypted', 'format_version', 'context_authenticated', 'legacy_unbound'}
            or encryption['encrypted'] is not True or type(encryption['format_version']) is not int
            or encryption['format_version'] != 1 or encryption['context_authenticated'] is not True
            or encryption['legacy_unbound'] is not False):
        raise ValueError('backup delivery encryption evidence is invalid')
    if encryption['encrypted'] is not published_plan['details']['encrypted']:
        raise ValueError('backup delivery encryption evidence contradicts publication')
    return value


def _identity(value):
    windows = {'pid', 'created_suspended', 'job_assigned_before_resume', 'kill_on_job_close',
        'breakaway_enabled', 'only_owned_standard_handles_inherited'}
    linux = {'pid', 'pgid', 'sid', 'start_ticks', 'gate_before_exec', 'exclusive_waiter_required'}
    if type(value) is not dict or set(value) not in (windows, linux):
        raise ValueError('backup command identity has an invalid shape')
    integer_fields = set(value) & {'pid', 'pgid', 'sid', 'start_ticks'}
    if any(type(value[key]) is not int or not 0 < value[key] < 2 ** 63 for key in integer_fields):
        raise ValueError('backup command identity exceeds its bound')
    if any(type(value[key]) is not bool for key in set(value) - integer_fields):
        raise ValueError('backup command identity requires exact native flags')
    if set(value) == windows:
        if value['breakaway_enabled'] or not all(value[key] for key in windows - {'pid', 'breakaway_enabled'}):
            raise ValueError('backup command is not in the required Windows job')
    elif (value['pid'] != value['pgid'] or value['pid'] != value['sid']
            or not value['gate_before_exec'] or not value['exclusive_waiter_required']):
        raise ValueError('backup command is not in the required Linux session')
    return dict(value)


@contextmanager
def _publication_authority(directory, publication_id):
    """Preserve consumer facts and keep the publication operation held to exit."""
    failure = None
    try:
        with publication.locked(directory, operation_id=publication_id) as root:
            try:
                with publication._held_operation(root, publication_id) as operation:
                    yield root, operation
                    root.check()
            except BaseException as error:
                failure = error
    except BaseException as error:
        if failure is None:
            failure = error
        else:
            stage_facts.carry(error, failure, stage_facts.observed(failure))
    if failure is not None:
        raise failure


def _put(root, directory, name, raw, check, witnesses=None):
    check()
    directory._case_spelling(name)
    directory.write_new(name, raw)
    check()
    observed, identity = directory.read_with_identity(name, len(raw))
    if observed != raw:
        raise ValueError('backup delivery evidence differs after publication')
    root.check()
    if witnesses is not None and name.endswith('.json'):
        witnesses[(id(directory), name)] = (directory, name, raw, identity)


def _optional(directory, name, cap=MAX_EVIDENCE, budget=None):
    if budget is not None:
        if budget[0] <= 0:
            raise ValueError('backup delivery metadata scan exceeds its aggregate byte budget')
        cap = min(cap, budget[0])
    directory._case_spelling(name)
    try:
        raw, _ = directory.read_with_identity(name, cap)
    except FileNotFoundError as error:
        if native_cleanup.unknown(error): raise
        directory.check()
        return None
    if budget is not None:
        budget[0] -= len(raw)
    return raw


def _command_selection():
    names = ('OLYMPUS_BACKUP_CMD', 'OLYMPUS_BACKUP_ALLOW_PLAINTEXT',
             'OLYMPUS_SECRET_KEY', 'OLYMPUS_SECRET_KEY_FILE')
    return tuple((name, os.environ.get(name)) for name in names)


def _capture_environment():
    """Freeze bounded uploader inputs at entry, omitting private Olympus custody."""
    environment, total = {}, 0
    for key, value in os.environ.items():
        if type(key) is not str or type(value) is not str or len(key) > 32768 or len(value) > 32768:
            raise ValueError('backup uploader environment exceeds its bound')
        if key.upper() in _PRIVATE_ENV:
            continue
        total += len(key.encode('utf-8')) + len(value.encode('utf-8')) + 2
        if len(environment) >= 512 or total > 32768:
            raise ValueError('backup uploader environment exceeds its bound')
        environment[key] = value
    return environment


def _capture_command(selected, cwd, environment):
    permission = dict(selected).get('OLYMPUS_BACKUP_ALLOW_PLAINTEXT')
    if permission is not None and (type(permission) is not str or len(permission) > 16):
        raise ValueError('backup plaintext permission exceeds its bound')
    raw = dict(selected).get('OLYMPUS_BACKUP_CMD') or ''
    if type(raw) is not str or len(raw) > 32768:
        raise ValueError('backup command selection exceeds its bound')
    raw = raw.strip()
    if not raw:
        return None
    argv = shlex.split(raw)
    if not argv or len(argv) > 255 or any(len(value) > 8192 for value in argv):
        raise ValueError('backup command arguments exceed their bound')
    if '{path}' in argv[0]:
        raise ValueError('backup path substitution cannot select the executable')
    # Private vault/signing material and its selected custody paths are never
    # inherited merely because a trusted storage uploader was configured.
    environment = dict(environment)
    executable = argv[0]
    if not os.path.isabs(executable):
        if os.path.dirname(executable):
            executable = str(Path(cwd) / executable)
        else:
            search = environment.get('PATH', '')
            if type(search) is not str or len(search) > 32768:
                raise ValueError('backup command search path exceeds its bound')
            directories = search.split(os.pathsep)
            if any(not entry or not os.path.isabs(entry) for entry in directories):
                raise ValueError('backup command search path must contain absolute directories')
            # Resolve each explicit directory separately; avoid a Windows
            # current-directory search through shutil.which's implicit policy.
            suffixes = ('.exe',) if os.name == 'nt' and not Path(executable).suffix else ('',)
            candidates = [Path(directory) / (executable + suffix) for directory in directories for suffix in suffixes]
            executable = next((str(path) for path in candidates if path.is_file() and os.access(path, os.X_OK)), None)
            if executable is None:
                raise ValueError('backup command executable is unavailable')
    executable = str(Path(executable).resolve(strict=True))
    if not stat.S_ISREG(os.stat(executable).st_mode) or not os.access(executable, os.X_OK):
        raise ValueError('backup command executable is unavailable')
    argv[0] = executable
    # Validate frozen inputs before any delivery slot or large archive buffer.
    process.capture(argv, env=environment, cwd=cwd)
    return tuple(argv), environment


def _for_image(captured, path, cwd):
    argv, environment = captured
    replaced = [value.replace('{path}', str(path)) for value in argv]
    if not any('{path}' in value for value in argv):
        replaced.append(str(path))
    return process.capture(replaced, env=environment, cwd=cwd)


def _image_name(plan, component):
    return plan['archive_name'] + ('.sig.json' if component == 'signature' else '')


def _bound(plan, plan_hash, component):
    return {'schema': SCHEMA, 'publication_id': plan['publication_id'], 'delivery_id': plan['delivery_id'],
        'delivery_plan_sha256': plan_hash, 'component': component,
        'image_sha256': plan[component + '_sha256']}


def _dispatch(root, directory, plan, plan_hash, component, command, check, progress, witnesses):
    binding = _bound(plan, plan_hash, component)
    row = {'intent_confirmed': False, 'observed_outcome': None, 'outcome_receipt_confirmed': False}
    progress['components'][component] = row
    progress['phase'] = component + '_intent'
    _put(root, directory, component + '-intent.json', _json(binding), check, witnesses)
    row['intent_confirmed'] = True
    confirmed_identity = None
    def started(identity):
        nonlocal confirmed_identity
        captured = _identity(identity)
        progress['phase'] = component + '_started_evidence'
        _put(root, directory, component + '-started.json',
            _json({**binding, 'native_identity': captured}), check, witnesses)
        confirmed_identity = captured
    failure = None
    outcome = None
    try:
        progress['phase'] = component + '_command'
        outcome = process.execute(command, record_started=started, check_authority=check)
    except BaseException as error:
        failure = error
        outcome = getattr(error, 'process_outcome', None)
    if outcome is not None:
        try:
            outcome = _outcome(outcome)
            if outcome['release_attempted'] and confirmed_identity is None:
                raise ValueError('backup command release has no confirmed identity receipt')
            if confirmed_identity is not None and outcome['pid'] != confirmed_identity['pid']:
                raise ValueError('backup command outcome has a different process identity')
            row['observed_outcome'] = outcome
            progress['phase'] = component + '_outcome_evidence'
            _put(root, directory, component + '-outcome.json', _json({**binding, 'outcome': outcome}), check, witnesses)
            row['outcome_receipt_confirmed'] = True
        except BaseException as error:
            if failure is None:
                failure = error
    if failure is not None:
        raise failure
    if outcome is None or not _successful(outcome):
        raise ValueError('backup command did not complete successfully')
    check()


def deliver(archive_path, *, delivery_id=None):
    return _deliver(archive_path, delivery_id=delivery_id)


def _deliver(archive_path, *, delivery_id=None, _authority=None):
    from .backup import _held_backup_directory, _verification
    requested = uuid.uuid4().hex if delivery_id is None else publication._opid(delivery_id)
    if _authority is not None:
        from .backup_authority import require
        require(_authority, 'deliver')
    archive = aio.selected_path(archive_path) if _authority is None else _authority.bind_file(archive_path)
    match = publication._NAME.fullmatch(archive.name)
    if match is None:
        raise ValueError('delivery requires a managed local backup publication')
    publication_id = match.group(1)
    progress = {'phase': 'validation', 'components': {}, 'completion_receipt_confirmed': False}
    failure = None
    result = None
    confirmed = ()
    try:
        if _authority is None:
            selected, signing_selected, cwd = _command_selection(), signing._selection(), os.getcwd()
            environment = _capture_environment()
            memory_path = aio.selected_directory(config.MEMORY_DIR)
        else:
            selected, signing_selected, cwd = _authority.command_selection, _authority.signing_selection, _authority.cwd
            environment = dict(_authority.environment)
            memory_path = _authority.memory_path
        permission = dict(selected).get('OLYMPUS_BACKUP_ALLOW_PLAINTEXT') or ''
        if type(permission) is not str or len(permission) > 16:
            raise ValueError('backup plaintext permission exceeds its bound')
        allow_plaintext = permission.strip().lower() in ('1', 'true', 'yes', 'on')
        def unchanged():
            if _authority is not None:
                require(_authority, 'deliver')
                return
            if (_command_selection() != selected or aio.selected_directory(config.MEMORY_DIR) != memory_path
                    or os.getcwd() != cwd):
                raise ValueError('backup delivery selection changed')
            signing._check_selection(signing_selected)
        with aio.operation('backup', capture_key=False,
                selectors=None if _authority is None else _authority.vault_selectors,
                cwd=None if _authority is None else _authority.cwd) as session:
            with (_held_backup_directory(cwd) if _authority is None else nullcontext(_authority.working)) as working:
                with management._directories(_authority=_authority, _stage='deliver') as (memory, backups):
                    if backups is None or archive.parent != backups.path:
                        raise ValueError('delivery archive is outside the selected backup directory')
                    unchanged(); memory.check(); working.check()
                    captured = _capture_command(selected, cwd, environment) if _authority is None else _authority.command
                    unchanged(); memory.check(); working.check()
                    if captured is None:
                        return {'status': 'not_configured', 'publication_id': publication_id, 'delivery_id': None,
                            'delivery_commands_complete': False, 'remote_retention_verified': False}
                    with _publication_authority(backups, publication_id) as (root, operation):
                        witnesses = {}
                        def check():
                            root.check(); memory.check(); working.check(); unchanged()
                            for held, name, expected, identity in witnesses.values():
                                observed, current = held.read_with_identity(name, len(expected))
                                if observed != expected or current != identity:
                                    raise ValueError('backup delivery evidence identity or bytes changed')
                        check()
                        names = publication._names(operation, MAX_ENTRIES)
                        if any(name.casefold() in (CONTROL.casefold(), CLAIM.casefold()) for name in names):
                            progress['phase'] = 'existing_attempt_refused'
                            observed_claim = _optional(operation, CLAIM)
                            if observed_claim is not None:
                                observed_claim = _claim(fmt.strict_json(observed_claim, MAX_EVIDENCE), publication_id)
                                progress['existing_delivery_id_observed'] = observed_claim['delivery_id']
                            raise ValueError('backup publication already has delivery evidence; do not retry')
                        _admit_history(root, publication_id, progress)
                        policy = (signing.capture_policy(selection=signing_selected, cwd=cwd)
                                  if _authority is None else _authority.policy)
                        check()
                        raw, signature, published = publication.read_completed(root, archive.name)
                        published_plan, published_hash = _publication_metadata(
                            root, operation, publication_id, root.head(), [MAX_SCAN_BYTES])
                        if published_hash != published['plan_sha256']:
                            raise ValueError('backup publication plan changed during delivery admission')
                        verified = _verification(raw, signature, {'kind': 'local_completed'}, policy)
                        check()
                        if not (verified['signature_ok'] and verified['signer_authenticated'] and not verified['signer_default']):
                            raise ValueError('delivery requires an authenticated nondefault signer')
                        if raw[:2] == b'\x1f\x8b':
                            if not allow_plaintext:
                                raise ValueError('plaintext delivery requires explicit permission')
                            payload = raw
                            encryption = aio.encryption_metadata(plaintext=True)
                        else:
                            decoded = session.decrypt(raw)
                            encryption = aio.encryption_metadata(decoded)
                            if (encryption['format_version'] != 1 or not encryption['context_authenticated']
                                    or encryption['legacy_unbound']):
                                raise ValueError('delivery requires authenticated backup-context encryption')
                            payload = decoded.data
                        fmt.decode(payload)  # Validate every bounded manifest/path/hash before staging.
                        del payload
                        if raw[:2] != b'\x1f\x8b':
                            del decoded
                        if published['encrypted'] != encryption['encrypted']:
                            raise ValueError('backup publication encryption metadata contradicts its bytes')
                        commands = {component: _for_image(captured, operation.path / CONTROL /
                                    (archive.name + ('.sig.json' if component == 'signature' else '')), cwd)
                                    for component in ('archive', 'signature')}
                        check()
                        with _held_directory(root, _exclusive_child(operation, CONTROL)) as directory:
                            progress['phase'] = 'preparation'
                            plan = {'schema': SCHEMA, 'publication_id': publication_id, 'delivery_id': requested,
                                'publication_plan_sha256': published['plan_sha256'],
                                'publication_initialization_sha256': _hash(root.marker),
                                'publication_operation_identity': publication._ident(operation),
                                'delivery_directory_identity': publication._ident(directory),
                                'archive_name': archive.name, 'archive_sha256': _hash(raw), 'archive_bytes': len(raw),
                                'signature_sha256': _hash(signature), 'signature_bytes': len(signature),
                                'signer_authenticated': True, 'signer_default': False, 'publicKey': verified['publicKey'],
                                'archive_encryption': encryption, 'plaintext_allowed': allow_plaintext,
                                'created_unix': int(time.time())}
                            images = {archive.name: raw, archive.name + '.sig.json': signature}
                            for name, image in images.items():
                                _put(root, directory, name, image, check, witnesses)
                            identities = {name: aio.file_identity(directory.path / name, len(image)) for name, image in images.items()}
                            for component in ('archive', 'signature'):
                                plan[component + '_identity'] = list(identities[_image_name(plan, component)])
                            _plan(plan, root, operation, directory, published_plan, published_hash)
                            plan_raw = _json(plan)
                            plan_hash = _hash(plan_raw)
                            _put(root, directory, 'plan.json', plan_raw, check, witnesses)
                            claim = {'schema': SCHEMA, 'publication_id': publication_id, 'delivery_id': requested,
                                'publication_plan_sha256': published['plan_sha256'], 'delivery_plan_sha256': plan_hash,
                                'delivery_directory_identity': publication._ident(directory)}
                            _put(root, operation, CLAIM, _json(claim), check, witnesses)
                            def intact():
                                check()
                                if _optional(directory, 'plan.json') != plan_raw or _optional(operation, CLAIM) != _json(claim):
                                    raise ValueError('backup delivery plan or claim changed')
                                for name, identity in identities.items():
                                    if aio.file_identity(directory.path / name, len(images[name])) != identity:
                                        raise ValueError('backup delivery staged image identity changed')
                            for component in ('archive', 'signature'):
                                intact()
                                _dispatch(root, directory, plan, plan_hash, component, commands[component], intact, progress, witnesses)
                                observed, _ = directory.read_with_identity(_image_name(plan, component), plan[component + '_bytes'])
                                if _hash(observed) != plan[component + '_sha256']:
                                    raise ValueError('backup delivery staged bytes changed')
                            progress['phase'] = 'completion_evidence'
                            completion = {'schema': SCHEMA, 'publication_id': publication_id, 'delivery_id': requested,
                                'delivery_plan_sha256': plan_hash, 'archive_sha256': plan['archive_sha256'],
                                'signature_sha256': plan['signature_sha256'], 'delivery_commands_complete': True,
                                'remote_retention_verified': False}
                            _put(root, directory, 'complete.json', _json(completion), intact, witnesses)
                            progress['completion_receipt_confirmed'] = True
                            confirmed = (stage_facts.ConfirmedStage('delivery', requested,
                                plan['archive_sha256'], plan['signature_sha256'], plan_hash, publication_id),)
                            result = {**completion, 'status': 'command_pair_completed',
                                'intended_archive_sha256': plan['archive_sha256'],
                                'intended_signature_sha256': plan['signature_sha256'],
                                'archive_encryption': encryption, 'signer_authenticated': True,
                                'signer_default': False, 'components': progress['components']}
    except BaseException as error:
        failure = error
    if failure is not None:
        if not isinstance(failure, Exception):
            stage_facts.carry(failure, failure, confirmed)
            failure.publication_id = publication_id
            failure.delivery_id = requested
            failure.delivery_progress = progress
            raise failure
        public = DeliveryError(publication_id, requested, progress)
        stage_facts.carry(failure, public, confirmed)
        failure = None
        _raise_public_error(public)
    return result


def _inspect_held(root, operation, publication_plan, publication_hash, images, progress, budget=None):
    publication_id = publication_plan['operation_id']
    names = publication._names(operation, MAX_ENTRIES)
    operation._case_spelling(CONTROL); operation._case_spelling(CLAIM)
    claim_raw = _optional(operation, CLAIM, budget=budget)
    claim = None if claim_raw is None else _claim(fmt.strict_json(claim_raw, MAX_EVIDENCE), publication_id)
    if claim is not None:
        progress['existing_delivery_id_observed'] = claim['delivery_id']
        if claim['publication_plan_sha256'] != publication_hash:
            raise ValueError('backup delivery claim conflicts with its publication')
    report = {'publication_id': publication_id, 'delivery_id': None,
        'status': 'unattempted', 'attempt_evidence_visible': False, 'automatic_retry_permitted': False,
        'delivery_commands_complete': None, 'completion_barriers_reconfirmed': False,
        'remote_retention_verified': False, 'observational_only': True, 'components': {}}
    if CONTROL not in names:
        if claim is not None:
            raise ValueError('backup delivery claim survives without its directory; do not recreate')
        return report
    report.update(status='preparation_incomplete', attempt_evidence_visible=True)
    with _held_directory(root, operation.child(CONTROL)) as directory:
        entries = publication._names(directory, MAX_ENTRIES)
        allowed = {'plan.json', publication_plan['name'], publication_plan['name'] + '.sig.json', 'complete.json'}
        allowed |= {component + '-' + phase + '.json' for component in ('archive', 'signature')
                    for phase in ('intent', 'started', 'outcome')}
        if set(entries) - allowed:
            raise ValueError('backup delivery contains an unexpected journal entry')
        plan_raw = _optional(directory, 'plan.json', budget=budget)
        if plan_raw is None:
            if set(entries) - {publication_plan['name'], publication_plan['name'] + '.sig.json'} or claim is not None:
                raise ValueError('backup delivery evidence survives without its plan')
            return report
        plan = _plan(fmt.strict_json(plan_raw, MAX_EVIDENCE), root, operation, directory,
                     publication_plan, publication_hash)
        plan_hash = _hash(plan_raw)
        report['delivery_id'] = plan['delivery_id']
        progress['existing_delivery_id_observed'] = plan['delivery_id']
        if claim is not None and claim != {'schema': SCHEMA, 'publication_id': publication_id,
                'delivery_id': plan['delivery_id'], 'publication_plan_sha256': publication_hash,
                'delivery_plan_sha256': plan_hash, 'delivery_directory_identity': publication._ident(directory)}:
            raise ValueError('backup delivery claim does not bind this plan and directory')
        for index, component in enumerate(('archive', 'signature')):
            identity = aio.file_identity(directory.path / _image_name(plan, component), plan[component + '_bytes'])
            if list(identity) != plan[component + '_identity']:
                raise ValueError('backup delivery staged image identity differs from its plan')
            if images is not None:
                staged = _optional(directory, _image_name(plan, component), plan[component + '_bytes'])
                if staged != images[index]:
                    raise ValueError('backup delivery staging conflicts with retained publication bytes')
            root.check()
        for component in ('archive', 'signature'):
            binding = _bound(plan, plan_hash, component)
            intent = _optional(directory, component + '-intent.json', budget=budget)
            started = _optional(directory, component + '-started.json', budget=budget)
            outcome = _optional(directory, component + '-outcome.json', budget=budget)
            if (started is not None or outcome is not None) and intent is None:
                raise ValueError('backup delivery outcome has no component intent')
            if intent is not None and (claim is None or fmt.strict_json(intent, MAX_EVIDENCE) != binding):
                raise ValueError('backup delivery intent is not bound to a retained claim')
            native = None
            if started is not None:
                value = fmt.strict_json(started, MAX_EVIDENCE)
                if type(value) is not dict or set(value) != set(binding) | {'native_identity'}:
                    raise ValueError('backup delivery started receipt has an invalid shape')
                native = _identity(value['native_identity'])
                if value != {**binding, 'native_identity': native}:
                    raise ValueError('backup delivery started receipt is not bound to its plan')
            observed = None
            if outcome is not None:
                value = fmt.strict_json(outcome, MAX_EVIDENCE)
                if type(value) is not dict or set(value) != set(binding) | {'outcome'}:
                    raise ValueError('backup delivery outcome receipt has an invalid shape')
                observed = _outcome(value['outcome'])
                if value != {**binding, 'outcome': observed}:
                    raise ValueError('backup delivery outcome receipt is not bound to its plan')
                if observed['release_attempted'] and native is None:
                    raise ValueError('backup delivery release has no retained native identity')
                if native is not None and observed['pid'] != native['pid']:
                    raise ValueError('backup delivery outcome process identity differs')
            if component == 'signature' and intent is not None:
                previous = report['components']['archive']['observed_outcome']
                if previous is None or not _successful(previous):
                    raise ValueError('signature intent precedes a successful archive outcome')
            report['components'][component] = {'intent_visible': intent is not None,
                'started_identity': native, 'observed_outcome': observed,
                'outcome_receipt_visible': outcome is not None}
            if intent is not None:
                report['status'] = component + ('_outcome_visible' if outcome is not None else '_intent_visible')
        completed = _optional(directory, 'complete.json', budget=budget)
        if completed is not None:
            expected = {'schema': SCHEMA, 'publication_id': publication_id, 'delivery_id': plan['delivery_id'],
                'delivery_plan_sha256': plan_hash, 'archive_sha256': plan['archive_sha256'],
                'signature_sha256': plan['signature_sha256'], 'delivery_commands_complete': True,
                'remote_retention_verified': False}
            value = fmt.strict_json(completed, MAX_EVIDENCE)
            if (claim is None or type(value) is not dict or value != expected
                    or value['delivery_commands_complete'] is not True or value['remote_retention_verified'] is not False
                    or not all(row['observed_outcome'] is not None and _successful(row['observed_outcome'])
                               for row in report['components'].values())):
                raise ValueError('backup delivery completion conflicts with component evidence')
            report['status'] = 'completion_visible'
        report['retained_verification'] = {key: plan[key] for key in
            ('archive_sha256', 'signature_sha256', 'signer_authenticated', 'signer_default', 'publicKey', 'archive_encryption')}
        report['verification_reperformed'] = False
        report['staged_bytes_compared_with_publication_images'] = images is not None
        root.check()
        return report


def _publication_metadata(root, operation, operation_id, head, budget):
    """Validate the existing publication plan without allocating archive images."""
    raw = _optional(operation, 'plan.json', publication.MAX_PLAN, budget)
    if raw is None:
        raise ValueError('delivery history has no publication plan')
    value = fmt.strict_json(raw, publication.MAX_PLAN)
    keys = {'schema', 'operation_id', 'initialization_sha256', 'initialization_identity',
        'operation_identity', 'name', 'archive_sha256', 'archive_bytes',
        'signature_sha256', 'signature_bytes', 'details'}
    if (type(value) is not dict or set(value) != keys or value['schema'] != publication.SCHEMA
            or value['operation_id'] != operation_id or value['initialization_sha256'] != _hash(root.marker)
            or not _native_identity(value['initialization_identity'])
            or value['initialization_identity'] != root.marker_identity
            or not _native_identity(value['operation_identity'])
            or value['operation_identity'] != publication._ident(operation)):
        raise ValueError('delivery history publication identity is invalid')
    details = publication._details(value['details'])
    if value['name'] != publication.archive_name(operation_id, details['encrypted']):
        raise ValueError('delivery history publication name is invalid')
    for component, cap in (('archive', aio.MAX_CIPHERTEXT), ('signature', fmt.MAX_SIGNATURE)):
        if (type(value[component + '_bytes']) is not int or not 0 < value[component + '_bytes'] <= cap
                or not publication._digest(value[component + '_sha256'])):
            raise ValueError('delivery history publication image bound is invalid')
    digest = _hash(raw)
    if head['operation_id'] == operation_id and head['plan_sha256'] != digest:
        raise ValueError('delivery history conflicts with active publication head')
    complete = _optional(operation, 'complete.json', budget=budget)
    expected = {'schema': publication.SCHEMA, 'operation_id': operation_id, 'plan_sha256': digest,
        'name': value['name'], 'archive_sha256': value['archive_sha256'],
        'signature_sha256': value['signature_sha256'], 'publication_complete': True}
    observed = None if complete is None else fmt.strict_json(complete, MAX_EVIDENCE)
    if type(observed) is not dict or observed != expected or observed['publication_complete'] is not True:
        raise ValueError('delivery history has no valid publication completion')
    return value, digest


def _retained_staging(directory, name):
    """Recognize bounded retained single-file publication evidence, never read it."""
    if not re.fullmatch(r'\.archive-[0-9a-f]{32}\.tmp', name):
        return False
    aio.file_identity(directory.path / name, aio.MAX_CIPHERTEXT)
    directory.check()
    return True


def _admit_history(root, current_id, progress):
    """All prior delivery slots must contain a terminal, cleaned command pair.

This is a metadata safety gate, not retrospective durability acknowledgement
or remote-retention proof. Retired and historical operations remain included.
No image payload, signing material or decryption key is read for this scan.
"""
    head = root.head()
    names = publication._names(root.control, publication.MAX_OPERATIONS + 16)
    operations = sorted(name for name in names if publication._ID.fullmatch(name))
    unexplained = set(names) - set(operations) - {'initialization.json', 'publication.lock', 'head.json'}
    if any(not _retained_staging(root.control, name) for name in sorted(unexplained)):
        raise ValueError('backup delivery history contains unexplained control evidence')
    if len(operations) > publication.MAX_OPERATIONS:
        raise ValueError('backup delivery history exceeds its operation bound')
    if head['operation_id'] is not None and head['operation_id'] not in operations:
        raise ValueError('backup delivery history is missing the active publication')
    for name in publication._names(root.root, publication.MAX_DIRECTORY_ENTRIES):
        if not publication.managed_name(name):
            continue
        archive_name = name[:-len('.sig.json')] if name.endswith('.sig.json') else name
        matched = publication._NAME.fullmatch(archive_name)
        if matched is None or matched.group(1) not in operations:
            raise ValueError('reserved backup pair has no canonical retained publication operation')
    budget = [MAX_SCAN_BYTES]
    allowed = {'archive.image', 'signature.image', 'plan.json', 'progress.json', 'complete.json',
               'retirement-intent.json', 'retired.json', CONTROL, CLAIM}
    for operation_id in operations:
        if operation_id == current_id:
            continue
        # A later operation can fail before its claim is inspected. Never
        # attribute a previous operation's delivery ID to that new blocker.
        progress.pop('existing_delivery_id_observed', None)
        progress['blocking_publication_id_observed'] = operation_id
        with publication._held_operation(root, operation_id) as operation:
            entries = publication._names(operation, MAX_ENTRIES)
            if any(not _retained_staging(operation, name) for name in sorted(set(entries) - allowed)):
                progress['blocking_publication_id_observed'] = operation_id
                raise ValueError('backup delivery history contains unexplained operation evidence')
            if not any(name.casefold() in (CONTROL.casefold(), CLAIM.casefold()) for name in entries):
                continue
            progress['blocking_publication_id_observed'] = operation_id
            plan, digest = _publication_metadata(root, operation, operation_id, head, budget)
            observed = _inspect_held(root, operation, plan, digest, None, progress, budget)
            if observed['status'] != 'completion_visible':
                raise ValueError('an earlier backup delivery is partial or unresolved; do not dispatch another publication')
    progress.pop('blocking_publication_id_observed', None)
    progress.pop('existing_delivery_id_observed', None)
    root.check()


def inspect_delivery(archive_path):
    """Read bounded retained claims without keys, commands or receipt settlement."""
    archive = aio.selected_path(archive_path)
    match = publication._NAME.fullmatch(archive.name)
    if match is None:
        raise ValueError('delivery inspection requires a managed local backup publication')
    publication_id = match.group(1)
    progress = {'phase': 'inspection', 'components': {}, 'completion_receipt_confirmed': False}
    failure = None
    result = None
    try:
        with aio.operation('backup', capture_key=False):
            with management._directories() as (_memory, backups):
                if backups is None or archive.parent != backups.path:
                    raise ValueError('delivery archive is outside the selected backup directory')
                with _publication_authority(backups, publication_id) as (root, operation):
                    head = root.head()
                    expected = head['plan_sha256'] if head['operation_id'] == publication_id else None
                    plan, digest, images = publication._load(root, publication_id, expected)
                    if plan['name'] != archive.name:
                        raise ValueError('delivery archive name conflicts with publication')
                    completed, _ = publication._completion(root, plan, digest)
                    retirement, _ = publication._retirement_state(root, plan, digest)
                    result = _inspect_held(root, operation, plan, digest, images, progress)
                    result['publication_completion_visible'] = completed is not None
                    result['retirement_status_visible'] = retirement
    except BaseException as error:
        failure = error
    if failure is not None:
        observed_id = progress.get('existing_delivery_id_observed')
        if not isinstance(failure, Exception):
            failure.publication_id, failure.delivery_id, failure.delivery_progress = publication_id, observed_id, progress
            raise failure
        public = DeliveryError(publication_id, observed_id, progress)
        stage_facts.carry(failure, public)
        failure = None
        _raise_public_error(public)
    return result

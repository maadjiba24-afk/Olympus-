"""Captured, serialized credentials and explicit recoverable format changes.

New writes use salted password-hard, owner-bound records. Historical ciphertext
remains readable, but changing it requires an explicit migration operation.
Signing identity derivation is independent and is not changed here.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
import hashlib
import os
from pathlib import Path
import re
import threading
from types import SimpleNamespace

from . import config, memory, store, native_cleanup
from . import vault_codec as codec, vault_custody as custody
from . import vault_records as records, vault_recovery as recovery
from . import vault_storage as storage

try:
    Fernet, _, _, _ = codec._backend()
    Fernet(Fernet.generate_key())
    _HAVE_CRYPTO = True
except BaseException:
    _HAVE_CRYPTO = False

_NS = 'vault'
VAULT_KEY_VERSION = 2  # exact-owner addressing; separate from encryption version
MAX_ARCHIVE_BYTES = codec.MAX_PLAINTEXT
_ROW_CAP = 6 * 1024 * 1024  # bounded ciphertext for records.MAX_BYTES
_ARCHIVE_CONTEXTS = {
    'backup': b'olympus-backup-archive-v1',
    'notes': b'olympus-note-archive-v1',
}
_UNSELECTED = object()


class VaultError(RuntimeError):
    pass


class VaultMigrationRequired(VaultError):
    pass


class VaultConflict(VaultError):
    pass


class VaultRecoveryError(VaultError):
    def __init__(self, operation_id):
        super().__init__('vault change requires recovery; retained ciphertext was preserved')
        self.operation_id = operation_id


def _guard(function):
    """Keep private exception details out while carrying one fixed cleanup fact."""
    @wraps(function)
    def guarded(*args, **kwargs):
        operation_id = None
        cleanup_unknown = False
        try:
            return function(*args, **kwargs)
        except VaultError:
            raise
        except recovery.RecoveryError as exc:
            candidate = BaseException.__getattribute__(exc, '__dict__').get('operation_id')
            if type(candidate) is str and re.fullmatch('[0-9a-f]{32}', candidate) is not None:
                operation_id = candidate
            cleanup_unknown = native_cleanup.unknown(exc)
        except Exception as exc:
            cleanup_unknown = native_cleanup.unknown(exc)
        # Raise outside the handler; no private source context, notes or text.
        error = (VaultRecoveryError(operation_id) if operation_id is not None else
                 VaultError('vault operation is unavailable or invalid'))
        if cleanup_unknown:
            native_cleanup.mark(error)
        raise error from None
    return guarded


@_guard
def configured() -> bool:
    return custody.configured()


def _required(captured):
    if not _HAVE_CRYPTO:
        raise VaultError('cryptography with scrypt support is required for stored credentials')
    if captured is None:
        raise VaultError('OLYMPUS_SECRET_KEY / OLYMPUS_SECRET_KEY_FILE is not set')
    if type(captured) is not custody.CapturedPassphrase:
        raise VaultError('invalid captured vault custody')
    return captured.material


@_guard
def check_ready() -> None:
    """Check captured custody and actual v1 encryption without persisting data."""
    material = _required(custody.capture())
    codec.encrypt(b'', material, context=b'olympus-vault-readiness-v1', max_plaintext=0)


def _ready_captured(captured):
    codec.encrypt(b'', _required(captured.passphrase),
                  context=b'olympus-vault-readiness-v1', max_plaintext=0)


def available() -> bool:
    try:
        check_ready()
        return True
    except VaultError:
        return False


@_guard
def encrypt_bytes(data: bytes, *, purpose='backup') -> bytes:
    """Encrypt a bounded archive; note exports and backups have distinct contexts."""
    return codec.encrypt(data, _required(custody.capture()),
        context=_ARCHIVE_CONTEXTS[purpose], max_plaintext=MAX_ARCHIVE_BYTES)


@_guard
def decrypt_bytes(token: bytes, *, purpose='backup') -> bytes:
    """Read v1 or explicitly unbound historical archives; never rewrite them."""
    return codec.decrypt(token, _required(custody.capture()),
        context=_ARCHIVE_CONTEXTS[purpose], max_plaintext=MAX_ARCHIVE_BYTES,
        allow_legacy=True).data


def _owner(user):
    if user is not None and type(user) is not str:
        raise VaultError('invalid exact vault owner')
    exact = memory.canonical_owner(user)
    records.owner_context(exact)
    return exact


def _key(user: str) -> str:
    return memory.storage_key(_owner(user))


@dataclass(repr=False)
class _CaptureState:
    cleanup_unconfirmed: bool = False


@dataclass(frozen=True, repr=False)
class _Capture:
    owner: str | None
    root: Path
    backend: object
    passphrase: custody.CapturedPassphrase | None
    pid: int
    thread: int
    _root_identity: tuple | None = None
    _state: _CaptureState = field(default_factory=_CaptureState, compare=False)

    def check(self):
        if (self.pid, self.thread) != (os.getpid(), threading.get_ident()):
            raise VaultError('captured vault authority belongs to another process or thread')
        if self._state.cleanup_unconfirmed:
            error = VaultError('captured vault cleanup requires reconciliation')
            native_cleanup.mark(error)
            raise error


def _capture_callback(directory, function, *args, **kwargs):
    if directory is not None:
        directory.check()
    result = function(*args, **kwargs)
    if directory is not None:
        directory.check()
    return result


@dataclass(frozen=True, repr=False)
class _SnapshotSelection:
    """Private frozen selectors, not a public credential-free status value."""
    owner: object
    root: Path
    kind: str
    backend: object
    file_factory: object
    dsn: object
    connect: object
    initialize: bool
    selectors: object
    selected_custody: object
    pid: int
    thread: int

    def check(self):
        if (self.pid, self.thread) != (os.getpid(), threading.get_ident()):
            raise VaultError('vault snapshot selection belongs to another executor')


def _snapshot_selection(user, *, root, owner_required=True, selected_custody=_UNSELECTED):
    """Freeze choices without dispatching a backend, key reader or constructor."""
    from .archive_io import selected_directory, selected_path
    exact = _owner(user) if owner_required else None
    selected_root = selected_directory(root)
    selectors = None
    if selected_custody is _UNSELECTED:
        values = {name: os.environ.get(name) for name in
                  ('OLYMPUS_SECRET_KEY', 'OLYMPUS_SECRET_KEY_FILE', 'OLYMPUS_ENV')}
        key_path = custody._text(values['OLYMPUS_SECRET_KEY_FILE'], custody.MAX_PATH_CHARS)
        if key_path:
            values['OLYMPUS_SECRET_KEY_FILE'] = str(selected_path(key_path))
        selectors = tuple(values.items())
    backend = store._backend
    dsn, connect, initialize = None, None, False
    file_factory = store.FileStore
    if backend is None:
        dsn = os.environ.get('OLYMPUS_DATABASE_URL')
        if dsn:
            connect = _UNSELECTED  # Explicit default choice, resolved under the applicable guard.
            kind, initialize = 'postgres', True
        else:
            kind = 'file'
    elif type(backend) is store.FileStore:
        kind = 'file'
    elif type(backend) is store.PostgresStore:
        kind, dsn, connect = 'postgres', backend._dsn, backend._psycopg.connect
    else:
        raise VaultError('backend does not provide a verified vault snapshot')
    if kind == 'postgres' and (type(dsn) is not str or not 1 <= len(dsn) <= 32768
            or connect is not _UNSELECTED and not callable(connect)):
        raise VaultError('invalid captured vault database selection')
    return _SnapshotSelection(exact, selected_root, kind, backend, file_factory,
        dsn, connect, initialize, selectors, selected_custody, os.getpid(), threading.get_ident())


def _snapshot_material(plan, guard):
    plan.check()
    if plan.selected_custody is _UNSELECTED:
        return _capture_callback(guard, custody.capture, dict(plan.selectors))
    if guard is not None:
        guard.check()
    return plan.selected_custody


def _snapshot_file_capture(plan, guard=None, *, root_identity=None):
    plan.check()
    if plan.kind != 'file':
        raise VaultError('file vault snapshot selection changed')
    backend = plan.backend
    if backend is None:
        backend = _capture_callback(guard, plan.file_factory)
    elif guard is not None:
        guard.check()
    if type(backend) is not store.FileStore:
        raise VaultError('file vault snapshot selection changed')
    material = _snapshot_material(plan, guard)
    if guard is not None:
        guard.check()
    return _Capture(plan.owner, plan.root, backend, material, plan.pid, plan.thread,
                    _root_identity=root_identity)


def _resolve_snapshot_connector(plan, guard):
    """Resolve an explicit cold default once; never refresh a cached connector."""
    from dataclasses import replace
    plan.check()
    if plan.kind != 'postgres':
        raise VaultError('database vault snapshot selection changed')
    if plan.connect is _UNSELECTED:
        def resolve():
            import psycopg
            return psycopg.connect
        connect = _capture_callback(guard, resolve)
        if not callable(connect):
            raise VaultError('invalid captured vault database connector')
        plan = replace(plan, connect=connect)
    elif not callable(plan.connect):
        raise VaultError('invalid captured vault database connector')
    return plan


def _snapshot_postgres_capture(plan, guard=None):
    """Database authority comes only from its frozen, verified connection choice."""
    from . import vault_selection
    plan.check()
    if plan.kind != 'postgres':
        raise VaultError('database vault snapshot selection changed')
    plan = _resolve_snapshot_connector(plan, guard)
    backend = _capture_callback(guard, vault_selection.capture, plan.dsn,
        connect=plan.connect, initialize=plan.initialize)
    if type(backend) is not store.PostgresStore:
        raise VaultError('database vault snapshot selection changed')
    material = _snapshot_material(plan, guard)
    if guard is not None:
        guard.check()
    return _Capture(plan.owner, plan.root, backend, material, plan.pid, plan.thread,
                    _root_identity=None)


def _capture_snapshot(user=None, *, owner_required=True, selected_custody=_UNSELECTED,
                      root=None, expected_root_identity=None):
    """Standalone snapshots retain first-use/PG behavior; bound roots stay file-only."""
    if (root is None) != (expected_root_identity is None):
        raise VaultError('captured vault root arguments must be paired')
    storage._validate_root_identity(expected_root_identity)
    if root is not None and (type(root) is not type(Path()) or not root.is_absolute()):
        raise VaultError('captured vault root must be an absolute native path')
    plan = _snapshot_selection(user, root=config.MEMORY_DIR if root is None else root,
        owner_required=owner_required, selected_custody=selected_custody)
    directory, primary = None, None
    try:
        if expected_root_identity is not None:
            from .vault_native import Directory
            directory = Directory(plan.root, create=False)
            if storage._directory_identity(directory) != expected_root_identity:
                raise VaultError('captured vault root was replaced')
            if plan.kind != 'file':
                raise VaultError('filesystem root binding requires the file vault backend')
        if plan.kind == 'file':
            return _snapshot_file_capture(plan, directory, root_identity=expected_root_identity)
        return _snapshot_postgres_capture(plan)
    except BaseException as error:
        primary = error
        raise
    finally:
        if directory is not None:
            storage._drain_owned(directory, [directory.close], primary)


@_guard
def _snapshots_for_preferences(user, names, *, root, root_identity, for_update=False):
    """Guard an existing preference root around explicit file/PG snapshot work.

    This is not an atomic cross-store capture. The caller must also compare its
    complete preference evidence before prompting and before each preference
    CAS. The PG executor never receives filesystem transaction authority.
    """
    selected_names = _snapshot_names(names, for_update)
    storage._validate_root_identity(root_identity)
    if root_identity is None or type(root) is not type(Path()) or not root.is_absolute():
        raise VaultError('existing preference root authority is required')
    plan = _snapshot_selection(user, root=root)
    directory, primary = None, None
    try:
        from .vault_native import Directory
        directory = Directory(plan.root, create=False)
        if storage._directory_identity(directory) != root_identity:
            raise VaultError('captured preference root was replaced')
        if plan.kind == 'file':
            captured = _snapshot_file_capture(plan, directory, root_identity=root_identity)
        else:
            captured = _snapshot_postgres_capture(plan, directory)
        return _capture_callback(directory, _snapshots_captured, captured,
            selected_names, for_update=for_update)
    except BaseException as error:
        primary = error
        raise
    finally:
        if directory is not None:
            storage._drain_owned(directory, [directory.close], primary)


def _capture(user=None, *, owner_required=True, selected_custody=_UNSELECTED, snapshot_selection=False, root=None, expected_root_identity=None):
    """Optional existing-root binding for private filesystem workflows only.

    Standalone first-use and database captures keep their existing selection
    contract. The binding is not atomic root/key custody or database authority.
    """
    if snapshot_selection:
        return _capture_snapshot(user, owner_required=owner_required, selected_custody=selected_custody, root=root, expected_root_identity=expected_root_identity)
    from .archive_io import selected_directory, selected_path
    if (root is None) != (expected_root_identity is None):
        raise VaultError('captured vault root arguments must be paired')
    storage._validate_root_identity(expected_root_identity)
    if root is not None and (type(root) is not type(Path()) or not root.is_absolute()):
        raise VaultError('captured vault root must be an absolute native path')
    exact = _owner(user) if owner_required else None
    root = selected_directory(config.MEMORY_DIR if root is None else root)
    selectors = None
    if selected_custody is _UNSELECTED:
        selectors = {name: os.environ.get(name) for name in ('OLYMPUS_SECRET_KEY', 'OLYMPUS_SECRET_KEY_FILE', 'OLYMPUS_ENV')}
        key_path = custody._text(selectors['OLYMPUS_SECRET_KEY_FILE'], custody.MAX_PATH_CHARS)
        if key_path:
            selectors['OLYMPUS_SECRET_KEY_FILE'] = str(selected_path(key_path))
    directory, primary = (None, None)
    try:
        if expected_root_identity is not None:
            from .vault_native import Directory
            directory = Directory(root, create=False)
            if storage._directory_identity(directory) != expected_root_identity:
                raise VaultError('captured vault root was replaced')
            selected_backend = store._backend
            if selected_backend is None and os.environ.get('OLYMPUS_DATABASE_URL') or (selected_backend is not None and type(selected_backend) is not store.FileStore):
                raise VaultError('filesystem root binding requires the file vault backend')
        backend = _capture_callback(directory, store.backend)
        if type(backend) is store.PostgresStore:
            dsn, connect = (backend._dsn, backend._psycopg.connect)
            if type(dsn) is not str or not dsn or len(dsn) > 32768 or (not callable(connect)):
                raise VaultError('invalid captured vault backend')
            backend = object.__new__(store.PostgresStore)
            backend._dsn = dsn
            backend._psycopg = SimpleNamespace(connect=connect)
        if expected_root_identity is not None and type(backend) is not store.FileStore:
            raise VaultError('filesystem root binding requires the file vault backend')
        passphrase = _capture_callback(directory, custody.capture, selectors) if selected_custody is _UNSELECTED else selected_custody
        if directory is not None:
            directory.check()
        return _Capture(exact, root, backend, passphrase, os.getpid(), threading.get_ident(), _root_identity=expected_root_identity)
    except BaseException as error:
        primary = error
        raise
    finally:
        if directory is not None:
            storage._drain_owned(directory, [directory.close], primary)


@contextmanager
def _transaction(captured, *, expected_identity=None):
    captured.check()
    options = {'expected_identity': expected_identity}
    if captured._root_identity is not None:
        options['expected_root_identity'] = captured._root_identity
    try:
        with storage.transaction(captured.backend, captured.root, **options) as tx:
            yield tx
    except BaseException as error:
        if native_cleanup.unknown(error):
            captured._state.cleanup_unconfirmed = True
        raise


def _writable_owner(captured):
    if memory.is_ambiguous_gateway_owner(captured.owner):
        raise VaultError('cannot change credentials for an ambiguous pre-v2 gateway owner')


def _load_captured(captured, tx):
    if memory.is_ambiguous_gateway_owner(captured.owner):
        return {}, None, None
    blob = tx.get(_NS, _key(captured.owner), cap=_ROW_CAP)
    if blob is None:
        return {}, None, None
    try:
        entries, metadata = records.decode(captured.owner, blob, _required(captured.passphrase))
    except (codec.EnvelopeError, records.RecordError):
        raise VaultError('could not decrypt or authenticate the bounded vault record') from None
    return entries, blob, metadata


def _require_current_format(metadata):
    if metadata is not None and not metadata.context_authenticated:
        raise VaultMigrationRequired('explicit vault format migration is required before changing this record')


def _publish(captured, tx, entries):
    _writable_owner(captured)
    material = _required(captured.passphrase)
    blob = records.encode(captured.owner, entries, material)
    tx.put(_NS, _key(captured.owner), blob)


@_guard
def entries(user: str) -> dict:
    """Read one bounded snapshot; returned mutable values are detached."""
    captured = _capture(user)
    with _transaction(captured) as tx:
        data, _, _ = _load_captured(captured, tx)
        return data


@_guard
def put(user: str, name: str, value: dict | str) -> None:
    replacement = records.validate_entries({name: value})[name]
    captured = _capture(user)
    _writable_owner(captured)
    with _transaction(captured) as tx:
        data, _, metadata = _load_captured(captured, tx)
        _require_current_format(metadata)
        data[name] = replacement
        _publish(captured, tx, data)


@_guard
def get(user: str, name: str) -> dict | str | None:
    records.entry_name(name)
    return entries(user).get(name)


@_guard
def delete_many(user: str, names_to_delete) -> bool:
    if type(names_to_delete) not in (tuple, list) or len(names_to_delete) > records.MAX_ENTRIES:
        raise VaultError('invalid bounded vault deletion')
    selected = tuple(names_to_delete)
    for name in selected:
        records.entry_name(name)
    captured = _capture(user)
    _writable_owner(captured)
    with _transaction(captured) as tx:
        data, _, metadata = _load_captured(captured, tx)
        if not any(name in data for name in selected):
            return False
        _require_current_format(metadata)
        for name in selected:
            data.pop(name, None)
        _publish(captured, tx, data)
    return True


def delete(user: str, name: str) -> bool:
    return delete_many(user, (name,))


def names(user: str) -> list[str]:
    return sorted(entries(user))


@dataclass(frozen=True, repr=False)
class Snapshot:
    """Process/thread-bound CAS authority; repr contains no credential material."""
    _captured: _Capture
    _name: str
    _ciphertext_sha256: str | None
    _storage_identity: tuple | None
    value: dict | str | None = field(repr=False)
    legacy: bool


def _snapshot_names(names, for_update):
    if type(names) not in (tuple, list) or not 1 <= len(names) <= 32:
        raise VaultError('invalid snapshot entry selection')
    selected_names = tuple(names)
    for name in selected_names:
        records.entry_name(name)
    if len(set(selected_names)) != len(selected_names):
        raise VaultError('duplicate snapshot entry selection')
    if type(for_update) is not bool:
        raise VaultError('invalid snapshot mode')
    return selected_names


@_guard
def _snapshots_captured(captured, names, *, for_update=False):
    """Use exactly the caller's captured authority; never select a root/key/backend.

    Private workflows must establish their existing-root or database authority
    before this boundary. Public snapshots retain their standalone selection.
    """
    selected_names = _snapshot_names(names, for_update)
    if type(captured) is not _Capture:
        raise VaultError('invalid captured vault snapshot authority')
    captured.check()
    with _transaction(captured) as tx:
        data, blob, metadata = _load_captured(captured, tx)
        if for_update:
            _writable_owner(captured)
            _required(captured.passphrase)
            _require_current_format(metadata)
            _ready_captured(captured)
        identity = tx.authority_identity() if type(tx) is storage.FileTransaction else None
        revision = recovery._hash(blob)
        legacy = metadata is not None and not metadata.context_authenticated
        return {name: Snapshot(captured, name, revision, identity, data.get(name), legacy)
                for name in selected_names}


@_guard
def snapshots(user: str, names, *, for_update=False) -> dict[str, Snapshot]:
    """Read 1..32 entries under one captured selection and whole-row revision.

    Uses the same explicit PostgreSQL selection rules as snapshot(), including
    for read-only use. Each result is a separate CAS handle: settling one
    invalidates its siblings. This is not a multi-entry write transaction.
    """
    selected_names = _snapshot_names(names, for_update)
    captured = _capture(user, snapshot_selection=True)
    return _snapshots_captured(captured, selected_names, for_update=for_update)


@_guard
def snapshot(user: str, name: str, *, for_update=False) -> Snapshot:
    return snapshots(user, (name,), for_update=for_update)[name]


@_guard
def require_snapshot_writable(selected: Snapshot) -> None:
    """Actual captured-key v1 readiness before provider dispatch."""
    if type(selected) is not Snapshot:
        raise VaultError('invalid vault snapshot')
    selected._captured.check()
    _writable_owner(selected._captured)
    if selected.legacy:
        raise VaultMigrationRequired('explicit vault format migration is required before changing this record')
    _ready_captured(selected._captured)


@dataclass(repr=False)
class PendingUpdate:
    """Bounded local settlement handle; never stringify or persist plaintext.

    Exact target ciphertext is retained before publication so lost database or
    filesystem acknowledgement can be settled without repeating a provider.
    This in-memory handle is not recovery from process death or a lost provider
    response; those states require explicit account reconciliation/reconnection.
    """
    _snapshot: Snapshot
    _replacement: dict
    _ciphertext: bytes | None = None


@_guard
def prepare_snapshot_update(selected: Snapshot, value: dict | str) -> PendingUpdate:
    if type(selected) is not Snapshot:
        raise VaultError('invalid vault snapshot')
    captured = selected._captured
    captured.check()
    _writable_owner(captured)
    _required(captured.passphrase)
    if selected.legacy:
        raise VaultMigrationRequired('explicit vault format migration is required before changing this record')
    return PendingUpdate(selected, records.validate_entries({selected._name: value}))


@_guard
def settle_snapshot_update(pending: PendingUpdate) -> dict | str:
    """Publish once, accept an exact own committed target, or refuse a conflict."""
    if type(pending) is not PendingUpdate:
        raise VaultError('invalid pending vault update')
    selected = pending._snapshot
    captured = selected._captured
    captured.check()
    with _transaction(captured, expected_identity=selected._storage_identity) as tx:
        identity = tx.authority_identity() if type(tx) is storage.FileTransaction else None
        if identity != selected._storage_identity:
            raise VaultConflict('the captured vault storage location was replaced')
        current = tx.get(_NS, _key(captured.owner), cap=_ROW_CAP)
        if pending._ciphertext is not None and current == pending._ciphertext:
            # Exact own target, including its authenticated random revision.
            # Visibility alone does not prove that a previous filesystem
            # directory barrier succeeded. Re-publish these exact bytes under
            # the same lock and complete backend durability/commit again.
            tx.put(_NS, _key(captured.owner), pending._ciphertext)
            confirmed, metadata = records.decode(captured.owner, current,
                                                   _required(captured.passphrase))
            if not metadata.context_authenticated:
                raise VaultError('pending target is not an authenticated vault record')
            return confirmed[selected._name]
        if recovery._hash(current) != selected._ciphertext_sha256:
            raise VaultConflict('stored credentials changed while the operation was in progress')
        data, blob, metadata = _load_captured(captured, tx)
        _require_current_format(metadata)
        if blob != current:
            raise VaultConflict('stored credentials changed during settlement')
        if pending._ciphertext is None:
            replacement = records.validate_entries(pending._replacement)
            data[selected._name] = replacement[selected._name]
            pending._ciphertext = records.encode(captured.owner, data,
                                                 _required(captured.passphrase))
        tx.put(_NS, _key(captured.owner), pending._ciphertext)
        confirmed, _ = records.decode(captured.owner, pending._ciphertext,
                                       _required(captured.passphrase))
        return confirmed[selected._name]


def _legacy_key(key):
    storage._address(_NS, key)
    return key not in memory.SYSTEM_OWNERS and re.fullmatch(r'.*-[0-9a-f]{64}', key) is None


@_guard
def legacy_tenants() -> list[str]:
    captured = _capture(owner_required=False)
    with _transaction(captured) as tx:
        return [key for key in tx.keys(_NS) if _legacy_key(key)]


@_guard
def legacy_names(legacy_key: str) -> list[str]:
    if not _legacy_key(legacy_key):
        raise VaultError('the address is not a quarantined legacy vault')
    captured = _capture(owner_required=False)
    with _transaction(captured) as tx:
        blob = tx.get(_NS, legacy_key, cap=_ROW_CAP)
        if blob is None:
            return []
        return sorted(records.decode_legacy(blob, _required(captured.passphrase)))


def _verifier(owner, material):
    def verify(blob):
        _, metadata = records.decode(owner, blob, material)
        if not metadata.context_authenticated:
            raise VaultError('vault recovery target lacks authenticated owner binding')
        return True
    return verify


@_guard
def migrate_legacy(legacy_key: str, owner: str, *, operation_id: str,
                   overwrite: bool = False) -> int:
    """Explicit ownership claim; retain operation_id before calling or retrying."""
    recovery._id(operation_id)
    if type(overwrite) is not bool or not _legacy_key(legacy_key):
        raise VaultError('the address is not a quarantined legacy vault')
    captured = _capture(memory.assert_not_system_owner(_owner(owner)))
    _writable_owner(captured)
    material = _required(captured.passphrase)
    verify = _verifier(captured.owner, material)
    with _transaction(captured) as tx:
        blob = tx.get(_NS, legacy_key, cap=_ROW_CAP)
        if blob is None:
            raise VaultError('the quarantined legacy vault is absent; inspect the retained operation')
        old = records.decode_legacy(blob, material)
        data, previous, _ = _load_captured(captured, tx)
        moved = 0
        for name, value in old.items():
            if name not in data or overwrite:
                data[name] = value
                moved += 1
        target = records.encode(captured.owner, data, material)
        recovery.prepare(tx, material, operation_id=operation_id, kind='migrate',
            source_key=legacy_key, source_blob=blob, target_key=_key(captured.owner),
            previous_blob=previous, target_blob=target, owner=captured.owner,
            entries_changed=moved, verify_target=verify)
        return recovery.apply(tx, material, operation_id, owner=captured.owner,
                              verify_target=verify)['entries_changed']


@_guard
def discard_legacy(legacy_key: str, *, operation_id: str) -> bool:
    """Remove the live quarantine row; retain encrypted recovery copies."""
    recovery._id(operation_id)
    if not _legacy_key(legacy_key):
        return False
    captured = _capture(owner_required=False)
    material = _required(captured.passphrase)
    with _transaction(captured) as tx:
        blob = tx.get(_NS, legacy_key, cap=_ROW_CAP)
        if blob is None:
            return False
        # Explicit discard also resolves unreadable quarantine. No ownership
        # is assigned, and the exact opaque bytes remain in recovery storage.
        recovery.prepare(tx, material, operation_id=operation_id, kind='discard',
                         source_key=legacy_key, source_blob=blob)
        recovery.apply(tx, material, operation_id)
        return True


@_guard
def rotate(user: str, *, operation_id: str, replacement_custody=None) -> dict:
    """Explicit format/key change. No environment or custody source is modified.

    None keeps the captured key (format migration). Replacement custody is an
    already captured, bounded source. Recovery uses that replacement key;
    historical copies remain decryptable with the old key.
    """
    recovery._id(operation_id)
    captured = _capture(user)
    _writable_owner(captured)
    selected = captured.passphrase if replacement_custody is None else replacement_custody
    if type(selected) is not custody.CapturedPassphrase:
        raise VaultError('rotation requires captured replacement custody')
    material = _required(selected)
    verify = _verifier(captured.owner, material)
    with _transaction(captured) as tx:
        data, before, _ = _load_captured(captured, tx)
        if before is None:
            raise VaultError('there is no stored vault record to rotate')
        after = records.encode(captured.owner, data, material)
        recovery.prepare(tx, material, operation_id=operation_id, kind='rotate',
            source_key=_key(captured.owner), source_blob=before,
            target_key=_key(captured.owner), previous_blob=before, target_blob=after,
            owner=captured.owner, entries_changed=len(data), verify_target=verify)
        return recovery.apply(tx, material, operation_id, owner=captured.owner,
                              verify_target=verify)


@_guard
def resume_change(operation_id: str, *, owner=None, replacement_custody=None) -> dict:
    """Resume one explicitly selected prepared change; no new ciphertext/provider action."""
    recovery._id(operation_id)
    captured = _capture(owner, owner_required=owner is not None,
        selected_custody=_UNSELECTED if replacement_custody is None else replacement_custody)
    selected = captured.passphrase if replacement_custody is None else replacement_custody
    if type(selected) is not custody.CapturedPassphrase:
        raise VaultError('recovery requires captured custody')
    material = _required(selected)
    with _transaction(captured) as tx:
        return recovery.apply(tx, material, operation_id, owner=captured.owner,
            verify_target=_verifier(captured.owner, material) if captured.owner is not None else None)


@_guard
def inspect_change(operation_id: str, *, owner=None, replacement_custody=None) -> dict:
    """Authenticate retained evidence without modifying vault/recovery rows.

    A committed receipt records historical completion, not current row equality.
    An incomplete or unauthenticated receipt raises instead of claiming success.
    """
    recovery._id(operation_id)
    captured = _capture(owner, owner_required=owner is not None,
        selected_custody=_UNSELECTED if replacement_custody is None else replacement_custody)
    material = _required(captured.passphrase)
    with _transaction(captured) as tx:
        prepared, plan, copies = recovery._load(tx, material, captured.owner, operation_id)
        if plan['kind'] != 'discard':
            _verifier(captured.owner, material)(copies['target'])
        published = recovery._marker(tx, material, captured.owner, operation_id, 'published', prepared)
        committed = recovery._marker(tx, material, captured.owner, operation_id, 'committed', prepared)
        if committed and not published:
            raise VaultRecoveryError(operation_id)
        return {'operation_id': operation_id, 'kind': plan['kind'],
            'published': published, 'committed': committed,
            'source_sha256': plan['source_sha256'], 'target_sha256': plan['target_sha256'],
            'current_source_sha256': recovery._hash(tx.get(_NS, plan['source_key'], cap=_ROW_CAP)),
            'current_target_sha256': (recovery._hash(tx.get(_NS, plan['target_key'], cap=_ROW_CAP))
                                      if plan['target_key'] is not None else None),
            'entries_changed': plan['entries_changed']}


_LEGACY_MATCH_REASON = (
    'outbound content matches a stored secret from an unmigrated credential '
    'vault that may belong to this identity. An operator must migrate or '
    'discard it before this can be sent.')
_LEGACY_UNREADABLE_REASON = (
    'an unmigrated credential vault that may belong to this identity cannot be '
    'read, so outbound content cannot be checked against it. An operator must '
    'migrate or discard it before this can be sent.')


def _leaves(value):
    if type(value) is str:
        yield value
    elif type(value) is dict:
        for item in value.values():
            yield from _leaves(item)
    elif type(value) is list:
        for item in value:
            yield from _leaves(item)


def _legacy_key_for(user):
    """Quarantine inspection only; never an authentication fallback."""
    owner = _owner(user)
    if memory.is_system_owner(owner):
        return None
    key = memory.safe_id(owner)
    return key if _legacy_key(key) else None


def legacy_scan(user: str, matches) -> str | None:
    """Bounded quarantine scan; failures are generic outbound refusals."""
    try:
        captured = _capture(user)
        key = _legacy_key_for(captured.owner)
        if key is None:
            return None
        with _transaction(captured) as tx:
            blob = tx.get(_NS, key, cap=_ROW_CAP)
            if blob is None:
                return None
            data = records.decode_legacy(blob, _required(captured.passphrase))
            for value in _leaves(data):
                if matches(value):
                    return _LEGACY_MATCH_REASON
            return None
    except Exception:
        return _LEGACY_UNREADABLE_REASON

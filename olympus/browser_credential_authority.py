"""Private captured memory-root and lease-generation authority for credentials.

This binds lease lookup and claim to held native directories. Verification is
performed before each credential action; it does not hold a cross-process lock
across a browser command or prove that a provider completed that command.
"""
from . import native_cleanup
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
import json
import os
import secrets
import threading
import time

from . import archive_io, browserlease, config, vault_storage

_RECORD = 'browser_lease.json'
_LOCK = 'browser-lease.lock'
_MAX_RECORD = 64 * 1024


_CLEANUP_KEY = '_browser_credential_cleanup_unconfirmed'
_CLEANUP_NOTE = 'browser credential authority cleanup is unconfirmed'


def _cleanup_unknown(error):
    attributes = BaseException.__getattribute__(error, '__dict__')
    return (type(attributes) is dict and attributes.get(_CLEANUP_KEY) is True
            or native_cleanup.unknown(error))


def _mark_cleanup_unknown(error):
    attributes = BaseException.__getattribute__(error, '__dict__')
    if type(attributes) is dict:
        attributes[_CLEANUP_KEY] = True
    native_cleanup.mark(error)


def _guard(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        failed = False
        cleanup_unknown = False
        try:
            return function(*args, **kwargs)
        except BaseException as error:
            cleanup_unknown = _cleanup_unknown(error)
            if cleanup_unknown and args and type(args[0]) is Authority:
                args[0]._cleanup_unconfirmed = True
            if not isinstance(error, Exception):
                raise
            failed = True
        if failed:
            refusal = browserlease.OwnershipRefused()
            if cleanup_unknown:
                _mark_cleanup_unknown(refusal)
            raise refusal from None
    return guarded


def _drain(actions, primary=None, *, owner=None):
    try:
        confirmed = (native_cleanup.drain(actions, primary) if owner is None
                     else native_cleanup.drain_owned(owner, actions, primary))
    except BaseException as error:
        _mark_cleanup_unknown(error)
        raise
    if not confirmed:
        _mark_cleanup_unknown(primary)
    return confirmed


@dataclass(frozen=True, repr=False)
class _Binding:
    root: object
    root_identity: tuple
    locks_identity: tuple
    lock_identity: tuple
    owner: str
    lease_id: str


class Authority:
    """A process/thread-local captured root, explicitly closed by its workflow."""

    @_guard
    def __init__(self, *, binding=None, initialize_root=False):
        if type(initialize_root) is not bool or (binding is not None and initialize_root):
            raise browserlease.OwnershipRefused()
        self.root = archive_io.selected_directory(config.MEMORY_DIR)
        if binding is not None and (type(binding) is not _Binding or binding.root != self.root):
            raise browserlease.OwnershipRefused()
        self._pid, self._thread = os.getpid(), threading.get_ident()
        self._closed = False
        self._cleanup_unconfirmed = False
        self.directory = self.locks = None
        self._lock_identity = None
        try:
            self.directory = vault_storage._Directory(self.root, create=initialize_root)
            self.root_identity = vault_storage._directory_identity(self.directory)
            if binding is not None and binding.root_identity != self.root_identity:
                raise browserlease.OwnershipRefused()
            self.check()
            # Establish coordination identity before callbacks can run. The
            # memory root is held through explicit first-use creation and
            # coordination acquisition; there is no pathname-only handoff.
            fh = self._open_lock(binding)
            owned, fh = fh, None
            _drain([owned.close], owner=self)
        except BaseException as primary:
            _drain([self.close], primary, owner=self)
            raise

    @_guard
    def check(self):
        if self._cleanup_unconfirmed:
            error = browserlease.OwnershipRefused()
            _mark_cleanup_unknown(error)
            raise error
        if (self._closed or (self._pid, self._thread) != (os.getpid(), threading.get_ident())
                or archive_io.selected_directory(config.MEMORY_DIR) != self.root):
            raise browserlease.OwnershipRefused()
        self.directory.check()
        if vault_storage._directory_identity(self.directory) != self.root_identity:
            raise browserlease.OwnershipRefused()
        if self.locks is not None:
            self.locks.check()

    @_guard
    def _open_lock(self, binding=None):
        self.check()
        if self.locks is None:
            self.locks = self.directory.child('locks', create=binding is None)
        if binding is not None and vault_storage._directory_identity(self.locks) != binding.locks_identity:
            raise browserlease.OwnershipRefused()
        create = binding is None and self._lock_identity is None
        fd = self.locks._open(_LOCK, os.O_RDWR | (os.O_CREAT if create else 0))
        try:
            identity = vault_storage._file_identity(vault_storage._file_information(fd))[:2]
            if ((self._lock_identity is not None and identity != self._lock_identity)
                    or (binding is not None and identity != binding.lock_identity)):
                raise browserlease.OwnershipRefused()
            self._lock_identity = identity
            self.check()
        except BaseException as primary:
            owned, fd = fd, None
            _drain([lambda: os.close(owned)], primary, owner=self)
            raise
        # Dispatch can transfer the descriptor before a lost return. Once called,
        # an exception does not authorize a raw close or a second wrapping attempt.
        owned, fd = fd, None
        try:
            return os.fdopen(owned, 'r+b', buffering=0)
        except BaseException as error:
            self._cleanup_unconfirmed = True
            _mark_cleanup_unknown(error)
            raise

    @_guard
    def _check_lock(self):
        self.check()
        if (not getattr(browserlease._LOCAL, 'depth', 0)
                or getattr(browserlease._LOCAL, 'capture', None) is not self):
            raise browserlease.OwnershipRefused()
        fd = self.locks._open(_LOCK, os.O_RDWR)
        primary = None
        try:
            if vault_storage._file_identity(vault_storage._file_information(fd))[:2] != self._lock_identity:
                raise browserlease.OwnershipRefused()
        except BaseException as error:
            primary = error
            raise
        finally:
            owned, fd = fd, None
            _drain([lambda: os.close(owned)], primary, owner=self)
        self.check()

    @contextmanager
    def lock(self, timeout=browserlease.LOCK_TIMEOUT, *, binding=None):
        self.check()
        depth = getattr(browserlease._LOCAL, 'depth', 0)
        if depth:
            self._check_lock()
            if binding is not None and (binding.lock_identity != self._lock_identity
                    or binding.locks_identity != vault_storage._directory_identity(self.locks)):
                raise browserlease.OwnershipRefused()
            browserlease._LOCAL.depth = depth + 1
            try:
                yield
            except BaseException as error:
                if _cleanup_unknown(error):self._cleanup_unconfirmed = True
                raise
            finally:
                browserlease._LOCAL.depth -= 1
            return
        deadline = time.monotonic() + max(0.0, timeout)
        if not browserlease._THREAD_LOCK.acquire(timeout=max(0.0, timeout)):
            raise browserlease.OwnershipRefused()
        fh, locked, primary = None, False, None
        try:
            fh = self._open_lock(binding)
            browserlease._acquire_file_lock(fh, deadline)
            locked = True
            browserlease._LOCAL.root = self.root
            browserlease._LOCAL.capture = self
            browserlease._LOCAL.depth = 1
            self._check_lock()
            yield
        except BaseException as error:
            primary = error
            if _cleanup_unknown(error):self._cleanup_unconfirmed = True
            raise
        finally:
            browserlease._LOCAL.depth = 0
            browserlease._LOCAL.capture = None
            browserlease._LOCAL.root = None
            actions = []
            owned_fh, fh = fh, None
            if owned_fh is not None:
                if locked:
                    actions.append(lambda: browserlease._release_file_lock(owned_fh))
                actions.append(owned_fh.close)
            actions.append(browserlease._THREAD_LOCK.release)
            _drain(actions, primary, owner=self)

    @_guard
    def _read(self):
        self._check_lock()
        try:
            raw, _identity = self.directory.read_with_identity(_RECORD, _MAX_RECORD)
        except FileNotFoundError as error:
            if _cleanup_unknown(error): raise
            self._check_lock()
            return None
        record = browserlease._decode(raw.decode('utf-8'))
        self._check_lock()
        return record

    @_guard
    def _confirm(self, record):
        """Reconfirm the same visible record without rewriting or new generation."""
        self._check_lock()
        fd = self.directory._open(_RECORD, os.O_RDWR)
        primary = None
        try:
            identity = vault_storage._file_identity(vault_storage._file_information(fd))
            raw, observed = self.directory.read_with_identity(_RECORD, _MAX_RECORD)
            if ((observed['device'], observed['inode']) != identity[:2]
                    or browserlease._decode(raw.decode('utf-8')) != record):
                raise browserlease.OwnershipRefused()
            os.fsync(fd)
            if vault_storage._file_identity(vault_storage._file_information(fd)) != identity:
                raise browserlease.OwnershipRefused()
            self.directory.sync()
            raw, observed = self.directory.read_with_identity(_RECORD, _MAX_RECORD)
            if ((observed['device'], observed['inode']) != identity[:2]
                    or browserlease._decode(raw.decode('utf-8')) != record
                    or vault_storage._file_identity(vault_storage._file_information(fd)) != identity):
                raise browserlease.OwnershipRefused()
            self._check_lock()
            return record
        except BaseException as error:
            primary = error
            raise
        finally:
            owned, fd = fd, None
            _drain([lambda: os.close(owned)], primary, owner=self)

    @_guard
    def current(self):
        with self.lock():
            return self._read()

    @_guard
    def claim(self, owner, transport):
        """Claim under the original native root; no diagnostic backfill write."""
        name = browserlease.require_owner(owner)
        if type(transport) is not str or not transport or len(transport) > 128:
            raise browserlease.OwnershipRefused()
        with self.lock():
            record = self._read()
            if record is not None:
                if browserlease.owner_of(record) != name:
                    raise browserlease.OwnershipRefused()
                return self._confirm(record)
            record = {'schema_version': browserlease.SCHEMA_VERSION, 'owner': name,
                'lease_id': secrets.token_hex(16), 'transport': transport,
                'claimed_at': time.time(), 'fingerprint': ''}
            payload = json.dumps(record, ensure_ascii=True, allow_nan=False).encode('utf-8')
            if len(payload) > _MAX_RECORD:
                raise browserlease.OwnershipRefused()
            self._check_lock()
            try:
                self.directory.write_new(_RECORD, payload)
            except FileExistsError as error:
                if _cleanup_unknown(error): raise
                record = self._read()
                if record is None or browserlease.owner_of(record) != name:
                    raise browserlease.OwnershipRefused()
                return self._confirm(record)
            observed = self._read()
            if observed != record:
                raise browserlease.OwnershipRefused()
            return self._confirm(record)

    @_guard
    def bind(self, owner, record):
        """Bind an observed generation, then independently verify it on disk."""
        name = browserlease.require_owner(owner)
        if (type(record) is not dict or type(record.get('lease_id')) is not str
                or not record['lease_id'] or len(record['lease_id']) > 256):
            raise browserlease.OwnershipRefused()
        with self.lock():
            observed = self._read()
            if observed != record or browserlease.owner_of(observed) != name:
                raise browserlease.OwnershipRefused()
            self._confirm(observed)
            binding = _Binding(self.root, self.root_identity,
                vault_storage._directory_identity(self.locks), self._lock_identity,
                name, record['lease_id'])
            self.verify(binding, name)
            return binding

    @_guard
    def verify(self, binding, owner):
        self.check()
        name = browserlease.require_owner(owner)
        if (type(binding) is not _Binding or binding.root != self.root
                or binding.root_identity != self.root_identity or binding.owner != name):
            raise browserlease.OwnershipRefused()
        with self.lock(binding=binding):
            record = self._read()
            if (record is None or browserlease.owner_of(record) != name
                    or record['lease_id'] != binding.lease_id):
                raise browserlease.OwnershipRefused()
            return record

    @_guard
    def _read_record_fd(self, fd):
        before = vault_storage._file_identity(vault_storage._file_information(fd))
        if not 0 <= before[2] <= _MAX_RECORD:
            raise browserlease.OwnershipRefused()
        remaining, data = before[2] + 1, bytearray()
        while remaining:
            part = os.read(fd, remaining)
            if not part:
                break
            data.extend(part)
            remaining -= len(part)
        if len(data) != before[2] or vault_storage._file_identity(vault_storage._file_information(fd)) != before:
            raise browserlease.OwnershipRefused()
        self._check_lock()
        return browserlease._decode(bytes(data).decode('utf-8')), before

    @_guard
    def _release(self, binding, owner, *, allow_missing=False):
        """Local settlement only, after the browser has confirmed destruction.

        allow_missing is reserved for the same retained release attempt whose
        unlink may have succeeded before a lost directory-barrier response.
        This method does not itself prove external browser destruction.
        """
        self.check()
        name = browserlease.require_owner(owner)
        if (type(allow_missing) is not bool or type(binding) is not _Binding
                or binding.root != self.root or binding.root_identity != self.root_identity
                or binding.owner != name):
            raise browserlease.OwnershipRefused()
        with self.lock(binding=binding):
            record = self._read()
            if record is None:
                if not allow_missing:
                    raise browserlease.OwnershipRefused()
                self.directory.sync()
                if self._read() is not None:
                    raise browserlease.OwnershipRefused()
                return
            if browserlease.owner_of(record) != name or record['lease_id'] != binding.lease_id:
                raise browserlease.OwnershipRefused()
            primary = None
            if self.directory.win is None:
                fd = self.directory._open(_RECORD, os.O_RDONLY)
                try:
                    observed, identity = self._read_record_fd(fd)
                    if observed != record:
                        raise browserlease.OwnershipRefused()
                    self._check_lock()
                    if vault_storage._file_identity(os.stat(_RECORD, dir_fd=self.directory.fd,
                            follow_symlinks=False)) != identity:
                        raise browserlease.OwnershipRefused()
                    os.unlink(_RECORD, dir_fd=self.directory.fd)
                except BaseException as error:
                    primary = error
                    raise
                finally:
                    owned, fd = fd, None
                    _drain([lambda: os.close(owned)], primary, owner=self)
            else:
                from . import vault_windows as native
                import ctypes as C
                self.directory._case_spelling(_RECORD)
                handle = native.open_relative(self.directory.win.handle, _RECORD, delete=True)
                # Qualified transfer reports ambiguous outcomes without
                # guessing whether a raw handle may still be closed.
                fd = native.to_fd(handle, os.O_RDONLY)
                try:
                    observed, identity = self._read_record_fd(fd)
                    if observed != record:
                        raise browserlease.OwnershipRefused()
                    self._check_lock()
                    self.directory._case_spelling(_RECORD)
                    _kernel, nt = native._apis()
                    status, delete = native.IoStatus(), C.c_ubyte(1)
                    result = nt.NtSetInformationFile(handle, C.byref(status), C.byref(delete), 1, 13)
                    if result < 0:
                        native._error(result)
                except BaseException as error:
                    primary = error
                    raise
                finally:
                    owned, fd = fd, None
                    _drain([lambda: os.close(owned)], primary, owner=self)
            self.directory.sync()
            if self._read() is not None:
                raise browserlease.OwnershipRefused()

    @_guard
    def close(self):
        if (self._pid, self._thread) != (os.getpid(), threading.get_ident()):
            raise browserlease.OwnershipRefused()
        if self._closed:
            if self._cleanup_unconfirmed:
                error = browserlease.OwnershipRefused()
                _mark_cleanup_unknown(error)
                raise error
            return
        if getattr(browserlease._LOCAL, 'capture', None) is self:
            raise browserlease.OwnershipRefused()
        self._closed = True
        locks, directory = self.locks, self.directory
        self.locks = self.directory = None
        _drain([owned.close for owned in (locks, directory) if owned is not None], owner=self)
        if self._cleanup_unconfirmed:
            error = browserlease.OwnershipRefused()
            _mark_cleanup_unknown(error)
            raise error


@contextmanager
def capture(*, binding=None, initialize_root=False):
    authority = Authority(binding=binding, initialize_root=initialize_root)
    primary = None
    try:
        yield authority
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            authority.close()
        except BaseException:
            if primary is None:
                raise
            _mark_cleanup_unknown(primary)

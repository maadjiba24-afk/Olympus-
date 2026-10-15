"""Captured-root transactions for vault namespaces only.

This does not upgrade other FileStore/proclock callers or claim distributed
filesystem locking. All vault rows share one namespace lock so migrations can
serialize with CRUD. PostgreSQL uses one backend-native transaction instead.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from functools import wraps
import errno
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
import uuid

from . import store, native_cleanup
from .gallery_state import Directory as _GalleryDirectory

NAMESPACES = frozenset({'vault', 'vault_recovery'})
MAX_BLOB = 96 * 1024 * 1024
MAX_KEYS = 4096
MAX_KEY_BYTES = 128
LOCK_TIMEOUT = 60.0
_GATE = threading.RLock()
_LOCAL = threading.local()


def _after_fork():
    # A copied thread mutex is not authority in the child. Do not clear current:
    # inherited active transactions must still fail their captured-PID check.
    global _GATE
    _GATE = threading.RLock()


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_after_fork)


class StorageError(RuntimeError):
    """Vault storage or transaction availability failed closed."""


def _records_cleanup(function):
    """Retain uncertainty on this resource owner, never on unrelated authority."""
    @wraps(function)
    def guarded(self, *args, **kwargs):
        try:
            return function(self, *args, **kwargs)
        except BaseException as error:
            if native_cleanup.unknown(error):
                self._cleanup_unconfirmed = True
            raise
    return guarded


def _drain_owned(owner, actions, primary=None):
    if primary is not None and native_cleanup.unknown(primary):
        owner._cleanup_unconfirmed = True
    try:
        confirmed = native_cleanup.drain(actions, primary)
    except BaseException:
        owner._cleanup_unconfirmed = True
        raise
    if not confirmed:
        owner._cleanup_unconfirmed = True
    return confirmed


def _uncertain_owner():
    error = StorageError('vault authority cleanup requires reconciliation')
    native_cleanup.mark(error)
    return error


def _acquired_error(primary):
    # Only initial lookup can establish an absent leaf. Disappearance after
    # acquisition is a changed observation, even when all cleanup succeeds.
    if isinstance(primary, FileNotFoundError):
        error = StorageError('vault file changed after acquisition')
        if native_cleanup.unknown(primary):
            native_cleanup.mark(error)
        return error
    return primary


def _address(namespace, key):
    # FileStore's historical _safe conversion is lossy. Exact-owner keys already
    # fit this domain; reject aliases rather than silently changing an address.
    if (type(namespace) is not str or namespace not in NAMESPACES
            or type(key) is not str or not 1 <= len(key) <= MAX_KEY_BYTES
            or key in ('.', '..') or not re.fullmatch(r'[A-Za-z0-9_.-]+', key)):
        raise StorageError('invalid vault storage address')


def _cap(value, maximum=MAX_BLOB):
    if type(value) is not int or not 0 <= value <= maximum:
        raise StorageError('invalid vault storage bound')


class _Lease:
    def __init__(self):
        self.pid, self.thread = os.getpid(), threading.get_ident()
        self.active = False

    def owner(self):
        if (self.pid, self.thread) != (os.getpid(), threading.get_ident()):
            raise StorageError('vault authority belongs to another process or thread')

    def check(self):
        self.owner()
        if not self.active:
            raise StorageError('vault transaction is not active')


def _file_information(fd):
    if os.name == 'nt':
        import msvcrt
        from .gallery_windows import information
        return information(msvcrt.get_osfhandle(fd), False)
    return os.fstat(fd)


def _file_identity(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, 'st_file_attributes', 0) & 0x400):
        raise StorageError('vault storage requires an ordinary single-link file')
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            getattr(info, 'st_ctime_ns', None))


class _CapturedDirectory(_GalleryDirectory):
    """Native acquisition, lifetime, fresh reads and sticky cleanup uncertainty.

    Native lookup/rename primitives remain M07's. No claim is made that share
    flags prevent attribute mutation; every operation rechecks held ancestors.
    POSIX retains ancestor descriptors as well as the final directory so a
    changed path entry is refused before another vault mutation.
    """
    @_records_cleanup
    def __init__(self, path, create=False):
        self._initialize(Path(os.path.abspath(path)))
        try:
            if os.name == 'nt':
                from . import vault_windows as native
                self.win = object.__new__(native.NativeDirectory)
                self.win.owner = self
                self.win.handles, self.win.owned_start = [], 0
                self.win.handles.append(native._anchor(self.path.anchor))
                for part in self.path.parts[1:]:
                    self.win.check()
                    self.win.handles.append(native.open_relative(
                        self.win.handles[-1], part, directory=True, create=create))
                self.win.handle = self.win.handles[-1]
            else:
                self._parents.append(os.open(self.path.anchor,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
                self._names.append(self.path.anchor)
                for part in self.path.parts[1:]:
                    self._append_posix(part, create)
                self.fd = self._parents[-1]
                self.identity = os.fstat(self.fd)
            self.check()
        except BaseException as primary:
            _drain_owned(self, [self.close], primary)
            raise

    def _initialize(self, path):
        self.path, self.fd, self.win = path, None, None
        self.win_handles = []
        self._parents, self._names, self._owned_start = [], [], 0
        self._iterators = []
        self._cleanup_unconfirmed = False
        self._pid, self._thread, self._closed = os.getpid(), threading.get_ident(), False

    def _owner(self):
        if (self._pid, self._thread) != (os.getpid(), threading.get_ident()):
            raise StorageError('vault directory belongs to another process or thread')

    def _append_posix(self, name, create):
        parent = self._parents[-1]
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
            os.fsync(parent)
        self._parents.append(os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                     dir_fd=parent))
        self._names.append(name)

    @_records_cleanup
    def child(self, name, create=False):
        _address('vault', name)
        self.check()
        child = object.__new__(_CapturedDirectory)
        child._initialize(self.path / name)
        try:
            if self.win is not None:
                from . import vault_windows as native
                child.win = object.__new__(native.NativeDirectory)
                child.win.owner = child
                child.win.handles = list(self.win.handles)
                child.win.owned_start = len(child.win.handles)
                child.win.handles.append(native.open_relative(
                    self.win.handle, name, directory=True, create=create))
                child.win.handle = child.win.handles[-1]
            else:
                child._parents, child._names = list(self._parents), list(self._names)
                child._owned_start = len(child._parents)
                child._append_posix(name, create)
                child.fd = child._parents[-1]
                child.identity = os.fstat(child.fd)
            child.check()
            return child
        except BaseException as primary:
            _drain_owned(self, [child.close], primary)
            raise

    def check(self):
        self._owner()
        if self._cleanup_unconfirmed:
            error = StorageError('native directory cleanup requires reconciliation')
            native_cleanup.mark(error)
            raise error
        if self._closed:
            raise StorageError('vault directory is closed')
        if self.win is not None:
            if not self.win.handles:
                raise StorageError('vault directory has no held authority')
            self.win.check()
            return
        if not self._parents:
            raise StorageError('vault directory has no held authority')
        for index, fd in enumerate(self._parents):
            held = os.fstat(fd)
            entry = os.stat(self._names[index], follow_symlinks=False,
                            dir_fd=self._parents[index - 1] if index else None)
            if (not stat.S_ISDIR(held.st_mode) or not stat.S_ISDIR(entry.st_mode)
                    or (held.st_dev, held.st_ino) != (entry.st_dev, entry.st_ino)):
                raise StorageError('vault directory identity changed')

    def close(self):
        self._owner()
        if self._closed:
            if self._cleanup_unconfirmed:
                error = StorageError('native directory cleanup remains unconfirmed')
                native_cleanup.mark(error)
                raise error
            return
        self._closed = True
        iterators, self._iterators = self._iterators, []
        actions = [iterator.close for iterator in iterators]
        if self.win is not None:
            from .vault_windows import close
            handles = self.win.handles[self.win.owned_start:]
            self.win.handles = []
            self.win = None
        else:
            close, handles = os.close, self._parents[self._owned_start:]
        self.fd, self._parents = None, []
        actions.extend(lambda handle=handle: close(handle) for handle in reversed(handles))
        try:
            _drain_owned(self, actions)
        except BaseException:
            self._cleanup_unconfirmed = True
            raise
        if self._cleanup_unconfirmed:
            error = StorageError('native directory cleanup remains unconfirmed')
            native_cleanup.mark(error)
            raise error

    def entries(self):
        self.check()
        if len(self._iterators) >= 16:
            raise StorageError('too many live vault enumerations')
        native_mode = self.win is not None
        iterator = self.win.entries() if native_mode else os.scandir(self.fd)
        self._iterators.append(iterator)
        directory = self
        class Entry:
            def __init__(self, entry):
                self.name, self._entry = entry.name, entry
            def stat(self, *, follow_symlinks=False):
                if follow_symlinks:
                    raise StorageError('vault enumeration cannot follow links')
                directory.check()
                if native_mode:
                    info = directory._entry_information(self.name, self._entry.attributes)
                else:
                    info = os.stat(self.name, dir_fd=directory.fd, follow_symlinks=False)
                directory.check()
                return info
        primary = None
        try:
            while True:
                self.check()
                try:
                    entry = next(iterator)
                except StopIteration:
                    break
                self.check()
                yield Entry(entry)
            self.check()
        except BaseException as error:
            primary = error
            raise
        finally:
            # A foreign resume cannot close the owner's live iterator. The
            # owning directory still retains and drains it on its own close.
            if (self._pid, self._thread) == (os.getpid(), threading.get_ident()):
                if iterator in self._iterators:
                    self._iterators.remove(iterator)
                    try:
                        # generator.close() suppresses GeneratorExit. A cleanup
                        # failure must instead escape and remain on this owner.
                        preserved = None if isinstance(primary, GeneratorExit) else primary
                        if not _drain_owned(self, [iterator.close], preserved):
                            self._cleanup_unconfirmed = True
                    except BaseException:
                        self._cleanup_unconfirmed = True
                        raise

    @_records_cleanup
    def _open(self, name, flags, mode=0o600):
        _address('vault', name)
        self.check()
        if self.win is not None:
            self._case_spelling(name)
            fd = self.win.open_fd(name, flags)
        else:
            fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode, dir_fd=self.fd)
        try:
            _file_identity(_file_information(fd))
            self.check()
            if self.win is not None:
                self._case_spelling(name)
            return fd
        except BaseException as primary:
            owned, fd = fd, None
            _drain_owned(self, [lambda: os.close(owned)], primary)
            normalized = _acquired_error(primary)
            if normalized is not primary:
                raise normalized from primary
            raise

    @_records_cleanup
    def _case_spelling(self, name):
        """Refuse case-insensitive NT lookup aliases without renaming old keys."""
        self.check()
        if self.win is None:
            return
        count = 0
        entries, primary = self.entries(), None
        try:
            for entry in entries:
                count += 1
                if count > MAX_KEYS:
                    raise StorageError('vault directory exceeds spelling-check bound')
                if entry.name.casefold() == name.casefold() and entry.name != name:
                    raise StorageError('vault address spelling differs from the stored key')
        except BaseException as error:
            primary = error
            raise
        finally:
            _drain_owned(self, [entries.close], primary)
        self.check()

    @_records_cleanup
    def read_with_identity(self, name, cap):
        _cap(cap)
        fd = self._open(name, os.O_RDONLY)
        primary = None
        try:
            before = _file_information(fd)
            identity = _file_identity(before)
            if not 0 <= before.st_size <= cap:
                raise StorageError('vault ciphertext exceeds its read bound')
            remaining, data = before.st_size + 1, bytearray()
            while remaining:
                chunk = os.read(fd, min(remaining, 1024 * 1024))
                if not chunk:
                    break
                data.extend(chunk)
                remaining -= len(chunk)
            if len(data) != before.st_size or _file_identity(_file_information(fd)) != identity:
                raise StorageError('vault ciphertext changed during read')
            if self.win is None:
                if _file_identity(os.stat(name, dir_fd=self.fd, follow_symlinks=False)) != identity:
                    raise StorageError('vault ciphertext identity changed during read')
            self.check()
            return bytes(data), {'device': before.st_dev, 'inode': before.st_ino,
                                 'mtime_ns': before.st_mtime_ns}
        except BaseException as error:
            primary = _acquired_error(error)
            if primary is not error:
                raise primary from error
            raise
        finally:
            owned, fd = fd, None
            _drain_owned(self, [lambda: os.close(owned)], primary)

    @_records_cleanup
    def _write(self, fd, data):
        self.check()
        view, offset = memoryview(data), 0
        while offset < len(view):
            count = os.write(fd, view[offset:offset + 1024 * 1024])
            if count <= 0:
                raise StorageError('vault ciphertext write did not advance')
            offset += count
        os.fsync(fd)
        info = _file_information(fd)
        _file_identity(info)
        if info.st_size != len(data):
            raise StorageError('vault ciphertext size changed during publication')
        self.check()

    @_records_cleanup
    def write_new(self, name, data):
        fd = self._open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        primary = None
        try:
            self._write(fd, data)
        except BaseException as error:
            primary = error
            raise
        finally:
            owned, fd = fd, None
            _drain_owned(self, [lambda: os.close(owned)], primary)
        self.sync()

    @_records_cleanup
    def publish(self, name, data):
        _address('vault', name)
        staging = 'pending-' + uuid.uuid4().hex + '.json'
        if self.win is None:
            self.write_new(staging, data)
            try:
                fd = self._open(name, os.O_RDONLY)
            except FileNotFoundError as error:
                if native_cleanup.unknown(error):
                    raise
            else:
                owned, fd = fd, None
                _drain_owned(self, [lambda: os.close(owned)])
            self.check()
            os.replace(staging, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            self.sync()
            return
        from . import vault_windows as native
        from .atomicio import _REPLACE_DELAYS
        import msvcrt
        self.check()
        self._case_spelling(name)
        handle = native.open_relative(self.win.handle, staging, create=True,
                                      exclusive=True, write=True, delete=True)
        fd = native.to_fd(handle, os.O_RDWR)
        handle = None  # Ownership transferred to the CRT descriptor.
        primary = None
        try:
            self._write(fd, data)
            self.check()
            self._case_spelling(name)
            try:
                previous = native.open_relative(self.win.handle, name)
            except FileNotFoundError as error:
                if native_cleanup.unknown(error):
                    raise
            else:
                owned_previous, previous = previous, None
                _drain_owned(self, [lambda: native.close(owned_previous)])
            for attempt in range(len(_REPLACE_DELAYS) + 1):
                self.check()
                try:
                    native.rename_same_directory(msvcrt.get_osfhandle(fd), name)
                    break
                except PermissionError as exc:
                    if (getattr(exc, 'winerror', None) not in (5, 32, 33)
                            or attempt == len(_REPLACE_DELAYS)):
                        raise
                    time.sleep(_REPLACE_DELAYS[attempt])
            self.check()
        except BaseException as error:
            primary = error
            raise
        finally:
            owned, fd = fd, None
            _drain_owned(self, [lambda: os.close(owned)], primary)
        self.sync()

    @_records_cleanup
    def confirm(self, name):
        fd = self._open(name, os.O_RDWR if os.name == 'nt' else os.O_RDONLY)
        primary = None
        try:
            os.fsync(fd)
            self.check()
        except BaseException as error:
            primary = error
            raise
        finally:
            owned, fd = fd, None
            _drain_owned(self, [lambda: os.close(owned)], primary)
        self.sync()

    @_records_cleanup
    def _entry_information(self, name, attributes):
        from .vault_windows import entry_information
        return entry_information(self.win, name, attributes)

    @_records_cleanup
    def sync(self):
        self.check()
        if self.fd is not None:
            os.fsync(self.fd)
        self.check()


    @_records_cleanup
    def delete(self, name):
        _address('vault', name)
        directory = self
        # Require a safe ordinary destination even for deletion. POSIX uses
        # the held parent fd; Windows marks the already-open handle deleted.
        if directory.win is None:
            try:
                fd = directory._open(name, os.O_RDONLY)
            except FileNotFoundError as error:
                if native_cleanup.unknown(error):
                    raise
                directory.check()
                return
            owned, fd = fd, None
            _drain_owned(directory, [lambda: os.close(owned)])
            directory.check()
            os.unlink(name, dir_fd=directory.fd)
            directory.sync()
        else:
            from . import vault_windows as native
            import ctypes as C
            directory.check()
            directory._case_spelling(name)
            try:
                handle = native.open_relative(directory.win.handles[-1], name, delete=True)
            except FileNotFoundError as error:
                if native_cleanup.unknown(error):
                    raise
                directory.check()
                return
            primary = None
            try:
                directory.check()
                directory._case_spelling(name)
                native.information(handle, False)
                _, nt = native._apis()
                status = native.IoStatus()
                delete = C.c_ubyte(1)
                result = nt.NtSetInformationFile(handle, C.byref(status),
                                                C.byref(delete), 1, 13)
                if result < 0:
                    native._error(result)
            except BaseException as error:
                primary = error
                raise
            finally:
                owned, handle = handle, None
                _drain_owned(directory, [lambda: native.close(owned)], primary)
            directory.check()


# Shared consumers use the same qualified native owner and sticky cleanup rules.
_Directory = _CapturedDirectory


def _windows_lock(fd, acquire):
    import ctypes as C
    import msvcrt
    class Overlapped(C.Structure):
        _fields_ = [('Internal', C.c_size_t), ('InternalHigh', C.c_size_t),
                    ('Offset', C.c_uint32), ('OffsetHigh', C.c_uint32),
                    ('hEvent', C.c_void_p)]
    kernel = C.WinDLL('kernel32', use_last_error=True)
    handle = C.c_void_p(msvcrt.get_osfhandle(fd))
    overlap = Overlapped()
    if acquire:
        fn = kernel.LockFileEx
        fn.argtypes = [C.c_void_p, C.c_uint32, C.c_uint32, C.c_uint32,
                       C.c_uint32, C.POINTER(Overlapped)]
        fn.restype = C.c_int
        ok = fn(handle, 0x1 | 0x2, 0, 1, 0, C.byref(overlap))
    else:
        fn = kernel.UnlockFileEx
        fn.argtypes = [C.c_void_p, C.c_uint32, C.c_uint32, C.c_uint32,
                       C.POINTER(Overlapped)]
        fn.restype = C.c_int
        ok = fn(handle, 0, 1, 0, C.byref(overlap))
    if not ok:
        code = C.get_last_error()
        if acquire and code == 33:  # ERROR_LOCK_VIOLATION, genuine contention
            return False
        raise OSError(code, 'native vault lock operation failed')
    return True


def _try_lock(fd):
    if os.name == 'nt':
        return _windows_lock(fd, True)
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in (errno.EAGAIN, errno.EACCES):
            return False
        raise


def _unlock(fd):
    if os.name == 'nt':
        _windows_lock(fd, False)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


def _directory_identity(directory):
    directory.check()
    if directory.win is not None:
        from .gallery_windows import information
        info = information(directory.win.handle, True)
    else:
        info = os.fstat(directory.fd)
    directory.check()
    return info.st_dev, info.st_ino


def _validate_root_identity(identity):
    if identity is not None and (type(identity) is not tuple or len(identity) != 2
            or any(type(value) is not int or value < 0 for value in identity)):
        raise StorageError('invalid captured vault root identity')


class FileTransaction:
    @_records_cleanup
    def __init__(self, root, *, expected_identity=None, expected_root_identity=None):
        from .vault_native import Directory
        self.root = root
        self._lease = _Lease()
        self._closed = False
        self._cleanup_unconfirmed = False
        self.directories = []
        self.spaces = {}
        self.fd = None
        self.held = False
        try:
            _validate_root_identity(expected_root_identity)
            if expected_identity is not None and (type(expected_identity) is not tuple
                                                  or len(expected_identity) != 4):
                raise StorageError('invalid captured vault storage identity')
            create = expected_identity is None
            base = Directory(root, create=create and expected_root_identity is None)
            self.directories.append(base)
            if expected_root_identity is not None and _directory_identity(base) != expected_root_identity:
                raise StorageError('captured vault root was replaced')
            if expected_identity is not None and _directory_identity(base) != expected_identity[0]:
                raise StorageError('captured vault root was replaced')
            self.container = base.child('store', create=create)
            self.directories.append(self.container)
            if expected_identity is not None:
                if _directory_identity(self.container) != expected_identity[1]:
                    raise StorageError('captured vault container was replaced')
                directory = self.container.child('vault', create=False)
                self.directories.append(directory)
                self.spaces['vault'] = directory
                if _directory_identity(directory) != expected_identity[2]:
                    raise StorageError('captured vault namespace was replaced')
            self.fd = self.container._open('vault.transaction.lock',
                                           os.O_RDWR | (os.O_CREAT if create else 0))
            self._lock_identity = _file_identity(_file_information(self.fd))[:2]
            if expected_identity is not None and self._lock_identity != expected_identity[3]:
                raise StorageError('captured vault lock was replaced')
        except BaseException as primary:
            _drain_owned(self, [self.close], primary)
            raise

    def _assert_active(self):
        self._lease.owner()
        if self._cleanup_unconfirmed:
            raise _uncertain_owner()
        self._lease.check()
        current = getattr(_LOCAL, 'current', None)
        if (self._closed or not self.held or self.fd is None
                or current is None or current[1] is not self):
            raise StorageError('vault transaction is not current')
        self._check_lock()

    @_records_cleanup
    def _check_lock(self):
        self.container.check()
        if _file_identity(_file_information(self.fd))[:2] != self._lock_identity:
            raise StorageError('vault lock identity changed')
        if self.container.win is None:
            entry = os.stat('vault.transaction.lock', dir_fd=self.container.fd,
                            follow_symlinks=False)
            if _file_identity(entry)[:2] != self._lock_identity:
                raise StorageError('vault lock path identity changed')

    @_records_cleanup
    def authority_identity(self):
        """Fresh identity of held root/store/vault directories and lock inode."""
        self._assert_active()
        directories = (self.directories[0], self.container, self.space('vault'))
        identities = tuple(_directory_identity(directory) for directory in directories)
        self._check_lock()
        return identities + (self._lock_identity,)

    @_records_cleanup
    def space(self, namespace):
        self._assert_active()
        _address(namespace, 'enumeration')
        if namespace not in self.spaces:
            directory = self.container.child(namespace, create=True)
            self.spaces[namespace] = directory
            self.directories.append(directory)
        return self.spaces[namespace]

    @_records_cleanup
    def get(self, namespace, key, cap=MAX_BLOB):
        _address(namespace, key)
        _cap(cap)
        directory = self.space(namespace)
        try:
            return directory.read(key, cap)
        except FileNotFoundError as error:
            if native_cleanup.unknown(error):
                raise
            # Only an absent final blob is absence; a changed pinned parent
            # still fails its independent identity check.
            directory.check()
            return None

    @_records_cleanup
    def put(self, namespace, key, data):
        _address(namespace, key)
        if type(data) is not bytes or len(data) > MAX_BLOB:
            raise StorageError('invalid vault ciphertext size')
        self.space(namespace).publish(key, data)
        if self.get(namespace, key, len(data)) != data:
            raise StorageError('vault publication verification failed')

    @_records_cleanup
    def write_new(self, namespace, key, data):
        _address(namespace, key)
        if type(data) is not bytes or len(data) > MAX_BLOB:
            raise StorageError('invalid vault recovery size')
        self.space(namespace).write_new(key, data)
        if self.get(namespace, key, len(data)) != data:
            raise StorageError('vault recovery verification failed')

    @_records_cleanup
    def keys(self, namespace, cap=MAX_KEYS):
        _cap(cap, MAX_KEYS)
        result = []
        directory = self.space(namespace)
        entries, primary = directory.entries(), None
        try:
            for entry in entries:
                if len(result) >= cap:
                    raise StorageError('vault namespace exceeds enumeration bound')
                _address(namespace, entry.name)
                # Enumeration must not silently turn an unsafe object into a row.
                _file_identity(entry.stat(follow_symlinks=False))
                result.append(entry.name)
        except BaseException as error:
            primary = error
            raise
        finally:
            _drain_owned(self, [entries.close], primary)
        return sorted(result)

    @_records_cleanup
    def delete(self, namespace, key):
        _address(namespace, key)
        self.space(namespace).delete(key)
        if self.get(namespace, key, MAX_BLOB) is not None:
            raise StorageError('vault deletion verification failed')

    def close(self):
        # Detach every resource before attempting cleanup. Unlock, descriptor
        # close and all directory closes remain independent single attempts.
        self._lease.owner()
        if self._closed:
            if self._cleanup_unconfirmed:
                raise _uncertain_owner()
            return
        self._closed = True
        self._lease.active = False
        fd, held = self.fd, self.held
        self.fd, self.held = None, False
        directories, self.directories = self.directories, []
        actions = []
        if fd is not None:
            if held:
                actions.append(lambda: _unlock(fd))
            actions.append(lambda: os.close(fd))
        actions.extend(directory.close for directory in reversed(directories))
        _drain_owned(self, actions)
        if self._cleanup_unconfirmed:
            raise _uncertain_owner()


@contextmanager
def transaction(backend, root, *, timeout=LOCK_TIMEOUT, expected_identity=None, expected_root_identity=None):
    """Capture backend/root once; nested cross-root/backend access is refused."""
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
        raise StorageError('invalid vault transaction timeout')
    _validate_root_identity(expected_root_identity)
    captured = Path(os.path.abspath(root))
    identity = (id(backend), os.path.normcase(str(captured)))
    current = getattr(_LOCAL, 'current', None)
    if current is not None:
        current[1]._assert_active()
        if current[0] != identity:
            raise StorageError('nested vault transaction changed its authority')
        if expected_root_identity is not None and (type(current[1]) is not FileTransaction
                or _directory_identity(current[1].directories[0]) != expected_root_identity):
            raise StorageError('nested vault transaction changed its root identity')
        if expected_identity is not None and (type(current[1]) is not FileTransaction
                or current[1].authority_identity() != expected_identity):
            raise StorageError('nested vault transaction changed its storage identity')
        yield current[1]
        return
    deadline = time.monotonic() + timeout
    if not _GATE.acquire(timeout=timeout):
        raise StorageError('vault transaction acquisition timed out')
    outer_primary = None
    try:
        if type(backend) is store.FileStore:
            tx, primary = None, None
            try:
                if expected_root_identity is None:
                    tx = (FileTransaction(captured) if expected_identity is None else
                          FileTransaction(captured, expected_identity=expected_identity))
                else:
                    tx = FileTransaction(captured, expected_identity=expected_identity,
                                         expected_root_identity=expected_root_identity)
                while not _try_lock(tx.fd):
                    if time.monotonic() >= deadline:
                        raise StorageError('vault transaction acquisition timed out')
                    time.sleep(min(0.02, max(0, deadline - time.monotonic())))
                tx.held = True
                tx._check_lock()
                _LOCAL.current = (identity, tx)
                tx._lease.active = True
                try:
                    yield tx
                finally:
                    tx._lease.active = False
                    _LOCAL.current = None
            except BaseException as error:
                primary = error
                raise
            finally:
                if tx is not None:
                    _drain_owned(tx, [tx.close], primary)
        elif type(backend) is store.PostgresStore:
            if expected_identity is not None or expected_root_identity is not None:
                raise StorageError('filesystem identity cannot authorize a database transaction')
            with backend.vault_transaction(timeout=max(0, deadline - time.monotonic())) as tx:
                _LOCAL.current = (identity, tx)
                try:
                    yield tx
                finally:
                    _LOCAL.current = None
        else:
            raise StorageError('backend does not provide a verified vault transaction')
    except BaseException as exc:
        outer_primary = exc
        if isinstance(exc, (OSError, ValueError)):
            error = StorageError('vault storage is unavailable; retained evidence was not removed')
            if native_cleanup.unknown(exc):
                native_cleanup.mark(error)
            outer_primary = error
            raise error from exc
        raise
    finally:
        native_cleanup.drain([_GATE.release], outer_primary)

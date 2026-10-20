"""Private native directory ownership for preference transactions only.

Cleanup uncertainty is part of this adapter's contract. It is deliberately
separate from older vault/archive callers that interpret ordinary absence.
"""
import os
from pathlib import Path
import stat
import sys
import threading
import time
import uuid

from . import native_cleanup
from .gallery_state import Directory as _GalleryDirectory
from .vault_storage import (
    MAX_KEYS, StorageError, _address, _cap, _directory_identity,
    _file_identity, _file_information, _try_lock, _unlock,
)


def _rename_exclusive(directory_fd, source, destination):
    # Linux is the qualified POSIX platform. Refuse where an atomic no-replace
    # relative rename is unavailable; never emulate it with unlink or replace.
    if not sys.platform.startswith('linux'):
        raise StorageError('exclusive preference retirement is unavailable')
    import ctypes as C
    try:
        rename = C.CDLL(None, use_errno=True).renameat2
    except AttributeError:
        raise StorageError('exclusive preference retirement is unavailable') from None
    rename.argtypes = [C.c_int, C.c_char_p, C.c_int, C.c_char_p, C.c_uint]
    rename.restype = C.c_int
    result = rename(directory_fd, os.fsencode(source), directory_fd,
                    os.fsencode(destination), 1)  # RENAME_NOREPLACE
    if result != 0:
        raise OSError(C.get_errno(), 'exclusive preference retirement failed')


class _Directory(_GalleryDirectory):
    """Vault-local acquisition, lifetime, fresh reads and complete cleanup.

    Native lookup/rename primitives remain M07's. No claim is made that share
    flags prevent attribute mutation; every operation rechecks held ancestors.
    POSIX retains ancestor descriptors as well as the final directory so a
    changed path entry is refused before another vault mutation.
    """
    def __init__(self, path, create=False):
        self._initialize(Path(os.path.abspath(path)))
        try:
            if os.name == 'nt':
                from . import prefs_windows as native
                self.win = object.__new__(native.NativeDirectory)
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
            native_cleanup.drain([self.close], primary)
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

    def child(self, name, create=False):
        _address('vault', name)
        self.check()
        child = object.__new__(_Directory)
        child._initialize(self.path / name)
        try:
            if self.win is not None:
                from . import prefs_windows as native
                child.win = object.__new__(native.NativeDirectory)
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
            native_cleanup.drain([child.close], primary)
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
            from .prefs_windows import close
            handles = self.win.handles[self.win.owned_start:]
            self.win.handles = []
            self.win = None
        else:
            close, handles = os.close, self._parents[self._owned_start:]
        self.fd, self._parents = None, []
        actions.extend(lambda handle=handle: close(handle) for handle in reversed(handles))
        try:
            native_cleanup.drain(actions)
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
                    from .prefs_windows import entry_information
                    info = entry_information(directory.win, self.name, self._entry.attributes)
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
                        if not native_cleanup.drain([iterator.close], preserved):
                            self._cleanup_unconfirmed = True
                    except BaseException:
                        self._cleanup_unconfirmed = True
                        raise

    def _open(self, name, flags, mode=0o600):
        _address('vault', name)
        # _case_spelling begins with the same fresh owner/ancestor check.
        # On POSIX it performs only that check; Windows also scans spelling.
        self._case_spelling(name)
        if self.win is not None:
            fd = self.win.open_fd(name, flags)
        else:
            fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode, dir_fd=self.fd)
        try:
            _file_identity(_file_information(fd))
            self._case_spelling(name)
            return fd
        except BaseException as primary:
            owned, fd = fd, None
            native_cleanup.drain([lambda: os.close(owned)], primary)
            raise

    def _case_spelling(self, name):
        """Scan exact spelling privately; no authority survives this call.

        Unlike entries(), this scan never yields directory records to callers.
        The native iterator checks held ancestors before every OS batch read;
        validate the preference owner again after consuming its bounded records.
        Public entries() retains its checks around every externally visible yield.
        """
        self.check()
        if self.win is None:
            return
        if len(self._iterators) >= 16:
            raise StorageError('too many live vault enumerations')
        count = 0
        folded = name.casefold()
        entries, primary = self.win.entries(), None
        self._iterators.append(entries)
        try:
            for entry in entries:
                count += 1
                if count > MAX_KEYS:
                    raise StorageError('vault directory exceeds spelling-check bound')
                if entry.name.casefold() == folded and entry.name != name:
                    raise StorageError('vault address spelling differs from the stored key')
        except BaseException as error:
            primary = error
            raise
        finally:
            # An owner close may already have detached and attempted this
            # iterator. Do not retry a possibly completed close after lost ack.
            actions = []
            if entries in self._iterators:
                self._iterators.remove(entries)
                actions.append(entries.close)
            # Retain incoming uncertainty even when this scan's close succeeds.
            native_cleanup.drain_owned(self, actions, primary)
        self.check()

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
            primary = error
            raise
        finally:
            owned, fd = fd, None
            native_cleanup.drain([lambda: os.close(owned)], primary)

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
            native_cleanup.drain([lambda: os.close(owned)], primary)
        self.sync()

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
                native_cleanup.drain([lambda: os.close(owned)])
            self.check()
            os.replace(staging, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            self.sync()
            return
        from . import prefs_windows as native
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
                native_cleanup.drain([lambda: native.close(owned_previous)])
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
            native_cleanup.drain([lambda: os.close(owned)], primary)
        self.sync()

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
            native_cleanup.drain([lambda: os.close(owned)], primary)
        self.sync()

    def retire(self, name, destination, expected_identity):
        _address('vault', name)
        _address('vault', destination)
        if name == destination or type(expected_identity) is not tuple or len(expected_identity) != 5:
            raise StorageError('invalid captured preference retirement')
        self.check()
        if self.win is not None:
            from . import prefs_windows as native
            import msvcrt
            self._case_spelling(name)
            self._case_spelling(destination)
            handle = native.open_relative(self.win.handle, name, write=True, delete=True)
            fd = native.to_fd(handle, os.O_RDWR)
            handle = None
        else:
            fd = self._open(name, os.O_RDONLY)
        primary = None
        try:
            if _file_identity(_file_information(fd)) != expected_identity:
                raise StorageError('preference retirement source changed')
            os.fsync(fd)
            self.check()
            if self.win is not None:
                self._case_spelling(name)
                if _file_identity(_file_information(fd)) != expected_identity:
                    raise StorageError('preference retirement source changed')
                native.rename_exclusive(msvcrt.get_osfhandle(fd), destination)
            else:
                # NOREPLACE protects the destination. These fresh observations
                # do not make expected-inode rename atomic against external
                # writers that do not hold the preference mutex.
                observed = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
                if (_file_identity(observed) != expected_identity
                        or _file_identity(_file_information(fd)) != expected_identity):
                    raise StorageError('preference retirement source changed')
                _rename_exclusive(self.fd, name, destination)
            self.check()
            os.fsync(fd)
        except BaseException as error:
            primary = error
            raise
        finally:
            owned, fd = fd, None
            native_cleanup.drain([lambda: os.close(owned)], primary)
        self.sync()

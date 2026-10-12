"""Private native preference coordination within one captured memory root.

Every cooperating preference mutation must use this boundary. It does not
coordinate old binaries or direct external writers and does not make multiple
publications/deletions crash-atomic. Preference paths and policy interpretation
belong to prefs; this module handles only native authority and bounded bytes.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import math
import os
import stat
import threading
import time

from . import archive_io, native_cleanup, prefs_storage as storage

_LOCK_NAME = 'preferences.native.lock'
_TIMEOUT = 5.0
_MAX_BYTES = 1024 * 1024
_GATE = threading.RLock()
_LOCAL = threading.local()
_CLEANUP = '_preference_cleanup_unconfirmed'
_MAX_DIRECTORIES = 64
_MAX_LEAVES = 256
_IN_FLIGHT = False
_POISONED = False


def _after_fork():
    global _GATE, _LOCAL, _POISONED
    # Process-wide state also catches a fork performed by a different thread.
    # Never unlock inherited flock descriptions: that releases the parent's
    # lock too. Such children must promptly exec/exit; this is not a descriptor
    # recovery protocol if the parent dies while the child remains alive.
    _POISONED = _POISONED or _IN_FLIGHT
    _GATE, _LOCAL = threading.RLock(), threading.local()


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_after_fork)


class PreferenceStorageError(RuntimeError):
    pass


def _process_available():
    if _POISONED:
        raise PreferenceStorageError('preference authority requires process reconciliation')


def require_outside_transaction():
    """Reject callbacks that would enter another subsystem while coordinated."""
    _process_available()
    if getattr(_LOCAL, 'current', None) is not None:
        raise PreferenceStorageError('backup collection cannot nest in a preference transaction')


def coordination_path():
    """The one preference mutex address; no filesystem access."""
    return ('locks', _LOCK_NAME)


def cleanup_unknown(error):
    attributes = BaseException.__getattribute__(error, '__dict__')
    return type(attributes) is dict and attributes.get(_CLEANUP) is True


def _mark_cleanup(error):
    attributes = BaseException.__getattribute__(error, '__dict__')
    if type(attributes) is dict:
        attributes[_CLEANUP] = True


def _delegated(function, *args, **kwargs):
    global _POISONED
    try:
        return function(*args, **kwargs)
    except BaseException as error:
        if native_cleanup.unknown(error):
            _POISONED = True
            _mark_cleanup(error)
        raise


def _drain(actions, primary=None):
    global _POISONED
    failed = None
    for action in actions:
        try:
            action()
        except BaseException as error:
            if failed is None:
                failed = error
    if failed is not None:
        _POISONED = True
        if primary is not None:
            _mark_cleanup(primary)
        else:
            _mark_cleanup(failed)
            raise failed
    return failed is None


@dataclass(frozen=True, repr=False)
class Binding:
    root: object
    root_identity: tuple
    locks_identity: tuple
    lock_identity: tuple
    pid: int
    thread: int

    def check_executor(self):
        if (self.pid, self.thread) != (os.getpid(), threading.get_ident()):
            raise PreferenceStorageError('captured preference authority requires its original executor')


class Transaction:
    def __init__(self, root, binding=None, *, root_identity=None):
        self.root = root
        self.pid, self.thread = os.getpid(), threading.get_ident()
        self.closed, self.held = False, False
        self.cleanup_unconfirmed = False
        self.observed_leaves = {}
        self.fd = None
        self.directories, self.children = [], {}
        try:
            create = binding is None
            self.directory = _delegated(storage._Directory, root, create=create and root_identity is None)
            self.directories.append(self.directory)
            self.children[()] = self.directory
            observed_root = storage._directory_identity(self.directory)
            if (binding is not None and binding.root_identity != observed_root
                    or root_identity is not None and root_identity != observed_root):
                raise PreferenceStorageError('captured preference root was replaced')
            self.locks = _delegated(self.directory.child, 'locks', create=create)
            self.directories.append(self.locks)
            locks_identity = storage._directory_identity(self.locks)
            if binding is not None and binding.locks_identity != locks_identity:
                raise PreferenceStorageError('captured preference coordination was replaced')
            self.fd = _delegated(self.locks._open, _LOCK_NAME, os.O_RDWR | (os.O_CREAT if create else 0))
            lock_identity = storage._file_identity(storage._file_information(self.fd))[:2]
            if binding is not None and binding.lock_identity != lock_identity:
                raise PreferenceStorageError('captured preference lock was replaced')
            self.binding = Binding(root, observed_root, locks_identity, lock_identity, self.pid, self.thread)
        except BaseException as primary:
            _drain([self.close], primary)
            raise

    def _executor(self):
        if (self.pid, self.thread) != (os.getpid(), threading.get_ident()):
            raise PreferenceStorageError('preference transaction belongs to another executor')

    def check(self):
        _process_available()
        self._executor()
        if self.closed or not self.held or self.fd is None or getattr(_LOCAL, 'current', None) is not self:
            raise PreferenceStorageError('preference transaction is not current')
        for directory in self.directories:
            directory.check()
        if (storage._directory_identity(self.directory) != self.binding.root_identity
                or storage._directory_identity(self.locks) != self.binding.locks_identity
                or storage._file_identity(storage._file_information(self.fd))[:2] != self.binding.lock_identity):
            raise PreferenceStorageError('preference authority identity changed')
        # Reopen only the persistent lock name under its held parent. This
        # detects pathname replacement on POSIX and exact spelling on Windows.
        observed = _delegated(self.locks._open, _LOCK_NAME, os.O_RDWR)
        primary = None
        try:
            if storage._file_identity(storage._file_information(observed))[:2] != self.binding.lock_identity:
                raise PreferenceStorageError('preference lock pathname changed')
        except BaseException as error:
            primary = error
            raise
        finally:
            _drain([lambda: os.close(observed)], primary)

    @staticmethod
    def _address(parts):
        if type(parts) is not tuple or not 1 <= len(parts) <= 8:
            raise PreferenceStorageError('invalid preference relative address')
        for part in parts:
            archive_io._filename(part)  # DOS names, spelling aliases, portable bounds.
            storage._address('vault', part)  # Exact storage alphabet/length too.
        if parts[0].casefold() == 'locks':
            raise PreferenceStorageError('preference data cannot address coordination state')

    def _capacity(self, addresses):
        leaves = set(addresses) | set(self.observed_leaves)
        prefixes = {parts[:index] for parts in leaves for index in range(1, len(parts))}
        missing = prefixes - set(self.children)
        if len(leaves) > _MAX_LEAVES or len(self.directories) + len(missing) > _MAX_DIRECTORIES:
            raise PreferenceStorageError('preference planned authority exceeds its bound')

    def _admit(self, parts):
        self._address(parts)
        self.check()
        self._capacity((parts,))
        if parts not in self.observed_leaves:
            self.observed_leaves[parts] = False

    def _parent(self, parts, *, create=False):
        self._address(parts)  # Validate the complete path before creating anything.
        self.check()
        self._capacity((parts,))
        return self._parent_after_admission(parts, create=create)

    def _parent_after_admission(self, parts, *, create=False):
        """Descend immediately after this call stack checked admission.

        Only _parent, read and inventory call this private helper. Each
        validates the exact address, checks the full transaction and
        accounts for every planned prefix before reaching this point.
        No caller control or native I/O intervenes in that handoff.
        This retains no validation state across operations; direct
        parent requests must use _parent and its fresh check.
        """
        current = self.directory
        for index, part in enumerate(parts[:-1]):
            prefix = parts[:index + 1]
            if prefix not in self.children:
                if len(self.directories) >= _MAX_DIRECTORIES:
                    raise PreferenceStorageError('preference directory authority exceeds its bound')
                child = _delegated(current.child, part, create=create)
                self.directories.append(child)
                self.children[prefix] = child
            current = self.children[prefix]
            current.check()
        return current, parts[-1]

    def reserve(self, addresses):
        """Admit all planned leaves and directory prefixes before any effect."""
        if type(addresses) is not tuple or len(addresses) > _MAX_LEAVES:
            raise PreferenceStorageError('invalid preference address reservation')
        for parts in addresses:
            self._address(parts)
        self._capacity(addresses)
        self.check()
        for parts in addresses:
            self.observed_leaves.setdefault(parts, False)

    def read(self, parts, *, max_bytes=_MAX_BYTES):
        if type(max_bytes) is not int or not 0 <= max_bytes <= _MAX_BYTES:
            raise PreferenceStorageError('invalid preference read bound')
        self._admit(parts)
        try:
            directory, name = self._parent_after_admission(parts)
        except FileNotFoundError as error:
            if cleanup_unknown(error):
                raise
            self.check()  # Every previously retained ancestor, not just root/locks.
            if self.observed_leaves.get(parts):
                raise PreferenceStorageError('observed preference authority disappeared') from None
            self.observed_leaves[parts] = False
            return None, None
        try:
            fd = _delegated(directory._open, name, os.O_RDONLY)
        except FileNotFoundError as error:
            if cleanup_unknown(error):
                raise
            self.check()
            if self.observed_leaves.get(parts):
                raise PreferenceStorageError('observed preference record disappeared') from None
            self.observed_leaves[parts] = False
            return None, None
        # From this point the record exists. A subsequent disappearance is an
        # authority failure, never initial absence. Read the same descriptor.
        self.observed_leaves[parts] = True
        primary = None
        try:
            before = storage._file_information(fd)
            identity = storage._file_identity(before)
            if not 0 <= before.st_size <= max_bytes:
                raise PreferenceStorageError('preference record exceeds its read bound')
            remaining, data = before.st_size + 1, bytearray()
            while remaining:
                chunk = os.read(fd, min(remaining, 1024 * 1024))
                if not chunk:
                    break
                data.extend(chunk)
                remaining -= len(chunk)
            if len(data) != before.st_size or storage._file_identity(storage._file_information(fd)) != identity:
                raise PreferenceStorageError('preference record changed during read')
            if directory.win is None:
                observed = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
                if storage._file_identity(observed) != identity:
                    raise PreferenceStorageError('preference record identity changed during read')
            else:
                _delegated(directory._case_spelling, name)
            self.check()
            return bytes(data), identity
        except FileNotFoundError as error:
            if cleanup_unknown(error):
                primary = error
                raise
            primary = PreferenceStorageError('acquired preference evidence disappeared during read')
            raise primary from None
        except BaseException as error:
            primary = error
            raise
        finally:
            _drain([lambda: os.close(fd)], primary)

    def inventory(self, parts, *, limit):
        """Observe a bounded immediate directory through held native authority.

        Missing before acquisition is distinct from an acquired/listed entry
        disappearing. The result is detached names/kinds/identities only;
        callers must still open each selected child through this transaction.
        """
        if (type(parts) is not tuple or not 0 <= len(parts) <= 7
                or type(limit) is not int or not 0 <= limit <= 64):
            raise PreferenceStorageError('invalid preference inventory bound')
        probe = parts + ('.inventory-probe',)
        self._admit(probe)  # Missing/listing probes count against the leaf cap.
        try:
            directory, _unused = self._parent_after_admission(probe)
        except FileNotFoundError as error:
            if cleanup_unknown(error):
                raise
            self.check()
            if self.observed_leaves.get(probe):
                raise PreferenceStorageError('observed preference directory disappeared') from None
            return None
        self.observed_leaves[probe] = True
        identity = storage._directory_identity(directory)
        iterator = _delegated(directory.entries)
        primary = None
        entries, names = [], set()
        try:
            while True:
                try:
                    entry = _delegated(next, iterator)
                except StopIteration:
                    break
                if len(entries) >= limit:
                    raise PreferenceStorageError('preference directory inventory exceeds its bound')
                # Listing root may observe the coordination directory. Do not
                # grant a data address into it: read/publish/reserve still use
                # _address and refuse every locks-prefixed selection.
                archive_io._filename(entry.name)
                storage._address('vault', entry.name)
                if entry.name.casefold() in names:
                    raise PreferenceStorageError('preference directory has aliased entries')
                names.add(entry.name.casefold())
                info = _delegated(entry.stat, follow_symlinks=False)
                if getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise PreferenceStorageError('preference directory contains a reparse entry')
                if stat.S_ISDIR(info.st_mode):
                    observed = (info.st_dev, info.st_ino)
                    kind = 'directory'
                else:
                    observed = storage._file_identity(info)  # No links/devices/FIFOs.
                    kind = 'file'
                entries.append((entry.name, kind, observed))
            if storage._directory_identity(directory) != identity:
                raise PreferenceStorageError('preference directory identity changed')
            self.check()
            return tuple(sorted(entries))
        except FileNotFoundError as error:
            if cleanup_unknown(error):
                primary = error
                raise
            primary = PreferenceStorageError('listed preference evidence disappeared')
            raise primary from None
        except BaseException as error:
            primary = error
            raise
        finally:
            _drain([iterator.close], primary)

    def publish(self, parts, data):
        self._admit(parts)
        if type(data) is not bytes or len(data) > _MAX_BYTES:
            raise PreferenceStorageError('invalid preference publication size')
        directory, name = self._parent(parts, create=True)
        _delegated(directory.publish, name, data)
        self.check()
        if self.read(parts)[0] != data:
            raise PreferenceStorageError('preference publication is unconfirmed')

    def confirm(self, parts, expected_raw, expected_identity):
        """Reconfirm one observed image through its exact held native file.

        This never republishes or creates a missing record. It is a barrier
        primitive for a retained caller plan, not proof that a caller owns
        otherwise identical bytes. Noncooperating external writers remain
        outside this mutex's exclusion guarantee.
        """
        self._admit(parts)
        if (type(expected_raw) is not bytes or len(expected_raw) > _MAX_BYTES
                or type(expected_identity) is not tuple or len(expected_identity) != 5
                or any(type(item) is not int for item in expected_identity[:4])
                or expected_identity[4] is not None and type(expected_identity[4]) is not int):
            raise PreferenceStorageError('invalid preference confirmation selection')
        if self.read(parts) != (expected_raw, expected_identity):
            raise PreferenceStorageError('preference confirmation evidence changed')
        directory, name = self._parent(parts)
        fd = _delegated(directory._open, name, os.O_RDWR if os.name == 'nt' else os.O_RDONLY)
        primary = None
        try:
            if storage._file_identity(storage._file_information(fd)) != expected_identity:
                raise PreferenceStorageError('preference confirmation file changed')
            if directory.win is None:
                observed = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
                if storage._file_identity(observed) != expected_identity:
                    raise PreferenceStorageError('preference confirmation pathname changed')
            else:
                _delegated(directory._case_spelling, name)
            os.fsync(fd)
            if storage._file_identity(storage._file_information(fd)) != expected_identity:
                raise PreferenceStorageError('preference confirmation image changed')
            self.check()
        except BaseException as error:
            primary = error
            raise
        finally:
            owned, fd = fd, None
            _drain([lambda: os.close(owned)], primary)
        _delegated(directory.sync)
        self.check()
        if self.read(parts) != (expected_raw, expected_identity):
            raise PreferenceStorageError('preference confirmation is unconfirmed')

    def write_new(self, parts, data):
        self._admit(parts)
        if type(data) is not bytes or len(data) > _MAX_BYTES:
            raise PreferenceStorageError('invalid retained preference evidence size')
        directory, name = self._parent(parts, create=True)
        _delegated(directory.write_new, name, data)
        self.check()
        if self.read(parts)[0] != data:
            raise PreferenceStorageError('retained preference evidence is unconfirmed')

    def retire(self, parts, retained_parts, expected_raw, expected_identity):
        """Move an exact source to a new same-parent evidence name, never replace.

        Retained evidence is confirmed again on an idempotent resume. The
        caller must retain an immutable plan binding both names and images.
        This primitive does not provide a multi-file recovery protocol.
        Retry after an unconfirmed rename requires a fresh transaction. Linux
        source identity is observed, not atomically protected against writers
        outside this native mutex; destination replacement is always refused.
        """
        self._admit(parts)
        self._admit(retained_parts)
        if (parts == retained_parts or parts[:-1] != retained_parts[:-1]
                or type(expected_raw) is not bytes or len(expected_raw) > _MAX_BYTES
                or type(expected_identity) is not tuple or len(expected_identity) != 5):
            raise PreferenceStorageError('invalid retained preference selection')
        raw, identity = self.read(parts)
        retained, retained_identity = self.read(retained_parts)
        directory, name = self._parent(parts)
        if raw is None:
            if retained != expected_raw or retained_identity[:2] != expected_identity[:2]:
                raise PreferenceStorageError('retained preference source cannot be reconciled')
            _delegated(directory.confirm, retained_parts[-1])
            confirmed, confirmed_identity = self.read(retained_parts)
            remaining = self.read(parts)[0]
            if (confirmed != expected_raw or confirmed_identity[:2] != expected_identity[:2]
                    or remaining is not None):
                raise PreferenceStorageError('retained preference confirmation changed')
            return False
        if raw != expected_raw or identity != expected_identity or retained is not None:
            raise PreferenceStorageError('preference retirement evidence changed')
        _delegated(directory.retire, name, retained_parts[-1], expected_identity)
        self.observed_leaves[parts] = False  # This operation intentionally retired it.
        self.check()
        remaining = self.read(parts)[0]
        confirmed, confirmed_identity = self.read(retained_parts)
        if remaining is not None or confirmed != expected_raw or confirmed_identity[:2] != expected_identity[:2]:
            raise PreferenceStorageError('preference retirement is unconfirmed')
        return True

    def close(self):
        global _POISONED
        self._executor()
        if self.closed:
            if self.cleanup_unconfirmed:
                error = PreferenceStorageError('preference authority cleanup remains unconfirmed')
                _mark_cleanup(error)
                raise error
            return
        self.closed = True
        fd, held = self.fd, self.held
        self.fd, self.held = None, False
        actions = []
        if fd is not None:
            if held:
                actions.append(lambda: storage._unlock(fd))
            actions.append(lambda: os.close(fd))
        actions.extend(directory.close for directory in reversed(self.directories))
        self.directories = []
        self.children = {}
        self.observed_leaves = {}
        try:
            _drain(actions)
        except BaseException:
            self.cleanup_unconfirmed = True
            _POISONED = True
            raise


@contextmanager
def transaction(root, *, binding=None, root_identity=None, timeout=_TIMEOUT, allow_nested=True):
    global _IN_FLIGHT, _POISONED
    _process_available()  # Before even resolving a selector after an unsafe fork.
    if type(allow_nested) is not bool:
        raise PreferenceStorageError('invalid preference nesting selection')
    if not allow_nested:
        require_outside_transaction()
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= 60:
        raise PreferenceStorageError('invalid preference acquisition deadline')
    if binding is not None:
        if type(binding) is not Binding:
            raise PreferenceStorageError('invalid captured preference binding')
        binding.check_executor()
    if root_identity is not None:
        if (type(root_identity) is not tuple or len(root_identity) != 2
                or any(type(item) is not int for item in root_identity)):
            raise PreferenceStorageError('invalid expected preference root identity')
        if binding is not None and binding.root_identity != root_identity:
            raise PreferenceStorageError('captured preference root identities disagree')
    selected = archive_io.selected_directory(root)
    if binding is not None and binding.root != selected:
        raise PreferenceStorageError('captured preference root changed')
    current = getattr(_LOCAL, 'current', None)
    if current is not None:
        if not allow_nested or not getattr(_LOCAL, 'allow_nested', True):
            raise PreferenceStorageError('preference collection cannot admit nested operations')
        current.check()
        if (current.root != selected or binding is not None and binding != current.binding
                or root_identity is not None and root_identity != current.binding.root_identity):
            raise PreferenceStorageError('nested preference operation changed authority')
        yield current
        current.check()
        return
    deadline = time.monotonic() + timeout
    if not _GATE.acquire(timeout=timeout):
        raise PreferenceStorageError('preference transaction acquisition timed out')
    tx, primary = None, None
    _IN_FLIGHT = True  # Before construction can acquire native descriptors.
    try:
        _process_available()  # A previous executor may have poisoned cleanup.
        tx = (Transaction(selected, binding) if root_identity is None else
              Transaction(selected, binding, root_identity=root_identity))
        while True:
            if timeout and time.monotonic() >= deadline:
                raise PreferenceStorageError('preference transaction acquisition timed out')
            acquired = storage._try_lock(tx.fd)
            if acquired:
                tx.held = True  # A late successful probe still needs unlock.
            if timeout and time.monotonic() >= deadline:
                raise PreferenceStorageError('preference transaction acquisition timed out')
            if acquired:
                break
            if not timeout:  # Zero means one immediate native lock probe.
                raise PreferenceStorageError('preference transaction is busy')
            time.sleep(min(0.02, max(0, deadline - time.monotonic())))
        _LOCAL.current = tx
        _LOCAL.allow_nested = allow_nested
        tx.check()
        if timeout and time.monotonic() >= deadline:
            raise PreferenceStorageError('preference transaction acquisition timed out')
        yield tx
        tx.check()
    except BaseException as error:
        primary = error
        raise
    finally:
        _LOCAL.current = None
        _LOCAL.allow_nested = True
        try:
            try:
                if tx is not None and not _drain([tx.close], primary):
                    _POISONED = True
            except BaseException as error:
                _POISONED = True
                primary = error
                raise
        finally:
            # After confirmed closure or sticky poison, no child can treat
            # this as a reusable captured operation.
            _IN_FLIGHT = False
            try:
                if not _drain([_GATE.release], primary):
                    _POISONED = True
            except BaseException:
                _POISONED = True
                raise

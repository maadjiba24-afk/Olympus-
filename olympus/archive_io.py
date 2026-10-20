"""Bounded archive allocation, held-parent reads and single-file publication.

The admission slot is local to one process. It does not bound arbitrary caller
buffers, independent worker processes or host RSS. Returned bytes are owned by
the caller and can outlive the slot. Publication replaces only its selected
target; it never changes custody or permissions or deletes failed staging.
"""
from __future__ import annotations

from . import native_cleanup
from contextlib import contextmanager
from dataclasses import dataclass, field
import io
import gzip
import os
from pathlib import Path
import stat
import threading
import time
import tarfile
import uuid

from . import vault_codec as codec, vault_custody as custody

MAX_PLAINTEXT = codec.MAX_PLAINTEXT
MAX_CIPHERTEXT = (codec._HEADER.size + codec._fernet_size(
    MAX_PLAINTEXT + len(codec._INNER) + codec._HEADER.size + 2 + codec.MAX_CONTEXT))
_CONTEXTS = {'backup':b'olympus-backup-archive-v1', 'notes':b'olympus-note-archive-v1'}
_GATE = threading.BoundedSemaphore(1)


class ArchiveError(ValueError):
    pass


class ArchivePublicationError(ArchiveError):
    """A failed single-file publication; never a multi-file rollback promise."""
    def __init__(self, rename_started, staging):
        self.publication_uncertain = rename_started
        self.retained_staging_name = staging
        super().__init__('archive publication completion is unconfirmed' if rename_started
                         else 'archive publication failed before replacement')


class BoundedBuffer(io.BytesIO):
    """Refuse a compressor write before it grows the owned buffer past cap."""
    def __init__(self, cap):
        if type(cap) is not int or not 0 <= cap <= 256 * 1024 * 1024:
            raise ArchiveError('invalid archive buffer bound')
        super().__init__()
        self.cap = cap

    def write(self, data):
        if self.tell() + len(data) > self.cap:
            raise ArchiveError('encoded archive bound exceeded')
        return super().write(data)


def encode_tar(rows, *, max_expanded, max_compressed=MAX_PLAINTEXT):
    """Bound both tar metadata/padding and compressor output during creation.

    Callers bound their source inventory and own its byte images. No source
    file is reopened here; manifest hashes and archive payloads use those same
    images. PAX headers count toward the expanded limit before compression.
    """
    if type(max_expanded) is not int or not 0 <= max_expanded <= 256 * 1024 * 1024:
        raise ArchiveError('invalid expanded archive bound')
    output = BoundedBuffer(max_compressed)
    class Writer:
        def __init__(self, stream):
            self.stream, self.count = stream, 0
        def tell(self):
            return self.count
        def write(self, raw):
            if self.count + len(raw) > max_expanded:
                raise ArchiveError('expanded archive bound exceeded')
            result = self.stream.write(raw)
            self.count += result
            return result
    with gzip.GzipFile(fileobj=output, mode='wb', mtime=0) as compressed:
        with tarfile.open(fileobj=Writer(compressed), mode='w') as archive:
            for name, raw in rows:
                if (type(name) is not str or len(name.encode('utf-8')) > 4096
                        or type(raw) is not bytes or len(raw) > max_expanded):
                    raise ArchiveError('invalid archive member')
                item = tarfile.TarInfo(name)
                item.size, item.mtime, item.mode = len(raw), 0, 0o600
                archive.addfile(item, io.BytesIO(raw))
    return output.getvalue()


def _after_fork():
    global _GATE
    _GATE = threading.BoundedSemaphore(1)


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_after_fork)


@dataclass(repr=False)
class ArchiveSession:
    purpose: str
    selectors: dict = field(repr=False)
    pid: int
    thread: int
    active: bool = True
    passphrase: custody.CapturedPassphrase | None = field(default=None, repr=False)
    key_selected: bool = False
    key_selection_failed: bool = False
    key_cleanup_unconfirmed: bool = False

    def check(self):
        if not self.active or (self.pid,self.thread) != (os.getpid(),threading.get_ident()):
            error = ArchiveError('archive authority is inactive or belongs to another process/thread')
            if self.key_cleanup_unconfirmed is True:
                native_cleanup.mark(error)
            raise error from None

    def encrypt(self, raw):
        self.check()
        return codec.encrypt(raw, self.material(), context=_CONTEXTS[self.purpose],
                             max_plaintext=MAX_PLAINTEXT)

    def decrypt(self, raw):
        self.check()
        return codec.decrypt(raw, self.material(), context=_CONTEXTS[self.purpose],
                             max_plaintext=MAX_PLAINTEXT, allow_legacy=True)

    def select_key(self):
        self.check()
        if not self.key_selected:
            # A failed selection is not retried against a changed file.
            self.key_selected = True
            self.key_selection_failed = True
            try:
                self.passphrase = custody.capture(self.selectors)
                self.key_selection_failed = False
            except BaseException as error:
                self.key_cleanup_unconfirmed = native_cleanup.unknown(error)
                raise
            finally:
                self.selectors = {}
        if self.key_selection_failed:
            error = ArchiveError('selected archive custody remains unavailable')
            if self.key_cleanup_unconfirmed is True:
                native_cleanup.mark(error)
            raise error from None
        return self.passphrase

    def material(self):
        self.select_key()
        if self.passphrase is None:
            raise ArchiveError('archive encryption custody is not configured')
        return self.passphrase.material


def freeze_selectors(selectors=None, *, cwd=None):
    """Freeze archive custody paths against an explicitly captured directory.

    An invalid selected key path remains permanently invalid, while a
    plaintext reader may defer that unused custody selection as before.
    This function performs no private file read and acquires no archive slot.
    """
    base = os.getcwd() if cwd is None else cwd
    if type(base) is not str or not os.path.isabs(base) or '\0' in base:
        raise ArchiveError('archive custody requires a captured absolute working directory')
    selected = os.environ if selectors is None else selectors
    frozen = {name: selected.get(name) for name in
              ('OLYMPUS_SECRET_KEY', 'OLYMPUS_SECRET_KEY_FILE', 'OLYMPUS_ENV')}
    path = frozen['OLYMPUS_SECRET_KEY_FILE']
    if type(path) is str and len(path) <= custody.MAX_PATH_CHARS and path.strip():
        try:
            original = Path(path.strip())
            _filename(original.name)
            if original.drive and not original.root:
                raise ArchiveError('drive-relative archive custody is unsupported')
            frozen['OLYMPUS_SECRET_KEY_FILE'] = str(selected_path(Path(base) / original))
        except Exception:
            frozen['OLYMPUS_SECRET_KEY_FILE'] = object()
    return frozen


@contextmanager
def operation(purpose, *, require_key=False, selectors=None, capture_key=True, cwd=None):
    """Admit before collecting/reading large payloads, then capture custody once.

    Selectors (and relative file paths) are frozen at entry. Creation captures
    material immediately. With capture_key=False, an input reader defers file
    custody until it knows encryption is present: a plaintext-only note import
    need not open an irrelevant key file. There is at most one material read.
    Historical decryption metadata must reach the caller's report.
    """
    if (purpose not in _CONTEXTS or type(require_key) is not bool
            or type(capture_key) is not bool):
        raise ArchiveError('invalid archive operation')
    gate = _GATE
    if not gate.acquire(blocking=False):
        raise ArchiveError('archive processing capacity is busy')
    session = None
    try:
        frozen = freeze_selectors(selectors, cwd=cwd)
        session = ArchiveSession(purpose, frozen, os.getpid(), threading.get_ident())
        if capture_key or require_key:
            session.select_key()
        if require_key:
            session.material()
        yield session
    finally:
        if session is not None:
            session.active = False
            session.selectors = {}
            session.passphrase = None
        gate.release()


def _identity(info, cap):
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, 'st_file_attributes', 0) & 0x400
            or not 0 <= info.st_size <= cap):
        raise ArchiveError('archive input is not a bounded regular file without additional links')
    return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,
            getattr(info,'st_ctime_ns',None))


def read_file(path, cap, *, optional=False, expected_identity=None):
    """One bounded image from a held parent and fresh native file information.

    Absence is optional only for the named leaf inside an opened valid parent.
    No links or hardlinks are accepted. A later replacement cannot change the
    returned bytes; metadata-preserving hostile writes/rollback are not covered.
    """
    from .vault_storage import _Directory
    if type(cap) is not int or not 0 <= cap <= 256 * 1024 * 1024 or type(optional) is not bool:
        raise ArchiveError('invalid archive input limit')
    selected = selected_path(path)
    directory, fd, failure, result = None, None, None, None
    try:
        directory = _Directory(selected.parent)
        directory.check()
        try:
            if os.name == 'nt':
                fd = directory.win.open_fd(selected.name, os.O_RDONLY)
            else:
                fd = os.open(selected.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory.fd)
        except FileNotFoundError as error:
            if not optional or native_cleanup.unknown(error):
                raise
            directory.check()
        if fd is not None:
            before = custody._information(fd)
            identity = _identity(before, cap)
            if expected_identity is not None and identity != expected_identity:
                raise ArchiveError('archive input differs from its captured inventory identity')
            chunks, remaining = [], before.st_size + 1
            while remaining:
                chunk = os.read(fd, min(remaining, 1024 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            result = b''.join(chunks)
            if len(result) != before.st_size or _identity(custody._information(fd),cap) != identity:
                raise ArchiveError('archive input changed during read')
            if os.name == 'posix':
                entry = os.stat(selected.name, dir_fd=directory.fd, follow_symlinks=False)
                if _identity(entry,cap) != identity:
                    raise ArchiveError('archive input was replaced during read')
            directory.check()
    except BaseException as exc:
        failure = exc
    finally:
        owned_fd, fd = fd, None
        owned_directory, directory = directory, None
        actions = []
        if owned_fd is not None:actions.append(lambda:os.close(owned_fd))
        if owned_directory is not None:actions.append(owned_directory.close)
        try:native_cleanup.drain(actions, failure)
        except BaseException as error:failure = error
    if failure is not None:
        if isinstance(failure, Exception):
            error = ArchiveError('archive input could not be read as one bounded regular file')
            if native_cleanup.unknown(failure):native_cleanup.mark(error)
            raise error from None
        raise failure
    return result



def file_identity(path, cap):
    """Inspect one held regular-file identity without reading private bytes.

    This lets custody exclusions recognize a native filename alias. It does
    not resolve links or change permissions. Missing selected files refuse.
    """
    from .vault_storage import _Directory
    if type(cap) is not int or not 0 <= cap <= 256 * 1024 * 1024:
        raise ArchiveError('invalid archive identity limit')
    selected = selected_path(path)
    directory,fd,failure,result = None,None,None,None
    try:
        directory = _Directory(selected.parent)
        directory.check(); directory._case_spelling(selected.name)
        fd = (directory.win.open_fd(selected.name,os.O_RDONLY) if directory.win is not None else
              os.open(selected.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory.fd))
        result = _identity(custody._information(fd),cap)
        directory.check(); directory._case_spelling(selected.name)
        if directory.win is None:
            if _identity(os.stat(selected.name,dir_fd=directory.fd,follow_symlinks=False),cap) != result:
                raise ArchiveError('archive identity path changed')
        if _identity(custody._information(fd),cap) != result:
            raise ArchiveError('archive identity changed')
    except BaseException as error:
        failure = error
    finally:
        owned_fd, fd = fd, None
        owned_directory, directory = directory, None
        actions = []
        if owned_fd is not None:actions.append(lambda:os.close(owned_fd))
        if owned_directory is not None:actions.append(owned_directory.close)
        try:native_cleanup.drain(actions, failure)
        except BaseException as error:failure = error
    if failure is not None:
        if isinstance(failure,Exception):
            error = ArchiveError('selected archive identity is unavailable')
            if native_cleanup.unknown(failure):native_cleanup.mark(error)
            raise error from None
        raise failure
    return result


def encryption_metadata(decoded=None, *, plaintext=False):
    if plaintext:
        return {'encrypted':False, 'format_version':None, 'context_authenticated':False}
    if type(decoded) is not codec.Decoded:
        raise ArchiveError('archive encryption evidence is unavailable')
    return {'encrypted':True, 'format_version':decoded.format_version,
            'context_authenticated':decoded.context_authenticated,
            'legacy_unbound':not decoded.context_authenticated}


# Portable leaf-only DOS device grammar, independent of the host platform.
# The enclosing validator already rejects separators, colons, controls and
# trailing dots/spaces. Trim only ASCII spaces before the first extension.
_WINDOWS_DEVICE_STEMS = frozenset((
    'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$',
    'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
    'COM\u00b9', 'COM\u00b2', 'COM\u00b3',
    'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9',
    'LPT\u00b9', 'LPT\u00b2', 'LPT\u00b3',
))


def _filename(name):
    """One portable Unicode leaf; bound UTF-8 bytes and Windows UTF-16 units."""
    if (type(name) is not str or not name or name in ('.','..')
            or name[-1] in '. ' or any(ord(c) < 32 or c in '<>:"/\\|?*' for c in name)
            or name.partition('.')[0].rstrip(' ').upper() in _WINDOWS_DEVICE_STEMS):
        raise ArchiveError('invalid archive output filename')
    try:
        valid = len(name.encode('utf-8')) <= 255 and len(name.encode('utf-16-le')) <= 510
    except UnicodeError:
        valid = False
    if not valid:
        raise ArchiveError('archive output filename exceeds its portable bound')


def selected_directory(path):
    """Validate original components, then bind an ordinary directory selector.

    Dot, parent and filesystem-root selections remain directory authorities.
    Reject drive-relative and Windows-normalized spelling aliases first. A
    validated extended local-drive selector uses the same spelling as its
    ordinary form. UNC and other device spellings retain their prior behavior.
    This does not confine the selected directory to an application sandbox.
    """
    original = Path(path)
    if original.drive and not original.root:
        raise ArchiveError('drive-relative archive paths are unsupported')
    for part in original.parts:
        if part not in (original.anchor, '.', '..'):
            _filename(part)
    drive = original.drive
    if (os.name == 'nt' and len(drive) == 6 and drive.startswith('\\\\?\\')
            and drive[4] in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
            and drive[5] == ':' and original.root == '\\'):
        original = Path(str(original)[4:])
    return Path(os.path.abspath(original))


def selected_path(path):
    """Bind a file selector without normalizing an invalid original spelling."""
    original = Path(path)
    _filename(original.name)
    return selected_directory(original)



def _windows_publish_rename(directory, fd, name, expected_target):
    """Retry only a refused native rename of this same held staging image.

    Every retry rechecks ancestors, spelling, staging identity and the exact
    originally selected target. A changed target or any other error refuses.
    This never restarts archive creation, signing, or a logical operation.
    """
    import msvcrt
    from . import gallery_windows as native
    from .atomicio import _REPLACE_DELAYS
    staged = _identity(custody._information(fd),MAX_CIPHERTEXT)
    for attempt in range(len(_REPLACE_DELAYS)+1):
        directory.check(); directory._case_spelling(name)
        current, current_fd, primary = None, None, None
        try:
            try: current_fd = directory.win.open_fd(name,os.O_RDONLY)
            except FileNotFoundError as error:
                if native_cleanup.unknown(error):raise
            if current_fd is not None:
                current = _identity(custody._information(current_fd),MAX_CIPHERTEXT)
        except BaseException as error:
            primary = error
            raise
        finally:
            owned, current_fd = current_fd, None
            if owned is not None:native_cleanup.drain([lambda:os.close(owned)], primary)
        if current != expected_target or _identity(custody._information(fd),MAX_CIPHERTEXT) != staged:
            raise ArchiveError('archive publication authority changed before native rename')
        directory.check(); directory._case_spelling(name)
        try:
            native.rename_same_directory(msvcrt.get_osfhandle(fd),name)
            return
        except PermissionError as error:
            if (native_cleanup.unknown(error) or getattr(error,'winerror',None) not in (5,32,33)
                    or attempt==len(_REPLACE_DELAYS)):
                raise
            time.sleep(_REPLACE_DELAYS[attempt])


def publish_file(path, raw):
    """Publish bounded bytes through held ancestors and an exclusive staging file.

    File data is fsynced before replacement. POSIX then fsyncs its directory;
    Windows performs fresh held-ancestor checks, not directory fsync. Failures
    after rename begins report an uncertain outcome. Staging is retained on
    failure; there is no cleanup, retry of the logical operation or rollback.
    Cooperative directory custody is required: this does not exclude arbitrary
    hostile writes inside an otherwise valid directory.
    """
    from .vault_storage import _Directory
    if type(raw) is not bytes or len(raw) > MAX_CIPHERTEXT:
        raise ArchiveError('invalid or oversized archive output')
    selected = selected_path(path)
    directory, fd, previous, failure = None, None, None, None
    staging = '.archive-' + uuid.uuid4().hex + '.tmp'
    rename_started = False
    previous_identity = None
    try:
        directory = _Directory(selected.parent, create=True)
        directory.check()
        directory._case_spelling(selected.name)
        # Refuse unsafe old targets without unlinking or changing permissions.
        try:
            previous = (directory.win.open_fd(selected.name, os.O_RDONLY)
                        if directory.win is not None else os.open(
                            selected.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                            dir_fd=directory.fd))
        except FileNotFoundError as error:
            if native_cleanup.unknown(error):raise
        if previous is not None:
            previous_identity = _identity(custody._information(previous), MAX_CIPHERTEXT)
            closing, previous = previous, None
            native_cleanup.drain([lambda:os.close(closing)])
        directory.check()
        if directory.win is not None:
            from . import vault_windows as native
            import msvcrt
            fd = native.to_fd(native.open_relative(directory.win.handle, staging,
                create=True, exclusive=True, write=True, delete=True), os.O_RDWR)
        else:
            fd = directory._open(staging, os.O_RDWR | os.O_CREAT | os.O_EXCL)
        directory._write(fd, raw)
        directory._case_spelling(selected.name)
        if directory.win is None:
            if (_identity(os.stat(staging, dir_fd=directory.fd, follow_symlinks=False),
                          MAX_CIPHERTEXT) != _identity(custody._information(fd), MAX_CIPHERTEXT)):
                raise ArchiveError('archive staging identity changed')
        directory.check()
        rename_started = True
        if directory.win is not None:
            _windows_publish_rename(directory,fd,selected.name,previous_identity)
        else:
            os.replace(staging, selected.name, src_dir_fd=directory.fd, dst_dir_fd=directory.fd)
        _identity(custody._information(fd), MAX_CIPHERTEXT)
        if directory.win is None:
            if (_identity(os.stat(selected.name, dir_fd=directory.fd, follow_symlinks=False),
                          MAX_CIPHERTEXT) != _identity(custody._information(fd), MAX_CIPHERTEXT)):
                raise ArchiveError('archive target identity changed')
        directory.sync()
    except BaseException as exc:
        failure = exc
    finally:
        owned_previous, previous = previous, None
        owned_fd, fd = fd, None
        owned_directory, directory = directory, None
        actions = []
        if owned_previous is not None:actions.append(lambda:os.close(owned_previous))
        if owned_fd is not None:actions.append(lambda:os.close(owned_fd))
        if owned_directory is not None:actions.append(owned_directory.close)
        try:native_cleanup.drain(actions, failure)
        except BaseException as error:failure = error
    if failure is not None:
        if isinstance(failure, Exception):
            error = ArchivePublicationError(rename_started, staging)
            if native_cleanup.unknown(failure):native_cleanup.mark(error)
            raise error from None
        attributes = BaseException.__getattribute__(failure, '__dict__')
        if type(attributes) is dict:
            attributes['publication_uncertain'] = rename_started
            attributes['retained_staging_name'] = staging
        raise failure

"""Exact-owner gallery authority; immutable blobs and recoverable operations.

This is a workspace surface boundary, not isolation from shared file tools.
POSIX uses descriptor-relative no-follow IO and flock. Windows supports one
process per workspace, checks reparse points, and cannot promise directory fsync.
No recovery path invokes a provider or discards an uncertain output.
"""
from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import threading
import time
import uuid

OMITTED = object()
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_STATE_BYTES = 8 * 1024 * 1024
MAX_IMAGES = 1000
MAX_LIVE_BYTES = 512 * 1024 * 1024
MAX_RETAINED_BYTES = 1024 * 1024 * 1024
MAX_RETAINED_ENTRIES = 50000
MAX_OPERATIONS = 10000
LOCK_TIMEOUT = 60.0
PENDING_METADATA_BYTES = 16 * 1024
MAX_OUTPUT_RECEIPT_BYTES = 64 * 1024
MAX_JSON_DEPTH = 32
EXTENSIONS = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
              '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp'}
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
_HEX = re.compile(r'^[0-9a-f]{64}$')
_ID = re.compile(r'^[A-Za-z0-9_-]{1,128}$')


class GalleryError(Exception):
    def __init__(self, code, message, status=503, operation_id=None):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.operation_id = operation_id

    def to_dict(self):
        return {'status': 'error', 'code': self.code, 'error': self.message,
                'operation_id': self.operation_id}


def capture_owner(value=OMITTED):
    if value is OMITTED:
        from .memory import current_owner
        value = current_owner()
    if not isinstance(value, str) or not value.strip() or len(value) > 8192:
        raise GalleryError('invalid_owner', 'An exact nonempty owner is required', 400)
    try:
        encoded = value.encode('utf-8', errors='strict')
    except UnicodeError as exc:
        raise GalleryError('invalid_owner', 'Owner must be strict UTF-8', 400) from exc
    if len(encoded) > 32768 or '\x00' in value:
        raise GalleryError('invalid_owner', 'Owner exceeds the supported bound', 400)
    return value


def validate_name(name):
    if (not isinstance(name, str) or not name or name[-1:] in (' ', '.')
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or any(c in name for c in '/\\:*?"<>|') or name in ('.', '..')):
        raise GalleryError('invalid_name', 'A safe image display filename is required', 400)
    try:
        size = len(name.encode('utf-8', errors='strict'))
    except UnicodeError as exc:
        raise GalleryError('invalid_name', 'Filename must be strict UTF-8', 400) from exc
    if size > 128 or name.split('.')[0].rstrip(' .').upper() in {
            'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)),
            *(f'LPT{i}' for i in range(1, 10)), 'COM¹', 'COM²', 'COM³', 'LPT¹', 'LPT²', 'LPT³'}:
        raise GalleryError('invalid_name', 'Unsupported display filename', 400)
    if Path(name).suffix.lower() not in EXTENSIONS:
        raise GalleryError('unsupported_type', 'Unsupported image extension', 415)
    return name


def validate_operation_id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise GalleryError('invalid_operation', 'A stable operation ID is required', 400)
    return value


def _json(value):
    # Bound structure before the serializer; interpreter recursion limits are
    # not a stable input contract and may be raised by other application code.
    stack = [(value, 0)]
    visited = 0
    while stack:
        item, depth = stack.pop()
        visited += 1
        if depth > MAX_JSON_DEPTH or visited > 1000000:
            raise GalleryError('invalid', 'JSON metadata structure exceeds its bound', 400)
        if isinstance(item, (dict, list)):
            if len(item) > 100000:
                raise GalleryError('invalid', 'JSON metadata collection exceeds its bound', 400)
            if isinstance(item, dict):
                if any(not isinstance(key, str) for key in item):
                    raise GalleryError('invalid', 'JSON metadata keys must be strings', 400)
                children = item.values()
            else:
                children = item
            stack.extend((child, depth + 1) for child in children)
        elif not isinstance(item, (str, int, float, bool, type(None))):
            raise GalleryError('invalid', 'Unsupported JSON metadata value', 400)
        elif isinstance(item, str) and len(item) > MAX_STATE_BYTES:
            raise GalleryError('invalid', 'JSON metadata string exceeds its bound', 400)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise GalleryError('invalid', 'Invalid JSON metadata', 400) from exc


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _decode(data):
    depth = 0
    in_string = False
    escaped = False
    for byte in data:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                in_string = False
        elif byte == 34:
            in_string = True
        elif byte in (91, 123):
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise GalleryError('unavailable', 'Gallery metadata nesting exceeds its bound; preserved')
        elif byte in (93, 125):
            depth -= 1
    try:
        return json.loads(data.decode('utf-8'), object_pairs_hook=_pairs,
                          parse_constant=lambda s: (_ for _ in ()).throw(ValueError(s)))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise GalleryError('unavailable', 'Gallery metadata is malformed; preserved for recovery') from exc


def _regular(st, directory=False):
    return ((stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode))
            and not (getattr(st, 'st_file_attributes', 0) & 0x400)
            and (directory or st.st_nlink == 1))


def _native(path):
    text = str(path)
    if os.name == 'nt' and not text.startswith('\\\\?\\'):
        return '\\\\?\\UNC\\' + text[2:] if text.startswith('\\\\') else '\\\\?\\' + text
    return text




class Directory:
    """Pinned, handle-relative authority on POSIX and Windows."""
    def __init__(self, path, create=False):
        self.path = Path(os.path.abspath(path))
        self.fd = None
        self.win_handles = []
        self.win = None
        if os.name == 'nt':
            from .gallery_windows import NativeDirectory
            self.win = NativeDirectory(self.path, create=create)
            return
        current = Path(self.path.anchor)
        fd = None
        try:
            fd = os.open(str(current), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            for part in self.path.parts[1:]:
                current = current / part
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                    os.fsync(fd)
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = nxt
            self.fd = fd
            self.identity = os.fstat(fd)
        except BaseException:
            if fd is not None:
                os.close(fd)
            raise

    def child(self, name, create=False):
        if '/' in name or '\\' in name or name in ('', '.', '..'):
            raise OSError('unsafe directory component')
        self.check()
        child = object.__new__(Directory)
        child.path = self.path / name
        child.fd, child.win, child.win_handles = None, None, []
        if self.win is not None:
            child.win = self.win.child(name, create=create)
            return child
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=self.fd)
            except FileExistsError:
                pass
            os.fsync(self.fd)
        child.fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
        child.identity = os.fstat(child.fd)
        return child

    def entries(self):
        self.check()
        if self.win is not None:
            yield from self.win.entries()
        else:
            with os.scandir(self.fd) as entries:
                yield from entries
        self.check()

    def free_bytes(self):
        self.check()
        if self.win is not None:
            return self.win.free_bytes()
        return shutil.disk_usage(self.path).free

    def close(self):
        if self.win is not None:
            self.win.close()
            self.win = None
        for handle in reversed(self.win_handles):
            os.close(handle)
        self.win_handles = []
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def check(self):
        if self.win is not None:
            self.win.check()
            return
        st = os.stat(_native(self.path), follow_symlinks=False)
        if not _regular(st, True) or (st.st_dev, st.st_ino) != (self.identity.st_dev, self.identity.st_ino):
            raise OSError('gallery directory identity changed')

    def _open(self, name, flags, mode=0o600):
        if '/' in name or '\\' in name or name in ('', '.', '..'):
            raise OSError('unsafe internal component')
        self.check()
        if self.fd is not None:
            fd = os.open(name, flags | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0), mode, dir_fd=self.fd)
        else:
            fd = self.win.open_fd(name, flags)
        if not _regular(os.fstat(fd)):
            os.close(fd)
            raise OSError('unsafe regular file')
        return fd

    def read_with_identity(self, name, cap):
        fd = self._open(name, os.O_RDONLY)
        with os.fdopen(fd, 'rb') as handle:
            before = os.fstat(handle.fileno())
            if before.st_size > cap:
                raise GalleryError('unavailable', 'Gallery evidence exceeds its bound')
            data = handle.read(cap + 1)
            after = os.fstat(handle.fileno())
            if len(data) > cap or (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                raise GalleryError('unavailable', 'Gallery evidence changed during read')
        self.check()
        return data, {'device': before.st_dev, 'inode': before.st_ino, 'mtime_ns': before.st_mtime_ns}

    def read(self, name, cap):
        return self.read_with_identity(name, cap)[0]

    def sync(self):
        if self.fd is not None:
            os.fsync(self.fd)
        self.check()

    def write_new(self, name, data):
        fd = self._open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        self.sync()

    def confirm(self, name):
        fd = self._open(name, os.O_RDWR if os.name == 'nt' else os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        self.sync()

    def publish(self, name, data):
        staging = 'pending-' + uuid.uuid4().hex + '.json'
        if self.win is not None:
            from .gallery_windows import publish
            publish(self.win, staging, name, data, self.check)
            self.sync()
            return
        self.write_new(staging, data)
        try:
            # Refuse an unsafe old destination even though replace itself would
            # not follow a symlink. Never chmod/unlink it to force success.
            fd = self._open(name, os.O_RDONLY)
        except FileNotFoundError:
            pass
        else:
            os.close(fd)
        self.check()
        os.replace(staging, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        self.sync()


@contextlib.contextmanager
def _guard(path, create=False):
    key = os.path.normcase(os.path.abspath(path))
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(key, threading.RLock())
    if not lock.acquire(timeout=LOCK_TIMEOUT):
        raise GalleryError('unavailable', 'Gallery lock acquisition timed out')
    try:
        directory = None
        fd = None
        try:
            directory = Directory(path, create=create)
            fd = directory._open('store.lock', os.O_RDWR | os.O_CREAT)
            if os.name != 'nt':
                import fcntl
                deadline = time.monotonic() + LOCK_TIMEOUT
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise OSError('gallery lock timeout')
                        time.sleep(0.02)
            try:
                yield directory
            except FileNotFoundError as exc:
                raise GalleryError('unavailable', 'Gallery evidence disappeared during operation') from exc
        except GalleryError:
            raise
        except FileNotFoundError:
            raise
        except (OSError, ValueError) as exc:
            raise GalleryError('unavailable', 'Gallery storage is unavailable; evidence preserved') from exc
        finally:
            if fd is not None:
                os.close(fd)
            if directory is not None:
                directory.close()
    finally:
        lock.release()


def _image(record):
    if not isinstance(record, dict) or set(record) != {'id', 'name', 'revision', 'bytes', 'mime', 'updated'}:
        raise ValueError('bad image')
    validate_name(record['name'])
    if (not isinstance(record['id'], str) or not re.fullmatch('[0-9a-f]{32}', record['id'])
            or not isinstance(record['revision'], str) or not _HEX.fullmatch(record['revision'])
            or type(record['bytes']) is not int or not 0 < record['bytes'] <= MAX_IMAGE_BYTES
            or record['mime'] != EXTENSIONS[Path(record['name']).suffix.lower()]
            or type(record['updated']) not in (int, float) or not 0 <= record['updated'] <= 1e12):
        raise ValueError('bad image fields')


def _typed_io(method):
    @functools.wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except GalleryError as exc:
            if exc.operation_id is None:
                if method.__name__ == 'delete':
                    exc.operation_id = kwargs.get('operation_id', args[4] if len(args) > 4 else None)
                elif method.__name__ != 'read_source':
                    exc.operation_id = kwargs.get('operation_id', args[1] if len(args) > 1 else None)
            raise
        except OSError as exc:
            operation_id = kwargs.get('operation_id', args[1] if len(args) > 1 and method.__name__ != 'read_source' else None)
            raise GalleryError('unavailable', 'Gallery storage is unavailable; evidence preserved', operation_id=operation_id) from exc
    return wrapped


class Store:
    def __init__(self, owner):
        from . import memory, sandbox
        self.owner = capture_owner(owner)
        try:
            self.workspace = Path(os.path.abspath(sandbox.workdir()))
        except OSError as exc:
            raise GalleryError('unavailable', 'Configured gallery workspace is unavailable') from exc
        self.root = self.workspace / 'gallery-v2' / memory.owner_key(self.owner)

    def _empty(self):
        return {'version': 2, 'owner': self.owner, 'images': {}, 'operations': {}, 'tombstones': {}}

    def _load(self, directory):
        try:
            state = _decode(directory.read('state.json', MAX_STATE_BYTES))
        except FileNotFoundError:
            try:
                directory.read('initialized.json', 65536)
            except FileNotFoundError:
                pass
            else:
                raise GalleryError('unavailable', 'Initialized gallery manifest is missing; replay refused')
            if any(entry.name.startswith('pending-') for entry in directory.entries()):
                raise GalleryError('unavailable', 'Manifest is missing but uncertain publication evidence exists')
            # A missing manifest is clean only before the first publication.
            for child in ('objects', 'outputs'):
                try:
                    evidence = directory.child(child)
                except FileNotFoundError:
                    continue
                try:
                    entries = evidence.entries()
                    try:
                        if next(entries, None) is not None:
                            raise GalleryError('unavailable', 'Gallery manifest is missing but recovery evidence exists')
                    finally:
                        entries.close()
                finally:
                    evidence.close()
            return self._empty()
        try:
            initialized = _decode(directory.read('initialized.json', 65536))
            if initialized != {'version': 2, 'owner': self.owner} or type(initialized.get('version')) is not int:
                raise ValueError('initialization mismatch')
            if (not isinstance(state, dict) or set(state) != {'version', 'owner', 'images', 'operations', 'tombstones'}
                    or type(state['version']) is not int or state['version'] != 2 or state['owner'] != self.owner
                    or any(not isinstance(state[k], dict) for k in ('images', 'operations', 'tombstones'))
                    or len(state['images']) > MAX_IMAGES or len(state['operations']) > MAX_OPERATIONS
                    or len(state['tombstones']) > MAX_OPERATIONS):
                raise ValueError('bad state')
            ids = set()
            for name, record in state['images'].items():
                _image(record)
                if name != record['name'] or record['id'] in ids:
                    raise ValueError('bad image key')
                ids.add(record['id'])
            if sum(i['bytes'] for i in state['images'].values()) > MAX_LIVE_BYTES:
                raise ValueError('capacity')
            for key, op in state['operations'].items():
                validate_operation_id(key)
                if (not isinstance(op, dict) or op.get('operation_id') != key or op.get('owner') != self.owner
                        or op.get('kind') not in ('generate', 'edit', 'claim', 'delete')
                        or op.get('status') not in ('reserved', 'started', 'complete', 'failed', 'deleted')
                        or not isinstance(op.get('request_hash'), str) or not _HEX.fullmatch(op['request_hash'])
                        or not isinstance(op.get('object_id'), str) or not re.fullmatch('[0-9a-f]{32}', op['object_id'])):
                    raise ValueError('bad operation')
                if (not set(op) <= {'operation_id', 'owner', 'kind', 'request_hash', 'name', 'source', 'object_id', 'status', 'updated', 'image', 'metadata', 'error', 'request'}
                        or not {'operation_id', 'owner', 'kind', 'request_hash', 'name', 'source', 'object_id', 'status', 'updated'} <= set(op)
                        or type(op['updated']) not in (int, float) or not 0 <= op['updated'] <= 1e12):
                    raise ValueError('operation fields')
                validate_name(op['name'])
                if op['source'] is not None:
                    self._validate_source(op['source'])
                if op['kind'] in ('edit', 'delete') and op['source'] is None:
                    raise ValueError('missing source')
                if op['kind'] in ('generate', 'claim') and op['source'] is not None:
                    raise ValueError('unexpected source')
                if (op['kind'] == 'delete') != (op['status'] == 'deleted'):
                    raise ValueError('invalid deletion state')
                if (op['status'] == 'failed') != ('error' in op):
                    raise ValueError('invalid failed state')
                if (op['status'] in ('complete', 'deleted')) != ('image' in op):
                    raise ValueError('invalid image state')
                if 'request' in op and (not isinstance(op['request'], dict) or len(_json(op['request'])) > 65536):
                    raise ValueError('request metadata')
                if 'metadata' in op and (not isinstance(op['metadata'], dict) or len(_json(op['metadata'])) > 8192):
                    raise ValueError('output metadata')
                if 'error' in op and (not isinstance(op['error'], dict) or set(op['error']) != {'code', 'message'}
                        or not isinstance(op['error']['code'], str) or len(op['error']['code']) > 128
                        or not isinstance(op['error']['message'], str) or len(op['error']['message']) > 1024):
                    raise ValueError('error metadata')
                if op['status'] in ('complete', 'deleted') and 'image' not in op:
                    raise ValueError('missing completed image')
                if 'image' in op:
                    _image(op['image'])
            for key, record in state['tombstones'].items():
                _image(record)
                if key != record['id']:
                    raise ValueError('bad tombstone')
            output_ids = set()
            pending_names = set()
            for op in state['operations'].values():
                if op['kind'] != 'delete':
                    if op['object_id'] in output_ids:
                        raise ValueError('duplicate output identity')
                    output_ids.add(op['object_id'])
                if op['status'] in ('reserved', 'started'):
                    if op['kind'] == 'delete' or op['name'] in pending_names or op['name'] in state['images']:
                        raise ValueError('conflicting reservation')
                    pending_names.add(op['name'])
                if 'image' in op:
                    record = op['image']
                    if record['id'] != op['object_id'] or record['name'] != op['name']:
                        raise ValueError('operation image mismatch')
                    committed = state['images'].get(record['name'])
                    retained = state['tombstones'].get(record['id'])
                    if record != committed and record != retained:
                        raise ValueError('operation image uncommitted')
                    if op['status'] == 'deleted' and record != retained:
                        raise ValueError('deletion tombstone missing')
                    if op['kind'] == 'delete' and op['source'] != {'name': record['name'], 'id': record['id'], 'revision': record['revision']}:
                        raise ValueError('deletion source mismatch')
            if (len(state['images']) + len(pending_names) > MAX_IMAGES
                    or sum(r['bytes'] for r in state['images'].values()) + len(pending_names) * MAX_IMAGE_BYTES > MAX_LIVE_BYTES):
                raise ValueError('reservation capacity')
            if len(_json(state)) + len(pending_names) * PENDING_METADATA_BYTES > MAX_STATE_BYTES:
                raise ValueError('reserved metadata capacity')
            complete_images = {o['image']['id']: o['image'] for o in state['operations'].values() if o['status'] == 'complete'}
            deleted_images = {o['image']['id']: o['image'] for o in state['operations'].values() if o['status'] == 'deleted'}
            for record in state['images'].values():
                if record != complete_images.get(record['id']) or record['id'] in state['tombstones']:
                    raise ValueError('unbound live image')
            for record in state['tombstones'].values():
                if record != complete_images.get(record['id']) or record != deleted_images.get(record['id']):
                    raise ValueError('unbound tombstone')
        except (ValueError, TypeError, KeyError, GalleryError, FileNotFoundError) as exc:
            raise GalleryError('unavailable', 'Gallery authority is invalid; preserved for recovery') from exc
        directory.confirm('initialized.json')
        directory.confirm('state.json')
        return state

    def _save(self, directory, state):
        data = _json(state)
        pending = sum(op['status'] in ('reserved', 'started') for op in state['operations'].values())
        if len(data) + pending * PENDING_METADATA_BYTES > MAX_STATE_BYTES:
            raise GalleryError('capacity', 'Gallery metadata capacity reached including output reservations', 409)
        try:
            directory.write_new('initialized.json', _json({'version': 2, 'owner': self.owner}))
        except FileExistsError:
            initialized = _decode(directory.read('initialized.json', 65536))
            if initialized != {'version': 2, 'owner': self.owner} or type(initialized.get('version')) is not int:
                raise GalleryError('unavailable', 'Gallery initialization identity mismatch')
            directory.confirm('initialized.json')
        try:
            directory.publish('state.json', data)
        except OSError as exc:
            raise GalleryError('publication_unconfirmed', 'Gallery publication is unconfirmed; recover using the same operation ID') from exc

    def _receipt(self, op, execute=False, *, directory):
        if op['status'] in ('complete', 'deleted'):
            self._blob(op['image'], directory)
            evidence = directory.child('objects')
            try:
                evidence.confirm(op['image']['id'] + '.blob')
            finally:
                evidence.close()
        result = dict(op)
        result.pop('request_hash', None)
        result.pop('owner', None)
        result['execute'] = execute
        if result['status'] == 'started':
            result['status'] = 'indeterminate'
        return result

    def _blob(self, record, directory):
        try:
            objects = directory.child('objects')
            try:
                data = objects.read(record['id'] + '.blob', MAX_IMAGE_BYTES)
            finally:
                objects.close()
        except FileNotFoundError as exc:
            raise GalleryError('unavailable', 'Committed image bytes are missing') from exc
        if len(data) != record['bytes'] or _digest(data) != record['revision']:
            raise GalleryError('unavailable', 'Image content identity changed; evidence preserved')
        return data

    def _root_present(self):
        try:
            root = Directory(self.root)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise GalleryError('unavailable', 'Gallery root identity is unavailable') from exc
        try:
            root.check()
            return True
        except OSError as exc:
            raise GalleryError('unavailable', 'Gallery root identity changed') from exc
        finally:
            root.close()

    def list_images(self):
        try:
            with _guard(self.root) as directory:
                state = self._load(directory)
                images = list(state['images'].values())
                for record in images:
                    self._blob(record, directory)
                return sorted(images, key=lambda item: item['updated'], reverse=True)
        except FileNotFoundError as exc:
            if self._root_present():
                raise GalleryError('unavailable', 'Gallery image evidence is missing') from exc
            return []

    def read(self, name, expected_id=None, expected_revision=None, cap=MAX_IMAGE_BYTES):
        validate_name(name)
        try:
            with _guard(self.root) as directory:
                state = self._load(directory)
                record = state['images'].get(name)
                if record is None:
                    raise GalleryError('missing', 'Image not found', 404)
                if ((expected_id is not None and expected_id != record['id'])
                        or (expected_revision is not None and expected_revision != record['revision'])):
                    raise GalleryError('stale', 'Image revision changed; refresh before continuing', 409)
                if record['bytes'] > cap:
                    raise GalleryError('oversized', 'Source image exceeds the edit bound', 413)
                return self._blob(record, directory), dict(record)
        except FileNotFoundError as exc:
            if self._root_present():
                raise GalleryError('unavailable', 'Image evidence is missing') from exc
            raise GalleryError('missing', 'Image not found', 404) from exc

    @_typed_io
    def read_source(self, source):
        self._validate_source(source)
        return self.read(source['name'], source['id'], source['revision'], MAX_SOURCE_BYTES)

    @staticmethod
    def _validate_source(source):
        if (not isinstance(source, dict) or set(source) != {'name', 'id', 'revision'}
                or not isinstance(source['id'], str) or not re.fullmatch('[0-9a-f]{32}', source['id'])
                or not isinstance(source['revision'], str) or not _HEX.fullmatch(source['revision'])):
            raise GalleryError('invalid_source', 'Exact source ID and revision are required', 400)
        validate_name(source['name'])

    def lookup(self, operation_id, kind, request, name=None, source=None):
        """Read an existing request before checking execution-only dependencies."""
        validate_operation_id(operation_id)
        if kind not in ('generate', 'edit', 'claim') or not isinstance(request, dict):
            raise GalleryError('invalid', 'Invalid image request', 400)
        if name is not None:
            validate_name(name)
        if kind == 'edit':
            self._validate_source(source)
        elif source is not None:
            raise GalleryError('invalid_source', 'Source only permitted for edits', 400)
        encoded = _json({'kind': kind, 'request': request, 'name': name, 'source': source})
        if len(encoded) > 65536:
            raise GalleryError('oversized', 'Request metadata exceeds its bound', 413)
        try:
            with _guard(self.root) as directory:
                op = self._load(directory)['operations'].get(operation_id)
                if op is None:
                    return None
                if op['request_hash'] != _digest(encoded):
                    raise GalleryError('operation_conflict', 'Operation ID belongs to a different request', 409, operation_id)
                return self._receipt(op, directory=directory)
        except FileNotFoundError:
            return None

    def _admit_storage(self, pending_count, root_directory):
        """Account all owned recovery bytes, including abandoned snapshots.

        Available disk space is a conservative pre-call observation, not a
        promise against unrelated processes consuming the same filesystem.
        """
        total = 0
        count = 0
        for folder in (None, 'objects', 'outputs'):
            try:
                directory = root_directory if folder is None else root_directory.child(folder)
            except FileNotFoundError:
                continue
            try:
                entries = directory.entries()
                try:
                    for entry in entries:
                        count += 1
                        if count > MAX_RETAINED_ENTRIES:
                            raise GalleryError('capacity', 'Retained gallery inventory bound reached', 409)
                        info = entry.stat(follow_symlinks=False)
                        if folder is None and entry.name in ('objects', 'outputs'):
                            if not _regular(info, True):
                                raise GalleryError('unavailable', 'Unsafe gallery recovery directory')
                            continue
                        if not _regular(info):
                            raise GalleryError('unavailable', 'Unsafe gallery retained evidence')
                        total += info.st_size
                finally:
                    entries.close()
                directory.check()
            finally:
                if folder is not None:
                    directory.close()
        # Two full snapshots leave room for publication/recovery evidence;
        # immutable output and bounded output receipt are reserved separately.
        reservation = MAX_IMAGE_BYTES + 2 * MAX_STATE_BYTES + MAX_OUTPUT_RECEIPT_BYTES
        required = (pending_count + 1) * reservation
        if total + required > MAX_RETAINED_BYTES:
            raise GalleryError('capacity', 'Retained gallery capacity reached; recoverable deletion does not reclaim quota', 409)
        try:
            free = root_directory.free_bytes()
        except OSError as exc:
            raise GalleryError('unavailable', 'Available gallery disk space could not be established') from exc
        if type(free) is not int or free < 0:
            raise GalleryError('unavailable', 'Available gallery disk space is invalid')
        if free < required:
            raise GalleryError('capacity', 'Insufficient available disk space for durable image output', 409)

    @_typed_io
    def reserve(self, operation_id, kind, request, name=None, source=None):
        validate_operation_id(operation_id)
        if kind not in ('generate', 'edit', 'claim') or not isinstance(request, dict):
            raise GalleryError('invalid', 'Invalid image request', 400)
        if name is not None:
            validate_name(name)
        if kind == 'edit':
            self._validate_source(source)
        elif source is not None:
            raise GalleryError('invalid_source', 'Source only permitted for edits', 400)
        binding = {'kind': kind, 'request': request, 'name': name, 'source': source}
        encoded = _json(binding)
        if len(encoded) > 65536:
            raise GalleryError('oversized', 'Request metadata exceeds its bound', 413)
        request_hash = _digest(encoded)
        with _guard(self.root, create=True) as directory:
            state = self._load(directory)
            previous = state['operations'].get(operation_id)
            if previous:
                if previous['request_hash'] != request_hash:
                    raise GalleryError('operation_conflict', 'Operation ID belongs to a different request', 409, operation_id)
                return self._receipt(previous, directory=directory)
            if len(state['operations']) >= MAX_OPERATIONS:
                raise GalleryError('capacity', 'Operation receipt capacity reached', 409, operation_id)
            pending = [o for o in state['operations'].values() if o['status'] in ('reserved', 'started') and o['kind'] != 'delete']
            if (len(state['images']) + len(pending) >= MAX_IMAGES
                    or sum(r['bytes'] for r in state['images'].values()) + (len(pending) + 1) * MAX_IMAGE_BYTES > MAX_LIVE_BYTES):
                raise GalleryError('capacity', 'Gallery capacity reached', 409, operation_id)
            self._admit_storage(len(pending), directory)
            name = name or ('image-' + uuid.uuid4().hex + '.png')
            if name in state['images'] or any(o['name'] == name for o in pending):
                raise GalleryError('name_conflict', 'Output display name already exists or is reserved', 409, operation_id)
            if source is not None:
                record = state['images'].get(source['name'])
                if not record or record['id'] != source['id'] or record['revision'] != source['revision']:
                    raise GalleryError('stale', 'Edit source revision changed', 409, operation_id)
                if record['bytes'] > MAX_SOURCE_BYTES:
                    raise GalleryError('oversized', 'Edit source exceeds its bound', 413, operation_id)
                self._blob(record, directory)
            op = {'operation_id': operation_id, 'owner': self.owner, 'kind': kind,
                  'request_hash': request_hash, 'name': name, 'source': source,
                  'object_id': uuid.uuid4().hex, 'status': 'reserved', 'updated': time.time(),
                  'request': request}
            state['operations'][operation_id] = op
            self._save(directory, state)
            return self._receipt(op, True, directory=directory)

    @_typed_io
    def start(self, operation_id):
        validate_operation_id(operation_id)
        with _guard(self.root) as directory:
            state = self._load(directory)
            op = state['operations'].get(operation_id)
            if not op:
                raise GalleryError('missing', 'Operation not found', 404, operation_id)
            if op['status'] != 'reserved':
                raise GalleryError('operation_conflict', 'Operation already started; recover without repeating provider work', 409, operation_id)
            op['status'], op['updated'] = 'started', time.time()
            self._save(directory, state)
            return self._receipt(op, directory=directory)

    def status(self, operation_id):
        validate_operation_id(operation_id)
        try:
            with _guard(self.root) as directory:
                op = self._load(directory)['operations'].get(operation_id)
                if not op:
                    raise GalleryError('missing', 'Operation not found', 404, operation_id)
                return self._receipt(op, directory=directory)
        except FileNotFoundError as exc:
            raise GalleryError('missing', 'Operation not found', 404, operation_id) from exc

    @_typed_io
    def mark_failed(self, operation_id, code, message):
        validate_operation_id(operation_id)
        with _guard(self.root) as directory:
            state = self._load(directory)
            op = state['operations'].get(operation_id)
            if not op:
                raise GalleryError('missing', 'Operation not found', 404, operation_id)
            if op['status'] in ('complete', 'deleted'):
                return self._receipt(op, directory=directory)
            op['status'] = 'failed'
            op['error'] = {'code': str(code)[:128], 'message': str(message)[:1024]}
            self._save(directory, state)
            return self._receipt(op, directory=directory)

    @_typed_io
    def finalize(self, operation_id, data, metadata=None):
        from .image_validation import validate_image
        validate_operation_id(operation_id)
        if not isinstance(data, bytes) or not 0 < len(data) <= MAX_IMAGE_BYTES:
            raise GalleryError('oversized', 'Invalid image byte length', 413, operation_id)
        info = validate_image(data)
        metadata = {} if metadata is None else metadata
        if not isinstance(metadata, dict) or len(_json(metadata)) > 8192:
            raise GalleryError('invalid', 'Invalid output metadata', 400, operation_id)
        with _guard(self.root) as directory:
            state = self._load(directory)
            op = state['operations'].get(operation_id)
            if not op:
                raise GalleryError('missing', 'Operation not found', 404, operation_id)
            if op['status'] == 'complete':
                if op['image']['revision'] != _digest(data):
                    raise GalleryError('operation_conflict', 'Operation already has different output', 409, operation_id)
                return self._receipt(op, directory=directory)
            if op['status'] != 'started':
                raise GalleryError('operation_conflict', 'Operation is not started', 409, operation_id)
            if EXTENSIONS[Path(op['name']).suffix.lower()] != info['mime']:
                raise GalleryError('unsupported_type', 'Image format does not match output extension', 415, operation_id)
            record = {'id': op['object_id'], 'name': op['name'], 'revision': _digest(data),
                      'bytes': len(data), 'mime': info['mime'], 'updated': time.time()}
            with contextlib.ExitStack() as stack:
                outputs = directory.child('outputs', create=True)
                stack.callback(outputs.close)
                objects = directory.child('objects', create=True)
                stack.callback(objects.close)
                blob = record['id'] + '.blob'
                try:
                    objects.write_new(blob, data)
                except FileExistsError:
                    if objects.read(blob, MAX_IMAGE_BYTES) != data:
                        raise GalleryError('operation_conflict', 'Conflicting durable output preserved', 409, operation_id)
                receipt = {'version': 2, 'owner': self.owner, 'operation_id': operation_id,
                           'request_hash': op['request_hash'], 'image': record, 'metadata': metadata}
                try:
                    outputs.write_new(op['object_id'] + '.json', _json(receipt))
                except FileExistsError:
                    receipt = _decode(outputs.read(op['object_id'] + '.json', MAX_OUTPUT_RECEIPT_BYTES))
                return self._adopt(directory, state, op, receipt)


    def _adopt(self, directory, state, op, receipt):
        try:
            if (not isinstance(receipt, dict) or set(receipt) != {'version', 'owner', 'operation_id', 'request_hash', 'image', 'metadata'}
                    or type(receipt['version']) is not int or receipt['version'] != 2 or receipt['owner'] != self.owner
                    or receipt['operation_id'] != op['operation_id'] or receipt['request_hash'] != op['request_hash']):
                raise ValueError('receipt mismatch')
            record = receipt['image']
            _image(record)
            if record['id'] != op['object_id'] or record['name'] != op['name']:
                raise ValueError('output mismatch')
            if not isinstance(receipt['metadata'], dict) or len(_json(receipt['metadata'])) > 8192:
                raise ValueError('metadata')
        except (KeyError, TypeError, ValueError, GalleryError) as exc:
            raise GalleryError('unavailable', 'Durable output receipt is invalid; preserved') from exc
        self._blob(record, directory)
        for folder, leaf in (('objects', record['id'] + '.blob'), ('outputs', op['object_id'] + '.json')):
            evidence = directory.child(folder)
            try:
                evidence.confirm(leaf)
            finally:
                evidence.close()
        source = op.get('source')
        if source:
            current = state['images'].get(source['name'])
            if not current or current['id'] != source['id'] or current['revision'] != source['revision']:
                raise GalleryError('stale', 'Source changed; durable output retained for recovery', 409, op['operation_id'])
            self._blob(current, directory)
        existing = state['images'].get(record['name'])
        if existing and existing != record:
            raise GalleryError('name_conflict', 'Output name changed; durable output retained', 409, op['operation_id'])
        state['images'][record['name']] = record
        op.update(status='complete', image=record, metadata=receipt['metadata'], updated=time.time())
        self._save(directory, state)
        return self._receipt(op, directory=directory)

    def recover(self, operation_id):
        validate_operation_id(operation_id)
        try:
            with _guard(self.root) as directory:
                state = self._load(directory)
                op = state['operations'].get(operation_id)
                if not op:
                    raise GalleryError('missing', 'Operation not found', 404, operation_id)
                if op['status'] != 'started':
                    return self._receipt(op, directory=directory)
                try:
                    outputs = directory.child('outputs')
                    try:
                        receipt = _decode(outputs.read(op['object_id'] + '.json', MAX_OUTPUT_RECEIPT_BYTES))
                    finally:
                        outputs.close()
                except FileNotFoundError:
                    return self._receipt(op, directory=directory)
                return self._adopt(directory, state, op, receipt)
        except FileNotFoundError as exc:
            raise GalleryError('missing', 'Operation not found', 404, operation_id) from exc

    @_typed_io
    def delete(self, name, expected_id, expected_revision, operation_id):
        validate_name(name)
        validate_operation_id(operation_id)
        source = {'name': name, 'id': expected_id, 'revision': expected_revision}
        self._validate_source(source)
        binding = _digest(_json({'kind': 'delete', 'source': source}))
        try:
            with _guard(self.root) as directory:
                state = self._load(directory)
                previous = state['operations'].get(operation_id)
                if previous:
                    if previous['request_hash'] != binding:
                        raise GalleryError('operation_conflict', 'Deletion ID belongs to a different request', 409, operation_id)
                    return self._receipt(previous, directory=directory)
                record = state['images'].get(name)
                if not record:
                    raise GalleryError('missing', 'Image not found', 404, operation_id)
                if record['id'] != expected_id or record['revision'] != expected_revision:
                    raise GalleryError('stale', 'Image revision changed; deletion refused', 409, operation_id)
                self._blob(record, directory)
                if len(state['operations']) >= MAX_OPERATIONS:
                    raise GalleryError('capacity', 'Operation receipt capacity reached', 409, operation_id)
                op = {'operation_id': operation_id, 'owner': self.owner, 'kind': 'delete',
                      'request_hash': binding, 'name': name, 'source': source,
                      'object_id': record['id'], 'status': 'deleted', 'image': record,
                      'updated': time.time()}
                state['operations'][operation_id] = op
                state['tombstones'][record['id']] = record
                del state['images'][name]
                self._save(directory, state)
                return self._receipt(op, directory=directory)
        except FileNotFoundError as exc:
            raise GalleryError('missing', 'Image not found', 404, operation_id) from exc

"""Exact-owner workspace evidence; legacy normalized records stay unclaimed.

Local process locks serialize transactions on one state directory. They do not
provide a distributed Postgres transaction or native Windows process locking.
Validation detects damage; a filesystem/database administrator remains trusted.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import tempfile

from . import atomicio, config, memory, proclock, store
from .assessment_evidence import _windows_extended_path


class OwnerEvidenceStateError(RuntimeError):
    def __init__(self, name: str, reason: str):
        self.store = name
        self.reason = reason
        super().__init__(f"{name} evidence is unavailable ({reason}). Preserve the state; "
                         "no automatic repair or legacy attribution was performed.")


def exact(owner):
    value = memory.canonical_owner(owner)
    text(value, 8192)
    return value


def workspace(owner):
    return config.MEMORY_DIR / "owners" / memory.owner_key(exact(owner)) / "workspace-v2"


@contextmanager
def guard(owner, name="workspace"):
    key = hashlib.sha256((name + "\0" + exact(owner)).encode("utf-8")).hexdigest()
    try:
        with proclock.lock("owner-evidence-" + key):
            yield
    except OSError as err:
        raise OwnerEvidenceStateError(name, "transaction unavailable") from err


def _io(path):
    return Path(_windows_extended_path(path)) if os.name == "nt" else Path(path)


def _parents(path, name):
    base = config.MEMORY_DIR.absolute()
    original = Path(path).absolute()
    if not original.is_relative_to(base):
        raise OwnerEvidenceStateError(name, "path outside configured state")
    for parent in original.parents:
        if parent == base:
            break
        try:
            info = _io(parent).lstat()
        except FileNotFoundError:
            continue
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise OwnerEvidenceStateError(name, "non-directory or linked state parent")


def read_bytes(path, cap, name):
    try:
        _parents(path, name)
        path = _io(path)
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise OwnerEvidenceStateError(name, "evidence is not a regular file")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (not stat.S_ISREG(opened.st_mode)
                    or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)):
                raise OwnerEvidenceStateError(name, "evidence changed during read")
            raw = handle.read(cap + 1)
        if len(raw) > cap:
            raise OwnerEvidenceStateError(name, "size bound exceeded")
        return raw
    except FileNotFoundError:
        return None
    except OSError as err:
        raise OwnerEvidenceStateError(name, "read unavailable") from err


def publish(path, raw, name):
    """Unique private staging; callers hold their complete transaction guard."""
    tmp = None
    try:
        _parents(path, name)
        path = _io(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, filename = tempfile.mkstemp(prefix=".own-", suffix=".tmp", dir=path.parent)
        os.close(fd)
        tmp = Path(filename)
        atomicio.publish(tmp, path, raw, chmod=0o600)
    except OSError as err:
        raise OwnerEvidenceStateError(name, "publication unconfirmed; re-read before retry") from err
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass  # Preserve an inaccessible staging file; do not mask the refusal.


def unlink(path, name):
    try:
        _io(path).unlink()
        atomicio.fsync_dir(_io(path).parent)
    except OSError as err:
        raise OwnerEvidenceStateError(name, "removal unconfirmed; re-read before retry") from err


def text(value, cap, *, empty=False):
    if not isinstance(value, str) or len(value) > cap or (not empty and not value.strip()):
        raise ValueError("invalid text")


def number(value, *, minimum=0):
    if (isinstance(value, bool) or not isinstance(value, (float, int))
            or not math.isfinite(value) or value < minimum):
        raise ValueError("invalid number")


def integer(value, *, minimum=0, maximum=2**53):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid integer")


def records(value, cap):
    if not isinstance(value, list) or len(value) > cap:
        raise ValueError("invalid record collection")


def fields(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError("invalid record fields")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("non-finite JSON number")


def decode(raw, name, cap):
    try:
        if not isinstance(raw, bytes) or len(raw) > cap:
            raise ValueError("invalid byte value")
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_constant=_constant)
    except (ValueError, TypeError, RecursionError, OverflowError) as err:
        raise OwnerEvidenceStateError(name, "invalid JSON or size") from err


@dataclass(frozen=True)
class JsonStore:
    owner: str
    name: str
    validate: object
    empty: object = list
    max_bytes: int = 4 * 1024 * 1024
    path: Path | None = None
    namespace: str | None = None
    backend_override: object | None = None
    strict_durability: bool = False

    def _backend(self):
        return self.backend_override if self.backend_override is not None else store.backend()

    @property
    def key(self):
        return memory.storage_key(exact(self.owner))

    def guard(self):
        return guard(self.owner, self.namespace or "workspace")

    def _file(self):
        if self.path is not None:
            return self.path
        backend = self._backend()
        if type(backend) is store.FileStore:
            return config.MEMORY_DIR / "store" / self.namespace / self.key
        return None

    def _raw(self):
        try:
            path = self._file()
            if path is not None:
                return read_bytes(path, self.max_bytes, self.name)
            return self._backend().get(self.namespace, self.key)
        except OwnerEvidenceStateError:
            raise
        except Exception as err:
            raise OwnerEvidenceStateError(self.name, "backend read unavailable") from err

    def _value(self, raw):
        if raw is None:
            return self.empty()
        envelope = decode(raw, self.name, self.max_bytes)
        try:
            fields(envelope, ("version", "owner", "data"))
            if type(envelope["version"]) is not int or envelope["version"] != 2:
                raise ValueError("invalid evidence version")
            if envelope["owner"] != exact(self.owner):
                raise ValueError("wrong owner")
            self.validate(envelope["data"])
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as err:
            raise OwnerEvidenceStateError(self.name, "invalid schema or owner") from err
        return envelope["data"]

    def load(self):
        return self._value(self._raw())

    def save(self, value):
        try:
            self.validate(value)
            raw = json.dumps({"version": 2, "owner": exact(self.owner), "data": value},
                             sort_keys=True, allow_nan=False).encode("utf-8")
            if len(raw) > self.max_bytes:
                raise ValueError("size bound exceeded")
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as err:
            raise OwnerEvidenceStateError(self.name, "invalid publication schema or size") from err
        with self.guard():
            self.load()  # Ordinary publication never repairs existing damage.
            path = self._file()
            if path is not None:
                if self.strict_durability:
                    from . import note_evidence
                    try:
                        note_evidence.mkdir(path.parent)
                        publish(path, raw, self.name)
                        note_evidence.sync_dir(path.parent)
                    except OSError as err:
                        raise OwnerEvidenceStateError(self.name,
                            "durable publication unconfirmed; re-read before retry") from err
                else:
                    publish(path, raw, self.name)
            else:
                try:
                    self._backend().put(self.namespace, self.key, raw)
                except Exception as err:
                    raise OwnerEvidenceStateError(self.name, "backend publication unconfirmed") from err

    def status(self):
        try:
            with self.guard():
                raw = self._raw()
                value = self._value(raw)
            return {"store": self.name, "state": "missing" if raw is None else "valid",
                    "count": len(value) if value is not None else 0, "reason": None}
        except OwnerEvidenceStateError as err:
            return {"store": self.name, "state": "unavailable", "count": None,
                    "reason": err.reason}


def cli_errors(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except OwnerEvidenceStateError as err:
            print(str(err))
            return 1
    return wrapped


def tool_errors(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except OwnerEvidenceStateError as err:
            return str(err)
    return wrapped


def namespace_values(namespace, factory, *, max_owners=1000, max_rows=100000):
    """Enumerate owner envelopes without treating a storage key as an identity."""
    import re
    label = namespace
    try:
        backend = store.backend()
        directory = config.MEMORY_DIR / "store" / namespace
        def keys():
            if type(backend) is store.FileStore:
                _parents(directory, label)
                try:
                    info = _io(directory).lstat()
                except FileNotFoundError:
                    return []
                if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise OwnerEvidenceStateError(label, "invalid namespace directory")
                source = (p.name for p in _io(directory).iterdir()
                          if not (p.name.startswith(".own-") and p.name.endswith(".tmp")))
            else:
                source = backend.keys(namespace)
            names = []
            for key in source:
                if (not isinstance(key, str)
                        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key)):
                    raise OwnerEvidenceStateError(label, "invalid storage key")
                names.append(key)
                if len(names) > max_owners:
                    raise OwnerEvidenceStateError(label, "owner-count bound exceeded")
            if len(set(names)) != len(names):
                raise OwnerEvidenceStateError(label, "duplicate storage key")
            return sorted(names)
        names = keys()
        result, count = {}, 0
        cap = factory("shared").max_bytes
        for key in names:
            raw = (read_bytes(directory / key, cap, label)
                   if type(backend) is store.FileStore else backend.get(namespace, key))
            if raw is None:
                raise OwnerEvidenceStateError(label, "enumerated evidence disappeared")
            envelope = decode(raw, label, cap)
            if not isinstance(envelope, dict):
                raise OwnerEvidenceStateError(label, "invalid owner envelope")
            owner = envelope.get("owner")
            text(owner, 8192)
            if owner != exact(owner) or memory.storage_key(owner) != key:
                raise OwnerEvidenceStateError(label, "key/owner attribution mismatch")
            value = factory(owner)._value(raw)
            result[owner] = value
            count += len(value)
            if count > max_rows:
                raise OwnerEvidenceStateError(label, "aggregate row bound exceeded")
        if keys() != names:
            raise OwnerEvidenceStateError(label, "owner enumeration changed")
        return result
    except OwnerEvidenceStateError:
        raise
    except Exception as err:
        raise OwnerEvidenceStateError(label, "aggregation unavailable") from err

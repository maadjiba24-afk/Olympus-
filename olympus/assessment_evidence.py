"""Bounded, exact-owner evidence transactions for assessment result stores.

Validation detects damage; it does not authenticate a local writer. Callers
hold the store guard across the complete read/modify/publish transaction.
Windows retains proclock's documented single-process topology.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import os
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import atomicio, memory, proclock


class AssessEvidenceStateError(RuntimeError):
    """Existing evidence is unavailable, or publication was not confirmed."""

    def __init__(self, owner: str, store: str, reason: str):
        self.owner = memory.canonical_owner(owner)
        self.store = store
        self.reason = reason
        self.repair_command = (
            f"olympus assess evidence {store} --owner <exact-owner> --repair")
        super().__init__(f"Assessment {store} evidence is unavailable ({reason}). "
                         "Preserve the bytes and inspect evidence status before repair.")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("non-finite JSON number")


def _windows_extended_path(path):
    """Use the same absolute file through the Windows extended-length API.

    Owner keys and full quarantine digests remain intact. This changes neither
    the on-disk layout nor the host's LongPathsEnabled policy. Normalize ordinary
    paths before adding the prefix; already-extended paths retain their spelling.
    """
    raw = os.fspath(path)
    if raw.startswith("\\\\?\\"):
        return raw
    raw = ntpath.abspath(raw)
    if raw.startswith("\\\\"):
        return "\\\\?\\UNC\\" + raw[2:]
    return "\\\\?\\" + raw


@dataclass(frozen=True)
class Store:
    owner: str
    name: str
    path_fn: Callable
    validate: Callable
    empty: Callable
    max_bytes: int
    repair_bytes: int = 32 * 1024 * 1024

    def error(self, reason):
        return AssessEvidenceStateError(self.owner, self.name, reason)

    def path(self):
        try:
            path = self.path_fn(self.owner)
            return Path(_windows_extended_path(path)) if os.name == "nt" else path
        except OSError as err:
            raise self.error(f"directory unavailable: {type(err).__name__}") from err

    @contextmanager
    def guard(self):
        # All three stores share one exact-owner lock. Nested calls are reentrant.
        digest = hashlib.sha256(self.owner.encode("utf-8")).hexdigest()
        try:
            with proclock.lock(f"assess-data-{digest}"):
                yield
        except OSError as err:
            raise self.error(f"transaction unavailable: {type(err).__name__}") from err

    def read_bytes(self, cap=None):
        path = self.path()
        limit = self.max_bytes if cap is None else cap
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise self.error("evidence is not a regular file")
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as handle:
                opened = os.fstat(handle.fileno())
                if (not stat.S_ISREG(opened.st_mode)
                        or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)):
                    raise self.error("evidence changed during read")
                raw = handle.read(limit + 1)
        except FileNotFoundError:
            return None
        except OSError as err:
            raise self.error(f"read failed: {type(err).__name__}") from err
        if len(raw) > limit:
            raise self.error("evidence exceeds the read or preservation bound")
        return raw

    def decode(self, raw):
        if len(raw) > self.max_bytes:
            raise self.error("evidence exceeds the read bound")
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                               parse_constant=_constant)
            self.validate(value)
            return value
        except (ValueError, TypeError, OverflowError, RecursionError) as err:
            # Neither JSON fragments nor attacker-controlled validation values
            # belong in health, tool errors or prompt context.
            raise self.error("invalid JSON or record schema") from err

    def load(self):
        raw = self.read_bytes()
        return self.empty() if raw is None else self.decode(raw)

    def save_locked(self, value):
        try:
            self.validate(value)
            raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, OverflowError, RecursionError) as err:
            raise self.error("invalid publication schema") from err
        if len(raw) > self.max_bytes:
            raise self.error("publication exceeds the size bound")
        path = self.path()
        try:
            self.publish(path, raw)
        except OSError as err:
            raise self.error("publication not confirmed; re-read before retry") from err

    def publish(self, path, raw):
        # Unique mode-0600 staging files, even after an interrupted publication.
        # The destination already carries its identity. Repeating a full digest
        # in the staging name wastes path/component space without adding safety.
        fd, name = tempfile.mkstemp(prefix=".assess-", suffix=".tmp",
                                    dir=path.parent)
        os.close(fd)
        tmp = Path(name)
        try:
            atomicio.publish(tmp, path, raw, chmod=0o600)
        finally:
            tmp.unlink(missing_ok=True)

    def status(self):
        try:
            with self.guard():
                raw = self.read_bytes()
                value = self.empty() if raw is None else self.decode(raw)
                return {"owner": self.owner, "store": self.name,
                        "state": "missing" if raw is None else "valid",
                        "count": len(value), "reason": None, "repair_command": None}
        except AssessEvidenceStateError as err:
            return {"owner": self.owner, "store": self.name, "state": "unavailable",
                    "count": None, "reason": err.reason,
                    "repair_command": err.repair_command}

    def repair(self):
        """Operator-only: durably preserve exact bytes before resetting damage.

        Valid/missing state and ambiguous legacy directories remain untouched.
        Unreadable/nonregular/too-large evidence is refused unchanged.
        """
        with self.guard():
            raw = self.read_bytes(self.repair_bytes)
            if raw is None:
                return {**self.status(), "repaired": False}
            try:
                self.decode(raw)
            except AssessEvidenceStateError:
                pass
            else:
                return {**self.status(), "repaired": False}
            digest = hashlib.sha256(raw).hexdigest()
            path = self.path()
            quarantine = path.with_name(f"{path.stem}.corrupt.{digest}.json")
            try:
                if not stat.S_ISREG(quarantine.lstat().st_mode):
                    raise self.error("quarantine is not a regular file")
                with quarantine.open("rb") as handle:
                    existing = handle.read(self.repair_bytes + 1)
            except FileNotFoundError:
                try:
                    self.publish(quarantine, raw)
                except OSError as err:
                    raise self.error("quarantine publication not confirmed") from err
            except OSError as err:
                raise self.error(f"quarantine unavailable: {type(err).__name__}") from err
            else:
                if existing != raw:
                    raise self.error("quarantine collision")
                # A previous attempt may have renamed the archive then failed
                # its directory fsync. Matching bytes alone do not prove that
                # preservation is durable before the live record is reset.
                try:
                    # Windows FlushFileBuffers requires a writable handle.
                    # r+b never truncates or rewrites the preserved bytes.
                    with quarantine.open("r+b" if os.name == "nt" else "rb") as handle:
                        os.fsync(handle.fileno())
                    atomicio.fsync_dir(quarantine.parent)
                except OSError as err:
                    raise self.error("quarantine durability not confirmed") from err
            self.save_locked(self.empty())
            return {**self.status(), "repaired": True,
                    "quarantined_sha256": digest, "quarantine_file": quarantine.name}

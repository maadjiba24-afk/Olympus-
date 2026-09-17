"""Validated exact-owner snapshots for typed memory and relationship graphs.

One publication contains every collection participating in a local transaction.
Legacy normalized keys are never read as an exact principal or overwritten.
File writers use the established state-directory lock; Windows still requires
one process per state directory until M13. Distributed backend work is M12.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import replace
from functools import wraps

from . import config, memory, owner_evidence as oe, store
import hashlib

_CURRENT = ContextVar("owner_memory_transactions", default=None)


class Snapshot:
    def __init__(self, owner, namespace, collections, validate, *, strict_durability=False):
        self.owner = oe.exact(owner)
        self.namespace = namespace
        self.collections = collections
        self.validate = validate
        self.document = oe.JsonStore(
            self.owner, namespace, validate,
            empty=lambda: {name: [] for name in collections},
            namespace=namespace, max_bytes=8 * 1024 * 1024,
            strict_durability=strict_durability,
        )

    def legacy(self):
        """Fingerprint but never attribute or rewrite normalized legacy values."""
        backend = store.backend()
        key = memory.safe_id(self.owner)
        result = {}
        for namespace in self.collections:
            try:
                raw = (oe.read_bytes(config.MEMORY_DIR / "store" / namespace / key,
                                     self.document.max_bytes, self.namespace)
                       if type(backend) is store.FileStore else backend.get(namespace, key))
            except oe.OwnerEvidenceStateError:
                raise
            except Exception as err:
                raise oe.OwnerEvidenceStateError(self.namespace, "legacy inventory unavailable") from err
            if raw is not None:
                if not isinstance(raw, bytes) or len(raw) > self.document.max_bytes:
                    raise oe.OwnerEvidenceStateError(self.namespace, "legacy size or type invalid")
                result[namespace] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        return result

    def status(self):
        result = self.document.status()
        try:
            result["legacy_unclaimed"] = self.legacy()
            if result["state"] == "missing" and result["legacy_unclaimed"]:
                result.update(state="unavailable", count=None, reason="unclaimed legacy state")
        except oe.OwnerEvidenceStateError as err:
            result.update(state="unavailable", count=None, reason=err.reason)
        return result

    @contextmanager
    def backend_document(self):
        backend = store.backend()
        backend_guard = (backend.owner_transaction(self.namespace, self.document.key)
                         if isinstance(backend, store.PostgresStore) else nullcontext(None))
        try:
            with self.document.guard(), backend_guard as transaction_backend:
                yield (replace(self.document, backend_override=transaction_backend)
                       if transaction_backend is not None else self.document)
        except oe.OwnerEvidenceStateError:
            raise
        except Exception as err:
            if (isinstance(err, OSError) or
                    (isinstance(backend, store.PostgresStore)
                     and isinstance(err, backend._psycopg.Error))):
                raise oe.OwnerEvidenceStateError(self.namespace,
                    "backend transaction unconfirmed; reread before retry") from err
            raise

    def initialize_empty(self, *, acknowledge_legacy=False):
        """Explicitly start NEW exact-owner state; never repair or migrate bytes."""
        if acknowledge_legacy is not True:
            raise ValueError("explicit acknowledgement of unclaimed legacy preservation required")
        with self.backend_document() as document:
            if document._raw() is not None:
                document.load()
                raise ValueError("exact-owner state already exists; initialization refused")
            before = self.legacy()
            document.save(document.empty())
            if self.legacy() != before:
                raise oe.OwnerEvidenceStateError(self.namespace, "legacy inventory changed during initialization")
            return {"state": "initialized empty", "legacy_unclaimed_preserved": before}

    def identity(self):
        return (str(config.MEMORY_DIR.absolute()), id(store.backend()),
                self.namespace, self.owner)

    @contextmanager
    def transaction(self):
        identity = self.identity()
        current = _CURRENT.get() or {}
        if identity in current:
            # Nested failures restore their in-memory savepoint even if callers
            # handle them. The outer operation remains the only publisher.
            state = current[identity]
            previous = deepcopy(state)
            try:
                yield state
            except BaseException:
                state.clear()
                state.update(previous)
                raise
            return
        with self.backend_document() as document:
            if document._raw() is None and self.legacy():
                raise oe.OwnerEvidenceStateError(self.namespace,
                    "unclaimed legacy state exists; inspect memory state-status before explicit initialize-empty")
            state = document.load()
            previous = deepcopy(state)
            token = _CURRENT.set({**current, identity: state})
            try:
                yield state
                if state != previous:
                    document.save(state)
            finally:
                _CURRENT.reset(token)

    def load(self, collection):
        current = _CURRENT.get() or {}
        if self.identity() in current:
            return deepcopy(current[self.identity()][collection])
        with self.transaction() as state:
            return deepcopy(state[collection])

    def save(self, collection, value):
        with self.transaction() as state:
            state[collection] = deepcopy(value)


def transaction(factory):
    def decorate(fn):
        @wraps(fn)
        def wrapped(user, *args, **kwargs):
            with factory(user).transaction():
                return fn(user, *args, **kwargs)
        return wrapped
    return decorate


def rows(value, cap):
    oe.records(value, cap)
    seen = set()
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("invalid row")
        oe.text(row.get("id"), 128)
        if row["id"] in seen:
            raise ValueError("duplicate row identifier")
        seen.add(row["id"])
    return value


def probability(value):
    oe.number(value)
    if value > 1:
        raise ValueError("probability exceeds one")


def timestamp(value):
    oe.number(value, minimum=-1e15)


def strings(value, cap=2000):
    oe.records(value, cap)
    for item in value:
        oe.text(item, 8192)

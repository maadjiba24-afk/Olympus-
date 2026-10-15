"""Bounded acknowledgments retained across later failures, without disk reads.

Only trusted mutation boundaries mint these facts, after the completion
barrier and required image checks return. The type alone is not proof. Facts
do not settle an interrupted operation, revalidate present disk state, attest
remote storage, or make an enclosing failed workflow successful.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

SCHEMA = 'olympus-backup-stage-ack/1'
_ID = re.compile(r'[0-9a-f]{32}\Z')
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_KINDS = frozenset(('publication', 'retirement', 'restore', 'delivery'))
MAX_FACTS = 1028
_ATTRIBUTE = '_backup_confirmed_stages'


def operation_id(value):
    return type(value) is str and bool(_ID.fullmatch(value))


def _digest(value):
    return type(value) is str and bool(_HASH.fullmatch(value))


@dataclass(frozen=True)
class ConfirmedStage:
    kind: str
    operation_id: str
    archive_sha256: str
    signature_sha256: str | None
    plan_sha256: str
    publication_id: str | None = None

    def __post_init__(self):
        if not valid(self):
            raise ValueError('invalid bounded backup stage acknowledgment')

    def report(self):
        if not valid(self):
            raise ValueError('invalid bounded backup stage acknowledgment')
        return {'schema': SCHEMA, 'kind': self.kind, 'operation_id': self.operation_id,
                'archive_sha256': self.archive_sha256, 'signature_sha256': self.signature_sha256,
                'plan_sha256': self.plan_sha256, 'publication_id': self.publication_id,
                'completion_acknowledgment_observed': True,
                'enclosing_operation_complete': False, 'current_state_revalidated': False,
                'remote_retention_verified': False}


def valid(value):
    if type(value) is not ConfirmedStage:
        return False
    try:
        return (type(value.kind) is str and value.kind in _KINDS
                and operation_id(value.operation_id) and _digest(value.archive_sha256)
                and _digest(value.plan_sha256)
                and (_digest(value.signature_sha256) or value.signature_sha256 is None and value.kind == 'restore')
                and (operation_id(value.publication_id) if value.kind == 'delivery' else value.publication_id is None))
    except AttributeError:
        return False


def _merge(*groups):
    unique = {}
    for group in groups:
        if type(group) is not tuple or len(group) > MAX_FACTS:
            return ()
        for item in group:
            if not valid(item):
                return ()
            key = item.kind, item.operation_id
            previous = unique.get(key)
            if previous is not None and previous != item:
                return ()
            unique[key] = item
            if len(unique) > MAX_FACTS:
                return ()
    return tuple(unique.values())


def _attributes(error):
    if not isinstance(error, BaseException):
        return None
    try:
        attributes = BaseException.__getattribute__(error, '__dict__')
        return attributes if type(attributes) is dict else None
    except Exception:
        return None


def observed(error):
    """Only exact typed, bounded facts can cross a public error boundary."""
    attributes = _attributes(error)
    return () if attributes is None else _merge(attributes.get(_ATTRIBUTE, ()))


def carry(source, target, additional=()):
    """Deduplicate bounded inputs before enforcing the unique-fact bound."""
    attributes = _attributes(target)
    if attributes is not None:
        attributes[_ATTRIBUTE] = _merge(observed(source), _merge(additional))
        source_attributes = _attributes(source)
        for name in ('_preference_cleanup_unconfirmed', '_native_cleanup_unconfirmed'):
            if source_attributes is not None and source_attributes.get(name) is True:
                attributes[name] = True


def retain_id(error, fallback):
    """Preserve an observed recovery ID, including on system interruptions."""
    attributes = _attributes(error)
    if attributes is not None:
        existing = attributes.get('operation_id')
        attributes['operation_id'] = existing if operation_id(existing) else fallback if operation_id(fallback) else None



def accumulate(current, item):
    """Keep a root's acknowledgments bounded and deduplicated during its lease.

    Public mutation paths have their own smaller operation/history bounds.
    An invalid private caller cannot silently erase earlier acknowledged facts.
    """
    merged = _merge(current, (item,))
    if not merged:
        raise ValueError('backup acknowledgment accumulator is invalid or full')
    return merged

"""Explicit vault changes with retained ciphertext and resumable receipts.

The caller holds one captured vault transaction and supplies the exact owner
and a target-authentication function. No key lookup, environment mutation,
signing operation, provider call or automatic migration occurs in this module.
"""
from __future__ import annotations

import hashlib
import json
import re

from . import vault_codec, vault_records, vault_storage, native_cleanup

MAX_RECEIPT = 16 * 1024
MAX_RECEIPT_PLAINTEXT = 4 * 1024
_NS = 'vault_recovery'
_FIELDS = {'schema', 'operation_id', 'kind', 'owner_sha256', 'source_key',
           'target_key', 'source_sha256', 'previous_sha256', 'target_sha256',
           'entries_changed'}


class RecoveryError(RuntimeError):
    def __init__(self, operation_id, message='vault change requires recovery; ciphertext was retained'):
        super().__init__(message)
        self.operation_id = operation_id


def _id(value):
    if type(value) is not str or re.fullmatch('[0-9a-f]{32}', value) is None:
        raise RecoveryError(None, 'invalid vault recovery operation identifier')
    return value


def _key(operation_id, part):
    return 'change-' + _id(operation_id) + '-' + part


def _hash(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def _owner(owner):
    vault_records.owner_context(owner)
    return hashlib.sha256(owner.encode('utf-8')).hexdigest()


def _serialize(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'),
                     ensure_ascii=True, allow_nan=False).encode('ascii')
    if len(raw) > MAX_RECEIPT_PLAINTEXT:
        raise RecoveryError(None, 'vault recovery receipt exceeds its bound')
    return raw


def _context(owner, operation_id, stage):
    identity = bytes.fromhex(_owner(owner)) if owner is not None else b'discard-operator'
    return (b'olympus-vault-recovery-v1\x00' + identity + b'\x00'
            + _id(operation_id).encode('ascii') + b'\x00' + stage.encode('ascii'))


def _seal(raw, passphrase, owner, operation_id, stage):
    return vault_codec.encrypt(raw, passphrase, context=_context(owner, operation_id, stage),
                               max_plaintext=MAX_RECEIPT_PLAINTEXT)


def _open(raw, passphrase, owner, operation_id, stage):
    return vault_codec.decrypt(raw, passphrase, context=_context(owner, operation_id, stage),
                               max_plaintext=MAX_RECEIPT_PLAINTEXT).data


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate receipt field')
        result[key] = value
    return result


def _validate(plan, operation_id):
    if (type(plan) is not dict or set(plan) != _FIELDS
            or type(plan['schema']) is not int or plan['schema'] != 1
            or plan['operation_id'] != _id(operation_id)
            or plan['kind'] not in ('migrate', 'rotate', 'discard')
            or type(plan['entries_changed']) is not int
            or not 0 <= plan['entries_changed'] <= vault_records.MAX_ENTRIES):
        raise RecoveryError(operation_id, 'invalid vault recovery receipt')
    for name in ('owner_sha256', 'source_sha256', 'previous_sha256', 'target_sha256'):
        value = plan[name]
        if value is not None and (type(value) is not str or re.fullmatch('[0-9a-f]{64}', value) is None):
            raise RecoveryError(operation_id, 'invalid vault recovery digest')
    vault_storage._address('vault', plan['source_key'])
    if plan['source_sha256'] is None:
        raise RecoveryError(operation_id, 'missing vault recovery source')
    if plan['kind'] == 'discard':
        if any(plan[name] is not None for name in
               ('owner_sha256', 'target_key', 'previous_sha256', 'target_sha256')):
            raise RecoveryError(operation_id, 'invalid discard receipt')
    else:
        vault_storage._address('vault', plan['target_key'])
        if plan['owner_sha256'] is None or plan['target_sha256'] is None:
            raise RecoveryError(operation_id, 'missing vault recovery destination')
        if plan['kind'] == 'rotate':
            if (plan['source_key'] != plan['target_key']
                    or plan['source_sha256'] != plan['previous_sha256']):
                raise RecoveryError(operation_id, 'invalid rotation receipt')
        elif plan['source_key'] == plan['target_key']:
            raise RecoveryError(operation_id, 'migration source and destination coincide')
    return plan


def _verify(callback, blob, operation_id):
    if not callable(callback) or callback(blob) is not True:
        raise RecoveryError(operation_id, 'vault target authentication was not confirmed')


def prepare(tx, passphrase, *, operation_id, kind, source_key, source_blob, target_key=None,
            previous_blob=None, target_blob=None, owner=None, entries_changed=0,
            verify_target=None):
    """Retain immutable encrypted before/after images, then the prepared receipt.

    A receipt is deliberately a separate stage: an interrupted preparation
    leaves its partial copies untouched and is never treated as published.
    The caller must retain an explicit operation_id before entering this call,
    including when KeyboardInterrupt/SystemExit aborts a durable stage. The
    target verifier must return the singleton True after authentication; every
    other result or raised error refuses the change.
    """
    operation_id = _id(operation_id)
    try:
        for value in (source_blob, previous_blob, target_blob):
            if value is not None and (type(value) is not bytes or len(value) > vault_storage.MAX_BLOB):
                raise ValueError('invalid recovery ciphertext')
        plan = _validate({'schema': 1, 'operation_id': operation_id, 'kind': kind,
            'owner_sha256': _owner(owner) if owner is not None else None,
            'source_key': source_key, 'target_key': target_key,
            'source_sha256': _hash(source_blob), 'previous_sha256': _hash(previous_blob),
            'target_sha256': _hash(target_blob), 'entries_changed': entries_changed}, operation_id)
        if tx.get('vault', source_key) != source_blob:
            raise RecoveryError(operation_id, 'vault source changed before preparation')
        if kind != 'discard':
            if tx.get('vault', target_key) != previous_blob or not callable(verify_target):
                raise RecoveryError(operation_id, 'vault destination changed before preparation')
            _verify(verify_target, target_blob, operation_id)
            if (tx.get('vault', source_key) != source_blob
                    or tx.get('vault', target_key) != previous_blob):
                raise RecoveryError(operation_id, 'vault rows changed during target authentication')
        for label, blob in (('source', source_blob), ('previous', previous_blob), ('target', target_blob)):
            if blob is not None:
                tx.write_new(_NS, _key(operation_id, label), blob)
        receipt = _seal(_serialize(plan), passphrase, owner, operation_id, 'prepared')
        tx.write_new(_NS, _key(operation_id, 'prepared'), receipt)
        return operation_id
    except RecoveryError:
        raise
    except Exception as exc:
        error = RecoveryError(operation_id)
        if native_cleanup.unknown(exc):
            native_cleanup.mark(error)
        raise error from None


def _load(tx, passphrase, owner, operation_id):
    raw = tx.get(_NS, _key(operation_id, 'prepared'), MAX_RECEIPT)
    if raw is None:
        raise RecoveryError(operation_id, 'vault change has no complete prepared receipt')
    try:
        plaintext = _open(raw, passphrase, owner, operation_id, 'prepared')
        plan = json.loads(plaintext.decode('ascii'), object_pairs_hook=_unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        _validate(plan, operation_id)
        if _serialize(plan) != plaintext:
            raise ValueError('noncanonical receipt')
    except Exception:
        raise RecoveryError(operation_id, 'invalid vault recovery receipt') from None
    copies = {}
    for label, field in (('source', 'source_sha256'), ('previous', 'previous_sha256'), ('target', 'target_sha256')):
        blob = tx.get(_NS, _key(operation_id, label))
        if _hash(blob) != plan[field]:
            raise RecoveryError(operation_id, 'vault recovery ciphertext is missing or changed')
        copies[label] = blob
    return raw, plan, copies


def _marker(tx, passphrase, owner, operation_id, stage, prepared, *, create=False):
    expected = _serialize({'schema': 1, 'operation_id': operation_id,
                           'stage': stage, 'prepared_sha256': _hash(prepared)})
    key = _key(operation_id, stage)
    actual = tx.get(_NS, key, MAX_RECEIPT)
    if actual is not None and _open(actual, passphrase, owner, operation_id, stage) != expected:
        raise RecoveryError(operation_id, 'vault recovery stage receipt conflicts')
    if actual is None and create:
        tx.write_new(_NS, key, _seal(expected, passphrase, owner, operation_id, stage))
    return actual is not None


def apply(tx, passphrase, operation_id, *, owner=None, verify_target=None):
    """Finish exactly the recorded change, or refuse a conflicting current row.

    Retrying an explicit operation ID never repeats a provider action or
    creates a new ciphertext. Committed means this operation committed in the
    past, not that no later authorized write has changed the row.
    """
    operation_id = _id(operation_id)
    try:
        prepared, plan, copies = _load(tx, passphrase, owner, operation_id)
        kind = plan['kind']
        if kind != 'discard':
            if _owner(owner) != plan['owner_sha256'] or not callable(verify_target):
                raise RecoveryError(operation_id, 'vault recovery owner does not match')
            _verify(verify_target, copies['target'], operation_id)
        elif owner is not None:
            raise RecoveryError(operation_id, 'discard does not assign a vault owner')
        committed = _marker(tx, passphrase, owner, operation_id, 'committed', prepared)
        published = _marker(tx, passphrase, owner, operation_id, 'published', prepared)
        if committed:
            if not published:
                raise RecoveryError(operation_id, 'committed receipt lacks publication evidence')
            return {'operation_id': operation_id, 'committed': True,
                    'entries_changed': plan['entries_changed'], 'already_committed': True}
        source = tx.get('vault', plan['source_key'])
        if kind == 'discard':
            if _hash(source) not in (plan['source_sha256'], None):
                raise RecoveryError(operation_id, 'vault source changed after preparation')
            if published and source is not None:
                raise RecoveryError(operation_id, 'discarded vault source was restored')
            if source is not None:
                tx.delete('vault', plan['source_key'])
            if tx.get('vault', plan['source_key']) is not None:
                raise RecoveryError(operation_id, 'vault discard could not be verified')
        else:
            current = tx.get('vault', plan['target_key'])
            current_hash = _hash(current)
            if current_hash not in (plan['previous_sha256'], plan['target_sha256']):
                raise RecoveryError(operation_id, 'vault destination changed after preparation')
            if kind == 'migrate' and (_hash(source) not in (plan['source_sha256'], None)
                    or source is None and current_hash != plan['target_sha256']):
                raise RecoveryError(operation_id, 'vault source changed after preparation')
            if current_hash != plan['target_sha256']:
                if published:
                    raise RecoveryError(operation_id, 'published vault destination was rolled back')
                tx.put('vault', plan['target_key'], copies['target'])
            observed = tx.get('vault', plan['target_key'])
            if _hash(observed) != plan['target_sha256']:
                raise RecoveryError(operation_id, 'vault destination verification failed')
            _verify(verify_target, observed, operation_id)
            # A verifier is application code, not authority to change the
            # prepared operation. Recheck the actual rows after it returns.
            if _hash(tx.get('vault', plan['target_key'])) != plan['target_sha256']:
                raise RecoveryError(operation_id, 'vault destination changed during authentication')
            if kind == 'migrate' and tx.get('vault', plan['source_key']) != source:
                raise RecoveryError(operation_id, 'vault source changed during authentication')
            _marker(tx, passphrase, owner, operation_id, 'published', prepared, create=True)
            if kind == 'migrate' and source is not None:
                if (tx.get('vault', plan['source_key']) != source
                        or _hash(tx.get('vault', plan['target_key'])) != plan['target_sha256']):
                    raise RecoveryError(operation_id, 'vault rows changed before legacy removal')
                tx.delete('vault', plan['source_key'])
                if tx.get('vault', plan['source_key']) is not None:
                    raise RecoveryError(operation_id, 'legacy vault removal could not be verified')
        _marker(tx, passphrase, owner, operation_id, 'published', prepared, create=True)
        _marker(tx, passphrase, owner, operation_id, 'committed', prepared, create=True)
        return {'operation_id': operation_id, 'committed': True,
                'entries_changed': plan['entries_changed'], 'already_committed': False}
    except RecoveryError:
        raise
    except Exception as exc:
        error = RecoveryError(operation_id)
        if native_cleanup.unknown(exc):
            native_cleanup.mark(error)
        raise error from None

"""Owned ciphertext recovery state transitions; no credentials or providers."""
import hashlib
import uuid

import pytest

from olympus import store, vault_recovery as recovery, vault_storage

PASSWORD = b'owned-recovery-fixture-only'


def verified(blob):
    assert blob == b'owned-new-authenticated-ciphertext'
    return True


def prepare(tx, kind='migrate', operation_id=None):
    operation_id = operation_id if operation_id is not None else uuid.uuid4().hex
    tx.put('vault', 'legacy', b'owned-old-ciphertext')
    target = 'legacy' if kind == 'rotate' else 'exact-owner'
    previous = b'owned-old-ciphertext' if kind == 'rotate' else None
    return recovery.prepare(tx, PASSWORD, kind=kind, source_key='legacy',
        source_blob=b'owned-old-ciphertext', target_key=target,
        previous_blob=previous, target_blob=b'owned-new-authenticated-ciphertext',
        owner='exact-owner', entries_changed=1, verify_target=verified,
        operation_id=operation_id)


def test_migration_preserves_encrypted_copies_and_immutable_receipts(tmp_path):
    with vault_storage.transaction(store.FileStore(), tmp_path) as tx:
        operation_id = prepare(tx)
        assert tx.get('vault', 'legacy') == b'owned-old-ciphertext'
        assert tx.get('vault', 'exact-owner') is None
        result = recovery.apply(tx, PASSWORD, operation_id, owner='exact-owner', verify_target=verified)
        assert result['committed'] and result['entries_changed'] == 1
        assert tx.get('vault', 'legacy') is None
        assert tx.get('vault', 'exact-owner') == b'owned-new-authenticated-ciphertext'
        copies = {key: tx.get('vault_recovery', key) for key in tx.keys('vault_recovery')}
        assert copies[recovery._key(operation_id, 'source')] == b'owned-old-ciphertext'
        assert copies[recovery._key(operation_id, 'target')] == b'owned-new-authenticated-ciphertext'
        assert recovery.apply(tx, PASSWORD, operation_id, owner='exact-owner', verify_target=verified)['already_committed']
        assert copies == {key: tx.get('vault_recovery', key) for key in tx.keys('vault_recovery')}


def test_lost_publication_ack_resumes_exact_ciphertext(tmp_path, monkeypatch):
    with vault_storage.transaction(store.FileStore(), tmp_path) as tx:
        operation_id = prepare(tx)
        original = tx.put
        def publish_then_fail(namespace, key, blob):
            original(namespace, key, blob)
            raise OSError('owned simulated lost acknowledgment')
        monkeypatch.setattr(tx, 'put', publish_then_fail)
        with pytest.raises(recovery.RecoveryError) as failure:
            recovery.apply(tx, PASSWORD, operation_id, owner='exact-owner', verify_target=verified)
        assert failure.value.operation_id == operation_id
        assert tx.get('vault', 'legacy') == b'owned-old-ciphertext'
        monkeypatch.setattr(tx, 'put', lambda *a: pytest.fail('resume published twice'))
        assert recovery.apply(tx, PASSWORD, operation_id, owner='exact-owner', verify_target=verified)['committed']


def test_lost_source_deletion_ack_resumes_without_redeleting(tmp_path, monkeypatch):
    with vault_storage.transaction(store.FileStore(), tmp_path) as tx:
        operation_id = prepare(tx)
        original = tx.delete
        def delete_then_fail(namespace, key):
            original(namespace, key)
            raise OSError('owned simulated lost deletion acknowledgment')
        monkeypatch.setattr(tx, 'delete', delete_then_fail)
        with pytest.raises(recovery.RecoveryError):
            recovery.apply(tx, PASSWORD, operation_id, owner='exact-owner', verify_target=verified)
        monkeypatch.setattr(tx, 'delete', lambda *a: pytest.fail('resume deleted twice'))
        assert recovery.apply(tx, PASSWORD, operation_id, owner='exact-owner', verify_target=verified)['committed']


@pytest.mark.parametrize('changed', ['legacy', 'exact-owner'])
def test_concurrent_change_refuses_and_preserves_both_rows(tmp_path, changed):
    with vault_storage.transaction(store.FileStore(), tmp_path) as tx:
        operation_id = prepare(tx)
        tx.put('vault', changed, b'owned-later-change')
        before = {key: tx.get('vault', key) for key in tx.keys('vault')}
        with pytest.raises(recovery.RecoveryError, match='changed after preparation'):
            recovery.apply(tx, PASSWORD, operation_id, owner='exact-owner', verify_target=verified)
        assert before == {key: tx.get('vault', key) for key in tx.keys('vault')}


def test_rotation_preserves_old_and_new_ciphertext(tmp_path):
    with vault_storage.transaction(store.FileStore(), tmp_path) as tx:
        operation_id = prepare(tx, kind='rotate')
        assert recovery.apply(tx, PASSWORD, operation_id, owner='exact-owner', verify_target=verified)['committed']
        assert tx.get('vault', 'legacy') == b'owned-new-authenticated-ciphertext'
        assert tx.get('vault_recovery', recovery._key(operation_id, 'source')) == b'owned-old-ciphertext'


def test_discard_preserves_unreadable_ciphertext_for_recovery(tmp_path):
    with vault_storage.transaction(store.FileStore(), tmp_path) as tx:
        tx.put('vault', 'legacy', b'owned-unreadable-ciphertext')
        operation_id = recovery.prepare(tx, PASSWORD, operation_id=uuid.uuid4().hex,
                                        kind='discard', source_key='legacy',
                                        source_blob=b'owned-unreadable-ciphertext')
        assert recovery.apply(tx, PASSWORD, operation_id)['committed']
        assert tx.get('vault', 'legacy') is None
        assert tx.get('vault_recovery', recovery._key(operation_id, 'source')) == b'owned-unreadable-ciphertext'


def test_wrong_owner_and_changed_recovery_copy_refuse(tmp_path):
    with vault_storage.transaction(store.FileStore(), tmp_path) as tx:
        operation_id = prepare(tx)
        with pytest.raises(recovery.RecoveryError):
            recovery.apply(tx, PASSWORD, operation_id, owner='different-owner', verify_target=verified)
        tx.put('vault_recovery', recovery._key(operation_id, 'target'), b'changed')
        with pytest.raises(recovery.RecoveryError, match='missing or changed'):
            recovery.apply(tx, PASSWORD, operation_id, owner='exact-owner', verify_target=verified)
        assert tx.get('vault', 'legacy') == b'owned-old-ciphertext'


def test_committed_receipt_never_overwrites_a_later_authorized_write(tmp_path):
    with vault_storage.transaction(store.FileStore(), tmp_path) as tx:
        operation_id = prepare(tx)
        recovery.apply(tx, PASSWORD, operation_id, owner='exact-owner', verify_target=verified)
        tx.put('vault', 'exact-owner', b'owned-later-write')
        assert recovery.apply(tx, PASSWORD, operation_id, owner='exact-owner', verify_target=verified)['already_committed']
        assert tx.get('vault', 'exact-owner') == b'owned-later-write'


def test_partial_preparation_is_retained_and_never_treated_as_ready(tmp_path, monkeypatch):
    with vault_storage.transaction(store.FileStore(), tmp_path) as tx:
        original = tx.write_new
        operation_id = 'f' * 32
        def fail_receipt(namespace, key, value):
            if key.endswith('-prepared'):
                raise OSError('owned receipt failure')
            original(namespace, key, value)
        monkeypatch.setattr(tx, 'write_new', fail_receipt)
        with pytest.raises(recovery.RecoveryError) as failure:
            prepare(tx, operation_id=operation_id)
        assert failure.value.operation_id == operation_id
        saved = tx.get('vault_recovery', recovery._key(operation_id, 'source'))
        assert hashlib.sha256(saved).hexdigest() == hashlib.sha256(b'owned-old-ciphertext').hexdigest()
        with pytest.raises(recovery.RecoveryError, match='no complete prepared receipt'):
            recovery.apply(tx, PASSWORD, operation_id, owner='exact-owner', verify_target=verified)
        assert tx.get('vault', 'legacy') == b'owned-old-ciphertext'


@pytest.mark.parametrize('stage', ['prepared', 'published', 'committed'])
def test_changed_receipt_refuses_without_changing_active_vault(tmp_path, stage):
    with vault_storage.transaction(store.FileStore(), tmp_path) as tx:
        operation_id = prepare(tx)
        if stage != 'prepared':
            recovery.apply(tx, PASSWORD, operation_id, owner='exact-owner', verify_target=verified)
        key = recovery._key(operation_id, stage)
        raw = bytearray(tx.get('vault_recovery', key))
        raw[-15] ^= 1
        tx.put('vault_recovery', key, bytes(raw))
        before = {key: tx.get('vault', key) for key in tx.keys('vault')}
        with pytest.raises(recovery.RecoveryError):
            recovery.apply(tx, PASSWORD, operation_id, owner='exact-owner', verify_target=verified)
        assert before == {key: tx.get('vault', key) for key in tx.keys('vault')}


def test_receipt_cannot_be_relabelled_as_another_stage_or_operation(tmp_path):
    with vault_storage.transaction(store.FileStore(), tmp_path) as tx:
        operation_id = prepare(tx)
        prepared = tx.get('vault_recovery', recovery._key(operation_id, 'prepared'))
        tx.write_new('vault_recovery', recovery._key(operation_id, 'committed'), prepared)
        with pytest.raises(recovery.RecoveryError):
            recovery.apply(tx, PASSWORD, operation_id, owner='exact-owner', verify_target=verified)
        other = 'c' * 32
        tx.write_new('vault_recovery', recovery._key(other, 'prepared'), prepared)
        with pytest.raises(recovery.RecoveryError):
            recovery.apply(tx, PASSWORD, other, owner='exact-owner', verify_target=verified)
        assert tx.get('vault', 'legacy') == b'owned-old-ciphertext'


@pytest.mark.parametrize('changed', ['legacy', 'exact-owner'])
def test_verifier_cannot_change_live_rows_then_authorize_source_deletion(tmp_path, changed):
    with vault_storage.transaction(store.FileStore(), tmp_path) as tx:
        operation_id = prepare(tx)
        calls = []
        def changes_row(blob):
            verified(blob)
            calls.append(True)
            if len(calls) == 2:
                tx.put('vault', changed, b'owned-callback-change')
            return True
        with pytest.raises(recovery.RecoveryError, match='changed during authentication'):
            recovery.apply(tx, PASSWORD, operation_id, owner='exact-owner', verify_target=changes_row)
        assert len(calls) == 2
        assert tx.get('vault', 'legacy') == (
            b'owned-callback-change' if changed == 'legacy' else b'owned-old-ciphertext')
        assert tx.get('vault_recovery', recovery._key(operation_id, 'committed')) is None
        assert tx.get('vault_recovery', recovery._key(operation_id, 'published')) is None


@pytest.mark.parametrize('result', [False, None, 1, 'yes'])
def test_verifier_requires_explicit_true(tmp_path, result):
    with vault_storage.transaction(store.FileStore(), tmp_path) as tx:
        operation_id = prepare(tx)
        with pytest.raises(recovery.RecoveryError, match='authentication was not confirmed'):
            recovery.apply(tx, PASSWORD, operation_id, owner='exact-owner',
                           verify_target=lambda blob: result)
        assert tx.get('vault', 'legacy') == b'owned-old-ciphertext'
        assert tx.get('vault', 'exact-owner') is None


@pytest.mark.parametrize('stage', ['source', 'previous', 'target', 'prepared', 'published', 'committed'])
@pytest.mark.parametrize('fault', [OSError, KeyboardInterrupt, SystemExit])
def test_explicit_operation_id_survives_interrupted_durable_stage(tmp_path, monkeypatch, stage, fault):
    operation_id = uuid.uuid4().hex  # retained by the caller before any mutation
    with vault_storage.transaction(store.FileStore(), tmp_path) as tx:
        original = tx.write_new
        def interrupted(namespace, key, value):
            original(namespace, key, value)
            if key == recovery._key(operation_id, stage):
                raise fault('owned interruption after durable write')
        with monkeypatch.context() as patch:
            patch.setattr(tx, 'write_new', interrupted)
            expected = recovery.RecoveryError if fault is OSError else fault
            with pytest.raises(expected):
                prepare(tx, kind='rotate', operation_id=operation_id)
                recovery.apply(tx, PASSWORD, operation_id, owner='exact-owner', verify_target=verified)
        assert tx.get('vault_recovery', recovery._key(operation_id, stage)) is not None
        if stage in ('source', 'previous', 'target'):
            with pytest.raises(recovery.RecoveryError, match='no complete prepared receipt'):
                recovery.apply(tx, PASSWORD, operation_id, owner='exact-owner', verify_target=verified)
            assert tx.get('vault', 'legacy') == b'owned-old-ciphertext'
        else:
            assert recovery.apply(tx, PASSWORD, operation_id, owner='exact-owner', verify_target=verified)['committed']
            assert tx.get('vault', 'legacy') == b'owned-new-authenticated-ciphertext'
        assert tx.get('vault_recovery', recovery._key(operation_id, 'source')) == b'owned-old-ciphertext'


def test_real_owner_bound_rotation_and_old_key_recovery(tmp_path):
    from olympus import memory, vault_codec, vault_records
    owner = 'owned-recovery:exact-owner'
    key = memory.storage_key(owner)
    entries = {'credential': {'token': 'owned-synthetic-token'}}
    old_key, new_key = b'owned-old-passphrase', b'owned-new-passphrase'
    before = vault_records.encode(owner, entries, old_key)
    after = vault_records.encode(owner, entries, new_key)
    operation_id = uuid.uuid4().hex
    def authenticate(blob):
        decoded, metadata = vault_records.decode(owner, blob, new_key)
        assert decoded == entries and metadata.context_authenticated
        return True
    with vault_storage.transaction(store.FileStore(), tmp_path) as tx:
        tx.put('vault', key, before)
        recovery.prepare(tx, new_key, operation_id=operation_id, kind='rotate',
                         source_key=key, source_blob=before, target_key=key,
                         previous_blob=before, target_blob=after, owner=owner,
                         entries_changed=1, verify_target=authenticate)
        assert recovery.apply(tx, new_key, operation_id, owner=owner, verify_target=authenticate)['committed']
        current = tx.get('vault', key)
        assert authenticate(current)
        with pytest.raises(vault_codec.EnvelopeError):
            vault_records.decode(owner, current, old_key)
        retained = tx.get('vault_recovery', recovery._key(operation_id, 'source'))
        assert retained == before and vault_records.decode(owner, retained, old_key)[0] == entries
        with pytest.raises(recovery.RecoveryError):
            recovery.apply(tx, old_key, operation_id, owner=owner, verify_target=authenticate)

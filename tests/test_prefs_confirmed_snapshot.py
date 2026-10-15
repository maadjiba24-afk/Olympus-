"""Historical preference image handoff controls, with owned native storage."""
from dataclasses import fields
import hashlib
import threading

import pytest

from olympus import config, prefs, prefs_native as native
from test_prefs_pending import OWNER, owned, _path, _pending


def _files(root):
    io_root = prefs._io(root)
    return {p.relative_to(io_root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in io_root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('present', [False, True])
def test_confirmed_handoff_preserves_every_other_field_without_io(owned, monkeypatch, tmp_path, present):
    pending, calls = _pending(owned, present)
    prefs.settle_mutation(OWNER, pending)
    wrong_root = tmp_path / 'unselected-root'
    monkeypatch.setattr(config, 'MEMORY_DIR', wrong_root)

    def deny(*args, **kwargs):
        pytest.fail('historical handoff performed filesystem acquisition or recapture')

    monkeypatch.setattr(native, 'transaction', deny)
    monkeypatch.setattr(native.storage._Directory, '__init__', deny)
    monkeypatch.setattr(prefs, 'capture', deny)
    snapshot = prefs._confirmed_mutation_snapshot(pending)
    assert type(snapshot) is prefs.Snapshot and snapshot is not pending.before
    assert snapshot.raw == pending.after and snapshot.identity == pending._attempt.confirmed_identity
    for item in fields(snapshot):
        if item.name not in ('raw', 'identity'):
            assert getattr(snapshot, item.name) == getattr(pending.before, item.name)
    assert snapshot.data()['scope'] == 'after' and calls == [True]
    assert not wrong_root.exists()


def test_prepared_only_intent_cannot_supply_a_confirmed_image(owned):
    pending, _calls = _pending(owned)
    before = _files(owned)
    with pytest.raises(prefs.PreferenceConflict):
        prefs._confirmed_mutation_snapshot(pending)
    assert _files(owned) == before


@pytest.mark.parametrize('fault_type', [OSError, KeyboardInterrupt, SystemExit])
def test_lost_ack_requires_original_settlement_before_handoff(owned, monkeypatch, fault_type):
    pending, calls = _pending(owned)
    real_publish = native.Transaction.publish
    fault = fault_type('owned-private-lost-ack')
    fired = []

    def lose_ack(tx, parts, raw):
        real_publish(tx, parts, raw)
        fired.append(True)
        raise fault

    with monkeypatch.context() as patch:
        patch.setattr(native.Transaction, 'publish', lose_ack)
        with pytest.raises(prefs.PreferenceOperationError if fault_type is OSError else fault_type):
            prefs.settle_mutation(OWNER, pending)
    assert fired == [True] and _path(owned).read_bytes() == pending.after
    with pytest.raises(prefs.PreferenceConflict):
        prefs._confirmed_mutation_snapshot(pending)
    with monkeypatch.context() as patch:
        patch.setattr(native.Transaction, 'publish', lambda *a: pytest.fail('handoff settlement republished'))
        prefs.settle_mutation(OWNER, pending)
    after = prefs._confirmed_mutation_snapshot(pending)
    assert after.raw == pending.after and calls == [True]
    assert prefs._same_evidence(after, prefs.capture(OWNER))


@pytest.mark.parametrize('fault_type', [OSError, KeyboardInterrupt, SystemExit])
def test_confirmed_publication_with_cleanup_uncertainty_cannot_handoff(owned, monkeypatch, fault_type):
    pending, _calls = _pending(owned)
    real_close = native.Transaction.close
    fault = fault_type('owned-private-close-uncertainty')
    closed = []

    def fail_close(tx):
        real_close(tx)
        if not closed:
            closed.append(True)
            raise fault

    with monkeypatch.context() as patch:
        patch.setattr(native.Transaction, 'close', fail_close)
        with pytest.raises(prefs.PreferenceOperationError if fault_type is OSError else fault_type) as observed:
            prefs.settle_mutation(OWNER, pending)
        if fault_type is not OSError:
            assert observed.value is fault
    assert closed == [True]
    assert pending._attempt.confirmed and pending._attempt.cleanup_unconfirmed
    with pytest.raises(prefs.PreferenceOperationError):
        prefs._confirmed_mutation_snapshot(pending)


def test_handoff_requires_original_executor(owned):
    pending, _calls = _pending(owned)
    prefs.settle_mutation(OWNER, pending)
    outcomes = []

    def foreign():
        try:
            prefs._confirmed_mutation_snapshot(pending)
        except Exception as error:
            outcomes.append(type(error))

    worker = threading.Thread(target=foreign)
    worker.start()
    worker.join(10)
    assert not worker.is_alive() and outcomes == [native.PreferenceStorageError]
    assert prefs._confirmed_mutation_snapshot(pending).raw == pending.after


@pytest.mark.parametrize('changed', ['current', 'same-bytes-inode', 'legacy', 'recovery', 'root', 'locks', 'lock'])
def test_later_cas_refuses_changed_evidence_without_rebase_or_transform(owned, monkeypatch, changed):
    pending, _calls = _pending(owned)
    prefs.settle_mutation(OWNER, pending)
    historical = prefs._confirmed_mutation_snapshot(pending)
    retained = None
    if changed in ('current', 'same-bytes-inode', 'legacy'):
        with native.transaction(owned) as tx:
            if changed == 'legacy':
                tx.publish(historical.legacy_parts, b'{"autonomy":0}')
            else:
                tx.publish(historical.current_parts, historical.raw if changed == 'same-bytes-inode' else b'{"scope":"revoked"}')
    elif changed == 'recovery':
        other = 'owned-unrelated-recovery'
        with native.transaction(owned) as tx:
            tx.publish(prefs._relative_paths(other)[0], b'{owned-corrupt')
        prefs.repair(other, operation_id='7' * 32)
    else:
        path = {'root': owned, 'locks': owned / 'locks', 'lock': owned / 'locks' / native._LOCK_NAME}[changed]
        retained = path.with_name(path.name + '-retained')
        path.rename(retained)
        if changed == 'lock':
            path.write_bytes(b'owned-replacement')
        else:
            path.mkdir()
    before = _files(owned)
    retained_before = (None if retained is None else
                       hashlib.sha256(retained.read_bytes()).hexdigest() if retained.is_file() else _files(retained))
    # Producing a historical image is still permitted. It must never refresh
    # the old observation into the changed live policy or replacement root.
    again = prefs._confirmed_mutation_snapshot(pending)
    assert prefs._same_evidence(again, historical)
    monkeypatch.setattr(native.Transaction, 'publish', lambda *a: pytest.fail('stale image published'))
    with pytest.raises((prefs.PreferenceConflict, prefs.PreferenceOperationError, prefs.PreferencesStateError)):
        prefs.prepare_mutation(OWNER, lambda *a: pytest.fail('stale image transform reached'), captured=again)
    assert _files(owned) == before
    if retained is not None:
        assert (hashlib.sha256(retained.read_bytes()).hexdigest() if retained.is_file() else _files(retained)) == retained_before


def test_handoff_during_transform_is_refused_without_new_publication(owned):
    pending, _calls = _pending(owned)
    prefs.settle_mutation(OWNER, pending)
    before = _files(owned)

    def nested(data):
        prefs._confirmed_mutation_snapshot(pending)
        return data

    with pytest.raises(prefs.PreferenceConflict):
        prefs.mutate(OWNER, nested)
    assert _files(owned) == before



def test_historical_image_can_authorize_only_the_next_exact_cas(owned, tmp_path, monkeypatch):
    pending, _calls = _pending(owned)
    prefs.settle_mutation(OWNER, pending)
    image = prefs._confirmed_mutation_snapshot(pending)
    foreign = tmp_path / 'unselected-next-root'
    monkeypatch.setattr(config, 'MEMORY_DIR', foreign)
    calls = []

    def next_transition(data):
        assert data == {'scope': 'after'}
        calls.append(True)
        data['scope'] = 'next'
        return data

    next_pending = prefs.prepare_mutation(OWNER, next_transition, captured=image)
    assert prefs._same_evidence(next_pending.before, image)
    assert prefs.settle_mutation(OWNER, next_pending)['publication_confirmed']
    next_image = prefs._confirmed_mutation_snapshot(next_pending)
    assert next_image.data() == {'scope': 'next'} and calls == [True]
    assert next_image.binding == image.binding and _path(owned).read_bytes() == next_image.raw
    assert not foreign.exists()

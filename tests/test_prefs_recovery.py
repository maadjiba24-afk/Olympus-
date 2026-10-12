"""Owned recovery state-machine controls; no live or credential operations."""
import hashlib
import threading

import pytest

from olympus import config, prefs, prefs_native as native, prefs_records as codec
from olympus import prefs_recovery_engine as engine, prefs_recovery_records as records

OWNER = 'owned-recovery-principal'
LEGACY = 'owned-recovery-principal'
OP = '1' * 32
NEXT = '2' * 32


@pytest.fixture
def owned(tmp_path, monkeypatch):
    root = tmp_path / 'owned-recovery'
    root.mkdir()
    monkeypatch.setattr(config, 'MEMORY_DIR', root)
    monkeypatch.setattr(native, '_GATE', threading.RLock())
    monkeypatch.setattr(native, '_LOCAL', threading.local())
    monkeypatch.setattr(native, '_IN_FLIGHT', False)
    monkeypatch.setattr(native, '_POISONED', False)
    monkeypatch.setattr(prefs, '_TRANSFORM', threading.local())
    return root


def _write(root, parts, raw):
    with native.transaction(root) as tx:
        tx.publish(parts, raw)
        return tx.read(parts)


def _read(root, parts):
    with native.transaction(root) as tx:
        return tx.read(parts)


def _setup(root, kind):
    current, legacy = prefs._relative_paths(OWNER)
    _write(root, current, b'{"autonomy":0,"keep":"before"}')
    if kind == 'repair':
        _write(root, current, b'{owned-corrupt-private-value')
    else:
        _write(root, legacy, b'{"autonomy":4,"add":true,"remove":null}')
    return current, legacy


def _call(kind, identifier=OP, *, overwrite=False):
    if kind == 'repair':
        return prefs.repair(OWNER, operation_id=identifier)
    if kind == 'discard':
        return prefs.discard_legacy(LEGACY, operation_id=identifier)
    return prefs.migrate_legacy(LEGACY, OWNER, overwrite=overwrite, operation_id=identifier)


def _unarmed(root, monkeypatch, kind):
    _setup(root, kind)
    with monkeypatch.context() as patch:
        def stop(*args, **kwargs):
            raise OSError('owned-plan-retained')
        patch.setattr(engine, 'settle', stop)
        with pytest.raises(prefs.PreferenceOperationError) as error:
            _call(kind)
        assert error.value.operation_id == OP
    with native.transaction(root) as tx:
        journal = prefs._journal(tx)
        loaded = journal.read_plan(OP)
        assert journal.head.value()['state'] == 'idle'
    return loaded


def _deny_effects(monkeypatch):
    for name in ('publish', 'write_new', 'retire'):
        monkeypatch.setattr(native.Transaction, name,
            lambda *a, **k: pytest.fail('unexpected recovery publication or retirement'))


@pytest.mark.parametrize('kind', ['repair', 'migrate', 'discard'])
def test_complete_operation_retains_original_evidence_and_replays_no_effects(owned, monkeypatch, kind):
    current, legacy = _setup(owned, kind)
    source = current if kind == 'repair' else legacy
    original = _read(owned, source)
    _call(kind)
    with native.transaction(owned) as tx:
        journal = prefs._journal(tx)
        _record, plan, selected, images = journal.read_plan(OP)
        receipt = journal.completion(OP, records.digest(_record.raw))
        assert journal.head.value()['state'] == 'complete' and receipt is not None
        retained = tx.read(selected['retained'])
        assert retained[0] == original[0]
        if kind != 'repair':
            assert retained[1][:2] == original[1][:2]
            assert tx.read(source) == (None, None)
        else:
            assert tx.read(current)[0] == codec.encode({})
    assert not prefs.capture(OWNER).recovery_pending
    _deny_effects(monkeypatch)
    result = _call(kind)
    if kind == 'repair': assert result['historical'] and result['repaired']
    elif kind == 'migrate': assert result == 2
    else: assert result is True


@pytest.mark.parametrize('overwrite', [False, True])
def test_migration_preserves_raw_non_null_policy_and_exact_null_semantics(owned, overwrite):
    current, legacy = prefs._relative_paths(OWNER)
    _write(owned, current, b'{"autonomy":0,"false":false,"zero":0,"empty":"","none":null,"remove":1}')
    _write(owned, legacy, b'{"autonomy":4,"false":true,"zero":9,"empty":"new","none":"filled","remove":null,"absent":null}')
    assert prefs.is_quarantined(OWNER)
    count = _call('migrate', overwrite=overwrite)
    expected = ({'autonomy':4, 'false':True, 'zero':9, 'empty':'new', 'none':'filled'} if overwrite
                else {'autonomy':0, 'false':False, 'zero':0, 'empty':'', 'none':'filled', 'remove':1})
    assert count == (7 if overwrite else 2)
    assert prefs.load(OWNER) == expected
    assert not prefs.is_quarantined(OWNER)


@pytest.mark.parametrize('present', [False, True])
def test_valid_or_missing_repair_is_inert_without_journal(owned, monkeypatch, present):
    current = prefs._relative_paths(OWNER)[0]
    if present: _write(owned, current, b'{"autonomy":0}')
    before = _read(owned, current)
    _deny_effects(monkeypatch)
    result = _call('repair')
    assert not result['repaired'] and result['operation_id'] is None
    assert _read(owned, current) == before
    assert not (owned / 'prefs-recovery').exists()


BOUNDARIES = [
    ('migrate', 'owner-claim'), ('migrate', 'legacy-claim'), ('migrate', 'pending-head'),
    ('migrate', 'target'), ('migrate', 'retirement'), ('migrate', 'receipt'), ('migrate', 'complete-head'),
    ('repair', 'archive'), ('repair', 'target'), ('repair', 'receipt'),
    ('discard', 'retirement'), ('discard', 'receipt'),
]


def _interrupt_after(root, monkeypatch, kind, boundary, fault):
    current, legacy = _setup(root, kind)
    originals = {name: getattr(native.Transaction, name) for name in ('publish', 'write_new', 'retire')}
    fired = []
    def wrap(method):
        def call(tx, parts, *args, **kwargs):
            result = originals[method](tx, parts, *args, **kwargs)
            match = (boundary == 'owner-claim' and parts == prefs._claim_paths(OWNER)[0]
                or boundary == 'legacy-claim' and parts == prefs._claim_paths(OWNER)[1]
                or boundary == 'target' and method == 'publish' and parts == current
                or boundary == 'archive' and method == 'write_new' and parts[-1].startswith('prefs.corrupt.')
                or boundary == 'retirement' and method == 'retire'
                or boundary == 'receipt' and parts == prefs._recovery_locations(OP)['completion']
                or boundary in ('pending-head', 'complete-head') and method == 'publish'
                    and parts == prefs._recovery_layout()['head']
                    and codec.decode(args[0])['state'] == boundary.split('-')[0])
            if match and not fired:
                fired.append(True)
                raise fault
            return result
        return call
    with monkeypatch.context() as patch:
        for name in originals: patch.setattr(native.Transaction, name, wrap(name))
        expected = prefs.PreferenceOperationError if isinstance(fault, OSError) else type(fault)
        with pytest.raises(expected) as error:
            _call(kind)
        if isinstance(fault, OSError):
            assert error.value.operation_id == OP
            assert error.value.__context__ is None and error.value.__cause__ is None
            assert 'owned-private' not in str(error.value)
        else:
            assert error.value is fault
            assert error.value.__dict__['_preference_recovery_operation'] == OP
    assert fired == [True]
    return current, legacy


@pytest.mark.parametrize('kind,boundary', BOUNDARIES)
@pytest.mark.parametrize('fault_type', [OSError, KeyboardInterrupt, SystemExit])
def test_lost_acknowledgment_resumes_fixed_operation(owned, monkeypatch, kind, boundary, fault_type):
    _interrupt_after(owned, monkeypatch, kind, boundary, fault_type('owned-private-ack'))
    observed = prefs.capture(OWNER)
    if boundary != 'complete-head':
        assert observed.recovery_pending and observed.quarantined
        assert prefs.get(OWNER, 'autonomy', captured=observed) == 0
        with pytest.raises(prefs.PreferenceConflict):
            prefs.mutate(OWNER, lambda _: pytest.fail('pending recovery reached transform'))
    _call(kind)
    assert not prefs.capture(OWNER).recovery_pending
    _deny_effects(monkeypatch)
    _call(kind)


@pytest.mark.parametrize('kind', ['migrate', 'discard'])
def test_unarmed_plan_refuses_already_retired_source_before_pointer_writes(owned, monkeypatch, kind):
    _record, plan, selected, images = _unarmed(owned, monkeypatch, kind)
    with native.transaction(owned) as tx:
        tx.retire(selected['source'], selected['retained'], images['source'], tuple(plan['source_identity']))
    head = _read(owned, prefs._recovery_layout()['head'])
    _deny_effects(monkeypatch)
    with pytest.raises(prefs.PreferenceOperationError): _call(kind)
    assert _read(owned, prefs._recovery_layout()['head']) == head
    for role in ('owner_claim', 'legacy_claim'):
        if selected[role] is not None: assert _read(owned, selected[role]) == (None, None)


def test_unarmed_visible_completion_refuses_before_pointer_writes(owned, monkeypatch):
    record, plan, selected, _images = _unarmed(owned, monkeypatch, 'discard')
    receipt = {'version':1, 'operation':OP, 'plan_sha256':records.digest(record.raw),
        'target_identity':None, 'retained_identity':plan['source_identity']}
    _write(owned, prefs._recovery_locations(OP)['completion'], records.encode(receipt))
    _deny_effects(monkeypatch)
    with pytest.raises(prefs.PreferenceOperationError): _call('discard')


@pytest.mark.parametrize('kind', ['repair', 'migrate'])
def test_unarmed_stale_target_refuses_before_arming(owned, monkeypatch, kind):
    _record, _plan, selected, _images = _unarmed(owned, monkeypatch, kind)
    changed = _write(owned, selected['target'], b'{"autonomy":0,"revoked":true}')
    _deny_effects(monkeypatch)
    with pytest.raises(prefs.PreferenceOperationError): _call(kind)
    assert _read(owned, selected['target']) == changed


@pytest.mark.parametrize('variant', ['repair-after', 'migration-after', 'migration-count', 'migration-malformed'])
def test_invalid_retained_semantics_refuses_without_arming(owned, monkeypatch, variant):
    kind = 'repair' if variant == 'repair-after' else 'migrate'
    _record, plan, _selected, images = _unarmed(owned, monkeypatch, kind)
    if variant == 'migration-count':
        plan['result_count'] += 1
    else:
        images['after'] = b'{"autonomy":4,"unrelated":"grant"}' if variant != 'migration-malformed' else b'[]'
        plan['images']['after'] = records.image_descriptor(images['after'])
        _write(owned, prefs._recovery_locations(OP)['after'], images['after'])
    _write(owned, prefs._recovery_locations(OP)['plan'], records.encode(plan))
    _deny_effects(monkeypatch)
    with pytest.raises(prefs.PreferenceOperationError): _call(kind)


@pytest.mark.parametrize('kind', ['repair', 'discard'])
def test_new_noop_request_exposes_existing_pending_id(owned, monkeypatch, kind):
    _interrupt_after(owned, monkeypatch, kind, 'target' if kind == 'repair' else 'retirement', OSError('owned-ack'))
    _deny_effects(monkeypatch)
    with pytest.raises(prefs.PreferenceOperationError) as error:
        _call(kind, NEXT)
    assert error.value.operation_id == OP


@pytest.mark.parametrize('present', [False, True])
def test_inert_repair_refuses_damaged_recovery_metadata(owned, monkeypatch, present):
    _setup(owned, 'discard'); _call('discard')
    if not present:
        # Retain the fixture target outside its live address, never delete it.
        current = prefs._relative_paths(OWNER)[0]
        path = prefs._io(owned.joinpath(*current))
        path.rename(path.with_name('owned-retained-target.bin'))
    _write(owned, prefs._recovery_layout()['head'], b'{owned-damaged-head')
    _deny_effects(monkeypatch)
    with pytest.raises(prefs.PreferenceOperationError): _call('repair', NEXT)


@pytest.mark.parametrize('kind', ['repair', 'migrate', 'discard'])
def test_historical_completion_keeps_later_revocation_and_new_head(owned, monkeypatch, kind):
    current, _legacy = _setup(owned, kind)
    _call(kind)
    prefs.set(OWNER, 'autonomy', 0)
    prefs.set(OWNER, 'revoked', True)
    # A second completed operation changes the head, not this owner's target.
    other = 'second-owned-group'
    _write(owned, ('users', other, 'prefs.json'), b'{"owned":true}')
    prefs.discard_legacy(other, operation_id=NEXT)
    head = _read(owned, prefs._recovery_layout()['head'])
    target = _read(owned, current)
    _deny_effects(monkeypatch)
    _call(kind)
    assert _read(owned, current) == target
    assert _read(owned, prefs._recovery_layout()['head']) == head
    assert prefs.get(OWNER, 'autonomy') == 0


def test_receipt_substitution_after_history_verification_refuses(owned, monkeypatch):
    _setup(owned, 'migrate'); _call('migrate')
    original = records.Journal.completion
    publish = native.Transaction.publish
    observations = []
    def changed(journal, identifier, plan_digest, expected_digest=None):
        observations.append(identifier)
        if len(observations) == 2:
            receipt = original(journal, identifier, plan_digest, expected_digest)
            value = receipt.value()
            value['target_identity'][2] += 1
            publish(journal.tx, journal.locate(identifier)['completion'], records.encode(value))
        return original(journal, identifier, plan_digest, expected_digest)
    _deny_effects(monkeypatch)
    monkeypatch.setattr(records.Journal, 'completion', changed)
    with pytest.raises(prefs.PreferenceOperationError): _call('migrate')
    assert len(observations) == 2


@pytest.mark.parametrize('changed', ['owner', 'legacy', 'kind', 'overwrite'])
def test_existing_operation_refuses_rebound_request(owned, monkeypatch, changed):
    _unarmed(owned, monkeypatch, 'migrate')
    _deny_effects(monkeypatch)
    with pytest.raises(prefs.PreferenceOperationError):
        if changed == 'owner': prefs.migrate_legacy(LEGACY, 'other-owned-principal', operation_id=OP)
        elif changed == 'legacy': prefs.migrate_legacy('other-owned-group', OWNER, operation_id=OP)
        elif changed == 'kind': prefs.discard_legacy(LEGACY, operation_id=OP)
        else: _call('migrate', overwrite=True)


@pytest.mark.parametrize('stage_parent', ['target', 'legacy', 'journal'])
def test_unbound_staging_is_retained_and_refuses_new_recovery(owned, monkeypatch, stage_parent):
    current, legacy = _setup(owned, 'migrate')
    address = {'target':current, 'legacy':legacy, 'journal':prefs._recovery_layout()['head']}[stage_parent]
    stage = address[:-1] + ('pending-owned-unknown.json',)
    before = _write(owned, stage, b'{"owned-retained-stage":true}')
    _deny_effects(monkeypatch)
    with pytest.raises(prefs.PreferenceOperationError): _call('migrate')
    assert _read(owned, stage) == before


def test_legacy_inspection_uses_bounded_native_keys_without_values(owned):
    current, legacy = _setup(owned, 'migrate')
    assert prefs.legacy_owners() == [LEGACY]
    assert prefs.legacy_keys(LEGACY) == ['add', 'autonomy', 'remove']
    _call('migrate')
    assert prefs.legacy_owners() == [] and prefs.legacy_keys(LEGACY) == []


@pytest.mark.parametrize('invalid', ['../other', 'A' * 65, 'a/b', 'a.b'])
def test_invalid_legacy_selector_refuses_before_root_access(owned, monkeypatch, invalid):
    monkeypatch.setattr(native, 'transaction', lambda *a, **k: pytest.fail('invalid selector opened a root'))
    with pytest.raises((ValueError, native.PreferenceStorageError)):
        prefs.discard_legacy(invalid, operation_id=OP)


def test_sixteen_operation_limit_refuses_seventeenth_without_effect(owned, monkeypatch):
    for index in range(16):
        group = f'owned-group-{index}'
        _write(owned, ('users', group, 'prefs.json'), b'{"owned":true}')
        assert prefs.discard_legacy(group, operation_id=f'{index:032x}')
    group = 'owned-group-overflow'
    address = ('users', group, 'prefs.json')
    original = _write(owned, address, b'{"owned":true}')
    _deny_effects(monkeypatch)
    with pytest.raises(prefs.PreferenceOperationError):
        prefs.discard_legacy(group, operation_id='f' * 32)
    assert _read(owned, address) == original


# Appended to the uninstalled recovery tests; no standalone execution.

@pytest.mark.parametrize('metadata', ['head', 'namespace'])
@pytest.mark.parametrize('boundary', ['file', 'directory'])
@pytest.mark.parametrize('fault_type', [OSError, KeyboardInterrupt, SystemExit])
def test_visible_initialization_reconfirms_real_failed_barrier_before_plan_effects(owned, monkeypatch, metadata, boundary, fault_type):
    import os
    _setup(owned, 'migrate')
    selected = prefs._recovery_layout()[metadata]
    confirm = native.Transaction.confirm
    fsync = os.fsync
    sync = native.storage._Directory.sync
    active, fired = [], []
    fault = fault_type('owned-private-native-barrier')
    def tagged(tx, parts, raw, identity):
        active.append(parts)
        try: return confirm(tx, parts, raw, identity)
        finally: active.pop()
    def fail_file(fd):
        if active and active[-1] == selected and boundary == 'file' and not fired:
            fired.append(True)
            raise fault
        return fsync(fd)
    def fail_directory(directory):
        if active and active[-1] == selected and boundary == 'directory' and not fired:
            fired.append(True)
            raise fault
        return sync(directory)
    with monkeypatch.context() as patch:
        patch.setattr(native.Transaction, 'confirm', tagged)
        patch.setattr(os, 'fsync', fail_file)
        patch.setattr(native.storage._Directory, 'sync', fail_directory)
        expected = prefs.PreferenceOperationError if fault_type is OSError else fault_type
        with pytest.raises(expected) as error: _call('migrate')
        if fault_type is not OSError: assert error.value is fault
    assert fired == [True]
    with native.transaction(owned) as tx:
        journal = prefs._journal(tx)
        assert journal.head.value()['state'] == 'idle'
        assert not journal.operations and not journal.initialization_confirmed
        assert journal.interrupted_initialization == (metadata == 'head')
    order = []
    write_new = native.Transaction.write_new
    def confirmed(tx, parts, raw, identity):
        result = confirm(tx, parts, raw, identity)
        order.append(('confirm', parts))
        return result
    def written(tx, parts, raw):
        if parts[:2] == prefs._recovery_layout()['operations']:
            assert ('confirm', prefs._recovery_layout()['head']) in order
            assert ('confirm', prefs._recovery_layout()['namespace']) in order
        order.append(('write', parts))
        return write_new(tx, parts, raw)
    with monkeypatch.context() as patch:
        patch.setattr(native.Transaction, 'confirm', confirmed)
        patch.setattr(native.Transaction, 'write_new', written)
        assert _call('migrate') == 2
    assert ('write', prefs._recovery_locations(OP)['plan']) in order


@pytest.mark.parametrize('record_name', ['plan', 'completion'])
@pytest.mark.parametrize('damage', ['missing', 'changed'])
def test_valid_complete_head_refuses_damaged_predecessor_history(owned, monkeypatch, record_name, damage):
    _setup(owned, 'migrate'); _call('migrate')
    other = 'other-owned-history'
    _write(owned, ('users', other, 'prefs.json'), b'{"owned":true}')
    prefs.discard_legacy(other, operation_id=NEXT)
    head = _read(owned, prefs._recovery_layout()['head'])
    assert records.decode(head[0])['state'] == 'complete'
    selected = prefs._recovery_locations(OP)[record_name]
    path = prefs._io(owned.joinpath(*selected))
    retained = owned.parent / 'retained-owned-history.bin'
    if damage == 'missing':
        path.rename(retained)
    else:
        with retained.open('xb') as stream: stream.write(path.read_bytes())
        _write(owned, selected, b'{"owned-damaged-history":true}')
    _deny_effects(monkeypatch)
    with pytest.raises(prefs.PreferenceOperationError): _call('migrate')
    assert prefs.get(OWNER, 'autonomy') == 0
    with pytest.raises(prefs.PreferencesStateError):
        prefs.mutate(OWNER, lambda _: pytest.fail('damaged history reached a transform'))
    assert _read(owned, prefs._recovery_layout()['head']) == head


_RESUME_CHILD = r'''
import json, socket, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
def denied(*args, **kwargs): raise AssertionError('owned recovery child network disabled')
socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.create_connection = denied
socket.socket.bind = denied
from olympus import config, prefs
config.MEMORY_DIR = Path(sys.argv[2])
owner, legacy, identifier, kind = sys.argv[3:]
if kind == 'repair': prefs.repair(owner, operation_id=identifier)
elif kind == 'migrate': prefs.migrate_legacy(legacy, owner, operation_id=identifier)
else: prefs.discard_legacy(legacy, operation_id=identifier)
assert not prefs.capture(owner).recovery_pending
print(json.dumps({'resumed': True, 'operation_id': identifier}))
'''


@pytest.mark.parametrize('kind', ['repair', 'migrate', 'discard'])
def test_fresh_owned_process_resumes_original_retained_operation(owned, monkeypatch, kind):
    import json
    from pathlib import Path
    import subprocess
    import sys
    _interrupt_after(owned, monkeypatch, kind, 'target' if kind == 'repair' else 'retirement', OSError('owned-ack'))
    child, primary, cleanup = None, None, []
    try:
        child = subprocess.Popen([sys.executable, '-I', '-B', '-c', _RESUME_CHILD,
            str(Path(native.__file__).resolve().parents[1]), str(owned), OWNER, LEGACY, OP, kind],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = child.communicate(timeout=30)
        assert child.returncode == 0 and stderr == b'' and len(stdout) < 200
        assert json.loads(stdout) == {'resumed': True, 'operation_id': OP}
    except BaseException as error:
        primary = error
        raise
    finally:
        if child is not None:
            try:
                if child.poll() is None:
                    child.kill()  # Retained native handle / direct unreaped child only.
                    child.wait(timeout=3)
            except BaseException as error: cleanup.append(type(error).__name__)
            for stream in (child.stdout, child.stderr):
                if stream is not None:
                    try: stream.close()
                    except BaseException as error: cleanup.append(type(error).__name__)
        if cleanup:
            if primary is not None:
                primary.add_note('owned recovery child cleanup is unconfirmed')
            else:
                raise AssertionError('owned recovery child cleanup is unconfirmed')
    assert not prefs.capture(OWNER).recovery_pending


# Appended to owned recovery controls for the separate missing-root fast path.

def test_absent_journal_root_avoids_redundant_descendant_probes(owned, monkeypatch):
    with native.transaction(owned) as tx:
        reads, listings = [], []
        read, inventory = tx.read, tx.inventory
        def observed_read(parts, **kwargs):
            reads.append(parts)
            return read(parts, **kwargs)
        def observed_inventory(parts, **kwargs):
            listings.append(parts)
            return inventory(parts, **kwargs)
        monkeypatch.setattr(tx, 'read', observed_read)
        monkeypatch.setattr(tx, 'inventory', observed_inventory)
        journal = prefs._journal(tx)
        assert journal.root_absent and journal.operations == ()
        assert journal.namespace == journal.head == records.Record(None, None)
        assert listings == [prefs._recovery_layout()['root']] and reads == []
        engine.View(journal).snapshot(*prefs._claim_paths(OWNER))
        assert reads == list(prefs._claim_paths(OWNER))


def test_existing_empty_journal_never_uses_absence_shortcut(owned, monkeypatch):
    (owned / 'prefs-recovery').mkdir()
    with native.transaction(owned) as tx:
        reads = []
        read = tx.read
        def observed(parts, **kwargs):
            reads.append(parts)
            return read(parts, **kwargs)
        monkeypatch.setattr(tx, 'read', observed)
        journal = prefs._journal(tx)
        assert not journal.root_absent
        assert reads == [prefs._recovery_layout()['namespace'], prefs._recovery_layout()['head']]


@pytest.mark.parametrize('fault_type', [PermissionError, FileNotFoundError, KeyboardInterrupt])
def test_journal_inventory_failure_is_never_initial_absence(owned, monkeypatch, fault_type):
    fault = fault_type('owned-private-journal-inventory')
    native._mark_cleanup(fault)
    with native.transaction(owned) as tx:
        def failed(*args, **kwargs): raise fault
        monkeypatch.setattr(tx, 'inventory', failed)
        monkeypatch.setattr(tx, 'read', lambda *a, **k: pytest.fail('failed inventory used a fallback read'))
        with pytest.raises(fault_type) as error: prefs._journal(tx)
        assert error.value is fault and native.cleanup_unknown(error.value)


@pytest.mark.parametrize('role', ['owner', 'legacy'])
def test_absent_journal_does_not_hide_dangling_claim(owned, monkeypatch, role):
    selected = prefs._claim_paths(OWNER)[0 if role == 'owner' else 1]
    _write(owned, selected, records.encode(records.pointer(OP, '0' * 64)))
    assert not (owned / 'prefs-recovery').exists()
    _deny_effects(monkeypatch)
    with pytest.raises(prefs.PreferencesStateError): prefs.capture(OWNER)
    assert prefs.get(OWNER, 'autonomy') == 0


def test_new_journal_after_capture_is_seen_before_mutation_callback(owned):
    prefs.set(OWNER, 'autonomy', 0)
    captured = prefs.capture(OWNER)
    with native.transaction(owned) as tx:
        journal = prefs._journal(tx)
        assert journal.root_absent
        journal.initialize()
        journal.scan()
        assert not journal.root_absent and journal.root_listed
        assert prefs._journal(tx).initialized
    with pytest.raises(prefs.PreferenceConflict):
        prefs.prepare_mutation(OWNER, lambda _: pytest.fail('changed journal reached callback'), captured=captured)


def test_recovery_appearance_during_transform_is_seen_before_publication(owned, monkeypatch):
    prefs.set(OWNER, 'autonomy', 0)
    calls = []
    def transform(data):
        calls.append(True)
        # Owned low-level fault injection models a writer outside the public
        # pure-transform contract. Public nested preference calls still refuse.
        tx = native._LOCAL.current
        prefs._journal(tx).initialize()
        data['autonomy'] = 4
        return data
    monkeypatch.setattr(native.Transaction, 'publish', lambda *a, **k: pytest.fail('changed recovery state published a target'))
    with pytest.raises(prefs.PreferenceConflict): prefs.mutate(OWNER, transform)
    assert calls == [True] and prefs.get(OWNER, 'autonomy') == 0

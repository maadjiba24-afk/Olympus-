"""Owned remembered-login state, publication, interruption and claimant controls."""
from collections import OrderedDict
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from olympus import config, native_cleanup, prefs, prefs_native, prefs_recovery_records
from olympus import securecapture as capture, store, vault
from olympus import vault_custody as custody

OWNER = 'owned-secure-capture'
SITE = 'example.test'
KEY = 'owned-capture-key-material'
PASSWORD = 'owned-capture-password'


def forbidden(*args, **kwargs):
    pytest.fail('refused workflow reached a forbidden callback')


@pytest.fixture
def owned(tmp_path, monkeypatch):
    root = tmp_path / 'memory'
    root.mkdir()
    monkeypatch.setattr(config, 'MEMORY_DIR', root)
    monkeypatch.setattr(store, '_backend', store.FileStore())
    monkeypatch.delenv('OLYMPUS_DATABASE_URL', raising=False)
    monkeypatch.delenv('OLYMPUS_SECRET_KEY_FILE', raising=False)
    monkeypatch.setenv('OLYMPUS_SECRET_KEY', KEY)
    monkeypatch.setenv('OLYMPUS_ENV', 'test')
    monkeypatch.setattr(capture, '_WORKFLOWS', OrderedDict())
    monkeypatch.setattr(capture, '_ACTIVE', {})
    assert capture._MAX_WORKFLOWS == 64
    return root


def request_and_begin():
    request_id = capture.request(OWNER, SITE)
    facts = capture.begin(OWNER)
    assert facts['state'] == 'ready' and facts['claim'] == 'confirmed' and not facts['prompt_dispatched']
    assert facts['request_id'] == request_id
    return facts


def prompt(facts):
    admitted = capture.start_prompt(OWNER, facts['workflow_id'])
    assert admitted['state'] == 'prompting' and admitted['prompt_dispatched']
    return admitted


def complete(facts, root):
    assert facts['state'] == 'complete' and facts['complete'] and not facts['cleanup_unconfirmed']
    assert all(facts[name] == 'confirmed' for name in ('claim', 'saving', 'vault', 'authorization'))
    assert prefs.get(OWNER, 'pending_secure_login') is None
    settings = prefs.get(OWNER, 'operator')
    assert settings['enabled'] is True and settings['sites'][SITE]['login'] == 'remember'
    assert KEY not in repr(facts) and PASSWORD not in repr(facts)
    # Independent persisted readback: original owned root/key, no workflow
    # custody or backend reselection. This still executes authenticated decode.
    with vault.storage.transaction(store.FileStore(), root) as tx:
        blob = tx.get(vault._NS, vault._key(OWNER), cap=vault._ROW_CAP)
    assert type(blob) is bytes and blob
    entries, metadata = vault.records.decode(OWNER, blob, KEY.encode())
    assert metadata.context_authenticated
    assert entries == {'site:' + SITE: {'username': 'owned-user', 'password': PASSWORD}}


def files(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_request_uuid_replacement_and_exact_cancellation_never_preauthorize(owned):
    first = capture.request(OWNER, 'Example.Test')
    record = prefs.get(OWNER, 'pending_secure_login')
    assert record == {'version': 1, 'request_id': first, 'domain': SITE, 'state': 'requested'}
    assert len(first) == 32 and prefs.get(OWNER, 'operator') is None
    second = capture.request(OWNER, SITE)
    assert second != first and capture.pending(OWNER) == SITE
    assert capture.clear(OWNER, request_id=first) is False
    assert prefs.get(OWNER, 'pending_secure_login')['request_id'] == second
    assert capture.clear(OWNER, request_id=second) is True and capture.pending(OWNER) is None
    assert prefs.get(OWNER, 'operator') is None


@pytest.mark.parametrize('invalid', ['example.test', {}, {'version': 2},
    {'version': 1, 'request_id': 'a' * 32, 'domain': SITE, 'state': 'prompting'},
    {'version': 1, 'request_id': 'a' * 32, 'domain': SITE, 'state': 'saving'},
    {'version': True, 'request_id': 'a' * 32, 'domain': SITE, 'state': 'requested'}],
    ids=['legacy-string', 'empty', 'future-version', 'unclaimed-prompting', 'unclaimed-saving', 'bool-version'])
def test_invalid_or_legacy_request_refuses_before_vault(owned, monkeypatch, invalid):
    prefs.set(OWNER, 'pending_secure_login', invalid)
    before = prefs.capture(OWNER).raw
    monkeypatch.setattr(vault, '_snapshots_for_preferences', forbidden)
    facts = capture.begin(OWNER)
    assert facts['state'] == 'unconfirmed' and facts['claim'] == 'not_attempted'
    with pytest.raises(capture.SecureCaptureError):
        capture.pending(OWNER)
    assert prefs.capture(OWNER).raw == before


@pytest.mark.parametrize('state', ['prompting', 'saving'])
def test_process_death_marker_never_becomes_another_prompt(owned, monkeypatch, state):
    record = {'version': 1, 'request_id': 'a' * 32, 'claim_id': 'b' * 32, 'domain': SITE, 'state': state}
    prefs.set(OWNER, 'pending_secure_login', record)
    monkeypatch.setattr(vault, '_snapshots_for_preferences', forbidden)
    facts = capture.begin(OWNER)
    assert facts['state'] == 'reconciliation_required' and not facts['prompt_dispatched']
    assert capture.pending(OWNER) is None
    with pytest.raises(capture.SecureCaptureError):
        capture.request(OWNER, SITE)
    with pytest.raises(capture.SecureCaptureError):
        capture.clear(OWNER, request_id=record['request_id'])
    assert prefs.get(OWNER, 'pending_secure_login') == record


@pytest.mark.parametrize('settings', [None, [], {'enabled': 1}, {'enabled': 'true'}, {'sites': []},
    {'sites': {'Example.Test': {}}}, {'sites': {SITE: None}}, {'sites': {SITE: {'login': 'automatic'}}}],
    ids=['null', 'list', 'integer-enabled', 'text-enabled', 'sites-list', 'noncanonical', 'bad-entry', 'bad-mode'])
def test_invalid_policy_refuses_before_vault_capture_or_input(owned, monkeypatch, settings):
    prefs.mutate(OWNER, lambda data: {**data, 'operator': settings})
    assert 'operator' in prefs.capture(OWNER).data()
    assert prefs.capture(OWNER).data()['operator'] == settings
    request_id = capture.request(OWNER, SITE)
    monkeypatch.setattr(vault, '_snapshots_for_preferences', forbidden)
    facts = capture.begin(OWNER)
    assert facts['state'] == 'unconfirmed' and facts['claim'] == 'not_attempted'
    assert not facts['prompt_dispatched'] and prefs.get(OWNER, 'operator') == settings
    assert prefs.get(OWNER, 'pending_secure_login')['request_id'] == request_id


@pytest.mark.requires_crypto
@pytest.mark.parametrize('existing', [False, True])
def test_site_capacity_checks_prospective_grant_before_capture(owned, monkeypatch, existing):
    sites = {f's{index}.example.test': {'login': 'manual'} for index in range(255)}
    sites[SITE if existing else 'last.example.test'] = {'login': 'manual'}
    prefs.set(OWNER, 'operator', {'enabled': False, 'sites': sites})
    capture.request(OWNER, SITE)
    if not existing:
        monkeypatch.setattr(vault, '_snapshots_for_preferences', forbidden)
    facts = capture.begin(OWNER)
    assert facts['state'] == ('ready' if existing else 'unconfirmed')
    assert not facts['prompt_dispatched'] and len(prefs.get(OWNER, 'operator')['sites']) == 256


def test_missing_key_refuses_before_claim_or_prompt(owned, monkeypatch):
    request_id = capture.request(OWNER, SITE)
    monkeypatch.delenv('OLYMPUS_SECRET_KEY')
    facts = capture.begin(OWNER)
    assert facts['state'] == 'unconfirmed' and facts['claim'] == 'not_attempted'
    assert not facts['prompt_dispatched']
    assert prefs.get(OWNER, 'pending_secure_login') == {'version': 1, 'request_id': request_id, 'domain': SITE, 'state': 'requested'}


@pytest.mark.requires_crypto
def test_claim_has_unique_workflow_identity_and_full_success_releases_secrets(owned):
    facts = request_and_begin()
    record = prefs.get(OWNER, 'pending_secure_login')
    assert record['claim_id'] == facts['workflow_id'] and record['state'] == 'prompting'
    assert capture.pending(OWNER) is None and prefs.get(OWNER, 'operator') is None
    prompt(facts)
    result = capture.fulfill(OWNER, SITE, 'owned-user', PASSWORD, workflow_id=facts['workflow_id'])
    complete(result, owned)
    retained = capture._WORKFLOWS[facts['workflow_id']]
    assert all(getattr(retained, field) is None for field in ('selected', 'update', 'before', 'claim',
        'prompting_image', 'saving', 'saving_image', 'authorization', 'cancellation'))
    assert capture.active_status(OWNER) == result
    assert all(KEY.encode() not in value and PASSWORD.encode() not in value for value in files(owned).values())


def test_private_values_without_prepared_identity_cannot_select_authority(owned, monkeypatch):
    monkeypatch.setattr(prefs, 'capture', forbidden)
    monkeypatch.setattr(vault, '_snapshots_for_preferences', forbidden)
    with pytest.raises(capture.SecureCaptureError) as caught:
        capture.fulfill(OWNER, SITE, 'owned-user', PASSWORD)
    assert PASSWORD not in str(caught.value) and list(owned.iterdir()) == []


@pytest.mark.requires_crypto
@pytest.mark.parametrize('stage', ['claim', 'saving', 'authorization'])
@pytest.mark.parametrize('fault', [RuntimeError, KeyboardInterrupt, SystemExit])
def test_lost_preference_acknowledgment_uses_original_pending_handle(owned, monkeypatch, stage, fault):
    capture.request(OWNER, SITE)
    custody_calls, pending_handles = [], []
    original_custody, original_settle = custody.capture, prefs.settle_mutation
    def key(*args, **kwargs):
        custody_calls.append(True)
        return original_custody(*args, **kwargs)
    monkeypatch.setattr(custody, 'capture', key)
    facts = None
    if stage != 'claim':
        facts = capture.begin(OWNER)
        assert facts['state'] == 'ready'
        prompt(facts)
    primary = fault('owned-private-acknowledgment')
    fired = []
    def settle(owner, pending):
        record = json.loads(pending.after).get('pending_secure_login')
        selected_stage = 'authorization' if record is None else ('claim' if record['state'] == 'prompting' else 'saving')
        result = original_settle(owner, pending)
        if selected_stage == stage:
            pending_handles.append(pending)
            if not fired:
                fired.append(True)
                raise primary
        return result
    monkeypatch.setattr(prefs, 'settle_mutation', settle)
    def dispatch():
        return capture.begin(OWNER) if stage == 'claim' else capture.fulfill(OWNER, SITE, 'owned-user', PASSWORD, workflow_id=facts['workflow_id'])
    if fault is RuntimeError:
        observed = dispatch()
    else:
        with pytest.raises(fault) as caught:
            dispatch()
        assert caught.value is primary
        observed = capture.active_status(OWNER)
        assert primary.__dict__['_secure_capture_workflow']['workflow_id'] == observed['workflow_id']
    assert fired == [True] and observed[stage] == 'unconfirmed' and not observed['cleanup_unconfirmed']
    monkeypatch.setattr(custody, 'capture', forbidden)
    monkeypatch.setattr(vault, '_snapshot_selection', forbidden)
    settled = capture.settle(OWNER, observed['workflow_id'])
    if stage == 'claim':
        assert settled['state'] == 'ready' and not settled['prompt_dispatched']
        prompt(settled)
        settled = capture.fulfill(OWNER, SITE, 'owned-user', PASSWORD, workflow_id=settled['workflow_id'])
    complete(settled, owned)
    assert custody_calls == [True] and len(pending_handles) == 2 and pending_handles[0] is pending_handles[1]


@pytest.mark.requires_crypto
@pytest.mark.parametrize('fault', [RuntimeError, KeyboardInterrupt, SystemExit])
def test_lost_vault_acknowledgment_never_repeats_input_or_selection(owned, monkeypatch, fault):
    facts = request_and_begin()
    prompt(facts)
    original, seen = vault.settle_snapshot_update, []
    primary = fault('owned-private-vault-acknowledgment')
    def settle(pending):
        seen.append(pending)
        result = original(pending)
        if len(seen) == 1:
            raise primary
        return result
    monkeypatch.setattr(vault, 'settle_snapshot_update', settle)
    if fault is RuntimeError:
        observed = capture.fulfill(OWNER, SITE, 'owned-user', PASSWORD, workflow_id=facts['workflow_id'])
    else:
        with pytest.raises(fault) as caught:
            capture.fulfill(OWNER, SITE, 'owned-user', PASSWORD, workflow_id=facts['workflow_id'])
        assert caught.value is primary
        observed = capture.active_status(OWNER)
    assert observed['vault'] == 'unconfirmed' and observed['authorization'] == 'not_attempted'
    monkeypatch.setattr(vault, '_snapshot_selection', forbidden)
    monkeypatch.setattr(custody, 'capture', forbidden)
    with pytest.raises(capture.SecureCaptureError):
        capture.fulfill(OWNER, SITE, 'owned-user', PASSWORD, workflow_id=facts['workflow_id'])
    complete(capture.settle(OWNER, facts['workflow_id']), owned)
    assert len(seen) == 2 and seen[0] is seen[1]


@pytest.mark.requires_crypto
@pytest.mark.parametrize('change', ['none', 'preferences', 'root'])
def test_claim_getter_failure_cancellation_keeps_original_image(owned, monkeypatch, change):
    capture.request(OWNER, SITE)
    getter, settle = prefs._confirmed_mutation_snapshot, prefs.settle_mutation
    calls = {'getter': 0, 'settle': 0}
    def after_image(pending):
        calls['getter'] += 1
        result = getter(pending)
        if calls['getter'] == 1:
            raise RuntimeError('owned-private-getter')
        return result
    def settling(*args, **kwargs):
        calls['settle'] += 1
        return settle(*args, **kwargs)
    monkeypatch.setattr(prefs, '_confirmed_mutation_snapshot', after_image)
    monkeypatch.setattr(prefs, 'settle_mutation', settling)
    facts = capture.begin(OWNER)
    assert facts['state'] == 'unconfirmed' and facts['claim'] == 'confirmed'
    assert capture._WORKFLOWS[facts['workflow_id']].prompting_image is None
    if change == 'preferences':
        prefs.set(OWNER, 'owned_revocation_marker', True)
    elif change == 'root':
        owned.rename(owned.with_name('retained-memory'))
        owned.mkdir()
    before = files(owned)
    original_prepare, captures = prefs.prepare_mutation, []
    def preparing(*args, **kwargs):
        assert kwargs.get('captured') is not None
        captures.append(kwargs['captured'])
        return original_prepare(*args, **kwargs)
    monkeypatch.setattr(prefs, 'prepare_mutation', preparing)
    monkeypatch.setattr(prefs, 'capture', forbidden)
    result = capture.cancel(OWNER, facts['workflow_id'])
    assert calls['getter'] == 2 and len(captures) == 1
    if change == 'none':
        assert result['state'] == 'canceled' and result['cancellation'] == 'confirmed' and calls['settle'] == 2
    else:
        assert result['state'] == 'unconfirmed' and calls['settle'] == 1 and files(owned) == before
    assert not result['prompt_dispatched'] and result['vault'] == 'not_attempted'


@pytest.mark.requires_crypto
def test_saving_getter_failure_retains_acknowledgment_without_resettling(owned, monkeypatch):
    facts = request_and_begin()
    prompt(facts)
    getter, settle = prefs._confirmed_mutation_snapshot, prefs.settle_mutation
    saving_calls, getter_calls = [], []
    def after_image(pending):
        result = getter(pending)
        if json.loads(pending.after)['pending_secure_login']['state'] == 'saving':
            getter_calls.append(pending)
            if len(getter_calls) == 1:
                raise RuntimeError('owned-private-saving-getter')
        return result
    def settling(owner, pending):
        record = json.loads(pending.after).get('pending_secure_login')
        if record is not None and record['state'] == 'saving':
            saving_calls.append(pending)
        return settle(owner, pending)
    monkeypatch.setattr(prefs, '_confirmed_mutation_snapshot', after_image)
    monkeypatch.setattr(prefs, 'settle_mutation', settling)
    result = capture.fulfill(OWNER, SITE, 'owned-user', PASSWORD, workflow_id=facts['workflow_id'])
    assert result['saving'] == 'confirmed' and result['vault'] == 'not_attempted' and result['state'] == 'unconfirmed'
    monkeypatch.setattr(vault, '_snapshot_selection', forbidden)
    monkeypatch.setattr(custody, 'capture', forbidden)
    complete(capture.settle(OWNER, facts['workflow_id']), owned)
    assert len(saving_calls) == 1 and len(getter_calls) == 2 and getter_calls[0] is getter_calls[1]


@pytest.mark.requires_crypto
@pytest.mark.parametrize('transport', ['preference', 'native'])
@pytest.mark.parametrize('fault', [RuntimeError, KeyboardInterrupt, SystemExit])
def test_exceptional_cleanup_uncertainty_blocks_all_further_work(owned, monkeypatch, transport, fault):
    capture.request(OWNER, SITE)
    primary = fault('owned-private-cleanup')
    if transport == 'preference':
        prefs_native._mark_cleanup(primary)
    else:
        native_cleanup.mark(primary)
    reached = []
    def settle(*args, **kwargs):
        reached.append(True)
        raise primary
    monkeypatch.setattr(prefs, 'settle_mutation', settle)
    if fault is RuntimeError:
        facts = capture.begin(OWNER)
    else:
        with pytest.raises(fault) as caught:
            capture.begin(OWNER)
        assert caught.value is primary and native_cleanup.unknown(primary)
        facts = capture.active_status(OWNER)
        assert primary.__dict__['_secure_capture_workflow']['cleanup_unconfirmed']
    assert reached == [True] and facts['cleanup_unconfirmed'] and facts['state'] == 'unconfirmed'
    monkeypatch.setattr(prefs, 'settle_mutation', forbidden)
    monkeypatch.setattr(prefs, 'capture', forbidden)
    for function in (capture.settle, capture.start_prompt, capture.cancel):
        with pytest.raises(capture.SecureCaptureError):
            function(OWNER, facts['workflow_id'])
    assert capture.begin(OWNER)['workflow_id'] == facts['workflow_id']


@pytest.mark.requires_crypto
def test_returned_cleanup_uncertainty_keeps_confirmed_publication_fact(owned, monkeypatch):
    capture.request(OWNER, SITE)
    original = prefs.settle_mutation
    def settle(*args, **kwargs):
        return {**original(*args, **kwargs), 'cleanup_unconfirmed': True}
    monkeypatch.setattr(prefs, 'settle_mutation', settle)
    facts = capture.begin(OWNER)
    assert facts['claim'] == 'confirmed' and facts['cleanup_unconfirmed'] and not facts['prompt_dispatched']
    with pytest.raises(capture.SecureCaptureError):
        capture.settle(OWNER, facts['workflow_id'])


@pytest.mark.requires_crypto
@pytest.mark.parametrize('phase', ['claim', 'completion'])
@pytest.mark.parametrize('fault', [RuntimeError, KeyboardInterrupt, SystemExit])
def test_finalizer_failure_after_effects_keeps_facts_and_blocks_owner_reuse(owned, monkeypatch, phase, fault):
    capture.request(OWNER, SITE)
    facts = None
    if phase == 'completion':
        facts = capture.begin(OWNER)
        prompt(facts)
    primary, reached = fault('owned-private-finalizer'), []
    original = capture._finish
    def finish(attempt):
        original(attempt)
        reached.append(attempt)
        raise primary
    monkeypatch.setattr(capture, '_finish', finish)
    def dispatch():
        return capture.begin(OWNER) if phase == 'claim' else capture.fulfill(OWNER, SITE, 'owned-user', PASSWORD, workflow_id=facts['workflow_id'])
    if fault is RuntimeError:
        result = dispatch()
    else:
        with pytest.raises(fault) as caught:
            dispatch()
        assert caught.value is primary
        result = capture.active_status(OWNER)
        assert primary.__dict__['_secure_capture_workflow']['cleanup_unconfirmed']
    assert len(reached) == 1 and result['cleanup_unconfirmed'] and not result['complete']
    assert result['claim'] == 'confirmed'
    if phase == 'completion':
        assert result['vault'] == result['authorization'] == 'confirmed'
    monkeypatch.setattr(prefs, 'capture', forbidden)
    monkeypatch.setattr(capture, '_MAX_WORKFLOWS', 1)
    assert capture.begin(OWNER)['workflow_id'] == result['workflow_id']
    with pytest.raises(capture.SecureCaptureError):
        capture.begin('owned-other-owner')
    with pytest.raises(capture.SecureCaptureError):
        capture.settle(OWNER, result['workflow_id'])


@pytest.mark.requires_crypto
def test_begin_status_and_commands_refuse_foreign_thread_before_io(owned, monkeypatch):
    facts = request_and_begin()
    monkeypatch.setattr(prefs, 'capture', forbidden)
    outcomes = []
    def foreign():
        for function, args in ((capture.begin, (OWNER,)), (capture.status, (OWNER, facts['workflow_id'])),
                               (capture.active_status, (OWNER,))):
            try:
                function(*args)
            except capture.SecureCaptureError:
                outcomes.append('refused')
    thread = threading.Thread(target=foreign)
    thread.start()
    thread.join(5)
    if thread.is_alive():
        pytest.exit('owned executor-control thread did not stop', returncode=3)
    assert outcomes == ['refused'] * 3


@pytest.mark.requires_crypto
def test_reentrant_capture_observes_reservation_without_repeated_key_or_mutex(owned, monkeypatch):
    capture.request(OWNER, SITE)
    original, seen = custody.capture, []
    def key(*args, **kwargs):
        assert not capture._LOCK._is_owned() and not prefs_native._IN_FLIGHT
        facts = capture.begin(OWNER)
        assert facts['busy'] and facts['state'] == 'capturing' and not facts['prompt_dispatched']
        seen.append(facts['workflow_id'])
        return original(*args, **kwargs)
    monkeypatch.setattr(custody, 'capture', key)
    facts = capture.begin(OWNER)
    assert facts['state'] == 'ready' and seen == [facts['workflow_id']]


@pytest.mark.requires_crypto
def test_distinct_process_cannot_share_another_claims_after_image(owned, monkeypatch):
    capture.request(OWNER, SITE)
    reached = []
    original = prefs_recovery_records.checked_publish
    def fail_before_effect(tx, parts, raw):
        record = json.loads(raw).get('pending_secure_login')
        assert record['state'] == 'prompting' and record['claim_id']
        reached.append(record)
        raise RuntimeError('owned-before-publication')
    monkeypatch.setattr(prefs_recovery_records, 'checked_publish', fail_before_effect)
    first = capture.begin(OWNER)
    assert first['state'] == 'unconfirmed' and len(reached) == 1
    retained = capture._WORKFLOWS[first['workflow_id']]
    assert retained.claim._attempt.dispatched and not retained.claim._attempt.confirmed
    assert prefs.get(OWNER, 'pending_secure_login')['state'] == 'requested'
    monkeypatch.setattr(prefs_recovery_records, 'checked_publish', original)
    code = '''import json,os,socket,sys
from pathlib import Path
def denied(*a,**k):raise RuntimeError('owned child network disabled')
socket.socket.connect=socket.socket.connect_ex=socket.socket.bind=denied
socket.create_connection=denied
sys.path.insert(0,sys.argv[1])
from olympus import config,store,securecapture
assert Path(securecapture.__file__).resolve()==Path(sys.argv[1]).resolve()/'olympus'/'securecapture.py'
config.MEMORY_DIR=Path(sys.argv[2])
store._backend=store.FileStore()
os.environ.pop('OLYMPUS_DATABASE_URL',None)
os.environ.pop('OLYMPUS_SECRET_KEY_FILE',None)
os.environ['OLYMPUS_SECRET_KEY']='owned-capture-key-material'
os.environ['OLYMPUS_ENV']='test'
facts=securecapture.begin('owned-secure-capture')
assert facts['state']=='ready'
facts=securecapture.start_prompt('owned-secure-capture',facts['workflow_id'])
assert facts['state']=='prompting' and facts['prompt_dispatched']
print(json.dumps(facts))
'''
    child = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(Path(capture.__file__).parent.parent), str(owned)],
        capture_output=True, timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert child.returncode == 0 and child.stderr == b'' and len(child.stdout) < 4096
    second = json.loads(child.stdout)
    assert second['workflow_id'] != first['workflow_id'] and second['request_id'] == first['request_id']
    assert prefs.get(OWNER, 'pending_secure_login')['claim_id'] == second['workflow_id']
    before = prefs.capture(OWNER).raw
    monkeypatch.setattr(vault, '_snapshot_selection', forbidden)
    monkeypatch.setattr(custody, 'capture', forbidden)
    result = capture.settle(OWNER, first['workflow_id'])
    assert result['state'] == 'unconfirmed' and not result['prompt_dispatched'] and result['claim'] == 'unconfirmed'
    with pytest.raises(capture.SecureCaptureError):
        capture.start_prompt(OWNER, first['workflow_id'])
    assert prefs.capture(OWNER).raw == before


@pytest.mark.parametrize('uncertain', [False, True])
def test_full_registry_preserves_all_unresolved_owners_before_more_io(owned, monkeypatch, uncertain):
    calls = []
    def unavailable(owner):
        calls.append(owner)
        error = RuntimeError('owned synthetic unavailable capture')
        if uncertain:
            native_cleanup.mark(error)
        raise error
    monkeypatch.setattr(prefs, 'capture', unavailable)
    results = [capture.begin('owned-capacity-' + str(index)) for index in range(64)]
    assert all(item['state'] == 'unconfirmed' and not item['complete']
               and item['cleanup_unconfirmed'] is uncertain for item in results)
    before = dict(capture._ACTIVE)
    identities = tuple(capture._WORKFLOWS)
    with pytest.raises(capture.SecureCaptureError):
        capture.begin('owned-capacity-overflow')
    assert len(calls) == 64 and capture._ACTIVE == before and tuple(capture._WORKFLOWS) == identities
    assert capture.begin('owned-capacity-0') == results[0] and len(calls) == 64


def test_registry_evicts_only_clean_completed_entry(owned, monkeypatch):
    completed = capture.begin(OWNER)
    assert completed['state'] == 'no_request' and completed['complete']
    def unavailable(owner):
        raise RuntimeError('owned synthetic unavailable capture')
    monkeypatch.setattr(prefs, 'capture', unavailable)
    unresolved = [capture.begin('owned-retained-' + str(index)) for index in range(63)]
    replacement = capture.begin('owned-replacement')
    assert replacement['state'] == 'unconfirmed' and len(capture._WORKFLOWS) == 64
    assert completed['workflow_id'] not in capture._WORKFLOWS and capture.active_status(OWNER) is None
    assert all(capture.status('owned-retained-' + str(index), facts['workflow_id']) == facts
               for index, facts in enumerate(unresolved))


@pytest.mark.skipif(not hasattr(os, 'fork'), reason='actual POSIX fork control; separate Windows child claimant control')
def test_actual_fork_refuses_inherited_workflow_before_registry_lock_or_io(owned, monkeypatch):
    import signal
    completed = capture.begin(OWNER)
    class PoisonLock:
        def __enter__(self):
            raise AssertionError('fork entered inherited registry lock')
        def __exit__(self, *args):
            raise AssertionError('fork exited inherited registry lock')
    import time
    child, reaped, child_status = None, False, None
    def reap(deadline):
        nonlocal reaped, child_status
        while True:
            observed, status = os.waitpid(child, os.WNOHANG)
            if observed == child:
                reaped, child_status = True, status
                return
            assert observed == 0
            if time.monotonic() >= deadline:
                raise TimeoutError('owned fork control exceeded parent deadline')
            time.sleep(0.01)
    try:
        child = os.fork()
        if child == 0:
            try:
                signal.signal(signal.SIGALRM, lambda *args: os._exit(124))
                signal.alarm(5)
                capture._LOCK = PoisonLock()
                prefs.capture = forbidden
                vault._snapshot_selection = forbidden
                identity = completed['workflow_id']
                for function, args in ((capture.begin, (OWNER,)), (capture.status, (OWNER, identity)),
                        (capture.active_status, (OWNER,)), (capture.start_prompt, (OWNER, identity)),
                        (capture.settle, (OWNER, identity)), (capture.cancel, (OWNER, identity))):
                    try:
                        function(*args)
                    except capture.SecureCaptureError:
                        continue
                    os._exit(2)
                os._exit(0)
            except BaseException:
                os._exit(3)
        reap(time.monotonic() + 8)
        assert os.waitstatus_to_exitcode(child_status) == 0
    finally:
        try:
            if child is not None and child > 0 and not reaped:
                # Successful WNOHANG observation retains direct-child ownership;
                # ECHILD or observation failure never authorizes a numeric signal.
                observed, status = os.waitpid(child, os.WNOHANG)
                if observed == child:
                    reaped, child_status = True, status
                elif observed == 0:
                    os.kill(child, signal.SIGKILL)
                    reap(time.monotonic() + 2)
                else:
                    raise AssertionError('unexpected owned-child observation')
        except BaseException:
            pytest.exit('owned fork cleanup requires reconciliation', returncode=3)
    assert capture.active_status(OWNER) == completed


@pytest.mark.requires_crypto
def test_competing_vault_write_is_preserved_without_authorizing_stale_input(owned, monkeypatch):
    facts = request_and_begin()
    prompt(facts)
    replacement = {'username': 'owned-other-user', 'password': 'owned-other-password'}
    vault.put(OWNER, 'site:' + SITE, replacement)
    monkeypatch.setattr(vault, '_snapshot_selection', forbidden)
    monkeypatch.setattr(custody, 'capture', forbidden)
    result = capture.fulfill(OWNER, SITE, 'owned-user', PASSWORD, workflow_id=facts['workflow_id'])
    assert result['state'] == 'unconfirmed' and result['vault'] == 'unconfirmed'
    assert result['authorization'] == 'not_attempted' and prefs.get(OWNER, 'operator') is None
    with vault.storage.transaction(store.FileStore(), owned) as tx:
        blob = tx.get(vault._NS, vault._key(OWNER), cap=vault._ROW_CAP)
    entries, metadata = vault.records.decode(OWNER, blob, KEY.encode())
    assert metadata.context_authenticated and entries == {'site:' + SITE: replacement}


@pytest.mark.requires_crypto
def test_revocation_after_vault_ack_blocks_authorization_and_keeps_original_handle(owned, monkeypatch):
    facts = request_and_begin()
    prompt(facts)
    original, observed = vault.settle_snapshot_update, []
    def settle(pending):
        result = original(pending)
        observed.append(pending)
        prefs.set(OWNER, 'owned_revocation', True)
        return result
    monkeypatch.setattr(vault, 'settle_snapshot_update', settle)
    result = capture.fulfill(OWNER, SITE, 'owned-user', PASSWORD, workflow_id=facts['workflow_id'])
    assert result['vault'] == 'confirmed' and result['state'] == 'unconfirmed'
    assert result['authorization'] != 'confirmed' and prefs.get(OWNER, 'operator') is None
    assert prefs.get(OWNER, 'owned_revocation') is True and len(observed) == 1
    with vault.storage.transaction(store.FileStore(), owned) as tx:
        blob = tx.get(vault._NS, vault._key(OWNER), cap=vault._ROW_CAP)
    entries, metadata = vault.records.decode(OWNER, blob, KEY.encode())
    assert metadata.context_authenticated and entries == {'site:' + SITE: {'username': 'owned-user', 'password': PASSWORD}}
    before = prefs.capture(OWNER).raw
    monkeypatch.setattr(vault, 'settle_snapshot_update', forbidden)
    monkeypatch.setattr(vault, '_snapshot_selection', forbidden)
    monkeypatch.setattr(custody, 'capture', forbidden)
    settled = capture.settle(OWNER, facts['workflow_id'])
    assert settled['vault'] == 'confirmed' and settled['authorization'] != 'confirmed'
    assert prefs.capture(OWNER).raw == before and len(observed) == 1

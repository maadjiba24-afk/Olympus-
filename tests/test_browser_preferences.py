"""Owned browser preference authority controls; no browser, provider or credentials."""
from contextlib import closing
import json
import threading

import pytest

from olympus import config, prefs, prefs_native as native
from olympus import prefs_recovery_records as recovery
from olympus import browser_credential_authority as authority
from olympus import browser_credential_policy as policy
from olympus import browser_credential_records as records
from olympus.vault_storage import _Directory, _directory_identity

OWNER = 'cli'
SITE = 'owned.example'
OP = '3' * 32


@pytest.fixture
def owned(tmp_path, monkeypatch):
    root = tmp_path / 'owned-browser-policy'
    root.mkdir()
    monkeypatch.setattr(config, 'MEMORY_DIR', root)
    monkeypatch.setattr(native, '_GATE', threading.RLock())
    monkeypatch.setattr(native, '_LOCAL', threading.local())
    monkeypatch.setattr(native, '_IN_FLIGHT', False)
    monkeypatch.setattr(native, '_POISONED', False)
    monkeypatch.setattr(prefs, '_TRANSFORM', threading.local())
    for name in ('OLYMPUS_OPERATOR', 'OLYMPUS_OPERATOR_DOMAINS', 'OLYMPUS_SOVEREIGN',
                 'OLYMPUS_EGRESS_ALLOWLIST', 'OLYMPUS_DATABASE_URL', 'OLYMPUS_SECRET_KEY',
                 'OLYMPUS_SECRET_KEY_FILE'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('OLYMPUS_ENV', 'test')
    monkeypatch.setattr(policy.providers, 'local_provider_hosts', lambda: set())
    return root


def _grant():
    prefs.set(OWNER, 'operator', {'enabled': True, 'sites': {SITE: {'login': 'remember'}}})


def _legacy(root):
    parts = prefs._relative_paths(OWNER)[1]
    with native.transaction(root) as tx:
        tx.publish(parts, b'{}')
    return root.joinpath(*parts)


@pytest.mark.parametrize('identity', [None, (), (1,), ('owned', 1), (True, 1)])
def test_invalid_captured_identity_refuses_before_native_selection(owned, monkeypatch, identity):
    monkeypatch.setattr(native, 'transaction', lambda *a, **k: pytest.fail('invalid identity reached native selection'))
    with pytest.raises(prefs.PreferenceConflict):
        prefs._capture_for_credentials(OWNER, owned, identity)
    assert list(owned.iterdir()) == []


def test_replaced_captured_root_receives_no_preference_coordination(owned):
    with closing(_Directory(owned)) as held:
        identity = _directory_identity(held)
    retained = owned.with_name('retained-browser-original')
    owned.rename(retained)
    owned.mkdir()
    with pytest.raises(prefs.PreferenceOperationError):
        prefs._capture_for_credentials(OWNER, owned, identity)
    assert list(owned.iterdir()) == [] and list(retained.iterdir()) == []


def test_private_capture_uses_frozen_root_without_ambient_selector(owned, tmp_path, monkeypatch):
    _grant()
    with closing(_Directory(owned)) as held:
        identity = _directory_identity(held)
    other = tmp_path / 'unselected-browser-policy'
    monkeypatch.setattr(config, 'MEMORY_DIR', other)
    observed = prefs._capture_for_credentials(OWNER, owned, identity)
    assert observed.data()['operator']['sites'][SITE]['login'] == 'remember'
    assert observed.binding.root == owned and not other.exists()


def test_pending_recovery_refuses_even_valid_current_policy_and_environment_grant(owned, monkeypatch):
    _grant()
    current = owned.joinpath(*prefs._relative_paths(OWNER)[0])
    before = current.read_bytes()
    legacy = _legacy(owned)
    replace = recovery.replace_pointer
    def pending(tx, parts, prior, value):
        if value.get('state') == 'complete':
            raise OSError('owned completion acknowledgment held pending')
        return replace(tx, parts, prior, value)
    with monkeypatch.context() as patch:
        patch.setattr(recovery, 'replace_pointer', pending)
        with pytest.raises(prefs.PreferenceOperationError):
            prefs.discard_legacy(OWNER, operation_id=OP)
    assert not legacy.exists() and current.read_bytes() == before
    monkeypatch.setenv('OLYMPUS_OPERATOR', 'true')
    monkeypatch.setenv('OLYMPUS_OPERATOR_DOMAINS', SITE)
    with authority.capture() as captured:
        with pytest.raises(records.BrowserCredentialError):
            policy.snapshot(OWNER, SITE, captured)
    prefs.discard_legacy(OWNER, operation_id=OP)
    with authority.capture() as captured:
        selected = policy.snapshot(OWNER, SITE, captured)
        assert selected.allowed and selected.permits_destination(SITE)
    assert current.read_bytes() == before


def test_completed_recovery_invalidates_old_policy_even_when_current_bytes_match(owned):
    _grant()
    target = owned.joinpath(*prefs._relative_paths(OWNER)[0])
    before = target.read_bytes()
    with authority.capture() as captured:
        selected = policy.snapshot(OWNER, SITE, captured)
        assert selected.allowed
        _legacy(owned)
        prefs.discard_legacy(OWNER, operation_id=OP)
        assert target.read_bytes() == before
        with pytest.raises(records.BrowserCredentialError):
            selected.verify(captured)
        refreshed = policy.snapshot(OWNER, SITE, captured)
        assert refreshed.allowed


@pytest.mark.parametrize('fault_type', [OSError, KeyboardInterrupt, SystemExit])
def test_preference_cleanup_uncertainty_survives_browser_policy_guard(owned, monkeypatch, fault_type):
    _grant()
    fault = fault_type('owned-private-preference-cleanup')
    fault.__notes__ = 123
    close = native.Transaction.close
    closed = []
    def lost_ack(tx):
        was_held = tx.held and not tx.closed
        close(tx)
        assert was_held and tx.closed and not tx.held
        closed.append(True)
        raise fault
    with authority.capture() as captured:
        with monkeypatch.context() as patch:
            patch.setattr(native.Transaction, 'close', lost_ack)
            expected = records.BrowserCredentialError if fault_type is OSError else fault_type
            with pytest.raises(expected) as caught:
                policy.snapshot(OWNER, SITE, captured)
            if fault_type is not OSError:
                assert caught.value is fault
            assert authority._cleanup_unknown(caught.value)
    assert closed == [True] and native._POISONED and not native._IN_FLIGHT


@pytest.mark.parametrize('replacement', ['lock-file', 'locks-directory'])
def test_captured_policy_refuses_replaced_preference_coordination(owned, replacement):
    _grant()
    # Browser authority also owns a locks-directory handle. Release it before
    # the owned fixture rename, then reacquire independently for verification.
    with authority.capture() as captured:
        selected = policy.snapshot(OWNER, SITE, captured)
    path = owned.joinpath(*native.coordination_path()) if replacement == 'lock-file' else owned / 'locks'
    retained = path.with_name('retained-browser-coordination')
    path.rename(retained)
    if replacement == 'lock-file':
        path.write_bytes(b'owned replacement lock')
    else:
        path.mkdir()
    with authority.capture() as captured:
        with pytest.raises(records.BrowserCredentialError):
            selected.verify(captured)
    assert retained.exists()
    if replacement == 'locks-directory':
        assert not owned.joinpath(*native.coordination_path()).exists()
    else:
        assert path.read_bytes() == b'owned replacement lock'


def test_policy_cas_refuses_revocation_after_snapshot(owned):
    _grant()
    with authority.capture() as captured:
        selected = policy.snapshot(OWNER, SITE, captured)
        prefs.set(OWNER, 'operator', {'enabled': False, 'sites': {}})
        with pytest.raises(records.BrowserCredentialError):
            selected.verify(captured)
        assert not policy.snapshot(OWNER, SITE, captured).allowed


@pytest.mark.parametrize('raw', [b'{"operator":{},"operator":{}}', b'{"operator":NaN}', b'[]'])
def test_policy_refuses_non_strict_current_json_despite_environment_grant(owned, monkeypatch, raw):
    parts = prefs._relative_paths(OWNER)[0]
    with native.transaction(owned) as tx:
        tx.publish(parts, raw)
    monkeypatch.setenv('OLYMPUS_OPERATOR', 'true')
    monkeypatch.setenv('OLYMPUS_OPERATOR_DOMAINS', SITE)
    with authority.capture() as captured:
        with pytest.raises(records.BrowserCredentialError):
            policy.snapshot(OWNER, SITE, captured)
    assert owned.joinpath(*parts).read_bytes() == raw

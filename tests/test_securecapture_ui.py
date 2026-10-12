"""Private local input and status integration; owned data, no providers or browser."""
from collections import OrderedDict
import getpass
import json
from pathlib import Path
from types import SimpleNamespace
import warnings

import pytest

from olympus import approvals, config, interactive, memory, native_cleanup, operator
from olympus import prefs, prefs_native, securecapture as capture, store, tools, tui, vault

OWNER = 'owned-capture-ui'
SITE = 'example.test'
PASSWORD = 'owned-ui-password'


def forbidden(*args, **kwargs):
    pytest.fail('private local workflow reached an unselected interaction')


@pytest.fixture
def owned(tmp_path, monkeypatch):
    root = tmp_path / 'memory'
    root.mkdir()
    monkeypatch.setattr(config, 'MEMORY_DIR', root)
    monkeypatch.setattr(store, '_backend', store.FileStore())
    monkeypatch.delenv('OLYMPUS_DATABASE_URL', raising=False)
    monkeypatch.delenv('OLYMPUS_SECRET_KEY_FILE', raising=False)
    monkeypatch.setenv('OLYMPUS_SECRET_KEY', 'owned-ui-key')
    monkeypatch.setenv('OLYMPUS_ENV', 'test')
    monkeypatch.setattr(capture, '_WORKFLOWS', OrderedDict())
    monkeypatch.setattr(capture, '_ACTIVE', {})
    monkeypatch.setattr(approvals, 'pending', lambda user: [])
    memory.set_user(OWNER)
    return root


def persisted_credentials(root, *, present):
    # No ambient selector, workflow handle, custody callback or public vault read.
    with vault.storage.transaction(store.FileStore(), root) as tx:
        blob = tx.get(vault._NS, vault._key(OWNER), cap=vault._ROW_CAP)
    if not present:
        assert blob is None
        return
    assert type(blob) is bytes and blob
    entries, metadata = vault.records.decode(OWNER, blob, b'owned-ui-key')
    assert metadata.context_authenticated
    assert entries == {'site:' + SITE: {'username': 'owned-user', 'password': PASSWORD}}


def forbid_status_io(monkeypatch):
    monkeypatch.setattr(prefs, 'capture', forbidden)
    monkeypatch.setattr(prefs_native, 'transaction', forbidden)
    monkeypatch.setattr(vault, '_transaction', forbidden)
    monkeypatch.setattr(vault.storage, 'transaction', forbidden)
    monkeypatch.setattr(vault.custody, 'capture', forbidden)
    monkeypatch.setattr(vault, '_snapshot_selection', forbidden)
    monkeypatch.setattr(store, 'backend', forbidden)


class PrivateIO:
    def __init__(self, username='owned-user', password=PASSWORD):
        self.username, self.password = username, password
        self.calls, self.output = [], []
    def line(self, message):
        assert not capture._LOCK._is_owned() and not prefs_native._IN_FLIGHT
        self.calls.append('username')
        facts = capture.active_status(OWNER)
        record = prefs.get(OWNER, 'pending_secure_login')
        assert facts['prompt_dispatched'] and record['state'] == 'prompting'
        assert record['claim_id'] == facts['workflow_id'] and prefs.get(OWNER, 'operator') is None
        return self.username
    def secret(self, message):
        assert not capture._LOCK._is_owned() and not prefs_native._IN_FLIGHT
        self.calls.append('password')
        return self.password
    def out(self, message):
        assert PASSWORD not in message
        self.output.append(message)


@pytest.mark.requires_crypto
def test_private_input_follows_claim_and_uses_frozen_root_and_key(owned, monkeypatch):
    capture.request(OWNER, SITE)
    other = owned.parent / 'other'
    other.mkdir()
    class IO(PrivateIO):
        def secret(self, message):
            result = super().secret(message)
            monkeypatch.setattr(config, 'MEMORY_DIR', other)
            monkeypatch.setenv('OLYMPUS_SECRET_KEY', 'owned-unselected-key')
            monkeypatch.setattr(store, '_backend', None)
            return result
    io = IO()
    assert interactive.after_turn(OWNER, io) == ['saved-login:' + SITE]
    assert io.calls == ['username', 'password'] and list(other.iterdir()) == []
    facts = capture.active_status(OWNER)
    assert facts['state'] == 'complete' and facts['vault'] == facts['authorization'] == 'confirmed'
    assert facts['workflow_id'] in io.output[0]
    assert 'authorization confirmed' in io.output[-1]
    assert all(PASSWORD.encode() not in p.read_bytes() for p in owned.rglob('*') if p.is_file())
    persisted_credentials(owned, present=True)


@pytest.mark.requires_crypto
def test_private_input_revocation_refuses_vault_dispatch_without_rebase(owned, monkeypatch):
    capture.request(OWNER, SITE)
    class IO(PrivateIO):
        def secret(self, message):
            result = super().secret(message)
            prefs.set(OWNER, 'owned_revocation', True)
            return result
    monkeypatch.setattr(vault, 'settle_snapshot_update', forbidden)
    io = IO()
    events = interactive.after_turn(OWNER, io)
    facts = capture.active_status(OWNER)
    assert events == ['login-unconfirmed:' + facts['workflow_id']]
    assert facts['vault'] == facts['authorization'] == 'not_attempted' and facts['state'] == 'unconfirmed'
    assert prefs.get(OWNER, 'owned_revocation') is True and prefs.get(OWNER, 'operator') is None
    assert not any('Credentials stored and' in message for message in io.output)
    persisted_credentials(owned, present=False)


@pytest.mark.requires_crypto
@pytest.mark.parametrize('blank', ['username', 'password'])
@pytest.mark.parametrize('fault', [RuntimeError, KeyboardInterrupt, SystemExit])
def test_blank_input_cancellation_lost_ack_is_dispatched_once(owned, monkeypatch, blank, fault):
    capture.request(OWNER, SITE)
    original, canceled = prefs.settle_mutation, []
    primary = fault('owned-private-cancel-ack')
    def settle(owner, pending):
        facts = original(owner, pending)
        if json.loads(pending.after).get('pending_secure_login') is None:
            canceled.append(pending)
            raise primary
        return facts
    monkeypatch.setattr(prefs, 'settle_mutation', settle)
    io = PrivateIO(username='' if blank == 'username' else 'owned-user', password='' if blank == 'password' else PASSWORD)
    if fault is RuntimeError:
        interactive.after_turn(OWNER, io)
    else:
        with pytest.raises(fault) as caught:
            interactive.after_turn(OWNER, io)
        assert caught.value is primary
    assert len(canceled) == 1
    assert io.calls == (['username'] if blank == 'username' else ['username', 'password'])
    facts = capture.active_status(OWNER)
    assert facts['cancellation'] == 'unconfirmed' and facts['vault'] == 'not_attempted'
    assert prefs.get(OWNER, 'pending_secure_login') is None and prefs.get(OWNER, 'operator') is None


@pytest.mark.requires_crypto
@pytest.mark.parametrize('prompt_fault', [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('cancel_fault', [None, RuntimeError, KeyboardInterrupt, SystemExit])
def test_actual_prompt_interrupt_survives_cancellation_cleanup_failure(owned, monkeypatch, prompt_fault, cancel_fault):
    capture.request(OWNER, SITE)
    primary = prompt_fault('owned-private-prompt-interrupt')
    secondary = cancel_fault('owned-private-cancel-cleanup') if cancel_fault else None
    if secondary is not None:
        prefs_native._mark_cleanup(secondary)
    original, canceled = prefs.settle_mutation, []
    def settle(owner, pending):
        result = original(owner, pending)
        if json.loads(pending.after).get('pending_secure_login') is None:
            canceled.append(pending)
            if secondary is not None:
                raise secondary
        return result
    monkeypatch.setattr(prefs, 'settle_mutation', settle)
    class IO(PrivateIO):
        def secret(self, message):
            self.calls.append('password')
            raise primary
    io = IO()
    with pytest.raises(prompt_fault) as caught:
        interactive.after_turn(OWNER, io)
    assert caught.value is primary and len(canceled) == 1 and io.calls == ['username', 'password']
    facts = capture.active_status(OWNER)
    assert primary.__dict__['_secure_capture_workflow']['workflow_id'] == facts['workflow_id']
    assert primary.__dict__['_secure_capture_workflow']['cleanup_unconfirmed'] is (secondary is not None)
    assert native_cleanup.unknown(primary) is (secondary is not None)
    assert facts['vault'] == 'not_attempted' and len(io.output) == 1


def test_getpass_warning_stops_before_fallback_input(monkeypatch):
    fallback = []
    def unsafe(prompt):
        warnings.warn('owned synthetic fallback warning', getpass.GetPassWarning)
        fallback.append(True)
        return PASSWORD
    monkeypatch.setattr(getpass, 'getpass', unsafe)
    with pytest.raises(getpass.GetPassWarning):
        interactive.IO().secret('private input')
    assert fallback == []


@pytest.mark.requires_crypto
def test_getpass_warning_cancels_claim_without_reading_password(owned, monkeypatch, capsys):
    capture.request(OWNER, SITE)
    fallback = []
    monkeypatch.setattr('builtins.input', lambda prompt: 'owned-user')
    def unsafe(prompt):
        warnings.warn('owned synthetic fallback warning', getpass.GetPassWarning)
        fallback.append(True)
        return PASSWORD
    monkeypatch.setattr(getpass, 'getpass', unsafe)
    assert interactive.after_turn(OWNER, interactive.IO()) == []
    facts = capture.active_status(OWNER)
    assert facts['state'] == 'canceled' and facts['cancellation'] == 'confirmed' and facts['vault'] == 'not_attempted'
    assert fallback == [] and PASSWORD not in capsys.readouterr().out


@pytest.mark.requires_crypto
@pytest.mark.parametrize('outcome', ['confirmed', 'vault-ack', 'revoked'])
@pytest.mark.parametrize('fault', [OSError, KeyboardInterrupt, SystemExit])
def test_reporting_failure_retains_actual_known_facts_without_repeating_work(owned, monkeypatch, outcome, fault):
    capture.request(OWNER, SITE)
    original, updates = vault.settle_snapshot_update, []
    def settle(pending):
        updates.append(pending)
        result = original(pending)
        if outcome == 'vault-ack':
            raise RuntimeError('owned-private-vault-ack')
        return result
    monkeypatch.setattr(vault, 'settle_snapshot_update', settle)
    primary = fault('owned-private-output')
    class IO(PrivateIO):
        def secret(self, message):
            result = super().secret(message)
            if outcome == 'revoked':
                prefs.set(OWNER, 'owned_revocation', True)
            return result
        def out(self, message):
            super().out(message)
            if len(self.output) == 2:
                raise primary
    io = IO()
    with pytest.raises(capture.SecureCaptureError if fault is OSError else fault) as caught:
        interactive.after_turn(OWNER, io)
    facts = capture.active_status(OWNER)
    known = caught.value.__dict__['_secure_capture_workflow']
    assert known == {key: value for key, value in facts.items() if key != 'domain'}
    assert caught.value.__dict__['_secure_capture_reporting_unconfirmed'] is True
    assert not native_cleanup.unknown(caught.value) and not facts['cleanup_unconfirmed']
    if fault is OSError:
        assert caught.value.__cause__ is None and caught.value.__context__ is None
        assert 'owned-private-output' not in str(caught.value)
    else:
        assert caught.value is primary
    assert len(updates) == (0 if outcome == 'revoked' else 1)
    assert facts['vault'] == {'confirmed': 'confirmed', 'vault-ack': 'unconfirmed', 'revoked': 'not_attempted'}[outcome]
    assert io.calls == ['username', 'password'] and facts['workflow_id'] in io.output[0]
    persisted_credentials(owned, present=outcome != 'revoked')
    if outcome != 'confirmed':
        assert prefs.get(OWNER, 'operator') is None
    else:
        assert prefs.get(OWNER, 'operator')['sites'][SITE]['login'] == 'remember'
    forbid_status_io(monkeypatch)
    monkeypatch.setattr(vault, 'settle_snapshot_update', forbidden)
    assert interactive.login_command(OWNER, 'status')
    assert capture.active_status(OWNER) == facts


@pytest.mark.requires_crypto
def test_local_settlement_and_status_never_prompt_or_select_new_authority(owned, monkeypatch):
    capture.request(OWNER, SITE)
    original, reached = prefs.settle_mutation, []
    def settle(owner, pending):
        result = original(owner, pending)
        reached.append(pending)
        if len(reached) == 1:
            raise RuntimeError('owned-private-claim-ack')
        return result
    monkeypatch.setattr(prefs, 'settle_mutation', settle)
    facts = capture.begin(OWNER)
    assert facts['state'] == 'unconfirmed'
    io = PrivateIO()
    monkeypatch.setattr(vault, '_snapshot_selection', forbidden)
    assert 'ready' in interactive.login_command(OWNER, 'settle ' + facts['workflow_id'], io)
    assert io.calls == [] and len(reached) == 2 and reached[0] is reached[1]
    forbid_status_io(monkeypatch)
    handled, text, exited = tui.dispatch_command(SimpleNamespace(user=OWNER), '/login status')
    assert handled and not exited and facts['workflow_id'] in text
    assert 'unavailable' in interactive.login_command('owned-other', 'status ' + facts['workflow_id'])
    assert io.calls == []


@pytest.mark.requires_crypto
def test_local_resume_uses_named_ready_workflow_once(owned):
    capture.request(OWNER, SITE)
    facts = capture.begin(OWNER)
    io = PrivateIO()
    assert 'authorization confirmed' in interactive.login_command(OWNER, 'resume ' + facts['workflow_id'], io)
    assert io.calls == ['username', 'password']
    assert 'authorization confirmed' in interactive.login_command(OWNER, 'resume ' + facts['workflow_id'], io)
    assert io.calls == ['username', 'password']
    persisted_credentials(owned, present=True)
    assert prefs.get(OWNER, 'operator')['sites'][SITE]['login'] == 'remember'


@pytest.mark.parametrize('path', ['remember-tool', 'authorize-tool'])
def test_both_remember_tools_request_without_preauthorization(owned, monkeypatch, path):
    monkeypatch.setattr(operator, 'authorize_site', forbidden)
    result = (tools._operator_remember_login(SITE) if path == 'remember-tool'
              else tools._operator_authorize_site(SITE, 'remember'))
    assert 'private prompt' in result and 'vault' in result
    record = prefs.get(OWNER, 'pending_secure_login')
    assert record['state'] == 'requested' and record['domain'] == SITE and prefs.get(OWNER, 'operator') is None


def test_direct_operator_private_input_requires_original_workflow(owned, monkeypatch):
    monkeypatch.setattr(vault, 'available', forbidden)
    monkeypatch.setattr(vault, 'put', forbidden)
    monkeypatch.setattr(prefs, 'capture', forbidden)
    with pytest.raises(capture.SecureCaptureError):
        operator.remember_credentials(OWNER, SITE, 'owned-user', PASSWORD)
    assert list(owned.iterdir()) == []

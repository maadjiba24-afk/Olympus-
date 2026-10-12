"""Captured vault reads and secret-free diagnostics over owned mock transports."""
import base64
import hashlib
import json
import traceback

import pytest

from olympus import browser, browserlease, config, store, vault, vault_records


@pytest.fixture(autouse=True)
def owned_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'MEMORY_DIR', tmp_path / 'owned-memory')
    monkeypatch.setenv('OLYMPUS_SECRET_KEY', 'owned-browser-primitives-key')
    monkeypatch.setenv('OLYMPUS_ENV', 'test')
    monkeypatch.delenv('OLYMPUS_SECRET_KEY_FILE', raising=False)
    monkeypatch.delenv('OLYMPUS_DATABASE_URL', raising=False)
    store.reset()
    browser.set_transport_factory(None)
    yield
    browser.set_transport_factory(None)
    store.reset()


def test_batch_reads_one_capture_and_revision_with_detached_values(monkeypatch):
    vault.put('cli', 'site:owned.test', {'username': 'owned-user', 'password': 'owned-private-password'})
    vault.put('cli', 'cookies:owned.test', {'cookies': []})
    original = vault._capture
    calls = []
    def capture(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(vault, '_capture', capture)
    batch = vault.snapshots('cli', ['site:owned.test', 'cookies:owned.test'])
    assert calls == [{'snapshot_selection': True}]
    first, second = batch.values()
    assert first._captured is second._captured
    assert first._ciphertext_sha256 == second._ciphertext_sha256
    assert first._storage_identity == second._storage_identity
    first.value['password'] = 'changed-copy'
    assert vault.get('cli', 'site:owned.test')['password'] == 'owned-private-password'
    assert 'owned-private-password' not in repr(batch)


@pytest.mark.parametrize('names', [None, 'one', [], (), ['one'] * 2, ['one', ''],
    ['one', 1], ['x' * 257], ['x\x00y'], list(map(str, range(33)))])
def test_invalid_batch_names_refuse_before_capture(monkeypatch, names):
    calls = []
    monkeypatch.setattr(vault, '_capture', lambda *a, **k: calls.append('capture'))
    with pytest.raises(vault.VaultError):
        vault.snapshots('cli', names)
    assert calls == []


def test_foreign_batch_container_is_not_iterated(monkeypatch):
    calls = []
    class Foreign(list):
        def __iter__(self):
            calls.append('iteration')
            raise AssertionError('foreign list')
    monkeypatch.setattr(vault, '_capture', lambda *a, **k: calls.append('capture'))
    with pytest.raises(vault.VaultError):
        vault.snapshots('cli', Foreign(['one']))
    assert calls == []


@pytest.mark.parametrize('mode', [None, 0, 1, 'true'])
def test_batch_mode_requires_exact_boolean_before_capture(monkeypatch, mode):
    calls = []
    monkeypatch.setattr(vault, '_capture', lambda *a, **k: calls.append('capture'))
    with pytest.raises(vault.VaultError):
        vault.snapshots('cli', ['one'], for_update=mode)
    assert calls == []


def test_batch_missing_and_legacy_records_remain_distinct():
    from cryptography.fernet import Fernet
    key = base64.urlsafe_b64encode(hashlib.sha256(b'owned-browser-primitives-key').digest())
    before = Fernet(key).encrypt(json.dumps({'one': 'owned-private-legacy'}).encode())
    store.backend().put('vault', vault._key('cli'), before)
    batch = vault.snapshots('cli', ('one', 'missing'))
    assert batch['one'].value == 'owned-private-legacy'
    assert batch['missing'].value is None
    assert all(item.legacy for item in batch.values())
    with pytest.raises(vault.VaultMigrationRequired):
        vault.require_snapshot_writable(batch['missing'])
    with pytest.raises(vault.VaultMigrationRequired):
        vault.snapshots('cli', ['one'], for_update=True)
    assert store.backend().get('vault', vault._key('cli')) == before


def test_batch_settlement_uses_original_root_key_backend_and_conflicts_siblings(tmp_path, monkeypatch):
    vault.put('cli', 'one', 'before-one')
    vault.put('cli', 'two', 'before-two')
    original_root = config.MEMORY_DIR
    batch = vault.snapshots('cli', ('one', 'two'), for_update=True)
    monkeypatch.setattr(config, 'MEMORY_DIR', tmp_path / 'unselected-memory')
    monkeypatch.setenv('OLYMPUS_SECRET_KEY', 'unselected-key')
    store.reset()
    first = vault.prepare_snapshot_update(batch['one'], 'after-one')
    second = vault.prepare_snapshot_update(batch['two'], 'after-two')
    assert vault.settle_snapshot_update(first) == 'after-one'
    with pytest.raises(vault.VaultConflict):
        vault.settle_snapshot_update(second)
    assert not config.MEMORY_DIR.exists()
    raw = (original_root / 'store/vault' / vault._key('cli')).read_bytes()
    data, _ = vault_records.decode('cli', raw, b'owned-browser-primitives-key')
    assert data == {'one': 'after-one', 'two': 'before-two'}


def _session():
    transport = browser.FakeTransport(present=['#username', '#password'])
    browser.set_transport_factory(lambda: transport)
    return browser.session('cli'), transport


@pytest.mark.parametrize('operation', ['cookies', 'fill', 'frame'])
def test_sensitive_payload_reaches_transport_once_without_retained_ledger_secrets(operation, monkeypatch):
    session, transport = _session()
    calls = []
    original = transport.send
    def send(method, params=None, **kwargs):
        calls.append((method, params, kwargs))
        return original(method, params, **kwargs)
    monkeypatch.setattr(transport, 'send', send)
    secret = 'owned-private-browser-payload'
    if operation == 'cookies':
        payload = [{'name': 'sid', 'value': secret, 'domain': 'owned.test'}]
        assert session.set_cookies(payload) == 1
        assert calls[0][1] == {'cookies': payload}
    elif operation == 'fill':
        assert session.fill('#password', secret)
        assert secret in calls[0][1]['expression']
    else:
        session.fill_in('owned-child-frame', '#password', secret)
        assert secret in calls[0][1]['expression']
        assert calls[0][2]['session_id'] == 'owned-child-frame'
    assert len(calls) == 1
    assert list(session.ledger) == [{'method': calls[0][0], 'params': {'redacted': True}}]
    assert secret not in repr(session.ledger) + repr(session.journal) + repr(session.recipe)


def test_focused_field_typing_omits_secret_from_ledger_and_status(monkeypatch):
    session, transport = _session()
    calls = []
    original = transport.send
    def send(method, params=None, **kwargs):
        calls.append((method, params))
        return original(method, params, **kwargs)
    monkeypatch.setattr(transport, 'send', send)
    secret = 'owned-private-focused-value'
    result = session.act('type', text=secret)
    assert calls == [('Input.insertText', {'text': secret})]
    assert list(session.ledger) == [{'method': 'Input.insertText', 'params': {'redacted': True}}]
    assert secret not in result + repr(session.ledger) + repr(session.journal) + repr(session.recipe)


def test_ordinary_diagnostics_still_retain_nonsecret_parameters():
    session, _ = _session()
    session._call('Runtime.evaluate', expression='1 + 1')
    assert session.ledger[-1]['params'] == {'expression': '1 + 1'}


def test_sensitive_transport_failure_is_unconfirmed_without_private_exception_chain(monkeypatch):
    session, transport = _session()
    calls = []
    def fail(*args, **kwargs):
        calls.append('sent')
        raise RuntimeError('owned-private-browser-failure')
    monkeypatch.setattr(transport, 'send', fail)
    private_value = 'owned-private-browser-value'
    with pytest.raises(browser.BrowserUnavailable, match='completion is unconfirmed') as raised:
        session.fill('#password', private_value)
    assert calls == ['sent']
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert 'owned-private-' not in ''.join(traceback.format_exception(raised.value))
    assert 'owned-private-' not in repr(session.ledger)


@pytest.mark.parametrize('interrupt', [KeyboardInterrupt(), SystemExit(13)])
def test_sensitive_transport_preserves_original_system_interrupt(monkeypatch, interrupt):
    session, transport = _session()
    calls = []
    def fail(*args, **kwargs):
        calls.append('sent')
        raise interrupt
    monkeypatch.setattr(transport, 'send', fail)
    with pytest.raises(type(interrupt)) as raised:
        session.fill('#password', 'owned-private-interrupted-value')
    assert raised.value is interrupt and calls == ['sent']
    assert 'owned-private-' not in repr(session.ledger)


@pytest.mark.parametrize('loss', ['before_navigation', 'after_navigation', 'after_username', 'after_password'])
def test_login_rechecks_ownership_before_credential_use_and_submission(monkeypatch, loss):
    # This is an owned fake destination; the independent SSRF guard has its own tests.
    monkeypatch.setattr('olympus.security.url_block_reason', lambda *args, **kwargs: None)
    session, transport = _session()
    profile = browser.SiteProfile(domain='owned.test', login_url='https://owned.test/login',
        username_selector='#username', password_selector='#password', submit_selector='#submit')
    events = []
    replacement_bytes = []
    def replace_generation():
        # Change the owned record itself: factory sessions now verify native
        # captured authority rather than the legacy path-level verify helper.
        path = config.MEMORY_DIR / 'browser_lease.json'
        record = json.loads(path.read_bytes())
        assert record['owner'] == 'cli'
        record['lease_id'] = 'f' * 32 if record['lease_id'] != 'f' * 32 else 'e' * 32
        raw = json.dumps(record).encode('utf-8')
        path.write_bytes(raw)
        replacement_bytes.append(raw)
    if loss == 'before_navigation':
        replace_generation()
    def navigate(url):
        events.append('navigate')
        transport._url = url  # The owned navigation actually changes the current page.
        if loss == 'after_navigation':
            replace_generation()
        return 'owned page'
    def fill(selector, value):
        events.append(selector)
        if loss == 'after_username' or (loss == 'after_password' and selector == '#password'):
            replace_generation()
        return True
    monkeypatch.setattr(session, 'open', navigate)
    monkeypatch.setattr(session, 'fill', fill)
    monkeypatch.setattr(session, 'act', lambda *a, **k: events.append('submit'))
    with pytest.raises(browserlease.OwnershipRefused):
        session.login(profile, {'username': 'owned-user', 'password': 'owned-private-password'})
    assert len(replacement_bytes) == 1
    assert (config.MEMORY_DIR / 'browser_lease.json').read_bytes() == replacement_bytes[0]
    assert events == {'before_navigation': [], 'after_navigation': ['navigate'],
        'after_username': ['navigate', '#username'],
        'after_password': ['navigate', '#username', '#password']}[loss]

"""Factory and teardown lifetime controls; only owned files and FakeTransport."""
import json
import os
from types import SimpleNamespace

import pytest

from olympus import browser, browserlease, config
from olympus import browser_credential_authority as authority


@pytest.fixture
def owned(tmp_path, monkeypatch):
    root, other = tmp_path / 'memory', tmp_path / 'other-memory'
    root.mkdir()
    other.mkdir()
    monkeypatch.setattr(config, 'MEMORY_DIR', root)
    # Restore test-owned module state directly through monkeypatch. Teardown
    # must not retry an intentionally uncertain simulated transport action.
    for name in ('_session', '_factory_attempt', '_release_attempt', '_launched'):
        monkeypatch.setattr(browser, name, None)
    transports = []
    def build():
        transport = browser.FakeTransport(present=['#username', '#password', '#success'])
        transports.append(transport)
        return transport
    monkeypatch.setattr(browser, '_TRANSPORT_FACTORY', build)
    monkeypatch.setattr('olympus.security.url_block_reason', lambda *args, **kwargs: None)
    return SimpleNamespace(root=root, other=other, transports=transports)


def _close_count(transport):
    return sum(call['method'] == '_close' for call in transport.calls)


def _profile():
    return browser.SiteProfile(domain='owned.test', login_url='https://owned.test/',
        username_selector='#username', password_selector='#password',
        submit_selector='#submit', success_selector='#success')


def _credential_call(session, operation, captured=None):
    if operation == 'get':
        return session.get_cookies('owned.test', _authority=captured)
    if operation == 'set':
        return session.set_cookies([{'name': 'sid', 'value': 'owned-private', 'domain': 'owned.test'}],
                                   _authority=captured)
    return session.login(_profile(), {'username': 'owned-user', 'password': 'owned-private'},
                         _authority=captured)


def test_first_use_creates_and_binds_memory_root_without_handoff(owned, monkeypatch):
    absent = owned.root / 'first-use'
    monkeypatch.setattr(config, 'MEMORY_DIR', absent)
    session = browser.session('cli')
    assert absent.is_dir() and len(owned.transports) == 1
    assert session._credential_binding.root == absent
    with authority.capture(binding=session._credential_binding) as captured:
        assert captured.verify(session._credential_binding, 'cli')['owner'] == 'cli'
    assert browser.session('cli') is session
    assert len(owned.transports) == 1


@pytest.mark.parametrize('invalid', [None, 1, 'true'])
def test_first_use_flag_is_exact_boolean(owned, invalid):
    with pytest.raises(browserlease.OwnershipRefused):
        authority.Authority(initialize_root=invalid)


def test_existing_binding_cannot_initialize_a_replacement_root(owned):
    session = browser.session('cli')
    with pytest.raises(browserlease.OwnershipRefused):
        authority.Authority(binding=session._credential_binding, initialize_root=True)


def test_first_use_keeps_native_root_held_through_coordination_acquisition(owned, monkeypatch):
    native_child = authority.vault_storage._Directory.child
    moved = []
    def replace_at_coordination(directory, name, *args, **kwargs):
        if directory.path == owned.root and name == 'locks' and not moved:
            try:
                owned.root.rename(owned.root.with_name('retained-root'))
            except PermissionError as error:
                assert os.name == 'nt' and getattr(error, 'winerror', None) in (5, 32, 33)
                moved.append(False)
            else:
                owned.root.mkdir()
                moved.append(True)
        return native_child(directory, name, *args, **kwargs)
    monkeypatch.setattr(authority.vault_storage._Directory, 'child', replace_at_coordination)
    if os.name == 'nt':
        assert browser.session('cli')._credential_binding.root == owned.root
        assert moved == [False]
    else:
        with pytest.raises(browserlease.OwnershipRefused):
            browser.session('cli')
        assert moved == [True] and owned.transports == []
        assert not (owned.root / 'locks').exists()


@pytest.mark.parametrize('operation', ['get', 'set', 'login'])
def test_saved_reference_refuses_same_owner_replacement_generation(owned, operation):
    session = browser.session('cli')
    path = owned.root / 'browser_lease.json'
    record = json.loads(path.read_bytes())
    record['lease_id'] = 'b' * 32
    path.write_text(json.dumps(record), encoding='utf-8')
    before = list(owned.transports[0].calls)
    with pytest.raises(browserlease.OwnershipRefused):
        _credential_call(session, operation)
    with pytest.raises(browserlease.OwnershipRefused):
        browser.session('cli')
    assert owned.transports[0].calls == before and len(owned.transports) == 1


def test_same_owner_foreign_root_cannot_drive_or_destroy_original_session(owned, monkeypatch):
    session = browser.session('cli')
    first_bytes = (owned.root / 'browser_lease.json').read_bytes()
    monkeypatch.setattr(config, 'MEMORY_DIR', owned.other)
    with authority.capture() as other:
        other.claim('cli', browser.FAKE)
    second_bytes = (owned.other / 'browser_lease.json').read_bytes()
    before = list(owned.transports[0].calls)
    for action in (lambda: browser.session('cli'), lambda: session.get_cookies('owned.test'),
                   lambda: browser.relinquish('cli')):
        with pytest.raises(browserlease.OwnershipRefused):
            action()
    assert owned.transports[0].calls == before
    assert (owned.root / 'browser_lease.json').read_bytes() == first_bytes
    assert (owned.other / 'browser_lease.json').read_bytes() == second_bytes


@pytest.mark.parametrize('kind', [RuntimeError, KeyboardInterrupt, SystemExit])
def test_returned_transport_survives_wrapper_fault_and_build_is_not_repeated(owned, monkeypatch, kind):
    primary = kind('owned-wrapper-fault')
    def failed_wrapper(*args, **kwargs):
        raise primary
    monkeypatch.setattr(browser, 'BrowserSession', failed_wrapper)
    with pytest.raises(kind) as raised:
        browser.session('cli')
    assert raised.value is primary
    attempt = browser._factory_attempt
    assert attempt.transport is owned.transports[0] and attempt.transport_returned
    assert attempt.wrapper is None and not attempt.claim_confirmed
    with pytest.raises(browserlease.OwnershipRefused):
        browser.session('cli')
    assert len(owned.transports) == 1
    browser.reset()
    assert _close_count(owned.transports[0]) == 1
    assert browser._factory_attempt is None and browser._session is None


@pytest.mark.parametrize('kind', [OSError, KeyboardInterrupt, SystemExit])
def test_pending_close_after_effect_failure_blocks_repeated_close_or_build(owned, monkeypatch, kind):
    def failed_wrapper(*args, **kwargs):
        raise RuntimeError('owned-wrapper-fault')
    monkeypatch.setattr(browser, 'BrowserSession', failed_wrapper)
    with pytest.raises(RuntimeError):
        browser.session('cli')
    transport = owned.transports[0]
    real_close = transport.close
    primary = kind('owned-private-close-fault')
    def close_then_fail():
        real_close()
        raise primary
    monkeypatch.setattr(transport, 'close', close_then_fail)
    expected = browser.BrowserUnavailable if kind is OSError else kind
    with pytest.raises(expected) as raised:
        browser.reset()
    if kind is not OSError:
        assert raised.value is primary
    assert raised.value._browser_outcome['close_attempted'] is True
    assert raised.value._browser_outcome['close_confirmed'] is False
    with pytest.raises(browser.BrowserUnavailable):
        browser.reset()
    with pytest.raises(browserlease.OwnershipRefused):
        browser.session('cli')
    assert _close_count(transport) == 1 and len(owned.transports) == 1


def test_successful_binding_is_retained_when_outer_cleanup_is_unconfirmed(owned, monkeypatch):
    native_close = authority.vault_storage._Directory.close
    def close_then_fail(directory):
        native_close(directory)
        if directory.path == owned.root:
            raise OSError('owned-private-root-close')
    with monkeypatch.context() as patch:
        patch.setattr(authority.vault_storage._Directory, 'close', close_then_fail)
        with pytest.raises(browserlease.OwnershipRefused) as raised:
            browser.session('cli')
    assert raised.value._browser_outcome['binding_returned'] is True
    assert authority._cleanup_unknown(raised.value)
    assert browser._factory_attempt is None
    session = browser._session
    assert session._credential_binding is not None and not session._credential_factory_unconfirmed
    assert browser.session('cli') is session and len(owned.transports) == 1


def test_login_uses_supplied_authority_and_refuses_drift_before_credentials(owned, monkeypatch):
    session = browser.session('cli')
    filled = []
    def open_then_drift(url):
        monkeypatch.setattr(config, 'MEMORY_DIR', owned.other)
        return 'owned-page'
    monkeypatch.setattr(session, 'open', open_then_drift)
    monkeypatch.setattr(session, 'fill', lambda *args: filled.append(args))
    with authority.capture(binding=session._credential_binding) as captured:
        with pytest.raises(browserlease.OwnershipRefused):
            _credential_call(session, 'login', captured)
    assert filled == [] and owned.transports[0].calls == []


def test_normal_fake_release_destroys_once_then_releases_bound_lease(owned):
    session = browser.session('cli')
    assert browser.relinquish('cli') == ''
    assert _close_count(owned.transports[0]) == 1
    assert not (owned.root / 'browser_lease.json').exists()
    with pytest.raises(browserlease.OwnershipRefused):
        session.get_cookies('owned.test')
    assert browser._session is None and browser._release_attempt is None


@pytest.mark.parametrize('kind', [OSError, KeyboardInterrupt, SystemExit])
def test_unknown_destruction_blocks_old_reference_and_never_repeats_teardown(owned, monkeypatch, kind):
    session = browser.session('cli')
    transport = owned.transports[0]
    real_close = transport.close
    primary = kind('owned-private-destruction-fault')
    def close_then_fail():
        real_close()
        raise primary
    monkeypatch.setattr(transport, 'close', close_then_fail)
    with pytest.raises(kind) as raised:
        browser.relinquish('cli')
    assert raised.value is primary
    assert raised.value._browser_outcome['destruction_started'] is True
    assert raised.value._browser_outcome['destruction_confirmed'] is False
    before = list(transport.calls)
    for operation in ('get', 'set', 'login'):
        with pytest.raises(browserlease.OwnershipRefused):
            _credential_call(session, operation)
    assert 'unconfirmed' in browser.relinquish('cli')
    assert transport.calls == before and _close_count(transport) == 1
    assert (owned.root / 'browser_lease.json').exists()


def test_lost_release_directory_barrier_retries_only_local_settlement(owned, monkeypatch):
    session = browser.session('cli')
    native_sync = authority.vault_storage._Directory.sync
    def missing_barrier(directory):
        if directory.path == owned.root:
            raise OSError('owned-private-release-barrier')
        return native_sync(directory)
    with monkeypatch.context() as patch:
        patch.setattr(authority.vault_storage._Directory, 'sync', missing_barrier)
        with pytest.raises(browserlease.OwnershipRefused) as raised:
            browser.relinquish('cli')
    assert raised.value._browser_outcome['destruction_confirmed'] is True
    assert raised.value._browser_outcome['release_started'] is True
    assert raised.value._browser_outcome['release_confirmed'] is False
    assert not (owned.root / 'browser_lease.json').exists()
    assert _close_count(owned.transports[0]) == 1
    with pytest.raises(browserlease.OwnershipRefused):
        browser.session('cli')
    with pytest.raises(browserlease.OwnershipRefused):
        session.get_cookies('owned.test')
    assert browser.relinquish('cli') == ''
    assert _close_count(owned.transports[0]) == 1 and len(owned.transports) == 1
    assert browser._release_attempt is None


def test_missing_or_unbound_local_state_does_not_authorize_teardown(owned, monkeypatch):
    session = browser.session('cli')
    retained = (owned.root / 'browser_lease.json').read_bytes()
    monkeypatch.setattr(browser, '_session', None)
    assert 'cannot prove' in browser.relinquish('cli')
    unbound = browser.BrowserSession(owned.transports[0], owner='cli')
    monkeypatch.setattr(browser, '_session', unbound)
    with pytest.raises(browserlease.OwnershipRefused):
        browser.relinquish('cli')
    with pytest.raises(browserlease.OwnershipRefused):
        browser.session('cli')
    assert _close_count(owned.transports[0]) == 0
    assert (owned.root / 'browser_lease.json').read_bytes() == retained


@pytest.mark.parametrize('private', [False, True], ids=['ordinary', 'supplied-authority'])
@pytest.mark.parametrize('fault', [OSError, KeyboardInterrupt, SystemExit])
def test_binding_facts_survive_actual_lock_close_failure(owned, monkeypatch, private, fault):
    native_open = authority.Authority._open_lock
    primary = fault('owned-private-lock-close')
    opened = []
    class FileProxy:
        def __init__(self, file): self.file = file
        def __getattr__(self, name): return getattr(self.file, name)
        def close(self):
            self.file.close()
            raise primary
    def open_lock(captured, binding):
        file = native_open(captured, binding)
        opened.append(file)
        return FileProxy(file) if len(opened) == 2 else file
    with monkeypatch.context() as patch:
        patch.setattr(authority.Authority, '_open_lock', open_lock)
        with pytest.raises(fault) as raised:
            if private:
                with authority.capture() as captured:
                    browser.session('cli', _authority=captured)
            else:
                browser.session('cli')
    assert raised.value is primary and authority._cleanup_unknown(primary)
    facts = primary._browser_outcome
    assert facts['binding_returned'] is False
    for name in ('construction_started', 'transport_returned', 'wrapper_returned',
                 'claim_confirmed', 'binding_confirmed'):
        assert facts[name] is True
    assert all(file.closed for file in opened)
    assert browser._factory_attempt is None
    assert browser.session('cli') is browser._session
    assert len(owned.transports) == 1


@pytest.mark.parametrize('fault', [RuntimeError, KeyboardInterrupt, SystemExit])
def test_unobserved_build_stays_blocked_without_a_second_construction(owned, monkeypatch, fault):
    calls = []
    primary = fault('owned-private-build-fault')
    def build():
        calls.append('build')
        raise primary
    monkeypatch.setattr(browser, '_TRANSPORT_FACTORY', build)
    with pytest.raises(fault) as raised:
        browser.session('cli')
    assert raised.value is primary
    assert primary._browser_outcome['construction_started'] is True
    assert primary._browser_outcome['transport_returned'] is False
    with pytest.raises(browser.BrowserUnavailable):
        browser.reset()
    with pytest.raises(browserlease.OwnershipRefused):
        browser.session('cli')
    assert calls == ['build'] and owned.transports == []
    assert not (owned.root / 'browser_lease.json').exists()


@pytest.mark.parametrize('old_kind', [browser.FAKE, browser.AUTOLAUNCH, browser.NONE, 'unknown'])
def test_existing_transport_kind_mismatch_refuses_before_build(owned, monkeypatch, old_kind):
    with authority.capture() as captured:
        captured.claim('cli', old_kind)
    retained = (owned.root / 'browser_lease.json').read_bytes()
    monkeypatch.setattr(browser, '_TRANSPORT_FACTORY', None)
    monkeypatch.setenv('OLYMPUS_BROWSER_ENGINE', 'chromium')
    monkeypatch.setenv('OLYMPUS_BROWSER_CDP_URL', 'http://127.0.0.1:59999')
    calls = []
    monkeypatch.setattr(browser, '_build_transport', lambda **kwargs: calls.append('build'))
    with pytest.raises(browserlease.OwnershipRefused):
        browser.session('cli')
    assert calls == [] and browser._factory_attempt is None
    assert (owned.root / 'browser_lease.json').read_bytes() == retained


def test_factory_uses_selection_captured_before_callbacks(owned, monkeypatch):
    unrelated = []
    def drift(kind):
        monkeypatch.setattr(browser, '_TRANSPORT_FACTORY', lambda: unrelated.append('wrong-build'))
        return False
    monkeypatch.setattr(browser, '_carries_foreign_state', drift)
    session = browser.session('cli')
    assert len(owned.transports) == 1 and unrelated == []
    assert session._credential_transport_selection.kind == browser.FAKE
    assert json.loads((owned.root / 'browser_lease.json').read_bytes())['transport'] == browser.FAKE


def test_lease_transport_edit_cannot_authorize_a_different_teardown(owned):
    browser.session('cli')
    path = owned.root / 'browser_lease.json'
    record = json.loads(path.read_bytes())
    record['transport'] = browser.AUTOLAUNCH
    path.write_text(json.dumps(record), encoding='utf-8')
    retained = path.read_bytes()
    with pytest.raises(browserlease.OwnershipRefused):
        browser.relinquish('cli')
    assert _close_count(owned.transports[0]) == 0
    assert browser._release_attempt is None and path.read_bytes() == retained


def test_lost_release_ack_cannot_remove_a_new_generation(owned, monkeypatch):
    browser.session('cli')
    native_sync = authority.vault_storage._Directory.sync
    def failed_barrier(directory):
        if directory.path == owned.root:
            raise OSError('owned-private-release-barrier')
        return native_sync(directory)
    with monkeypatch.context() as patch:
        patch.setattr(authority.vault_storage._Directory, 'sync', failed_barrier)
        with pytest.raises(browserlease.OwnershipRefused):
            browser.relinquish('cli')
    with authority.capture() as captured:
        captured.claim('cli', browser.FAKE)
    retained = (owned.root / 'browser_lease.json').read_bytes()
    with pytest.raises(browserlease.OwnershipRefused):
        browser.relinquish('cli')
    assert (owned.root / 'browser_lease.json').read_bytes() == retained
    assert _close_count(owned.transports[0]) == 1 and len(owned.transports) == 1

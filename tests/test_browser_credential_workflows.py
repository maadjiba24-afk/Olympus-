"""Owned native credential workflow controls; no real browser or provider."""
import base64
from collections import OrderedDict
import hashlib
import json
import os
import threading
from types import SimpleNamespace

import pytest

from olympus import browser, browserlease, config, memory, prefs, store, vault
from olympus import browser_credentials as workflow
from olympus import browser_credential_authority as authority
from olympus import browser_credential_policy as policy
from olympus import browser_credential_profiles as profiles
from olympus import browser_credential_records as records

_PASSWORD = 'owned-private-workflow-password'
_COOKIE = 'owned-private-workflow-cookie'
_KEY = 'owned-workflow-key'
_SITE = 'owned.test'


@pytest.fixture
def owned(tmp_path, monkeypatch):
    root = tmp_path / 'memory'
    root.mkdir()
    monkeypatch.setattr(config, 'MEMORY_DIR', root)
    for name in ('OLYMPUS_SECRET_KEY_FILE', 'OLYMPUS_DATABASE_URL', 'OLYMPUS_OPERATOR',
                 'OLYMPUS_OPERATOR_DOMAINS', 'OLYMPUS_SOVEREIGN', 'OLYMPUS_EGRESS_ALLOWLIST'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('OLYMPUS_SECRET_KEY', _KEY)
    monkeypatch.setenv('OLYMPUS_ENV', 'test')
    monkeypatch.setattr(workflow, '_WORKFLOWS', OrderedDict())
    monkeypatch.setattr(workflow, '_ACTIVE', {})
    for name in ('_session', '_factory_attempt', '_release_attempt', '_launched'):
        monkeypatch.setattr(browser, name, None)
    monkeypatch.setattr('olympus.security.url_block_reason', lambda *args, **kwargs: None)
    monkeypatch.setattr(browser, 'builtin_profiles', lambda: [])
    transports, logins = [], []
    def build():
        transport = browser.FakeTransport(present=['#username', '#password', '#submit', '#success'])
        transport.cookies = [{'name': 'sid', 'value': _COOKIE, 'domain': _SITE}]
        transports.append(transport)
        return transport
    monkeypatch.setattr(browser, '_TRANSPORT_FACTORY', build)
    real_login = browser.BrowserSession.login
    def login(session, *args, **kwargs):
        logins.append('login')
        return real_login(session, *args, **kwargs)
    monkeypatch.setattr(browser.BrowserSession, 'login', login)
    store.reset()
    prefs.set('cli', 'operator', {'enabled': True, 'sites': {_SITE: {'login': 'remember'}}})
    profile = browser.SiteProfile(domain=_SITE, login_url='https://owned.test/login',
        username_selector='#username', password_selector='#password', submit_selector='#submit', success_selector='#success')
    (root / 'site_profiles.json').write_text(json.dumps([profile.to_dict()]), encoding='utf-8')
    vault.put('cli', f'site:{_SITE}', {'username': 'owned-user', 'password': _PASSWORD})
    yield SimpleNamespace(root=root, transports=transports, logins=logins, profile=profile)
    store.reset()


def _attempt():
    assert len(workflow._WORKFLOWS) == 1
    return next(iter(workflow._WORKFLOWS.values()))


def _calls(owned, method):
    return sum(call['method'] == method for transport in owned.transports for call in transport.calls)


def _replace_generation(root):
    path = root / 'browser_lease.json'
    record = json.loads(path.read_bytes())
    record['lease_id'] = 'd' * 32 if record['lease_id'] != 'd' * 32 else 'c' * 32
    raw = json.dumps(record).encode()
    path.write_bytes(raw)
    return raw


def test_login_records_independent_outcomes_and_keeps_credentials_out_of_status(owned):
    message = workflow.login('cli', _SITE)
    attempt = _attempt()
    result = workflow.status('cli', attempt.identity)
    assert result['complete'] and result['login_result'] is True
    assert all(result[name] == 'confirmed' for name in ('factory', 'login', 'cookies', 'vault', 'telemetry'))
    assert owned.logins == ['login'] and len(owned.transports) == 1
    assert _calls(owned, 'Storage.getCookies') == 1
    assert vault.get('cli', f'cookies:{_SITE}') == {'cookies': [{'name': 'sid', 'value': _COOKIE, 'domain': _SITE}]}
    assert _PASSWORD not in message + json.dumps(result) + repr(attempt)
    assert _COOKIE not in message + json.dumps(result) + repr(attempt)
    result['vault'] = 'tampered-copy'
    assert workflow.status('cli', attempt.identity)['vault'] == 'confirmed'


def test_lost_vault_ack_settles_only_original_local_update(owned, monkeypatch):
    original = vault.settle_snapshot_update
    def publish_then_fail(pending):
        original(pending)
        raise OSError('owned-private-lost-ack')
    with monkeypatch.context() as patch:
        patch.setattr(vault, 'settle_snapshot_update', publish_then_fail)
        message = workflow.login('cli', _SITE)
    attempt = _attempt()
    assert attempt.login_result is True and attempt.stages['vault'] == 'unconfirmed'
    assert not attempt.complete and attempt.pending is not None
    exact_ciphertext = attempt.pending._ciphertext
    assert 'owned-private-' not in message
    repeated = workflow.login('cli', _SITE)
    assert attempt.identity in repeated and owned.logins == ['login']
    assert _calls(owned, 'Storage.getCookies') == 1
    settled = workflow.settle('cli', attempt.identity)
    assert attempt.complete and attempt.stages['vault'] == 'confirmed'
    assert owned.logins == ['login'] and _calls(owned, 'Storage.getCookies') == 1
    assert store.backend().get('vault', vault._key('cli')) == exact_ciphertext
    assert 'Completed.' in settled


@pytest.mark.parametrize('fault', [RuntimeError, KeyboardInterrupt, SystemExit])
def test_unknown_login_retains_entry_and_never_repeats_browser_action(owned, monkeypatch, fault):
    primary = fault('owned-private-login-fault')
    calls = []
    def failed_login(*args, **kwargs):
        calls.append('login-dispatched')
        raise primary
    monkeypatch.setattr(browser.BrowserSession, 'login', failed_login)
    if fault is RuntimeError:
        message = workflow.login('cli', _SITE)
        assert 'owned-private-' not in message
    else:
        with pytest.raises(fault) as raised:
            workflow.login('cli', _SITE)
        assert raised.value is primary
        assert primary._browser_credential_workflow['login'] == 'unconfirmed'
    attempt = _attempt()
    assert not attempt.busy and not attempt.complete and attempt.login_result is None
    assert attempt.identity in workflow.login('cli', _SITE)
    assert calls == ['login-dispatched'] and _calls(owned, 'Storage.getCookies') == 0


def test_disconnect_or_conflicting_row_cannot_be_resurrected_by_settlement(owned, monkeypatch):
    original = browser.BrowserSession.get_cookies
    def get_then_disconnect(session, *args, **kwargs):
        cookies = original(session, *args, **kwargs)
        vault.delete_many('cli', [f'site:{_SITE}', f'cookies:{_SITE}'])
        return cookies
    monkeypatch.setattr(browser.BrowserSession, 'get_cookies', get_then_disconnect)
    workflow.login('cli', _SITE)
    attempt = _attempt()
    assert attempt.login_result is True and attempt.stages['vault'] == 'unconfirmed'
    retained = store.backend().get('vault', vault._key('cli'))
    workflow.settle('cli', attempt.identity)
    assert not attempt.complete and store.backend().get('vault', vault._key('cli')) == retained
    assert vault.get('cli', f'site:{_SITE}') is None and vault.get('cli', f'cookies:{_SITE}') is None
    assert owned.logins == ['login'] and _calls(owned, 'Storage.getCookies') == 1


@pytest.mark.parametrize('operation', ['save', 'restore'])
@pytest.mark.parametrize('drift', ['generation', 'root', 'policy'])
def test_returned_cookie_effect_is_recorded_before_post_response_refusal(owned, monkeypatch, operation, drift):
    if operation == 'restore':
        vault.put('cli', f'cookies:{_SITE}', {'cookies': [{'name': 'sid', 'value': _COOKIE, 'domain': _SITE}]})
    before = store.backend().get('vault', vault._key('cli'))
    name = 'get_cookies' if operation == 'save' else 'set_cookies'
    original = getattr(browser.BrowserSession, name)
    changed = []
    other = owned.root.with_name('unselected-memory')
    def after_return(session, *args, **kwargs):
        result = original(session, *args, **kwargs)
        if drift == 'generation':
            changed.append(_replace_generation(owned.root))
        elif drift == 'root':
            monkeypatch.setattr(config, 'MEMORY_DIR', other)
        else:
            prefs.set('cli', 'operator', {'enabled': False, 'sites': {}})
        return result
    monkeypatch.setattr(browser.BrowserSession, name, after_return)
    function = workflow.save_auth if operation == 'save' else workflow.restore_auth
    function('cli', _SITE)
    attempt = _attempt()
    stage = 'cookies' if operation == 'save' else 'injection'
    assert attempt.stages[stage] == 'confirmed' and attempt.stopped and not attempt.complete
    assert attempt.identity in function('cli', _SITE)
    assert _calls(owned, 'Storage.getCookies' if operation == 'save' else 'Network.setCookies') == 1
    monkeypatch.setattr(config, 'MEMORY_DIR', owned.root)
    assert store.backend().get('vault', vault._key('cli')) == before
    assert not other.exists()
    if changed:
        assert (owned.root / 'browser_lease.json').read_bytes() == changed[0]


def test_legacy_login_is_read_only_and_explicit_save_refuses_before_browser(owned):
    from cryptography.fernet import Fernet
    key = base64.urlsafe_b64encode(hashlib.sha256(_KEY.encode()).digest())
    raw = Fernet(key).encrypt(json.dumps({f'site:{_SITE}': {'username': 'owned-user', 'password': _PASSWORD}}).encode())
    store.backend().put('vault', vault._key('cli'), raw)
    message = workflow.save_auth('cli', _SITE)
    assert 'migration' in message and owned.transports == [] and not workflow._WORKFLOWS
    workflow.login('cli', _SITE)
    attempt = _attempt()
    assert attempt.login_result is True and attempt.stages['vault'] == 'migration_required'
    assert _calls(owned, 'Storage.getCookies') == 0
    assert store.backend().get('vault', vault._key('cli')) == raw


@pytest.mark.parametrize('replacement', [None, False, '', {}, {'cookies': None}, {'cookies': [{}]}],
                         ids=['none-field', 'false-field', 'empty-string', 'missing-field', 'null-list', 'bad-row'])
def test_malformed_saved_cookies_never_become_absent_or_reach_browser(owned, replacement):
    # Vault records allow dict/string; malformed inner values are valid JSON.
    value = {'cookies': replacement} if replacement in (None, False, '') else replacement
    vault.put('cli', f'cookies:{_SITE}', value)
    message = workflow.restore_auth('cli', _SITE)
    assert message.startswith('Error:') and 'No saved session' not in message
    assert owned.transports == []
    with pytest.raises(records.BrowserCredentialError):
        workflow.has_saved_auth('cli', _SITE)


def test_empty_saved_bundle_is_distinct_from_absence(owned):
    assert not workflow.has_saved_auth('cli', _SITE)
    assert workflow.restore_auth('cli', _SITE).startswith('No saved session')
    vault.put('cli', f'cookies:{_SITE}', {'cookies': []})
    assert workflow.has_saved_auth('cli', _SITE)
    workflow.restore_auth('cli', _SITE)
    assert _attempt().complete and _attempt().stages['injection'] == 'confirmed'
    assert _calls(owned, 'Network.setCookies') == 0


@pytest.mark.parametrize('part', ['root', 'locks', 'lock-file'])
def test_bound_entry_refuses_missing_coordination_without_recreating_or_callbacks(owned, monkeypatch, part):
    browser.session('cli')
    target = {'root': owned.root, 'locks': owned.root / 'locks',
              'lock-file': owned.root / 'locks/browser-lease.lock'}[part]
    retained = target.with_name(target.name + '-retained')
    target.rename(retained)
    calls = []
    monkeypatch.setattr(policy, 'snapshot', lambda *args: calls.append('policy'))
    monkeypatch.setattr(vault, 'snapshot', lambda *args, **kwargs: calls.append('vault'))
    before = list(owned.transports[0].calls)
    assert workflow.save_auth('cli', _SITE) == browserlease.REFUSAL
    assert calls == [] and not target.exists() and retained.exists()
    assert owned.transports[0].calls == before


def test_transient_foreign_root_cannot_supply_authorization(owned, monkeypatch):
    from olympus import prefs_native

    prefs.set('cli', 'operator', {'enabled': False, 'sites': {}})
    other = owned.root.with_name('other-memory')
    with monkeypatch.context() as patch:
        patch.setattr(config, 'MEMORY_DIR', other)
        prefs.set('cli', 'operator', {'enabled': True, 'sites': {_SITE: {'login': 'remember'}}})
    current_parts = prefs.credential_paths('cli')[0]
    original_path = owned.root.joinpath(*current_parts)
    foreign_path = other.joinpath(*current_parts)
    before = (original_path.read_bytes(), foreign_path.read_bytes())
    original = prefs_native.Transaction.read
    observed = []
    def temporary_drift(transaction, parts, *, max_bytes=1024 * 1024):
        if parts == current_parts:
            assert transaction.binding.root == owned.root
            monkeypatch.setattr(config, 'MEMORY_DIR', other)
            try:
                result = original(transaction, parts, max_bytes=max_bytes)
                observed.append(result[0])
                return result
            finally:
                monkeypatch.setattr(config, 'MEMORY_DIR', owned.root)
        return original(transaction, parts, max_bytes=max_bytes)
    monkeypatch.setattr(prefs_native.Transaction, 'read', temporary_drift)
    calls = []
    monkeypatch.setattr(vault, 'snapshots', lambda *args, **kwargs: calls.append('vault'))
    assert workflow.login('cli', _SITE).startswith('Error:')
    assert calls == [] and owned.transports == [] and owned.logins == [] and observed
    assert all(raw == before[0] and json.loads(raw)['operator']['enabled'] is False for raw in observed)
    assert (original_path.read_bytes(), foreign_path.read_bytes()) == before


def test_owner_boundary_and_executor_affinity_do_not_dispatch_or_recover_by_recapture(owned, monkeypatch):
    original = vault.settle_snapshot_update
    def lost_ack(pending):
        original(pending)
        raise OSError('owned-private-ack')
    with monkeypatch.context() as patch:
        patch.setattr(vault, 'settle_snapshot_update', lost_ack)
        workflow.login('cli', _SITE)
    attempt = _attempt()
    with pytest.raises(workflow.BrowserCredentialWorkflowError):
        workflow.status('tg-other', attempt.identity)
    with pytest.raises(workflow.BrowserCredentialWorkflowError):
        workflow.settle('tg-other', attempt.identity)
    errors = []
    def other_worker():
        try:
            workflow.settle('cli', attempt.identity)
        except workflow.BrowserCredentialWorkflowError:
            errors.append('refused-original-executor')
    thread = threading.Thread(target=other_worker)
    thread.start(); thread.join(timeout=5)
    assert not thread.is_alive() and errors == ['refused-original-executor']
    assert not attempt.complete and owned.logins == ['login']
    assert _calls(owned, 'Storage.getCookies') == 1


def test_unresolved_registry_capacity_refuses_without_eviction_or_dispatch(owned):
    with browser._capture_credentials('cli') as captured:
        for index in range(workflow._MAX_WORKFLOWS):
            attempt, fresh = workflow._reserve('cli', f'site-{index}.owned.test', 'login', captured)
            assert fresh
            workflow._start(attempt, 'login')
    identities = list(workflow._WORKFLOWS)
    assert workflow.login('cli', _SITE).startswith('Error:')
    assert list(workflow._WORKFLOWS) == identities and owned.transports == []


def test_telemetry_lock_cleanup_uncertainty_is_not_lost_or_followed_by_cookie_read(owned, monkeypatch):
    native_open = authority.Authority._open_lock
    native_publish = authority.vault_storage._Directory.publish
    published, failed = [], []
    def publish(directory, name, data):
        result = native_publish(directory, name, data)
        if name == 'site_profiles.json':
            published.append(True)
        return result
    class FileProxy:
        def __init__(self, file): self.file = file
        def __getattr__(self, name): return getattr(self.file, name)
        def close(self):
            self.file.close()
            if published and not failed:
                failed.append(True)
                raise OSError('owned-private-telemetry-lock-close')
    monkeypatch.setattr(authority.Authority, '_open_lock', lambda selected, binding=None: FileProxy(native_open(selected, binding)))
    monkeypatch.setattr(authority.vault_storage._Directory, 'publish', publish)
    message = workflow.login('cli', _SITE)
    attempt = _attempt()
    assert published == [True] and failed == [True]
    assert attempt.login_result is True and attempt.stages['telemetry'] == 'unconfirmed'
    assert attempt.cleanup_unconfirmed and not attempt.complete
    assert _calls(owned, 'Storage.getCookies') == 0
    assert 'owned-private-' not in message
    assert attempt.identity in workflow.login('cli', _SITE)
    assert owned.logins == ['login']


@pytest.mark.parametrize('boundary', ['before_username', 'after_username', 'after_password'])
def test_cross_origin_navigation_stops_the_next_credential_or_submit(owned, monkeypatch, boundary):
    real_open, real_fill, real_act = browser.BrowserSession.open, browser.BrowserSession.fill, browser.BrowserSession.act
    fields, submits = [], []
    def open_page(session, url):
        result = real_open(session, url)
        if boundary == 'before_username':
            session._t._url = 'https://another-owned.test/login'
        return result
    def fill(session, selector, value):
        fields.append(selector)
        result = real_fill(session, selector, value)
        if (boundary == 'after_username' and selector == '#username'
                or boundary == 'after_password' and selector == '#password'):
            session._t._url = 'https://another-owned.test/login'
        return result
    def act(session, *args, **kwargs):
        submits.append('submit')
        return real_act(session, *args, **kwargs)
    monkeypatch.setattr(browser.BrowserSession, 'open', open_page)
    monkeypatch.setattr(browser.BrowserSession, 'fill', fill)
    monkeypatch.setattr(browser.BrowserSession, 'act', act)
    workflow.login('cli', _SITE)
    assert fields == {'before_username': [], 'after_username': ['#username'],
                      'after_password': ['#username', '#password']}[boundary]
    assert submits == [] and _calls(owned, 'Storage.getCookies') == 0
    attempt = _attempt()
    assert attempt.login_result is None and not attempt.complete
    assert attempt.identity in workflow.login('cli', _SITE) and owned.logins == ['login']


@pytest.mark.parametrize('location', ['', 'about:blank', 'http://owned.test/login', 'https://owned.test:444/login'],
                         ids=['unknown', 'blank', 'downgrade', 'other-port'])
def test_unknown_or_changed_origin_refuses_before_credential_fill(owned, monkeypatch, location):
    real_open = browser.BrowserSession.open
    fields = []
    def open_page(session, url):
        result = real_open(session, url)
        session._t._url = location
        return result
    monkeypatch.setattr(browser.BrowserSession, 'open', open_page)
    monkeypatch.setattr(browser.BrowserSession, 'fill', lambda *args: fields.append('fill'))
    workflow.login('cli', _SITE)
    assert fields == [] and _attempt().login_result is None
    assert _calls(owned, 'Storage.getCookies') == 0


def test_recipe_mutation_cannot_adopt_a_redirected_credential_destination(owned, monkeypatch):
    real_open = browser.BrowserSession.open
    selected = []
    real_profile = profiles.get_profile
    def get_profile(*args):
        profile = real_profile(*args)
        selected.append(profile)
        return profile
    def open_page(session, url):
        result = real_open(session, url)
        selected[0].domain = 'another-owned.test'
        selected[0].login_url = 'https://another-owned.test/login'
        session._t._url = selected[0].login_url
        return result
    fields = []
    monkeypatch.setattr(profiles, 'get_profile', get_profile)
    monkeypatch.setattr(browser.BrowserSession, 'open', open_page)
    monkeypatch.setattr(browser.BrowserSession, 'fill', lambda *args: fields.append('fill'))
    workflow.login('cli', _SITE)
    assert fields == [] and not _attempt().complete


def test_unresolved_legacy_policy_refuses_even_an_environment_override(owned, monkeypatch):
    owner = 'u:customer@example.test'
    assert browserlease.is_trusted(owner)
    assert not memory.is_system_owner(owner) and not memory.is_ambiguous_gateway_owner(owner)
    prefs.set(owner, 'operator', {'enabled': True, 'sites': {_SITE: {'login': 'remember'}}})
    legacy = owned.root / 'users' / memory.safe_id(owner) / 'prefs.json'
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text('{}', encoding='utf-8')
    monkeypatch.setenv('OLYMPUS_OPERATOR', '1')
    monkeypatch.setenv('OLYMPUS_OPERATOR_DOMAINS', _SITE)
    calls = []
    monkeypatch.setattr(vault, 'snapshots', lambda *args, **kwargs: calls.append('vault'))
    assert workflow.login(owner, _SITE).startswith('Error:')
    assert calls == [] and owned.transports == [] and legacy.read_bytes() == b'{}'


def test_complete_callback_failure_never_loses_registered_attempt(owned, monkeypatch):
    real_save = workflow._save
    def save_then_fail(*args):
        real_save(*args)
        raise OSError('owned-private-late-body-fault')
    monkeypatch.setattr(workflow, '_save', save_then_fail)
    message = workflow.login('cli', _SITE)
    attempt = _attempt()
    assert attempt.login_result is True and attempt.stages['vault'] == 'confirmed'
    assert attempt.stopped and not attempt.complete and not attempt.busy
    assert attempt.identity in message and attempt.identity in workflow.login('cli', _SITE)
    assert owned.logins == ['login'] and _calls(owned, 'Storage.getCookies') == 1


def test_finish_none_does_not_wait_for_registry_mutex(owned):
    entered, completed = threading.Event(), threading.Event()
    def finish_empty():
        entered.set()
        workflow._finish(None)
        completed.set()
    with workflow._LOCK:
        thread = threading.Thread(target=finish_empty, daemon=True)
        thread.start()
        assert entered.wait(2) and completed.wait(2)
    thread.join(timeout=2)
    assert not thread.is_alive()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX fork control; Windows has no os.fork')
def test_forked_child_refuses_before_inherited_registry_lock(owned):
    import select
    import signal
    import time
    import warnings
    ready, release = threading.Event(), threading.Event()
    def hold():
        with workflow._LOCK:
            ready.set()
            release.wait(20)
    thread = threading.Thread(target=hold, daemon=True)
    read_fd = write_fd = pid = None
    started = reaped = False
    cleanup_failures = []
    def reap(deadline):
        nonlocal reaped
        while True:
            observed, _status = os.waitpid(pid, os.WNOHANG)
            if observed == pid:
                reaped = True
                return
            assert observed == 0
            if time.monotonic() >= deadline:
                raise TimeoutError('owned direct child did not exit within its deadline')
            time.sleep(0.01)
    try:
        thread.start()
        started = True
        assert ready.wait(2)
        read_fd, write_fd = os.pipe()
        # This deliberate inherited-lock regression requires a live thread.
        # Silence only CPython's specific warning at this one fork call.
        with warnings.catch_warnings():
            warnings.filterwarnings('ignore', message=r'This process .* is multi-threaded, use of fork\(\) may lead to deadlocks in the child\.', category=DeprecationWarning)
            pid = os.fork()
        if pid == 0:
            try:
                os.close(read_fd)
                result = workflow.login('cli', _SITE)
                os.write(write_fd, b'R' if result.startswith('Error:') else b'F')
            finally:
                os._exit(0)
        closed_fd, write_fd = write_fd, None
        os.close(closed_fd)
        readable, _, _ = select.select([read_fd], [], [], 5)
        assert readable and os.read(read_fd, 1) == b'R'
        reap(time.monotonic() + 2)
    finally:
        try:
            if pid is not None and pid > 0 and not reaped:
                # Only this unreaped direct child is eligible for a signal.
                # ECHILD/observation failure skips signalling entirely.
                observed, _status = os.waitpid(pid, os.WNOHANG)
                if observed == pid:
                    reaped = True
                elif observed == 0:
                    os.kill(pid, signal.SIGKILL)
                    reap(time.monotonic() + 2)
                else:
                    raise AssertionError('unexpected owned-child observation')
        except BaseException as error:
            cleanup_failures.append(type(error).__name__)
        for fd in (write_fd, read_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except BaseException as error:
                    cleanup_failures.append(type(error).__name__)
        release.set()
        if started:
            try:
                thread.join(timeout=2)
                if thread.is_alive():
                    cleanup_failures.append('thread_still_running')
            except BaseException as error:
                cleanup_failures.append(type(error).__name__)
        assert not cleanup_failures
    assert reaped and not thread.is_alive() and owned.transports == []


@pytest.mark.parametrize('encoding', ['utf-16', 'utf-32'])
@pytest.mark.parametrize('target', ['policy', 'profile'])
def test_non_utf8_evidence_refuses_before_vault_or_browser_even_with_override(owned, monkeypatch, encoding, target):
    path = prefs._path('cli') if target == 'policy' else owned.root / 'site_profiles.json'
    decoded = path.read_text(encoding='utf-8')
    raw = decoded.encode(encoding)
    path.write_bytes(raw)
    monkeypatch.setenv('OLYMPUS_OPERATOR', '1')
    monkeypatch.setenv('OLYMPUS_OPERATOR_DOMAINS', _SITE)
    calls = []
    monkeypatch.setattr(vault, 'snapshots', lambda *args, **kwargs: calls.append('vault'))
    assert workflow.login('cli', _SITE).startswith('Error:')
    assert calls == [] and owned.transports == [] and path.read_bytes() == raw


def test_frozen_cidr_policy_rejects_descendant_destination_despite_env_drift(owned, monkeypatch):
    import socket
    monkeypatch.setenv('OLYMPUS_SOVEREIGN', '1')
    monkeypatch.setenv('OLYMPUS_EGRESS_ALLOWLIST', '192.0.2.0/24')
    resolved = []
    def resolve(host, port, **kwargs):
        resolved.append(host)
        assert host in (_SITE, 'login.owned.test')
        address = '192.0.2.10' if host == _SITE else '198.51.100.20'
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (address, 0))]
    monkeypatch.setattr(policy.security.socket, 'getaddrinfo', resolve)
    owned.profile.login_url = 'https://login.owned.test/login'
    (owned.root / 'site_profiles.json').write_text(json.dumps([owned.profile.to_dict()]), encoding='utf-8')
    original = browser.BrowserSession.login
    def under_disabled_ambient_policy(session, *args, **kwargs):
        with monkeypatch.context() as patch:
            patch.setenv('OLYMPUS_SOVEREIGN', '0')
            return original(session, *args, **kwargs)
    monkeypatch.setattr(browser.BrowserSession, 'login', under_disabled_ambient_policy)
    workflow.login('cli', _SITE)
    assert _SITE in resolved and 'login.owned.test' in resolved and config.sovereign_mode()
    assert _calls(owned, 'Page.navigate') == 0 and _calls(owned, 'Storage.getCookies') == 0
    assert _attempt().login_result is None and not _attempt().complete


@pytest.mark.parametrize('missing', ['username', 'password', 'submit'])
def test_known_recipe_step_failure_stops_later_steps_and_autosave(owned, monkeypatch, missing):
    setattr(owned.profile, missing + '_selector', '#missing-' + missing)
    owned.profile.success_selector = ''
    (owned.root / 'site_profiles.json').write_text(json.dumps([owned.profile.to_dict()]), encoding='utf-8')
    original_fill, original_act = browser.BrowserSession.fill, browser.BrowserSession.act
    events = []
    def fill(session, selector, value):
        events.append(selector)
        return original_fill(session, selector, value)
    def act(session, operation, **kwargs):
        events.append(kwargs.get('selector'))
        return original_act(session, operation, **kwargs)
    monkeypatch.setattr(browser.BrowserSession, 'fill', fill)
    monkeypatch.setattr(browser.BrowserSession, 'act', act)
    workflow.login('cli', _SITE)
    assert events == {'username': ['#missing-username'],
                      'password': ['#username', '#missing-password'],
                      'submit': ['#username', '#password', '#missing-submit']}[missing]
    assert _attempt().login_result is False and _attempt().complete
    assert _calls(owned, 'Storage.getCookies') == 0 and vault.get('cli', f'cookies:{_SITE}') is None


def test_explicit_zero_port_does_not_equal_https_default(owned, monkeypatch):
    original = browser.BrowserSession.open
    fields = []
    def open_page(session, url):
        result = original(session, url)
        session._t._url = 'https://owned.test:0/login'
        return result
    monkeypatch.setattr(browser.BrowserSession, 'open', open_page)
    monkeypatch.setattr(browser.BrowserSession, 'fill', lambda *args: fields.append('fill'))
    workflow.login('cli', _SITE)
    assert fields == [] and _attempt().login_result is None and not _attempt().complete


@pytest.mark.parametrize('response', [None, {}, {'cookies': None}, {'cookies': False}, {'cookies': [{}]},
    {'cookies': [], 'unexpected': True}], ids=['null', 'missing', 'null-list', 'false-list', 'bad-row', 'extra-field'])
def test_malformed_acquisition_is_never_an_empty_session(response):
    with pytest.raises(records.BrowserCredentialError):
        records.acquired_cookies(response, _SITE)


def test_acquisition_validates_unrelated_cookies_before_filtering():
    selected = {'name': 'sid', 'value': _COOKIE, 'domain': _SITE}
    unrelated = {'name': 'other', 'value': 'owned-unrelated-value', 'domain': 'other.test'}
    response = {'cookies': [selected, unrelated]}
    result = records.acquired_cookies(response, _SITE)
    assert result == [selected] and result[0] is not selected
    unrelated['secure'] = 'false'
    with pytest.raises(records.BrowserCredentialError):
        records.acquired_cookies(response, _SITE)


def test_acquisition_applies_aggregate_bound_before_domain_filtering():
    response = {'cookies': [{'name': f'sid-{index}', 'value': 'x' * records.MAX_CREDENTIAL_BYTES,
                            'domain': 'other.test'} for index in range(20)]}
    with pytest.raises(records.BrowserCredentialError):
        records.acquired_cookies(response, _SITE)


def test_public_login_and_local_status_settlement_keep_independent_outcomes(owned, monkeypatch):
    from olympus import operator, tools
    monkeypatch.setattr(tools, '_browser_owner', lambda: 'cli')
    original = vault.settle_snapshot_update
    def publish_then_fail(pending):
        original(pending)
        raise OSError('owned-private-public-lost-ack')
    with monkeypatch.context() as patch:
        patch.setattr(vault, 'settle_snapshot_update', publish_then_fail)
        result = tools._browser_login(_SITE)
    attempt = _attempt()
    assert attempt.identity in result and 'owned-private-' not in result
    state = operator.auth_workflow_status('cli', attempt.identity)
    assert state['login_result'] is True and state['vault'] == 'unconfirmed'
    assert 'Completed.' in operator.settle_auth_workflow('cli', attempt.identity)
    assert owned.logins == ['login'] and _calls(owned, 'Storage.getCookies') == 1
    assert operator.has_saved_auth('cli', _SITE)


def test_public_cookie_save_restore_and_empty_record_are_distinct(owned):
    from olympus import operator
    assert not operator.has_saved_auth('cli', _SITE)
    saved = operator.save_auth('cli', _SITE)
    assert 'vault=confirmed' in saved and 'Completed.' in saved
    assert operator.has_saved_auth('cli', _SITE)
    restored = operator.restore_auth('cli', _SITE)
    assert 'injection=confirmed' in restored and 'Completed.' in restored
    assert _calls(owned, 'Storage.getCookies') == 1 and _calls(owned, 'Network.setCookies') == 1
    vault.put('cli', f'cookies:{_SITE}', {'cookies': []})
    assert operator.has_saved_auth('cli', _SITE)
    assert 'Completed.' in operator.restore_auth('cli', _SITE)
    assert _calls(owned, 'Network.setCookies') == 1


def test_competing_login_returns_retained_busy_workflow_without_second_dispatch(owned, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = browser.BrowserSession.login
    results, failures = [], []
    def blocked(session, *args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(session, *args, **kwargs)
    def run():
        try:
            results.append(workflow.login('cli', _SITE))
        except BaseException as error:
            failures.append(type(error).__name__)
    monkeypatch.setattr(browser.BrowserSession, 'login', blocked)
    thread = threading.Thread(target=run, daemon=True)
    try:
        thread.start()
        assert entered.wait(5)
        attempt = _attempt()
        result = workflow.login('cli', _SITE)
        assert attempt.identity in result and attempt.busy
        assert len(owned.transports) == 1 and _calls(owned, 'Storage.getCookies') == 0
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive() and not failures
    assert len(results) == 1 and 'Completed.' in results[0]
    assert owned.logins == ['login'] and _calls(owned, 'Storage.getCookies') == 1


def test_foreign_durable_owner_after_reset_refuses_before_policy_or_vault(owned, monkeypatch):
    assert browserlease.is_trusted('u:another@example.test')
    browser.session('u:another@example.test')
    lease_path = owned.root / 'browser_lease.json'
    before = lease_path.read_bytes()
    browser.reset()
    assert browser._session is None
    calls = []
    def forbidden(*args, **kwargs):
        calls.append('forbidden')
        raise AssertionError('dependent credential work was dispatched')
    monkeypatch.setattr(policy, 'snapshot', forbidden)
    monkeypatch.setattr(profiles, 'get_profile', forbidden)
    monkeypatch.setattr(vault, 'snapshots', forbidden)
    monkeypatch.setattr(vault, 'snapshot', forbidden)
    monkeypatch.setattr(browser, '_build_transport', forbidden)
    for operation in (workflow.login, workflow.save_auth, workflow.restore_auth):
        assert operation('cli', _SITE) == browserlease.REFUSAL
    assert calls == [] and lease_path.read_bytes() == before
    assert len(owned.transports) == 1 and not workflow._WORKFLOWS


@pytest.mark.parametrize('owner,malformed', [('hook-unresolved', None), ('cli', b'{'), ('cli', b'[]')])
def test_quarantined_policy_refuses_before_credentials_despite_override(owned, monkeypatch, owner, malformed):
    assert browserlease.is_trusted(owner)
    if malformed is None:
        assert memory.is_ambiguous_gateway_owner(owner)
    if malformed is not None:
        prefs._path(owner).write_bytes(malformed)
    monkeypatch.setenv('OLYMPUS_OPERATOR', '1')
    monkeypatch.setenv('OLYMPUS_OPERATOR_DOMAINS', _SITE)
    calls = []
    monkeypatch.setattr(vault, 'snapshots', lambda *args, **kwargs: calls.append('vault'))
    monkeypatch.setattr(profiles, 'get_profile', lambda *args, **kwargs: calls.append('profile'))
    assert workflow.login(owner, _SITE).startswith('Error:')
    assert calls == [] and owned.transports == [] and not workflow._WORKFLOWS

"""Owned OAuth lifecycle/consumer controls; provider calls are fully mocked."""
import json
import threading
import time
import urllib.error

import pytest

from olympus import calendar, config, gmail, google_oauth as oauth, memory
from olympus import native_cleanup, store, vault, vault_records

OWNER = 'owned-oauth-consumer'
KEY = 'owned-oauth-consumer-custody'
RESPONSE = {'access_token': 'owned-new-access', 'refresh_token': 'owned-new-refresh',
            'expires_in': 3600}


@pytest.fixture(autouse=True)
def owned_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'MEMORY_DIR', tmp_path/'owned-state')
    for name in ('OLYMPUS_SECRET_KEY_FILE', 'OLYMPUS_DATABASE_URL'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('OLYMPUS_SECRET_KEY', KEY)
    monkeypatch.setenv('OLYMPUS_ENV', 'test')
    monkeypatch.setenv('OLYMPUS_GOOGLE_CLIENT_ID', 'owned-client')
    monkeypatch.setenv('OLYMPUS_GOOGLE_CLIENT_SECRET', 'owned-client-secret')
    monkeypatch.setattr(memory, 'current_owner', lambda: OWNER)
    monkeypatch.setattr(gmail, '_token', {'value': None, 'expires': 0})
    store.reset()
    yield
    store.reset()


def expired():
    return {'access_token': 'owned-old-access', 'refresh_token': 'owned-old-refresh',
            'expires_at': 1}


def raw_vault():
    return store.backend().get('vault', vault._key(OWNER))


def retained(error):
    return error if isinstance(error, oauth.OAuthSettlementError) else error._oauth_settlement


def assert_private(handle):
    text = str(handle) + repr(handle) + json.dumps(handle.status())
    for secret in ('owned-new-access', 'owned-new-refresh', 'owned-client-secret', KEY):
        assert secret not in text
    assert all(type(value) is bool for value in handle.status().values())


@pytest.mark.parametrize('kind', [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('stage', ['open', 'read', 'close'])
def test_actual_provider_interrupt_identity_and_observed_response_ownership(monkeypatch, kind, stage):
    primary = kind('owned private interruption marker')
    events = []

    class Response:
        def read(self, cap):
            events.append(('read', cap))
            if stage == 'read':
                raise primary
            return json.dumps(RESPONSE).encode()

        def close(self):
            events.append('close')
            if stage == 'close':
                raise primary

    def open_response(*args, **kwargs):
        events.append('open')
        if stage == 'open':
            raise primary
        return Response()

    monkeypatch.setattr(oauth.urllib.request, 'urlopen', open_response)
    with pytest.raises(kind) as caught:
        oauth.exchange_code(OWNER, 'owned-code')
    assert caught.value is primary
    facts = primary._oauth_outcome
    assert facts['provider_may_have_completed']
    assert not facts['local_publication_confirmed']
    assert not facts['storage_cleanup_unconfirmed']
    assert not native_cleanup.unknown(primary)
    assert events.count('open') == 1
    assert events.count('close') == (0 if stage == 'open' else 1)
    if stage != 'open':
        assert ('read', oauth.MAX_TOKEN_RESPONSE + 1) in events
    if stage == 'close':
        handle = retained(primary)
        assert facts['provider_response_validated'] and facts['response_cleanup_unconfirmed']
        assert facts['local_settlement_available']
        assert_private(handle)
        assert handle.settle() == 'Google account connected.'
        assert handle.status()['local_publication_confirmed']
        assert handle.status()['response_cleanup_unconfirmed']
        assert not handle.status()['storage_cleanup_unconfirmed']
        assert vault.get(OWNER, 'google')['access_token'] == RESPONSE['access_token']
        assert events.count('open') == events.count('close') == 1
    else:
        assert not facts['local_settlement_available']
        assert not facts['provider_response_validated']
        assert not hasattr(primary, '_oauth_settlement')
        assert vault.get(OWNER, 'google') is None


@pytest.mark.parametrize('primary_type', [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('close_type', [OSError, KeyboardInterrupt, SystemExit])
def test_response_close_cannot_replace_actual_read_interrupt(monkeypatch, primary_type, close_type):
    primary, secondary = primary_type('owned primary'), close_type('owned close')
    closes = []

    class Response:
        def read(self, cap):
            raise primary

        def close(self):
            closes.append(1)
            raise secondary

    monkeypatch.setattr(oauth.urllib.request, 'urlopen', lambda *a, **k: Response())
    with pytest.raises(primary_type) as caught:
        oauth.exchange_code(OWNER, 'owned-code')
    assert caught.value is primary and closes == [1]
    assert primary._oauth_outcome['response_cleanup_unconfirmed']
    assert not primary._oauth_outcome['storage_cleanup_unconfirmed']
    assert not native_cleanup.unknown(primary)
    assert vault.get(OWNER, 'google') is None


@pytest.mark.parametrize('close_type', [None, OSError, KeyboardInterrupt, SystemExit])
def test_http_error_body_is_not_read_and_close_uncertainty_is_separate(monkeypatch, close_type):
    closes = []

    class Body:
        def read(self, *args):
            raise AssertionError('must not read provider error body')

        def close(self):
            closes.append(1)
            if close_type is not None:
                raise close_type('owned private close text')

    original = urllib.error.HTTPError('https://owned.invalid', 400,
                                      'owned private HTTP text', {}, Body())

    def opener(*args, **kwargs):
        raise original

    monkeypatch.setattr(oauth.urllib.request, 'urlopen', opener)
    with pytest.raises(oauth.OAuthOutcomeUnknown) as caught:
        oauth.exchange_code(OWNER, 'owned-code')
    assert closes == [1]
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert 'owned private' not in str(caught.value)
    facts = caught.value.status()
    assert facts['response_cleanup_unconfirmed'] is (close_type is not None)
    assert not facts['storage_cleanup_unconfirmed']
    assert not native_cleanup.unknown(caught.value)
    assert not facts['local_settlement_available']


@pytest.mark.parametrize('kind', [RuntimeError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('stage', ['prepare', 'published'])
def test_retained_local_retry_preserves_actual_interrupt_and_exact_ciphertext(monkeypatch, kind, stage):
    vault.put(OWNER, 'google', expired())
    before = raw_vault()
    provider_calls, prepare_calls, pending_calls = [], [], []
    monkeypatch.setattr(oauth, '_post_token', lambda data: (provider_calls.append(1) or dict(RESPONSE)))
    primary = kind('owned private local failure')
    original_prepare, original_settle = vault.prepare_snapshot_update, vault.settle_snapshot_update
    fail = [True]

    def prepare(*args, **kwargs):
        prepare_calls.append(1)
        if stage == 'prepare' and fail[0]:
            fail[0] = False
            raise primary
        return original_prepare(*args, **kwargs)

    def settle(pending):
        pending_calls.append(pending)
        result = original_settle(pending)
        if stage == 'published' and fail[0]:
            fail[0] = False
            raise primary
        return result

    monkeypatch.setattr(vault, 'prepare_snapshot_update', prepare)
    monkeypatch.setattr(vault, 'settle_snapshot_update', settle)
    expected = oauth.OAuthSettlementError if kind is RuntimeError else kind
    with pytest.raises(expected) as caught:
        oauth.access_token(OWNER)
    if kind is not RuntimeError:
        assert caught.value is primary
    else:
        assert caught.value.__context__ is None
        assert 'owned private' not in str(caught.value)
    handle = retained(caught.value)
    assert_private(handle)
    after_first = raw_vault()
    assert (after_first == before) is (stage == 'prepare')
    assert handle.settle() == RESPONSE['access_token']
    assert provider_calls == [1]
    assert vault.get(OWNER, 'google')['refresh_token'] == RESPONSE['refresh_token']
    if stage == 'published':
        assert raw_vault() == after_first
        assert prepare_calls == [1]
        assert len(pending_calls) == 2 and pending_calls[0] is pending_calls[1]
    assert handle.status()['local_publication_confirmed']
    assert handle._selected is handle._bundle is handle._pending is None
    assert handle.settle() == RESPONSE['access_token']
    assert len(pending_calls) == (2 if stage == 'published' else 1)


@pytest.mark.parametrize('kind', [RuntimeError, KeyboardInterrupt, SystemExit])
def test_native_cleanup_uncertainty_blocks_retained_settlement_without_new_work(monkeypatch, kind):
    vault.put(OWNER, 'google', expired())
    calls = []
    monkeypatch.setattr(oauth, '_post_token', lambda data: dict(RESPONSE))
    original = vault.settle_snapshot_update
    primary = kind('owned cleanup uncertainty')
    native_cleanup.mark(primary)

    def settle(pending):
        calls.append(pending)
        original(pending)
        raise primary

    monkeypatch.setattr(vault, 'settle_snapshot_update', settle)
    with pytest.raises(oauth.OAuthSettlementError if kind is RuntimeError else kind) as caught:
        oauth.access_token(OWNER)
    if kind is not RuntimeError:
        assert caught.value is primary
    handle = retained(caught.value)
    assert native_cleanup.unknown(handle)
    assert handle.status()['storage_cleanup_unconfirmed']
    assert not handle.status()['response_cleanup_unconfirmed']
    assert not handle.status()['local_settlement_available']
    published = raw_vault()
    with pytest.raises(oauth.OAuthError) as refused:
        handle.settle()
    assert native_cleanup.unknown(refused.value)
    assert refused.value._oauth_settlement is handle
    assert len(calls) == 1 and raw_vault() == published
    assert vault.get(OWNER, 'google')['access_token'] == RESPONSE['access_token']


def test_retained_retry_uses_original_root_key_backend_and_frozen_expiration(monkeypatch, tmp_path):
    vault.put(OWNER, 'google', expired())
    root = config.MEMORY_DIR
    monkeypatch.setattr(oauth.time, 'time', lambda: 1000)
    providers = []
    monkeypatch.setattr(oauth, '_post_token', lambda data: (providers.append(1) or dict(RESPONSE)))
    original = vault.settle_snapshot_update
    once = [True]

    def fail_before(pending):
        if once[0]:
            once[0] = False
            raise vault.VaultError('owned before publication')
        return original(pending)

    monkeypatch.setattr(vault, 'settle_snapshot_update', fail_before)
    with pytest.raises(oauth.OAuthSettlementError) as caught:
        oauth.access_token(OWNER)
    monkeypatch.setattr(config, 'MEMORY_DIR', tmp_path/'unselected-root')
    monkeypatch.setenv('OLYMPUS_SECRET_KEY', 'unselected-different-custody')
    monkeypatch.setattr(store, '_backend', object())
    monkeypatch.setattr(oauth.time, 'time', lambda: 100000)
    assert caught.value.settle() == RESPONSE['access_token']
    raw = (root/'store/vault'/vault._key(OWNER)).read_bytes()
    entries, metadata = vault_records.decode(OWNER, raw, KEY.encode())
    assert metadata.context_authenticated
    assert entries['google'] == {**{key: RESPONSE[key] for key in ('access_token', 'refresh_token')},
                                 'expires_at': 4600}
    assert not config.MEMORY_DIR.exists() and providers == [1]


@pytest.mark.parametrize('change', ['disconnect', 'replace'])
def test_retained_provider_completion_cannot_overwrite_new_owner_record(monkeypatch, change):
    vault.put(OWNER, 'google', expired())
    calls = []

    def provider(data):
        calls.append(1)
        if change == 'disconnect':
            vault.delete(OWNER, 'google')
        else:
            vault.put(OWNER, 'google', {'access_token': 'owned-reconnected'})
        return dict(RESPONSE)

    monkeypatch.setattr(oauth, '_post_token', provider)
    with pytest.raises(oauth.OAuthSettlementError) as caught:
        oauth.access_token(OWNER)
    before = raw_vault()
    with pytest.raises(vault.VaultConflict):
        caught.value.settle()
    assert calls == [1] and raw_vault() == before
    assert vault.get(OWNER, 'google') == (None if change == 'disconnect' else
                                        {'access_token': 'owned-reconnected'})


def test_settlement_handle_refuses_foreign_thread_without_mutation(monkeypatch):
    vault.put(OWNER, 'google', expired())
    monkeypatch.setattr(oauth, '_post_token', lambda data: dict(RESPONSE))
    original = vault.settle_snapshot_update
    monkeypatch.setattr(vault, 'settle_snapshot_update',
                        lambda pending: (_ for _ in ()).throw(vault.VaultError('owned refusal')))
    with pytest.raises(oauth.OAuthSettlementError) as caught:
        oauth.access_token(OWNER)
    handle = caught.value
    monkeypatch.setattr(vault, 'settle_snapshot_update', original)
    before, errors = raw_vault(), []

    def foreign():
        for operation in (handle.status, handle.settle):
            try:
                operation()
            except oauth.OAuthError:
                errors.append('refused')

    thread = threading.Thread(target=foreign)
    thread.start()
    thread.join(10)
    if thread.is_alive():
        pytest.exit('Owned OAuth settlement thread remains live; stopping validation', returncode=2)
    assert errors == ['refused', 'refused']
    assert raw_vault() == before
    assert handle.settle() == RESPONSE['access_token']


@pytest.mark.parametrize('adapter', ['gmail', 'calendar'])
@pytest.mark.parametrize('damage', ['unreadable', 'missing_key', 'empty_dict', 'empty_string',
                                  'bad_expiry', 'provider_unknown', 'local_unknown'])
def test_actual_mail_calendar_requests_refuse_unsafe_authority_fallback(monkeypatch, adapter, damage):
    vault.put(OWNER, 'google', expired())
    monkeypatch.setenv('GMAIL_ACCESS_TOKEN', 'owned-environment-account')
    monkeypatch.setenv('GMAIL_REFRESH_TOKEN', 'owned-environment-refresh')
    monkeypatch.setenv('GMAIL_CLIENT_ID', 'owned-environment-client')
    monkeypatch.setenv('GMAIL_CLIENT_SECRET', 'owned-environment-client-secret')
    monkeypatch.setattr(gmail, '_token', {'value': 'owned-cached-other-account',
                                        'expires': time.time()+3600})
    calls = []

    def outbound(*args, **kwargs):
        calls.append(1)
        raise AssertionError('no mailbox, calendar, or environment refresh dispatch allowed')

    monkeypatch.setattr(gmail.urllib.request, 'urlopen', outbound)
    if damage == 'unreadable':
        store.backend().put('vault', vault._key(OWNER), b'owned damaged ciphertext')
    elif damage == 'missing_key':
        monkeypatch.delenv('OLYMPUS_SECRET_KEY')
    elif damage in ('empty_dict', 'empty_string', 'bad_expiry'):
        value = {'empty_dict': {}, 'empty_string': '',
                 'bad_expiry': {**expired(), 'expires_at': 'owned invalid'}}[damage]
        vault.put(OWNER, 'google', value)
        with pytest.raises(oauth.OAuthError):
            oauth.connected(OWNER)
    elif damage == 'provider_unknown':
        def unknown(data):
            raise oauth.OAuthOutcomeUnknown()
        monkeypatch.setattr(oauth, '_post_token', unknown)
    else:
        monkeypatch.setattr(oauth, '_post_token', lambda data: dict(RESPONSE))
        monkeypatch.setattr(vault, 'settle_snapshot_update',
            lambda pending: (_ for _ in ()).throw(vault.VaultError('owned local refusal')))
    with pytest.raises((vault.VaultError, oauth.OAuthError)):
        (gmail if adapter == 'gmail' else calendar)._request('GET', '/owned')
    assert calls == []
    assert gmail._token['value'] == 'owned-cached-other-account'


@pytest.mark.parametrize('source', ['cache', 'direct'])
def test_genuine_absence_preserves_explicit_environment_compatibility(monkeypatch, source):
    observations = []
    original = vault.snapshot

    def snapshot(user, name, **kwargs):
        observations.append((user, name))
        return original(user, name, **kwargs)

    monkeypatch.setattr(vault, 'snapshot', snapshot)
    monkeypatch.setenv('GMAIL_ACCESS_TOKEN', 'owned-environment-account')
    if source == 'cache':
        monkeypatch.setattr(gmail, '_token', {'value': 'owned-cached-account',
                                            'expires': time.time()+3600})
    else:
        for name in ('GMAIL_REFRESH_TOKEN', 'GMAIL_CLIENT_ID', 'GMAIL_CLIENT_SECRET'):
            monkeypatch.delenv(name, raising=False)
    assert gmail._access_token() == ('owned-cached-account' if source == 'cache'
                                     else 'owned-environment-account')
    assert observations == [(OWNER, 'google')]


@pytest.mark.parametrize('adapter', ['gmail', 'calendar'])
def test_actual_request_uses_authenticated_exact_owner_token(monkeypatch, adapter):
    vault.put(OWNER, 'google', {**expired(), 'expires_at': time.time()+3600})
    vault.put(OWNER+'-different', 'google', {'access_token': 'owned-other-account',
                                          'expires_at': time.time()+3600})
    observed = []

    class Response:
        def __enter__(self):
            return self

        def read(self):
            return b'{}'

        def __exit__(self, *args):
            observed.append('closed')

    def outbound(request, **kwargs):
        observed.append(request.get_header('Authorization'))
        return Response()

    monkeypatch.setattr(gmail.urllib.request, 'urlopen', outbound)
    assert (gmail if adapter == 'gmail' else calendar)._request('GET', '/owned') == {}
    assert observed == ['Bearer owned-old-access', 'closed']


def test_http_error_during_body_read_does_not_abandon_original_response(monkeypatch):
    closed = []

    class Body:
        def close(self):
            closed.append('error-body')

    primary = urllib.error.HTTPError('https://owned.invalid', 400, 'owned private', {}, Body())

    class Response:
        def read(self, cap):
            raise primary

        def close(self):
            closed.append('original-response')

    monkeypatch.setattr(oauth.urllib.request, 'urlopen', lambda *a, **k: Response())
    with pytest.raises(oauth.OAuthOutcomeUnknown) as caught:
        oauth.exchange_code(OWNER, 'owned-code')
    assert closed == ['error-body', 'original-response']
    assert not caught.value.status()['response_cleanup_unconfirmed']
    assert vault.get(OWNER, 'google') is None


@pytest.mark.parametrize('stage', ['prepare', 'publication'])
def test_original_handle_refuses_reentrant_settlement_without_duplicate_work(monkeypatch, stage):
    vault.put(OWNER, 'google', expired())
    monkeypatch.setattr(oauth, '_post_token', lambda data: dict(RESPONSE))
    original_prepare, original_settle = vault.prepare_snapshot_update, vault.settle_snapshot_update
    monkeypatch.setattr(vault, 'prepare_snapshot_update',
        lambda *a, **k: (_ for _ in ()).throw(vault.VaultError('owned initial refusal')))
    with pytest.raises(oauth.OAuthSettlementError) as caught:
        oauth.access_token(OWNER)
    handle, observations = caught.value, []

    def observe():
        assert not handle.status()['local_settlement_available']
        with pytest.raises(oauth.OAuthError, match='already in progress'):
            handle.settle()
        observations.append('refused')

    def prepare(*args, **kwargs):
        if stage == 'prepare':
            observe()
        return original_prepare(*args, **kwargs)

    def settle(pending):
        if stage == 'publication':
            observe()
        return original_settle(pending)

    monkeypatch.setattr(vault, 'prepare_snapshot_update', prepare)
    monkeypatch.setattr(vault, 'settle_snapshot_update', settle)
    assert handle.settle() == RESPONSE['access_token']
    assert observations == ['refused']
    assert handle.status()['local_publication_confirmed']
    assert not handle._busy
    assert vault.get(OWNER, 'google')['refresh_token'] == RESPONSE['refresh_token']


@pytest.mark.parametrize('outcome', ['unknown', 'returned', 'confirmed'])
def test_actual_web_callback_reports_fixed_known_outcome_without_exception_details(monkeypatch, outcome):
    from urllib.parse import urlparse
    from olympus import web
    state = oauth.issue_state(OWNER, 'ownedsession9')
    selected = vault.snapshot(OWNER, 'google', for_update=True)
    if outcome == 'unknown':
        error = oauth.OAuthOutcomeUnknown()
    else:
        error = oauth.OAuthSettlementError(selected,
            {'access_token': RESPONSE['access_token'], 'refresh_token': RESPONSE['refresh_token'],
             'expires_at': time.time()+3600}, 'connect', response_cleanup_unconfirmed=True)
        if outcome == 'confirmed':
            assert error.settle() == 'Google account connected.'
    calls, reports = [], []

    def exchange(user, code):
        calls.append((user, code))
        raise error

    monkeypatch.setattr(oauth, 'exchange_code', exchange)
    handler = object.__new__(web.Handler)
    handler.headers = {'Cookie': 'olympus_sid=ownedsession9'}
    handler._send = lambda status, body, kind: reports.append((status, body, kind))
    handler._oauth_callback(urlparse('/oauth/google/callback?state='+state+'&code=owned-code'))
    assert calls == [(OWNER, 'owned-code')]
    assert reports[0][0] == 200
    text = reports[0][1].decode()
    assert 'do not repeat' in text
    if outcome == 'unknown':
        assert 'outcome is unknown' in text
    elif outcome == 'returned':
        assert 'returned credentials' in text and 'storage is not confirmed' in text
    else:
        assert 'credentials were saved' in text and 'cleanup remains unconfirmed' in text
    assert RESPONSE['access_token'] not in text and RESPONSE['refresh_token'] not in text
    assert oauth.consume_state(state) is None


@pytest.mark.parametrize('kind', [OSError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('outcome', ['confirmed', 'local_unconfirmed'])
def test_callback_reporting_failure_preserves_confirmed_storage_facts(monkeypatch, kind, outcome):
    from urllib.parse import urlparse
    from olympus import web
    state = oauth.issue_state(OWNER, 'ownedsession9')
    providers = []
    monkeypatch.setattr(oauth, '_post_token', lambda data: (providers.append(1) or dict(RESPONSE)))
    original_settle = vault.settle_snapshot_update
    if outcome == 'local_unconfirmed':
        monkeypatch.setattr(vault, 'settle_snapshot_update',
            lambda pending: (_ for _ in ()).throw(vault.VaultError('owned local refusal')))
    primary = kind('owned report failure')
    handler = object.__new__(web.Handler)
    handler.headers = {'Cookie': 'olympus_sid=ownedsession9'}

    def send(*args):
        raise primary

    handler._send = send
    with pytest.raises(kind) as caught:
        handler._oauth_callback(urlparse('/oauth/google/callback?state='+state+'&code=owned-code'))
    assert caught.value is primary
    assert primary._oauth_reporting_unconfirmed is True
    assert primary._oauth_outcome['local_publication_confirmed'] is (outcome == 'confirmed')
    assert primary._oauth_outcome['provider_response_validated']
    assert not primary._oauth_outcome['response_cleanup_unconfirmed']
    assert not primary._oauth_outcome['storage_cleanup_unconfirmed']
    assert not native_cleanup.unknown(primary)
    assert providers == [1] and oauth.consume_state(state) is None
    if outcome == 'local_unconfirmed':
        handle = primary._oauth_settlement
        assert handle.status()['local_settlement_available']
        assert vault.get(OWNER, 'google') is None
        monkeypatch.setattr(vault, 'settle_snapshot_update', original_settle)
        assert handle.settle() == 'Google account connected.'
        assert providers == [1]
    assert vault.get(OWNER, 'google')['access_token'] == RESPONSE['access_token']


@pytest.mark.parametrize('entrypoint', ['connected', 'access_token_if_connected'])
@pytest.mark.parametrize('damage', ['surrogate', 'huge_integer'])
def test_public_lookup_sanitizes_invalid_returned_bundle_at_consumer_boundary(monkeypatch, entrypoint, damage):
    # The real vault decoder already rejects these values. This separate
    # boundary control ensures the OAuth validator itself never exposes a
    # UnicodeEncodeError token payload or an overflowing conversion exception.
    from types import SimpleNamespace
    value = {**expired(), 'access_token': 'owned-private-marker\ud800'} if damage == 'surrogate' else {
        **expired(), 'expires_at': 10**1000}
    monkeypatch.setattr(vault, 'get', lambda *a, **k: value)
    monkeypatch.setattr(vault, 'snapshot', lambda *a, **k: SimpleNamespace(value=value))
    with pytest.raises(oauth.OAuthError) as caught:
        getattr(oauth, entrypoint)(OWNER)
    assert str(caught.value) == 'stored Google credential bundle is invalid'
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert 'owned-private-marker' not in repr(caught.value)


@pytest.mark.parametrize('adapter', ['gmail', 'calendar'])
@pytest.mark.parametrize('damage', ['surrogate', 'huge_integer'])
def test_actual_authenticated_invalid_legacy_bundle_refuses_environment_fallback(monkeypatch, adapter, damage):
    import base64
    import hashlib
    from cryptography.fernet import Fernet
    value = {**expired(), 'access_token': 'owned-private-marker\ud800'} if damage == 'surrogate' else {
        **expired(), 'expires_at': 10**1000}
    original = Fernet(base64.urlsafe_b64encode(hashlib.sha256(KEY.encode()).digest())).encrypt(
        json.dumps({'google': value}, ensure_ascii=True).encode())
    store.backend().put('vault', vault._key(OWNER), original)
    monkeypatch.setenv('GMAIL_ACCESS_TOKEN', 'owned-fallback-must-not-be-used')
    monkeypatch.setattr(gmail, '_token', {'value': 'owned-cache-must-not-be-used',
                                        'expires': time.time()+3600})
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(1)
        raise AssertionError('invalid authenticated state must not license dispatch')

    monkeypatch.setattr(gmail.urllib.request, 'urlopen', forbidden)
    with pytest.raises(vault.VaultError) as caught:
        (gmail if adapter == 'gmail' else calendar)._request('GET', '/owned')
    assert 'owned-private-marker' not in str(caught.value)
    assert calls == [] and raw_vault() == original

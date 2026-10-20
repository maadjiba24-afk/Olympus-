"""Local vault resolution controls; no broker adapter or connection is invoked."""
import traceback

import pytest
from olympus import config, store, vault, vault_records
from olympus.trading.brokers.base import BrokerCredentials, resolve_credentials
from olympus.trading.errors import BrokerUnavailable


@pytest.fixture(autouse=True)
def owned_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'MEMORY_DIR', tmp_path / 'owned-memory')
    monkeypatch.setenv('OLYMPUS_ENV', 'test')
    monkeypatch.setenv('OLYMPUS_SECRET_KEY', 'owned-broker-resolution-custody')
    monkeypatch.delenv('OLYMPUS_SECRET_KEY_FILE', raising=False)
    monkeypatch.delenv('OLYMPUS_DATABASE_URL', raising=False)
    store.reset()
    yield
    store.reset()


def test_actual_vault_resolution_preserves_exact_owner_and_returns_copy():
    first, second = 'owned:broker/a', 'owned:broker-a'
    value = {'api_key': 'owned-private-first', 'options': {'paper': True, 'port': 1234}}
    vault.put(first, 'broker-credential', value)
    vault.put(second, 'broker-credential', {'api_key': 'owned-private-second'})
    handle = BrokerCredentials(ref='broker-credential', user=first)
    resolved = resolve_credentials(handle)
    assert resolved == value
    resolved['options']['port'] = 9999
    assert vault.get(first, 'broker-credential') == value
    assert resolve_credentials(BrokerCredentials(ref='broker-credential', user=second)) == {'api_key': 'owned-private-second'}
    assert 'owned-private-' not in repr(handle) + str(handle) + str(handle.to_dict())


def test_actual_string_credentials_remain_supported():
    vault.put('owned-user', 'token', 'owned-private-token')
    assert resolve_credentials(BrokerCredentials(ref='token', user='owned-user')) == {'value': 'owned-private-token'}


def test_only_inspected_none_is_missing(monkeypatch):
    seen = []
    def missing(user, ref):
        seen.append((user, ref))
        return None
    monkeypatch.setattr(vault, 'get', missing)
    with pytest.raises(BrokerUnavailable, match='no such credential'):
        resolve_credentials(BrokerCredentials(ref='owned-ref', user='owned-user'))
    assert seen == [('owned-user', 'owned-ref')]


def test_store_failure_has_no_private_text_or_exception_chain(monkeypatch):
    def unavailable(*args):
        raise RuntimeError('owned-private-store-detail')
    monkeypatch.setattr(vault, 'get', unavailable)
    with pytest.raises(BrokerUnavailable, match='credential store unavailable') as raised:
        resolve_credentials(BrokerCredentials(ref='owned-ref', user='owned-user'))
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert 'owned-private-store-detail' not in ''.join(traceback.format_exception(raised.value))


@pytest.mark.parametrize('invalid', ['', {}, [], [('api_key', 'owned-private')], True, 3,
    {'token': float('nan')}, {'token': float('inf')}, {'token': object()},
    {1: 'owned-private'}, {'token': 'x' * (vault_records.MAX_STRING_BYTES + 1)}])
def test_malformed_bundle_is_neither_missing_nor_coerced(monkeypatch, invalid):
    monkeypatch.setattr(vault, 'get', lambda *args: invalid)
    with pytest.raises(BrokerUnavailable, match='stored credential bundle is invalid') as raised:
        resolve_credentials(BrokerCredentials(ref='owned-ref'))
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert 'owned-private' not in ''.join(traceback.format_exception(raised.value))


def test_mapping_subclass_is_not_coerced(monkeypatch):
    calls = []
    class Foreign(dict):
        def items(self):
            calls.append('items')
            raise AssertionError('must not inspect a foreign mapping')
        def __iter__(self):
            calls.append('iter')
            raise AssertionError('must not iterate a foreign mapping')
    monkeypatch.setattr(vault, 'get', lambda *args: Foreign(api_key='owned-private'))
    with pytest.raises(BrokerUnavailable, match='stored credential bundle is invalid'):
        resolve_credentials(BrokerCredentials(ref='owned-ref'))
    assert calls == []


@pytest.mark.parametrize('invalid', [None, {}, 'owned-ref', object(),
    BrokerCredentials(ref=['owned-ref']), BrokerCredentials(ref='owned-ref', user=''),
    BrokerCredentials(ref='owned-ref', user=True), BrokerCredentials(ref='owned\x00ref')])
def test_invalid_handle_is_refused_before_vault_access(monkeypatch, invalid):
    def forbidden(*args):
        pytest.fail('invalid handle reached the credential store')
    monkeypatch.setattr(vault, 'get', forbidden)
    with pytest.raises(BrokerUnavailable, match='invalid credential handle'):
        resolve_credentials(invalid)


@pytest.mark.parametrize('interrupt_type', [KeyboardInterrupt, SystemExit])
def test_store_system_interruption_keeps_original_identity(monkeypatch, interrupt_type):
    original = interrupt_type('owned interruption')
    def interrupt(*args):
        raise original
    monkeypatch.setattr(vault, 'get', interrupt)
    with pytest.raises(interrupt_type) as raised:
        resolve_credentials(BrokerCredentials(ref='owned-ref'))
    assert raised.value is original


def test_valid_mock_bundle_is_deep_copied_before_adapter_receives_it(monkeypatch):
    value = {'api_key': 'owned-private', 'nested': {'enabled': False, 'values': [1, 2.5, None]}}
    monkeypatch.setattr(vault, 'get', lambda *args: value)
    selected = resolve_credentials(BrokerCredentials(ref='owned-ref'))
    assert selected == value and selected is not value
    selected['nested']['values'].append(3)
    assert value['nested']['values'] == [1, 2.5, None]

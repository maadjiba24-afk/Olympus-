"""Mock connection-validation ownership; never opens a database or socket."""
import sys
from types import ModuleType, SimpleNamespace

import pytest

from olympus import native_cleanup, vault, vault_selection as selection


@pytest.fixture
def owned(monkeypatch):
    for name in tuple(selection.os.environ):
        if name.startswith('PG'):
            monkeypatch.delenv(name)
    module = ModuleType('psycopg')
    module.connect = lambda *args, **kwargs: pytest.fail('ambient connection selector used')
    monkeypatch.setitem(sys.modules, 'psycopg', module)
    options = {'host': '127.0.0.1', 'port': '55483', 'dbname': 'owned', 'user': 'owned'}
    monkeypatch.setattr(selection, '_parameters', lambda dsn: (dict(options), False))
    return options


def _fault(kind, label):
    return {'ordinary': RuntimeError, 'interrupt': KeyboardInterrupt, 'exit': SystemExit}[kind](label)


@pytest.mark.parametrize('stage', ['environment', 'info', 'authentication'])
@pytest.mark.parametrize('body_kind', ['ordinary', 'interrupt', 'exit'])
@pytest.mark.parametrize('cleanup_kind', ['clean', 'ordinary', 'interrupt', 'exit'])
def test_validation_primary_survives_one_close_attempt(owned, monkeypatch, stage, body_kind, cleanup_kind):
    primary = _fault(body_kind, 'owned-validation-primary')
    cleanup = None if cleanup_kind == 'clean' else _fault(cleanup_kind, 'owned-close-secondary')
    calls = {'connect': 0, 'close': 0, 'environment': 0, 'fault': 0}
    def fail():
        calls['fault'] += 1
        raise primary
    class Authentication:
        used_password = True
        @property
        def used_gssapi(self):
            if stage == 'authentication':
                fail()
            return False
    class Connection:
        pgconn = Authentication()
        @property
        def info(self):
            if stage == 'info':
                fail()
            return SimpleNamespace(host=owned['host'], port=int(owned['port']),
                dbname=owned['dbname'], user=owned['user'], hostaddr='127.0.0.1')
        def close(self):
            calls['close'] += 1
            if cleanup is not None:
                raise cleanup
    connection = Connection()
    def connect(dsn, **kwargs):
        calls['connect'] += 1
        assert dsn == 'owned-selection' and kwargs == {'connect_timeout': 2}
        return connection
    original_environment = selection._environment
    def environment():
        calls['environment'] += 1
        original_environment()
        if stage == 'environment' and calls['environment'] == 2:
            fail()
    monkeypatch.setattr(selection, '_environment', environment)
    backend = selection.capture('owned-selection', connect=connect)
    with pytest.raises(type(primary)) as caught:
        backend._psycopg.connect('owned-selection', connect_timeout=2)
    assert caught.value is primary
    assert calls == {'connect': 1, 'close': 1, 'environment': 2, 'fault': 1}
    assert native_cleanup.unknown(primary) is (cleanup is not None)
    assert primary.__cause__ is None and primary.__context__ is None


@pytest.mark.parametrize('body_kind', ['ordinary', 'interrupt', 'exit'])
def test_unobserved_connection_failure_never_guesses_cleanup(owned, body_kind):
    primary = _fault(body_kind, 'owned-connect-primary')
    attempts = []
    def connect(dsn, **kwargs):
        attempts.append((dsn, kwargs))
        raise primary
    backend = selection.capture('owned-selection', connect=connect)
    with pytest.raises(type(primary)) as caught:
        backend._psycopg.connect('owned-selection')
    assert caught.value is primary and attempts == [('owned-selection', {})]
    assert not native_cleanup.unknown(primary)


@pytest.mark.parametrize('variant', ['dsn', 'positional', 'routing', 'environment'])
def test_refused_connection_selection_has_zero_dispatch(owned, monkeypatch, variant):
    calls = []
    backend = selection.capture('owned-selection', connect=lambda *a, **k: calls.append((a, k)))
    args, kwargs = (), {}
    dsn = 'changed-selection' if variant == 'dsn' else 'owned-selection'
    if variant == 'positional':
        args = ('unexpected',)
    elif variant == 'routing':
        kwargs['host'] = '127.0.0.2'
    elif variant == 'environment':
        monkeypatch.setenv('PGHOST', 'owned-invalid-selector')
    with pytest.raises(selection.SelectionError):
        backend._psycopg.connect(dsn, *args, **kwargs)
    assert calls == []


def test_valid_connection_returns_original_without_closing_it(owned):
    closed, connected = [], []
    connection = SimpleNamespace(
        info=SimpleNamespace(host=owned['host'], port=int(owned['port']),
            dbname=owned['dbname'], user=owned['user'], hostaddr='127.0.0.1'),
        pgconn=SimpleNamespace(used_gssapi=False, used_password=True),
        close=lambda: closed.append(True))
    def connect(dsn, **kwargs):
        connected.append((dsn, kwargs))
        return connection
    backend = selection.capture('owned-selection', connect=connect)
    assert backend._psycopg.connect('owned-selection', autocommit=False) is connection
    assert connected == [('owned-selection', {'autocommit': False})] and closed == []
    connection.close()
    assert closed == [True]


def test_public_vault_guard_sanitizes_validation_and_preserves_cleanup_fact(owned):
    calls = []
    class Connection:
        @property
        def info(self):
            raise RuntimeError('synthetic-private-validation-detail')
        def close(self):
            calls.append('close')
            raise OSError('synthetic-private-close-detail')
    backend = selection.capture('owned-selection', connect=lambda *a, **k: Connection())
    guarded = vault._guard(lambda: backend._psycopg.connect('owned-selection'))
    with pytest.raises(vault.VaultError) as caught:
        guarded()
    assert calls == ['close'] and native_cleanup.unknown(caught.value)
    assert str(caught.value) == 'vault operation is unavailable or invalid'
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert 'synthetic-private' not in repr(caught.value)


@pytest.mark.parametrize('variant', ['host', 'port', 'dbname', 'user', 'address', 'socket', 'gss', 'password'])
def test_actual_validation_refusal_keeps_fixed_error_when_close_fails(owned, monkeypatch, variant):
    local = variant == 'socket'
    info = SimpleNamespace(host=owned['host'], port=int(owned['port']),
        dbname=owned['dbname'], user=owned['user'], hostaddr='127.0.0.1')
    authentication = SimpleNamespace(used_gssapi=False, used_password=not local)
    if variant in ('host', 'dbname', 'user'):
        setattr(info, variant, 'owned-different')
    elif variant == 'port':
        info.port += 1
    elif variant == 'address':
        owned['hostaddr'] = '127.0.0.2'
    elif variant == 'gss':
        authentication.used_gssapi = True
    elif variant == 'password':
        authentication.used_password = False
    monkeypatch.setattr(selection, '_parameters', lambda dsn: (dict(owned), local))
    calls = []
    def close():
        calls.append('close')
        raise OSError('owned-close-secondary')
    connection = SimpleNamespace(info=info, pgconn=authentication, close=close)
    def connect(*args, **kwargs):
        calls.append('connect')
        return connection
    backend = selection.capture('owned-selection', connect=connect)
    with pytest.raises(selection.SelectionError) as caught:
        backend._psycopg.connect('owned-selection')
    assert calls == ['connect', 'close'] and native_cleanup.unknown(caught.value)
    assert str(caught.value) in (
        'actual PostgreSQL connection differs from captured selection',
        'actual PostgreSQL address differs from captured selection',
        'local PostgreSQL snapshot became a TCP connection',
        'PostgreSQL authentication differs from supported snapshot custody')
    assert caught.value.__cause__ is None and caught.value.__context__ is None

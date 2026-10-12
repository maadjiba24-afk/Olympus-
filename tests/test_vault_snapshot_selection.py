"""Owned file snapshots and mocked, explicitly selected PostgreSQL authority."""
import builtins
from contextlib import contextmanager
from dataclasses import replace
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from olympus import config, native_cleanup, store, vault, vault_selection
from olympus import vault_custody as custody, vault_native as native, vault_storage as storage


def forbidden(*args, **kwargs):
    pytest.fail('refused snapshot selection reached an unselected callback')


@pytest.fixture
def owned(tmp_path, monkeypatch):
    root = tmp_path / 'memory'
    root.mkdir()
    monkeypatch.setattr(config, 'MEMORY_DIR', root)
    monkeypatch.setattr(store, '_backend', store.FileStore())
    monkeypatch.delenv('OLYMPUS_DATABASE_URL', raising=False)
    monkeypatch.delenv('OLYMPUS_SECRET_KEY_FILE', raising=False)
    monkeypatch.setenv('OLYMPUS_SECRET_KEY', 'owned-snapshot-material')
    monkeypatch.setenv('OLYMPUS_ENV', 'test')
    return root


def identity(root):
    directory = native.Directory(root)
    try:
        return storage._directory_identity(directory)
    finally:
        directory.close()


def database(connect=forbidden):
    backend = object.__new__(store.PostgresStore)
    backend._dsn = 'owned-first-database-selection'
    backend._psycopg = SimpleNamespace(connect=connect)
    return backend


def inventory(*roots):
    return {str(root): None if not root.exists() else {
        'directories': sorted(p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_dir()),
        'files': {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}}
        for root in roots}


@pytest.mark.parametrize('kind', ['file', 'cold-pg', 'cached-pg'])
@pytest.mark.parametrize('change', ['wrong', 'absent', 'replaced'])
def test_preference_root_refuses_before_any_backend_key_or_connector_callback(owned, monkeypatch, kind, change):
    expected = identity(owned)
    retained = owned.with_name('retained-root')
    if change == 'wrong':
        expected = (expected[0], expected[1] + 1)
    else:
        owned.rename(retained)
        if change == 'replaced':
            owned.mkdir()
    if kind == 'cached-pg':
        monkeypatch.setattr(store, '_backend', database())
    else:
        monkeypatch.setattr(store, '_backend', None)
        if kind == 'cold-pg':
            monkeypatch.setenv('OLYMPUS_DATABASE_URL', 'owned-first-database-selection')
    monkeypatch.setattr(store.FileStore, '__init__', forbidden)
    monkeypatch.setattr(vault, '_resolve_snapshot_connector', forbidden)
    monkeypatch.setattr(vault_selection, 'capture', forbidden)
    monkeypatch.setattr(custody, 'capture', forbidden)
    monkeypatch.setattr(vault, '_snapshots_captured', forbidden)
    before = inventory(owned, retained)
    with pytest.raises(vault.VaultError) as caught:
        vault._snapshots_for_preferences('owned-owner', ('site:example.test',),
            root=owned, root_identity=expected, for_update=True)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert inventory(owned, retained) == before


@pytest.mark.parametrize('kind', ['cold-pg', 'cached-pg'])
def test_explicit_file_binding_refuses_pg_without_import_or_resolution(owned, monkeypatch, kind):
    expected = identity(owned)
    if kind == 'cold-pg':
        monkeypatch.setattr(store, '_backend', None)
        monkeypatch.setenv('OLYMPUS_DATABASE_URL', 'owned-first-database-selection')
    else:
        monkeypatch.setattr(store, '_backend', database())
    original_import = builtins.__import__
    def importing(name, *args, **kwargs):
        if name == 'psycopg' or name.startswith('psycopg.'):
            forbidden()
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', importing)
    monkeypatch.setattr(vault, '_resolve_snapshot_connector', forbidden)
    monkeypatch.setattr(vault_selection, 'capture', forbidden)
    monkeypatch.setattr(custody, 'capture', forbidden)
    with pytest.raises(vault.VaultError):
        vault._capture('owned-owner', snapshot_selection=True, root=owned, expected_root_identity=expected)
    assert list(owned.iterdir()) == []


@pytest.mark.parametrize('invalid', [None, False, 'not-a-connector', 17])
def test_cached_invalid_connector_never_falls_back_to_ambient_default(owned, monkeypatch, invalid):
    monkeypatch.setattr(store, '_backend', database(invalid))
    monkeypatch.setitem(sys.modules, 'psycopg', SimpleNamespace(connect=forbidden))
    monkeypatch.setattr(vault_selection, 'capture', forbidden)
    monkeypatch.setattr(custody, 'capture', forbidden)
    with pytest.raises(vault.VaultError):
        vault.snapshot('owned-owner', 'site:example.test')
    assert list(owned.iterdir()) == []


@pytest.mark.parametrize('invalid', [None, False, 'not-a-connector', 17])
def test_cold_invalid_connector_refuses_before_backend_and_custody(owned, monkeypatch, invalid):
    expected = identity(owned)
    monkeypatch.setattr(store, '_backend', None)
    monkeypatch.setenv('OLYMPUS_DATABASE_URL', 'owned-first-database-selection')
    monkeypatch.setitem(sys.modules, 'psycopg', SimpleNamespace(connect=invalid))
    monkeypatch.setattr(vault_selection, 'capture', forbidden)
    monkeypatch.setattr(custody, 'capture', forbidden)
    with pytest.raises(vault.VaultError):
        vault._snapshots_for_preferences('owned-owner', ('site:example.test',), root=owned, root_identity=expected)
    assert list(owned.iterdir()) == []


@pytest.mark.parametrize('kind', ['cold', 'cached'])
def test_pg_selector_cannot_return_file_backend(owned, monkeypatch, kind):
    expected, calls = identity(owned), []
    monkeypatch.setattr(store, '_backend', None if kind == 'cold' else database())
    monkeypatch.setenv('OLYMPUS_DATABASE_URL', 'owned-first-database-selection')
    monkeypatch.setitem(sys.modules, 'psycopg', SimpleNamespace(connect=forbidden))
    def select(*args, **kwargs):
        calls.append('selection')
        return store.FileStore()
    monkeypatch.setattr(vault_selection, 'capture', select)
    monkeypatch.setattr(custody, 'capture', forbidden)
    monkeypatch.setattr(vault, '_snapshots_captured', forbidden)
    with pytest.raises(vault.VaultError):
        vault._snapshots_for_preferences('owned-owner', ('site:example.test',), root=owned, root_identity=expected)
    assert calls == ['selection'] and list(owned.iterdir()) == []


@pytest.mark.parametrize('kind', ['cold', 'cached'])
@pytest.mark.parametrize('key_kind', ['environment', 'relative-file'])
def test_selected_connector_dsn_root_and_key_survive_callback_drift(owned, monkeypatch, kind, key_kind):
    expected = identity(owned)
    other = owned.parent / 'other'
    other.mkdir()
    monkeypatch.chdir(owned.parent)
    if key_kind == 'relative-file':
        fd = os.open(owned.parent / 'key', os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
        try:
            assert os.write(fd, b'owned-snapshot-material') == len(b'owned-snapshot-material')
        finally:
            os.close(fd)
        monkeypatch.delenv('OLYMPUS_SECRET_KEY')
        monkeypatch.setenv('OLYMPUS_SECRET_KEY_FILE', 'key')
    calls, captures = [], []
    def connector(*args, **kwargs):
        forbidden()
    backend = database(connector)
    def drift():
        monkeypatch.chdir(other)
        monkeypatch.setattr(config, 'MEMORY_DIR', other / 'foreign-memory')
        monkeypatch.setattr(store, '_backend', store.FileStore())
        monkeypatch.setenv('OLYMPUS_DATABASE_URL', 'owned-other-database-selection')
        monkeypatch.setenv('OLYMPUS_SECRET_KEY', 'owned-other-key')
        monkeypatch.setenv('OLYMPUS_SECRET_KEY_FILE', 'foreign-key')
    class Driver:
        @property
        def connect(self):
            calls.append('resolve')
            drift()
            return connector
    if kind == 'cold':
        monkeypatch.setattr(store, '_backend', None)
        monkeypatch.setenv('OLYMPUS_DATABASE_URL', backend._dsn)
        monkeypatch.setitem(sys.modules, 'psycopg', Driver())
    else:
        monkeypatch.setattr(store, '_backend', backend)
    def select(dsn, *, connect, initialize):
        calls.append('select')
        assert dsn == 'owned-first-database-selection' and connect is connector and initialize is (kind == 'cold')
        drift()
        backend._psycopg.connect = forbidden
        return backend
    def body(captured, names, *, for_update):
        calls.append('body')
        captures.append(captured)
        assert names == ('site:example.test',) and for_update is True
        assert captured.root == owned and captured._root_identity is None
        assert captured.backend is backend and captured.passphrase.material == b'owned-snapshot-material'
        return {'observed': True}
    monkeypatch.setattr(vault_selection, 'capture', select)
    monkeypatch.setattr(vault, '_snapshots_captured', body)
    assert vault._snapshots_for_preferences('owned-owner', ('site:example.test',),
        root=owned, root_identity=expected, for_update=True) == {'observed': True}
    assert calls == (['resolve', 'select', 'body'] if kind == 'cold' else ['select', 'body'])
    captures[0].check()
    assert list(owned.iterdir()) == [] and list(other.iterdir()) == []


@pytest.mark.parametrize('change', ['replacement', 'missing'])
@pytest.mark.parametrize('phase', ['resolve', 'select', 'custody'])
def test_callback_root_change_refuses_before_snapshot_body(owned, monkeypatch, change, phase):
    expected = identity(owned)
    (owned / 'marker').write_bytes(b'owned-retained-marker')
    retained = owned.with_name('retained-root')
    calls, native_refusals = [], []
    backend = database()
    monkeypatch.setattr(store, '_backend', None)
    monkeypatch.setenv('OLYMPUS_DATABASE_URL', backend._dsn)
    def change_root():
        try:
            owned.rename(retained)
        except OSError as error:
            assert os.name == 'nt' and error.winerror in (5, 32, 33)
            native_refusals.append(True)
            raise
        if change == 'replacement':
            owned.mkdir()
    class Driver:
        @property
        def connect(self):
            calls.append('resolve')
            if phase == 'resolve':
                change_root()
            return forbidden
    def select(*args, **kwargs):
        calls.append('select')
        if phase == 'select':
            change_root()
        return backend
    def key(selectors):
        calls.append('custody')
        if phase == 'custody':
            change_root()
        return custody.CapturedPassphrase(b'owned-material', 'environment', False, False)
    monkeypatch.setitem(sys.modules, 'psycopg', Driver())
    monkeypatch.setattr(vault_selection, 'capture', select)
    monkeypatch.setattr(custody, 'capture', key)
    monkeypatch.setattr(vault, '_snapshots_captured', forbidden)
    with pytest.raises(vault.VaultError):
        vault._snapshots_for_preferences('owned-owner', ('site:example.test',), root=owned, root_identity=expected)
    assert calls == ['resolve', 'select', 'custody'][:['resolve', 'select', 'custody'].index(phase) + 1]
    if native_refusals:
        assert not retained.exists() and (owned / 'marker').read_bytes() == b'owned-retained-marker'
    else:
        assert (retained / 'marker').read_bytes() == b'owned-retained-marker'
        assert (list(owned.iterdir()) == []) if change == 'replacement' else not owned.exists()


@pytest.mark.parametrize('phase', ['resolve', 'select', 'custody', 'body'])
@pytest.mark.parametrize('fault', [RuntimeError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('close_fault', [None, RuntimeError, KeyboardInterrupt, SystemExit])
def test_root_guard_close_preserves_callback_primary_and_cleanup_facts(owned, monkeypatch, phase, fault, close_fault):
    expected = identity(owned)
    primary = fault('owned-private-primary')
    secondary = close_fault('owned-private-close') if close_fault is not None else None
    backend, calls, closes = database(), [], []
    monkeypatch.setattr(store, '_backend', None)
    monkeypatch.setenv('OLYMPUS_DATABASE_URL', backend._dsn)
    def reach(name, value):
        calls.append(name)
        if phase == name:
            raise primary
        return value
    class Driver:
        @property
        def connect(self):
            return reach('resolve', forbidden)
    monkeypatch.setitem(sys.modules, 'psycopg', Driver())
    monkeypatch.setattr(vault_selection, 'capture', lambda *a, **k: reach('select', backend))
    monkeypatch.setattr(custody, 'capture', lambda *a, **k: reach('custody',
        custody.CapturedPassphrase(b'owned-material', 'environment', False, False)))
    monkeypatch.setattr(vault, '_snapshots_captured', lambda *a, **k: reach('body', {}))
    original_close = native.Directory.close
    def close(directory):
        closes.append(directory)
        original_close(directory)
        if secondary is not None:
            raise secondary
    monkeypatch.setattr(native.Directory, 'close', close)
    expected_type = vault.VaultError if fault is RuntimeError else fault
    with pytest.raises(expected_type) as caught:
        vault._snapshots_for_preferences('owned-owner', ('site:example.test',), root=owned, root_identity=expected)
    assert calls == ['resolve', 'select', 'custody', 'body'][:['resolve', 'select', 'custody', 'body'].index(phase) + 1]
    assert len(closes) == 1
    assert closes[0]._closed and closes[0].fd is None and closes[0].win is None and closes[0]._parents == []
    assert native_cleanup.unknown(caught.value) is (secondary is not None)
    if fault is RuntimeError:
        assert caught.value.__cause__ is None and caught.value.__context__ is None
        assert 'owned-private' not in str(caught.value)
    else:
        assert caught.value is primary
    assert list(owned.iterdir()) == []


@pytest.mark.parametrize('fault', [RuntimeError, KeyboardInterrupt, SystemExit])
def test_successful_body_cannot_hide_root_close_failure(owned, monkeypatch, fault):
    expected, closes, calls = identity(owned), [], []
    error = fault('owned-private-close')
    def body(*args, **kwargs):
        calls.append('body')
        return {'not_delivered': True}
    original_close = native.Directory.close
    def close(directory):
        closes.append(directory)
        original_close(directory)
        raise error
    monkeypatch.setattr(vault, '_snapshots_captured', body)
    monkeypatch.setattr(native.Directory, 'close', close)
    with pytest.raises(vault.VaultError if fault is RuntimeError else fault) as caught:
        vault._snapshots_for_preferences('owned-owner', ('site:example.test',), root=owned, root_identity=expected)
    assert calls == ['body'] and len(closes) == 1 and native_cleanup.unknown(caught.value)
    assert closes[0]._closed and closes[0].fd is None and closes[0].win is None and closes[0]._parents == []
    if fault is not RuntimeError:
        assert caught.value is error
    else:
        assert caught.value.__cause__ is None and caught.value.__context__ is None


@pytest.mark.requires_crypto
def test_file_snapshot_remains_usable_after_temporary_guard_closes(owned, monkeypatch):
    expected = identity(owned)
    selected = vault._snapshots_for_preferences('owned-owner', ('site:example.test',),
        root=owned, root_identity=expected, for_update=True)['site:example.test']
    assert selected._captured._root_identity == expected and selected._storage_identity[0] == expected
    assert len(selected._storage_identity) == 4
    monkeypatch.setattr(vault, '_snapshot_selection', forbidden)
    monkeypatch.setattr(custody, 'capture', forbidden)
    pending = vault.prepare_snapshot_update(selected, {'username': 'owned-user', 'password': 'owned-password'})
    assert vault.settle_snapshot_update(pending) == {'username': 'owned-user', 'password': 'owned-password'}
    assert vault.settle_snapshot_update(pending) == {'username': 'owned-user', 'password': 'owned-password'}
    assert b'owned-password' not in next((owned / 'store/vault').iterdir()).read_bytes()


@pytest.mark.requires_crypto
@pytest.mark.parametrize('component', ['root', 'store', 'vault', 'lock'])
def test_returned_file_snapshot_refuses_replaced_storage_without_writes(owned, component):
    selected = vault._snapshots_for_preferences('owned-owner', ('site:example.test',),
        root=owned, root_identity=identity(owned), for_update=True)['site:example.test']
    pending = vault.prepare_snapshot_update(selected, {'password': 'owned-password'})
    path = {'root': owned, 'store': owned / 'store', 'vault': owned / 'store/vault',
        'lock': owned / 'store/vault.transaction.lock'}[component]
    retained = path.with_name(path.name + '-retained')
    path.rename(retained)
    if component == 'lock':
        path.write_bytes(b'owned-other-lock')
    else:
        path.mkdir()
    before = inventory(owned, retained) if component == 'root' else inventory(owned)
    with pytest.raises(vault.VaultError):
        vault.settle_snapshot_update(pending)
    assert (inventory(owned, retained) if component == 'root' else inventory(owned)) == before


@pytest.mark.requires_crypto
def test_public_first_use_file_snapshot_creates_only_owned_root(owned, monkeypatch):
    absent = owned / 'first-use'
    monkeypatch.setattr(config, 'MEMORY_DIR', absent)
    monkeypatch.setattr(store, '_backend', None)
    selected = vault.snapshot('owned-owner', 'site:example.test', for_update=True)
    assert selected._captured._root_identity is None and absent.is_dir()
    assert selected.value is None and len(selected._storage_identity) == 4


@pytest.mark.requires_crypto
@pytest.mark.parametrize('bound', [False, True])
def test_pg_snapshot_and_settlement_never_receive_filesystem_authority(owned, monkeypatch, bound):
    expected, backend, rows, calls = identity(owned), database(), {}, []
    absent = owned / 'absent-local-root'
    monkeypatch.setattr(config, 'MEMORY_DIR', absent)
    monkeypatch.setattr(store, '_backend', backend)
    monkeypatch.setattr(vault_selection, 'capture', lambda *a, **k: backend)
    class Transaction:
        def get(self, namespace, key, *, cap):
            calls.append('get')
            return rows.get((namespace, key))
        def put(self, namespace, key, value):
            calls.append('put')
            rows[(namespace, key)] = value
    @contextmanager
    def transaction(self, *, timeout):
        assert self is backend and timeout >= 0
        calls.append('transaction')
        yield Transaction()
    monkeypatch.setattr(store.PostgresStore, 'vault_transaction', transaction)
    monkeypatch.setattr(storage, 'FileTransaction', forbidden)
    if bound:
        selected = vault._snapshots_for_preferences('owned-owner', ('site:example.test',),
            root=owned, root_identity=expected, for_update=True)['site:example.test']
    else:
        monkeypatch.setattr(native, 'Directory', forbidden)
        selected = vault.snapshot('owned-owner', 'site:example.test', for_update=True)
    assert selected._captured._root_identity is None and selected._storage_identity is None
    monkeypatch.setattr(vault_selection, 'capture', forbidden)
    monkeypatch.setattr(custody, 'capture', forbidden)
    pending = vault.prepare_snapshot_update(selected, {'password': 'owned-password'})
    assert vault.settle_snapshot_update(pending) == {'password': 'owned-password'}
    assert calls.count('transaction') == 2 and calls.count('put') == 1
    assert not absent.exists() and list(owned.iterdir()) == []


@pytest.mark.requires_crypto
@pytest.mark.parametrize('kind', ['cold', 'cached'])
def test_real_pg_selector_and_transaction_retain_connector_after_guard_close(owned, monkeypatch, kind):
    from types import ModuleType
    expected, calls, connections, rows, closed_guards = identity(owned), [], [], {}, []
    selected_dsn = 'owned-explicit-database-selection'
    options = {'host': '127.0.0.1', 'port': '55483', 'dbname': 'owned', 'user': 'owned', 'password': 'owned-synthetic-password'}
    for name in tuple(os.environ):
        if name.startswith('PG'):
            monkeypatch.delenv(name)
    class Cursor:
        def __init__(self, value=None):
            self.value = value
        def fetchone(self):
            return self.value
    class Connection:
        info = SimpleNamespace(host='127.0.0.1', port=55483, dbname='owned', user='owned', hostaddr='127.0.0.1')
        pgconn = SimpleNamespace(used_gssapi=False, used_password=True)
        closed = False
        def __enter__(self):
            assert not self.closed
            return self
        def __exit__(self, *args):
            self.close()
        def close(self):
            assert not self.closed
            self.closed = True
            calls.append('close')
        @contextmanager
        def transaction(self):
            assert not self.closed
            calls.append('transaction')
            yield self
        def execute(self, sql, params=None):
            assert not self.closed
            if sql.startswith('CREATE TABLE IF NOT EXISTS olympus_kv ('):
                assert kind == 'cold' and params is None
                calls.append('ddl')
                return Cursor()
            if sql == "SELECT set_config('statement_timeout', %s, true)":
                assert len(params) == 1 and int(params[0]) > 0
                return Cursor()
            if sql == 'SELECT pg_try_advisory_xact_lock(%s)':
                assert len(params) == 1 and type(params[0]) is int
                return Cursor((True,))
            if sql == ('SELECT octet_length(v), CASE WHEN octet_length(v)<=%s '
                       'THEN v ELSE NULL END FROM olympus_kv WHERE ns=%s AND k=%s'):
                cap, namespace, key = params
                value = rows.get((namespace, key))
                return Cursor(None if value is None else (len(value), value if len(value) <= cap else None))
            if sql == ('INSERT INTO olympus_kv (ns,k,v) VALUES (%s,%s,%s) '
                       'ON CONFLICT (ns,k) DO UPDATE SET v=EXCLUDED.v'):
                namespace, key, value = params
                assert namespace == 'vault' and type(value) is bytes
                rows[(namespace, key)] = value
                calls.append('put')
                return Cursor()
            pytest.fail('unexpected SQL in owned fake connection')
    def original_connect(dsn, **kwargs):
        assert dsn == selected_dsn and set(kwargs) <= {'autocommit', 'connect_timeout'}
        assert kwargs['autocommit'] is True
        calls.append('connect')
        connection = Connection()
        connections.append(connection)
        return connection
    driver, conninfo = ModuleType('psycopg'), ModuleType('psycopg.conninfo')
    driver.connect = original_connect
    def parse(dsn):
        assert dsn == selected_dsn
        calls.append('parse')
        return dict(options)
    conninfo.conninfo_to_dict = parse
    monkeypatch.setitem(sys.modules, 'psycopg', driver)
    monkeypatch.setitem(sys.modules, 'psycopg.conninfo', conninfo)
    if kind == 'cold':
        monkeypatch.setattr(store, '_backend', None)
        monkeypatch.setenv('OLYMPUS_DATABASE_URL', selected_dsn)
    else:
        backend = database(original_connect)
        backend._dsn = selected_dsn
        monkeypatch.setattr(store, '_backend', backend)
    original_close = native.Directory.close
    def close(directory):
        original_close(directory)
        closed_guards.append(directory)
    monkeypatch.setattr(native.Directory, 'close', close)
    monkeypatch.setattr(storage, 'FileTransaction', forbidden)
    selected = vault._snapshots_for_preferences('owned-owner', ('site:example.test',),
        root=owned, root_identity=expected, for_update=True)['site:example.test']
    assert len(closed_guards) == 1 and closed_guards[0]._closed and closed_guards[0].fd is None
    assert closed_guards[0].win is None and closed_guards[0]._parents == []
    assert selected._storage_identity is None and selected._captured._root_identity is None
    assert type(selected._captured.backend) is store.PostgresStore
    assert selected._captured.backend._dsn == selected_dsn
    monkeypatch.setattr(driver, 'connect', forbidden)
    monkeypatch.setattr(store, '_backend', database(forbidden))
    monkeypatch.setenv('OLYMPUS_DATABASE_URL', 'owned-unselected-database')
    monkeypatch.setenv('OLYMPUS_SECRET_KEY', 'owned-unselected-key')
    monkeypatch.setattr(config, 'MEMORY_DIR', owned / 'unselected-root')
    monkeypatch.setattr(vault_selection, 'capture', forbidden)
    monkeypatch.setattr(custody, 'capture', forbidden)
    monkeypatch.setattr(native, 'Directory', forbidden)
    pending = vault.prepare_snapshot_update(selected, {'password': 'owned-password'})
    assert vault.settle_snapshot_update(pending) == {'password': 'owned-password'}
    assert vault.settle_snapshot_update(pending) == {'password': 'owned-password'}
    assert calls.count('parse') == 1 and calls.count('transaction') == 3
    assert calls.count('ddl') == (1 if kind == 'cold' else 0)
    assert calls.count('connect') == calls.count('close') == (4 if kind == 'cold' else 3)
    assert calls.count('put') == 2 and all(connection.closed for connection in connections)
    assert list(owned.iterdir()) == []
    assert len(rows) == 1 and b'owned-password' not in next(iter(rows.values()))


def test_file_factory_cannot_return_database_backend(owned, monkeypatch):
    expected, calls = identity(owned), []
    monkeypatch.setattr(store, '_backend', None)
    original_selection = vault._snapshot_selection
    def factory():
        calls.append('factory')
        return database()
    def select(*args, **kwargs):
        return replace(original_selection(*args, **kwargs), file_factory=factory)
    monkeypatch.setattr(vault, '_snapshot_selection', select)
    monkeypatch.setattr(custody, 'capture', forbidden)
    monkeypatch.setattr(vault, '_snapshots_captured', forbidden)
    with pytest.raises(vault.VaultError):
        vault._snapshots_for_preferences('owned-owner', ('site:example.test',), root=owned, root_identity=expected)
    assert calls == ['factory'] and list(owned.iterdir()) == []

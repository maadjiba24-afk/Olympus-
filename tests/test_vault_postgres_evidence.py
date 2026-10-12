"""M11 real database controls, only inside a newly owned Unix-socket cluster.

Ambient database credentials are never accepted. Every test creates a separate
database inside the verified disposable cluster; all data remains for restart
and evidence inspection. Ordinary suites skip this required separate leg.
"""
from contextlib import contextmanager
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import uuid

import pytest

from olympus import store, vault_records, vault_recovery, vault_storage as vs


@pytest.fixture
def owned_pg(tmp_path):
    name = os.environ.get('OLYMPUS_M11_POSTGRES_FIXTURE')
    if not name:
        pytest.skip('required separate M11 owned PostgreSQL fixture leg')
    assert sys.platform == 'linux'
    import psycopg
    from psycopg import sql
    root = Path(name)
    assert root.resolve(strict=True) == root and root.name.startswith('olympus-m11-pg-')
    assert not str(root).startswith('/mnt/') and root.stat().st_uid == os.getuid()
    assert root.stat().st_mode & 0o077 == 0
    assert json.loads((root/'fixture-identity.json').read_bytes()) == {
        'purpose': 'M11 isolated PostgreSQL fixture', 'root': str(root), 'uid': os.getuid(), 'port': 55483}
    assert not any(key.startswith('PG') for key in os.environ)
    assert Path(os.environ['HOME']).resolve() == root/'owned-home'
    dsn = psycopg.conninfo.make_conninfo(host=str(root/'socket'), port=55483,
        dbname='postgres', user='maadjiba', connect_timeout=5, sslmode='disable',
        passfile=str(root/'owned-home/absent.pgpass'))
    database = 'm11_case_' + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert Path(conn.execute('SHOW data_directory').fetchone()[0]).resolve() == root/'data'
        assert conn.execute('SHOW listen_addresses').fetchone()[0] == ''
        assert conn.execute('SHOW fsync').fetchone()[0] == 'on'
        assert conn.execute('SHOW synchronous_commit').fetchone()[0] == 'on'
        conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database)))
    dsn = psycopg.conninfo.make_conninfo(dsn, dbname=database)
    backend = store.PostgresStore(dsn)
    return root, backend, tmp_path


def test_real_postgres_commit_and_escaped_transaction_refusal(owned_pg):
    _, backend, root = owned_pg
    with vs.transaction(backend, root) as tx:
        tx.put('vault', 'key', b'owned-ciphertext')
        tx.write_new('vault_recovery', 'copy', b'owned-original')
        assert tx.keys('vault') == ['key']
        with vs.transaction(backend, root) as nested:
            assert nested is tx
    assert backend.get('vault', 'key') == b'owned-ciphertext'
    assert backend.get('vault_recovery', 'copy') == b'owned-original'
    with pytest.raises(vs.StorageError):
        tx.get('vault', 'key')


@pytest.mark.parametrize('fault', [RuntimeError, KeyboardInterrupt, SystemExit])
def test_real_postgres_rollback_spans_vault_and_recovery(owned_pg, fault):
    _, backend, root = owned_pg
    backend.put('vault', 'key', b'preserved-before')
    with pytest.raises(fault):
        with vs.transaction(backend, root) as tx:
            tx.put('vault', 'key', b'uncommitted-change')
            tx.write_new('vault_recovery', 'copy', b'uncommitted-copy')
            raise fault('owned interruption before commit')
    assert backend.get('vault', 'key') == b'preserved-before'
    assert backend.get('vault_recovery', 'copy') is None
    with pytest.raises(vs.StorageError):
        tx.keys('vault')


def test_real_postgres_recovery_copy_cannot_be_overwritten(owned_pg):
    _, backend, root = owned_pg
    import psycopg
    with vs.transaction(backend, root) as tx:
        tx.write_new('vault_recovery', 'immutable', b'preserved-copy')
    with pytest.raises(psycopg.errors.UniqueViolation):
        with vs.transaction(backend, root) as tx:
            tx.put('vault', 'uncommitted', b'owned')
            tx.write_new('vault_recovery', 'immutable', b'forbidden-change')
    assert backend.get('vault_recovery', 'immutable') == b'preserved-copy'
    assert backend.get('vault', 'uncommitted') is None


def _bounded_transfer_observer(backend, observed):
    real = backend._psycopg
    class Cursor:
        def __init__(self, cursor, sql):
            self.cursor, self.sql = cursor, sql
        def fetchone(self):
            row = self.cursor.fetchone()
            if self.sql.startswith('SELECT octet_length(v)'):
                observed.append(row)
            return row
        def fetchall(self):
            rows = self.cursor.fetchall()
            if self.sql.startswith('SELECT octet_length(k)'):
                observed.extend(rows)
            return rows
    class Connection:
        def __init__(self, conn):
            self.conn = conn
        def __enter__(self):
            self.conn.__enter__()
            return self
        def __exit__(self, *args):
            return self.conn.__exit__(*args)
        def transaction(self):
            return self.conn.transaction()
        def execute(self, sql, params):
            return Cursor(self.conn.execute(sql, params), sql)
    backend._psycopg = SimpleNamespace(connect=lambda *a, **k: Connection(real.connect(*a, **k)))


def test_real_postgres_oversized_key_is_not_transferred_to_client(owned_pg):
    _, backend, root = owned_pg
    key = 'x' * 129  # exceeds the API bound while fitting the real B-tree key
    backend.put('vault', key, b'owned')
    observed = []
    _bounded_transfer_observer(backend, observed)
    with vs.transaction(backend, root) as tx:
        with pytest.raises(vs.StorageError, match='transfer bound'):
            tx.keys('vault')
    assert observed == [(len(key), None)]


def test_real_postgres_oversized_blob_is_not_transferred_to_client(owned_pg):
    _, backend, root = owned_pg
    backend.put('vault', 'large', b'x' * (1024 * 1024))
    observed = []
    _bounded_transfer_observer(backend, observed)
    with vs.transaction(backend, root) as tx:
        with pytest.raises(vs.StorageError, match='read bound'):
            tx.get('vault', 'large', cap=32)
    assert observed == [(1024 * 1024, None)]


def _observe_failed_advisory_lock(backend, blocked):
    real = backend._psycopg
    class Cursor:
        def __init__(self, cursor, query):
            self.cursor, self.query = cursor, query
        def fetchone(self):
            row = self.cursor.fetchone()
            if self.query == 'SELECT pg_try_advisory_xact_lock(%s)' and row == (False,):
                blocked.set()
            return row
        def fetchall(self):
            return self.cursor.fetchall()
    class Connection:
        def __init__(self, conn):
            self.conn = conn
        def __enter__(self):
            self.conn.__enter__()
            return self
        def __exit__(self, *args):
            return self.conn.__exit__(*args)
        def transaction(self):
            return self.conn.transaction()
        def execute(self, query, params):
            return Cursor(self.conn.execute(query, params), query)
    backend._psycopg = SimpleNamespace(connect=lambda *a, **k: Connection(real.connect(*a, **k)))


def _worker(dsn, root, label, ready, blocked, go, result, timeout):
    try:
        import psycopg
        dsn = psycopg.conninfo.make_conninfo(dsn, application_name=label)
        backend = store.PostgresStore(dsn)
        _observe_failed_advisory_lock(backend, blocked)
        ready.set()
        if not go.wait(15):
            raise RuntimeError('owned process start gate timed out')
        with vs.transaction(backend, Path(root), timeout=timeout) as tx:
            previous = tx.get('vault', 'shared', cap=4096) or b''
            tx.put('vault', 'shared', previous + label.encode() + b'\n')
        result.put(('ok', label))
    except BaseException as exc:
        branch = ('acquisition_timeout' if isinstance(exc, vs.StorageError)
                  and str(exc) == 'vault transaction acquisition timed out' else 'unexpected')
        result.put(('error', type(exc).__name__, branch))


def test_real_postgres_competing_processes_serialize_across_distinct_roots(owned_pg):
    fixture, backend, root = owned_pg
    ctx = multiprocessing.get_context('spawn')
    ready, go, result = [ctx.Event(), ctx.Event()], ctx.Event(), ctx.Queue()
    blocked = [ctx.Event(), ctx.Event()]
    labels = ['m11-worker-' + uuid.uuid4().hex for _ in range(2)]
    children = [ctx.Process(target=_worker, args=(backend._dsn, str(root/str(i)), labels[i],
                 ready[i], blocked[i], go, result, 15)) for i in range(2)]
    try:
        # Backend-native lock: children have different filesystem roots and
        # independent process admission mutexes, but share this database.
        with backend.vault_transaction() as blocker:
            blocker.put('vault', 'shared', b'')
            for child in children:
                child.start()
            assert all(event.wait(15) for event in ready)
            go.set()
            # Each signal is emitted only from its real server response False,
            # proving both peers attempted and failed the advisory acquisition.
            assert all(event.wait(10) for event in blocked)
        reports = [result.get(timeout=25) for _ in children]
        for child in children:
            child.join(15)
        assert sorted(reports) == sorted(('ok', label) for label in labels)
        assert all(child.exitcode == 0 for child in children)
        assert sorted(backend.get('vault', 'shared').decode().splitlines()) == sorted(labels)
    finally:
        go.set()
        for child in children:
            if child.is_alive():
                child.terminate()
            if child.pid is not None:
                child.join(10)
        result.close()
        result.join_thread()


def test_real_postgres_contention_timeout_preserves_state(owned_pg):
    _, backend, root = owned_pg
    ctx = multiprocessing.get_context('spawn')
    ready, go, result = ctx.Event(), ctx.Event(), ctx.Queue()
    blocked = ctx.Event()
    child = ctx.Process(target=_worker, args=(backend._dsn, str(root/'other'),
                        'owned-timeout-worker', ready, blocked, go, result, .15))
    try:
        with backend.vault_transaction() as blocker:
            blocker.put('vault', 'shared', b'preserved')
            child.start()
            assert ready.wait(15)
            go.set()
            assert blocked.wait(10)
            assert result.get(timeout=15) == ('error', 'StorageError', 'acquisition_timeout')
            child.join(10)
            assert child.exitcode == 0
            assert blocker.get('vault', 'shared') == b'preserved'
    finally:
        go.set()
        if child.is_alive():
            child.terminate()
        if child.pid is not None:
            child.join(10)
        result.close()
        result.join_thread()


@pytest.mark.parametrize('phase', ['before_commit', 'post_commit_application_error'])
def test_real_postgres_encrypted_recovery_across_commit_interruption(owned_pg, phase):
    """Application-level reporting faults, not transport/COMMIT-ack injection."""
    _, backend, root = owned_pg
    import psycopg
    owner, key = 'owned-pg-owner', 'owned-exact-key'
    password, new_password = b'owned-old-password', b'owned-new-password'
    entries = {'token': {'credential': 'owned-synthetic-value'}}
    before = vault_records.encode(owner, entries, password)
    after = vault_records.encode(owner, entries, new_password)
    backend.put('vault', key, before)
    operation_id = uuid.uuid4().hex
    def verify(blob):
        data, metadata = vault_records.decode(owner, blob, new_password)
        assert data == entries and metadata.context_authenticated
        return True
    def prepare(tx):
        return vault_recovery.prepare(tx, new_password, operation_id=operation_id,
            kind='rotate', source_key=key, source_blob=before, target_key=key,
            previous_blob=before, target_blob=after, owner=owner,
            entries_changed=1, verify_target=verify)
    with pytest.raises(psycopg.OperationalError):
        with vs.transaction(backend, root) as tx:
            prepare(tx)
            vault_recovery.apply(tx, new_password, operation_id, owner=owner, verify_target=verify)
            if phase == 'before_commit':
                raise psycopg.OperationalError('owned failure before commit')
        raise psycopg.OperationalError('owned lost commit acknowledgment')
    assert backend.get('vault', key) == (before if phase == 'before_commit' else after)
    with vs.transaction(backend, root) as tx:
        if phase == 'before_commit':
            assert tx.keys('vault_recovery') == []
            prepare(tx)
        outcome = vault_recovery.apply(tx, new_password, operation_id, owner=owner, verify_target=verify)
        assert outcome['committed'] and outcome['already_committed'] == (phase == 'post_commit_application_error')
        assert verify(tx.get('vault', key))
        assert tx.get('vault_recovery', vault_recovery._key(operation_id, 'source')) == before


def test_real_postgres_owned_backend_termination_rolls_back_both_namespaces(owned_pg):
    _, backend, root = owned_pg
    import psycopg
    backend.put('vault', 'key', b'preserved-before')
    connect = psycopg.connect
    captured = []
    def owned_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        captured.append(connection.info.backend_pid)
        return connection
    backend._psycopg = SimpleNamespace(connect=owned_connect)
    with pytest.raises(psycopg.OperationalError):
        with vs.transaction(backend, root) as tx:
            tx.put('vault', 'key', b'owned-uncommitted-change')
            tx.write_new('vault_recovery', 'copy', b'owned-uncommitted-copy')
            assert len(captured) == 1
            with connect(backend._dsn, autocommit=True) as observer:
                assert observer.info.backend_pid != captured[0]
                assert observer.execute('SELECT pg_terminate_backend(%s, 5000)', (captured[0],)).fetchone()[0]
            tx.get('vault', 'key')
    assert backend.get('vault', 'key') == b'preserved-before'
    assert backend.get('vault_recovery', 'copy') is None


@pytest.fixture
def owned_snapshot_pg(owned_pg, monkeypatch):
    """Explicit local selection in the owned cluster's private HOME."""
    from olympus import config, vault
    import psycopg
    fixture, backend, root = owned_pg
    options = psycopg.conninfo.conninfo_to_dict(backend._dsn)
    options.pop('passfile')
    backend._dsn = psycopg.conninfo.make_conninfo(**options)
    monkeypatch.setattr(store, '_backend', backend)
    monkeypatch.setattr(config, 'MEMORY_DIR', root/'captured-memory')
    monkeypatch.delenv('OLYMPUS_SECRET_KEY_FILE', raising=False)
    monkeypatch.delenv('OLYMPUS_DATABASE_URL', raising=False)
    monkeypatch.setenv('OLYMPUS_SECRET_KEY', 'owned-pg-snapshot-custody')
    return fixture, backend, root


def _snapshot_connect_observer(monkeypatch, backend):
    """Keep observations secret-free; return real psycopg connections."""
    real = backend._psycopg.connect
    class Observations(list):
        attempts = 0
    observed = Observations()
    def connect(*args, **kwargs):
        observed.attempts += 1
        connection = real(*args, **kwargs)
        observed.append({'port_type': type(connection.info.port).__name__,
            'port': connection.info.port, 'hostaddr': connection.info.hostaddr,
            'used_password': bool(connection.pgconn.used_password),
            'used_gssapi': bool(connection.pgconn.used_gssapi)})
        return connection
    monkeypatch.setattr(backend, '_psycopg', SimpleNamespace(connect=connect))
    return observed


def test_real_postgres_facade_crud_is_owner_bound(owned_snapshot_pg):
    from olympus import vault
    _, backend, _ = owned_snapshot_pg
    vault.put('owned-pg-owner', 'one', {'credential':'owned-synthetic-one'})
    vault.put('owned-pg-owner', 'two', 'owned-synthetic-two')
    before = backend.get('vault', vault._key('owned-pg-owner'))
    assert vault.entries('owned-pg-owner') == {
        'one':{'credential':'owned-synthetic-one'}, 'two':'owned-synthetic-two'}
    assert backend.get('vault', vault._key('owned-pg-owner')) == before
    backend.put('vault', vault._key('different-pg-owner'), before)
    with pytest.raises(vault.VaultError):
        vault.entries('different-pg-owner')
    vault.delete_many('owned-pg-owner', ['one', 'two'])
    assert vault.entries('owned-pg-owner') == {}
    assert backend.get('vault', vault._key('different-pg-owner')) == before


def test_real_postgres_snapshot_settlement_keeps_captured_authority(owned_snapshot_pg, monkeypatch):
    from olympus import config, vault
    import psycopg
    fixture, backend, root = owned_snapshot_pg
    vault.put('owned-pg-owner', 'one', 'before')
    dsn = backend._dsn
    reader = store.PostgresStore(dsn)
    observed = _snapshot_connect_observer(monkeypatch, backend)
    selected = vault.snapshot('owned-pg-owner', 'one', for_update=True)
    assert selected.value == 'before'
    pending = vault.prepare_snapshot_update(selected, 'after')
    assert len(observed) == 1
    monkeypatch.setattr(config, 'MEMORY_DIR', root/'unselected-memory')
    monkeypatch.setattr(store, '_backend', store.FileStore())
    monkeypatch.setenv('OLYMPUS_SECRET_KEY', 'unselected-owned-key')
    monkeypatch.setenv('OLYMPUS_DATABASE_URL', 'unselected-owned-database')
    monkeypatch.setattr(backend, '_dsn', psycopg.conninfo.make_conninfo(
        dsn, host=str(fixture/'unselected-socket')))
    def unselected(*args, **kwargs):
        raise AssertionError('mutated backend connection must not be used')
    monkeypatch.setattr(backend, '_psycopg', SimpleNamespace(connect=unselected))
    assert vault.settle_snapshot_update(pending) == 'after'
    assert len(observed) == 2
    assert all(item == {'port_type':'int', 'port':55483, 'hostaddr':'',
                       'used_password':False, 'used_gssapi':False} for item in observed)
    assert not (root/'unselected-memory').exists()
    blob = reader.get('vault', vault._key('owned-pg-owner'))
    values, metadata = vault_records.decode('owned-pg-owner', blob, b'owned-pg-snapshot-custody')
    assert values == {'one':'after'} and metadata.context_authenticated
    assert blob == pending._ciphertext


def test_real_postgres_cold_snapshot_initializes_through_verified_connection(owned_snapshot_pg, monkeypatch):
    from olympus import vault
    import psycopg
    _, backend, _ = owned_snapshot_pg
    observed = _snapshot_connect_observer(monkeypatch, backend)
    monkeypatch.setattr(psycopg, 'connect', backend._psycopg.connect)
    monkeypatch.setenv('OLYMPUS_DATABASE_URL', backend._dsn)
    monkeypatch.setattr(store, '_backend', None)
    selected = vault.snapshot('owned-pg-owner', 'one', for_update=True)
    assert selected.value is None
    assert len(observed) == 2  # verified constructor-equivalent DDL, then transaction
    pending = vault.prepare_snapshot_update(selected, 'cold-owned-value')
    assert vault.settle_snapshot_update(pending) == 'cold-owned-value'
    assert len(observed) == 3
    assert backend.get('vault', vault._key('owned-pg-owner')) == pending._ciphertext


@pytest.mark.parametrize('selector', ['PGHOSTADDR', 'PGSERVICE', 'PGPASSFILE'])
def test_real_postgres_cold_snapshot_refuses_environment_before_connect(owned_snapshot_pg, monkeypatch, selector):
    from olympus import vault
    import psycopg
    _, backend, _ = owned_snapshot_pg
    observed = _snapshot_connect_observer(monkeypatch, backend)
    monkeypatch.setattr(psycopg, 'connect', backend._psycopg.connect)
    monkeypatch.setenv('OLYMPUS_DATABASE_URL', backend._dsn)
    monkeypatch.setattr(store, '_backend', None)
    monkeypatch.setenv(selector, 'owned-selector-must-not-be-consulted')
    with pytest.raises(vault.VaultError):
        vault.snapshot('owned-pg-owner', 'one', for_update=True)
    assert observed.attempts == 0 and observed == []


@pytest.mark.parametrize('selector', ['PGHOSTADDR', 'PGSERVICE', 'PGPASSFILE'])
def test_real_postgres_pending_snapshot_refuses_later_environment_before_connect(owned_snapshot_pg, monkeypatch, selector):
    from olympus import vault
    _, backend, _ = owned_snapshot_pg
    observed = _snapshot_connect_observer(monkeypatch, backend)
    pending = vault.prepare_snapshot_update(
        vault.snapshot('owned-pg-owner', 'one', for_update=True), 'owned-value')
    assert len(observed) == 1
    monkeypatch.setenv(selector, 'owned-selector-must-not-be-consulted')
    with pytest.raises(vault.VaultError):
        vault.settle_snapshot_update(pending)
    assert observed.attempts == 1 and len(observed) == 1 and pending._ciphertext is None
    monkeypatch.delenv(selector)
    assert vault.settle_snapshot_update(pending) == 'owned-value'
    assert observed.attempts == 2 and len(observed) == 2


def test_real_postgres_stale_snapshot_cannot_resurrect_deleted_credential(owned_snapshot_pg):
    from olympus import vault
    _, backend, _ = owned_snapshot_pg
    vault.put('owned-pg-owner', 'one', 'before')
    pending = vault.prepare_snapshot_update(
        vault.snapshot('owned-pg-owner', 'one', for_update=True), 'after')
    vault.delete('owned-pg-owner', 'one')
    deleted = backend.get('vault', vault._key('owned-pg-owner'))
    with pytest.raises(vault.VaultConflict):
        vault.settle_snapshot_update(pending)
    assert backend.get('vault', vault._key('owned-pg-owner')) == deleted
    assert vault.entries('owned-pg-owner') == {}


def test_real_postgres_facade_format_change_retains_recovery_copy(owned_snapshot_pg):
    from olympus import vault
    import base64
    from cryptography.fernet import Fernet
    _, backend, _ = owned_snapshot_pg
    before = Fernet(base64.urlsafe_b64encode(hashlib.sha256(
        b'owned-pg-snapshot-custody').digest())).encrypt(b'{"one":"owned-legacy"}')
    backend.put('vault', vault._key('owned-pg-owner'), before)
    assert vault.get('owned-pg-owner', 'one') == 'owned-legacy'
    with pytest.raises(vault.VaultMigrationRequired):
        vault.put('owned-pg-owner', 'two', 'refused')
    assert backend.get('vault', vault._key('owned-pg-owner')) == before
    operation_id = uuid.uuid4().hex
    assert vault.rotate('owned-pg-owner', operation_id=operation_id)['committed']
    assert vault.inspect_change(operation_id, owner='owned-pg-owner')['committed']
    assert vault.resume_change(operation_id, owner='owned-pg-owner')['already_committed']
    assert backend.get('vault_recovery', 'change-'+operation_id+'-source') == before
    assert vault.entries('owned-pg-owner') == {'one':'owned-legacy'}


def test_real_postgres_oauth_exchange_refresh_disconnect_are_persisted(owned_snapshot_pg, monkeypatch):
    from olympus import google_oauth as oauth, vault
    _, backend, root = owned_snapshot_pg
    monkeypatch.setenv('OLYMPUS_GOOGLE_CLIENT_ID', 'owned-pg-client')
    monkeypatch.setenv('OLYMPUS_GOOGLE_CLIENT_SECRET', 'owned-pg-client-secret')
    owner, provider_calls = 'owned-pg-oauth-owner', []

    def provider(request):
        provider_calls.append(request['grant_type'])
        return {'access_token': 'owned-pg-access-'+str(len(provider_calls)),
                'refresh_token': 'owned-pg-refresh-'+str(len(provider_calls)), 'expires_in': 3600}

    monkeypatch.setattr(oauth, '_post_token', provider)
    vault.put(owner, 'unrelated', 'owned-preserved-value')
    assert oauth.exchange_code(owner, 'owned-pg-code') == 'Google account connected.'
    first = backend.get('vault', vault._key(owner))
    entries, metadata = vault_records.decode(owner, first, b'owned-pg-snapshot-custody')
    assert metadata.context_authenticated
    assert entries['google']['access_token'] == 'owned-pg-access-1'
    assert entries['google']['refresh_token'] == 'owned-pg-refresh-1'
    assert entries['unrelated'] == 'owned-preserved-value'
    assert b'owned-pg-access-1' not in first
    assert oauth.access_token(owner) == 'owned-pg-access-1'
    assert provider_calls == ['authorization_code']
    vault.put(owner, 'google', {**entries['google'], 'expires_at': 1})
    assert oauth.access_token(owner) == 'owned-pg-access-2'
    second = backend.get('vault', vault._key(owner))
    entries, metadata = vault_records.decode(owner, second, b'owned-pg-snapshot-custody')
    assert metadata.context_authenticated and second != first
    assert entries['google']['access_token'] == 'owned-pg-access-2'
    assert entries['google']['refresh_token'] == 'owned-pg-refresh-2'
    assert provider_calls == ['authorization_code', 'refresh_token']
    assert vault.get(owner+'-different', 'google') is None
    assert oauth.disconnect(owner) == 'Google account disconnected.'
    assert oauth.connected(owner) is False
    assert vault.entries(owner) == {'unrelated': 'owned-preserved-value'}
    assert not (root/'captured-memory').exists()


@pytest.mark.parametrize('operation', ['exchange', 'refresh'])
@pytest.mark.parametrize('kind', [RuntimeError, KeyboardInterrupt, SystemExit])
def test_real_postgres_oauth_lost_ack_retries_original_committed_ciphertext(
        owned_snapshot_pg, monkeypatch, operation, kind):
    from olympus import config, google_oauth as oauth, vault
    _, backend, root = owned_snapshot_pg
    assert type(backend) is store.PostgresStore
    monkeypatch.setenv('OLYMPUS_GOOGLE_CLIENT_ID', 'owned-pg-client')
    monkeypatch.setenv('OLYMPUS_GOOGLE_CLIENT_SECRET', 'owned-pg-client-secret')
    owner = 'owned-pg-oauth-owner'
    vault.put(owner, 'unrelated', 'owned-preserved-value')
    if operation == 'refresh':
        vault.put(owner, 'google', {'access_token': 'owned-pg-old-access',
                  'refresh_token': 'owned-pg-old-refresh', 'expires_at': 1})
    reader = store.PostgresStore(backend._dsn)
    connections, provider_calls, preparations, updates = [], [], [], []
    real_connect = backend._psycopg.connect

    def connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(backend, '_psycopg', SimpleNamespace(connect=connect))
    original_prepare, original_settle = vault.prepare_snapshot_update, vault.settle_snapshot_update
    primary, once = kind('owned lost application acknowledgement after real PostgreSQL commit'), [True]

    def provider(request):
        provider_calls.append(request['grant_type'])
        return {'access_token': 'owned-pg-new-access', 'refresh_token': 'owned-pg-new-refresh',
                'expires_in': 3600}

    def prepare(*args, **kwargs):
        result = original_prepare(*args, **kwargs)
        preparations.append(result)
        return result

    def settle(pending):
        updates.append(pending)
        result = original_settle(pending)
        if once[0]:
            once[0] = False
            raise primary
        return result

    monkeypatch.setattr(oauth, '_post_token', provider)
    monkeypatch.setattr(vault, 'prepare_snapshot_update', prepare)
    monkeypatch.setattr(vault, 'settle_snapshot_update', settle)
    with pytest.raises(oauth.OAuthSettlementError if kind is RuntimeError else kind) as caught:
        if operation == 'exchange':
            oauth.exchange_code(owner, 'owned-code')
        else:
            oauth.access_token(owner)
    if kind is not RuntimeError:
        assert caught.value is primary
    handle = caught.value if kind is RuntimeError else primary._oauth_settlement
    assert len(preparations) == len(updates) == 1
    pending = preparations[0]
    assert updates[0] is pending and type(pending._snapshot._captured.backend) is store.PostgresStore
    first = reader.get('vault', vault._key(owner))
    assert first == pending._ciphertext
    entries, metadata = vault_records.decode(owner, first, b'owned-pg-snapshot-custody')
    assert metadata.context_authenticated and entries['unrelated'] == 'owned-preserved-value'
    assert entries['google']['access_token'] == 'owned-pg-new-access'
    assert entries['google']['refresh_token'] == 'owned-pg-new-refresh'
    assert len(connections) == 2 and all(connection.closed for connection in connections)
    monkeypatch.setattr(config, 'MEMORY_DIR', root/'unselected-memory')
    monkeypatch.setattr(store, '_backend', store.FileStore())
    monkeypatch.setenv('OLYMPUS_SECRET_KEY', 'unselected-owned-vault-key')
    monkeypatch.setenv('OLYMPUS_DATABASE_URL', 'unselected-owned-database')

    def forbidden(*args, **kwargs):
        raise AssertionError('must retain original PostgreSQL connection selection')

    monkeypatch.setattr(backend, '_psycopg', SimpleNamespace(connect=forbidden))
    assert handle.settle() == ('Google account connected.' if operation == 'exchange' else 'owned-pg-new-access')
    assert len(preparations) == 1 and len(updates) == 2 and updates[1] is pending
    assert reader.get('vault', vault._key(owner)) == first
    assert len(provider_calls) == 1
    assert len(connections) == 3 and all(connection.closed for connection in connections)
    assert not (root/'unselected-memory').exists()
    assert handle.status()['local_publication_confirmed']
    assert not handle.status()['storage_cleanup_unconfirmed']




@pytest.fixture
def owned_pg_login(owned_snapshot_pg, monkeypatch):
    from collections import OrderedDict
    from olympus import approvals, config, securecapture, store
    cluster, backend, root = owned_snapshot_pg
    monkeypatch.setattr(config, 'MEMORY_DIR', root)
    monkeypatch.setattr(store, '_backend', backend)
    monkeypatch.delenv('OLYMPUS_DATABASE_URL', raising=False)
    monkeypatch.delenv('OLYMPUS_SECRET_KEY_FILE', raising=False)
    monkeypatch.setenv('OLYMPUS_SECRET_KEY', 'owned-real-pg-login-key')
    monkeypatch.setenv('OLYMPUS_ENV', 'test')
    monkeypatch.setattr(securecapture, '_WORKFLOWS', OrderedDict())
    monkeypatch.setattr(securecapture, '_ACTIVE', {})
    monkeypatch.setattr(approvals, 'pending', lambda owner: [])
    return backend, root


def _pg_login_readback(backend):
    from olympus import vault, vault_records
    raw = backend.get('vault', vault._key('owned-real-pg-login'))
    assert type(raw) is bytes and b'owned-real-pg-password' not in raw
    entries, metadata = vault_records.decode('owned-real-pg-login', raw, b'owned-real-pg-login-key')
    assert metadata.context_authenticated
    assert entries == {'site:example.test': {'username': 'owned-user', 'password': 'owned-real-pg-password'}}
    return raw


def test_real_postgres_private_login_keeps_selected_database_root_and_custody(owned_pg_login, monkeypatch):
    from olympus import config, interactive, prefs, securecapture, store
    backend, root = owned_pg_login
    owner, site = 'owned-real-pg-login', 'example.test'
    other = root / 'unselected-root'
    other.mkdir()
    securecapture.request(owner, site)
    calls, output = [], []
    class PrivateIO:
        def line(self, message):
            calls.append('username')
            assert prefs.get(owner, 'pending_secure_login')['state'] == 'prompting'
            assert prefs.get(owner, 'operator') is None
            return 'owned-user'
        def secret(self, message):
            calls.append('password')
            monkeypatch.setattr(config, 'MEMORY_DIR', other)
            monkeypatch.setattr(store, '_backend', store.FileStore())
            monkeypatch.setenv('OLYMPUS_SECRET_KEY', 'owned-unselected-key')
            return 'owned-real-pg-password'
        def out(self, message):
            assert 'owned-real-pg-password' not in message
            output.append(message)
    assert interactive.after_turn(owner, PrivateIO()) == ['saved-login:' + site]
    assert calls == ['username', 'password'] and 'authorization confirmed' in output[-1]
    assert securecapture.active_status(owner)['state'] == 'complete'
    _pg_login_readback(backend)
    assert list(other.iterdir()) == [] and not (root / 'store/vault').exists()
    monkeypatch.setattr(config, 'MEMORY_DIR', root)
    assert prefs.get(owner, 'pending_secure_login') is None
    assert prefs.get(owner, 'operator')['sites'][site]['login'] == 'remember'


@pytest.mark.parametrize('fault', [RuntimeError, KeyboardInterrupt, SystemExit])
def test_real_postgres_private_login_lost_ack_settles_identical_ciphertext_once(owned_pg_login, monkeypatch, fault):
    from olympus import config, prefs, securecapture, store, vault, vault_custody
    backend, root = owned_pg_login
    owner, site = 'owned-real-pg-login', 'example.test'
    securecapture.request(owner, site)
    facts = securecapture.begin(owner)
    assert facts['state'] == 'ready'
    assert securecapture.start_prompt(owner, facts['workflow_id'])['prompt_dispatched']
    original, calls = vault.settle_snapshot_update, []
    primary = fault('owned-real-pg-acknowledgment')
    def settle(pending):
        calls.append(pending)
        result = original(pending)
        if len(calls) == 1:
            raise primary
        return result
    monkeypatch.setattr(vault, 'settle_snapshot_update', settle)
    if fault is RuntimeError:
        result = securecapture.fulfill(owner, site, 'owned-user', 'owned-real-pg-password', workflow_id=facts['workflow_id'])
    else:
        with pytest.raises(fault) as caught:
            securecapture.fulfill(owner, site, 'owned-user', 'owned-real-pg-password', workflow_id=facts['workflow_id'])
        assert caught.value is primary
        result = securecapture.active_status(owner)
    assert result['vault'] == 'unconfirmed' and result['authorization'] == 'not_attempted'
    assert prefs.get(owner, 'operator') is None
    before = _pg_login_readback(backend)
    other = root / 'unselected-root'
    other.mkdir()
    monkeypatch.setattr(config, 'MEMORY_DIR', other)
    monkeypatch.setattr(store, '_backend', store.FileStore())
    monkeypatch.setenv('OLYMPUS_SECRET_KEY', 'owned-unselected-key')
    def forbidden(*args, **kwargs):
        pytest.fail('local PostgreSQL settlement selected new credential authority')
    monkeypatch.setattr(vault, '_snapshot_selection', forbidden)
    monkeypatch.setattr(vault_custody, 'capture', forbidden)
    result = securecapture.settle(owner, facts['workflow_id'])
    assert result['state'] == 'complete' and result['vault'] == result['authorization'] == 'confirmed'
    assert len(calls) == 2 and calls[0] is calls[1]
    assert _pg_login_readback(backend) == before and list(other.iterdir()) == []
    assert not (root / 'store/vault').exists()
    monkeypatch.setattr(config, 'MEMORY_DIR', root)
    assert prefs.get(owner, 'operator')['sites'][site]['login'] == 'remember'


@pytest.mark.parametrize('interrupt', [False, True])
def test_real_postgres_private_input_cancel_leaves_no_credentials_or_authorization(owned_pg_login, interrupt):
    from olympus import interactive, prefs, securecapture, vault
    backend, root = owned_pg_login
    owner = 'owned-real-pg-login'
    securecapture.request(owner, 'example.test')
    primary = KeyboardInterrupt('owned-real-pg-input-cancel')
    calls = []
    class PrivateIO:
        def line(self, message):
            calls.append('username')
            if interrupt:
                raise primary
            return ''
        def secret(self, message):
            pytest.fail('canceled username attempted password input')
        def out(self, message):
            assert 'owned-real-pg-password' not in message
    if interrupt:
        with pytest.raises(KeyboardInterrupt) as caught:
            interactive.after_turn(owner, PrivateIO())
        assert caught.value is primary
    else:
        assert interactive.after_turn(owner, PrivateIO()) == []
    assert calls == ['username'] and securecapture.active_status(owner)['state'] == 'canceled'
    assert backend.get('vault', vault._key(owner)) is None
    assert prefs.get(owner, 'operator') is None and prefs.get(owner, 'pending_secure_login') is None
    assert not (root / 'store/vault').exists()

"""Owned native vault transaction tests; no operator state or PostgreSQL DSN."""
import multiprocessing
import os
from pathlib import Path
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from olympus import config, store, vault_storage as vs, vault_native as vn


def _writer(root, ready, go, result, key):
    try:
        backend = store.FileStore()
        ready.set()
        if not go.wait(15):
            raise RuntimeError('owned start gate timed out')
        with vs.transaction(backend, Path(root), timeout=15) as tx:
            prior = tx.get('vault', 'shared', 4096) or b''
            tx.put('vault', 'shared', prior + key.encode() + b'\n')
        result.put(('ok', key))
    except BaseException as exc:
        result.put(('error', type(exc).__name__))


def _holder(root, ready):
    with vs.transaction(store.FileStore(), Path(root), timeout=15):
        ready.set()
        threading.Event().wait(60)


def test_owned_transaction_round_trip_and_durable_delete(tmp_path):
    backend = store.FileStore()
    with vs.transaction(backend, tmp_path) as tx:
        assert tx.get('vault', 'key') is None
        tx.put('vault', 'key', b'owned-ciphertext')
        assert tx.get('vault', 'key') == b'owned-ciphertext'
        assert tx.keys('vault') == ['key']
        tx.delete('vault', 'key')
        assert tx.get('vault', 'key') is None
        tx.delete('vault', 'key')


def test_recovery_creation_never_overwrites(tmp_path):
    with vs.transaction(store.FileStore(), tmp_path) as tx:
        tx.write_new('vault_recovery', 'operation-source', b'preserved')
        with pytest.raises(OSError):
            tx.write_new('vault_recovery', 'operation-source', b'replacement')
        assert tx.get('vault_recovery', 'operation-source') == b'preserved'


def test_root_capture_survives_configuration_change(tmp_path, monkeypatch):
    root, other = tmp_path/'first', tmp_path/'second'
    with vs.transaction(store.FileStore(), root) as tx:
        monkeypatch.setattr(config, 'MEMORY_DIR', other)
        tx.put('vault', 'key', b'captured')
    assert (root/'store/vault/key').read_bytes() == b'captured'
    assert not other.exists()


def test_nested_same_authority_reuses_handle_and_cross_root_refuses(tmp_path):
    backend = store.FileStore()
    with vs.transaction(backend, tmp_path/'first') as outer:
        with vs.transaction(backend, tmp_path/'first') as inner:
            assert inner is outer
        with pytest.raises(vs.StorageError, match='changed its authority'):
            with vs.transaction(backend, tmp_path/'other'):
                pytest.fail('cross-root nested transaction was accepted')


def test_namespace_and_read_size_bounds(tmp_path):
    with vs.transaction(store.FileStore(), tmp_path) as tx:
        tx.put('vault', 'key', b'1234')
        with pytest.raises(Exception):
            tx.get('vault', 'key', 3)
        assert tx.get('vault', 'key', 4) == b'1234'
        with pytest.raises(vs.StorageError):
            tx.get('unrelated-namespace', 'key')
        with pytest.raises(vs.StorageError):
            tx.get('vault', 'key', True)


def test_enumeration_bound_and_no_unrelated_namespace_access(tmp_path):
    with vs.transaction(store.FileStore(), tmp_path) as tx:
        tx.put('vault', 'a', b'a')
        tx.put('vault', 'b', b'b')
        with pytest.raises(vs.StorageError, match='enumeration bound'):
            tx.keys('vault', 1)
        assert tx.keys('vault', 2) == ['a', 'b']


def test_noncontention_os_error_is_not_retried(tmp_path, monkeypatch):
    calls = []
    def fail(fd):
        calls.append(fd)
        raise PermissionError('owned refusal')
    monkeypatch.setattr(vs, '_try_lock', fail)
    with pytest.raises(vs.StorageError):
        with vs.transaction(store.FileStore(), tmp_path):
            pytest.fail('failed lock entered transaction')
    assert len(calls) == 1


def test_baseexception_releases_authority(tmp_path):
    backend = store.FileStore()
    with pytest.raises(KeyboardInterrupt):
        with vs.transaction(backend, tmp_path):
            raise KeyboardInterrupt()
    with vs.transaction(backend, tmp_path, timeout=0) as tx:
        tx.put('vault', 'after', b'usable')


@pytest.mark.parametrize('timeout', [-1, float('nan'), float('inf'), True, None])
def test_invalid_timeout_refuses_without_creating_state(tmp_path, timeout):
    root = tmp_path/'untouched'
    with pytest.raises(vs.StorageError):
        with vs.transaction(store.FileStore(), root, timeout=timeout):
            pytest.fail('invalid timeout accepted')
    assert not root.exists()


def test_two_spawned_writers_preserve_both_updates(tmp_path):
    ctx = multiprocessing.get_context('spawn')
    ready = [ctx.Event(), ctx.Event()]
    go, result = ctx.Event(), ctx.Queue()
    children = [ctx.Process(target=_writer, args=(str(tmp_path), ready[i], go, result, str(i)))
                for i in range(2)]
    try:
        for p in children:
            p.start()
        assert all(e.wait(15) for e in ready)
        go.set()
        reports = [result.get(timeout=20) for _ in children]
        for p in children:
            p.join(15)
        assert all(p.exitcode == 0 for p in children)
        assert sorted(reports) == [('ok', '0'), ('ok', '1')]
        with vs.transaction(store.FileStore(), tmp_path) as tx:
            assert sorted(tx.get('vault', 'shared').splitlines()) == [b'0', b'1']
    finally:
        go.set()
        for p in children:
            if p.is_alive():
                p.terminate()
            if p.pid is not None:
                p.join(10)
        result.close()
        result.join_thread()


def test_live_holder_times_out_and_process_death_releases(tmp_path):
    ctx = multiprocessing.get_context('spawn')
    ready = ctx.Event()
    child = ctx.Process(target=_holder, args=(str(tmp_path), ready))
    try:
        child.start()
        assert ready.wait(15)
        with pytest.raises(vs.StorageError, match='timed out'):
            with vs.transaction(store.FileStore(), tmp_path, timeout=0.1):
                pytest.fail('peer held lock was acquired')
        child.terminate()
        child.join(10)
        assert not child.is_alive()
        with vs.transaction(store.FileStore(), tmp_path, timeout=5) as tx:
            tx.put('vault', 'after-death', b'usable')
    finally:
        if child.is_alive():
            child.terminate()
        if child.pid is not None:
            child.join(10)


@pytest.mark.skipif(os.name != 'posix', reason='POSIX link creation control')
def test_symlink_storage_target_is_refused_and_preserved(tmp_path):
    outside = tmp_path/'outside'
    outside.write_bytes(b'preserve')
    root = tmp_path/'root'
    directory = root/'store/vault'
    directory.mkdir(parents=True)
    (directory/'key').symlink_to(outside)
    with pytest.raises(vs.StorageError):
        with vs.transaction(store.FileStore(), root) as tx:
            tx.put('vault', 'key', b'forbidden')
    assert outside.read_bytes() == b'preserve'
    assert (directory/'key').is_symlink()


@pytest.mark.parametrize('key', ['', '.', '..', 'a/b', 'a\\b', 'a:b', 'a b',
                                'x\0y', '\u00e9', 'x'*129, 1, None])
def test_noncanonical_addresses_refuse_before_namespace_io(tmp_path, key, monkeypatch):
    with vs.transaction(store.FileStore(), tmp_path) as tx:
        monkeypatch.setattr(tx, 'space', lambda *a: pytest.fail('invalid address reached I/O'))
        for method, extra in ((tx.get, ()), (tx.put, (b'owned',)),
                              (tx.write_new, (b'owned',)), (tx.delete, ())):
            with pytest.raises(vs.StorageError):
                method('vault', key, *extra)
    assert not (tmp_path/'store/vault').exists()


def test_exact_owner_key_length_and_mixed_case_are_preserved(tmp_path):
    key = 'MixedOwner-' + 'a'*64
    with vs.transaction(store.FileStore(), tmp_path) as tx:
        tx.put('vault', key, b'owned')
        assert tx.get('vault', key) == b'owned' and tx.keys('vault') == [key]
        tx.put('vault', 'x'*128, b'boundary')
        assert tx.get('vault', 'x'*128) == b'boundary'


def test_escaped_transaction_and_directory_cannot_create_cwd_namespace(tmp_path, monkeypatch):
    root, outside = tmp_path/'root', tmp_path/'outside'
    outside.mkdir()
    with vs.transaction(store.FileStore(), root) as tx:
        directory = tx.space('vault')
        tx.put('vault', 'key', b'preserved')
    monkeypatch.chdir(outside)
    for operation in (
        lambda: tx.space('vault_recovery'), lambda: tx.get('vault', 'key'),
        lambda: tx.put('vault', 'key', b'changed'), lambda: tx.delete('vault', 'key'),
        lambda: tx.keys('vault'), lambda: directory.child('escaped', create=True),
        lambda: directory.read('key', 100),
    ):
        with pytest.raises(vs.StorageError):
            operation()
    assert list(outside.iterdir()) == []
    assert (root/'store/vault/key').read_bytes() == b'preserved'
    assert not (root/'store/vault_recovery').exists()


def test_escaped_enumeration_and_entry_refuse_after_directory_close(tmp_path):
    with vs.transaction(store.FileStore(), tmp_path) as tx:
        tx.put('vault', 'key', b'preserved')
        directory = tx.space('vault')
        iterator = directory.entries()
        entry = next(iterator)
        assert entry.name == 'key' and directory._iterators
    assert directory._iterators == []
    with pytest.raises(vs.StorageError):
        entry.stat()
    with pytest.raises(vs.StorageError):
        next(iterator)
    assert (tmp_path/'store/vault/key').read_bytes() == b'preserved'


def test_foreign_thread_cannot_use_or_close_current_transaction(tmp_path):
    with vs.transaction(store.FileStore(), tmp_path) as tx:
        tx.put('vault', 'key', b'preserved')
        directory = tx.space('vault')
        failures = []
        def other():
            for operation in (lambda: tx.get('vault', 'key'),
                              lambda: tx.put('vault', 'key', b'changed'),
                              lambda: tx.delete('vault', 'key'), tx.close, directory.close):
                try:
                    operation()
                except vs.StorageError:
                    failures.append('refused')
        worker = threading.Thread(target=other)
        worker.start()
        worker.join(10)
        assert not worker.is_alive() and failures == ['refused']*5
        assert tx.get('vault', 'key') == b'preserved'


def _inherited_worker(backend, root, tx, sender):
    results = []
    for action in ('read', 'close', 'nested'):
        try:
            if action == 'read':
                tx.get('vault', 'key')
            elif action == 'close':
                tx.close()
            else:
                with vs.transaction(backend, root, timeout=0.1):
                    pass
        except vs.StorageError:
            results.append(action)
    sender.send(results)
    sender.close()


@pytest.mark.skipif(os.name != 'posix', reason='requires an actual POSIX fork of active authority')
def test_forked_active_authority_refuses_read_close_and_nested_reuse(tmp_path):
    ctx = multiprocessing.get_context('fork')
    receiver, sender = ctx.Pipe(duplex=False)
    backend = store.FileStore()
    with vs.transaction(backend, tmp_path) as tx:
        tx.put('vault', 'key', b'preserved')
        child = ctx.Process(target=_inherited_worker, args=(backend, tmp_path, tx, sender))
        try:
            child.start()
            sender.close()
            assert receiver.poll(10)
            assert receiver.recv() == ['read', 'close', 'nested']
            child.join(10)
            assert child.exitcode == 0
            assert tx.get('vault', 'key') == b'preserved'
        finally:
            if child.is_alive():
                child.terminate()
            if child.pid is not None:
                child.join(10)
            receiver.close()


@pytest.mark.parametrize('fault', [OSError, KeyboardInterrupt, SystemExit])
def test_initial_file_information_failure_closes_descriptor_and_gate(tmp_path, monkeypatch, fault):
    descriptors = []
    def broken(fd):
        descriptors.append(fd)
        raise fault('owned information fault')
    with monkeypatch.context() as patch:
        patch.setattr(vs, '_file_information', broken)
        with pytest.raises(vs.StorageError if fault is OSError else fault):
            with vs.transaction(store.FileStore(), tmp_path):
                pytest.fail('failed acquisition yielded')
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    assert getattr(vs._LOCAL, 'current', None) is None
    with vs.transaction(store.FileStore(), tmp_path, timeout=0) as tx:
        tx.put('vault', 'after', b'usable')


@pytest.mark.parametrize('fault', [OSError, KeyboardInterrupt, SystemExit])
def test_directory_close_drains_every_owned_handle_after_interruption(tmp_path, monkeypatch, fault):
    directory = vn.Directory(tmp_path)
    if directory.win is not None:
        from olympus import vault_windows as native
        owner, attribute = native, 'close'
        handles = list(directory.win.handles[directory.win.owned_start:])
        original = native.close
    else:
        owner, attribute = os, 'close'
        handles = list(directory._parents[directory._owned_start:])
        original = os.close
    closed = []
    def close(handle):
        original(handle)
        closed.append(handle)
        if len(closed) == 1:
            raise fault('owned close interruption')
    with monkeypatch.context() as patch:
        patch.setattr(owner, attribute, close)
        with pytest.raises(fault):
            directory.close()
    assert closed == list(reversed(handles)) and directory._closed
    with monkeypatch.context() as patch:
        patch.setattr(owner, attribute, lambda *a: pytest.fail('numeric handle closed twice'))
        with pytest.raises(vs.StorageError) as repeated:
            directory.close()
    assert vs.native_cleanup.unknown(repeated.value)
    with pytest.raises(vs.StorageError):
        directory.child('escaped', create=True)


@pytest.mark.parametrize('fault', [OSError, KeyboardInterrupt, SystemExit])
def test_directory_initial_validation_failure_drains_acquired_ancestors(tmp_path, monkeypatch, fault):
    handles = []
    def fail(directory):
        handles.extend(directory.win.handles if directory.win is not None else directory._parents)
        raise fault('owned initial validation fault')
    with monkeypatch.context() as patch:
        patch.setattr(vn.Directory, 'check', fail)
        with pytest.raises(fault):
            vn.Directory(tmp_path)
    assert handles
    if os.name == 'nt':
        from olympus import vault_windows as native
        for handle in handles:
            with pytest.raises(OSError):
                native.information(handle, True)
    else:
        for fd in handles:
            with pytest.raises(OSError):
                os.fstat(fd)


@pytest.mark.parametrize('fault', [OSError, KeyboardInterrupt, SystemExit])
def test_unlock_interruption_still_closes_and_releases_kernel_lock(tmp_path, monkeypatch, fault):
    original = vs._unlock
    observed = []
    def interrupt(fd):
        original(fd)
        observed.append(fd)
        raise fault('owned unlock interruption')
    with monkeypatch.context() as patch:
        patch.setattr(vs, '_unlock', interrupt)
        with pytest.raises(vs.StorageError if fault is OSError else fault):
            with vs.transaction(store.FileStore(), tmp_path) as tx:
                tx.put('vault', 'key', b'preserved')
    assert len(observed) == 1 and not tx._lease.active and tx.fd is None
    with pytest.raises(OSError):
        os.fstat(observed[0])
    with vs.transaction(store.FileStore(), tmp_path, timeout=0) as fresh:
        assert fresh.get('vault', 'key') == b'preserved'


def test_fragmented_ciphertext_read_preserves_exact_size(tmp_path, monkeypatch):
    with vs.transaction(store.FileStore(), tmp_path) as tx:
        tx.put('vault', 'key', b'owned-fragmented-ciphertext')
        original = os.read
        sizes = []
        def fragmented(fd, cap):
            sizes.append(cap)
            return original(fd, min(cap, 3))
        monkeypatch.setattr(os, 'read', fragmented)
        assert tx.get('vault', 'key') == b'owned-fragmented-ciphertext'
        assert len(sizes) > 2 and max(sizes) == len(b'owned-fragmented-ciphertext') + 1


@pytest.mark.skipif(os.name != 'posix', reason='requires POSIX replacement of an open lock file')
def test_replaced_lock_inode_refuses_further_vault_io(tmp_path):
    with vs.transaction(store.FileStore(), tmp_path) as tx:
        tx.put('vault', 'key', b'preserved')
        lock = tmp_path/'store/vault.transaction.lock'
        retained = tmp_path/'store/retained-lock'
        lock.rename(retained)
        lock.write_bytes(b'owned-replacement')
        with pytest.raises(vs.StorageError, match='lock path identity'):
            tx.put('vault', 'key', b'changed')
        assert retained.read_bytes() == b''
        assert (tmp_path/'store/vault/key').read_bytes() == b'preserved'


@pytest.mark.skipif(os.name != 'posix', reason='requires POSIX rename of held ancestor directories')
def test_replaced_ancestor_refuses_before_writing_old_or_new_tree(tmp_path):
    root = tmp_path/'root'
    with vs.transaction(store.FileStore(), root) as tx:
        tx.put('vault', 'key', b'preserved')
        (root/'store').rename(root/'retained-store')
        (root/'store').mkdir()
        with pytest.raises(vs.StorageError, match='directory identity'):
            tx.put('vault', 'key', b'changed')
        assert (root/'retained-store/vault/key').read_bytes() == b'preserved'
        assert list((root/'store').iterdir()) == []


@pytest.mark.skipif(os.name != 'nt', reason='requires native Windows case-insensitive file lookup')
def test_native_windows_case_alias_get_put_delete_refuse_and_preserve(tmp_path):
    with vs.transaction(store.FileStore(), tmp_path) as tx:
        tx.put('vault', 'MixedCaseKey', b'preserved')
        before = sorted((p.name, p.read_bytes()) for p in (tmp_path/'store/vault').iterdir())
        for operation in (lambda: tx.get('vault', 'mixedcasekey'),
                          lambda: tx.put('vault', 'MIXEDCASEKEY', b'changed'),
                          lambda: tx.delete('vault', 'mixedcasekey')):
            with pytest.raises(vs.StorageError, match='spelling'):
                operation()
        assert sorted((p.name, p.read_bytes()) for p in (tmp_path/'store/vault').iterdir()) == before
        assert tx.get('vault', 'MixedCaseKey') == b'preserved'


@pytest.mark.skipif(os.name != 'nt', reason='requires native Windows deletion and ancestor checks')
def test_native_delete_checks_ancestor_before_open_or_disposition(tmp_path, monkeypatch):
    from olympus import vault_windows as native
    with vs.transaction(store.FileStore(), tmp_path) as tx:
        tx.put('vault', 'key', b'preserved')
        directory = tx.space('vault')
        opened = []
        original = native.open_relative
        def observe(parent, name, **kwargs):
            opened.append((name, kwargs.get('delete', False)))
            return original(parent, name, **kwargs)
        def unsafe():
            raise OSError('owned changed ancestor')
        with monkeypatch.context() as patch:
            patch.setattr(native, 'open_relative', observe)
            patch.setattr(directory, 'check', unsafe)
            with pytest.raises(OSError):
                tx.delete('vault', 'key')
        assert opened == []
        assert tx.get('vault', 'key') == b'preserved'


@pytest.mark.skipif(os.name != 'nt', reason='requires fresh held Windows file information')
def test_native_read_rechecks_attributes_after_read_instead_of_crt_cache(tmp_path, monkeypatch):
    with vs.transaction(store.FileStore(), tmp_path) as tx:
        tx.put('vault', 'key', b'preserved')
        directory = tx.space('vault')
        fd = directory._open('key', os.O_RDONLY)
        try:
            identity = vs._file_information(fd).st_ino
        finally:
            os.close(fd)
        original = vs._file_information
        seen = []
        def changed(fd):
            info = original(fd)
            if info.st_ino == identity:
                seen.append(fd)
                if len(seen) == 3:  # _open, before read, after read
                    return SimpleNamespace(**{**vars(info), 'st_file_attributes': 0x400})
            return info
        with monkeypatch.context() as patch:
            patch.setattr(vs, '_file_information', changed)
            with pytest.raises(vs.StorageError, match='ordinary'):
                tx.get('vault', 'key')
        assert len(seen) == 3 and len(set(seen)) == 1
        assert tx.get('vault', 'key') == b'preserved'


class _OwnedPG:
    """Protocol recorder only: not evidence of a live PostgreSQL server."""
    def __init__(self):
        self.rows, self.queries, self.exits = {}, [], []
        self.key_rows = None
        self.blob_row = None
    def __enter__(self):
        return self
    def __exit__(self, kind, value, trace):
        self.exits.append(kind)
    @contextmanager
    def transaction(self):
        before = dict(self.rows)
        try:
            yield self
        except BaseException:
            self.rows = before
            raise
    def execute(self, sql, params):
        self.queries.append((sql, params))
        result = None
        all_rows = []
        if 'pg_try_advisory_xact_lock' in sql:
            result = (True,)
        elif sql.startswith('SELECT octet_length(v)'):
            cap, namespace, key = params
            raw = self.rows.get((namespace, key))
            result = self.blob_row if self.blob_row is not None else (
                None if raw is None else (len(raw), raw if len(raw) <= cap else None))
        elif sql.startswith('SELECT octet_length(k)'):
            cap, namespace, count = params
            all_rows = self.key_rows if self.key_rows is not None else [
                (len(key.encode()), key if len(key.encode()) <= cap else None)
                for ns, key in sorted(self.rows) if ns == namespace][:count]
        elif sql.startswith('INSERT'):
            namespace, key, value = params
            if 'ON CONFLICT' not in sql and (namespace, key) in self.rows:
                raise ValueError('owned duplicate recovery')
            self.rows[namespace, key] = value
        elif sql.startswith('DELETE'):
            self.rows.pop(tuple(params), None)
        return SimpleNamespace(fetchone=lambda: result, fetchall=lambda: all_rows)


def _pg_backend():
    conn = _OwnedPG()
    backend = object.__new__(store.PostgresStore)
    backend._dsn = 'owned-protocol-fixture-no-network'
    backend._psycopg = SimpleNamespace(connect=lambda *a, **kw: conn)
    return backend, conn


def test_postgres_protocol_bounds_transfer_and_retains_one_connection(tmp_path):
    backend, conn = _pg_backend()
    with vs.transaction(backend, tmp_path) as tx:
        tx.put('vault', 'MixedKey', b'owned')
        assert tx.get('vault', 'MixedKey', 5) == b'owned'
        assert tx.keys('vault', 1) == ['MixedKey']
        query, params = conn.queries[-1]
        assert query.startswith('SELECT octet_length(k), CASE WHEN octet_length(k)<=%s')
        assert 'THEN k ELSE NULL END' in query and 'LIMIT %s' in query
        assert params == (vs.MAX_KEY_BYTES, 'vault', 2)
        with vs.transaction(backend, tmp_path) as nested:
            assert nested is tx
    with pytest.raises(vs.StorageError):
        tx.get('vault', 'MixedKey')
    assert sum('pg_try_advisory_xact_lock' in q for q, _ in conn.queries) == 1


@pytest.mark.parametrize('rows', [[(129, None)], [(3, 'a/b')], [(2, 'a')],
                                  [(1, None)], [(True, 'a')], [(1, 'a'), (1, 'a')]])
def test_postgres_oversized_noncanonical_and_inconsistent_keys_refuse(tmp_path, rows):
    backend, conn = _pg_backend()
    conn.key_rows = rows
    with vs.transaction(backend, tmp_path) as tx:
        with pytest.raises(vs.StorageError):
            tx.keys('vault')
    assert 'THEN k ELSE NULL END' in conn.queries[-1][0]


@pytest.mark.parametrize('row', [(129, None), (2, b'x'), (True, b'x'), (-1, b''), (1, 'x')])
def test_postgres_blob_metadata_and_length_are_validated(tmp_path, row):
    backend, conn = _pg_backend()
    conn.blob_row = row
    with vs.transaction(backend, tmp_path) as tx:
        with pytest.raises(vs.StorageError):
            tx.get('vault', 'key', 128)


def test_postgres_foreign_thread_and_invalid_key_do_not_issue_queries(tmp_path):
    backend, conn = _pg_backend()
    with vs.transaction(backend, tmp_path) as tx:
        count = len(conn.queries)
        failures = []
        def foreign():
            try:
                tx.put('vault', 'key', b'owned')
            except vs.StorageError:
                failures.append('refused')
        child = threading.Thread(target=foreign)
        child.start()
        child.join(10)
        assert failures == ['refused'] and not child.is_alive()
        with pytest.raises(vs.StorageError):
            tx.put('vault', 'a/b', b'owned')
        assert len(conn.queries) == count


@pytest.mark.parametrize('fault', [RuntimeError, KeyboardInterrupt, SystemExit])
def test_postgres_body_failure_invalidates_escaped_authority_and_rolls_back(tmp_path, fault):
    backend, conn = _pg_backend()
    with pytest.raises(fault):
        with vs.transaction(backend, tmp_path) as tx:
            tx.put('vault', 'key', b'owned')
            raise fault('owned transaction fault')
    assert conn.rows == {} and getattr(vs._LOCAL, 'current', None) is None
    with pytest.raises(vs.StorageError):
        tx.keys('vault')

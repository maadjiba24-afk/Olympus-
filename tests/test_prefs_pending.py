"""Draft native preference CAS/confirmation controls; recovery journal excluded."""
import os
import threading

import pytest

from olympus import config, prefs, prefs_native as native

OWNER = 'owned-preference-principal'


@pytest.fixture
def owned(tmp_path, monkeypatch):
    root = tmp_path / 'owned-preference-state'
    root.mkdir()
    monkeypatch.setattr(config, 'MEMORY_DIR', root)
    monkeypatch.setattr(native, '_GATE', threading.RLock())
    monkeypatch.setattr(native, '_LOCAL', threading.local())
    monkeypatch.setattr(native, '_IN_FLIGHT', False)
    monkeypatch.setattr(native, '_POISONED', False)
    monkeypatch.setattr(prefs, '_TRANSFORM', threading.local())
    return root


def _path(root):
    return prefs._io(root.joinpath(*prefs._relative_paths(OWNER)[0]))


def _pending(owned, present=True):
    if present:
        prefs.set(OWNER, 'scope', 'before')
    calls = []
    def transform(data):
        calls.append(True)
        data['scope'] = 'after'
        return data
    pending = prefs.prepare_mutation(OWNER, transform)
    assert calls == [True]
    return pending, calls


def test_actual_native_identity_roundtrip_confirmation_does_not_replace(owned, monkeypatch):
    prefs.set(OWNER, 'scope', 'before')
    parts = prefs._relative_paths(OWNER)[0]
    with native.transaction(owned) as tx:
        raw, identity = tx.read(parts)
        assert len(identity) == 5
        if os.name == 'nt':
            assert identity[4] is None  # Actual native Windows information, not os.stat.
        monkeypatch.setattr(tx, 'publish', lambda *a: pytest.fail('confirmation replaced a file'))
        monkeypatch.setattr(tx, 'write_new', lambda *a: pytest.fail('confirmation created a file'))
        tx.confirm(parts, raw, identity)
        assert tx.read(parts) == (raw, identity)


@pytest.mark.parametrize('selection', ['bytes', 'identity', 'missing'])
def test_wrong_confirmation_selection_refuses_before_flush(owned, monkeypatch, selection):
    prefs.set(OWNER, 'scope', 'before')
    parts = prefs._relative_paths(OWNER)[0]
    with native.transaction(owned) as tx:
        raw, identity = tx.read(parts)
        monkeypatch.setattr(os, 'fsync', lambda *a: pytest.fail('wrong image was flushed'))
        selected_parts = ('missing-owned-record',) if selection == 'missing' else parts
        selected_raw = b'different-owned-bytes' if selection == 'bytes' else raw
        selected_identity = (*identity[:2], identity[2] + 1, *identity[3:]) if selection == 'identity' else identity
        with pytest.raises(native.PreferenceStorageError):
            tx.confirm(selected_parts, selected_raw, selected_identity)


@pytest.mark.parametrize('present', [False, True])
@pytest.mark.parametrize('fault_type', [OSError, KeyboardInterrupt, SystemExit])
def test_actual_publication_lost_ack_settles_without_replace_or_transform(owned, monkeypatch, present, fault_type):
    pending, calls = _pending(owned, present)
    fault = fault_type('owned-private-publication-ack')
    original = native.Transaction.publish
    dispatched = []
    def lose_ack(tx, parts, data):
        original(tx, parts, data)
        dispatched.append((parts, data))
        raise fault
    expected = prefs.PreferenceOperationError if fault_type is OSError else fault_type
    with monkeypatch.context() as patch:
        patch.setattr(native.Transaction, 'publish', lose_ack)
        with pytest.raises(expected) as raised:
            prefs.settle_mutation(OWNER, pending)
        if fault_type is OSError:
            assert raised.value.__cause__ is None and raised.value.__context__ is None
            assert 'owned-private' not in str(raised.value)
        else:
            assert raised.value is fault
    assert len(dispatched) == 1 and calls == [True]
    assert _path(owned).read_bytes() == pending.after
    assert pending.status() == {'publication_dispatched': True, 'publication_confirmed': False, 'cleanup_unconfirmed': False}
    confirmed = []
    confirm = native.Transaction.confirm
    def record_confirm(tx, parts, raw, identity):
        if os.name == 'nt': assert identity[4] is None
        confirmed.append(identity)
        return confirm(tx, parts, raw, identity)
    with monkeypatch.context() as patch:
        patch.setattr(native.Transaction, 'publish', lambda *a: pytest.fail('settlement replaced the file again'))
        patch.setattr(native.Transaction, 'confirm', record_confirm)
        assert prefs.settle_mutation(OWNER, pending)['publication_confirmed']
        assert prefs.settle_mutation(OWNER, pending)['publication_confirmed']
    assert len(confirmed) == 2 and confirmed[0] == confirmed[1] and calls == [True]


def test_confirmed_same_bytes_new_inode_cannot_be_replayed(owned, monkeypatch):
    pending, calls = _pending(owned)
    prefs.settle_mutation(OWNER, pending)
    with native.transaction(owned) as tx:
        old = tx.read(pending.before.current_parts)
        tx.publish(pending.before.current_parts, pending.after)
        new = tx.read(pending.before.current_parts)
    assert new[0] == old[0] and new[1][:2] != old[1][:2]
    monkeypatch.setattr(native.Transaction, 'publish', lambda *a: pytest.fail('confirmed intent was replayed'))
    monkeypatch.setattr(native.Transaction, 'confirm', lambda *a: pytest.fail('changed confirmed inode accepted'))
    with pytest.raises(prefs.PreferenceConflict): prefs.settle_mutation(OWNER, pending)
    assert calls == [True] and _path(owned).read_bytes() == new[0]


@pytest.mark.parametrize('changed', ['current', 'same_bytes_inode', 'legacy'])
def test_prepared_cas_refuses_changes_before_any_dispatch(owned, monkeypatch, changed):
    pending, calls = _pending(owned)
    with native.transaction(owned) as tx:
        if changed == 'legacy':
            tx.publish(pending.before.legacy_parts, b'{"autonomy":0}')
        else:
            raw = pending.before.raw if changed == 'same_bytes_inode' else b'{"scope":"revoked"}'
            tx.publish(pending.before.current_parts, raw)
    before = _path(owned).read_bytes()
    monkeypatch.setattr(native.Transaction, 'publish', lambda *a: pytest.fail('stale intent was dispatched'))
    with pytest.raises(prefs.PreferenceConflict): prefs.settle_mutation(OWNER, pending)
    assert not pending.status()['publication_dispatched'] and calls == [True]
    assert _path(owned).read_bytes() == before


def test_capture_cas_conflict_does_not_invoke_transform(owned):
    prefs.set(OWNER, 'scope', 'before')
    captured = prefs.capture(OWNER)
    prefs.set(OWNER, 'scope', 'revoked')
    with pytest.raises(prefs.PreferenceConflict):
        prefs.prepare_mutation(OWNER, lambda *a: pytest.fail('stale snapshot transform reached'), captured=captured)
    assert prefs.get(OWNER, 'scope') == 'revoked'


@pytest.mark.parametrize('component', ['root', 'locks', 'lock'])
def test_captured_root_or_coordination_replacement_is_refused(owned, monkeypatch, component):
    pending, calls = _pending(owned)
    path = {'root': owned, 'locks': owned / 'locks', 'lock': owned / 'locks' / native._LOCK_NAME}[component]
    retained = path.with_name(path.name + '-retained')
    path.rename(retained)
    if component == 'lock': path.write_bytes(b'owned-replacement')
    else: path.mkdir()
    monkeypatch.setattr(native.Transaction, 'publish', lambda *a: pytest.fail('replacement authority used'))
    with pytest.raises(prefs.PreferenceOperationError): prefs.settle_mutation(OWNER, pending)
    assert retained.exists() and not pending.status()['publication_dispatched'] and calls == [True]
    if component != 'lock': assert list(path.iterdir()) == []
    else: assert path.read_bytes() == b'owned-replacement'


@pytest.mark.parametrize('entry', ['load', 'set', 'repair', 'migrate', 'discard'])
def test_nested_preference_access_is_refused_without_inner_write(owned, entry):
    prefs.set(OWNER, 'scope', 'before')
    before = _path(owned).read_bytes()
    operations = {
        'load': lambda: prefs.load(OWNER),
        'set': lambda: prefs.set(OWNER, 'scope', 'inner'),
        'repair': lambda: prefs.repair(OWNER),
        'migrate': lambda: prefs.migrate_legacy('owned-legacy', OWNER),
        'discard': lambda: prefs.discard_legacy('owned-legacy'),
    }
    def transform(data):
        operations[entry]()
        return data
    with pytest.raises(prefs.PreferenceConflict): prefs.mutate(OWNER, transform)
    assert _path(owned).read_bytes() == before and not prefs._TRANSFORM.active
    prefs.set(OWNER, 'scope', 'after')
    assert prefs.get(OWNER, 'scope') == 'after'


def test_captured_mutation_uses_original_root_and_does_not_touch_later_selector(owned, tmp_path, monkeypatch):
    pending, calls = _pending(owned)
    changed_root = tmp_path / 'untouched-later-selector'
    monkeypatch.setattr(config, 'MEMORY_DIR', changed_root)
    assert prefs.settle_mutation(OWNER, pending)['publication_confirmed']
    assert not changed_root.exists() and _path(owned).read_bytes() == pending.after and calls == [True]


@pytest.mark.parametrize('fault_type', [OSError, KeyboardInterrupt, SystemExit])
def test_actual_read_plus_close_uncertainty_never_becomes_default(owned, monkeypatch, fault_type):
    prefs.set(OWNER, 'scope', 'before')
    captured = prefs.capture(OWNER)
    identity = captured.identity[:2]
    read, close = os.read, os.close
    selected, attempts, retained_fd = [], [], []
    sentinel = owned / 'owned-close-sentinel'
    sentinel.write_bytes(b'owned-sentinel')
    fault = fault_type('owned-private-read-fault')
    def failed_read(fd, size):
        info = native.storage._file_information(fd)
        result = read(fd, size)
        if (info.st_dev, info.st_ino) == identity and not selected:
            selected.append(fd)
            raise fault
        return result
    def failed_close(fd):
        if selected and fd == selected[0]:
            attempts.append(fd)
            if not retained_fd:
                close(fd)
                retained_fd.append(os.open(sentinel, os.O_RDONLY))
                assert retained_fd[0] == fd
                raise OSError('owned-private-secondary-close')
        return close(fd)
    expected = prefs.PreferenceOperationError if fault_type is OSError else fault_type
    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, 'read', failed_read)
            patch.setattr(os, 'close', failed_close)
            with pytest.raises(expected) as raised:
                prefs.get(OWNER, 'cosmetic', 'unsafe-default')
            assert native.cleanup_unknown(raised.value)
            if fault_type is OSError:
                assert raised.value.__cause__ is None and raised.value.__context__ is None
                assert 'owned-private' not in str(raised.value)
            else: assert raised.value is fault
            assert attempts == selected and read(retained_fd[0], 64) == b'owned-sentinel'
        assert native._POISONED and not native._IN_FLIGHT
        with monkeypatch.context() as patch:
            patch.setattr(prefs.memory, 'canonical_owner', lambda *a: pytest.fail('poisoned owner selector reached'))
            with pytest.raises(prefs.PreferenceOperationError) as raised: prefs.capture(OWNER)
            assert native.cleanup_unknown(raised.value)
    finally:
        if retained_fd: close(retained_fd.pop())


@pytest.mark.parametrize('fault_type', [OSError, KeyboardInterrupt, SystemExit])
def test_actual_publish_plus_directory_close_uncertainty_retains_attempt(owned, monkeypatch, fault_type):
    pending, calls = _pending(owned)
    publish, close = native.storage._Directory.publish, native.storage._Directory.close
    fault = fault_type('owned-private-post-publication')
    closed = []
    def lost_ack(directory, name, data):
        publish(directory, name, data)
        raise fault
    def close_then_fail(directory):
        result = close(directory)
        if directory.path == owned:
            closed.append(directory)
            raise OSError('owned-private-secondary-directory-close')
        return result
    expected = prefs.PreferenceOperationError if fault_type is OSError else fault_type
    with monkeypatch.context() as patch:
        patch.setattr(native.storage._Directory, 'publish', lost_ack)
        patch.setattr(native.storage._Directory, 'close', close_then_fail)
        with pytest.raises(expected) as raised: prefs.settle_mutation(OWNER, pending)
        assert native.cleanup_unknown(raised.value)
        if fault_type is OSError:
            assert raised.value.__cause__ is None and raised.value.__context__ is None
            assert 'owned-private' not in str(raised.value)
        else: assert raised.value is fault
    assert len(closed) == 1 and native._POISONED and not native._IN_FLIGHT
    assert _path(owned).read_bytes() == pending.after and calls == [True]
    assert pending.status() == {'publication_dispatched': True, 'publication_confirmed': False, 'cleanup_unconfirmed': True}


def test_replacement_between_confirmation_read_and_open_refuses_before_flush(owned, monkeypatch):
    prefs.set(OWNER, 'scope', 'before')
    parts = prefs._relative_paths(OWNER)[0]
    path = _path(owned)
    retained = path.with_name('owned-before-confirmation')
    original_open = native.storage._Directory._open
    calls = []
    with native.transaction(owned) as tx:
        raw, identity = tx.read(parts)
        def opened(directory, name, flags, *args, **kwargs):
            if directory.path == owned.joinpath(*parts[:-1]) and name == parts[-1]:
                calls.append(True)
                if len(calls) == 2:
                    path.rename(retained)
                    path.write_bytes(raw)
            return original_open(directory, name, flags, *args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(native.storage._Directory, '_open', opened)
            patch.setattr(os, 'fsync', lambda *a: pytest.fail('replacement was flushed'))
            with pytest.raises(native.PreferenceStorageError): tx.confirm(parts, raw, identity)
    assert len(calls) == 2 and retained.read_bytes() == path.read_bytes() == raw
    with native.transaction(owned) as tx:
        assert tx.read(parts)[1][:2] != identity[:2]


@pytest.mark.parametrize('fault_type', [OSError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('boundary', ['file_flush', 'directory_confirmation', 'close'])
def test_confirmation_fault_preserves_primary_and_resource_ownership(owned, monkeypatch, fault_type, boundary):
    prefs.set(OWNER, 'scope', 'before')
    parts = prefs._relative_paths(OWNER)[0]
    with native.transaction(owned) as tx:
        raw, identity = tx.read(parts)
        binding = tx.binding
    flush, close, sync = os.fsync, os.close, native.storage._Directory.sync
    selected, failed, attempts, retained_fd = [], [], [], []
    sentinel = owned / 'owned-confirm-sentinel'
    sentinel.write_bytes(b'owned-confirm-sentinel')
    fault = fault_type('owned-private-confirm-fault')
    def flushed(fd):
        info = native.storage._file_information(fd)
        result = flush(fd)
        if (info.st_dev, info.st_ino) == identity[:2] and not selected:
            selected.append(fd)
            if boundary == 'file_flush':
                failed.append(True)
                raise fault
        return result
    def synced(directory):
        result = sync(directory)
        if selected and boundary == 'directory_confirmation' and not failed:
            failed.append(True)
            raise fault
        return result
    def closed(fd):
        if selected and fd == selected[0]:
            attempts.append(fd)
            if boundary == 'close' and not failed:
                close(fd)
                retained_fd.append(os.open(sentinel, os.O_RDONLY))
                assert retained_fd[0] == fd
                failed.append(True)
                raise fault
        return close(fd)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, 'fsync', flushed)
            patch.setattr(os, 'close', closed)
            patch.setattr(native.storage._Directory, 'sync', synced)
            with pytest.raises(fault_type) as raised:
                with native.transaction(owned, binding=binding) as tx:
                    tx.confirm(parts, raw, identity)
            assert raised.value is fault and failed and selected
            assert native.cleanup_unknown(fault) is (boundary == 'close')
            assert native._POISONED is (boundary == 'close') and not native._IN_FLIGHT
            assert attempts == selected
            if boundary == 'close':
                assert os.read(retained_fd[0], 64) == b'owned-confirm-sentinel'
            else:
                with pytest.raises(OSError): os.fstat(selected[0])
        assert _path(owned).read_bytes() == raw
        if boundary != 'close':
            with native.transaction(owned, binding=binding) as tx:
                tx.confirm(parts, raw, identity)
                assert tx.read(parts) == (raw, identity)
    finally:
        if retained_fd: close(retained_fd.pop())


@pytest.mark.parametrize('fault_type', [OSError, KeyboardInterrupt, SystemExit])
def test_actual_rename_then_directory_confirmation_failure_settles_without_replace(owned, monkeypatch, fault_type):
    pending, calls = _pending(owned)
    renamed, failed, reconfirmed = [], [], []
    fault = fault_type('owned-private-post-rename-barrier')
    sync = native.storage._Directory.sync
    parent = owned.joinpath(*pending.before.current_parts[:-1])
    with monkeypatch.context() as patch:
        if os.name == 'nt':
            from olympus import prefs_windows
            original_rename = prefs_windows.rename_same_directory
            def rename(*args, **kwargs):
                result = original_rename(*args, **kwargs)
                renamed.append(True)
                return result
            patch.setattr(prefs_windows, 'rename_same_directory', rename)
        else:
            original_rename = os.replace
            def rename(*args, **kwargs):
                result = original_rename(*args, **kwargs)
                renamed.append(True)
                return result
            patch.setattr(os, 'replace', rename)
        def fail_confirmation(directory):
            if renamed and directory.path == parent and not failed:
                failed.append(True)
                raise fault  # Before the post-rename directory confirmation.
            return sync(directory)
        patch.setattr(native.storage._Directory, 'sync', fail_confirmation)
        expected = prefs.PreferenceOperationError if fault_type is OSError else fault_type
        with pytest.raises(expected) as raised: prefs.settle_mutation(OWNER, pending)
        if fault_type is OSError:
            assert raised.value.__cause__ is None and raised.value.__context__ is None
        else: assert raised.value is fault
    assert renamed == failed == [True] and calls == [True]
    assert _path(owned).read_bytes() == pending.after
    assert pending.status() == {'publication_dispatched': True, 'publication_confirmed': False, 'cleanup_unconfirmed': False}
    def confirmed(directory):
        result = sync(directory)
        if directory.path == parent: reconfirmed.append(True)
        return result
    with monkeypatch.context() as patch:
        patch.setattr(native.Transaction, 'publish', lambda *a: pytest.fail('post-rename settlement replaced the file'))
        patch.setattr(native.storage._Directory, 'sync', confirmed)
        assert prefs.settle_mutation(OWNER, pending)['publication_confirmed']
    assert reconfirmed == [True] and calls == [True]
    # Windows checks this platform confirmation boundary, not directory fsync.


def test_pending_wrong_owner_refuses_before_native_acquisition(owned, monkeypatch):
    pending, calls = _pending(owned)
    monkeypatch.setattr(native, 'transaction', lambda *a, **k: pytest.fail('wrong-owner native acquisition'))
    with pytest.raises(prefs.PreferenceConflict): prefs.settle_mutation('different-owned-principal', pending)
    assert not pending.status()['publication_dispatched'] and calls == [True]


def test_pending_other_thread_refuses_before_native_acquisition(owned, monkeypatch):
    pending, calls = _pending(owned)
    errors = []
    monkeypatch.setattr(native, 'transaction', lambda *a, **k: pytest.fail('foreign-thread native acquisition'))
    def other_executor():
        try:
            prefs.settle_mutation(OWNER, pending)
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=other_executor)
    thread.start()
    thread.join(5)
    assert not thread.is_alive()
    assert len(errors) == 1 and type(errors[0]) is prefs.PreferenceOperationError
    assert errors[0].__cause__ is None and errors[0].__context__ is None
    assert not pending.status()['publication_dispatched'] and calls == [True]

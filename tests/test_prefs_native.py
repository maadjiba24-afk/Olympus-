"""Owned native preference primitives; no provider/browser/network actions."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from olympus import archive_io, prefs_native as native

_PARTS = ('prefs', 'owners', 'owned-principal', 'prefs.json')


@pytest.fixture
def owned(tmp_path, monkeypatch):
    root = tmp_path / 'owned-state'
    root.mkdir()
    monkeypatch.setattr(native, '_GATE', threading.RLock())
    monkeypatch.setattr(native, '_LOCAL', threading.local())
    monkeypatch.setattr(native, '_IN_FLIGHT', False)
    monkeypatch.setattr(native, '_POISONED', False)
    return root


def test_native_roundtrip_and_retained_binding(owned):
    raw = b'{"operator":{"enabled":false}}'
    with native.transaction(owned) as tx:
        assert tx.read(_PARTS) == (None, None)
        tx.publish(_PARTS, raw)
        value, identity = tx.read(_PARTS)
        assert value == raw and len(identity) == 5
        binding = tx.binding
        with native.transaction(owned, binding=binding) as nested:
            assert nested is tx
    with native.transaction(owned, binding=binding) as tx:
        assert tx.read(_PARTS) == (value, identity)
    assert not native._IN_FLIGHT and not native._POISONED


@pytest.mark.parametrize('component', ['root', 'locks', 'lock'])
@pytest.mark.parametrize('replacement', [False, True])
def test_bound_reentry_refuses_missing_or_replaced_authority_without_recreation(owned, component, replacement):
    with native.transaction(owned) as tx:
        binding = tx.binding
        tx.publish(_PARTS, b'{}')
    path = {'root': owned, 'locks': owned / 'locks', 'lock': owned / 'locks' / native._LOCK_NAME}[component]
    retained = path.with_name(path.name + '-retained')
    path.rename(retained)
    if replacement:
        if component == 'lock':
            path.write_bytes(b'')
        else:
            path.mkdir()
    with pytest.raises((native.PreferenceStorageError, native.storage.StorageError, OSError)):
        with native.transaction(owned, binding=binding):
            pytest.fail('replacement authority was accepted')
    assert retained.exists()
    assert path.exists() is replacement
    if replacement and component != 'lock':
        assert list(path.iterdir()) == []


@pytest.mark.parametrize('leaf', ['CON', 'con.txt', 'unsafe.', 'unsafe ', 'bad:name', 'a' * 129, '../escape'])
def test_invalid_leaf_refuses_before_creating_parent(owned, leaf):
    with native.transaction(owned) as tx:
        with pytest.raises((native.PreferenceStorageError, native.storage.StorageError, archive_io.ArchiveError)):
            tx.publish(('not-created', leaf), b'{}')
        assert not (owned / 'not-created').exists()


def test_data_api_cannot_address_its_coordination_inode(owned):
    with native.transaction(owned) as tx:
        original = tx.binding
        with pytest.raises(native.PreferenceStorageError):
            tx.publish(('locks', native._LOCK_NAME), b'replacement')
        assert tx.binding == original
        tx.check()
    assert (owned / 'locks' / native._LOCK_NAME).read_bytes() == b''


@pytest.mark.skipif(os.name == 'nt', reason='POSIX rename while an owned descriptor is held')
def test_acquired_leaf_disappearance_is_not_absence(owned, monkeypatch):
    with native.transaction(owned) as tx:
        tx.publish(_PARTS, b'{"bounded":true}')
        path = owned.joinpath(*_PARTS)
        inode = path.stat().st_ino
        original = os.read
        moved = []
        def read_then_move(fd, size):
            raw = original(fd, size)
            if os.fstat(fd).st_ino == inode and not moved:
                retained = path.with_name('retained-record')
                path.rename(retained)
                moved.append(retained)
            return raw
        with monkeypatch.context() as patch:
            patch.setattr(native.os, 'read', read_then_move)
            with pytest.raises((native.PreferenceStorageError, native.storage.StorageError)):
                tx.read(_PARTS)
        assert moved[0].read_bytes() == b'{"bounded":true}' and not path.exists()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX rename while an owned directory is held')
def test_cached_parent_disappearance_is_not_absence(owned):
    with pytest.raises((native.PreferenceStorageError, native.storage.StorageError, OSError)):
        with native.transaction(owned) as tx:
            tx.publish(_PARTS, b'{}')
            parent = owned.joinpath(*_PARTS[:-1])
            retained = parent.with_name('retained-principal')
            parent.rename(retained)
            with pytest.raises((native.PreferenceStorageError, native.storage.StorageError, OSError)):
                tx.read(_PARTS)
            assert retained.joinpath(_PARTS[-1]).read_bytes() == b'{}'
            # The context's final authority check must also refuse this loss.


@pytest.mark.parametrize('fault', [RuntimeError, KeyboardInterrupt, SystemExit])
def test_cleanup_uncertainty_is_sticky_and_does_not_retry_descriptor_close(owned, monkeypatch, fault):
    primary = fault('owned-cleanup-fault')
    original_unlock, original_close = native.storage._unlock, os.close
    calls, selected = [], []
    def unlock_then_fail(fd):
        original_unlock(fd)
        calls.append(('unlock', fd))
        raise primary
    def close(fd):
        if selected and fd == selected[0]:
            calls.append(('close', fd))
        return original_close(fd)
    with monkeypatch.context() as patch:
        patch.setattr(native.storage, '_unlock', unlock_then_fail)
        patch.setattr(native.os, 'close', close)
        with pytest.raises(fault) as raised:
            with native.transaction(owned) as tx:
                selected.append(tx.fd)
        assert raised.value is primary and native.cleanup_unknown(primary)
        before = list(calls)
        with pytest.raises(native.PreferenceStorageError) as repeated:
            tx.close()
        assert native.cleanup_unknown(repeated.value) and calls == before
    assert calls == [('unlock', selected[0]), ('close', selected[0])]
    assert native._POISONED and not native._IN_FLIGHT
    with monkeypatch.context() as patch:
        patch.setattr(native.archive_io, 'selected_directory', lambda *args: pytest.fail('selector evaluated after poisoned cleanup'))
        with pytest.raises(native.PreferenceStorageError):
            with native.transaction(owned):
                pytest.fail('uncertain authority reused')


def test_cleanup_failure_preserves_body_interrupt_and_attempts_every_close(owned, monkeypatch):
    body_error = KeyboardInterrupt('owned-body-interrupt')
    original = native.storage._unlock
    calls = []
    def unlock(fd):
        original(fd)
        calls.append(fd)
        raise OSError('owned-unlock-ack')
    monkeypatch.setattr(native.storage, '_unlock', unlock)
    with pytest.raises(KeyboardInterrupt) as raised:
        with native.transaction(owned) as tx:
            resources = _transaction_resources(tx)
            raise body_error
    assert raised.value is body_error and native.cleanup_unknown(body_error)
    assert len(calls) == 1 and tx.closed and tx.fd is None and tx.directories == []
    _assert_closed(resources)


def test_retained_directory_cap_refuses_before_next_creation(owned, monkeypatch):
    monkeypatch.setattr(native, '_MAX_DIRECTORIES', 3)
    with native.transaction(owned) as tx:
        tx.publish(('first', 'settings'), b'{}')
        with pytest.raises(native.PreferenceStorageError):
            tx.publish(('second', 'settings'), b'{}')
        assert not (owned / 'second').exists()


@pytest.mark.parametrize('late', ['constructor', 'successful_probe'])
def test_late_acquisition_refuses_and_releases_any_acquired_native_lock(owned, monkeypatch, late):
    clock = [10.0]
    original_tx, original_probe = native.Transaction, native.storage._try_lock
    probes = []
    monkeypatch.setattr(native.time, 'monotonic', lambda: clock[0])
    class SlowTransaction(original_tx):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            if late == 'constructor':
                clock[0] += 2
    def probe(fd):
        probes.append(fd)
        result = original_probe(fd)
        if late == 'successful_probe':
            clock[0] += 2
        return result
    monkeypatch.setattr(native, 'Transaction', SlowTransaction)
    monkeypatch.setattr(native.storage, '_try_lock', probe)
    with pytest.raises(native.PreferenceStorageError):
        with native.transaction(owned, timeout=1):
            pytest.fail('late authority yielded')
    assert len(probes) == (0 if late == 'constructor' else 1)
    assert not native._POISONED and not native._IN_FLIGHT
    with native.transaction(owned, timeout=0):
        pass


def test_zero_timeout_uses_only_one_immediate_probe(owned, monkeypatch):
    probes = []
    monkeypatch.setattr(native.storage, '_try_lock', lambda fd: probes.append(fd) or False)
    monkeypatch.setattr(native.time, 'sleep', lambda seconds: pytest.fail('zero-timeout acquisition slept'))
    with pytest.raises(native.PreferenceStorageError):
        with native.transaction(owned, timeout=0):
            pytest.fail('busy native lock was admitted')
    assert len(probes) == 1 and not native._POISONED


def _owned_child(root):
    """Fresh direct child in the harness's existing job/process group."""
    code = '''import json,socket,sys
def denied(*args,**kwargs):raise AssertionError('owned preference child network disabled')
socket.socket.connect=denied;socket.socket.connect_ex=denied
socket.create_connection=denied;socket.socket.bind=denied
sys.path.insert(0,sys.argv[1])
from olympus import prefs_native
facts={'acquired':False,'native_denied':False,'native_acquired':False}
original=prefs_native.storage._try_lock
def probe(fd):
    result=original(fd)
    facts['native_acquired' if result else 'native_denied']=True
    return result
prefs_native.storage._try_lock=probe
try:
    with prefs_native.transaction(sys.argv[2],timeout=1) as tx:
        tx.publish(('prefs','child','settings'),b'{"child":true}')
    facts['acquired']=True
except prefs_native.PreferenceStorageError:
    pass
print(json.dumps(facts))
'''
    child = None
    primary = None
    cleanup = []
    try:
        child = subprocess.Popen([sys.executable, '-I', '-B', '-c', code,
            str(Path(native.__file__).resolve().parents[1]), str(root)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = child.communicate(timeout=8)
        assert child.returncode == 0 and stderr == b'' and len(stdout) < 160
        return json.loads(stdout)
    except BaseException as error:
        primary = error
        raise
    finally:
        if child is not None:
            try:
                if child.poll() is None:
                    child.kill()  # Owned direct, unreaped child / retained Windows process handle.
                    child.wait(timeout=3)
            except BaseException as error:
                cleanup.append(type(error).__name__)
            for stream in (child.stdout, child.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except BaseException as error:
                        cleanup.append(type(error).__name__)
        if cleanup:
            if primary is not None:
                BaseException.__dict__['__dict__'].__get__(primary)['_owned_child_cleanup_unconfirmed'] = True
            else:
                raise AssertionError('owned native preference child cleanup is unconfirmed')


def test_native_lock_contends_across_fresh_process_and_releases(owned):
    with native.transaction(owned):
        assert _owned_child(owned) == {'acquired': False, 'native_denied': True, 'native_acquired': False}
        assert not (owned / 'prefs' / 'child').exists()
    assert _owned_child(owned) == {'acquired': True, 'native_denied': False, 'native_acquired': True}
    assert (owned / 'prefs' / 'child' / 'settings').read_bytes() == b'{"child":true}'


@pytest.mark.skipif(os.name == 'nt', reason='POSIX fork control; Windows has no os.fork')
def test_fork_from_another_thread_refuses_inherited_native_authority_before_selection(owned):
    import select
    import signal
    import time
    import warnings
    entered, release = threading.Event(), threading.Event()
    failures = []
    def hold():
        try:
            with native.transaction(owned):
                entered.set()
                assert release.wait(30)
        except BaseException as error:
            failures.append(type(error).__name__)
    thread = threading.Thread(target=hold, daemon=True)
    read_fd = write_fd = pid = None
    started = reaped = False
    cleanup = []
    def reap(deadline):
        nonlocal reaped
        while True:
            observed, status = os.waitpid(pid, os.WNOHANG)
            if observed == pid:
                reaped = True
                assert os.waitstatus_to_exitcode(status) == 0
                return
            assert observed == 0
            if time.monotonic() >= deadline:
                raise TimeoutError('owned fork child exceeded its exit deadline')
            time.sleep(0.01)
    try:
        thread.start()
        started = True
        assert entered.wait(3)
        read_fd, write_fd = os.pipe()
        with warnings.catch_warnings():
            warnings.filterwarnings('ignore', message=r'This process .* is multi-threaded, use of fork\(\) may lead to deadlocks in the child\.', category=DeprecationWarning)
            pid = os.fork()
        if pid == 0:
            try:
                os.close(read_fd)
                def forbidden_selector(*args):
                    raise AssertionError('forked selector was reached')
                native.archive_io.selected_directory = forbidden_selector
                try:
                    with native.transaction(owned):
                        os.write(write_fd, b'F')
                except native.PreferenceStorageError:
                    os.write(write_fd, b'R')
            finally:
                os._exit(0)
        closed, write_fd = write_fd, None
        os.close(closed)
        ready, _, _ = select.select([read_fd], [], [], 5)
        assert ready and os.read(read_fd, 1) == b'R'
        reap(time.monotonic() + 2)
        # The child's refusal/exit must not unlock the parent's flock.
        assert _owned_child(owned) == {'acquired': False, 'native_denied': True, 'native_acquired': False}
    finally:
        try:
            if pid is not None and pid > 0 and not reaped:
                observed, _status = os.waitpid(pid, os.WNOHANG)
                if observed == pid:
                    reaped = True
                elif observed == 0:
                    os.kill(pid, signal.SIGKILL)
                    reap(time.monotonic() + 2)
                else:
                    raise AssertionError('unexpected owned child observation')
        except BaseException as error:
            cleanup.append(type(error).__name__)
        for fd in (write_fd, read_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except BaseException as error:
                    cleanup.append(type(error).__name__)
        release.set()
        if started:
            try:
                thread.join(timeout=3)
                if thread.is_alive():
                    cleanup.append('thread_still_running')
            except BaseException as error:
                cleanup.append(type(error).__name__)
        assert not cleanup
    assert reaped and not failures and not native._POISONED


@pytest.mark.parametrize('operation', ['publish', 'write_new'])
def test_leaf_capacity_is_reserved_before_any_publication(owned, monkeypatch, operation):
    monkeypatch.setattr(native, '_MAX_LEAVES', 1)
    with native.transaction(owned) as tx:
        assert tx.read(('first',)) == (None, None)
        with pytest.raises(native.PreferenceStorageError):
            getattr(tx, operation)(('not-created', 'second'), b'{}')
        assert not (owned / 'not-created').exists()


def test_entire_directory_budget_is_admitted_before_creating_first_prefix(owned, monkeypatch):
    monkeypatch.setattr(native, '_MAX_DIRECTORIES', 3)
    with native.transaction(owned) as tx:
        with pytest.raises(native.PreferenceStorageError):
            tx.publish(('not-created', 'also-not-created', 'settings'), b'{}')
        assert not (owned / 'not-created').exists()


def _directory_resources(directory):
    if directory.win is not None:
        return [('handle', handle) for handle in directory.win.handles[directory.win.owned_start:]]
    return [('fd', fd) for fd in directory._parents[directory._owned_start:]]


def _transaction_resources(tx):
    return [('fd', tx.fd)] + [resource for directory in tx.directories for resource in _directory_resources(directory)]


def _assert_closed(resources):
    for kind, value in resources:
        with pytest.raises(OSError):
            if kind == 'fd':
                os.fstat(value)
            else:
                from olympus import prefs_windows
                prefs_windows.information(value)


@pytest.mark.parametrize('body_type', [RuntimeError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('stage', ['constructor', 'child'])
def test_delegated_directory_acquisition_drains_and_preserves_primary(owned, monkeypatch, stage, body_type):
    body, close_fault = body_type('owned-acquisition-body'), KeyboardInterrupt('owned-close-ack')
    target_path = owned if stage == 'constructor' else owned / 'fault-child'
    target_path.mkdir(exist_ok=True)
    original_check = native.storage._Directory.check
    resources, selected, attempts = [], [], []
    raised_body, raised_close, acquisition_index = [], [], []
    if os.name == 'nt':
        from olympus import prefs_windows
        close_module, original_close = prefs_windows, prefs_windows.close
    else:
        close_module, original_close = os, os.close
    def check(directory):
        original_check(directory)
        if directory.path == target_path and not raised_body:
            resources.extend(_directory_resources(directory))
            selected.append(resources[-1][1])
            # Earlier lock probes may have used this numeric descriptor before
            # the selected directory acquired it. Retain both histories.
            acquisition_index.append(len(attempts))
            raised_body.append(True)
            raise body
    def close(value):
        attempts.append(value)
        result = original_close(value)
        if selected and value == selected[0] and not raised_close:
            raised_close.append(True)
            raise close_fault
        return result
    with monkeypatch.context() as patch:
        patch.setattr(native.storage._Directory, 'check', check)
        patch.setattr(close_module, 'close', close)
        with pytest.raises(body_type) as raised:
            with native.transaction(owned) as tx:
                resources.extend(_transaction_resources(tx))
                tx.read(('fault-child', 'settings'))
        assert raised.value is body and native.cleanup_unknown(body)
        assert raised_body and raised_close and attempts[acquisition_index[0]:].count(selected[0]) == 1
    _assert_closed(resources)
    assert native._POISONED and not native._IN_FLIGHT


def _fd_close_fault(monkeypatch, owned, target):
    """Actual close followed by immediate CRT/POSIX fd reuse, then interruption."""
    sentinel_path = owned / 'owned-sentinel'
    sentinel_path.write_bytes(b'owned-sentinel')
    original = os.close
    state = {'sentinel': None, 'attempts': 0}
    fault = SystemExit('owned-close-ack')
    def close(fd):
        if target and fd == target[0]:
            state['attempts'] += 1
            if state['sentinel'] is None:
                original(fd)
                state['sentinel'] = os.open(sentinel_path, os.O_RDONLY)
                assert state['sentinel'] == fd
                raise fault
        return original(fd)
    monkeypatch.setattr(os, 'close', close)
    return state, fault, original


@pytest.mark.parametrize('body_type', [FileNotFoundError, KeyboardInterrupt, SystemExit])
def test_failed_leaf_acquisition_keeps_primary_and_never_recloses_reused_fd(owned, monkeypatch, body_type):
    body = body_type('owned-leaf-body')
    target, resources = [], []
    state = original_close = None
    try:
        with monkeypatch.context() as patch:
            with pytest.raises(body_type) as raised:
                with native.transaction(owned) as tx:
                    tx.publish(('settings',), b'{}')
                    resources.extend(_transaction_resources(tx))
                    value, identity = tx.read(('settings',))
                    original_information = native.storage._file_information
                    def information(fd):
                        info = original_information(fd)
                        if (info.st_dev, info.st_ino) == identity[:2] and not target:
                            target.append(fd)
                            raise body
                        return info
                    state, _fault, original_close = _fd_close_fault(patch, owned, target)
                    patch.setattr(native.storage, '_file_information', information)
                    tx.read(('settings',))
            assert raised.value is body and native.cleanup_unknown(body)
            assert state['attempts'] == 1 and state['sentinel'] == target[0]
            assert os.read(state['sentinel'], 64) == b'owned-sentinel'
        _assert_closed(resources)
        assert native._POISONED and not native._IN_FLIGHT
    finally:
        if state and state['sentinel'] is not None:
            fd, state['sentinel'] = state['sentinel'], None
            original_close(fd)


@pytest.mark.parametrize('operation', ['publish', 'write_new'])
@pytest.mark.parametrize('body_failure', [False, True])
def test_completed_write_cleanup_failure_preserves_fact_and_reused_fd(owned, monkeypatch, operation, body_failure):
    body = KeyboardInterrupt('owned-write-body')
    target, resources = [], []
    state = original_close = None
    try:
        with monkeypatch.context() as patch:
            with pytest.raises(KeyboardInterrupt if body_failure else SystemExit) as raised:
                with native.transaction(owned) as tx:
                    resources.extend(_transaction_resources(tx))
                    original_write = native.storage._Directory._write
                    def write(directory, fd, data):
                        original_write(directory, fd, data)
                        target.append(fd)
                        if body_failure:
                            raise body
                    state, close_fault, original_close = _fd_close_fault(patch, owned, target)
                    patch.setattr(native.storage._Directory, '_write', write)
                    getattr(tx, operation)(('completed-data',), b'{"written":true}')
            assert raised.value is (body if body_failure else close_fault)
            assert native.cleanup_unknown(raised.value) and state['attempts'] == 1
            assert os.read(state['sentinel'], 64) == b'owned-sentinel'
        _assert_closed(resources)
        retained = [p for p in owned.iterdir() if p.is_file() and (p.name == 'completed-data' or p.name.startswith('pending-'))]
        assert len(retained) == 1 and retained[0].read_bytes() == b'{"written":true}'
        assert native._POISONED and not native._IN_FLIGHT
    finally:
        if state and state['sentinel'] is not None:
            fd, state['sentinel'] = state['sentinel'], None
            original_close(fd)


@pytest.mark.parametrize('ending', ['early_close', 'consumer_failure', 'exhaustion'])
def test_enumeration_cleanup_is_observable_and_sticky(owned, monkeypatch, ending):
    from types import SimpleNamespace
    from olympus import native_cleanup
    directory = native.storage._Directory(owned)
    resources = _directory_resources(directory)
    fault, body = KeyboardInterrupt('owned-enumeration-close'), SystemExit('owned-consumer-body')
    calls = []
    class OwnedIterator:
        def __init__(self): self.remaining = True
        def __iter__(self): return self
        def __next__(self):
            if not self.remaining: raise StopIteration
            self.remaining = False
            return SimpleNamespace(name='owned-entry', attributes=0)
        def close(self):
            calls.append('close')
            raise fault
    iterator = OwnedIterator()
    try:
        if directory.win is not None:
            monkeypatch.setattr(directory.win, 'entries', lambda: iterator)
        else:
            monkeypatch.setattr(os, 'scandir', lambda fd: iterator)
        entries = directory.entries()
        next(entries)
        if ending == 'consumer_failure':
            try:
                raise body
            except BaseException as primary:
                assert not native_cleanup.drain([entries.close], primary)
                assert primary is body and native_cleanup.unknown(primary)
        else:
            with pytest.raises(KeyboardInterrupt) as raised:
                entries.close() if ending == 'early_close' else next(entries)
            assert raised.value is fault and native_cleanup.unknown(fault)
        assert calls == ['close']
        with pytest.raises(native.storage.StorageError) as unavailable:
            directory.check()
        assert native_cleanup.unknown(unavailable.value)
        with pytest.raises(native.storage.StorageError) as raised:
            directory.close()
        assert native_cleanup.unknown(raised.value)
        _assert_closed(resources)
        with pytest.raises(native.storage.StorageError) as repeated:
            directory.close()
        assert native_cleanup.unknown(repeated.value) and calls == ['close']
    finally:
        # A failed close is sticky; no numeric descriptor is retried.
        if not directory._closed:
            directory.close()


def test_malformed_exception_notes_do_not_replace_primary(owned):
    from olympus import native_cleanup
    body = KeyboardInterrupt('owned-primary')
    body.__notes__ = 17
    def failed_close(): raise SystemExit('owned-close')
    assert not native_cleanup.drain([failed_close], body)
    assert native_cleanup.unknown(body)
    native._mark_cleanup(body)
    assert native.cleanup_unknown(body) and body.__notes__ == 17


@pytest.mark.skipif(os.name != 'nt', reason='Windows native handle acquisition')
@pytest.mark.parametrize('stage', ['anchor', 'relative', 'to_fd', 'entry_information'])
def test_windows_local_acquisition_keeps_primary_and_closes_handle_once(owned, monkeypatch, stage):
    import msvcrt
    from olympus import native_cleanup, prefs_windows
    (owned / 'owned-leaf').write_bytes(b'{}')
    parent = native.storage._Directory(owned)
    parent_resources = _directory_resources(parent)
    body = FileNotFoundError('owned-native-information')
    close_fault = KeyboardInterrupt('owned-native-close')
    original_information, original_close = prefs_windows.information, prefs_windows.close
    captured, attempts, information_calls = [], [], []
    fired = []
    def information(handle, directory=None):
        result = original_information(handle, directory)
        information_calls.append(handle)
        if not fired and len(information_calls) == (2 if stage == 'entry_information' else 1):
            fired.append(True)
            captured.append(handle)
            raise body
        return result
    def close(handle):
        original_close(handle)
        attempts.append(handle)
        if captured and handle == captured[0]:
            raise close_fault
    try:
        with monkeypatch.context() as patch:
            patch.setattr(prefs_windows, 'close', close)
            if stage == 'to_fd':
                handle = prefs_windows.open_relative(parent.win.handle, 'owned-leaf')
                captured.append(handle)
                def fail_transfer(*args): raise body
                patch.setattr(msvcrt, 'open_osfhandle', fail_transfer)
                action = lambda: prefs_windows.to_fd(handle, os.O_RDONLY)
            else:
                patch.setattr(prefs_windows, 'information', information)
                if stage == 'anchor': action = lambda: prefs_windows._anchor(owned.anchor)
                elif stage == 'relative': action = lambda: prefs_windows.open_relative(parent.win.handle, 'owned-leaf')
                else: action = lambda: prefs_windows.entry_information(parent.win, 'owned-leaf', 0)
            with pytest.raises(FileNotFoundError) as raised:
                native._delegated(action)
            assert raised.value is body and native.cleanup_unknown(body) and native_cleanup.unknown(body)
            assert attempts == captured and len(attempts) == 1
            if stage == 'entry_information':
                assert information_calls == [captured[0], captured[0]]
        _assert_closed([('handle', captured[0])])
        assert native._POISONED
    finally:
        parent.close()
    _assert_closed(parent_resources)



def test_completed_read_cleanup_failure_preserves_reused_fd(owned, monkeypatch):
    target, resources, observed = [], [], []
    state = original_close = None
    try:
        with monkeypatch.context() as patch:
            with pytest.raises(SystemExit) as raised:
                with native.transaction(owned) as tx:
                    tx.publish(('settings',), b'{"read":true}')
                    resources.extend(_transaction_resources(tx))
                    original_read = os.read
                    def read(fd, count):
                        raw = original_read(fd, count)
                        if raw == b'{"read":true}':
                            target.append(fd)
                            observed.append(raw)
                        return raw
                    state, fault, original_close = _fd_close_fault(patch, owned, target)
                    patch.setattr(os, 'read', read)
                    tx.read(('settings',))
            assert raised.value is fault and native.cleanup_unknown(fault)
            assert observed == [b'{"read":true}'] and state['attempts'] == 1
            assert os.read(state['sentinel'], 64) == b'owned-sentinel'
        _assert_closed(resources)
        assert native._POISONED and not native._IN_FLIGHT
    finally:
        if state and state['sentinel'] is not None:
            fd, state['sentinel'] = state['sentinel'], None
            original_close(fd)


# Fresh-admission descent controls. No authority survives an operation boundary;
# these exercise actual owned transactions and their unchanged cleanup paths.
@pytest.mark.parametrize('operation', ['read', 'inventory'])
@pytest.mark.parametrize('present', [False, True], ids=['initial-absence', 'present'])
def test_fresh_admission_uses_one_pre_io_and_one_post_io_transaction_check(owned, monkeypatch, operation, present):
    raw = b'owned admitted image'
    if present:
        (owned / 'box').mkdir()
        (owned / 'box' / 'leaf').write_bytes(raw)
    with native.transaction(owned) as tx:
        checks = []
        actual_check = tx.check
        def check():
            checks.append(True)
            return actual_check()
        with monkeypatch.context() as patch:
            patch.setattr(tx, 'check', check)
            result = tx.read(('box', 'leaf')) if operation == 'read' else tx.inventory(('box',), limit=1)
        assert len(checks) == 2
        if present:
            # Python 3.12 os.stat may expose a different-width Windows device
            # number. Compare the same native identity domain, independently
            # acquired; retain separate Python inode and exact-content checks.
            path = owned / 'box' / 'leaf'
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0))
            primary = None
            try:
                independent_identity = native.storage._file_identity(native.storage._file_information(fd))
                independent_bytes = os.read(fd, len(raw) + 1)
                assert independent_bytes == raw and os.read(fd, 1) == b''
                assert independent_identity == native.storage._file_identity(native.storage._file_information(fd))
                assert independent_identity[1] == os.fstat(fd).st_ino == path.stat().st_ino
            except BaseException as error:
                primary = error
                raise
            finally:
                owned_fd, fd = fd, None
                native._drain([lambda: os.close(owned_fd)], primary)
        if operation == 'read':
            if present:
                assert result[0] == raw and len(result[1]) == 5
                assert result[1] == independent_identity
            else:
                assert result == (None, None)
        elif present:
            assert [(name, kind) for name, kind, _identity in result] == [('leaf', 'file')]
            assert result[0][2] == independent_identity
        else:
            assert result is None
    assert tx.closed and tx.directories == [] and tx.fd is None
    assert not native._IN_FLIGHT and not native._POISONED
    assert (owned / 'box').exists() is present
    if present:
        assert (owned / 'box' / 'leaf').read_bytes() == raw


@pytest.mark.parametrize('operation', ['read', 'inventory'])
@pytest.mark.parametrize('kind', [FileNotFoundError, OSError, KeyboardInterrupt, SystemExit])
def test_admission_authority_failure_is_not_reclassified_as_initial_absence(owned, monkeypatch, operation, kind):
    primary = kind('owned admission boundary refusal')
    with native.transaction(owned) as tx:
        checked, descended = [], []
        def check():
            checked.append(True)
            raise primary
        def descend(*args, **kwargs):
            descended.append(True)
            pytest.fail('failed admission reached native descent')
        with monkeypatch.context() as patch:
            patch.setattr(tx, 'check', check)
            patch.setattr(tx, '_parent_after_admission', descend)
            with pytest.raises(kind) as caught:
                if operation == 'read':
                    tx.read(('would-not-exist', 'leaf'))
                else:
                    tx.inventory(('would-not-exist',), limit=1)
        assert caught.value is primary and checked == [True] and descended == []
        assert tx.observed_leaves == {} and not (owned / 'would-not-exist').exists()
    assert tx.closed and tx.fd is None and not native._POISONED


@pytest.mark.parametrize('operation', ['read', 'inventory'])
@pytest.mark.parametrize('kind', [OSError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('cleanup_fails', [False, True], ids=['cleanup-confirmed', 'lost-close-ack'])
def test_post_io_transaction_check_and_independent_cleanup_remain(owned, monkeypatch, operation, kind, cleanup_fails):
    raw = b'owned post-observation image'
    (owned / 'box').mkdir()
    (owned / 'box' / 'leaf').write_bytes(raw)
    primary = kind('owned post-observation authority failure')
    secondary = OSError('owned cleanup acknowledgment lost')
    checks, observed_fds, close_attempts, listed, iterators = [], [], [], [], []
    tx = None
    with pytest.raises(kind) as caught:
        with native.transaction(owned) as tx:
            # Acquire this owned parent through the unchanged checked entry.
            directory, name = tx._parent(('box', 'leaf'))
            actual_check, actual_close = tx.check, os.close
            actual_open, actual_entries = directory._open, directory.entries
            def check():
                actual_check()
                checks.append(True)
                if len(checks) == 2:
                    raise primary
            def opened(selected, flags, *args, **kwargs):
                fd = actual_open(selected, flags, *args, **kwargs)
                if selected == name:
                    observed_fds.append(fd)
                return fd
            def closed(fd):
                target = fd in observed_fds
                if target:
                    close_attempts.append(fd)  # Include unsuccessful acknowledgments.
                actual_close(fd)
                if target and cleanup_fails:
                    raise secondary
            class ObservedIterator:
                def __init__(self):
                    self.inner = actual_entries()
                def __iter__(self):
                    return self
                def __next__(self):
                    entry = next(self.inner)
                    listed.append(entry.name)
                    return entry
                def close(self):
                    close_attempts.append('iterator')
                    self.inner.close()
                    if cleanup_fails:
                        raise secondary
            def entries():
                iterator = ObservedIterator()
                iterators.append(iterator)
                return iterator
            with monkeypatch.context() as patch:
                patch.setattr(tx, 'check', check)
                if operation == 'read':
                    patch.setattr(directory, '_open', opened)
                    patch.setattr(native.os, 'close', closed)
                    tx.read(('box', 'leaf'))
                else:
                    patch.setattr(directory, 'entries', entries)
                    tx.inventory(('box',), limit=1)
    assert caught.value is primary and checks == [True, True]
    assert native.cleanup_unknown(primary) is cleanup_fails
    assert native._POISONED is cleanup_fails and not native._IN_FLIGHT
    assert tx.closed and tx.fd is None and tx.directories == []
    assert directory._closed and directory._iterators == []
    if operation == 'read':
        assert len(observed_fds) == 1 and close_attempts == observed_fds
        with pytest.raises(OSError):
            os.fstat(observed_fds[0])
    else:
        assert listed == ['leaf'] and close_attempts == ['iterator'] and len(iterators) == 1
        assert iterators[0].inner.gi_frame is None
    assert (owned / 'box' / 'leaf').read_bytes() == raw


@pytest.mark.parametrize('operation', ['read', 'inventory'])
@pytest.mark.parametrize('budget', ['leaf', 'directory'])
def test_admission_capacity_refuses_before_any_descent_or_creation(owned, monkeypatch, operation, budget):
    with native.transaction(owned) as tx:
        before = (list(tx.directories), dict(tx.children), dict(tx.observed_leaves))
        descended = []
        def descend(*args, **kwargs):
            descended.append(True)
            pytest.fail('capacity refusal reached a parent')
        with monkeypatch.context() as patch:
            if budget == 'leaf':
                patch.setattr(native, '_MAX_LEAVES', 0)
            else:
                patch.setattr(native, '_MAX_DIRECTORIES', len(tx.directories))
            patch.setattr(tx, '_parent_after_admission', descend)
            with pytest.raises(native.PreferenceStorageError, match='planned authority exceeds'):
                if operation == 'read':
                    tx.read(('would-not-exist', 'leaf'))
                else:
                    tx.inventory(('would-not-exist',), limit=1)
        assert descended == []
        assert (tx.directories, tx.children, tx.observed_leaves) == before
        assert not (owned / 'would-not-exist').exists()


@pytest.mark.parametrize('kind', [FileNotFoundError, OSError, KeyboardInterrupt, SystemExit])
def test_direct_parent_entry_keeps_its_full_authority_check(owned, monkeypatch, kind):
    primary = kind('owned direct-parent boundary refusal')
    with native.transaction(owned) as tx:
        touched = []
        def check():
            touched.append('check')
            raise primary
        def descend(*args, **kwargs):
            touched.append('descend')
            pytest.fail('unchecked direct parent')
        with monkeypatch.context() as patch:
            patch.setattr(tx, 'check', check)
            patch.setattr(tx, '_parent_after_admission', descend)
            with pytest.raises(kind) as caught:
                tx._parent(('would-not-exist', 'leaf'))
        assert caught.value is primary and touched == ['check']
        assert tx.observed_leaves == {} and not (owned / 'would-not-exist').exists()


def test_direct_parent_preserves_validation_and_does_not_reserve_a_leaf(owned, monkeypatch):
    with native.transaction(owned) as tx:
        checked = []
        actual_check = tx.check
        def check():
            checked.append(True)
            return actual_check()
        with monkeypatch.context() as patch:
            patch.setattr(tx, 'check', check)
            with pytest.raises(native.PreferenceStorageError, match='coordination'):
                tx._parent(('locks', 'leaf'))
            assert checked == []
            directory, name = tx._parent(('ordinary',))
        assert directory is tx.directory and name == 'ordinary'
        assert checked == [True] and tx.observed_leaves == {}

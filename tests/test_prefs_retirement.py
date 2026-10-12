"""Owned native retirement/reconfirmation controls; no credentials or networks."""
import errno
import os
import threading

import pytest

from olympus import prefs_native as native

SOURCE = ('source',)
RETAINED = ('retained',)
RAW = b'{"owned-evidence":true}'


@pytest.fixture
def owned(tmp_path, monkeypatch):
    root = tmp_path / 'owned-retirement'
    root.mkdir()
    monkeypatch.setattr(native, '_GATE', threading.RLock())
    monkeypatch.setattr(native, '_LOCAL', threading.local())
    monkeypatch.setattr(native, '_IN_FLIGHT', False)
    monkeypatch.setattr(native, '_POISONED', False)
    return root


def _plant(root):
    with native.transaction(root) as tx:
        tx.publish(SOURCE, RAW)
        raw, identity = tx.read(SOURCE)
        return tx.binding, raw, identity


def _rename_hook(monkeypatch, callback):
    if os.name == 'nt':
        from olympus import prefs_windows
        module, name = prefs_windows, 'rename_exclusive'
    else:
        module, name = native.storage, '_rename_exclusive'
    original = getattr(module, name)
    monkeypatch.setattr(module, name, lambda *args: callback(original, args))


def _resume_without_rename(root, binding, raw, identity, monkeypatch):
    with monkeypatch.context() as patch:
        _rename_hook(patch, lambda *args: pytest.fail('retirement was repeated during confirmation'))
        with native.transaction(root, binding=binding) as tx:
            assert tx.retire(SOURCE, RETAINED, raw, identity) is False
            assert tx.read(SOURCE) == (None, None)
            retained, retained_identity = tx.read(RETAINED)
            assert retained == raw and retained_identity[:2] == identity[:2]


def test_actual_retirement_retains_inode_and_fresh_resume_only_confirms(owned, monkeypatch):
    binding, raw, identity = _plant(owned)
    with native.transaction(owned, binding=binding) as tx:
        assert tx.retire(SOURCE, RETAINED, raw, identity) is True
        assert tx.read(SOURCE) == (None, None)
        retained, observed = tx.read(RETAINED)
        assert retained == RAW and observed[:2] == identity[:2]
    _resume_without_rename(owned, binding, raw, identity, monkeypatch)


@pytest.mark.parametrize('same_bytes', [False, True])
@pytest.mark.parametrize('late', [False, True])
def test_destination_collision_never_replaces_owned_existing_evidence(owned, monkeypatch, same_bytes, late):
    binding, raw, identity = _plant(owned)
    collision = raw if same_bytes else b'other-owned-evidence'
    calls = []
    def create_target():
        with (owned / RETAINED[0]).open('xb') as stream: stream.write(collision)
    if not late: create_target()
    def collide(original, args):
        calls.append(True)
        assert late
        create_target()
        return original(*args)
    with monkeypatch.context() as patch:
        _rename_hook(patch, collide)
        with native.transaction(owned, binding=binding) as tx:
            with pytest.raises((native.PreferenceStorageError, native.storage.StorageError, OSError)):
                tx.retire(SOURCE, RETAINED, raw, identity)
            assert tx.read(SOURCE) == (raw, identity)
            retained, other_identity = tx.read(RETAINED)
            assert retained == collision and other_identity[:2] != identity[:2]
    assert len(calls) == (1 if late else 0)
    assert not native._POISONED


def test_copied_retained_image_is_not_own_retirement(owned):
    binding, raw, identity = _plant(owned)
    (owned / SOURCE[0]).rename(owned / 'original-owned-evidence')
    (owned / RETAINED[0]).write_bytes(raw)
    with native.transaction(owned, binding=binding) as tx:
        with pytest.raises(native.PreferenceStorageError):
            tx.retire(SOURCE, RETAINED, raw, identity)
    assert (owned / 'original-owned-evidence').read_bytes() == raw
    assert (owned / RETAINED[0]).read_bytes() == raw


@pytest.mark.parametrize('replacement', [RAW, b'recreated-owned-evidence'])
def test_idempotent_confirmation_refuses_source_recreation(owned, monkeypatch, replacement):
    binding, raw, identity = _plant(owned)
    with native.transaction(owned, binding=binding) as tx:
        assert tx.retire(SOURCE, RETAINED, raw, identity)
    original = native.storage._Directory.confirm
    def confirm(directory, name):
        original(directory, name)
        (owned / SOURCE[0]).write_bytes(replacement)
    monkeypatch.setattr(native.storage._Directory, 'confirm', confirm)
    with native.transaction(owned, binding=binding) as tx:
        with pytest.raises(native.PreferenceStorageError):
            tx.retire(SOURCE, RETAINED, raw, identity)
    assert (owned / SOURCE[0]).read_bytes() == replacement
    assert (owned / RETAINED[0]).read_bytes() == raw


@pytest.mark.skipif(os.name == 'nt', reason='POSIX source-name substitution while held')
@pytest.mark.parametrize('mode', ['replace', 'hardlink'])
def test_source_substitution_before_rename_is_refused(owned, monkeypatch, mode):
    binding, raw, identity = _plant(owned)
    original = os.fsync
    changed = []
    def sync(fd):
        info = native.storage._file_information(fd)
        if (info.st_dev, info.st_ino) == identity[:2] and not changed:
            changed.append(True)
            if mode == 'replace':
                (owned / SOURCE[0]).rename(owned / 'original-owned-evidence')
                (owned / SOURCE[0]).write_bytes(b'other-owned-evidence')
            else:
                os.link(owned / SOURCE[0], owned / 'original-owned-evidence')
        return original(fd)
    with monkeypatch.context() as patch:
        patch.setattr(os, 'fsync', sync)
        _rename_hook(patch, lambda *args: pytest.fail('changed source reached rename'))
        with native.transaction(owned, binding=binding) as tx:
            with pytest.raises((native.PreferenceStorageError, native.storage.StorageError)):
                tx.retire(SOURCE, RETAINED, raw, identity)
    assert changed and not (owned / RETAINED[0]).exists()
    assert (owned / 'original-owned-evidence').read_bytes() == raw


@pytest.mark.skipif(os.name != 'nt', reason='Windows exact source spelling after handle acquisition')
def test_windows_source_case_drift_after_acquisition_is_refused(owned, monkeypatch):
    import msvcrt
    from olympus import prefs_windows
    binding, raw, identity = _plant(owned)
    original, rename = os.fsync, prefs_windows.rename_exclusive
    changed = []
    def sync(fd):
        info = native.storage._file_information(fd)
        if (info.st_dev, info.st_ino) == identity[:2] and not changed:
            changed.append(True)
            rename(msvcrt.get_osfhandle(fd), 'source-intermediate')
            rename(msvcrt.get_osfhandle(fd), 'SOURCE')
        return original(fd)
    with monkeypatch.context() as patch:
        patch.setattr(os, 'fsync', sync)
        _rename_hook(patch, lambda *args: pytest.fail('case-drifted source reached final rename'))
        with native.transaction(owned, binding=binding) as tx:
            with pytest.raises((native.PreferenceStorageError, native.storage.StorageError)):
                tx.retire(SOURCE, RETAINED, raw, identity)
    assert changed and not (owned / RETAINED[0]).exists()
    assert 'SOURCE' in [p.name for p in owned.iterdir()]
    assert (owned / 'SOURCE').read_bytes() == raw


@pytest.mark.parametrize('fault_type', [OSError, KeyboardInterrupt, SystemExit])
def test_actual_rename_then_interruption_requires_fresh_transaction(owned, monkeypatch, fault_type):
    binding, raw, identity = _plant(owned)
    fault = fault_type('owned-post-rename-ack')
    calls = []
    def rename_then_fail(original, args):
        original(*args)
        calls.append(True)
        raise fault
    with monkeypatch.context() as patch:
        _rename_hook(patch, rename_then_fail)
        with native.transaction(owned, binding=binding) as tx:
            with pytest.raises(fault_type) as raised:
                tx.retire(SOURCE, RETAINED, raw, identity)
            assert raised.value is fault and not native.cleanup_unknown(fault)
            with pytest.raises(native.PreferenceStorageError):
                tx.retire(SOURCE, RETAINED, raw, identity)
    assert len(calls) == 1 and not native._POISONED
    _resume_without_rename(owned, binding, raw, identity, monkeypatch)


@pytest.mark.parametrize('fault_type', [OSError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('barrier', ['file_before', 'file_after', 'directory_after'])
def test_barrier_failure_retains_evidence_and_resumes_exact_operation(owned, monkeypatch, fault_type, barrier):
    binding, raw, identity = _plant(owned)
    fault = fault_type('owned-retirement-barrier')
    original_fsync, original_sync = os.fsync, native.storage._Directory.sync
    renamed, failed, selected = [], [], []
    def rename(original, args):
        result = original(*args)
        renamed.append(True)
        return result
    def fsync(fd):
        info = native.storage._file_information(fd)
        result = original_fsync(fd)
        if (info.st_dev, info.st_ino) == identity[:2]:
            selected.append(fd)
            wanted = (barrier == 'file_before' and not renamed) or (barrier == 'file_after' and renamed)
            if wanted and not failed:
                failed.append(True)
                raise fault
        return result
    def sync(directory):
        result = original_sync(directory)
        if barrier == 'directory_after' and renamed and directory.path == owned and not failed:
            failed.append(True)
            raise fault
        return result
    with monkeypatch.context() as patch:
        _rename_hook(patch, rename)
        patch.setattr(os, 'fsync', fsync)
        patch.setattr(native.storage._Directory, 'sync', sync)
        with pytest.raises(fault_type) as raised:
            with native.transaction(owned, binding=binding) as tx:
                tx.retire(SOURCE, RETAINED, raw, identity)
        assert raised.value is fault and not native.cleanup_unknown(fault)
    assert failed and not native._POISONED
    for fd in set(selected):
        with pytest.raises(OSError): os.fstat(fd)
    if barrier == 'file_before':
        assert not renamed and (owned / SOURCE[0]).read_bytes() == raw
        with native.transaction(owned, binding=binding) as tx:
            assert tx.retire(SOURCE, RETAINED, raw, identity)
    else:
        assert len(renamed) == 1 and (owned / RETAINED[0]).read_bytes() == raw
    _resume_without_rename(owned, binding, raw, identity, monkeypatch)


def test_retirement_close_uncertainty_never_recloses_reused_fd(owned, monkeypatch):
    binding, raw, identity = _plant(owned)
    sentinel = owned / 'owned-sentinel'
    sentinel.write_bytes(b'owned-sentinel')
    original_sync, original_close = os.fsync, os.close
    selected, attempts, retained_fd = [], [], []
    fault = KeyboardInterrupt('owned-retirement-close')
    def sync(fd):
        info = native.storage._file_information(fd)
        if (info.st_dev, info.st_ino) == identity[:2] and not selected: selected.append(fd)
        return original_sync(fd)
    def close(fd):
        if selected and fd == selected[0]:
            attempts.append(fd)
            if not retained_fd:
                original_close(fd)
                retained_fd.append(os.open(sentinel, os.O_RDONLY))
                assert retained_fd[0] == fd
                raise fault
        return original_close(fd)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, 'fsync', sync)
            patch.setattr(os, 'close', close)
            with pytest.raises(KeyboardInterrupt) as raised:
                with native.transaction(owned, binding=binding) as tx:
                    tx.retire(SOURCE, RETAINED, raw, identity)
            assert raised.value is fault and native.cleanup_unknown(fault)
            assert attempts == selected and os.read(retained_fd[0], 64) == b'owned-sentinel'
            patch.setattr(native.archive_io, 'selected_directory', lambda *args: pytest.fail('poisoned selector reached'))
            with pytest.raises(native.PreferenceStorageError):
                with native.transaction(owned): pytest.fail('uncertain authority reused')
        assert native._POISONED and not native._IN_FLIGHT
        assert not (owned / SOURCE[0]).exists() and (owned / RETAINED[0]).read_bytes() == raw
    finally:
        if retained_fd:
            fd = retained_fd.pop()
            original_close(fd)


@pytest.mark.skipif(os.name == 'nt', reason='Linux unavailable exclusive-rename responses')
@pytest.mark.parametrize('code', [errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP])
def test_unavailable_exclusive_rename_has_no_overwrite_or_unlink_fallback(owned, monkeypatch, code):
    binding, raw, identity = _plant(owned)
    calls = []
    def unavailable(*args):
        calls.append(True)
        raise OSError(code, 'owned-unavailable-rename')
    monkeypatch.setattr(native.storage, '_rename_exclusive', unavailable)
    with native.transaction(owned, binding=binding) as tx:
        with pytest.raises(OSError) as raised:
            tx.retire(SOURCE, RETAINED, raw, identity)
        assert raised.value.errno == code
        assert calls == [True]
        assert tx.read(SOURCE) == (raw, identity)
        assert tx.read(RETAINED) == (None, None)


@pytest.mark.parametrize('fault_type', [OSError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('boundary', ['file_flush', 'directory_confirmation', 'close'])
def test_already_retired_reconfirmation_fault_never_reports_success(owned, monkeypatch, fault_type, boundary):
    binding, raw, identity = _plant(owned)
    with native.transaction(owned, binding=binding) as tx:
        assert tx.retire(SOURCE, RETAINED, raw, identity)
    sentinel = owned / 'owned-confirmation-sentinel'
    sentinel.write_bytes(b'owned-confirmation-sentinel')
    fault = fault_type('owned-reconfirmation-fault')
    original_flush, original_close = os.fsync, os.close
    original_sync = native.storage._Directory.sync
    selected, failed, close_attempts, retained_fd = [], [], [], []
    def flush(fd):
        info = native.storage._file_information(fd)
        result = original_flush(fd)
        if (info.st_dev, info.st_ino) == identity[:2] and not selected:
            selected.append(fd)
            if boundary == 'file_flush':
                failed.append(True)
                raise fault
        return result
    def sync(directory):
        result = original_sync(directory)
        if selected and boundary == 'directory_confirmation' and not failed:
            failed.append(True)
            raise fault
        return result
    def close(fd):
        if selected and fd == selected[0]:
            close_attempts.append(fd)
            if boundary == 'close' and not failed:
                original_close(fd)
                retained_fd.append(os.open(sentinel, os.O_RDONLY))
                assert retained_fd[0] == fd
                failed.append(True)
                raise fault
        return original_close(fd)
    try:
        with monkeypatch.context() as patch:
            _rename_hook(patch, lambda *args: pytest.fail('reconfirmation attempted another rename'))
            patch.setattr(os, 'fsync', flush)
            patch.setattr(os, 'close', close)
            patch.setattr(native.storage._Directory, 'sync', sync)
            with pytest.raises(fault_type) as raised:
                with native.transaction(owned, binding=binding) as tx:
                    tx.retire(SOURCE, RETAINED, raw, identity)
            assert raised.value is fault and failed and selected
            assert native.cleanup_unknown(fault) is (boundary == 'close')
            assert native._POISONED is (boundary == 'close') and not native._IN_FLIGHT
            assert close_attempts == selected
            if boundary == 'close':
                assert os.read(retained_fd[0], 64) == b'owned-confirmation-sentinel'
                patch.setattr(native.archive_io, 'selected_directory', lambda *args: pytest.fail('poisoned selector reached'))
                with pytest.raises(native.PreferenceStorageError):
                    with native.transaction(owned): pytest.fail('uncertain confirmation authority reused')
            else:
                with pytest.raises(OSError): os.fstat(selected[0])
        assert not (owned / SOURCE[0]).exists() and (owned / RETAINED[0]).read_bytes() == raw
        if boundary != 'close':
            _resume_without_rename(owned, binding, raw, identity, monkeypatch)
        else:
            # Observe the owned retained file directly after the preference
            # process boundary is poisoned; do not reuse that boundary.
            observed_fd = os.open(owned / RETAINED[0], os.O_RDONLY)
            try:
                info = native.storage._file_information(observed_fd)
                assert native.storage._file_identity(info)[:2] == identity[:2]
            finally:
                closed, observed_fd = observed_fd, None
                original_close(closed)
    finally:
        if retained_fd:
            fd = retained_fd.pop()
            original_close(fd)

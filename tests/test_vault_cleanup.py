"""Owned native fault controls; never use providers or private user data."""
import os
from types import SimpleNamespace

import pytest

from olympus import config, native_cleanup, store, vault
from olympus import vault_native as vn, vault_storage as vs, vault_recovery as recovery


def _raise(error):
    raise error


def _finish(directory):
    """Drain an affected owner once; a prior uncertain result remains sticky."""
    with pytest.raises(vs.StorageError) as failure:
        directory.close()
    assert native_cleanup.unknown(failure.value)
    with pytest.raises(vs.StorageError) as repeated:
        directory.close()
    assert native_cleanup.unknown(repeated.value)


def _native_closer(directory):
    if directory.win is not None:
        from olympus import vault_windows as native
        return native, native.close, list(directory.win.handles[directory.win.owned_start:])
    return os, os.close, list(directory._parents[directory._owned_start:])


@pytest.mark.parametrize('operation', ['read', 'write', 'confirm'])
@pytest.mark.parametrize('fault', [OSError, KeyboardInterrupt, SystemExit])
def test_leaf_primary_survives_close_and_reused_descriptor(tmp_path, monkeypatch, operation, fault):
    (tmp_path/'key').write_bytes(b'owned-existing')
    (tmp_path/'reused').write_bytes(b'owned-reused')
    directory = vn.Directory(tmp_path)
    primary, secondary = fault('owned-primary'), SystemExit('owned-secondary-close')
    original_open, original_close = directory._open, os.close
    original_read, original_write, original_sync = os.read, os.write, os.fsync
    target, replacement, closed = [], [], []

    def opened(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        target.append(fd)
        return fd

    def close(fd):
        original_close(fd)
        if fd in target:
            closed.append(fd)
            replacement.append(os.open(tmp_path/'reused', os.O_RDONLY))
            raise secondary

    def read(fd, size):
        return _raise(primary) if fd in target else original_read(fd, size)

    def write(fd, data):
        return _raise(primary) if fd in target else original_write(fd, data)

    def sync(fd):
        return _raise(primary) if fd in target else original_sync(fd)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(directory, '_open', opened)
            patch.setattr(os, 'close', close)
            patch.setattr(os, {'read':'read','write':'write','confirm':'fsync'}[operation],
                          {'read':read,'write':write,'confirm':sync}[operation])
            with pytest.raises(fault) as failure:
                if operation == 'read': directory.read('key', 100)
                elif operation == 'write': directory.write_new('new', b'owned-new')
                else: directory.confirm('key')
            assert failure.value is primary and native_cleanup.unknown(primary)
            assert closed == target and len(target) == 1
            # Closing a numeric value a second time would close this newly
            # acquired owned descriptor. No cleanup retry may touch it.
            assert replacement == target
            assert os.fstat(replacement[0]).st_size == len(b'owned-reused')
            with pytest.raises(vs.StorageError) as refusal:
                directory.sync()
            assert native_cleanup.unknown(refusal.value)
        _finish(directory)
        assert os.fstat(replacement[0]).st_size == len(b'owned-reused')
    finally:
        for fd in replacement: original_close(fd)
        if not directory._closed: _finish(directory)


@pytest.mark.parametrize('fault', [FileNotFoundError, KeyboardInterrupt, SystemExit])
def test_cleanup_only_failure_escapes_and_is_sticky(tmp_path, monkeypatch, fault):
    (tmp_path/'key').write_bytes(b'owned')
    directory=vn.Directory(tmp_path)
    target=[]; original_open=directory._open; original_close=os.close
    error=fault('owned-close-only')
    def opened(*args,**kwargs):
        fd=original_open(*args,**kwargs);target.append(fd);return fd
    def close(fd):
        original_close(fd)
        if fd in target:raise error
    with monkeypatch.context() as patch:
        patch.setattr(directory,'_open',opened);patch.setattr(os,'close',close)
        with pytest.raises(fault) as failure:directory.read('key',100)
    assert failure.value is error and native_cleanup.unknown(error)
    _finish(directory)


@pytest.mark.parametrize('fault', [OSError, KeyboardInterrupt, SystemExit])
def test_constructor_primary_survives_independent_ancestor_drain(tmp_path, monkeypatch, fault):
    primary=fault('owned-constructor');owners=[];closed=[]
    if os.name=='nt':
        from olympus import vault_windows as closer
    else:closer=os
    original_close=closer.close
    def close(handle):
        original_close(handle);closed.append(handle)
        if len(closed)==1:raise SystemExit('owned-secondary')
    def check(directory):
        owners.append((directory,_native_closer(directory)[2]))
        raise primary
    with monkeypatch.context() as patch:
        patch.setattr(vn.Directory,'check',check);patch.setattr(closer,'close',close)
        with pytest.raises(fault) as failure:vn.Directory(tmp_path)
    assert failure.value is primary and native_cleanup.unknown(primary)
    assert len(owners)==1 and closed==list(reversed(owners[0][1]))
    directory=owners[0][0]
    with monkeypatch.context() as patch:
        patch.setattr(closer,'close',lambda *a:pytest.fail('closed numeric handle twice'))
        _finish(directory)


@pytest.mark.parametrize('fault', [OSError, KeyboardInterrupt, SystemExit])
def test_child_primary_survives_close_and_taints_parent(tmp_path, monkeypatch, fault):
    (tmp_path/'child').mkdir()
    parent=vn.Directory(tmp_path);original_check=vn.Directory.check
    closer, original_close, _ = _native_closer(parent)
    primary=fault('owned-child-check');closed=[]
    def check(directory):
        if directory.path==tmp_path/'child':raise primary
        return original_check(directory)
    def close(handle):
        original_close(handle);closed.append(handle);raise OSError('owned-close')
    with monkeypatch.context() as patch:
        patch.setattr(vn.Directory,'check',check);patch.setattr(closer,'close',close)
        with pytest.raises(fault) as failure:parent.child('child')
    assert failure.value is primary and native_cleanup.unknown(primary) and len(closed)==1
    with pytest.raises(vs.StorageError) as refusal:parent.check()
    assert native_cleanup.unknown(refusal.value)
    _finish(parent)


class _ClosingIterator:
    def __init__(self, iterator, error, calls):
        self.iterator,self.error,self.calls=iterator,error,calls
    def __iter__(self):return self
    def __next__(self):return next(self.iterator)
    def close(self):
        self.calls.append('close')
        self.iterator.close()
        raise self.error


def _fault_iterator(directory, monkeypatch, error, calls):
    if directory.win is not None:
        original=directory.win.entries
        monkeypatch.setattr(directory.win,'entries',lambda:_ClosingIterator(original(),error,calls))
    else:
        original=os.scandir
        monkeypatch.setattr(os,'scandir',lambda fd:_ClosingIterator(original(fd),error,calls)
                            if fd==directory.fd else original(fd))


@pytest.mark.parametrize('fault', [OSError, KeyboardInterrupt, SystemExit])
def test_consumer_primary_survives_iterator_close(tmp_path, monkeypatch, fault):
    primary=fault('owned-consumer');calls=[]
    with pytest.raises(vs.StorageError if fault is OSError else fault) as failure:
        with vs.transaction(store.FileStore(),tmp_path) as tx:
            tx.put('vault','key',b'owned')
            directory=tx.space('vault')
            with monkeypatch.context() as patch:
                _fault_iterator(directory,patch,SystemExit('owned-iterator-close'),calls)
                original=vs._address
                patch.setattr(vs,'_address',lambda namespace,key:_raise(primary) if key=='key'
                              else original(namespace,key))
                tx.keys('vault')
    assert (failure.value.__cause__ is primary if fault is OSError else failure.value is primary)
    assert native_cleanup.unknown(primary) and native_cleanup.unknown(failure.value)
    assert calls==['close'] and directory._iterators==[]
    assert not tx._lease.active and tx.fd is None


def test_explicit_generator_close_cannot_suppress_cleanup_failure(tmp_path,monkeypatch):
    (tmp_path/'key').write_bytes(b'owned')
    directory=vn.Directory(tmp_path);calls=[];error=OSError('owned-iterator-close')
    with monkeypatch.context() as patch:
        _fault_iterator(directory,patch,error,calls)
        entries=directory.entries();assert next(entries).name=='key'
        with pytest.raises(OSError) as failure:entries.close()
    assert failure.value is error and native_cleanup.unknown(error)
    assert calls==['close'] and directory._iterators==[]
    _finish(directory)
    assert calls==['close']


@pytest.mark.parametrize('fault', [OSError, KeyboardInterrupt, SystemExit])
def test_transaction_primary_survives_unlock_fd_and_directory_faults(tmp_path,monkeypatch,fault):
    primary=fault('owned-transaction-body');closed=[];directories=[]
    original_unlock,original_close,original_directory_close=vs._unlock,os.close,vn.Directory.close
    selected=[]
    def unlock(fd):original_unlock(fd);raise OSError('owned-unlock')
    def close(fd):
        original_close(fd)
        if fd in selected:closed.append(fd);raise SystemExit('owned-fd-close')
    def directory_close(directory):
        original_directory_close(directory);directories.append(directory.path)
        if len(directories)==1:raise KeyboardInterrupt('owned-directory-close')
    with pytest.raises(vs.StorageError if fault is OSError else fault) as failure:
        with monkeypatch.context() as patch:
            patch.setattr(vs,'_unlock',unlock);patch.setattr(os,'close',close)
            patch.setattr(vn.Directory,'close',directory_close)
            with vs.transaction(store.FileStore(),tmp_path) as tx:
                tx.put('vault','key',b'owned');selected.append(tx.fd)
                raise primary
    assert (failure.value.__cause__ is primary if fault is OSError else failure.value is primary)
    assert native_cleanup.unknown(primary) and native_cleanup.unknown(failure.value)
    assert closed==selected and len(directories)==3 and tx.directories==[]
    assert vs._LOCAL.current is None and not tx._lease.active
    with monkeypatch.context() as patch:
        patch.setattr(os,'close',lambda *a:pytest.fail('detached fd retried'))
        with pytest.raises(vs.StorageError) as repeated:tx.close()
    assert native_cleanup.unknown(repeated.value)
    with vs.transaction(store.FileStore(),tmp_path,timeout=0) as fresh:
        assert fresh.get('vault','key')==b'owned'


@pytest.mark.parametrize('flag', [True, False, 1])
def test_only_exact_cleanup_fact_prevents_optional_absence(tmp_path,monkeypatch,flag):
    error=FileNotFoundError('owned-initial-lookup')
    error.__dict__['_native_cleanup_unconfirmed']=flag
    with pytest.raises(vs.StorageError) if flag is True else _succeeds():
        with vs.transaction(store.FileStore(),tmp_path) as tx:
            directory=tx.space('vault')
            monkeypatch.setattr(directory,'read',lambda *a:_raise(error))
            if flag is True:
                with pytest.raises(FileNotFoundError) as failure:tx.get('vault','key')
                assert failure.value is error
                with pytest.raises(vs.StorageError) as refusal:tx.keys('vault')
                assert native_cleanup.unknown(refusal.value)
            else:assert tx.get('vault','key') is None


def _succeeds():
    from contextlib import nullcontext
    return nullcontext()


@pytest.mark.skipif(os.name!='posix',reason='POSIX post-read pathname observation')
def test_post_read_disappearance_is_not_initial_absence(tmp_path,monkeypatch):
    (tmp_path/'key').write_bytes(b'owned')
    directory=vn.Directory(tmp_path);original=os.stat
    def observed(path,*args,**kwargs):
        if path=='key' and kwargs.get('dir_fd')==directory.fd:raise FileNotFoundError('owned-disappearance')
        return original(path,*args,**kwargs)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(os,'stat',observed)
            with pytest.raises(vs.StorageError) as failure:directory.read('key',100)
        assert not native_cleanup.unknown(failure.value)
        assert directory.read('key',100)==b'owned'
    finally:directory.close()


@pytest.mark.skipif(os.name!='nt',reason='actual Windows handle acquisition')
@pytest.mark.parametrize('operation', ['get', 'delete', 'publish'])
@pytest.mark.parametrize('close_fault', [False, True])
def test_acquired_windows_handle_disappearance_never_becomes_absence(tmp_path,monkeypatch,operation,close_fault):
    from olympus import vault_windows as native
    path=tmp_path/'key';path.write_bytes(b'owned-preserved')
    directory=vn.Directory(tmp_path);original_information=native.information;original_close=native.close
    closes=[];observed=[];error=FileNotFoundError('owned-after-acquisition')
    def information(handle,directory_arg=None):
        if directory_arg is False:
            observed.append(handle)
            # Publication must reach the destination probe after retaining its
            # staging bytes; otherwise this would test only staging failure.
            if len(observed)==(2 if operation=='publish' else 1):raise error
        return original_information(handle,directory_arg)
    def close(handle):
        original_close(handle);closes.append(handle)
        if close_fault:raise SystemExit('owned-close')
    with monkeypatch.context() as patch:
        patch.setattr(native,'information',information);patch.setattr(native,'close',close)
        with pytest.raises(vs.StorageError) as failure:
            if operation=='get':directory.read('key',100)
            elif operation=='delete':directory.delete('key')
            else:directory.publish('key',b'forbidden')
    assert len(closes)==1 and native_cleanup.unknown(failure.value) is close_fault
    assert failure.value.__cause__ is error
    assert len(observed)==(2 if operation=='publish' else 1)
    assert path.read_bytes()==b'owned-preserved'
    if close_fault:_finish(directory)
    else:directory.close()


@pytest.mark.skipif(os.name!='nt',reason='actual Windows delegated handle cleanup')
@pytest.mark.parametrize('operation', ['relative', 'anchor', 'to_fd', 'entry'])
@pytest.mark.parametrize('fault', [OSError, KeyboardInterrupt, SystemExit])
def test_native_acquisition_preserves_primary_through_close(tmp_path,monkeypatch,operation,fault):
    import msvcrt
    from olympus import vault_windows as native
    (tmp_path/'key').write_bytes(b'owned')
    directory=vn.Directory(tmp_path);primary=fault('owned-native-primary');closed=[]
    original_information,original_close=native.information,native.close
    handle=native.open_relative(directory.win.handle,'key') if operation=='to_fd' else None
    seen=[]
    def information(handle,*args):
        seen.append(handle)
        if operation!='entry' or len(seen)==2:raise primary
        return original_information(handle,*args)
    def close(value):
        nonlocal handle
        if handle==value:handle=None
        original_close(value);closed.append(value);raise SystemExit('owned-secondary')
    test_primary=None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(native,'close',close)
            if operation=='to_fd':patch.setattr(msvcrt,'open_osfhandle',lambda *a:_raise(primary))
            else:patch.setattr(native,'information',information)
            with pytest.raises(fault) as failure:
                if operation=='relative':native.open_relative(directory.win.handle,'key')
                elif operation=='anchor':native._anchor(tmp_path.anchor)
                elif operation=='to_fd':native.to_fd(handle,os.O_RDONLY)
                else:directory._entry_information('key',0)
        assert failure.value is primary and native_cleanup.unknown(primary)
        if operation=='to_fd':
            assert closed==[]
            original_information(handle,False)
        else:
            assert len(closed)==1
    except BaseException as error:
        test_primary=error;raise
    finally:
        # This fixture's injected transfer never dispatched to CRT. Detach raw
        # ownership before one close even if an earlier assertion failed.
        retained,handle=handle,None
        actions=([lambda:original_close(retained)] if retained is not None else [])
        actions.append(lambda:_finish(directory) if directory._cleanup_unconfirmed else directory.close())
        native_cleanup.drain(actions,test_primary)


@pytest.mark.parametrize('flag', [True, False, 1])
@pytest.mark.parametrize('kind', ['ordinary', 'recovery'])
def test_public_cleanup_transport_is_exact_and_redacted(flag,kind):
    operation_id='a'*32
    error=OSError('owned-private-detail') if kind=='ordinary' else recovery.RecoveryError(operation_id,'owned-private-detail')
    error.__dict__['_native_cleanup_unconfirmed']=flag
    error.add_note('owned-private-note')
    guarded=vault._guard(lambda:_raise(error))
    with pytest.raises(vault.VaultError) as failure:guarded()
    assert native_cleanup.unknown(failure.value) is (flag is True)
    assert failure.value.__cause__ is None and failure.value.__context__ is None
    assert 'owned-private' not in str(failure.value) and not getattr(failure.value,'__notes__',[])
    if kind=='recovery':assert failure.value.operation_id==operation_id


@pytest.mark.parametrize('fault', [KeyboardInterrupt, SystemExit])
def test_public_interrupt_object_is_preserved(fault):
    error=fault('owned-interrupt');native_cleanup.mark(error)
    with pytest.raises(fault) as failure:vault._guard(lambda:_raise(error))()
    assert failure.value is error and native_cleanup.unknown(failure.value)


@pytest.mark.parametrize('operation', ['prepare', 'apply'])
def test_recovery_error_retains_id_and_cleanup_fact(operation):
    error=OSError('owned-private-recovery');native_cleanup.mark(error)
    tx=SimpleNamespace(get=lambda *a,**k:_raise(error));operation_id='b'*32
    with pytest.raises(recovery.RecoveryError) as failure:
        if operation=='prepare':
            recovery.prepare(tx,b'owned-passphrase',operation_id=operation_id,kind='discard',
                             source_key='legacy',source_blob=b'owned-ciphertext')
        else:recovery.apply(tx,b'owned-passphrase',operation_id)
    assert failure.value.operation_id==operation_id and native_cleanup.unknown(failure.value)
    assert 'owned-private' not in str(failure.value)


@pytest.mark.parametrize('fault', [OSError, KeyboardInterrupt, SystemExit])
def test_pending_cleanup_refuses_before_another_acquisition(tmp_path,monkeypatch,fault):
    monkeypatch.setattr(config,'MEMORY_DIR',tmp_path/'memory')
    monkeypatch.setattr(store,'_backend',store.FileStore())
    monkeypatch.delenv('OLYMPUS_SECRET_KEY_FILE',raising=False)
    monkeypatch.delenv('OLYMPUS_DATABASE_URL',raising=False)
    monkeypatch.setenv('OLYMPUS_SECRET_KEY','owned-synthetic-cleanup-key')
    monkeypatch.setenv('OLYMPUS_ENV','test')
    vault.put('owned-owner','entry','before')
    selected=vault.snapshot('owned-owner','entry',for_update=True)
    pending=vault.prepare_snapshot_update(selected,'after')
    original=vs._unlock;primary=fault('owned-unlock-ack')
    def unlock(fd):original(fd);raise primary
    with monkeypatch.context() as patch:
        patch.setattr(vs,'_unlock',unlock)
        with pytest.raises(vault.VaultError if fault is OSError else fault) as failure:
            vault.settle_snapshot_update(pending)
    assert native_cleanup.unknown(failure.value) and pending._ciphertext is not None
    if fault is not OSError:assert failure.value is primary
    with monkeypatch.context() as patch:
        patch.setattr(vs,'transaction',lambda *a,**k:pytest.fail('uncertain capture reacquired authority'))
        for action in (lambda:vault.settle_snapshot_update(pending),lambda:vault.require_snapshot_writable(selected),
                       lambda:vault.prepare_snapshot_update(selected,'another')):
            with pytest.raises(vault.VaultError) as refusal:action()
            assert native_cleanup.unknown(refusal.value)
    # Fresh, independently captured authority remains permitted after an actual
    # unlock/close whose acknowledgment fault was injected by this test.
    assert vault.get('owned-owner','entry')=='after'


@pytest.mark.skipif(os.name!='nt',reason='Windows final delete-handle cleanup')
@pytest.mark.parametrize('fault', [OSError, KeyboardInterrupt, SystemExit])
def test_windows_delete_body_failure_survives_final_close(tmp_path,monkeypatch,fault):
    from olympus import vault_windows as native
    path=tmp_path/'key';path.write_bytes(b'owned-preserved')
    directory=vn.Directory(tmp_path);primary=fault('owned-delete-body')
    original_information,original_close=native.information,native.close
    seen=[];closed=[]
    def information(handle,*args):
        seen.append(handle)
        if len(seen)==2:raise primary
        return original_information(handle,*args)
    def close(handle):
        original_close(handle);closed.append(handle);raise SystemExit('owned-delete-close')
    with monkeypatch.context() as patch:
        patch.setattr(native,'information',information);patch.setattr(native,'close',close)
        with pytest.raises(fault) as failure:directory.delete('key')
    assert failure.value is primary and native_cleanup.unknown(primary)
    assert len(seen)==2 and closed==[seen[0]] and seen[0]==seen[1]
    assert path.read_bytes()==b'owned-preserved'
    _finish(directory)


@pytest.mark.skipif(os.name!='posix',reason='POSIX descriptor close before unlink')
@pytest.mark.parametrize('fault', [OSError, KeyboardInterrupt, SystemExit])
def test_posix_uncertain_preunlink_close_never_deletes(tmp_path,monkeypatch,fault):
    path=tmp_path/'key';path.write_bytes(b'owned-preserved')
    directory=vn.Directory(tmp_path);error=fault('owned-preunlink-close')
    original_open,original_close=directory._open,os.close
    selected=[];closed=[]
    def opened(*args,**kwargs):
        fd=original_open(*args,**kwargs);selected.append(fd);return fd
    def close(fd):
        original_close(fd)
        if fd in selected:closed.append(fd);raise error
    with monkeypatch.context() as patch:
        patch.setattr(directory,'_open',opened);patch.setattr(os,'close',close)
        patch.setattr(os,'unlink',lambda *a,**k:pytest.fail('uncertain descriptor authorized unlink'))
        with pytest.raises(fault) as failure:directory.delete('key')
    assert failure.value is error and native_cleanup.unknown(error)
    assert len(selected)==1 and closed==selected and path.read_bytes()==b'owned-preserved'
    _finish(directory)

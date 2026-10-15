"""Owned Windows handle-output and CRT transfer controls; no guessed cleanup."""
import ctypes as C
import os
from types import SimpleNamespace
import pytest
from olympus import native_cleanup, vault_native as vn, vault_storage as storage

pytestmark=pytest.mark.skipif(os.name!='nt',reason='Windows native output and CRT ownership controls')


@pytest.fixture
def owned(tmp_path):
    from olympus import vault_windows as native
    (tmp_path/'key').write_bytes(b'owned handle fixture')
    directory=vn.Directory(tmp_path)
    raw,descriptors=[],[]
    actual_close=native.close
    def acquire():
        handle=native.open_relative(directory.win.handle,'key');raw.append(handle);return handle
    def closed(handle):
        raw.remove(handle);actual_close(handle)
    state=SimpleNamespace(directory=directory,raw=raw,descriptors=descriptors,acquire=acquire,closed=closed)
    primary=None
    try:yield state
    except BaseException as error:
        primary=error;raise
    finally:
        # Only the fixture knows these exact outcomes; detach each once and
        # drain independently. Pytest also retains separate teardown failures.
        pending_fds,pending_raw=list(descriptors),list(raw)
        descriptors.clear();raw.clear()
        actions=[lambda fd=fd:os.close(fd) for fd in pending_fds]
        actions.extend(lambda handle=handle:actual_close(handle) for handle in pending_raw)
        if not directory._closed:actions.append(directory.close)
        native_cleanup.drain(actions,primary)


@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
@pytest.mark.parametrize('secondary',[False,True])
@pytest.mark.parametrize('status_failure',[False,True])
def test_relative_populated_output_is_drained_once_and_preserves_primary(owned,monkeypatch,fault,secondary,status_failure):
    from olympus import vault_windows as native
    handle=owned.acquire();primary=fault('owned API failure');calls=[]
    def call(output,*args):
        C.cast(output,C.POINTER(native.HANDLE)).contents.value=handle
        if status_failure:return -1
        raise primary
    def failed_status(code):raise primary
    def close(value):
        owned.closed(value);calls.append(value)
        if secondary:raise SystemExit('owned close failure')
    with monkeypatch.context() as patch:
        patch.setattr(native,'_apis',lambda:(None,SimpleNamespace(NtCreateFile=call)))
        patch.setattr(native,'_error',failed_status);patch.setattr(native,'close',close)
        with pytest.raises(fault) as caught:native.open_relative(owned.directory.win.handle,'key')
    assert caught.value is primary and native_cleanup.unknown(primary) is secondary
    assert calls==[handle] and handle not in owned.raw


@pytest.mark.parametrize('populated',[False,True])
@pytest.mark.parametrize('secondary',[False,True])
def test_initial_absence_is_distinct_from_populated_output_failure(owned,monkeypatch,populated,secondary):
    from olympus import vault_windows as native
    handle=owned.acquire() if populated else None;primary=FileNotFoundError('owned missing');calls=[]
    def call(output,*args):
        C.cast(output,C.POINTER(native.HANDLE)).contents.value=handle
        return -1
    def status_error(code):raise primary
    def close(value):
        owned.closed(value);calls.append(value)
        if secondary:raise KeyboardInterrupt('owned secondary')
    with monkeypatch.context() as patch:
        patch.setattr(native,'_apis',lambda:(None,SimpleNamespace(NtCreateFile=call)))
        patch.setattr(native,'_error',status_error)
        patch.setattr(native,'close',close)
        with pytest.raises(storage.StorageError if populated else FileNotFoundError) as caught:
            native.open_relative(owned.directory.win.handle,'key')
    assert native_cleanup.unknown(caught.value) is (populated and secondary)
    if populated:assert caught.value.__cause__ is primary and calls==[handle]
    else:assert caught.value is primary and calls==[]


@pytest.mark.parametrize('value',[None,0,-1])
def test_relative_success_without_valid_output_refuses_without_numeric_close(owned,monkeypatch,value):
    from olympus import vault_windows as native
    def call(output,*args):
        C.cast(output,C.POINTER(native.HANDLE)).contents.value=value
        return 0
    with monkeypatch.context() as patch:
        patch.setattr(native,'_apis',lambda:(None,SimpleNamespace(NtCreateFile=call)))
        patch.setattr(native,'information',lambda *a:pytest.fail('invalid output inspected'))
        patch.setattr(native,'close',lambda *a:pytest.fail('invalid output closed'))
        with pytest.raises(storage.StorageError) as caught:native.open_relative(owned.directory.win.handle,'key')
    assert native_cleanup.unknown(caught.value)


@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
def test_anchor_lost_return_marks_uncertainty_without_guessing_resource(owned,monkeypatch,fault):
    from olympus import vault_windows as native
    primary=fault('owned lost return');calls=[]
    kernel,_=native._apis()
    def call(*args):
        handle=kernel.CreateFileW(*args)
        assert native._valid_handle(handle)
        owned.raw.append(handle);calls.append(handle)
        raise primary
    with monkeypatch.context() as patch:
        patch.setattr(native,'_apis',lambda:(SimpleNamespace(CreateFileW=call),None))
        patch.setattr(native,'close',lambda *a:pytest.fail('unobserved output guessed'))
        with pytest.raises(fault) as caught:native._anchor(owned.directory.path)
    assert caught.value is primary and native_cleanup.unknown(primary) and len(calls)==1
    native.information(calls[0],True)


@pytest.mark.parametrize('value',[None,0,-1])
def test_anchor_explicit_invalid_return_never_closes_sentinel(owned,monkeypatch,value):
    from olympus import vault_windows as native
    returned=native.HANDLE(value).value
    with monkeypatch.context() as patch:
        patch.setattr(native,'_apis',lambda:(SimpleNamespace(CreateFileW=lambda *a:returned),None))
        patch.setattr(native,'close',lambda *a:pytest.fail('invalid output closed'))
        patch.setattr(native,'information',lambda *a:pytest.fail('invalid output inspected'))
        with pytest.raises((OSError,storage.StorageError)) as caught:native._anchor(owned.directory.path.anchor)
    assert native_cleanup.unknown(caught.value) is (value != -1)


@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
@pytest.mark.parametrize('transferred',[False,True])
def test_transfer_dispatch_error_never_raw_closes_or_retries(owned,monkeypatch,fault,transferred):
    import msvcrt
    from olympus import vault_windows as native
    handle=owned.acquire();primary=fault('owned lost transfer return');calls=[]
    real_transfer=msvcrt.open_osfhandle
    def transfer(value,mode):
        calls.append(value)
        if transferred:
            fd=real_transfer(value,mode);owned.raw.remove(value);owned.descriptors.append(fd)
        raise primary
    with monkeypatch.context() as patch:
        patch.setattr(msvcrt,'open_osfhandle',transfer)
        patch.setattr(native,'close',lambda *a:pytest.fail('ambiguous transfer raw-closed'))
        with pytest.raises(fault) as caught:native.to_fd(handle,os.O_RDONLY)
    assert caught.value is primary and native_cleanup.unknown(primary) and calls==[handle]
    if transferred:
        fd=owned.descriptors[0]
        assert msvcrt.get_osfhandle(fd)==handle and os.read(fd,100)==b'owned handle fixture'
    else:native.information(handle,False)


@pytest.mark.parametrize('value',[None,False,True,-1])
def test_transfer_invalid_return_refuses_without_raw_close(owned,monkeypatch,value):
    import msvcrt
    from olympus import vault_windows as native
    handle=owned.acquire();calls=[]
    def transfer(*args):calls.append(1);return value
    with monkeypatch.context() as patch:
        patch.setattr(msvcrt,'open_osfhandle',transfer)
        patch.setattr(native,'close',lambda *a:pytest.fail('unconfirmed transfer raw-closed'))
        with pytest.raises(storage.StorageError) as caught:native.to_fd(handle,os.O_RDONLY)
    assert native_cleanup.unknown(caught.value) and calls==[1]
    native.information(handle,False)


def test_descriptor_zero_is_accepted_as_a_return_value(owned,monkeypatch):
    import msvcrt
    from olympus import vault_windows as native
    handle=owned.acquire()
    # Return-domain control only; fixture keeps the real raw resource.
    monkeypatch.setattr(msvcrt,'open_osfhandle',lambda *a:0)
    assert native.to_fd(handle,os.O_RDONLY)==0


@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
@pytest.mark.parametrize('secondary',[False,True])
def test_local_preparation_failure_drains_known_raw_ownership(owned,monkeypatch,fault,secondary):
    import msvcrt
    from olympus import vault_windows as native
    handle=owned.acquire();primary=fault('owned preparation failure');calls=[]
    class Flags:
        def __and__(self,other):raise primary
    def close(value):
        owned.closed(value);calls.append(value)
        if secondary:raise SystemExit('owned secondary')
    with monkeypatch.context() as patch:
        patch.setattr(native,'close',close)
        patch.setattr(msvcrt,'open_osfhandle',lambda *a:pytest.fail('dispatched after preparation failed'))
        with pytest.raises(fault) as caught:native.to_fd(handle,Flags())
    assert caught.value is primary and native_cleanup.unknown(primary) is secondary and calls==[handle]


@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
def test_directory_owner_retains_ambiguous_transfer_and_unrelated_owner_stays_usable(owned,tmp_path,monkeypatch,fault):
    import msvcrt
    from olympus import vault_windows as native
    primary=fault('owned ambiguous transfer');actual=msvcrt.open_osfhandle;calls=[]
    def transfer(handle,mode):
        fd=actual(handle,mode);owned.descriptors.append(fd);calls.append(handle);raise primary
    with monkeypatch.context() as patch:
        patch.setattr(msvcrt,'open_osfhandle',transfer)
        patch.setattr(native,'close',lambda *a:pytest.fail('transferred handle raw-closed'))
        with pytest.raises(fault) as caught:owned.directory.win.open_fd('key',os.O_RDONLY)
    assert caught.value is primary and native_cleanup.unknown(primary) and len(calls)==1
    with pytest.raises(storage.StorageError) as refused:owned.directory.check()
    assert native_cleanup.unknown(refused.value)
    with pytest.raises(storage.StorageError) as refused:owned.directory.close()
    assert native_cleanup.unknown(refused.value) and owned.directory._closed
    # Preserve the uncertainty on this owner, but let the fixture finish safely.
    fresh=vn.Directory(tmp_path)
    try:assert fresh.read('key',100)==b'owned handle fixture'
    finally:fresh.close()
    assert owned.directory._cleanup_unconfirmed


def test_actual_transfer_success_returns_live_descriptor_with_single_fixture_owner(owned):
    import msvcrt
    from olympus import vault_windows as native
    handle=owned.acquire()
    fd=native.to_fd(handle,os.O_RDONLY)
    owned.raw.remove(handle);owned.descriptors.append(fd)
    assert type(fd) is int and fd>=0 and msvcrt.get_osfhandle(fd)==handle
    assert os.read(fd,100)==b'owned handle fixture'


@pytest.mark.parametrize('fault',[OSError,FileNotFoundError,KeyboardInterrupt,SystemExit])
def test_relative_raised_call_with_no_output_is_unconfirmed_not_clean_absence(owned,monkeypatch,fault):
    from olympus import vault_windows as native
    primary=fault('owned unobserved call');calls=[]
    def call(*args):calls.append('call');raise primary
    with monkeypatch.context() as patch:
        patch.setattr(native,'_apis',lambda:(None,SimpleNamespace(NtCreateFile=call)))
        patch.setattr(native,'close',lambda *a:pytest.fail('unobserved resource guessed'))
        with pytest.raises(fault) as caught:native.open_relative(owned.directory.win.handle,'key')
    assert caught.value is primary and native_cleanup.unknown(primary) and calls==['call']

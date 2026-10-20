"""Owned custody lifetime faults; synthetic material and API outputs only."""
import ctypes as C
import os
from types import SimpleNamespace
import pytest
from olympus import native_cleanup, vault_custody as custody, vault_native as native

MATERIAL=b'owned-custody-cleanup-material'


def key(path):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_BINARY',0),0o600)
    try:assert os.write(fd,MATERIAL)==len(MATERIAL)
    finally:os.close(fd)
    return path


def selected(path):
    return {'OLYMPUS_SECRET_KEY_FILE':str(path),'OLYMPUS_ENV':'test'}


@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
@pytest.mark.parametrize('flag',[False,True])
def test_capture_retains_only_fixed_cleanup_fact_and_actual_interrupt(monkeypatch,fault,flag):
    error=fault('owned-private-diagnostic');error.__notes__ = ['owned-private-note']
    if flag:native_cleanup.mark(error)
    def fail(*args):raise error
    monkeypatch.setattr(custody,'_read_file',fail)
    expected=custody.CustodyError if fault is OSError else fault
    with pytest.raises(expected) as caught:custody.capture(selected('owned-private-path'))
    assert native_cleanup.unknown(caught.value) is flag
    if fault is OSError:
        assert caught.value is not error and caught.value.__cause__ is None and caught.value.__context__ is None
        assert vars(caught.value)==({'_native_cleanup_unconfirmed':True} if flag else {})
        assert str(caught.value)==custody._FAILURE
    else:assert caught.value is error


@pytest.mark.parametrize('value',[1,'true',None])
def test_capture_never_promotes_truthy_nonboolean_cleanup_attributes(monkeypatch,value):
    error=OSError('owned-private');error._native_cleanup_unconfirmed=value
    def fail(*args):raise error
    monkeypatch.setattr(custody,'_read_file',fail)
    with pytest.raises(custody.CustodyError) as caught:custody.capture(selected('owned-key'))
    assert not native_cleanup.unknown(caught.value) and vars(caught.value)=={}


@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
@pytest.mark.parametrize('directory_fault',[False,True])
def test_read_primary_survives_closed_reused_fd_and_independent_directory_drain(tmp_path,monkeypatch,fault,directory_fault):
    path=key(tmp_path/'key');spare_path=key(tmp_path/'spare')
    primary=fault('owned-read-primary');selected_fds=[];closed=[];spares=[];directories=[]
    original_close,original_open,original_directory_close=os.close,os.open,native.Directory.close
    def read(fd,size):selected_fds.append(fd);raise primary
    def close(fd):
        original_close(fd)
        if fd in selected_fds:
            closed.append(fd)
            spare=original_open(spare_path,os.O_RDONLY|getattr(os,'O_BINARY',0));spares.append(spare)
            assert spare==fd  # Prove a second close would affect a newly owned descriptor.
            raise SystemExit('owned-secondary-file-close')
    def close_directory(directory):
        original_directory_close(directory);directories.append(directory)
        if directory_fault:raise KeyboardInterrupt('owned-secondary-directory-close')
    try:
        with monkeypatch.context() as scope:
            scope.setattr(os,'read',read);scope.setattr(os,'close',close)
            scope.setattr(native.Directory,'close',close_directory)
            with pytest.raises(fault) as caught:custody._read_file(path,False)
        assert caught.value is primary and native_cleanup.unknown(primary)
        assert len(selected_fds)==1 and closed==selected_fds and spares==selected_fds
        assert len(directories)==1 and directories[0]._closed
        assert directories[0].fd is None and directories[0].win is None
        assert os.fstat(spares[0]).st_size==len(MATERIAL)
        assert path.read_bytes()==MATERIAL and custody.capture(selected(path)).material==MATERIAL
    finally:
        for fd in spares:original_close(fd)


@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
@pytest.mark.parametrize('boundary',['file','directory'])
def test_cleanup_only_cannot_return_material_and_does_not_poison_fresh_capture(tmp_path,monkeypatch,fault,boundary):
    path=key(tmp_path/'key');error=fault('owned-close-only');fds=[];attempts=[];directories=[]
    real_info,real_close,real_directory_close=custody._information,os.close,native.Directory.close
    def information(fd):
        if fd not in fds:fds.append(fd)
        return real_info(fd)
    def close(fd):
        real_close(fd)
        if boundary=='file' and fd in fds:attempts.append(fd);raise error
    def directory_close(directory):
        real_directory_close(directory);directories.append(directory)
        if boundary=='directory':attempts.append(directory);raise error
    with monkeypatch.context() as scope:
        scope.setattr(custody,'_information',information);scope.setattr(os,'close',close)
        scope.setattr(native.Directory,'close',directory_close)
        with pytest.raises(fault) as caught:custody._read_file(path,False)
    assert caught.value is error and native_cleanup.unknown(error)
    assert len(attempts)==1 and len(directories)==1 and directories[0]._closed
    assert directories[0].fd is None and directories[0].win is None
    for fd in fds:
        with pytest.raises(OSError):os.fstat(fd)
    assert custody.capture(selected(path)).material==MATERIAL


@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
@pytest.mark.parametrize('stage',['thread','process','information'])
def test_populated_token_is_drained_once_when_acquisition_or_read_raises(monkeypatch,fault,stage):
    primary=fault('owned-token-primary');tokens=[];closed=[];process_calls=[]
    def thread(_thread,_rights,_self,output):
        if stage=='process':return False
        output._obj.value=101;tokens.append(output._obj)
        if stage=='thread':raise primary
        return True
    def process(_process,_rights,output):
        process_calls.append(1);output._obj.value=102;tokens.append(output._obj);raise primary
    def information(*args):raise primary
    def close(handle):
        assert tokens[0].value is None;closed.append(handle);raise SystemExit('owned-token-close')
    kernel=SimpleNamespace(GetCurrentThread=lambda:1,GetCurrentProcess=lambda:2,CloseHandle=close)
    advapi=SimpleNamespace(OpenThreadToken=thread,OpenProcessToken=process,GetTokenInformation=information)
    monkeypatch.setattr(C,'get_last_error',lambda:1008,raising=False)
    with pytest.raises(fault) as caught:custody._windows_user_sid(kernel,advapi)
    assert caught.value is primary and native_cleanup.unknown(primary)
    assert closed==[102 if stage=='process' else 101] and process_calls==([1] if stage=='process' else [])


@pytest.mark.parametrize('stage',['thread','process','empty-success'])
def test_token_result_failure_never_overwrites_an_acquired_output(monkeypatch,stage):
    tokens=[];closed=[];calls=[]
    def thread(_thread,_rights,_self,output):
        if stage=='empty-success':return True
        if stage=='thread':output._obj.value=101;tokens.append(output._obj)
        return False
    def process(_process,_rights,output):
        calls.append('process');output._obj.value=102;tokens.append(output._obj);return False
    def close(handle):
        assert tokens[0].value is None;closed.append(handle);return True
    def forbidden(*args):pytest.fail('invalid token output reached information')
    kernel=SimpleNamespace(GetCurrentThread=lambda:1,GetCurrentProcess=lambda:2,CloseHandle=close)
    advapi=SimpleNamespace(OpenThreadToken=thread,OpenProcessToken=process,GetTokenInformation=forbidden)
    monkeypatch.setattr(C,'get_last_error',lambda:1008,raising=False)
    with pytest.raises(custody.CustodyError) as caught:custody._windows_user_sid(kernel,advapi)
    assert not native_cleanup.unknown(caught.value)
    assert closed==([] if stage=='empty-success' else [102 if stage=='process' else 101])
    assert calls==(['process'] if stage=='process' else [])


@pytest.mark.skipif(os.name!='nt',reason='actual owned Windows token close')
@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
def test_actual_token_close_then_interruption_has_one_attempt(fault):
    kernel,advapi=custody._security_apis();error=fault('owned-after-token-close');closed=[]
    def close(handle):
        assert kernel.CloseHandle(handle);closed.append(handle);raise error
    proxy=SimpleNamespace(GetCurrentThread=kernel.GetCurrentThread,GetCurrentProcess=kernel.GetCurrentProcess,CloseHandle=close)
    with pytest.raises(fault) as caught:custody._windows_user_sid(proxy,advapi)
    assert caught.value is error and native_cleanup.unknown(error) and len(closed)==1


@pytest.mark.skipif(os.name!='nt',reason='Windows security-descriptor API adapter fault')
@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
@pytest.mark.parametrize('stage',['acquire','validate','cleanup'])
def test_descriptor_output_has_single_primary_preserving_release(monkeypatch,fault,stage):
    import msvcrt
    primary=fault('owned-descriptor-primary');outputs=[];freed=[]
    acl=C.create_string_buffer(b'\x02\x00\x08\x00\x00\x00\x00\x00')
    def info(_handle,_kind,_flags,owner,_group,dacl,_sacl,descriptor):
        descriptor._obj.value=101;outputs.append(descriptor._obj)
        owner._obj.value=102;dacl._obj.value=C.addressof(acl)
        if stage=='acquire':raise primary
        return 0
    def valid(*args):
        if stage=='validate':raise primary
        return True
    def control(_descriptor,value,revision):value._obj.value=4;revision._obj.value=1;return True
    def free(pointer):
        assert outputs[0].value is None;freed.append(pointer)
        if stage=='cleanup':raise primary
        raise SystemExit('owned-secondary-descriptor-release')
    kernel=SimpleNamespace(LocalFree=free)
    advapi=SimpleNamespace(GetSecurityInfo=info,IsValidSecurityDescriptor=valid,
        GetSecurityDescriptorControl=control,IsValidAcl=lambda _:True)
    monkeypatch.setattr(custody,'_security_apis',lambda:(kernel,advapi))
    monkeypatch.setattr(custody,'_windows_user_sid',lambda *args:b'owned-user')
    monkeypatch.setattr(custody,'_copy_sid',lambda *args:b'owned-user')
    monkeypatch.setattr(custody,'_check_windows_acl',lambda *args:('owned-security',))
    monkeypatch.setattr(msvcrt,'get_osfhandle',lambda _:1)
    with pytest.raises(fault) as caught:custody._windows_permissions(-1)
    assert caught.value is primary and native_cleanup.unknown(primary) and freed==[101]


@pytest.mark.skipif(os.name!='nt',reason='Windows failed security API output cleanup')
@pytest.mark.parametrize('populated',[False,True])
def test_failed_security_api_releases_only_populated_output(monkeypatch,populated):
    import msvcrt
    freed=[];outputs=[]
    def info(*args):
        output=args[-1]._obj;outputs.append(output)
        if populated:output.value=101
        return 5
    def free(pointer):assert outputs[0].value is None;freed.append(pointer);return pointer
    monkeypatch.setattr(custody,'_security_apis',lambda:(SimpleNamespace(LocalFree=free),SimpleNamespace(GetSecurityInfo=info)))
    monkeypatch.setattr(custody,'_windows_user_sid',lambda *args:b'owned-user')
    monkeypatch.setattr(msvcrt,'get_osfhandle',lambda _:1)
    with pytest.raises(custody.CustodyError) as caught:custody._windows_permissions(-1)
    assert native_cleanup.unknown(caught.value) is populated and freed==([101] if populated else [])

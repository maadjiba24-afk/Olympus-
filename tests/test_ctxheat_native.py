"""Actual local filesystem adversaries on M07 roots; all paths are owned."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from olympus import config, ctxheat as heat, ctxheat_state as state
from olympus.gallery_state import _native


@pytest.fixture
def owned(monkeypatch):
    monkeypatch.setenv('OLYMPUS_CTXHEAT','shadow')
    assert heat.record('item','wiki',retrieved=True,user='native:Owner.A')
    return 'native:Owner.A'


def _kernel():
    import ctypes
    from ctypes import wintypes as w
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    k.CreateFileW.argtypes=[w.LPCWSTR,w.DWORD,w.DWORD,w.LPVOID,w.DWORD,w.DWORD,w.HANDLE]
    k.CreateFileW.restype=w.HANDLE
    k.CloseHandle.argtypes=[w.HANDLE];k.CloseHandle.restype=w.BOOL
    k.DeviceIoControl.argtypes=[w.HANDLE,w.DWORD,w.LPVOID,w.DWORD,w.LPVOID,w.DWORD,ctypes.POINTER(w.DWORD),w.LPVOID]
    k.DeviceIoControl.restype=w.BOOL
    return k


def _set_reparse(handle,target):
    import ctypes,struct
    from ctypes import wintypes as w
    text=_native(target)
    substitute=('\\??\\'+text[4:]).encode('utf-16-le')
    printable=str(target).encode('utf-16-le')
    paths=substitute+b'\0\0'+printable+b'\0\0'
    payload=struct.pack('<IHHHHHH',0xA0000003,len(paths)+8,0,0,len(substitute),len(substitute)+2,len(printable))+paths
    buffer=ctypes.create_string_buffer(payload);returned=w.DWORD()
    assert _kernel().DeviceIoControl(handle,0x000900A4,buffer,len(payload),None,0,ctypes.byref(returned),None),ctypes.get_last_error()


def _junction(link,target):
    import ctypes
    os.mkdir(_native(link));k=_kernel()
    handle=k.CreateFileW(_native(link),0x40000000,7,None,3,0x02200000,None)
    assert handle!=ctypes.c_void_p(-1).value
    try:_set_reparse(handle,target)
    finally:k.CloseHandle(handle)


@pytest.mark.skipif(os.name!='nt',reason='native Windows M07 junction refusal')
@pytest.mark.parametrize('leaf',[False,True],ids=['parent','leaf'])
def test_windows_m07_junction_refuses_without_target_access(owned,tmp_path,leaf):
    path=heat.ledger_path(owned) if leaf else state.path(owned)
    os.rename(_native(path),_native(path.with_name(path.name+'-retained')))
    target=tmp_path/'owned-target';target.mkdir()
    (target/'state.json').write_bytes(b'preserve-target')
    _junction(path,target)
    assert heat.status(owned)['status']=='unavailable'
    assert not heat.record('item','wiki',retrieved=True,user=owned)
    assert (target/'state.json').read_bytes()==b'preserve-target'
    assert [p.name for p in target.iterdir()]==['state.json']


@pytest.mark.skipif(os.name!='nt',reason='native Windows M07 sharing refusal/retry')
def test_windows_m07_sharing_denial_retains_authority_and_retries(owned):
    import ctypes
    path=heat.ledger_path(owned);before=path.read_bytes();k=_kernel()
    handle=k.CreateFileW(_native(path),0x80000000,1,None,3,0,None)
    assert handle!=ctypes.c_void_p(-1).value
    try:
        assert not heat.record('item','wiki',retrieved=True,user=owned)
        assert path.read_bytes()==before
    finally:k.CloseHandle(handle)
    assert heat.record('item','wiki',retrieved=True,user=owned)
    assert heat.entry('item','wiki',user=owned)['hits']==2


@pytest.mark.skipif(os.name!='nt',reason='native Windows M07 held-handle reparse race')
def test_windows_m07_reparse_mutation_at_relative_read_is_confined(owned,tmp_path,monkeypatch):
    import ctypes
    from olympus import gallery_windows as native
    # Windows refuses converting a nonempty directory (ERROR_DIR_NOT_EMPTY).
    # Use an empty, owned authority directory to exercise actual metadata
    # mutation after its handle was pinned and before marker-relative lookup.
    race_owner='native:race-empty'
    root=state.path(race_owner);root.mkdir(parents=True)
    target=tmp_path/'race-target';target.mkdir()
    (target/'initialized.json').write_bytes(b'outside-marker')
    (target/'state.json').write_bytes(b'outside-state')
    k=_kernel();attack=k.CreateFileW(_native(root),0x100,7,None,3,0x02200000,None)
    assert attack!=ctypes.c_void_p(-1).value
    original=native.open_relative;attacked=[];returned=[]
    read=state.Directory.read
    def observed(directory,name,cap):
        raw=read(directory,name,cap)
        returned.append(raw)
        return raw
    monkeypatch.setattr(state.Directory,'read',observed)
    def race(handle,name,**kwargs):
        if name==state.MARKER and not attacked:
            _set_reparse(attack,target);attacked.append(True)
        return original(handle,name,**kwargs)
    monkeypatch.setattr(native,'open_relative',race)
    try:
        assert heat.status(race_owner)['status']=='unavailable'
        assert attacked==[True]
        assert b'outside-marker' not in returned and b'outside-state' not in returned
    finally:k.CloseHandle(attack)
    assert (target/'initialized.json').read_bytes()==b'outside-marker'
    assert (target/'state.json').read_bytes()==b'outside-state'
    assert len(list(target.iterdir()))==2


@pytest.mark.skipif(os.name!='nt',reason='native Windows M07 long-path handling')
def test_windows_m07_deep_owner_path(monkeypatch,tmp_path):
    monkeypatch.setenv('OLYMPUS_CTXHEAT','shadow')
    deep=tmp_path/('deep-'+'a'*90)/('nested-'+'b'*90)
    monkeypatch.setattr(config,'MEMORY_DIR',deep)
    owner='Long.Unicode.\u03a9-'+'x'*1000
    assert len(str(heat.ledger_path(owner)))>260
    assert heat.record('item','wiki',retrieved=True,user=owner)
    assert heat.status(owner)['status']=='available'
    assert heat.entry('item','wiki',user=owner)['hits']==1


@pytest.mark.skipif(os.name=='nt',reason='native POSIX M07 symlink refusal')
@pytest.mark.parametrize('leaf',[False,True],ids=['parent','leaf'])
def test_posix_m07_symlink_refuses_without_target_access(owned,tmp_path,leaf):
    path=heat.ledger_path(owned) if leaf else state.path(owned)
    path.rename(path.with_name(path.name+'-retained'))
    target=tmp_path/'target'
    if leaf:target.write_bytes(b'outside-preserved')
    else:target.mkdir();(target/'state.json').write_bytes(b'outside-preserved')
    path.symlink_to(target,target_is_directory=not leaf)
    assert heat.status(owned)['status']=='unavailable'
    assert not heat.record('item','wiki',retrieved=True,user=owned)
    assert (target if leaf else target/'state.json').read_bytes()==b'outside-preserved'


@pytest.mark.skipif(os.name=='nt',reason='native POSIX M07 competing processes; Windows stays single-process')
def test_posix_m07_competing_processes_keep_every_record(owned):
    source=Path(heat.__file__).resolve().parent.parent
    code='''import sys
sys.path.insert(0,sys.argv[1])
from pathlib import Path
from olympus import config,ctxheat
config.MEMORY_DIR=Path(sys.argv[2])
assert all(ctxheat.record('item','wiki',retrieved=True,user='native:Owner.A') for _ in range(12))
'''
    argv=[sys.executable,'-I','-B','-c',code,str(source),str(config.MEMORY_DIR)]
    children=[subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(2)]
    for child in children:
        out,err=child.communicate(timeout=60)
        assert child.returncode==0,err.decode(errors='replace')[-1000:]
    assert heat.entry('item','wiki',user=owned)['hits']==25

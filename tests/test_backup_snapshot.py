"""Owned source capture: exact images, bounded allocation and native refusal."""
from contextlib import closing
import os
from pathlib import Path
import pytest
from olympus import archive_io as io, backup_archive as fmt, backup_snapshot as snapshot
from olympus.vault_storage import _Directory


def collect(path,*,full=False,excluded=frozenset()):
    with io.operation('backup',selectors={},capture_key=False):
        with closing(_Directory(path)) as root:
            return snapshot.collect(root,full=full,excluded=excluded)


def test_unicode_paths_and_same_captured_images_are_used_once(tmp_path,monkeypatch):
    folder=tmp_path/'owned \u00e9';folder.mkdir()
    path=folder/'file \u65e5.txt';path.write_bytes(b'first image')
    original=io.read_file;calls=[]
    def read(selected,cap,**kwargs):
        calls.append(selected);return original(selected,cap,**kwargs)
    monkeypatch.setattr(io,'read_file',read)
    images=collect(tmp_path)
    assert images=={'owned \u00e9/file \u65e5.txt':b'first image'} and calls==[path]
    path.write_bytes(b'later independent image')
    assert images=={'owned \u00e9/file \u65e5.txt':b'first image'}


def test_custody_and_default_exclusions_preserve_literal_paths(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path/'~').mkdir();key=tmp_path/'~'/'key';key.write_bytes(b'owned custody')
    for name in ('Signing_Seed','keep.txt','.owned.tmp'):(tmp_path/name).write_bytes(b'owned')
    for name in ('Backups','Responses','context','tool_results'):
        directory=tmp_path/name;directory.mkdir();(directory/'item').write_bytes(b'cache')
    excluded=snapshot.custody_paths({'OLYMPUS_SECRET_KEY_FILE':'~/key'})
    assert excluded==frozenset((key,))
    assert collect(tmp_path,excluded=excluded)=={'keep.txt':b'owned'}
    assert set(collect(tmp_path,full=True,excluded=excluded))=={
        'keep.txt','Responses/item','context/item','tool_results/item'}
    assert key.read_bytes()==b'owned custody'


@pytest.mark.parametrize('value',[123,'bad.','bad /key','name:stream'])
def test_invalid_original_custody_selector_refuses(value):
    with pytest.raises((ValueError,TypeError)):
        snapshot.custody_paths({'OLYMPUS_SECRET_KEY_FILE':value})


def test_hardlinked_source_refuses_before_capture(tmp_path,monkeypatch):
    first=tmp_path/'a';first.write_bytes(b'owned');os.link(first,tmp_path/'b')
    calls=[]
    monkeypatch.setattr(io,'read_file',lambda *a,**k:calls.append(a))
    with pytest.raises(OSError if os.name=='nt' else ValueError):collect(tmp_path)
    assert calls==[] and first.read_bytes()==b'owned'


@pytest.mark.parametrize('bound',['file_size','aggregate','files','entries','depth'])
def test_bounds_refuse_before_extra_source_allocation(tmp_path,monkeypatch,bound):
    calls=[];original=io.read_file
    def read(path,cap,**kwargs):calls.append(path);return original(path,cap,**kwargs)
    monkeypatch.setattr(io,'read_file',read)
    if bound=='depth':
        (tmp_path/'child').mkdir();(tmp_path/'child'/'data').write_bytes(b'x')
        monkeypatch.setattr(snapshot,'MAX_DEPTH',0)
    else:
        (tmp_path/'a').write_bytes(b'1234');(tmp_path/'b').write_bytes(b'5678')
        if bound=='file_size':monkeypatch.setattr(fmt,'MAX_FILE',3)
        elif bound=='aggregate':monkeypatch.setattr(fmt,'MAX_SOURCE',4)
        elif bound=='files':monkeypatch.setattr(fmt,'MAX_FILES',1)
        elif bound=='entries':monkeypatch.setattr(snapshot,'MAX_DIRECTORY_ENTRIES',1)
    with pytest.raises(ValueError):collect(tmp_path)
    assert len(calls)==(0 if bound in ('file_size','depth') else 1)


def test_directory_replaced_between_inventory_and_acquisition_refuses(tmp_path,monkeypatch):
    child=tmp_path/'child';child.mkdir();(child/'first').write_bytes(b'original')
    original=snapshot._Directory;calls=[];changed=False
    def directory(path):
        nonlocal changed
        if Path(path)==child and not changed:
            changed=True;child.rename(tmp_path/'retained-child');child.mkdir()
            (child/'foreign').write_bytes(b'foreign')
        return original(path)
    monkeypatch.setattr(snapshot,'_Directory',directory)
    monkeypatch.setattr(io,'read_file',lambda *a,**k:calls.append(a))
    with pytest.raises(ValueError):collect(tmp_path)
    assert changed and calls==[]
    assert (tmp_path/'retained-child'/'first').read_bytes()==b'original'
    assert (child/'foreign').read_bytes()==b'foreign'


def test_source_replaced_after_inventory_cannot_change_returned_manifest_image(tmp_path,monkeypatch):
    path=tmp_path/'data';path.write_bytes(b'original')
    original=io.read_file;changed=False
    def read(selected,cap,**kwargs):
        nonlocal changed
        assert selected==path and not changed
        changed=True;path.rename(tmp_path/'retained-original');path.write_bytes(b'foreign!')
        return original(selected,cap,**kwargs)
    monkeypatch.setattr(io,'read_file',read)
    with pytest.raises(ValueError):collect(tmp_path)
    assert changed and path.read_bytes()==b'foreign!'
    assert (tmp_path/'retained-original').read_bytes()==b'original'


@pytest.mark.skipif(os.name!='posix',reason='POSIX case-distinct source names')
def test_case_colliding_entries_refuse(tmp_path):
    (tmp_path/'a').write_bytes(b'first');(tmp_path/'A').write_bytes(b'second')
    with pytest.raises(ValueError):collect(tmp_path)



def test_selector_outer_whitespace_preserves_custody_contract(tmp_path):
    assert snapshot.custody_paths({'OLYMPUS_SECRET_KEY_FILE':'  '+str(tmp_path/'key')+'  '})==frozenset((tmp_path/'key',))


def test_swap_read_restore_refuses_before_any_foreign_content_read(tmp_path,monkeypatch):
    path=tmp_path/'data';path.write_bytes(b'original')
    original=io.read_file;reads=[];real_read=os.read;changed=False
    def observed(fd,size):reads.append(size);return real_read(fd,size)
    def swap(selected,cap,**kwargs):
        nonlocal changed
        assert selected==path and not changed
        changed=True;path.rename(tmp_path/'retained-original');path.write_bytes(b'foreign!')
        try:return original(selected,cap,**kwargs)
        finally:
            path.rename(tmp_path/'retained-foreign')
            (tmp_path/'retained-original').rename(path)
    monkeypatch.setattr(io,'read_file',swap)
    monkeypatch.setattr(io.os,'read',observed)
    with pytest.raises(ValueError):collect(tmp_path)
    assert changed and reads==[] and path.read_bytes()==b'original'
    assert (tmp_path/'retained-foreign').read_bytes()==b'foreign!'


def test_custody_identity_inspection_never_reads_material(tmp_path,monkeypatch):
    key=tmp_path/'private-key';key.write_bytes(b'owned custody')
    monkeypatch.setattr(io.os,'read',lambda *args:pytest.fail('identity inspection read private material'))
    identity=io.file_identity(key,1024)
    # Native Windows HANDLE information supplies a 32-bit volume serial;
    # Python's stat may supply a wider device identifier. Compare the object
    # identity and size here; the alias control compares complete native IDs.
    info=key.stat()
    assert identity[1:3]==(info.st_ino,info.st_size)
    if os.name=='posix':assert identity[0]==info.st_dev
    assert collect(tmp_path,excluded=frozenset((key,)))=={}


def test_custody_replaced_during_collection_refuses(tmp_path,monkeypatch):
    key=tmp_path/'private-key';key.write_bytes(b'owned custody')
    data=tmp_path/'data';data.write_bytes(b'owned data')
    original=io.read_file;changed=False
    def read(path,cap,**kwargs):
        nonlocal changed
        assert path==data and not changed
        changed=True;key.rename(tmp_path/'retained-private-key');key.write_bytes(b'new custody')
        return original(path,cap,**kwargs)
    monkeypatch.setattr(io,'read_file',read)
    with pytest.raises(ValueError):collect(tmp_path,excluded=frozenset((key,)))
    assert changed and (tmp_path/'retained-private-key').read_bytes()==b'owned custody'


@pytest.mark.skipif(os.name!='nt',reason='Windows native short-name custody alias')
def test_native_short_name_custody_alias_is_excluded_by_identity(tmp_path):
    import ctypes
    key=tmp_path/'owned long private custody material.txt';key.write_bytes(b'owned custody')
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetShortPathNameW.argtypes=[ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_uint32]
    kernel.GetShortPathNameW.restype=ctypes.c_uint32
    buffer=ctypes.create_unicode_buffer(32768)
    size=kernel.GetShortPathNameW(str(key),buffer,len(buffer))
    assert 0<size<len(buffer)
    alias=key.with_name(Path(buffer.value).name)
    if alias.name==key.name:pytest.skip('owned volume did not assign a short filename; no native alias coverage claimed')
    excluded=snapshot.custody_paths({'OLYMPUS_SECRET_KEY_FILE':str(alias)})
    assert excluded==frozenset((alias,)) and alias!=key
    assert io.file_identity(alias,1024)==io.file_identity(key,1024)
    assert collect(tmp_path,excluded=excluded)=={} and key.read_bytes()==b'owned custody'



def test_pre_material_exclusion_identity_cannot_be_recaptured_after_key_move(tmp_path,monkeypatch):
    key=tmp_path/'selected-private-key';key.write_bytes(b'owned synthetic private key');key.chmod(0o600)
    excluded=frozenset((key,));captured=snapshot.capture_exclusions(excluded)
    material=io.custody.capture({'OLYMPUS_ENV':'test','OLYMPUS_SECRET_KEY_FILE':str(key)})
    assert material.material==b'owned synthetic private key'
    key.rename(tmp_path/'old-key-moved-into-source');key.write_bytes(b'replacement custody')
    reads=[]
    monkeypatch.setattr(io,'read_file',lambda *a,**k:reads.append(a))
    with closing(_Directory(tmp_path)) as root:
        with pytest.raises(ValueError):
            snapshot.collect(root,full=True,excluded=excluded,captured_exclusions=captured)
    assert reads==[] and (tmp_path/'old-key-moved-into-source').read_bytes()==material.material


def test_frozen_exclusion_selection_cannot_be_substituted(tmp_path):
    key=tmp_path/'private-key';key.write_bytes(b'owned key')
    captured=snapshot.capture_exclusions(frozenset((key,)))
    with pytest.raises(ValueError):snapshot.check_exclusions(frozenset(),captured)

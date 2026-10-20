"""Owned file-only custody controls; no real keys or permission repairs."""
import os
from pathlib import Path
import pytest
from olympus import archive_io, vault_custody as custody, vault_native, native_cleanup


MATERIAL=b'owned private key material for path controls'


def owned_key(path,raw=MATERIAL):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_BINARY',0),0o600)
    try:
        assert os.write(fd,raw)==len(raw)
    finally:os.close(fd)
    assert path.read_bytes()==raw
    return path


def selection(path):
    return {'OLYMPUS_SECRET_KEY_FILE':str(path),'OLYMPUS_ENV':'test'}


@pytest.mark.parametrize('alias',['leaf-dot','ancestor-dot','leaf-space','stream'])
@pytest.mark.parametrize('absolute',[False,True])
def test_original_custody_spelling_is_rejected_before_acquisition(tmp_path,monkeypatch,alias,absolute):
    parent=tmp_path/'custody';parent.mkdir()
    path=owned_key(parent/'key');monkeypatch.chdir(tmp_path)
    spelling={'leaf-dot':'custody/key.','ancestor-dot':'custody./key',
              'leaf-space':'custody/key ','stream':'custody/key:alternate'}[alias]
    # capture retains historical whitespace-strip selection. A space on an
    # ancestor, rather than trailing selector whitespace, is the alias control.
    if alias=='leaf-space':spelling='custody /key'
    selected=tmp_path/spelling if absolute else Path(spelling)
    def no_directory(*args,**kwargs):pytest.fail('invalid spelling reached native directory acquisition')
    monkeypatch.setattr(vault_native.Directory,'__init__',no_directory)
    with pytest.raises(custody.CustodyError):custody.capture(selection(selected))
    assert path.read_bytes()==MATERIAL


@pytest.mark.skipif(os.name!='nt',reason='Windows same-drive relative alias')
def test_drive_relative_custody_cannot_select_an_existing_file(tmp_path,monkeypatch):
    path=owned_key(tmp_path/'key');monkeypatch.chdir(tmp_path)
    def no_directory(*args,**kwargs):pytest.fail('drive-relative key reached native acquisition')
    monkeypatch.setattr(vault_native.Directory,'__init__',no_directory)
    with pytest.raises(custody.CustodyError):custody.capture(selection(tmp_path.drive+'key'))
    assert path.read_bytes()==MATERIAL


@pytest.mark.skipif(os.name!='nt',reason='Windows case-insensitive existing leaf alias')
def test_case_alias_is_refused_without_opening_the_key(tmp_path,monkeypatch):
    path=owned_key(tmp_path/'KeyCase')
    opened=[];original=custody._information
    def observed(fd):opened.append(fd);return original(fd)
    monkeypatch.setattr(custody,'_information',observed)
    with pytest.raises(custody.CustodyError):custody.capture(selection(tmp_path/'keycase'))
    assert opened==[] and path.read_bytes()==MATERIAL


def test_relative_key_binds_cwd_before_directory_acquisition(tmp_path,monkeypatch):
    first=tmp_path/'first';second=tmp_path/'second';first.mkdir();second.mkdir()
    owned_key(first/'key',b'owned first material');owned_key(second/'key',b'owned second material')
    monkeypatch.chdir(first)
    original=vault_native.Directory.__init__
    def drift(self,path,*args,**kwargs):
        monkeypatch.chdir(second)
        return original(self,path,*args,**kwargs)
    monkeypatch.setattr(vault_native.Directory,'__init__',drift)
    assert custody.capture(selection('key')).material==b'owned first material'


def test_deferred_archive_key_binds_cwd_and_reads_once(tmp_path,monkeypatch):
    first=tmp_path/'first';second=tmp_path/'second';first.mkdir();second.mkdir()
    owned_key(first/'key',b'owned first material');owned_key(second/'key',b'owned second material')
    monkeypatch.chdir(first)
    reads=[];original=custody._read_file
    def read(path,named):reads.append(path);return original(path,named)
    monkeypatch.setattr(custody,'_read_file',read)
    with archive_io.operation('notes',selectors=selection('key'),capture_key=False) as session:
        assert reads==[]
        monkeypatch.chdir(second)
        assert session.material()==b'owned first material'
        assert session.material()==b'owned first material'
    assert len(reads)==1 and Path(reads[0])==first/'key'


@pytest.mark.parametrize('deferred',[False,True])
def test_archive_never_normalizes_bad_key_into_valid_custody(tmp_path,monkeypatch,deferred):
    path=owned_key(tmp_path/'key');monkeypatch.chdir(tmp_path)
    reads=[];original=custody._read_file
    def read(path,named):reads.append(path);return original(path,named)
    monkeypatch.setattr(custody,'_read_file',read)
    if deferred:
        with archive_io.operation('notes',selectors=selection('key.'),capture_key=False) as session:
            # A plain import can finish without acquiring this unused key.
            assert not session.key_selected and reads==[]
            with pytest.raises(custody.CustodyError):session.material()
            monkeypatch.setenv('OLYMPUS_SECRET_KEY_FILE',str(path))
            with pytest.raises(archive_io.ArchiveError):session.material()
    else:
        with pytest.raises(custody.CustodyError):
            with archive_io.operation('backup',selectors=selection('key.')):pytest.fail('bad key was accepted')
    assert reads==[] and path.read_bytes()==MATERIAL


def test_normalization_failure_stays_unavailable_after_cwd_repair(tmp_path,monkeypatch):
    path=owned_key(tmp_path/'key');monkeypatch.chdir(tmp_path)
    def unavailable(path):raise OSError('owned path binding failure')
    original=archive_io.selected_path
    monkeypatch.setattr(archive_io,'selected_path',unavailable)
    with archive_io.operation('backup',selectors=selection('key'),capture_key=False) as session:
        monkeypatch.setattr(archive_io,'selected_path',original)
        with pytest.raises(custody.CustodyError):session.material()
        assert session.key_selection_failed and session.selectors=={}
    assert path.read_bytes()==MATERIAL


def test_key_file_close_interruption_still_drains_parent_chain(tmp_path,monkeypatch):
    path=owned_key(tmp_path/'key')
    original_close,original_directory_close=os.close,vault_native.Directory.close
    events,guards=[],[]
    def close(fd):
        original_close(fd)
        if not events:
            events.append('file');raise KeyboardInterrupt('owned interruption after close')
    def directory_close(directory):
        guards.append(directory);original_directory_close(directory);events.append('directory')
    monkeypatch.setattr(os,'close',close)
    monkeypatch.setattr(vault_native.Directory,'close',directory_close)
    with pytest.raises(KeyboardInterrupt):custody._read_file(path,False)
    assert events==['file','directory'] and guards and all(g._closed for g in guards)
    assert all(g.fd is None and g.win is None for g in guards)


@pytest.mark.skipif(os.name!='posix',reason='POSIX actual constructor descriptor cleanup')
def test_key_constructor_interruption_drains_actual_posix_descriptors(tmp_path,monkeypatch):
    path=owned_key(tmp_path/'key');acquired,closed=[],[]
    primary=OSError('owned constructor fault')
    real_open,real_close,append=os.open,os.close,vault_native.Directory._append_posix
    def opened(*args,**kwargs):
        fd=real_open(*args,**kwargs);acquired.append(fd);return fd
    def close(fd):
        real_close(fd);closed.append(fd)
        if len(closed)==1:raise KeyboardInterrupt('owned close interruption')
    def appended(directory,name,create):
        append(directory,name,create)
        if name==tmp_path.name:raise primary
    monkeypatch.setattr(os,'open',opened);monkeypatch.setattr(os,'close',close)
    monkeypatch.setattr(vault_native.Directory,'_append_posix',appended)
    with pytest.raises(OSError) as caught:custody._read_file(path,False)
    assert caught.value is primary and native_cleanup.unknown(primary)
    assert acquired and closed==list(reversed(acquired))
    for fd in acquired:
        with pytest.raises(OSError):os.fstat(fd)


@pytest.mark.skipif(os.name!='nt',reason='Windows actual constructor handle cleanup')
def test_key_constructor_interruption_drains_actual_windows_handles(tmp_path,monkeypatch):
    from olympus import vault_windows as native
    path=owned_key(tmp_path/'key');acquired,closed=[],[]
    primary=OSError('owned constructor fault')
    real_anchor,real_relative,real_close=native._anchor,native.open_relative,native.close
    real_check=vault_native.Directory.check
    def anchor(*args,**kwargs):
        handle=real_anchor(*args,**kwargs);acquired.append(handle);return handle
    def relative(*args,**kwargs):
        handle=real_relative(*args,**kwargs);acquired.append(handle);return handle
    def close(handle):
        real_close(handle);closed.append(handle)
        if len(closed)==1:raise KeyboardInterrupt('owned native close interruption')
    def check(directory):
        real_check(directory)
        if directory.path==tmp_path:raise primary
    monkeypatch.setattr(native,'_anchor',anchor);monkeypatch.setattr(native,'open_relative',relative)
    monkeypatch.setattr(native,'close',close);monkeypatch.setattr(vault_native.Directory,'check',check)
    with pytest.raises(OSError) as caught:custody._read_file(path,False)
    assert caught.value is primary and native_cleanup.unknown(primary)
    assert acquired and closed==list(reversed(acquired))
    for handle in acquired:
        with pytest.raises(Exception):native.information(handle,True)


@pytest.mark.skipif(os.name!='nt',reason='Windows case-only rename between spelling check and native acquisition')
def test_case_rename_at_open_refuses_before_read_and_drains_custody(tmp_path,monkeypatch):
    from olympus import vault_windows as native
    path=owned_key(tmp_path/'KeyCase');alias=tmp_path/'keycase'
    real_open=native.NativeDirectory.open_fd
    real_close=vault_native.Directory.close
    opened,guards,reads=[],[],[]
    renamed=False
    def opening(directory,name,*args,**kwargs):
        nonlocal renamed
        if name=='KeyCase' and not renamed:
            path.rename(alias);renamed=True
        fd=real_open(directory,name,*args,**kwargs)
        opened.append(fd)
        return fd
    def closing(directory):
        guards.append(directory);return real_close(directory)
    def no_read(*args):
        reads.append(args[0]);pytest.fail('case-swapped private material was read')
    monkeypatch.setattr(native.NativeDirectory,'open_fd',opening)
    monkeypatch.setattr(vault_native.Directory,'close',closing)
    monkeypatch.setattr(os,'read',no_read)
    with pytest.raises(custody.CustodyError) as caught:
        custody.capture(selection(path))
    assert renamed and opened and reads==[]
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert alias.read_bytes()==MATERIAL and [p.name for p in tmp_path.iterdir()]==['keycase']
    assert guards and all(g._closed and g.fd is None and g.win is None for g in guards)
    for fd in opened:
        with pytest.raises(OSError):os.fstat(fd)

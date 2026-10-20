"""Owned archive boundary faults with real descriptors and retained staging."""
import os
from pathlib import Path
import pytest
from olympus import archive_io as archive, native_cleanup, vault_custody as custody, vault_storage as storage


def _drain_owned_fds(values,close,primary=None):
    pending=list(values);values.clear()
    native_cleanup.drain([lambda fd=fd:close(fd) for fd in pending],primary)


@pytest.mark.parametrize('endpoint',['read','identity'])
@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
@pytest.mark.parametrize('secondary',[False,True])
def test_archive_input_primary_survives_real_close_and_reused_descriptor(tmp_path,monkeypatch,endpoint,fault,secondary):
    path=tmp_path/'input';path.write_bytes(b'owned input')
    replacement=tmp_path/'replacement';replacement.write_bytes(b'owned replacement')
    primary=fault('owned private native failure')
    target,attempted,reused,duplicates,guards=[],[],[],[],[]
    actual_close=os.close;directory_close=storage._Directory.close
    def information(fd):target.append(fd);raise primary
    def close(fd):
        if fd in reused:
            duplicates.append(fd);raise AssertionError('reused descriptor must not be closed by product')
        if fd in target:
            attempted.append(fd);actual_close(fd)
            reused.append(os.open(replacement,os.O_RDONLY|getattr(os,'O_BINARY',0)))
            if secondary:raise SystemExit('owned secondary close failure')
        else:actual_close(fd)
    def directory_finished(directory):
        guards.append(directory);return directory_close(directory)
    test_primary=None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(custody,'_information',information)
            patch.setattr(os,'close',close)
            patch.setattr(storage._Directory,'close',directory_finished)
            with pytest.raises(archive.ArchiveError if fault is OSError else fault) as caught:
                if endpoint=='read':archive.read_file(path,100)
                else:archive.file_identity(path,100)
        assert len(target)==1 and attempted==target and not duplicates
        assert guards and all(g._closed and g.fd is None and g.win is None for g in guards)
        assert native_cleanup.unknown(caught.value) is secondary
        if fault is OSError:
            assert caught.value.__cause__ is None and caught.value.__context__ is None
            assert 'owned private' not in str(caught.value)
        else:assert caught.value is primary
        assert len(reused)==1 and reused[0]==target[0]
        assert os.read(reused[0],100)==b'owned replacement'
        assert path.read_bytes()==b'owned input'
    except BaseException as error:
        test_primary=error;raise
    finally:
        pending=list(reused)+[fd for fd in target if fd not in attempted]
        reused.clear();target.clear()
        _drain_owned_fds(pending,actual_close,test_primary)


@pytest.mark.parametrize('when',['before-rename','after-rename'])
@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
@pytest.mark.parametrize('secondary',[False,True])
def test_archive_publication_retains_primary_uncertainty_and_staging(tmp_path,monkeypatch,when,fault,secondary):
    path=tmp_path/'output';raw=b'owned published image';primary=fault('owned private publication failure')
    targets,attempted,guards=[],[],[]
    actual_close=os.close;write=storage._Directory._write;sync=storage._Directory.sync
    directory_close=storage._Directory.close
    def writing(directory,fd,data):
        targets.append(fd)
        if when=='before-rename':raise primary
        return write(directory,fd,data)
    def syncing(directory):
        if when=='after-rename':raise primary
        return sync(directory)
    def close(fd):
        if fd in targets:
            if fd in attempted:attempted.append(fd);raise AssertionError('publication descriptor retried')
            attempted.append(fd);actual_close(fd)
            if secondary:raise KeyboardInterrupt('owned secondary')
        else:actual_close(fd)
    def directory_finished(directory):guards.append(directory);return directory_close(directory)
    test_primary=None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(storage._Directory,'_write',writing)
            patch.setattr(storage._Directory,'sync',syncing)
            patch.setattr(storage._Directory,'close',directory_finished)
            patch.setattr(os,'close',close)
            with pytest.raises(archive.ArchivePublicationError if fault is OSError else fault) as caught:
                archive.publish_file(path,raw)
        assert len(targets)==1 and attempted==targets
        assert guards and all(g._closed and g.fd is None and g.win is None for g in guards)
        assert native_cleanup.unknown(caught.value) is secondary
        assert caught.value.publication_uncertain is (when=='after-rename')
        retained=caught.value.retained_staging_name
        assert retained.startswith('.archive-') and retained.endswith('.tmp') and Path(retained).name==retained
        if fault is OSError:
            assert caught.value.__cause__ is None and caught.value.__context__ is None
            assert 'owned private' not in str(caught.value)
        else:assert caught.value is primary
        if when=='after-rename':assert path.read_bytes()==raw
        else:assert not path.exists() and (tmp_path/retained).is_file()
    except BaseException as error:
        test_primary=error;raise
    finally:
        pending=[fd for fd in targets if fd not in attempted];targets.clear()
        _drain_owned_fds(pending,actual_close,test_primary)


@pytest.mark.parametrize('optional',[False,True])
@pytest.mark.parametrize('uncertain',[False,True])
def test_archive_optional_absence_never_discards_cleanup_uncertainty(tmp_path,monkeypatch,optional,uncertain):
    error=FileNotFoundError('owned missing input')
    if uncertain:native_cleanup.mark(error)
    original_init=storage._Directory.__init__;real_open=os.open;calls=[]
    def fail(*args,**kwargs):calls.append(1);raise error
    def initialized(directory,*args,**kwargs):
        original_init(directory,*args,**kwargs)
        if directory.win is not None:directory.win.open_fd=fail
    def opened(name,*args,**kwargs):
        if name=='missing':return fail()
        return real_open(name,*args,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(storage._Directory,'__init__',initialized)
        patch.setattr(os,'open',opened)
        if optional and not uncertain:assert archive.read_file(tmp_path/'missing',100,optional=True) is None
        else:
            with pytest.raises(archive.ArchiveError) as caught:archive.read_file(tmp_path/'missing',100,optional=optional)
            assert native_cleanup.unknown(caught.value) is uncertain
    assert calls==[1]

"""Append-only source text for owned archive cleanup qualification."""

@pytest.mark.parametrize('endpoint',['read','identity','publish'])
@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
def test_archive_cleanup_only_failure_is_reported_without_retry(tmp_path,monkeypatch,endpoint,fault):
    path=tmp_path/'input';path.write_bytes(b'owned input')
    targets,attempted=[],[];primary=fault('owned private close failure')
    actual_close=os.close;information=custody._information;write=storage._Directory._write
    def observed(fd):
        if endpoint!='publish' and fd not in targets:targets.append(fd)
        return information(fd)
    def writing(directory,fd,data):targets.append(fd);return write(directory,fd,data)
    def close(fd):
        if fd in targets:
            if fd in attempted:attempted.append(fd);raise AssertionError('cleanup descriptor retried')
            attempted.append(fd);actual_close(fd);raise primary
        actual_close(fd)
    test_primary=None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(custody,'_information',observed)
            patch.setattr(storage._Directory,'_write',writing)
            patch.setattr(os,'close',close)
            with pytest.raises(archive.ArchiveError if fault is OSError else fault) as caught:
                if endpoint=='read':archive.read_file(path,100)
                elif endpoint=='identity':archive.file_identity(path,100)
                else:archive.publish_file(tmp_path/'output',b'owned output')
        assert len(targets)==1 and attempted==targets and native_cleanup.unknown(caught.value)
        if fault is OSError:
            assert caught.value.__cause__ is None and caught.value.__context__ is None
            assert 'owned private' not in str(caught.value)
        else:assert caught.value is primary
        if endpoint=='publish':assert caught.value.publication_uncertain and (tmp_path/'output').read_bytes()==b'owned output'
    except BaseException as error:test_primary=error;raise
    finally:
        pending=[fd for fd in targets if fd not in attempted];targets.clear()
        _drain_owned_fds(pending,actual_close,test_primary)


@pytest.fixture
def native_publication(tmp_path):
    from types import SimpleNamespace
    from olympus import vault_windows as native
    directory=storage._Directory(tmp_path);fd=None;primary=None
    try:
        handle=native.open_relative(directory.win.handle,'stage',create=True,exclusive=True,write=True,delete=True)
        fd=native.to_fd(handle,os.O_RDWR)
        directory._write(fd,b'owned staged image')
        yield SimpleNamespace(directory=directory,fd=fd,path=tmp_path)
    except BaseException as error:primary=error;raise
    finally:
        held,fd=fd,None
        actions=([lambda:os.close(held)] if held is not None else [])
        actions.append(directory.close)
        native_cleanup.drain(actions,primary)


@pytest.mark.skipif(os.name!='nt',reason='Windows native archive rename boundary')
@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
@pytest.mark.parametrize('secondary',[False,True])
def test_windows_rename_target_primary_survives_close(native_publication,monkeypatch,fault,secondary):
    from olympus import gallery_windows
    owned=native_publication;(owned.path/'target').write_bytes(b'owned existing target')
    primary=fault('owned target information failure');targets,attempted=[],[]
    information=custody._information;actual_close=os.close
    def observed(fd):
        if fd!=owned.fd:targets.append(fd);raise primary
        return information(fd)
    def close(fd):
        if fd in targets:
            if fd in attempted:attempted.append(fd);raise AssertionError('target descriptor retried')
            attempted.append(fd);actual_close(fd)
            if secondary:raise SystemExit('owned secondary')
        else:actual_close(fd)
    test_primary=None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(custody,'_information',observed);patch.setattr(os,'close',close)
            patch.setattr(gallery_windows,'rename_same_directory',lambda *a:pytest.fail('renamed after failed target observation'))
            with pytest.raises(fault) as caught:
                archive._windows_publish_rename(owned.directory,owned.fd,'target',None)
        assert caught.value is primary and native_cleanup.unknown(primary) is secondary
        assert len(targets)==1 and attempted==targets
        assert (owned.path/'target').read_bytes()==b'owned existing target'
        os.lseek(owned.fd,0,os.SEEK_SET)
        assert os.read(owned.fd,100)==b'owned staged image' and (owned.path/'stage').is_file()
    except BaseException as error:test_primary=error;raise
    finally:
        pending=[fd for fd in targets if fd not in attempted];targets.clear()
        _drain_owned_fds(pending,actual_close,test_primary)


@pytest.mark.skipif(os.name!='nt',reason='Windows native archive rename boundary')
def test_windows_rename_never_retries_an_uncertain_permission_failure(native_publication,monkeypatch):
    from olympus import gallery_windows
    owned=native_publication;error=PermissionError('owned uncertain rename');error.winerror=32
    native_cleanup.mark(error);calls=[]
    def rename(*args):calls.append(1);raise error
    with monkeypatch.context() as patch:
        patch.setattr(gallery_windows,'rename_same_directory',rename)
        patch.setattr(archive.time,'sleep',lambda *a:pytest.fail('retried uncertain rename'))
        with pytest.raises(PermissionError) as caught:
            archive._windows_publish_rename(owned.directory,owned.fd,'target',None)
    assert caught.value is error and native_cleanup.unknown(error) and calls==[1]
    assert not (owned.path/'target').exists() and (owned.path/'stage').is_file()


@pytest.mark.skipif(os.name!='nt',reason='Windows native archive rename boundary')
@pytest.mark.parametrize('uncertain',[False,True])
def test_windows_rename_absence_requires_confirmed_observation(native_publication,monkeypatch,uncertain):
    owned=native_publication;error=FileNotFoundError('owned absent target');calls=[]
    if uncertain:native_cleanup.mark(error)
    def absent(*args):calls.append(1);raise error
    with monkeypatch.context() as patch:
        patch.setattr(owned.directory.win,'open_fd',absent)
        if uncertain:
            with pytest.raises(FileNotFoundError) as caught:
                archive._windows_publish_rename(owned.directory,owned.fd,'target',None)
            assert caught.value is error and native_cleanup.unknown(error)
        else:archive._windows_publish_rename(owned.directory,owned.fd,'target',None)
    assert calls==[1]
    if uncertain:assert not (owned.path/'target').exists() and (owned.path/'stage').is_file()
    else:
        os.lseek(owned.fd,0,os.SEEK_SET)
        assert os.read(owned.fd,100)==b'owned staged image' and (owned.path/'target').is_file()

"""Additional owned pre-staging and independent directory-drain controls."""

@pytest.fixture(autouse=True)
def owned_directory_guards(monkeypatch):
    resources=[];original_init=storage._Directory.__init__;original_close=storage._Directory.close
    def initialized(directory,*args,**kwargs):
        original_init(directory,*args,**kwargs)
        resources.append(directory)
    monkeypatch.setattr(storage._Directory,'__init__',initialized)
    primary=None
    try:yield resources
    except BaseException as error:primary=error;raise
    finally:
        pending=[resource for resource in resources if not resource._closed];resources.clear()
        native_cleanup.drain([lambda resource=resource:original_close(resource) for resource in reversed(pending)],primary)
        if pending and primary is None:
            raise AssertionError('product left directory owners open; independent fixture intervention was required')


@pytest.mark.parametrize('scenario,secondary',[('metadata',False),('metadata',True),('close',False)],
    ids=['metadata','metadata-and-close','close-only'])
@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
def test_existing_target_failure_prevents_staging_and_preserves_target(tmp_path,monkeypatch,owned_directory_guards,scenario,secondary,fault):
    path=tmp_path/'output';path.write_bytes(b'owned existing target');primary=fault('owned private previous-target failure')
    targets,attempted=[],[];information=custody._information;actual_close=os.close
    def observed(fd):
        if fd not in targets:targets.append(fd)
        if scenario=='metadata':raise primary
        return information(fd)
    def close(fd):
        if fd in targets:
            if fd in attempted:attempted.append(fd);raise AssertionError('previous target close retried')
            attempted.append(fd);actual_close(fd)
            if scenario=='close':raise primary
            if secondary:raise SystemExit('owned secondary')
        else:actual_close(fd)
    test_primary=None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(custody,'_information',observed);patch.setattr(os,'close',close)
            patch.setattr(storage._Directory,'_write',lambda *a:pytest.fail('staging written after previous-target failure'))
            with pytest.raises(archive.ArchivePublicationError if fault is OSError else fault) as caught:
                archive.publish_file(path,b'forbidden replacement')
        assert len(targets)==1 and attempted==targets
        assert native_cleanup.unknown(caught.value) is (scenario=='close' or secondary)
        assert not caught.value.publication_uncertain
        if fault is not OSError:assert caught.value is primary
        assert {p.name for p in tmp_path.iterdir()}=={'output'} and path.read_bytes()==b'owned existing target'
        assert owned_directory_guards and all(d._closed and d.fd is None and d.win is None for d in owned_directory_guards)
    except BaseException as error:test_primary=error;raise
    finally:
        pending=[fd for fd in targets if fd not in attempted];targets.clear()
        _drain_owned_fds(pending,actual_close,test_primary)


@pytest.mark.parametrize('uncertain',[False,True])
def test_publication_absence_with_cleanup_uncertainty_stops_before_staging(tmp_path,monkeypatch,uncertain):
    error=FileNotFoundError('owned absent target')
    if uncertain:native_cleanup.mark(error)
    original_init=storage._Directory.__init__;actual_open=os.open;calls=[]
    def absent(*args,**kwargs):calls.append(1);raise error
    def initialized(directory,*args,**kwargs):
        original_init(directory,*args,**kwargs)
        if directory.win is not None:directory.win.open_fd=absent
    def opened(name,*args,**kwargs):
        if name=='output':return absent()
        return actual_open(name,*args,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(storage._Directory,'__init__',initialized)
        patch.setattr(os,'open',opened)
        if uncertain:
            patch.setattr(storage._Directory,'_write',lambda *a:pytest.fail('staging written after uncertain absence'))
            with pytest.raises(archive.ArchivePublicationError) as caught:
                archive.publish_file(tmp_path/'output',b'owned image')
            assert native_cleanup.unknown(caught.value) and not caught.value.publication_uncertain
        else:archive.publish_file(tmp_path/'output',b'owned image')
    if uncertain:assert calls==[1] and not list(tmp_path.iterdir())
    else:assert (tmp_path/'output').read_bytes()==b'owned image'


@pytest.mark.parametrize('endpoint',['read','identity','publish'])
@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
@pytest.mark.parametrize('body_failure',[False,True])
def test_archive_directory_cleanup_and_three_way_failures_drain_independently(tmp_path,monkeypatch,owned_directory_guards,endpoint,fault,body_failure):
    path=tmp_path/'input';path.write_bytes(b'owned input');primary=fault('owned private operation failure')
    targets,attempted,events=[],[],[]
    information=custody._information;write=storage._Directory._write
    actual_close=os.close;directory_close=storage._Directory.close
    def observed(fd):
        if endpoint!='publish':
            if fd not in targets:targets.append(fd)
            if body_failure:raise primary
        return information(fd)
    def writing(directory,fd,data):
        targets.append(fd)
        if body_failure:raise primary
        return write(directory,fd,data)
    def close(fd):
        if fd in targets:
            if fd in attempted:attempted.append(fd);raise AssertionError('owned file close retried')
            attempted.append(fd);events.append('file');actual_close(fd)
            if body_failure:raise OSError('owned secondary file close')
        else:actual_close(fd)
    def finish(directory):
        directory_close(directory);events.append('directory')
        if body_failure:raise SystemExit('owned secondary directory close')
        raise primary
    test_primary=None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(custody,'_information',observed)
            patch.setattr(storage._Directory,'_write',writing)
            patch.setattr(storage._Directory,'close',finish)
            patch.setattr(os,'close',close)
            with pytest.raises(archive.ArchiveError if fault is OSError else fault) as caught:
                if endpoint=='read':archive.read_file(path,100)
                elif endpoint=='identity':archive.file_identity(path,100)
                else:archive.publish_file(tmp_path/'output',b'owned output')
        assert len(targets)==1 and attempted==targets and events==['file','directory']
        assert owned_directory_guards and all(d._closed and d.fd is None and d.win is None for d in owned_directory_guards)
        assert native_cleanup.unknown(caught.value)
        if fault is OSError:
            assert caught.value.__cause__ is None and caught.value.__context__ is None
            assert 'owned private' not in str(caught.value)
        else:assert caught.value is primary
        if endpoint=='publish':assert caught.value.publication_uncertain is (not body_failure)
    except BaseException as error:test_primary=error;raise
    finally:
        pending=[fd for fd in targets if fd not in attempted];targets.clear()
        _drain_owned_fds(pending,actual_close,test_primary)

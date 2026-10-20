"""Actual Windows sharing failures; only fresh owned synthetic file images."""
from contextlib import closing
import os
import pytest
from olympus import archive_io as io, vault_storage

pytestmark=pytest.mark.skipif(os.name!='nt',reason='native Windows sharing and rename controls')


@pytest.fixture
def target(tmp_path):
    path=tmp_path/'owned-archive';path.write_bytes(b'old owned image')
    return path


def retained(target):
    return {p.name:p.read_bytes() for p in target.parent.iterdir() if p.is_file()}


def test_actual_locked_target_releases_then_same_held_image_is_published(target,monkeypatch):
    from olympus import gallery_windows as native
    original=native.rename_same_directory;calls=[];blocker=None;errors=[]
    with closing(vault_storage._Directory(target.parent)) as directory:
        blocker=directory.win.open_fd(target.name,os.O_RDONLY)
        def rename(handle,name):
            nonlocal blocker
            calls.append((handle,name))
            try:return original(handle,name)
            except OSError as error:
                errors.append((type(error).__name__,error.winerror))
                assert len(calls)==1 and error.winerror in (5,32,33)
                os.close(blocker);blocker=None
                raise
        monkeypatch.setattr(native,'rename_same_directory',rename)
        try:io.publish_file(target,b'new owned image')
        finally:
            if blocker is not None:os.close(blocker)
    assert len(calls)==2 and calls[0]==calls[1] and len(errors)==1
    assert retained(target)=={target.name:b'new owned image'}


def test_actual_locked_target_exhausts_bound_and_retains_stage(target,monkeypatch):
    from olympus import gallery_windows as native
    from olympus.atomicio import _REPLACE_DELAYS
    original=native.rename_same_directory;calls=[];delays=[]
    def rename(handle,name):
        calls.append((handle,name));return original(handle,name)
    monkeypatch.setattr(native,'rename_same_directory',rename)
    monkeypatch.setattr(io.time,'sleep',delays.append)
    with closing(vault_storage._Directory(target.parent)) as directory:
        blocker=directory.win.open_fd(target.name,os.O_RDONLY)
        try:
            with pytest.raises(io.ArchivePublicationError) as caught:io.publish_file(target,b'new owned image')
        finally:os.close(blocker)
    assert len(calls)==len(_REPLACE_DELAYS)+1 and len(set(calls))==1
    assert delays==list(_REPLACE_DELAYS) and caught.value.publication_uncertain
    assert retained(target)=={target.name:b'old owned image',caught.value.retained_staging_name:b'new owned image'}


@pytest.mark.parametrize('change',['replace','hardlink'])
def test_target_drift_during_wait_refuses_without_another_rename(target,monkeypatch,change):
    from olympus import gallery_windows as native
    calls=[];delays=[]
    def rename(handle,name):
        calls.append((handle,name))
        error=PermissionError('owned sharing refusal');error.winerror=32;raise error
    def pause(delay):
        delays.append(delay)
        if change=='replace':
            target.rename(target.with_name('retained-old'))
            target.write_bytes(b'foreign owned image')
        else:os.link(target,target.with_name('additional-owned-link'))
    monkeypatch.setattr(native,'rename_same_directory',rename)
    monkeypatch.setattr(io.time,'sleep',pause)
    with pytest.raises(io.ArchivePublicationError) as caught:io.publish_file(target,b'new owned image')
    assert len(calls)==1 and len(delays)==1
    assert target.read_bytes()==(b'foreign owned image' if change=='replace' else b'old owned image')
    assert (target.parent/caught.value.retained_staging_name).read_bytes()==b'new owned image'


@pytest.mark.parametrize('kind',['other_permission','io_failure'])
def test_other_native_errors_are_not_retried(target,monkeypatch,kind):
    from olympus import gallery_windows as native
    calls=[];delays=[]
    def rename(handle,name):
        calls.append((handle,name))
        error=PermissionError('owned refusal') if kind=='other_permission' else OSError('owned I/O failure')
        error.winerror=87;raise error
    monkeypatch.setattr(native,'rename_same_directory',rename)
    monkeypatch.setattr(io.time,'sleep',delays.append)
    with pytest.raises(io.ArchivePublicationError) as caught:io.publish_file(target,b'new owned image')
    assert len(calls)==1 and delays==[]
    assert retained(target)=={target.name:b'old owned image',caught.value.retained_staging_name:b'new owned image'}


def test_wait_interruption_drains_handles_and_retains_exact_images(target,monkeypatch):
    from olympus import gallery_windows as native
    original=native.rename_same_directory;calls=[]
    interruption=KeyboardInterrupt('owned interruption')
    def rename(handle,name):calls.append((handle,name));return original(handle,name)
    def pause(delay):raise interruption
    monkeypatch.setattr(native,'rename_same_directory',rename)
    monkeypatch.setattr(io.time,'sleep',pause)
    with closing(vault_storage._Directory(target.parent)) as directory:
        blocker=directory.win.open_fd(target.name,os.O_RDONLY)
        try:
            with pytest.raises(KeyboardInterrupt) as caught:io.publish_file(target,b'new owned image')
        finally:os.close(blocker)
    assert caught.value is interruption and len(calls)==1
    before=retained(target)
    assert before=={target.name:b'old owned image',interruption.retained_staging_name:b'new owned image'}
    # Actual native rename is possible only after all no-delete-share handles
    # acquired by the interrupted publication have closed.
    moved=target.parent.with_name(target.parent.name+'-retained')
    target.parent.rename(moved)
    assert {p.name:p.read_bytes() for p in moved.iterdir() if p.is_file()}==before

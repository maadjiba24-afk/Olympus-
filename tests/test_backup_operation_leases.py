"""Operation-directory substitution and real publication-barrier fault controls."""
import hashlib
import os
from pathlib import Path
import uuid

import pytest

from olympus import archive_io, backup_publication as pub, backup_restore as restore


def _verification():
    return {'archive_sha256':hashlib.sha256(b'owned archive').hexdigest(),
            'signature_sha256':None,'signed':False,'signature_ok':False,'signature_override':False,
            'archive_encryption':archive_io.encryption_metadata(plaintext=True)}


def _seed(tmp_path, module):
    op = uuid.uuid4().hex
    live = tmp_path/'live'; live.mkdir()
    if module is pub:
        destination = tmp_path/'backups'; destination.mkdir()
        result = pub.publish(destination,b'owned archive',b'owned signature',
            {'encrypted':False,'files':1,'full':False},operation_id=op)
    else:
        destination = tmp_path/'offline'
        result = restore.restore(destination,{'a.txt':b'A'},_verification(),operation_id=op,live_root=live)
    return op,live,destination,result


def _replace_operation(root,op,observed):
    directory = root.control.path/op
    observed['handles'] = list(root.operations)
    assert observed['handles'], 'operation has no retained native directory authority'
    moved = directory.with_name('retained-'+op)
    try:
        directory.rename(moved)
    except OSError as error:
        # Windows custody refuses renaming the held operation directory.
        assert os.name=='nt' and error.winerror in (5,32)
        observed['native_rename_refused'] = True
        raise OSError('owned interruption after rejected directory replacement') from None
    observed['native_rename_refused'] = False
    directory.mkdir()
    (directory/'foreign-evidence.txt').write_bytes(b'leave this replacement alone')
    observed['replacement'] = directory


@pytest.mark.parametrize('module',[pub,restore],ids=['publication','restore'])
@pytest.mark.parametrize('consumer',['resume','inspect'])
def test_operation_replacement_after_plan_load_fails_before_consumption(tmp_path,monkeypatch,module,consumer):
    op,live,dest,result = _seed(tmp_path,module)
    observed = {}; loader_name = '_load' if module is pub else '_plan'
    original = getattr(module,loader_name)
    def drift(root,operation_id,*args,**kwargs):
        value = original(root,operation_id,*args,**kwargs)
        if not observed: _replace_operation(root,operation_id,observed)
        return value
    monkeypatch.setattr(module,loader_name,drift)
    error_type = pub.PublicationError if module is pub else restore.RestoreError
    with pytest.raises(error_type):
        getattr(module,consumer)(dest,operation_id=op,**({} if module is pub else {'live_root':live}))
    assert observed
    assert all(directory._closed for directory in observed['handles'])
    if not observed['native_rename_refused']:
        replacement = observed['replacement']
        assert {p.name for p in replacement.iterdir()}=={'foreign-evidence.txt'}
        assert (replacement/'foreign-evidence.txt').read_bytes()==b'leave this replacement alone'
    if module is restore:
        assert (dest/'a.txt').read_bytes()==b'A'
    else:
        assert (dest/result['name']).read_bytes()==b'owned archive'


@pytest.mark.parametrize('module',[pub,restore],ids=['publication','restore'])
@pytest.mark.parametrize('boundary',['progress.json','complete.json'])
def test_operation_replacement_before_evidence_write_never_writes_replacement(tmp_path,monkeypatch,module,boundary):
    op = uuid.uuid4().hex; live=tmp_path/'live'; live.mkdir()
    destination=tmp_path/'backups' if module is pub else tmp_path/'offline'
    if module is pub: destination.mkdir()
    observed = {}; original = module._Root.publish
    def drift(self,path,raw):
        if path.name==boundary and not observed:
            _replace_operation(self,op,observed)
        return original(self,path,raw)
    monkeypatch.setattr(module._Root,'publish',drift)
    error_type = pub.PublicationError if module is pub else restore.RestoreError
    with pytest.raises(error_type):
        if module is pub:
            pub.publish(destination,b'owned archive',b'owned signature',
                {'encrypted':False,'files':1,'full':False},operation_id=op)
        else:
            restore.restore(destination,{'a.txt':b'A'},_verification(),operation_id=op,live_root=live)
    assert observed and all(directory._closed for directory in observed['handles'])
    if not observed['native_rename_refused']:
        replacement=observed['replacement']
        assert {p.name for p in replacement.iterdir()}=={'foreign-evidence.txt'}


def test_completed_pair_read_keeps_operation_bound_through_return(tmp_path,monkeypatch):
    op,live,dest,result = _seed(tmp_path,pub)
    original = pub._load; observed={}
    def drift(root,operation_id,*args,**kwargs):
        value=original(root,operation_id,*args,**kwargs)
        if not observed: _replace_operation(root,operation_id,observed)
        return value
    monkeypatch.setattr(pub,'_load',drift)
    with pytest.raises(pub.PublicationError):
        with pub.locked(dest) as root: pub.read_completed(root,result['name'])
    assert observed and all(directory._closed for directory in observed['handles'])
    if not observed['native_rename_refused']:
        assert {p.name for p in observed['replacement'].iterdir()}=={'foreign-evidence.txt'}


def test_historical_pair_read_requires_intact_active_head(tmp_path):
    op,live,dest,result = _seed(tmp_path,pub)
    pub.publish(dest,b'new owned archive',b'new owned signature',
        {'encrypted':False,'files':1,'full':False},operation_id=uuid.uuid4().hex)
    with pub.locked(dest) as root:
        assert pub.read_completed(root,result['name'])[2]['completion_barrier_reconfirmed']
    head = dest/pub.CONTROL/'head.json'; retained = head.with_name('retained-head.json')
    head.rename(retained)
    with pytest.raises(pub.PublicationError):
        with pub.locked(dest) as root: pub.read_completed(root,result['name'])
    assert not head.exists() and retained.is_file()
    assert (dest/result['name']).read_bytes()==b'owned archive'


def test_actual_post_rename_completion_barrier_must_settle_before_consumption(tmp_path,monkeypatch):
    destination=tmp_path/'backups'; destination.mkdir()
    op=uuid.uuid4().hex; name=pub.archive_name(op,False)
    operation=destination/pub.CONTROL/op
    state={'in_completion':False,'enabled':True,'uncertain':[],'confirmed_barriers':0}
    publish = archive_io.publish_file; sync=pub._Directory.sync
    def publishing(path,raw):
        previous=state['in_completion']
        state['in_completion']=Path(path)==operation/'complete.json'
        try:
            return publish(path,raw)
        except archive_io.ArchivePublicationError as error:
            if state['in_completion']:
                state['uncertain'].append(error.publication_uncertain)
            raise
        finally: state['in_completion']=previous
    def barrier(self):
        if state['in_completion'] and self.path==operation:
            # The actual publisher has renamed its flushed staging file.
            assert (operation/'complete.json').is_file()
            if state['enabled']:
                raise OSError('owned post-rename directory-barrier failure')
            value=sync(self); state['confirmed_barriers']+=1; return value
        return sync(self)
    monkeypatch.setattr(archive_io,'publish_file',publishing)
    monkeypatch.setattr(pub._Directory,'sync',barrier)
    with pytest.raises(pub.PublicationError):
        pub.publish(destination,b'owned archive',b'owned signature',
            {'encrypted':False,'files':1,'full':False},operation_id=op)
    assert state['uncertain']==[True]
    before={path.name:(path.stat().st_ino,path.stat().st_mtime_ns,path.read_bytes())
            for path in (destination/name,destination/(name+'.sig.json'))}
    assert pub.inspect(destination,operation_id=op)['status']=='completion_visible'
    with pytest.raises(pub.PublicationError):
        with pub.locked(destination) as root: pub.read_completed(root,name)
    assert state['uncertain']==[True,True]
    state['enabled']=False
    with pub.locked(destination) as root:
        archive,signature,receipt=pub.read_completed(root,name)
    assert (archive,signature)==(b'owned archive',b'owned signature')
    assert receipt['completion_barrier_reconfirmed'] and state['confirmed_barriers']>0
    after={path.name:(path.stat().st_ino,path.stat().st_mtime_ns,path.read_bytes())
           for path in (destination/name,destination/(name+'.sig.json'))}
    assert before==after


def test_repeated_completed_reads_release_scoped_directory_references(tmp_path):
    op,live,dest,result = _seed(tmp_path,pub)
    with pub.locked(dest) as root:
        baseline = len(root.directories)
        for _ in range(4):
            assert pub.read_completed(root,result['name'])[2]['completion_barrier_reconfirmed']
            assert len(root.directories)==baseline and root.operations==[]

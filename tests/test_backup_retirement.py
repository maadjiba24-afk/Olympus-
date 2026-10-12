"""Owned logical retirement retains all backup bytes and its recovery intent."""
import os
from pathlib import Path
import uuid
import pytest
from olympus import backup_publication as pub


def create(root):
    return pub.publish(root,b'owned archive',b'owned sidecar',
        {'encrypted':False,'files':1,'full':False},operation_id=uuid.uuid4().hex)


@pytest.fixture
def owned(tmp_path):
    root=tmp_path/'backups';root.mkdir()
    return root


def pair(root,result):
    values={}
    for name in (result['name'],result['name']+'.sig.json'):
        path=root/name;info=path.stat()
        values[name]=(path.read_bytes(),info.st_dev,info.st_ino,info.st_size,info.st_mode,info.st_mtime_ns)
    return values


def images(root):
    return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_retirement_keeps_all_images_and_withdraws_local_consumption(owned):
    result=create(owned);op=result['operation_id'];before=images(owned);original=pair(owned,result)
    retired=pub.retire(owned,operation_id=op)
    assert retired['retired'] and retired['retirement_complete'] and retired['retained_images']
    assert retired['files_deleted']==0 and retired['retained_pair_bytes']==len(b'owned archiveowned sidecar')
    assert not retired['historical'] and pair(owned,result)==original
    after=images(owned)
    assert set(after)-set(before)=={pub.CONTROL+'/'+op+'/'+name for name in ('retirement-intent.json','retired.json')}
    assert all(after[name]==raw for name,raw in before.items())
    status=pub.inspect(owned,operation_id=op)
    assert status['retirement_status']=='completion_visible' and status['retirement_resumable']
    assert not status['resumable'] and status['conflicts']==[]
    with pub.locked(owned) as root:
        with pytest.raises(pub.PublicationError):pub.read_completed(root,result['name'])
    with pytest.raises(pub.PublicationError):pub.resume(owned,operation_id=op)
    assert pair(owned,result)==original


@pytest.mark.parametrize('leaf',['retirement-intent.json','retired.json'])
@pytest.mark.parametrize('after',[False,True])
def test_interrupted_retirement_resumes_same_id_without_republishing_backup(owned,monkeypatch,leaf,after):
    result=create(owned);op=result['operation_id'];original=pair(owned,result)
    publish=pub._Root.publish;armed=True
    def fault(self,path,raw):
        nonlocal armed
        if armed and path.name==leaf:
            armed=False
            if after:publish(self,path,raw)
            raise OSError('owned private interruption sentinel')
        return publish(self,path,raw)
    monkeypatch.setattr(pub._Root,'publish',fault)
    with pytest.raises(pub.PublicationError) as caught:pub.retire(owned,operation_id=op)
    assert not armed and caught.value.operation_id==op
    assert 'sentinel' not in str(caught.value) and caught.value.__cause__ is None and caught.value.__context__ is None
    assert pair(owned,result)==original
    monkeypatch.setattr(pub._Root,'publish',publish)
    status=pub.inspect(owned,operation_id=op)
    intent_visible=leaf!='retirement-intent.json' or after
    assert (status['retirement_status'] is not None)==intent_visible
    if intent_visible:
        with pub.locked(owned) as root:
            with pytest.raises(pub.PublicationError):pub.read_completed(root,result['name'])
    calls=[]
    def observed(self,path,raw):
        calls.append(path.name);return publish(self,path,raw)
    monkeypatch.setattr(pub._Root,'publish',observed)
    retired=pub.retire(owned,operation_id=op)
    assert retired['retirement_complete'] and calls==['retirement-intent.json','retired.json']
    assert pair(owned,result)==original


def test_retiring_historical_id_does_not_change_newest_publication(owned):
    old=create(owned);new=create(owned)
    old_pair,new_pair=pair(owned,old),pair(owned,new)
    head=(owned/pub.CONTROL/'head.json').read_bytes()
    with pub.locked(owned) as root:
        assert pub.read_completed(root,old['name'])[2]['publication_complete']
    pub.retire(owned,operation_id=old['operation_id'])
    assert pub.retire(owned,operation_id=old['operation_id'])['historical']
    assert pair(owned,old)==old_pair and pair(owned,new)==new_pair
    assert (owned/pub.CONTROL/'head.json').read_bytes()==head
    with pub.locked(owned) as root:
        assert pub.read_completed(root,new['name'])[2]['publication_complete']
        with pytest.raises(pub.PublicationError):pub.read_completed(root,old['name'])


@pytest.mark.parametrize('leaf',['retirement-intent.json','retired.json'])
def test_corrupted_retirement_evidence_is_not_repaired(owned,leaf):
    result=create(owned);op=result['operation_id'];pub.retire(owned,operation_id=op)
    path=owned/pub.CONTROL/op/leaf;path.write_bytes(b'owned corrupt evidence')
    before=images(owned)
    with pytest.raises(pub.PublicationError):pub.retire(owned,operation_id=op)
    with pytest.raises(pub.PublicationError):pub.inspect(owned,operation_id=op)
    assert images(owned)==before


def test_completed_retirement_without_intent_is_not_reinitialized(owned):
    result=create(owned);op=result['operation_id'];pub.retire(owned,operation_id=op)
    path=owned/pub.CONTROL/op/'retirement-intent.json';path.rename(path.with_name('retained-intent.json'))
    before=images(owned)
    with pytest.raises(pub.PublicationError):pub.retire(owned,operation_id=op)
    with pub.locked(owned) as root:
        with pytest.raises(Exception):pub.read_completed(root,result['name'])
    assert images(owned)==before and not path.exists()


def test_changed_retired_pair_is_never_reapplied(owned):
    result=create(owned);op=result['operation_id'];pub.retire(owned,operation_id=op)
    (owned/result['name']).write_bytes(b'owned later bytes')
    before=images(owned)
    with pytest.raises(pub.PublicationError):pub.retire(owned,operation_id=op)
    with pytest.raises(pub.PublicationError):pub.resume(owned,operation_id=op)
    assert not pub.inspect(owned,operation_id=op)['retirement_resumable']
    assert images(owned)==before


def test_retirement_does_not_claim_to_reclaim_history_capacity(owned,monkeypatch):
    monkeypatch.setattr(pub,'MAX_OPERATIONS',1)
    result=create(owned);pub.retire(owned,operation_id=result['operation_id'])
    before=images(owned)
    with pytest.raises(pub.PublicationError):create(owned)
    assert images(owned)==before


def test_incomplete_publication_must_resume_before_retirement(owned,monkeypatch):
    op=uuid.uuid4().hex;name=pub.archive_name(op,False);publish=pub._Root.publish
    def fault(self,path,raw):
        if path.name==name+'.sig.json':raise OSError('owned sidecar interruption')
        return publish(self,path,raw)
    monkeypatch.setattr(pub._Root,'publish',fault)
    with pytest.raises(pub.PublicationError):
        pub.publish(owned,b'owned archive',b'owned sidecar',{'encrypted':False,'files':1,'full':False},operation_id=op)
    monkeypatch.setattr(pub._Root,'publish',publish)
    before=images(owned)
    with pytest.raises(pub.PublicationError):pub.retire(owned,operation_id=op)
    assert images(owned)==before
    assert pub.resume(owned,operation_id=op)['publication_complete']
    assert pub.retire(owned,operation_id=op)['retirement_complete']


def test_operation_directory_remains_held_through_retirement_evidence(owned,monkeypatch):
    result=create(owned);op=result['operation_id'];publish=pub._Root.publish;observed={}
    def replace(self,path,raw):
        if path.name=='retirement-intent.json':
            directory=self.control.path/op;observed['guards']=list(self.operations)
            assert observed['guards']
            try:directory.rename(directory.with_name('retained-'+op))
            except OSError as error:
                assert os.name=='nt' and error.winerror in (5,32)
                observed['native_refusal']=True
                raise OSError('owned interruption after native rename refusal') from None
            directory.mkdir();(directory/'foreign.txt').write_bytes(b'owned foreign evidence')
            observed['replacement']=directory
        return publish(self,path,raw)
    monkeypatch.setattr(pub._Root,'publish',replace)
    with pytest.raises(pub.PublicationError):pub.retire(owned,operation_id=op)
    assert observed and all(g._closed for g in observed['guards'])
    if 'replacement' in observed:
        assert [p.name for p in observed['replacement'].iterdir()]==['foreign.txt']
        assert (observed['replacement']/'foreign.txt').read_bytes()==b'owned foreign evidence'



@pytest.mark.parametrize('consumer',['retire','read_completed'])
def test_active_head_wrong_valid_hash_refuses_before_any_publication(owned,monkeypatch,consumer):
    import json
    result=create(owned);op=result['operation_id'];original=pair(owned,result)
    path=owned/pub.CONTROL/'head.json';head=json.loads(path.read_bytes())
    wrong='0'*64 if head['plan_sha256']!='0'*64 else '1'*64
    head['plan_sha256']=wrong
    path.write_bytes(pub._json(head))
    before=images(owned);calls=[]
    def forbidden(self,path,raw):
        calls.append(path.name)
        raise AssertionError('publication must not follow mismatched active plan hash')
    monkeypatch.setattr(pub._Root,'publish',forbidden)
    with pytest.raises(pub.PublicationError):
        if consumer=='retire':pub.retire(owned,operation_id=op)
        else:
            with pub.locked(owned) as root:pub.read_completed(root,result['name'])
    assert calls==[] and images(owned)==before and pair(owned,result)==original

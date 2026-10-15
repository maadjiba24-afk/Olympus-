"""Actual native pair publication, fault recovery, lifetime and bounds controls."""
import hashlib
import os
from pathlib import Path
import threading
import uuid

import pytest

from olympus import backup_publication as pub


def new_id():
    return uuid.uuid4().hex


@pytest.fixture
def owned(tmp_path):
    root = tmp_path/'backups'; root.mkdir()
    return root


def create(root, operation_id=None):
    operation_id = new_id() if operation_id is None else operation_id
    return pub.publish(root,b'owned archive bytes',b'owned signature bytes',
        {'encrypted':False,'files':2,'full':False},operation_id=operation_id)


def images(root):
    return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_native_pair_is_complete_and_readable_only_through_live_lease(owned):
    result = create(owned)
    op = result['operation_id']; name = result['name']
    assert result['publication_complete'] and not result['historical']
    assert (owned/name).read_bytes()==b'owned archive bytes'
    assert (owned/(name+'.sig.json')).read_bytes()==b'owned signature bytes'
    assert (owned/pub.CONTROL/op/'archive.image').read_bytes()==b'owned archive bytes'
    assert (owned/pub.CONTROL/op/'signature.image').read_bytes()==b'owned signature bytes'
    with pub.locked(owned) as root:
        archive,signature,receipt = pub.read_completed(root,name)
        assert (archive,signature)==(b'owned archive bytes',b'owned signature bytes')
        assert receipt['archive_sha256']==hashlib.sha256(archive).hexdigest()
        assert receipt['signature_sha256']==hashlib.sha256(signature).hexdigest()
    with pytest.raises(Exception): pub.read_completed(root,name)
    before = images(owned)
    status = pub.inspect(owned,operation_id=op)
    assert status['status']=='completion_visible' and status['conflicts']==[] and status['resumable']
    assert images(owned)==before
    assert pub.resume(owned,operation_id=op)['historical']


@pytest.mark.parametrize('target', ['archive','signature','progress','complete','head'])
@pytest.mark.parametrize('after', [False,True])
def test_interrupted_pair_rolls_forward_exact_retained_images(owned,monkeypatch,target,after):
    op = new_id(); name = pub.archive_name(op,False)
    original = pub._Root.publish
    armed = True
    def fault(self,path,raw):
        nonlocal armed
        selected = ('archive' if path==owned/name else 'signature' if path==owned/(name+'.sig.json')
                    else 'progress' if path.name=='progress.json' else 'complete' if path.name=='complete.json'
                    else 'head' if path.name=='head.json' else None)
        if armed and selected==target:
            armed = False
            if after: original(self,path,raw)
            raise OSError('owned fault sentinel that must not appear in public error')
        return original(self,path,raw)
    monkeypatch.setattr(pub._Root,'publish',fault)
    with pytest.raises(pub.PublicationError) as caught:
        create(owned,op)
    assert caught.value.operation_id==op and not armed
    assert 'sentinel' not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    monkeypatch.setattr(pub._Root,'publish',original)
    status = pub.inspect(owned,operation_id=op)
    if target=='head' and not after:
        assert status['status']=='prepared_inactive' and not status['resumable']
        assert status['final_files_changed'] is False
        with pytest.raises(pub.PublicationError): pub.resume(owned,operation_id=op)
        assert not (owned/name).exists() and not (owned/(name+'.sig.json')).exists()
        return
    assert status['resumable'] and status['conflicts']==[]
    if status['status']!='completion_visible':
        with pub.locked(owned) as root:
            with pytest.raises(pub.PublicationError): pub.read_completed(root,name)
        with pytest.raises(pub.PublicationError) as blocked: create(owned)
        assert blocked.value.operation_id==op
    # Recovery cannot invoke key selection or signing again.
    from olympus import vault_custody as custody
    monkeypatch.setattr(custody,'capture',lambda *a,**k: pytest.fail('resume reacquired private custody'))
    result = pub.resume(owned,operation_id=op)
    assert result['publication_complete']
    assert (owned/name).read_bytes()==b'owned archive bytes'
    assert (owned/(name+'.sig.json')).read_bytes()==b'owned signature bytes'
    assert pub.inspect(owned,operation_id=op)['status']=='completion_visible'


@pytest.mark.parametrize('leaf', ['archive.image','signature.image','plan.json'])
def test_pre_intent_preparation_failure_is_retained_and_not_resumable(owned,monkeypatch,leaf):
    original = pub._Directory.write_new; op = new_id()
    def fault(self,name,raw):
        if name==leaf:
            raise OSError('owned preparation failure')
        return original(self,name,raw)
    monkeypatch.setattr(pub._Directory,'write_new',fault)
    with pytest.raises(pub.PublicationError): create(owned,op)
    monkeypatch.setattr(pub._Directory,'write_new',original)
    status = pub.inspect(owned,operation_id=op)
    assert status=={'operation_id':op,'status':'preparation_incomplete','resumable':False,'final_files_changed':False}
    with pytest.raises(pub.PublicationError): pub.resume(owned,operation_id=op)
    assert not (owned/pub.archive_name(op,False)).exists()
    assert (owned/pub.CONTROL/op).is_dir()
    # A different ID can make a new backup because the old intent never became active.
    assert create(owned)['publication_complete']


def interrupted_archive(owned,monkeypatch):
    op = new_id(); name = pub.archive_name(op,False); original = pub._Root.publish
    def fault(self,path,raw):
        result = original(self,path,raw)
        if path==owned/name: raise OSError('owned after-archive failure')
        return result
    monkeypatch.setattr(pub._Root,'publish',fault)
    with pytest.raises(pub.PublicationError): create(owned,op)
    monkeypatch.setattr(pub._Root,'publish',original)
    return op,name


@pytest.mark.parametrize('leaf', ['archive.image','signature.image','plan.json'])
def test_bad_retained_copy_refuses_before_the_missing_sidecar_is_written(owned,monkeypatch,leaf):
    op,name = interrupted_archive(owned,monkeypatch)
    path = owned/pub.CONTROL/op/leaf
    path.write_bytes(b'corrupted owned evidence')
    before = images(owned)
    with pytest.raises(pub.PublicationError): pub.resume(owned,operation_id=op)
    assert images(owned)==before and not (owned/(name+'.sig.json')).exists()


def test_conflicting_current_archive_stops_before_sidecar_publication(owned,monkeypatch):
    op,name = interrupted_archive(owned,monkeypatch)
    (owned/name).write_bytes(b'unrelated replacement')
    before = images(owned)
    status = pub.inspect(owned,operation_id=op)
    assert status['conflicts']==['archive'] and not status['resumable']
    with pytest.raises(pub.PublicationError): pub.resume(owned,operation_id=op)
    assert (owned/name).read_bytes()==b'unrelated replacement'
    assert not (owned/(name+'.sig.json')).exists()
    # Confirming the same active intent may replace head.json, but cannot change bytes.
    assert images(owned)==before


def test_completed_id_never_restores_a_later_changed_archive(owned):
    result = create(owned); path = owned/result['name']
    path.write_bytes(b'later owned content')
    with pytest.raises(pub.PublicationError): pub.resume(owned,operation_id=result['operation_id'])
    assert path.read_bytes()==b'later owned content'
    with pub.locked(owned) as root:
        with pytest.raises(pub.PublicationError): pub.read_completed(root,result['name'])


@pytest.mark.parametrize('target', ['marker','head','lock'])
def test_missing_control_evidence_is_never_reinitialized(owned,target):
    result = create(owned); op = result['operation_id']
    leaf = {'marker':'initialization.json','head':'head.json','lock':'publication.lock'}[target]
    path = owned/pub.CONTROL/leaf
    # Retain the exact old file under a new fixture-only name, rather than delete it.
    path.rename(path.with_name('retained-'+leaf))
    before = images(owned)
    with pytest.raises(pub.PublicationError): pub.resume(owned,operation_id=op)
    assert images(owned)==before and not path.exists()


def test_identical_initialization_replacement_breaks_operation_binding(owned):
    result = create(owned)
    marker = owned/pub.CONTROL/'initialization.json'
    retained = marker.with_name('retained-initialization.json')
    marker.rename(retained); marker.write_bytes(retained.read_bytes())
    before = images(owned)
    with pytest.raises(pub.PublicationError): pub.resume(owned,operation_id=result['operation_id'])
    assert images(owned)==before


def test_generated_name_without_journal_cannot_become_legacy(owned):
    path = owned/pub.archive_name(new_id(),False)
    path.write_bytes(b'preexisting owned archive')
    before = images(owned)
    with pytest.raises(pub.PublicationError): create(owned)
    assert images(owned)==before and not (owned/pub.CONTROL).exists()
    assert pub.managed_name(path.name.upper())


def test_unfinished_control_directory_is_not_repaired(owned):
    (owned/pub.CONTROL).mkdir()
    (owned/pub.CONTROL/'retained-fixture').write_bytes(b'keep')
    before = images(owned)
    with pytest.raises(pub.PublicationError): create(owned)
    assert images(owned)==before


def test_unrelated_legacy_files_are_preserved(owned):
    old = owned/'olympus-backup-20251001T000000Z.tar.gz'; old.write_bytes(b'legacy')
    unrelated = owned/'operator-evidence.txt'; unrelated.write_bytes(b'unrelated')
    result = create(owned)
    assert result['publication_complete']
    assert old.read_bytes()==b'legacy' and unrelated.read_bytes()==b'unrelated'


def test_native_lock_refuses_another_thread_and_foreign_lease(owned,monkeypatch):
    result = create(owned); errors = []; native_results = []
    before = images(owned)
    original = pub._try_lock
    def observe(fd):
        value = original(fd)
        native_results.append((threading.get_ident(),value))
        return value
    monkeypatch.setattr(pub,'_try_lock',observe)
    with pub.locked(owned) as root:
        def contender():
            try:
                with pub.locked(owned): pass
            except pub.PublicationError: errors.append('busy')
            try: pub.read_completed(root,result['name'])
            except Exception: errors.append('foreign-lease')
        thread = threading.Thread(target=contender); thread.start(); thread.join(5)
        assert not thread.is_alive() and errors==['busy','foreign-lease']
        assert native_results==[(threading.get_ident(),True),(thread.ident,False)]
        root.check()
    # LockFileEx also denies reads through other handles to its locked byte.
    # Compare all bytes, including the native lock file, after releasing it.
    assert images(owned)==before
    assert pub.resume(owned,operation_id=result['operation_id'])['historical']


@pytest.mark.parametrize('value', ['', 'A'*32, '../escape', True])
def test_invalid_operation_ids_do_not_create_journals(owned,value):
    with pytest.raises(ValueError): create(owned,value)
    assert list(owned.iterdir())==[]


@pytest.mark.parametrize('field,value', [('encrypted',1),('files',True),('files',-1),('full','true')])
def test_metadata_types_rejected_before_mutation(owned,field,value):
    details = {'encrypted':False,'files':2,'full':False}; details[field]=value
    with pytest.raises(ValueError): pub.publish(owned,b'a',b's',details,operation_id=new_id())
    assert list(owned.iterdir())==[]


def test_oversized_signature_is_refused_before_mutation(owned,monkeypatch):
    monkeypatch.setattr(pub.fmt,'MAX_SIGNATURE',4)
    with pytest.raises(pub.PublicationError): create(owned)
    assert list(owned.iterdir())==[]


def test_operation_history_is_bounded_without_pruning_evidence(owned,monkeypatch):
    monkeypatch.setattr(pub,'MAX_OPERATIONS',1)
    result = create(owned); before = images(owned)
    with pytest.raises(pub.PublicationError): create(owned)
    assert images(owned)==before
    assert pub.resume(owned,operation_id=result['operation_id'])['historical']


def test_same_id_requires_resume_and_cannot_replace_prepared_bytes(owned):
    result = create(owned); before = images(owned)
    with pytest.raises(pub.PublicationError):
        pub.publish(owned,b'different archive',b'different signature',
            {'encrypted':False,'files':2,'full':False},operation_id=result['operation_id'])
    assert images(owned)==before


def test_relative_directory_is_frozen_before_filesystem_callbacks(owned,monkeypatch,tmp_path):
    elsewhere = tmp_path/'elsewhere'; elsewhere.mkdir()
    monkeypatch.chdir(owned.parent)
    original = pub._Directory.entries; switched = False
    def drift(self):
        nonlocal switched
        if not switched:
            switched = True; os.chdir(elsewhere)
        yield from original(self)
    monkeypatch.setattr(pub._Directory,'entries',drift)
    result = create(Path('backups'))
    assert Path(result['path']).parent==owned and list(elsewhere.iterdir())==[]

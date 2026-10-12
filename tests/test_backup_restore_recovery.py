"""Offline backup recovery: actual retained images, barriers and conflicts."""
import hashlib
import json
import os
from pathlib import Path
import threading
import uuid

import pytest

from olympus import archive_io, backup_archive, backup_restore as restore


def _id():
    return uuid.uuid4().hex


def _evidence():
    return {'archive_sha256':hashlib.sha256(b'owned archive fixture').hexdigest(),
            'signature_sha256':None,'signed':False,'signature_ok':False,
            'signature_override':False,
            'archive_encryption':archive_io.encryption_metadata(plaintext=True)}


@pytest.fixture
def roots(tmp_path):
    live = tmp_path/'live'; live.mkdir()
    (live/'untouched').write_bytes(b'active memory unchanged')
    return live,tmp_path/'offline'


def _run(roots, changes=None, operation_id=None, **options):
    live,dest = roots
    return restore.restore(dest, {'a.txt':b'A','nested/b.txt':b'B'} if changes is None else changes,
                           _evidence(),operation_id=operation_id or _id(),live_root=live,**options)


def _resume(roots, opid):
    return restore.resume(roots[1],operation_id=opid,live_root=roots[0])


def _control(roots, opid):
    return roots[1]/restore.CONTROL/opid


def _images(directory):
    return {p.relative_to(directory).as_posix():p.read_bytes() for p in directory.rglob('*') if p.is_file()}


def test_restore_retains_exact_plan_images_and_completion(roots):
    opid = _id(); result = _run(roots,operation_id=opid)
    assert result['restored'] == 2 and result['operation_id'] == opid and not result['historical']
    plan = json.loads((_control(roots,opid)/'plan.json').read_bytes())
    assert plan['evidence'] == _evidence()
    assert [row['before_sha256'] for row in plan['files']] == [None,None]
    assert (_control(roots,opid)/'images-000/00000.after').read_bytes() == b'A'
    assert (roots[1]/'nested/b.txt').read_bytes() == b'B'
    assert (roots[0]/'untouched').read_bytes() == b'active memory unchanged'
    assert _resume(roots,opid)['historical'] is True


def test_empty_restore_has_owned_initialization_and_receipt(roots):
    opid = _id(); assert _run(roots,{},opid)['restored'] == 0
    assert _resume(roots,opid)['historical']
    assert (roots[1]/restore.CONTROL/'initialization.json').is_file()


@pytest.mark.parametrize('occupied',[False,True])
@pytest.mark.parametrize('force',[False,True])
def test_existing_unmanaged_directory_refused_even_empty_or_forced(roots,occupied,force):
    roots[1].mkdir()
    if occupied:
        (roots[1]/'keep').write_bytes(b'original')
    before = _images(roots[1])
    with pytest.raises(restore.RestoreError) as caught:
        _run(roots,force=force)
    assert caught.value.phase == 'initialization'
    assert _images(roots[1]) == before


@pytest.mark.parametrize('choice',['live','child','ancestor'])
def test_live_root_overlap_refused_before_destination_mutation(roots,choice):
    live,_ = roots
    dest = {'live':live,'child':live/'child','ancestor':live.parent}[choice]
    before = _images(live)
    with pytest.raises(restore.RestoreError):
        _run((live,dest))
    assert _images(live) == before
    assert not (dest/restore.CONTROL).exists()


@pytest.mark.skipif(os.name != 'nt', reason='native Windows path namespace aliases')
@pytest.mark.parametrize('choice',['live','child','ancestor'])
def test_windows_extended_namespace_cannot_bypass_live_overlap(roots,choice):
    live,_ = roots
    dest = {'live':live,'child':live/'child','ancestor':live.parent}[choice]
    alias = Path('\\\\?\\'+str(dest))
    with pytest.raises(restore.RestoreError):
        _run((live,alias))
    assert (live/'untouched').read_bytes() == b'active memory unchanged'
    assert not (dest/restore.CONTROL).exists()


def test_input_and_custody_containment_refused_without_reading_file(roots):
    roots[1].mkdir()
    custody = roots[1]/'private-key'
    custody.write_bytes(b'owned secret fixture')
    before = _images(roots[1])
    with pytest.raises(restore.RestoreError):
        _run(roots,forbidden=[custody],force=True)
    assert _images(roots[1]) == before


@pytest.mark.parametrize('where',['after_image','plan'])
def test_pre_intent_failure_retained_and_classified_not_resumable(roots,monkeypatch,where):
    opid = _id(); original = restore._Directory.write_new
    def fail(directory,name,raw):
        original(directory,name,raw)
        if name == ('00000.after' if where == 'after_image' else 'plan.json'):
            raise OSError('owned simulated lost acknowledgment')
    monkeypatch.setattr(restore._Directory,'write_new',fail)
    with pytest.raises(restore.RestoreError) as caught:
        _run(roots,operation_id=opid)
    assert caught.value.operation_id == opid and caught.value.phase == 'preparation'
    assert not (roots[1]/'a.txt').exists()
    monkeypatch.setattr(restore._Directory,'write_new',original)
    status = restore.inspect(roots[1],operation_id=opid,live_root=roots[0])
    assert status['status'] == ('preparation_incomplete' if where == 'after_image' else 'prepared_inactive')
    assert status['resumable'] is False and status['target_data_changed'] is False
    with pytest.raises(restore.RestoreError,match='not the active'):
        _resume(roots,opid)


@pytest.mark.parametrize('where',['initialization.json','head.json'])
def test_interrupted_initialization_never_adopts_existing_directory(roots,monkeypatch,where):
    original = restore._Directory.write_new
    def fail(directory,name,raw):
        if name == where:
            raise OSError('owned initialization fault')
        return original(directory,name,raw)
    monkeypatch.setattr(restore._Directory,'write_new',fail)
    with pytest.raises(restore.RestoreError) as caught:
        _run(roots)
    assert caught.value.phase == 'initialization'
    before = _images(roots[1])
    monkeypatch.setattr(restore._Directory,'write_new',original)
    with pytest.raises(restore.RestoreError):
        _run(roots,force=True)
    assert _images(roots[1]) == before and not (roots[1]/'a.txt').exists()


@pytest.mark.parametrize('boundary',['head.json','a.txt','progress.json','complete.json'])
def test_lost_publication_acknowledgment_resumes_exact_retained_images(roots,monkeypatch,boundary):
    opid = _id(); original = archive_io.publish_file; fired = []
    def publish(path,raw):
        original(path,raw)
        if Path(path).name == boundary and not fired:
            fired.append(str(path))
            raise OSError('owned lost publication acknowledgment')
    monkeypatch.setattr(archive_io,'publish_file',publish)
    with pytest.raises(restore.RestoreError) as caught:
        _run(roots,operation_id=opid)
    assert fired and caught.value.operation_id == opid
    retained = _images(_control(roots,opid))
    monkeypatch.setattr(archive_io,'publish_file',original)
    result = _resume(roots,opid)
    assert result['restored'] == 2
    assert (roots[1]/'a.txt').read_bytes() == b'A'
    assert (roots[1]/'nested/b.txt').read_bytes() == b'B'
    for name,raw in retained.items():
        if name not in ('progress.json','complete.json'):
            assert (_control(roots,opid)/name).read_bytes() == raw


def _interrupt_after_first_target(roots,monkeypatch):
    opid = _id(); original = archive_io.publish_file
    def publish(path,raw):
        original(path,raw)
        if Path(path) == roots[1]/'a.txt':
            raise OSError('owned stop after first target')
    monkeypatch.setattr(archive_io,'publish_file',publish)
    with pytest.raises(restore.RestoreError):
        _run(roots,operation_id=opid)
    monkeypatch.setattr(archive_io,'publish_file',original)
    return opid


def test_all_targets_preflight_blocks_earlier_republication_on_later_conflict(roots,monkeypatch):
    opid = _interrupt_after_first_target(roots,monkeypatch)
    (roots[1]/'nested').mkdir()
    (roots[1]/'nested/b.txt').write_bytes(b'conflict')
    published = []; original = archive_io.publish_file
    def spy(path,raw):
        if restore.CONTROL not in Path(path).parts:
            published.append(path)
        return original(path,raw)
    monkeypatch.setattr(archive_io,'publish_file',spy)
    with pytest.raises(restore.RestoreError,match='conflicts'):
        _resume(roots,opid)
    assert published == []
    assert (roots[1]/'nested/b.txt').read_bytes() == b'conflict'


@pytest.mark.parametrize('name',['plan.json','images-000/00000.after'])
def test_damaged_plan_or_image_refused_before_any_target_write(roots,monkeypatch,name):
    opid = _interrupt_after_first_target(roots,monkeypatch)
    path = _control(roots,opid)/name
    path.write_bytes(b'damaged owned fixture')
    before = _images(roots[1])
    with pytest.raises(restore.RestoreError):
        _resume(roots,opid)
    assert (roots[1]/'a.txt').read_bytes() == b'A'
    assert not (roots[1]/'nested/b.txt').exists()
    # Intent may be reconfirmed, but every retained byte stays unchanged.
    assert _images(roots[1]) == before


def test_resume_reconfirms_initialization_and_head_before_data(roots,monkeypatch):
    opid = _interrupt_after_first_target(roots,monkeypatch)
    events = []; original = archive_io.publish_file; original_fsync = os.fsync
    def sync(fd):
        events.append('fsync')
        return original_fsync(fd)
    def publish(path,raw):
        events.append(Path(path).name)
        return original(path,raw)
    monkeypatch.setattr(os,'fsync',sync)
    monkeypatch.setattr(archive_io,'publish_file',publish)
    _resume(roots,opid)
    assert events.index('fsync') < events.index('head.json') < events.index('a.txt')
    assert events.index('a.txt') < events.index('complete.json')


def test_visible_after_image_is_republished_to_repeat_its_barriers(roots,monkeypatch):
    opid = _interrupt_after_first_target(roots,monkeypatch)
    events = []; original = archive_io.publish_file
    def publish(path,raw):
        events.append((Path(path),raw))
        return original(path,raw)
    monkeypatch.setattr(archive_io,'publish_file',publish)
    _resume(roots,opid)
    assert (roots[1]/'a.txt',b'A') in events


def test_completed_id_never_reapplies_later_changed_data(roots):
    opid = _id(); _run(roots,operation_id=opid)
    (roots[1]/'a.txt').write_bytes(b'later owned edit')
    with pytest.raises(restore.RestoreError,match='completed historically'):
        _resume(roots,opid)
    assert (roots[1]/'a.txt').read_bytes() == b'later owned edit'


def test_force_new_operation_keeps_before_images_unrelated_and_prior_evidence(roots):
    first = _id(); _run(roots,operation_id=first)
    old_evidence = _images(_control(roots,first))
    (roots[1]/'a.txt').write_bytes(b'owned original')
    (roots[1]/'unrelated').write_bytes(b'keep')
    second = _id()
    with pytest.raises(restore.RestoreError,match='requires force'):
        _run(roots,{'a.txt':b'new'},second)
    _run(roots,{'a.txt':b'new'},second,force=True)
    assert (_control(roots,second)/'images-000/00000.before').read_bytes() == b'owned original'
    assert (roots[1]/'a.txt').read_bytes() == b'new'
    assert (roots[1]/'unrelated').read_bytes() == b'keep'
    assert _images(_control(roots,first)) == old_evidence


def test_before_aggregate_bound_refuses_before_publication(roots,monkeypatch):
    _run(roots,{'a.txt':b'a','b.txt':b'b'})
    for name,raw in [('a.txt',b'AAAA'),('b.txt',b'BBBB')]:
        (roots[1]/name).write_bytes(raw)
    before = {name:(roots[1]/name).read_bytes() for name in ('a.txt','b.txt')}
    reads = []; original = archive_io.read_file
    def read(path,cap,**kwargs):
        if Path(path).parent == roots[1]:
            reads.append((Path(path).name,cap))
        return original(path,cap,**kwargs)
    monkeypatch.setattr(archive_io,'read_file',read)
    monkeypatch.setattr(backup_archive,'MAX_SOURCE',6)
    with pytest.raises(restore.RestoreError) as caught:
        _run(roots,{'a.txt':b'a','b.txt':b'b'},force=True)
    assert caught.value.phase == 'preparation'
    assert ('a.txt',6) in reads and ('b.txt',2) in reads
    assert {name:(roots[1]/name).read_bytes() for name in before} == before


@pytest.mark.parametrize('damaged',['initialization.json','head.json','restore.lock'])
def test_missing_control_evidence_is_not_reinitialized(roots,damaged):
    opid = _id(); _run(roots,operation_id=opid)
    source = roots[1]/restore.CONTROL/damaged
    source.rename(source.with_name(source.name+'.retained'))
    before = _images(roots[1])
    with pytest.raises(restore.RestoreError):
        _resume(roots,opid)
    assert _images(roots[1]) == before


def test_identical_marker_replacement_refused_by_identity(roots):
    opid = _id(); _run(roots,operation_id=opid)
    marker = roots[1]/restore.CONTROL/'initialization.json'
    raw = marker.read_bytes()
    marker.rename(marker.with_name('initialization.retained'))
    marker.write_bytes(raw)
    with pytest.raises(restore.RestoreError):
        _resume(roots,opid)
    assert (roots[1]/'a.txt').read_bytes() == b'A'


def test_native_restore_lock_excludes_another_thread_and_then_releases(roots,monkeypatch):
    opid = _id(); _run(roots,operation_id=opid)
    locks = []; original = restore._try_lock
    def lock(fd):
        result = original(fd)
        locks.append((threading.get_ident(),result))
        return result
    monkeypatch.setattr(restore,'_try_lock',lock)
    with restore._authority(roots[1],roots[0],(),opid,initialize=False):
        results = []
        def contender():
            try:
                _resume(roots,opid)
            except restore.RestoreError:
                results.append('busy')
        thread = threading.Thread(target=contender)
        thread.start(); thread.join(10)
        assert not thread.is_alive() and results == ['busy']
        assert locks == [(threading.get_ident(),True),(thread.ident,False)]
    assert _resume(roots,opid)['historical']


def test_projected_directory_capacity_refuses_before_any_payload(roots,monkeypatch):
    monkeypatch.setattr(restore,'MAX_DIRECTORY_ENTRIES',3)
    with pytest.raises(restore.RestoreError):
        _run(roots,{f'{n}.txt':b'x' for n in range(4)})
    assert not list(roots[1].glob('*.txt'))


def test_existing_unrelated_entries_and_retained_staging_count_toward_capacity(roots,monkeypatch):
    opid = _interrupt_after_first_target(roots,monkeypatch)
    for name in ('keep1','keep2','.archive-owned-failure.tmp'):
        (roots[1]/name).write_bytes(b'keep')
    monkeypatch.setattr(restore,'MAX_DIRECTORY_ENTRIES',5)
    with pytest.raises(restore.RestoreError):
        _resume(roots,opid)
    assert not (roots[1]/'nested').exists()
    monkeypatch.setattr(restore,'MAX_DIRECTORY_ENTRIES',6)
    assert _resume(roots,opid)['restored'] == 2
    assert (roots[1]/'.archive-owned-failure.tmp').read_bytes() == b'keep'


@pytest.mark.parametrize('rel',['../escape','a/../../escape','CON','trail.','a:stream',
                                '.olympus-restore/head.json','nested/signing_seed'])
def test_unsafe_or_reserved_paths_refused_before_destination_exists(roots,rel):
    with pytest.raises((ValueError,restore.RestoreError)):
        _run(roots,{rel:b'unsafe'})
    assert not roots[1].exists()


def test_unicode_targets_use_exact_portable_spelling(roots):
    result = _run(roots,{'données/日本語.txt':b'owned unicode image'})
    assert result['restored'] == 1
    assert (roots[1]/'données/日本語.txt').read_bytes() == b'owned unicode image'


@pytest.mark.parametrize('paths',[('Dir/a.txt','dir/b.txt'),('Straße/a.txt','STRASSE/b.txt'),
                                 ('parent','parent/child')])
def test_intermediate_component_collisions_refused_before_destination_creation(roots,paths):
    with pytest.raises((ValueError,restore.RestoreError)):
        _run(roots,{name:b'owned image' for name in paths})
    assert not roots[1].exists()
    payloads = {'data/'+name:b'owned image' for name in paths}
    manifest = {'schema':backup_archive.SCHEMA,'created':'owned','olympus_version':'test','host':'owned',
                'full':False,'file_count':len(paths),
                'files':[{'path':name,'bytes':11,'sha256':hashlib.sha256(b'owned image').hexdigest()} for name in paths]}
    with pytest.raises(backup_archive.BackupFormatError,match='collision'):
        backup_archive.validate_inventory(manifest,payloads)


def test_baseexception_drains_native_authorities_and_retains_id(roots,monkeypatch):
    opid = _id(); original = archive_io.publish_file
    def interrupted(path,raw):
        if Path(path).name == 'a.txt':
            raise KeyboardInterrupt()
        return original(path,raw)
    monkeypatch.setattr(archive_io,'publish_file',interrupted)
    with pytest.raises(KeyboardInterrupt) as caught:
        _run(roots,operation_id=opid)
    assert caught.value.operation_id == opid
    monkeypatch.setattr(archive_io,'publish_file',original)
    assert _resume(roots,opid)['restored'] == 2


def test_caller_changes_dictionary_cannot_change_captured_plan_during_io(roots,monkeypatch):
    changes = {'a.txt':b'captured'}
    original = restore._exclusive_child
    def create(parent,name):
        changes.clear()
        changes['unreviewed.txt'] = b'late caller mutation'
        return original(parent,name)
    monkeypatch.setattr(restore,'_exclusive_child',create)
    result = _run(roots,changes)
    assert result['restored'] == 1
    assert (roots[1]/'a.txt').read_bytes() == b'captured'
    assert not (roots[1]/'unreviewed.txt').exists()


def test_protected_path_generator_is_frozen_once_and_reaches_native_check(roots,monkeypatch):
    parent = roots[0].parent/'protected'; parent.mkdir()
    selected = parent/'selected-custody'
    yielded,opened = [],[]
    def paths():
        yielded.append(str(selected))
        yield selected
    original = restore._Directory.__init__
    def initialize(directory,path,**kwargs):
        opened.append(Path(path))
        return original(directory,path,**kwargs)
    monkeypatch.setattr(restore._Directory,'__init__',initialize)
    assert _run(roots,forbidden=paths())['restored'] == 2
    assert yielded == [str(selected)] and parent in opened
    assert not selected.exists()  # no credential file read or creation required


def test_protected_path_iterator_has_an_enforced_consumption_bound(roots):
    yielded = []
    def paths():
        while True:
            yielded.append(1)
            yield roots[0]/'selected-custody'
    with pytest.raises(restore.RestoreError):
        _run(roots,forbidden=paths())
    assert len(yielded) == restore.MAX_FORBIDDEN_PATHS + 1
    assert not roots[1].exists()


def test_missing_selected_live_root_refuses_without_creating_either_directory(roots):
    missing = roots[0].parent/'missing-live'
    with pytest.raises(restore.RestoreError) as caught:
        _run((missing,roots[1]))
    assert caught.value.phase == 'initialization'
    assert not missing.exists() and not roots[1].exists()

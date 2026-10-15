"""Owned catalog observations and byte-preserving logical retirement."""
import os
from pathlib import Path
import uuid
import pytest
from olympus import backup, backup_management as management, backup_publication as publication
from olympus import archive_io as aio, backup_signing as signing, config, witness


@pytest.fixture
def owned(tmp_path,monkeypatch):
    root=tmp_path/'memory';root.mkdir();(root/'data.txt').write_bytes(b'owned catalog data')
    package=tmp_path/'package';package.mkdir()
    monkeypatch.setattr(config,'MEMORY_DIR',root)
    monkeypatch.setattr(witness,'_package_dir',lambda:package)
    for name in (*signing._NAMES,'OLYMPUS_SECRET_KEY','OLYMPUS_SECRET_KEY_FILE'):
        monkeypatch.delenv(name,raising=False)
    monkeypatch.setenv('OLYMPUS_ENV','test')
    return root


def files(root):
    return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}


def create_ordered(count=3):
    rows=[]
    for i in range(count):
        result=backup.create();os.utime(result['path'],(1000+i,1000+i));rows.append(result)
    return rows


def test_missing_backups_listing_and_retirement_do_not_create_directory(owned):
    before=files(owned)
    assert backup.list_backups()==[]
    result=backup.prune(keep=0)
    assert result['retired_count']==0 and result['files_deleted']==0 and result['batch_complete']
    assert files(owned)==before and not (owned/'backups').exists()


def test_catalog_is_observational_and_does_not_claim_reconfirmed_barriers(owned):
    created=create_ordered(2);before=files(owned)
    rows=backup.list_backups()
    assert [row['operation_id'] for row in rows]==[item['operation_id'] for item in reversed(created)]
    assert all(row['status']=='completion_visible' and row['publication_complete'] is None for row in rows)
    assert all(row['completion_visible'] and not row['completion_barrier_reconfirmed'] for row in rows)
    assert all(row['selection_candidate'] and not row['encryption_verified'] and row['signer_authenticated'] is None for row in rows)
    assert all(row['ordering_source']=='observed_filesystem_mtime' for row in rows)
    assert files(owned)==before


def test_lost_completion_barrier_is_visible_but_never_claimed_reconfirmed_by_listing(owned,monkeypatch):
    original=publication._Root.publish;fired=[]
    def publish(root,path,raw):
        original(root,path,raw)
        if path.name=='complete.json' and not fired:
            fired.append(True);raise OSError('owned lost completion acknowledgement')
    monkeypatch.setattr(publication._Root,'publish',publish)
    with pytest.raises(backup.BackupError):backup.create()
    monkeypatch.setattr(publication._Root,'publish',original)
    before=files(owned);rows=backup.list_backups()
    assert fired and len(rows)==1 and rows[0]['status']=='completion_visible'
    assert rows[0]['publication_complete'] is None and not rows[0]['completion_barrier_reconfirmed']
    assert files(owned)==before


def test_retirement_keeps_every_original_byte_and_history_capacity(owned):
    created=create_ordered();before=files(owned)
    result=backup.prune(keep=1)
    assert result['retired_count']==2 and result['batch_complete']
    assert result['files_deleted']==result['bytes_reclaimed']==result['history_capacity_reclaimed']==0
    assert set(result['retired_operations'])=={row['operation_id'] for row in created[:2]}
    after=files(owned);assert all(after[name]==raw for name,raw in before.items())
    rows=backup.list_backups();assert len(rows)==3
    assert [row['operation_id'] for row in rows if row['selection_candidate']]==[created[-1]['operation_id']]
    assert sum(row['retirement_status']=='completion_visible' for row in rows)==2
    assert backup.prune(keep=1)['retired_count']==0


def test_legacy_rows_never_derive_encryption_from_suffix_or_enter_retirement(owned):
    folder=owned/'backups';folder.mkdir()
    legacy=folder/'olympus-backup-legacy.tar.gz.enc';legacy.write_bytes(b'owned plaintext with misleading suffix')
    before=files(owned);rows=backup.list_backups()
    assert len(rows)==1 and rows[0]['status']=='legacy_unverified' and rows[0]['encrypted'] is None
    assert not rows[0]['selection_candidate'] and not rows[0]['publication_complete']
    assert backup.prune(keep=0)['retired_count']==0 and files(owned)==before
    create_ordered(1);assert len(backup.list_backups())==2
    assert backup.prune(keep=0)['retired_count']==1 and legacy.read_bytes()==before['backups/'+legacy.name]


@pytest.mark.parametrize('keep',[-1,True,1.5,'1',None])
def test_retirement_configuration_is_bounded_before_any_mutation(owned,monkeypatch,keep):
    if keep is None:monkeypatch.setattr(config,'backup_keep',lambda:publication.MAX_OPERATIONS+1)
    before=files(owned)
    with pytest.raises(backup.BackupError):backup.prune(keep=keep)
    assert files(owned)==before and not (owned/'backups').exists()


@pytest.mark.parametrize('method',['list_backups','prune'])
def test_catalog_admission_precedes_large_image_reads(owned,monkeypatch,method):
    monkeypatch.setattr(management,'_directories',lambda:pytest.fail('busy admission reached directory acquisition'))
    with aio.operation('notes',selectors={},capture_key=False):
        with pytest.raises(backup.BackupError):getattr(backup,method)()


def test_reserved_pair_without_journal_is_unavailable_not_legacy(owned):
    folder=owned/'backups';folder.mkdir()
    (folder/publication.archive_name(uuid.uuid4().hex,False)).write_bytes(b'owned orphan')
    before=files(owned)
    with pytest.raises(backup.BackupError):backup.list_backups()
    with pytest.raises(backup.BackupError):backup.prune(keep=0)
    assert files(owned)==before and not (folder/publication.CONTROL).exists()


@pytest.mark.parametrize('witness_name',['progress.json','complete.json','retirement-intent.json','retired.json'])
def test_missing_inactive_plan_with_postplan_evidence_is_not_preparation(owned,witness_name):
    create_ordered(1)
    operation_id=uuid.uuid4().hex
    with publication.locked(owned/'backups') as root:
        directory=publication._exclusive_child(root.control,operation_id)
        directory.close()
    op=owned/'backups'/publication.CONTROL/operation_id
    before_rows=backup.list_backups()
    row=next(row for row in before_rows if row.get('operation_id')==operation_id)
    assert row['status']=='preparation_incomplete' and not row['selection_candidate']
    assert list(op.iterdir())==[]
    (op/witness_name).write_bytes(b'owned contradictory receipt')
    before=files(owned)
    with pytest.raises(backup.BackupError):backup.list_backups()
    assert files(owned)==before


def test_aggregate_catalog_budget_refuses_before_retained_image_payload_read(owned,monkeypatch):
    create_ordered(1);before=files(owned)
    monkeypatch.setattr(publication,'MAX_CATALOG_IMAGE_BYTES',1)
    original=aio.read_file;reads=[]
    def read(path,cap,**kwargs):
        if path.name in ('archive.image','signature.image'):
            reads.append(path);pytest.fail('over-budget catalog acquired large image')
        return original(path,cap,**kwargs)
    monkeypatch.setattr(aio,'read_file',read)
    with pytest.raises(backup.BackupError):backup.list_backups()
    assert reads==[] and files(owned)==before


def test_catalog_current_file_size_conflict_refuses_before_large_payload_read(owned,monkeypatch):
    created=create_ordered(1)[0];path=Path(created['path']);path.write_bytes(path.read_bytes()+b'owned appended bytes')
    original=os.read;payload_reads=[]
    # The descriptor's size is rejected before its first read. Compare owned
    # inode rather than path strings or unrelated descriptor activity.
    inode=path.stat().st_ino
    def read(fd,size):
        if os.fstat(fd).st_ino==inode:payload_reads.append(size)
        return original(fd,size)
    monkeypatch.setattr(os,'read',read)
    with pytest.raises(backup.BackupError):backup.list_backups()
    assert payload_reads==[]


def test_second_retirement_failure_reports_first_completed_id_and_failed_id(owned,monkeypatch):
    created=create_ordered();original=publication._retire_held;calls=[]
    def retire(root,op):
        calls.append(op)
        if len(calls)==2:raise OSError('owned second retirement failure sentinel')
        return original(root,op)
    monkeypatch.setattr(publication,'_retire_held',retire)
    with pytest.raises(backup.BackupError) as caught:backup.prune(keep=0)
    assert len(calls)==2 and caught.value.operation_id==calls[1]
    assert caught.value.completed_retirement_ids==(calls[0],) and caught.value.files_deleted==0
    assert 'sentinel' not in str(caught.value) and caught.value.__context__ is None and caught.value.__cause__ is None
    assert all(Path(row['path']).exists() for row in created)


def test_interrupted_retirement_remains_visible_and_never_counts_as_complete(owned,monkeypatch):
    created=create_ordered(1)[0];original=publication._Root.publish
    def publish(root,path,raw):
        if path.name=='retired.json':raise OSError('owned retirement completion interruption')
        return original(root,path,raw)
    monkeypatch.setattr(publication._Root,'publish',publish)
    with pytest.raises(backup.BackupError) as caught:backup.prune(keep=0)
    assert caught.value.operation_id==created['operation_id']
    monkeypatch.setattr(publication._Root,'publish',original)
    before=files(owned);rows=backup.list_backups()
    assert rows[0]['retirement_status']=='intent_visible' and not rows[0]['selection_candidate']
    result=backup.prune(keep=0)
    assert result['batch_complete'] and result['retired_count']==0 and 'retirement_complete' not in result
    assert result['pending_retirement_ids']==[created['operation_id']] and files(owned)==before


def test_catalog_holds_operation_directory_before_image_reads(owned,monkeypatch):
    created=create_ordered(1)[0];op=owned/'backups'/publication.CONTROL/created['operation_id']
    original=publication._load;observed=[]
    def load(root,operation_id,*args):
        assert root.operations and any(d.path==op for d in root.operations)
        try:op.rename(op.with_name(op.name+'-retained'))
        except OSError as error:
            assert os.name=='nt' and error.winerror in (5,32)
            observed.append('native-refusal');raise OSError('owned native move refused')
        op.mkdir();(op/'foreign').write_bytes(b'untouched foreign')
        observed.append('replaced')
        return original(root,operation_id,*args)
    monkeypatch.setattr(publication,'_load',load)
    with pytest.raises(backup.BackupError):backup.list_backups()
    assert observed
    if observed==['replaced']:assert {p.name:p.read_bytes() for p in op.iterdir()}=={'foreign':b'untouched foreign'}


def test_active_budget_preview_must_match_head_before_any_image_read(owned,monkeypatch):
    import json
    created=create_ordered(1)[0]
    op=owned/'backups'/publication.CONTROL/created['operation_id'];path=op/'plan.json'
    original_plan=path.read_bytes();preview=json.loads(original_plan)
    preview['archive_bytes']=1;preview['signature_bytes']=1
    path.write_bytes(json.dumps(preview).encode())
    original=aio.read_file;swapped=[];image_reads=[]
    def read(selected,cap,**kwargs):
        if selected.parent==op and selected.name in ('archive.image','signature.image'):
            image_reads.append(selected);pytest.fail('mismatched budget preview reached image reads')
        result=original(selected,cap,**kwargs)
        if selected==path and not swapped:
            path.write_bytes(original_plan);swapped.append(True)
        return result
    monkeypatch.setattr(aio,'read_file',read)
    with pytest.raises(backup.BackupError):backup.list_backups()
    assert swapped==[True] and image_reads==[] and path.read_bytes()==original_plan


def test_lost_retirement_completion_ack_is_not_settled_by_later_noop_batch(owned,monkeypatch):
    created=create_ordered(1)[0];original=publication._Root.publish;fired=[]
    def publish(root,path,raw):
        original(root,path,raw)
        if path.name=='retired.json' and not fired:
            fired.append(True);raise OSError('owned lost retirement acknowledgement')
    monkeypatch.setattr(publication._Root,'publish',publish)
    with pytest.raises(backup.BackupError) as caught:backup.prune(keep=0)
    assert fired and caught.value.operation_id==created['operation_id']
    assert caught.value.completed_retirement_ids==()
    monkeypatch.setattr(publication._Root,'publish',original)
    before=files(owned);result=backup.prune(keep=0)
    assert result['batch_complete'] and result['retired_count']==0 and result['retired_operations']==[]
    assert result['observed_retired_ids']==[created['operation_id']]
    assert not result['observed_retirement_barriers_reconfirmed'] and 'retirement_complete' not in result
    assert files(owned)==before


def test_retirement_interrupt_keeps_identity_and_failed_member_id(owned,monkeypatch):
    created=create_ordered(1)[0];failure=KeyboardInterrupt('owned interruption')
    def retire(root,op):raise failure
    monkeypatch.setattr(publication,'_retire_held',retire)
    with pytest.raises(KeyboardInterrupt) as caught:backup.prune(keep=0)
    assert caught.value is failure and caught.value.operation_id==created['operation_id']
    assert caught.value.completed_retirement_ids==()


def test_postbatch_cleanup_failure_preserves_completed_retirement_facts(owned,monkeypatch):
    created=create_ordered(1)[0];armed=[];fired=[]
    original=publication._retire_held;close=publication._Directory.close
    def retire(root,op):
        result=original(root,op);armed.append(True);return result
    def fail_close(directory):
        close(directory)
        if armed and directory.path==owned and not fired:
            fired.append(True);raise OSError('owned late batch cleanup failure')
    monkeypatch.setattr(publication,'_retire_held',retire)
    monkeypatch.setattr(publication._Directory,'close',fail_close)
    with pytest.raises(backup.BackupError) as caught:backup.prune(keep=0)
    assert armed and fired and caught.value.completed_retirement_ids==(created['operation_id'],)
    assert caught.value.files_deleted==0 and caught.value.__context__ is None and caught.value.__cause__ is None

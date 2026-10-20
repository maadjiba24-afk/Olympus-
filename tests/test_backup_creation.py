"""Owned public backup creation integration; no off-machine delivery."""
import hashlib
import os
from pathlib import Path
import uuid
import pytest
from olympus import backup, config, archive_io as aio, backup_archive as fmt
from olympus import backup_signing as signing, backup_snapshot as snapshot


@pytest.fixture
def owned(tmp_path,monkeypatch):
    root=tmp_path/'memory';root.mkdir()
    (root/'owned data.txt').write_bytes(b'owned source bytes')
    monkeypatch.setattr(config,'MEMORY_DIR',root)
    for name in (*signing._NAMES,'OLYMPUS_SECRET_KEY','OLYMPUS_SECRET_KEY_FILE'):
        monkeypatch.delenv(name,raising=False)
    monkeypatch.setenv('OLYMPUS_ENV','test')
    return root


def no_pair(root):
    return not (root/'backups').exists() or not list((root/'backups').glob('olympus-backup-v2-*'))


def test_manifest_and_tar_use_each_captured_image_once(owned,monkeypatch):
    original=aio.read_file;calls=[]
    def read(path,cap,**kwargs):
        if path==owned/'owned data.txt':calls.append(path)
        return original(path,cap,**kwargs)
    monkeypatch.setattr(aio,'read_file',read)
    result=backup.create(operation_id=uuid.uuid4().hex)
    raw=Path(result['path']).read_bytes()
    manifest,images=fmt.decode(raw)
    assert calls==[owned/'owned data.txt'] and images=={'owned data.txt':b'owned source bytes'}
    assert manifest['files']==[{'path':'owned data.txt','bytes':len(b'owned source bytes'),
        'sha256':hashlib.sha256(b'owned source bytes').hexdigest()}]
    assert result['publication_complete'] and result['signed'] and result['signature_ok']
    assert result['signer_default'] and not result['signer_authenticated']
    assert result['backup_scope']=='filesystem_memory_only' and not result['database_included']
    assert not result['application_consistent_snapshot'] and result['sha256']==hashlib.sha256(raw).hexdigest()
    assert Path(result['path']+'.sig.json').is_file()


def test_source_root_is_held_before_signer_callback(owned,monkeypatch):
    original=signing.capture_signer;observed=[]
    def capture():
        try:owned.rename(owned.with_name('retained-memory'))
        except OSError as error:
            assert os.name=='nt' and error.winerror in (5,32)
            observed.append('native-refusal')
            raise RuntimeError('owned private sentinel')
        observed.append('replaced')
        owned.mkdir();(owned/'foreign').write_bytes(b'foreign bytes')
        return original()
    monkeypatch.setattr(signing,'capture_signer',capture)
    monkeypatch.setattr(snapshot,'collect',lambda *a,**k:pytest.fail('source collection followed root replacement'))
    with pytest.raises(backup.BackupError) as caught:backup.create()
    assert observed and no_pair(owned)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert 'sentinel' not in str(caught.value)


@pytest.mark.parametrize('kind',['signing','encryption'])
def test_captured_private_file_move_refuses_before_source_collection(owned,monkeypatch,kind):
    key=owned/'selected-private-key';key.write_bytes(b'owned private key');key.chmod(0o600)
    name='OLYMPUS_SIGNING_SEED_FILE' if kind=='signing' else 'OLYMPUS_SECRET_KEY_FILE'
    monkeypatch.setenv(name,str(key))
    selected=signing if kind=='signing' else aio.custody
    method='capture_signer' if kind=='signing' else 'capture'
    original=getattr(selected,method);calls=[]
    def capture(*args,**kwargs):
        result=original(*args,**kwargs);calls.append('material captured')
        key.rename(owned/'retained-original-private-key');key.write_bytes(b'replacement key')
        return result
    monkeypatch.setattr(selected,method,capture)
    monkeypatch.setattr(snapshot,'collect',lambda *a,**k:pytest.fail('private key movement reached source collection'))
    with pytest.raises(backup.BackupError):backup.create()
    assert calls==['material captured'] and no_pair(owned)
    assert (owned/'retained-original-private-key').read_bytes()==b'owned private key'


def test_manifest_bound_applies_during_escaped_json_encoding(owned,monkeypatch):
    for number in range(3):(owned/(str(number)+'\u65e5'*70)).write_bytes(b'x')
    monkeypatch.setattr(fmt,'MAX_MANIFEST',1024)
    sizes=[];original=aio.BoundedBuffer.write
    def write(self,data):
        try:return original(self,data)
        finally:sizes.append(self.tell())
    monkeypatch.setattr(aio.BoundedBuffer,'write',write)
    monkeypatch.setattr(aio,'encode_tar',lambda *a,**k:pytest.fail('oversized manifest reached compression'))
    with pytest.raises(backup.BackupError):backup.create()
    assert sizes and max(sizes)<=1024 and no_pair(owned)


def test_relative_key_is_bound_before_signer_changes_cwd(owned,tmp_path,monkeypatch):
    key=tmp_path/'private-key';key.write_bytes(b'owned original custody');key.chmod(0o600)
    other=tmp_path/'other';other.mkdir();(other/'private-key').write_bytes(b'different custody')
    monkeypatch.chdir(tmp_path);monkeypatch.setenv('OLYMPUS_SECRET_KEY_FILE','private-key')
    original=signing.capture_signer
    def capture():
        signer=original();monkeypatch.chdir(other);return signer
    monkeypatch.setattr(signing,'capture_signer',capture)
    result=backup.create()
    assert result['encrypted'] and result['archive_encryption']['context_authenticated']
    with aio.operation('backup',selectors={'OLYMPUS_ENV':'test','OLYMPUS_SECRET_KEY_FILE':str(key)}) as session:
        decoded=session.decrypt(Path(result['path']).read_bytes())
    assert fmt.decode(decoded.data)[1]=={'owned data.txt':b'owned source bytes'}


def test_busy_admission_refuses_before_key_capture(owned,monkeypatch):
    monkeypatch.setattr(signing,'capture_signer',lambda:pytest.fail('busy admission read private custody'))
    with aio.operation('notes',selectors={},capture_key=False):
        with pytest.raises(backup.BackupError):backup.create()
    assert no_pair(owned)


def test_selected_missing_signing_file_never_becomes_unsigned_backup(owned,monkeypatch):
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED_FILE',str(owned/'missing-private-key'))
    with pytest.raises(backup.BackupError) as caught:backup.create()
    assert caught.value.__cause__ is None and caught.value.__context__ is None and no_pair(owned)


def test_publication_interruption_reports_id_and_retains_pair_plan(owned,monkeypatch):
    from olympus import backup_publication as pub
    op=uuid.uuid4().hex;original=pub._Root.publish
    def fault(self,path,raw):
        if path.name.endswith('.sig.json'):raise OSError('owned private failure sentinel')
        return original(self,path,raw)
    monkeypatch.setattr(pub._Root,'publish',fault)
    with pytest.raises(backup.BackupError) as caught:backup.create(operation_id=op)
    assert caught.value.operation_id==op and 'sentinel' not in str(caught.value)
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    journal=owned/'backups'/pub.CONTROL/op
    assert (journal/'plan.json').is_file() and (journal/'archive.image').is_file() and (journal/'signature.image').is_file()
    assert not (journal/'complete.json').exists()



def test_held_destination_handoff_cannot_publish_into_replacement(owned,monkeypatch):
    from olympus import backup_publication as pub
    from olympus.vault_storage import _Directory
    original=pub.publish;observed=[]
    def handoff(directory,*args,**kwargs):
        assert type(directory) is _Directory and not directory._closed
        path=directory.path
        try:path.rename(path.with_name('retained-backups'))
        except OSError as error:
            assert os.name=='nt' and error.winerror in (5,32)
            observed.append('native-refusal')
            raise OSError('owned native handoff refusal')
        path.mkdir();(path/'foreign-evidence').write_bytes(b'foreign')
        observed.append(path)
        return original(directory,*args,**kwargs)
    monkeypatch.setattr(pub,'publish',handoff)
    with pytest.raises(backup.BackupError):backup.create()
    assert observed
    if isinstance(observed[0],Path):
        assert {p.name:p.read_bytes() for p in observed[0].iterdir()}=={'foreign-evidence':b'foreign'}
        assert list((owned/'retained-backups').iterdir())==[]
    else:assert list((owned/'backups').iterdir())==[]


def test_publication_borrows_destination_until_outer_creation_closes_it(owned,monkeypatch):
    from olympus import backup_publication as pub
    from olympus.vault_storage import _Directory
    original=pub.publish;borrowed=[]
    def publish(directory,*args,**kwargs):
        assert type(directory) is _Directory and not directory._closed
        result=original(directory,*args,**kwargs)
        directory.check();assert not directory._closed
        borrowed.append(directory)
        return result
    monkeypatch.setattr(pub,'publish',publish)
    assert backup.create()['publication_complete']
    assert len(borrowed)==1 and borrowed[0]._closed

"""Owned public consumption/recovery; no off-machine delivery or live state."""
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import uuid
import pytest
from olympus import backup, config, archive_io as aio, backup_archive as fmt
from olympus import backup_signing as signing, backup_restore as recovery, witness


@pytest.fixture
def owned(tmp_path,monkeypatch):
    live=tmp_path/'live';live.mkdir();(live/'data.txt').write_bytes(b'owned live image')
    package=tmp_path/'package';package.mkdir()
    monkeypatch.setattr(config,'MEMORY_DIR',live)
    monkeypatch.setattr(witness,'_package_dir',lambda:package)
    for name in (*signing._NAMES,'OLYMPUS_SECRET_KEY','OLYMPUS_SECRET_KEY_FILE'):
        monkeypatch.delenv(name,raising=False)
    monkeypatch.setenv('OLYMPUS_ENV','test')
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED','owned signing fixture')
    return live,tmp_path/'offline',package


def images_payload(images=None):
    images={'restored.txt':b'owned restore image'} if images is None else images
    manifest={'schema':fmt.SCHEMA,'created':'20261004T000000Z','olympus_version':'owned',
        'host':'','full':False,'file_count':len(images),
        'files':[{'path':name,'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)} for name,raw in images.items()]}
    return manifest,[(fmt.MANIFEST,json.dumps(manifest).encode()),*[(('data/'+name),raw) for name,raw in images.items()]]


def pair(owned,raw=None,*,signed=True,name='external-copy.bin'):
    if raw is None:
        _,members=images_payload()
        raw=aio.encode_tar(members,max_expanded=fmt.MAX_EXPANDED,max_compressed=aio.MAX_PLAINTEXT)
    folder=owned[0].parent/('input-'+uuid.uuid4().hex);folder.mkdir()
    path=folder/name;path.write_bytes(raw)
    if signed:
        signer=signing.capture_signer()
        path.with_name(path.name+'.sig.json').write_bytes(signer.signature(hashlib.sha256(raw).hexdigest()))
    return path


@pytest.mark.parametrize('encrypted',[False,True])
def test_create_verify_restore_reports_exact_trust_and_consumed_bytes(owned,monkeypatch,encrypted):
    if encrypted:monkeypatch.setenv('OLYMPUS_SECRET_KEY','owned sufficiently distinct encryption material')
    created=backup.create();path=Path(created['path']);raw=path.read_bytes()
    verified=backup.verify_archive(str(path))
    assert verified['signature_ok'] and verified['signer_authenticated']
    assert not verified['decryption_verified'] and not verified['manifest_integrity_verified']
    assert verified['local_publication']['kind']=='local_completed'
    result=backup.restore(str(path),into=owned[1])
    assert result['restore_complete'] and result['restored']==1
    assert (owned[1]/'data.txt').read_bytes()==b'owned live image'
    assert result['sha256']==result['consumed_archive_sha256']==hashlib.sha256(raw).hexdigest()
    assert result['signer_authenticated'] and not result['signature_override']
    assert result['archive_encryption']['encrypted']==encrypted
    assert result['archive_encryption']['context_authenticated']==encrypted
    assert result['local_publication']['archive_sha256']==result['sha256']
    assert not result['database_included'] and not result['application_consistent_snapshot']
    assert backup.inspect_restore(owned[1],operation_id=result['operation_id'])['status']=='completed'


def test_explicit_external_pair_can_recover_without_local_publication_journal(owned):
    created=backup.create();source=Path(created['path'])
    path=pair(owned,source.read_bytes(),name=source.name)
    with pytest.raises(backup.BackupError):backup.restore(str(path),into=owned[1])
    assert not owned[1].exists()
    result=backup.restore(str(path),into=owned[1],external=True)
    assert result['local_publication']=={'kind':'external_pair'} and result['signer_authenticated']
    assert (owned[1]/'data.txt').read_bytes()==b'owned live image'


def test_default_signer_never_becomes_authenticated_restore(owned,monkeypatch):
    monkeypatch.delenv('OLYMPUS_SIGNING_SEED')
    result=backup.restore(str(pair(owned)),into=owned[1])
    assert result['signature_ok'] and result['signer_default'] and not result['signer_authenticated']
    assert result['signer_trust']=='public_default_local_integrity'


def test_consumption_uses_one_captured_image_even_if_input_bytes_change(owned,monkeypatch):
    path=pair(owned);original_raw=path.read_bytes();original=aio.read_file;reads=[]
    def read(selected,cap,**kwargs):
        result=original(selected,cap,**kwargs)
        if selected==path:
            reads.append(selected);path.write_bytes(b'changed after bounded read')
        return result
    monkeypatch.setattr(aio,'read_file',read)
    result=backup.restore(str(path),into=owned[1])
    assert reads==[path] and result['consumed_archive_sha256']==hashlib.sha256(original_raw).hexdigest()
    assert (owned[1]/'restored.txt').read_bytes()==b'owned restore image'


@pytest.mark.parametrize('method',['verify_archive','restore'])
def test_read_boundary_pin_change_cannot_install_new_trust(owned,monkeypatch,method):
    path=pair(owned);attacker=json.loads(path.with_name(path.name+'.sig.json').read_bytes())['publicKey']
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED','different owned local identity')
    original=aio.read_file;changed=[]
    def read(selected,cap,**kwargs):
        result=original(selected,cap,**kwargs)
        if selected==path:
            monkeypatch.setenv('OLYMPUS_PINNED_PUBKEY',attacker);changed.append(True)
        return result
    monkeypatch.setattr(aio,'read_file',read)
    with pytest.raises(backup.BackupError):
        getattr(backup,method)(str(path),**({'into':owned[1]} if method=='restore' else {}))
    assert changed==[True] and not owned[1].exists()


def test_pin_file_snapshot_precedes_archive_reads(owned,monkeypatch):
    path=pair(owned);key=json.loads(path.with_name(path.name+'.sig.json').read_bytes())['publicKey']
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED','different owned local identity')
    pinfile=owned[2]/'witness_pubkey.txt';pinfile.write_text(witness.pubkey_for_seed('different pin'),encoding='ascii')
    original=aio.read_file;counts=[]
    def read(selected,cap,**kwargs):
        result=original(selected,cap,**kwargs)
        if selected==pinfile:counts.append(selected)
        if selected==path:pinfile.write_text(key,encoding='ascii')
        return result
    monkeypatch.setattr(aio,'read_file',read)
    with pytest.raises(backup.BackupError):backup.restore(str(path),into=owned[1])
    assert counts==[pinfile] and not owned[1].exists()


def test_pinned_external_restore_does_not_read_unavailable_private_signing_file(owned,monkeypatch):
    path=pair(owned);public=json.loads(path.with_name(path.name+'.sig.json').read_bytes())['publicKey']
    monkeypatch.delenv('OLYMPUS_SIGNING_SEED');monkeypatch.setenv('OLYMPUS_PINNED_PUBKEY',public)
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED_FILE',str(owned[0].parent/'missing-private'))
    monkeypatch.setattr(signing,'_read_seed',lambda *a,**k:pytest.fail('pinned verification read private signing custody'))
    result=backup.restore(str(path),into=owned[1],external=True)
    assert result['signer_authenticated'] and result['local_signing_custody']=='not_inspected_pinned_verification'


@pytest.mark.parametrize('signed',[False,True])
def test_insecure_overrides_only_recorded_signature_refusal(owned,monkeypatch,signed):
    path=pair(owned,signed=signed)
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED','different owned signer')
    with pytest.raises(backup.BackupError):backup.restore(str(path),into=owned[1])
    assert not owned[1].exists()
    result=backup.restore(str(path),into=owned[1],insecure=True)
    assert not result['signature_ok'] and not result['signer_authenticated'] and result['signature_override']
    assert result['signed']==signed


@pytest.mark.parametrize('fault',['sidecar','missing_private','wrong_hash','traversal','symlink','duplicate','undeclared','truncated_gzip','bound'])
def test_malformed_input_never_creates_destination_even_insecure(owned,monkeypatch,fault):
    manifest,members=images_payload()
    if fault=='wrong_hash':
        manifest['files'][0]['sha256']='0'*64;members[0]=(fmt.MANIFEST,json.dumps(manifest).encode())
    elif fault=='traversal':members.append(('data/../escaped',b'bad'))
    elif fault=='duplicate':members.append(members[-1])
    elif fault=='undeclared':members.append(('data/undeclared',b'bad'))
    if fault in ('symlink','traversal','duplicate','undeclared'):
        stream=io.BytesIO()
        with tarfile.open(fileobj=stream,mode='w:gz') as archive:
            for name,raw in members:
                info=tarfile.TarInfo(name);info.size=len(raw);archive.addfile(info,io.BytesIO(raw))
            if fault=='symlink':
                info=tarfile.TarInfo('data/link');info.type=tarfile.SYMTYPE;info.linkname='../outside';archive.addfile(info)
        raw=stream.getvalue()
    else:raw=aio.encode_tar(members,max_expanded=fmt.MAX_EXPANDED,max_compressed=aio.MAX_PLAINTEXT)
    if fault=='truncated_gzip':raw=raw[:-4]
    path=pair(owned,raw)
    if fault=='sidecar':path.with_name(path.name+'.sig.json').write_bytes(b'{malformed private sentinel')
    elif fault=='missing_private':
        monkeypatch.delenv('OLYMPUS_SIGNING_SEED');monkeypatch.setenv('OLYMPUS_SIGNING_SEED_FILE',str(owned[0]/'missing'))
    elif fault=='bound':monkeypatch.setattr(fmt,'MAX_EXPANDED',128)
    with pytest.raises(backup.BackupError) as caught:backup.restore(str(path),into=owned[1],insecure=True)
    assert not owned[1].exists() and not (owned[0].parent/'escaped').exists()
    assert 'sentinel' not in str(caught.value) and caught.value.__context__ is None and caught.value.__cause__ is None


def replace_directory(path,observed):
    try:path.rename(path.with_name(path.name+'-retained'))
    except OSError as error:
        assert os.name=='nt' and error.winerror in (5,32)
        observed.append('native-refusal');raise OSError('owned native replacement refused')
    path.mkdir();(path/'foreign').write_bytes(b'foreign original bytes');observed.append(path)


@pytest.mark.parametrize('which',['live','input'])
def test_original_directory_authority_survives_engine_handoff(owned,monkeypatch,which):
    path=pair(owned);original=recovery.restore;observed=[];borrowed=[]
    def handoff(*args,**kwargs):
        live=kwargs['live_root'];directories=kwargs['protected_directories']
        live.check();assert directories and all(not d._closed for d in directories)
        borrowed.extend([live,*directories])
        replace_directory(owned[0] if which=='live' else path.parent,observed)
        return original(*args,**kwargs)
    monkeypatch.setattr(recovery,'restore',handoff)
    with pytest.raises(backup.BackupError):backup.restore(str(path),into=owned[1])
    assert observed and borrowed and all(d._closed for d in borrowed)
    assert not owned[1].exists()
    if isinstance(observed[0],Path):assert {p.name:p.read_bytes() for p in observed[0].iterdir()}=={'foreign':b'foreign original bytes'}


def test_live_directory_held_before_public_policy_callback(owned,monkeypatch):
    path=pair(owned);original=signing.capture_policy;observed=[]
    def capture(**kwargs):
        replace_directory(owned[0],observed)
        return original(**kwargs)
    monkeypatch.setattr(signing,'capture_policy',capture)
    with pytest.raises(backup.BackupError):backup.restore(str(path),into=owned[1])
    assert observed and not owned[1].exists()


def test_input_authority_rechecked_before_each_target_publication(owned,monkeypatch):
    path=pair(owned);original=recovery._Root.publish;observed=[]
    def publish(root,target,raw):
        if target==owned[1]/'restored.txt':replace_directory(path.parent,observed)
        return original(root,target,raw)
    monkeypatch.setattr(recovery._Root,'publish',publish)
    with pytest.raises(backup.BackupError):backup.restore(str(path),into=owned[1])
    assert observed and not (owned[1]/'restored.txt').exists()
    assert (owned[1]/recovery.CONTROL/'head.json').exists()


@pytest.mark.parametrize('method,engine',[('resume_restore','resume'),('inspect_restore','inspect')])
def test_recovery_admission_precedes_retained_image_reads(owned,monkeypatch,method,engine):
    monkeypatch.setattr(recovery,engine,lambda *a,**k:pytest.fail('busy admission reached recovery reader'))
    with aio.operation('notes',selectors={},capture_key=False):
        with pytest.raises(backup.BackupError):getattr(backup,method)(owned[1],operation_id=uuid.uuid4().hex)
    assert not owned[1].exists()


def test_interrupted_restore_resumes_retained_images_without_archive_or_private_keys(owned,monkeypatch):
    path=pair(owned);op=uuid.uuid4().hex;original=aio.publish_file;fired=[]
    def publish(target,raw):
        original(target,raw)
        if target==owned[1]/'restored.txt' and not fired:
            fired.append(True);raise OSError('owned lost acknowledgement sentinel')
    monkeypatch.setattr(aio,'publish_file',publish)
    with pytest.raises(backup.BackupError) as caught:backup.restore(str(path),into=owned[1],operation_id=op)
    assert fired and caught.value.operation_id==op and caught.value.__context__ is None
    monkeypatch.setattr(aio,'publish_file',original)
    monkeypatch.setattr(backup,'_read_pair',lambda *a,**k:pytest.fail('recovery reread archive'))
    monkeypatch.setattr(signing,'capture_policy',lambda *a,**k:pytest.fail('recovery recaptured policy'))
    monkeypatch.setattr(signing,'_key',lambda *a,**k:pytest.fail('recovery read signing key'))
    monkeypatch.setattr(aio.custody,'capture',lambda *a,**k:pytest.fail('recovery read vault key'))
    assert backup.inspect_restore(owned[1],operation_id=op)['status']=='active'
    result=backup.resume_restore(owned[1],operation_id=op)
    assert result['operation_id']==op and result['restore_complete'] and result['signer_authenticated']
    assert result['consumed_archive_sha256']==hashlib.sha256(path.read_bytes()).hexdigest()


def test_late_authority_cleanup_failure_keeps_completed_restore_operation_id(owned,monkeypatch):
    path=pair(owned);op=uuid.uuid4().hex;original=recovery._Directory.close;fired=[]
    def close(directory):
        original(directory)
        if directory.path==owned[0] and (owned[1]/recovery.CONTROL/op/'complete.json').exists() and not fired:
            fired.append(True);raise OSError('owned late cleanup sentinel')
    monkeypatch.setattr(recovery._Directory,'close',close)
    with pytest.raises(backup.BackupError) as caught:backup.restore(str(path),into=owned[1],operation_id=op)
    assert fired and caught.value.operation_id==op and caught.value.__context__ is None
    monkeypatch.setattr(recovery._Directory,'close',original)
    assert backup.inspect_restore(owned[1],operation_id=op)['status']=='completed'
    assert backup.resume_restore(owned[1],operation_id=op)['historical']


@pytest.mark.parametrize('field',['publicKey','signer_authenticated','signer_trusted','local_publication','archive_encryption'])
def test_retained_verification_schema_refuses_inconsistent_decisions(owned,field):
    result=backup.restore(str(pair(owned)),into=owned[1]);evidence=result['evidence']
    evidence=json.loads(json.dumps(evidence))
    if field=='archive_encryption':evidence[field]['format_version']=1
    elif field=='local_publication':evidence['backup_verification'][field]={'kind':'local_completed'}
    elif field=='publicKey':evidence['backup_verification'][field]='invalid'
    else:evidence['backup_verification'][field]=False
    with pytest.raises(ValueError):recovery._evidence(evidence)


@pytest.mark.parametrize('cleanup_failure',[False,True])
def test_managed_input_preserves_existing_unfinished_restore_id(owned,monkeypatch,cleanup_failure):
    from olympus import backup_publication as publication
    created=backup.create();path=Path(created['path']);existing=uuid.uuid4().hex
    original=aio.publish_file;fired=[]
    def publish(target,raw):
        if target==owned[1]/'data.txt':
            fired.append(True);raise OSError('owned target interruption')
        return original(target,raw)
    monkeypatch.setattr(aio,'publish_file',publish)
    with pytest.raises(backup.BackupError) as caught:backup.restore(str(path),into=owned[1],operation_id=existing)
    assert fired and caught.value.operation_id==existing
    monkeypatch.setattr(aio,'publish_file',original)
    if cleanup_failure:
        close=publication._Root.close
        def fail_close(root):
            close(root);raise OSError('owned secondary input cleanup fault')
        monkeypatch.setattr(publication._Root,'close',fail_close)
    requested=uuid.uuid4().hex
    with pytest.raises(backup.BackupError) as caught:
        backup.restore(str(path),into=owned[1],operation_id=requested,force=True)
    assert caught.value.operation_id==existing and existing!=requested
    assert not (owned[1]/recovery.CONTROL/requested).exists() and not (owned[1]/'data.txt').exists()
    assert caught.value.__context__ is None and caught.value.__cause__ is None


@pytest.mark.parametrize('witness_name',['initialization.json','publication.lock'])
@pytest.mark.parametrize('boundary',['handoff','target'])
def test_full_input_lease_is_checked_before_restore_mutations(owned,monkeypatch,witness_name,boundary):
    from olympus import backup_publication as publication
    created=backup.create();path=Path(created['path']);observed=[]
    marker=path.parent/publication.CONTROL/witness_name
    def replace():
        # Keep the original file; never delete/rewrite a held lock's bytes.
        try:marker.rename(marker.with_name(marker.name+'.retained'))
        except OSError as error:
            assert os.name=='nt' and error.winerror in (5,32)
            observed.append('native-refusal');raise OSError('owned held witness replacement refused')
        marker.write_bytes(b'0' if witness_name=='publication.lock' else marker.with_name(marker.name+'.retained').read_bytes())
        observed.append('replaced')
    if boundary=='handoff':
        original=recovery.restore
        def restore(*args,**kwargs):
            assert type(kwargs['input_lease']) is publication._Root
            replace();return original(*args,**kwargs)
        monkeypatch.setattr(recovery,'restore',restore)
    else:
        original=recovery._Root.publish
        def publish(root,target,raw):
            if target==owned[1]/'data.txt':replace()
            return original(root,target,raw)
        monkeypatch.setattr(recovery._Root,'publish',publish)
    with pytest.raises(backup.BackupError):backup.restore(str(path),into=owned[1])
    assert observed and not (owned[1]/'data.txt').exists()
    if boundary=='handoff':assert not owned[1].exists()


@pytest.mark.parametrize('which',['input','live'])
def test_primary_existing_restore_id_survives_external_and_live_close_fault(owned,monkeypatch,which):
    path=pair(owned);existing=uuid.uuid4().hex;original=aio.publish_file
    def publish(target,raw):
        if target==owned[1]/'restored.txt':raise OSError('owned target interruption')
        return original(target,raw)
    monkeypatch.setattr(aio,'publish_file',publish)
    with pytest.raises(backup.BackupError) as caught:
        backup.restore(str(path),into=owned[1],operation_id=existing,external=True)
    assert caught.value.operation_id==existing
    monkeypatch.setattr(aio,'publish_file',original)
    armed=[];fired=[];engine=recovery.restore;close=recovery._Directory.close
    def restore(*args,**kwargs):
        try:return engine(*args,**kwargs)
        except recovery.RestoreError as error:
            assert error.operation_id==existing
            armed.append(True);raise
    selected=path.parent if which=='input' else owned[0]
    def fail_close(directory):
        close(directory)
        if armed and directory.path==selected and not fired:
            fired.append(True);raise OSError('owned secondary directory cleanup fault')
    monkeypatch.setattr(recovery,'restore',restore)
    monkeypatch.setattr(recovery._Directory,'close',fail_close)
    requested=uuid.uuid4().hex
    with pytest.raises(backup.BackupError) as caught:
        backup.restore(str(path),into=owned[1],operation_id=requested,force=True,external=True)
    assert armed and fired and caught.value.operation_id==existing and existing!=requested
    assert not (owned[1]/recovery.CONTROL/requested).exists()
    assert caught.value.__context__ is None and caught.value.__cause__ is None

"""Owned cleanup reporting through actual custody consumer entrypoints."""
import json
import os
from types import SimpleNamespace
import pytest
from olympus import archive_io, backup_signing as signing, browser_credential_authority as authority
from olympus import browserlease, memory, native_cleanup, note_archive, vault, vault_commands
from olympus import vault_custody as custody


def failure(fault,flag):
    error=fault('owned-private-material-and-path');error.add_note('owned-private-note')
    if flag:native_cleanup.mark(error)
    return error


def sanitized(error,flag):
    assert native_cleanup.unknown(error) is flag
    assert error.__cause__ is None and error.__context__ is None
    assert 'owned-private' not in str(error) and not getattr(error,'__notes__',None)


@pytest.fixture
def signing_selection(monkeypatch):
    for name in signing._NAMES:monkeypatch.delenv(name,raising=False)
    monkeypatch.setenv('OLYMPUS_ENV','test')
    return signing._selection()


@pytest.mark.parametrize('endpoint',['signer','policy','verifier'])
@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
@pytest.mark.parametrize('flag',[False,True])
def test_signing_normalizers_retain_cleanup_without_private_context(signing_selection,tmp_path,monkeypatch,endpoint,fault,flag):
    error=failure(fault,flag);calls=[]
    def fail(*args):calls.append(1);raise error
    policy=signing.VerificationPolicy(signing_selection,str(tmp_path),frozenset(),'0'*64)
    if endpoint=='policy':monkeypatch.setattr(signing,'_pins',fail)
    else:monkeypatch.setattr(signing,'_key',fail)
    raw=json.dumps({'schema':signing.SCHEMA,'sha256':'0'*64,'signature':'0'*128,
        'publicKey':'1'*64,'seedDerivation':signing.witness.SEED_DERIVATION}).encode()
    def invoke():
        if endpoint=='signer':return signing.capture_signer(selection=signing_selection,cwd=str(tmp_path),policy=policy)
        if endpoint=='policy':return signing.capture_policy(selection=signing_selection,cwd=str(tmp_path))
        return signing.capture_verifier(raw,policy=policy)
    with pytest.raises(signing.SignatureError if fault is OSError else fault) as caught:invoke()
    assert calls==[1]
    if fault is OSError:sanitized(caught.value,flag)
    else:assert caught.value is error


def test_signing_reuses_raw_custody_without_vault_material_limit(tmp_path,monkeypatch):
    path=tmp_path/'owned-seed';raw=b'  '+b'x'*4097+b'\r\nlast\r  '
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_BINARY',0),0o600)
    try:assert os.write(fd,raw)==len(raw)
    finally:os.close(fd)
    calls=[];read=custody._read_file
    def observed(path,named):calls.append((path,named));return read(path,named)
    monkeypatch.setattr(custody,'_read_file',observed)
    seed=signing._seed({'OLYMPUS_SIGNING_SEED_FILE':str(path),'OLYMPUS_ENV':'test'})
    assert seed==b'x'*4097+b'\nlast' and calls==[(path,False)] and path.read_bytes()==raw
    with pytest.raises(custody.CustodyError):custody.capture({'OLYMPUS_SECRET_KEY_FILE':str(path),'OLYMPUS_ENV':'test'})


@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
@pytest.mark.parametrize('flag',[False,True])
def test_archive_selection_failure_latches_cleanup_without_reacquiring(monkeypatch,fault,flag):
    error=failure(fault,flag);calls=[]
    def fail(*args):calls.append(1);raise error
    monkeypatch.setattr(custody,'capture',fail)
    with archive_io.operation('notes',selectors={'OLYMPUS_SECRET_KEY':'owned-selected'},capture_key=False) as session:
        with pytest.raises(fault) as caught:session.material()
        assert caught.value is error and session.selectors=={} and session.passphrase is None
        assert session.key_selection_failed and session.key_cleanup_unconfirmed is flag
        for _ in range(2):
            with pytest.raises(archive_io.ArchiveError) as refusal:session.material()
            sanitized(refusal.value,flag)
        assert calls==[1]
    with pytest.raises(archive_io.ArchiveError) as refusal:session.material()
    sanitized(refusal.value,flag)
    assert calls==[1]


@pytest.mark.parametrize('value',[False,True,1,'true',None])
def test_browser_classifier_accepts_only_exact_native_flag(monkeypatch,value):
    error=OSError('owned-private');error._native_cleanup_unconfirmed=value
    def fail():raise error
    with pytest.raises(browserlease.OwnershipRefused) as caught:authority._guard(fail)()
    assert authority._cleanup_unknown(error) is (value is True)
    assert authority._cleanup_unknown(caught.value) is (value is True)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert all('owned-private' not in note for note in getattr(caught.value,'__notes__',[]))
    legacy=OSError('owned-private');authority._mark_cleanup_unknown(legacy)
    assert authority._cleanup_unknown(legacy)


@pytest.mark.parametrize('endpoint',['notes','memory'])
@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
@pytest.mark.parametrize('flag',[False,True])
def test_note_consumers_retain_custody_cleanup_and_actual_interrupt(monkeypatch,endpoint,fault,flag):
    error=failure(fault,flag);calls=[]
    def fail(*args):calls.append(1);raise error
    monkeypatch.setattr(custody,'capture',fail)
    if endpoint=='notes':
        monkeypatch.setattr(note_archive,'_check_root',lambda _:None)
        monkeypatch.setattr(note_archive,'external_read',lambda *args:b'owned-encrypted')
    expected=ValueError if fault is OSError else fault
    with pytest.raises(expected) as caught:
        if endpoint=='memory':memory._maybe_decrypt(b'owned-encrypted')
        else:
            with archive_io.operation('notes',capture_key=False) as session:
                note_archive._load_archive('owned-input',session,None)
    assert calls==[1]
    if fault is OSError:sanitized(caught.value,flag)
    else:assert caught.value is error


@pytest.mark.parametrize('kind',['recovery','command','custody','ordinary'])
@pytest.mark.parametrize('flag',[False,True])
def test_vault_cli_reports_cleanup_as_a_boolean_without_private_diagnostics(monkeypatch,capsys,kind,flag):
    if kind=='recovery':error=vault.VaultRecoveryError('a'*32)
    elif kind=='command':error=vault_commands.CommandRefused('fixed operator explanation')
    elif kind=='custody':error=custody.CustodyError('owned-private-diagnostic')
    else:error=OSError('owned-private-diagnostic')
    if flag:native_cleanup.mark(error)
    calls=[]
    def fail(*args):calls.append(1);raise error
    monkeypatch.setattr(vault,'legacy_tenants',fail)
    args=SimpleNamespace(action='legacy-list',operation_id=None,replacement_key_file=None,
        prompt_replacement_key=False,overwrite=False,owner=None,operator_reviewed=False,legacy_key=None)
    assert vault_commands.command(args)==1 and calls==[1]
    raw=capsys.readouterr().out;assert 'owned-private' not in raw
    result=json.loads(raw);assert result['cleanup_unconfirmed'] is flag
    assert result['state'] in ('refused','recovery_required','unavailable')


@pytest.mark.parametrize('fault',[KeyboardInterrupt,SystemExit])
def test_vault_cli_preserves_actual_interrupt_with_cleanup(monkeypatch,capsys,fault):
    error=failure(fault,True)
    def fail(*args):raise error
    monkeypatch.setattr(vault,'legacy_tenants',fail)
    args=SimpleNamespace(action='legacy-list',operation_id=None,replacement_key_file=None,
        prompt_replacement_key=False,overwrite=False,owner=None,operator_reviewed=False,legacy_key=None)
    with pytest.raises(fault) as caught:vault_commands.command(args)
    assert caught.value is error and native_cleanup.unknown(error) and capsys.readouterr().out==''


@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
@pytest.mark.parametrize('with_primary',[False,True])
def test_browser_cleanup_with_malformed_notes_retains_original_and_drains(fault,with_primary):
    error=fault('owned-private');error.__notes__=object();original_notes=error.__notes__
    secondary=OSError('owned secondary');calls=[]
    def failed():calls.append('first');raise secondary if with_primary else error
    def remaining():calls.append('last')
    if with_primary:
        with pytest.raises(fault) as caught:
            try:raise error
            finally:authority._drain([failed,remaining],error)
    else:
        with pytest.raises(fault) as caught:authority._drain([failed,remaining])
    assert caught.value is error and error.__notes__ is original_notes
    assert authority._cleanup_unknown(error) and calls==['first','last']


@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
@pytest.mark.parametrize('flag',[False,True])
def test_vault_cli_output_failure_preserves_cleanup_and_never_retries(monkeypatch,fault,flag):
    custody_error=failure(OSError,flag);output_error=fault('owned output failure');calls=[]
    def fail_operation(*args):calls.append('operation');raise custody_error
    def fail_output(*args,**kwargs):calls.append('output');raise output_error
    monkeypatch.setattr(vault,'legacy_tenants',fail_operation)
    monkeypatch.setattr(vault_commands,'print',fail_output,raising=False)
    args=SimpleNamespace(action='legacy-list',operation_id=None,replacement_key_file=None,
        prompt_replacement_key=False,overwrite=False,owner=None,operator_reviewed=False,legacy_key=None)
    with pytest.raises(fault) as caught:vault_commands.command(args)
    assert caught.value is output_error and native_cleanup.unknown(output_error) is flag
    assert output_error.__cause__ is None and output_error.__context__ is None
    assert calls==['operation','output']

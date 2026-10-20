"""Owned backup signing custody and public verification controls."""
import hashlib
import json
import os

import pytest

from olympus import backup_signing as signing, witness


DIGEST = hashlib.sha256(b'owned archive image').hexdigest()


@pytest.fixture
def policy(tmp_path,monkeypatch):
    for name in signing._NAMES:
        monkeypatch.delenv(name,raising=False)
    package = tmp_path/'package';package.mkdir()
    monkeypatch.setattr(witness,'_package_dir',lambda:package)
    return package


def _owned_seed(path,raw):
    fd = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_BINARY',0),0o600)
    try:
        assert os.write(fd,raw) == len(raw)
    finally:
        os.close(fd)
    assert path.read_bytes() == raw


def test_same_captured_key_produces_historical_public_identity_and_signature(policy,monkeypatch):
    seed = '  owned historical signing identity 日本語  '
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED',seed)
    signer = signing.capture_signer()
    assert signer.public_key == witness.pubkey_for_seed(seed.strip())
    raw = signer.signature(DIGEST)
    parsed = json.loads(raw)
    assert parsed['publicKey'] == signer.public_key
    assert parsed['seedDerivation'] == witness.SEED_DERIVATION == 'ed25519(sha256(seed))'
    assert witness.verify_signature(signer.public_key,DIGEST.encode(),parsed['signature'])
    result = signer.verify(DIGEST,raw)
    assert result['signature_ok'] and result['signer_authenticated'] and result['signer_trust'] == 'local'


def test_file_acquired_once_with_historical_universal_newlines(policy,tmp_path,monkeypatch):
    path = tmp_path/'owned-seed'
    _owned_seed(path,b'  owned\r\nmultiline\rseed \r\n')
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED_FILE',str(path))
    reads = []; original = signing._read_seed
    def read(selected,named):
        reads.append((selected,named));return original(selected,named)
    monkeypatch.setattr(signing,'_read_seed',read)
    signer = signing.capture_signer()
    assert signer.public_key == witness.pubkey_for_seed('owned\nmultiline\nseed')
    raw = signer.signature(DIGEST)
    path.write_bytes(b'changed owned seed after capture')
    assert signer.verify(DIGEST,raw)['signature_ok']
    assert signer.signature(DIGEST) == raw and len(reads) == 1


@pytest.mark.parametrize('selected',[False,True])
def test_default_key_is_forgeable_even_when_explicit_or_pinned(policy,monkeypatch,selected):
    if selected:
        monkeypatch.setenv('OLYMPUS_SIGNING_SEED',witness._DEFAULT_SEED)
    default = witness.default_public_key_hex()
    monkeypatch.setenv('OLYMPUS_PINNED_PUBKEY',default)
    signer = signing.capture_signer();raw = signer.signature(DIGEST)
    result = signing.capture_verifier(raw).verify(DIGEST,raw)
    assert result['signature_ok'] and result['signer_trusted'] and result['signer_default']
    assert result['signer_authenticated'] is False
    assert result['signer_trust'] == 'public_default_local_integrity'


@pytest.mark.parametrize('mode',['production','prod'])
def test_production_default_refused_even_with_sovereign_lab_override(policy,monkeypatch,mode):
    monkeypatch.setenv('OLYMPUS_ENV',mode)
    monkeypatch.setenv('OLYMPUS_SOVEREIGN','1')
    monkeypatch.setenv('OLYMPUS_SOVEREIGN_ALLOW_DEV_SEED','1')
    with pytest.raises(signing.SignatureError):
        signing.capture_signer()


def test_sovereign_default_guard_and_existing_loud_lab_warning(policy,monkeypatch,caplog):
    monkeypatch.setenv('OLYMPUS_SOVEREIGN','1')
    with pytest.raises(signing.SignatureError):
        signing.capture_signer()
    monkeypatch.setenv('OLYMPUS_SOVEREIGN_ALLOW_DEV_SEED','1')
    signer = signing.capture_signer()
    assert 'FORGEABLE' in caplog.text
    assert signer.public_key == witness.default_public_key_hex()


def test_pinned_verification_does_not_require_private_signing_file(policy,tmp_path,monkeypatch):
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED','owned pinned fixture')
    signer = signing.capture_signer();raw = signer.signature(DIGEST)
    monkeypatch.setenv('OLYMPUS_PINNED_PUBKEY',signer.public_key)
    monkeypatch.delenv('OLYMPUS_SIGNING_SEED')
    missing = tmp_path/'missing-private-file'
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED_FILE',str(missing))
    def forbidden(*args,**kwargs):
        raise AssertionError('private signing custody must not be read for pinned verification')
    monkeypatch.setattr(signing,'_read_seed',forbidden)
    result = signing.capture_verifier(raw).verify(DIGEST,raw)
    assert result['signature_ok'] and result['signer_authenticated']
    assert result['signer_trust'] == 'pinned'
    assert result['local_signing_custody'] == 'not_inspected_pinned_verification'
    assert not missing.exists()


@pytest.mark.parametrize('selector',['NUL','trailing.','C:drive-relative'])
def test_pinned_verification_does_not_validate_an_unused_private_path(policy,monkeypatch,selector):
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED','owned pinned signer')
    signer=signing.capture_signer();raw=signer.signature(DIGEST)
    monkeypatch.delenv('OLYMPUS_SIGNING_SEED')
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED_FILE',selector)
    monkeypatch.setenv('OLYMPUS_PINNED_PUBKEY',signer.public_key)
    result=signing.capture_verifier(raw).verify(DIGEST,raw)
    assert result['signature_ok'] and result['local_signing_custody']=='not_inspected_pinned_verification'
    monkeypatch.delenv('OLYMPUS_PINNED_PUBKEY')
    with pytest.raises(signing.SignatureError) as caught:
        signing.capture_verifier(raw)
    assert caught.value.__context__ is None and caught.value.__cause__ is None


def test_unknown_mathematically_valid_key_is_not_trusted(policy,monkeypatch):
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED','owned first key')
    raw = signing.capture_signer().signature(DIGEST)
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED','owned different local key')
    result = signing.capture_verifier(raw).verify(DIGEST,raw)
    assert result['signature_math_valid'] and not result['signature_ok']
    assert result['signer_trust'] == 'unknown' and not result['signer_authenticated']


def test_broken_selected_local_fallback_is_unavailable_not_absence(policy,tmp_path,monkeypatch):
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED','owned first key')
    raw = signing.capture_signer().signature(DIGEST)
    monkeypatch.delenv('OLYMPUS_SIGNING_SEED')
    missing = tmp_path/'private-unavailable'
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED_FILE',str(missing))
    with pytest.raises(signing.SignatureError) as caught:
        signing.capture_verifier(raw)
    assert str(missing) not in str(caught.value)
    assert 'unavailable' in str(caught.value)
    assert caught.value.__context__ is None and caught.value.__cause__ is None


@pytest.mark.parametrize('trust',['local','pinned'])
@pytest.mark.parametrize('damage',['archive_digest','claimed_digest','signature'])
def test_trusted_key_still_requires_matching_digest_and_valid_signature(policy,monkeypatch,trust,damage):
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED','owned trusted signing key')
    signer = signing.capture_signer();raw = signer.signature(DIGEST)
    if trust == 'pinned':
        monkeypatch.setenv('OLYMPUS_PINNED_PUBKEY',signer.public_key)
    digest = DIGEST
    if damage == 'archive_digest':
        digest = '00'*32
    else:
        value = json.loads(raw)
        value['sha256' if damage == 'claimed_digest' else 'signature'] = '00'*(32 if damage == 'claimed_digest' else 64)
        raw = json.dumps(value).encode()
    result = signing.capture_verifier(raw).verify(digest,raw)
    assert result['signer_trusted'] and not result['signature_ok'] and not result['signer_authenticated']


def test_oversized_seed_refused_before_any_file_payload_read(policy,tmp_path,monkeypatch):
    path = tmp_path/'oversized-seed'
    _owned_seed(path,b'x'*(signing.custody.MAX_FILE_BYTES+1))
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED_FILE',str(path))
    calls = []; original = os.read
    def read(fd,count):
        calls.append(count);return original(fd,count)
    monkeypatch.setattr(os,'read',read)
    with pytest.raises(signing.SignatureError):
        signing.capture_signer()
    assert calls == []
    assert path.stat().st_size == signing.custody.MAX_FILE_BYTES+1


@pytest.mark.skipif(os.name != 'posix',reason='owned POSIX FIFO custody control')
def test_signing_fifo_is_refused_without_blocking(policy,tmp_path,monkeypatch):
    path = tmp_path/'owned-fifo';os.mkfifo(path,0o600)
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED_FILE',str(path))
    with pytest.raises(signing.SignatureError):
        signing.capture_signer()


@pytest.mark.skipif(os.name != 'posix',reason='owned POSIX private-mode custody control')
def test_signing_public_file_mode_refused_without_permission_repair(policy,tmp_path,monkeypatch):
    path = tmp_path/'owned-seed';_owned_seed(path,b'owned private seed')
    path.chmod(0o644)
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED_FILE',str(path))
    with pytest.raises(signing.SignatureError):
        signing.capture_signer()
    assert path.stat().st_mode & 0o777 == 0o644
    assert path.read_bytes() == b'owned private seed'


def test_oversized_pin_file_is_bounded(policy):
    path = policy/'witness_pubkey.txt'
    path.write_bytes(b'A'*(signing.MAX_PIN_BYTES+1))
    with pytest.raises(signing.SignatureError):
        signing.capture_signer()


def test_actual_pin_count_is_bounded(policy,monkeypatch):
    monkeypatch.setenv('OLYMPUS_PINNED_PUBKEY',','.join(f'{i:064x}' for i in range(signing.MAX_PINS+1)))
    with pytest.raises(signing.SignatureError):
        signing.capture_signer()


def test_capture_time_policy_drift_refuses_without_reacquiring_key(policy,monkeypatch):
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED','owned first key')
    calls = []; key = signing._key;pins = signing._pins
    def get_key(selected):
        calls.append('key');return key(selected)
    def get_pins(selected):
        monkeypatch.setenv('OLYMPUS_SIGNING_SEED','owned second key')
        return pins(selected)
    monkeypatch.setattr(signing,'_key',get_key)
    monkeypatch.setattr(signing,'_pins',get_pins)
    with pytest.raises(signing.SignatureError):
        signing.capture_signer()
    assert calls == ['key']


def test_post_capture_policy_drift_refuses_before_signature(policy,monkeypatch):
    signer = signing.capture_signer()
    monkeypatch.setenv('OLYMPUS_SOVEREIGN','1')
    with pytest.raises(signing.SignatureError,match='policy changed'):
        signer.signature(DIGEST)


def test_vault_key_selection_does_not_change_signing_identity(policy,monkeypatch):
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED','owned stable signing identity')
    signer = signing.capture_signer();raw = signer.signature(DIGEST)
    monkeypatch.setenv('OLYMPUS_SECRET_KEY','different owned vault encryption key')
    assert signer.signature(DIGEST) == raw
    assert signing.capture_signer().public_key == signer.public_key


@pytest.mark.parametrize('failure',['missing','unicode','native'])
def test_signer_errors_do_not_retain_private_exception_context(policy,tmp_path,monkeypatch,failure):
    path = tmp_path/'private-path-marker'
    if failure != 'missing':
        _owned_seed(path,b'\xffprivate-invalid-text' if failure == 'unicode' else b'owned material')
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED_FILE',str(path))
    if failure == 'native':
        def denied(*args,**kwargs):
            raise OSError('private-path-marker native fault')
        monkeypatch.setattr(signing,'_read_seed',denied)
    with pytest.raises(signing.SignatureError) as caught:
        signing.capture_signer()
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert 'private-path-marker' not in str(caught.value)
    assert 'private-invalid-text' not in str(caught.value)


def test_relative_seed_path_is_bound_before_key_acquisition_io(policy,tmp_path,monkeypatch):
    first,second = tmp_path/'first',tmp_path/'second'
    first.mkdir();second.mkdir()
    _owned_seed(first/'seed',b'owned first seed')
    _owned_seed(second/'seed',b'owned second seed')
    monkeypatch.chdir(first)
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED_FILE','seed')
    original = signing._key
    def key(selected):
        monkeypatch.chdir(second)
        return original(selected)
    monkeypatch.setattr(signing,'_key',key)
    assert signing.capture_signer().public_key == witness.pubkey_for_seed('owned first seed')


def test_local_verification_binds_relative_seed_before_pin_file_io(policy,tmp_path,monkeypatch):
    first,second=tmp_path/'first',tmp_path/'second';first.mkdir();second.mkdir()
    _owned_seed(first/'seed',b'owned first seed');_owned_seed(second/'seed',b'owned second seed')
    monkeypatch.chdir(first);monkeypatch.setenv('OLYMPUS_SIGNING_SEED_FILE','seed')
    raw=signing.capture_signer().signature(DIGEST)
    original=signing._pins
    def pins(selected):
        monkeypatch.chdir(second);return original(selected)
    monkeypatch.setattr(signing,'_pins',pins)
    result=signing.capture_verifier(raw).verify(DIGEST,raw)
    assert result['signature_ok'] and result['signer_trust']=='local'


@pytest.mark.skipif(os.name!='nt',reason='Windows same-drive relative normalization control')
def test_drive_relative_seed_cannot_alias_existing_cwd_file(policy,tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    path=tmp_path/'drive-relative';_owned_seed(path,b'owned seed must not be selected through drive alias')
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED_FILE',tmp_path.drive+'drive-relative')
    reads=[];original=signing._read_seed
    def read(*args,**kwargs):
        reads.append(1);return original(*args,**kwargs)
    monkeypatch.setattr(signing,'_read_seed',read)
    with pytest.raises(signing.SignatureError):signing.capture_signer()
    assert reads==[] and path.read_bytes()==b'owned seed must not be selected through drive alias'


def test_seed_file_close_interruption_drains_held_parent_chain(policy,tmp_path,monkeypatch):
    from olympus import vault_native,native_cleanup
    path = tmp_path/'seed';_owned_seed(path,b'owned seed')
    original_close,original_directory_close = os.close,vault_native.Directory.close
    events,guards = [],[]
    def close(fd):
        original_close(fd)
        if not events:
            events.append('file')
            raise KeyboardInterrupt('owned interruption after close')
    def directory_close(directory):
        guards.append(directory)
        original_directory_close(directory)
        events.append('directory')
    monkeypatch.setattr(os,'close',close)
    monkeypatch.setattr(vault_native.Directory,'close',directory_close)
    with pytest.raises(KeyboardInterrupt) as caught:
        signing._read_seed(path,False)
    assert native_cleanup.unknown(caught.value)
    assert events == ['file','directory'] and all(g._closed for g in guards)
    assert all(g.fd is None and g.win is None for g in guards)


@pytest.mark.skipif(os.name != 'posix',reason='POSIX actual descriptor constructor cleanup')
def test_seed_constructor_interruption_drains_all_actual_posix_descriptors(policy,tmp_path,monkeypatch):
    from olympus import vault_native,native_cleanup
    path = tmp_path/'seed';_owned_seed(path,b'owned seed')
    primary=OSError('owned failure after final parent acquisition')
    acquired,closed = [],[]
    real_open,real_close,append = os.open,os.close,vault_native.Directory._append_posix
    def opened(*args,**kwargs):
        fd = real_open(*args,**kwargs);acquired.append(fd);return fd
    def close(fd):
        real_close(fd);closed.append(fd)
        if len(closed) == 1:
            raise KeyboardInterrupt('owned close interruption')
    def appended(directory,name,create):
        append(directory,name,create)
        if name == tmp_path.name:
            raise primary
    monkeypatch.setattr(os,'open',opened);monkeypatch.setattr(os,'close',close)
    monkeypatch.setattr(vault_native.Directory,'_append_posix',appended)
    with pytest.raises(OSError) as caught:
        signing._read_seed(path,False)
    assert caught.value is primary and native_cleanup.unknown(primary)
    assert acquired and closed == list(reversed(acquired))
    for fd in acquired:
        with pytest.raises(OSError):os.fstat(fd)


@pytest.mark.skipif(os.name != 'nt',reason='Windows actual native constructor cleanup')
def test_seed_constructor_interruption_drains_all_actual_windows_handles(policy,tmp_path,monkeypatch):
    from olympus import vault_native,vault_windows as native,native_cleanup
    path = tmp_path/'seed';_owned_seed(path,b'owned seed')
    primary=OSError('owned failure after parent acquisition')
    acquired,closed = [],[]
    real_anchor,real_relative,real_close = native._anchor,native.open_relative,native.close
    real_check = vault_native.Directory.check
    def anchor(*args,**kwargs):
        handle=real_anchor(*args,**kwargs);acquired.append(handle);return handle
    def relative(*args,**kwargs):
        handle=real_relative(*args,**kwargs);acquired.append(handle);return handle
    def close(handle):
        real_close(handle);closed.append(handle)
        if len(closed)==1:raise KeyboardInterrupt('owned native close interruption')
    def check(directory):
        real_check(directory)
        if directory.path==tmp_path:raise primary
    monkeypatch.setattr(native,'_anchor',anchor);monkeypatch.setattr(native,'open_relative',relative)
    monkeypatch.setattr(native,'close',close);monkeypatch.setattr(vault_native.Directory,'check',check)
    with pytest.raises(OSError) as caught:signing._read_seed(path,False)
    assert caught.value is primary and native_cleanup.unknown(primary)
    assert acquired and closed==list(reversed(acquired))
    for handle in acquired:
        with pytest.raises(Exception):native.information(handle,True)

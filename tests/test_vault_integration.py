"""Owned synthetic integration controls; no provider, database or browser calls."""
from contextlib import contextmanager
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import threading
import time
from types import SimpleNamespace
import urllib.error
import uuid

import pytest

from olympus import config, google_oauth as oauth, store, vault
from olympus import vault_codec as codec, vault_custody as custody
from olympus import vault_records as records, vault_storage as storage, vault_native as native


@pytest.fixture(autouse=True)
def owned_custody(monkeypatch):
    for key in ('OLYMPUS_SECRET_KEY_FILE', 'OLYMPUS_DATABASE_URL'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('OLYMPUS_SECRET_KEY', 'owned-synthetic-vault-old-key')
    monkeypatch.setenv('OLYMPUS_ENV', 'test')
    monkeypatch.setenv('OLYMPUS_GOOGLE_CLIENT_ID', 'owned-client')
    monkeypatch.setenv('OLYMPUS_GOOGLE_CLIENT_SECRET', 'owned-client-secret')
    store.reset()
    yield
    store.reset()


def _legacy(data):
    from cryptography.fernet import Fernet
    material = b'owned-synthetic-vault-old-key'
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(material).digest())).encrypt(
        json.dumps(data).encode())


def _blob(owner='owned-owner'):
    return store.backend().get('vault', vault._key(owner))


def _expired():
    return {'access_token':'owned-old-token', 'refresh_token':'owned-refresh',
            'expires_at':time.time()-10}


def test_new_record_owner_bound_and_reads_preserve_bytes():
    vault.put('owned-owner', 'one', {'secret':'owned-value'})
    before = _blob()
    assert before.startswith(codec.MAGIC)
    assert vault.get('owned-owner', 'one') == {'secret':'owned-value'}
    assert _blob() == before
    store.backend().put('vault', vault._key('different-owner'), before)
    with pytest.raises(vault.VaultError):
        vault.get('different-owner', 'one')


def test_historical_read_requires_explicit_format_change():
    before = _legacy({'one':'owned-value'})
    store.backend().put('vault', vault._key('owned-owner'), before)
    assert vault.get('owned-owner', 'one') == 'owned-value'
    with pytest.raises(vault.VaultMigrationRequired):
        vault.put('owned-owner', 'two', 'owned-new')
    with pytest.raises(vault.VaultMigrationRequired):
        vault.delete('owned-owner', 'one')
    assert _blob() == before
    operation_id = uuid.uuid4().hex
    assert vault.rotate('owned-owner', operation_id=operation_id)['committed']
    assert _blob().startswith(codec.MAGIC) and _blob() != before
    assert store.backend().get('vault_recovery', 'change-'+operation_id+'-source') == before
    report = vault.inspect_change(operation_id, owner='owned-owner')
    assert report['committed'] and report['source_sha256'] == hashlib.sha256(before).hexdigest()
    assert vault.resume_change(operation_id, owner='owned-owner')['already_committed']
    vault.put('owned-owner', 'two', 'owned-new')
    assert vault.names('owned-owner') == ['one','two']


def test_explicit_replacement_recovery_does_not_read_broken_old_custody(monkeypatch,tmp_path):
    vault.put('owned-owner', 'one', 'owned-value')
    replacement = custody.capture({'OLYMPUS_SECRET_KEY':'owned-synthetic-new-key'})
    operation_id = uuid.uuid4().hex
    vault.rotate('owned-owner', operation_id=operation_id, replacement_custody=replacement)
    after = _blob()
    monkeypatch.delenv('OLYMPUS_SECRET_KEY')
    monkeypatch.setenv('OLYMPUS_SECRET_KEY_FILE',str(tmp_path/'absent-old-key'))
    assert vault.resume_change(operation_id, owner='owned-owner',
        replacement_custody=replacement)['already_committed']
    assert vault.inspect_change(operation_id, owner='owned-owner',
        replacement_custody=replacement)['committed']
    assert _blob() == after


def test_discard_unreadable_quarantine_preserves_exact_bytes():
    before = b'owned-corrupt-unreadable-ciphertext'
    store.backend().put('vault','owned-legacy',before)
    operation_id = uuid.uuid4().hex
    assert vault.discard_legacy('owned-legacy',operation_id=operation_id)
    assert store.backend().get('vault','owned-legacy') is None
    assert store.backend().get('vault_recovery','change-'+operation_id+'-source') == before
    assert vault.resume_change(operation_id)['already_committed']


def test_absent_and_empty_corrupt_rows_are_distinct(monkeypatch):
    monkeypatch.delenv('OLYMPUS_SECRET_KEY')
    assert vault.get('owned-owner','missing') is None
    store.backend().put('vault',vault._key('owned-owner'),b'')
    with pytest.raises(vault.VaultError):
        vault.get('owned-owner','missing')


def test_delete_many_publishes_one_new_record(monkeypatch):
    vault.put('owned-owner','site:owned.test','owned-site')
    vault.put('owned-owner','cookies:owned.test','owned-cookies')
    original = storage.FileTransaction.put
    published = []
    def put(self,ns,key,blob):
        published.append((ns,key))
        return original(self,ns,key,blob)
    monkeypatch.setattr(storage.FileTransaction,'put',put)
    vault.delete_many('owned-owner',['site:owned.test','cookies:owned.test'])
    assert vault.entries('owned-owner') == {}
    assert published == [('vault',vault._key('owned-owner'))]


@pytest.mark.parametrize('component',['root','store','vault','lock'])
@pytest.mark.parametrize('own_target',[False,True])
def test_settlement_refuses_replaced_storage_without_touching_replacement(component,own_target):
    vault.put('owned-owner','one','before')
    selected = vault.snapshot('owned-owner','one',for_update=True)
    pending = vault.prepare_snapshot_update(selected,'after')
    if own_target:
        assert vault.settle_snapshot_update(pending) == 'after'
    root = config.MEMORY_DIR
    target = {'root':root,'store':root/'store','vault':root/'store/vault',
              'lock':root/'store/vault.transaction.lock'}[component]
    retained = target.with_name(target.name+'-retained-owned')
    target.rename(retained)
    if component == 'lock':
        target.write_bytes(retained.read_bytes())
    else:
        shutil.copytree(retained,target)
    def inventory():
        return {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                for p in root.rglob('*') if p.is_file()}
    before = inventory()
    with pytest.raises(vault.VaultError):
        vault.settle_snapshot_update(pending)
    assert inventory() == before


@pytest.mark.parametrize('fault_stage',['staging_write','publication_barrier'])
def test_lost_publication_barrier_retry_republishes_identical_target(monkeypatch,fault_stage):
    """POSIX directory-fsync / Windows completion-check fault, not a power-cut test."""
    vault.put('owned-owner','one','before')
    pending = vault.prepare_snapshot_update(vault.snapshot('owned-owner','one',for_update=True),'after')
    original_sync = native.Directory.sync
    original_write = native.Directory._write
    barriers = []
    writes = []
    fail = [True]
    def write(self,fd,data):
        result = original_write(self,fd,data)
        if self.path.name == 'vault' and data == pending._ciphertext:
            writes.append(data)
        if self.path.name == 'vault' and fault_stage == 'staging_write' and fail[0]:
            fail[0] = False
            raise OSError('owned fault after staging-file fsync before publication')
        return result
    def sync(self):
        # POSIX publication has a staging-directory barrier before rename.
        # Target the real published image, not a platform-specific call count.
        target_visible = (self.path.name == 'vault'
            and self.read(vault._key('owned-owner'),vault._ROW_CAP) == pending._ciphertext)
        if target_visible:
            barriers.append('attempt')
            if fault_stage == 'publication_barrier' and fail[0]:
                fail[0] = False
                raise OSError('owned fault after publication before directory barrier')
        result = original_sync(self)
        if target_visible:barriers.append('completed')
        return result
    before = _blob()
    monkeypatch.setattr(native.Directory,'_write',write)
    monkeypatch.setattr(native.Directory,'sync',sync)
    with pytest.raises(vault.VaultError):
        vault.settle_snapshot_update(pending)
    assert fail[0] is False
    published = _blob()
    target = pending._ciphertext
    assert published == (before if fault_stage == 'staging_write' else target)
    assert 'completed' not in barriers
    assert writes == [target]
    assert vault.settle_snapshot_update(pending) == 'after'
    assert _blob() == target and 'completed' in barriers
    assert writes == [target,target]


def test_snapshot_captures_root_backend_and_key(monkeypatch,tmp_path):
    vault.put('owned-owner','one','before')
    selected = vault.snapshot('owned-owner','one',for_update=True)
    original_root = config.MEMORY_DIR
    monkeypatch.setattr(config,'MEMORY_DIR',tmp_path/'different-root')
    monkeypatch.setenv('OLYMPUS_SECRET_KEY','different-owned-key')
    store.reset()
    pending = vault.prepare_snapshot_update(selected,'after')
    assert vault.settle_snapshot_update(pending) == 'after'
    assert not config.MEMORY_DIR.exists()
    data,_ = records.decode('owned-owner',(original_root/'store/vault'/vault._key('owned-owner')).read_bytes(),
                             b'owned-synthetic-vault-old-key')
    assert data == {'one':'after'}


@pytest.mark.parametrize('action',['exchange','refresh'])
@pytest.mark.parametrize('fault',['missing_key','legacy','backend','scrypt'])
def test_oauth_refuses_before_dispatch_when_mutation_cannot_be_preflighted(monkeypatch,action,fault):
    if action == 'refresh':vault.put('owned-owner','google',_expired())
    if fault == 'missing_key':monkeypatch.delenv('OLYMPUS_SECRET_KEY')
    elif fault == 'legacy':
        store.backend().put('vault',vault._key('owned-owner'),_legacy({'google':_expired()}))
    elif fault == 'backend':
        monkeypatch.setattr(store,'_backend',object())
    else:
        def unavailable(*a,**k):raise codec.EnvelopeError('owned unavailable scrypt')
        monkeypatch.setattr(codec,'_cipher',unavailable)
    calls = []
    monkeypatch.setattr(oauth,'_post_token',lambda data:calls.append(data))
    with pytest.raises(vault.VaultError):
        oauth.exchange_code('owned-owner','owned-code') if action == 'exchange' else oauth.access_token('owned-owner')
    assert calls == []


@pytest.mark.parametrize('concurrent',['disconnect','replace'])
def test_oauth_stale_response_cannot_resurrect_or_overwrite(monkeypatch,concurrent):
    vault.put('owned-owner','google',_expired())
    calls = []
    def provider(request):
        calls.append(1)
        if concurrent == 'disconnect':vault.delete('owned-owner','google')
        else:vault.put('owned-owner','google',{'access_token':'owned-reconnected'})
        return {'access_token':'owned-obsolete-response','expires_in':3600}
    monkeypatch.setattr(oauth,'_post_token',provider)
    with pytest.raises(oauth.OAuthSettlementError) as refusal:
        oauth.access_token('owned-owner')
    with pytest.raises(vault.VaultConflict):refusal.value.settle()
    assert calls == [1]
    assert vault.get('owned-owner','google') == (None if concurrent == 'disconnect'
                                                else {'access_token':'owned-reconnected'})


@pytest.mark.parametrize('fault',['prepare','put_ack'])
def test_oauth_retained_completion_retries_only_local_work(monkeypatch,fault):
    vault.put('owned-owner','google',_expired())
    calls = []
    monkeypatch.setattr(oauth,'_post_token',lambda data:(calls.append(1) or
        {'access_token':'owned-new-token','refresh_token':'owned-new-refresh','expires_in':3600}))
    once = [True]
    if fault == 'prepare':
        original = vault.prepare_snapshot_update
        def prepare(*a,**k):
            if once[0]:
                once[0] = False
                raise vault.VaultError('owned preparation fault')
            return original(*a,**k)
        monkeypatch.setattr(vault,'prepare_snapshot_update',prepare)
    else:
        original = storage.FileTransaction.put
        def put(*a,**k):
            result = original(*a,**k)
            if once[0]:
                once[0] = False
                raise OSError('owned lost local acknowledgement')
            return result
        monkeypatch.setattr(storage.FileTransaction,'put',put)
    with pytest.raises(oauth.OAuthSettlementError) as pending:
        oauth.access_token('owned-owner')
    before = _blob()
    assert pending.value.settle() == 'owned-new-token'
    assert calls == [1]
    assert vault.get('owned-owner','google')['refresh_token'] == 'owned-new-refresh'
    if fault == 'put_ack':assert _blob() == before
    assert 'owned-new-token' not in repr(pending.value)


@pytest.mark.parametrize('mode',['oversize','duplicate','invalid','close'])
def test_bounded_provider_response_and_cleanup_outcomes(monkeypatch,mode):
    calls, reads = [], []
    raw = {'oversize':b'x'*(oauth.MAX_TOKEN_RESPONSE+1),
           'duplicate':b'{"access_token":"a","access_token":"b"}',
           'invalid':b'{"access_token":null}',
           'close':b'{"access_token":"owned-valid-token","expires_in":3600}'}[mode]
    class Response:
        def __enter__(self):return self
        def read(self,cap):
            reads.append(cap)
            return raw[:cap]
        def close(self):
            if mode == 'close':raise OSError('owned response cleanup fault')
    def opener(*a,**k):
        calls.append(1)
        return Response()
    monkeypatch.setattr(oauth.urllib.request,'urlopen',opener)
    with pytest.raises(oauth.OAuthOutcomeUnknown) as result:
        oauth.exchange_code('owned-owner','owned-code')
    assert calls == [1] and reads == [oauth.MAX_TOKEN_RESPONSE+1]
    if mode == 'close':
        assert isinstance(result.value,oauth.OAuthSettlementError)
        assert result.value.settle() == 'Google account connected.'
        assert vault.get('owned-owner','google')['access_token'] == 'owned-valid-token'
        assert calls == [1]
    else:assert not isinstance(result.value,oauth.OAuthSettlementError)


def test_http_error_body_and_close_details_never_escape(monkeypatch):
    class Body(io.BytesIO):
        failed = False
        def read(self,*a):raise AssertionError('remote error body must not be read')
        def close(self):
            if not self.failed:
                self.failed = True
                raise OSError('owned-sensitive-error-detail')
            return super().close()
    body = Body(b'owned-sensitive-response')
    error = urllib.error.HTTPError('https://owned.invalid',400,'owned-sensitive-message',{},body)
    def opener(*a,**k):raise error
    monkeypatch.setattr(oauth.urllib.request,'urlopen',opener)
    with pytest.raises(oauth.OAuthOutcomeUnknown) as result:
        oauth.exchange_code('owned-owner','owned-code')
    assert 'owned-sensitive' not in str(result.value)
    assert result.value.__context__ is None
    assert result.value.status()['response_cleanup_unconfirmed']
    assert not result.value.status()['storage_cleanup_unconfirmed']


def test_cross_purpose_archives_and_legacy_compatibility():
    blob = vault.encrypt_bytes(b'owned-archive',purpose='notes')
    assert vault.decrypt_bytes(blob,purpose='notes') == b'owned-archive'
    with pytest.raises(vault.VaultError):vault.decrypt_bytes(blob,purpose='backup')
    from cryptography.fernet import Fernet
    legacy = Fernet(base64.urlsafe_b64encode(hashlib.sha256(
        b'owned-synthetic-vault-old-key').digest())).encrypt(b'owned-legacy-archive')
    assert vault.decrypt_bytes(legacy,purpose='backup') == b'owned-legacy-archive'


def test_codec_admission_precedes_allocation_and_releases_on_interrupt(monkeypatch):
    gate = codec._MESSAGE_GATE
    assert gate.acquire(blocking=False)
    calls = []
    monkeypatch.setattr(codec.os,'urandom',lambda n:(calls.append(n) or b'x'*n))
    try:
        with pytest.raises(codec.EnvelopeError,match='capacity'):
            codec.encrypt(b'owned',b'owned-key',context=b'owned',max_plaintext=5)
        with pytest.raises(codec.EnvelopeError,match='capacity'):
            codec.decrypt(b'owned',b'owned-key',context=b'owned',max_plaintext=5,allow_legacy=True)
        assert calls == []
    finally:gate.release()
    def interrupted(*a,**k):raise KeyboardInterrupt()
    monkeypatch.setattr(codec,'_cipher',interrupted)
    with pytest.raises(KeyboardInterrupt):
        codec.encrypt(b'owned',b'owned-key',context=b'owned',max_plaintext=5)
    assert gate.acquire(blocking=False)
    gate.release()


def test_pending_handle_refuses_foreign_thread():
    pending = vault.prepare_snapshot_update(vault.snapshot('owned-owner','one',for_update=True),'owned')
    errors = []
    def other():
        try:vault.settle_snapshot_update(pending)
        except vault.VaultError:errors.append('refused')
    thread = threading.Thread(target=other)
    thread.start()
    thread.join(5)
    assert not thread.is_alive() and errors == ['refused']
    assert _blob() is None

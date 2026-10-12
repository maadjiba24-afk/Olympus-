"""Optional private filesystem binding; owned roots and mocked DB selectors."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from olympus import config, native_cleanup, store, vault, vault_selection
from olympus import vault_custody as custody, vault_native as native, vault_storage as storage


@pytest.fixture
def owned(tmp_path,monkeypatch):
    root=tmp_path/'memory';root.mkdir()
    monkeypatch.setattr(config,'MEMORY_DIR',root)
    monkeypatch.setattr(store,'_backend',store.FileStore())
    monkeypatch.delenv('OLYMPUS_DATABASE_URL',raising=False)
    monkeypatch.delenv('OLYMPUS_SECRET_KEY_FILE',raising=False)
    monkeypatch.setenv('OLYMPUS_SECRET_KEY','owned-root-binding-material')
    monkeypatch.setenv('OLYMPUS_ENV','test')
    return root


def identity(path):
    directory=native.Directory(path)
    try:return storage._directory_identity(directory)
    finally:directory.close()


def material():
    return custody.CapturedPassphrase(b'owned-explicit-material','environment',False,False)


def inventory(*roots):
    return {str(root):{'directories': sorted(p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_dir()),
                      'files': {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}}
            if root.exists() else None for root in roots}


def forbidden(*args,**kwargs):
    pytest.fail('refused binding reached a callback or mutation')


@pytest.mark.parametrize('bad',['missing_root','missing_identity','text_root','relative_root',
    'list_identity','short_identity','long_identity','bool_identity','negative_identity'])
def test_malformed_binding_refuses_before_callbacks(owned,monkeypatch,bad):
    root,expected=owned,identity(owned)
    values={
        'missing_root':(None,expected),'missing_identity':(root,None),
        'text_root':(str(root),expected),'relative_root':(Path('memory'),expected),
        'list_identity':(root,list(expected)),'short_identity':(root,(1,)),
        'long_identity':(root,(1,2,3)),'bool_identity':(root,(True,2)),
        'negative_identity':(root,(-1,2))}
    root,expected=values[bad]
    monkeypatch.setattr(store,'backend',forbidden);monkeypatch.setattr(custody,'capture',forbidden)
    with pytest.raises((vault.VaultError,storage.StorageError)):
        vault._capture('owned-owner',root=root,expected_root_identity=expected)
    assert list(owned.iterdir())==[]


def test_wrong_root_identity_refuses_before_backend_or_custody(owned,monkeypatch):
    device,inode=identity(owned)
    monkeypatch.setattr(store,'backend',forbidden);monkeypatch.setattr(custody,'capture',forbidden)
    with pytest.raises(vault.VaultError):
        vault._capture('owned-owner',root=owned,expected_root_identity=(device,inode+1))
    assert list(owned.iterdir())==[]


def test_explicit_absent_binding_never_creates_root(owned,monkeypatch):
    absent=owned/'absent'
    monkeypatch.setattr(store,'backend',forbidden);monkeypatch.setattr(custody,'capture',forbidden)
    with pytest.raises(OSError):
        vault._capture('owned-owner',root=absent,expected_root_identity=(1,2))
    assert list(owned.iterdir())==[]


@pytest.mark.parametrize('snapshot',[False,True])
@pytest.mark.parametrize('kind',['file','environment'])
def test_bound_callback_drift_keeps_original_root_and_selectors(owned,monkeypatch,snapshot,kind):
    expected=identity(owned);first=owned.parent;other=first/'other';other.mkdir()
    monkeypatch.chdir(first);monkeypatch.setattr(config,'MEMORY_DIR',Path('memory'))
    if kind=='file':
        key=first/'key';fd=os.open(key,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:assert os.write(fd,b'owned-root-binding-material')==len(b'owned-root-binding-material')
        finally:os.close(fd)
        monkeypatch.delenv('OLYMPUS_SECRET_KEY');monkeypatch.setenv('OLYMPUS_SECRET_KEY_FILE','key')
    calls=[];backend=store.FileStore()
    def drift():
        calls.append('backend');monkeypatch.chdir(other)
        monkeypatch.setattr(config,'MEMORY_DIR',other/'foreign')
        if kind=='file':monkeypatch.setenv('OLYMPUS_SECRET_KEY_FILE','foreign-key')
        else:monkeypatch.setenv('OLYMPUS_SECRET_KEY','owned-foreign-material')
    if snapshot:
        original=store.FileStore.__init__
        def initialize(self):original(self);drift()
        monkeypatch.setattr(store,'_backend',None)
        monkeypatch.setattr(store.FileStore,'__init__',initialize)
    else:
        def selected():drift();return backend
        monkeypatch.setattr(store,'backend',selected)
    captured=vault._capture('owned-owner',root=owned,expected_root_identity=expected,snapshot_selection=snapshot)
    assert calls==['backend'] and captured.root==owned and captured._root_identity==expected
    assert captured.passphrase.material==b'owned-root-binding-material'
    assert list(owned.iterdir())==[] and list(other.iterdir())==[]


@pytest.mark.parametrize('callback',['backend','custody'])
@pytest.mark.parametrize('change',['replacement','missing'])
def test_persistent_callback_root_change_refuses(owned,monkeypatch,callback,change):
    expected=identity(owned);(owned/'marker').write_bytes(b'owned-retained')
    retained=owned.with_name('retained-memory');calls=[];native_refusals=[]
    def change_root():
        try:owned.rename(retained)
        except OSError as error:
            assert os.name=='nt' and getattr(error,'winerror',None) in (5,32,33)
            native_refusals.append(error)
            raise
        if change=='replacement':owned.mkdir()
    def backend():
        calls.append('backend')
        if callback=='backend':change_root()
        return store._backend
    def key(_selectors):
        calls.append('custody')
        if callback=='custody':change_root()
        return material()
    monkeypatch.setattr(store,'backend',backend);monkeypatch.setattr(custody,'capture',key)
    with pytest.raises((OSError,storage.StorageError,vault.VaultError)):
        vault._capture('owned-owner',root=owned,expected_root_identity=expected)
    assert calls==(['backend'] if callback=='backend' else ['backend','custody'])
    if native_refusals:
        assert not retained.exists() and (owned/'marker').read_bytes()==b'owned-retained'
        assert sorted(p.name for p in owned.iterdir())==['marker']
    else:
        assert (retained/'marker').read_bytes()==b'owned-retained'
        assert list(owned.iterdir())==[] if change=='replacement' else not owned.exists()


@pytest.mark.parametrize('change',['replacement','missing'])
def test_change_after_capture_refuses_before_coordination_creation(owned,change):
    expected=identity(owned);(owned/'marker').write_bytes(b'owned-retained')
    captured=vault._capture('owned-owner',root=owned,expected_root_identity=expected)
    retained=owned.with_name('retained-memory');owned.rename(retained)
    if change=='replacement':owned.mkdir()
    before=inventory(owned,retained)
    with pytest.raises(storage.StorageError):
        with vault._transaction(captured):pytest.fail('changed root yielded a transaction')
    assert inventory(owned,retained)==before


def test_root_only_binding_allows_new_children_and_carries_full_cas(owned):
    expected=identity(owned)
    captured=vault._capture('owned-owner',root=owned,expected_root_identity=expected)
    assert list(owned.iterdir())==[]
    with vault._transaction(captured) as tx:
        assert tx.get('vault','key') is None
        tx.put('vault','key',b'owned')
        full=tx.authority_identity()
    assert len(full)==4 and full[0]==expected
    with vault._transaction(captured,expected_identity=full) as tx:
        assert tx.get('vault','key')==b'owned'


@pytest.mark.parametrize('component',['store','vault','lock'])
def test_root_only_binding_does_not_weaken_full_cas(owned,component):
    expected=identity(owned)
    with storage.transaction(store.FileStore(),owned,expected_root_identity=expected) as tx:
        tx.put('vault','key',b'owned');full=tx.authority_identity()
    path={'store':owned/'store','vault':owned/'store/vault','lock':owned/'store/vault.transaction.lock'}[component]
    retained=path.with_name(path.name+'-retained');path.rename(retained)
    if component=='lock':path.write_bytes(b'owned-foreign-lock')
    else:path.mkdir()
    before=inventory(owned)
    with pytest.raises(storage.StorageError):
        with storage.transaction(store.FileStore(),owned,expected_root_identity=expected,expected_identity=full):
            pytest.fail('full CAS accepted replaced storage')
    assert inventory(owned)==before


def test_nested_binding_checks_root_without_creating_namespace(owned):
    expected=identity(owned);backend=store.FileStore()
    with storage.transaction(backend,owned,expected_root_identity=expected) as tx:
        assert not (owned/'store/vault').exists()
        with storage.transaction(backend,owned,expected_root_identity=expected) as nested:assert nested is tx
        with pytest.raises(storage.StorageError):
            with storage.transaction(backend,owned,expected_root_identity=(expected[0],expected[1]+1)):
                pytest.fail('nested root mismatch accepted')
        assert not (owned/'store/vault').exists()
        tx.put('vault','key',b'owned')


def test_direct_binding_wrong_identity_cannot_create_store(owned):
    expected=identity(owned)
    with pytest.raises(storage.StorageError):
        with storage.transaction(store.FileStore(),owned,expected_root_identity=(expected[0],expected[1]+1)):
            pytest.fail('wrong root created authority')
    assert list(owned.iterdir())==[]


def test_standalone_first_use_preserves_absent_root_contract(owned,monkeypatch):
    absent=owned/'first-use';monkeypatch.setattr(config,'MEMORY_DIR',absent)
    captured=vault._capture('owned-owner')
    assert captured._root_identity is None and not absent.exists()
    with vault._transaction(captured) as tx:tx.put('vault','key',b'owned')
    assert (absent/'store/vault/key').read_bytes()==b'owned'


def database():
    backend=object.__new__(store.PostgresStore)
    backend._dsn='owned-synthetic-database-selection'
    backend._psycopg=SimpleNamespace(connect=forbidden)
    return backend


@pytest.mark.parametrize('snapshot',[False,True])
def test_ordinary_database_capture_has_no_local_root_prerequisite(owned,monkeypatch,snapshot):
    absent=owned/'captured-memory';backend=database()
    monkeypatch.setattr(config,'MEMORY_DIR',absent);monkeypatch.setattr(store,'_backend',backend)
    calls=[]
    monkeypatch.setattr(store,'backend',lambda:backend)
    def select(*args,**kwargs):calls.append('selected');return backend
    monkeypatch.setattr(vault_selection,'capture',select)
    monkeypatch.setattr(native,'Directory',forbidden)
    captured=vault._capture('owned-owner',selected_custody=material(),snapshot_selection=snapshot)
    assert type(captured.backend) is store.PostgresStore and captured._root_identity is None
    assert calls==(['selected'] if snapshot else []) and not absent.exists()


@pytest.mark.parametrize('selection',['cached','cold','callback-result'])
def test_bound_capture_refuses_database_selection_before_custody(owned,monkeypatch,selection):
    expected=identity(owned);backend=database();calls=[]
    monkeypatch.setattr(custody,'capture',forbidden)
    monkeypatch.setattr(vault_selection,'capture',forbidden)
    if selection=='cached':monkeypatch.setattr(store,'_backend',backend)
    elif selection=='cold':
        monkeypatch.setattr(store,'_backend',None);monkeypatch.setenv('OLYMPUS_DATABASE_URL','owned-database-selection')
    def selected():calls.append('backend');return backend
    monkeypatch.setattr(store,'backend',selected)
    with pytest.raises(vault.VaultError):vault._capture('owned-owner',root=owned,expected_root_identity=expected)
    assert calls==(['backend'] if selection=='callback-result' else [])
    assert list(owned.iterdir())==[]


def test_filesystem_identity_cannot_authorize_database_transaction(owned,monkeypatch):
    absent=owned/'db-memory';backend=database()
    monkeypatch.setattr(store.PostgresStore,'vault_transaction',forbidden)
    with pytest.raises(storage.StorageError):
        with storage.transaction(backend,absent,expected_root_identity=(1,2)):
            pytest.fail('filesystem identity authorized database transaction')
    assert not absent.exists()


def test_bound_explicit_custody_bypasses_unselected_key_file(owned,monkeypatch):
    expected=identity(owned);selected=material()
    monkeypatch.delenv('OLYMPUS_SECRET_KEY');monkeypatch.setenv('OLYMPUS_SECRET_KEY_FILE','unselected-invalid-key.')
    monkeypatch.setattr(custody,'capture',forbidden)
    captured=vault._capture('owned-owner',root=owned,expected_root_identity=expected,selected_custody=selected)
    assert captured.passphrase is selected and list(owned.iterdir())==[]


@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
def test_callback_primary_survives_bound_root_close_fault(owned,monkeypatch,fault):
    expected=identity(owned);primary=fault('owned-callback-primary');closed=[]
    original=native.Directory.close
    def close(directory):original(directory);closed.append(directory);raise SystemExit('owned-secondary-close')
    def callback():raise primary
    monkeypatch.setattr(native.Directory,'close',close);monkeypatch.setattr(store,'backend',callback)
    monkeypatch.setattr(custody,'capture',forbidden)
    with pytest.raises(fault) as failure:vault._capture('owned-owner',root=owned,expected_root_identity=expected)
    assert failure.value is primary and native_cleanup.unknown(primary)
    assert len(closed)==1 and closed[0]._closed and list(owned.iterdir())==[]


@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
def test_bound_capture_cannot_return_after_uncertain_root_close(owned,monkeypatch,fault):
    expected=identity(owned);error=fault('owned-close-only');closed=[]
    original=native.Directory.close
    def close(directory):original(directory);closed.append(directory);raise error
    monkeypatch.setattr(native.Directory,'close',close)
    with pytest.raises(fault) as failure:
        vault._capture('owned-owner',root=owned,expected_root_identity=expected,selected_custody=material())
    assert failure.value is error and native_cleanup.unknown(error)
    assert len(closed)==1 and closed[0]._closed and list(owned.iterdir())==[]

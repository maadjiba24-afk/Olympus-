"""Owned root/key selection across backend callbacks, before storage mutation."""
import os
import sys
from types import SimpleNamespace
from pathlib import Path
import pytest
from olympus import config,store,vault,vault_custody as custody,vault_selection


def private(path,raw):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_BINARY',0),0o600)
    try:assert os.write(fd,raw)==len(raw)
    finally:os.close(fd)
    return path


@pytest.fixture
def owned(tmp_path,monkeypatch):
    first=tmp_path/'first';second=tmp_path/'second';first.mkdir();second.mkdir()
    (first/'state').mkdir();(second/'state').mkdir()
    private(first/'key',b'owned first material');private(second/'key',b'owned second material')
    monkeypatch.chdir(first)
    monkeypatch.setattr(config,'MEMORY_DIR',Path('state'))
    monkeypatch.delenv('OLYMPUS_SECRET_KEY',raising=False)
    monkeypatch.setenv('OLYMPUS_SECRET_KEY_FILE','key')
    monkeypatch.setenv('OLYMPUS_ENV','test')
    monkeypatch.delenv('OLYMPUS_DATABASE_URL',raising=False)
    return first,second


@pytest.mark.parametrize('root',['state.','state /child','state./child'])
def test_invalid_original_root_refuses_before_backend_or_custody(owned,monkeypatch,root):
    first,_second=owned
    monkeypatch.setattr(config,'MEMORY_DIR',Path(root))
    def forbidden(*args,**kwargs):pytest.fail('invalid root reached backend or custody')
    monkeypatch.setattr(store,'backend',forbidden);monkeypatch.setattr(custody,'capture',forbidden)
    with pytest.raises(vault.VaultError) as caught:vault.entries('owned-user')
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert list((first/'state').iterdir())==[]


@pytest.mark.skipif(os.name!='nt',reason='Windows same-drive relative root alias')
def test_drive_relative_root_refuses_before_backend(owned,monkeypatch):
    first,_second=owned
    monkeypatch.setattr(config,'MEMORY_DIR',Path(first.drive+'state'))
    def forbidden(*args,**kwargs):pytest.fail('drive-relative root reached backend')
    monkeypatch.setattr(store,'backend',forbidden)
    with pytest.raises(vault.VaultError):vault.entries('owned-user')
    assert list((first/'state').iterdir())==[]


@pytest.mark.parametrize('snapshot',[False,True],ids=['ordinary-capture','provider-snapshot'])
@pytest.mark.parametrize('kind',['file','environment'])
def test_backend_callback_cannot_redirect_captured_root_or_key(owned,monkeypatch,snapshot,kind):
    first,second=owned;calls=[]
    if snapshot:
        backend=object.__new__(store.PostgresStore)
        backend._dsn='owned-synthetic-routing-selection'
        def connector(*args,**kwargs):pytest.fail('synthetic selector attempted a real connection')
        backend._psycopg=SimpleNamespace(connect=connector)
        monkeypatch.setitem(sys.modules,'psycopg',SimpleNamespace(connect=connector))
    else:backend=store.FileStore()
    if kind=='environment':
        monkeypatch.delenv('OLYMPUS_SECRET_KEY_FILE')
        monkeypatch.setenv('OLYMPUS_SECRET_KEY','owned first material')
    def callback(*args,**kwargs):
        calls.append(1)
        monkeypatch.chdir(second);monkeypatch.setattr(config,'MEMORY_DIR',second/'state')
        if kind=='file':monkeypatch.setenv('OLYMPUS_SECRET_KEY_FILE',str(second/'key'))
        else:monkeypatch.setenv('OLYMPUS_SECRET_KEY','owned second material')
        return backend
    if snapshot:
        monkeypatch.setattr(store,'_backend',None)
        monkeypatch.setenv('OLYMPUS_DATABASE_URL','owned-synthetic-routing-selection')
        monkeypatch.setattr(vault_selection,'capture',callback)
    else:monkeypatch.setattr(store,'backend',callback)
    selected=vault._capture('owned-user',snapshot_selection=snapshot)
    assert calls==[1] and selected.root==first/'state' and selected.backend is backend
    assert selected.passphrase.material==b'owned first material'
    assert b'owned first material'.decode() not in repr(selected)
    assert list((first/'state').iterdir())==[] and list((second/'state').iterdir())==[]


def test_invalid_original_key_selector_refuses_before_backend(owned,monkeypatch):
    first,_second=owned
    monkeypatch.setenv('OLYMPUS_SECRET_KEY_FILE','key.')
    def forbidden(*args,**kwargs):pytest.fail('ambiguous key spelling reached backend')
    monkeypatch.setattr(store,'backend',forbidden)
    with pytest.raises(vault.VaultError):vault.entries('owned-user')
    assert (first/'key').read_bytes()==b'owned first material'


def test_explicit_custody_does_not_inspect_unselected_environment_file(owned,monkeypatch):
    first,_second=owned
    monkeypatch.setenv('OLYMPUS_SECRET_KEY_FILE','unselected-invalid-key.')
    backend=store.FileStore();monkeypatch.setattr(store,'backend',lambda:backend)
    def forbidden(*args,**kwargs):pytest.fail('explicit custody reacquired environment material')
    monkeypatch.setattr(custody,'capture',forbidden)
    material=custody.CapturedPassphrase(b'owned explicit material','environment',False,False)
    selected=vault._capture('owned-user',selected_custody=material)
    assert selected.root==first/'state' and selected.passphrase is material


@pytest.mark.parametrize('spelling',['.','..'])
def test_directory_dot_selectors_bind_before_backend_callback(owned,monkeypatch,spelling):
    first,second=owned;backend=store.FileStore()
    monkeypatch.setattr(config,'MEMORY_DIR',Path(spelling))
    expected=first if spelling=='.' else first.parent
    drift_to=second/'nested';drift_to.mkdir()
    assert expected != (drift_to if spelling=='.' else drift_to.parent)
    def drift():
        monkeypatch.chdir(drift_to);return backend
    monkeypatch.setattr(store,'backend',drift)
    selected=vault._capture('owned-user')
    assert selected.root==expected and selected.passphrase.material==b'owned first material'
    assert list((first/'state').iterdir())==[] and list((second/'state').iterdir())==[]


def test_absolute_filesystem_root_selection_is_non_io(owned,monkeypatch):
    first,_second=owned;backend=store.FileStore()
    monkeypatch.setattr(config,'MEMORY_DIR',Path(first.anchor))
    monkeypatch.setattr(store,'backend',lambda:backend)
    def forbidden(*args,**kwargs):pytest.fail('root selection performed storage or custody I/O')
    monkeypatch.setattr(custody,'capture',forbidden)
    monkeypatch.setattr(vault.storage,'transaction',forbidden)
    material=custody.CapturedPassphrase(b'owned explicit material','environment',False,False)
    selected=vault._capture('owned-user',selected_custody=material)
    assert selected.root==Path(first.anchor) and selected.backend is backend

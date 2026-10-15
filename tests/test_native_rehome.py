"""Owned compatibility and import-order controls for the private class move."""
import os
from pathlib import Path
import subprocess
import sys
import pytest
from olympus import native_cleanup, store, vault_native as native, vault_storage as storage


def test_shared_alias_and_children_keep_exact_qualified_identity(tmp_path):
    (tmp_path/'child').mkdir();(tmp_path/'child'/'key').write_bytes(b'owned alias image')
    resources=[];primary=None
    try:
        shared=storage._Directory(tmp_path);resources.append(shared)
        captured=native.Directory(tmp_path);resources.append(captured)
        child=captured.child('child');resources.append(child)
        assert native.Directory is storage._CapturedDirectory is storage._Directory
        assert type(child) is type(captured) is native.Directory
        assert type(shared) is type(captured) is storage._Directory
        assert child.read('key',100)==b'owned alias image'
        shared.check();captured.check()
    except BaseException as error:
        primary=error;raise
    finally:
        actions=[resource.close for resource in reversed(resources)];resources.clear()
        native_cleanup.drain(actions,primary)


def test_file_transaction_keeps_private_class_and_existing_helper_hooks(tmp_path,monkeypatch):
    calls=[];original=storage._file_information
    def observed(fd):calls.append(fd);return original(fd)
    monkeypatch.setattr(storage,'_file_information',observed)
    with storage.transaction(store.FileStore(),tmp_path) as transaction:
        transaction.put('vault','key',b'owned transaction image')
        assert transaction.get('vault','key')==b'owned transaction image'
        assert transaction.directories and all(type(item) is native.Directory for item in transaction.directories)
        assert calls


@pytest.mark.parametrize('first',['vault_native','vault_storage'])
def test_private_alias_import_order_in_owned_fresh_process(tmp_path,first):
    source=Path(__file__).resolve().parents[1]
    script='''import importlib,socket,sys
def deny(*args,**kwargs):raise AssertionError('owned import context cannot use network or child processes')
for method in ('connect','connect_ex','bind','sendto','sendmsg'):
    if hasattr(socket.socket,method):setattr(socket.socket,method,deny)
socket.create_connection=deny
socket.getaddrinfo=deny
def audit(event,args):
    if event in ('subprocess.Popen','os.system','os.posix_spawn','os.spawn','os.exec'):deny()
sys.addaudithook(audit)
sys.path.insert(0,sys.argv[1])
importlib.import_module('olympus.'+sys.argv[2])
from olympus import vault_native,vault_storage
assert vault_native.Directory is vault_storage._CapturedDirectory
assert vault_storage._Directory is vault_native.Directory
print('owned-import-order-confirmed')
'''
    result=subprocess.run([sys.executable,'-I','-B','-c',script,str(source),first],
        cwd=tmp_path,capture_output=True,timeout=25,check=False)
    assert result.returncode==0
    assert result.stdout==b'owned-import-order-confirmed'+os.linesep.encode()
    assert result.stderr==b''

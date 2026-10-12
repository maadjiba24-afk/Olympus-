"""Real child-process restore lock controls; requires the full owned-process harness."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from olympus import archive_io, backup_restore as restore


_CHILD = r'''
import json,socket,sys
from pathlib import Path
def denied(*args,**kwargs):raise AssertionError('network forbidden in owned restore lock control')
socket.socket.connect=denied;socket.socket.connect_ex=denied
socket.socket.bind=denied;socket.create_connection=denied
sys.path.insert(0,sys.argv[1])
from olympus import backup_restore as restore
observed=[];original=restore._try_lock
def lock(fd):
    value=original(fd);observed.append(value);return value
restore._try_lock=lock
try:
    result=restore.resume(Path(sys.argv[2]),operation_id=sys.argv[4],live_root=Path(sys.argv[3]))
except restore.RestoreError:
    print(json.dumps({'outcome':'refused','lock_results':observed}))
else:
    print(json.dumps({'outcome':'completed','lock_results':observed,'restored':result['restored']}))
'''


def test_separate_process_native_restore_lock_and_release(tmp_path):
    live,dest = tmp_path/'live',tmp_path/'offline'
    live.mkdir()
    opid = uuid.uuid4().hex
    evidence = {'archive_sha256':hashlib.sha256(b'owned archive').hexdigest(),
                'signature_sha256':None,'signed':False,'signature_ok':False,'signature_override':False,
                'archive_encryption':archive_io.encryption_metadata(plaintext=True)}
    restore.restore(dest,{'a.txt':b'A'},evidence,operation_id=opid,live_root=live)
    args = [sys.executable,'-I','-B','-c',_CHILD,str(Path(restore.__file__).parents[1]),str(dest),str(live),opid]
    def child():
        result = subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=25,check=False,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        assert result.returncode == 0 and result.stderr == b''
        return json.loads(result.stdout)
    with restore._authority(dest,live,(),opid,initialize=False):
        assert child() == {'outcome':'refused','lock_results':[False]}
    assert child() == {'outcome':'completed','lock_results':[True],'restored':1}
    assert (dest/'a.txt').read_bytes() == b'A'

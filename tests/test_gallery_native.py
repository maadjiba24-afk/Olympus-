"""Required native lifecycle legs; platform skips are not successful execution."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest
from olympus import gallery, gallery_state as state, sandbox


@pytest.fixture
def owned(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, 'workdir', lambda: tmp_path)
    return tmp_path


def png():
    from PIL import Image
    output = io.BytesIO()
    Image.new('RGB', (2, 2), 'blue').save(output, 'PNG')
    return output.getvalue()


def started(owner='Owner.With.Punctuation', op='nativeop'):
    store = state.Store(owner)
    store.reserve(op, 'generate', {}, 'native.png')
    store.start(op)
    return store


def test_local_lock_wait_is_bounded(owned, monkeypatch):
    root = state.Store('owner').root
    entered = threading.Event()
    release = threading.Event()
    def held():
        with state._guard(root, create=True):
            entered.set()
            assert release.wait(10)
    thread = threading.Thread(target=held)
    thread.start()
    assert entered.wait(10)
    monkeypatch.setattr(state, 'LOCK_TIMEOUT', 0.01)
    try:
        with pytest.raises(state.GalleryError, match='timed out'):
            state.Store('owner').reserve('blocked', 'generate', {})
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()


@pytest.mark.skipif(os.name != 'nt', reason='native Windows extended path and handle evidence')
def test_native_windows_deep_exact_owner_lifecycle(owned, monkeypatch):
    workspace = owned / ('owned-' + 'a' * 100) / ('owned-' + 'b' * 100)
    # Directory helpers create these owned paths using the extended syntax.
    root = state.Directory(workspace, create=True)
    root.close()
    monkeypatch.setattr(sandbox, 'workdir', lambda: workspace)
    store = started()
    assert len(str(store.root)) > 260
    result = store.finalize('nativeop', png())
    image = result['image']
    assert store.read('native.png')[0] == png()
    assert store.recover('nativeop')['status'] == 'complete'
    assert store.delete('native.png', image['id'], image['revision'], 'delete')['status'] == 'deleted'
    assert store.recover('delete')['status'] == 'deleted'
    assert gallery.list_images('Owner.With.Punctuation') == []
    assert gallery.list_images('Owner@With@Punctuation') == []


def _set_reparse(handle, target):
    import ctypes
    import struct
    from ctypes import wintypes
    kernel = _kernel()
    text = state._native(target)
    substitute = ('\\??\\' + text[4:]).encode('utf-16-le')
    printable = str(target).encode('utf-16-le')
    paths = substitute + b'\0\0' + printable + b'\0\0'
    payload = struct.pack('<IHHHHHH', 0xA0000003, len(paths) + 8, 0,
                          0, len(substitute), len(substitute) + 2, len(printable)) + paths
    returned = wintypes.DWORD()
    buffer = ctypes.create_string_buffer(payload)
    changed = kernel.DeviceIoControl(handle, 0x000900A4, buffer, len(payload),
                                      None, 0, ctypes.byref(returned), None)
    return bool(changed), ctypes.get_last_error()


def _junction(link, target):
    import ctypes
    os.mkdir(state._native(link))
    kernel = _kernel()
    handle = kernel.CreateFileW(state._native(link), 0x40000000, 7, None, 3, 0x02200000, None)
    assert handle != ctypes.c_void_p(-1).value, ctypes.get_last_error()
    try:
        changed, error = _set_reparse(handle, target)
        assert changed, error
    finally:
        kernel.CloseHandle(handle)
    assert os.lstat(state._native(link)).st_file_attributes & 0x400


@pytest.mark.skipif(os.name != 'nt', reason='native Windows junction refusal')
def test_native_windows_owner_junction_refused(owned):
    store = state.Store('owner')
    os.mkdir(state._native(store.root.parent))
    target = owned / 'owned-target'
    target.mkdir()
    (target / 'state.json').write_bytes(b'preserved')
    _junction(store.root, target)
    with pytest.raises(state.GalleryError):
        store.reserve('op', 'generate', {})
    assert (target / 'state.json').read_bytes() == b'preserved'
    assert list(target.iterdir()) == [target / 'state.json']


@pytest.mark.skipif(os.name != 'nt', reason='native Windows leaf reparse refusal')
def test_native_windows_leaf_reparse_refused(owned):
    store = started()
    result = store.finalize('nativeop', png())
    blob = store.root / 'objects' / (result['image']['id'] + '.blob')
    os.rename(state._native(blob), state._native(blob.with_suffix('.preserved')))
    target = owned / 'leaf-target'
    target.mkdir()
    (target / 'secret.txt').write_text('owned-secret')
    _junction(blob, target)
    with pytest.raises(state.GalleryError):
        store.read('native.png')
    assert (target / 'secret.txt').read_text() == 'owned-secret'


def _kernel():
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
                                       wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                       ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    kernel.DeviceIoControl.restype = wintypes.BOOL
    return kernel


@pytest.mark.skipif(os.name != 'nt', reason='native Windows pinned directory rename denial')
def test_native_windows_pinned_parent_blocks_rename(owned):
    parent = owned / 'pinned'
    parent.mkdir()
    directory = state.Directory(parent)
    try:
        with pytest.raises(PermissionError):
            os.rename(state._native(parent), state._native(owned / 'renamed'))
    finally:
        directory.close()


@pytest.mark.skipif(os.name != 'nt', reason='native Windows actual attribute mutation and operation confinement')
@pytest.mark.parametrize('desired_access', [0, 0x00000080, 0x00000100, 0x40000000])
def test_native_windows_reparse_mutation_does_not_redirect_operations(owned, desired_access):
    """The original candidate's ioctl256 attack SUCCEEDED; don't erase that fact.

    Share modes do not protect attributes. Assert confinement of operations,
    rather than claiming that the OS must deny reparse metadata mutation.
    """
    import ctypes
    parent, target = owned / 'pinned-ioctl', owned / 'redirect-target'
    parent.mkdir()
    target.mkdir()
    (target / 'marker.txt').write_bytes(b'external-preserved')
    _junction(owned / 'unpinned-control', target)
    directory = state.Directory(parent)
    kernel = _kernel()
    handle = kernel.CreateFileW(state._native(parent), desired_access, 7, None, 3, 0x02200000, None)
    try:
        assert handle != ctypes.c_void_p(-1).value, ctypes.get_last_error()
        changed, error = _set_reparse(handle, target)
        if desired_access == 0x100:
            assert changed, ('Known FILE_WRITE_ATTRIBUTES attack must execute', error)
        if changed:
            operations = [lambda: directory.read('marker.txt', 100),
                          lambda: directory.write_new('created.blob', b'owned'),
                          lambda: list(directory.entries()),
                          lambda: directory.child('created-child', create=True),
                          lambda: directory.publish('published.json', b'owned'),
                          directory.free_bytes]
            for operation in operations:
                with pytest.raises(OSError):
                    operation()
        else:
            assert error in (5, 32, 1314)
    finally:
        kernel.CloseHandle(handle)
        directory.close()
    assert sorted(p.name for p in target.iterdir()) == ['marker.txt']
    assert (target / 'marker.txt').read_bytes() == b'external-preserved'


@pytest.mark.skipif(os.name != 'nt', reason='native Windows reparse race at relative-open seam')
@pytest.mark.parametrize('operation', ['read', 'write', 'child'])
def test_native_windows_mutation_after_check_cannot_redirect_relative_open(owned, monkeypatch, operation):
    import ctypes
    from olympus import gallery_windows as native
    parent, target = owned / 'race-parent', owned / 'race-target'
    parent.mkdir()
    target.mkdir()
    (target / 'marker.txt').write_bytes(b'external-preserved')
    directory = state.Directory(parent)
    kernel = _kernel()
    attack = kernel.CreateFileW(state._native(parent), 0x100, 7, None, 3, 0x02200000, None)
    assert attack != ctypes.c_void_p(-1).value
    original = native.open_relative
    attacked = []
    def raced(handle, name, **kwargs):
        if handle == directory.win.handle and not attacked:
            changed, error = _set_reparse(attack, target)
            assert changed, error
            attacked.append(True)
        return original(handle, name, **kwargs)
    monkeypatch.setattr(native, 'open_relative', raced)
    try:
        try:
            if operation == 'read':
                result = directory.read('marker.txt', 100)
                assert result != b'external-preserved'
            elif operation == 'write':
                directory.write_new('created.blob', b'owned')
            else:
                child = directory.child('created-child', create=True)
                child.close()
        except OSError:
            pass  # Explicit refusal is safe; the redirect must remain untouched.
        assert attacked == [True]
    finally:
        kernel.CloseHandle(attack)
        directory.close()
    assert sorted(p.name for p in target.iterdir()) == ['marker.txt']
    assert (target / 'marker.txt').read_bytes() == b'external-preserved'


@pytest.mark.skipif(os.name != 'nt', reason='native Windows existing attribute handle cannot authorize redirect')
def test_native_windows_existing_attribute_handle_does_not_redirect(owned):
    import ctypes
    parent, target = owned / 'write-held', owned / 'write-target'
    parent.mkdir()
    target.mkdir()
    kernel = _kernel()
    handle = kernel.CreateFileW(state._native(parent), 0x100, 7, None, 3, 0x02200000, None)
    assert handle != ctypes.c_void_p(-1).value
    directory = state.Directory(parent)
    try:
        changed, error = _set_reparse(handle, target)
        assert changed, error
        with pytest.raises(OSError):
            directory.write_new('refused.blob', b'owned')
    finally:
        directory.close()
        kernel.CloseHandle(handle)
    assert list(target.iterdir()) == []


@contextlib.contextmanager
def _deny_delete(path):
    import ctypes
    kernel = _kernel()
    handle = kernel.CreateFileW(state._native(path), 0x80000000, 3, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value, ctypes.get_last_error()
    active = True
    def release():
        nonlocal active
        if active:
            assert kernel.CloseHandle(handle)
            active = False
    try:
        yield release
    finally:
        release()


@pytest.mark.skipif(os.name != 'nt', reason='native Windows denied replace preserves output for recovery')
def test_native_windows_denied_replace_preserves_and_recovers(owned):
    store = started()
    before = Path(state._native(store.root / 'state.json')).read_bytes()
    with _deny_delete(store.root / 'state.json'):
        with pytest.raises(state.GalleryError) as caught:
            store.finalize('nativeop', png())
        assert caught.value.code == 'publication_unconfirmed'
        assert Path(state._native(store.root / 'state.json')).read_bytes() == before
    assert list(Path(state._native(store.root)).glob('pending-*.json'))
    assert store.recover('nativeop')['status'] == 'complete'
    assert store.read('native.png')[0] == png()


@pytest.mark.skipif(os.name != 'nt', reason='native Windows real sharing retry')
def test_native_windows_sharing_retry_after_handle_release(owned, monkeypatch):
    store = started()
    from olympus import gallery_windows as native
    original = native.rename_same_directory
    denials = []
    with _deny_delete(store.root / 'state.json') as release:
        def replace(handle, destination):
            try:
                return original(handle, destination)
            except PermissionError as exc:
                denials.append(exc.winerror)
                release()
                raise
        monkeypatch.setattr(native, 'rename_same_directory', replace)
        assert store.finalize('nativeop', png())['status'] == 'complete'
    assert denials and all(error in (5, 32, 33) for error in denials)


@pytest.mark.skipif(os.name != 'nt', reason='native Windows lost-ack read recovery')
def test_native_windows_lost_ack_recovery(owned, monkeypatch):
    store = started()
    original = state.Directory.publish
    def lost(directory, name, data):
        original(directory, name, data)
        if name == 'state.json':
            raise OSError('owned postcommit lost acknowledgement')
    with monkeypatch.context() as patch:
        patch.setattr(state.Directory, 'publish', lost)
        with pytest.raises(state.GalleryError):
            store.finalize('nativeop', png())
    assert store.recover('nativeop')['status'] == 'complete'
    assert store.lookup('nativeop', 'generate', {}, 'native.png')['execute'] is False


@pytest.mark.skipif(os.name == 'nt', reason='POSIX competing process flock')
def test_posix_competing_process_reservation(owned):
    script = '''import json,sys
assert sys.flags.isolated and sys.dont_write_bytecode
sys.path.insert(0, sys.argv[3])
from pathlib import Path
from olympus import sandbox
from olympus.gallery_state import Store,GalleryError
sandbox.workdir=lambda: Path(sys.argv[1])
sys.stdin.read(1)
try: print(json.dumps(Store('owner').reserve(sys.argv[2], 'generate', {}, 'one.png')))
except GalleryError as exc: print(json.dumps(exc.to_dict()))
'''
    children = [subprocess.Popen([sys.executable, '-I', '-B', '-c', script, str(owned), 'op' + str(i), str(Path(state.__file__).parent.parent)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for i in range(4)]
    try:
        for child in children:
            child.stdin.write('x')
            child.stdin.flush()
        results = []
        for child in children:
            stdout, stderr = child.communicate(timeout=30)
            assert child.returncode == 0, stderr
            results.append(json.loads(stdout))
        assert sum(r.get('execute') is True for r in results) == 1
        assert sum(r.get('code') == 'name_conflict' for r in results) == 3
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX actual process death releases owner lock')
def test_posix_process_death_after_started_never_replays(owned):
    script = '''import os,sys
assert sys.flags.isolated and sys.dont_write_bytecode
sys.path.insert(0, sys.argv[2])
from pathlib import Path
from olympus import sandbox
from olympus.gallery_state import Store,_guard
sandbox.workdir=lambda: Path(sys.argv[1])
s=Store('owner')
s.reserve('crash', 'generate', {}, 'crash.png')
s.start('crash')
with _guard(s.root): os._exit(17)
'''
    result = subprocess.run([sys.executable, '-I', '-B', '-c', script, str(owned), str(Path(state.__file__).parent.parent)], capture_output=True, timeout=30)
    assert result.returncode == 17, result.stderr
    store = state.Store('owner')
    assert store.recover('crash')['status'] == 'indeterminate'
    assert store.reserve('crash', 'generate', {}, 'crash.png')['execute'] is False
    assert store.reserve('next', 'generate', {}, 'next.png')['execute'] is True


@pytest.mark.skipif(os.name == 'nt', reason='POSIX real directory fsync failure and retry')
def test_posix_directory_fsync_failure_recovery(owned, monkeypatch):
    import stat
    store = started()
    original = os.fsync
    failed = []
    def sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            failed.append(fd)
            raise OSError('owned actual directory-fsync failure')
        original(fd)
    with monkeypatch.context() as patch:
        patch.setattr(os, 'fsync', sync)
        with pytest.raises(state.GalleryError):
            store.finalize('nativeop', png())
    assert failed
    assert store.recover('nativeop')['status'] == 'indeterminate'
    assert store.finalize('nativeop', png())['status'] == 'complete'


@pytest.mark.skipif(os.name != 'nt', reason='native Windows retained-file link identity')
def test_native_windows_enumeration_uses_handle_link_identity(owned):
    directory = state.Directory(owned)
    try:
        directory.write_new('single.blob', b'owned')
        entries = list(directory.entries())
        assert len(entries) == 1 and entries[0].name == 'single.blob'
        info = entries[0].stat(follow_symlinks=False)
        assert info.st_nlink == 1 and info.st_ino != 0 and info.st_size == 5
        os.link(state._native(owned / 'single.blob'), state._native(owned / 'linked.blob'))
        with pytest.raises(OSError):
            entries[0].stat(follow_symlinks=False)
    finally:
        directory.close()


@pytest.mark.skipif(os.name != 'nt', reason='native Windows mutation at handle-enumeration syscall seam')
def test_native_windows_mutation_at_enumeration_syscall_stays_confined(owned, monkeypatch):
    import ctypes
    from olympus import gallery_windows as native
    parent, target = owned / 'scan-parent', owned / 'scan-target'
    parent.mkdir()
    target.mkdir()
    (target / 'external-only.txt').write_bytes(b'preserve-external')
    directory = state.Directory(parent)
    kernel = _kernel()
    attack = kernel.CreateFileW(state._native(parent), 0x100, 7, None, 3, 0x02200000, None)
    assert attack != ctypes.c_void_p(-1).value
    original = native._apis
    attacked = []
    class KernelProxy:
        def __init__(self, real):
            self.real = real
        def __getattr__(self, name):
            return getattr(self.real, name)
        def GetFileInformationByHandleEx(self, handle, kind, buffer, size):
            if handle == directory.win.handle and kind in (14, 15) and not attacked:
                changed, error = _set_reparse(attack, target)
                assert changed, error
                attacked.append(True)
            return self.real.GetFileInformationByHandleEx(handle, kind, buffer, size)
    def apis():
        real, nt = original()
        return KernelProxy(real), nt
    monkeypatch.setattr(native, '_apis', apis)
    seen = []
    try:
        try:
            for entry in directory.entries():
                seen.append(entry.name)
        except OSError:
            pass
        assert attacked == [True]
        assert 'external-only.txt' not in seen
    finally:
        kernel.CloseHandle(attack)
        directory.close()
    assert sorted(p.name for p in target.iterdir()) == ['external-only.txt']
    assert (target / 'external-only.txt').read_bytes() == b'preserve-external'


@pytest.mark.skipif(os.name != 'nt', reason='native Windows mutation attempt at handle-rename syscall seam')
def test_native_windows_mutation_at_rename_syscall_stays_confined(owned, monkeypatch):
    import ctypes
    from olympus import gallery_windows as native
    parent, target = owned / 'publish-parent', owned / 'publish-target'
    parent.mkdir()
    target.mkdir()
    (target / 'state.json').write_bytes(b'preserve-external-state')
    (target / 'marker.txt').write_bytes(b'preserve-external-marker')
    directory = state.Directory(parent)
    kernel = _kernel()
    attack = kernel.CreateFileW(state._native(parent), 0x100, 7, None, 3, 0x02200000, None)
    assert attack != ctypes.c_void_p(-1).value
    original = native._apis
    attempts = []
    class NtProxy:
        def __init__(self, real):
            self.real = real
        def __getattr__(self, name):
            return getattr(self.real, name)
        def NtSetInformationFile(self, handle, status, buffer, size, kind):
            if kind == 10 and not attempts:
                changed, error = _set_reparse(attack, target)
                # Some native filesystems refuse converting a nonempty
                # directory (the durable staging file already exists).
                # Preserve that distinction; the empty-dir attack above MUST
                # succeed. Either way this real rename must not touch target.
                assert changed or error in (5, 145), error
                attempts.append((changed, error))
            return self.real.NtSetInformationFile(handle, status, buffer, size, kind)
    def apis():
        real, nt = original()
        return real, NtProxy(nt)
    monkeypatch.setattr(native, '_apis', apis)
    try:
        try:
            directory.publish('state.json', b'owned-new-state')
        except OSError:
            pass
        assert len(attempts) == 1
        if not attempts[0][0]:
            assert directory.read('state.json', 100) == b'owned-new-state'
    finally:
        kernel.CloseHandle(attack)
        directory.close()
    assert sorted(p.name for p in target.iterdir()) == ['marker.txt', 'state.json']
    assert (target / 'state.json').read_bytes() == b'preserve-external-state'
    assert (target / 'marker.txt').read_bytes() == b'preserve-external-marker'

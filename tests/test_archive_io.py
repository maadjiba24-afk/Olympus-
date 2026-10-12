"""Owned archive primitive controls; no provider or real custody access."""
import base64
import hashlib
import os
import threading

import pytest

from olympus import archive_io as aio, vault_codec as codec, vault_custody as custody
from olympus import vault_storage


def _selectors(**extra):
    return {'OLYMPUS_ENV':'test', 'OLYMPUS_SECRET_KEY':'owned-archive-custody', **extra}


@pytest.mark.parametrize('size', [0, 4, 5], ids=['empty','at-cap','over-cap'])
def test_held_read_checks_size_before_reading(tmp_path, monkeypatch, size):
    path = tmp_path/'owned archive \u00e9.bin'
    path.write_bytes(b'x' * size)
    original = os.read
    reads = []
    def read(fd, length):
        reads.append(length)
        return original(fd,length)
    monkeypatch.setattr(aio.os, 'read', read)
    if size <= 4:
        assert aio.read_file(path, 4) == b'x' * size
    else:
        with pytest.raises(aio.ArchiveError):
            aio.read_file(path, 4)
        assert reads == []


def test_optional_absence_is_only_a_leaf_in_a_valid_parent(tmp_path):
    assert aio.read_file(tmp_path/'missing', 4, optional=True) is None
    with pytest.raises(aio.ArchiveError):
        aio.read_file(tmp_path/'missing-parent/file', 4, optional=True)


def test_held_read_refuses_hardlink(tmp_path):
    original = tmp_path/'original'
    original.write_bytes(b'owned')
    linked = tmp_path/'linked'
    os.link(original, linked)
    with pytest.raises(aio.ArchiveError):
        aio.read_file(linked, 10)


def test_held_read_rechecks_fresh_file_information(tmp_path, monkeypatch):
    path = tmp_path/'owned'
    path.write_bytes(b'owned')
    original = custody._information
    calls = []
    def information(fd):
        info = original(fd)
        calls.append(info.st_size)
        if len(calls) == 2:
            from types import SimpleNamespace
            values = {name:getattr(info,name) for name in
                ('st_mode','st_nlink','st_dev','st_ino','st_size','st_mtime_ns')}
            values['st_size'] += 1
            return SimpleNamespace(**values)
        return info
    monkeypatch.setattr(custody,'_information',information)
    with pytest.raises(aio.ArchiveError):
        aio.read_file(path, 10)
    assert calls == [5,5]


def test_archive_admission_refuses_before_custody_and_releases_on_interruption(monkeypatch):
    calls = []
    original = custody.capture
    def capture(selectors):
        calls.append('capture')
        return original(selectors)
    monkeypatch.setattr(custody,'capture',capture)
    with aio.operation('notes', selectors=_selectors()):
        with pytest.raises(aio.ArchiveError,match='capacity'):
            with aio.operation('backup',selectors=_selectors()):
                raise AssertionError('nested admission must refuse')
    assert calls == ['capture']
    with pytest.raises(KeyboardInterrupt):
        with aio.operation('notes',selectors=_selectors()):
            raise KeyboardInterrupt('owned interruption')
    with aio.operation('backup',selectors=_selectors()) as session:
        assert session.active


def test_plaintext_policy_does_not_open_broken_irrelevant_custody(tmp_path, monkeypatch):
    selectors = {'OLYMPUS_ENV':'test', 'OLYMPUS_SECRET_KEY_FILE':str(tmp_path/'missing')}
    calls = []
    def forbidden(*args):
        calls.append('capture')
        raise AssertionError('plaintext-only input must not select key material')
    monkeypatch.setattr(custody,'capture',forbidden)
    with aio.operation('notes',capture_key=False,selectors=selectors) as session:
        assert not session.key_selected
        assert aio.encryption_metadata(plaintext=True)['encrypted'] is False
    assert calls == []


def test_deferred_custody_freezes_relative_path_and_selectors_once(tmp_path, monkeypatch):
    first, second = tmp_path/'first', tmp_path/'second'
    first.mkdir(); second.mkdir()
    key = first/'owned-key'
    key.write_bytes(b'owned-file-custody'); key.chmod(0o600)
    monkeypatch.chdir(first)
    selectors = {'OLYMPUS_ENV':'test', 'OLYMPUS_SECRET_KEY_FILE':'owned-key'}
    with aio.operation('notes',capture_key=False,selectors=selectors) as session:
        selectors['OLYMPUS_SECRET_KEY_FILE'] = 'unselected-key'
        monkeypatch.chdir(second)
        assert session.material() == b'owned-file-custody'
        key.write_bytes(b'changed-owned-file-custody')
        assert session.material() == b'owned-file-custody'
    assert session.passphrase is None and session.selectors == {}


def test_failed_custody_does_not_retry_against_changed_file(tmp_path, monkeypatch):
    path = tmp_path/'initially-absent'
    selectors = {'OLYMPUS_ENV':'test', 'OLYMPUS_SECRET_KEY_FILE':str(path)}
    original = custody.capture
    calls = []
    def capture(selected):
        calls.append('capture')
        return original(selected)
    monkeypatch.setattr(custody,'capture',capture)
    with aio.operation('notes',capture_key=False,selectors=selectors) as session:
        with pytest.raises(custody.CustodyError):
            session.material()
        path.write_bytes(b'owned-new-key'); path.chmod(0o600)
        with pytest.raises(aio.ArchiveError):
            session.select_key()
        with pytest.raises(aio.ArchiveError):
            session.material()
    assert calls == ['capture']


def test_archive_context_separation_and_legacy_metadata():
    from cryptography.fernet import Fernet
    raw = b'owned compressed archive bytes'
    with aio.operation('notes',selectors=_selectors()) as session:
        encrypted = session.encrypt(raw)
        decoded = session.decrypt(encrypted)
        assert decoded.data == raw and aio.encryption_metadata(decoded)['context_authenticated']
    with aio.operation('backup',selectors=_selectors()) as session:
        with pytest.raises(codec.EnvelopeError):
            session.decrypt(encrypted)
        legacy = Fernet(base64.urlsafe_b64encode(hashlib.sha256(b'owned-archive-custody').digest())).encrypt(raw)
        before = legacy
        decoded = session.decrypt(legacy)
        assert decoded.data == raw and legacy == before
        assert aio.encryption_metadata(decoded) == {
            'encrypted':True,'format_version':0,'context_authenticated':False,'legacy_unbound':True}


def test_archive_session_cannot_escape_thread_or_lifetime():
    failures = []
    with aio.operation('notes',selectors=_selectors()) as session:
        def foreign():
            try:
                session.material()
            except aio.ArchiveError:
                failures.append('refused')
        thread = threading.Thread(target=foreign)
        thread.start(); thread.join(5)
        assert not thread.is_alive() and failures == ['refused']
    with pytest.raises(aio.ArchiveError):
        session.material()


@pytest.mark.skipif(os.name != 'posix', reason='POSIX descriptor acquisition control')
def test_acquisition_failure_drains_new_child_and_all_ancestors(tmp_path, monkeypatch):
    path = tmp_path / 'owned'
    path.write_bytes(b'owned')
    acquired, closed, fired = [], [], []
    real_open, real_close = os.open, os.close
    original_append = vault_storage._Directory._append_posix
    primary = RuntimeError('owned private acquisition failure')
    def opened(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        acquired.append(descriptor)
        return descriptor
    def closed_with_fault(descriptor):
        real_close(descriptor)
        closed.append(descriptor)
        if len(closed) == 1:
            raise KeyboardInterrupt('owned secondary cleanup interruption')
    def append(directory, name, create):
        original_append(directory, name, create)
        if name == tmp_path.name:
            fired.append(primary)
            raise primary
    monkeypatch.setattr(os, 'open', opened)
    monkeypatch.setattr(os, 'close', closed_with_fault)
    monkeypatch.setattr(vault_storage._Directory, '_append_posix', append)
    with pytest.raises(aio.ArchiveError) as caught:
        aio.read_file(path, 10)
    assert fired == [primary] and aio.native_cleanup.unknown(primary)
    assert aio.native_cleanup.unknown(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert 'owned private acquisition failure' not in str(caught.value)
    assert len(acquired) == len(tmp_path.parts) and closed == list(reversed(acquired))
    for descriptor in acquired:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.skipif(os.name != 'nt', reason='Windows native handle acquisition control')
def test_native_acquisition_failure_drains_all_handles_despite_close_interruption(tmp_path, monkeypatch):
    from olympus import vault_windows as native
    path = tmp_path / 'owned'
    path.write_bytes(b'owned')
    acquired, closed, fired = [], [], []
    real_anchor, real_relative, real_close = native._anchor, native.open_relative, native.close
    original_check = vault_storage._Directory.check
    primary = RuntimeError('owned private acquisition failure')
    def anchor(*args, **kwargs):
        handle = real_anchor(*args, **kwargs)
        acquired.append(handle)
        return handle
    def relative(*args, **kwargs):
        handle = real_relative(*args, **kwargs)
        acquired.append(handle)
        return handle
    def check(directory):
        original_check(directory)
        if directory.path == tmp_path:
            fired.append(primary)
            raise primary
    def close(handle):
        real_close(handle)
        closed.append(handle)
        if len(closed) == 1:
            raise KeyboardInterrupt('owned secondary cleanup interruption')
    monkeypatch.setattr(native, '_anchor', anchor)
    monkeypatch.setattr(native, 'open_relative', relative)
    monkeypatch.setattr(native, 'close', close)
    monkeypatch.setattr(vault_storage._Directory, 'check', check)
    with pytest.raises(aio.ArchiveError) as caught:
        aio.read_file(path, 10)
    assert fired == [primary] and aio.native_cleanup.unknown(primary)
    assert aio.native_cleanup.unknown(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert 'owned private acquisition failure' not in str(caught.value)
    assert len(acquired) == len(tmp_path.parts) and closed == list(reversed(acquired))
    for handle in acquired:
        with pytest.raises(OSError):
            native.information(handle, True)


def test_file_close_interruption_still_drains_directory(tmp_path, monkeypatch):
    path = tmp_path/'owned'; path.write_bytes(b'owned')
    original_close = os.close
    original_directory_close = vault_storage._Directory.close
    closed = []
    def file_close(fd):
        original_close(fd)
        if not closed:
            closed.append('file')
            raise KeyboardInterrupt('owned interruption after closing file')
    def directory_close(self):
        result = original_directory_close(self)
        closed.append('directory')
        return result
    monkeypatch.setattr(os,'close',file_close)
    monkeypatch.setattr(vault_storage._Directory,'close',directory_close)
    with pytest.raises(KeyboardInterrupt):
        aio.read_file(path,10)
    assert closed == ['file','directory']


@pytest.mark.skipif(os.name != 'nt', reason='native Windows selector contract')
@pytest.mark.parametrize('kind', ['file', 'directory'])
def test_validated_extended_local_drive_selector_has_same_authority_spelling(tmp_path, kind):
    selected = tmp_path / ('a' * 80) / ('b' * 80) / ('c' * 80) / 'owned image'
    assert len(str(selected)) > 260
    choose = aio.selected_path if kind == 'file' else aio.selected_directory
    assert choose('\\\\?\\' + str(selected)) == choose(selected) == selected


@pytest.mark.skipif(os.name != 'nt', reason='native Windows selector contract')
@pytest.mark.parametrize('leaf', ['trailing.', 'trailing ', 'CON', 'con.txt', 'owned:stream'])
@pytest.mark.parametrize('extended', [False, True])
def test_original_invalid_components_refuse_before_extended_drive_normalization(tmp_path, leaf, extended):
    selected = str(tmp_path / leaf)
    if extended:
        selected = '\\\\?\\' + selected
    for choose in (aio.selected_path, aio.selected_directory):
        with pytest.raises(aio.ArchiveError):
            choose(selected)


@pytest.mark.skipif(os.name != 'nt', reason='native Windows selector contract')
@pytest.mark.parametrize('extended', [False, True])
def test_drive_relative_selector_is_not_made_absolute_by_prefix_removal(tmp_path, extended):
    selected = tmp_path.drive + 'relative'
    if extended:
        selected = '\\\\?\\' + selected
    for choose in (aio.selected_path, aio.selected_directory):
        with pytest.raises(aio.ArchiveError):
            choose(selected)


@pytest.mark.skipif(os.name != 'nt', reason='native Windows lexical selector contract; no IO')
@pytest.mark.parametrize('selected', [
    '\\\\?\\UNC\\owned-server\\owned-share\\folder',
    '\\\\?\\Volume{00000000-0000-0000-0000-000000000001}\\folder',
    '\\\\.\\C:\\folder',
], ids=['unc-spelling', 'volume-spelling', 'device-spelling'])
def test_other_windows_selector_namespaces_keep_prior_spelling_without_io(selected):
    assert str(aio.selected_directory(selected)) == selected


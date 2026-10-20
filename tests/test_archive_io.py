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



# Portable archive leaf policy: no native filesystem access is needed here.
@pytest.fixture
def _no_platform_reserved_name_lookup(monkeypatch):
    import ntpath
    import pathlib
    import warnings
    calls = []
    def forbidden(*args, **kwargs):
        calls.append('platform-reserved-name-lookup')
        raise AssertionError('portable leaf validation must not call a platform reserved-name API')
    monkeypatch.setattr(pathlib.PureWindowsPath, 'is_reserved', forbidden, raising=False)
    monkeypatch.setattr(ntpath, 'isreserved', forbidden, raising=False)
    monkeypatch.setattr(os.path, 'isreserved', forbidden, raising=False)
    with warnings.catch_warnings():
        warnings.simplefilter('error', DeprecationWarning)
        yield
    assert calls == []


@pytest.mark.usefixtures('_no_platform_reserved_name_lookup')
@pytest.mark.parametrize('name,reason', [('CON', 'invalid archive output filename'),
 ('con.txt', 'invalid archive output filename'),
 ('cON  .tar.gz', 'invalid archive output filename'),
 ('con...image', 'invalid archive output filename'),
 ('PRN', 'invalid archive output filename'),
 ('prn.txt', 'invalid archive output filename'),
 ('pRN  .tar.gz', 'invalid archive output filename'),
 ('prn...image', 'invalid archive output filename'),
 ('AUX', 'invalid archive output filename'),
 ('aux.txt', 'invalid archive output filename'),
 ('aUX  .tar.gz', 'invalid archive output filename'),
 ('aux...image', 'invalid archive output filename'),
 ('NUL', 'invalid archive output filename'),
 ('nul.txt', 'invalid archive output filename'),
 ('nUL  .tar.gz', 'invalid archive output filename'),
 ('nul...image', 'invalid archive output filename'),
 ('CONIN$', 'invalid archive output filename'),
 ('conin$.txt', 'invalid archive output filename'),
 ('cONIN$  .tar.gz', 'invalid archive output filename'),
 ('conin$...image', 'invalid archive output filename'),
 ('CONOUT$', 'invalid archive output filename'),
 ('conout$.txt', 'invalid archive output filename'),
 ('cONOUT$  .tar.gz', 'invalid archive output filename'),
 ('conout$...image', 'invalid archive output filename'),
 ('COM1', 'invalid archive output filename'),
 ('com1.txt', 'invalid archive output filename'),
 ('cOM1  .tar.gz', 'invalid archive output filename'),
 ('com1...image', 'invalid archive output filename'),
 ('COM2', 'invalid archive output filename'),
 ('com2.txt', 'invalid archive output filename'),
 ('cOM2  .tar.gz', 'invalid archive output filename'),
 ('com2...image', 'invalid archive output filename'),
 ('COM3', 'invalid archive output filename'),
 ('com3.txt', 'invalid archive output filename'),
 ('cOM3  .tar.gz', 'invalid archive output filename'),
 ('com3...image', 'invalid archive output filename'),
 ('COM4', 'invalid archive output filename'),
 ('com4.txt', 'invalid archive output filename'),
 ('cOM4  .tar.gz', 'invalid archive output filename'),
 ('com4...image', 'invalid archive output filename'),
 ('COM5', 'invalid archive output filename'),
 ('com5.txt', 'invalid archive output filename'),
 ('cOM5  .tar.gz', 'invalid archive output filename'),
 ('com5...image', 'invalid archive output filename'),
 ('COM6', 'invalid archive output filename'),
 ('com6.txt', 'invalid archive output filename'),
 ('cOM6  .tar.gz', 'invalid archive output filename'),
 ('com6...image', 'invalid archive output filename'),
 ('COM7', 'invalid archive output filename'),
 ('com7.txt', 'invalid archive output filename'),
 ('cOM7  .tar.gz', 'invalid archive output filename'),
 ('com7...image', 'invalid archive output filename'),
 ('COM8', 'invalid archive output filename'),
 ('com8.txt', 'invalid archive output filename'),
 ('cOM8  .tar.gz', 'invalid archive output filename'),
 ('com8...image', 'invalid archive output filename'),
 ('COM9', 'invalid archive output filename'),
 ('com9.txt', 'invalid archive output filename'),
 ('cOM9  .tar.gz', 'invalid archive output filename'),
 ('com9...image', 'invalid archive output filename'),
 ('COM\xb9', 'invalid archive output filename'),
 ('com\xb9.txt', 'invalid archive output filename'),
 ('cOM\xb9  .tar.gz', 'invalid archive output filename'),
 ('com\xb9...image', 'invalid archive output filename'),
 ('COM\xb2', 'invalid archive output filename'),
 ('com\xb2.txt', 'invalid archive output filename'),
 ('cOM\xb2  .tar.gz', 'invalid archive output filename'),
 ('com\xb2...image', 'invalid archive output filename'),
 ('COM\xb3', 'invalid archive output filename'),
 ('com\xb3.txt', 'invalid archive output filename'),
 ('cOM\xb3  .tar.gz', 'invalid archive output filename'),
 ('com\xb3...image', 'invalid archive output filename'),
 ('LPT1', 'invalid archive output filename'),
 ('lpt1.txt', 'invalid archive output filename'),
 ('lPT1  .tar.gz', 'invalid archive output filename'),
 ('lpt1...image', 'invalid archive output filename'),
 ('LPT2', 'invalid archive output filename'),
 ('lpt2.txt', 'invalid archive output filename'),
 ('lPT2  .tar.gz', 'invalid archive output filename'),
 ('lpt2...image', 'invalid archive output filename'),
 ('LPT3', 'invalid archive output filename'),
 ('lpt3.txt', 'invalid archive output filename'),
 ('lPT3  .tar.gz', 'invalid archive output filename'),
 ('lpt3...image', 'invalid archive output filename'),
 ('LPT4', 'invalid archive output filename'),
 ('lpt4.txt', 'invalid archive output filename'),
 ('lPT4  .tar.gz', 'invalid archive output filename'),
 ('lpt4...image', 'invalid archive output filename'),
 ('LPT5', 'invalid archive output filename'),
 ('lpt5.txt', 'invalid archive output filename'),
 ('lPT5  .tar.gz', 'invalid archive output filename'),
 ('lpt5...image', 'invalid archive output filename'),
 ('LPT6', 'invalid archive output filename'),
 ('lpt6.txt', 'invalid archive output filename'),
 ('lPT6  .tar.gz', 'invalid archive output filename'),
 ('lpt6...image', 'invalid archive output filename'),
 ('LPT7', 'invalid archive output filename'),
 ('lpt7.txt', 'invalid archive output filename'),
 ('lPT7  .tar.gz', 'invalid archive output filename'),
 ('lpt7...image', 'invalid archive output filename'),
 ('LPT8', 'invalid archive output filename'),
 ('lpt8.txt', 'invalid archive output filename'),
 ('lPT8  .tar.gz', 'invalid archive output filename'),
 ('lpt8...image', 'invalid archive output filename'),
 ('LPT9', 'invalid archive output filename'),
 ('lpt9.txt', 'invalid archive output filename'),
 ('lPT9  .tar.gz', 'invalid archive output filename'),
 ('lpt9...image', 'invalid archive output filename'),
 ('LPT\xb9', 'invalid archive output filename'),
 ('lpt\xb9.txt', 'invalid archive output filename'),
 ('lPT\xb9  .tar.gz', 'invalid archive output filename'),
 ('lpt\xb9...image', 'invalid archive output filename'),
 ('LPT\xb2', 'invalid archive output filename'),
 ('lpt\xb2.txt', 'invalid archive output filename'),
 ('lPT\xb2  .tar.gz', 'invalid archive output filename'),
 ('lpt\xb2...image', 'invalid archive output filename'),
 ('LPT\xb3', 'invalid archive output filename'),
 ('lpt\xb3.txt', 'invalid archive output filename'),
 ('lPT\xb3  .tar.gz', 'invalid archive output filename'),
 ('lpt\xb3...image', 'invalid archive output filename'),
 ('owned', None),
 ('owned image.bin', None),
 ('.CON', None),
 ('.NUL.txt', None),
 ('.hidden', None),
 ('..hidden', None),
 ('CONSOLE', None),
 ('PRNTER', None),
 ('AUXILIARY', None),
 ('NULL', None),
 ('CONIN', None),
 ('CONOUT', None),
 ('CONIN$x', None),
 ('xCON', None),
 ('xNUL.txt', None),
 ('COM0', None),
 ('COM10', None),
 ('COM01', None),
 ('LPT0', None),
 ('LPT10', None),
 ('LPT01', None),
 ('COM\u2074', None),
 ('LPT\u2070', None),
 ('COM\xb9x', None),
 ('LPT\xb2extra.txt', None),
 ('COM\u0661', None),
 ('LPT\uff11', None),
 ('\uff23\uff2f\uff2e', None),
 (' CON', None),
 (' NUL.txt', None),
 ('CON\xa0.txt', None),
 ('NUL\u2000.txt', None),
 ('\xa0', None),
 ('name\x7f.bin', None),
 ('name\x85.bin', None),
 ('owner .data', None),
 ('report..json', None),
 ('owned\xe9', None),
 ('\u6587\u4ef6', None),
 ('\U0001f512', None),
 ('owned\x00leaf', 'invalid archive output filename'),
 ('owned\x01leaf', 'invalid archive output filename'),
 ('owned\x02leaf', 'invalid archive output filename'),
 ('owned\x03leaf', 'invalid archive output filename'),
 ('owned\x04leaf', 'invalid archive output filename'),
 ('owned\x05leaf', 'invalid archive output filename'),
 ('owned\x06leaf', 'invalid archive output filename'),
 ('owned\x07leaf', 'invalid archive output filename'),
 ('owned\x08leaf', 'invalid archive output filename'),
 ('owned\tleaf', 'invalid archive output filename'),
 ('owned\nleaf', 'invalid archive output filename'),
 ('owned\x0bleaf', 'invalid archive output filename'),
 ('owned\x0cleaf', 'invalid archive output filename'),
 ('owned\rleaf', 'invalid archive output filename'),
 ('owned\x0eleaf', 'invalid archive output filename'),
 ('owned\x0fleaf', 'invalid archive output filename'),
 ('owned\x10leaf', 'invalid archive output filename'),
 ('owned\x11leaf', 'invalid archive output filename'),
 ('owned\x12leaf', 'invalid archive output filename'),
 ('owned\x13leaf', 'invalid archive output filename'),
 ('owned\x14leaf', 'invalid archive output filename'),
 ('owned\x15leaf', 'invalid archive output filename'),
 ('owned\x16leaf', 'invalid archive output filename'),
 ('owned\x17leaf', 'invalid archive output filename'),
 ('owned\x18leaf', 'invalid archive output filename'),
 ('owned\x19leaf', 'invalid archive output filename'),
 ('owned\x1aleaf', 'invalid archive output filename'),
 ('owned\x1bleaf', 'invalid archive output filename'),
 ('owned\x1cleaf', 'invalid archive output filename'),
 ('owned\x1dleaf', 'invalid archive output filename'),
 ('owned\x1eleaf', 'invalid archive output filename'),
 ('owned\x1fleaf', 'invalid archive output filename'),
 ('owned<leaf', 'invalid archive output filename'),
 ('owned>leaf', 'invalid archive output filename'),
 ('owned:leaf', 'invalid archive output filename'),
 ('owned"leaf', 'invalid archive output filename'),
 ('owned/leaf', 'invalid archive output filename'),
 ('owned\\leaf', 'invalid archive output filename'),
 ('owned|leaf', 'invalid archive output filename'),
 ('owned?leaf', 'invalid archive output filename'),
 ('owned*leaf', 'invalid archive output filename'),
 ('', 'invalid archive output filename'),
 ('.', 'invalid archive output filename'),
 ('..', 'invalid archive output filename'),
 (' ', 'invalid archive output filename'),
 ('owned.', 'invalid archive output filename'),
 ('owned ', 'invalid archive output filename'),
 ('owned. ', 'invalid archive output filename'),
 ('owned .', 'invalid archive output filename'),
 ('C:owned', 'invalid archive output filename'),
 ('C:\\owned', 'invalid archive output filename'),
 ('\\\\server\\share\\owned', 'invalid archive output filename'),
 ('\\\\?\\C:\\owned', 'invalid archive output filename'),
 ('\\\\.\\NUL', 'invalid archive output filename'),
 ('owned:stream', 'invalid archive output filename'),
 ('con\u0131n$.txt', 'invalid archive output filename'),
 ('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
  None),
 ('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
  'archive output filename exceeds its portable bound'),
 ('\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9a',
  None),
 ('\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9',
  'archive output filename exceeds its portable bound'),
 ('\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c', None),
 ('\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c\u754c',
  'archive output filename exceeds its portable bound'),
 ('\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512abc', None),
 ('\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512\U0001f512',
  'archive output filename exceeds its portable bound'),
 ('owned\ud800', 'archive output filename exceeds its portable bound'),
 ('owned\udfff', 'archive output filename exceeds its portable bound')], ids=['device-00-form-0',
 'device-00-form-1',
 'device-00-form-2',
 'device-00-form-3',
 'device-01-form-0',
 'device-01-form-1',
 'device-01-form-2',
 'device-01-form-3',
 'device-02-form-0',
 'device-02-form-1',
 'device-02-form-2',
 'device-02-form-3',
 'device-03-form-0',
 'device-03-form-1',
 'device-03-form-2',
 'device-03-form-3',
 'device-04-form-0',
 'device-04-form-1',
 'device-04-form-2',
 'device-04-form-3',
 'device-05-form-0',
 'device-05-form-1',
 'device-05-form-2',
 'device-05-form-3',
 'device-06-form-0',
 'device-06-form-1',
 'device-06-form-2',
 'device-06-form-3',
 'device-07-form-0',
 'device-07-form-1',
 'device-07-form-2',
 'device-07-form-3',
 'device-08-form-0',
 'device-08-form-1',
 'device-08-form-2',
 'device-08-form-3',
 'device-09-form-0',
 'device-09-form-1',
 'device-09-form-2',
 'device-09-form-3',
 'device-10-form-0',
 'device-10-form-1',
 'device-10-form-2',
 'device-10-form-3',
 'device-11-form-0',
 'device-11-form-1',
 'device-11-form-2',
 'device-11-form-3',
 'device-12-form-0',
 'device-12-form-1',
 'device-12-form-2',
 'device-12-form-3',
 'device-13-form-0',
 'device-13-form-1',
 'device-13-form-2',
 'device-13-form-3',
 'device-14-form-0',
 'device-14-form-1',
 'device-14-form-2',
 'device-14-form-3',
 'device-15-form-0',
 'device-15-form-1',
 'device-15-form-2',
 'device-15-form-3',
 'device-16-form-0',
 'device-16-form-1',
 'device-16-form-2',
 'device-16-form-3',
 'device-17-form-0',
 'device-17-form-1',
 'device-17-form-2',
 'device-17-form-3',
 'device-18-form-0',
 'device-18-form-1',
 'device-18-form-2',
 'device-18-form-3',
 'device-19-form-0',
 'device-19-form-1',
 'device-19-form-2',
 'device-19-form-3',
 'device-20-form-0',
 'device-20-form-1',
 'device-20-form-2',
 'device-20-form-3',
 'device-21-form-0',
 'device-21-form-1',
 'device-21-form-2',
 'device-21-form-3',
 'device-22-form-0',
 'device-22-form-1',
 'device-22-form-2',
 'device-22-form-3',
 'device-23-form-0',
 'device-23-form-1',
 'device-23-form-2',
 'device-23-form-3',
 'device-24-form-0',
 'device-24-form-1',
 'device-24-form-2',
 'device-24-form-3',
 'device-25-form-0',
 'device-25-form-1',
 'device-25-form-2',
 'device-25-form-3',
 'device-26-form-0',
 'device-26-form-1',
 'device-26-form-2',
 'device-26-form-3',
 'device-27-form-0',
 'device-27-form-1',
 'device-27-form-2',
 'device-27-form-3',
 'device-28-form-0',
 'device-28-form-1',
 'device-28-form-2',
 'device-28-form-3',
 'device-29-form-0',
 'device-29-form-1',
 'device-29-form-2',
 'device-29-form-3',
 'allowed-00',
 'allowed-01',
 'allowed-02',
 'allowed-03',
 'allowed-04',
 'allowed-05',
 'allowed-06',
 'allowed-07',
 'allowed-08',
 'allowed-09',
 'allowed-10',
 'allowed-11',
 'allowed-12',
 'allowed-13',
 'allowed-14',
 'allowed-15',
 'allowed-16',
 'allowed-17',
 'allowed-18',
 'allowed-19',
 'allowed-20',
 'allowed-21',
 'allowed-22',
 'allowed-23',
 'allowed-24',
 'allowed-25',
 'allowed-26',
 'allowed-27',
 'allowed-28',
 'allowed-29',
 'allowed-30',
 'allowed-31',
 'allowed-32',
 'allowed-33',
 'allowed-34',
 'allowed-35',
 'allowed-36',
 'allowed-37',
 'allowed-38',
 'allowed-39',
 'ascii-control-00',
 'ascii-control-01',
 'ascii-control-02',
 'ascii-control-03',
 'ascii-control-04',
 'ascii-control-05',
 'ascii-control-06',
 'ascii-control-07',
 'ascii-control-08',
 'ascii-control-09',
 'ascii-control-10',
 'ascii-control-11',
 'ascii-control-12',
 'ascii-control-13',
 'ascii-control-14',
 'ascii-control-15',
 'ascii-control-16',
 'ascii-control-17',
 'ascii-control-18',
 'ascii-control-19',
 'ascii-control-20',
 'ascii-control-21',
 'ascii-control-22',
 'ascii-control-23',
 'ascii-control-24',
 'ascii-control-25',
 'ascii-control-26',
 'ascii-control-27',
 'ascii-control-28',
 'ascii-control-29',
 'ascii-control-30',
 'ascii-control-31',
 'forbidden-character-0',
 'forbidden-character-1',
 'forbidden-character-2',
 'forbidden-character-3',
 'forbidden-character-4',
 'forbidden-character-5',
 'forbidden-character-6',
 'forbidden-character-7',
 'forbidden-character-8',
 'invalid-lexical-00',
 'invalid-lexical-01',
 'invalid-lexical-02',
 'invalid-lexical-03',
 'invalid-lexical-04',
 'invalid-lexical-05',
 'invalid-lexical-06',
 'invalid-lexical-07',
 'invalid-lexical-08',
 'invalid-lexical-09',
 'invalid-lexical-10',
 'invalid-lexical-11',
 'invalid-lexical-12',
 'invalid-lexical-13',
 'invalid-lexical-14',
 'encoding-bound-00',
 'encoding-bound-01',
 'encoding-bound-02',
 'encoding-bound-03',
 'encoding-bound-04',
 'encoding-bound-05',
 'encoding-bound-06',
 'encoding-bound-07',
 'encoding-bound-08',
 'encoding-bound-09'])
def test_portable_leaf_preserves_windows_device_and_encoding_policy(name, reason):
    if reason is None:
        assert aio._filename(name) is None
    else:
        with pytest.raises(aio.ArchiveError, match='^' + reason + '$'):
            aio._filename(name)


@pytest.mark.usefixtures('_no_platform_reserved_name_lookup')
@pytest.mark.parametrize('value', [None, True, 1, b'owned', ['owned']],
                         ids=['none', 'bool', 'integer', 'bytes', 'list'])
def test_portable_leaf_requires_exact_builtin_string(value):
    with pytest.raises(aio.ArchiveError, match='^invalid archive output filename$'):
        aio._filename(value)


@pytest.mark.usefixtures('_no_platform_reserved_name_lookup')
def test_portable_leaf_rejects_string_subclass_before_dispatching_hooks():
    calls = []
    class HostileString(str):
        def __getattribute__(self, name):
            calls.append(name)
            raise AssertionError('string subclass callbacks must not execute')
    with pytest.raises(aio.ArchiveError, match='^invalid archive output filename$'):
        aio._filename(HostileString('owned'))
    assert calls == []


@pytest.mark.usefixtures('_no_platform_reserved_name_lookup')
@pytest.mark.parametrize('leaf', ['NUL', 'LPT\u00b3.ini', 'conin$ .txt'],
                         ids=['nul', 'lpt-superscript', 'console-extension'])
@pytest.mark.parametrize('kind', ['file', 'directory'])
def test_reserved_portable_leaf_refuses_through_both_selectors(tmp_path, leaf, kind):
    choose = aio.selected_path if kind == 'file' else aio.selected_directory
    with pytest.raises(aio.ArchiveError, match='^invalid archive output filename$'):
        choose(tmp_path / leaf)

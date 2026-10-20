"""Private spelling scan: deterministic fault controls and owned Windows files.

Synthetic owners below contain no native handles. They qualify the private
control flow, not filesystem identity. Separate native cases retain real owned
directories. No network, child process, provider or timing assertion is used.
"""
import ctypes
import os
import struct
import sys
from types import SimpleNamespace

import pytest

from olympus import native_cleanup, prefs_storage as storage


class Stream:
    def __init__(self, names=(), *, body=None, close_error=None, on_next=None):
        self.values = iter(names)
        self.body, self.close_error, self.on_next = body, close_error, on_next
        self.next_calls = self.close_calls = 0

    def __iter__(self):
        return self

    def __next__(self):
        self.next_calls += 1
        if self.on_next is not None:
            callback, self.on_next = self.on_next, None
            callback()
        if self.body is not None:
            raise self.body
        return SimpleNamespace(name=next(self.values))

    def close(self):
        self.close_calls += 1  # Count attempted closes, including failures.
        if self.close_error is not None:
            raise self.close_error


def fake_owner(tmp_path, stream):
    owner = object.__new__(storage._Directory)
    owner._initialize(tmp_path)
    state = {'checks': 0, 'entries': 0, 'failure': None}
    def check():
        state['checks'] += 1
        if state['failure'] is not None:
            raise state['failure']
    def entries():
        state['entries'] += 1
        return stream
    owner.win = SimpleNamespace(handles=[object()], owned_start=0,
                                check=check, entries=entries)
    return owner, state


@pytest.mark.parametrize('names', [(), ('key',), ('other', 'key', 'last')],
                         ids=['absent', 'exact', 'complete-scan'])
def test_private_scan_has_initial_final_checks_and_no_public_yields(tmp_path, monkeypatch, names):
    stream = Stream(names)
    owner, state = fake_owner(tmp_path, stream)
    monkeypatch.setattr(owner, 'entries', lambda: pytest.fail('public consumer path used'))
    owner._case_spelling('key')
    assert state == {'checks': 2, 'entries': 1, 'failure': None}
    assert stream.next_calls == len(names) + 1 and stream.close_calls == 1
    assert owner._iterators == [] and not owner._cleanup_unconfirmed


@pytest.mark.parametrize('names', [('KEY',), ('key', 'other', 'KEY')],
                         ids=['alias-first', 'alias-after-exact'])
def test_case_alias_is_refused_without_early_exact_match_return(tmp_path, names):
    stream = Stream(names)
    owner, _ = fake_owner(tmp_path, stream)
    with pytest.raises(storage.StorageError, match='spelling differs'):
        owner._case_spelling('key')
    assert stream.next_calls == len(names) and stream.close_calls == 1
    assert owner._iterators == [] and not owner._cleanup_unconfirmed


@pytest.mark.parametrize('count', [3, 4])
def test_private_scan_preserves_exact_entry_budget(tmp_path, monkeypatch, count):
    monkeypatch.setattr(storage, 'MAX_KEYS', 3)
    stream = Stream(tuple('item' + str(n) for n in range(count)))
    owner, _ = fake_owner(tmp_path, stream)
    if count == 3:
        owner._case_spelling('absent')
    else:
        with pytest.raises(storage.StorageError, match='spelling-check bound'):
            owner._case_spelling('absent')
    assert stream.next_calls == 4 and stream.close_calls == 1
    assert owner._iterators == []


def test_sixteen_live_iterators_refuse_before_native_stream_creation(tmp_path):
    stream = Stream()
    owner, state = fake_owner(tmp_path, stream)
    retained = [object() for _ in range(16)]
    owner._iterators[:] = retained
    with pytest.raises(storage.StorageError, match='too many live'):
        owner._case_spelling('key')
    assert owner._iterators == retained and state['entries'] == 0
    assert stream.next_calls == stream.close_calls == 0


@pytest.mark.parametrize('kind', [OSError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('close_fails', [False, True])
def test_primary_is_preserved_and_iterator_close_is_attempted_once(tmp_path, kind, close_fails):
    primary = kind('owned scan body')
    stream = Stream(body=primary, close_error=OSError('owned close') if close_fails else None)
    owner, state = fake_owner(tmp_path, stream)
    with pytest.raises(kind) as caught:
        owner._case_spelling('key')
    assert caught.value is primary and stream.close_calls == 1 and owner._iterators == []
    assert native_cleanup.unknown(primary) is close_fails
    assert owner._cleanup_unconfirmed is close_fails
    before = state['checks']
    if close_fails:
        with pytest.raises(storage.StorageError) as blocked:
            owner.check()
        assert native_cleanup.unknown(blocked.value) and state['checks'] == before
    else:
        owner.check()


@pytest.mark.parametrize('kind', [OSError, KeyboardInterrupt, SystemExit])
def test_incoming_cleanup_uncertainty_remains_on_owner_after_successful_close(tmp_path, kind):
    primary = kind('owned flagged acquisition')
    native_cleanup.mark(primary)
    stream = Stream(body=primary)
    owner, state = fake_owner(tmp_path, stream)
    with pytest.raises(kind) as caught:
        owner._case_spelling('key')
    assert caught.value is primary and native_cleanup.unknown(primary)
    assert stream.close_calls == 1 and owner._iterators == [] and owner._cleanup_unconfirmed
    before = state['checks']
    with pytest.raises(storage.StorageError) as blocked:
        owner.check()
    assert native_cleanup.unknown(blocked.value) and state['checks'] == before


@pytest.mark.parametrize('kind', [OSError, KeyboardInterrupt, SystemExit])
def test_cleanup_only_error_is_exact_and_not_retried(tmp_path, kind):
    failure = kind('owned cleanup-only')
    stream = Stream(('key',), close_error=failure)
    owner, _ = fake_owner(tmp_path, stream)
    with pytest.raises(kind) as caught:
        owner._case_spelling('key')
    assert caught.value is failure and native_cleanup.unknown(failure)
    assert owner._cleanup_unconfirmed and owner._iterators == [] and stream.close_calls == 1


@pytest.mark.parametrize('close_fails', [False, True])
def test_owner_close_detaches_iterator_and_scan_never_attempts_second_close(tmp_path, monkeypatch, close_fails):
    failure = OSError('owned reentrant close')
    stream = Stream(close_error=failure if close_fails else None)
    owner, _ = fake_owner(tmp_path, stream)
    held = list(owner.win.handles)
    native_closes = []
    # Synthetic owner only: the sentinel objects are never passed to an OS API.
    monkeypatch.setitem(sys.modules, 'olympus.prefs_windows',
                        SimpleNamespace(close=native_closes.append))
    stream.on_next = owner.close
    with pytest.raises(OSError if close_fails else storage.StorageError) as caught:
        owner._case_spelling('key')
    if close_fails:
        assert caught.value is failure and native_cleanup.unknown(failure)
        assert owner._cleanup_unconfirmed
    else:
        assert 'closed' in str(caught.value)
    assert stream.close_calls == 1 and owner._iterators == []
    assert native_closes == held and owner._closed and owner.win is None


@pytest.mark.parametrize('mode', ['closed', 'cleanup', 'process', 'thread'])
def test_invalid_owner_refuses_before_native_enumeration(tmp_path, mode):
    stream = Stream()
    owner, state = fake_owner(tmp_path, stream)
    if mode == 'closed':
        owner._closed = True
    elif mode == 'cleanup':
        owner._cleanup_unconfirmed = True
    elif mode == 'process':
        owner._pid += 1
    else:
        owner._thread += 1
    with pytest.raises(storage.StorageError):
        owner._case_spelling('key')
    assert state['entries'] == state['checks'] == 0
    assert stream.next_calls == stream.close_calls == 0


def test_final_owner_refusal_precedes_leaf_open(tmp_path, monkeypatch):
    stream = Stream(('key',))
    owner, state = fake_owner(tmp_path, stream)
    original_next = stream.__class__.__next__
    failure = OSError('owned ancestor metadata refusal')
    def next_then_change(selected):
        try:
            return original_next(selected)
        except StopIteration:
            if selected is stream:
                state['failure'] = failure
            raise
    monkeypatch.setattr(Stream, '__next__', next_then_change)
    owner.win.open_fd = lambda *args: pytest.fail('leaf opened after failed final check')
    with pytest.raises(OSError) as caught:
        owner._open('key', os.O_RDONLY)
    assert caught.value is failure and stream.close_calls == 1 and owner._iterators == []


def test_public_entries_rechecks_after_external_consumer_control(tmp_path):
    stream = Stream(('first', 'second'))
    owner, state = fake_owner(tmp_path, stream)
    iterator = owner.entries()
    assert next(iterator).name == 'first'
    failure = OSError('owned public consumer changed authority')
    state['failure'] = failure
    with pytest.raises(OSError) as caught:
        next(iterator)
    assert caught.value is failure and stream.next_calls == 1
    assert stream.close_calls == 1 and owner._iterators == []


def _batch(names):
    chunks = []
    for index, name in enumerate(names):
        raw = name.encode('utf-16-le')
        size = (68 + len(raw) + 7) // 8 * 8
        record = bytearray(size)
        struct.pack_into('<I', record, 0, size if index + 1 < len(names) else 0)
        struct.pack_into('<II', record, 56, 0, len(raw))
        record[68:68 + len(raw)] = raw
        chunks.append(bytes(record))
    return b''.join(chunks)


@pytest.mark.skipif(os.name != 'nt', reason='real Windows native parser and ctypes error channel')
@pytest.mark.parametrize('mode', ['two-batches', 'between-batch-refusal', 'bad-length', 'bad-offset'])
def test_private_scan_retains_native_batch_freshness_and_record_validation(tmp_path, monkeypatch, mode):
    from olympus import gallery_windows as win
    owner, state = fake_owner(tmp_path, None)
    observed, batches = [], [_batch(('first', 'key')), _batch(('last',))]
    failure = OSError('owned next-batch ancestor refusal')
    def query(handle, information_class, buffer, size):
        observed.append(information_class)
        assert size == 65536
        if len(observed) > len(batches):
            ctypes.set_last_error(18)
            return 0
        raw = bytearray(batches[len(observed) - 1])
        if mode == 'bad-length':
            struct.pack_into('<I', raw, 60, 3)
        elif mode == 'bad-offset':
            struct.pack_into('<I', raw, 0, 69)
        ctypes.memmove(buffer, bytes(raw), len(raw))
        if mode == 'between-batch-refusal':
            state['failure'] = failure
        return 1
    owner.win.handle = owner.win.handles[-1]
    owner.win.entries = lambda: win.NativeDirectory.entries(owner.win)
    monkeypatch.setattr(win, '_apis', lambda: (SimpleNamespace(GetFileInformationByHandleEx=query), None))
    if mode == 'two-batches':
        owner._case_spelling('key')
        assert observed == [15, 14, 14]
        # Two outer owner checks, native initial check, one check before each batch.
        assert state['checks'] == 6
    else:
        with pytest.raises(OSError) as caught:
            owner._case_spelling('key')
        assert observed == [15]
        if mode == 'between-batch-refusal':
            assert caught.value is failure
        else:
            assert 'invalid native directory' in str(caught.value)
    assert owner._iterators == [] and not owner._cleanup_unconfirmed


@pytest.mark.skipif(os.name != 'nt', reason='owned native Windows spelling behavior')
@pytest.mark.parametrize('requested', ['owned-key', 'absent', 'OWNED-KEY'])
def test_actual_owned_windows_spelling_scan_is_observational(tmp_path, requested):
    root = tmp_path / 'owned-spelling'
    root.mkdir()
    path = root / 'owned-key'
    path.write_bytes(b'owned preserved image')
    before = (path.read_bytes(), path.stat().st_ino)
    owner = storage._Directory(root, create=False)
    primary = None
    try:
        if requested == 'OWNED-KEY':
            with pytest.raises(storage.StorageError, match='spelling differs'):
                owner._case_spelling(requested)
        else:
            owner._case_spelling(requested)
        assert owner._iterators == []
    except BaseException as error:
        primary = error
        raise
    finally:
        native_cleanup.drain([owner.close], primary)
    assert owner._closed and owner.win is None
    assert (path.read_bytes(), path.stat().st_ino) == before
    assert sorted(p.name for p in root.iterdir()) == ['owned-key']


@pytest.fixture
def opened_scan_case(tmp_path, monkeypatch):
    """Synthetic Windows chain plus actual owned file descriptors on both OSes."""
    root = tmp_path / 'owned-open-boundary'
    root.mkdir()
    path = root / 'owned-key'
    path.write_bytes(b'owned original bytes')
    owner, state = fake_owner(root, None)
    retained, opened, scans, closed = set(), [], [], []
    actual_open, actual_close = os.open, os.close
    state['names'] = ('owned-key',)
    def entries():
        stream = Stream(state['names'])
        scans.append(stream)
        return stream
    def open_fd(name, flags):
        assert name == 'owned-key'
        fd = actual_open(path, flags | getattr(os, 'O_BINARY', 0))
        retained.add(fd)
        opened.append(fd)
        callback = state.get('after_open')
        if callback is not None:
            callback()
        return fd
    def close(fd):
        if fd in retained:
            retained.remove(fd)  # Relinquish before the one close attempt.
            closed.append(fd)
        return actual_close(fd)
    owner.win.entries = entries
    owner.win.open_fd = open_fd
    monkeypatch.setattr(storage.os, 'close', close)
    case = SimpleNamespace(owner=owner, state=state, path=path, scans=scans,
        retained=retained, opened=opened, closed=closed,
        actual_open=actual_open, actual_close=actual_close, close=close)
    yield case
    # Test-only cleanup cannot turn an unclosed production-owned fd into a pass.
    leftovers = list(retained)
    retained.clear()
    native_cleanup.drain([lambda fd=fd: actual_close(fd) for fd in leftovers])
    assert leftovers == [], 'owned descriptor was left for fixture cleanup'


def test_open_preserves_both_spelling_barriers_without_adjacent_rechecks(opened_scan_case):
    case = opened_scan_case
    fd = case.owner._open('owned-key', os.O_RDONLY)
    try:
        assert os.read(fd, 64) == b'owned original bytes'
        assert case.state['checks'] == 4  # Start/end of each private scan.
        assert len(case.scans) == 2
        assert all(stream.next_calls == 2 and stream.close_calls == 1 for stream in case.scans)
        assert case.owner._iterators == []
    finally:
        case.close(fd)
    assert case.opened == case.closed and len(case.opened) == 1
    assert case.path.read_bytes() == b'owned original bytes'


@pytest.mark.parametrize('kind', [OSError, KeyboardInterrupt, SystemExit])
def test_initial_spelling_check_refuses_before_file_acquisition(opened_scan_case, kind):
    case = opened_scan_case
    primary = kind('owned pre-open authority failure')
    case.state['failure'] = primary
    with pytest.raises(kind) as caught:
        case.owner._open('owned-key', os.O_RDONLY)
    assert caught.value is primary
    assert case.opened == case.closed == case.scans == []
    assert case.path.read_bytes() == b'owned original bytes'


@pytest.mark.parametrize('kind', [OSError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('secondary_kind', [None, OSError, KeyboardInterrupt, SystemExit],
                         ids=['close-ok', 'close-oserror', 'close-interrupt', 'close-exit'])
def test_post_metadata_barrier_preserves_primary_and_single_close(opened_scan_case, monkeypatch, kind, secondary_kind):
    case = opened_scan_case
    primary = kind('owned post-metadata authority failure')
    secondary = None if secondary_kind is None else secondary_kind('owned lost close acknowledgment')
    actual_information = storage._file_information
    observed = []
    def information(fd):
        info = actual_information(fd)
        if fd in case.retained:
            observed.append((fd, info.st_dev, info.st_ino))
            case.state['failure'] = primary
        return info
    def close_then_fail(fd):
        target = fd in case.retained
        case.close(fd)
        if target and secondary is not None:
            raise secondary
    with monkeypatch.context() as patch:
        patch.setattr(storage, '_file_information', information)
        patch.setattr(storage.os, 'close', close_then_fail)
        with pytest.raises(kind) as caught:
            case.owner._open('owned-key', os.O_RDONLY)
    assert caught.value is primary
    assert native_cleanup.unknown(primary) is (secondary is not None)
    assert len(observed) == 1 and [entry[0] for entry in observed] == case.opened == case.closed
    assert len(case.scans) == 1 and case.scans[0].close_calls == 1
    assert case.owner._iterators == [] and not case.retained
    with pytest.raises(OSError):
        os.fstat(case.opened[0])
    assert case.path.read_bytes() == b'owned original bytes'


def test_post_open_case_alias_refuses_and_closes_observed_file(opened_scan_case):
    case = opened_scan_case
    case.state['after_open'] = lambda: case.state.update(names=('OWNED-KEY',))
    with pytest.raises(storage.StorageError, match='spelling differs'):
        case.owner._open('owned-key', os.O_RDONLY)
    assert len(case.scans) == 2 and all(stream.close_calls == 1 for stream in case.scans)
    assert case.owner._iterators == [] and case.opened == case.closed and len(case.opened) == 1
    assert case.path.read_bytes() == b'owned original bytes'


def test_failed_postcheck_close_cannot_close_immediately_reused_descriptor(opened_scan_case, monkeypatch):
    case = opened_scan_case
    primary = KeyboardInterrupt('owned post-open interruption')
    sentinel_path = case.path.with_name('sentinel')
    sentinel_path.write_bytes(b'owned sentinel stays live')
    sentinel = []
    case.state['after_open'] = lambda: case.state.update(failure=primary)
    def close_reuse(fd):
        target = fd in case.retained
        case.close(fd)
        if target:
            replacement = case.actual_open(sentinel_path, os.O_RDONLY | getattr(os, 'O_BINARY', 0))
            sentinel.append(replacement)
            raise OSError('owned lost close acknowledgment after fd reuse')
    try:
        with monkeypatch.context() as patch:
            patch.setattr(storage.os, 'close', close_reuse)
            with pytest.raises(KeyboardInterrupt) as caught:
                case.owner._open('owned-key', os.O_RDONLY)
        assert caught.value is primary and native_cleanup.unknown(primary)
        assert case.opened == case.closed == sentinel and len(sentinel) == 1
        assert os.read(sentinel[0], 64) == b'owned sentinel stays live'
    finally:
        owned_sentinels, sentinel = sentinel, []
        native_cleanup.drain([lambda fd=fd: case.actual_close(fd) for fd in owned_sentinels])


def test_actual_open_retains_readonly_bytes_identity_and_directory_lifetime(tmp_path):
    root = tmp_path / 'real-open-boundary'
    root.mkdir()
    path = root / 'owned-key'
    path.write_bytes(b'actual owned contents')
    before = path.stat()
    owner, fd, primary = storage._Directory(root, create=False), None, None
    try:
        fd = owner._open('owned-key', os.O_RDONLY)
        assert os.read(fd, 64) == b'actual owned contents'
        held = os.fstat(fd)
        assert (held.st_dev, held.st_ino) == (before.st_dev, before.st_ino)
        owner.check()
    except BaseException as error:
        primary = error
        raise
    finally:
        owned_fd, fd = fd, None
        actions = [] if owned_fd is None else [lambda: os.close(owned_fd)]
        native_cleanup.drain(actions + [owner.close], primary)
    assert owner._closed and owner.win is None and owner.fd is None
    assert path.read_bytes() == b'actual owned contents'
    assert (path.stat().st_dev, path.stat().st_ino) == (before.st_dev, before.st_ino)

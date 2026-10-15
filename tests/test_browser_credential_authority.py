"""Captured credential lease controls over owned local fixtures only."""
import json
import os
from pathlib import Path
import threading
import traceback

import pytest

from olympus import browser_credential_authority as authority
from olympus import browserlease, config


@pytest.fixture
def roots(tmp_path, monkeypatch):
    original = tmp_path / 'original'
    other = tmp_path / 'other'
    original.mkdir()
    other.mkdir()
    monkeypatch.setattr(config, 'MEMORY_DIR', original)
    assert not browserlease._holding_lock()
    yield original, other
    assert not browserlease._holding_lock()


def _record(owner='cli', lease_id='a' * 32):
    return {'schema_version': 1, 'owner': owner, 'lease_id': lease_id,
            'transport': 'mock', 'claimed_at': 123.0, 'fingerprint': ''}


def _write(root, record):
    (root / 'browser_lease.json').write_text(json.dumps(record), encoding='utf-8')


def test_claim_binds_original_root_and_generation_without_diagnostic_backfill(roots):
    original, _other = roots
    with authority.capture() as captured:
        record = captured.claim('cli', 'mock')
        bound = captured.bind('cli', record)
        assert captured.verify(bound, 'cli') == record
        assert captured.claim('cli', 'another-mock') == record
        record['owner'] = 'u-foreign'
        assert captured.current()['owner'] == 'cli'
    assert (original / 'locks/browser-lease.lock').is_file()
    with authority.capture(binding=bound) as later:
        assert later.verify(bound, 'cli')['lease_id'] == bound.lease_id


@pytest.mark.parametrize('operation', ['current', 'claim', 'verify'])
def test_observable_root_drift_refuses_without_access_to_other_root(roots, monkeypatch, operation):
    original, other = roots
    _write(other, _record('u-foreign'))
    before = (other / 'browser_lease.json').read_bytes()
    with authority.capture() as captured:
        bound = captured.bind('cli', captured.claim('cli', 'mock'))
        monkeypatch.setattr(config, 'MEMORY_DIR', other)
        with pytest.raises(browserlease.OwnershipRefused):
            if operation == 'current':
                captured.current()
            elif operation == 'claim':
                captured.claim('cli', 'mock')
            else:
                captured.verify(bound, 'cli')
        assert (other / 'browser_lease.json').read_bytes() == before
        assert not (other / 'locks').exists()
        monkeypatch.setattr(config, 'MEMORY_DIR', original)
        assert captured.verify(bound, 'cli')['owner'] == 'cli'


def test_temporary_ambient_switch_cannot_redirect_native_lease_read(roots, monkeypatch):
    original, other = roots
    _write(other, _record('u-foreign'))
    with authority.capture() as captured:
        bound = captured.bind('cli', captured.claim('cli', 'mock'))
        native_read = captured.directory.read_with_identity
        observed = []
        def switched(name, cap):
            monkeypatch.setattr(config, 'MEMORY_DIR', other)
            try:
                value = native_read(name, cap)
                observed.append(json.loads(value[0])['owner'])
                return value
            finally:
                monkeypatch.setattr(config, 'MEMORY_DIR', original)
        monkeypatch.setattr(captured.directory, 'read_with_identity', switched)
        assert captured.verify(bound, 'cli')['owner'] == 'cli'
        assert observed == ['cli']
    assert not (other / 'locks').exists()


@pytest.mark.parametrize('replacement', ['absent', 'same-owner-generation', 'other-owner'])
def test_missing_or_replaced_generation_never_authorizes_old_binding(roots, replacement):
    original, _other = roots
    with authority.capture() as captured:
        bound = captured.bind('cli', captured.claim('cli', 'mock'))
        if replacement == 'absent':
            (original / 'browser_lease.json').unlink()
        else:
            _write(original, _record('cli' if replacement == 'same-owner-generation' else 'u-foreign', 'b' * 32))
        with pytest.raises(browserlease.OwnershipRefused):
            captured.verify(bound, 'cli')


def test_cross_root_binding_refuses_even_for_identical_copied_record(roots, monkeypatch):
    original, other = roots
    with authority.capture() as first:
        record = first.claim('cli', 'mock')
        bound = first.bind('cli', record)
    _write(other, record)
    monkeypatch.setattr(config, 'MEMORY_DIR', other)
    with pytest.raises(browserlease.OwnershipRefused):
        with authority.capture(binding=bound):
            pytest.fail('foreign-root binding must refuse at acquisition')
    assert not (other / 'locks').exists()


def test_captured_and_legacy_locks_nest_only_at_original_root(roots, monkeypatch):
    original, other = roots
    with authority.capture() as captured:
        with captured.lock():
            with browserlease._lock():
                assert browserlease._path() == original / 'browser_lease.json'
            monkeypatch.setattr(config, 'MEMORY_DIR', other)
            with pytest.raises(browserlease.OwnershipRefused):
                with browserlease._lock():
                    pytest.fail('other-root nesting must refuse')
            with pytest.raises(browserlease.OwnershipRefused):
                browserlease._read()
            monkeypatch.setattr(config, 'MEMORY_DIR', original)
        assert not (other / 'locks').exists()


def test_legacy_reentrancy_refuses_other_root(roots, monkeypatch):
    original, other = roots
    with browserlease._lock():
        monkeypatch.setattr(config, 'MEMORY_DIR', other)
        with pytest.raises(browserlease.OwnershipRefused):
            browserlease.claim('cli', 'mock')
        assert not (other / 'locks').exists()
        monkeypatch.setattr(config, 'MEMORY_DIR', original)
        assert browserlease.claim('cli', 'mock')['owner'] == 'cli'


def test_capture_cannot_adopt_an_unbound_legacy_lock(roots):
    with authority.capture() as captured:
        with browserlease._lock():
            with pytest.raises(browserlease.OwnershipRefused):
                captured.current()


def test_second_capture_cannot_adopt_first_capture_lock(roots):
    with authority.capture() as first, authority.capture() as second:
        with first.lock():
            with pytest.raises(browserlease.OwnershipRefused):
                second.current()
            with first.lock():
                assert first.current() is None


def test_lock_file_replacement_is_not_silently_reacquired(roots):
    original, _other = roots
    with authority.capture() as captured:
        bound = captured.bind('cli', captured.claim('cli', 'mock'))
        lock = original / 'locks/browser-lease.lock'
        lock.rename(lock.with_name('retained-original.lock'))
        lock.write_bytes(b'')
        with pytest.raises(browserlease.OwnershipRefused):
            captured.verify(bound, 'cli')


def test_native_root_replacement_refuses_or_is_blocked_by_held_handles(roots):
    original, _other = roots
    with authority.capture() as captured:
        record = captured.claim('cli', 'mock')
        bound = captured.bind('cli', record)
        retained = original.with_name('retained-original')
        try:
            original.rename(retained)
        except PermissionError as error:
            assert os.name == 'nt' and getattr(error, 'winerror', None) in (5, 32, 33)
            assert captured.verify(bound, 'cli') == record
        else:
            original.mkdir()
            _write(original, record)
            with pytest.raises(browserlease.OwnershipRefused):
                captured.verify(bound, 'cli')
            assert not (original / 'locks').exists()


def test_hardlinked_lease_is_refused_before_decode(roots):
    original, other = roots
    _write(original, _record())
    os.link(original / 'browser_lease.json', other / 'retained-lease.json')
    with authority.capture() as captured:
        with pytest.raises(browserlease.OwnershipRefused):
            captured.current()


@pytest.mark.parametrize('raw', [b'', b'{broken-owned-private', b'[]', b'x' * 65537],
                         ids=['empty', 'malformed-json', 'wrong-shape', 'over-bound'])
def test_unreadable_records_refuse_with_fixed_unchained_error(roots, raw):
    original, _other = roots
    (original / 'browser_lease.json').write_bytes(raw)
    with authority.capture() as captured:
        with pytest.raises(browserlease.OwnershipRefused) as raised:
            captured.current()
    assert str(raised.value) == browserlease.REFUSAL
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert 'broken-owned-private' not in ''.join(traceback.format_exception(raised.value))


@pytest.mark.parametrize('kind', [KeyboardInterrupt, SystemExit])
def test_system_interrupt_identity_is_preserved_and_locks_are_released(roots, monkeypatch, kind):
    marker = kind('owned-interruption')
    with authority.capture() as captured:
        def interrupted(*args, **kwargs):
            raise marker
        monkeypatch.setattr(captured.directory, 'read_with_identity', interrupted)
        with pytest.raises(kind) as raised:
            captured.current()
        assert raised.value is marker
        assert not browserlease._holding_lock()
        assert browserlease._THREAD_LOCK.acquire(blocking=False)
        browserlease._THREAD_LOCK.release()


def test_authority_refuses_cross_thread_use_and_closed_reuse(roots):
    captured = authority.Authority()
    outcomes = []
    def foreign_thread():
        try:
            captured.current()
        except browserlease.OwnershipRefused:
            outcomes.append('refused')
    thread = threading.Thread(target=foreign_thread)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive() and outcomes == ['refused']
    captured.close()
    captured.close()
    with pytest.raises(browserlease.OwnershipRefused):
        captured.current()


def test_close_while_locked_refuses_without_invalidating_active_authority(roots):
    with authority.capture() as captured:
        with captured.lock():
            with pytest.raises(browserlease.OwnershipRefused):
                captured.close()
            assert captured.current() is None


def test_foreign_owner_cannot_claim_or_bind_existing_lease(roots):
    with authority.capture() as captured:
        record = captured.claim('cli', 'mock')
        with pytest.raises(browserlease.OwnershipRefused):
            captured.claim('u-foreign', 'mock')
        with pytest.raises(browserlease.OwnershipRefused):
            captured.bind('u-foreign', record)
        assert captured.current() == record


@pytest.mark.parametrize('replacement', ['missing-file', 'replaced-file', 'missing-directory', 'replaced-directory'])
def test_bound_recapture_refuses_changed_coordination_before_creation(roots, replacement):
    original, _other = roots
    with authority.capture() as captured:
        bound = captured.bind('cli', captured.claim('cli', 'mock'))
    locks = original / 'locks'
    if replacement.endswith('file'):
        lock = locks / 'browser-lease.lock'
        lock.rename(locks / 'retained-original.lock')
        if replacement == 'replaced-file':
            lock.write_bytes(b'')
    else:
        locks.rename(original / 'retained-locks')
        if replacement == 'replaced-directory':
            locks.mkdir()
            (locks / 'browser-lease.lock').write_bytes(b'')
    before = sorted(str(p.relative_to(original)) for p in original.rglob('*'))
    with pytest.raises(browserlease.OwnershipRefused):
        with authority.capture(binding=bound):
            pytest.fail('changed coordination must refuse at acquisition')
    assert sorted(str(p.relative_to(original)) for p in original.rglob('*')) == before


def test_existing_capture_does_not_recreate_deleted_coordination_file(roots):
    original, _other = roots
    with authority.capture() as captured:
        bound = captured.bind('cli', captured.claim('cli', 'mock'))
        lock = original / 'locks/browser-lease.lock'
        lock.unlink()
        with pytest.raises(browserlease.OwnershipRefused):
            captured.verify(bound, 'cli')
        assert not lock.exists()


def test_native_lock_contention_uses_the_same_legacy_coordination_file(roots):
    original, _other = roots
    with authority.capture() as captured:
        lock_path = browserlease._lock_path()
        assert lock_path == original / 'locks/browser-lease.lock'
        with lock_path.open('r+b', buffering=0) as other:
            browserlease._acquire_file_lock(other, authority.time.monotonic() + 1)
            try:
                with pytest.raises((browserlease.OwnershipRefused, TimeoutError)):
                    with captured.lock(timeout=0.025):
                        pytest.fail('independent descriptor already owns the native lock')
            finally:
                browserlease._release_file_lock(other)
        assert not browserlease._holding_lock()
        assert captured.current() is None


@pytest.mark.parametrize('barrier', ['file', 'directory'])
def test_visible_claim_after_lost_barrier_requires_reconfirmation_without_rewrite(roots, monkeypatch, barrier):
    original, _other = roots
    with authority.capture() as captured:
        native_sync = captured.directory.sync
        native_fsync = os.fsync
        failed = []
        def missing_barrier(*args):
            failed.append(True)
            raise OSError('owned-private-barrier-fault')
        with monkeypatch.context() as patch:
            if barrier == 'file':
                patch.setattr(os, 'fsync', missing_barrier)
            else:
                patch.setattr(captured.directory, 'sync', missing_barrier)
            with pytest.raises(browserlease.OwnershipRefused):
                captured.claim('cli', 'mock')
        assert failed
        path = original / 'browser_lease.json'
        visible = path.read_bytes()
        generation = json.loads(visible)['lease_id']
        calls = []
        def confirmed_file(fd):
            calls.append('file')
            return native_fsync(fd)
        def confirmed_directory():
            calls.append('directory')
            return native_sync()
        def no_rewrite(*args):
            pytest.fail('visible generation must only reconfirm barriers')
        monkeypatch.setattr(os, 'fsync', confirmed_file)
        monkeypatch.setattr(captured.directory, 'sync', confirmed_directory)
        monkeypatch.setattr(captured.directory, 'write_new', no_rewrite)
        record = captured.claim('cli', 'mock')
        assert record['lease_id'] == generation and path.read_bytes() == visible
        assert calls[0] == 'file' and 'directory' in calls
        assert captured.bind('cli', record).lease_id == generation


@pytest.mark.parametrize('kind', [KeyboardInterrupt, SystemExit, RuntimeError])
def test_primary_body_error_survives_secondary_lock_close_fault(roots, monkeypatch, kind):
    primary = kind('owned-private-primary')
    cleanup = OSError('owned-private-cleanup')
    captured = authority.Authority()
    try:
        native_open = captured._open_lock
        closes = []
        class CloseFault:
            def __init__(self, real):
                self.real = real
            def fileno(self):
                return self.real.fileno()
            def seek(self, *args):
                return self.real.seek(*args)
            def close(self):
                self.real.close()
                closes.append('closed')
                raise cleanup
        monkeypatch.setattr(captured, '_open_lock', lambda binding=None: CloseFault(native_open(binding)))
        def failed_read(*args, **kwargs):
            raise primary
        monkeypatch.setattr(captured.directory, 'read_with_identity', failed_read)
        expected = kind if kind in (KeyboardInterrupt, SystemExit) else browserlease.OwnershipRefused
        with pytest.raises(expected) as raised:
            captured.current()
        if kind in (KeyboardInterrupt, SystemExit):
            assert raised.value is primary
        else:
            assert str(raised.value) == browserlease.REFUSAL
            assert raised.value.__cause__ is None and raised.value.__context__ is None
            assert 'owned-private' not in ''.join(traceback.format_exception(raised.value))
        assert authority._cleanup_unknown(raised.value)
        assert closes == ['closed'] and not browserlease._holding_lock()
        assert browserlease._THREAD_LOCK.acquire(blocking=False)
        browserlease._THREAD_LOCK.release()
    finally:
        with pytest.raises(browserlease.OwnershipRefused) as closed:
            captured.close()
        assert authority._cleanup_unknown(closed.value)



@pytest.mark.parametrize('kind', [KeyboardInterrupt, SystemExit, OSError])
def test_constructor_close_fault_never_retries_reused_numeric_descriptor(roots, monkeypatch, kind):
    original, _other = roots
    fault = kind('owned-private-constructor-close')
    real_fdopen = os.fdopen
    sentinels, closes = [], []
    class CloseFault:
        def __init__(self, real):
            self.real = real
        def close(self):
            fd = self.real.fileno()
            self.real.close()
            closes.append(fd)
            sentinel = os.open(original / 'retained-sentinel', os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            sentinels.append(sentinel)
            assert sentinel == fd, 'control must exercise actual descriptor-slot reuse'
            raise fault
    monkeypatch.setattr(os, 'fdopen', lambda *args, **kwargs: CloseFault(real_fdopen(*args, **kwargs)))
    expected = kind if kind in (KeyboardInterrupt, SystemExit) else browserlease.OwnershipRefused
    try:
        with pytest.raises(expected) as raised:
            authority.Authority()
        if kind in (KeyboardInterrupt, SystemExit):
            assert raised.value is fault
        else:
            assert str(raised.value) == browserlease.REFUSAL
            assert raised.value.__cause__ is None and raised.value.__context__ is None
        assert authority._cleanup_unknown(raised.value)
        assert len(closes) == len(sentinels) == 1
        os.fstat(sentinels[0])
        os.write(sentinels[0], b'owned-sentinel-remains-open')
    finally:
        for fd in sentinels:
            os.close(fd)


def test_cleanup_only_failure_is_fixed_and_all_directories_are_drained(roots, monkeypatch):
    captured = authority.Authority()
    held_locks, held_directory = captured.locks, captured.directory
    real_close = captured.locks.close
    calls = []
    def close_then_fail():
        calls.append(True)
        real_close()
        raise OSError('owned-private-directory-close')
    monkeypatch.setattr(captured.locks, 'close', close_then_fail)
    with pytest.raises(browserlease.OwnershipRefused) as raised:
        captured.close()
    assert held_locks._closed and held_directory._closed
    assert captured.locks is captured.directory is None
    assert authority._cleanup_unknown(raised.value)
    assert str(raised.value) == browserlease.REFUSAL
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    with pytest.raises(browserlease.OwnershipRefused) as again:
        captured.close()
    assert authority._cleanup_unknown(again.value) and calls == [True]



def test_constructor_requires_existing_memory_root_without_creating_it(roots, monkeypatch):
    original, _other = roots
    absent = original / 'absent-memory'
    monkeypatch.setattr(config, 'MEMORY_DIR', absent)
    with pytest.raises(browserlease.OwnershipRefused):
        authority.Authority()
    assert not absent.exists()

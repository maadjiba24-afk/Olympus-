"""Owned bounded native enumeration and reservation controls."""
import os
import threading

import pytest

from olympus import config, prefs, prefs_native as native


@pytest.fixture
def owned(tmp_path, monkeypatch):
    root = tmp_path / 'owned-listing'
    root.mkdir()
    monkeypatch.setattr(config, 'MEMORY_DIR', root)
    monkeypatch.setattr(native, '_GATE', threading.RLock())
    monkeypatch.setattr(native, '_LOCAL', threading.local())
    monkeypatch.setattr(native, '_IN_FLIGHT', False)
    monkeypatch.setattr(native, '_POISONED', False)
    monkeypatch.setattr(prefs, '_TRANSFORM', threading.local())
    return root


def test_initialized_root_inventory_observes_coordination_without_data_access(owned):
    with native.transaction(owned) as tx:
        entries = tx.inventory((), limit=64)
        assert [(name, kind) for name, kind, _identity in entries] == [('locks', 'directory')]
        for callback in (lambda: tx.read(('locks', 'x')), lambda: tx.inventory(('locks',), limit=1),
                         lambda: tx.reserve((('locks', 'x'),))):
            with pytest.raises(native.PreferenceStorageError): callback()
        tx.publish(('ordinary',), b'owned')
        listed = dict((name, identity) for name, _kind, identity in tx.inventory((), limit=64))
        assert listed['ordinary'] == tx.read(('ordinary',))[1]


def test_reserved_prefixes_cannot_be_consumed_by_later_probe(owned, monkeypatch):
    monkeypatch.setattr(native, '_MAX_DIRECTORIES', 5)
    with native.transaction(owned) as tx:
        planned = (('first', 'child', 'one'), ('second', 'two'))
        tx.reserve(planned)
        with pytest.raises(native.PreferenceStorageError): tx.read(('unrelated', 'record'))
        assert not (owned / 'unrelated').exists()
        tx.publish(planned[0], b'one')
        tx.publish(planned[1], b'two')
        assert tx.read(planned[0])[0] == b'one' and tx.read(planned[1])[0] == b'two'
        assert len(tx.directories) == 5


@pytest.mark.parametrize('invalid', [('valid', '../invalid'), ('locks', 'record'), ('valid', 'CON')])
def test_reservation_validates_all_addresses_before_directory_effects(owned, invalid):
    with native.transaction(owned) as tx:
        with pytest.raises((native.PreferenceStorageError, native.storage.StorageError, native.archive_io.ArchiveError)):
            tx.reserve((('would-be-created', 'record'), invalid))
    assert sorted(p.name for p in owned.iterdir()) == ['locks']


def test_metadata_size_limit_refuses_before_payload_read(owned, monkeypatch):
    with native.transaction(owned) as tx:
        tx.publish(('large-metadata',), b'x' * 65537)
        calls = []
        def no_read(*args):
            calls.append(True)
            pytest.fail('oversized metadata reached payload read')
        monkeypatch.setattr(os, 'read', no_read)
        with pytest.raises(native.PreferenceStorageError): tx.read(('large-metadata',), max_bytes=65536)
        assert calls == []


@pytest.mark.parametrize('invalid', [-1, True, 1048577])
def test_invalid_read_bound_refuses_before_address_admission(owned, monkeypatch, invalid):
    with native.transaction(owned) as tx:
        monkeypatch.setattr(tx, '_admit', lambda *a: pytest.fail('invalid read bound admitted an address'))
        with pytest.raises(native.PreferenceStorageError): tx.read(('record',), max_bytes=invalid)


def test_inventory_overflow_does_not_hide_partial_results(owned):
    with native.transaction(owned) as tx:
        for index in range(3): tx.write_new(('group', f'entry-{index}'), b'owned')
        with pytest.raises(native.PreferenceStorageError): tx.inventory(('group',), limit=2)
        assert len(tx.inventory(('group',), limit=3)) == 3


def test_inventory_missing_and_later_disappearing_directory_are_distinct(owned):
    with native.transaction(owned) as tx:
        assert tx.inventory(('missing',), limit=1) is None
        tx.write_new(('present', 'entry'), b'owned')
        assert tx.inventory(('present',), limit=1)[0][0] == 'entry'
    (owned / 'present').rename(owned / 'retained-present')
    # A fresh transaction can observe initial absence. An acquired directory's
    # disappearance is tested at the held iterator boundary below.
    with native.transaction(owned) as tx:
        assert tx.inventory(('present',), limit=1) is None


@pytest.mark.parametrize('fault_type', [FileNotFoundError, KeyboardInterrupt, SystemExit])
def test_listed_entry_stat_failure_closes_iterator_preserving_primary(owned, monkeypatch, fault_type):
    fault = fault_type('owned-private-listing-fault')
    closed = []
    class Entry:
        name = 'owned-record'
        def stat(self, **kwargs): raise fault
    class Iterator:
        def __next__(self): return Entry()
        def close(self): closed.append(True)
    with native.transaction(owned) as tx:
        monkeypatch.setattr(tx.directory, 'entries', lambda: Iterator())
        expected = native.PreferenceStorageError if fault_type is FileNotFoundError else fault_type
        with pytest.raises(expected) as error: tx.inventory((), limit=2)
        if fault_type is FileNotFoundError: assert 'owned-private' not in str(error.value)
        else: assert error.value is fault
        assert closed == [True]
    assert closed == [True]


@pytest.mark.parametrize('with_primary', [False, True])
def test_listing_cleanup_failure_is_sticky_and_preserves_primary(owned, monkeypatch, with_primary):
    primary = KeyboardInterrupt('owned-listing-primary')
    closed = []
    class Iterator:
        def __next__(self):
            if with_primary: raise primary
            raise StopIteration
        def close(self):
            closed.append(True)
            raise OSError('owned-listing-close-failure')
    expected = KeyboardInterrupt if with_primary else OSError
    with pytest.raises(expected) as error:
        with native.transaction(owned) as tx:
            monkeypatch.setattr(tx.directory, 'entries', lambda: Iterator())
            tx.inventory((), limit=2)
    if with_primary: assert error.value is primary
    assert native.cleanup_unknown(error.value) and native._POISONED
    assert closed == [True]
    with pytest.raises(native.PreferenceStorageError):
        with native.transaction(owned): pytest.fail('poisoned native executor admitted')


def test_unbound_stage_blocks_ordinary_write_without_losing_evidence(owned, monkeypatch):
    owner = 'owned-listing-principal'
    prefs.set(owner, 'autonomy', 0)
    current = prefs._relative_paths(owner)[0]
    stage = current[:-1] + ('pending-owned-unbound.json',)
    with native.transaction(owned) as tx:
        tx.write_new(stage, b'owned-retained-stage')
        original = tx.read(current)
        retained = tx.read(stage)
    monkeypatch.setattr(native.Transaction, 'publish', lambda *a, **k: pytest.fail('unbound stage allowed another write'))
    with pytest.raises(prefs.PreferenceOperationError): prefs.set(owner, 'autonomy', 4)
    with native.transaction(owned) as tx:
        assert tx.read(current) == original and tx.read(stage) == retained

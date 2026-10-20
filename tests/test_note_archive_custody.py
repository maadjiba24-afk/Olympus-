"""Owned M11 archive caller evidence; no provider, live custody or child process."""
import base64
import gzip
import hashlib
import io
import os
import tarfile

import pytest

from olympus import archive_io as aio, config, memory, note_archive as archives
from olympus import note_evidence as notes, vault_codec as codec, vault_custody as custody
from olympus import vault_storage


@pytest.fixture(autouse=True)
def owned_key(monkeypatch):
    monkeypatch.setenv('OLYMPUS_ENV', 'test')
    monkeypatch.setenv('OLYMPUS_SECRET_KEY', 'owned-note-archive-key')
    monkeypatch.delenv('OLYMPUS_SECRET_KEY_FILE', raising=False)


def seed():
    with memory.user_context('archive-owner'):
        return memory.save('lessons', 'Owned archive', 'Owned original bytes')


def test_note_new_envelope_exact_roundtrip_and_report(tmp_path):
    note = seed()
    before = note.read_bytes()
    path = tmp_path/'notes \u00e9.enc'
    created = archives.export(path, user='archive-owner', encrypt=True)
    assert created['archive_encryption'] == {
        'encrypted':True, 'format_version':1, 'context_authenticated':True, 'legacy_unbound':False}
    ciphertext = path.read_bytes()
    assert not ciphertext.startswith(b'\x1f\x8b')
    report = archives.restore(path, user='archive-owner')
    assert report['archive_encryption'] == created['archive_encryption']
    assert report['count'] >= 1 and note.read_bytes() == before and path.read_bytes() == ciphertext
    assert memory._maybe_decrypt(ciphertext).startswith(b'\x1f\x8b')


def test_note_and_backup_purposes_cannot_substitute(tmp_path):
    seed()
    plain = tmp_path/'plain.tgz'
    archives.export(plain, user='archive-owner')
    raw = plain.read_bytes()
    with aio.operation('backup') as session:
        ciphertext = session.encrypt(raw)
    wrong = tmp_path/'wrong.enc'
    wrong.write_bytes(ciphertext)
    with pytest.raises(ValueError, match='could not be decrypted'):
        archives.load_archive(wrong)
    with pytest.raises(ValueError, match='could not be decrypted'):
        memory._maybe_decrypt(ciphertext)
    right = tmp_path/'right.enc'
    archives.export(right, user='archive-owner', encrypt=True)
    with aio.operation('backup') as session:
        with pytest.raises(codec.EnvelopeError):
            session.decrypt(right.read_bytes())


def test_legacy_encryption_reports_unbound_and_preserves_source(tmp_path):
    from cryptography.fernet import Fernet
    seed()
    plain = tmp_path/'plain.tgz'
    archives.export(plain, user='archive-owner')
    key = base64.urlsafe_b64encode(hashlib.sha256(b'owned-note-archive-key').digest())
    ciphertext = Fernet(key).encrypt(plain.read_bytes())
    path = tmp_path/'legacy.enc'
    path.write_bytes(ciphertext)
    manifest, changes = archives.load_archive(path)
    expected = {'encrypted':True,'format_version':0,'context_authenticated':False,'legacy_unbound':True}
    assert changes and manifest['archive_encryption'] == expected
    report = archives.restore(path, user='archive-owner')
    assert report['archive_encryption'] == expected and path.read_bytes() == ciphertext


def test_plaintext_export_and_import_ignore_unrelated_broken_key(tmp_path, monkeypatch):
    seed()
    monkeypatch.delenv('OLYMPUS_SECRET_KEY')
    monkeypatch.setenv('OLYMPUS_SECRET_KEY_FILE', str(tmp_path/'absent-key'))
    calls = []
    def forbidden(*args, **kwargs):
        calls.append('capture')
        raise AssertionError('plaintext must not open custody')
    monkeypatch.setattr(custody, 'capture', forbidden)
    path = tmp_path/'plain.tgz'
    created = archives.export(path, user='archive-owner')
    report = archives.restore(path, user='archive-owner')
    assert not created['archive_encryption']['encrypted']
    assert report['archive_encryption'] == created['archive_encryption'] and calls == []


def test_export_captures_selected_file_material_before_inventory(tmp_path, monkeypatch):
    seed()
    key = tmp_path/'owned-key'
    key.write_bytes(b'first-owned-key'); key.chmod(0o600)
    monkeypatch.delenv('OLYMPUS_SECRET_KEY')
    monkeypatch.setenv('OLYMPUS_SECRET_KEY_FILE', str(key))
    original = archives.collect
    def changed(roots):
        key.write_bytes(b'second-owned-key')
        monkeypatch.setenv('OLYMPUS_SECRET_KEY_FILE', str(tmp_path/'unselected'))
        return original(roots)
    monkeypatch.setattr(archives, 'collect', changed)
    path = tmp_path/'captured.enc'
    archives.export(path, user='archive-owner', encrypt=True)
    with aio.operation('notes', selectors={'OLYMPUS_ENV':'test','OLYMPUS_SECRET_KEY':'first-owned-key'}) as session:
        assert session.decrypt(path.read_bytes()).data.startswith(b'\x1f\x8b')
    assert key.read_bytes() == b'second-owned-key'


def test_import_freezes_key_selector_before_external_read(tmp_path, monkeypatch):
    seed()
    path = tmp_path/'captured.enc'
    archives.export(path, user='archive-owner', encrypt=True)
    original = archives.external_read
    def changed(*args):
        monkeypatch.setenv('OLYMPUS_SECRET_KEY', 'wrong-new-ambient-key')
        return original(*args)
    monkeypatch.setattr(archives, 'external_read', changed)
    manifest, changes = archives.load_archive(path)
    assert changes and manifest['archive_encryption']['context_authenticated']


def test_archive_admission_precedes_inventory_and_input_reads(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(archives, 'collect', lambda *a: calls.append('inventory'))
    monkeypatch.setattr(archives, 'external_read', lambda *a: calls.append('read'))
    with aio.operation('backup', capture_key=False):
        with pytest.raises(aio.ArchiveError, match='capacity'):
            archives.export(tmp_path/'output', user='archive-owner')
        with pytest.raises(aio.ArchiveError, match='capacity'):
            archives.restore(tmp_path/'input')
    assert calls == []


def test_bad_custody_refuses_before_inventory_or_publication(tmp_path, monkeypatch):
    monkeypatch.delenv('OLYMPUS_SECRET_KEY')
    monkeypatch.setenv('OLYMPUS_SECRET_KEY_FILE', str(tmp_path/'absent'))
    calls = []
    monkeypatch.setattr(archives, 'collect', lambda *a: calls.append('inventory'))
    output = tmp_path/'output.enc'
    with pytest.raises(custody.CustodyError):
        archives.export(output, user='archive-owner', encrypt=True)
    assert calls == [] and not output.exists()


def test_wrong_decryption_never_reaches_note_transaction(tmp_path, monkeypatch):
    note = seed()
    before = note.read_bytes()
    path = tmp_path/'owned.enc'
    archives.export(path, user='archive-owner', encrypt=True)
    calls = []
    monkeypatch.setattr(notes, 'transact', lambda *a: calls.append('mutation'))
    monkeypatch.setenv('OLYMPUS_SECRET_KEY','incorrect-owned-key')
    with pytest.raises(ValueError):
        archives.restore(path)
    assert calls == [] and note.read_bytes() == before


@pytest.mark.parametrize('limit', ['compressed','expanded','manifest'])
def test_export_limits_refuse_before_publication_and_release_admission(tmp_path, monkeypatch, limit):
    seed()
    if limit == 'compressed':
        monkeypatch.setattr(aio, 'MAX_PLAINTEXT', 32)
    elif limit == 'expanded':
        monkeypatch.setattr(archives, 'MAX_ARCHIVE', 511)
    else:
        # Empty inventory isolates the manifest bound from source-file bounds.
        monkeypatch.setattr(archives, 'collect', lambda *a: [])
        monkeypatch.setattr(notes, 'MAX_NOTE', 8)
    output = tmp_path/'refused.tgz'
    with pytest.raises(ValueError, match='bound'):
        archives.export(output, user='archive-owner')
    assert not output.exists()
    with aio.operation('backup',capture_key=False) as session:
        assert session.active


def test_expansion_refuses_before_tar_header_parser(tmp_path, monkeypatch):
    path = tmp_path/'bomb.tgz'
    path.write_bytes(gzip.compress(b'X' * 2048))
    monkeypatch.setattr(archives, 'MAX_ARCHIVE', 1024)
    calls = []
    monkeypatch.setattr(archives.tarfile, 'open', lambda *a, **k: calls.append('parser'))
    with pytest.raises(ValueError, match='expanded archive bound'):
        archives.load_archive(path)
    assert calls == []


def test_root_change_prevents_export_publication(tmp_path, monkeypatch):
    seed()
    original = archives.collect
    def changed(roots):
        result = original(roots)
        monkeypatch.setattr(config, 'MEMORY_DIR', tmp_path/'different-memory')
        return result
    monkeypatch.setattr(archives, 'collect', changed)
    path = tmp_path/'output.tgz'
    with pytest.raises(ValueError):
        archives.export(path, user='archive-owner')
    assert not path.exists()


@pytest.mark.parametrize('seam', ['external_read','ingest_gate','decrypt'])
def test_restore_refuses_root_change_before_transaction(tmp_path, monkeypatch, seam):
    note = seed()
    before = note.read_bytes()
    archive = tmp_path/'owned.enc'
    archives.export(archive, user='archive-owner', encrypt=True)
    alternate = tmp_path/'different-memory'
    alternate.mkdir()
    retained = alternate/'untouched'
    retained.write_bytes(b'owned existing state')
    calls = []
    monkeypatch.setattr(notes, 'transact', lambda *a: calls.append('transaction'))
    if seam == 'external_read':
        original = archives.external_read
        def changed(*args):
            raw = original(*args)
            monkeypatch.setattr(config, 'MEMORY_DIR', alternate)
            return raw
        monkeypatch.setattr(archives,'external_read',changed)
    elif seam == 'ingest_gate':
        def changed(*args):
            monkeypatch.setattr(config, 'MEMORY_DIR', alternate)
        monkeypatch.setattr(memory,'_gate_import',changed)
    else:
        original = aio.ArchiveSession.decrypt
        def changed(self, raw):
            result = original(self,raw)
            monkeypatch.setattr(config, 'MEMORY_DIR', alternate)
            return result
        monkeypatch.setattr(aio.ArchiveSession,'decrypt',changed)
    with pytest.raises(ValueError, match='root changed'):
        archives.restore(archive,user='archive-owner')
    assert calls == [] and note.read_bytes() == before
    assert list(alternate.iterdir()) == [retained] and retained.read_bytes() == b'owned existing state'


@pytest.mark.parametrize('selected_owner', [None, 'archive-owner'])
def test_export_preserves_default_shared_and_explicit_owner_during_custody(tmp_path, monkeypatch, selected_owner):
    private = seed()
    with memory.user_context('shared'):
        shared = memory.save('lessons','Shared archive','Owned shared bytes')
    original = custody.capture
    def changed(selectors):
        result = original(selectors)
        memory.set_user('different-owner')
        return result
    monkeypatch.setattr(custody,'capture',changed)
    archive = tmp_path/'owned.enc'
    with memory.user_context('archive-owner'):
        manifest = archives.export(archive, user=selected_owner, encrypt=True)
    expected_owner = 'shared' if selected_owner is None else selected_owner
    selected = shared if selected_owner is None else private
    excluded = private if selected_owner is None else shared
    assert manifest['scope'] == {'user':expected_owner}
    paths = {row['path'] for row in manifest['files']}
    assert notes.relative(selected) in paths and notes.relative(excluded) not in paths


def test_export_root_change_during_custody_precedes_note_guard(tmp_path, monkeypatch):
    seed()
    original = custody.capture
    alternate = tmp_path/'alternate'
    calls = []
    def changed(selectors):
        result = original(selectors)
        monkeypatch.setattr(config,'MEMORY_DIR',alternate)
        return result
    monkeypatch.setattr(custody,'capture',changed)
    monkeypatch.setattr(notes,'guard',lambda: calls.append('guard'))
    archive = tmp_path/'refused.enc'
    with pytest.raises(ValueError,match='root changed'):
        archives.export(archive,user='archive-owner',encrypt=True)
    assert calls == [] and not alternate.exists() and not archive.exists()


def test_compressed_buffer_refuses_before_growth():
    output = aio.BoundedBuffer(4)
    output.write(b'abcd')
    with pytest.raises(aio.ArchiveError):
        output.write(b'e')
    assert output.getvalue() == b'abcd'


def test_tar_headers_and_padding_count_toward_creation_bound():
    rows = [('unicode/\u00e9' + 'x' * 150, b'owned')]
    with pytest.raises(aio.ArchiveError, match='expanded'):
        aio.encode_tar(rows, max_expanded=512)
    raw = aio.encode_tar(rows, max_expanded=10240)
    assert len(gzip.decompress(raw)) == 10240
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
        assert archive.extractfile(rows[0][0]).read() == b'owned'


def test_publication_unicode_replacement_and_hardlink_refusal(tmp_path):
    output = tmp_path/'nested'/'owned \u00e9.tgz'
    aio.publish_file(output,b'first')
    aio.publish_file(output,b'second')
    assert aio.read_file(output,10) == b'second'
    linked = tmp_path/'linked'
    os.link(output,linked)
    with pytest.raises(aio.ArchivePublicationError) as failure:
        aio.publish_file(output,b'third')
    assert not failure.value.publication_uncertain
    assert linked.read_bytes() == output.read_bytes() == b'second'


@pytest.mark.parametrize('name', ['CON','COM\u00b9.txt','LPT\u00b2','a:stream','trail.','x'*256])
def test_invalid_output_leaf_refuses_before_creating_parent(tmp_path, name):
    parent = tmp_path/'absent'
    with pytest.raises(aio.ArchiveError):
        aio.publish_file(parent/name,b'owned')
    assert not parent.exists()


def test_input_alias_refuses_before_reading_normalized_leaf(tmp_path):
    path = tmp_path/'owned'
    path.write_bytes(b'owned')
    with pytest.raises(aio.ArchiveError):
        aio.read_file(tmp_path/'owned.', 10)
    assert path.read_bytes() == b'owned'


def test_output_parent_alias_refuses_before_creating_normalized_parent(tmp_path):
    with pytest.raises(aio.ArchiveError):
        aio.publish_file(tmp_path/'absent.'/'owned',b'owned')
    assert not (tmp_path/'absent').exists() and not (tmp_path/'absent.').exists()


def test_publication_write_failure_retains_stage_and_old_target(tmp_path, monkeypatch):
    path = tmp_path/'owned'
    path.write_bytes(b'original')
    original = vault_storage._Directory._write
    def failed(directory,fd,raw):
        original(directory,fd,raw)
        raise OSError('owned injected failure')
    monkeypatch.setattr(vault_storage._Directory,'_write',failed)
    with pytest.raises(aio.ArchivePublicationError) as failure:
        aio.publish_file(path,b'replacement')
    assert not failure.value.publication_uncertain and path.read_bytes() == b'original'
    staging = tmp_path/failure.value.retained_staging_name
    assert staging.read_bytes() == b'replacement'


def test_publication_completion_failure_reports_uncertainty(tmp_path, monkeypatch):
    path = tmp_path/'owned'
    original = vault_storage._Directory.sync
    calls = []
    def failed(directory):
        original(directory)
        calls.append('after rename')
        raise OSError('owned completion failure')
    monkeypatch.setattr(vault_storage._Directory,'sync',failed)
    with pytest.raises(aio.ArchivePublicationError) as failure:
        aio.publish_file(path,b'replacement')
    assert calls == ['after rename'] and failure.value.publication_uncertain
    assert path.read_bytes() == b'replacement'


def test_publication_file_close_interruption_still_drains_parent(tmp_path, monkeypatch):
    real_close = aio.os.close
    directory_close = vault_storage._Directory.close
    interrupted, drained = [], []
    def close(fd):
        real_close(fd)
        if not interrupted:
            interrupted.append(fd)
            raise KeyboardInterrupt('owned close interruption')
    def drained_close(directory):
        try:
            return directory_close(directory)
        finally:
            drained.append(directory._closed)
    monkeypatch.setattr(aio.os, 'close', close)
    monkeypatch.setattr(vault_storage._Directory,'close',drained_close)
    path = tmp_path/'owned'
    with pytest.raises(KeyboardInterrupt) as failure:
        aio.publish_file(path,b'owned')
    assert interrupted and drained == [True]
    assert failure.value.publication_uncertain and path.read_bytes() == b'owned'

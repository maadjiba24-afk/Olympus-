"""Owned gallery lifecycle evidence. No providers or network access."""
import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import threading

import pytest

from olympus import gallery, gallery_state as state, memory, sandbox


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, 'workdir', lambda: tmp_path)
    return tmp_path


@pytest.fixture
def png():
    from PIL import Image
    output = io.BytesIO()
    Image.new('RGB', (3, 2), (20, 40, 60)).save(output, format='PNG')
    return output.getvalue()


def publish(owner, data, operation='generate1', name='owned.png'):
    store = state.Store(owner)
    reservation = store.reserve(operation, 'generate', {'prompt_digest': 'owned'}, name)
    assert reservation['execute']
    store.start(operation)
    return store.finalize(operation, data)


def manifest(store):
    return json.loads((store.root / 'state.json').read_text())


def replace_manifest(store, value):
    (store.root / 'state.json').write_text(json.dumps(value))


@pytest.mark.parametrize('owner', [None, '', ' ', 42, {}, 'a' * 8193, '\ud800'])
def test_explicit_invalid_owner_never_falls_back(workspace, owner):
    with pytest.raises(state.GalleryError) as caught:
        gallery.list_images(owner)
    assert caught.value.code == 'invalid_owner'
    assert list(workspace.iterdir()) == []


def test_exact_owner_capture_and_collisions(workspace, png):
    owners = ['a.b', 'a@b', 'a b', 'a-b', 'A-B', 'é', 'é', 'a' * 65, 'a' * 64 + 'b']
    for owner in owners:
        publish(owner, png)
    assert len({state.Store(o).root for o in owners}) == len(owners)
    with memory.user_context('a.b'):
        assert gallery.list_images()[0]['name'] == 'owned.png'
        with memory.user_context('other'):
            assert gallery.list_images() == []
        assert memory.current_owner() == 'a.b'
    for owner in owners:
        assert len(gallery.list_images(owner)) == 1
        assert gallery.read_image('owned.png', owner)[0] == png
    with pytest.raises(state.GalleryError, match='not found'):
        gallery.read_image('owned.png', 'other')


def test_missing_unclaimed_and_owned_are_distinct(workspace, png):
    assert gallery.list_result('alice')['status'] == 'missing'
    (workspace / 'legacy.png').write_bytes(png)
    (workspace / 'gallery' / 'alice').mkdir(parents=True)
    (workspace / 'gallery' / 'alice' / 'old.png').write_bytes(png)
    result = gallery.list_result('alice')
    assert result == {'status': 'unclaimed', 'images': [], 'unclaimed': True}
    with pytest.raises(state.GalleryError) as caught:
        gallery.read_image('legacy.png', 'alice')
    assert caught.value.status == 404


@pytest.mark.parametrize('name', ['../x.png', '/x.png', 'C:\\x.png', 'x.png:stream', 'CON.png',
                                  'x.png.', 'x.png ', 'x\x00.png', 'x\n.png', 'x/y.png', 'a' * 125 + '.png'])
def test_names_refused_before_reservation(workspace, name):
    with pytest.raises(state.GalleryError):
        state.Store('alice').reserve('op', 'generate', {}, name)
    assert list(workspace.iterdir()) == []


def test_operation_replay_conflict_delete_tombstone(workspace, png):
    store = state.Store('alice')
    result = publish('alice', png)
    image = result['image']
    assert store.reserve('generate1', 'generate', {'prompt_digest': 'owned'}, 'owned.png')['execute'] is False
    with pytest.raises(state.GalleryError) as caught:
        store.reserve('generate1', 'generate', {'prompt_digest': 'different'}, 'owned.png')
    assert caught.value.code == 'operation_conflict'
    with pytest.raises(state.GalleryError):
        store.delete('owned.png', image['id'], '0' * 64, 'delete1')
    deleted = store.delete('owned.png', image['id'], image['revision'], 'delete1')
    assert deleted['status'] == 'deleted'
    assert store.delete('owned.png', image['id'], image['revision'], 'delete1') == deleted
    assert gallery.list_images('alice') == []
    assert (store.root / 'objects' / (image['id'] + '.blob')).read_bytes() == png
    assert manifest(store)['tombstones'][image['id']] == image
    assert store.lookup('generate1', 'generate', {'prompt_digest': 'owned'}, 'owned.png')['status'] == 'complete'
    replacement = publish('alice', png, 'generate2')
    with pytest.raises(state.GalleryError) as caught:
        store.delete('owned.png', image['id'], image['revision'], 'delete2')
    assert caught.value.code == 'stale'
    assert gallery.list_images('alice')[0]['id'] == replacement['image']['id']


def test_started_without_output_is_indeterminate_no_replay(workspace):
    store = state.Store('alice')
    store.reserve('op', 'generate', {})
    store.start('op')
    assert store.recover('op')['status'] == 'indeterminate'
    assert not store.reserve('op', 'generate', {})['execute']
    with pytest.raises(state.GalleryError):
        store.start('op')


def test_missing_initialized_manifest_refuses_replay(workspace):
    store = state.Store('alice')
    store.reserve('op', 'generate', {})
    store.start('op')
    (store.root / 'state.json').rename(store.root / 'state-lost-evidence.json')
    with pytest.raises(state.GalleryError) as caught:
        store.reserve('op', 'generate', {})
    assert caught.value.code == 'unavailable'


@pytest.mark.parametrize('corruption', ['empty', 'duplicate', 'owner', 'version', 'complete_no_image', 'edit_bad_source', 'duplicate_object'])
def test_malformed_authority_preserved(workspace, corruption):
    store = state.Store('alice')
    store.reserve('op', 'generate', {}, 'a.png')
    current = manifest(store)
    if corruption == 'empty':
        raw = b''
    elif corruption == 'duplicate':
        raw = b'{"version":2,"version":2}'
    else:
        if corruption == 'owner':
            current['owner'] = 'bob'
        elif corruption == 'version':
            current['version'] = 1
        elif corruption == 'complete_no_image':
            current['operations']['op']['status'] = 'complete'
        elif corruption == 'edit_bad_source':
            current['operations']['op'].update(kind='edit', source=['x'])
        else:
            current['operations']['other'] = {**current['operations']['op'], 'operation_id': 'other', 'name': 'b.png'}
        raw = json.dumps(current).encode()
    (store.root / 'state.json').write_bytes(raw)
    with pytest.raises(state.GalleryError) as caught:
        gallery.list_images('alice')
    assert caught.value.code == 'unavailable'
    assert (store.root / 'state.json').read_bytes() == raw


def test_output_recovers_after_manifest_failure(workspace, png, monkeypatch):
    store = state.Store('alice')
    store.reserve('op', 'generate', {}, 'a.png')
    store.start('op')
    original = state.Directory.publish
    def fail(directory, name, data):
        if name == 'state.json':
            raise OSError('owned fail before manifest')
        return original(directory, name, data)
    with monkeypatch.context() as patch:
        patch.setattr(state.Directory, 'publish', fail)
        with pytest.raises(state.GalleryError):
            store.finalize('op', png)
    assert store.list_images() == []
    assert store.recover('op')['status'] == 'complete'
    assert gallery.read_image('a.png', 'alice')[0] == png


@pytest.mark.parametrize('folder', ['objects', 'outputs'])
def test_failed_evidence_barrier_retried_before_adoption(workspace, png, monkeypatch, folder):
    store = state.Store('alice')
    store.reserve('op', 'generate', {}, 'a.png')
    store.start('op')
    original = state.Directory.sync
    calls = []
    def fail(directory):
        calls.append(directory.path.name)
        if directory.path.name == folder:
            raise OSError('owned directory barrier failure')
        return original(directory)
    with monkeypatch.context() as patch:
        patch.setattr(state.Directory, 'sync', fail)
        with pytest.raises(state.GalleryError):
            store.finalize('op', png)
        if folder == 'outputs':
            with pytest.raises(state.GalleryError):
                store.recover('op')
        assert store.status('op')['status'] == 'indeterminate'
    assert folder in calls
    # For the blob-only crash, received bytes may be replayed into local
    # persistence, but recover never guesses a provider result from an orphan.
    if folder == 'objects':
        assert store.recover('op')['status'] == 'indeterminate'
        store.finalize('op', png)
    else:
        store.recover('op')
    assert store.status('op')['status'] == 'complete'


def test_lost_ack_after_manifest_commit_is_complete(workspace, png, monkeypatch):
    store = state.Store('alice')
    store.reserve('op', 'generate', {}, 'a.png')
    store.start('op')
    original = state.Directory.publish
    def fail(directory, name, data):
        original(directory, name, data)
        if name == 'state.json':
            raise OSError('owned lost acknowledgement')
    with monkeypatch.context() as patch:
        patch.setattr(state.Directory, 'publish', fail)
        with pytest.raises(state.GalleryError):
            store.finalize('op', png)
    assert store.recover('op')['status'] == 'complete'


def test_modified_blob_is_unavailable(workspace, png):
    result = publish('alice', png)
    store = state.Store('alice')
    blob = store.root / 'objects' / (result['image']['id'] + '.blob')
    blob.write_bytes(b'changed')
    for action in (lambda: store.list_images(), lambda: store.read('owned.png'),
                   lambda: store.delete('owned.png', result['image']['id'], result['image']['revision'], 'delete')):
        with pytest.raises(state.GalleryError) as caught:
            action()
        assert caught.value.code == 'unavailable'


def test_concurrent_name_reservations_one_winner(workspace):
    barrier = threading.Barrier(8)
    results = []
    def run(index):
        barrier.wait()
        try:
            results.append(state.Store('alice').reserve(f'op{index}', 'generate', {}, 'one.png'))
        except state.GalleryError as exc:
            results.append(exc.code)
    threads = [threading.Thread(target=run, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()
    assert len([r for r in results if isinstance(r, dict)]) == 1
    assert results.count('name_conflict') == 7


def test_claim_review_copy_and_cross_owner_conflict(workspace, png):
    source = workspace / 'legacy.png'
    source.write_bytes(png)
    review = gallery.legacy_review('alice')
    result = gallery.claim_legacy('alice', review=review, review_digest=review['digest'])
    assert result['status'] == 'complete'
    assert source.read_bytes() == png
    assert gallery.read_image('legacy.png', 'alice')[0] == png
    assert gallery.claim_legacy('alice', review=review, review_digest=review['digest'])['status'] == 'complete'
    other = gallery.legacy_review('bob')
    refused = gallery.claim_legacy('bob', review=other, review_digest=other['digest'])
    assert refused['results'][0]['code'] == 'legacy_conflict'
    source.rename(workspace / 'renamed.png')
    renamed = gallery.legacy_review('bob')
    assert gallery.claim_legacy('bob', review=renamed, review_digest=renamed['digest'])['results'][0]['code'] == 'legacy_conflict'


def test_claim_source_change_and_duplicate_review_refused(workspace, png):
    source = workspace / 'legacy.png'
    source.write_bytes(png)
    review = gallery.legacy_review('alice')
    duplicate = copy.deepcopy(review)
    duplicate['items'] *= 2
    duplicate.pop('digest')
    duplicate['digest'] = state._digest(state._json(duplicate))
    with pytest.raises(state.GalleryError) as caught:
        gallery.claim_legacy('alice', review=duplicate, review_digest=duplicate['digest'])
    assert caught.value.code == 'review_conflict'
    source.write_bytes(png + b'changed')
    assert gallery.claim_legacy('alice', review=review, review_digest=review['digest'])['results'][0]['code'] == 'stale'


def test_missing_initialized_claim_ledger_refuses_second_owner(workspace, png):
    (workspace / 'legacy.png').write_bytes(png)
    review = gallery.legacy_review('alice')
    assert gallery.claim_legacy('alice', review=review, review_digest=review['digest'])['status'] == 'complete'
    ledger = workspace / 'gallery-v2' / 'claims.json'
    ledger.rename(ledger.with_suffix('.preserved'))
    other = gallery.legacy_review('bob')
    with pytest.raises(state.GalleryError) as caught:
        gallery.claim_legacy('bob', review=other, review_digest=other['digest'])
    assert caught.value.code == 'unavailable'


@pytest.mark.skipif(os.name == 'nt', reason='POSIX owned symlink/hardlink fixture')
def test_unsafe_blob_and_parent_refused(workspace, png):
    result = publish('alice', png)
    store = state.Store('alice')
    blob = store.root / 'objects' / (result['image']['id'] + '.blob')
    preserved = blob.with_suffix('.preserved')
    blob.rename(preserved)
    blob.symlink_to(preserved)
    with pytest.raises(state.GalleryError):
        store.read('owned.png')
    blob.unlink()
    os.link(preserved, blob)
    with pytest.raises(state.GalleryError):
        store.read('owned.png')


def test_retained_tombstone_bytes_count_against_quota(workspace, png, monkeypatch):
    store = state.Store('alice')
    image = publish('alice', png)['image']
    store.delete(image['name'], image['id'], image['revision'], 'delete')
    retained = sum(path.stat().st_size for path in store.root.rglob('*') if path.is_file())
    reservation = state.MAX_IMAGE_BYTES + 2 * state.MAX_STATE_BYTES + state.MAX_OUTPUT_RECEIPT_BYTES
    monkeypatch.setattr(state, 'MAX_RETAINED_BYTES', retained + reservation - 1)
    with pytest.raises(state.GalleryError) as caught:
        store.reserve('next', 'generate', {}, 'next.png')
    assert caught.value.code == 'capacity'
    assert store.lookup('next', 'generate', {}, 'next.png') is None
    assert (store.root / 'objects' / (image['id'] + '.blob')).read_bytes() == png


@pytest.mark.parametrize('failure', ['retained', 'disk_low', 'disk_unknown'])
def test_storage_admission_refuses_before_provider(workspace, monkeypatch, failure):
    from olympus import media
    from types import SimpleNamespace
    monkeypatch.setenv('OLYMPUS_MEDIA_API_KEY', 'owned-fixture-not-a-real-key')
    monkeypatch.setenv('OLYMPUS_MEDIA_BASE_URL', 'https://owned.invalid/v1')
    calls = []
    monkeypatch.setattr(media, '_post_image', lambda *args, **kwargs: calls.append(args))
    if failure == 'retained':
        monkeypatch.setattr(state, 'MAX_RETAINED_BYTES', 1)
    elif failure == 'disk_low':
        monkeypatch.setattr(state.Directory, 'free_bytes', lambda directory: 0)
    else:
        def unavailable(path):
            raise PermissionError('owned space evidence denial')
        monkeypatch.setattr(state.Directory, 'free_bytes', unavailable)
    with pytest.raises(state.GalleryError) as caught:
        media.generate_image_result('owned prompt', owner='alice', operation_id='7' * 32)
    assert caught.value.code == ('unavailable' if failure == 'disk_unknown' else 'capacity')
    assert calls == []


def test_maximum_utf8_owner_can_recover_durable_output(workspace, png, monkeypatch):
    owner = '\U0001f600' * 8192
    store = state.Store(owner)
    store.reserve('op', 'generate', {}, 'a.png')
    store.start('op')
    original = state.Directory.publish
    def fail(directory, name, data):
        if name == 'state.json':
            raise OSError('owned publication interruption')
        return original(directory, name, data)
    with monkeypatch.context() as patch:
        patch.setattr(state.Directory, 'publish', fail)
        with pytest.raises(state.GalleryError):
            store.finalize('op', png, {'reported_model': 'x' * 8000})
    assert store.recover('op')['status'] == 'complete'
    assert store.read('a.png')[0] == png


def test_deep_metadata_is_typed_refusal_before_reservation(workspace):
    nested = {}
    for _ in range(2000):
        nested = {'nested': nested}
    with pytest.raises(state.GalleryError) as caught:
        state.Store('alice').reserve('deep', 'generate', nested)
    assert caught.value.code == 'invalid'
    assert list(workspace.iterdir()) == []


def test_deep_disk_metadata_is_unavailable_and_preserved(workspace):
    store = state.Store('alice')
    store.reserve('op', 'generate', {})
    raw = b'[' * 2000 + b'0' + b']' * 2000
    (store.root / 'state.json').write_bytes(raw)
    with pytest.raises(state.GalleryError) as caught:
        store.status('op')
    assert caught.value.code == 'unavailable'
    assert (store.root / 'state.json').read_bytes() == raw

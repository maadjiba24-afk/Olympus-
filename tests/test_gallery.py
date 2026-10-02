"""Gallery public surface uses strict owned state, never raw legacy filenames."""
import io
import pytest
from olympus import gallery, gallery_state, sandbox


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, 'workdir', lambda: tmp_path)
    return tmp_path


def _png():
    from PIL import Image
    out = io.BytesIO()
    Image.new('RGB', (2, 2), 'blue').save(out, 'PNG')
    return out.getvalue()


def _put(name, *, owner='shared', operation=None):
    store = gallery_state.Store(owner)
    operation = operation or name.replace('.', '_')
    store.reserve(operation, 'generate', {}, name)
    store.start(operation)
    return store.finalize(operation, _png())['image']


def test_list_empty(workspace):
    assert gallery.list_images('shared') == []


def test_list_only_owned_images_newest_first(workspace):
    _put('a.png')
    (workspace / 'notes.txt').write_text('hello')
    (workspace / 'legacy.png').write_bytes(_png())
    _put('b.png')
    assert [i['name'] for i in gallery.list_images('shared')] == ['b.png', 'a.png']


def test_list_reports_size(workspace):
    _put('a.png')
    assert gallery.list_images('shared')[0]['bytes'] == len(_png())


def test_read_image_returns_bytes_and_type(workspace):
    _put('pic.png')
    assert gallery.read_image('pic.png', 'shared') == (_png(), 'image/png')


@pytest.mark.parametrize('name,code', [('notes.txt', 'unsupported_type'), ('nope.png', 'missing'), ('../secret.png', 'invalid_name')])
def test_read_typed_refusals(workspace, name, code):
    with pytest.raises(gallery_state.GalleryError) as caught:
        gallery.read_image(name, 'shared')
    assert caught.value.code == code


def test_delete_requires_identity_and_retains_bytes(workspace):
    image = _put('gone.png')
    with pytest.raises(gallery_state.GalleryError):
        gallery.delete_image('gone.png', 'shared')
    result = gallery.delete_image('gone.png', 'shared', expected_id=image['id'],
                                  expected_revision=image['revision'], operation_id='delete')
    assert result['status'] == 'deleted'
    assert gallery.list_images('shared') == []
    assert (gallery.owner_root('shared') / 'objects' / (image['id'] + '.blob')).read_bytes() == _png()


def test_delete_rejects_traversal(workspace):
    secret = workspace / 'keep.png'
    secret.write_bytes(_png())
    with pytest.raises(gallery_state.GalleryError):
        gallery.delete_image('../keep.png', 'shared')
    assert secret.read_bytes() == _png()


def test_render_list_empty(workspace):
    assert 'No owned images' in gallery.render_list('shared')


def test_render_list_shows_names_and_identity(workspace):
    image = _put('shot.png')
    rendered = gallery.render_list('shared')
    assert 'shot.png' in rendered and 'KB' in rendered
    assert image['id'] in rendered and image['revision'] in rendered

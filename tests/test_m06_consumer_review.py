"""Independent M06 consumer review; owned transports only, no sockets."""
import base64
import io
import json

import pytest

from olympus import media, memory, tools
from olympus.gallery_state import GalleryError


@pytest.fixture
def transport(monkeypatch, tmp_path):
    from PIL import Image
    buffer = io.BytesIO()
    Image.new('RGB', (4, 3), 'green').save(buffer, format='PNG')
    image = buffer.getvalue()
    calls = []
    monkeypatch.setenv('OPENAI_API_KEY', 'owned-review-fixture')
    monkeypatch.setenv('OLYMPUS_EXEC_WORKDIR', str(tmp_path / 'workspace'))
    monkeypatch.delenv('OLYMPUS_SHADOW_MODE', raising=False)

    class Response:
        def __init__(self, raw): self.raw = raw
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, bound=-1): return self.raw if bound < 0 else self.raw[:bound]

    def request(req, timeout):
        body = json.loads(req.data)
        calls.append((req.full_url, body))
        if req.full_url.endswith('/images/generations'):
            return Response(json.dumps({'data': [{'b64_json': base64.b64encode(image).decode()}]}).encode())
        assert req.full_url.endswith('/chat/completions')
        return Response(b'{"choices":[{"message":{"content":"Owned green image"}}]}')

    monkeypatch.setattr(media.urllib.request, 'urlopen', request)
    return calls, image


def test_resolved_generation_to_real_vision_payload_and_owner_refusal(transport):
    calls, raw = transport
    generate = tools.resolve_handler('generate_image')
    analyze = tools.resolve_handler('analyze_image')
    with memory.user_context('Owner:Exact Case'):
        result = generate(prompt='owned green fixture', operation_id='ab' * 16)
        assert result['status'] == 'complete'
        record = result['image']
        answer = analyze(image=record['name'], question='What color?',
                         gallery_id=record['id'], gallery_revision=record['revision'])
        assert answer == 'Owned green image'
        assert generate(operation_id='ab' * 16, mode='recover')['status'] == 'complete'
    assert len(calls) == 2
    content = calls[1][1]['messages'][0]['content']
    assert content[0]['text'] == 'What color?'
    assert content[1]['image_url']['url'] == 'data:image/png;base64,' + base64.b64encode(raw).decode()
    with memory.user_context('Owner-Exact-Case'):
        assert analyze(image=record['name'], gallery_id=record['id'],
                       gallery_revision=record['revision']).startswith('Error:')
        assert generate(operation_id='ab' * 16, mode='recover')['code'] == 'missing'
    assert len(calls) == 2


@pytest.mark.parametrize('changes', [
    {'filename': True}, {'prompt': True}, {'operation_id': 12},
    {'operation_id': 'A' * 32}, {'filename': 'bad.png\r\nX: injected'},
    {'filename': 'nul.png'}, {'filename': 'drive:file.png'},
])
def test_real_tool_admission_zero_transport(transport, changes):
    values = {'prompt': 'fixture', 'operation_id': 'cd' * 16}
    values.update(changes)
    with memory.user_context('review-owner'):
        result = tools.resolve_handler('generate_image')(**values)
    assert result['status'] == 'error'
    assert not transport[0]


def test_resolved_shadow_image_mutations_never_reach_provider(transport, monkeypatch):
    monkeypatch.setenv('OLYMPUS_SHADOW_MODE', '1')
    with memory.user_context('review-owner'):
        tools.resolve_handler('generate_image')(prompt='fixture', operation_id='cd' * 16)
        tools.resolve_handler('edit_image')(prompt='fixture', source='source.png',
            source_id='ab' * 16, source_revision='cd' * 32, operation_id='ef' * 16)
    assert not transport[0]


def test_compatibility_error_preserves_supplied_operation_id(transport, monkeypatch):
    from olympus.gallery_state import Store
    operation_id = 'ef' * 16
    def publication_failure(*args, **kwargs):
        raise GalleryError('publication_unconfirmed', 'Owned publication acknowledgement lost', 503)
    monkeypatch.setattr(Store, 'finalize', publication_failure)
    monkeypatch.setattr(Store, 'recover', publication_failure)
    result = media.generate_image('fixture', owner='review-owner', operation_id=operation_id)
    assert operation_id in result
    assert len(transport[0]) == 1


@pytest.mark.parametrize('kind', ['generate', 'edit'])
def test_compatibility_missing_id_has_no_provider_work(transport, kind):
    if kind == 'generate':
        result = media.generate_image('fixture', owner='review-owner')
    else:
        result = media.edit_image('fixture', 'source.png', owner='review-owner',
                                  source_id='ab' * 16, source_revision='cd' * 32)
    assert result.startswith('Error:')
    assert 'operation ID' in result
    assert not transport[0]


@pytest.mark.parametrize('fmt,ending', [('JPEG', b'\xff\xd9'), ('GIF', b';'),
    ('PNG', b'\x00\x00\x00\x00IEND\xaeB`\x82')])
def test_decoder_rejects_trailing_content_even_with_repeated_end_marker(fmt, ending):
    from PIL import Image
    from olympus.image_validation import validate_image
    out = io.BytesIO()
    Image.new('RGB', (2, 2), 'red').save(out, format=fmt)
    with pytest.raises(GalleryError):
        validate_image(out.getvalue() + b'unexpected trailing content' + ending)


def test_valid_progressive_jpeg_marker_segments():
    from PIL import Image
    from olympus.image_validation import validate_image
    out = io.BytesIO()
    Image.new('RGB', (8, 7), 'orange').save(out, format='JPEG', progressive=True)
    raw = out.getvalue()
    # COM payload may legally contain EOI-looking bytes; never scan blindly
    # for the first byte pair rather than honoring length-bearing segments.
    comment = b'owned comment \xff\xd9 bytes'
    raw = raw[:2] + b'\xff\xfe' + (len(comment) + 2).to_bytes(2, 'big') + comment + raw[2:]
    assert validate_image(raw)['mime'] == 'image/jpeg'


def test_valid_animated_gif_extensions_and_local_tables():
    from PIL import Image
    from olympus.image_validation import validate_image
    out = io.BytesIO()
    frames = [Image.new('RGB', (3, 2), color) for color in ('red', 'green', 'blue')]
    frames[0].save(out, format='GIF', save_all=True, append_images=frames[1:],
                   loop=0, duration=20, comment=b'owned ; trailer-like comment')
    assert validate_image(out.getvalue())['frames'] == 3


@pytest.mark.parametrize('fmt', ['PNG', 'JPEG', 'GIF'])
def test_plain_trailing_junk_is_refused(fmt):
    from PIL import Image
    from olympus.image_validation import validate_image
    out = io.BytesIO()
    Image.new('RGB', (2, 2), 'red').save(out, format=fmt)
    with pytest.raises(GalleryError):
        validate_image(out.getvalue() + b'junk')

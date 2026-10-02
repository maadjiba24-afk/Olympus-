"""M06 owned in-memory HTTP parser/authentication/response integration.

No sockets or live providers. Native browser/server coverage is a separate gate.
"""
import io
import json
import shutil
import subprocess
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import pytest
from olympus import config, gallery, media, memory, web
from olympus.gallery_state import GalleryError

ID = 'a' * 32
REV = 'b' * 64
IMAGE = {'name': 'shot.png', 'id': ID, 'revision': REV, 'bytes': 68, 'mime': 'image/png'}


@pytest.fixture()
def web_request(monkeypatch, tmp_path):
    monkeypatch.setattr(web, '_SESSIONS', {})
    monkeypatch.setattr(web, '_HITS', {})
    monkeypatch.setattr(config, 'MEMORY_DIR', tmp_path / 'memory')
    monkeypatch.setenv('OLYMPUS_EXEC_WORKDIR', str(tmp_path))
    monkeypatch.delenv('OLYMPUS_ACCESS_TOKEN', raising=False)
    monkeypatch.setattr(web.accounts, 'require_login', lambda: True)
    monkeypatch.setattr(web.accounts, 'namespace_for_token', lambda token: {
        'cookie-a': 'Case.Owner', 'cookie-b': 'Case-Owner'}.get(token))
    def call(payload=None, *, method='POST', cookie='cookie-a', path='/api/gallery', raw=None, length=None):
        handler = object.__new__(web.Handler)
        handler.path = path
        handler.client_address = ('127.0.0.1', 12345)
        handler.server = SimpleNamespace(server_address=('127.0.0.1', 8123))
        handler.headers = Message()
        handler.headers['Cookie'] = 'olympus_sid=' + cookie
        raw = raw if raw is not None else json.dumps(payload).encode()
        handler.headers['Content-Length'] = str(len(raw)) if length is None else length
        handler.headers['Content-Type'] = 'application/json'
        handler.rfile = io.BytesIO(raw)
        handler.wfile = io.BytesIO()
        result = {'headers': {}}
        handler.send_response = lambda code: result.update(code=code)
        handler.send_header = lambda key, value: result['headers'].update({key: value})
        handler.end_headers = lambda: None
        getattr(handler, 'do_' + method)()
        body = handler.wfile.getvalue()
        result['body'] = json.loads(body) if result['headers']['Content-Type'] == 'application/json' else body
        assert result['headers']['Cache-Control'] == 'no-store'
        assert result['headers']['X-Content-Type-Options'] == 'nosniff'
        return result
    return call


def test_list_empty_and_legacy_hidden(web_request, tmp_path):
    assert web_request(method='GET')['body']['images'] == []
    (tmp_path / 'private.png').write_bytes(b'legacy')
    assert web_request(method='GET')['body']['images'] == []


def test_list_read_exact_owner_and_content_identity(web_request, monkeypatch):
    seen = []
    def listing(owner):
        seen.append(owner)
        return {'status': 'ok', 'images': [IMAGE], 'unclaimed': False}
    monkeypatch.setattr(gallery, 'list_result', listing)
    assert web_request(method='GET')['body']['images'][0]['revision'] == REV
    def read(name, owner, **kw):
        assert (name, owner, kw) == ('shot.png', 'Case.Owner', {'expected_id': ID, 'expected_revision': REV})
        return b'owned-bytes', 'image/png'
    monkeypatch.setattr(gallery, 'read_image', read)
    out = web_request(method='GET', path=f'/api/gallery/image?name=shot.png&expected_id={ID}&expected_revision={REV}')
    assert out['body'] == b'owned-bytes'
    assert out['headers']['X-Gallery-Content-ID'] == ID
    assert seen == ['Case.Owner']


def test_image_read_requires_revision(web_request):
    assert web_request(method='GET', path='/api/gallery/image?name=shot.png')['code'] == 400


@pytest.mark.parametrize('payload', [[], None, {'op': []}, {'op': 'claim'}, {'op': 'edit', 'prompt': 5},
    {'op': 'delete', 'name': 'shot.png'}, {'op': 'recover', 'operation_id': ID, 'owner': 'victim'},
    {'op': 'recover', 'operation_id': ID, 'session': []}, {'op': 'recover', 'operation_id': True}])
def test_invalid_inputs_refused(web_request, payload):
    assert web_request(payload)['code'] == 400


@pytest.mark.parametrize('raw,length', [(b'{"op":"edit","op":"delete"}', None),
    (b'{"prompt":NaN}', None), (b'{}', '-1'), (b'{}', '999999'), (b'{}', '3')])
def test_strict_json(web_request, raw, length):
    assert web_request(raw=raw, length=length)['code'] == 400


def mutation(op='delete'):
    out = {'op': op, 'name': 'shot.png', 'expected_id': ID, 'expected_revision': REV, 'operation_id': ID}
    if op == 'edit': out['prompt'] = 'make it blue'
    return out


def test_delete_expected_identity_and_stale_conflict(web_request, monkeypatch):
    def remove(name, owner, **kw):
        assert owner == 'Case.Owner' and kw['expected_revision'] == REV
        raise GalleryError('conflict', 'stale revision', 409, kw['operation_id'])
    monkeypatch.setattr(gallery, 'delete_image', remove)
    out = web_request(mutation())
    assert out['code'] == 409 and out['body']['operation_id'] == ID


def test_edit_explicit_owner_and_ambient_restoration(web_request, monkeypatch):
    def edit(prompt, source, **kw):
        assert kw == {'owner': 'Case.Owner', 'source_id': ID, 'source_revision': REV, 'operation_id': ID}
        assert memory.current_owner() == 'Outer.Exact'
        return {'status': 'complete', 'operation_id': ID, 'image': IMAGE}
    monkeypatch.setattr(media, 'edit_image_result', edit)
    with memory.user_context('Outer.Exact'):
        out = web_request(mutation('edit'))
        assert memory.current_owner() == 'Outer.Exact'
    assert out['code'] == 200


@pytest.mark.parametrize('status,code', [('complete', 200), ('deleted', 200), ('reserved', 202),
                                        ('indeterminate', 202), ('failed', 502)])
def test_status_and_recover_typed_outcomes(web_request, monkeypatch, status, code):
    def result(operation_id, owner):
        assert operation_id == ID and owner == 'Case.Owner'
        return {'status': status, 'operation_id': ID}
    monkeypatch.setattr(gallery, 'operation_status', result)
    monkeypatch.setattr(gallery, 'recover_operation', result)
    for op in ('status', 'recover'):
        assert web_request({'op': op, 'operation_id': ID})['code'] == code


def test_unavailable_not_empty_and_auth_preserved(web_request, monkeypatch):
    def unavailable(owner):
        raise GalleryError('unavailable', 'Store unreadable', 503)
    monkeypatch.setattr(gallery, 'list_result', unavailable)
    out = web_request(method='GET')
    assert out['code'] == 503 and 'images' not in out['body']
    assert web_request(method='GET', cookie='unknown')['code'] == 401


def test_owned_gallery_dom_contract():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for owned DOM/fetch execution; native browser gate remains separate')
    source = Path(web.__file__).read_text()
    script = source.split('// --- gallery (workspace images) ---', 1)[1].split('// --- agenda ', 1)[0]
    fixture = Path(__file__).parent / 'fixtures' / 'gallery_ui_owned.cjs'
    result = subprocess.run([node, str(fixture)], input=script, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'OWNED_GALLERY_UI_PASSED' in result.stdout


@pytest.fixture()
def native_server(monkeypatch, tmp_path):
    """Real transport coverage: execute only on an authorized native/CI route."""
    import threading
    from http.server import ThreadingHTTPServer
    monkeypatch.setattr(web, '_SESSIONS', {})
    monkeypatch.setattr(web, '_HITS', {})
    monkeypatch.setattr(config, 'MEMORY_DIR', tmp_path / 'memory')
    monkeypatch.setenv('OLYMPUS_EXEC_WORKDIR', str(tmp_path))
    monkeypatch.delenv('OLYMPUS_ACCESS_TOKEN', raising=False)
    monkeypatch.setattr(web.accounts, 'require_login', lambda: True)
    monkeypatch.setattr(web.accounts, 'namespace_for_token', lambda token: 'Case.Owner' if token == 'owned-cookie' else None)
    srv = ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{srv.server_address[1]}', tmp_path
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def native_call(server, path='/api/gallery', payload=None):
    import urllib.request
    import urllib.error
    req = urllib.request.Request(server[0] + path, headers={'Cookie': 'olympus_sid=owned-cookie', 'Content-Type': 'application/json'},
        data=None if payload is None else json.dumps(payload).encode())
    try:
        response = urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        body = response.read()
        return response.status, json.loads(body) if response.headers['Content-Type'] == 'application/json' else body, response.headers


def test_native_http_page_and_empty_gallery(native_server):
    assert b'renderGallery' in native_call(native_server, '/')[1]
    code, result, headers = native_call(native_server)
    assert code == 200 and result['images'] == []
    assert headers['Cache-Control'] == 'no-store'
    assert headers['X-Content-Type-Options'] == 'nosniff'


def test_native_http_legacy_hidden_and_bad_read(native_server):
    (native_server[1] / 'private.png').write_bytes(b'legacy')
    assert native_call(native_server)[1]['images'] == []
    code, _, _ = native_call(native_server, f'/api/gallery/image?name=../secret.png&expected_id={ID}&expected_revision={REV}')
    assert code in (400, 404)


def test_native_http_read_response_metadata(native_server, monkeypatch):
    monkeypatch.setattr(gallery, 'read_image', lambda *a, **kw: (b'owned-image', 'image/png'))
    code, raw, headers = native_call(native_server, f'/api/gallery/image?name=shot.png&expected_id={ID}&expected_revision={REV}')
    assert code == 200 and raw == b'owned-image'
    assert headers['X-Gallery-Revision'] == REV
    assert headers['Content-Type'] == 'image/png'


def test_native_http_delete_edit_and_unknown(native_server, monkeypatch):
    monkeypatch.setattr(gallery, 'delete_image', lambda *a, **kw: {'status': 'deleted', 'operation_id': ID})
    monkeypatch.setattr(media, 'edit_image_result', lambda *a, **kw: {'status': 'complete', 'operation_id': ID})
    assert native_call(native_server, payload=mutation())[1]['status'] == 'deleted'
    assert native_call(native_server, payload=mutation('edit'))[1]['status'] == 'complete'
    assert native_call(native_server, payload={'op': 'unknown'})[0] == 400


def seed_owned_image(owner, name='shot.png'):
    """Real validation/publication, with a locally generated 1px PNG fixture."""
    import struct
    import zlib
    import uuid
    from olympus.gallery_state import Store
    def chunk(kind, body):
        return struct.pack('!I', len(body)) + kind + body + struct.pack('!I', zlib.crc32(kind + body))
    raw = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
    raw += chunk(b'IDAT', zlib.compress(b'\x00\xff\x00\x00')) + chunk(b'IEND', b'')
    operation_id = uuid.uuid4().hex
    store = Store(owner)
    store.reserve(operation_id, 'generate', {'owned': True}, name=name)
    store.start(operation_id)
    receipt = store.finalize(operation_id, raw)
    return raw, receipt['image']


def test_real_store_web_lifecycle_and_owner_refusal(web_request):
    raw, image = seed_owned_image('Case.Owner')
    assert web_request(method='GET')['body']['images'] == [image]
    path = '/api/gallery/image?name=shot.png&expected_id=' + image['id'] + '&expected_revision=' + image['revision']
    assert web_request(method='GET', path=path)['body'] == raw
    assert web_request(method='GET', path=path, cookie='cookie-b')['code'] == 404
    removal = {'op': 'delete', 'name': image['name'], 'expected_id': image['id'],
               'expected_revision': image['revision'], 'operation_id': ID}
    assert web_request(removal)['body']['status'] == 'deleted'
    assert web_request(removal)['body']['status'] == 'deleted'
    assert web_request(method='GET')['body']['images'] == []


def test_native_http_real_store_lifecycle(native_server):
    raw, image = seed_owned_image('Case.Owner')
    assert native_call(native_server)[1]['images'] == [image]
    path = '/api/gallery/image?name=shot.png&expected_id=' + image['id'] + '&expected_revision=' + image['revision']
    assert native_call(native_server, path)[1] == raw
    removal = {'op': 'delete', 'name': image['name'], 'expected_id': image['id'],
               'expected_revision': image['revision'], 'operation_id': ID}
    assert native_call(native_server, payload=removal)[1]['status'] == 'deleted'
    assert native_call(native_server)[1]['images'] == []


def test_recovery_accepts_existing_claim_id_without_claim_authority(web_request, monkeypatch):
    operation_id = 'claim-' + REV
    def recover(op_id, owner):
        assert op_id == operation_id and owner == 'Case.Owner'
        return {'status': 'complete', 'operation_id': op_id}
    monkeypatch.setattr(gallery, 'recover_operation', recover)
    assert web_request({'op': 'recover', 'operation_id': operation_id})['code'] == 200
    assert web_request({'op': 'claim', 'operation_id': operation_id})['code'] == 400

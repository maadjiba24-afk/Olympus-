"""Phase 3 — the gallery over the web UI: list endpoint, path-confined image
serving, and the panel wired into the page. The gallery reflects the single
confined workspace, so it is server-scoped (not per-session) like the images
the assistant generates there.
"""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from olympus import config, web


_PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
        b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


@pytest.fixture()
def server(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "_SESSIONS", {})
    monkeypatch.setattr(web, "_HITS", {})
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(ws))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", ws
    srv.shutdown()


def test_page_has_gallery_surface(server):
    url, _ = server
    html = urllib.request.urlopen(url + "/").read().decode()
    assert "🖼 gallery" in html and "/api/gallery" in html
    assert "renderGallery" in html


def test_list_empty(server):
    url, _ = server
    out = json.loads(urllib.request.urlopen(url + "/api/gallery?session=s1").read())
    assert out["images"] == []


def test_list_and_serve_image(server):
    url, ws = server
    (ws / "shot.png").write_bytes(_PNG)
    out = json.loads(urllib.request.urlopen(url + "/api/gallery?session=s1").read())
    assert [im["name"] for im in out["images"]] == ["shot.png"]
    resp = urllib.request.urlopen(url + "/api/gallery/image?name=shot.png&session=s1")
    assert resp.read() == _PNG
    assert resp.headers["Content-Type"] == "image/png"


def test_serve_missing_is_404(server):
    url, _ = server
    try:
        urllib.request.urlopen(url + "/api/gallery/image?name=ghost.png&session=s1")
        assert False, "expected 404"
    except urllib.error.HTTPError as e:
        assert e.code == 404


def test_serve_rejects_traversal(server):
    url, ws = server
    (ws.parent / "secret.png").write_bytes(_PNG)
    try:
        urllib.request.urlopen(
            url + "/api/gallery/image?name=../secret.png&session=s1")
        assert False, "expected 404"
    except urllib.error.HTTPError as e:
        assert e.code == 404

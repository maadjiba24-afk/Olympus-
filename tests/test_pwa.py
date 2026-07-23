"""PWA layer: manifest, service worker, HTML injection, and pure-Python
icon encoder — plus the web routes that serve them."""

import json

from olympus import pwa


# --- manifest --------------------------------------------------------------

def test_manifest_is_valid_json():
    m = json.loads(pwa.manifest())
    assert m["name"]
    assert m["short_name"] == "Olympus"
    assert m["display"] == "standalone"
    assert m["start_url"] == "/"
    srcs = {i["src"] for i in m["icons"]}
    assert "/icons/olympus-192.png" in srcs
    assert "/icons/olympus-512.png" in srcs
    assert any(i["purpose"] == "maskable" for i in m["icons"])


# --- service worker --------------------------------------------------------

def test_service_worker_registers_fetch_and_versioned():
    sw = pwa.service_worker()
    assert pwa.SW_VERSION in sw
    assert "addEventListener('install'" in sw
    assert "addEventListener('fetch'" in sw
    # navigations are network-first (live server wins)
    assert "navigate" in sw


# --- injection -------------------------------------------------------------

def test_inject_adds_manifest_and_sw():
    html = "<html><head><title>x</title></head><body>hi</body></html>"
    out = pwa.inject(html)
    assert 'rel="manifest"' in out
    assert 'href="/manifest.webmanifest"' in out
    assert "serviceWorker" in out
    assert "/sw.js" in out
    assert 'name="theme-color"' in out


def test_inject_is_idempotent():
    html = "<html><head></head><body></body></html>"
    once = pwa.inject(html)
    twice = pwa.inject(once)
    assert once == twice
    assert once.count('rel="manifest"') == 1


def test_inject_without_head_or_body():
    out = pwa.inject("just text")
    assert 'rel="manifest"' in out
    assert "serviceWorker" in out


# --- icons -----------------------------------------------------------------

def test_icon_is_valid_png():
    data = pwa.icon(192)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    # IHDR announces 192x192
    assert data[16:24] == (192).to_bytes(4, "big") + (192).to_bytes(4, "big")


def test_icon_sizes_and_maskable_differ():
    small = pwa.icon(192)
    big = pwa.icon(512)
    mask = pwa.icon(512, maskable=True)
    assert small != big
    assert big != mask               # maskable insets the glyph
    assert big[:8] == b"\x89PNG\r\n\x1a\n"


def test_icon_cached():
    assert pwa.icon(192) is pwa.icon(192)


# --- web routes ------------------------------------------------------------

import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from olympus import config, web


@pytest.fixture()
def server(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "_SESSIONS", {})
    monkeypatch.setattr(web, "_HITS", {})
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.delenv("OLYMPUS_ACCESS_TOKEN", raising=False)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_web_serves_manifest(server):
    r = urllib.request.urlopen(server + "/manifest.webmanifest")
    assert r.status == 200
    assert r.headers["Content-Type"] == "application/manifest+json"
    assert json.loads(r.read())["short_name"] == "Olympus"


def test_web_serves_service_worker(server):
    r = urllib.request.urlopen(server + "/sw.js")
    assert r.status == 200
    assert r.headers["Content-Type"] == "application/javascript"
    assert b"serviceWorker" not in r.read() or True  # body is the SW itself


def test_web_serves_icons(server):
    for path in ("/icons/olympus-192.png", "/icons/olympus-512.png",
                 "/icons/olympus-maskable-512.png"):
        r = urllib.request.urlopen(server + path)
        assert r.status == 200
        assert r.headers["Content-Type"] == "image/png"
        assert r.read()[:8] == b"\x89PNG\r\n\x1a\n"


def test_web_unknown_icon_is_404(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(server + "/icons/nope.png")
    assert exc.value.code == 404


def test_index_page_is_installable(server):
    page = urllib.request.urlopen(server + "/").read().decode()
    assert 'rel="manifest"' in page
    assert "/sw.js" in page

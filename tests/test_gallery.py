"""Phase 3 — gallery. Surfaces images generated into the confined workspace so
the web UI can show them and the CLI can list them. Serving is path-confined
(no traversal), image-types only, and size-capped.
"""

import pytest

from olympus import gallery, sandbox


# A 1x1 PNG (valid header) is enough for the type/serve tests.
_PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
        b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


@pytest.fixture()
def workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path))
    return tmp_path


def _put(root, name, data=_PNG):
    (root / name).write_bytes(data)


# --- listing ---------------------------------------------------------------

def test_list_empty(workspace):
    assert gallery.list_images() == []


def test_list_only_images_newest_first(workspace):
    _put(workspace, "a.png")
    _put(workspace, "notes.txt", b"hello")
    _put(workspace, "b.jpg")
    import os
    # make b.jpg newer than a.png deterministically
    os.utime(workspace / "a.png", (1000, 1000))
    os.utime(workspace / "b.jpg", (2000, 2000))
    names = [im["name"] for im in gallery.list_images()]
    assert names == ["b.jpg", "a.png"]          # newest first, txt excluded


def test_list_reports_size(workspace):
    _put(workspace, "a.png")
    im = gallery.list_images()[0]
    assert im["bytes"] == len(_PNG)


# --- reading / serving -----------------------------------------------------

def test_read_image_returns_bytes_and_type(workspace):
    _put(workspace, "pic.png")
    raw, ctype = gallery.read_image("pic.png")
    assert raw == _PNG and ctype == "image/png"


def test_read_rejects_non_image(workspace):
    _put(workspace, "notes.txt", b"hello")
    assert gallery.read_image("notes.txt") is None


def test_read_rejects_missing(workspace):
    assert gallery.read_image("nope.png") is None


def test_read_rejects_traversal(workspace, tmp_path):
    secret = tmp_path.parent / "secret.png"
    secret.write_bytes(_PNG)
    assert gallery.read_image("../secret.png") is None


def test_read_rejects_oversize(workspace, monkeypatch):
    monkeypatch.setattr(gallery, "_MAX_SERVE_BYTES", 10)
    _put(workspace, "big.png")
    assert gallery.read_image("big.png") is None


# --- delete ----------------------------------------------------------------

def test_delete_removes_image(workspace):
    _put(workspace, "gone.png")
    assert gallery.delete_image("gone.png") is True
    assert not (workspace / "gone.png").exists()


def test_delete_rejects_traversal(workspace, tmp_path):
    secret = tmp_path.parent / "keep.png"
    secret.write_bytes(_PNG)
    assert gallery.delete_image("../keep.png") is False
    assert secret.exists()


def test_delete_missing_is_false(workspace):
    assert gallery.delete_image("nope.png") is False


# --- render ----------------------------------------------------------------

def test_render_list_empty(workspace):
    assert "No images" in gallery.render_list()


def test_render_list_shows_names(workspace):
    _put(workspace, "shot.png")
    out = gallery.render_list()
    assert "shot.png" in out and "KB" in out

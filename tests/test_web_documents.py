"""Phase 3 — the documents workspace over the web UI: list / read / save /
delete endpoints, per-session isolation, and the panel wired into the page.
The user editing their own doc in the UI saves directly (they clicked Save);
the agent's write_document tool is what routes through the approval spine.
"""

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from olympus import config, documents, web


@pytest.fixture()
def server(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "_SESSIONS", {})
    monkeypatch.setattr(web, "_HITS", {})
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _get(url, path):
    return json.loads(urllib.request.urlopen(url + path).read())


def _post(url, path, payload):
    req = urllib.request.Request(
        url + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req).read())


def test_page_has_documents_surface(server):
    html = urllib.request.urlopen(server + "/").read().decode()
    assert "📄 docs" in html and "/api/documents" in html
    assert "renderDocuments" in html


def test_list_empty(server):
    out = _get(server, "/api/documents?session=s1")
    assert out["documents"] == []


def test_save_then_list_and_read(server):
    out = _post(server, "/api/documents",
                {"session": "s1", "op": "save", "name": "Plan",
                 "content": "# Plan\nstep 1"})
    assert out["ok"] is True
    names = [d["name"] for d in out["documents"]]
    assert "Plan" in names
    doc = _get(server, "/api/documents?name=Plan&session=s1")
    assert doc["content"] == "# Plan\nstep 1"


def test_save_is_per_session_isolated(server):
    _post(server, "/api/documents",
          {"session": "sA", "op": "save", "name": "Mine", "content": "secret"})
    assert _get(server, "/api/documents?session=sB")["documents"] == []


def test_delete(server):
    _post(server, "/api/documents",
          {"session": "s1", "op": "save", "name": "Temp", "content": "x"})
    out = _post(server, "/api/documents",
                {"session": "s1", "op": "delete", "name": "Temp"})
    assert out["ok"] is True and out["documents"] == []


def test_save_requires_name(server):
    try:
        _post(server, "/api/documents",
              {"session": "s1", "op": "save", "name": "", "content": "x"})
        assert False, "expected 400"
    except urllib.error.HTTPError as e:
        assert e.code == 400


def test_read_missing_is_404(server):
    try:
        _get(server, "/api/documents?name=ghost&session=s1")
        assert False, "expected 404"
    except urllib.error.HTTPError as e:
        assert e.code == 404


def test_web_save_writes_directly_no_pending_action(server):
    """UI save is a direct user action — it must NOT leave a pending
    write_document action for the user to re-approve."""
    from olympus import actions
    _post(server, "/api/documents",
          {"session": "sD", "op": "save", "name": "Direct", "content": "hi"})
    user = web._user_for("sD")
    assert documents.read(user, "Direct") == "hi"
    assert [a for a in actions.pending(user) if a.type == "write_document"] == []

"""Gap 1 — notes/todos/reminders over the web UI: the /api/todos add/complete/
delete endpoint, agenda integration (open items + overdue flag), and the panel
wired into the page.
"""

import json
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


def test_page_has_list_surface(server):
    html = urllib.request.urlopen(server + "/").read().decode()
    assert "/api/todos" in html and "renderAgenda" in html
    assert "Your list" in html


def test_add_then_agenda_shows_it(server):
    out = _post(server, "/api/todos",
                {"session": "s1", "op": "add", "text": "buy milk"})
    assert out["ok"] is True
    ag = _get(server, "/api/agenda?session=s1")
    assert [t["text"] for t in ag["todos"]] == ["buy milk"]


def test_complete_removes_from_open_agenda(server):
    out = _post(server, "/api/todos",
                {"session": "s1", "op": "add", "text": "task"})
    tid = out["todos"][0]["id"]
    _post(server, "/api/todos", {"session": "s1", "op": "complete", "id": tid})
    ag = _get(server, "/api/agenda?session=s1")
    assert ag["todos"] == []            # agenda shows only open items


def test_overdue_reminder_flagged(server):
    _post(server, "/api/todos",
          {"session": "s1", "op": "add", "text": "overdue", "due": 100.0})
    ag = _get(server, "/api/agenda?session=s1")
    row = next(t for t in ag["todos"] if t["text"] == "overdue")
    assert row["overdue"] is True


def test_delete(server):
    out = _post(server, "/api/todos",
                {"session": "s1", "op": "add", "text": "temp"})
    tid = out["todos"][0]["id"]
    after = _post(server, "/api/todos",
                  {"session": "s1", "op": "delete", "id": tid})
    assert after["todos"] == []


def test_per_session_isolation(server):
    _post(server, "/api/todos", {"session": "sA", "op": "add", "text": "mine"})
    assert _get(server, "/api/agenda?session=sB")["todos"] == []

"""Tests for the web action-card surface (list / approve / reject / undo)."""

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from olympus import actions, builtin_actions, web  # noqa: F401


@pytest.fixture()
def server(monkeypatch):
    monkeypatch.setattr(web, "_SESSIONS", {})
    monkeypatch.setattr(web, "_HITS", {})
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


def test_page_has_actions_surface(server):
    html = urllib.request.urlopen(server + "/").read().decode()
    assert "📋 actions" in html and "/api/actions" in html and "renderCards" in html


def test_actions_endpoint_includes_budget(server):
    out = _get(server, "/api/actions?session=b1")
    assert "budget" in out and "enabled" in out["budget"]


def test_actions_endpoint_lists_prepared(server):
    # an action prepared for the web user shows up in the view
    actions.prepare(web._user_for("sess1"), "save_note",
                    {"title": "n", "body": "remember", "_user": "web-sess1"})
    out = _get(server, "/api/actions?session=sess1")
    assert len(out["actions"]) == 1
    assert out["actions"][0]["status"] == "prepared"
    # isolated per session
    assert _get(server, "/api/actions?session=other")["actions"] == []


def test_approve_via_web_executes(server):
    user = web._user_for("s2")
    actions.grant_scope(user, "notes")
    a = actions.prepare(user, "save_note",
                        {"title": "n", "body": "x", "_user": user})
    out = _post(server, "/api/action",
                {"session": "s2", "action_id": a.id, "op": "approve"})
    assert out["ok"] is True
    assert actions.get(user, a.id).status == actions.EXECUTED


def test_reject_via_web(server):
    user = web._user_for("s3")
    a = actions.prepare(user, "send_email",
                        {"to": "x@y.z", "subject": "h", "body": "b"})
    _post(server, "/api/action",
          {"session": "s3", "action_id": a.id, "op": "reject", "reason": "no"})
    assert actions.get(user, a.id).status == actions.REJECTED


def test_undo_via_web(server):
    user = web._user_for("s4")
    actions.grant_scope(user, "notes")
    a = actions.prepare(user, "save_note", {"title": "n", "body": "y", "_user": user})
    actions.approve(user, a.id)
    out = _post(server, "/api/action",
                {"session": "s4", "action_id": a.id, "op": "undo"})
    assert out["ok"] is True
    assert actions.get(user, a.id).status == actions.UNDONE


def test_unknown_op_rejected(server):
    user = web._user_for("s5")
    a = actions.prepare(user, "save_note", {"title": "n", "body": "z", "_user": user})
    import urllib.error
    with pytest.raises(urllib.error.HTTPError) as err:
        _post(server, "/api/action",
              {"session": "s5", "action_id": a.id, "op": "delete_everything"})
    assert err.value.code == 400


def test_edit_via_web_then_approve(server):
    user = web._user_for("s6")
    actions.grant_scope(user, "notes")
    a = actions.prepare(user, "save_note",
                        {"title": "n", "body": "draft", "_user": user},
                        why="you asked to keep this")
    # the view exposes why + editable payload (without internal keys)
    view = _get(server, "/api/actions?session=s6")["actions"][0]
    assert view["why"] == "you asked to keep this"
    assert "_user" not in view["payload"] and view["payload"]["body"] == "draft"
    # edit, then approve the edited version
    out = _post(server, "/api/action",
                {"session": "s6", "action_id": a.id, "op": "edit",
                 "changes": {"body": "final"}})
    assert out["ok"] is True
    assert actions.get(user, a.id).payload["body"] == "final"
    assert actions.get(user, a.id).status == actions.PREPARED
    _post(server, "/api/action",
          {"session": "s6", "action_id": a.id, "op": "approve"})
    assert actions.get(user, a.id).status == actions.EXECUTED


def test_edit_requires_object_changes(server):
    user = web._user_for("s7")
    a = actions.prepare(user, "save_note", {"title": "n", "body": "b", "_user": user})
    import urllib.error
    with pytest.raises(urllib.error.HTTPError) as err:
        _post(server, "/api/action",
              {"session": "s7", "action_id": a.id, "op": "edit",
               "changes": "not-a-dict"})
    assert err.value.code == 400


def test_stream_content_type_check_is_robust():
    # the browser must match the streamed reply's content-type tolerantly
    # ("text/plain; charset=utf-8"), not by brittle exact equality — otherwise
    # it parses the streamed text as JSON and errors. (regression)
    from olympus import web
    assert "includes('text/plain')" in web.PAGE
    assert "=== 'text/plain'" not in web.PAGE

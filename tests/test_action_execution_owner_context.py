"""Action callbacks execute only as the authenticated action owner.

All fixtures are local and synthetic.  Gmail and Calendar functions are
replaced with harmless recorders; no network or external service is used.
"""

from __future__ import annotations

import pytest

from olympus import actions, builtin_actions, calendar, config, gmail, memory  # noqa: F401


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    memory.set_user("shared")


def _register(monkeypatch, name, *, preview=None, execute=None, undo=None):
    action_type = actions.ActionType(
        name=name,
        risk_class=actions.IRREVERSIBLE,
        scope="",
        preview=preview or (lambda payload: "synthetic preview"),
        execute=execute or (lambda payload: {}),
        undo=undo,
    )
    monkeypatch.setitem(actions._REGISTRY, name, action_type)


def test_preview_execute_and_undo_use_owner_then_restore_ambient(monkeypatch):
    seen = []

    def preview(payload):
        seen.append(("preview", memory.current_user()))
        return "synthetic preview"

    def execute(payload):
        seen.append(("execute", memory.current_user()))
        return {"fixture": True}

    def undo(result):
        seen.append(("undo", memory.current_user()))
        return "undone"

    _register(monkeypatch, "synthetic_owner_context", preview=preview,
              execute=execute, undo=undo)
    memory.set_user("bob")

    action = actions.prepare("alice", "synthetic_owner_context", {})
    done = actions.approve("alice", action.id)
    undone = actions.undo("alice", action.id)

    assert done.status == actions.EXECUTED
    assert undone.status == actions.UNDONE
    assert seen == [
        ("preview", "alice"),
        ("preview", "alice"),
        ("execute", "alice"),
        ("undo", "alice"),
    ]
    assert memory.current_user() == "bob"


def test_failed_callback_restores_ambient_owner(monkeypatch):
    def fail(payload):
        assert memory.current_user() == "alice"
        raise RuntimeError("synthetic failure")

    _register(monkeypatch, "synthetic_owner_failure", execute=fail)
    memory.set_user("bob")
    action = actions.prepare("alice", "synthetic_owner_failure", {})

    done = actions.approve("alice", action.id)

    assert done.status == actions.FAILED
    assert done.error == "synthetic failure"
    assert memory.current_user() == "bob"


def test_gmail_action_uses_authenticated_owner_not_approver_context(monkeypatch):
    seen = []

    def fake_send(to, subject, body):
        seen.append(memory.current_user())
        return {"id": "synthetic-message"}

    monkeypatch.setattr(gmail, "send", fake_send)
    actions.grant_scope("alice", "gmail.send")
    memory.set_user("bob")
    action = actions.prepare(
        "alice", "gmail_send",
        {"to": "fixture@example.test", "subject": "Fixture", "body": "Hi"},
    )

    done = actions.approve("alice", action.id)

    assert done.status == actions.EXECUTED
    assert seen == ["alice"]
    assert memory.current_user() == "bob"


def test_calendar_execute_and_undo_use_authenticated_owner(monkeypatch):
    seen = []

    def fake_create(summary, start, end, attendees=None, description=""):
        seen.append(("create", memory.current_user()))
        return {"id": "synthetic-event"}

    def fake_delete(event_id):
        seen.append(("delete", memory.current_user()))
        return {}

    monkeypatch.setattr(calendar, "create_event", fake_create)
    monkeypatch.setattr(calendar, "delete_event", fake_delete)
    actions.grant_scope("alice", "calendar.events")
    memory.set_user("bob")
    action = actions.prepare(
        "alice", "calendar_create",
        {"summary": "Fixture", "start": "2026-01-01T00:00:00Z",
         "end": "2026-01-01T00:30:00Z"},
    )

    done = actions.approve("alice", action.id)
    undone = actions.undo("alice", action.id)

    assert done.status == actions.EXECUTED
    assert undone.status == actions.UNDONE
    assert seen == [("create", "alice"), ("delete", "alice")]
    assert memory.current_user() == "bob"

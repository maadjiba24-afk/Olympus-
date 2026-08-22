"""Approval previews must cover the exact irreversible payload that executes.

All fixtures are synthetic.  Network, browser, Gmail, calendar, and OS
actuators are never contacted.
"""

from __future__ import annotations

import pytest

from olympus import (actions, approvals, browser, builtin_actions, computeruse,
                     config, operator, tools)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    yield


def test_email_preview_and_executor_cover_the_same_full_body(monkeypatch):
    marker = "SYNTHETIC-HARMLESS-TAIL"
    body = "a" * 1_050 + marker
    sent = {}

    def fake_send(to, subject, delivered_body, *, _approved=False):
        sent.update(to=to, subject=subject, body=delivered_body,
                    approved=_approved)
        return "sent (synthetic fixture)"

    monkeypatch.setattr(tools, "_send_email", fake_send)
    actions.grant_scope("preview-user", "email")
    action = actions.prepare(
        "preview-user", "send_email",
        {"to": "person@example.test", "subject": "fixture", "body": body})

    assert marker in action.preview
    done = actions.approve("preview-user", action.id)
    assert done.status == actions.EXECUTED
    assert sent == {"to": "person@example.test", "subject": "fixture",
                    "body": body, "approved": True}


@pytest.mark.parametrize(
    "renderer,payload,marker",
    [
        (builtin_actions._gmail_send_preview,
         {"to": "person@example.test", "subject": "fixture",
          "body": "b" * 1_050 + "GMAIL-TAIL"},
         "GMAIL-TAIL"),
        (builtin_actions._cal_create_preview,
         {"summary": "fixture", "start": "s", "end": "e",
          "description": "c" * 350 + "CALENDAR-TAIL"},
         "CALENDAR-TAIL"),
        (builtin_actions._run_python_preview,
         {"code": "# harmless\n" + "x = 1\n" * 350 + "# PYTHON-TAIL"},
         "PYTHON-TAIL"),
    ],
)
def test_irreversible_builtin_previews_do_not_hide_old_truncation_tails(
        renderer, payload, marker):
    assert marker in renderer(payload)


def test_chat_approval_card_is_not_silently_truncated():
    marker = "CHAT-VISIBLE-TAIL"
    body = "d" * 450 + marker
    action = actions.prepare(
        "chat-user", "send_email",
        {"to": "person@example.test", "subject": "fixture", "body": body})

    assert marker in approvals.summary("chat-user")
    assert marker in approvals.footer("chat-user")
    assert action.id in approvals.summary("chat-user")


@pytest.mark.parametrize(
    "name,payload,marker",
    [
        ("computer_type", {"text": "typed " + "TYPE-VISIBLE-TAIL"},
         "TYPE-VISIBLE-TAIL"),
        ("computer_key", {"keys": "ctrl+shift+KEY-VISIBLE-TAIL"},
         "KEY-VISIBLE-TAIL"),
        ("computer_launch", {"command": "fixture --arg LAUNCH-VISIBLE-TAIL"},
         "LAUNCH-VISIBLE-TAIL"),
    ],
)
def test_computer_use_approval_preview_shows_exact_actuator_input(
        name, payload, marker):
    assert marker in computeruse._preview(name)(payload)


def test_operator_preview_resolves_the_value_that_will_be_filled():
    browser.set_template(
        "example.test", "synthetic-submit", "irreversible",
        [{"op": "fill", "selector": "#message", "value": "$message"},
         {"op": "click", "selector": "#submit"}])

    shown = operator.preview({
        "domain": "example.test",
        "template": "synthetic-submit",
        "params": {"message": "FORM-VISIBLE-TAIL"},
    })
    assert "fill #message with 'FORM-VISIBLE-TAIL'" in shown
    assert "click #submit" in shown


def test_changed_payload_cannot_execute_under_an_old_preview(monkeypatch):
    called = []
    monkeypatch.setattr(
        tools, "_send_email",
        lambda *args, **kwargs: called.append((args, kwargs)) or "sent")
    actions.grant_scope("mismatch-user", "email")
    action = actions.prepare(
        "mismatch-user", "send_email",
        {"to": "first@example.test", "subject": "fixture", "body": "first"})

    # Harmlessly model a corrupted/stale pending record: payload changes while
    # the human-visible preview remains the one for the original recipient.
    action.payload["to"] = "second@example.test"
    actions._save(action)

    done = actions.approve("mismatch-user", action.id)
    assert done.status == actions.FAILED
    assert "preview no longer matches" in done.error
    assert called == []


def test_previous_boundary_version_requires_a_fresh_review(monkeypatch):
    called = []
    monkeypatch.setattr(
        tools, "_send_email",
        lambda *args, **kwargs: called.append((args, kwargs)) or "sent")
    actions.grant_scope("legacy-user", "email")
    action = actions.prepare(
        "legacy-user", "send_email",
        {"to": "person@example.test", "subject": "fixture", "body": "safe"})
    action.boundary_version = 1
    actions._save(action)

    done = actions.approve("legacy-user", action.id)
    assert done.status == actions.FAILED
    assert "legacy trust-boundary format" in done.error
    assert called == []


def test_unrenderable_preview_is_never_queued(monkeypatch):
    name = "synthetic_preview_failure"

    def broken_preview(payload):
        raise RuntimeError("synthetic render failure")

    actions.register(actions.ActionType(
        name=name, risk_class=actions.IRREVERSIBLE, scope="",
        preview=broken_preview, execute=lambda payload: {"should_not": "run"}))
    try:
        with pytest.raises(ValueError, match="could not render action preview"):
            actions.prepare("render-user", name, {"value": "harmless"})
        assert actions.pending("render-user") == []
    finally:
        actions._REGISTRY.pop(name, None)

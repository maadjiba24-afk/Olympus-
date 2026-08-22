"""Authenticated action owners, never caller payloads, select a tenant.

The fixtures use local temporary storage and fake gates only.  No browser,
network, payment, assessment, or OS actuator is contacted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from olympus import (actions, browser, builtin_actions,  # noqa: F401
                     computeruse, config, operator)      # noqa: F401


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    browser.set_transport_factory(None)
    yield
    browser.set_transport_factory(None)


def test_declared_owner_is_derived_when_omitted_or_forged():
    action_type = actions.ActionType(
        name="synthetic_owner_binding", risk_class=actions.IRREVERSIBLE,
        scope="", preview=lambda payload: "synthetic preview",
        execute=lambda payload: {}, binds_user=True)

    omitted = actions._canonical_payload("alice", action_type, {"value": 1})
    forged = actions._canonical_payload(
        "alice", action_type, {"value": 1, "_user": "bob"})

    assert omitted == {"value": 1, "_user": "alice"}
    assert forged == {"value": 1, "_user": "alice"}


def test_owner_agnostic_action_remains_owner_agnostic():
    action_type = actions.ActionType(
        name="synthetic_ownerless", risk_class=actions.IRREVERSIBLE,
        scope="", preview=lambda payload: "synthetic preview",
        execute=lambda payload: {})

    assert actions._canonical_payload("alice", action_type, {"value": 1}) == {
        "value": 1}


def test_direct_note_prepare_cannot_fall_back_to_shared_storage():
    actions.grant_scope("alice", "notes")
    action = actions.prepare(
        "alice", "save_note", {"title": "fixture", "body": "alice only"})

    assert action.payload["_user"] == "alice"
    done = actions.approve("alice", action.id)
    assert done.status == actions.EXECUTED
    assert config.MEMORY_DIR / "notes" / "alice" in Path(
        done.result["path"]).parents
    assert not (config.MEMORY_DIR / "notes" / "shared").exists()


@pytest.mark.parametrize("name", [
    "save_note",
    "authorize_payment",
    "write_document",
    "authorize_assessment",
    "browser_operate",
    "browser_operate_irreversible",
    "browser_operate_financial",
    "computer_screenshot",
    "computer_move",
    "computer_click",
    "computer_type",
    "computer_key",
    "computer_launch",
])
def test_every_builtin_owner_consumer_declares_binding(name):
    assert actions.registered()[name].binds_user is True


def test_forged_public_operator_user_is_ignored(monkeypatch):
    browser.set_template(
        "example.test", "synthetic-click", "irreversible",
        [{"op": "click", "selector": "#synthetic"}])
    actions.grant_scope("alice", operator.OPERATE_SCOPE)
    seen_users = []

    def fake_gate(user, domain):
        seen_users.append(user)
        return "synthetic stop before browser actuation"

    monkeypatch.setattr(operator, "_gate", fake_gate)
    action = actions.prepare(
        "alice", "browser_operate_irreversible",
        {"domain": "example.test", "template": "synthetic-click",
         "params": {}, "user": "bob", "_user": "bob"})

    assert action.user == "alice"
    assert action.payload["_user"] == "alice"
    done = actions.approve("alice", action.id)
    assert done.status == actions.FAILED
    assert seen_users == ["alice"]
    assert "synthetic stop" in done.error


def test_direct_computer_action_gets_authenticated_audit_owner():
    action = actions.prepare("alice", "computer_click", {"x": 1, "y": 2})
    assert action.payload["_user"] == "alice"

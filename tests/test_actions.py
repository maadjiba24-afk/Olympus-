"""Tests for the Action spine — prepare → approve → execute → undo → log."""

import pytest

from olympus import actions
from olympus import builtin_actions  # noqa: F401  (registers built-in types)


@pytest.fixture()
def reg(monkeypatch):
    """A clean registry with a couple of controllable test action types."""
    monkeypatch.setattr(actions, "_REGISTRY", {})
    executed = {"count": 0}

    actions.register(actions.ActionType(
        name="t_note", risk_class=actions.TRIVIAL, scope="notes",
        preview=lambda p: f"note: {p.get('body','')}",
        execute=lambda p: (executed.__setitem__("count", executed["count"] + 1)
                           or {"saved": p.get("body", "")}),
        undo=lambda r: "deleted"))
    actions.register(actions.ActionType(
        name="t_send", risk_class=actions.IRREVERSIBLE, scope="email",
        preview=lambda p: f"send to {p.get('to','')}",
        execute=lambda p: {"sent": True}))
    actions.register(actions.ActionType(
        name="t_pay", risk_class=actions.FINANCIAL_LEGAL, scope="pay",
        preview=lambda p: f"pay {p.get('amount','')}",
        execute=lambda p: {"paid": True}))
    return executed


# --- core lifecycle ------------------------------------------------------

def test_prepare_does_not_execute(reg):
    a = actions.prepare("u", "t_send", {"to": "x@y.z"})
    assert a.status == actions.PREPARED
    assert reg["count"] == 0          # nothing ran
    assert "send to x@y.z" in a.preview


def test_approve_executes(reg):
    actions.grant_scope("u", "email")
    a = actions.prepare("u", "t_send", {"to": "x@y.z"})
    done = actions.approve("u", a.id)
    assert done.status == actions.EXECUTED
    assert done.result == {"sent": True}


def test_reject_records_feedback(reg):
    from olympus import memory
    a = actions.prepare("u", "t_send", {"to": "x@y.z"})
    actions.reject("u", a.id, "wrong recipient")
    assert actions.get("u", a.id).status == actions.REJECTED
    memory.set_user("u")
    assert "rejected action" in memory.recent("feedback")


def test_undo_reverses_executed_action(reg):
    actions.grant_scope("u", "notes")
    a = actions.prepare("u", "t_note", {"body": "hi", "_user": "u"})
    actions.approve("u", a.id)
    undone = actions.undo("u", a.id)
    assert undone.status == actions.UNDONE


def test_irreversible_action_cannot_be_undone(reg):
    actions.grant_scope("u", "email")
    a = actions.prepare("u", "t_send", {"to": "x@y.z"})
    actions.approve("u", a.id)
    with pytest.raises(ValueError, match="not reversible"):
        actions.undo("u", a.id)


# --- the safety invariants (the whole point) -----------------------------

def test_scope_gate_blocks_execution(reg):
    # no scope granted -> approval still won't execute
    a = actions.prepare("u", "t_send", {"to": "x@y.z"})
    done = actions.approve("u", a.id)
    assert done.status == actions.FAILED
    assert "not granted" in done.error


def test_financial_action_never_auto_executes(reg):
    actions.grant_scope("u", "pay")
    actions.set_autonomy("u", actions.L4_STANDING)   # highest autonomy
    a = actions.prepare("u", "t_pay", {"amount": 100})
    held = actions.auto_or_hold(a)
    assert held.status == actions.PREPARED           # still needs explicit approval
    assert not actions.can_auto_execute(a)


def test_irreversible_action_never_auto_executes(reg):
    actions.grant_scope("u", "email")
    actions.set_autonomy("u", actions.L4_STANDING)
    a = actions.prepare("u", "t_send", {"to": "x@y.z"})
    assert not actions.can_auto_execute(a)
    assert actions.auto_or_hold(a).status == actions.PREPARED


def test_trivial_action_auto_executes_only_at_l3(reg):
    actions.grant_scope("u", "notes")
    # default autonomy L1 -> holds
    a1 = actions.prepare("u", "t_note", {"body": "a"})
    assert actions.auto_or_hold(a1).status == actions.PREPARED
    # L3 -> auto-runs trivial reversible action
    actions.set_autonomy("u", actions.L3_AUTO_SAFE)
    a2 = actions.prepare("u", "t_note", {"body": "b"})
    assert actions.auto_or_hold(a2).status == actions.EXECUTED


def test_auto_execute_still_requires_scope(reg):
    actions.set_autonomy("u", actions.L3_AUTO_SAFE)   # would auto-run...
    a = actions.prepare("u", "t_note", {"body": "x"})  # ...but no 'notes' scope
    assert not actions.can_auto_execute(a)
    assert actions.auto_or_hold(a).status == actions.PREPARED


def test_revoke_all_is_a_kill_switch(reg):
    actions.grant_scope("u", "email")
    actions.grant_scope("u", "notes")
    actions.revoke_all("u")
    assert actions.granted_scopes("u") == set()
    a = actions.prepare("u", "t_send", {"to": "x@y.z"})
    assert actions.approve("u", a.id).status == actions.FAILED


# --- audit log + isolation ----------------------------------------------

def test_audit_log_records_transitions(reg):
    actions.grant_scope("u", "email")
    a = actions.prepare("u", "t_send", {"to": "x@y.z"})
    actions.approve("u", a.id)
    from olympus import config
    import json
    log = config.MEMORY_DIR / "actions" / "u" / "audit.jsonl"
    events = [json.loads(l)["event"] for l in log.read_text().splitlines()]
    assert "prepared" in events and "approved" in events and "executed" in events


def test_per_user_action_isolation(reg):
    actions.prepare("alice", "t_send", {"to": "x@y.z"})
    assert len(actions.pending("alice")) == 1
    assert actions.pending("bob") == []


def test_initial_user_field_cannot_redirect_a_built_in_note():
    """End-to-end: Alice's approved action must never write into Bob's store."""
    from pathlib import Path

    from olympus import config

    actions.grant_scope("alice", "notes")
    a = actions.prepare(
        "alice", "save_note",
        {"title": "tenant-bound", "body": "alice only", "_user": "bob"})
    done = actions.approve("alice", a.id)

    assert done.status == actions.EXECUTED
    assert Path(done.result["path"]).parent == (
        config.MEMORY_DIR / "notes" / "alice")
    assert not (config.MEMORY_DIR / "notes" / "bob").exists()


# --- built-in types registered ------------------------------------------

def test_builtins_registered():
    names = actions.registered()
    assert {"send_email", "call_webhook", "save_note"} <= set(names)
    assert names["send_email"].risk_class == actions.IRREVERSIBLE
    assert names["save_note"].reversible is True
    assert names["send_email"].reversible is False


def test_prepare_action_tool_queues_not_executes():
    from olympus import tools, memory
    memory.set_user("toolu")
    msg = tools._prepare_action(
        "send_email",
        {"to": "a@b.c", "subject": "Hi", "body": "yo",
         "_user": "mallory"})
    assert "awaiting your approval" in msg
    assert len(actions.pending("toolu")) == 1
    assert actions.pending("toolu")[0].status == actions.PREPARED
    assert actions.pending("toolu")[0].payload["_user"] == "toolu"


# --- edit before approve (user control: Approve / Edit / Reject) ----------

def test_edit_updates_payload_and_preview(reg):
    a = actions.prepare("u", "t_send", {"to": "wrong@x.z"})
    edited = actions.edit("u", a.id, {"to": "right@x.z"})
    assert edited.status == actions.PREPARED       # still awaiting approval
    assert edited.payload["to"] == "right@x.z"
    assert "send to right@x.z" in edited.preview   # preview re-rendered
    assert reg["count"] == 0                       # editing never executes


def test_edit_cannot_touch_internal_fields(reg):
    a = actions.prepare("u", "t_note", {"body": "hi", "_user": "u"})
    edited = actions.edit("u", a.id, {"body": "new", "_user": "mallory"})
    assert edited.payload["_user"] == "u"          # internal key protected
    assert edited.payload["body"] == "new"


def test_prepare_replaces_all_caller_supplied_internal_fields(reg):
    """The initial payload is as untrusted as a later edit."""
    a = actions.prepare(
        "alice", "t_note",
        {"body": "hi", "_user": "mallory", "_forged_authority": "yes"})
    assert a.payload["_user"] == "alice"
    assert "_forged_authority" not in a.payload


def test_prepare_does_not_add_user_to_an_owner_agnostic_action(reg):
    a = actions.prepare("alice", "t_note", {"body": "hi"})
    assert a.payload == {"body": "hi"}


def test_prepare_requires_an_object_payload(reg):
    with pytest.raises(ValueError, match="payload must be an object"):
        actions.prepare("u", "t_note", ["not", "an", "object"])


def test_edit_only_prepared_actions(reg):
    actions.grant_scope("u", "email")
    a = actions.prepare("u", "t_send", {"to": "x@y.z"})
    actions.approve("u", a.id)
    with pytest.raises(ValueError, match="only prepared"):
        actions.edit("u", a.id, {"to": "other@y.z"})


def test_edit_is_audited(reg):
    from olympus import config
    import json
    a = actions.prepare("u", "t_send", {"to": "x@y.z"})
    actions.edit("u", a.id, {"to": "y@y.z"})
    log = config.MEMORY_DIR / "actions" / "u" / "audit.jsonl"
    events = [json.loads(l)["event"] for l in log.read_text().splitlines()]
    assert "edited" in events


def test_why_is_recorded_and_round_trips(reg):
    a = actions.prepare("u", "t_send", {"to": "x@y.z"},
                        why="you asked me to confirm the meeting")
    assert actions.get("u", a.id).why == "you asked me to confirm the meeting"


# --- run_python: Python execution rides the same spine as run_command ------

def test_run_python_prepares_then_executes_under_exec_scope(monkeypatch, tmp_path):
    """End-to-end: the built-in run_python action stages (never auto-runs),
    then executes the snippet through the sandbox once the exec scope is
    granted and the user approves — same posture as run_command."""
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    monkeypatch.setenv("OLYMPUS_EXEC_BACKEND", "local")
    assert "run_python" in actions.registered()

    # Never auto-executes, even at the highest autonomy (irreversible/exec).
    actions.grant_scope("shared", "exec")
    actions.set_autonomy("shared", actions.L4_STANDING)
    a = actions.prepare("shared", "run_python", {"code": "print(2 ** 10)"})
    assert a.status == actions.PREPARED
    assert not actions.can_auto_execute(a)

    # Explicit approval runs it and captures stdout.
    done = actions.approve("shared", a.id)
    assert done.status == actions.EXECUTED
    assert done.result["ok"] and "1024" in done.result["output"]


def test_run_python_without_exec_scope_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    monkeypatch.setenv("OLYMPUS_EXEC_BACKEND", "local")
    a = actions.prepare("shared", "run_python", {"code": "print('x')"})
    done = actions.approve("shared", a.id)          # no exec scope granted
    assert done.status == actions.FAILED
    assert "not granted" in done.error

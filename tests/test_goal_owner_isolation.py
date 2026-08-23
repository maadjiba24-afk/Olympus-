"""Goal identifiers never grant authority across authenticated owners.

All fixtures use temporary local state, synthetic identities, fake process
liveness, and synthetic runners.  No model, network, or OS actuator runs.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from olympus import adminpanel, config, gateway, goals, mcp_server


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)


def _same_goal_id(monkeypatch):
    monkeypatch.setattr(
        goals.uuid, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    goals.add("alice", "alice private goal")
    goals.add("bob", "bob private goal")
    return "a" * 8


def test_same_identifier_is_scoped_by_owner(monkeypatch):
    goal_id = _same_goal_id(monkeypatch)

    assert goals.get(goal_id, user="alice").text == "alice private goal"
    assert goals.get(goal_id, user="bob").text == "bob private goal"
    assert goals.get(goal_id, user="mallory") is None


def test_active_goal_quota_is_per_owner(monkeypatch):
    monkeypatch.setattr(goals, "MAX_ACTIVE", 1)

    assert "Goal" in goals.add("alice", "alice one")
    assert "already" in goals.add("alice", "alice two")
    assert "Goal" in goals.add("bob", "bob one")

    assert len(goals.active("alice")) == 1
    assert len(goals.active("bob")) == 1


def test_global_storage_cap_fails_closed_without_deleting_goals(monkeypatch):
    monkeypatch.setattr(goals, "MAX_TOTAL_GOALS", 2)
    goals.add("alice", "alice goal")
    goals.add("bob", "bob goal")

    result = goals.add("charlie", "charlie goal")

    assert "capacity" in result.lower()
    assert {(goal.user, goal.text) for goal in goals._load()} == {
        ("alice", "alice goal"), ("bob", "bob goal")}


def test_status_and_progress_mutate_only_the_owner(monkeypatch):
    goal_id = _same_goal_id(monkeypatch)

    goals.note_progress(goal_id, "alice evidence", user="alice")
    result = goals.set_status(goal_id, "done", evidence="alice proof",
                              user="alice")

    assert "now done" in result
    alice = goals.get(goal_id, user="alice")
    bob = goals.get(goal_id, user="bob")
    assert alice.status == "done" and alice.evidence == "alice proof"
    assert alice.progress[-1]["note"] == "alice evidence"
    assert bob.status == "active" and bob.evidence == ""
    assert bob.progress == []


def test_forged_owner_cannot_read_drop_or_park_another_goal(monkeypatch):
    goals.add("bob", "bob private goal")
    goal_id = goals.active("bob")[0].id
    monkeypatch.setattr(goals, "_pid_alive", lambda pid: True)

    assert goals.get(goal_id, user="alice") is None
    assert "No goal" in goals.set_status(
        goal_id, "dropped", user="alice")
    assert "No goal" in goals.wait_on(goal_id, 4242, user="alice")

    bob = goals.get(goal_id, user="bob")
    assert bob.status == "active" and bob.wait_pid == 0


def test_chat_goal_commands_use_authenticated_owner(monkeypatch):
    goals.add("ol-bob", "bob private goal")
    goal_id = goals.active("ol-bob")[0].id
    monkeypatch.setattr(goals, "_pid_alive", lambda pid: True)

    dropped = "\n".join(gateway.reply_for(
        {}, "alice", f"/goal drop {goal_id}"))
    parked = "\n".join(gateway.reply_for(
        {}, "alice", f"/goal wait {goal_id} 4242"))

    assert "No goal" in dropped and "No goal" in parked
    bob = goals.get(goal_id, user="ol-bob")
    assert bob.status == "active" and bob.wait_pid == 0


def test_mcp_goal_listing_uses_the_exposed_owner(monkeypatch):
    goals.add("alice", "alice private goal")
    goals.add("bob", "bob private goal")
    monkeypatch.setenv("OLYMPUS_MCP_USER", "alice")

    result = mcp_server.handle_message({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "olympus_goals", "arguments": {}},
    })
    rendered = result["result"]["content"][0]["text"]

    assert "alice private goal" in rendered
    assert "bob private goal" not in rendered


def test_admin_controls_carry_the_selected_owner(monkeypatch):
    goal_id = _same_goal_id(monkeypatch)

    result = adminpanel.act(
        "goal_done", {"id": goal_id, "user": "alice"})

    assert result["ok"]
    assert goals.get(goal_id, user="alice").status == "done"
    assert goals.get(goal_id, user="bob").status == "active"


def test_background_work_updates_only_the_goal_owner(monkeypatch):
    goal_id = _same_goal_id(monkeypatch)

    result = goals.work_one(
        goals.get(goal_id, user="alice"),
        runner=lambda user, prompt: "alice produced report.txt",
        judge_fn=lambda system, messages, schema: {
            "done": False,
            "confidence": 0.1,
            "evidence": "",
            "missing": "review",
        },
    )

    assert "progress logged" in result
    alice = goals.get(goal_id, user="alice")
    bob = goals.get(goal_id, user="bob")
    assert alice.checks == 1 and len(alice.progress) == 1
    assert bob.checks == 0 and bob.progress == []

"""Tests for playbooks — procedural memory (save/match/run, approval-gated)."""

import pytest

from olympus import playbooks, config, store


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    store.reset()
    yield
    store.reset()


def test_save_and_get():
    p = playbooks.save("u", "weekly investor update",
                       ["pull metrics", "draft in my voice", "CC cofounder"])
    assert p["version"] == 1 and len(p["steps"]) == 3
    assert playbooks.get("u", "weekly investor update")["id"] == p["id"]


def test_save_requires_name_and_steps():
    with pytest.raises(ValueError):
        playbooks.save("u", "", ["x"])
    with pytest.raises(ValueError):
        playbooks.save("u", "name", [])


def test_resave_bumps_version_keeps_id_and_uses():
    p1 = playbooks.save("u", "report", ["a", "b"])
    playbooks.mark_used("u", p1["id"])
    p2 = playbooks.save("u", "report", ["a", "b", "c"])
    assert p2["id"] == p1["id"] and p2["version"] == 2
    assert playbooks.get("u", "report")["use_count"] == 1   # stats preserved


def test_match_requires_most_name_words():
    playbooks.save("u", "weekly investor update", ["a"])
    assert playbooks.match("u", "run my weekly investor update please")["name"] \
        == "weekly investor update"
    assert playbooks.match("u", "what's the weather") is None
    # a single shared word is not enough to fire
    assert playbooks.match("u", "give me a weekly summary") is None


def test_context_block_is_pure_and_formats():
    playbooks.save("u", "deploy flow", ["run tests", "tag release", "push"])
    block = playbooks.context_block("u", "do the deploy flow")
    assert "Saved procedure" in block and "run tests" in block
    assert playbooks.get("u", "deploy flow")["use_count"] == 0   # pure: no mark


def test_run_block_marks_used():
    playbooks.save("u", "deploy flow", ["a"])
    assert playbooks.run_block("u", "deploy flow")
    assert playbooks.get("u", "deploy flow")["use_count"] == 1


def test_proposed_not_matched_until_approved():
    playbooks.propose("u", "monthly close", ["reconcile", "report"])
    assert playbooks.match("u", "run the monthly close") is None   # proposed: inert
    playbooks.approve("u", "monthly close")
    assert playbooks.match("u", "run the monthly close")["name"] == "monthly close"


def test_propose_refuses_to_clobber_active():
    playbooks.save("u", "standup", ["a"])
    with pytest.raises(ValueError):
        playbooks.propose("u", "standup", ["b"])


def test_delete():
    playbooks.save("u", "temp", ["a"])
    assert playbooks.delete("u", "temp")
    assert playbooks.get("u", "temp") is None


def test_per_user():
    playbooks.save("alice", "x", ["a"])
    assert playbooks.list_all("bob") == []


def test_propose_tool_handler():
    from olympus import tools, memory
    memory.set_user("toolu")
    msg = tools._propose_playbook("onboarding", ["create account", "send welcome"])
    assert "Proposed" in msg
    # proposed, so inert until approved
    assert playbooks.match("toolu", "run onboarding") is None
    assert playbooks.list_all("toolu", status=playbooks.PROPOSED)[0]["name"] == "onboarding"


def test_injected_into_router(monkeypatch):
    from olympus import orchestrator, backend
    playbooks.save("pbu", "quarterly review",
                   ["gather numbers", "draft summary", "schedule meeting"])
    captured = {}
    monkeypatch.setattr(backend, "complete_json",
                        lambda settings, system, messages, schema, effort="high":
                        captured.update(system=system) or
                        {"mode": "direct", "direct_reply": "ok", "specialists": [],
                         "brief": None, "needs_verification": False})
    bot = orchestrator.Olympus(user="pbu")
    bot._route("let's do the quarterly review")
    assert "gather numbers" in captured["system"]

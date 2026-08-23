"""Scheduled jobs are isolated by authenticated owner as well as name.

The fixtures use temporary state and synthetic runners only.  They do not
contact a model, delivery channel, subprocess, or external service.
"""

from __future__ import annotations

import pytest

from olympus import adminpanel, config, scheduler


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)


def test_same_name_jobs_coexist_and_only_same_owner_is_replaced():
    scheduler.add("daily", 60, "alice first", user="alice", now=0)
    scheduler.add("daily", 60, "bob only", user="bob", now=1)
    scheduler.add("daily", 60, "alice replacement", user="alice", now=2)

    jobs = sorted(scheduler.jobs(), key=lambda job: job.user)
    assert [(job.user, job.name, job.prompt) for job in jobs] == [
        ("alice", "daily", "alice replacement"),
        ("bob", "daily", "bob only"),
    ]


def test_remove_and_enable_mutate_only_the_named_owner():
    scheduler.add("daily", 60, "alice", user="alice", now=0)
    scheduler.add("daily", 60, "bob", user="bob", now=0)

    assert scheduler.set_enabled("daily", False, user="alice") is True
    assert [(job.user, job.enabled) for job in scheduler.jobs()] == [
        ("alice", False), ("bob", True)]
    assert scheduler.remove("daily", user="alice") is True
    assert [(job.user, job.name) for job in scheduler.jobs()] == [
        ("bob", "daily")]
    assert scheduler.remove("daily", user="alice") is False


def test_due_jobs_with_same_name_both_run_and_mark_independently(monkeypatch):
    monkeypatch.setattr(scheduler, "_deliver", lambda job, answer: None)
    scheduler.add("daily", 60, "alice task", user="alice", now=0)
    scheduler.add("daily", 60, "bob task", user="bob", now=0)
    calls = []

    log = scheduler.run_due(
        now=61,
        runner=lambda prompt, user: calls.append((user, prompt)) or "ok")

    assert sorted(calls) == [
        ("alice", "alice task"), ("bob", "bob task")]
    assert len(log) == 2
    assert {(job.user, job.last_run) for job in scheduler.jobs()} == {
        ("alice", 61), ("bob", 61)}


def test_interrupted_state_is_scoped_by_owner():
    scheduler.add("daily", 60, "alice", user="alice", now=0)
    scheduler.add("daily", 60, "bob", user="bob", now=0)

    scheduler._mark_started("daily", 100, user="alice")
    interrupted = scheduler.interrupted(now=100 + scheduler.RESUME_AFTER + 1)

    assert [(job.user, job.name) for job in interrupted] == [
        ("alice", "daily")]


def test_storage_cap_is_per_owner(monkeypatch):
    monkeypatch.setattr(scheduler, "MAX_JOBS", 2)
    for i in range(3):
        scheduler.add(f"alice-{i}", 60, "a", user="alice", now=i)
    for i in range(2):
        scheduler.add(f"bob-{i}", 60, "b", user="bob", now=i)

    assert {job.name for job in scheduler.jobs("alice")} == {
        "alice-1", "alice-2"}
    assert {job.name for job in scheduler.jobs("bob")} == {
        "bob-0", "bob-1"}


def test_global_cap_fails_closed_without_evicting_another_owner(monkeypatch):
    monkeypatch.setattr(scheduler, "MAX_TOTAL_JOBS", 2)
    scheduler.add("one", 60, "alice", user="alice", now=0)
    scheduler.add("two", 60, "bob", user="bob", now=0)

    with pytest.raises(ValueError, match="capacity"):
        scheduler.add("three", 60, "charlie", user="charlie", now=0)

    assert {(job.user, job.name) for job in scheduler.jobs()} == {
        ("alice", "one"), ("bob", "two")}


def test_summary_can_be_scoped_to_one_owner():
    scheduler.add("private", 60, "alice text", user="alice", now=0)
    scheduler.add("private", 60, "bob text", user="bob", now=0)

    rendered = scheduler.summary(user="alice")

    assert "alice text" in rendered
    assert "bob text" not in rendered


def test_admin_controls_carry_the_selected_owner():
    scheduler.add("daily", 60, "alice", user="alice", now=0)
    scheduler.add("daily", 60, "bob", user="bob", now=0)

    assert adminpanel.act(
        "schedule_disable", {"name": "daily", "user": "alice"})["ok"]
    assert [(job.user, job.enabled) for job in scheduler.jobs()] == [
        ("alice", False), ("bob", True)]
    assert adminpanel.act(
        "schedule_remove", {"name": "daily", "user": "alice"})["ok"]
    assert [(job.user, job.name) for job in scheduler.jobs()] == [
        ("bob", "daily")]


def test_same_name_on_exit_jobs_fire_and_disable_independently(monkeypatch):
    alive = {11: True, 22: True}
    monkeypatch.setattr(scheduler, "_pid_alive", lambda pid: alive[pid])
    monkeypatch.setattr(scheduler, "_deliver", lambda job, answer: None)
    scheduler.add_on_exit(
        "watch", 11, "alice report", user="alice", now=0)
    scheduler.add_on_exit(
        "watch", 22, "bob report", user="bob", now=0)
    alive.update({11: False, 22: False})
    calls = []

    log = scheduler.run_due(
        now=1,
        runner=lambda prompt, user: calls.append((user, prompt)) or "ok")

    assert len(log) == 2
    assert {user for user, _prompt in calls} == {"alice", "bob"}
    assert any("alice report" in prompt for _user, prompt in calls)
    assert any("bob report" in prompt for _user, prompt in calls)
    assert {(job.user, job.enabled) for job in scheduler.jobs()} == {
        ("alice", False), ("bob", False)}


def test_same_name_on_change_jobs_mark_hashes_independently(monkeypatch):
    observed = {"alice-cmd": "a1", "bob-cmd": "b1"}
    monkeypatch.setattr(
        scheduler, "_watch_hash", lambda command: observed[command])
    monkeypatch.setattr(scheduler, "_deliver", lambda job, answer: None)
    scheduler.add_on_change(
        "watch", "alice-cmd", "alice report", user="alice", now=0)
    scheduler.add_on_change(
        "watch", "bob-cmd", "bob report", user="bob", now=0)
    observed.update({"alice-cmd": "a2", "bob-cmd": "b2"})
    calls = []

    log = scheduler.run_due(
        now=scheduler.ON_CHANGE_POLL + 1,
        runner=lambda prompt, user: calls.append((user, prompt)) or "ok")

    assert len(log) == 2
    assert {user for user, _prompt in calls} == {"alice", "bob"}
    assert {(job.user, job.last_hash) for job in scheduler.jobs()} == {
        ("alice", "a2"), ("bob", "b2")}

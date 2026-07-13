"""Graceful update handoff: interrupted scheduled jobs resume with a
continuation, and an upgrade journals in-flight work for the next process."""

import json

from olympus import config, gateway, heartbeat, scheduler, selfupdate


def _set_started(name, started_at):
    """Simulate a run that began at `started_at` and never finished."""
    jobs = scheduler._load()
    for j in jobs:
        if j.name == name:
            j.started_at = started_at
    scheduler._save(jobs)


def test_interrupted_job_resumes_with_continuation():
    scheduler.add("daily-report", 86400, "write the report", now=0.0)
    ran = []
    scheduler.run_due(now=86401.0, runner=lambda p, u: ran.append(p) or "ok")
    assert len(ran) == 1

    # next run started at t=90000, process died; well past RESUME_AFTER now
    _set_started("daily-report", 90000.0)
    now = 90000.0 + scheduler.RESUME_AFTER + 5
    log = scheduler.run_due(now=now,
                            runner=lambda p, u: ran.append(p) or "ok")
    assert any("resumed interrupted job" in line for line in log)
    assert "interrupted by a restart" in ran[-1]
    assert "write the report" in ran[-1]


def test_interrupted_resume_happens_once():
    scheduler.add("daily-report", 86400, "report", now=0.0)
    scheduler.run_due(now=86401.0, runner=lambda p, u: "ok")
    _set_started("daily-report", 90000.0)
    now = 90000.0 + scheduler.RESUME_AFTER + 5

    def crash(prompt, user):
        raise RuntimeError("killed again")

    scheduler.run_due(now=now, runner=crash)
    # crashed during resume: no second resume attempt (bounded at 1)
    assert scheduler.interrupted(now=now + scheduler.RESUME_AFTER + 5) == []


def test_fresh_run_is_not_interrupted():
    scheduler.add("j", 3600, "p", now=0.0)
    scheduler.run_due(now=3601.0, runner=lambda p, u: "ok")
    assert scheduler.interrupted(now=3601.0 + scheduler.RESUME_AFTER + 1) == []


def test_successful_resume_clears_resume_state():
    scheduler.add("j2", 86400, "p", now=0.0)
    scheduler.run_due(now=86401.0, runner=lambda p, u: "ok")
    _set_started("j2", 90000.0)
    now = 90000.0 + scheduler.RESUME_AFTER + 5
    log = scheduler.run_due(now=now, runner=lambda p, u: "ok")
    assert any("resumed" in line for line in log)
    job = next(j for j in scheduler.jobs() if j.name == "j2")
    assert job.resume_attempts == 0
    assert job.started_at <= job.last_run


def test_write_and_take_handoff(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    gateway.inflight_mark("tg-1", 1, "long question")
    scheduler.add("mid-run", 3600, "p", now=0.0)
    scheduler._mark_started("mid-run", 50.0)

    handoff = selfupdate.write_handoff()
    assert "tg-1" in handoff["pending"]["inflight"]
    assert "mid-run" in handoff["pending"]["jobs_running"]
    assert (tmp_path / "handoff.json").exists()

    taken = selfupdate.take_handoff()
    assert taken["from_version"] == handoff["from_version"]
    assert not (tmp_path / "handoff.json").exists()
    assert selfupdate.take_handoff() is None            # consumed once


def test_heartbeat_reports_handoff(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    notified = []
    monkeypatch.setattr(gateway, "notify_all",
                        lambda t: notified.append(t) or ["telegram"])
    gateway.inflight_mark("tg-9", 9, "pending question")
    selfupdate.write_handoff()

    log = heartbeat.tick({}, now=1.0)
    assert any("restarted" in line and "1 in-flight" in line for line in log)
    assert notified and "resume" in notified[0]


def test_heartbeat_quiet_without_handoff(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    log = heartbeat.tick({}, now=1.0)
    assert not any("restarted" in line for line in log)

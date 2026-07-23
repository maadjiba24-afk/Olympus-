"""Change-triggered scheduling: the on_change job kind fires when a watched
command's output changes, recurring, throttled, and only after a baseline."""

import pytest

from olympus import gateway, scheduler


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    # Poll every tick in tests instead of once a minute.
    monkeypatch.setattr(scheduler, "ON_CHANGE_POLL", 0)


def test_add_captures_baseline_and_does_not_fire_immediately(tmp_path):
    f = tmp_path / "watched.txt"
    f.write_text("v1", encoding="utf-8")
    job = scheduler.add_on_change("w", f"cat {f}", "summarize it")
    assert job.kind == "on_change" and job.last_hash          # baseline set
    assert scheduler.changed(now=job.created + 1) == []       # no change yet


def test_fires_once_per_change(tmp_path):
    f = tmp_path / "watched.txt"
    f.write_text("v1", encoding="utf-8")
    scheduler.add_on_change("w", f"cat {f}", "summarize", now=0.0)

    ran = []
    runner = lambda p, u: ran.append(p) or "ok"

    # no change yet
    assert scheduler.run_due(now=100.0, runner=runner) == []
    # change the watched output
    f.write_text("v2", encoding="utf-8")
    log = scheduler.run_due(now=200.0, runner=runner)
    assert any("ran scheduled job 'w'" in ln for ln in log)
    assert "output changed" in ran[0]
    # same output → no re-fire (hash was persisted)
    assert scheduler.run_due(now=300.0, runner=runner) == []
    # change again → fires again (recurring, unlike on_exit)
    f.write_text("v3", encoding="utf-8")
    assert scheduler.run_due(now=400.0, runner=runner)
    assert len(ran) == 2


def test_missing_command_output_is_not_a_change(tmp_path):
    # A failing watch command yields no observation, never a spurious fire.
    scheduler.add_on_change("w", "exit 3", "do", now=0.0)
    ran = []
    scheduler.run_due(now=100.0, runner=lambda p, u: ran.append(1) or "ok")
    assert ran == []


def test_empty_command_rejected():
    with pytest.raises(ValueError):
        scheduler.add_on_change("w", "   ", "do")


def test_poll_is_throttled(monkeypatch, tmp_path):
    monkeypatch.setattr(scheduler, "ON_CHANGE_POLL", 3600)
    f = tmp_path / "w.txt"
    f.write_text("a", encoding="utf-8")
    scheduler.add_on_change("w", f"cat {f}", "do", now=0.0)
    f.write_text("b", encoding="utf-8")
    # within the throttle window → not re-polled, so no fire
    assert scheduler.changed(now=100.0) == []
    # after the window → observed
    assert scheduler.changed(now=4000.0)


def test_summary_and_nextdue_render_on_change(tmp_path):
    f = tmp_path / "w.txt"
    f.write_text("a", encoding="utf-8")
    scheduler.add_on_change("w", f"cat {f}", "do", label="my file", now=0.0)
    assert "when my file changes" in scheduler.summary()
    assert scheduler.next_due_in(now=0.0) == 0.0     # ON_CHANGE_POLL patched to 0


def test_chat_onchange_command(tmp_path):
    f = tmp_path / "w.txt"
    f.write_text("a", encoding="utf-8")
    out = gateway.reply_for({}, "u1", f"/onchange cat {f} :: report the change")
    assert any("Watching" in c for c in out)
    job = next(j for j in scheduler.jobs() if j.kind == "on_change")
    assert job.user == "ol-u1" and "report the change" in job.prompt


def test_chat_onchange_usage():
    out = gateway.reply_for({}, "u1", "/onchange onlycmd")
    assert any("Usage" in c for c in out)

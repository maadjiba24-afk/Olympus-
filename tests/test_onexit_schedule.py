"""Event-driven scheduling: the on_exit job kind fires when a watched
process exits, once, and asks to be polled while the process lives."""

import os
import subprocess
import sys

import pytest


from olympus import gateway, scheduler


@pytest.fixture
def sleeper():
    proc = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(300)"]
)
    yield proc
    proc.kill()
    proc.wait()


def test_add_on_exit_requires_live_process():
    with pytest.raises(ValueError):
        scheduler.add_on_exit("j", 99999999, "do things")


def test_not_due_while_process_lives(sleeper):
    scheduler.add_on_exit("watch", sleeper.pid, "summarize the log")
    assert scheduler.due() == []
    # while alive, the watch asks to be re-polled shortly, not to fire
    assert scheduler.next_due_in() == float(scheduler.ON_EXIT_POLL)


def test_fires_once_after_exit_and_disables(sleeper):
    scheduler.add_on_exit("watch", sleeper.pid, "summarize the log",
                          label="the training run")
    sleeper.kill()
    sleeper.wait()
    assert scheduler.next_due_in() == 0.0

    ran = []

    def runner(prompt, user):
        ran.append((prompt, user))
        return "done"

    log = scheduler.run_due(runner=runner)
    assert log == ["ran scheduled job 'watch'"]
    prompt = ran[0][0]
    assert "the training run" in prompt and "has exited" in prompt
    assert "summarize the log" in prompt

    # one-shot: fired and now dormant, even though the pid is still gone
    assert scheduler.due() == []
    assert scheduler.run_due(runner=runner) == []
    job = next(j for j in scheduler.jobs() if j.name == "watch")
    assert job.enabled is False


def test_interval_jobs_unaffected_by_new_fields():
    scheduler.add("plain", 3600, "do the thing", now=0.0)
    jobs = scheduler.jobs()
    assert jobs[0].kind == "interval"
    assert jobs[0].due(3601.0) is True


def test_summary_renders_on_exit_kind(sleeper):
    scheduler.add_on_exit("watch", sleeper.pid, "report", label="my build")
    text = scheduler.summary()
    assert "when my build exits" in text


# --- chat wiring --------------------------------------------------------------

def test_chat_onexit_command(sleeper):
    out = gateway.reply_for({}, "u1", f"/onexit {sleeper.pid} report results")
    assert any("Watching pid" in c for c in out)
    job = next(j for j in scheduler.jobs()
               if j.name == f"onexit-{sleeper.pid}")
    assert job.kind == "on_exit" and job.user == "ol-u1"


def test_chat_onexit_usage_and_dead_pid():
    out = gateway.reply_for({}, "u1", "/onexit nope")
    assert any("Usage" in c for c in out)
    out = gateway.reply_for({}, "u1", "/onexit 99999999 report")
    assert any("isn't running" in c for c in out)


def test_own_process_pid_counts_as_alive():
    # sanity for the liveness helper on this platform
    assert scheduler._pid_alive(os.getpid()) is True
    assert scheduler._pid_alive(0) is False

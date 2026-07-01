"""Natural-language cron: interval parsing, due logic, run+deliver, heartbeat hook."""

from olympus import scheduler


def test_parse_interval_phrases():
    assert scheduler.parse_interval("hourly") == 3600
    assert scheduler.parse_interval("daily") == 86400
    assert scheduler.parse_interval("weekly") == 7 * 86400
    assert scheduler.parse_interval("every 30m") == 1800
    assert scheduler.parse_interval("every 2 days") == 2 * 86400
    assert scheduler.parse_interval("6h") == 6 * 3600
    assert scheduler.parse_interval("gibberish") == 86400        # forgiving
    assert scheduler.parse_interval("every 1s") == 60            # min floor


def test_add_list_remove_roundtrip():
    scheduler.add("scan", "hourly", "scan the market", now=1000.0)
    js = scheduler.jobs()
    assert len(js) == 1 and js[0].name == "scan" and js[0].interval == 3600
    assert scheduler.remove("scan") is True
    assert scheduler.jobs() == []


def test_add_replaces_by_name():
    scheduler.add("j", "hourly", "a", now=0)
    scheduler.add("j", "daily", "b", now=0)
    js = scheduler.jobs()
    assert len(js) == 1 and js[0].prompt == "b" and js[0].interval == 86400


def test_summary_renders_coarse_intervals():
    # weekly/daily must read as days, not 168h/24h; sub-minute keeps seconds.
    assert scheduler._human_interval(7 * 86400) == "7d"
    assert scheduler._human_interval(86400) == "1d"
    assert scheduler._human_interval(2 * 86400) == "2d"
    assert scheduler._human_interval(6 * 3600) == "6h"
    assert scheduler._human_interval(1800) == "30m"
    assert scheduler._human_interval(45) == "45s"

    scheduler.add("weekly-report", "weekly", "summarize the week", now=0.0)
    out = scheduler.summary()
    assert "every 7d" in out and "168h" not in out


def test_due_and_next_due_in():
    scheduler.add("j", "hourly", "x", now=0.0)
    assert scheduler.due(now=10.0) == []                    # not yet
    due = scheduler.due(now=3601.0)
    assert len(due) == 1
    wait = scheduler.next_due_in(now=0.0)
    assert abs(wait - 3600) < 2


def test_disable_excludes_from_due():
    scheduler.add("j", "hourly", "x", now=0.0)
    scheduler.set_enabled("j", False)
    assert scheduler.due(now=99999.0) == []


def test_run_due_executes_delivers_and_marks(monkeypatch):
    delivered = {}
    monkeypatch.setattr(scheduler, "_deliver",
                        lambda job, ans: delivered.update({job.name: ans}))
    scheduler.add("rep", "hourly", "make a report", now=0.0)
    calls = []

    def runner(prompt, user):
        calls.append((prompt, user))
        return "the report"

    log = scheduler.run_due(now=3601.0, runner=runner)
    assert calls == [("make a report", "shared")]
    assert delivered["rep"] == "the report"
    assert "ran scheduled job 'rep'" in log
    # marked as run → no longer due immediately
    assert scheduler.due(now=3602.0) == []


def test_heartbeat_invokes_scheduler(monkeypatch):
    from olympus import heartbeat
    seen = {}

    def fake_run_due(now=None):
        seen["ran"] = True
        return ["did a thing"]

    monkeypatch.setattr(scheduler, "run_due", fake_run_due)
    # stop the rest of the tick from doing real work
    monkeypatch.setattr(heartbeat, "_due", lambda *a, **k: False)
    log = heartbeat.tick({}, now=123.0)
    assert seen.get("ran") and any("did a thing" in line for line in log)

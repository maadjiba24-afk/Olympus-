"""The operator dashboard folds reports, errors, spend, and cost policy into one
read-only view. It must surface a real signal (over-cap spend, the latest report
and error) and never raise on a fresh, empty instance."""

from olympus import config, dashboard, errors, support, usage


def test_summary_has_the_operator_fields(monkeypatch):
    monkeypatch.setattr(support, "recent", lambda n=100000: [])
    monkeypatch.setattr(errors, "recent", lambda n=100000: [])
    monkeypatch.setattr(usage, "today_spend", lambda: 0.0)
    monkeypatch.setattr(usage, "daily_budget", lambda: 0.0)
    s = dashboard.summary()
    for key in ("uptime_seconds", "spend_today", "daily_budget",
                "reports_total", "errors_total", "cost_policy"):
        assert key in s
    assert s["reports_total"] == 0 and s["errors_total"] == 0


def test_render_empty_instance_is_quiet_and_safe(monkeypatch):
    monkeypatch.setattr(support, "recent", lambda n=100000: [])
    monkeypatch.setattr(errors, "recent", lambda n=100000: [])
    monkeypatch.setattr(usage, "today_spend", lambda: 0.0)
    monkeypatch.setattr(usage, "daily_budget", lambda: 0.0)
    out = dashboard.render()
    assert "operator dashboard" in out
    assert "all quiet" in out


def test_render_surfaces_latest_report_error_and_over_cap(monkeypatch):
    monkeypatch.setattr(support, "recent",
                        lambda n=100000: [{"ts": "2026-06-23T00:00:00Z",
                                           "user": "bob", "contact": "bob@x.io",
                                           "message": "login is broken"}])
    monkeypatch.setattr(errors, "recent",
                        lambda n=100000: [{"ts": "2026-06-23T00:01:00Z",
                                           "where": "web.chat",
                                           "error": "RuntimeError: boom"}])
    monkeypatch.setattr(usage, "today_spend", lambda: 7.5)
    monkeypatch.setattr(usage, "daily_budget", lambda: 5.0)
    out = dashboard.render()
    assert "login is broken" in out and "bob" in out
    assert "web.chat" in out and "boom" in out
    assert "OVER CAP" in out                 # spend exceeded the cap → flagged


def test_policy_line_reflects_free_allowance(monkeypatch):
    monkeypatch.setattr(config, "free_chats", lambda: 5)
    monkeypatch.setattr(config, "require_byok", lambda: False)
    monkeypatch.setattr(config, "daily_chat_limit", lambda: 50)
    line = dashboard._policy_line({"free_chats": 5, "require_byok": False,
                                   "daily_chat_limit": 50})
    assert "5 free chats/day" in line and "50/user/day" in line

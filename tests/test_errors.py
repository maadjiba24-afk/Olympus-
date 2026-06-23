"""Operator error capture: durable log + rate-limited Telegram alerts."""

from olympus import config, errors


def test_capture_persists_and_alerts(monkeypatch):
    alerts = []
    monkeypatch.setattr(errors.telegram, "notify", lambda m: alerts.append(m) or True)
    errors._alert_times.clear()
    try:
        raise ValueError("boom in the pipeline")
    except ValueError as err:
        rec = errors.capture("web /api/chat", err, context="user asked X")
    assert rec["where"] == "web /api/chat"
    assert "ValueError: boom" in rec["error"]
    assert "boom in the pipeline" in rec["traceback"]

    path = config.MEMORY_DIR / "errors" / "errors.jsonl"
    assert path.exists() and len(path.read_text().splitlines()) == 1
    assert alerts and "web /api/chat" in alerts[0]


def test_capture_never_raises(monkeypatch):
    # Even if alerting blows up, capture must not raise (it guards error paths).
    def boom(_m):
        raise RuntimeError("telegram down")
    monkeypatch.setattr(errors.telegram, "notify", boom)
    errors._alert_times.clear()
    rec = errors.capture("x", RuntimeError("inner"))
    assert rec["error"].startswith("RuntimeError")


def test_alerts_are_rate_limited(monkeypatch):
    sent = []
    monkeypatch.setattr(errors.telegram, "notify", lambda m: sent.append(m) or True)
    errors._alert_times.clear()
    for i in range(20):
        errors.capture("loop", RuntimeError(f"e{i}"))
    # capped per hour, so far fewer than 20 alerts despite 20 captures
    assert len(sent) == errors._ALERT_MAX_PER_HOUR
    # but every error was still persisted
    path = config.MEMORY_DIR / "errors" / "errors.jsonl"
    assert len(path.read_text().splitlines()) == 20


def test_recent_returns_newest_last(monkeypatch):
    monkeypatch.setattr(errors.telegram, "notify", lambda m: True)
    errors._alert_times.clear()
    errors.capture("a", RuntimeError("first"))
    errors.capture("b", RuntimeError("second"))
    msgs = [e["error"] for e in errors.recent()]
    assert msgs[-2:] == ["RuntimeError: first", "RuntimeError: second"]

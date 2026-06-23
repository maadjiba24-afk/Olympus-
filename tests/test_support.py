"""Problem-report (complaint) channel: durable capture + operator alert."""

import json

import pytest

from olympus import config, support


def test_report_is_persisted_and_alerts(monkeypatch):
    alerted = {}
    monkeypatch.setattr(support.telegram, "notify",
                        lambda msg: alerted.setdefault("msg", msg) or True)
    rec = support.report("the inbox triage gave wrong dates",
                         user="alice", contact="alice@example.com")
    assert rec["user"] == "alice" and rec["message"].startswith("the inbox")

    # persisted to disk, one JSON line
    path = config.MEMORY_DIR / "reports" / "complaints.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["contact"] == "alice@example.com"
    # operator was alerted, with the message in it
    assert "inbox triage" in alerted["msg"]


def test_empty_report_is_rejected(monkeypatch):
    monkeypatch.setattr(support.telegram, "notify", lambda msg: True)
    with pytest.raises(ValueError):
        support.report("   ")


def test_alert_failure_does_not_lose_the_report(monkeypatch):
    def boom(_msg):
        raise RuntimeError("telegram down")
    monkeypatch.setattr(support.telegram, "notify", boom)
    rec = support.report("something broke", user="bob")   # must not raise
    assert rec["message"] == "something broke"
    assert support.recent()[-1]["message"] == "something broke"


def test_recent_returns_newest_last(monkeypatch):
    monkeypatch.setattr(support.telegram, "notify", lambda msg: True)
    support.report("first", user="u")
    support.report("second", user="u")
    recent = support.recent()
    assert [r["message"] for r in recent] == ["first", "second"]
    assert support.recent(1) == [recent[-1]]


def test_fields_are_truncated(monkeypatch):
    monkeypatch.setattr(support.telegram, "notify", lambda msg: True)
    rec = support.report("x" * 9000, user="u", contact="c" * 500,
                         context="y" * 9000)
    assert len(rec["message"]) == 4000
    assert len(rec["contact"]) == 200
    assert len(rec["context"]) == 500

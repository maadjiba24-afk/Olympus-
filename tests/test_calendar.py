"""Tests for the Google Calendar integration: adapter, action, safety."""

import pytest

from olympus import actions, builtin_actions, calendar, security
from olympus.specialists import SPECIALISTS


@pytest.fixture()
def fake_cal(monkeypatch):
    calls = []

    def fake_request(method, path, body=None):
        calls.append((method, path, body))
        if method == "GET":
            return {"items": [{
                "id": "e1", "summary": "Standup",
                "start": {"dateTime": "2026-06-15T09:00:00Z"},
                "end": {"dateTime": "2026-06-15T09:15:00Z"},
                "attendees": [{"email": "team@co.com"}]}]}
        return {"id": "new-event"}   # POST create / DELETE

    monkeypatch.setattr(calendar, "_request", fake_request)
    monkeypatch.setenv("GMAIL_ACCESS_TOKEN", "tok")
    return calls


def test_list_events_summarizes(fake_cal):
    out = calendar.list_events("2026-06-15T00:00:00Z", "2026-06-16T00:00:00Z")
    assert "Standup" in out and "09:00" in out and "team@co.com" in out


def test_upcoming_returns_structured_events(fake_cal):
    events = calendar.upcoming()
    assert len(events) == 1
    e = events[0]
    assert e["summary"] == "Standup"
    assert e["start"] == "2026-06-15T09:00:00Z"
    assert e["attendees"] == ["team@co.com"]
    assert e["all_day"] is False
    method, path, _ = [c for c in fake_cal if c[0] == "GET"][0]
    assert "singleEvents=true" in path and "orderBy=startTime" in path


def test_upcoming_degrades_to_empty_on_error(monkeypatch):
    def boom(method, path, body=None):
        raise calendar.CalendarError("Calendar API 401")
    monkeypatch.setattr(calendar, "_request", boom)
    assert calendar.upcoming() == []


def test_create_event_emails_attendees(fake_cal):
    r = calendar.create_event("Intro call", "2026-06-16T10:00:00Z",
                              "2026-06-16T10:30:00Z", ["jordan@client.com"])
    assert r["id"] == "new-event"
    method, path, body = [c for c in fake_cal if c[0] == "POST"][0]
    assert "sendUpdates=all" in path            # invitation is emailed
    assert body["attendees"] == [{"email": "jordan@client.com"}]


def test_reuses_google_token(monkeypatch):
    # calendar auth piggybacks on the gmail/Google token
    from olympus import gmail
    monkeypatch.setenv("GOOGLE_ACCESS_TOKEN", "shared-tok")
    for k in ("GMAIL_ACCESS_TOKEN", "GMAIL_REFRESH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(gmail, "_token", {"value": None, "expires": 0.0})
    assert gmail._access_token() == "shared-tok"


# --- calendar action on the spine ---------------------------------------

def test_calendar_create_is_gated_and_never_auto(fake_cal):
    a = actions.prepare("u", "calendar_create", {
        "summary": "Intro", "start": "2026-06-16T10:00:00Z",
        "end": "2026-06-16T10:30:00Z", "attendees": ["jordan@client.com"]})
    assert a.risk_class == actions.IRREVERSIBLE
    actions.set_autonomy("u", actions.L4_STANDING)
    assert not actions.can_auto_execute(a)       # invites never auto-send
    # no scope -> blocked
    assert actions.approve("u", a.id).status == actions.FAILED


def test_calendar_create_executes_and_can_cancel(fake_cal):
    actions.grant_scope("u", "calendar.events")
    a = actions.prepare("u", "calendar_create", {
        "summary": "Intro", "start": "2026-06-16T10:00:00Z",
        "end": "2026-06-16T10:30:00Z", "attendees": ["jordan@client.com"]})
    done = actions.approve("u", a.id)
    assert done.status == actions.EXECUTED and done.result["event_id"] == "new-event"
    undone = actions.undo("u", a.id)             # undo cancels the event
    assert undone.status == actions.UNDONE
    assert any(c[0] == "DELETE" for c in fake_cal)


# --- safety / agent wiring ----------------------------------------------

def test_calendar_reading_is_untrusted():
    assert security.should_wrap("read_calendar")


def test_angelos_has_calendar_but_cannot_act_directly():
    names = {d.get("name") for d in SPECIALISTS["angelos"].tool_defs("anthropic")}
    assert "read_calendar" in names and "prepare_action" in names
    assert "send_email" not in names and "call_webhook" not in names

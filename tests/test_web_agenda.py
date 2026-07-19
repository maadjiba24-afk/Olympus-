"""Phase 3 — the agenda over the web UI: scheduled tasks (always available) and
upcoming calendar events (only when a Google account is connected, read-only).
Tasks are filtered to the requesting principal (plus shared jobs); calendar
reads degrade gracefully to "not connected" rather than erroring.
"""

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from olympus import config, scheduler, web


@pytest.fixture()
def server(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "_SESSIONS", {})
    monkeypatch.setattr(web, "_HITS", {})
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _get(url, path):
    return json.loads(urllib.request.urlopen(url + path).read())


def test_page_has_agenda_surface(server):
    html = urllib.request.urlopen(server + "/").read().decode()
    assert "📅 agenda" in html and "/api/agenda" in html
    assert "renderAgenda" in html


def test_agenda_empty(server):
    out = _get(server, "/api/agenda?session=s1")
    assert out["tasks"] == []
    assert out["calendar_connected"] is False
    assert out["events"] is None


def test_agenda_lists_shared_tasks(server):
    scheduler.add("nightly scan", "daily", "scan the news", user="shared")
    out = _get(server, "/api/agenda?session=s1")
    names = [t["name"] for t in out["tasks"]]
    assert "nightly scan" in names
    row = next(t for t in out["tasks"] if t["name"] == "nightly scan")
    assert row["when"].startswith("every 1d")
    assert row["enabled"] is True
    assert row["due_in"] is not None


def test_agenda_hides_other_users_tasks(server):
    scheduler.add("mine", "daily", "x", user="someone-else")
    out = _get(server, "/api/agenda?session=s1")
    assert [t["name"] for t in out["tasks"]] == []


def test_agenda_calendar_disconnected_is_graceful(server, monkeypatch):
    # calendar configured but the principal hasn't connected -> not connected,
    # no error, no events.
    from olympus import calendar as gcal, google_oauth
    monkeypatch.setattr(gcal, "configured", lambda: True)
    monkeypatch.setattr(google_oauth, "connected", lambda user: False)
    out = _get(server, "/api/agenda?session=s1")
    assert out["calendar_connected"] is False
    assert out["events"] is None


def test_agenda_calendar_connected_shows_events(server, monkeypatch):
    from olympus import calendar as gcal, google_oauth
    monkeypatch.setattr(gcal, "configured", lambda: True)
    monkeypatch.setattr(google_oauth, "connected", lambda user: True)
    monkeypatch.setattr(gcal, "upcoming", lambda: [
        {"id": "1", "summary": "Standup", "start": "2026-07-09T09:00:00Z",
         "end": "2026-07-09T09:15:00Z", "all_day": False, "attendees": []}])
    out = _get(server, "/api/agenda?session=s1")
    assert out["calendar_connected"] is True
    assert out["events"][0]["summary"] == "Standup"

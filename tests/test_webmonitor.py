"""Web change-monitor (olympus/webmonitor.py) — opt-in, gated, replay-inert."""

import pytest

from olympus import config, security, webmonitor


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.delenv("OLYMPUS_WEB_MONITOR", raising=False)
    monkeypatch.delenv("OLYMPUS_REPLAY", raising=False)
    return tmp_path


# --- opt-in / replay gating -------------------------------------------------

def test_disabled_by_default(store):
    assert webmonitor.enabled() is False
    assert webmonitor.run_due(0) == []                 # no-op, no fetch


def test_inert_during_replay(store, monkeypatch):
    monkeypatch.setenv("OLYMPUS_WEB_MONITOR", "1")
    monkeypatch.setenv("OLYMPUS_REPLAY", "1")
    assert webmonitor.enabled() is False               # replay forces off


def test_enabled_when_flag_set(store, monkeypatch):
    monkeypatch.setenv("OLYMPUS_WEB_MONITOR", "1")
    assert webmonitor.enabled() is True


# --- CRUD -------------------------------------------------------------------

def test_add_records_and_lists(store):
    msg = webmonitor.add("u1", "https://ex.com/page", interval=3600)
    assert "Watching" in msg
    mine = webmonitor.list_for("u1")
    assert len(mine) == 1 and mine[0].url == "https://ex.com/page"


def test_add_refuses_internal_url(store):
    msg = webmonitor.add("u1", "http://127.0.0.1/admin")
    assert "Refused" in msg
    assert webmonitor.list_for("u1") == []


def test_add_refuses_non_http(store):
    assert "http(s)" in webmonitor.add("u1", "file:///etc/passwd")


def test_add_is_idempotent_per_user(store):
    webmonitor.add("u1", "https://ex.com/x")
    webmonitor.add("u1", "https://ex.com/x")
    assert len(webmonitor.list_for("u1")) == 1


def test_remove(store):
    webmonitor.add("u1", "https://ex.com/x")
    mid = webmonitor.list_for("u1")[0].id
    assert "Stopped" in webmonitor.remove("u1", mid)
    assert webmonitor.list_for("u1") == []


# --- scheduled check --------------------------------------------------------

def test_run_due_baseline_then_change(store, monkeypatch):
    monkeypatch.setenv("OLYMPUS_WEB_MONITOR", "1")
    webmonitor.add("u1", "https://ex.com/x", interval=1)
    notes = []
    monkeypatch.setattr("olympus.gateway.notify_all", lambda t: notes.append(t))

    from olympus import webctx
    # first pass: establish a baseline — no notification on first sight
    monkeypatch.setattr(webctx, "diff",
                        lambda url, prev: {"changed": True, "current_hash": "h1",
                                           "current_markdown": "v1", "diff": ""})
    log1 = webmonitor.run_due(now=1000)
    assert any("baseline" in ln for ln in log1)
    assert notes == []                                 # no alert on baseline

    # second pass: a real change → notify
    monkeypatch.setattr(webctx, "diff",
                        lambda url, prev: {"changed": True, "current_hash": "h2",
                                           "current_markdown": "v2",
                                           "diff": "- v1\n+ v2"})
    log2 = webmonitor.run_due(now=2000)
    assert any("CHANGED" in ln for ln in log2)
    assert len(notes) == 1 and "Page changed" in notes[0]
    assert webmonitor.list_for("u1")[0].changes == 1


def test_run_due_no_notify_when_unchanged(store, monkeypatch):
    monkeypatch.setenv("OLYMPUS_WEB_MONITOR", "1")
    webmonitor.add("u1", "https://ex.com/x", interval=1)
    notes = []
    monkeypatch.setattr("olympus.gateway.notify_all", lambda t: notes.append(t))
    from olympus import webctx
    monkeypatch.setattr(webctx, "diff",
                        lambda url, prev: {"changed": False, "current_hash": "h1",
                                           "current_markdown": "v1", "diff": ""})
    webmonitor.run_due(now=1000)                        # baseline
    webmonitor.run_due(now=2000)                        # same hash
    assert notes == []


# --- classification ---------------------------------------------------------

def test_monitor_tools_are_trusted():
    for name in ("web_monitor_add", "web_monitor_list"):
        assert name in security.TRUSTED_TOOLS

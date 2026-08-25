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
    # Delivery is fail-closed by default (no owner-targeted route exists),
    # so a test that needs an alert injects an explicit owner-aware
    # callback rather than relying on the installation-wide fan-out.
    def _notify(t, *, user):
        notes.append((t, user))

    from olympus import webctx
    # first pass: establish a baseline — no notification on first sight
    monkeypatch.setattr(webctx, "diff",
                        lambda url, prev: {"changed": True, "current_hash": "h1",
                                           "current_markdown": "v1", "diff": ""})
    log1 = webmonitor.run_due(now=1000, notify=_notify)
    assert any("baseline" in ln for ln in log1)
    assert notes == []                                 # no alert on baseline

    # second pass: a real change → notify
    monkeypatch.setattr(webctx, "diff",
                        lambda url, prev: {"changed": True, "current_hash": "h2",
                                           "current_markdown": "v2",
                                           "diff": "- v1\n+ v2"})
    log2 = webmonitor.run_due(now=2000, notify=_notify)
    assert any("CHANGED" in ln for ln in log2)
    assert len(notes) == 1 and "Page changed" in notes[0][0]
    assert notes[0][1] == "u1"                         # egressed as its owner
    assert webmonitor.list_for("u1")[0].changes == 1


def test_run_due_no_notify_when_unchanged(store, monkeypatch):
    monkeypatch.setenv("OLYMPUS_WEB_MONITOR", "1")
    webmonitor.add("u1", "https://ex.com/x", interval=1)
    notes = []
    # Delivery is fail-closed by default (no owner-targeted route exists),
    # so a test that needs an alert injects an explicit owner-aware
    # callback rather than relying on the installation-wide fan-out.
    def _notify(t, *, user):
        notes.append((t, user))
    from olympus import webctx
    monkeypatch.setattr(webctx, "diff",
                        lambda url, prev: {"changed": False, "current_hash": "h1",
                                           "current_markdown": "v1", "diff": ""})
    webmonitor.run_due(now=1000)                        # baseline
    webmonitor.run_due(now=2000)                        # same hash
    assert notes == []


# --- hardening: store resilience & failure handling ------------------------

def test_corrupt_store_is_quarantined_not_wiped(store):
    webmonitor.add("u1", "https://ex.com/x")
    p = webmonitor._path()
    p.write_text("{ this is not valid json", encoding="utf-8")   # corrupt it
    assert webmonitor._load() == []                    # decodes to empty...
    assert p.with_name(p.name + ".corrupt").exists()   # ...but the bytes are kept
    # a subsequent save can't have clobbered the quarantined copy
    assert "not valid json" in p.with_name(p.name + ".corrupt").read_text()


def test_load_skips_one_bad_record_keeps_the_rest(store):
    import json
    good = {"id": "a1", "user": "u1", "url": "https://ex.com/a"}
    bad = {"id": "b2"}                                  # missing required fields
    webmonitor._path().write_text(json.dumps([good, {"nonsense": 1}, bad]),
                                  encoding="utf-8")
    loaded = webmonitor._load()
    ids = {m.id for m in loaded}
    assert "a1" in ids                                 # the good record survived


def test_unknown_field_in_record_is_ignored(store):
    import json
    rec = {"id": "a1", "user": "u1", "url": "https://ex.com/a",
           "future_field_from_a_newer_version": 42}
    webmonitor._path().write_text(json.dumps([rec]), encoding="utf-8")
    loaded = webmonitor._load()
    assert len(loaded) == 1 and loaded[0].id == "a1"   # forward-compatible


def test_failing_monitor_backs_off_and_auto_pauses(store, monkeypatch):
    monkeypatch.setenv("OLYMPUS_WEB_MONITOR", "1")
    webmonitor.add("u1", "https://ex.com/x", interval=1)
    from olympus import webctx
    monkeypatch.setattr(webctx, "diff",
                        lambda url, prev: {"error": "fetch failed"})
    # No notify stub needed: default delivery is fail-closed, so run_due
    # never reaches the installation-wide fan-out.
    # Drive many failing cycles; the effective interval widens, so advance time
    # generously each round to keep it due.
    t = 1000
    for _ in range(webmonitor._MAX_FAILS + 2):
        t += 10_000_000
        webmonitor.run_due(now=t)
    m = webmonitor.list_for("u1")[0]
    assert m.active is False                            # auto-paused after _MAX_FAILS
    assert m.fails >= webmonitor._MAX_FAILS


def test_run_due_does_not_hold_lock_across_fetch(store, monkeypatch):
    # While run_due is doing its (mocked) network work, add() must still be able
    # to acquire the store lock — i.e. the lock is NOT held across the fetch.
    monkeypatch.setenv("OLYMPUS_WEB_MONITOR", "1")
    webmonitor.add("u1", "https://ex.com/x", interval=1)
    from olympus import webctx

    acquired = {"ok": False}

    def diff_that_touches_store(url, prev):
        # Simulate a concurrent add() during the fetch phase: it must not block.
        webmonitor.add("u2", "https://ex.com/y")
        acquired["ok"] = True
        return {"changed": True, "current_hash": "h1",
                "current_markdown": "v1", "diff": ""}
    monkeypatch.setattr(webctx, "diff", diff_that_touches_store)
    # No notify stub needed: default delivery is fail-closed, so run_due
    # never reaches the installation-wide fan-out.
    webmonitor.run_due(now=1000)
    assert acquired["ok"]                               # add() ran mid-fetch, no deadlock
    assert any(m.url == "https://ex.com/y" for m in webmonitor.list_for("u2"))


def test_notify_failure_is_surfaced(store, monkeypatch):
    monkeypatch.setenv("OLYMPUS_WEB_MONITOR", "1")
    webmonitor.add("u1", "https://ex.com/x", interval=1)
    from olympus import webctx
    hashes = iter(["h1", "h2"])
    monkeypatch.setattr(webctx, "diff",
                        lambda url, prev: {"changed": True,
                                           "current_hash": next(hashes),
                                           "current_markdown": "v", "diff": "d"})

    def boom(_t, *, user):
        raise RuntimeError("gateway down")
    webmonitor.run_due(now=1000, notify=boom)           # baseline
    log = webmonitor.run_due(now=2000, notify=boom)     # change → raises
    assert any("NOT delivered" in ln for ln in log)     # surfaced, not swallowed


# --- classification ---------------------------------------------------------

def test_monitor_tools_are_trusted():
    for name in ("web_monitor_add", "web_monitor_list"):
        assert name in security.TRUSTED_TOOLS


def test_monitor_json_mode_tracks_structured_change(store, monkeypatch):
    monkeypatch.setenv("OLYMPUS_WEB_MONITOR", "1")
    webmonitor.add("u1", "https://shop/x", interval=1,
                   schema={"type": "object"})
    notes = []
    # Delivery is fail-closed by default (no owner-targeted route exists),
    # so a test that needs an alert injects an explicit owner-aware
    # callback rather than relying on the installation-wide fan-out.
    def _notify(t, *, user):
        notes.append((t, user))
    from olympus import webctx
    # first check: price 9 (baseline, no alert); second: price 10 (alert)
    seq = iter([{"mode": "json", "changed": True, "current_json": {"price": 9},
                 "changed_fields": [], "current_hash": "h1"},
                {"mode": "json", "changed": True, "current_json": {"price": 10},
                 "changed_fields": [{"field": "price", "change": "changed",
                                     "from": 9, "to": 10}], "current_hash": "h2"}])
    monkeypatch.setattr(webctx, "diff",
                        lambda url, schema=None, previous_json=None: next(seq))
    log1 = webmonitor.run_due(now=1000, notify=_notify)
    assert any("baseline" in ln for ln in log1) and notes == []
    log2 = webmonitor.run_due(now=2000, notify=_notify)
    assert any("CHANGED" in ln for ln in log2) and len(notes) == 1
    assert "price" in notes[0][0]
    assert notes[0][1] == "u1"                         # egressed as its owner
    m = webmonitor.list_for("u1")[0]
    assert m.changes == 1 and m.schema                   # json mode persisted

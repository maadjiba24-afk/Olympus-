"""Web reflection — discoveries from accumulated domain lore (opt-in, dedup)."""

import pytest

from olympus import config, domainlore, webreflect


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.delenv("OLYMPUS_REPLAY", raising=False)
    monkeypatch.setenv("OLYMPUS_WEB_REFLECT", "1")
    monkeypatch.delenv("OLYMPUS_WEB_LORE", raising=False)
    return tmp_path


def _seed():
    domainlore.observe("https://a.com/", ok=True, has_jsonld=True)
    domainlore.observe("https://b.com/", ok=True, has_jsonld=True,
                       feed_url="https://b.com/f.xml")
    domainlore.observe("https://c.com/", ok=True, actions_helped=True)
    domainlore.observe("https://c.com/", ok=True, actions_helped=True)


def test_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.delenv("OLYMPUS_WEB_REFLECT", raising=False)
    assert webreflect.enabled() is False
    assert webreflect.run_due(0) == []


def test_inert_under_replay(env, monkeypatch):
    monkeypatch.setenv("OLYMPUS_REPLAY", "1")
    assert webreflect.enabled() is False


def test_discoveries_from_lore(env):
    _seed()
    kinds = {d["title"] for d in webreflect.discoveries()}
    assert any("JSON-LD" in t for t in kinds)
    assert any("feed" in t for t in kinds)
    assert any("interaction" in t for t in kinds)


def test_needs_minimum_corpus(env):
    domainlore.observe("https://only.com/", ok=True, has_jsonld=True)
    assert webreflect.discoveries() == []          # < _MIN_DOMAINS


def test_run_due_surfaces_once_then_dedups(env):
    _seed()
    notes = []
    log1 = webreflect.run_due(now=10**9, notify=lambda t: notes.append(t))
    assert log1 and notes                           # surfaced + notified
    # cadence not elapsed → nothing
    assert webreflect.run_due(now=10**9 + 5, notify=lambda t: notes.append(t)) == []
    # cadence elapsed but no NEW patterns → still nothing new
    log3 = webreflect.run_due(now=10**9 + webreflect._every() + 10,
                              notify=lambda t: notes.append(t))
    assert log3 == []
    assert len(notes) == 1                          # notified exactly once


def test_lesson_persisted(env, monkeypatch):
    _seed()
    saved = {}
    monkeypatch.setattr("olympus.memory.save",
                        lambda cat, title, body: saved.update(
                            {"cat": cat, "body": body}))
    webreflect.run_due(now=10**9, notify=lambda t: None)
    assert saved.get("cat") == "lessons" and "JSON-LD" in saved.get("body", "")

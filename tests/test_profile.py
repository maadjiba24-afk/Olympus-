"""Tests for the per-user profile card and tool prefix caching."""

import pytest

from olympus import profile, prefs, config, llm


@pytest.fixture(autouse=True)
def tmp_memory(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)


# --- profile card --------------------------------------------------------

def test_empty_profile_costs_nothing():
    assert profile.card("u") == ""          # no padding when there's nothing


def test_about_note_appears():
    profile.set_about("u", "Founder of Acme; keep replies short")
    card = profile.card("u")
    assert "Founder of Acme" in card and "What you know about this user" in card


def test_structured_facts_appear():
    profile.set_fact("u", "company", "Acme")
    profile.set_fact("u", "timezone", "CET")
    card = profile.card("u")
    assert "Company: Acme" in card and "Timezone: CET" in card


def test_clearing_about_and_facts():
    profile.set_about("u", "hi")
    profile.set_fact("u", "role", "CEO")
    profile.set_about("u", "")              # clear the note
    profile.set_fact("u", "role", "")       # clear the fact
    assert profile.card("u") == ""


def test_language_shown_unless_auto():
    prefs.set("u", "language", "French")
    assert "Preferred language: French" in profile.card("u")
    prefs.set("u", "language", "auto")
    assert "Preferred language" not in profile.card("u")


def test_autonomy_only_shown_when_off_default():
    assert profile.card("u") == ""          # default L1 → not stated
    prefs.set("u", "autonomy", 3)
    assert "Autonomy preference: L3" in profile.card("u")


def test_granted_scopes_shown():
    prefs.set("u", "scopes", ["gmail.send", "calendar.events"])
    card = profile.card("u")
    assert "gmail.send" in card and "calendar.events" in card


def test_clear_wipes_profile_not_other_prefs():
    profile.set_about("u", "note")
    prefs.set("u", "autonomy", 2)
    profile.clear("u")
    assert profile.get("u") == {}
    assert prefs.get("u", "autonomy") == 2  # unrelated settings survive


def test_profile_is_per_user():
    profile.set_about("alice", "alice's note")
    assert profile.card("bob") == ""


# --- tool prefix caching -------------------------------------------------

def test_cache_tools_marks_last_only():
    tools = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    out = llm._cache_tools(tools)
    assert "cache_control" not in out[0] and "cache_control" not in out[1]
    assert out[2]["cache_control"] == {"type": "ephemeral"}


def test_cache_tools_does_not_mutate_caller():
    tools = [{"name": "a"}, {"name": "b"}]
    llm._cache_tools(tools)
    assert "cache_control" not in tools[-1]   # original list untouched


def test_cache_tools_handles_empty():
    assert llm._cache_tools(None) is None
    assert llm._cache_tools([]) == []


# --- the card actually reaches Zeus's routing context --------------------

def test_card_injected_into_router_system(monkeypatch):
    from olympus import orchestrator, backend
    profile.set_about("zeususer", "I run a bakery called Crumb")
    captured = {}

    def fake_complete_json(settings, system, messages, schema, effort="high"):
        captured["system"] = system
        return {"mode": "direct", "direct_reply": "ok", "specialists": [],
                "brief": None, "needs_verification": False}

    monkeypatch.setattr(backend, "complete_json", fake_complete_json)
    bot = orchestrator.Olympus(user="zeususer")
    bot._route("hello")
    assert "Crumb" in captured["system"]    # the profile rode into the prompt

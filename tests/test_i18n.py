"""Tests for multilingual support: preferences, directives, per-user isolation."""

from olympus import i18n, memory, orchestrator, prefs


def test_normalize_aliases():
    assert i18n.normalize("spanish") == "Spanish"
    assert i18n.normalize("ES") == "Spanish"
    assert i18n.normalize("français") == "French"
    assert i18n.normalize("中文") == "Chinese"
    assert i18n.normalize("auto") is None
    assert i18n.normalize("match") is None
    # unknown but real language name is trusted, title-cased
    assert i18n.normalize("tagalog") == "Tagalog"


def test_preference_roundtrip_and_isolation():
    assert i18n.get_preference("alice") is None
    i18n.set_preference("alice", "Spanish")
    i18n.set_preference("bob", "Japanese")
    assert i18n.get_preference("alice") == "Spanish"
    assert i18n.get_preference("bob") == "Japanese"     # isolated per user
    # auto clears the preference
    i18n.set_preference("alice", "auto")
    assert i18n.get_preference("alice") is None


def test_directive_reflects_preference():
    auto = i18n.directive("nouser")
    assert "SAME language" in auto                       # fallback: match user
    i18n.set_preference("fruser", "French")
    d = i18n.directive("fruser")
    assert "in French" in d and "natively" in d


def test_content_directive_targets_language():
    i18n.set_preference("u", "German")
    assert "German" in i18n.content_directive("u")
    assert "user's own language" in i18n.content_directive("nopref")


def test_set_language_messages():
    assert "Spanish" in i18n.set_preference("u2", "spanish")
    assert "automatic" in i18n.set_preference("u2", "auto")


def test_orchestrator_set_language_persists():
    bot = orchestrator.Olympus(user="carol")
    msg = bot.set_language("Arabic")
    assert "Arabic" in msg
    # a fresh bot for the same user inherits the preference
    assert prefs.get("carol", "language") == "Arabic"
    assert "in Arabic" in i18n.directive("carol")


def test_route_and_synthesis_include_language_directive(monkeypatch):
    """The language directive must reach Zeus's user-facing generation steps."""
    captured = {}

    def fake_json(settings, system, messages, schema, effort="high"):
        # First call = routing (carries the language directive); a later call is
        # the M4 direct-verify screen (internal, English-only by design).
        captured.setdefault("route_system", system)
        return {"mode": "direct", "direct_reply": "hola",
                "specialists": [], "brief": None, "needs_verification": False}

    monkeypatch.setattr(orchestrator.backend, "complete_json", fake_json)
    bot = orchestrator.Olympus(user="es")
    bot.set_language("Spanish")
    bot.ask("hola")
    assert "LANGUAGE:" in captured["route_system"]
    assert "in Spanish" in captured["route_system"]

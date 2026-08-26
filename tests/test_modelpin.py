"""Per-conversation model pins (/model) and per-specialist overrides."""

import json

from olympus import config, gateway, modelpin, orchestrator


def _pool_env(monkeypatch):
    monkeypatch.setenv("OLYMPUS_PROVIDER", "anthropic")
    monkeypatch.setenv("OLYMPUS_MODEL", "claude-opus-5")
    monkeypatch.setenv("OLYMPUS_API_KEY", "k1")
    monkeypatch.setenv("OLYMPUS_MODELS", json.dumps([
        {"provider": "openai", "model": "gpt-5", "api_key": "k2"},
        {"provider": "anthropic", "model": "claude-haiku-4-5", "api_key": "k3"},
    ]))


def test_shorthand_matches_pool_member(monkeypatch):
    _pool_env(monkeypatch)
    pool = config.ModelPool.from_env()
    assert modelpin.match(pool, "gpt").model == "gpt-5"
    assert modelpin.match(pool, "haiku").model == "claude-haiku-4-5"
    assert modelpin.match(pool, "opus").model == "claude-opus-5"
    assert modelpin.match(pool, "gemini") is None


def test_set_pin_validates_and_persists(monkeypatch):
    _pool_env(monkeypatch)
    out = modelpin.set_pin("u1", "haiku")
    assert "pinned" in out.lower()
    assert modelpin.resolve("u1").model == "claude-haiku-4-5"
    out = modelpin.set_pin("u1", "auto")
    assert "cleared" in out.lower()
    assert modelpin.resolve("u1") is None


def test_unknown_pin_rejected_and_lists_options(monkeypatch):
    _pool_env(monkeypatch)
    out = modelpin.set_pin("u1", "llama")
    assert "No configured model" in out and "gpt-5" in out
    assert modelpin.get_pin("u1") is None


def test_pinned_conversation_narrows_the_pool(monkeypatch):
    _pool_env(monkeypatch)
    modelpin.set_pin("u2", "haiku")
    bot = orchestrator.Olympus(user="u2")
    assert [m.model for m in bot.pool.members] == ["claude-haiku-4-5"]
    assert bot.settings.model == "claude-haiku-4-5"


def test_byok_sessions_are_never_pinned(monkeypatch):
    _pool_env(monkeypatch)
    modelpin.set_pin("u3", "haiku")
    visitor = config.Settings(provider="openai", model="gpt-5",
                              api_key="visitor-key")
    bot = orchestrator.Olympus(user="u3", settings=visitor)
    assert bot.settings.api_key == "visitor-key"
    assert bot.settings.model == "gpt-5"


def test_stale_pin_fails_open(monkeypatch):
    _pool_env(monkeypatch)
    modelpin.set_pin("u4", "gpt")
    monkeypatch.setenv("OLYMPUS_MODELS", "[]")     # gpt member removed
    assert modelpin.resolve("u4") is None
    bot = orchestrator.Olympus(user="u4")
    assert bot.settings.model == "claude-opus-5"   # normal selection


def test_specialist_override_env(monkeypatch):
    _pool_env(monkeypatch)
    monkeypatch.setenv("OLYMPUS_SPECIALIST_MODELS",
                       json.dumps({"hephaestus": "haiku"}))
    pool = config.ModelPool.from_env()
    assert pool.for_specialist("hephaestus").model == "claude-haiku-4-5"
    # others unaffected: coding override doesn't leak to reasoning specialists
    assert pool.for_specialist("plutus").model != "claude-haiku-4-5"


def test_specialist_override_malformed_ignored(monkeypatch):
    _pool_env(monkeypatch)
    monkeypatch.setenv("OLYMPUS_SPECIALIST_MODELS", "{bad json")
    assert config.specialist_model_overrides() == {}
    pool = config.ModelPool.from_env()
    pool.for_specialist("hephaestus")              # must not raise


def test_chat_model_command(monkeypatch):
    _pool_env(monkeypatch)
    uid = gateway.principal_id("u5")
    bots = {uid: object()}
    out = gateway.reply_for(bots, "u5", "/model haiku")
    assert any("pinned" in c.lower() for c in out)
    assert uid not in bots                         # session rebuilt with pin
    out = gateway.reply_for({}, "u5", "/model")
    assert any("claude-haiku-4-5" in c for c in out)

"""BYOK enforcement + per-user daily cost cap for the public web instance."""

from olympus import config, web


# --- config flags --------------------------------------------------------

def test_require_byok_flag(monkeypatch):
    monkeypatch.delenv("OLYMPUS_REQUIRE_BYOK", raising=False)
    assert config.require_byok() is False
    for on in ("1", "true", "YES", "on"):
        monkeypatch.setenv("OLYMPUS_REQUIRE_BYOK", on)
        assert config.require_byok() is True
    monkeypatch.setenv("OLYMPUS_REQUIRE_BYOK", "0")
    assert config.require_byok() is False


def test_daily_chat_limit_flag(monkeypatch):
    monkeypatch.delenv("OLYMPUS_DAILY_CHATS", raising=False)
    assert config.daily_chat_limit() == 0
    monkeypatch.setenv("OLYMPUS_DAILY_CHATS", "25")
    assert config.daily_chat_limit() == 25
    monkeypatch.setenv("OLYMPUS_DAILY_CHATS", "junk")
    assert config.daily_chat_limit() == 0          # bad value -> unlimited


# --- BYOK detection ------------------------------------------------------

def test_brought_own_key_detects_user_credentials():
    assert web._brought_own_key({"api_key": "sk-123"}) is True
    assert web._brought_own_key({"base_url": "http://localhost:11434/v1"}) is True
    assert web._brought_own_key({"extra": {"api_key": "k2"}}) is True
    # only a model name / language, no actual key -> not BYOK
    assert web._brought_own_key({"model": "claude-opus-4-8"}) is False
    assert web._brought_own_key({"api_key": "   "}) is False
    assert web._brought_own_key({}) is False


# --- daily cap -----------------------------------------------------------

def test_daily_limited_caps_per_key(monkeypatch):
    web._DAILY.clear()
    # limit of 3: first three pass, the fourth is blocked
    assert [web._daily_limited("u:alice", 3) for _ in range(4)] == \
        [False, False, False, True]
    # a different user is independent
    assert web._daily_limited("u:bob", 3) is False


def test_daily_limited_zero_is_unlimited():
    web._DAILY.clear()
    assert all(web._daily_limited("u:x", 0) is False for _ in range(100))

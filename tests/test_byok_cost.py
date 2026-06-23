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


def test_daily_count_and_bump():
    web._DAILY.clear()
    assert web._daily_count("free:a") == 0
    web._daily_bump("free:a")
    web._daily_bump("free:a")
    assert web._daily_count("free:a") == 2
    assert web._daily_count("free:b") == 0      # independent per key


# --- free-allowance policy (BYOK as a limit, not a wall) -----------------

def test_free_chats_flag(monkeypatch):
    monkeypatch.delenv("OLYMPUS_FREE_CHATS", raising=False)
    assert config.free_chats() == 0
    monkeypatch.setenv("OLYMPUS_FREE_CHATS", "5")
    assert config.free_chats() == 5
    monkeypatch.setenv("OLYMPUS_FREE_CHATS", "junk")
    assert config.free_chats() == 0


def test_byok_user_always_allowed(monkeypatch):
    monkeypatch.setenv("OLYMPUS_REQUIRE_BYOK", "1")
    monkeypatch.setenv("OLYMPUS_FREE_CHATS", "3")
    assert web._key_decision(brought=True, free_used=999) == ""


def test_free_allowance_then_requires_key(monkeypatch):
    monkeypatch.setenv("OLYMPUS_FREE_CHATS", "3")
    monkeypatch.delenv("OLYMPUS_REQUIRE_BYOK", raising=False)
    assert web._key_decision(brought=False, free_used=0) == ""
    assert web._key_decision(brought=False, free_used=2) == ""
    assert web._key_decision(brought=False, free_used=3) == "over_free"
    assert web._key_decision(brought=False, free_used=10) == "over_free"


def test_free_allowance_overrides_require_byok(monkeypatch):
    monkeypatch.setenv("OLYMPUS_REQUIRE_BYOK", "1")
    monkeypatch.setenv("OLYMPUS_FREE_CHATS", "2")
    assert web._key_decision(brought=False, free_used=0) == ""      # free taste first
    assert web._key_decision(brought=False, free_used=2) == "over_free"


def test_hard_byok_when_no_free_allowance(monkeypatch):
    monkeypatch.setenv("OLYMPUS_REQUIRE_BYOK", "1")
    monkeypatch.setenv("OLYMPUS_FREE_CHATS", "0")
    assert web._key_decision(brought=False, free_used=0) == "byok"


def test_no_policy_means_operator_funded(monkeypatch):
    monkeypatch.delenv("OLYMPUS_REQUIRE_BYOK", raising=False)
    monkeypatch.setenv("OLYMPUS_FREE_CHATS", "0")
    assert web._key_decision(brought=False, free_used=100) == ""

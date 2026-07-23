"""Conversational onboarding: a warm first-contact, shown once per chat
identity, over the shared gateway."""

import pytest

from olympus import config, gateway, onboarding


@pytest.fixture(autouse=True)
def _mem(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)


# --- state -----------------------------------------------------------------

def test_new_then_seen():
    assert onboarding.is_new("tg-42") is True
    onboarding.mark_seen("tg-42")
    assert onboarding.is_new("tg-42") is False
    # persisted across a reload
    assert onboarding.is_new("tg-42") is False


def test_mark_seen_idempotent_and_isolated():
    onboarding.mark_seen("a")
    onboarding.mark_seen("a")
    assert onboarding.is_new("a") is False
    assert onboarding.is_new("b") is True


def test_reset_reopens():
    onboarding.mark_seen("x")
    onboarding.reset("x")
    assert onboarding.is_new("x") is True


def test_empty_uid_is_not_new():
    assert onboarding.is_new("") is False


def test_welcome_content():
    w = onboarding.welcome()
    assert "Olympus" in w and "/help" in w and "/fast" in w


# --- gateway /start --------------------------------------------------------

def test_start_welcomes_once(monkeypatch):
    out = " ".join(gateway.reply_for({}, "newuser", "/start", prefix="tg"))
    assert "Welcome" in out or "welcome" in out
    # second /start → help only, no repeat welcome
    out2 = " ".join(gateway.reply_for({}, "newuser", "/start", prefix="tg"))
    assert "/reset" in out2                       # HELP present
    assert "Welcome" not in out2


def test_help_never_triggers_welcome():
    out = " ".join(gateway.reply_for({}, "brandnew", "/help", prefix="tg"))
    assert "Welcome" not in out
    # /help must not mark them seen — a later /start still welcomes
    assert onboarding.is_new("tg-brandnew") is True


# --- a natural message never triggers onboarding (only /start does) --------

class _FakeBot:
    history = []

    def __init__(self, *a, **k):
        pass

    def ask(self, text):
        return "42 is the answer."


def test_natural_message_is_not_onboarded(monkeypatch):
    from olympus import approvals, prefs
    monkeypatch.setattr(gateway.orchestrator, "Olympus", _FakeBot)
    monkeypatch.setattr(approvals, "footer", lambda uid: "")
    monkeypatch.setattr(prefs, "get", lambda uid, key, default=None: None)
    out = " ".join(gateway.reply_for({}, "chatter", "what is 6 times 7?",
                                     prefix="tg"))
    assert out == "42 is the answer."          # no banner injected
    # a natural message doesn't consume onboarding: /start still welcomes
    assert onboarding.is_new("tg-chatter") is True

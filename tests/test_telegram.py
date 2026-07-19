"""Telegram transport: untrusted-by-default access, pairing flow, reply
quoting, and streaming progress. All Bot API I/O is monkeypatched."""

import pytest

from olympus import gateway, pairing, telegram


@pytest.fixture
def api_calls(monkeypatch):
    """Capture every Bot API call; sendMessage returns a fake message id."""
    calls = []

    def fake_call(token, method, **params):
        calls.append((method, params))
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": 900 + len(calls)}}
        return {"ok": True, "result": {}}

    monkeypatch.setattr(telegram, "_call", fake_call)
    return calls


# --- access control ---------------------------------------------------------

def test_default_policy_is_pairing_and_denies_strangers(monkeypatch):
    monkeypatch.delenv("TELEGRAM_DM_POLICY", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    monkeypatch.delenv("TELEGRAM_NOTIFY_CHAT_ID", raising=False)
    assert telegram._policy() == "pairing"
    assert telegram._allowed(12345) is False


def test_paired_chat_is_allowed(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    code = pairing.issue_code("telegram")
    pairing.redeem("telegram", code, 12345)
    assert telegram._allowed(12345) is True
    assert telegram._allowed(99999) is False


def test_env_allowlist_and_notify_chat_are_always_allowed(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "111, 222")
    monkeypatch.setenv("TELEGRAM_NOTIFY_CHAT_ID", "333")
    for chat_id in (111, 222, 333):
        assert telegram._allowed(chat_id) is True
    assert telegram._allowed(444) is False


def test_open_policy_allows_everyone(monkeypatch):
    monkeypatch.setenv("TELEGRAM_DM_POLICY", "open")
    assert telegram._allowed(31337) is True


def test_allowlist_policy_ignores_pairing(monkeypatch):
    monkeypatch.setenv("TELEGRAM_DM_POLICY", "allowlist")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "111")
    code = pairing.issue_code("telegram")
    pairing.redeem("telegram", code, 555)
    assert telegram._allowed(111) is True
    assert telegram._allowed(555) is False


def test_stranger_gets_pair_hint_once_per_hour(api_calls, monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    telegram._DENIED_AT.clear()
    telegram._handle("tok", {}, 777, "hello", message_id=1)
    telegram._handle("tok", {}, 777, "hello again", message_id=2)
    hints = [p for (m, p) in api_calls
             if m == "sendMessage" and "private" in p.get("text", "")]
    assert len(hints) == 1     # rate-limited, not per-message


def test_pair_command_pairs_the_chat(api_calls, monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    code = pairing.issue_code("telegram")
    telegram._handle("tok", {}, 888, f"/pair {code}", message_id=5)
    assert pairing.is_paired("telegram", 888) is True
    sent = [p for (m, p) in api_calls if m == "sendMessage"]
    assert any("Paired" in p.get("text", "") for p in sent)


def test_bad_pair_code_does_not_pair(api_calls):
    telegram._handle("tok", {}, 889, "/pair WRONG123", message_id=5)
    assert pairing.is_paired("telegram", 889) is False


# --- reply quoting -----------------------------------------------------------

def test_send_quotes_only_first_chunk(api_calls):
    telegram._send("tok", 1, "x" * (telegram.CHUNK + 10), reply_to=42)
    sends = [p for (m, p) in api_calls if m == "sendMessage"]
    assert len(sends) == 2
    assert sends[0]["reply_to_message_id"] == 42
    assert sends[0]["allow_sending_without_reply"] is True
    assert "reply_to_message_id" not in sends[1]


# --- streaming progress -------------------------------------------------------

def test_progress_placeholder_edited_to_final_answer(api_calls):
    p = telegram._Progress("tok", 1, reply_to=7)
    p.start()
    assert p.message_id is not None
    p.finish("the answer")
    edits = [q for (m, q) in api_calls if m == "editMessageText"]
    assert edits and edits[-1]["text"] == "the answer"
    assert edits[-1]["message_id"] == p.message_id


def test_progress_disabled_falls_back_to_plain_send(api_calls, monkeypatch):
    monkeypatch.setenv("TELEGRAM_PROGRESS", "0")
    p = telegram._Progress("tok", 1, reply_to=7)
    p.start()
    assert p.message_id is None
    p.finish("plain")
    sends = [q for (m, q) in api_calls if m == "sendMessage"]
    assert sends and sends[-1]["text"] == "plain"
    assert sends[-1]["reply_to_message_id"] == 7


def test_progress_long_answer_spills_into_followups(api_calls):
    p = telegram._Progress("tok", 1, reply_to=None)
    p.start()
    p.finish("y" * (telegram.CHUNK + 5))
    edits = [q for (m, q) in api_calls if m == "editMessageText"]
    sends = [q for (m, q) in api_calls if m == "sendMessage"]
    assert len(edits[-1]["text"]) == telegram.CHUNK
    assert sends[-1]["text"] == "y" * 5


# --- routing through the shared gateway --------------------------------------

def test_text_routes_through_gateway_with_raw_uid(api_calls, monkeypatch):
    monkeypatch.setenv("TELEGRAM_DM_POLICY", "open")
    seen = {}

    def fake_reply_for(bots, user_key, text, prefix="ol", uid=None):
        seen.update(user_key=user_key, text=text, prefix=prefix, uid=uid)
        return ["ok"]

    monkeypatch.setattr(gateway, "reply_for", fake_reply_for)
    telegram._handle("tok", {}, -100123, "what's up", message_id=9)
    assert seen["uid"] == "tg--100123"      # raw chat id survives (groups)
    assert seen["prefix"] == "tg"
    assert seen["text"] == "what's up"


def test_slash_command_skips_progress_placeholder(api_calls, monkeypatch):
    monkeypatch.setenv("TELEGRAM_DM_POLICY", "open")
    monkeypatch.setattr(gateway, "reply_for",
                        lambda *a, **k: ["help text"])
    telegram._handle("tok", {}, 5, "/help", message_id=3)
    sends = [p for (m, p) in api_calls if m == "sendMessage"]
    assert [p["text"] for p in sends] == ["help text"]
    assert sends[0]["reply_to_message_id"] == 3
    assert not any(m == "editMessageText" for (m, _) in api_calls)

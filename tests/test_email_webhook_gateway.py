"""#19: email gateway + inbound webhook gateway (testable cores)."""

import pytest

from olympus import email_gateway, gateway, gmail, webhook_gateway


# --- email gateway --------------------------------------------------------

def test_email_handle_replies_via_gmail(monkeypatch):
    sent = {}
    monkeypatch.setattr(gmail, "send",
                        lambda to, subject, body: sent.update(
                            to=to, subject=subject, body=body) or {"id": "1"})
    monkeypatch.setattr(gateway, "reply_for",
                        lambda bots, user, text, prefix="ol": ["the answer"])
    monkeypatch.delenv("OLYMPUS_EMAIL_ALLOW", raising=False)
    msg = {"id": "m1", "sender": "a@x.com", "subject": "Question", "body": "hi?"}
    reply = email_gateway.handle_message({}, msg)
    assert reply == "the answer"
    assert sent["to"] == "a@x.com"
    assert sent["subject"] == "Re: Question"     # Re: prefix added


def test_email_allowlist_blocks_stranger(monkeypatch):
    monkeypatch.setenv("OLYMPUS_EMAIL_ALLOW", "boss@x.com")
    called = {"sent": False}
    monkeypatch.setattr(gmail, "send",
                        lambda *a, **k: called.__setitem__("sent", True))
    monkeypatch.setattr(gateway, "reply_for", lambda *a, **k: ["x"])
    reply = email_gateway.handle_message({}, {"sender": "rando@y.com",
                                              "subject": "s", "body": "b"})
    assert reply is None and called["sent"] is False


def test_email_allowlist_allows_listed(monkeypatch):
    monkeypatch.setenv("OLYMPUS_EMAIL_ALLOW", "boss@x.com, cfo@x.com")
    monkeypatch.setattr(gmail, "send", lambda *a, **k: {"id": "1"})
    monkeypatch.setattr(gateway, "reply_for", lambda *a, **k: ["ok"])
    assert email_gateway.handle_message(
        {}, {"sender": "cfo@x.com", "subject": "s", "body": "b"}) == "ok"


def test_email_poll_marks_read(monkeypatch):
    read = []
    monkeypatch.setattr(gmail, "list_unread", lambda max_results=10: [
        {"id": "m1", "sender": "a@x.com", "subject": "s", "body": "b"}])
    monkeypatch.setattr(gmail, "mark_read", lambda mid: read.append(mid))
    monkeypatch.setattr(gmail, "send", lambda *a, **k: {"id": "1"})
    monkeypatch.setattr(gateway, "reply_for", lambda *a, **k: ["r"])
    monkeypatch.delenv("OLYMPUS_EMAIL_ALLOW", raising=False)
    log = email_gateway.poll_once({})
    assert read == ["m1"] and any("replied" in l for l in log)


def test_reply_subject_no_double_re():
    assert email_gateway._reply_subject("Re: hi") == "Re: hi"
    assert email_gateway._reply_subject("hi") == "Re: hi"


def test_gmail_parse_address():
    assert gmail.parse_address("Jane Doe <jane@x.com>") == "jane@x.com"
    assert gmail.parse_address("bob@y.com") == "bob@y.com"


# --- webhook gateway ------------------------------------------------------

def test_webhook_handle_payload(monkeypatch):
    monkeypatch.setattr(gateway, "reply_for",
                        lambda bots, user, text, prefix="ol": ["hello " + user])
    out = webhook_gateway.handle_payload({}, {"user": "u1", "text": "hi"})
    assert out == {"reply": "hello u1"}


def test_webhook_missing_text_raises():
    with pytest.raises(ValueError):
        webhook_gateway.handle_payload({}, {"user": "u1"})


def test_webhook_anonymous_user(monkeypatch):
    seen = {}
    monkeypatch.setattr(gateway, "reply_for",
                        lambda bots, user, text, prefix="ol":
                        seen.update(user=user) or ["ok"])
    webhook_gateway.handle_payload({}, {"text": "hi"})
    assert seen["user"] == "anonymous"


def test_webhook_secret_check(monkeypatch):
    monkeypatch.setenv("OLYMPUS_WEBHOOK_SECRET", "s3cret")
    assert webhook_gateway._secret_ok("s3cret") is True
    assert webhook_gateway._secret_ok("wrong") is False
    monkeypatch.delenv("OLYMPUS_WEBHOOK_SECRET", raising=False)
    assert webhook_gateway._secret_ok("") is True      # unset → open

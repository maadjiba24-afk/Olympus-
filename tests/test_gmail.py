"""Tests for the Gmail integration: adapter, actions on the spine, safety."""

import pytest

from olympus import actions, builtin_actions, gmail, security
from olympus.specialists import SPECIALISTS


@pytest.fixture()
def fake_gmail(monkeypatch):
    """Mock the HTTP layer so no network/token is needed."""
    calls = []

    def fake_request(method, path, body=None):
        calls.append((method, path, body))
        if path.startswith("/messages?"):
            return {"messages": [{"id": "m1"}, {"id": "m2"}]}
        if path.startswith("/messages/m") and "modify" not in path:
            return {"snippet": "hello there",
                    "payload": {"headers": [
                        {"name": "From", "value": "a@b.com"},
                        {"name": "Subject", "value": "Hi"}]}}
        if path == "/messages/send":
            return {"id": "sent1"}
        if path == "/drafts":
            return {"id": "draft1"}
        return {"ok": True}

    monkeypatch.setattr(gmail, "_request", fake_request)
    monkeypatch.setenv("GMAIL_ACCESS_TOKEN", "tok")
    return calls


# --- adapter -------------------------------------------------------------

def test_list_inbox_summarizes(fake_gmail):
    out = gmail.list_inbox("in:inbox", 5)
    assert "a@b.com" in out and "Subject: Hi" in out and "hello there" in out


def test_send_builds_raw_message(fake_gmail):
    r = gmail.send("x@y.z", "Subject", "Body")
    assert r["id"] == "sent1"
    method, path, body = [c for c in fake_gmail if c[1] == "/messages/send"][0]
    assert method == "POST" and "raw" in body        # base64url MIME sent


def test_draft_create_and_delete(fake_gmail):
    r = gmail.create_draft("x@y.z", "S", "B")
    assert r["id"] == "draft1"
    gmail.delete_draft("draft1")
    assert ("DELETE", "/drafts/draft1", None) in fake_gmail


def test_not_configured_raises(monkeypatch):
    for k in ("GMAIL_ACCESS_TOKEN", "GMAIL_REFRESH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(gmail, "_token", {"value": None, "expires": 0.0})
    with pytest.raises(gmail.GmailError, match="not connected"):
        gmail._access_token()


# --- Gmail actions on the spine -----------------------------------------

def test_gmail_send_is_irreversible_and_gated(fake_gmail):
    a = actions.prepare("u", "gmail_send",
                        {"to": "x@y.z", "subject": "Hi", "body": "yo"})
    assert a.risk_class == actions.IRREVERSIBLE
    assert not actions.can_auto_execute(a)          # never auto, even at L4
    # no scope -> approval fails closed
    assert actions.approve("u", a.id).status == actions.FAILED
    # with scope -> executes
    actions.grant_scope("u", "gmail.send")
    a2 = actions.prepare("u", "gmail_send", {"to": "x@y.z", "subject": "H", "body": "b"})
    done = actions.approve("u", a2.id)
    assert done.status == actions.EXECUTED and done.result["sent"] is True


def test_gmail_draft_is_reversible(fake_gmail):
    actions.grant_scope("u", "gmail.compose")
    a = actions.prepare("u", "gmail_draft", {"to": "x@y.z", "subject": "S", "body": "B"})
    assert a.reversible is True
    actions.approve("u", a.id)
    undone = actions.undo("u", a.id)
    assert undone.status == actions.UNDONE
    assert ("DELETE", "/drafts/draft1", None) in fake_gmail


def test_gmail_archive_reversible(fake_gmail):
    actions.grant_scope("u", "gmail.modify")
    a = actions.prepare("u", "gmail_archive", {"message_id": "m1"})
    actions.approve("u", a.id)
    actions.undo("u", a.id)
    # archive removes INBOX, undo re-adds it
    bodies = [b for _, p, b in fake_gmail if "modify" in p]
    assert any("removeLabelIds" in str(b) for b in bodies)
    assert any("addLabelIds" in str(b) for b in bodies)


# --- the safety architecture (the whole point) --------------------------

def test_email_reading_is_treated_as_untrusted():
    assert security.should_wrap("read_inbox")
    assert security.should_wrap("read_email")


def test_angelos_reads_but_cannot_act_directly():
    """Angelos ingests untrusted email, so it must NOT hold direct action
    tools — only prepare_action, which is gated by human approval."""
    names = {d.get("name") for d in SPECIALISTS["angelos"].tool_defs("anthropic")}
    assert "read_inbox" in names and "prepare_action" in names
    # it ingests email -> direct actuators are stripped by capability separation
    assert "send_email" not in names and "call_webhook" not in names


def test_angelos_loadout_is_ingesting():
    from olympus import security
    defs = SPECIALISTS["angelos"].tool_defs("anthropic")
    assert security.loadout_ingests_external(defs)

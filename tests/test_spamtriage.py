"""Gap 2 — email spam triage. A read-only heuristic classifier that sorts the
inbox into important / promotions / spam / other. No model call, no side effect;
message content stays untrusted (the tool is wrapped like other inbox reads).
"""

import pytest

from olympus import spamtriage


# --- classification --------------------------------------------------------

def test_spam_scam_subject():
    v = spamtriage.classify("winner@lotto.biz",
                            "Congratulations, you have won the lottery!",
                            "claim your prize now, act now")
    assert v["category"] == "spam"


def test_phishing_is_spam():
    v = spamtriage.classify("security@paypa1.com",
                            "Verify your account or it will be suspended",
                            "confirm your password immediately")
    assert v["category"] == "spam"


def test_promotions_newsletter():
    v = spamtriage.classify("newsletter@store.com", "50% off summer sale",
                            "shop now — unsubscribe here")
    assert v["category"] == "promotions"


def test_noreply_sender_leans_promo():
    v = spamtriage.classify("no-reply@service.com", "Your weekly digest",
                            "here is what happened")
    assert v["category"] == "promotions"


def test_important_reply_thread():
    v = spamtriage.classify("jordan@client.com", "Re: contract review",
                            "can you send the signed copy?")
    assert v["category"] == "important"


def test_important_actionable():
    v = spamtriage.classify("boss@company.com", "Invoice payment due Friday",
                            "please review and approve")
    assert v["category"] == "important"


def test_other_when_no_signal():
    v = spamtriage.classify("alice@example.com", "hey", "just checking in")
    assert v["category"] == "other"


def test_none_subject_or_snippet_does_not_leak_literal_none():
    # a direct caller passing None must not put the string "None" in the scored
    # blob (would spuriously match a future 'none' pattern).
    v = spamtriage.classify("x@y.com", None, None)
    assert v["category"] == "other"       # no signal, no crash


def test_shouty_caps_subject_boosts_spam():
    v = spamtriage.classify("x@y.com", "FREE MONEY NOW!!!", "click here")
    assert v["category"] == "spam"


# --- triage() aggregation --------------------------------------------------

@pytest.fixture()
def fake_inbox(monkeypatch):
    from olympus import gmail
    msgs = [
        {"id": "1", "date": "d", "from": "newsletter@store.com",
         "subject": "40% off sale", "snippet": "unsubscribe"},
        {"id": "2", "date": "d", "from": "jordan@client.com",
         "subject": "Re: meeting", "snippet": "can you confirm the time?"},
        {"id": "3", "date": "d", "from": "winner@lotto.biz",
         "subject": "You have won!!!", "snippet": "claim your prize, act now"},
    ]
    monkeypatch.setattr(gmail, "messages_meta", lambda q="in:inbox", n=20: msgs)
    return msgs


def test_triage_buckets_and_counts(fake_inbox):
    t = spamtriage.triage()
    assert t["total"] == 3
    assert t["counts"]["promotions"] == 1
    assert t["counts"]["important"] == 1
    assert t["counts"]["spam"] == 1


def test_triage_degrades_on_error(monkeypatch):
    from olympus import gmail

    def boom(q="in:inbox", n=20):
        raise gmail.GmailError("not connected")

    monkeypatch.setattr(gmail, "messages_meta", boom)
    t = spamtriage.triage()
    assert t["total"] == 0 and "error" in t


def test_render_lists_categories(fake_inbox):
    out = spamtriage.render()
    assert "Important" in out and "Likely spam" in out and "Promotions" in out
    assert "[2]" in out            # the important message id shows


def test_render_no_messages(monkeypatch):
    from olympus import gmail
    monkeypatch.setattr(gmail, "messages_meta", lambda q="in:inbox", n=20: [])
    assert "No messages" in spamtriage.render()


# --- wiring ----------------------------------------------------------------

def test_tool_registered_wrapped_and_on_angelos():
    from olympus import tools, specialists, security
    assert "triage_inbox" in tools.EXTRA_TOOLS
    assert "triage_inbox" in tools.HANDLERS
    assert "triage_inbox" in specialists.SPECIALISTS["angelos"].extra_tools
    assert security.should_wrap("triage_inbox")     # untrusted content wrapped


def test_tool_handler_renders(fake_inbox):
    from olympus import tools
    out = tools.HANDLERS["triage_inbox"]("in:inbox", 20)
    assert "Triaged 3 message(s)" in out

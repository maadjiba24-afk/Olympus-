"""Tests for the opt-in cross-model learning pool + anonymization."""

from olympus import contrib, memory, orchestrator, security


# --- anonymization -------------------------------------------------------

def test_anonymize_strips_pii_and_secrets():
    text = ("Email me at jane.doe@example.com or call +1 (415) 555-2671. "
            "My account is 998877665544 and key sk-ant-abc123XYZ456789.")
    out = security.anonymize(text)
    assert "jane.doe@example.com" not in out and "[email]" in out
    assert "555-2671" not in out and "[phone]" in out
    assert "998877665544" not in out            # account number redacted
    assert "sk-ant-abc123XYZ456789" not in out and "[redacted-key]" in out


def test_anonymize_redacts_bare_long_number():
    out = security.anonymize("Order reference 84736251 shipped.")
    assert "84736251" not in out and "[number]" in out


def test_anonymize_keeps_normal_text():
    text = "Use a password manager and enable MFA on all 3 accounts."
    out = security.anonymize(text)
    assert "password manager" in out and "MFA" in out


# --- opt-in pool ---------------------------------------------------------

def test_contribution_is_opt_in_default_off():
    assert contrib.is_enabled("newuser") is False
    # offering without opting in stores nothing
    assert contrib.offer("newuser", "gpt-4o", "q", "a") is False
    assert contrib.count() == 0


def test_opt_in_then_contributes_anonymized_and_tagged():
    contrib.set_enabled("u1", True)
    assert contrib.is_enabled("u1") is True
    ok = contrib.offer("u1", "gpt-4o",
                       "My email is bob@corp.com, how do I price my SaaS?",
                       "Anchor on value; my number is 4155551234.")
    assert ok is True
    rows = contrib.recent()
    assert rows[-1]["model"] == "gpt-4o"
    # PII anonymized before entering the shared pool
    assert "bob@corp.com" not in rows[-1]["q"] and "[email]" in rows[-1]["q"]
    assert "4155551234" not in rows[-1]["a"]


def test_opt_out_stops_contribution():
    contrib.set_enabled("u2", True)
    contrib.offer("u2", "claude-opus-4-8", "q1", "a1")
    contrib.set_enabled("u2", False)
    before = contrib.count()
    contrib.offer("u2", "claude-opus-4-8", "q2", "a2")
    assert contrib.count() == before        # nothing added after opt-out


def test_digest_groups_by_model():
    contrib.set_enabled("u3", True)
    contrib.offer("u3", "gpt-4o", "qa", "aa")
    contrib.offer("u3", "gemini-2", "qb", "ab")
    d = contrib.digest()
    assert "From model: gpt-4o" in d and "From model: gemini-2" in d


def test_clear_old_bounds_queue():
    contrib.set_enabled("u4", True)
    for i in range(50):
        contrib.offer("u4", "m", f"q{i}", f"a{i}")
    removed = contrib.clear_old(keep=10)
    assert removed == 40
    assert contrib.count() == 10


# --- orchestrator integration -------------------------------------------

def test_set_contribute_via_bot():
    bot = orchestrator.Olympus(user="webuser")
    msg = bot.set_contribute(True)
    assert "ON" in msg
    assert contrib.is_enabled("webuser") is True
    assert "OFF" in bot.set_contribute(False)


def test_finish_contributes_only_when_opted_in(monkeypatch):
    # opted-out user: _finish must not contribute
    bot = orchestrator.Olympus(user="optout")
    bot._finish("question one", "answer one")
    assert contrib.count() == 0

    # opted-in user: _finish contributes the exchange tagged with the model
    bot2 = orchestrator.Olympus(user="optin")
    bot2.set_contribute(True)
    bot2._finish("question two", "answer two")
    rows = contrib.recent()
    assert any(r["q"].startswith("question two") for r in rows)


def test_privacy_isolation_of_optin_flag():
    contrib.set_enabled("alice", True)
    assert contrib.is_enabled("alice") is True
    assert contrib.is_enabled("bob") is False    # per-user, not global

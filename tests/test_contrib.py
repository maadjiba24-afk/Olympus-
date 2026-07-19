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


# --- Phase B: the contribution pool routed through the egress guard ------
# docs/DESIGN_BOUNDARY_LAYER.md Part 5 Phase B. When OLYMPUS_EGRESS_GUARD is on,
# the pool's redaction becomes a POOLED policy decision recorded in the log —
# equivalent to the direct anonymize path, plus auditable + secret-hold.

from olympus import egress, trace  # noqa: E402


def test_pool_redaction_via_guard_when_enabled(monkeypatch):
    monkeypatch.setenv("OLYMPUS_EGRESS_GUARD", "1")
    contrib.set_enabled("pb1", True)
    ok = contrib.offer("pb1", "gpt-4o",
                       "My email is carol@corp.com, how do I price?",
                       "Anchor on value; call 4155559876.")
    assert ok is True
    row = contrib.recent()[-1]
    # Redacted through guard(POOLED) exactly as the direct path would have.
    assert "carol@corp.com" not in row["q"] and "[email]" in row["q"]
    assert "4155559876" not in row["a"] and "[phone]" in row["a"]


def test_pool_holds_stored_secret_via_guard_and_drops(monkeypatch):
    # A real stored secret (key-shaped env var) that lands in the snapshot must
    # be HELD by the guard → the whole snapshot is dropped, nothing pooled.
    secret = "sk-live-9f8e7d6c5b4a3210zzz"
    monkeypatch.setenv("MYAPP_API_KEY", secret)
    monkeypatch.setenv("OLYMPUS_EGRESS_GUARD", "1")
    contrib.set_enabled("pb2", True)
    before = contrib.count()
    ok = contrib.offer("pb2", "gpt-4o", "how do I call the API?",
                       f"use the key {secret} in the header")
    assert ok is False
    assert contrib.count() == before        # nothing reached the pool


def test_pool_redaction_identical_guard_on_vs_off(monkeypatch):
    # Equivalence: guard(POOLED) redaction == the direct anonymize path.
    q = "reach me at dave@x.com or 4155551212 about pricing"
    a = "the plan token is fine; my number 9998887777 works too"
    monkeypatch.delenv("OLYMPUS_EGRESS_GUARD", raising=False)
    contrib.set_enabled("pb_off", True)
    assert contrib.offer("pb_off", "m", q, a) is True
    off = contrib.recent()[-1]
    monkeypatch.setenv("OLYMPUS_EGRESS_GUARD", "1")
    contrib.set_enabled("pb_on", True)
    assert contrib.offer("pb_on", "m", q, a) is True
    on = contrib.recent()[-1]
    assert on["q"] == off["q"] and on["a"] == off["a"]


def test_pool_guard_records_egress_decision(monkeypatch):
    # The whole point of Phase B: the redaction is now an auditable decision in
    # the signed Trace, not an inline side effect.
    monkeypatch.setenv("OLYMPUS_EGRESS_GUARD", "1")
    contrib.set_enabled("pb3", True)
    tr = trace.Trace("t")
    tok = trace.set_current(tr)
    try:
        contrib.offer("pb3", "gpt-4o", "email erin@co.com please",
                      "sure, ping 4155550000")
    finally:
        trace.reset_current(tok)
    pooled = [d for d in tr.decisions
              if d["decision_type"] == "egress"
              and d["rationale"]["channel"] == "pooled"]
    assert pooled, "expected a POOLED egress decision recorded"
    assert all(d["rationale"]["verdict"] in ("allow", "redact") for d in pooled)

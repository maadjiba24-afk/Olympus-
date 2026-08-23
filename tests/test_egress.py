"""The egress gateway (Phase A: the spine + the two raw actuators).

Layers proven here:
* Pure unit — classify() and guard() in isolation: regex-driven classification,
  the channel policy matrix, ALLOW / HOLD / REDACT, and fail-closed routing.
* Integration — _send_email / _call_webhook: distribution-safety (inert when
  off), and the re-guard loop (a HELD send approved via the actions spine sends
  EXACTLY once and is not re-held on the approved path — the subtlest bug).
* Recording + replay — an `egress` decision is written into the signed Trace
  with the right verdict/class/channel, and its core is replay-stable.
"""

import smtplib
import urllib.request

from olympus import (actions, builtin_actions, config, egress, memory,  # noqa: F401
                     security, tools, trace)

# A string that matches security._KEYISH → forces SENSITIVE classification.
KEY = "sk-ABCDEFGH12345678"


# ======================================================================
# Pure unit — classify (Part 7, 1)
# ======================================================================

def test_classify_clean_public_stays_public():
    assert egress.classify("just some marketing copy",
                           asserted=egress.DataClass.PUBLIC) is egress.DataClass.PUBLIC


def test_classify_api_key_overrides_assertion():
    assert egress.classify(f"here is a token {KEY}",
                           asserted=egress.DataClass.PUBLIC) is egress.DataClass.SENSITIVE
    # even asserted OPERATIONAL, a secret-shaped string is SENSITIVE (fail-closed)
    assert egress.classify(f"token {KEY}",
                           asserted=egress.DataClass.OPERATIONAL) is egress.DataClass.SENSITIVE


def test_classify_email_asserted_public_becomes_sensitive():
    assert egress.classify("reach me at alice@example.com",
                           asserted=egress.DataClass.PUBLIC) is egress.DataClass.SENSITIVE


def test_classify_operational_no_pii_stays_operational():
    assert egress.classify("let's meet on tuesday to plan the sprint",
                           asserted=egress.DataClass.OPERATIONAL) is egress.DataClass.OPERATIONAL


# ======================================================================
# Pure unit — guard ALLOW / HOLD / REDACT (Part 7, 2–6)
# ======================================================================

def test_guard_allow_public_to_broadcast():
    d = egress.guard("public announcement", egress.ChannelKind.BROADCAST, user="u")
    assert d.verdict is egress.Verdict.ALLOW
    assert d.data_class is egress.DataClass.PUBLIC


def test_guard_allow_operational_to_user_directed():
    d = egress.guard("draft reply to the client", egress.ChannelKind.USER_DIRECTED,
                     user="u", asserted=egress.DataClass.OPERATIONAL)
    assert d.verdict is egress.Verdict.ALLOW
    assert d.data_class is egress.DataClass.OPERATIONAL


def test_guard_hold_sensitive_to_user_directed_prepares_action():
    d = egress.guard(f"wire instructions {KEY}", egress.ChannelKind.USER_DIRECTED,
                     user="holder", asserted=egress.DataClass.OPERATIONAL,
                     action_type="email_egress_held",
                     payload={"to": "x@y.com", "subject": "s", "body": "b"})
    assert d.verdict is egress.Verdict.HOLD
    assert d.data_class is egress.DataClass.SENSITIVE
    pend = actions.pending("holder")
    assert len(pend) == 1 and pend[0].type == "email_egress_held"


def test_guard_hold_no_registered_action_type_fails_closed():
    d = egress.guard(f"secret {KEY}", egress.ChannelKind.USER_DIRECTED,
                     user="nobody", asserted=egress.DataClass.OPERATIONAL,
                     action_type="not_a_real_type",
                     payload={"to": "x@y.com", "subject": "s", "body": "b"})
    assert d.verdict is egress.Verdict.HOLD          # fail closed — NOT sent
    assert "approval routing failed" in d.reason
    assert actions.pending("nobody") == []           # nothing was prepared


def test_guard_redact_sensitive_to_pooled():
    text = f"distilled method mentioning {KEY} and bob@acme.com"
    d = egress.guard(text, egress.ChannelKind.POOLED, user="u")
    assert d.verdict is egress.Verdict.REDACT
    assert d.data_class is egress.DataClass.PUBLIC    # downgraded after redaction
    assert d.redacted_text == security.anonymize(text)
    assert "[redacted-key]" in d.redacted_text and "[email]" in d.redacted_text


def test_guard_latent_leak_to_broadcast_and_external_sink_is_held():
    # C1 (operational working content) must NOT broadcast.
    d1 = egress.guard("the user's private working notes",
                      egress.ChannelKind.BROADCAST, user="u",
                      asserted=egress.DataClass.OPERATIONAL)
    assert d1.verdict is egress.Verdict.HOLD
    # C2 must NOT hit an external sink.
    d2 = egress.guard(f"secret {KEY}", egress.ChannelKind.EXTERNAL_SINK, user="u")
    assert d2.verdict is egress.Verdict.HOLD


# ======================================================================
# Integration — distribution safety + the re-guard loop (Part 7, 7–8)
# ======================================================================

def _smtp_recorder(monkeypatch):
    sends = []

    class _FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): pass
        def login(self, *a): pass
        def send_message(self, msg): sends.append(msg)

    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setenv("SMTP_FROM", "me@test")
    monkeypatch.setenv("OLYMPUS_EMAIL_ALLOWLIST", "to@dest.com")
    return sends


def _webhook_recorder(monkeypatch):
    calls = []

    class _FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=None): return b"ok"

    class _FakeOpener:                       # _call_webhook uses a safe-redirect
        def open(self, req, timeout=30):     # opener, not urlopen
            calls.append(req)
            return _FakeResp()

    monkeypatch.setattr(tools._urlreq, "build_opener", lambda *a, **k: _FakeOpener())
    # Don't do a real DNS lookup for the SSRF gate in tests.
    monkeypatch.setattr(security, "url_block_reason", lambda url, **kw: None)
    monkeypatch.setenv("OLYMPUS_WEBHOOKS", "hook=https://example.com/h")
    return calls


def test_distribution_safety_guard_off_sends_even_sensitive(monkeypatch):
    """With OLYMPUS_EGRESS_GUARD unset, _send_email/_call_webhook behave exactly
    as today — even SENSITIVE content sends, nothing is held (zero change)."""
    monkeypatch.delenv("OLYMPUS_EGRESS_GUARD", raising=False)
    sends = _smtp_recorder(monkeypatch)
    out = tools._send_email("to@dest.com", "subj", f"body with {KEY}")
    assert out == "Email sent to to@dest.com."
    assert len(sends) == 1
    assert actions.pending(memory.current_user()) == []   # nothing held

    calls = _webhook_recorder(monkeypatch)
    out2 = tools._call_webhook("hook", {"secret": KEY})
    assert out2.startswith("Webhook 'hook' responded 200")
    assert len(calls) == 1


def test_webhook_url_ssrf_gated(monkeypatch):
    # A webhook pointed at an internal/metadata host is refused before the POST.
    calls = _webhook_recorder(monkeypatch)
    monkeypatch.setattr(security, "url_block_reason",
                        lambda url, **kw: "refusing to fetch an internal host")
    out = tools._call_webhook("hook", {"x": 1})
    assert "blocked" in out.lower() and calls == []       # never sent


def test_guard_on_holds_sensitive_email_then_approves_sends_once(monkeypatch):
    """TEST 8 (mandatory): the re-guard loop. Guard ON: a SENSITIVE email is
    HELD (0 sends); approving it via the actions spine sends EXACTLY once and is
    NOT re-held on the approved path."""
    monkeypatch.setenv("OLYMPUS_EGRESS_GUARD", "1")
    sends = _smtp_recorder(monkeypatch)
    user = memory.current_user()                          # "shared" in tests
    actions.grant_scope(user, "email")                    # let approval execute

    held = tools._send_email("to@dest.com", "subj", f"sensitive {KEY}")
    assert held.startswith("[Held for approval")
    assert len(sends) == 0                                # NOT sent yet
    pend = actions.pending(user)
    assert len(pend) == 1 and pend[0].type == "email_egress_held"

    result = actions.approve(user, pend[0].id)
    assert result.status == "executed"
    assert len(sends) == 1                                # sent EXACTLY once
    assert actions.pending(user) == []                    # NOT re-held


def test_guard_on_holds_sensitive_webhook_then_approves_sends_once(monkeypatch):
    """Same re-guard-loop guarantee for the webhook actuator."""
    monkeypatch.setenv("OLYMPUS_EGRESS_GUARD", "1")
    calls = _webhook_recorder(monkeypatch)
    user = memory.current_user()
    actions.grant_scope(user, "webhook")

    held = tools._call_webhook("hook", {"creds": KEY})
    assert held.startswith("[Held for approval")
    assert len(calls) == 0
    pend = actions.pending(user)
    assert len(pend) == 1 and pend[0].type == "webhook_egress_held"

    result = actions.approve(user, pend[0].id)
    assert result.status == "executed"
    assert len(calls) == 1
    assert actions.pending(user) == []


def test_guard_on_allows_clean_email_unchanged(monkeypatch):
    """Guard ON but content within policy (C1, no secrets) → ALLOW → sends as
    today. The common path changes nothing."""
    monkeypatch.setenv("OLYMPUS_EGRESS_GUARD", "1")
    sends = _smtp_recorder(monkeypatch)
    out = tools._send_email("to@dest.com", "lunch?", "are you free tuesday")
    assert out == "Email sent to to@dest.com."
    assert len(sends) == 1


# ======================================================================
# Recording into the signed Trace + replay (Part 7, 9–10)
# ======================================================================

def _egress_decs(tr):
    return [d for d in tr.decisions if d["decision_type"] == "egress"]


def test_egress_decision_recorded_on_allow_and_hold():
    tr = trace.Trace("t")
    tok = trace.set_current(tr)
    try:
        egress.guard("public note", egress.ChannelKind.BROADCAST, user="u")
        egress.guard(f"secret {KEY}", egress.ChannelKind.USER_DIRECTED, user="u",
                     asserted=egress.DataClass.OPERATIONAL,
                     action_type="email_egress_held",
                     payload={"to": "x@y.com", "subject": "s", "body": "b"})
    finally:
        trace.reset_current(tok)

    decs = _egress_decs(tr)
    assert len(decs) == 2
    allow, hold = decs[0], decs[1]
    assert allow["rationale"]["verdict"] == "allow"
    assert allow["rationale"]["data_class"] == "C0"
    assert allow["rationale"]["channel"] == "broadcast"
    assert allow["outcome"]["status"] == "ok"
    assert hold["rationale"]["verdict"] == "hold"
    assert hold["rationale"]["data_class"] == "C2"
    assert hold["rationale"]["channel"] == "user_directed"
    assert hold["outcome"]["status"] == "hold"


def test_egress_records_replay_byte_identical():
    """guard()/classify() are pure + deterministic, so re-running the same
    egress decisions produces byte-identical decision cores (diff empty). Proves
    the `egress` record is replay-stable (Part 6 / Part 8).

    NOTE: this is a determinism check, not a full OLYMPUS_REPLAY run — see the
    PR report's flag that tool handlers (where egress fires) are skipped in
    replay mode, so a real replay_run cannot reproduce a handler-recorded egress
    decision. Here the egress decisions are driven directly, deterministically.
    """
    def drive(tr):
        tok = trace.set_current(tr)
        try:
            egress.guard("public", egress.ChannelKind.BROADCAST, user="u")
            egress.guard(f"secret {KEY}", egress.ChannelKind.USER_DIRECTED,
                         user="u", asserted=egress.DataClass.OPERATIONAL,
                         action_type="email_egress_held",
                         payload={"to": "x@y.com", "subject": "s", "body": "b"})
        finally:
            trace.reset_current(tok)

    rec = trace.Trace("t")
    drive(rec)
    fresh = trace.Trace("t")
    drive(fresh)

    assert len(_egress_decs(rec)) == 2
    assert trace.diff_decisions(rec.decisions, fresh.decisions) == []
    core = trace.decision_core(_egress_decs(rec)[0])
    assert "record_id" not in core and "ts" not in core
    assert core["decision_type"] == "egress"


# ======================================================================
# Phase C — broadcast + external sinks (chat notify_all, agentbeat, GitHub)
# ======================================================================

def test_notify_all_holds_sensitive_broadcast(monkeypatch):
    from olympus import gateway, slack
    sent = []
    monkeypatch.setattr(slack, "notify", lambda t: (sent.append(t) or True))
    monkeypatch.setattr(gateway, "NOTIFY_CHANNELS", ("slack",))
    monkeypatch.setenv("OLYMPUS_EGRESS_GUARD", "1")
    # clean/public → broadcast proceeds
    assert gateway.notify_all("backup completed: 3 files") == ["slack"]
    assert sent == ["backup completed: 3 files"]
    sent.clear()
    # PII in a proactive push → HELD, reaches no channel
    assert gateway.notify_all("reminder: email john@corp.com re: the deal") == []
    assert sent == []


def test_notify_all_off_by_default_broadcasts_even_sensitive(monkeypatch):
    # Distribution safety: with the guard off (default), notify_all is inert —
    # it fans out exactly as before, so enabling the feature is the only change.
    from olympus import gateway, slack
    sent = []
    monkeypatch.setattr(slack, "notify", lambda t: (sent.append(t) or True))
    monkeypatch.setattr(gateway, "NOTIFY_CHANNELS", ("slack",))
    monkeypatch.delenv("OLYMPUS_EGRESS_GUARD", raising=False)
    assert gateway.notify_all("email john@corp.com") == ["slack"]
    assert sent == ["email john@corp.com"]


def test_agentbeat_deliver_holds_sensitive_to_direct_channel(monkeypatch):
    # The direct deliver_to path bypasses notify_all, so it must be guarded too.
    from olympus import agentbeat, slack
    sent = []
    monkeypatch.setattr(slack, "notify", lambda t: (sent.append(t) or True))
    monkeypatch.setenv("OLYMPUS_EGRESS_GUARD", "1")
    beat = agentbeat.Beat(id="1", user="shared", every=86400, prompt="p",
                          deliver_to="slack")
    where = agentbeat._deliver(beat, "your card 4111111111111111 was charged")
    assert "held" in where.lower() and sent == []


def test_notify_all_checks_the_authenticated_owner_secret_namespace(monkeypatch):
    from olympus import gateway, security, slack
    sent = []
    seen = []

    def held(user):
        seen.append(user)
        if user == "alice":
            return [("vault:synthetic", "synthetic-alice-secret-12345")]
        return []

    monkeypatch.setattr(security, "_held_secrets", held)
    monkeypatch.setattr(slack, "notify", lambda text: (sent.append(text) or True))
    monkeypatch.setattr(gateway, "NOTIFY_CHANNELS", ("slack",))
    monkeypatch.setenv("OLYMPUS_EGRESS_GUARD", "1")

    delivered = gateway.notify_all(
        "status: synthetic-alice-secret-12345", user="alice")

    assert delivered == []
    assert sent == []
    assert seen == ["alice"]


def test_agentbeat_direct_delivery_checks_the_beat_owner(monkeypatch):
    from olympus import agentbeat, security, slack
    sent = []
    seen = []

    def held(user):
        seen.append(user)
        if user == "alice":
            return [("vault:synthetic", "synthetic-alice-secret-12345")]
        return []

    monkeypatch.setattr(security, "_held_secrets", held)
    monkeypatch.setattr(slack, "notify", lambda text: (sent.append(text) or True))
    monkeypatch.setenv("OLYMPUS_EGRESS_GUARD", "1")
    beat = agentbeat.Beat(id="1", user="alice", every=86400, prompt="p",
                          deliver_to="slack")

    where = agentbeat._deliver(beat, "status: synthetic-alice-secret-12345")

    assert "held" in where.lower()
    assert sent == []
    assert seen == ["alice"]


def test_scheduler_delivery_checks_job_owner_before_channel(monkeypatch):
    from olympus import scheduler, security, slack
    sent = []
    seen = []

    def held(user):
        seen.append(user)
        if user == "alice":
            return [("vault:synthetic", "synthetic-alice-secret-12345")]
        return []

    monkeypatch.setattr(security, "_held_secrets", held)
    monkeypatch.setattr(slack, "notify", lambda text: (sent.append(text) or True))
    monkeypatch.setenv("OLYMPUS_EGRESS_GUARD", "1")
    job = scheduler.Job(name="daily", interval=3600, prompt="p",
                        deliver_to="slack", user="alice")

    scheduler._deliver(job, "status: synthetic-alice-secret-12345")

    assert sent == []
    assert seen == ["alice"]


def test_propose_upgrade_withholds_pii_from_github(monkeypatch):
    from olympus import tools, github
    filed = []
    monkeypatch.setattr(github, "create_issue",
                        lambda t, b: (filed.append((t, b)) or "http://issue/1"))
    monkeypatch.setenv("OLYMPUS_EGRESS_GUARD", "1")
    out = tools._propose_upgrade(
        "contact fix", "reach the maintainer at dev@corp.com about this")
    assert "withheld" in out.lower() and filed == []      # not filed publicly
    assert "saved" in out.lower()                          # still saved locally


def test_propose_upgrade_files_clean_proposal(monkeypatch):
    from olympus import tools, github
    filed = []
    monkeypatch.setattr(github, "create_issue",
                        lambda t, b: (filed.append((t, b)) or "http://issue/1"))
    monkeypatch.setenv("OLYMPUS_EGRESS_GUARD", "1")
    out = tools._propose_upgrade("add caching",
                                 "cache the model list for five minutes")
    assert "issue" in out.lower() and len(filed) == 1     # clean → filed

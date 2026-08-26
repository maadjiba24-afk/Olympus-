"""Adversarial contracts for transport-to-owner identity binding.

All senders, messages and credentials are synthetic.  Network delivery and
model execution are replaced with local test doubles.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from olympus import (agentbeat, config, email_gateway, gateway, gmail, goals,
                     memory, onboarding, operator, prefs, scheduler, steering,
                     vault, webctx, webmonitor)


PUNCT_A = "a.b@example.com"
PUNCT_B = "a-b@example.com"
LONG_A = "x" * 80 + ".alpha@example.com"
LONG_B = "x" * 80 + "-alpha@example.com"


@pytest.mark.parametrize("first,second", [
    (PUNCT_A, PUNCT_B),
    (LONG_A, LONG_B),
    ("a@b", "a b"),
])
def test_transport_principal_is_exact_collision_resistant(first, second):
    assert memory.safe_id(first) == memory.safe_id(second)
    a = gateway.principal_id(first, "email")
    b = gateway.principal_id(second, "email")
    assert a != b
    assert len(a) <= 64 and len(b) <= 64
    assert memory.safe_id(a) == a and memory.safe_id(b) == b


def test_transport_principal_is_deterministic_and_domain_separated():
    first = gateway.principal_id(PUNCT_A, "email")
    assert first == gateway.principal_id(PUNCT_A, "email")
    assert first != gateway.principal_id(PUNCT_A, "hook")
    assert PUNCT_A not in first
    assert first.startswith("email-v2-")


def test_only_lossy_pre_v2_gateway_principals_are_ambiguous():
    old = f"email-{memory.safe_id(PUNCT_A)}"
    assert memory.is_ambiguous_gateway_owner(old)
    assert not memory.is_ambiguous_gateway_owner(
        gateway.principal_id(PUNCT_A, "email"))
    for proven_platform_id in ("tg--100", "dc-42", "sl-U1", "wa-42"):
        assert not memory.is_ambiguous_gateway_owner(proven_platform_id)


def test_ambiguous_gateway_principal_has_fail_closed_preferences_and_vault():
    old = f"email-{memory.safe_id(PUNCT_A)}"
    prefs.set(old, "autonomy", 4)
    prefs.set(old, "scopes", ["mail.send"])
    assert prefs.is_quarantined(old)
    assert prefs.get(old, "autonomy") == 0
    assert prefs.get(old, "scopes") == []
    assert prefs.get(old, "capability_profile") == "guest"
    assert vault.get(old, "oauth") is None
    with pytest.raises(vault.VaultError, match="ambiguous pre-v2"):
        vault.put(old, "oauth", {"access_token": "mock-secret"})


def test_ambiguous_gateway_owner_standing_work_is_inert(monkeypatch):
    old = f"email-{memory.safe_id(PUNCT_A)}"
    calls = []

    job = scheduler.add("legacy", 60, "private report", user=old, now=0)
    assert scheduler.quarantined_job(job)
    assert not job.due(10_000)
    assert scheduler.jobs(old) == []

    agentbeat.add(old, 1, "private beat", now=0)
    assert agentbeat.run_due(
        now=2, runner=lambda beat: calls.append(("beat", beat.user)) or "alert"
    ) == []
    assert agentbeat.next_due_in(now=2) is None

    monkeypatch.setenv("OLYMPUS_GOALS_EVERY", "1")
    goals.add(old, "private goal", "complete")
    assert goals.run_due(
        now=time.time() + 2,
        runner=lambda *args: calls.append(("goal", old)) or "result",
    ) == []
    assert goals.next_due_in(now=time.time() + 2) is None

    monkeypatch.setenv("OLYMPUS_WEB_MONITOR", "1")
    webmonitor.add(old, "https://example.test/private", interval=900)
    monkeypatch.setattr(
        webctx, "diff",
        lambda *args, **kwargs: pytest.fail("legacy monitor performed I/O"),
    )
    assert webmonitor.run_due(now=time.time() + 2_000) == []

    operator.schedule(old, "legacy", "example.test", "fixture", 300)
    monkeypatch.setattr(
        operator, "run",
        lambda *args, **kwargs: pytest.fail("legacy operator job executed"),
    )
    assert operator.run_due(now=time.time() + 400) == []
    assert calls == []


def test_email_gateway_uses_allowlist_case_canonicalization(monkeypatch):
    monkeypatch.setenv("OLYMPUS_EMAIL_ALLOW", "owner@example.com")
    seen = []
    monkeypatch.setattr(gmail, "send", lambda *args, **kwargs: {"id": "mock"})
    monkeypatch.setattr(
        gateway, "reply_for",
        lambda bots, user, text, prefix="ol": seen.append((user, prefix)) or ["ok"],
    )
    email_gateway.handle_message(
        {}, {"sender": "Owner@Example.COM", "body": "fixture",
             "authenticated": True})
    assert seen == [("owner@example.com", "email")]


@pytest.mark.parametrize("prefix", ["", "email-unsafe", "x" * 17, "é"])
def test_transport_principal_rejects_unsafe_prefix(prefix):
    with pytest.raises(ValueError, match="transport prefix"):
        gateway.principal_id("owner", prefix)


@pytest.mark.parametrize("key", [None, "", "  ", "\t"])
def test_transport_principal_rejects_missing_key(key):
    with pytest.raises(ValueError, match="user key"):
        gateway.principal_id(key, "email")


def test_explicit_transport_principal_is_bounded_and_prefix_matched():
    assert gateway._resolved_principal("-100", "tg", "tg--100") == "tg--100"
    with pytest.raises(ValueError, match="wrong prefix"):
        gateway._resolved_principal("42", "tg", "sl-42")
    with pytest.raises(ValueError, match="path-safe"):
        gateway._resolved_principal("42", "tg", "tg-user@example.com")
    with pytest.raises(ValueError, match="path-safe"):
        gateway._resolved_principal("42", "tg", "tg-" + "x" * 70)


def test_reply_fallback_steers_the_explicit_historical_principal():
    uid = "tg--100123"
    out = gateway.reply_for({}, "-100123", "/steer inspect the boundary",
                            prefix="tg", uid=uid)
    assert out and "next tool call" in out[0]
    assert steering.drain(uid) == ["inspect the boundary"]
    assert steering.drain(gateway.principal_id("-100123", "tg")) == []


def test_authenticated_colliding_email_senders_get_distinct_olympus(monkeypatch):
    monkeypatch.setenv("OLYMPUS_EMAIL_ALLOW", f"{PUNCT_A},{PUNCT_B}")
    sent = []
    monkeypatch.setattr(gmail, "send",
                        lambda to, subject, body:
                        sent.append((to, subject, body)) or {"id": "mock"})

    created = []

    class DummyOlympus:
        def __init__(self, *, user, conversation_id):
            self.user = user
            self.conversation_id = conversation_id
            created.append((user, conversation_id))

        def ask(self, text):
            return f"owner={self.user}; text={text}"

    monkeypatch.setattr(gateway.orchestrator, "Olympus", DummyOlympus)
    bots = {}
    for sender in (PUNCT_A, PUNCT_B):
        email_gateway.handle_message(
            bots,
            {"sender": sender, "subject": "fixture", "body": "hello",
             "authenticated": True},
        )

    expected = {gateway.principal_id(PUNCT_A, "email"),
                gateway.principal_id(PUNCT_B, "email")}
    assert set(bots) == expected
    assert len(created) == 2
    assert {user for user, _conversation in created} == expected
    assert all(user == conversation for user, conversation in created)
    assert {to for to, _subject, _body in sent} == {PUNCT_A, PUNCT_B}


def test_email_commands_and_owner_stores_remain_isolated(monkeypatch):
    monkeypatch.setenv("OLYMPUS_EMAIL_ALLOW", f"{PUNCT_A},{PUNCT_B}")
    replies = {}
    monkeypatch.setattr(
        gmail, "send",
        lambda to, subject, body: replies.__setitem__(to, body) or {"id": "mock"},
    )
    bots = {}

    email_gateway.handle_message(
        bots, {"sender": PUNCT_A, "body": "/goal private alpha :: complete",
               "authenticated": True},
    )
    email_gateway.handle_message(
        bots, {"sender": PUNCT_B, "body": "/goal list",
               "authenticated": True},
    )

    owner_a = gateway.principal_id(PUNCT_A, "email")
    owner_b = gateway.principal_id(PUNCT_B, "email")
    assert [g.text for g in goals.active(owner_a)] == ["private alpha"]
    assert goals.active(owner_b) == []
    assert "private alpha" not in replies[PUNCT_B]

    prefs.set(owner_a, "language", "French")
    prefs.set(owner_b, "language", "Arabic")
    assert prefs.get(owner_a, "language") == "French"
    assert prefs.get(owner_b, "language") == "Arabic"

    memory.save_conversation(
        owner_a, [{"role": "user", "content": "alpha evidence"}], owner=owner_a)
    memory.save_conversation(
        owner_b, [{"role": "user", "content": "beta evidence"}], owner=owner_b)
    assert memory.load_conversation(owner_a)[0]["content"] == "alpha evidence"
    assert memory.load_conversation(owner_b)[0]["content"] == "beta evidence"


def test_colliding_senders_have_independent_onboarding_state():
    a = gateway.reply_for({}, PUNCT_A, "/start", prefix="email")
    b = gateway.reply_for({}, PUNCT_B, "/start", prefix="email")
    assert "Welcome" in " ".join(a)
    assert "Welcome" in " ".join(b)
    assert onboarding.is_new(gateway.principal_id(PUNCT_A, "email")) is False
    assert onboarding.is_new(gateway.principal_id(PUNCT_B, "email")) is False


def test_ambiguous_legacy_conversation_is_not_claimed(monkeypatch):
    legacy_uid = f"email-{memory.safe_id(PUNCT_A)}"
    legacy = config.MEMORY_DIR / "conversations" / f"{legacy_uid}.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps([
        {"role": "user", "content": "legacy collision secret"}
    ]), encoding="utf-8")

    seen = []

    def fake_ask(self, text):
        seen.extend(self.history)
        return "bounded reply"

    monkeypatch.setattr(gateway.orchestrator.Olympus, "ask", fake_ask)
    gateway.reply_for({}, PUNCT_A, "new request", prefix="email")
    assert all("legacy collision secret" not in str(turn) for turn in seen)
    assert legacy.exists(), "ambiguous legacy data is preserved for operator review"


def test_ambiguous_v1_inflight_record_is_dropped_not_replayed():
    old = config.MEMORY_DIR / "inflight" / "email-a-b-example-com.json"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text(json.dumps({
        "uid": "email-a-b-example-com",
        "key": PUNCT_A,
        "text": "legacy private request",
        "attempts": 1,
        "ts": 9999999999,
    }), encoding="utf-8")
    assert gateway.inflight_take("email-") == []
    assert not old.exists()


def test_v2_inflight_paths_do_not_collapse_exact_owners():
    a = gateway.principal_id(PUNCT_A, "email")
    b = gateway.principal_id(PUNCT_B, "email")
    gateway.inflight_mark(a, PUNCT_A, "alpha request")
    gateway.inflight_mark(b, PUNCT_B, "beta request")
    assert gateway._inflight_path(a) != gateway._inflight_path(b)
    taken = gateway.inflight_take("email-")
    assert {(entry["uid"], entry["text"]) for entry in taken} == {
        (a, "alpha request"), (b, "beta request")}


def test_no_gateway_principal_is_minted_with_safe_id_only():
    root = Path(__file__).resolve().parents[1] / "olympus"
    gateway_source = (root / "gateway.py").read_text(encoding="utf-8")
    chanbase_source = (root / "chanbase.py").read_text(encoding="utf-8")
    assert 'f"{prefix}-{memory.safe_id(user_key)}"' not in gateway_source
    assert 'f"{prefix}-{sender_id}"' not in chanbase_source

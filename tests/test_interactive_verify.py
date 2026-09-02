"""Interactive-path verification (M4 / closes DEFERRED #6, #7).

Before M4 a Zeus DIRECT reply skipped the whole council and shipped entirely
unverified (#6), and the router's `needs_verification=False` opt-out skipped the
verify chain SILENTLY (#7). M4 adds a bounded-latency screen on direct replies
(banner an unsupported factual claim, otherwise pass) and turns every
verification skip — direct no-claim, clarify, router opt-out, disabled — into
a ledgered `verify.exempt` decision that flushes durably to the trace. Verifier
outages and malformed verdicts are visibly bannered and never authorize an
unmarked reply.
"""
import json

import pytest

from olympus import config, orchestrator


def _bot(monkeypatch, route, verdict=None):
    """Wire Zeus's routing call and (optionally) the direct-verify call.

    `backend.complete_json` is used by BOTH `_route` and the direct verifier;
    the first call returns the route, later calls return `verdict`.
    """
    bot = orchestrator.Olympus(user="iv")
    calls = {"n": 0}

    def fake_complete_json(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return route
        return verdict if verdict is not None else route

    monkeypatch.setattr(orchestrator.backend, "complete_json", fake_complete_json)
    return bot, calls


def _exemptions(bot):
    tr = orchestrator.trace_mod.load_run(bot.last_run_id)
    return [d for d in (tr.get("decisions") or [])
            if d.get("decision_type") == "verify.exempt"]


def _direct_verify_decs(bot):
    tr = orchestrator.trace_mod.load_run(bot.last_run_id)
    return [d for d in (tr.get("decisions") or [])
            if d.get("decision_type") == "direct_verify"]


# --- (a) direct reply with an UNSUPPORTED claim → UNVERIFIED banner --------

def test_direct_unsupported_claim_is_bannered(monkeypatch):
    monkeypatch.setenv("OLYMPUS_INTERACTIVE_VERIFY", "on")
    bot, _ = _bot(
        monkeypatch,
        route={"mode": "direct",
               "direct_reply": "The Eiffel Tower was completed in 1725.",
               "specialists": [], "brief": None,
               "clarifying_questions": [], "needs_verification": False},
        verdict={"checkable": True, "supported": False,
                 "unsupported_claims": ["Eiffel Tower completed in 1725"]})
    reply = bot.ask("when was the eiffel tower built?")
    assert "⚠️ UNVERIFIED" in reply
    assert "1725" in reply                     # original reply preserved below
    decs = _direct_verify_decs(bot)
    assert decs and decs[0]["rationale"]["status"] == "unsupported"


# --- (b) direct reply with a SUPPORTED claim → passes untouched, ledgered --

def test_direct_supported_claim_passes_clean(monkeypatch):
    monkeypatch.setenv("OLYMPUS_INTERACTIVE_VERIFY", "on")
    bot, _ = _bot(
        monkeypatch,
        route={"mode": "direct", "direct_reply": "Paris is the capital of France.",
               "specialists": [], "brief": None,
               "clarifying_questions": [], "needs_verification": False},
        verdict={"checkable": True, "supported": True, "unsupported_claims": []})
    reply = bot.ask("capital of france?")
    assert reply == "Paris is the capital of France."   # no banner
    decs = _direct_verify_decs(bot)
    assert decs and decs[0]["rationale"]["status"] == "pass"


# --- (c) casual chat with NO checkable claim → ledgered exemption ----------

def test_direct_no_claim_is_ledgered_exemption(monkeypatch):
    monkeypatch.setenv("OLYMPUS_INTERACTIVE_VERIFY", "on")
    bot, _ = _bot(
        monkeypatch,
        route={"mode": "direct", "direct_reply": "Hey! How can I help today?",
               "specialists": [], "brief": None,
               "clarifying_questions": [], "needs_verification": False},
        verdict={"checkable": False, "supported": True, "unsupported_claims": []})
    reply = bot.ask("hi")
    assert reply == "Hey! How can I help today?"
    ex = _exemptions(bot)
    assert ex and ex[0]["rationale"] == {"mode": "direct",
                                         "reason": "no_checkable_claim"}


# --- (d) disabling the tier still LEDGERS the skip (never silent) ----------

def test_disabled_tier_ledgers_exemption(monkeypatch):
    monkeypatch.setenv("OLYMPUS_INTERACTIVE_VERIFY", "off")
    bot, calls = _bot(
        monkeypatch,
        route={"mode": "direct", "direct_reply": "The sky is green.",
               "specialists": [], "brief": None,
               "clarifying_questions": [], "needs_verification": False})
    reply = bot.ask("hi")
    assert reply == "The sky is green."
    assert calls["n"] == 1                      # no verifier call when disabled
    ex = _exemptions(bot)
    assert ex and ex[0]["rationale"]["reason"] == "disabled"


# --- (e) unavailable verifier evidence is visible and cannot authorize -----

def test_infra_error_is_bannered_and_recorded_as_unavailable(monkeypatch):
    monkeypatch.setenv("OLYMPUS_INTERACTIVE_VERIFY", "on")
    monkeypatch.setenv("OLYMPUS_INTERACTIVE_VERIFY_TIMEOUT", "0")  # no thread cap
    bot = orchestrator.Olympus(user="iv")
    n = {"i": 0}

    def flaky(*a, **k):
        n["i"] += 1
        if n["i"] == 1:
            return {"mode": "direct", "direct_reply": "Answer 42.",
                    "specialists": [], "brief": None,
                    "clarifying_questions": [], "needs_verification": False}
        raise RuntimeError("verifier boom")

    monkeypatch.setattr(orchestrator.backend, "complete_json", flaky)
    reply = bot.ask("meaning of life?")
    assert reply.startswith("⚠️ UNVERIFIED")
    assert reply.endswith("Answer 42.")
    assert not _exemptions(bot)
    decs = _direct_verify_decs(bot)
    assert decs and decs[0]["rationale"]["status"] == "unavailable"


@pytest.mark.parametrize("verdict", [
    [],
    {},
    {"checkable": "false", "supported": True, "unsupported_claims": []},
    {"checkable": True, "supported": 1, "unsupported_claims": []},
    {"checkable": True, "supported": False, "unsupported_claims": "claim"},
    {"checkable": True, "supported": False, "unsupported_claims": [""]},
    {"checkable": True, "supported": False,
     "unsupported_claims": (["claim"] * 10) + [42]},
    {"checkable": False, "supported": True,
     "unsupported_claims": ["contradiction"]},
    {"checkable": True, "supported": True,
     "unsupported_claims": ["contradiction"]},
])
def test_malformed_direct_verdict_is_bannered(monkeypatch, verdict):
    monkeypatch.setenv("OLYMPUS_INTERACTIVE_VERIFY", "on")
    bot, _ = _bot(
        monkeypatch,
        route={"mode": "direct", "direct_reply": "Potential factual claim.",
               "specialists": [], "brief": None,
               "clarifying_questions": [], "needs_verification": False},
        verdict=verdict)
    reply = bot.ask("is this true?")
    assert reply.startswith("⚠️ UNVERIFIED")
    assert reply.endswith("Potential factual claim.")
    assert not _exemptions(bot)
    decs = _direct_verify_decs(bot)
    assert decs and decs[0]["rationale"]["status"] == "invalid"


# --- (f) clarify turns carry no claim → ledgered exemption -----------------

def test_clarify_records_exemption(monkeypatch):
    bot, _ = _bot(
        monkeypatch,
        route={"mode": "clarify", "direct_reply": None, "specialists": [],
               "brief": None,
               "clarifying_questions": ["Which platform?", "What budget?"],
               "needs_verification": False})
    reply = bot.ask("optimize it")
    assert "Which platform?" in reply
    ex = _exemptions(bot)
    assert ex and ex[0]["rationale"] == {"mode": "clarify", "reason": "no_claim"}


# --- (g) DEFERRED #7: needs_verification=False on DELEGATE → ledgered -------

def test_router_opt_out_on_delegate_is_ledgered(monkeypatch):
    bot = orchestrator.Olympus(user="iv")
    monkeypatch.setattr(orchestrator.backend, "complete_json",
                        lambda *a, **k: {
                            "mode": "delegate", "direct_reply": None,
                            "specialists": ["plutus"], "brief": "b",
                            "clarifying_questions": [], "needs_verification": False})
    monkeypatch.setattr(bot, "_plan",
                        lambda *a, **k: [{"id": "s0", "specialist": "plutus",
                                          "task": "t", "depends_on": []}])
    monkeypatch.setattr(bot, "_dispatch_dag", lambda *a, **k: [("plutus", "ok")])
    monkeypatch.setattr(bot, "_review",
                        lambda *a, **k: {"verdict": "approve", "feedback": "",
                                         "retry_specialists": []})
    monkeypatch.setattr(bot, "_synthesize", lambda *a, **k: "final")
    reply = bot.ask("do it")
    assert reply == "final"
    ex = _exemptions(bot)
    assert ex and ex[0]["rationale"] == {"mode": "delegate",
                                         "reason": "router_opt_out"}

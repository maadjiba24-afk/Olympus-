"""ADR 0005 amendment 4: the synthesis stage is verified too.

The specialists' findings are fact-checked upstream; the residual risk on
the interactive path was Zeus ADDING claims at compose time. Covers: the
faithful pass-through, unfaithful → exactly one recompose, a second reject →
UNVERIFIED ADDITIONS banner, checker infrastructure failure (traced, never
bannered — the findings themselves were verified), the router/env/fast-mode
skips, the contract wiring, and the streaming trailing-correction note.
"""

from olympus import backend
from olympus import behavioral_contracts as abc_mod
from olympus import config, memory, orchestrator

FAITHFUL = {"faithful": True, "unsupported_additions": [], "confidence": 0.9}
UNFAITHFUL = {"faithful": False,
              "unsupported_additions": ["X raised $50M in 2025"],
              "confidence": 0.9}


def _mk_bot(monkeypatch, check_results):
    """An Olympus whose pipeline stages are canned and whose synthesis
    checker consumes `check_results` per call (a callable entry raises)."""
    memory.set_user("synthcheck")
    bot = orchestrator.Olympus(user="synthcheck")
    calls = {"check": 0, "synth": 0}
    monkeypatch.setattr(bot, "_route", lambda *a, **k: {
        "mode": "delegate", "direct_reply": None, "brief": "the brief",
        "specialists": ["hermes"], "clarifying_questions": [],
        "needs_verification": True})
    monkeypatch.setattr(bot, "_plan", lambda brief, keys, **kw: [
        {"id": "s0", "specialist": "hermes", "task": "t", "depends_on": []}])
    monkeypatch.setattr(bot, "_dispatch_dag",
                        lambda steps, tr, **kw: [("hermes", "findings")])
    monkeypatch.setattr(bot, "_verify", lambda brief, outputs: (
        "verified findings",
        {"status": "pass", "unsupported_claims": [], "confidence": 1.0}))
    monkeypatch.setattr(bot, "_review", lambda *a, **k: {
        "verdict": "approve", "feedback": "", "retry_specialists": []})

    def fake_synthesize(user_message, brief, verified):
        calls["synth"] += 1
        return f"composed reply v{calls['synth']}"

    monkeypatch.setattr(bot, "_synthesize", fake_synthesize)

    def fake_complete_json(settings, system, messages, schema, effort="high"):
        i = min(calls["check"], len(check_results) - 1)
        calls["check"] += 1
        r = check_results[i]
        if callable(r):
            return r()
        return dict(r)

    monkeypatch.setattr(backend, "complete_json", fake_complete_json)
    return bot, calls


def test_faithful_reply_passes_through(monkeypatch):
    bot, calls = _mk_bot(monkeypatch, [FAITHFUL])
    reply = bot.ask("question")
    assert reply == "composed reply v1"
    assert calls["check"] == 1 and calls["synth"] == 1
    assert bot._unverified_banner is None


def test_unfaithful_recomposes_exactly_once_then_passes(monkeypatch):
    bot, calls = _mk_bot(monkeypatch, [UNFAITHFUL, FAITHFUL])
    reply = bot.ask("question")
    assert reply == "composed reply v2"        # the recompose shipped
    assert calls["synth"] == 2                 # exactly one recompose
    assert calls["check"] == 2
    assert bot._unverified_banner is None


def test_unfaithful_twice_ships_with_additions_banner(monkeypatch):
    bot, calls = _mk_bot(monkeypatch, [UNFAITHFUL, UNFAITHFUL])
    reply = bot.ask("question")
    assert calls["synth"] == 2                 # never a third compose
    assert reply.startswith("⚠️ UNVERIFIED ADDITIONS")
    assert "X raised $50M in 2025" in reply
    assert "composed reply v2" in reply        # content still delivered


def test_checker_failure_ships_untouched_and_unbannered(monkeypatch):
    def boom():
        raise RuntimeError("checker died")
    bot, calls = _mk_bot(monkeypatch, [boom])
    reply = bot.ask("question")
    assert reply == "composed reply v1"
    assert bot._unverified_banner is None      # findings WERE verified


def test_env_kill_switch_skips_check(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SYNTH_CHECK", "off")
    bot, calls = _mk_bot(monkeypatch, [UNFAITHFUL])
    reply = bot.ask("question")
    assert reply == "composed reply v1"
    assert calls["check"] == 0


def test_fast_mode_skips_check(monkeypatch):
    monkeypatch.setenv("OLYMPUS_FAST", "1")
    bot, calls = _mk_bot(monkeypatch, [UNFAITHFUL])
    bot.ask("question")
    assert calls["check"] == 0


def test_router_opt_out_skips_check(monkeypatch):
    bot, calls = _mk_bot(monkeypatch, [UNFAITHFUL])
    monkeypatch.setattr(bot, "_route", lambda *a, **k: {
        "mode": "delegate", "direct_reply": None, "brief": "b",
        "specialists": ["hermes"], "clarifying_questions": [],
        "needs_verification": False})
    bot.ask("question")
    assert calls["check"] == 0                 # no verified source to compare


def test_answer_synthesis_contract_wired():
    v = abc_mod.check("answer.synthesis", {"verify_verdict": "reject"})
    assert not v.ok
    assert abc_mod.check("answer.synthesis", {"verify_verdict": "pass"}).ok


def test_streaming_gets_trailing_correction_note(monkeypatch):
    bot, calls = _mk_bot(monkeypatch, [UNFAITHFUL])

    class _FakePool:
        def for_role(self, role):
            return config.Settings(provider="openai", model="m", api_key="k")

    monkeypatch.setattr(bot, "pool", _FakePool())
    monkeypatch.setattr(backend, "complete_text",
                        lambda *a, **k: "streamed reply")
    pieces = list(bot.ask_stream("question"))
    joined = "".join(pieces)
    assert joined.startswith("streamed reply")     # tokens not retracted
    assert "⚠️ Correction" in joined               # trailing note appended
    assert "X raised $50M in 2025" in joined
    assert pieces[-1].lstrip().startswith("---")   # note is the LAST chunk

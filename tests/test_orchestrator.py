import threading
import time

import pytest

from olympus import config, memory, orchestrator, replaystore, trace
from olympus.evals import load_benchmarks
from olympus.specialists import SPECIALISTS


def test_feedback_records_last_exchange():
    bot = orchestrator.Olympus(user="tester")
    assert "Nothing to rate" in bot.feedback("up")
    bot.history = [{"role": "user", "content": "How do I budget?"},
                   {"role": "assistant", "content": "Like this..."}]
    assert "Thanks" in bot.feedback("down", "too vague")
    memory.set_user("tester")
    recorded = memory.recent("feedback")
    assert "negative" in recorded and "too vague" in recorded


def test_conversation_persists_across_instances():
    bot = orchestrator.Olympus(user="u1", conversation_id="conv-1")
    bot.history = [{"role": "user", "content": "remember me"},
                   {"role": "assistant", "content": "ok"}]
    memory.save_conversation("conv-1", bot.history)
    bot2 = orchestrator.Olympus(user="u1", conversation_id="conv-1")
    assert bot2.history == bot.history


def test_invalid_settings_surface_cleanly():
    bot = orchestrator.Olympus(
        settings=config.Settings(provider="openai", model=""))
    assert "Configuration problem" in bot.ask("hello")


def test_conversation_trigger(monkeypatch):
    ran = threading.Event()
    monkeypatch.setattr(orchestrator, "evolution_audit",
                        lambda settings=None: (ran.set() or "done"))
    monkeypatch.setattr(config, "AUDIT_EVERY_CHATS", 2)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-dummy")
    orchestrator.note_conversation()
    assert not ran.is_set()
    orchestrator.note_conversation()
    assert ran.wait(5)
    # counter reset after triggering (separate file from heartbeat state)
    assert memory.bump_conversation_count() == 1


def test_conversation_trigger_guards(monkeypatch):
    ran = threading.Event()
    monkeypatch.setattr(orchestrator, "evolution_audit",
                        lambda settings=None: (ran.set() or "done"))
    monkeypatch.setattr(config, "AUDIT_EVERY_CHATS", 0)
    orchestrator.note_conversation()
    assert not ran.is_set()
    monkeypatch.setattr(config, "AUDIT_EVERY_CHATS", 1)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OLYMPUS_BASE_URL", raising=False)
    orchestrator.note_conversation()
    time.sleep(0.2)
    assert not ran.is_set()  # no server credentials -> skip


def test_benchmarks_reference_real_specialists():
    for bench in load_benchmarks():
        assert bench["specialist"] in SPECIALISTS
        assert bench["task"] and bench["criteria"]


def test_coding_benchmark_is_polyglot():
    """Hephaestus must be benchmarked across many languages, not just Python,
    so the self-improvement loop strengthens all of them equally."""
    from olympus import evals
    coding = [b for b in load_benchmarks() if b["specialist"] == "hephaestus"]
    blob = " ".join((b["task"] + b["criteria"]).lower() for b in coding)
    for lang in ("python", "go", "rust", "typescript", "sql"):
        assert lang in blob, f"no coding benchmark covers {lang}"
    assert len(evals.ids_for(["hephaestus"])) >= 5


def test_reverify_divergence_is_not_masked(monkeypatch):
    """A ReplayDivergence on the rework *reverify* path must propagate, exactly
    like the first verify/route/review paths — never be swallowed as a benign
    verify failure. Otherwise a code/prompt change on the rework branch would
    replay to a false green instead of being caught as a divergence."""
    bot = orchestrator.Olympus(user="tester")
    monkeypatch.setattr(bot, "_route", lambda msg: {
        "mode": "delegate", "direct_reply": None, "specialists": ["argus"],
        "brief": "do it", "needs_verification": True})
    monkeypatch.setattr(bot, "_plan", lambda brief, keys: [
        {"id": "s1", "specialist": "argus", "task": "t", "depends_on": []}])
    monkeypatch.setattr(bot, "_dispatch_dag", lambda steps, tr: [("argus", "out")])
    monkeypatch.setattr(bot, "_dispatch", lambda redo: [("argus", "redone")])
    # First verify succeeds; Athena orders a retry; the reverify call diverges.
    calls = {"verify": 0}

    def verify(brief, outputs):
        calls["verify"] += 1
        if calls["verify"] == 1:
            return "verified-once"
        raise replaystore.ReplayDivergence("h" * 64, {"model": "m"})
    monkeypatch.setattr(bot, "_verify", verify)
    monkeypatch.setattr(bot, "_review", lambda brief, verified: {
        "verdict": "retry", "feedback": "fix it", "retry_specialists": ["argus"]})

    tr = trace.Trace("chat", "tester")
    with pytest.raises(replaystore.ReplayDivergence):
        bot._pipeline("a real user message", tr)
    assert calls["verify"] == 2          # we actually reached the reverify path


def test_heartbeat_nothing_due():
    from olympus import heartbeat
    state = {k: 1e18 for k in ("opportunity_scan", "watchlist", "maintenance",
                               "daily_learning", "train", "evolution_audit",
                               "replay_gate")}
    assert heartbeat.tick(state) == []

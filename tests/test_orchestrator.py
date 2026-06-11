import threading
import time

from olympus import config, memory, orchestrator
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
    assert memory.load_state()["conversation_count"] == 0


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


def test_heartbeat_nothing_due():
    from olympus import heartbeat
    state = {k: 1e18 for k in ("opportunity_scan", "watchlist",
                               "daily_learning", "evolution_audit")}
    assert heartbeat.tick(state) == []

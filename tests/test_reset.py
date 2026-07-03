"""Tier B: distill-then-clear /reset and idle gateway-session sweeps."""

from olympus import backend, gateway, memory, orchestrator


def test_reset_distills_then_clears(monkeypatch):
    monkeypatch.setattr(backend, "complete_text",
                        lambda *a, **k: "STATE: user builds an AI agent")
    bot = orchestrator.Olympus(user="r1")
    bot.history = [{"role": "user", "content": "help me build X"},
                   {"role": "assistant", "content": "sure, here's how"},
                   {"role": "user", "content": "and Y?"},
                   {"role": "assistant", "content": "like so"}]
    msg = bot.reset()
    assert "Fresh start" in msg
    # History collapsed to a state block + ack, carrying the distilled summary.
    assert len(bot.history) == 2
    assert "STATE: user builds an AI agent" in bot.history[0]["content"]
    assert "Conversation state" in bot.history[0]["content"]


def test_reset_on_empty_is_noop():
    bot = orchestrator.Olympus(user="r2")
    assert "already empty" in bot.reset()


def test_reset_falls_back_to_clear_on_distill_failure(monkeypatch):
    monkeypatch.setattr(backend, "complete_text",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    bot = orchestrator.Olympus(user="r3")
    bot.history = [{"role": "user", "content": "x"},
                   {"role": "assistant", "content": "y"}]
    msg = bot.reset()
    assert bot.history == []            # cleared, not crashed
    assert "Fresh start" in msg


def test_gateway_reset_command(monkeypatch):
    monkeypatch.setattr(backend, "complete_text", lambda *a, **k: "state")
    bots: dict = {}
    # seed a session with history
    gateway.reply_for(bots, "u", "hello")   # creates the bot
    uid = next(iter(bots))
    bots[uid].history = [{"role": "user", "content": "hi"},
                         {"role": "assistant", "content": "yo"}]
    out = gateway.reply_for(bots, "u", "/reset")
    assert any("Fresh start" in c for c in out)


def test_idle_sessions_are_swept(monkeypatch):
    import time
    monkeypatch.setattr(backend, "complete_text", lambda *a, **k: "state")
    bots: dict = {}
    bot = bots["ol-u"] = orchestrator.Olympus(user="ol-u")
    bot.history = [{"role": "user", "content": "old"},
                   {"role": "assistant", "content": "reply"}]
    bot._last_active = time.time() - 10_000     # stale
    n = gateway.reset_idle_sessions(bots, max_age_secs=3600)
    assert n == 1
    # a fresh session is left alone
    bot2 = bots["ol-v"] = orchestrator.Olympus(user="ol-v")
    bot2.history = [{"role": "user", "content": "recent"}]
    bot2._last_active = time.time()
    assert gateway.reset_idle_sessions(bots, max_age_secs=3600) == 0


def test_sweep_disabled_when_max_age_zero():
    bots = {"ol-u": orchestrator.Olympus(user="ol-u")}
    assert gateway.reset_idle_sessions(bots, max_age_secs=0) == 0

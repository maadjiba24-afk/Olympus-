"""The clarify route — Zeus asks 1-2 questions instead of guessing."""

from olympus import orchestrator


def _route(monkeypatch, route):
    bot = orchestrator.Olympus(user="clar")
    monkeypatch.setattr(orchestrator.backend, "complete_json",
                        lambda *a, **k: route)
    return bot


def test_clarify_returns_questions_without_delegating(monkeypatch):
    bot = _route(monkeypatch, {
        "mode": "clarify", "direct_reply": None, "specialists": [],
        "brief": None,
        "clarifying_questions": ["Which platform?", "What's the budget?"],
        "needs_verification": False,
    })
    reply = bot.ask("optimize it")
    assert "Which platform?" in reply and "What's the budget?" in reply
    # It short-circuited: the questions ARE the reply, saved to history.
    assert bot.history[-1]["content"] == reply


def test_clarify_caps_at_two_questions(monkeypatch):
    bot = _route(monkeypatch, {
        "mode": "clarify", "direct_reply": None, "specialists": [],
        "brief": None,
        "clarifying_questions": ["q1", "q2", "q3", "q4"],
        "needs_verification": False,
    })
    reply = bot.ask("do the thing")
    assert "q1" in reply and "q2" in reply
    assert "q3" not in reply and "q4" not in reply


def test_clarify_with_no_questions_falls_through(monkeypatch):
    # Model asked to clarify but gave nothing — must not reply with emptiness.
    bot = _route(monkeypatch, {
        "mode": "clarify", "direct_reply": None, "specialists": ["plutus"],
        "brief": "help with money", "clarifying_questions": [],
        "needs_verification": False,
    })
    # Stub the rest of the pipeline so this doesn't make real calls.
    monkeypatch.setattr(bot, "_plan",
                        lambda *a, **k: [{"id": "s0", "specialist": "plutus",
                                          "task": "t", "depends_on": []}])
    monkeypatch.setattr(bot, "_dispatch_dag", lambda *a, **k: [("plutus", "ok")])
    monkeypatch.setattr(bot, "_verify", lambda *a, **k: "verified")
    monkeypatch.setattr(bot, "_review",
                        lambda *a, **k: {"verdict": "approve", "feedback": "",
                                         "retry_specialists": []})
    monkeypatch.setattr(bot, "_synthesize", lambda *a, **k: "final answer")
    reply = bot.ask("help with money")
    assert reply == "final answer"          # delegated, not an empty clarify


def test_format_clarify_shape():
    out = orchestrator._format_clarify(["A?", "B?"])
    assert "1. A?" in out and "2. B?" in out

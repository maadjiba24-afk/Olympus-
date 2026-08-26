"""Per-reply and per-session usage/cost accounting surfaced in chat."""

from olympus import gateway, memory, usage


def _record_as(user, model="claude-x", in_tokens=1000, out_tokens=200):
    memory.set_user(user)
    try:
        usage.record(model, in_tokens, out_tokens)
    finally:
        memory.set_user("shared")


def test_session_totals_accumulate_per_user():
    _record_as("ua", in_tokens=1000, out_tokens=100)
    _record_as("ua", in_tokens=500, out_tokens=50)
    _record_as("ub", in_tokens=9999, out_tokens=1)
    totals = usage.session_totals("ua")
    assert totals["calls"] == 2
    assert totals["in"] == 1500 and totals["out"] == 150
    assert totals["cost"] > 0
    assert usage.session_totals("ub")["in"] == 9999


def test_delta_isolates_one_reply():
    before = usage.session_totals("uc")
    _record_as("uc", in_tokens=800, out_tokens=80)
    spent = usage.delta(before, usage.session_totals("uc"))
    assert spent["calls"] == 1 and spent["in"] == 800


def test_footer_renders_reply_session_today():
    _record_as("ud", in_tokens=12345, out_tokens=678)
    line = usage.footer({"calls": 1, "in": 12345, "out": 678,
                         "cost": 0.0421}, "ud")
    assert "12.3k in" in line and "678 out" in line
    assert "$0.0421 this reply" in line
    assert "session" in line and "today" in line


def test_chat_usage_command_reports_session():
    _record_as("ol-u1", in_tokens=2000, out_tokens=100)
    out = gateway.reply_for({}, "u1", "/usage")
    joined = "\n".join(out)
    assert "This session" in joined and "Today" in joined


def test_footer_is_opt_in(monkeypatch):
    monkeypatch.setattr("olympus.orchestrator.Olympus.ask",
                        lambda self, msg: "answer")
    out = gateway.reply_for({}, "u2", "hello")
    assert "⏱" not in "\n".join(out)

    gateway.reply_for({}, "u2", "/usage on")
    out = gateway.reply_for({}, "u2", "hello again")
    assert "⏱" in "\n".join(out)

    gateway.reply_for({}, "u2", "/usage off")
    out = gateway.reply_for({}, "u2", "third")
    assert "⏱" not in "\n".join(out)


def test_footer_reflects_the_turns_own_spend(monkeypatch):
    def fake_ask(self, msg):
        _record_as(gateway.principal_id("u3"), in_tokens=4000,
                   out_tokens=400)
        return "did work"
    monkeypatch.setattr("olympus.orchestrator.Olympus.ask", fake_ask)
    gateway.reply_for({}, "u3", "/usage on")
    out = gateway.reply_for({}, "u3", "do work")
    joined = "\n".join(out)
    assert "4.0k in" in joined and "400 out" in joined

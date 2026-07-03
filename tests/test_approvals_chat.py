"""Exec approvals from chat: the approval spine surfaced through the shared
gateway so /approve and /deny work from any channel, and a turn that held an
action tells the user in-channel."""

import pytest

from olympus import actions, approvals, gateway


@pytest.fixture
def action_type():
    executed = []
    actions.register(actions.ActionType(
        name="test_op", risk_class="irreversible", scope="",
        preview=lambda p: f"would run: {p.get('cmd', '?')}",
        execute=lambda p: executed.append(p) or {"ran": p.get("cmd")},
        description="test action"))
    return executed


def _prepare(user, cmd="rm -rf ./build"):
    return actions.prepare(user, "test_op", {"cmd": cmd})


def test_summary_lists_pending(action_type):
    a = _prepare("u1")
    text = approvals.summary("u1")
    assert a.id in text and "would run" in text
    assert "/approve" in text


def test_summary_empty_queue():
    assert "Nothing" in approvals.summary("u-empty")


def test_footer_only_when_pending(action_type):
    assert approvals.footer("u2") == ""
    a = _prepare("u2")
    footer = approvals.footer("u2")
    assert a.id in footer and "Held for approval" in footer


def test_approve_command_executes(action_type):
    a = _prepare("u3")
    reply = approvals.handle_command("u3", "/approve", a.id)
    assert "approved and executed" in reply
    assert action_type == [{"cmd": "rm -rf ./build"}]


def test_approve_accepts_hash_prefix(action_type):
    a = _prepare("u3b")
    reply = approvals.handle_command("u3b", "/approve", f"#{a.id}")
    assert "approved and executed" in reply


def test_deny_command_rejects(action_type):
    a = _prepare("u4")
    reply = approvals.handle_command("u4", "/deny", f"{a.id} too risky")
    assert "denied" in reply
    assert action_type == []
    assert actions.get("u4", a.id).status == actions.REJECTED


def test_unknown_id_reports_error(action_type):
    assert "Could not approve" in approvals.handle_command(
        "u5", "/approve", "nope")


def test_non_approval_command_returns_none():
    assert approvals.handle_command("u6", "/scan", "") is None


# --- through the shared gateway ---------------------------------------------

def test_gateway_routes_approve(action_type, monkeypatch):
    a = _prepare("ol-u7")
    out = gateway.reply_for({}, "u7", f"/approve {a.id}")
    assert any("approved and executed" in c for c in out)


def test_gateway_lists_approvals(action_type):
    a = _prepare("ol-u8")
    out = gateway.reply_for({}, "u8", "/approvals")
    assert any(a.id in c for c in out)


def test_reply_gains_footer_when_turn_holds_action(action_type, monkeypatch):
    def fake_ask(self, msg):
        _prepare("ol-u9", cmd="deploy prod")
        return "I prepared the deploy."
    monkeypatch.setattr("olympus.orchestrator.Olympus.ask", fake_ask)
    out = gateway.reply_for({}, "u9", "deploy please")
    joined = "\n".join(out)
    assert "I prepared the deploy." in joined
    assert "Held for approval" in joined and "/approve" in joined


def test_reply_has_no_footer_when_nothing_pending(monkeypatch):
    monkeypatch.setattr("olympus.orchestrator.Olympus.ask",
                        lambda self, msg: "plain answer")
    out = gateway.reply_for({}, "u10", "hi")
    assert "\n".join(out).strip() == "plain answer"

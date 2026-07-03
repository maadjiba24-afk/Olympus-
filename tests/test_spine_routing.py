"""send_email / call_webhook tool calls must route through the approval spine
(prepare → hold for approval), never execute directly. Regression test for the
audit finding that the raw senders were the registered tool handlers.
"""

from olympus import actions, builtin_actions, memory, tools

builtin_actions.register_builtins()


def test_registered_handlers_are_the_spine_routers():
    assert tools.HANDLERS["send_email"] is tools._send_email_tool
    assert tools.HANDLERS["call_webhook"] is tools._call_webhook_tool


def test_send_email_tool_holds_for_approval():
    memory.set_user("shared")
    out = tools._send_email_tool("x@y.z", "subj", "body")
    assert "approval" in out.lower() and "approve" in out.lower()
    # a PREPARED (not executed) action now exists — nothing auto-sent
    pending = [a for a in actions.pending("shared") if a.type == "send_email"]
    assert pending and pending[0].status == actions.PREPARED


def test_call_webhook_tool_holds_for_approval():
    memory.set_user("shared")
    out = tools._call_webhook_tool("hook", {"x": 1})
    assert "approval" in out.lower()
    assert any(a.type == "call_webhook" for a in actions.pending("shared"))


def test_raw_senders_still_exist_for_the_execute_path():
    # The ActionType execute callbacks and existing tests use the raw senders;
    # those remain, just no longer registered as the agent-facing tool.
    assert callable(tools._send_email) and callable(tools._call_webhook)

"""Mid-run steering (/steer) and conversation undo (/undo)."""

import types

import anthropic

from olympus import agent, config, gateway, llm, memory, orchestrator, steering, tui


# --- steering store -------------------------------------------------------

def test_put_drain_roundtrip():
    assert steering.put("k1", "check the euro price too")
    assert steering.pending("k1") == 1
    assert steering.drain("k1") == ["check the euro price too"]
    assert steering.drain("k1") == []          # drained


def test_queue_is_bounded():
    for i in range(steering.MAX_PENDING):
        assert steering.put("k2", f"n{i}")
    assert not steering.put("k2", "overflow")
    steering.drain("k2")


def test_empty_or_unkeyed_notes_rejected():
    assert not steering.put("", "note")
    assert not steering.put("k3", "   ")


def test_drain_current_uses_contextvar():
    token = steering.set_current("ctx-key")
    try:
        steering.put("ctx-key", "note")
        assert steering.drain_current() == ["note"]
    finally:
        steering.reset(token)
    assert steering.drain_current() == []      # no current key -> nothing


# --- agent-loop injection -------------------------------------------------

def _msg(payload):
    return anthropic.types.Message.model_validate(payload)


def _tooluse(tid="toolu_steer"):
    return _msg({
        "id": "m1", "type": "message", "role": "assistant", "model": "claude-test",
        "content": [{"type": "tool_use", "id": tid, "name": "current_time",
                     "input": {}}],
        "stop_reason": "tool_use", "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}})


def _text(text="done"):
    return _msg({
        "id": "m2", "type": "message", "role": "assistant", "model": "claude-test",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}})


def test_agent_sees_steering_note_after_tool_round(monkeypatch):
    from olympus import tools
    requests = []

    def stream(**params):
        requests.append(params)

        class _S:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def get_final_message(s):
                return _text() if len(requests) > 1 else _tooluse()
        return _S()

    msgs = types.SimpleNamespace(stream=stream)
    fake = types.SimpleNamespace(messages=msgs,
                                 beta=types.SimpleNamespace(messages=msgs))
    monkeypatch.setattr(llm, "client", lambda settings=None: fake)
    monkeypatch.setattr(tools, "resolve_handler",
                        lambda name: (lambda **_: "12:00"))

    token = steering.set_current("steer-conv")
    try:
        steering.put("steer-conv", "actually, use UTC")
        out = agent.run_agent(
            "sys", "time?", settings=config.Settings(provider="anthropic",
                                                     model="claude-test"),
            tool_defs=[{"name": "current_time", "description": "",
                        "input_schema": {}}])
    finally:
        steering.reset(token)
    assert out == "done"
    # The second request's tool-result turn carries the steering note.
    last_user = requests[1]["messages"][-1]["content"]
    notes = [b for b in last_user if isinstance(b, dict)
             and b.get("type") == "text" and "use UTC" in b.get("text", "")]
    assert notes, f"steering note missing from {last_user}"
    assert steering.pending("steer-conv") == 0   # drained by the run


# --- undo ------------------------------------------------------------------

def _bot(monkeypatch, conv="conv-undo"):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    return orchestrator.Olympus(user="undo-user", conversation_id=conv)


def test_undo_removes_last_exchange_and_persists(monkeypatch):
    bot = _bot(monkeypatch)
    bot.history = [{"role": "user", "content": "q1"},
                   {"role": "assistant", "content": "a1"},
                   {"role": "user", "content": "q2"},
                   {"role": "assistant", "content": "a2"}]
    memory.save_conversation("conv-undo", bot.history, owner=bot.user)
    out = bot.undo()
    assert "1 exchange" in out
    assert [m["content"] for m in bot.history] == ["q1", "a1"]
    assert len(memory.load_conversation("conv-undo")) == 2   # persisted


def test_undo_multiple_and_empty(monkeypatch):
    bot = _bot(monkeypatch, conv="conv-undo2")
    bot.history = [{"role": "user", "content": "q1"},
                   {"role": "assistant", "content": "a1"}]
    assert "1 exchange" in bot.undo(5)          # clamps to what exists
    assert bot.history == []
    assert bot.undo() == "Nothing to undo."


def test_undo_stops_at_non_pair_boundary(monkeypatch):
    bot = _bot(monkeypatch, conv="conv-undo3")
    bot.history = [{"role": "assistant", "content": "[state block]"},
                   {"role": "user", "content": "q"},
                   {"role": "assistant", "content": "a"}]
    bot.undo(3)
    assert bot.history == [{"role": "assistant", "content": "[state block]"}]


# --- command surfaces -------------------------------------------------------

def test_tui_dispatch_undo_and_steer():
    class FakeBot:
        user = "cli"
        conversation_id = "cli-default"
        def undo(self, n): return f"undid {n}"
    handled, out, _ = tui.dispatch_command(FakeBot(), "/undo 2")
    assert handled and out == "undid 2"
    handled, out, _ = tui.dispatch_command(FakeBot(), "/steer look at ARM too")
    assert handled and "next tool call" in out
    assert steering.drain("cli-default") == ["look at ARM too"]


def test_gateway_try_steer_fast_path():
    assert gateway.try_steer("U1", "hello there", prefix="sl") is None
    out = gateway.try_steer("U1", "/steer focus on Q3", prefix="sl")
    assert out and "next tool call" in out[0]
    assert steering.drain("sl-U1") == ["focus on Q3"]
    out = gateway.try_steer("U1", "/steer", prefix="sl")
    assert out and "Usage" in out[0]

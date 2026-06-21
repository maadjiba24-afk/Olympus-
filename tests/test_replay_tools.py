"""Re-executable replay must freeze client-side tool results, not just LLM
responses — otherwise a nondeterministic tool (e.g. `current_time`) breaks a
byte-identical replay. This reproduces the live Tier-1 finding in miniature.
"""

import types

import anthropic
import pytest

from olympus import agent, config, llm, replaystore, tools


def _tooluse_message(tool_id: str, name: str):
    return anthropic.types.Message.model_validate({
        "id": "m1", "type": "message", "role": "assistant", "model": "claude-test",
        "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": {}}],
        "stop_reason": "tool_use", "stop_sequence": None,
        "usage": {"input_tokens": 5, "output_tokens": 5,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
    })


def _text_message(text: str):
    return anthropic.types.Message.model_validate({
        "id": "m2", "type": "message", "role": "assistant", "model": "claude-test",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 5, "output_tokens": 5,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
    })


def _has_tool_result(messages) -> bool:
    last = messages[-1] if messages else {}
    content = last.get("content") if isinstance(last, dict) else None
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def _client_factory(call_counter):
    """A model that calls `current_time` once, then answers using its result."""
    def stream(**params):
        class _S:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def get_final_message(s):
                call_counter[0] += 1
                if _has_tool_result(params["messages"]):
                    return _text_message("done")
                return _tooluse_message("toolu_clock", "current_time")
        return _S()
    msgs = types.SimpleNamespace(stream=stream)
    return types.SimpleNamespace(messages=msgs,
                                 beta=types.SimpleNamespace(messages=msgs))


def test_nondeterministic_tool_is_frozen_and_replayed(monkeypatch):
    # A genuinely nondeterministic tool: a different value every call.
    clock = [0]

    def fake_clock(**_):
        clock[0] += 1
        return f"2026-06-21T00:00:0{clock[0]}"

    monkeypatch.setattr(tools, "resolve_handler",
                        lambda name: fake_clock if name == "current_time" else None)
    api_calls = [0]
    monkeypatch.setattr(llm, "client", lambda settings=None: _client_factory(api_calls))
    settings = config.Settings(provider="anthropic", model="claude-test")
    tool_defs = [{"name": "current_time", "description": "now", "input_schema": {}}]

    # Record: the tool runs for real (clock -> 1) and the result is frozen.
    out = agent.run_agent("sys", "what time is it?", settings=settings,
                          tool_defs=tool_defs)
    assert out == "done"
    assert clock[0] == 1                          # tool executed once
    api_after_record = api_calls[0]

    # Replay: the tool must NOT run again; its frozen value is reused so the
    # second LLM request matches the frozen one (no divergence, no network).
    monkeypatch.setenv("OLYMPUS_REPLAY", "some-run")
    monkeypatch.setattr(llm, "client", lambda settings=None: (_ for _ in ()).throw(
        AssertionError("replay must not call the API")))

    out2 = agent.run_agent("sys", "what time is it?", settings=settings,
                           tool_defs=tool_defs)
    assert out2 == "done"
    assert clock[0] == 1            # tool was NOT re-executed on replay
    assert api_calls[0] == api_after_record   # no new API calls


def test_missing_tool_result_in_replay_is_divergence(monkeypatch):
    monkeypatch.setenv("OLYMPUS_REPLAY", "some-run")
    block = types.SimpleNamespace(type="tool_use", id="toolu_unseen",
                                  name="current_time", input={})
    with pytest.raises(replaystore.ReplayDivergence):
        agent._tool_result(block)


def test_tool_result_roundtrips_through_store():
    replaystore.put_tool("toolu_abc", "frozen value", is_error=False)
    got = replaystore.get_tool("toolu_abc")
    assert got == {"content": "frozen value", "is_error": False}
    assert replaystore.get_tool("toolu_missing") is None

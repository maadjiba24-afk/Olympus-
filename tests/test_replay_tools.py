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


# --- frozen run-state context (the memory-drift fix) ---------------------

def test_frozen_context_replays_despite_state_change(monkeypatch):
    # Record: capture the memory context for this run.
    replaystore.set_run("run-xyz")
    monkeypatch.delenv("OLYMPUS_REPLAY", raising=False)
    recorded = replaystore.frozen_context("route", lambda: "MEMORY AS RECORDED")
    assert recorded == "MEMORY AS RECORDED"

    # Replay: even though memory has since changed, the frozen value is returned
    # (so the router's prompt — and thus its request — stays byte-identical).
    monkeypatch.setenv("OLYMPUS_REPLAY", "run-xyz")
    replayed = replaystore.frozen_context(
        "route", lambda: "MEMORY MUTATED LATER — should NOT be used")
    assert replayed == "MEMORY AS RECORDED"


def test_frozen_context_missing_on_replay_is_divergence(monkeypatch):
    monkeypatch.setenv("OLYMPUS_REPLAY", "run-never-recorded")
    with pytest.raises(replaystore.ReplayDivergence):
        replaystore.frozen_context("route", lambda: "x")


def test_frozen_context_is_noop_outside_a_run(monkeypatch):
    monkeypatch.delenv("OLYMPUS_REPLAY", raising=False)
    replaystore.set_run(None)
    assert replaystore.frozen_context("route", lambda: "live") == "live"


# --- end-to-end: the live Tier-1 finding, reproduced and fixed -----------

def _pipeline_message(text):
    return anthropic.types.Message.model_validate({
        "id": "m", "type": "message", "role": "assistant", "model": "claude-test",
        "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 5, "output_tokens": 5,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
    })


def _pipeline_client():
    import json as _json

    def stream(**params):
        schema = ((params.get("output_config") or {}).get("format") or {}).get("schema") or {}
        props = schema.get("properties", {})

        class _S:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def get_final_message(s):
                if "mode" in props:
                    return _pipeline_message(_json.dumps(
                        {"mode": "delegate", "direct_reply": None,
                         "specialists": ["argus"], "brief": "research",
                         "needs_verification": False}))
                if "steps" in props:
                    return _pipeline_message(_json.dumps(
                        {"steps": [{"id": "s1", "specialist": "argus",
                                    "task": "research", "depends_on": []}]}))
                if "verdict" in props:
                    return _pipeline_message(_json.dumps(
                        {"verdict": "approve", "feedback": "",
                         "retry_specialists": []}))
                return _pipeline_message("findings")
        return _S()
    msgs = types.SimpleNamespace(stream=stream)
    return types.SimpleNamespace(messages=msgs,
                                 beta=types.SimpleNamespace(messages=msgs))


def test_pipeline_replays_despite_memory_context_drift(monkeypatch):
    """The exact live-Tier-1 failure: the router's memory context changes
    between record and replay. With it frozen, the run replays byte-identically."""
    from olympus import config, orchestrator, recall

    seen = {"n": 0}

    def drifting(user, msg):                 # different every call (like the learner)
        seen["n"] += 1
        return f"\n[recalled memory version {seen['n']}]"

    monkeypatch.setattr(recall, "context_block", drifting)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(config, "MEMORY_ENABLED", False)
    monkeypatch.setattr(llm, "client", lambda settings=None: _pipeline_client())

    bot = orchestrator.Olympus(pool=config.ModelPool.from_env())
    bot.ask("research the current state of X")
    rid = bot.last_run_id
    calls_at_record = seen["n"]

    monkeypatch.setattr(llm, "client", lambda settings=None: (_ for _ in ()).throw(
        AssertionError("replay must not call the API")))
    _orig, _fresh, diffs = orchestrator.replay_run(rid)

    assert diffs == []                       # byte-identical despite the drift
    assert seen["n"] == calls_at_record      # context was frozen, not recomputed

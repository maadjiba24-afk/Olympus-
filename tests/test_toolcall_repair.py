"""Tests for refusal-safe tool-call repair.

Absorbs alibaba/the surveyed DOM agent's `autoFixer` malformed-tool-call salvage — and
proves the structural inversion: recovery NEVER fabricates an action from a
refusal or a plain answer, because it fires only when the content names a tool
the model was actually offered.
"""

from __future__ import annotations

import json

import pytest

from olympus import toolcall_repair as tr


# --- extract_json_object ------------------------------------------------------

def test_extract_plain_object():
    assert tr.extract_json_object('{"a": 1}') == {"a": 1}


def test_extract_fenced_object():
    assert tr.extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_object_with_trailing_prose():
    # The naive find('{')..rfind('}') survives this, but the balanced scan is
    # what makes the NEXT case work.
    assert tr.extract_json_object('Here you go: {"a": 1}. Done.') == {"a": 1}


def test_extract_stops_at_first_balanced_object_ignoring_later_braces():
    # A trailing second object (or a stray '}') must not defeat parsing — the
    # brace-balanced scan returns the first complete object.
    assert tr.extract_json_object('{"a": 1} and then {"b": 2}') == {"a": 1}


def test_extract_object_with_brace_inside_string():
    assert tr.extract_json_object('{"note": "a } brace"}') == {"note": "a } brace"}


def test_extract_none_for_prose():
    assert tr.extract_json_object("I cannot help with that.") is None


def test_extract_none_for_bare_array():
    # A non-object JSON value is not an object.
    assert tr.extract_json_object("[1, 2, 3]") is None


def test_extract_passthrough_dict():
    assert tr.extract_json_object({"x": 9}) == {"x": 9}


# --- repair_arguments ---------------------------------------------------------

def test_args_plain_json():
    assert tr.repair_arguments('{"index": 3}') == {"index": 3}


def test_args_already_dict():
    assert tr.repair_arguments({"index": 3}) == {"index": 3}


def test_args_double_encoded():
    # The classic weak-model bug: arguments is a JSON string of a JSON object.
    assert tr.repair_arguments(json.dumps('{"index": 3}')) == {"index": 3}


def test_args_fenced():
    assert tr.repair_arguments('```json\n{"q": "hi"}\n```') == {"q": "hi"}


def test_args_empty_and_none_degrade_to_empty_dict():
    assert tr.repair_arguments(None) == {}
    assert tr.repair_arguments("") == {}
    assert tr.repair_arguments("   ") == {}


def test_args_unrecoverable_degrades_not_raises():
    # Non-JSON garbage → {}, so the handler errors on missing params exactly as
    # it would without repair (no new failure mode).
    assert tr.repair_arguments("not json at all") == {}


# --- recover_tool_call: the refusal-safety guard ------------------------------

KNOWN = {"web_search", "browser_click"}


def test_recover_flat_name_arguments_shape():
    content = '{"name": "web_search", "arguments": {"query": "olympus"}}'
    call = tr.recover_tool_call(content, KNOWN)
    assert call is not None
    assert call["function"]["name"] == "web_search"
    assert json.loads(call["function"]["arguments"]) == {"query": "olympus"}


def test_recover_nested_function_shape():
    content = '{"function": {"name": "browser_click", "arguments": {"index": 2}}}'
    call = tr.recover_tool_call(content, KNOWN)
    assert call is not None
    assert call["function"]["name"] == "browser_click"
    assert json.loads(call["function"]["arguments"]) == {"index": 2}


def test_recover_tool_parameters_shape():
    content = '{"tool": "web_search", "parameters": {"query": "x"}}'
    call = tr.recover_tool_call(content, KNOWN)
    assert call is not None and call["function"]["name"] == "web_search"


def test_recover_dom_agent_single_key_action_shape():
    # {"<tool>": {...}} — the surveyed DOM agent's action shape.
    content = '{"browser_click": {"index": 5}}'
    call = tr.recover_tool_call(content, KNOWN)
    assert call is not None
    assert json.loads(call["function"]["arguments"]) == {"index": 5}


def test_recover_double_encoded_arguments():
    content = json.dumps({"name": "web_search",
                          "arguments": json.dumps({"query": "z"})})
    call = tr.recover_tool_call(content, KNOWN)
    assert call is not None
    assert json.loads(call["function"]["arguments"]) == {"query": "z"}


def test_refusal_is_never_recovered():
    assert tr.recover_tool_call("I'm sorry, I can't do that.", KNOWN) is None


def test_plain_answer_object_naming_no_tool_is_not_recovered():
    # A final-answer wrapper that names no offered tool stays text.
    assert tr.recover_tool_call('{"answer": "the capital is Paris"}', KNOWN) is None


def test_unknown_tool_name_is_not_recovered():
    # Names a tool, but not one that was offered → refuse to fabricate.
    content = '{"name": "rm_rf_everything", "arguments": {}}'
    assert tr.recover_tool_call(content, KNOWN) is None


def test_no_known_names_recovers_nothing():
    assert tr.recover_tool_call('{"name": "web_search", "arguments": {}}', set()) is None


def test_single_key_shape_requires_known_tool():
    # A single-key object whose key isn't an offered tool must not match.
    assert tr.recover_tool_call('{"result": {"ok": true}}', KNOWN) is None


def test_recover_missing_arguments_defaults_empty():
    content = '{"name": "web_search"}'
    call = tr.recover_tool_call(content, KNOWN)
    assert call is not None
    assert json.loads(call["function"]["arguments"]) == {}


# --- integration: run_agent recovers a content-emitted tool call --------------

def test_run_agent_recovers_content_tool_call(monkeypatch):
    """A model that returns the tool call as JSON in `content` (empty
    `tool_calls`) still drives the tool — then finishes on the next turn."""
    from olympus import config, openai_compat, tools

    calls: list[dict] = []

    responses = [
        # Turn 1: tool call emitted as content JSON, no tool_calls array.
        {"choices": [{"message": {
            "content": '{"name": "demo_tool", "arguments": {"x": 7}}',
            "tool_calls": [],
        }}]},
        # Turn 2: a normal final answer.
        {"choices": [{"message": {"content": "done", "tool_calls": []}}]},
    ]

    # Advance through the two canned responses.
    idx = {"i": 0}

    def fake_post(settings, payload):
        r = responses[idx["i"]]
        idx["i"] += 1
        return r

    def demo_tool(x: int) -> str:
        calls.append({"x": x})
        return f"got {x}"

    monkeypatch.setattr(openai_compat, "_post", fake_post)
    monkeypatch.setattr(tools, "resolve_handler",
                        lambda name: demo_tool if name == "demo_tool" else None)

    settings = config.Settings(provider="openai_compat", model="local-model")
    out = openai_compat.run_agent(
        settings, "sys", "do it",
        [{"name": "demo_tool", "description": "d",
          "input_schema": {"type": "object"}}],
    )
    assert out == "done"
    assert calls == [{"x": 7}]  # the recovered call actually executed


def test_run_agent_refusal_not_turned_into_tool_call(monkeypatch):
    """A refusal in `content` with empty `tool_calls` is returned as the final
    answer — never fabricated into an action."""
    from olympus import config, openai_compat, tools

    executed: list[str] = []

    def fake_post(settings, payload):
        return {"choices": [{"message": {
            "content": "I can't comply with that request.",
            "tool_calls": [],
        }}]}

    monkeypatch.setattr(openai_compat, "_post", fake_post)
    monkeypatch.setattr(tools, "resolve_handler",
                        lambda name: executed.append(name))

    settings = config.Settings(provider="openai_compat", model="local-model")
    out = openai_compat.run_agent(
        settings, "sys", "do it",
        [{"name": "demo_tool", "description": "d",
          "input_schema": {"type": "object"}}],
    )
    assert out == "I can't comply with that request."
    assert executed == []  # nothing was executed

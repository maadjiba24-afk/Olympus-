"""Validate the Tier-1 exit-gate harness end-to-end against a mocked model.

This proves the harness itself works: it drives the *real* orchestrator pipeline
(route -> plan -> dispatch -> review -> synthesize), then re-executes each run
against the frozen responses and correctly reports completed + replayable. The
live gate (real Claude calls on 3 prompts) runs the same code with a real key.
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

import anthropic

from olympus import config, llm, orchestrator  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "tier1_exit_check", ROOT / "scripts" / "tier1_exit_check.py")
harness = importlib.util.module_from_spec(_spec)
sys.modules["tier1_exit_check"] = harness
_spec.loader.exec_module(harness)


def _message(text: str) -> anthropic.types.Message:
    return anthropic.types.Message.model_validate({
        "id": "m", "type": "message", "role": "assistant", "model": "claude-test",
        "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 5, "output_tokens": 5,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
    })


def _responder(params):
    """A deterministic stand-in model: returns a schema-valid response for each
    pipeline stage (route/plan/review) and plain text for specialist + synth."""
    schema = ((params.get("output_config") or {}).get("format") or {}).get("schema") or {}
    props = schema.get("properties", {})
    if "mode" in props:
        return _message(json.dumps({"mode": "delegate", "direct_reply": None,
                                    "specialists": ["argus"],
                                    "brief": "research and summarize",
                                    "needs_verification": False}))
    if "steps" in props:
        return _message(json.dumps({"steps": [{"id": "s1", "specialist": "argus",
                                               "task": "Research and list risks",
                                               "depends_on": []}]}))
    if "verdict" in props:
        return _message(json.dumps({"verdict": "approve", "feedback": "",
                                    "retry_specialists": []}))
    return _message("Findings: solid. Risks: r1, r2.")


def _fake_client(settings=None):
    def stream(**params):
        class _S:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def get_final_message(s): return _responder(params)
            @property
            def text_stream(s):
                yield "answer"
        return _S()
    msgs = types.SimpleNamespace(stream=stream)
    return types.SimpleNamespace(messages=msgs,
                                 beta=types.SimpleNamespace(messages=msgs))


def test_harness_reports_gate_met_when_runs_replay(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")     # pass settings.validate()
    monkeypatch.setattr(config, "MEMORY_ENABLED", False)  # no bg extraction thread
    monkeypatch.setattr(llm, "client", _fake_client)

    prompts = ["task one, multi-step", "task two, different", "task three, also"]
    all_pass, results = harness.run_exit_check(prompts, report=lambda *a: None)

    assert all_pass is True
    assert len(results) == 3
    for r in results:
        assert r["completed"] is True
        assert r["replayable"] is True            # replayed byte-identically
        assert r["decisions"] >= 3                # route + plan + review
        assert r["run_id"]


def test_harness_fails_gate_with_fewer_than_three(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(config, "MEMORY_ENABLED", False)
    monkeypatch.setattr(llm, "client", _fake_client)

    all_pass, results = harness.run_exit_check(["only one task"],
                                               report=lambda *a: None)
    assert all_pass is False                       # the gate requires 3
    assert results[0]["completed"] and results[0]["replayable"]


def test_harness_flags_incomplete_run(monkeypatch):
    # A bot whose ask() returns a config error must be reported as not completed.
    monkeypatch.setattr(config, "MEMORY_ENABLED", False)

    class _Stub:
        last_run_id = None

        def ask(self, prompt):
            return "Configuration problem: no API key configured."

    all_pass, results = harness.run_exit_check(
        ["a", "b", "c"], make_bot=lambda: _Stub(), report=lambda *a: None)
    assert all_pass is False
    assert all(r["completed"] is False for r in results)

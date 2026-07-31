"""Validate the Tier-1 exit-gate harness end-to-end against a mocked model.

This proves the harness itself works: it drives the *real* orchestrator pipeline
(route -> plan -> dispatch -> review -> synthesize), then re-executes each run
against the frozen responses and correctly reports completed + replayable. The
live gate (real Claude calls on 3 prompts) runs the same code with a real key.
"""

import json
import types

import anthropic

from olympus import config, llm, orchestrator  # noqa: F401
from olympus import replaygate as harness


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


# --- the automatic tripwire (self_check escalation) ----------------------

def test_self_check_passes_quietly_when_replayable(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(config, "MEMORY_ENABLED", False)
    monkeypatch.setattr(llm, "client", _fake_client)
    res = harness.self_check(["one", "two", "three"], report=lambda *a: None)
    assert res["passed"] is True and res["ran"] is True


def test_self_check_escalates_on_divergence(monkeypatch):
    # A run that completes but never replays -> the gate must fail AND escalate.
    from olympus import github, memory, telegram

    class _Stub:
        last_run_id = "r1"
        def ask(self, prompt):
            return "a real answer"

    # make it look completed-but-not-replayable: a REAL recorded run, so its
    # decision carries the frozen `model_request_hash` a recorded model call
    # stamps. Without one the run is "nothing was recorded" — inconclusive, not
    # a divergence — and must NOT escalate (see test_replaygate.py, E25).
    monkeypatch.setattr(harness, "trace", types.SimpleNamespace(
        load_run=lambda rid: {"decisions": [{"model_request_hash": "h1"}]}))
    monkeypatch.setattr(harness.orchestrator, "replay_run",
                        lambda rid: (_ for _ in ()).throw(
                            harness.replaystore.ReplayDivergence("h", {"model": "m"})))

    alerts = {}
    monkeypatch.setattr(telegram, "notify", lambda msg: alerts.setdefault("tg", msg))
    monkeypatch.setattr(github, "configured", lambda: True)

    def _fake_issue(title, body):
        alerts["issue"] = title
        return "http://x/1"
    monkeypatch.setattr(github, "create_issue", _fake_issue)
    saved = []
    monkeypatch.setattr(memory, "save", lambda cat, title, body: saved.append((cat, title)))

    res = harness.self_check(["a", "b", "c"], make_bot=lambda: _Stub(),
                             report=lambda *a: None)
    assert res["passed"] is False
    assert "tg" in alerts                      # Telegram alerted
    assert "issue" in alerts                    # GitHub issue filed
    assert any(cat == "corrections" for cat, _ in saved)   # recorded to memory
    assert res["issue"] == "http://x/1"


def test_self_check_skips_when_budget_exceeded(monkeypatch):
    from olympus import usage
    monkeypatch.setattr(usage, "check_budget",
                        lambda: (_ for _ in ()).throw(usage.BudgetExceeded("cap")))
    res = harness.self_check(["a", "b", "c"], report=lambda *a: None)
    assert res["ran"] is False and "cap" in res["skipped"]


def test_provider_error_is_skipped_not_failed():
    # An out-of-credits / billing error must be a SKIP, never a replay failure.
    class _Broke:
        last_run_id = None
        def ask(self, prompt):
            raise RuntimeError("Error code: 400 — Your credit balance is too low "
                               "to access the Anthropic API. Plans & Billing.")

    all_pass, results = harness.run_exit_check(
        ["a", "b", "c"], make_bot=lambda: _Broke(), report=lambda *a: None)
    assert all_pass is False
    assert all(r["skipped"] for r in results)
    assert harness.genuine_failures(results) == []     # nothing genuinely failed


def test_self_check_does_not_escalate_on_provider_error(monkeypatch):
    from olympus import github, telegram
    alerts = {}
    monkeypatch.setattr(telegram, "notify", lambda m: alerts.setdefault("tg", m))
    monkeypatch.setattr(github, "configured", lambda: True)
    monkeypatch.setattr(github, "create_issue",
                        lambda t, b: alerts.setdefault("issue", t))

    class _Broke:
        last_run_id = None
        def ask(self, prompt):
            raise RuntimeError("credit balance is too low")

    res = harness.self_check(["a", "b", "c"], make_bot=lambda: _Broke(),
                             report=lambda *a: None)
    assert res["ran"] is False and res.get("skipped")
    assert "tg" not in alerts and "issue" not in alerts   # no false alarm


def test_gate_bot_pins_cheaper_model(monkeypatch):
    # The gate runs on the cheaper GATE_MODEL by default, not the main model,
    # so the weekly tripwire stays affordable.
    monkeypatch.setenv("OLYMPUS_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(config, "GATE_MODEL", "claude-sonnet-4-6")
    bot = harness._gate_bot()
    assert bot.pool.primary().model == "claude-sonnet-4-6"


def test_self_check_does_not_escalate_when_nothing_was_recorded(monkeypatch):
    """E25's blast radius on the automatic tripwire: a keyless/degraded run
    freezes no model call, so it is INCONCLUSIVE. It must not file a GitHub
    issue or page anyone — a false divergence alert on a cadence is exactly how
    an operator learns to ignore the tripwire."""
    from olympus import github, memory, telegram

    class _Stub:
        last_run_id = "r1"

        def ask(self, prompt):
            return "a real answer"

    monkeypatch.setattr(config, "MEMORY_ENABLED", False)
    monkeypatch.setattr(harness, "trace", types.SimpleNamespace(
        load_run=lambda rid: {"decisions": [{"model_request_hash": None}]}))

    alerts = {}
    monkeypatch.setattr(telegram, "notify",
                        lambda msg: alerts.setdefault("tg", msg))
    monkeypatch.setattr(github, "configured", lambda: True)
    monkeypatch.setattr(github, "create_issue",
                        lambda t, b: alerts.setdefault("issue", t))
    monkeypatch.setattr(memory, "save",
                        lambda cat, title, body: alerts.setdefault("mem", cat))

    res = harness.self_check(["a", "b", "c"], make_bot=lambda: _Stub(),
                             report=lambda *a: None)
    assert res["passed"] is not True            # not a pass either
    assert alerts == {}, alerts                 # nothing escalated

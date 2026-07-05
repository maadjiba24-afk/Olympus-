"""SPEC-04 Phase A: routing-outcome telemetry (a passive sensor).

Proves the linkage row, the outcome-signal precedence, outcomes.py-style caps +
per-user scoping, the `routing-stats` gate math (synthetic/self-only never
counts), and — critically — that model SELECTION is unchanged and a telemetry
failure never breaks a run. No real network anywhere.
"""

import json
import socket
import types

import anthropic
import pytest

from olympus import config, llm, recall, routing_outcomes as ro


@pytest.fixture(autouse=True)
def no_outbound_sockets(monkeypatch):
    real = socket.socket.connect

    def guard(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"unexpected outbound socket to {address!r}")
        return real(self, address)

    monkeypatch.setattr(socket.socket, "connect", guard)
    yield


# --- a mocked council model (no network) -------------------------------------
def _msg(text):
    return anthropic.types.Message.model_validate({
        "id": "m", "type": "message", "role": "assistant", "model": "claude-test",
        "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 5, "output_tokens": 5,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
    })


def _pipeline_client(verdict="approve"):
    def stream(**params):
        schema = ((params.get("output_config") or {}).get("format") or {}).get("schema") or {}
        props = schema.get("properties", {})

        class _S:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def get_final_message(s):
                if "mode" in props:
                    return _msg(json.dumps({"mode": "delegate", "direct_reply": None,
                        "specialists": ["argus"], "brief": "research",
                        "needs_verification": False}))
                if "steps" in props:
                    return _msg(json.dumps({"steps": [{"id": "s1", "specialist": "argus",
                        "task": "research", "depends_on": []}]}))
                if "verdict" in props:
                    return _msg(json.dumps({"verdict": verdict, "feedback": "",
                        "retry_specialists": []}))
                return _msg("findings")
        return _S()
    msgs = types.SimpleNamespace(stream=stream)
    return types.SimpleNamespace(messages=msgs, beta=types.SimpleNamespace(messages=msgs))


def _run(monkeypatch, verdict="approve"):
    from olympus import orchestrator
    monkeypatch.setattr(recall, "context_block", lambda user, msg: "")
    monkeypatch.setattr(config, "MEMORY_ENABLED", False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(llm, "client", lambda settings=None: _pipeline_client(verdict))
    bot = orchestrator.Olympus(pool=config.ModelPool.from_env(), user="alice")
    bot.ask("research the current state of X")
    return bot


# --- 1. linkage --------------------------------------------------------------
def test_run_records_linked_routing_outcome(monkeypatch):
    bot = _run(monkeypatch, verdict="approve")
    rows = ro.events("alice")
    assert len(rows) == 1
    r = rows[0]
    assert r["run_id"] == bot.last_run_id
    assert r["specialist"] == "argus"
    assert r["role"] == config.specialist_role("argus")
    assert r["model"]                                   # the model actually used
    assert r["task_type"] == "research"
    assert r["outcome_signal"] == ro.POSITIVE           # from the review verdict
    assert r["signal_source"] == ro.SRC_REVIEW


def test_retry_verdict_is_negative(monkeypatch):
    _run(monkeypatch, verdict="retry")
    rows = ro.events("alice")
    assert rows and rows[0]["outcome_signal"] == ro.NEGATIVE


# --- 2. precedence -----------------------------------------------------------
def test_signal_precedence_order():
    # feedback > review > action
    assert ro.resolve_signal(feedback=ro.POSITIVE, review=ro.NEGATIVE,
                             action=ro.NEGATIVE) == (ro.POSITIVE, ro.SRC_FEEDBACK)
    assert ro.resolve_signal(review=ro.POSITIVE,
                             action=ro.NEGATIVE) == (ro.POSITIVE, ro.SRC_REVIEW)
    assert ro.resolve_signal(action=ro.APPROVED_AFTER_EDIT) == (
        ro.APPROVED_AFTER_EDIT, ro.SRC_ACTION)
    assert ro.resolve_signal() == (ro.PENDING, ro.SRC_NONE)


def test_feedback_upgrades_review_signal(monkeypatch):
    bot = _run(monkeypatch, verdict="approve")
    assert ro.events("alice")[0]["signal_source"] == ro.SRC_REVIEW
    # A later explicit 👎 overrides the review verdict (feedback is top tier).
    bot.feedback("down")
    r = ro.events("alice")[0]
    assert r["outcome_signal"] == ro.NEGATIVE
    assert r["signal_source"] == ro.SRC_FEEDBACK


# --- 3. caps + per-user scoping (mirrors outcomes.py) ------------------------
def test_cap_and_rolling_window():
    for i in range(ro._MAX + 50):
        ro.record_run("bob", f"run{i}", "hi", ["argus"],
                      models={"argus": "m"}, roles={"argus": "reasoning"},
                      review_verdict="approve")
    rows = ro.events("bob")
    assert len(rows) == ro._MAX                         # hard rolling cap
    assert rows[-1]["run_id"] == f"run{ro._MAX + 49}"   # newest retained


def test_per_user_scoping():
    ro.record_run("u1", "r1", "hi", ["argus"], models={"argus": "m"},
                  roles={"argus": "reasoning"}, review_verdict="approve")
    ro.record_run("u2", "r2", "hi", ["plutus"], models={"plutus": "m"},
                  roles={"plutus": "reasoning"}, review_verdict="retry")
    assert len(ro.events("u1")) == 1 and ro.events("u1")[0]["specialist"] == "argus"
    assert len(ro.events("u2")) == 1 and ro.events("u2")[0]["specialist"] == "plutus"


# --- 4. routing-stats math + the gate ---------------------------------------
def _seed_corpus():
    # Two real users, several task-types, > a few labeled outcomes each.
    specs = ["argus", "hephaestus", "plutus", "peitho"]     # research/code/finance/marketing
    for u in ("real-a", "real-b"):
        for i, k in enumerate(specs * 3):
            ro.record_run(u, f"{u}-{i}", "task", [k], models={k: "opus"},
                          roles={k: config.specialist_role(k)},
                          review_verdict="approve")


def test_stats_counts_and_breakdown():
    _seed_corpus()
    s = ro.stats()
    assert s["labeled"] == 24                            # 2 users * 12 rows
    assert set(s["task_types"]) == {"research", "code", "finance", "marketing"}
    assert s["distinct_users"] == 2
    assert s["by_specialist_model"]["argus/opus"] == 6


def test_synthetic_and_self_only_do_not_satisfy_gate():
    # A large SYNTHETIC corpus from one source must not count toward the gate.
    for i in range(ro.GATE_MIN_LABELED + 100):
        ro.record_run("loadtest", f"s{i}", "task", ["argus"], models={"argus": "m"},
                      roles={"argus": "reasoning"}, review_verdict="approve",
                      synthetic=True)
    g = ro.gate_status()
    assert g["met"] is False
    assert g["labeled"] == 0                             # synthetic excluded
    # Real but single-source + below-threshold also fails, with reasons.
    ro.record_run("real-a", "r", "task", ["argus"], models={"argus": "m"},
                  roles={"argus": "reasoning"}, review_verdict="approve")
    g2 = ro.gate_status()
    assert g2["met"] is False and g2["reasons"]


def test_gate_met_only_when_all_thresholds_pass(monkeypatch):
    # Shrink thresholds so we can drive a MET verdict deterministically.
    monkeypatch.setattr(ro, "GATE_MIN_LABELED", 4)
    monkeypatch.setattr(ro, "GATE_MIN_TASK_TYPES", 2)
    monkeypatch.setattr(ro, "GATE_MIN_DISTINCT_USERS", 2)
    for u in ("real-a", "real-b"):
        for k in ("argus", "hephaestus"):               # research + code
            ro.record_run(u, f"{u}-{k}", "task", [k], models={k: "m"},
                          roles={k: "reasoning"}, review_verdict="approve")
    g = ro.gate_status()
    assert g["met"] is True and not g["reasons"]


# --- 5. non-interference (critical) -----------------------------------------
def test_model_selection_unchanged_with_telemetry(monkeypatch):
    # Selection must remain exactly capability_score's choice — telemetry reads
    # it, never changes it.
    opus = config.Settings(provider="anthropic", model="claude-opus-4-8")
    haiku = config.Settings(provider="anthropic", model="claude-haiku-4-5")
    pool = config.ModelPool.of(opus, haiku)
    expected = max((opus, haiku),
                   key=lambda s: config.capability_score(s.model, "reasoning"))
    assert pool.for_role("reasoning").model == expected.model == "claude-opus-4-8"
    # Running a full instrumented pipeline does not perturb the pool's choice.
    _run(monkeypatch, verdict="approve")
    assert pool.for_role("reasoning").model == "claude-opus-4-8"


def test_telemetry_failure_never_breaks_a_run(monkeypatch):
    boom = {"n": 0}

    def explode(*a, **k):
        boom["n"] += 1
        raise RuntimeError("telemetry is down")

    monkeypatch.setattr(ro, "record_run", explode)
    bot = _run(monkeypatch, verdict="approve")           # must NOT raise
    assert bot.last_run_id                                # the run still completed
    assert boom["n"] >= 1                                 # our failing hook was hit


def test_replay_run_emits_no_telemetry(monkeypatch):
    from olympus import orchestrator, replaystore
    bot = _run(monkeypatch, verdict="approve")
    rid = bot.last_run_id
    before = len(ro.events("alice"))
    monkeypatch.setattr(llm, "client", lambda settings=None: (_ for _ in ()).throw(
        AssertionError("replay must not call the API")))
    orchestrator.replay_run(rid)
    assert len(ro.events("alice")) == before             # replay writes no rows

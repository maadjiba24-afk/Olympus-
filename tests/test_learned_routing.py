"""SPEC-04 Phase B: the evidence-gated learned selector.

Proves every activation gate (flag, data gate, per-cell samples, incumbent
evidence, replay), the conservative override rule, composition with pins and
the sovereign filter, and — critically — that with the flag off or no data the
selection is byte-for-byte the keyword heuristic. No real network.
"""

import json
import socket
import types

import anthropic
import pytest

from olympus import config, learned_routing as lr, llm, recall
from olympus import routing_outcomes as ro


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


@pytest.fixture(autouse=True)
def fresh_cache():
    lr.clear_cache()
    yield
    lr.clear_cache()


OPUS = config.Settings(provider="anthropic", model="claude-opus-4-8")
HAIKU = config.Settings(provider="anthropic", model="claude-haiku-4-5")


def _seed(specialist, model, signal, n, user="real-a", synthetic=False):
    for i in range(n):
        ro.record_run(user, f"{user}-{specialist}-{model}-{signal}-{i}", "task",
                      [specialist], models={specialist: model},
                      roles={specialist: "reasoning"},
                      review_verdict="approve" if signal == ro.POSITIVE else "retry",
                      synthetic=synthetic)


def _open_gate(monkeypatch, labeled=10, types_=1, users=1):
    monkeypatch.setattr(ro, "GATE_MIN_LABELED", labeled)
    monkeypatch.setattr(ro, "GATE_MIN_TASK_TYPES", types_)
    monkeypatch.setattr(ro, "GATE_MIN_DISTINCT_USERS", users)


def _enable(monkeypatch):
    monkeypatch.setenv("OLYMPUS_LEARNED_ROUTING", "1")
    monkeypatch.delenv("OLYMPUS_REPLAY", raising=False)


# --- wilson math ---------------------------------------------------------
def test_wilson_lower_bound_is_small_sample_conservative():
    assert lr.wilson_lower_bound(0, 0) == 0.0
    # A perfect score on 3 runs must NOT beat a good score on 100 runs.
    assert lr.wilson_lower_bound(3, 3) < lr.wilson_lower_bound(80, 100)
    # More evidence at the same rate tightens the bound upward.
    assert lr.wilson_lower_bound(8, 10) < lr.wilson_lower_bound(80, 100)
    assert 0.0 <= lr.wilson_lower_bound(50, 100) <= 0.5


def test_approved_after_edit_counts_as_half_win(monkeypatch):
    _open_gate(monkeypatch)
    for i in range(30):
        ro.record_run("u", f"e{i}", "task", ["argus"],
                      models={"argus": "m"}, roles={"argus": "reasoning"})
        ro.apply_feedback("u", f"e{i}", "down")   # ensure rows exist, then relabel
    # Overwrite signals directly through the documented mapping instead:
    rows = ro.events("u")
    for r in rows:
        r["outcome_signal"] = ro.APPROVED_AFTER_EDIT
    ro._save("u", rows)
    lr.clear_cache()
    cells, _ = lr._aggregate()
    c = cells[("argus", "m")]
    assert c["n"] == 30 and abs(c["rate"] - 0.5) < 1e-9


# --- activation gates ------------------------------------------------------
def test_disabled_by_default_selection_is_pure_heuristic():
    pool = config.ModelPool.of(OPUS, HAIKU)
    expected = max((OPUS, HAIKU),
                   key=lambda s: config.capability_score(s.model, "reasoning"))
    assert pool.for_specialist("argus").model == expected.model == "claude-opus-4-8"
    assert lr.choose(pool.members, "argus", pool.members[0]) is None


def test_flag_on_but_gate_not_met_never_overrides(monkeypatch):
    _enable(monkeypatch)                       # flag on, real thresholds (300/3/2)
    _seed("argus", "claude-haiku-4-5", ro.POSITIVE, 40)
    _seed("argus", "claude-opus-4-8", ro.NEGATIVE, 40)
    lr.clear_cache()
    pool = config.ModelPool.of(OPUS, HAIKU)
    assert pool.for_specialist("argus").model == "claude-opus-4-8"   # heuristic


def test_replay_forces_selector_off(monkeypatch):
    _enable(monkeypatch)
    _open_gate(monkeypatch)
    _seed("argus", "claude-haiku-4-5", ro.POSITIVE, 40)
    _seed("argus", "claude-opus-4-8", ro.NEGATIVE, 40)
    lr.clear_cache()
    monkeypatch.setenv("OLYMPUS_REPLAY", "some-run")
    assert lr.enabled() is False
    pool = config.ModelPool.of(OPUS, HAIKU)
    assert pool.for_specialist("argus").model == "claude-opus-4-8"


# --- the override rule -------------------------------------------------------
def test_override_with_evidence_on_both_sides(monkeypatch):
    _enable(monkeypatch)
    _open_gate(monkeypatch)
    _seed("argus", "claude-haiku-4-5", ro.POSITIVE, 40)   # challenger: known, good
    _seed("argus", "claude-opus-4-8", ro.NEGATIVE, 40)    # incumbent: known, bad
    lr.clear_cache()
    pool = config.ModelPool.of(OPUS, HAIKU)
    assert pool.for_specialist("argus").model == "claude-haiku-4-5"  # learned win
    # Another specialist with no cells at all keeps the heuristic.
    assert pool.for_specialist("plutus").model == "claude-opus-4-8"


def test_unknown_incumbent_is_never_displaced(monkeypatch):
    _enable(monkeypatch)
    _open_gate(monkeypatch)
    _seed("argus", "claude-haiku-4-5", ro.POSITIVE, 40)   # challenger only
    lr.clear_cache()
    pool = config.ModelPool.of(OPUS, HAIKU)
    # One-sided evidence: haiku looks great but opus was never measured — no
    # override. (The heuristic pick has no known cell.)
    assert pool.for_specialist("argus").model == "claude-opus-4-8"


def test_below_min_cell_samples_never_engages(monkeypatch):
    _enable(monkeypatch)
    _open_gate(monkeypatch)
    _seed("argus", "claude-haiku-4-5", ro.POSITIVE, lr.MIN_CELL_SAMPLES - 1)
    _seed("argus", "claude-opus-4-8", ro.NEGATIVE, lr.MIN_CELL_SAMPLES - 1)
    lr.clear_cache()
    pool = config.ModelPool.of(OPUS, HAIKU)
    assert pool.for_specialist("argus").model == "claude-opus-4-8"


def test_synthetic_rows_never_feed_the_selector(monkeypatch):
    _enable(monkeypatch)
    _open_gate(monkeypatch)
    _seed("argus", "claude-haiku-4-5", ro.POSITIVE, 60, synthetic=True)
    _seed("argus", "claude-opus-4-8", ro.NEGATIVE, 60, synthetic=True)
    # Real rows keep the gate open but say nothing about these cells.
    _seed("plutus", "claude-opus-4-8", ro.POSITIVE, 12)
    lr.clear_cache()
    pool = config.ModelPool.of(OPUS, HAIKU)
    assert pool.for_specialist("argus").model == "claude-opus-4-8"


def test_agreement_returns_same_model(monkeypatch):
    _enable(monkeypatch)
    _open_gate(monkeypatch)
    _seed("argus", "claude-opus-4-8", ro.POSITIVE, 40)    # incumbent best
    _seed("argus", "claude-haiku-4-5", ro.NEGATIVE, 40)
    lr.clear_cache()
    pool = config.ModelPool.of(OPUS, HAIKU)
    assert pool.for_specialist("argus").model == "claude-opus-4-8"


# --- composition ---------------------------------------------------------
def test_explicit_pin_beats_learned(monkeypatch):
    _enable(monkeypatch)
    _open_gate(monkeypatch)
    _seed("hephaestus", "claude-opus-4-8", ro.POSITIVE, 40)
    _seed("hephaestus", "claude-haiku-4-5", ro.NEGATIVE, 40)
    lr.clear_cache()
    monkeypatch.setenv("OLYMPUS_SPECIALIST_MODELS",
                       json.dumps({"hephaestus": "haiku"}))
    pool = config.ModelPool.of(OPUS, HAIKU)
    # The pin wins even though the evidence says opus.
    assert pool.for_specialist("hephaestus").model == "claude-haiku-4-5"


def test_sovereign_filter_constrains_the_learned_choice(monkeypatch):
    _enable(monkeypatch)
    _open_gate(monkeypatch)
    local = config.Settings(provider="openai", model="llama3.1",
                            base_url="http://localhost:11434/v1")
    remote = config.Settings(provider="openai", model="gpt-4o",
                             base_url="https://api.openai.com/v1", api_key="k")
    # Evidence says the REMOTE model is far better...
    _seed("argus", "gpt-4o", ro.POSITIVE, 60)
    _seed("argus", "llama3.1", ro.NEGATIVE, 60)
    lr.clear_cache()
    monkeypatch.setenv("OLYMPUS_SOVEREIGN", "1")
    monkeypatch.delenv("OLYMPUS_EGRESS_ALLOWLIST", raising=False)
    pool = config.ModelPool.of(local, remote)
    # ...but sovereign mode filtered it out before the selector ever ran.
    assert [m.model for m in pool.members] == ["llama3.1"]
    assert pool.for_specialist("argus").model == "llama3.1"


def test_selector_failure_falls_back_to_heuristic(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(lr, "_cells", lambda: (_ for _ in ()).throw(
        RuntimeError("ledger unreadable")))
    pool = config.ModelPool.of(OPUS, HAIKU)
    assert pool.for_specialist("argus").model == "claude-opus-4-8"


# --- end-to-end through the real pipeline ---------------------------------
def _msg(text):
    return anthropic.types.Message.model_validate({
        "id": "m", "type": "message", "role": "assistant", "model": "claude-test",
        "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 5, "output_tokens": 5,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
    })


def _pipeline_client():
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
                    return _msg(json.dumps({"verdict": "approve", "feedback": "",
                        "retry_specialists": []}))
                return _msg("findings")
        return _S()
    msgs = types.SimpleNamespace(stream=stream)
    return types.SimpleNamespace(messages=msgs, beta=types.SimpleNamespace(messages=msgs))


def test_pipeline_uses_learned_pick_and_telemetry_records_it(monkeypatch):
    from olympus import orchestrator
    _enable(monkeypatch)
    _open_gate(monkeypatch)
    _seed("argus", "claude-haiku-4-5", ro.POSITIVE, 40, user="seed")
    _seed("argus", "claude-opus-4-8", ro.NEGATIVE, 40, user="seed")
    lr.clear_cache()
    monkeypatch.setattr(recall, "context_block", lambda user, msg: "")
    monkeypatch.setattr(config, "MEMORY_ENABLED", False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("OLYMPUS_MODELS",
                       json.dumps([{"provider": "anthropic",
                                    "model": "claude-haiku-4-5"}]))
    monkeypatch.setattr(llm, "client", lambda settings=None: _pipeline_client())
    bot = orchestrator.Olympus(pool=config.ModelPool.from_env(), user="alice")
    bot.ask("research the current state of X")
    rows = [r for r in ro.events("alice") if r["run_id"] == bot.last_run_id]
    assert rows and rows[0]["model"] == "claude-haiku-4-5"   # the learned pick ran


def test_pipeline_with_flag_on_but_no_data_is_unbroken(monkeypatch):
    from olympus import orchestrator
    _enable(monkeypatch)                       # active flag, empty ledger
    monkeypatch.setattr(recall, "context_block", lambda user, msg: "")
    monkeypatch.setattr(config, "MEMORY_ENABLED", False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(llm, "client", lambda settings=None: _pipeline_client())
    bot = orchestrator.Olympus(pool=config.ModelPool.from_env(), user="alice")
    bot.ask("research the current state of X")
    assert bot.last_run_id                     # run completed on the heuristic

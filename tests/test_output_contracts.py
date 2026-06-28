"""Hard output contracts (the pre-emission clamp at orchestrator._run_one).

Three layers are proven here:

* Pure unit — `contracts.check` in isolation (no orchestrator, no I/O): every
  check, its boundaries, and that None/no-op/absent-count cost nothing.
* Integration — with OLYMPUS_CONTRACTS unset a violating output is returned
  UNCHANGED (the distribution-safety guarantee); with it set, a violation
  degrades to the typed "treat this part as missing" placeholder, the rest of
  the pipeline still completes, and a signed `contract` decision is recorded
  (status="violation" on fail, "ok" on pass).
* Replay — adding `contract` records keeps the decision path byte-identical
  under replay (diff_decisions == []), proving the new record type didn't break
  the re-executable log.
"""

import dataclasses
import json

from olympus import config, contracts, orchestrator, trace
from olympus.contracts import OutputContract, check
from olympus.specialists import SPECIALISTS


# ======================================================================
# Pure unit tests — contracts.check (Part 9, 1–6)
# ======================================================================

def test_none_and_noop_contracts_pass():
    assert check("anything at all", None).ok is True
    assert check("anything at all", None).violations == ()
    noop = OutputContract()
    assert noop.is_noop() is True
    res = check("anything at all", noop)
    assert res.ok is True and res.violations == ()


def test_max_chars_under_at_over():
    c = OutputContract(max_chars=5)
    assert check("abcd", c).ok is True            # under
    assert check("abcde", c).ok is True           # exactly at the limit
    over = check("abcdef", c)                      # one over
    assert over.ok is False
    assert over.violations == ("output is 6 chars, limit is 5",)


def test_must_be_json_valid_vs_prose():
    c = OutputContract(must_be_json=True)
    assert check('{"a": 1}', c).ok is True
    assert check("just some prose", c).ok is False
    assert check("just some prose", c).violations == ("output is not valid JSON",)


def test_json_schema_missing_required_key_fails():
    c = OutputContract(json_schema={"type": "object", "required": ["name"]})
    res = check('{"other": 1}', c)
    assert res.ok is False
    assert "missing required key: 'name'" in res.violations


def test_json_schema_wrong_top_level_type_fails():
    c = OutputContract(json_schema={
        "type": "object",
        "properties": {"count": {"type": "integer"}}})
    res = check('{"count": "not a number"}', c)
    assert res.ok is False
    assert any("count" in v and "integer" in v for v in res.violations)


def test_json_schema_correct_passes_and_nested_irrelevant_key_ignored():
    c = OutputContract(json_schema={
        "type": "object",
        "required": ["name"],
        "properties": {"name": {"type": "string"}}})
    # Correct + an extra/nested key the shallow check never looks inside.
    payload = json.dumps({"name": "ok", "nested": {"deep": [1, 2, 3]}})
    res = check(payload, c)
    assert res.ok is True and res.violations == ()


def test_must_be_json_object_type_mismatch():
    # must_be_json passes for any valid JSON; the schema's type=object is what
    # rejects a JSON array — shallow, but it catches "promised an object".
    c = OutputContract(json_schema={"type": "object", "required": ["k"]})
    res = check("[1, 2, 3]", c)
    assert res.ok is False
    assert any("expected a JSON object" in v for v in res.violations)


def test_max_tool_calls_under_at_over_and_none_does_not_fire():
    c = OutputContract(max_tool_calls=3)
    assert check("x", c, tool_calls=2).ok is True       # under
    assert check("x", c, tool_calls=3).ok is True       # at
    over = check("x", c, tool_calls=4)                   # over
    assert over.ok is False
    assert over.violations == ("made 4 tool calls, limit is 3",)
    # No count available (non-Anthropic provider) → the cap simply doesn't fire.
    assert check("x", c, tool_calls=None).ok is True


def test_multiple_simultaneous_violations_all_reported():
    c = OutputContract(max_chars=2, must_be_json=True, max_tool_calls=1)
    res = check("not json and too long", c, tool_calls=9)
    assert res.ok is False
    assert len(res.violations) == 3
    joined = " | ".join(res.violations)
    assert "chars" in joined and "not valid JSON" in joined and "tool calls" in joined


# ======================================================================
# Integration tests — enforcement at _run_one (Part 9, 7–9)
# ======================================================================

def _stub_run_counted(monkeypatch, output_for=lambda key: f"output[{key}]",
                      tool_calls=0):
    """Make every specialist return a deterministic output + tool-call count,
    with zero model calls — _run_one funnels through run_counted."""
    def fake(self, task, settings=None, effort="high"):
        return output_for(self.key), tool_calls
    monkeypatch.setattr(SPECIALISTS["plutus"].__class__, "run_counted", fake)


def _with_contract(monkeypatch, key, contract):
    """Attach a contract to one specialist (frozen → replace the registry
    entry). The dict is shared with orchestrator, so one setitem covers both."""
    new = dataclasses.replace(SPECIALISTS[key], contract=contract)
    monkeypatch.setitem(SPECIALISTS, key, new)


def test_disabled_by_default_returns_violating_output_unchanged(monkeypatch):
    """OLYMPUS_CONTRACTS unset → a violating output is returned VERBATIM and no
    contract decision is recorded. This is the distribution-safety guarantee."""
    monkeypatch.delenv("OLYMPUS_CONTRACTS", raising=False)
    assert config.contracts_enabled() is False
    _stub_run_counted(monkeypatch)
    _with_contract(monkeypatch, "plutus", OutputContract(max_chars=3))

    bot = orchestrator.Olympus(user="contracts")
    tr = trace.Trace("t")
    steps = [{"id": "a", "specialist": "plutus", "task": "go", "depends_on": []}]
    out = dict(bot._dispatch_dag(steps, tr))

    assert out["plutus"] == "output[plutus]"          # UNCHANGED, not clamped
    assert [d for d in tr.decisions if d["decision_type"] == "contract"] == []


def test_enabled_violation_degrades_and_pipeline_completes(monkeypatch):
    """OLYMPUS_CONTRACTS=1: the violating specialist degrades to the typed
    placeholder while the other specialist's real output survives — the rest of
    the pipeline tolerates a 'this part is missing' output unchanged."""
    monkeypatch.setenv("OLYMPUS_CONTRACTS", "1")
    monkeypatch.setenv("OLYMPUS_FAST", "1")           # skip the review LLM call
    _stub_run_counted(monkeypatch)
    _with_contract(monkeypatch, "plutus", OutputContract(max_chars=3))  # violates
    # peitho has no contract → passes through.

    bot = orchestrator.Olympus(user="contracts")
    monkeypatch.setattr(bot, "_route", lambda msg: {
        "mode": "delegate", "direct_reply": None,
        "specialists": ["plutus", "peitho"], "brief": "do it",
        "needs_verification": True})
    monkeypatch.setattr(bot, "_plan", lambda brief, keys: [
        {"id": "a", "specialist": "plutus", "task": "t1", "depends_on": []},
        {"id": "b", "specialist": "peitho", "task": "t2", "depends_on": []}])
    # Verify just echoes the bundle so we can inspect what survived.
    monkeypatch.setattr(bot, "_verify", lambda brief, outputs:
                        "\n\n".join(f"### {k}\n{v}" for k, v in outputs))

    tr = trace.Trace("t")
    mode, brief, verified = bot._pipeline("a real message", tr)

    assert mode == "delegate"
    assert "output[peitho]" in verified                       # other specialist OK
    assert "rejected by its output contract" in verified      # violator degraded
    assert "Treat this part as missing" in verified


def test_contract_decision_recorded_violation_and_ok(monkeypatch):
    """A `contract` decision is appended for every checked specialist: status
    'violation' on failure, 'ok' on pass."""
    monkeypatch.setenv("OLYMPUS_CONTRACTS", "1")
    _stub_run_counted(monkeypatch)
    _with_contract(monkeypatch, "plutus", OutputContract(max_chars=3))  # fails
    # peitho: no contract → passes (still recorded).

    bot = orchestrator.Olympus(user="contracts")
    tr = trace.Trace("t")
    steps = [
        {"id": "a", "specialist": "plutus", "task": "t1", "depends_on": []},
        {"id": "b", "specialist": "peitho", "task": "t2", "depends_on": ["a"]},
    ]
    bot._dispatch_dag(steps, tr)

    contract_decs = [d for d in tr.decisions if d["decision_type"] == "contract"]
    by_key = {d["agent"]["key"]: d for d in contract_decs}
    assert set(by_key) == {"plutus", "peitho"}
    assert by_key["plutus"]["outcome"]["status"] == "violation"
    assert by_key["plutus"]["rationale"]["violations"]                  # non-empty
    assert by_key["peitho"]["outcome"]["status"] == "ok"
    assert by_key["peitho"]["rationale"]["violations"] == []


def test_tool_call_count_threads_into_the_cap(monkeypatch):
    """The client-side tool-call count returned by run_counted reaches
    contracts.check, so the cap actually fires end to end."""
    monkeypatch.setenv("OLYMPUS_CONTRACTS", "1")
    _stub_run_counted(monkeypatch, output_for=lambda key: "short", tool_calls=5)
    _with_contract(monkeypatch, "plutus", OutputContract(max_tool_calls=2))

    bot = orchestrator.Olympus(user="contracts")
    tr = trace.Trace("t")
    steps = [{"id": "a", "specialist": "plutus", "task": "go", "depends_on": []}]
    out = dict(bot._dispatch_dag(steps, tr))

    assert "rejected by its output contract" in out["plutus"]
    dec = [d for d in tr.decisions if d["decision_type"] == "contract"][0]
    assert dec["outcome"]["status"] == "violation"
    assert any("tool calls" in v for v in dec["rationale"]["violations"])


def test_pipeline_records_contracts_flag_in_meta(monkeypatch):
    """The enforcement mode is stamped into tr.meta so a replay can reproduce
    it (Part 8.3). meta is NOT part of the diffed decision path."""
    monkeypatch.setenv("OLYMPUS_CONTRACTS", "1")
    monkeypatch.setenv("OLYMPUS_FAST", "1")
    bot = orchestrator.Olympus(user="contracts")
    monkeypatch.setattr(bot, "_route", lambda msg: {
        "mode": "direct", "direct_reply": "hi"})       # shortest pipeline path
    tr = trace.Trace("t")
    bot._pipeline("hello", tr)
    assert tr.meta["contracts_enabled"] is True


# ======================================================================
# Replay test — adding contract records keeps the path byte-identical (10)
# ======================================================================

def test_contract_records_replay_byte_identical(monkeypatch):
    """Record a contracts-on dispatch and 'replay' the identical deterministic
    dispatch; the decision paths (including the new `contract` records) must be
    byte-identical. A dependent (sequential) DAG is used so the two contract
    records have a deterministic order — see the report's note on parallel
    levels."""
    monkeypatch.setenv("OLYMPUS_CONTRACTS", "1")
    _stub_run_counted(monkeypatch)
    _with_contract(monkeypatch, "plutus", OutputContract(max_chars=3))  # violates

    steps = [
        {"id": "a", "specialist": "plutus", "task": "t1", "depends_on": []},
        {"id": "b", "specialist": "peitho", "task": "t2", "depends_on": ["a"]},
    ]

    bot = orchestrator.Olympus(user="contracts")
    rec = trace.Trace("t")
    bot._dispatch_dag(steps, rec)
    fresh = trace.Trace("t")
    bot._dispatch_dag(steps, fresh)

    # Two contract records each, byte-identical cores → empty diff.
    assert len([d for d in rec.decisions
                if d["decision_type"] == "contract"]) == 2
    assert trace.diff_decisions(rec.decisions, fresh.decisions) == []
    # And the record core genuinely drops volatile fields (Part 8.2).
    core = trace.decision_core(
        [d for d in rec.decisions if d["decision_type"] == "contract"][0])
    assert "record_id" not in core and "ts" not in core
    assert core["decision_type"] == "contract"

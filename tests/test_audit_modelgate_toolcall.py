"""INDEPENDENT AUDIT probes — C6 drift tripwire, C8 tool-call recovery, the
cost-budget guard (I-M1), C7 read-only guarantee (I-P1), and Wave-1 config
documentation.

Adversarial, written from scratch by the auditor. These do NOT reuse the
authors' fixtures except the shared conftest isolation. Where a real defect is
found it is encoded as an explicit assertion (budget overrun) or an xfail
(config gap) so the evidence lives in the suite, not just prose.

Source is never modified; defects are documented.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from olympus import (config, doctor, evals, modelgate,
                     openai_compat, toolcall_repair as tr, tools)


# ===========================================================================
# helpers
# ===========================================================================

def _settings() -> config.Settings:
    return config.Settings.from_env()


def _member() -> str:
    s = _settings()
    return f"{s.provider}/{s.model}"


def _uniform_run_fn(domain_scores=None, *, cost=0.0, default=8.0,
                    all_error=False, error_domains=()):
    """Deterministic scorer with fixed per-domain scores/costs."""
    domain_scores = domain_scores or {}
    error_domains = set(error_domains)

    def run_fn(settings, item):
        dom = item.get("domain") or item.get("specialist")
        if all_error or dom in error_domains:
            return {"score": 0.0, "cost": cost, "error": True}
        return {"score": domain_scores.get(dom, default), "cost": cost,
                "error": False}
    return run_fn


def _cost_sequence_run_fn(costs, default=8.0):
    """Scorer that returns a PRESCRIBED cost sequence per successive item —
    the adversarial mid-run cost spike lever."""
    seq = list(costs)
    idx = {"i": 0}

    def run_fn(settings, item):
        i = idx["i"]
        idx["i"] = i + 1
        cost = seq[i] if i < len(seq) else 0.0
        return {"score": default, "cost": cost, "error": False}
    return run_fn


def _flaky_domain_run_fn(domain, first_score, retry_score, default=8.0):
    """A domain that scores `first_score` on its first observation and
    `retry_score` on the confirmation re-run — the reproduce-before-believe
    lever."""
    seen: dict[str, int] = {}

    def run_fn(settings, item):
        dom = item.get("domain") or item.get("specialist")
        if dom == domain:
            n = seen.get(dom, 0)
            seen[dom] = n + 1
            return {"score": first_score if n == 0 else retry_score,
                    "cost": 0.0, "error": False}
        return {"score": default, "cost": 0.0, "error": False}
    return run_fn


def _seed_baseline(default=8.0):
    s = _settings()
    res = modelgate.run_gate(s, run_fn=_uniform_run_fn(default=default))
    assert res.severity == modelgate.NO_BASELINE
    return s


# ===========================================================================
# AREA 1 — provider-drift classification: EVERY severity boundary
# classify() is pure (no I/O), so boundary sweeps run directly against it.
# ===========================================================================

def test_no_baseline_first_run_seeds_and_never_gates():
    assert modelgate.classify({"scores": {"code": 8.0}, "error_rate": 0.0},
                              {}) == modelgate.NO_BASELINE
    s = _settings()
    res = modelgate.run_gate(s, run_fn=_uniform_run_fn(default=8.0))
    assert res.severity == modelgate.NO_BASELINE
    assert res.ok
    assert modelgate.load_baseline(modelgate.result_key(s))  # baseline written
    assert not modelgate.frozen_members()                    # nothing gated


def test_drop_exactly_at_tolerance_is_info_not_flagged():
    """Boundary: DRIFT_TOLERANCE is 1.0. A drop of EXACTLY 1.0 (drop > tol is
    False) must classify INFO — the tolerance edge is inclusive of 'ok'."""
    assert modelgate.DRIFT_TOLERANCE == evals.DEFAULT_TOLERANCE == 1.0
    base = {"code": 8.0}
    at_edge = {"scores": {"code": 7.0}, "error_rate": 0.0}   # drop == 1.0
    assert modelgate.classify(at_edge, base) == modelgate.INFO
    just_over = {"scores": {"code": 6.9}, "error_rate": 0.0}  # drop == 1.1
    assert modelgate.classify(just_over, base) == modelgate.WARN_PENDING


def test_single_domain_drop_is_warn_pending():
    base = {"code": 8.0, "routing": 8.0}
    result = {"scores": {"code": 3.0, "routing": 8.0}, "error_rate": 0.0}
    assert modelgate.classify(result, base) == modelgate.WARN_PENDING


def test_two_domain_drop_hits_freeze_threshold():
    base = {"code": 8.0, "routing": 8.0, "plan": 8.0}
    result = {"scores": {"code": 3.0, "routing": 3.0, "plan": 8.0},
              "error_rate": 0.0}
    assert modelgate.classify(result, base) == modelgate.FREEZE


def test_verify_domain_drop_is_block_even_single_domain():
    base = {"verify": 8.0, "code": 8.0}
    result = {"scores": {"verify": 5.0, "code": 8.0}, "error_rate": 0.0}
    assert modelgate.classify(result, base) == modelgate.BLOCK


def test_refusal_domain_drop_is_block_even_single_domain():
    base = {"refusal": 8.0}
    result = {"scores": {"refusal": 5.0}, "error_rate": 0.0}
    assert modelgate.classify(result, base) == modelgate.BLOCK


def test_block_outranks_freeze_when_verify_among_multidrop():
    """Ladder ordering: a verify/refusal drop is BLOCK even when >=2 domains
    also regress (block must dominate freeze)."""
    base = {"verify": 8.0, "code": 8.0, "plan": 8.0}
    result = {"scores": {"verify": 4.0, "code": 4.0, "plan": 4.0},
              "error_rate": 0.0}
    assert modelgate.classify(result, base) == modelgate.BLOCK


def test_quarantine_outranks_everything_including_block():
    """error_rate >= 0.25 short-circuits to QUARANTINE before any regression
    math — even a verify drop underneath."""
    base = {"verify": 8.0}
    result = {"scores": {"verify": 4.0}, "error_rate": 0.25}
    assert modelgate.classify(result, base) == modelgate.QUARANTINE
    # just below threshold falls through to the regression ladder (block here)
    result_below = {"scores": {"verify": 4.0}, "error_rate": 0.2499}
    assert modelgate.classify(result_below, base) == modelgate.BLOCK


def test_mixed_improvements_and_regressions():
    """Improvements never offset regressions: one domain up + one down is still
    a single-domain regression (warn_pending); two down -> freeze regardless of
    unrelated gains."""
    base = {"a": 8.0, "b": 8.0, "c": 8.0}
    one_down = {"scores": {"a": 10.0, "b": 8.0, "c": 4.0}, "error_rate": 0.0}
    assert modelgate.classify(one_down, base) == modelgate.WARN_PENDING
    two_down = {"scores": {"a": 10.0, "b": 4.0, "c": 4.0}, "error_rate": 0.0}
    assert modelgate.classify(two_down, base) == modelgate.FREEZE


def test_missing_domain_is_not_counted_as_regression_by_classify():
    """AUDIT NOTE: classify() reads only reg['regressions'], not reg['missing'].
    A baseline domain that vanishes from the fresh scores (e.g. all its items
    errored but total error_rate stayed < 25%) is 'missing', which
    regression_check flags as a gate failure — yet classify() returns INFO.
    Documenting the exact behaviour (a latent blind spot, not a crash)."""
    base = {"code": 8.0, "verify": 8.0}
    # 'verify' absent from fresh scores entirely.
    result = {"scores": {"code": 8.0}, "error_rate": 0.0}
    reg = evals.regression_check(result["scores"], base, modelgate.DRIFT_TOLERANCE)
    assert reg["missing"] == ["verify"] and reg["ok"] is False
    # ...but classify does NOT escalate on 'missing':
    assert modelgate.classify(result, base) == modelgate.INFO


# --- reproduce-before-believe (the critical no-false-harden claim) ----------

def test_unconfirmed_single_regression_must_not_harden_to_warn():
    """A single-domain regression with confirm=False stays WARN_PENDING and
    NEVER hardens to WARN or freezes. This is the reproduce-before-believe
    guarantee at its most load-bearing."""
    s = _seed_baseline(8.0)
    res = modelgate.run_gate(s, run_fn=_uniform_run_fn({"code": 3}, default=8),
                             confirm=False)
    assert res.severity == modelgate.WARN_PENDING
    assert res.ok
    assert not modelgate.frozen_members()


def test_confirmed_regression_hardens_to_warn_but_never_freezes():
    s = _seed_baseline(8.0)
    res = modelgate.run_gate(s, run_fn=_uniform_run_fn({"code": 3}, default=8),
                             confirm=True)
    assert res.severity == modelgate.WARN
    assert res.ok                                # WARN is non-gating
    assert not modelgate.frozen_members()


def test_regression_that_does_not_reproduce_downgrades_to_info():
    """The confirmation pass re-runs ONLY the flagged domain. If the drop was
    judge noise (retry recovers), it downgrades to INFO — proving confirm can
    clear as well as harden."""
    s = _seed_baseline(8.0)
    run_fn = _flaky_domain_run_fn("code", first_score=3, retry_score=8,
                                  default=8)
    res = modelgate.run_gate(s, run_fn=run_fn, confirm=True)
    assert res.severity == modelgate.INFO
    assert res.ok
    assert not modelgate.frozen_members()


def test_multidomain_freeze_writes_reversible_marker_and_doctor_warns():
    s = _seed_baseline(8.0)
    res = modelgate.run_gate(
        s, run_fn=_uniform_run_fn({"code": 3, "multilingual": 2}, default=8))
    assert res.severity == modelgate.FREEZE and not res.ok
    assert modelgate.frozen_members()[_member()]["severity"] == modelgate.FREEZE
    assert any(c.name == "provider drift" and c.status == doctor.WARN
               for c in doctor._drift_checks())
    # I-M3: deleting the marker (operator action) unfreezes.
    assert modelgate.unfreeze(_member()) is True
    assert not modelgate.frozen_members()


def test_verify_block_fails_doctor():
    s = _seed_baseline(8.0)
    res = modelgate.run_gate(s, run_fn=_uniform_run_fn({"verify": 2}, default=8))
    assert res.severity == modelgate.BLOCK and not res.ok
    assert any(c.name == "provider drift" and c.status == doctor.FAIL
               for c in doctor._drift_checks())


# ===========================================================================
# AREA 2 — COST-BUDGET ENFORCEMENT (I-M1): denial-of-wallet guard
# ===========================================================================

def test_budget_uniform_cost_stops_before_overrun():
    """Sanity: with UNIFORM per-item cost and a matching pre-flight estimate the
    cap genuinely holds — stops before the item that would cross it."""
    s = _settings()
    res = modelgate.run_gate(s, budget_usd=1.0,
                             run_fn=_uniform_run_fn(default=8, cost=0.4),
                             cost_estimator=lambda _s, _i: 0.4)
    assert res.severity == modelgate.BUDGET_EXHAUSTED
    assert len(res.results) == 2            # 3rd estimated 1.2 > 1.0 -> stop
    assert res.cost == pytest.approx(0.8)
    assert res.cost <= 1.0                   # holds under uniform cost


def test_budget_MIDRUN_SPIKE_DOES_NOT_OVERRUN_cap():
    """FIXED (Wave-1 audit blocker I-M1): the guard is now a PRE-FLIGHT
    worst-case estimate, so a mid-run cost spike is stopped BEFORE it runs.

    Costs [0.1, 0.1, 5.0] under a $1.00 cap with a foresight estimator:
      item1: est 0.1, 0+0.1=0.1 <= 1.0 -> runs; spend 0.1
      item2: est 0.1, 0.1+0.1=0.2 <= 1.0 -> runs; spend 0.2
      item3: est 5.0, 0.2+5.0=5.2 >  1.0 -> STOP (before the spike runs)

    Result: 2 items ran, total $0.20, cap never exceeded. Contrast the old
    trailing-max projection, which ran the spike and spent $5.20."""
    s = _settings()
    seq = [0.1, 0.1, 5.0]
    est = iter(seq)                                   # exact foresight
    res = modelgate.run_gate(s, budget_usd=1.0,
                             run_fn=_cost_sequence_run_fn(seq),
                             cost_estimator=lambda _s, _i: next(est, 0.0))
    assert res.severity == modelgate.BUDGET_EXHAUSTED
    assert len(res.results) == 2                     # spike item did NOT run
    assert res.cost == pytest.approx(0.2)
    assert res.cost <= 1.0                            # <-- cap HELD
    assert not modelgate.frozen_members()


def test_budget_first_item_over_cap_does_not_run():
    """FIXED: with the pre-flight estimate there is no 'first item always runs'
    exception — a single first item whose worst case exceeds the whole cap is
    stopped before running. $5 estimate under a $1 cap -> $0 spent, 0 items."""
    s = _settings()
    res = modelgate.run_gate(s, budget_usd=1.0,
                             run_fn=_cost_sequence_run_fn([5.0, 5.0]),
                             cost_estimator=lambda _s, _i: 5.0)
    assert res.cost == pytest.approx(0.0)
    assert res.cost <= 1.0
    assert len(res.results) == 0                     # nothing ran


def test_budget_zero_cap_means_unbounded_not_zero():
    """Documenting semantics: cap<=0 disables the guard (runs full corpus),
    it does NOT mean 'spend nothing'."""
    s = _settings()
    res = modelgate.run_gate(s, budget_usd=0.0,
                             run_fn=_uniform_run_fn(default=8, cost=0.01))
    assert res.severity != modelgate.BUDGET_EXHAUSTED
    assert len(res.results) == len(modelgate.load_drift_corpus())


# ===========================================================================
# AREA 3 — TOOL-CALL RECOVERY SAFETY (I-T1) through openai_compat.run_agent
# ===========================================================================

_INT_TOOL = [{"name": "demo_tool", "description": "d",
              "input_schema": {"type": "object",
                               "properties": {"count": {"type": "integer"}},
                               "required": ["count"]}}]


def _fake_post(responses):
    idx = {"i": 0}

    def fake(settings, payload):
        r = responses[idx["i"]]
        idx["i"] += 1
        return r
    return fake


def _native_call(args_json: str, tool="demo_tool") -> dict:
    return {"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": tool, "arguments": args_json}}]}}]}


def _final(text="done") -> dict:
    return {"choices": [{"message": {"content": text, "tool_calls": []}}]}


def test_bool_for_int_param_is_rejected_not_laundered(monkeypatch):
    """Adversarial coercion: a JSON boolean for an integer param must NOT be
    laundered into 0/1 (bool is excluded from the integer check AND from
    coercion). The handler must never run."""
    invoked: list[dict] = []
    monkeypatch.setattr(openai_compat, "_post", _fake_post([
        _native_call('{"count": true}'), _final()]))
    monkeypatch.setattr(tools, "resolve_handler",
                        lambda n: (lambda **kw: invoked.append(kw) or "ran")
                        if n == "demo_tool" else None)
    s = config.Settings(provider="openai_compat", model="weak")
    assert openai_compat.run_agent(s, "sys", "go", _INT_TOOL) == "done"
    assert invoked == []                    # never executed with a laundered bool
    # pure-function corroboration: coercion leaves the bool untouched
    assert tr.coerce_arguments({"count": True}, _INT_TOOL[0]["input_schema"]) \
        == {"count": True}
    errs, _ = tr.validate_arguments({"count": True},
                                    _INT_TOOL[0]["input_schema"])
    assert errs                             # still invalid after coercion


def test_nonnumeric_string_for_int_not_coerced_and_rejected(monkeypatch):
    """A garbage string ('5abc') for an integer param does not full-match the
    int regex, so coercion is declined and the call is rejected — coercion
    never fabricates a semantically-wrong-but-valid value."""
    invoked: list[dict] = []
    monkeypatch.setattr(openai_compat, "_post", _fake_post([
        _native_call('{"count": "5abc"}'), _final()]))
    monkeypatch.setattr(tools, "resolve_handler",
                        lambda n: (lambda **kw: invoked.append(kw) or "ran"))
    s = config.Settings(provider="openai_compat", model="weak")
    assert openai_compat.run_agent(s, "sys", "go", _INT_TOOL) == "done"
    assert invoked == []
    assert tr.coerce_arguments({"count": "5abc"},
                               _INT_TOOL[0]["input_schema"]) == {"count": "5abc"}


def test_valid_coercion_executes_with_typed_value(monkeypatch):
    """Positive control: '5' for an integer param IS coerced and the handler
    runs with int 5 (rung 4), never the raw string."""
    invoked: list[dict] = []
    monkeypatch.setattr(openai_compat, "_post", _fake_post([
        _native_call('{"count": "5"}'), _final()]))
    monkeypatch.setattr(tools, "resolve_handler",
                        lambda n: (lambda **kw: invoked.append(kw) or "ok"))
    s = config.Settings(provider="openai_compat", model="weak")
    assert openai_compat.run_agent(s, "sys", "go", _INT_TOOL) == "done"
    assert invoked == [{"count": 5}]        # int, not "5"


def test_unknown_tool_name_always_refused_recovery_and_execution(monkeypatch):
    """I-T2: an unknown tool name is never recovered from content, and a native
    call to an unknown handler returns an error rather than executing."""
    # (a) recovery is refusal-safe: content naming an un-offered tool -> None
    assert tr.recover_tool_call('{"name": "evil", "arguments": {}}',
                                {"demo_tool"}) is None
    assert tr.recover_tool_call('{"evil": {"x": 1}}', {"demo_tool"}) is None
    # (b) a native call to an unknown handler bounces as an error, never runs
    monkeypatch.setattr(openai_compat, "_post", _fake_post([
        _native_call('{"count": 5}', tool="ghost"), _final()]))
    monkeypatch.setattr(tools, "resolve_handler",
                        lambda n: None)     # nothing resolves
    s = config.Settings(provider="openai_compat", model="weak")
    out = openai_compat.run_agent(s, "sys", "go", _INT_TOOL)
    assert out == "done"


def test_recovery_refuses_plain_answer_and_refusal_text():
    """A refusal or a plain final answer names no offered tool -> stays text."""
    assert tr.recover_tool_call("I can't help with that.", {"demo_tool"}) is None
    assert tr.recover_tool_call('{"answer": "the capital is Paris"}',
                                {"demo_tool"}) is None
    assert tr.recover_tool_call("just some prose", {"demo_tool"}) is None
    assert tr.recover_tool_call('{"name": "demo_tool"}', None) is None  # no names


def test_close_truncated_refuses_ambiguous_tails():
    """A truncated tail is closed ONLY when unambiguous. Partial keys and
    dangling separators must refuse (return None) rather than invent structure
    that would parse to valid-but-wrong JSON."""
    assert tr.close_truncated('{"na') is None            # partial KEY
    assert tr.close_truncated('{"name":') is None         # dangling ':'
    assert tr.close_truncated('{"name": "x",') is None    # dangling ','
    # deep nesting beyond the closer cap refuses
    assert tr.close_truncated("{" + '"a":{' * 9) is None
    # unambiguous truncated VALUE closes and parses to an object
    closed = tr.close_truncated('{"name": "web_search", "arguments": {"query": "ab')
    assert closed is not None and isinstance(json.loads(closed), dict)


def test_salvage_refuses_multiparam_and_nonstring_required():
    """Salvage maps a lone payload ONLY onto a single string/untyped required
    param. Multi-param schemas and typed-non-string required params refuse, so a
    payload can never be mapped onto the wrong parameter."""
    two_req = {"type": "object",
               "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
               "required": ["a", "b"]}
    assert tr.salvage_arguments("hello", two_req) is None
    int_req = {"type": "object", "properties": {"n": {"type": "integer"}},
               "required": ["n"]}
    assert tr.salvage_arguments("hello", int_req) is None
    ok_req = {"type": "object", "properties": {"q": {"type": "string"}},
              "required": ["q"]}
    assert tr.salvage_arguments("hello", ok_req) == {"q": "hello"}


def test_invalid_repaired_call_never_reaches_handler_and_feedback(monkeypatch):
    """I-T1 end-to-end: a missing-required call is rejected, handler never runs,
    and a typed error is fed back to the model as a tool-role message."""
    invoked: list[dict] = []
    seen: list[dict] = []

    responses = [_native_call('{"wrong": "x"}'), _final()]
    idx = {"i": 0}

    def fake(settings, payload):
        seen.append(payload)
        r = responses[idx["i"]]
        idx["i"] += 1
        return r
    monkeypatch.setattr(openai_compat, "_post", fake)
    monkeypatch.setattr(tools, "resolve_handler",
                        lambda n: (lambda **kw: invoked.append(kw) or "ran"))
    s = config.Settings(provider="openai_compat", model="weak")
    assert openai_compat.run_agent(s, "sys", "go", _INT_TOOL) == "done"
    assert invoked == []
    tool_msgs = [m for m in seen[1]["messages"] if m.get("role") == "tool"]
    assert tool_msgs and "Error: invalid arguments" in tool_msgs[0]["content"]
    assert "missing required key: count" in tool_msgs[0]["content"]


def test_validate_off_is_the_less_safe_escape_hatch(monkeypatch):
    """OLYMPUS_TOOL_VALIDATE=off is the intended escape hatch and is
    DEMONSTRABLY less safe: an unvalidated, un-coerced wrong-typed value reaches
    the handler verbatim (string '5' where the schema declares integer)."""
    monkeypatch.setenv("OLYMPUS_TOOL_VALIDATE", "off")
    invoked: list[dict] = []
    monkeypatch.setattr(openai_compat, "_post", _fake_post([
        _native_call('{"count": "5"}'), _final()]))
    monkeypatch.setattr(tools, "resolve_handler",
                        lambda n: (lambda **kw: invoked.append(kw) or "ok"))
    s = config.Settings(provider="openai_compat", model="weak")
    assert openai_compat.run_agent(s, "sys", "go", _INT_TOOL) == "done"
    assert invoked == [{"count": "5"}]      # raw string reaches handler — unsafe


# ===========================================================================
# AREA 4 — repair telemetry: decay warning, corruption, never-raises
# ===========================================================================

def _write_repair_stats(rung_counts: dict[int, int]):
    import time
    path = config.MEMORY_DIR / "repair_stats.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    day = time.strftime("%Y-%m-%d")
    cells = {f"{r}:x": n for r, n in rung_counts.items()}
    path.write_text(json.dumps({"v": 1, "days": {day: {"p/m/t": cells}}}))


def test_doctor_warns_when_repair_rate_rises():
    _write_repair_stats({1: 70, 4: 30})         # 30% rung>=3 over 100 calls
    checks = doctor._repair_checks()
    assert checks and checks[0].status == doctor.WARN
    assert "30.0%" in checks[0].detail


def test_doctor_ok_below_threshold_and_below_volume():
    _write_repair_stats({1: 95, 4: 5})          # 5% — healthy
    assert doctor._repair_checks()[0].status == doctor.OK
    _write_repair_stats({1: 10, 4: 30})         # 75% but only 40 calls (<50)
    assert doctor._repair_checks()[0].status == doctor.OK


def test_corrupt_stats_sanitized_and_counting_continues():
    path = config.MEMORY_DIR / "repair_stats.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json at all")
    tr.record_repair("p", "m", "t", 1, "ok")    # must not raise (I-T3)
    aside = list(config.MEMORY_DIR.glob("repair_stats.corrupt.*.json"))
    assert aside and aside[0].read_text() == "{ not json at all"
    assert tr.repair_rate(7)["total"] == 1      # fresh counting resumed


def test_record_repair_never_raises_even_with_broken_lock(monkeypatch):
    from olympus import proclock

    def boom(*a, **k):
        raise RuntimeError("lock exploded")
    monkeypatch.setattr(proclock, "lock", boom)
    tr.record_repair("p", "m", "t", 2, "rejected")   # swallowed (I-T3)


def test_repair_rate_math_and_window():
    import time
    for _ in range(6):
        tr.record_repair("oc", "m1", "web_search", 1, "ok")
    for _ in range(3):
        tr.record_repair("oc", "m1", "web_search", 4, "coerced")
    tr.record_repair("oc", "m1", "web_search", 3, "ok")
    r = tr.repair_rate(7)
    assert r["total"] == 10 and r["rung_ge3"] == 4
    assert r["rate"] == pytest.approx(0.4)
    # old days fall outside the window
    path = config.MEMORY_DIR / "repair_stats.json"
    data = json.loads(path.read_text())
    data["days"]["2001-01-01"] = {"p/m/t": {"4:coerced": 99}}
    path.write_text(json.dumps(data))
    assert tr.repair_rate(7)["total"] == 10     # 99 excluded (out of window)


# ===========================================================================
# AREA 5 — C7 read-only guarantee (I-P1) + floors + no prefetch consumer
# ===========================================================================

def _fs_snapshot(root: Path) -> dict:
    snap = {}
    if not root.exists():
        return snap
    for p in sorted(root.rglob("*")):
        st = p.stat()
        snap[str(p)] = (st.st_mtime_ns, st.st_size if p.is_file() else -1)
    return snap


def _mk_run(rid, seq):
    steps = [{"id": f"s{i}", "specialist": sp, "task": f"do {sp}",
              "depends_on": ([f"s{i-1}"] if i else [])}
             for i, sp in enumerate(seq)]
    events = [{"t": 0.1, "stage": "dispatch", "specialists": list(seq)}]
    for i, sp in enumerate(seq):
        events.append({"t": 0.2, "stage": "dag.level", "n": i + 1,
                       "steps": [f"s{i}"]})
    return {"id": rid, "kind": "chat", "user": "shared", "events": events,
            "decisions": [{"schema_version": 1, "record_id": f"r{rid}",
                           "run_id": rid, "decision_type": "plan",
                           "agent": {"name": "athena", "role": "supervisor"},
                           "rationale": steps,
                           "outcome": {"status": "ok", "error": ""}}]}


def _write_runs(runs):
    import time
    base = config.MEMORY_DIR / "traces"
    base.mkdir(parents=True, exist_ok=True)
    with (base / f"{time.strftime('%Y%m%d')}.jsonl").open("a") as f:
        for r in runs:
            f.write(json.dumps(r) + "\n")


def test_predictability_report_writes_nothing(monkeypatch):
    from olympus import coupling
    _write_runs([_mk_run(f"r{i:04d}", ["argus", "hephaestus"])
                 for i in range(120)])
    before = _fs_snapshot(config.MEMORY_DIR)
    rep = coupling.predictability_report(days=30)
    assert rep["status"] == "ok"
    after = _fs_snapshot(config.MEMORY_DIR)
    assert before == after, "predictability_report mutated MEMORY_DIR (I-P1)"


def test_predictability_abstains_below_min_runs():
    from olympus import coupling
    _write_runs([_mk_run(f"r{i:04d}", ["argus", "hephaestus"])
                 for i in range(10)])
    rep = coupling.predictability_report(days=30)
    assert rep["status"] == "insufficient_data"
    assert rep["by_predictor"] == {} and rep["floors"]["pass"] is False


def test_predictability_floors_require_n200():
    from olympus import coupling
    _write_runs([_mk_run(f"r{i:04d}", ["argus", "hephaestus"])
                 for i in range(60)])       # perfect but n<200
    rep = coupling.predictability_report(days=30)
    assert rep["status"] == "ok"
    assert rep["by_predictor"]["P2_prev_specialist"]["recall_at_2"] \
        == pytest.approx(1.0)
    assert rep["floors"]["pass"] is False   # n<FLOOR_N gates the floor
    assert rep["floors"]["qualifying"] == []


def test_no_olympus_prefetch_consumer_anywhere_in_package():
    """A11 / I-P1: independently fs-audit the whole package — no source file may
    read or reference OLYMPUS_PREFETCH."""
    pkg = Path(config.__file__).parent
    offenders = [p.name for p in pkg.rglob("*.py")
                 if "OLYMPUS_PREFETCH" in p.read_text(encoding="utf-8")]
    assert offenders == [], f"OLYMPUS_PREFETCH referenced in: {offenders}"


# ===========================================================================
# AREA 6 — CONFIG DOCUMENTATION: every Wave-1 module env read is in .env.example
# ===========================================================================

_WAVE1_MODULES = ("sessionlog", "ctxbudget", "usage", "modelgate",
                  "toolcall_repair")
_ENV_LITERAL = re.compile(r'os\.environ(?:\.get)?\(\s*["\'](OLYMPUS_[A-Z0-9_]+)')


def _module_env_reads(modname: str) -> set[str]:
    path = Path(config.__file__).parent / f"{modname}.py"
    return set(_ENV_LITERAL.findall(path.read_text(encoding="utf-8")))


def _env_example_text() -> str:
    return (Path(config.__file__).parent.parent / ".env.example").read_text(
        encoding="utf-8")


def test_every_wave1_module_env_read_is_documented():
    """Grep each Wave-1 module for literal os.environ reads of OLYMPUS_* knobs
    and assert each appears in .env.example. (flags_fingerprint reads the
    pre-existing _DRIFT_FLAGS via a loop variable, not string literals, so they
    are correctly out of scope here.)"""
    doc = _env_example_text()
    undocumented: dict[str, list[str]] = {}
    for mod in _WAVE1_MODULES:
        missing = sorted(k for k in _module_env_reads(mod) if k not in doc)
        if missing:
            undocumented[mod] = missing
    assert not undocumented, f"undocumented Wave-1 knobs: {undocumented}"


def test_named_wave1_knobs_all_present():
    """Belt-and-braces: the spec-enumerated Wave-1 knob set is documented."""
    doc = _env_example_text()
    expected = [
        "OLYMPUS_SESSION_JOURNAL", "OLYMPUS_SESSION_FSYNC",
        "OLYMPUS_SESSION_JOURNAL_MAX_MB",
        "OLYMPUS_CTX_BUDGET", "OLYMPUS_CTX_OUTPUT_RESERVE_FRACTION",
        "OLYMPUS_CTX_REPAIR_RESERVE",
        "OLYMPUS_CACHE_READ_MULT", "OLYMPUS_CACHE_WRITE_MULT",
        "OLYMPUS_TOOL_VALIDATE", "OLYMPUS_TOOL_SALVAGE",
        "OLYMPUS_REPAIR_WARN_RATE",
        "OLYMPUS_DRIFT_GATE_EVERY", "OLYMPUS_DRIFT_BUDGET_USD",
        "OLYMPUS_COMMIT",
    ]
    missing = [k for k in expected if k not in doc]
    assert missing == [], f"missing from .env.example: {missing}"


def test_run_budget_usd_knob_is_documented():
    """FIXED (Wave-1 audit): OLYMPUS_RUN_BUDGET_USD (per-run USD ceiling, read in
    config.run_budget_usd() and surfaced by usage.run_budget()) is now documented
    in .env.example so an operator arming the budget guard can discover it."""
    # the knob is genuinely read by the source
    assert "OLYMPUS_RUN_BUDGET_USD" in (
        Path(config.__file__).read_text(encoding="utf-8"))
    assert "OLYMPUS_RUN_BUDGET_USD" in _env_example_text()

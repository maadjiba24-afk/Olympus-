"""C8 — typed tool-call recovery ladder (WAVE1_IMPLEMENTATION_SPEC §C8).

Corpus-driven ladder tests (golden malformation corpus in
tests/fixtures/toolcall_corpus/ — G5 discipline: every future real-world
malformation gets a case file), the I-T1 execution-precondition spy test
through openai_compat.run_agent, repair telemetry (aggregation, rate math,
corruption handling), the doctor decay warning, the legacy escape hatch, and
the ladder-overhead micro-benchmark.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from olympus import config, doctor, toolcall_repair as tr

CORPUS_DIR = Path(__file__).parent / "fixtures" / "toolcall_corpus"
CASES = sorted(CORPUS_DIR.glob("*.json"))
assert len(CASES) >= 20, "golden corpus must hold at least 20 cases"

_ARG_KEYS = ("arguments", "args", "parameters", "params", "input",
             "action_input")


def _strict(payload) -> bool:
    """Rung-1 shape: the payload parses directly as a JSON object AND its
    argument value (if any) is already a dict — no normalization needed."""
    if not isinstance(payload, str):
        return False
    try:
        obj = json.loads(payload)
    except (ValueError, TypeError):
        return False
    if not isinstance(obj, dict):
        return False
    fn = obj.get("function")
    if isinstance(fn, dict):
        a = fn.get("arguments")
        return a is None or isinstance(a, dict)
    for ak in _ARG_KEYS:
        if ak in obj:
            return isinstance(obj[ak], dict)
    return True


def run_ladder(payload, tool_defs, salvage: bool = False) -> dict:
    """Reference ladder — the same rungs run_agent wires, in pure form.

    Rungs: 1 strict · 2 validated-and-rejected (refused, handler never runs)
    · 3 normalized (fence/prose/double-encode/shape recovery) · 4 repaired
    (coerce / close-truncated / salvage) · 6 refused at recovery."""
    known = {t["name"] for t in tool_defs}
    schemas = {t["name"]: t.get("input_schema") or {} for t in tool_defs}
    call = tr.recover_tool_call(payload, known)
    closed_used = False
    if call is None and isinstance(payload, str):
        closed = tr.close_truncated(payload)
        if closed is not None:
            call = tr.recover_tool_call(closed, known)
            closed_used = call is not None
    if call is None:
        return {"rung": tr.RUNG_REFUSED, "args": None, "refused": True}
    name = call["function"]["name"]
    schema = schemas[name]
    args = tr.repair_arguments(call["function"].get("arguments"))
    errors, _warnings = tr.validate_arguments(args, schema)
    used_repair = closed_used
    if errors:
        coerced = tr.coerce_arguments(args, schema)
        e2, _ = tr.validate_arguments(coerced, schema)
        if not e2:
            args, errors, used_repair = coerced, [], True
    if errors and salvage and tr.salvage_enabled() and not args:
        raw = call["function"].get("arguments")
        payload_str = None
        if isinstance(raw, str) and raw.strip():
            try:
                v = json.loads(raw)
                payload_str = v if isinstance(v, str) else None
            except (ValueError, TypeError):
                payload_str = raw
        if payload_str:
            salvaged = tr.salvage_arguments(payload_str, schema)
            if salvaged is not None:
                e3, _ = tr.validate_arguments(salvaged, schema)
                if not e3:
                    args, errors, used_repair = salvaged, [], True
    if errors:
        return {"rung": tr.RUNG_VALIDATED, "args": None, "refused": True,
                "errors": errors}
    rung = (tr.RUNG_REPAIRED if used_repair
            else tr.RUNG_STRICT if _strict(payload) else tr.RUNG_NORMALIZED)
    return {"rung": rung, "args": args, "refused": False}


# --- 1. golden corpus ---------------------------------------------------------

@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_corpus_case(path, monkeypatch):
    case = json.loads(path.read_text(encoding="utf-8"))
    if case.get("salvage"):
        monkeypatch.setenv("OLYMPUS_TOOL_SALVAGE", "1")
    else:
        monkeypatch.delenv("OLYMPUS_TOOL_SALVAGE", raising=False)
    got = run_ladder(case["payload"], case["tools"],
                     salvage=bool(case.get("salvage")))
    exp = case["expect"]
    assert got["rung"] == exp["rung"], f"{path.stem}: rung {got}"
    assert got["refused"] == exp["refused"], f"{path.stem}: refusal {got}"
    # json.dumps comparison keeps types honest ("5" vs 5, True vs 1).
    assert (json.dumps(got["args"], sort_keys=True)
            == json.dumps(exp["args"], sort_keys=True)), f"{path.stem}: {got}"


def test_corpus_covers_every_required_class():
    """The spec's ≥20 malformation classes are all present by name."""
    stems = {p.stem for p in CASES}
    required = {
        "valid_strict", "fenced", "double_encoded_args", "prose_wrapped",
        "truncated_unambiguous_close", "truncated_ambiguous_partial_key",
        "digit_string_stays_string", "int_coercion", "float_coercion",
        "bool_coercion", "missing_required_rejected", "unknown_tool_refused",
        "shape_a_nested", "shape_b_tool_name_params",
        "shape_b_action_input_encoded", "shape_c_page_agent_fenced",
        "salvage_on_lone_payload", "salvage_off_lone_payload",
        "array_param_valid", "object_param_valid",
        "unknown_extra_key_allowed", "non_object_json_refused",
    }
    assert required <= stems


# --- 2. the I-T1 execution precondition through run_agent ----------------------

TOOL_DEFS = [{"name": "demo_tool", "description": "d",
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


def _native_call(args_json: str) -> dict:
    return {"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "demo_tool", "arguments": args_json}}]}}]}


def test_invalid_repaired_call_never_reaches_handler(monkeypatch):
    """I-T1: a schema-invalid call — even after repair — NEVER invokes the
    handler; the model gets an error result instead (spy on resolve_handler)."""
    from olympus import openai_compat, tools

    invoked: list[dict] = []

    def spy_handler(**kw):
        invoked.append(kw)
        return "ran"

    monkeypatch.setattr(openai_compat, "_post", _fake_post([
        _native_call('{"wrong_key": "x"}'),      # missing required 'count'
        {"choices": [{"message": {"content": "done", "tool_calls": []}}]},
    ]))
    monkeypatch.setattr(tools, "resolve_handler",
                        lambda name: spy_handler if name == "demo_tool" else None)

    settings = config.Settings(provider="openai_compat", model="weak-model")
    out = openai_compat.run_agent(settings, "sys", "go", TOOL_DEFS)
    assert out == "done"
    assert invoked == []                       # the handler NEVER ran
    # ...and the rejection was counted at rung 2.
    assert tr.repair_rate(7)["by_rung"].get("2") == 1


def test_error_feedback_message_reaches_model(monkeypatch):
    """The rejection is fed back as a tool-role error message (the existing
    error-feedback pattern), so the model can regenerate."""
    from olympus import openai_compat, tools

    seen_payloads: list[dict] = []

    responses = [
        _native_call('{}'),
        {"choices": [{"message": {"content": "ok", "tool_calls": []}}]},
    ]
    idx = {"i": 0}

    def fake(settings, payload):
        seen_payloads.append(payload)
        r = responses[idx["i"]]
        idx["i"] += 1
        return r

    monkeypatch.setattr(openai_compat, "_post", fake)
    monkeypatch.setattr(tools, "resolve_handler",
                        lambda name: (lambda **kw: "ran"))
    settings = config.Settings(provider="openai_compat", model="weak-model")
    assert openai_compat.run_agent(settings, "sys", "go", TOOL_DEFS) == "ok"
    tool_msgs = [m for m in seen_payloads[1]["messages"]
                 if m.get("role") == "tool"]
    assert tool_msgs and "Error: invalid arguments" in tool_msgs[0]["content"]
    assert "missing required key: count" in tool_msgs[0]["content"]


def test_coercible_call_executes_with_coerced_args(monkeypatch):
    """'5' for an integer param is coerced (rung 4) and the handler runs with
    the int — never with the raw string."""
    from olympus import openai_compat, tools

    invoked: list[dict] = []

    monkeypatch.setattr(openai_compat, "_post", _fake_post([
        _native_call('{"count": "5"}'),
        {"choices": [{"message": {"content": "done", "tool_calls": []}}]},
    ]))
    monkeypatch.setattr(
        tools, "resolve_handler",
        lambda name: (lambda **kw: invoked.append(kw) or "ok"))
    settings = config.Settings(provider="openai_compat", model="weak-model")
    assert openai_compat.run_agent(settings, "sys", "go", TOOL_DEFS) == "done"
    assert invoked == [{"count": 5}]
    r = tr.repair_rate(7)
    assert r["by_rung"].get("4") == 1          # recorded as rung-4 repair


def test_truncated_content_call_closed_and_executed(monkeypatch):
    """A truncated content-emitted call is closed (rung 4) and recovered
    through the same refusal-gated path."""
    from olympus import openai_compat, tools

    invoked: list[dict] = []
    defs = [{"name": "web_search", "description": "d",
             "input_schema": {"type": "object",
                              "properties": {"query": {"type": "string"}},
                              "required": ["query"]}}]
    monkeypatch.setattr(openai_compat, "_post", _fake_post([
        {"choices": [{"message": {
            "content": '{"name": "web_search", "arguments": {"query": "olympus tren',
            "tool_calls": []}}]},
        {"choices": [{"message": {"content": "done", "tool_calls": []}}]},
    ]))
    monkeypatch.setattr(
        tools, "resolve_handler",
        lambda name: (lambda **kw: invoked.append(kw) or "ok"))
    settings = config.Settings(provider="openai_compat", model="weak-model")
    assert openai_compat.run_agent(settings, "sys", "go", defs) == "done"
    assert invoked == [{"query": "olympus tren"}]


def test_legacy_passthrough_flag_off(monkeypatch):
    """OLYMPUS_TOOL_VALIDATE=off reverts to the pre-C8 pass-through: the
    handler receives the raw (uncoerced, unvalidated) args."""
    from olympus import openai_compat, tools

    monkeypatch.setenv("OLYMPUS_TOOL_VALIDATE", "off")
    invoked: list[dict] = []
    monkeypatch.setattr(openai_compat, "_post", _fake_post([
        _native_call('{"count": "5"}'),        # would be coerced when on
        {"choices": [{"message": {"content": "done", "tool_calls": []}}]},
    ]))
    monkeypatch.setattr(
        tools, "resolve_handler",
        lambda name: (lambda **kw: invoked.append(kw) or "ok"))
    settings = config.Settings(provider="openai_compat", model="weak-model")
    assert openai_compat.run_agent(settings, "sys", "go", TOOL_DEFS) == "done"
    assert invoked == [{"count": "5"}]         # raw string — legacy behaviour


def test_salvage_default_off_in_run_agent(monkeypatch):
    """A14 fail-safe default: without OLYMPUS_TOOL_SALVAGE the lone-payload
    call bounces as invalid; with it, the payload maps onto the single
    required param and executes."""
    from olympus import openai_compat, tools

    defs = [{"name": "web_search", "description": "d",
             "input_schema": {"type": "object",
                              "properties": {"query": {"type": "string"}},
                              "required": ["query"]}}]

    def run(salvage: bool):
        if salvage:
            monkeypatch.setenv("OLYMPUS_TOOL_SALVAGE", "1")
        else:
            monkeypatch.delenv("OLYMPUS_TOOL_SALVAGE", raising=False)
        invoked: list[dict] = []
        monkeypatch.setattr(openai_compat, "_post", _fake_post([
            {"choices": [{"message": {"content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "web_search",
                              "arguments": "who is athena"}}]}}]},
            {"choices": [{"message": {"content": "done", "tool_calls": []}}]},
        ]))
        monkeypatch.setattr(
            tools, "resolve_handler",
            lambda name: (lambda **kw: invoked.append(kw) or "ok"))
        settings = config.Settings(provider="openai_compat", model="weak")
        assert openai_compat.run_agent(settings, "sys", "go", defs) == "done"
        return invoked

    assert run(salvage=False) == []                          # rejected
    assert run(salvage=True) == [{"query": "who is athena"}]  # salvaged


# --- 3. telemetry -------------------------------------------------------------

def test_record_repair_aggregates_and_rate_math():
    for _ in range(6):
        tr.record_repair("openai-compat", "m1", "web_search", 1, "ok")
    for _ in range(3):
        tr.record_repair("openai-compat", "m1", "web_search", 4, "coerced")
    tr.record_repair("openai-compat", "m1", "web_search", 3, "ok")
    r = tr.repair_rate(7)
    assert r["total"] == 10
    assert r["rung_ge3"] == 4
    assert r["rate"] == pytest.approx(0.4)
    assert r["by_rung"] == {"1": 6, "3": 1, "4": 3}
    # Day-bucketed schema on disk.
    data = json.loads((config.MEMORY_DIR / "repair_stats.json").read_text())
    assert data["v"] == 1
    day = time.strftime("%Y-%m-%d")
    assert data["days"][day]["openai-compat/m1/web_search"]["1:ok"] == 6


def test_repair_rate_window_excludes_old_days():
    path = config.MEMORY_DIR / "repair_stats.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"v": 1, "days": {
        "2001-01-01": {"p/m/t": {"4:coerced": 99}},
        time.strftime("%Y-%m-%d"): {"p/m/t": {"1:ok": 2}},
    }}))
    r = tr.repair_rate(7)
    assert r["total"] == 2 and r["rung_ge3"] == 0


def test_corrupt_stats_file_moved_aside_and_restarted():
    path = config.MEMORY_DIR / "repair_stats.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json !!!")
    tr.record_repair("p", "m", "t", 1, "ok")   # must not raise (I-T3)
    corrupt = list(config.MEMORY_DIR.glob("repair_stats.corrupt.*.json"))
    assert corrupt, "corrupt telemetry moved aside, not deleted or repaired"
    assert corrupt[0].read_text() == "{not json !!!"
    assert tr.repair_rate(7)["total"] == 1     # fresh counting resumed


def test_record_repair_never_raises(monkeypatch):
    # Even a broken lock layer must not surface into the call path (I-T3).
    from olympus import proclock

    def boom(*a, **k):
        raise RuntimeError("lock exploded")
    monkeypatch.setattr(proclock, "lock", boom)
    tr.record_repair("p", "m", "t", 2, "rejected")   # no exception


def test_repair_rate_on_missing_file_is_zero():
    r = tr.repair_rate(7)
    assert r == {"days": 7, "total": 0, "rung_ge3": 0, "rate": 0.0,
                 "by_rung": {}}


# --- 4. doctor decay warning ----------------------------------------------------

def _write_stats(rung_counts: dict[int, int]):
    path = config.MEMORY_DIR / "repair_stats.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    day = time.strftime("%Y-%m-%d")
    cells = {f"{r}:x": n for r, n in rung_counts.items()}
    path.write_text(json.dumps({"v": 1, "days": {day: {"p/m/t": cells}}}))


def test_doctor_warns_on_rising_repair_rate():
    _write_stats({1: 70, 4: 30})               # 30% rung>=3 over 100 calls
    checks = doctor._repair_checks()
    assert len(checks) == 1
    assert checks[0].status == doctor.WARN
    assert "30.0%" in checks[0].detail and "100 calls" in checks[0].detail


def test_doctor_ok_below_threshold_or_below_volume():
    _write_stats({1: 95, 4: 5})                # 5% — healthy
    assert doctor._repair_checks()[0].status == doctor.OK
    _write_stats({1: 10, 4: 30})               # 75% but only 40 calls
    assert doctor._repair_checks()[0].status == doctor.OK


def test_doctor_threshold_env_override(monkeypatch):
    monkeypatch.setenv("OLYMPUS_REPAIR_WARN_RATE", "0.5")
    _write_stats({1: 70, 4: 30})               # 30% < 50% override
    assert doctor._repair_checks()[0].status == doctor.OK


def test_repair_check_registered_in_run_checks():
    names = [c.name for c in doctor.run_checks()]
    assert "tool-call repair" in names


# --- 5. ladder overhead micro-check ---------------------------------------------

def test_ladder_overhead_under_5ms_per_call():
    schema = TOOL_DEFS[0]["input_schema"]
    payload = '{"name": "demo_tool", "arguments": {"count": "5"}}'
    tool_list = TOOL_DEFS
    n = 200
    t0 = time.perf_counter()
    for _ in range(n):
        run_ladder(payload, tool_list)
        errs, _ = tr.validate_arguments({"count": "5"}, schema)
        tr.coerce_arguments({"count": "5"}, schema)
    per_call_ms = (time.perf_counter() - t0) / n * 1000
    print(f"\nladder overhead: {per_call_ms:.4f} ms/call (loose bound 5 ms)")
    assert per_call_ms < 5.0

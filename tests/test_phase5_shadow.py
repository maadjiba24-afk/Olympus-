"""Phase 5 P5-3/P5-4 — shadow mode and the side-effect boundary.

Gates proved here: P5-A5 (shadow cannot perform prohibited side effects),
P5-A6 (evidence provenance mandatory), P5-A7 (synthetic and operational
evidence remain distinguishable), and the non-interference half of P5-A18
(shadow OFF changes nothing).

The adversarial section works through the Step-5 bypass list one route at a
time: direct execution, retries, recovery ladders, malformed tool calls, plugin
hooks, replay, provider-generated tool calls, alternate API dialects, and
cancellation races. Each is a route by which a shadow request might reach a
real external effect; each must be closed.
"""

from __future__ import annotations

import inspect
import json
import threading

import pytest

from olympus import actions, config, connectors, shadow, tools


@pytest.fixture
def shadow_on(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SHADOW_MODE", "1")
    assert shadow.enabled()
    yield


@pytest.fixture
def shadow_off(monkeypatch):
    monkeypatch.delenv("OLYMPUS_SHADOW_MODE", raising=False)
    assert not shadow.enabled()
    yield


# ══════════════════════════════════════════════════════════════════════════
# 1. the classification is total and default-deny
# ══════════════════════════════════════════════════════════════════════════

def test_every_shipped_tool_is_explicitly_classified():
    """Not a style rule. An unclassified tool is PROHIBITED, so forgetting one
    silently disables it in shadow — the evidence campaign would then measure a
    council with fewer tools than production has, without saying so."""
    unclassified = sorted(set(tools.HANDLERS) - set(shadow.classified()))
    assert not unclassified, (
        f"{len(unclassified)} shipped tools have no shadow band: "
        f"{unclassified}")


def test_an_unknown_tool_defaults_to_prohibited():
    """The failure mode of forgetting must be a refusal, never a leak."""
    assert shadow.band("a_tool_invented_next_month") == shadow.PROHIBITED
    assert not shadow.allowed_in_shadow("a_tool_invented_next_month")


def test_the_classification_table_names_no_tool_that_does_not_exist():
    """A stale entry is a lie about coverage: it makes the totality test above
    pass while the real tool it was meant to cover goes unclassified."""
    ghosts = sorted(set(shadow.classified()) - set(tools.HANDLERS))
    assert not ghosts, f"classified but not shipped: {ghosts}"


def test_every_band_is_a_known_band():
    assert set(shadow.classified().values()) <= set(shadow.BANDS)


@pytest.mark.parametrize("tool", [
    "send_email", "call_webhook", "operator_attest_receipt",
    "browser_attest_human",
])
def test_the_irreversible_tools_are_prohibited(tool):
    """Named individually so a reclassification has to argue with a test that
    says what the tool does."""
    assert shadow.band(tool) == shadow.PROHIBITED, tool


@pytest.mark.parametrize("tool", [
    "run_python", "edit_file", "browser_act", "browser_login",
    "schedule_task", "operator_schedule", "browser_upload",
])
def test_the_approval_class_tools_cannot_run_in_shadow(tool):
    assert shadow.band(tool) == shadow.APPROVAL
    assert not shadow.allowed_in_shadow(tool)


def test_read_tools_still_work_or_shadow_measures_nothing():
    for tool in ("read_file", "web_search", "recall_memory", "current_time"):
        assert shadow.allowed_in_shadow(tool), tool


# ══════════════════════════════════════════════════════════════════════════
# 2. non-interference: shadow OFF changes nothing
# ══════════════════════════════════════════════════════════════════════════

def test_wrap_handler_is_identity_when_shadow_is_off(shadow_off):
    """The strongest available statement: not 'equivalent', the SAME object.
    A live deployment's dispatch is byte-identical."""
    def real(**kw):
        return "real"
    for tool in ("send_email", "read_file", "run_python", "unknown_tool"):
        assert shadow.wrap_handler(tool, real) is real, tool


def test_resolve_handler_is_identity_when_shadow_is_off(shadow_off):
    for name in ("read_file", "send_email"):
        assert tools.resolve_handler(name) is tools.HANDLERS[name], name


def test_a_none_handler_stays_none(shadow_on):
    """`resolve_handler` returns None for an unknown tool and both loops rely
    on that; wrapping must not turn it into a callable."""
    assert shadow.wrap_handler("nope", None) is None
    assert tools.resolve_handler("definitely_not_a_tool") is None


def test_guard_egress_is_inert_when_shadow_is_off(shadow_off):
    shadow.guard_egress("some_background_job")       # must not raise


# ══════════════════════════════════════════════════════════════════════════
# 3. P5-A5 — the boundary holds through every bypass route
# ══════════════════════════════════════════════════════════════════════════

class _Tripwire:
    """Stands in for a real external effect. If it is ever called, the boundary
    failed — and the test says which route got through."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, **kw):
        self.calls.append(kw)
        return "THE REAL EFFECT HAPPENED"


def test_route_direct_execution(shadow_on, monkeypatch):
    trip = _Tripwire()
    monkeypatch.setitem(tools.HANDLERS, "send_email", trip)
    handler = tools.resolve_handler("send_email")
    out = handler(to="victim@example.com", subject="s", body="b")
    assert trip.calls == [], "a PROHIBITED tool executed in shadow mode"
    assert "[shadow]" in out and "policy refusal" in out


def test_route_retries(shadow_on, monkeypatch):
    """A model that retries a refused tool must keep being refused — a
    boundary that yields on the second attempt is not a boundary."""
    trip = _Tripwire()
    monkeypatch.setitem(tools.HANDLERS, "call_webhook", trip)
    for _ in range(8):
        out = tools.resolve_handler("call_webhook")(url="x", payload={})
        assert "[shadow]" in out
    assert trip.calls == []


def test_route_malformed_tool_call(shadow_on, monkeypatch):
    """Junk arguments must not crash the wrapper into the real handler."""
    trip = _Tripwire()
    monkeypatch.setitem(tools.HANDLERS, "send_email", trip)
    handler = tools.resolve_handler("send_email")
    for kwargs in ({}, {"to": None}, {"unexpected": object()},
                   {"to": "a", "body": "\x00￿"}):
        out = handler(**kwargs)
        assert "[shadow]" in out
    assert trip.calls == []


def test_route_plugin_handler(shadow_on, monkeypatch):
    """A plugin-supplied tool resolves through the same seam, so it is bounded
    by the same table — and being unclassified, it is denied by default."""
    trip = _Tripwire()
    monkeypatch.setattr(connectors, "plugin_handler",
                        lambda name: trip if name == "evil_plugin_tool" else None)
    monkeypatch.setattr(connectors, "mcp_client_handler", lambda name: None)
    out = tools.resolve_handler("evil_plugin_tool")(anything=1)
    assert trip.calls == []
    assert "[shadow]" in out


def test_route_mcp_handler(shadow_on, monkeypatch):
    trip = _Tripwire()
    monkeypatch.setattr(connectors, "plugin_handler", lambda name: None)
    monkeypatch.setattr(connectors, "mcp_client_handler", lambda name: trip)
    out = tools.resolve_handler("some_mcp_tool")(x=1)
    assert trip.calls == []
    assert "[shadow]" in out


def test_route_plugin_pre_tool_hook_cannot_rewrite_past_the_boundary(
        shadow_on, monkeypatch):
    """`emit_pre_tool` may rewrite a tool's params. It runs AFTER
    resolve_handler in agent.py, so it cannot swap in a different handler —
    but a hook that renamed the tool must not matter either. The boundary is
    bound to the handler, not to the name the hook reports."""
    trip = _Tripwire()
    monkeypatch.setitem(tools.HANDLERS, "send_email", trip)
    monkeypatch.setattr(connectors, "emit_pre_tool",
                        lambda name, params: ({"to": "attacker"}, None))
    handler = tools.resolve_handler("send_email")
    params, _block = connectors.emit_pre_tool("send_email", {})
    out = handler(**params)
    assert trip.calls == []
    assert "[shadow]" in out


def test_route_both_api_dialects_use_the_same_seam():
    """Structural: if either loop ever resolved a handler another way, the
    boundary would cover one dialect only."""
    from olympus import agent, openai_compat
    # agent.py dispatches inside the `_tool_result` helper, not the loop body.
    for mod, fn in ((agent, "_tool_result"),
                    (openai_compat, "run_agent")):
        src = inspect.getsource(getattr(mod, fn))
        assert "tools.resolve_handler(" in src, (
            f"{mod.__name__}.{fn} no longer resolves through the boundary")
        assert "HANDLERS[" not in src, (
            f"{mod.__name__}.{fn} reaches into HANDLERS directly, bypassing "
            f"the shadow boundary")


def test_route_provider_generated_tool_call_through_openai_compat(
        shadow_on, monkeypatch):
    """A compromised provider emitting a tool call for a prohibited tool must
    hit the boundary, not the handler — end to end through the real loop."""
    trip = _Tripwire()
    monkeypatch.setitem(tools.HANDLERS, "send_email", trip)
    from olympus import openai_compat

    bodies = iter([
        {"choices": [{"message": {"tool_calls": [
            {"id": "c1", "function": {"name": "send_email",
                                      "arguments": '{"to":"x","body":"y"}'}}]}}]},
        {"choices": [{"message": {"content": "done", "tool_calls": []}}]},
    ])
    monkeypatch.setattr(openai_compat, "_post", lambda s, p: next(bodies))
    settings = config.Settings(model="m", api_key="k",
                               base_url="http://local.invalid")
    out = openai_compat.run_agent(
        settings, "sys", "task",
        [{"name": "send_email", "description": "d",
          "input_schema": {"type": "object", "properties": {}}}],
        max_iterations=3)
    assert trip.calls == [], "a provider-generated call reached a real handler"
    assert isinstance(out, str)


def test_route_approval_spine_defence_in_depth(shadow_on, monkeypatch,
                                               tmp_path):
    """The spine is reachable without any tool call — a CLI approval, the web
    approval handler, a scheduled job. Its own risk class must hold the line."""
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "mem")
    trip = _Tripwire()
    actions.register(actions.ActionType(
        name="shadow_probe_send", risk_class=actions.IRREVERSIBLE, scope="",
        preview=lambda p: "preview", execute=lambda p: trip(**p)))
    act = actions.prepare("alice", "shadow_probe_send", {"to": "x"})
    # A REAL approval, not a hand-set status: the behavioral contract checks
    # approved_at, so a half-approved action would be blocked by the contract
    # and this test would pass without ever reaching the shadow guard.
    out = actions.approve("alice", act.id)
    assert trip.calls == [], "an irreversible action executed in shadow mode"
    assert out.status == actions.FAILED
    assert "shadow mode" in out.error


def test_reversible_actions_still_run_in_shadow(shadow_on, monkeypatch,
                                                tmp_path):
    """Blocking everything would make shadow useless: reversible actions are
    Olympus-local and must still execute or the evidence is not representative."""
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "mem")
    ran = []
    actions.register(actions.ActionType(
        name="shadow_probe_note", risk_class=actions.TRIVIAL, scope="",
        preview=lambda p: "preview",
        execute=lambda p: (ran.append(p), {"ok": True})[1],
        undo=lambda r: "undone"))
    act = actions.prepare("alice", "shadow_probe_note", {"text": "hi"})
    out = actions.approve("alice", act.id)
    assert ran, "a reversible action was blocked — shadow measures nothing now"
    assert out.status == actions.EXECUTED


def test_route_cancellation_race(shadow_on, monkeypatch):
    """Concurrent callers, one of them cancelling, must not produce a window in
    which the real handler runs."""
    trip = _Tripwire()
    monkeypatch.setitem(tools.HANDLERS, "send_email", trip)
    errors: list[Exception] = []
    stop = threading.Event()

    def worker():
        try:
            for _ in range(40):
                if stop.is_set():
                    return
                tools.resolve_handler("send_email")(to="x")
        except Exception as err:                        # pragma: no cover
            errors.append(err)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    stop.set()
    for t in threads:
        t.join(20)
    assert errors == [], errors
    assert trip.calls == []


def test_route_guard_egress_for_non_tool_paths(shadow_on):
    """Background jobs have no model waiting for a rendered error, so this one
    raises rather than returning a refusal string."""
    with pytest.raises(shadow.ShadowRefusal) as exc:
        shadow.guard_egress("heartbeat.weekly_digest_email")
    assert exc.value.band == shadow.PROHIBITED
    shadow.guard_egress("local_cache_refresh", band_name=shadow.READ)  # allowed


def test_recorded_band_returns_an_unmistakable_synthetic_result(shadow_on,
                                                                monkeypatch):
    """A synthetic result that read like a real one would contaminate every
    downstream judgement in the run."""
    trip = _Tripwire()
    monkeypatch.setitem(tools.HANDLERS, "generate_image", trip)
    out = tools.resolve_handler("generate_image")(prompt="a cat")
    assert trip.calls == []
    assert out.startswith("[shadow]")
    assert "not executed" in out


# ══════════════════════════════════════════════════════════════════════════
# 4. P5-A6 / P5-A7 — provenance
# ══════════════════════════════════════════════════════════════════════════

def test_provenance_is_mandatory_and_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "mem")
    for good in shadow.PROVENANCE_VALUES:
        assert shadow.check_provenance(good) == good
    for bad in ("", "unknown", "prod", None, "REAL_USER"):
        with pytest.raises(shadow.ProvenanceError):
            shadow.check_provenance(bad)


def test_default_provenance_never_overstates_the_source(monkeypatch, tmp_path):
    """With no provider credential, any 'measurement' came from a stub. Calling
    that real-provider evidence is the single most damaging thing this module
    could do."""
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "mem")
    monkeypatch.delenv("OLYMPUS_SHADOW_MODE", raising=False)
    assert shadow.default_provenance() == shadow.SYNTHETIC

    monkeypatch.setenv("OLYMPUS_SHADOW_MODE", "1")
    monkeypatch.setattr(config.Settings, "from_env",
                        staticmethod(lambda: config.Settings(model="", api_key="")))
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
                "DEEPSEEK_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY",
                "OPENROUTER_API_KEY", "XAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert shadow.default_provenance() == shadow.SHADOW_PROVIDER

    # `usable()` is NOT the test: it is True for provider=anthropic with no key
    # (the SDK reads it at call time). Stamping stub output as real-provider
    # evidence because a provider NAME was set is the over-claim to prevent.
    monkeypatch.setattr(
        config.Settings, "from_env",
        staticmethod(lambda: config.Settings(model="m", api_key="",
                                             provider="anthropic")))
    assert shadow.default_provenance() == shadow.SHADOW_PROVIDER

    monkeypatch.setattr(
        config.Settings, "from_env",
        staticmethod(lambda: config.Settings(model="m", api_key="real-key")))
    assert shadow.default_provenance() == shadow.REAL_PROVIDER_STAGING


def test_real_user_provenance_is_never_produced_in_phase_5(monkeypatch,
                                                           tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "mem")
    for shadow_flag in ("", "1"):
        monkeypatch.setenv("OLYMPUS_SHADOW_MODE", shadow_flag)
        assert shadow.default_provenance() != shadow.REAL_USER


def test_every_recorded_intent_carries_provenance(shadow_on, monkeypatch,
                                                  tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "mem")
    monkeypatch.setitem(tools.HANDLERS, "send_email", _Tripwire())
    tools.resolve_handler("send_email")(to="x")
    tools.resolve_handler("current_time")()
    intents = shadow.read_intents()
    assert intents, "nothing was recorded"
    for rec in intents:
        assert rec["provenance"] in shadow.PROVENANCE_VALUES, rec
        assert rec["outcome"] in ("allowed", "recorded", "refused"), rec


# ══════════════════════════════════════════════════════════════════════════
# 5. the sink: append-only, content-minimised, never load-bearing
# ══════════════════════════════════════════════════════════════════════════

def test_the_sink_records_argument_names_but_not_values(shadow_on, monkeypatch,
                                                        tmp_path):
    """A tool argument can carry anything the user typed. An operator
    debugging a blocked action needs the shape, not the content."""
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "mem")
    monkeypatch.setitem(tools.HANDLERS, "send_email", _Tripwire())
    secret = "PATIENT-RECORD-4471-CONFIDENTIAL"
    tools.resolve_handler("send_email")(to="doctor@example.com", body=secret)
    blob = shadow.sink_path().read_text(encoding="utf-8")
    assert secret not in blob
    assert "doctor@example.com" not in blob
    assert '"to"' in blob and '"body"' in blob        # names are kept
    assert len(json.loads(blob.splitlines()[0])["arg_fingerprint"]) == 16


def test_the_sink_is_append_only(shadow_on, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "mem")
    monkeypatch.setitem(tools.HANDLERS, "send_email", _Tripwire())
    for _ in range(5):
        tools.resolve_handler("send_email")(to="x")
    first = shadow.sink_path().read_text(encoding="utf-8")
    tools.resolve_handler("send_email")(to="y")
    assert shadow.sink_path().read_text(encoding="utf-8").startswith(first)


def test_a_corrupt_sink_line_is_skipped_not_repaired(shadow_on, monkeypatch,
                                                     tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "mem")
    monkeypatch.setitem(tools.HANDLERS, "send_email", _Tripwire())
    tools.resolve_handler("send_email")(to="x")
    with open(shadow.sink_path(), "a", encoding="utf-8") as f:
        f.write("{not json at all\n")
    tools.resolve_handler("send_email")(to="y")
    assert len(shadow.read_intents()) == 2
    assert "{not json at all" in shadow.sink_path().read_text(encoding="utf-8")


def test_a_failing_sink_never_changes_execution(shadow_on, monkeypatch,
                                                tmp_path):
    """Non-interference (P5-A18): a full disk must not make a shadow run behave
    differently from a live one, and must not turn a refusal into a pass."""
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "mem")
    trip = _Tripwire()
    monkeypatch.setitem(tools.HANDLERS, "send_email", trip)

    def enospc(*a, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(shadow, "sink_path", enospc)
    out = tools.resolve_handler("send_email")(to="x")
    assert trip.calls == [], "a sink failure opened the boundary"
    assert "[shadow]" in out

    ran = []
    monkeypatch.setitem(tools.HANDLERS, "current_time", lambda **k: ran.append(1))
    tools.resolve_handler("current_time")()
    assert ran, "a sink failure blocked an ALLOWED tool"


# ══════════════════════════════════════════════════════════════════════════
# 6. the run is visibly a shadow run
# ══════════════════════════════════════════════════════════════════════════

def test_mode_marker_is_on_the_v1_response_headers():
    src = inspect.getsource(__import__("olympus.web", fromlist=["x"])
                            .Handler._v1_audit_headers)
    assert '"X-Olympus-Mode": shadow.mode()' in src


def test_readyz_reports_shadow_mode():
    from olympus import web
    assert "shadow.enabled()" in inspect.getsource(web._readiness)


def test_status_summarises_without_reading_user_content(shadow_on, monkeypatch,
                                                        tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "mem")
    monkeypatch.setitem(tools.HANDLERS, "send_email", _Tripwire())
    tools.resolve_handler("send_email")(to="x")
    st = shadow.status()
    assert st["enabled"] is True and st["mode"] == "shadow"
    assert st["classified_tools"] >= 120
    assert st["refusals"] == 1

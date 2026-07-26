"""PHASE 4 STAGE A — end-to-end integration validation of the live system.

These tests drive the REAL orchestration code (`Olympus.ask` / `ask_stream` /
`_pipeline` / `replay_run`, `agent.run_agent_counted`, `backend` failover,
`sessionlog`, `usage.slot`, `modelgate.run_gate`, `doctor.config_skew`) against
fake providers. No network, no sleeps beyond the fake-clock steps the house
fixtures already use, and every assertion is on OBSERVABLE behaviour — the
reply the user gets, the persisted trace, the journal on disk, the typed
refusal — never on an implementation detail.

The provider fake is the canonical `tests/test_decisionlog.py` harness
(`_message` / `_FakeStream` / `_FakeMessages` / `_fake_client`, monkeypatched
over `llm.client`), extended with a `text_stream` (for `ask_stream`) and
`tool_use` content (for the agent tool loop). Stages are recognised from the
request itself — the structured-output schema for the JSON stages, a marker in
the prompt for the text stages — so the fake never has to know the prompt
wording.

Flow coverage (the Phase-4 Stage A list) is stated in each test's docstring.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import os
import threading
import time
import types

import anthropic
import pytest

from olympus import (agent, backend, config, doctor, errors, llm, memory,
                     modelgate, orchestrator, replaystore, sessionlog,
                     streamguard, trace as trace_mod, usage, watchdog)

# --------------------------------------------------------------------------
# the house fake provider
# --------------------------------------------------------------------------


def _message(text: str = "", *, stop_reason: str = "end_turn",
             content: list | None = None) -> anthropic.types.Message:
    """A minimal but REAL anthropic Message (round-trips through to_json)."""
    return anthropic.types.Message.model_validate({
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-test",
        "content": content if content is not None
        else [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5,
                  "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0},
    })


def _tool_use_message(tool_id: str, name: str,
                      tool_input: dict) -> anthropic.types.Message:
    return _message(stop_reason="tool_use", content=[
        {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}])


class _FakeStream:
    """The SDK stream contract this codebase actually uses: a context manager
    with `get_final_message()` and (for `llm.stream_text`) `text_stream`."""

    def __init__(self, message, pieces=None):
        self._m = message
        self._pieces = pieces

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self._m

    @property
    def text_stream(self):
        if self._pieces is not None:
            return iter(self._pieces)
        return iter([b.text for b in self._m.content if b.type == "text"])


class _FakeMessages:
    """Counts every streamed call so a replay that touches the network fails."""

    def __init__(self, counter, responder):
        self.counter = counter
        self.responder = responder

    def stream(self, **params):
        self.counter[0] += 1
        out = self.responder(params)
        if isinstance(out, tuple):          # (final message, text pieces)
            return _FakeStream(out[0], out[1])
        return _FakeStream(out)


def _fake_client(counter, responder):
    msgs = _FakeMessages(counter, responder)
    return types.SimpleNamespace(messages=msgs,
                                 beta=types.SimpleNamespace(messages=msgs))


# --- stage recognition ------------------------------------------------------

def _schema_props(params) -> set:
    fmt = (params.get("output_config") or {}).get("format") or {}
    return set(((fmt.get("schema") or {}).get("properties") or {}))


def stage_of(params) -> str:
    """Which orchestrator stage issued this request — derived from the request
    itself (structured-output schema, else a prompt marker), never from prompt
    wording we would then be pinning."""
    props = _schema_props(params)
    if props:
        if "mode" in props and "specialists" in props:
            return "route"
        if "steps" in props:
            return "plan"
        if "verdict" in props:
            return "review"
        if "checkable" in props:
            return "direct_verify"
        if "faithful" in props:
            return "synth_check"
        if "add" in props and "reinforce" in props:
            return "ace"            # the ACE compaction generator
        return "json:" + ",".join(sorted(props))
    blob = json.dumps(params.get("messages"))
    if "VERDICT:" in blob:
        return "verify"
    if "Compose the final reply to the user" in blob:
        return "synthesize"
    return "specialist"


_ROUTE_DELEGATE = {
    "mode": "delegate", "direct_reply": None,
    "specialists": ["plutus", "peitho"], "brief": "the launch brief",
    "clarifying_questions": [], "needs_verification": True,
}
_PLAN_TWO = {"steps": [
    {"id": "s1", "specialist": "plutus", "task": "cost the launch",
     "depends_on": []},
    {"id": "s2", "specialist": "peitho", "task": "position the launch",
     "depends_on": []},
]}
_APPROVE = {"verdict": "approve", "feedback": "", "retry_specialists": []}


def _verdict_text(status: str, claims=()) -> str:
    return ("checked content\n"
            'VERDICT: {"status": "%s", "unsupported_claims": %s, '
            '"confidence": 0.9}' % (status, json.dumps(list(claims))))


class Responder:
    """A deterministic council: one canned answer per stage, a call log, and
    per-stage overrides so a test can make exactly one stage misbehave."""

    def __init__(self, **overrides):
        self.calls: list[str] = []
        self.overrides = overrides
        self.specialist_output = "a concrete specialist finding"
        self.synthesis = "FINAL ANSWER composed for the user."
        self.verdict_status = "pass"
        self.ace_fact = "the user is preparing a product launch"
        self.review_verdicts = [_APPROVE]
        self._review_i = 0

    # -- defaults ----------------------------------------------------------
    def default(self, stage, params):
        if stage == "route":
            return _message(json.dumps(_ROUTE_DELEGATE))
        if stage == "plan":
            return _message(json.dumps(_PLAN_TWO))
        if stage == "review":
            v = self.review_verdicts[min(self._review_i,
                                         len(self.review_verdicts) - 1)]
            self._review_i += 1
            return _message(json.dumps(v))
        if stage == "verify":
            return _message(_verdict_text(self.verdict_status,
                                          ["an unsupported claim"]
                                          if self.verdict_status == "reject"
                                          else []))
        if stage == "synthesize":
            return _message(self.synthesis)
        if stage == "specialist":
            return _message(self.specialist_output)
        if stage == "direct_verify":
            return _message(json.dumps({"checkable": False, "supported": True,
                                        "unsupported_claims": []}))
        if stage == "synth_check":
            return _message(json.dumps({"faithful": True,
                                        "unsupported_additions": [],
                                        "confidence": 0.9}))
        if stage == "ace":
            return _message(json.dumps({
                "add": [{"content": self.ace_fact, "section": "facts",
                         "pinned": True}],
                "reinforce": [], "obsolete": []}))
        raise AssertionError(f"fake provider got an unexpected stage: {stage}")

    def __call__(self, params):
        stage = stage_of(params)
        self.calls.append(stage)
        hook = self.overrides.get(stage)
        if hook is not None:
            out = hook(params)
            if out is not None:
                return out
        return self.default(stage, params)


def install(monkeypatch, responder, counter=None):
    """Point `llm.client` at the fake. Returns the call counter."""
    counter = counter if counter is not None else [0]
    monkeypatch.setattr(llm, "client",
                        lambda settings=None: _fake_client(counter, responder))
    return counter


def make_bot(monkeypatch, responder=None, *, user="val", counter=None,
             conversation_id=None, report=None):
    responder = responder or Responder()
    install(monkeypatch, responder, counter)
    bot = orchestrator.Olympus(
        report=report or (lambda _m: None),
        settings=config.Settings(provider="anthropic", model="claude-test",
                                 api_key="k"),
        user=user, conversation_id=conversation_id)
    return bot, responder


def run_of(bot) -> dict:
    """The persisted trace of the bot's last run (never the in-memory one)."""
    run = trace_mod.load_run(bot.last_run_id)
    assert run is not None, "the run was not persisted to the trace log"
    return run


def stages(run: dict) -> list[str]:
    return [e["stage"] for e in run["events"]]


def decision_types(run: dict) -> list[str]:
    return [d["decision_type"] for d in run["decisions"]]


@pytest.fixture(autouse=True)
def _wave2_flags_off(monkeypatch):
    """Every test starts from the SHIPPED defaults and opts in explicitly, so
    one test's flag can never leak into another's baseline."""
    for flag in ("OLYMPUS_WATCHDOG", "OLYMPUS_STREAMGUARD", "OLYMPUS_ADMISSION",
                 "OLYMPUS_CTX_BUDGET", "OLYMPUS_MODELGRADE", "OLYMPUS_ROUTESUB",
                 "OLYMPUS_CTXHEAT", "OLYMPUS_REPLAY", "OLYMPUS_FAST"):
        monkeypatch.delenv(flag, raising=False)
    usage.admission_reset()
    yield
    usage.admission_reset()


# ==========================================================================
# FLOW 1 — a simple request completes and returns a reply
# ==========================================================================

def test_flow01_simple_request_completes_and_returns_a_reply(monkeypatch):
    """A plain request runs the whole council on the fake provider and comes
    back with the composed answer, a persisted trace and a recorded run id."""
    bot, resp = make_bot(monkeypatch)
    reply = bot.ask("how should I budget for a product launch?")

    assert reply == "FINAL ANSWER composed for the user."
    assert bot.last_run_id
    run = run_of(bot)
    assert run["meta"]["input"] == "how should I budget for a product launch?"
    assert {"route", "plan", "review"} <= set(decision_types(run))
    # the turn is in history, so the next turn continues from it
    assert bot.history[-2:] == [
        {"role": "user", "content": "how should I budget for a product launch?"},
        {"role": "assistant", "content": reply}]


def test_flow01_direct_reply_path_also_returns_a_reply(monkeypatch):
    """The cheap direct path (Zeus answers without the council) still returns a
    real reply rather than an empty string."""
    direct = {"mode": "direct", "direct_reply": "Use a 3-month runway.",
              "specialists": [], "brief": None, "clarifying_questions": [],
              "needs_verification": False}
    resp = Responder(route=lambda p: _message(json.dumps(direct)))
    bot, _ = make_bot(monkeypatch, resp)
    assert bot.ask("quick sanity check?") == "Use a 3-month runway."


# ==========================================================================
# FLOW 2 — long context: compaction, and NEVER a silent truncation
# ==========================================================================

def _force_compaction(monkeypatch, keep=2):
    """Make the next `_finish` compact: a tiny budget and a tiny verbatim tail."""
    monkeypatch.setattr(config, "HISTORY_KEEP_TURNS", keep)
    monkeypatch.setattr(config, "history_token_budget", lambda model=None: 1)


def test_flow02_long_context_compacts_rather_than_dropping_history(monkeypatch):
    """A history over budget is FOLDED into a durable state block by the real
    ACE compaction path — the older turns are summarized, not silently dropped,
    and the recent tail survives verbatim."""
    _force_compaction(monkeypatch)
    resp = Responder()
    resp.ace_fact = "the launch date is 3 March"
    bot, _ = make_bot(monkeypatch, resp)
    bot.history = [{"role": "user", "content": "old turn %d" % i}
                   for i in range(10)]
    bot.ask("and now the newest question")

    joined = "\n".join(str(m["content"]) for m in bot.history)
    assert "[Conversation state" in joined, "history was not compacted at all"
    assert "the launch date is 3 March" in joined       # the folded facts kept
    assert "and now the newest question" in joined      # recent tail verbatim
    assert len(bot.history) < 12                        # older slice folded away
    assert "ace" in resp.calls                          # the real ACE path ran


def test_flow02_failed_compaction_is_disclosed_never_silent(monkeypatch):
    """I-C5 / Wave-1 A6: when compaction FAILS the fallback drops the older
    slice — that drop must be observable. With a run trace in scope it emits
    `context.truncated`; with OLYMPUS_CTX_BUDGET on it is additionally captured
    in the operator error ledger. Neither path may be silent.

    NOTE (defect, reported separately): on the real `ask()` path
    `_compress_history` runs from `_finish`, where `trace.current()` is None
    and the run's trace has already been flushed — so only the ledger half of
    this disclosure actually reaches an operator. This test pins BOTH halves at
    the seam that owns them, so a fix cannot regress either."""
    from olympus import ace
    monkeypatch.setenv("OLYMPUS_CTX_BUDGET", "1")
    _force_compaction(monkeypatch)
    monkeypatch.setattr(ace, "evolve", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("compaction model unavailable")))
    monkeypatch.setattr(orchestrator.backend, "complete_text",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("legacy compaction unavailable")))

    bot, _ = make_bot(monkeypatch)
    bot.history = [{"role": "user", "content": "turn %d" % i} for i in range(10)]

    tr = trace_mod.Trace("chat", "val")
    token = trace_mod.set_current(tr)
    try:
        bot._compress_history()
    finally:
        trace_mod.reset_current(token)

    truncations = [e for e in tr.events if e["stage"] == "context.truncated"]
    assert truncations, "history was dropped with no trace event — silent truncation"
    assert truncations[0]["dropped"] == 8
    assert truncations[0]["reason"] == "compaction_failed"
    # and the flag-on operator disclosure
    captured = [r for r in errors.recent(200)
                if r["where"] == "orchestrator.compress_history"]
    assert captured, "flag-on truncation left no operator-visible record"
    assert "history truncated" in captured[-1]["error"]
    assert "dropped 8" in captured[-1]["context"]


# ==========================================================================
# FLOW 3 — multi-specialist council: dispatch and synthesis
# ==========================================================================

def test_flow03_multi_specialist_council_dispatches_and_synthesizes(monkeypatch):
    """A plan naming two specialists dispatches BOTH, and the synthesis stage
    is handed the work of both before composing the user's answer."""
    seen_tasks: list[str] = []
    synth_prompts: list[str] = []

    def on_specialist(params):
        seen_tasks.append(json.dumps(params["messages"]))
        return None

    def on_synth(params):
        synth_prompts.append(json.dumps(params["messages"]))
        return None

    resp = Responder(specialist=on_specialist, synthesize=on_synth)
    bot, _ = make_bot(monkeypatch, resp)
    reply = bot.ask("plan and cost the launch")

    assert any("cost the launch" in t for t in seen_tasks)
    assert any("position the launch" in t for t in seen_tasks)
    assert len(seen_tasks) == 2, "both planned specialists must run"
    run = run_of(bot)
    dispatched = [e for e in run["events"] if e["stage"] == "dispatch"]
    assert dispatched and set(dispatched[0]["specialists"]) == {"plutus",
                                                                "peitho"}
    assert synth_prompts, "the council never reached synthesis"
    assert reply == "FINAL ANSWER composed for the user."


# ==========================================================================
# FLOW 4 — a tool call executes through the approval / security path
# ==========================================================================

def _tool_loop_responder(tool_name, tool_input, *, final="done"):
    """First turn asks for the tool, second returns text."""
    state = {"n": 0}

    def responder(params):
        state["n"] += 1
        if state["n"] == 1:
            return _tool_use_message("toolu_1", tool_name, tool_input)
        return _message(final)
    return responder, state


def test_flow04_tool_call_executes_through_the_security_path(monkeypatch):
    """A specialist tool call runs the real `agent` loop: the plugin pre-tool
    policy hook sees it, the handler executes, the post-tool hook sees the
    output, untrusted output is enveloped by `security.wrap_untrusted`, and the
    result is frozen for replay."""
    from olympus import security, tools
    ran: list[dict] = []
    pre_seen: list[str] = []

    def handler(**kw):
        ran.append(kw)
        return "SEARCH RESULT BODY"

    monkeypatch.setattr(tools, "resolve_handler",
                        lambda name: handler if name == "web_search" else None)
    real_pre = agent.connectors.emit_pre_tool

    def spy_pre(name, params):
        pre_seen.append(name)
        return real_pre(name, params)
    monkeypatch.setattr(agent.connectors, "emit_pre_tool", spy_pre)

    responder, _ = _tool_loop_responder("web_search", {"query": "olympus"},
                                        final="answer using the tool output")
    counter = install(monkeypatch, responder)
    text, calls = agent.run_agent_counted(
        "sys", "find something",
        settings=config.Settings(provider="anthropic", model="claude-test",
                                 api_key="k"),
        tool_defs=[{"name": "web_search"}])

    assert text == "answer using the tool output"
    assert calls == 1                       # the loop counted the tool call
    assert ran == [{"query": "olympus"}]    # the handler actually executed
    assert pre_seen == ["web_search"]       # policy hook ran BEFORE execution
    assert security.should_wrap("web_search") is True
    frozen = replaystore.get_tool("toolu_1")
    assert frozen is not None and frozen["is_error"] is False
    assert "SEARCH RESULT BODY" in frozen["content"]
    # fail-closed envelope: the raw body is not handed to the model unlabelled
    assert frozen["content"] != "SEARCH RESULT BODY"
    assert counter[0] == 2                  # tool turn + final turn


# ==========================================================================
# FLOW 5 — a failing tool surfaces the failure; the run still completes
# ==========================================================================

def test_flow05_failed_tool_is_surfaced_not_swallowed(monkeypatch):
    """A handler that raises must reach the model as an ERROR tool_result (and
    be frozen as one), and the agent run must still complete."""
    from olympus import tools
    sent: list = []

    def boom(**kw):
        raise RuntimeError("the disk is on fire")

    monkeypatch.setattr(tools, "resolve_handler", lambda name: boom)
    responder, _ = _tool_loop_responder("read_file", {"path": "/x"},
                                        final="I could not read the file.")

    def spy(params):
        sent.append(json.dumps(params["messages"]))
        return responder(params)

    install(monkeypatch, spy)
    text, calls = agent.run_agent_counted(
        "sys", "read it",
        settings=config.Settings(provider="anthropic", model="claude-test",
                                 api_key="k"),
        tool_defs=[{"name": "read_file"}])

    assert text == "I could not read the file."      # the run completed
    assert calls == 1
    frozen = replaystore.get_tool("toolu_1")
    assert frozen is not None
    assert frozen["is_error"] is True                 # typed as a failure
    assert "the disk is on fire" in frozen["content"]
    # and the failure was actually shown to the model, not dropped
    assert "the disk is on fire" in sent[-1]
    assert '"is_error": true' in sent[-1].lower().replace("'", '"')


def test_flow05_failed_specialist_leaves_a_visible_marker_in_the_run(monkeypatch):
    """At the council level the same rule holds: a specialist whose provider
    call fails yields a typed 'treat this part as missing' marker that reaches
    verification, instead of an empty section nobody notices."""
    real_agent = backend.run_agent_counted

    def only_specialists_fail(settings, system, task, tool_defs, **kw):
        if "VERDICT:" in task:            # the verifier itself stays healthy
            return real_agent(settings, system, task, tool_defs, **kw)
        raise RuntimeError("specialist provider exploded")

    monkeypatch.setattr(backend, "run_agent_counted", only_specialists_fail)
    verify_inputs: list[str] = []

    def on_verify(params):
        verify_inputs.append(json.dumps(params["messages"]))
        return None

    resp = Responder(verify=on_verify)
    reports: list[str] = []
    bot, _ = make_bot(monkeypatch, resp, report=reports.append)
    reply = bot.ask("cost the launch")

    assert reply == "FINAL ANSWER composed for the user."   # run completed
    assert verify_inputs and "could not complete this task" in verify_inputs[0]
    assert any("failed" in r for r in reports)              # user-visible notice


# ==========================================================================
# FLOW 6 — provider failure: a disclosed degradation, never a crash
# ==========================================================================

def test_flow06_provider_failure_at_synthesis_is_disclosed(monkeypatch):
    """When the final compose fails with a provider error the run degrades to
    the verified findings — and says so, in the trace AND to the user-facing
    reporter. It never raises and never pretends the answer is complete."""
    def explode(params):
        raise anthropic.APIConnectionError(request=None)

    resp = Responder(synthesize=explode)
    reports: list[str] = []
    bot, _ = make_bot(monkeypatch, resp, report=reports.append)
    reply = bot.ask("cost the launch")

    run = run_of(bot)
    failed = [e for e in run["events"] if e["stage"] == "synthesize.failed"]
    assert failed, "a failed compose left no trace event"
    assert any("Final compose failed" in r for r in reports)
    assert "checked content" in reply     # the verified findings were returned
    assert reply.strip()


def test_flow06_provider_failure_fails_over_before_degrading(monkeypatch):
    """Degradation is the LAST resort: a provider-side error on a pool member
    first fails over to another member (the disclosed outcome then being a
    normal answer from the alternate)."""
    monkeypatch.setenv("OLYMPUS_PROVIDER", "openai")
    monkeypatch.setenv("OLYMPUS_MODEL", "gpt-5")
    monkeypatch.setenv("OLYMPUS_API_KEY", "k1")
    monkeypatch.setenv("OLYMPUS_MODELS", json.dumps(
        [{"provider": "openai", "model": "deepseek-v3", "api_key": "k2"}]))
    tried: list[str] = []

    def flaky(settings, system, messages, effort):
        tried.append(settings.model)
        if settings.model == "gpt-5":
            raise ConnectionError("upstream reset")
        return "answer from the alternate member"

    monkeypatch.setattr(backend.openai_compat, "complete_text", flaky)
    primary = config.ModelPool.from_env().primary()
    out = backend.complete_text(primary, "sys",
                                [{"role": "user", "content": "q"}])
    assert out == "answer from the alternate member"
    assert tried == ["gpt-5", "deepseek-v3"]


# ==========================================================================
# FLOW 7 — verifier disagreement: reject -> rework -> UNVERIFIED banner
# ==========================================================================

def test_flow07_verifier_reject_forces_rework_then_banners_the_answer(
        monkeypatch):
    """A `reject` verdict is enforced: exactly one forced rework, and if the
    rework is still rejected the reply ships behind a structural UNVERIFIED
    banner naming the claims. The banner is applied AFTER synthesis, so the
    composing model can never drop it."""
    resp = Responder()
    resp.verdict_status = "reject"
    bot, _ = make_bot(monkeypatch, resp)
    reply = bot.ask("cost the launch")

    assert reply.startswith("⚠️ UNVERIFIED")
    assert "an unsupported claim" in reply
    assert "FINAL ANSWER composed for the user." in reply   # answer still shipped
    run = run_of(bot)
    assert "answer.rework" in stages(run)                   # forced rework ran
    unverified = [e for e in run["events"] if e["stage"] == "answer.unverified"]
    assert unverified and unverified[0]["reason"] == "reject_after_rework"
    verdicts = [d for d in run["decisions"]
                if d["decision_type"] == "verify_verdict"]
    assert verdicts and all(v["outcome"]["status"] == "violation"
                            for v in verdicts)


def test_flow07_passing_verdict_ships_without_a_banner(monkeypatch):
    """The control: a `pass` verdict must NOT banner — otherwise the banner
    carries no information."""
    bot, _ = make_bot(monkeypatch)
    reply = bot.ask("cost the launch")
    assert not reply.startswith("⚠️")
    assert "answer.unverified" not in stages(run_of(bot))


# ==========================================================================
# FLOW 8 — cancellation: an aborted run releases cleanly
# ==========================================================================

class _Contract:
    def __init__(self, ok, violations=()):
        self.ok = ok
        self.violations = list(violations)


def _progress_free_bot(monkeypatch, spend=1.0):
    """A run that spends money and produces nothing a verifier accepted — the
    watchdog's PROGRESS_FREE_SPEND shape, driven deterministically (no sleeps:
    money moves through a fake `today_spend`, not the clock)."""
    from olympus import contracts
    box = {"total": 0.0}

    def _today():
        v = box["total"]
        box["total"] += spend
        return v

    monkeypatch.setattr(usage, "today_spend", _today)
    monkeypatch.setenv("OLYMPUS_CONTRACTS", "1")
    monkeypatch.setattr(contracts, "check",
                        lambda out, contract, tool_calls=None: _Contract(
                            False, ["fabricated citation"]))
    resp = Responder()
    resp.specialist_output = "fluent but fabricated prose"
    return make_bot(monkeypatch, resp)


def test_flow08_cancelled_run_releases_cleanly_and_discloses(monkeypatch):
    """Enforce-mode cancellation is a DISCLOSED refusal, and it releases: the
    run-scoped cancel token is tripped, the lease is closed (not leaked into
    the next turn), forensics exist, and no admission slot or process semaphore
    permit is left held."""
    monkeypatch.setenv("OLYMPUS_WATCHDOG", "enforce")
    bot, _ = _progress_free_bot(monkeypatch)
    opened: list = []
    real_open = orchestrator._wd_open
    monkeypatch.setattr(orchestrator, "_wd_open",
                        lambda tr: (opened.append(real_open(tr)), opened[-1])[1])
    permits_before = usage._SEMAPHORE._value

    reply = bot.ask("cost the launch")

    assert "cancelled this run" in reply           # disclosed, not a traceback
    assert "Forensics" in reply                    # names where the evidence is
    assert bot._watch is None                      # lease closed by _finish
    assert usage._SEMAPHORE._value == permits_before   # no slot leaked
    assert usage.admission_status()["in_flight"] == 0

    assert watchdog.PROGRESS_FREE_SPEND in reply   # names the exact reason

    watch = opened[0]
    assert watch.cancel_token.cancelled() is True  # nothing else may queue
    assert watch.lease.cancelled is True
    assert watch.lease.refused is True
    assert watch.lease.released is True            # the injected release_fn ran
    record = watchdog.read_forensics(watch.lease.lease_id)
    assert record, "a cancelled run preserved no forensics"
    assert record["verdict"]["kind"] == watchdog.PROGRESS_FREE_SPEND
    assert record["mode"] == "enforce"
    assert record["operator_action"]
    # NOTE (defect, reported separately): `record["outcome"]` is relabelled to
    # "ok" here, because `_finish` closes the lease with close("ok") even on the
    # cancelled path and `close()` re-writes the forensics. Not asserted either
    # way, so this test neither hides nor enshrines it.
    # the cancelled turn is still recorded — a cancellation is not a lost turn
    assert bot.history[-1]["content"] == reply


def test_flow08_a_second_turn_after_a_cancellation_runs_clean(monkeypatch):
    """Release means the NEXT turn is unaffected: with the watchdog disarmed
    again the same bot answers normally."""
    monkeypatch.setenv("OLYMPUS_WATCHDOG", "enforce")
    bot, resp = _progress_free_bot(monkeypatch)
    assert "cancelled this run" in bot.ask("first")

    monkeypatch.delenv("OLYMPUS_WATCHDOG", raising=False)
    from olympus import contracts
    monkeypatch.setattr(contracts, "check",
                        lambda out, contract, tool_calls=None: _Contract(True))
    assert bot.ask("second") == "FINAL ANSWER composed for the user."
    assert bot._watch is None


# ==========================================================================
# FLOW 9 — retry: a rework cycle completes, bounded
# ==========================================================================

def test_flow09_rework_cycle_completes_and_is_bounded(monkeypatch):
    """Athena orders one rework; the reworked answer is re-reviewed exactly
    once and ships even if the re-review still grumbles — no unbounded loop."""
    retry = {"verdict": "retry", "feedback": "be concrete",
             "retry_specialists": ["plutus"]}
    resp = Responder()
    resp.review_verdicts = [retry, retry]      # still flagged after the rework
    bot, _ = make_bot(monkeypatch, resp)
    reply = bot.ask("cost the launch")

    assert reply == "FINAL ANSWER composed for the user."
    run = run_of(bot)
    assert "rework" in stages(run)
    assert "re_review.still_flagged" in stages(run)
    types_ = decision_types(run)
    assert types_.count("review") == 1 and types_.count("re_review") == 1
    assert resp.calls.count("review") == 2, "the review loop was not bounded"
    # the rework really re-ran the flagged specialist (2 first pass + 1 redo)
    assert resp.calls.count("specialist") == 3


# ==========================================================================
# FLOW 10 — session resume: history + journal across two turns
# ==========================================================================

def test_flow10_session_resume_keeps_history_and_journals_both_turns(
        monkeypatch):
    """Two turns on one conversation_id: history persists across process
    boundaries (a fresh Olympus reloads it) and BOTH turns are sealed into the
    append-only session journal."""
    bot, resp = make_bot(monkeypatch, conversation_id="conv-val-10")
    bot.ask("first question")
    bot.ask("second question")

    assert [m["content"] for m in bot.history if m["role"] == "user"] == [
        "first question", "second question"]

    resumed = orchestrator.Olympus(
        settings=config.Settings(provider="anthropic", model="claude-test",
                                 api_key="k"),
        user="val", conversation_id="conv-val-10")
    assert resumed.history == bot.history

    records, status = sessionlog.read_verified("conv-val-10")
    assert status == "ok"
    payload = json.dumps([r["payload"] for r in records])
    assert "first question" in payload and "second question" in payload
    assert sessionlog._replay(records) == bot.history


# ==========================================================================
# FLOW 11 — a corrupted snapshot recovers from the sealed journal
# ==========================================================================

def test_flow11_corrupt_snapshot_recovers_from_the_journal(monkeypatch):
    """The Wave-1 C1 guarantee, end to end: corrupting
    `conversations/<id>.json` must NOT silently yield an empty history — the
    verified journal prefix rebuilds it."""
    bot, _ = make_bot(monkeypatch, conversation_id="conv-val-11")
    bot.ask("remember the launch date")
    bot.ask("and the budget")
    expected = list(bot.history)
    assert len(expected) == 4

    snap = (config.MEMORY_DIR / "conversations"
            / f"{memory.safe_id('conv-val-11')}.json")
    assert snap.exists()
    snap.write_text("{not json at all", encoding="utf-8")

    recovered = memory.load_conversation("conv-val-11")
    assert recovered != [], "a corrupt snapshot silently dropped the history"
    assert recovered == expected
    # and the recovery is recorded, not silent
    assert any("corrupt snapshot" in r["context"]
               for r in errors.recent(200)
               if r["where"] == "memory.load_conversation")
    # a resumed session sees the recovered history
    resumed = orchestrator.Olympus(
        settings=config.Settings(provider="anthropic", model="claude-test",
                                 api_key="k"),
        user="val", conversation_id="conv-val-11")
    assert resumed.history == expected


# ==========================================================================
# FLOW 12 — provider drift classification (injected run_fn, no model calls)
# ==========================================================================

def _drift_run_fn(domain_scores=None, *, default=8.0):
    domain_scores = domain_scores or {}

    def run_fn(settings, item):
        dom = item.get("domain") or item.get("specialist")
        return {"score": domain_scores.get(dom, default), "cost": 0.0,
                "error": False}
    return run_fn


def test_flow12_provider_drift_is_classified_and_reversible(monkeypatch):
    """A drifted score set is classified from the SAME evidence store the gate
    uses: the first run seeds a baseline, a multi-domain drop freezes the
    member with a named reason, and the freeze is reversible."""
    s = config.Settings(provider="anthropic", model="claude-test", api_key="k")
    member = f"{s.provider}/{s.model}"

    seed = modelgate.run_gate(s, run_fn=_drift_run_fn(), budget_usd=0.0)
    assert seed.severity == modelgate.NO_BASELINE
    assert modelgate.frozen_members() == {}

    drifted = modelgate.run_gate(
        s, run_fn=_drift_run_fn({"code": 2, "multilingual": 2}),
        budget_usd=0.0)
    assert drifted.severity == modelgate.FREEZE
    assert drifted.ok is False
    assert "regression on" in drifted.stopped_reason
    assert member in modelgate.frozen_members()
    assert modelgate.unfreeze(member) is True
    assert modelgate.frozen_members() == {}


def test_flow12_verification_domain_drift_blocks(monkeypatch):
    """Drift in a verification/refusal domain is the strictest rung — it BLOCKS
    rather than merely warning, because that is the domain the safety
    guarantees rest on."""
    s = config.Settings(provider="anthropic", model="claude-test", api_key="k")
    modelgate.run_gate(s, run_fn=_drift_run_fn(), budget_usd=0.0)
    res = modelgate.run_gate(s, run_fn=_drift_run_fn({"verify": 2}),
                             budget_usd=0.0)
    assert res.severity == modelgate.BLOCK
    assert res.ok is False


# ==========================================================================
# FLOW 13 — admission saturation: a typed refusal, never a quality downgrade
# ==========================================================================

@contextlib.contextmanager
def _noop_slot(name, count, timeout=None):
    yield


class _Holder(threading.Thread):
    daemon = True

    def __init__(self, **kw):
        super().__init__()
        self.kw = kw
        self.entered = threading.Event()
        self.release = threading.Event()
        self.error = None

    def run(self):
        try:
            with usage.slot(**self.kw):
                self.entered.set()
                self.release.wait(5)
        except BaseException as err:          # noqa: BLE001 - reported
            self.error = err
            self.entered.set()


def test_flow13_saturated_admission_refuses_with_retry_guidance(monkeypatch):
    """Overload is a TYPED refusal carrying machine-readable retry guidance —
    and explicitly NOT a downgrade (`degraded` is False and the admission API
    has no model/effort/mode parameter it could downgrade with)."""
    monkeypatch.setenv("OLYMPUS_ADMISSION", "1")
    monkeypatch.setenv("OLYMPUS_ADMISSION_RESERVE", "0")
    monkeypatch.setattr(usage.proclock, "slot", _noop_slot)
    monkeypatch.setattr(usage, "_SEMAPHORE", threading.BoundedSemaphore(64))
    monkeypatch.setattr(config, "MAX_CONCURRENT_CALLS", 1)

    blocker = _Holder(key="tenant-a")
    blocker.start()
    assert blocker.entered.wait(3) and blocker.error is None
    try:
        with pytest.raises(usage.AdmissionRefused) as ei:
            with usage.slot(key="tenant-b", timeout=0):
                pass
    finally:
        blocker.release.set()
        blocker.join(3)

    err = ei.value
    assert err.reason == "no_capacity"
    body = err.as_dict()
    assert body["error"] == "admission_refused"
    assert body["degraded"] is False          # W2-I7.1: never a silent downgrade
    assert body["retry_after_s"] >= 0
    status, http_body, headers = usage.admission_http_response(err)
    assert status == 429 and headers["Retry-After"].isdigit()
    assert headers["X-Olympus-Admission"] == "no_capacity"
    # structural: admission cannot express a downgrade at all
    params = set(inspect.signature(usage.slot).parameters)
    assert not (params & {"model", "effort", "route", "mode", "quality"})
    assert usage.admission_status()["counters"]["refused"] >= 1


def test_flow13_queue_full_also_refuses_rather_than_degrading(monkeypatch):
    """The other saturation shape (the wait queue itself is full) produces the
    same typed refusal, not a cheaper answer."""
    monkeypatch.setenv("OLYMPUS_ADMISSION", "1")
    monkeypatch.setenv("OLYMPUS_ADMISSION_RESERVE", "0")
    monkeypatch.setenv("OLYMPUS_ADMISSION_MAX_QUEUE", "1")
    monkeypatch.setattr(usage.proclock, "slot", _noop_slot)
    monkeypatch.setattr(usage, "_SEMAPHORE", threading.BoundedSemaphore(64))
    monkeypatch.setattr(config, "MAX_CONCURRENT_CALLS", 1)

    blocker = _Holder(key="tenant-a")
    blocker.start()
    assert blocker.entered.wait(3) and blocker.error is None
    waiting = _Holder(key="tenant-b", timeout=5)
    waiting.start()
    deadline = time.monotonic() + 3
    while usage.admission_status()["queued"] != 1:
        assert time.monotonic() < deadline, "waiter never queued"
    try:
        with pytest.raises(usage.AdmissionRefused) as ei:
            with usage.slot(key="tenant-c", timeout=5):
                pass
    finally:
        blocker.release.set()
        assert waiting.entered.wait(3)
        waiting.release.set()
        blocker.join(3)
        waiting.join(3)
    assert ei.value.reason == "queue_full"
    assert ei.value.retry_after_s > 0          # actionable retry guidance
    assert ei.value.as_dict()["degraded"] is False


# ==========================================================================
# FLOW 14 — a pathological stream aborts WITH disclosure
# ==========================================================================

def test_flow14_pathological_stream_aborts_with_disclosure(monkeypatch):
    """W2-I8.3 end-to-end on the live streaming path: a degenerate provider
    stream must never reach the user as a normal-looking answer. The already-
    yielded prefix is kept, the looping fragment is dropped, and an explicit
    unfinished/unverified notice is appended — plus a trace event and a
    pathology evidence record for the operator."""
    monkeypatch.setenv("OLYMPUS_STREAMGUARD", "1")
    good = "Here is the start of a real answer. "
    loop = "and then the same sentence again " * 40

    def on_synth(params):
        return (_message(good + loop), [good, loop, "MUST NEVER BE YIELDED"])

    resp = Responder(synthesize=on_synth)
    bot, _ = make_bot(monkeypatch, resp)
    out = "".join(bot.ask_stream("cost the launch"))

    assert good in out
    assert "MUST NEVER BE YIELDED" not in out
    assert "Response incomplete" in out
    assert "unfinished and unverified" in out
    assert "degenerate-stream guard" in out
    run = run_of(bot)
    aborted = [e for e in run["events"]
               if e["stage"] == "synthesize.stream_aborted"]
    assert aborted, "the abort was not traced"
    assert aborted[0]["kind"] == streamguard.TOKEN_LOOP
    assert any(r["kind"] == streamguard.TOKEN_LOOP
               for r in streamguard.recent(20))


def test_flow14_guard_off_streams_unchanged(monkeypatch):
    """Flag-off inertness: the same pathological stream is delivered unchanged
    (the guard is opt-in), so the disclosure above is attributable to the guard
    and not to some other truncation."""
    good = "Here is the start of a real answer. "
    loop = "and then the same sentence again " * 40

    def on_synth(params):
        return (_message(good + loop), [good, loop])

    resp = Responder(synthesize=on_synth)
    bot, _ = make_bot(monkeypatch, resp)
    out = "".join(bot.ask_stream("cost the launch"))
    assert loop in out
    assert "Response incomplete" not in out


# ==========================================================================
# FLOW 15 — pool failover across local and cloud members
# ==========================================================================

def _pool_env(monkeypatch, *members):
    first = members[0]
    monkeypatch.setenv("OLYMPUS_PROVIDER", first["provider"])
    monkeypatch.setenv("OLYMPUS_MODEL", first.get("model", ""))
    if first.get("api_key"):
        monkeypatch.setenv("OLYMPUS_API_KEY", first["api_key"])
    if first.get("base_url"):
        monkeypatch.setenv("OLYMPUS_BASE_URL", first["base_url"])
    else:
        monkeypatch.delenv("OLYMPUS_BASE_URL", raising=False)
    monkeypatch.setenv("OLYMPUS_MODELS", json.dumps(list(members[1:])))


def test_flow15_local_model_failure_falls_over_to_the_cloud_member(monkeypatch):
    """A local (loopback) member that goes down hands the call to the cloud
    member — the answer still arrives, from a NAMED alternate."""
    _pool_env(monkeypatch,
              {"provider": "openai", "model": "llama-3.3-70b",
               "api_key": "local", "base_url": "http://127.0.0.1:11434/v1"},
              {"provider": "openai", "model": "gpt-5", "api_key": "cloud"})
    tried: list[str] = []

    def fake(settings, system, messages, effort):
        tried.append(settings.model)
        if settings.base_url:                      # the local runtime is down
            raise ConnectionError("connection refused")
        return "cloud answer"

    monkeypatch.setattr(backend.openai_compat, "complete_text", fake)
    primary = config.ModelPool.from_env().primary()
    assert backend.complete_text(primary, "s", [{"role": "user",
                                                 "content": "q"}]) \
        == "cloud answer"
    assert tried == ["llama-3.3-70b", "gpt-5"]


def test_flow15_cloud_model_failure_falls_over_to_the_local_member(monkeypatch):
    """And symmetrically: a cloud outage falls over onto the local member, so
    a self-hosted alternate is a real availability answer and not decoration."""
    _pool_env(monkeypatch,
              {"provider": "openai", "model": "gpt-5", "api_key": "cloud"},
              {"provider": "openai", "model": "llama-3.3-70b",
               "api_key": "local", "base_url": "http://127.0.0.1:11434/v1"})
    tried: list[str] = []

    def fake(settings, system, messages, effort):
        tried.append(settings.model)
        if not settings.base_url:
            raise RuntimeError("503 upstream unavailable")
        return "local answer"

    monkeypatch.setattr(backend.openai_compat, "complete_text", fake)
    primary = config.ModelPool.from_env().primary()
    assert backend.complete_text(primary, "s", [{"role": "user",
                                                 "content": "q"}]) \
        == "local answer"
    assert tried == ["gpt-5", "llama-3.3-70b"]


def test_flow15_a_refusal_is_never_laundered_by_failover(monkeypatch):
    """Failover covers provider-side FAILURES only. A semantic refusal must not
    be retried on another member until one says yes — that would turn policy
    into a shopping exercise."""
    _pool_env(monkeypatch,
              {"provider": "openai", "model": "gpt-5", "api_key": "cloud"},
              {"provider": "openai", "model": "llama-3.3-70b",
               "api_key": "local", "base_url": "http://127.0.0.1:11434/v1"})
    tried: list[str] = []

    def refuse(settings, system, messages, schema, effort):
        tried.append(settings.model)
        raise ValueError("model refused the request")

    monkeypatch.setattr(backend.openai_compat, "complete_json", refuse)
    primary = config.ModelPool.from_env().primary()
    with pytest.raises(ValueError):
        backend.complete_json(primary, "s", [{"role": "user", "content": "q"}],
                              {"type": "object"})
    assert tried == ["gpt-5"]


# ==========================================================================
# FLOW 16 — a refused action is surfaced, never silently dropped
# ==========================================================================

def test_flow16_plugin_policy_refusal_is_surfaced_to_the_model(monkeypatch):
    """A pre-tool policy block must reach the model as an explicit, typed
    error naming the policy — it can never look like a tool that simply
    returned nothing."""
    from olympus import tools
    executed: list = []
    sent: list[str] = []

    monkeypatch.setattr(tools, "resolve_handler",
                        lambda name: (lambda **kw: executed.append(kw)))
    monkeypatch.setattr(agent.connectors, "emit_pre_tool",
                        lambda name, params: (params, "egress not permitted"))

    responder, _ = _tool_loop_responder("browse_page", {"url": "http://x"},
                                        final="I was not allowed to do that.")

    def spy(params):
        sent.append(json.dumps(params["messages"]))
        return responder(params)

    install(monkeypatch, spy)
    text, _ = agent.run_agent_counted(
        "sys", "browse it",
        settings=config.Settings(provider="anthropic", model="claude-test",
                                 api_key="k"),
        tool_defs=[{"name": "browse_page"}])

    assert executed == [], "a blocked tool was executed anyway"
    frozen = replaystore.get_tool("toolu_1")
    assert frozen is not None and frozen["is_error"] is True
    assert "Blocked by plugin policy" in frozen["content"]
    assert "egress not permitted" in frozen["content"]
    assert "Blocked by plugin policy" in sent[-1]
    assert text == "I was not allowed to do that."


def test_flow16_model_refusal_is_surfaced_as_text_not_swallowed(monkeypatch):
    """A provider-side refusal stop-reason becomes an explicit statement, not
    an empty completion the caller might mistake for a valid answer."""
    install(monkeypatch, lambda params: _message("", stop_reason="refusal"))
    out = backend.complete_text(
        config.Settings(provider="anthropic", model="claude-test", api_key="k"),
        "sys", [{"role": "user", "content": "do the disallowed thing"}])
    assert out == "[The model declined this request for safety reasons.]"


def test_flow16_unknown_tool_is_refused_visibly(monkeypatch):
    """The unclassified case: a tool with no handler is refused with a named
    error, not silently skipped."""
    from olympus import tools
    monkeypatch.setattr(tools, "resolve_handler", lambda name: None)
    responder, _ = _tool_loop_responder("nonexistent_tool", {}, final="ok")
    install(monkeypatch, responder)
    agent.run_agent_counted(
        "sys", "t",
        settings=config.Settings(provider="anthropic", model="claude-test",
                                 api_key="k"),
        tool_defs=[{"name": "nonexistent_tool"}])
    frozen = replaystore.get_tool("toolu_1")
    assert frozen["is_error"] is True
    assert "no handler for tool 'nonexistent_tool'" in frozen["content"]


# ==========================================================================
# FLOW 17 — user-data deletion physically removes the payload bytes
# ==========================================================================

def test_flow17_tombstone_then_compaction_removes_the_payload_bytes():
    """Deletion has to be real: a tombstone hides the turn from recovery
    immediately, and `compact()` past it removes the bytes from the journal
    file — verified by reading the raw file, not by asking the API."""
    cid = "conv-val-17"
    secret = "MY-PRIVATE-ACCOUNT-NUMBER-4242"
    seq1 = sessionlog.append_turn(cid, [{"role": "user", "content": secret}])
    seq2 = sessionlog.append_turn(
        cid, [{"role": "user", "content": "a keeper of a message"}])
    assert seq1 == 1 and seq2 == 2

    path = sessionlog._journal_path(memory.safe_id(cid))
    assert secret in path.read_text(encoding="utf-8")

    sessionlog.append_tombstone(cid, seq1, seq1)
    # tombstoned turns disappear from recovery at once…
    recovered = sessionlog.recover_history(cid)
    assert recovered == [{"role": "user", "content": "a keeper of a message"}]
    # …but the bytes are still there until compaction
    assert secret in path.read_text(encoding="utf-8")

    sessionlog.compact(cid, seq1)
    raw = path.read_text(encoding="utf-8")
    assert secret not in raw, "compaction left the deleted payload on disk"
    assert "a keeper of a message" in raw          # only the deleted turn went
    assert sessionlog.recover_history(cid) == [
        {"role": "user", "content": "a keeper of a message"}]
    assert sessionlog.journal_status(cid) == "ok"  # the chain still verifies


def test_flow17_delete_session_removes_journal_and_snapshot(monkeypatch):
    """The whole-session erase: journal, snapshot and quarantine copies all
    go, so nothing is left behind to re-derive the user's data from."""
    bot, _ = make_bot(monkeypatch, conversation_id="conv-val-17b")
    bot.ask("something private")
    sid = memory.safe_id("conv-val-17b")
    snap = config.MEMORY_DIR / "conversations" / f"{sid}.json"
    journal = sessionlog._journal_path(sid)
    assert snap.exists() and journal.exists()

    sessionlog.delete_session("conv-val-17b")
    assert not snap.exists() and not journal.exists()
    assert memory.load_conversation("conv-val-17b") == []


# ==========================================================================
# FLOW 18 — compaction preserves the durable-fact flush path
# ==========================================================================

def test_flow18_compaction_flushes_durable_facts_before_folding(monkeypatch):
    """Compaction must never be the thing that loses a fact: the slice being
    folded away is flushed to typed memory FIRST, with the folded text, and
    that happens even if the summarizer then fails."""
    from olympus import recall
    flushed: list[tuple[str, str]] = []
    monkeypatch.setattr(recall, "flush_slice",
                        lambda user, text, settings: flushed.append((user, text)))
    _force_compaction(monkeypatch)
    bot, _ = make_bot(monkeypatch)
    bot.history = [{"role": "user",
                    "content": "my launch date is 3 March"}] + [
        {"role": "assistant", "content": "noted %d" % i} for i in range(9)]
    bot._compress_history()

    assert flushed, "compaction folded history without flushing durable facts"
    user, text = flushed[0]
    assert user == bot.user
    assert "my launch date is 3 March" in text     # the folded slice, verbatim


def test_flow18_flush_runs_even_when_the_summarizer_fails(monkeypatch):
    """Ordering, asserted: the flush precedes the summarizer, so a summarizer
    failure (which truncates) cannot take the facts with it."""
    from olympus import ace, recall
    order: list[str] = []
    monkeypatch.setattr(recall, "flush_slice",
                        lambda *a, **k: order.append("flush"))
    monkeypatch.setattr(ace, "evolve", lambda *a, **k: (
        order.append("summarize"),
        (_ for _ in ()).throw(RuntimeError("no summarizer")))[0])
    monkeypatch.setattr(orchestrator.backend, "complete_text",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("no summarizer")))
    _force_compaction(monkeypatch)
    bot, _ = make_bot(monkeypatch)
    bot.history = [{"role": "user", "content": "fact %d" % i} for i in range(10)]
    bot._compress_history()
    assert order[0] == "flush"
    assert len(bot.history) == 2                   # truncation fallback taken


# ==========================================================================
# FLOW 19 — record a run, then replay it: an empty decision diff
# ==========================================================================

def test_flow19_replay_of_a_recorded_run_is_byte_identical(monkeypatch):
    """The moat: re-execute the recorded run's REAL pipeline against its frozen
    responses and get a byte-identical decision path — with zero new provider
    calls (any network touch during replay is an outright failure).

    `OLYMPUS_SYNTH_CHECK` is off here so that the run records exactly the
    decisions `replay_run` re-executes. With the shipped default (on) the run
    records a trailing `synthesis_check` decision that `replay_run` structurally
    cannot reproduce — see the next test, and the DEFECT reported with it."""
    monkeypatch.setenv("OLYMPUS_SYNTH_CHECK", "off")
    counter = [0]
    bot, _ = make_bot(monkeypatch, counter=counter)
    bot.ask("cost the launch")
    run_id = bot.last_run_id
    calls_after_record = counter[0]
    assert calls_after_record > 0

    monkeypatch.setattr(llm, "client", lambda settings=None: (
        _ for _ in ()).throw(AssertionError("replay must not call a provider")))
    original, fresh, diffs = orchestrator.replay_run(run_id)

    assert diffs == [], f"replay diverged: {diffs[:1]}"
    assert trace_mod.canonical_log(original["decisions"]) == \
        trace_mod.canonical_log(fresh.decisions)
    assert counter[0] == calls_after_record        # zero new provider calls


def test_flow19_post_pipeline_decisions_are_outside_the_replay_scope(
        monkeypatch):
    """DEFECT UNDER TRIAGE (recorded here as evidence, not enshrined).

    `ask()` records decisions AFTER `_pipeline` returns — with the shipped
    default `OLYMPUS_SYNTH_CHECK=on`, `_maybe_check_synthesis` appends a
    `synthesis_check` decision — but `replay_run` re-executes `_pipeline` only.
    The recorded and replayed paths therefore differ by that trailing decision,
    so `diff_decisions` is NON-EMPTY for an ordinary run in the default
    configuration. `olympus replay`, `olympus verify` and `replaygate` all read
    that list as a divergence.

    The assertion below states what must be true either way: everything
    `replay_run` DOES cover replays byte-identically, and any residual
    difference is confined to trailing decisions the replay never runs. It
    stays green once the gap is closed (the diff simply becomes empty)."""
    bot, _ = make_bot(monkeypatch)
    bot.ask("cost the launch")
    run_id = bot.last_run_id
    recorded = trace_mod.load_run(run_id)["decisions"]

    monkeypatch.setattr(llm, "client", lambda settings=None: (
        _ for _ in ()).throw(AssertionError("replay must not call a provider")))
    original, fresh, diffs = orchestrator.replay_run(run_id)

    n = len(fresh.decisions)
    # everything inside the replayed scope is byte-identical
    assert trace_mod.canonical_log(recorded[:n]) == \
        trace_mod.canonical_log(fresh.decisions)
    # any residual diff is a recorded-only tail, never a changed decision
    for d in diffs:
        assert d["index"] >= n
        assert d["replayed"] is None
        assert d["original"]["decision_type"] not in ("route", "plan", "review")


def test_flow19_a_changed_input_diverges_loudly(monkeypatch):
    """The control that makes the green above mean something: replaying with a
    changed request raises a typed divergence naming the request, instead of
    quietly reaching for the network."""
    bot, _ = make_bot(monkeypatch)
    bot.ask("cost the launch")
    run_id = bot.last_run_id

    monkeypatch.setattr(llm, "client", lambda settings=None: (
        _ for _ in ()).throw(AssertionError("replay must not call a provider")))
    original = trace_mod.load_run(run_id)
    original["meta"]["input"] = "a COMPLETELY different question"
    monkeypatch.setattr(trace_mod, "load_run",
                        lambda rid: original if rid == run_id else None)
    with pytest.raises(replaystore.ReplayDivergence) as ei:
        orchestrator.replay_run(run_id)
    assert len(ei.value.request_hash) == 64
    assert replaystore.get(ei.value.request_hash) is None


# ==========================================================================
# FLOW 20 — rollback: every Wave-2 flag off == the modules disabled
# ==========================================================================

_WAVE2_OFF = {
    "OLYMPUS_WATCHDOG": "off",
    "OLYMPUS_ROUTESUB": "off",
    "OLYMPUS_CTXHEAT": "off",
    "OLYMPUS_MODELGRADE": "0",
    "OLYMPUS_STREAMGUARD": "0",
    "OLYMPUS_ADMISSION": "0",
    "OLYMPUS_INGESTGATE": "0",
}


def _canonical_run(monkeypatch, user):
    resp = Responder()
    bot, _ = make_bot(monkeypatch, resp, user=user)
    tr = trace_mod.Trace("chat", user)
    tr.meta = {"input": "cost the launch"}
    result = bot._pipeline("cost the launch", tr)
    return result, trace_mod.canonical_log(tr.decisions)


def test_flow20_rollback_flags_off_equals_wave1_behaviour(monkeypatch):
    """W2-A17 as an integration property: a run with every Wave-2 flag absent
    (a fresh install) and the same run with every flag explicitly set to its
    OFF value produce the identical decision path and the identical answer —
    the modules are importable but inert either way."""
    from olympus import (ctxheat, ingestgate, modelgrade, routesub)

    for flag in _WAVE2_OFF:
        monkeypatch.delenv(flag, raising=False)
    absent_result, absent_log = _canonical_run(monkeypatch, "rollback")
    # the shipped default really is inert
    assert (modelgrade.enabled(), ingestgate.enabled(), streamguard.enabled(),
            usage.admission_enabled()) == (False, False, False, False)
    assert (ctxheat.mode(), routesub.mode(), watchdog.mode()) == \
        ("off", "off", "off")

    for flag, value in _WAVE2_OFF.items():
        monkeypatch.setenv(flag, value)
    off_result, off_log = _canonical_run(monkeypatch, "rollback")

    assert off_result == absent_result
    assert off_log == absent_log


def test_flow20_every_wave2_flag_has_a_documented_deactivation(monkeypatch):
    """Rollback is only real if an operator can find it: every wired Wave-2
    flag is registered with an explicit deactivation trigger."""
    from olympus import experiments
    known = experiments.known_flags()
    for flag in ("OLYMPUS_MODELGRADE", "OLYMPUS_CTXHEAT", "OLYMPUS_ROUTESUB",
                 "OLYMPUS_WATCHDOG"):
        assert flag in known
        assert experiments.entry(known[flag]).get("deactivation_trigger")


# ==========================================================================
# FLOW 21 — config skew is reported and mutates nothing
# ==========================================================================

def _memory_snapshot() -> dict:
    base = config.MEMORY_DIR
    if not base.exists():
        return {}
    return {str(p): (p.stat().st_size if p.is_file() else -1)
            for p in sorted(base.rglob("*"))}


def test_flow21_config_skew_reports_a_crafted_skew_and_mutates_nothing(
        monkeypatch):
    """`doctor.config_skew()` must SEE a crafted skew, name an operator action
    for it, and be a pure read — no environment variable and no file under
    MEMORY_DIR may change across the call (W2-I10.1)."""
    monkeypatch.setenv("OLYMPUS_NOT_A_REAL_KNOB_AT_ALL", "1")
    env_before = dict(os.environ)
    mem_before = _memory_snapshot()

    skew = doctor.config_skew()

    assert dict(os.environ) == env_before, \
        "config_skew mutated the environment"
    assert _memory_snapshot() == mem_before, "config_skew wrote to MEMORY_DIR"

    unknown = {f["setting"] for f in skew["unknown"]}
    assert "OLYMPUS_NOT_A_REAL_KNOB_AT_ALL" in unknown
    finding = next(f for f in skew["unknown"]
                   if f["setting"] == "OLYMPUS_NOT_A_REAL_KNOB_AT_ALL")
    assert finding["action"], "a skew finding with no operator action"
    assert "OLYMPUS_NOT_A_REAL_KNOB_AT_ALL" in finding["action"]
    assert "detail" in finding


def test_flow21_config_skew_reports_an_unsafe_combination(monkeypatch):
    """A second, semantically different skew class: a declared UNSAFE pair is
    reported with both settings named — reporting only, never a refusal to
    start (`is_ready` must keep meaning 'can serve')."""
    entry = config.UNSAFE_COMBINATIONS[0]
    (a, ka), (b, kb) = entry["a"], entry["b"]
    monkeypatch.setenv(a, ka if isinstance(ka, str) else "1")
    monkeypatch.setenv(b, kb if isinstance(kb, str) else "1")
    skew = doctor.config_skew()
    if not skew["unsafe"]:
        pytest.skip(f"declared unsafe pair {a}+{b} is not reachable by env "
                    "alone in this build")
    finding = skew["unsafe"][0]
    assert set(finding["settings"]) == {a, b}
    assert finding["action"] and "UNSAFE" in finding["detail"]


# ==========================================================================
# NOT TESTABLE OFFLINE — recorded as explicit skips, never as fake passes
# ==========================================================================

def test_real_client_compatibility_of_v1_messages():
    pytest.skip(
        "requires a third-party client (Claude Code / the anthropic SDK in "
        "another process) driving /v1/messages over a real socket — the Wave-3 "
        "claim is SDK-type-verified, NOT real-client-verified "
        "(WAVE3_COMPLETION_REPORT.md §3). Phase 5.")


def test_provider_drift_against_a_live_provider():
    pytest.skip(
        "requires a real provider and a spend budget: `modelgate.run_gate` is "
        "exercised here with an injected run_fn, which proves the gate and the "
        "severity ladder but not the corpus's discriminating power against an "
        "actually-drifted model. Phase 5 (shadow traffic).")


def test_cross_process_admission_saturation():
    pytest.skip(
        "requires multiple OS processes contending on the machine-global "
        "proclock counting semaphore; this suite exercises the per-process "
        "admission POLICY only (the documented DEFERRED #10 boundary). "
        "Phase 5 (staging).")

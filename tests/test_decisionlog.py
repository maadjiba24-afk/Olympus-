"""The re-executable decision log (Task 1 of the moat plan).

What these tests prove — the whole point of the feature:

1. Every LLM request is hashed deterministically (canonical JSON, the
   server-allocated `container` dropped), so structurally-equal requests hash
   identically and a changed request hashes differently.
2. In replay mode (`OLYMPUS_REPLAY`), `llm.complete()` returns the frozen
   response with **zero** Anthropic API calls, and the re-run produces a
   **byte-identical** decision path (`trace.canonical_log`).
3. If a prompt/code change alters a request, replay raises `ReplayDivergence`
   naming that exact request — i.e. the log catches a changed decision.

Unlike state-replay, we re-run the real call path (`backend.complete_json`,
the same surface the orchestrator uses) against the frozen responses; the
decisions are recorded exactly as `orchestrator._pipeline` records them.
"""

import json
import types

import anthropic
import pytest

from olympus import backend, config, llm, replaystore, trace


# --- helpers -------------------------------------------------------------

def _message(text: str) -> anthropic.types.Message:
    """A minimal but real anthropic Message (round-trips through to_json)."""
    return anthropic.types.Message.model_validate({
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-test",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5,
                  "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0},
    })


class _FakeStream:
    def __init__(self, message):
        self._m = message

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self._m


class _FakeMessages:
    """Counts every streamed call so a replay that touches the network fails."""

    def __init__(self, counter, responder):
        self.counter = counter
        self.responder = responder

    def stream(self, **params):
        self.counter[0] += 1
        return _FakeStream(self.responder(params))


def _fake_client(counter, responder):
    msgs = _FakeMessages(counter, responder)
    client = types.SimpleNamespace(messages=msgs,
                                   beta=types.SimpleNamespace(messages=msgs))
    return client


_SETTINGS = config.Settings(provider="anthropic", model="claude-test")


def _mini_pipeline(tr: trace.Trace, user_text: str) -> None:
    """A faithful miniature of orchestrator._pipeline's *decision* wiring:
    route -> plan -> review, each through the real backend.complete_json and
    each recorded with trace.decision + the replaystore reference. The same
    code runs in record and replay; only OLYMPUS_REPLAY changes behaviour."""
    model = {"provider": "anthropic", "model": "claude-test", "version": None}
    schema = {"type": "object", "properties": {"k": {"type": "string"}},
              "required": ["k"]}

    route = backend.complete_json(_SETTINGS, "route-system",
                                  [{"role": "user", "content": user_text}],
                                  schema, effort="medium")
    route_rec = tr.decision("route", model, route, status="ok", model=model,
                            request_hash=replaystore.last_ref(),
                            response_ref=replaystore.last_ref())

    plan = backend.complete_json(_SETTINGS, "plan-system",
                                 [{"role": "user", "content": user_text}],
                                 schema)
    tr.decision("plan", model, plan, status="ok",
                parent_record_id=route_rec["record_id"],
                model=model, request_hash=replaystore.last_ref(),
                response_ref=replaystore.last_ref())

    review = backend.complete_json(_SETTINGS, "review-system",
                                   [{"role": "user", "content": user_text}],
                                   schema)
    tr.decision("review", model, review, status="ok", model=model,
                request_hash=replaystore.last_ref(),
                response_ref=replaystore.last_ref())


def _responder(params):
    """Deterministic 'model': answer keyed by the system prompt so each stage
    gets a distinct, schema-valid JSON response."""
    sysblk = params["system"][0]["text"]
    return _message(json.dumps({"k": sysblk.split("-")[0]}))


# --- 1. canonical hashing -------------------------------------------------

def test_container_is_excluded_from_hash():
    base = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    h1 = replaystore.request_hash({**base, "container": "ctr-aaa"})
    h2 = replaystore.request_hash({**base, "container": "ctr-bbb"})
    h3 = replaystore.request_hash(base)
    assert h1 == h2 == h3   # server-allocated container never affects the hash


def test_changed_request_changes_hash():
    a = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    b = {"model": "m", "messages": [{"role": "user", "content": "HELLO"}]}
    assert replaystore.request_hash(a) != replaystore.request_hash(b)


def test_hash_is_order_independent():
    a = {"model": "m", "max_tokens": 5}
    b = {"max_tokens": 5, "model": "m"}
    assert replaystore.request_hash(a) == replaystore.request_hash(b)


# --- 2. replay = zero API calls + byte-identical decision path ------------

def test_replay_makes_zero_api_calls_and_is_byte_identical(monkeypatch):
    counter = [0]
    monkeypatch.setattr(llm, "client",
                        lambda settings=None: _fake_client(counter, _responder))

    # Record pass: real calls, responses frozen to disk.
    rec = trace.Trace("chat", "shared")
    _mini_pipeline(rec, "do the thing")
    rec.flush()
    calls_after_record = counter[0]
    assert calls_after_record == 3            # route + plan + review
    original_log = trace.canonical_log(rec.decisions)

    # Replay pass: same code, OLYMPUS_REPLAY set. Any network touch must blow up.
    monkeypatch.setenv("OLYMPUS_REPLAY", rec.id)

    def _forbidden(settings=None):
        raise AssertionError("replay must not call the Anthropic API")

    monkeypatch.setattr(llm, "client", _forbidden)

    fresh = trace.Trace("replay", "shared")
    _mini_pipeline(fresh, "do the thing")

    assert counter[0] == calls_after_record   # ZERO new API calls during replay
    assert trace.canonical_log(fresh.decisions) == original_log   # byte-identical
    assert trace.diff_decisions(rec.decisions, fresh.decisions) == []


def test_replay_run_diffs_recorded_run(monkeypatch):
    """The orchestrator entry point: replay_run loads the recorded run, re-runs
    the pipeline against frozen responses, and reports an empty diff."""
    counter = [0]
    monkeypatch.setattr(llm, "client",
                        lambda settings=None: _fake_client(counter, _responder))

    rec = trace.Trace("chat", "shared")
    rec.meta = {"input": "do the thing"}
    _mini_pipeline(rec, "do the thing")
    rec.flush()

    monkeypatch.setenv("OLYMPUS_REPLAY", rec.id)
    monkeypatch.setattr(llm, "client", lambda settings=None: (_ for _ in ()).throw(
        AssertionError("network during replay")))

    loaded = trace.load_run(rec.id)
    assert loaded is not None
    fresh = trace.Trace("replay", "shared")
    _mini_pipeline(fresh, loaded["meta"]["input"])
    assert trace.diff_decisions(loaded["decisions"], fresh.decisions) == []


# --- 3. a changed request makes replay diverge, naming the record ---------

def test_mutated_prompt_raises_replay_divergence(monkeypatch):
    counter = [0]
    monkeypatch.setattr(llm, "client",
                        lambda settings=None: _fake_client(counter, _responder))

    rec = trace.Trace("chat", "shared")
    _mini_pipeline(rec, "original input")
    rec.flush()

    # The route stage's frozen request is for "original input". In replay, feed
    # a mutated input: its request hash isn't on disk -> ReplayDivergence.
    monkeypatch.setenv("OLYMPUS_REPLAY", rec.id)
    monkeypatch.setattr(llm, "client", lambda settings=None: (_ for _ in ()).throw(
        AssertionError("network during replay")))

    with pytest.raises(replaystore.ReplayDivergence) as ei:
        _mini_pipeline(trace.Trace("replay", "shared"), "MUTATED input")

    err = ei.value
    # The exception pins the exact request that changed since the recorded run.
    assert err.request_hash and len(err.request_hash) == 64
    assert "divergence" in str(err)
    # And it is genuinely a request that was never recorded.
    assert replaystore.get(err.request_hash) is None


def test_missing_response_is_divergence_not_silent(monkeypatch):
    """A request with no frozen response in replay mode must raise, never
    fabricate or fall through to the network."""
    monkeypatch.setenv("OLYMPUS_REPLAY", "some-run")
    monkeypatch.setattr(llm, "client", lambda settings=None: (_ for _ in ()).throw(
        AssertionError("must not hit network")))
    with pytest.raises(replaystore.ReplayDivergence):
        llm.complete("sys", [{"role": "user", "content": "never recorded"}],
                     settings=_SETTINGS)


# --- store round-trip -----------------------------------------------------

def test_put_get_roundtrips_message():
    msg = _message("frozen reasoning")
    h = "deadbeef" * 8
    replaystore.put(h, msg)
    got = replaystore.get(h)
    assert got is not None
    assert llm.text_of(got) == "frozen reasoning"
    assert replaystore.get("f" * 64) is None   # absent hash -> None


# --- 4. orphaned frozen responses are swept, recorded runs stay replayable -

def test_orphan_responses_are_swept_referenced_kept(monkeypatch):
    from olympus import memory
    counter = [0]
    monkeypatch.setattr(llm, "client",
                        lambda settings=None: _fake_client(counter, _responder))

    rec = trace.Trace("chat", "shared")
    _mini_pipeline(rec, "keep me")
    rec.flush()

    # An unreferenced frozen response (no trace points at it).
    replaystore.put("a" * 64, _message("orphan"))

    removed = memory.sweep_orphan_responses()
    assert removed == 1
    assert replaystore.get("a" * 64) is None
    # the run's own responses survive, so it stays fully re-executable
    for d in rec.decisions:
        assert replaystore.get(d["model_response_ref"]) is not None


# --- F4: status is mandatory in the decision log -------------------------

def test_decision_requires_status():
    tr = trace.Trace("ask", "shared")
    with pytest.raises(TypeError):
        tr.decision("route", {"name": "zeus"}, {"mode": "direct"})  # no status


def test_failed_decision_records_error_not_ok():
    tr = trace.Trace("ask", "shared")
    rec = tr.decision("verify", {"name": "aletheia"}, {"note": "blew up"},
                      status="error", error="boom")
    assert rec["outcome"] == {"status": "error", "error": "boom"}
    ok = tr.decision("route", {"name": "zeus"}, {"mode": "direct"}, status="ok")
    assert ok["outcome"]["status"] == "ok"

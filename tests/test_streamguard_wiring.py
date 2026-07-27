"""W2-PR14 — streamguard wired into the OpenAI-compatible and web seams.

Closes acceptance gate A12 (full). The Wave-2 completion report recorded C8 as
`⚠️ partial`, with one named gap:

    "streamguard event-order detectors are unreachable [from] the Anthropic
     streaming seam until W2-PR14"

— because `llm.stream_text` feeds `stream.text_stream`, which yields text and
nothing else. `detect_malformed_event`, `detect_event_order`,
`detect_invalid_tool_delta` and `detect_impossible_usage` therefore had no live
caller: they were tested only by calling them directly.

This suite asserts they are now reachable from a REAL seam. Every reachability
test drives a crafted provider PAYLOAD through the production entry points
(`openai_compat.complete_text` / `run_agent`, `web.Handler._guarded_pieces`)
and never calls a detector itself — a test that called the detector would prove
nothing about wiring, which is the mistake this PR exists to correct.

Two seams, and the honest division of labour between them:

* `openai_compat` is NOT streaming (`_post` json-parses the whole body), so
  there is no delta seam. The guard runs at the RESPONSE-PARSE point, over
  `response_events()` — a faithful projection of the body back into the
  canonical event sequence. This is where the four event/usage detectors
  become reachable.
* `web.py`'s two streaming handlers carry TEXT fragments, so the text detectors
  run there. They are the last seam before bytes reach a client and they see
  every provider, including the non-Anthropic branch of
  `orchestrator._synthesize_stream`, which has no provider-level guard.
"""
from __future__ import annotations

import json

import pytest

from olympus import config, openai_compat, streamguard, web


# --- helpers ---------------------------------------------------------------

def _on(monkeypatch):
    monkeypatch.setenv("OLYMPUS_STREAMGUARD", "1")


def _off(monkeypatch):
    monkeypatch.delenv("OLYMPUS_STREAMGUARD", raising=False)


def _settings():
    return config.Settings(provider="openai", model="gpt-test",
                           api_key="k", base_url="https://example.invalid/v1")


def _body(content="hello", *, tool_calls=None, usage=None, extra_message=None):
    """A well-formed /chat/completions body, optionally perturbed."""
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    if extra_message:
        message.update(extra_message)
    body = {"choices": [{"index": 0, "message": message,
                         "finish_reason": "stop"}]}
    if usage is not None:
        body["usage"] = usage
    return body


def _tool(name="echo", args='{"text": "hi"}', *, call_id="call_1", index=None):
    call = {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": args}}
    if index is not None:
        call["index"] = index
    return call


def _stub_post(monkeypatch, body, seen=None):
    def fake(settings, payload):
        if seen is not None:
            seen.append(payload)
        return body
    monkeypatch.setattr(openai_compat, "_post", fake)


def _ledger():
    return config.MEMORY_DIR / "streamguard" / "pathologies.jsonl"


def _records():
    path = _ledger()
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


class _Pieces:
    """A stand-in for `bot.ask_stream` that also records what was consumed."""

    def __init__(self, pieces):
        self.pieces = list(pieces)
        self.consumed: list[str] = []

    def __iter__(self):
        for piece in self.pieces:
            self.consumed.append(piece)
            yield piece


def _drain(pieces, *, model="council"):
    """Run the production web guard wrapper without an HTTP server."""
    return list(web.Handler._guarded_pieces(web.Handler, pieces, model=model))


# --- 1. flag-off byte-identity, per wired path -----------------------------

def test_flag_off_openai_compat_complete_text_is_byte_identical(monkeypatch):
    """The response-parse guard must not alter a single byte with the flag off
    — including for a body that WOULD trip it (A17 rollback)."""
    _off(monkeypatch)
    hostile = _body("x", tool_calls=[_tool(args=12345)],
                    usage={"prompt_tokens": 10, "completion_tokens": -5,
                           "prompt_tokens_details": {"cached_tokens": 900}})
    _stub_post(monkeypatch, hostile)
    assert openai_compat.complete_text(_settings(), "sys", []) == "x"
    assert _records() == []


def test_flag_off_run_agent_is_byte_identical_and_still_executes(monkeypatch):
    """Flag off ⇒ run_agent behaves exactly as before, tool call and all."""
    _off(monkeypatch)
    calls = []
    monkeypatch.setattr(openai_compat.tools, "resolve_handler",
                        lambda name: (lambda **kw: calls.append(kw) or "ok"))
    replies = [_body(None, tool_calls=[_tool()]), _body("done")]
    monkeypatch.setattr(openai_compat, "_post",
                        lambda s, p: replies.pop(0))
    assert openai_compat.run_agent(_settings(), "sys", "task", None) == "done"
    assert calls == [{"text": "hi"}]         # the tool really ran


def test_flag_off_web_stream_is_byte_identical(monkeypatch):
    """`_guarded_pieces` with the flag off yields exactly its input, in order —
    including a stream that WOULD trip the guard when it is on."""
    _off(monkeypatch)
    degenerate = ["all work and no play makes jack " * 4] * 40
    assert _drain(_Pieces(degenerate)) == degenerate
    assert _records() == []


def test_flag_off_projection_is_not_even_walked(monkeypatch):
    """Flag off must cost nothing: `_guard_response` returns the NullMonitor
    before projecting, so a body that cannot be projected is still fine."""
    _off(monkeypatch)
    sentinel = object()
    monkeypatch.setattr(openai_compat, "response_events",
                        lambda *a, **k: pytest.fail("projected while off"))
    guard = openai_compat._guard_response(_settings(), {"model": "m"}, sentinel)
    assert isinstance(guard, streamguard.NullMonitor)


# --- 2. previously-unreachable EVENT detectors, reached from the wire ------

def test_invalid_tool_delta_is_reachable_from_a_crafted_payload(monkeypatch):
    """`detect_invalid_tool_delta`, arm 1: an argument buffer that is not a
    string. Driven through `complete_text`, never called directly."""
    _on(monkeypatch)
    _stub_post(monkeypatch, _body("hi", tool_calls=[_tool(args=[1, 2, 3])]))
    with pytest.raises(streamguard.StreamAborted) as excinfo:
        openai_compat.complete_text(_settings(), "sys", [])
    assert excinfo.value.kind == streamguard.INVALID_TOOL_DELTA
    assert "not str" in excinfo.value.detail


def test_tool_id_changed_mid_block_is_reachable(monkeypatch):
    """`detect_invalid_tool_delta`, arm 2: two DIFFERENT call ids interleaved
    into one `index` — i.e. one argument buffer — which is how a provider
    synthesizes a call nobody asked for. The projection groups parallel calls
    by `index` exactly as the OpenAI protocol does, so the payload alone
    reaches the detector."""
    _on(monkeypatch)
    _stub_post(monkeypatch, _body("hi", tool_calls=[
        _tool(call_id="call_a", index=0),
        _tool(call_id="call_b", index=0),
    ]))
    with pytest.raises(streamguard.StreamAborted) as excinfo:
        openai_compat.complete_text(_settings(), "sys", [])
    assert excinfo.value.kind == streamguard.INVALID_TOOL_DELTA
    assert "tool id changed mid-block" in excinfo.value.detail


def test_event_order_is_reachable_from_a_crafted_payload(monkeypatch):
    """`detect_event_order`: a tool RESULT echoed back as the model's own
    message (a broken relay). The body literally asserts a tool_result with no
    tool_use before it, so the state machine rejects it."""
    _on(monkeypatch)
    _stub_post(monkeypatch, _body(
        "hi", extra_message={"tool_call_id": "call_zzz"}))
    with pytest.raises(streamguard.StreamAborted) as excinfo:
        openai_compat.complete_text(_settings(), "sys", [])
    assert excinfo.value.kind == streamguard.EVENT_ORDER
    assert "tool_result before any tool_use" in excinfo.value.detail


def test_malformed_event_is_reachable_from_a_crafted_payload(monkeypatch):
    """`detect_malformed_event`: `content` that is not a string (a provider
    returning content parts where a string belongs). Today that crashes
    `complete_text` with an AttributeError; now it is a typed pathology."""
    _on(monkeypatch)
    _stub_post(monkeypatch, _body([{"type": "text", "text": "hi"}]))
    with pytest.raises(streamguard.StreamAborted) as excinfo:
        openai_compat.complete_text(_settings(), "sys", [])
    assert excinfo.value.kind == streamguard.MALFORMED_EVENTS


def test_impossible_usage_is_reachable_from_a_crafted_payload(monkeypatch):
    """`detect_impossible_usage`: cached input exceeding the provider's own
    declared input total. Reached through the projected `message_delta`."""
    _on(monkeypatch)
    _stub_post(monkeypatch, _body("hi", usage={
        "prompt_tokens": 100, "completion_tokens": 5,
        "prompt_tokens_details": {"cached_tokens": 4000}}))
    with pytest.raises(streamguard.StreamAborted) as excinfo:
        openai_compat.complete_text(_settings(), "sys", [])
    assert excinfo.value.kind == streamguard.IMPOSSIBLE_USAGE
    assert "cache_read" in excinfo.value.detail


def test_negative_usage_counter_is_reachable(monkeypatch):
    _on(monkeypatch)
    _stub_post(monkeypatch, _body("hi", usage={"prompt_tokens": 10,
                                               "completion_tokens": -1}))
    with pytest.raises(streamguard.StreamAborted) as excinfo:
        openai_compat.complete_text(_settings(), "sys", [])
    assert excinfo.value.kind == streamguard.IMPOSSIBLE_USAGE


def test_all_four_named_detectors_are_reachable_from_this_seam(monkeypatch):
    """The completion report's gap, stated as one assertion: every detector it
    listed as unreachable now trips from a payload alone."""
    _on(monkeypatch)
    cases = {
        streamguard.MALFORMED_EVENTS: _body(42),
        streamguard.EVENT_ORDER: _body("hi",
                                       extra_message={"role": "tool"}),
        streamguard.INVALID_TOOL_DELTA: _body("hi",
                                              tool_calls=[_tool(args=7)]),
        streamguard.IMPOSSIBLE_USAGE: _body(
            "hi", usage={"prompt_tokens": -3, "completion_tokens": 1}),
    }
    seen = set()
    for expected, body in cases.items():
        _stub_post(monkeypatch, body)
        with pytest.raises(streamguard.StreamAborted) as excinfo:
            openai_compat.complete_text(_settings(), "sys", [])
        seen.add(excinfo.value.kind)
        assert excinfo.value.kind == expected
    assert seen == set(cases)


def test_projection_is_pure(monkeypatch):
    """W2-I8.2 holds for the new projection too: `response_events` is a pure
    generator — no I/O, no clock — so it stays testable in isolation."""
    _on(monkeypatch)
    body = _body("hi", tool_calls=[_tool()], usage={"prompt_tokens": 1})
    first = [dict(e) if isinstance(e, dict) else e
             for e in openai_compat.response_events(body)]
    second = [dict(e) if isinstance(e, dict) else e
              for e in openai_compat.response_events(body)]
    assert first == second
    assert [e["type"] for e in first][:2] == ["message_start",
                                              "content_block_start"]
    assert first[-1]["type"] == "message_stop"
    assert not _ledger().exists()          # projecting writes nothing


# --- 3. a tripping stream aborts with disclosure and writes evidence -------

def test_web_stream_trip_discloses_and_records_evidence(monkeypatch):
    """W2-I8.3 at the egress seam: the tripping fragment is never written, the
    client receives an explicit unfinished-answer notice instead, and the
    pathology reaches the ledger as provider-decay evidence."""
    _on(monkeypatch)
    good = "Here is the beginning of a real answer. "
    loop = "and then the same thing again forever " * 30
    pieces = _Pieces([good, loop, "THIS MUST NEVER BE SENT"])
    out = _drain(pieces)

    assert out[0] == good
    assert loop not in out                  # the offending fragment is dropped
    assert "THIS MUST NEVER BE SENT" not in out
    assert "Response incomplete" in out[-1]
    assert "unfinished and unverified" in out[-1]
    assert "degenerate-stream guard" in out[-1]

    rows = _records()
    assert len(rows) == 1
    assert rows[0]["kind"] == streamguard.TOKEN_LOOP
    assert rows[0]["provider"] == "olympus-web"
    assert rows[0]["stats"]["deltas"] >= 2


def test_web_stream_stops_consuming_the_generator_on_a_trip(monkeypatch):
    """"Abort safely" means the upstream generator is abandoned, not drained —
    a degenerate provider must not keep being paid for."""
    _on(monkeypatch)
    pieces = _Pieces([" " * 9000, "never reached", "nor this"])
    _drain(pieces)
    assert pieces.consumed == [" " * 9000]


def test_v1_sse_envelope_stays_wellformed_through_an_abort(monkeypatch):
    """The disclosure rides as an ordinary content delta, so the stop frame and
    the `[DONE]` terminator still close the SSE stream: no client sees a
    half-written event, and none sees a silently short answer either."""
    from olympus import openai_server
    _on(monkeypatch)
    pieces = _Pieces(["ok. ", "loop loop loop loop " * 40])
    frames = list(openai_server.stream_events(
        _drain(pieces, model="olympus-council"), "olympus-council"))
    assert frames[-1] == "data: [DONE]\n\n"
    blob = "".join(frames)
    assert "Response incomplete" in blob
    assert '"finish_reason": "stop"' in blob or "finish_reason" in blob


def test_openai_compat_trip_records_evidence_and_raises_typed(monkeypatch):
    """The response-parse seam: a typed StreamAborted (never the text), plus an
    evidence record naming the provider and the pathology."""
    _on(monkeypatch)
    _stub_post(monkeypatch, _body("hi", tool_calls=[_tool(args=7)]))
    with pytest.raises(streamguard.StreamPathology) as excinfo:
        openai_compat.complete_text(_settings(), "sys", [])
    assert isinstance(excinfo.value, streamguard.StreamAborted)
    assert excinfo.value.record["kind"] == streamguard.INVALID_TOOL_DELTA
    rows = _records()
    assert len(rows) == 1
    assert rows[0]["provider"] == "openai-compat"
    assert rows[0]["model"] == "gpt-test"


def test_run_agent_trip_returns_a_disclosed_structured_failure(monkeypatch):
    """`run_agent` returns a string by contract, so its structured failure is a
    DISCLOSURE — it names the pathology and says nothing above is finished. It
    is never the model's half-formed text passed off as an answer."""
    _on(monkeypatch)
    _stub_post(monkeypatch, _body("partial thoughts",
                                  tool_calls=[_tool(args=7)]))
    monkeypatch.setattr(openai_compat.tools, "resolve_handler",
                        lambda name: (lambda **kw: "ok"))
    out = openai_compat.run_agent(_settings(), "sys", "task",
                                  [{"name": "echo", "input_schema": {}}])
    assert out.startswith("[Response aborted:")
    assert streamguard.INVALID_TOOL_DELTA in out
    assert "No tool call from this response was executed" in out
    assert "partial thoughts" not in out


# --- 4. W2-I8.1 — a partial tool call in flight is never executed ----------

def test_partial_tool_call_in_flight_is_not_executed(monkeypatch):
    """The invariant, at the seam: a response whose tool block trips the guard
    reaches NO handler. The guard runs before `repair_arguments` is even
    consulted, so the repair ladder cannot close the braces on a call the guard
    rejected."""
    _on(monkeypatch)
    executed = []

    def handler(**kwargs):
        executed.append(kwargs)
        return "ran"

    monkeypatch.setattr(openai_compat.tools, "resolve_handler",
                        lambda name: handler)
    _stub_post(monkeypatch, _body(None, tool_calls=[
        _tool(name="safe_one", args='{"text": "complete"}', call_id="a",
              index=0),
        _tool(name="in_flight", args='{"text": "trunc', call_id="b", index=0),
    ]))
    out = openai_compat.run_agent(_settings(), "sys", "task",
                                  [{"name": "safe_one", "input_schema": {}},
                                   {"name": "in_flight", "input_schema": {}}])
    assert executed == []                  # W2-I8.1: nothing ran
    assert out.startswith("[Response aborted:")


def test_safe_tool_calls_excludes_the_call_in_flight(monkeypatch):
    """`safe_tool_calls()` is the authority the evidence record uses: the calls
    completed BEFORE the trip are named, the one in flight is absent."""
    _on(monkeypatch)
    _stub_post(monkeypatch, _body(None, tool_calls=[
        _tool(name="finished", args='{"text": "ok"}', call_id="a", index=0),
        _tool(name="doomed", args=99, call_id="b", index=1),
    ]))
    with pytest.raises(streamguard.StreamAborted) as excinfo:
        openai_compat.complete_text(_settings(), "sys", [])
    names = excinfo.value.record["safe_tool_calls"]
    assert "finished" in names
    assert "doomed" not in names
    assert excinfo.value.record["stats"]["tool_call_in_flight"] is True


def test_disclosure_names_what_was_discarded(monkeypatch):
    """The disclosure is honest about the completed calls it threw away — a
    discard, not a deferral."""
    _on(monkeypatch)
    _stub_post(monkeypatch, _body(None, tool_calls=[
        _tool(name="finished", args='{"text": "ok"}', call_id="a", index=0),
        _tool(name="doomed", args=99, call_id="b", index=1),
    ]))
    monkeypatch.setattr(openai_compat.tools, "resolve_handler",
                        lambda name: (lambda **kw: pytest.fail("executed")))
    out = openai_compat.run_agent(_settings(), "sys", "task",
                                  [{"name": "finished", "input_schema": {}}])
    assert "finished" in out
    assert "discarded" in out


# --- 5. false-positive guard ----------------------------------------------

def test_long_legitimate_web_stream_does_not_trip(monkeypatch):
    """A long, slow, varied answer streams through untouched with the guard
    ON — the wiring must not turn a healthy stream into a failure."""
    _on(monkeypatch)
    pieces = [f"Paragraph {i}: a distinct sentence about topic {i}. "
              for i in range(800)]
    out = _drain(_Pieces(pieces))
    assert out == pieces
    assert _records() == []


def test_legitimate_code_answer_with_repeated_structure_does_not_trip(
        monkeypatch):
    """Real code and banners repeat — closing braces, rules, blank lines. The
    low-alphabet and whitespace guards must keep them out of TOKEN_LOOP."""
    _on(monkeypatch)
    body = "".join(f"    self.field_{i} = value_{i}\n" for i in range(200))
    pieces = ["```python\n", "class Thing:\n", body, "    }\n" * 30,
              "-" * 500 + "\n", "```"]
    assert _drain(_Pieces(pieces)) == pieces
    assert _records() == []


def test_large_legitimate_tool_call_does_not_trip(monkeypatch):
    """A big, healthy tool call (a 40 KB argument object, parallel calls with
    distinct ids) walks the projection cleanly and still executes."""
    _on(monkeypatch)
    big = {"rows": [{"i": i, "note": f"row number {i} of a real payload"}
                    for i in range(600)]}
    executed = []
    monkeypatch.setattr(openai_compat.tools, "resolve_handler",
                        lambda name: (lambda **kw: executed.append(name)
                                      or "ok"))
    replies = [
        _body(None, tool_calls=[
            _tool(name="bulk", args=json.dumps(big), call_id="a", index=0),
            _tool(name="echo", args='{"text": "hi"}', call_id="b", index=1),
        ]),
        _body("all done"),
    ]
    monkeypatch.setattr(openai_compat, "_post", lambda s, p: replies.pop(0))
    out = openai_compat.run_agent(
        _settings(), "sys", "task",
        [{"name": "bulk", "input_schema": {}},
         {"name": "echo", "input_schema": {}}])
    assert out == "all done"
    assert executed == ["bulk", "echo"]
    assert _records() == []


def test_healthy_response_leaves_the_repair_ladder_its_job(monkeypatch):
    """With the guard ON, a merely UNPARSEABLE argument buffer on an otherwise
    healthy response is still the repair ladder's business, not a pathology —
    the guard must not quietly take over (and drop) calls it was never meant
    to adjudicate."""
    _on(monkeypatch)
    executed = []
    monkeypatch.setattr(openai_compat.tools, "resolve_handler",
                        lambda name: (lambda **kw: executed.append(kw) or "ok"))
    replies = [_body(None, tool_calls=[
        _tool(args='```json\n{"text": "fenced"}\n```')]), _body("fine")]
    monkeypatch.setattr(openai_compat, "_post", lambda s, p: replies.pop(0))
    assert openai_compat.run_agent(_settings(), "sys", "task", None) == "fine"
    assert executed == [{"text": "fenced"}]
    assert _records() == []


def test_dict_arguments_are_not_flagged(monkeypatch):
    """`repair_arguments` accepts a dict `arguments` as a supported provider
    variant, so the projection encodes it rather than calling it invalid —
    otherwise the guard would reject calls this backend has always run."""
    _on(monkeypatch)
    executed = []
    monkeypatch.setattr(openai_compat.tools, "resolve_handler",
                        lambda name: (lambda **kw: executed.append(kw) or "ok"))
    replies = [_body(None, tool_calls=[_tool(args={"text": "structured"})]),
               _body("fine")]
    monkeypatch.setattr(openai_compat, "_post", lambda s, p: replies.pop(0))
    assert openai_compat.run_agent(_settings(), "sys", "task", None) == "fine"
    assert executed == [{"text": "structured"}]


def test_ordinary_usage_and_streaming_style_tool_calls_do_not_trip(monkeypatch):
    """Anthropic-style usage (cache_read legitimately above the UNCACHED
    `input_tokens`) and a continuation entry with no id must both pass."""
    _on(monkeypatch)
    _stub_post(monkeypatch, _body("fine", tool_calls=[
        _tool(name="echo", args='{"text": ', call_id="a", index=0),
        {"index": 0, "function": {"arguments": '"hi"}'}},
    ], usage={"input_tokens": 12, "cache_read_input_tokens": 9000,
              "output_tokens": 5}))
    assert openai_compat.complete_text(_settings(), "sys", []) == "fine"
    assert _records() == []

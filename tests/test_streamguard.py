"""Degenerate-stream defense (Wave-2 capability C8) — acceptance gate W2-A12.

What these tests prove:

1. Each of the ten pathologies trips with its own kind: token loop, empty
   progress, whitespace flood, partial-JSON loop, invalid Unicode, malformed
   events, event-order violations, invalid tool deltas, impossible usage
   counters, output past the declared bound.
2. Chunk-boundary safety: a pathology sliced arbitrarily across deltas is
   caught exactly as an unsliced one; a legitimate stream sliced oddly (one
   char per delta) is not.
3. False-positive guards: long legitimate code (walls of closing braces,
   rules, banners) does not trip the loop detector, a slow stream of a few
   deltas does not trip empty-progress, and a large healthy tool call does not
   trip the partial-JSON detector.
4. W2-I8.1: `safe_tool_calls()` excludes the call in flight when the trip
   happens — a partial tool call can never reach the repair ladder.
5. W2-I8.2: `feed()` is side-effect-free — a monitor driven all the way to a
   trip writes nothing to MEMORY_DIR; the detectors are pure functions.
6. W2-I8.3: through `llm.stream_text`, a tripped stream raises a typed
   `StreamAborted` (never a silently truncated answer) and leaves a pathology
   record; with the flag off the same stream is byte-identical to the pre-C8
   path and nothing is recorded.
7. Evidence writing never raises — a broken proclock loses the record, not the
   request.
"""

import json
import types

import anthropic
import pytest

from olympus import config, llm, streamguard


# --- helpers ---------------------------------------------------------------

def _mon(**kw) -> streamguard.StreamMonitor:
    """A real monitor regardless of the (default-off) flag — the detector
    tests exercise the object directly."""
    return streamguard.StreamMonitor(provider="anthropic", model="m", **kw)


def _trip(fn) -> streamguard.StreamPathology:
    with pytest.raises(streamguard.StreamPathology) as excinfo:
        fn()
    return excinfo.value


def _feed_all(mon, items) -> None:
    for item in items:
        mon.feed(item)


def _tool_start(tool_id="toolu_1", name="write"):
    return {"type": "content_block_start",
            "content_block": {"type": "tool_use", "id": tool_id, "name": name}}


def _args(payload, **extra):
    delta = {"type": "input_json_delta", "partial_json": payload}
    delta.update(extra)
    return {"type": "content_block_delta", "delta": delta}


# A fake Anthropic streaming client, mirroring tests/test_usage_cache.py.

def _message(text="ok", out_toks=5):
    return anthropic.types.Message.model_validate({
        "id": "msg_test", "type": "message", "role": "assistant",
        "model": "claude-test",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": out_toks,
                  "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0},
    })


class _FakeStream:
    def __init__(self, chunks, message):
        self._chunks = chunks
        self._m = message

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def text_stream(self):
        return iter(self._chunks)

    def get_final_message(self):
        return self._m


def _fake_client(chunks, message=None):
    msg = message or _message()
    msgs = types.SimpleNamespace(stream=lambda **p: _FakeStream(chunks, msg))
    return types.SimpleNamespace(messages=msgs,
                                 beta=types.SimpleNamespace(messages=msgs))


_SETTINGS = config.Settings(provider="anthropic", model="claude-test")


def _ledger_path():
    return config.MEMORY_DIR / "streamguard" / "pathologies.jsonl"


def _records():
    path = _ledger_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


# --- 1. one test per pathology ---------------------------------------------

def test_token_loop_trips():
    mon = _mon()
    exc = _trip(lambda: _feed_all(mon, ["I am sorry, I cannot "] * 40))
    assert exc.kind == streamguard.TOKEN_LOOP
    assert "repeated" in exc.detail


def test_empty_progress_trips():
    mon = _mon()
    exc = _trip(lambda: _feed_all(mon, [""] * 260))
    assert exc.kind == streamguard.EMPTY_PROGRESS
    assert mon.empty_run > 200


def test_whitespace_flood_trips():
    mon = _mon()
    exc = _trip(lambda: mon.feed(" " * 5000 + "x"))
    assert exc.kind == streamguard.WHITESPACE_FLOOD


def test_partial_json_loop_trips():
    """Tool arguments that grow forever without ever becoming closable."""
    mon = _mon()
    events = [{"type": "message_start"}, _tool_start()]
    events += [_args(f"tok{i} ") for i in range(60)]
    exc = _trip(lambda: _feed_all(mon, events))
    assert exc.kind == streamguard.PARTIAL_JSON_LOOP


def test_invalid_unicode_trips_on_unpaired_surrogate():
    mon = _mon()
    exc = _trip(lambda: mon.feed("hello \ud800 world"))
    assert exc.kind == streamguard.INVALID_UNICODE


def test_invalid_unicode_trips_on_undecodable_bytes():
    mon = _mon()
    exc = _trip(lambda: mon.feed(b"\xff\xfe ok"))
    assert exc.kind == streamguard.INVALID_UNICODE


def test_malformed_events_trip():
    assert _trip(lambda: _mon().feed(["not", "a", "mapping"])).kind == \
        streamguard.MALFORMED_EVENTS
    assert _trip(lambda: _mon().feed({"foo": 1})).kind == \
        streamguard.MALFORMED_EVENTS
    assert _trip(lambda: _mon().feed({"type": "wat"})).kind == \
        streamguard.MALFORMED_EVENTS
    # a delta event with no delta mapping is shape-broken, not order-broken
    mon = _mon()
    mon.feed({"type": "message_start"})
    mon.feed({"type": "content_block_start", "content_block": {"type": "text"}})
    assert _trip(lambda: mon.feed({"type": "content_block_delta"})).kind == \
        streamguard.MALFORMED_EVENTS


def test_event_order_trips():
    # a content_block_delta before any content_block_start
    delta = {"type": "content_block_delta",
             "delta": {"type": "text_delta", "text": "x"}}
    assert _trip(lambda: _mon().feed(delta)).kind == streamguard.EVENT_ORDER

    # stop after stop
    mon = _mon()
    mon.feed({"type": "message_start"})
    mon.feed({"type": "message_stop"})
    assert _trip(lambda: mon.feed({"type": "message_stop"})).kind == \
        streamguard.EVENT_ORDER

    # a tool result before any tool use
    mon2 = _mon()
    mon2.feed({"type": "message_start"})
    assert _trip(lambda: mon2.feed({"type": "tool_result"})).kind == \
        streamguard.EVENT_ORDER


def test_invalid_tool_delta_trips():
    # arguments delta that is not a string
    mon = _mon()
    exc = _trip(lambda: _feed_all(mon, [{"type": "message_start"},
                                        _tool_start(),
                                        _args({"already": "parsed"})]))
    assert exc.kind == streamguard.INVALID_TOOL_DELTA

    # tool id changing mid-block
    mon2 = _mon()
    exc2 = _trip(lambda: _feed_all(mon2, [
        {"type": "message_start"}, _tool_start("toolu_1"),
        _args('{"a":', id="toolu_2")]))
    assert exc2.kind == streamguard.INVALID_TOOL_DELTA
    assert "changed mid-block" in exc2.detail


def test_impossible_usage_trips():
    assert _trip(lambda: _mon().feed_usage({"input_tokens": -3})).kind == \
        streamguard.IMPOSSIBLE_USAGE
    mon = _mon(max_tokens=100)
    assert _trip(lambda: mon.feed_usage({"output_tokens": 4000})).kind == \
        streamguard.IMPOSSIBLE_USAGE
    # OpenAI-style: cached input larger than the declared input total
    bad = {"prompt_tokens": 10, "completion_tokens": 1,
           "prompt_tokens_details": {"cached_tokens": 99}}
    assert _trip(lambda: _mon().feed_usage(bad)).kind == \
        streamguard.IMPOSSIBLE_USAGE


def test_impossible_usage_accepts_anthropic_cache_split():
    """Anthropic's input_tokens is the UNCACHED remainder, so cache_read
    legitimately exceeds it — that must NOT be called impossible."""
    mon = _mon(max_tokens=1000)
    mon.feed_usage({"input_tokens": 10, "output_tokens": 5,
                    "cache_read_input_tokens": 4000,
                    "cache_creation_input_tokens": 0})


def test_output_bounds_trips():
    mon = _mon(max_tokens=10)          # bound = 10 * 16 chars
    exc = _trip(lambda: _feed_all(mon, ["chunk of prose. "] * 40))
    assert exc.kind == streamguard.OUTPUT_BOUNDS


def test_every_kind_has_a_test():
    """The pathology list is the binding contract — keep the kinds and the
    detectors in step (a new kind with no detector must fail here)."""
    assert len(streamguard.KINDS) == 10
    assert len(set(streamguard.KINDS)) == 10


# --- 2. chunk-boundary safety ----------------------------------------------

def test_pathology_split_across_deltas_is_still_caught():
    """The same loop, sliced one character per delta, trips identically."""
    whole = _mon()
    exc_whole = _trip(lambda: _feed_all(whole, ["the same thing over. "] * 40))

    sliced = _mon()
    text = "the same thing over. " * 40

    def drive():
        for ch in text:
            sliced.feed(ch)

    exc_sliced = _trip(drive)
    assert exc_whole.kind == exc_sliced.kind == streamguard.TOKEN_LOOP


def test_whitespace_flood_split_across_deltas_is_caught():
    mon = _mon()
    exc = _trip(lambda: _feed_all(mon, [" " * 100] * 60))
    assert exc.kind == streamguard.WHITESPACE_FLOOD


def test_legitimate_stream_split_oddly_does_not_trip():
    """A real answer delivered one character at a time is not a pathology."""
    prose = ("Here is a short explanation of the algorithm. It walks the "
             "tree depth-first, memoising each node, and returns the total "
             "in O(n) time. Edge cases: empty input, cycles, and very deep "
             "recursion (guarded by an explicit stack).\n\n"
             "```python\ndef total(node, seen=None):\n"
             "    seen = seen or set()\n"
             "    if node.id in seen:\n"
             "        return 0\n"
             "    seen.add(node.id)\n"
             "    return node.value + sum(total(c, seen) for c in node.kids)\n"
             "```\n")
    mon = _mon(max_tokens=8000)
    for ch in prose:
        mon.feed(ch)
    assert mon.chars == len(prose)
    assert mon.tripped is None


# --- 3. false-positive guards ----------------------------------------------

def test_repeated_braces_in_long_code_do_not_trip():
    """Walls of closing braces, indentation, rules and banners are what real
    code output looks like — none of it is a repetition loop."""
    code = (
        "".join("        }\n" for _ in range(40))       # a wall of braces
        + "=" * 80 + "\n"                               # banner rule
        + "-" * 200 + "\n"                              # a very long rule
        + "".join(f"    - item {i}\n" for i in range(60))
        + "".join(f"            );  // step {i}\n" for i in range(30))
        + "".join("    " * 4 + f"value_{i} = compute({i})\n" for i in range(40))
    )
    mon = _mon(max_tokens=40_000)
    for i in range(0, len(code), 7):       # awkward, non-aligned slicing
        mon.feed(code[i:i + 7])
    assert mon.tripped is None


def test_slow_stream_with_few_deltas_does_not_trip_empty_progress():
    """A slow model that sends a handful of deltas (some of them pure
    whitespace) is slow, not stalled."""
    mon = _mon(max_tokens=8000)
    for i in range(20):
        mon.feed("\n\n")                   # whitespace-only, but only 20 of them
        mon.feed(f"paragraph {i} of a slowly produced answer. ")
    assert mon.tripped is None
    assert mon.empty_run == 0


def test_large_healthy_tool_call_does_not_trip_partial_json():
    """Hundreds of deltas of a real tool call: every prefix is completable, so
    PARTIAL_JSON_LOOP must stay silent."""
    body = json.dumps({"path": "/srv/app/main.py",
                       "content": "".join(
                           f"def step_{i}(x):\n    return x * {i}\n\n"
                           for i in range(40)),
                       "count": 12345, "overwrite": True})
    mon = _mon(max_tokens=40_000)
    mon.feed({"type": "message_start"})
    mon.feed(_tool_start())
    for i in range(0, len(body), 3):
        mon.feed(_args(body[i:i + 3]))
    mon.feed({"type": "content_block_stop"})
    mon.feed({"type": "message_stop"})
    assert mon.tripped is None
    calls = mon.safe_tool_calls()
    assert [c["name"] for c in calls] == ["write"]
    assert calls[0]["input"]["path"] == "/srv/app/main.py"


def test_json_prefix_viability_is_true_for_every_prefix_of_real_args():
    body = json.dumps({"path": "/a/b.py", "content": "x\n\"q\"\\ é\n" * 5,
                       "n": 42, "flag": False, "list": [1, 2, {"k": "v"}]})
    bad = [i for i in range(1, len(body) + 1)
           if not streamguard.json_prefix_viable(body[:i])]
    assert bad == []
    assert not streamguard.json_prefix_viable('{"a": 1}{"a": 1}')
    assert not streamguard.json_prefix_viable("tok1 tok2 tok3")


# --- 4. W2-I8.1 no partial tool call escapes -------------------------------

def test_safe_tool_calls_excludes_the_call_in_flight():
    """One complete call, then a second call that trips mid-arguments: only
    the complete one is returned, and the partial buffer is nowhere in it."""
    mon = _mon(max_tokens=40_000)
    mon.feed({"type": "message_start"})
    mon.feed(_tool_start("toolu_1", "read_file"))
    for piece in ('{"path"', ': "/etc/', 'hosts"}'):
        mon.feed(_args(piece))
    mon.feed({"type": "content_block_stop"})

    mon.feed(_tool_start("toolu_2", "delete_everything"))
    mon.feed(_args('{"target": "/"'))      # in flight — never completed

    exc = _trip(lambda: _feed_all(
        mon, [_args(f"garbage{i} ") for i in range(60)]))
    assert exc.kind == streamguard.PARTIAL_JSON_LOOP

    safe = mon.safe_tool_calls()
    assert [c["name"] for c in safe] == ["read_file"]
    assert safe[0]["input"] == {"path": "/etc/hosts"}
    # the partial call must be absent in every form
    blob = json.dumps(safe)
    assert "delete_everything" not in blob
    assert "toolu_2" not in blob
    assert "target" not in blob


def test_unparseable_closed_tool_call_is_not_promoted():
    """A tool block that closes with unparseable arguments is dropped here —
    repair is the ladder's job on a healthy stream, never streamguard's."""
    mon = _mon(max_tokens=40_000)
    mon.feed({"type": "message_start"})
    mon.feed(_tool_start("toolu_9", "rm"))
    mon.feed(_args('{"path": "/tmp'))
    mon.feed({"type": "content_block_stop"})
    assert mon.safe_tool_calls() == []


def test_safe_tool_calls_returns_copies():
    mon = _mon()
    mon.feed({"type": "message_start"})
    mon.feed(_tool_start("toolu_1", "read"))
    mon.feed(_args('{"path": "/a"}'))
    mon.feed({"type": "content_block_stop"})
    first = mon.safe_tool_calls()
    first[0]["name"] = "mutated"
    first[0]["input"]["path"] = "/etc/shadow"        # deep, not just shallow
    assert mon.safe_tool_calls()[0]["name"] == "read"
    assert mon.safe_tool_calls()[0]["input"] == {"path": "/a"}


# --- 5. W2-I8.2 detectors are pure -----------------------------------------

def test_feed_writes_nothing_to_memory_dir():
    """Driving a monitor all the way to a trip must not touch the store."""
    assert not config.MEMORY_DIR.exists()
    mon = _mon(max_tokens=100)
    _trip(lambda: _feed_all(mon, ["round and round we go. "] * 40))
    assert not config.MEMORY_DIR.exists()


def test_pure_detectors_take_explicit_state():
    """Each detector is callable standalone with explicit inputs (this is what
    makes them independently testable)."""
    assert streamguard.find_repetition_loop("ab " * 3, repeats=12) is None
    assert streamguard.detect_token_loop("hello world foo " * 20, repeats=12)
    assert streamguard.detect_empty_progress(5, limit=200) is None
    assert streamguard.detect_empty_progress(201, limit=200)
    assert streamguard.detect_whitespace_flood(10, limit=4096) is None
    assert streamguard.detect_whitespace_flood(5000, limit=4096)
    assert streamguard.detect_invalid_unicode("plain") is None
    assert streamguard.detect_invalid_unicode("\udfff")
    assert streamguard.detect_malformed_event({"type": "ping"}) is None
    assert streamguard.detect_event_order("init", "message_start",
                                          saw_tool_use=False) is None
    assert streamguard.detect_event_order("done", "ping", saw_tool_use=True)
    assert streamguard.detect_invalid_tool_delta("{", open_id="a",
                                                 delta_id="a") is None
    assert streamguard.detect_invalid_tool_delta(3, open_id=None,
                                                 delta_id=None)
    assert streamguard.detect_impossible_usage({"output_tokens": 5},
                                               max_output_tokens=10) is None
    assert streamguard.detect_impossible_usage({"output_tokens": 50},
                                               max_output_tokens=10)
    assert streamguard.detect_output_bounds(10, max_chars=None) is None
    assert streamguard.detect_output_bounds(200, max_chars=100)
    assert streamguard.output_bound_chars(None, chars_per_token=16) is None
    assert streamguard.whitespace_run(2, "  x  ") == (2, 4)


def test_detectors_are_deterministic():
    text = "loop de loop " * 30
    first = streamguard.find_repetition_loop(text, repeats=12)
    assert first == streamguard.find_repetition_loop(text, repeats=12)


# --- 6. flags ---------------------------------------------------------------

def test_flag_defaults_off_and_monitor_is_inert(monkeypatch):
    monkeypatch.delenv("OLYMPUS_STREAMGUARD", raising=False)
    assert streamguard.enabled() is False
    mon = streamguard.monitor(provider="anthropic", model="m", max_tokens=10)
    assert isinstance(mon, streamguard.NullMonitor)
    # everything a caller can do to it is a no-op, including the shapes that
    # would trip every detector
    for item in (["x"] * 400 + [""] * 400 + [{"type": "wat"}, b"\xff", 7]):
        mon.feed(item)
    mon.feed_usage({"output_tokens": -1})
    assert mon.safe_tool_calls() == []
    assert mon.stats() == {}
    assert not config.MEMORY_DIR.exists()


def test_flag_on_gives_a_real_monitor(monkeypatch):
    monkeypatch.setenv("OLYMPUS_STREAMGUARD", "1")
    assert streamguard.enabled() is True
    assert isinstance(streamguard.monitor(), streamguard.StreamMonitor)


def test_thresholds_are_env_tunable(monkeypatch):
    monkeypatch.setenv("OLYMPUS_STREAM_LOOP_REPEATS", "3")
    monkeypatch.setenv("OLYMPUS_STREAM_EMPTY_DELTAS", "5")
    monkeypatch.setenv("OLYMPUS_STREAM_WS_RUN", "10")
    monkeypatch.setenv("OLYMPUS_STREAM_PARTIAL_JSON", "2")
    lim = streamguard.limits()
    assert lim["loop_repeats"] == 3 and lim["empty_deltas"] == 5
    assert lim["ws_run"] == 10 and lim["partial_json"] == 2
    exc = _trip(lambda: _feed_all(_mon(), [""] * 7))
    assert exc.kind == streamguard.EMPTY_PROGRESS
    # a garbage threshold falls back to the default instead of exploding
    monkeypatch.setenv("OLYMPUS_STREAM_WS_RUN", "not-a-number")
    assert streamguard.limits()["ws_run"] == streamguard.DEFAULT_WS_RUN


# --- 7. llm.stream_text wiring ---------------------------------------------

_LOOP_CHUNKS = ["I cannot help with that. "] * 40


def test_stream_text_flag_off_is_byte_identical(monkeypatch):
    """A17 rollback: with the flag off, a stream that WOULD trip every
    detector is passed through unchanged and nothing is recorded."""
    monkeypatch.delenv("OLYMPUS_STREAMGUARD", raising=False)
    chunks = _LOOP_CHUNKS + [" " * 9000, ""] * 3
    monkeypatch.setattr(llm, "client",
                        lambda settings=None: _fake_client(list(chunks)))
    out = list(llm.stream_text("SYS", [{"role": "user", "content": "x"}],
                               settings=_SETTINGS, max_tokens=8))
    assert out == chunks                    # byte-identical, same order
    assert "".join(out) == "".join(chunks)
    assert not _ledger_path().exists()


def test_stream_text_trips_and_raises_typed_error(monkeypatch):
    """W2-I8.3: an explicit typed failure, never a silent truncated answer."""
    monkeypatch.setenv("OLYMPUS_STREAMGUARD", "1")
    monkeypatch.setattr(llm, "client",
                        lambda settings=None: _fake_client(list(_LOOP_CHUNKS)))
    seen = []
    with pytest.raises(streamguard.StreamAborted) as excinfo:
        for piece in llm.stream_text("SYS", [{"role": "user", "content": "x"}],
                                     settings=_SETTINGS, max_tokens=4000):
            seen.append(piece)
    exc = excinfo.value
    assert exc.kind == streamguard.TOKEN_LOOP
    assert isinstance(exc, streamguard.StreamPathology)
    # the stream was aborted early — the caller did NOT receive the whole loop
    assert 0 < len(seen) < len(_LOOP_CHUNKS)
    # …and the evidence landed
    recs = _records()
    assert len(recs) == 1
    assert recs[0]["kind"] == streamguard.TOKEN_LOOP
    assert recs[0]["provider"] == "anthropic"
    assert recs[0]["model"] == "claude-test"
    assert recs[0]["stats"]["deltas"] == len(seen) + 1
    assert exc.record == recs[0]


def test_stream_text_healthy_stream_is_unaffected_with_flag_on(monkeypatch):
    monkeypatch.setenv("OLYMPUS_STREAMGUARD", "1")
    chunks = ["Hello, ", "this is ", "a normal answer.\n"]
    monkeypatch.setattr(llm, "client",
                        lambda settings=None: _fake_client(list(chunks)))
    out = list(llm.stream_text("SYS", [{"role": "user", "content": "x"}],
                               settings=_SETTINGS))
    assert out == chunks
    assert not _ledger_path().exists()
    # usage still recorded (the C5 path is untouched)
    assert (config.MEMORY_DIR / "usage").exists()


def test_stream_text_records_impossible_final_usage(monkeypatch):
    """The stream finished, so an impossible final counter is evidence, not a
    retraction — the deltas already reached the user."""
    monkeypatch.setenv("OLYMPUS_STREAMGUARD", "1")
    chunks = ["fine ", "answer"]
    monkeypatch.setattr(
        llm, "client",
        lambda settings=None: _fake_client(list(chunks),
                                           _message(out_toks=9_000_000)))
    out = list(llm.stream_text("SYS", [{"role": "user", "content": "x"}],
                               settings=_SETTINGS, max_tokens=16))
    assert out == chunks
    assert [r["kind"] for r in _records()] == [streamguard.IMPOSSIBLE_USAGE]


def test_complete_records_impossible_usage_without_failing(monkeypatch):
    """The non-streaming seam only *records* an impossible counter — a
    completed answer is not retracted over telemetry."""
    monkeypatch.setenv("OLYMPUS_STREAMGUARD", "1")
    msg = _message(out_toks=9_000_000)
    monkeypatch.setattr(llm, "client", lambda settings=None: types.SimpleNamespace(
        messages=types.SimpleNamespace(stream=lambda **p: _FakeStream([], msg)),
        beta=types.SimpleNamespace(
            messages=types.SimpleNamespace(stream=lambda **p: _FakeStream([], msg)))))
    out = llm.complete("SYS", [{"role": "user", "content": "hi"}],
                       settings=_SETTINGS, max_tokens=16)
    assert llm.text_of(out) == "ok"
    recs = _records()
    assert [r["kind"] for r in recs] == [streamguard.IMPOSSIBLE_USAGE]


def test_complete_is_silent_with_flag_off(monkeypatch):
    monkeypatch.delenv("OLYMPUS_STREAMGUARD", raising=False)
    msg = _message(out_toks=9_000_000)
    monkeypatch.setattr(llm, "client", lambda settings=None: types.SimpleNamespace(
        messages=types.SimpleNamespace(stream=lambda **p: _FakeStream([], msg)),
        beta=types.SimpleNamespace(
            messages=types.SimpleNamespace(stream=lambda **p: _FakeStream([], msg)))))
    llm.complete("SYS", [{"role": "user", "content": "hi"}],
                 settings=_SETTINGS, max_tokens=16)
    assert not _ledger_path().exists()


# --- 8. evidence ------------------------------------------------------------

def test_pathology_record_is_pure_and_structured():
    mon = _mon(max_tokens=1000)
    exc = _trip(lambda: _feed_all(mon, ["round and round. "] * 40))
    rec = streamguard.pathology_record(mon, exc, provider="anthropic",
                                       model="claude-test")
    assert rec["kind"] == streamguard.TOKEN_LOOP
    assert rec["provider"] == "anthropic" and rec["model"] == "claude-test"
    assert rec["stats"]["chars"] > 0
    assert rec["safe_tool_calls"] == []
    assert rec["ts"].endswith("Z")
    assert not config.MEMORY_DIR.exists()   # building a record writes nothing


def test_record_pathology_appends_and_reads_back():
    for kind in (streamguard.TOKEN_LOOP, streamguard.EVENT_ORDER):
        streamguard.record_pathology(streamguard.pathology_record(
            None, streamguard.StreamPathology(kind, "detail"),
            provider="anthropic", model="m"))
    recs = streamguard.recent()
    assert [r["kind"] for r in recs] == [streamguard.TOKEN_LOOP,
                                         streamguard.EVENT_ORDER]
    assert streamguard.kind_counts() == {streamguard.TOKEN_LOOP: 1,
                                         streamguard.EVENT_ORDER: 1}


def test_record_pathology_never_raises_on_broken_proclock(monkeypatch):
    """Evidence is best-effort: a wedged lock loses the record, not the run."""
    from olympus import proclock

    def boom(*a, **kw):
        raise TimeoutError("lock wedged")

    monkeypatch.setattr(proclock, "lock", boom)
    streamguard.record_pathology({"kind": "x"})          # must not raise
    assert not _ledger_path().exists()


def test_record_pathology_never_raises_on_unserialisable_record(monkeypatch):
    streamguard.record_pathology({"kind": "x", "obj": object()})
    assert len(streamguard.recent()) == 1                # default=str fallback


def test_stream_text_trip_survives_a_broken_ledger(monkeypatch):
    """The typed failure still reaches the caller when evidence writing dies."""
    monkeypatch.setenv("OLYMPUS_STREAMGUARD", "1")
    from olympus import proclock
    monkeypatch.setattr(proclock, "lock",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("nope")))
    monkeypatch.setattr(llm, "client",
                        lambda settings=None: _fake_client(list(_LOOP_CHUNKS)))
    with pytest.raises(streamguard.StreamAborted):
        list(llm.stream_text("SYS", [{"role": "user", "content": "x"}],
                             settings=_SETTINGS, max_tokens=4000))


def test_recent_tolerates_a_corrupt_ledger():
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"kind": "token_loop"}\nnot json\n{"kind": "x"}\n',
                    encoding="utf-8")
    assert [r["kind"] for r in streamguard.recent()] == ["token_loop", "x"]


def test_recent_is_empty_without_a_ledger():
    assert streamguard.recent() == []
    assert streamguard.kind_counts() == {}

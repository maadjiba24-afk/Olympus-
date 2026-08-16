"""W3-C5 — the Anthropic-compatible `POST /v1/messages` handler.

Two layers, per the spec's "pure helpers are the testable core, the handler is
thin":

  * unit tests for the translation helpers in `olympus.web`
    (`_anthropic_to_internal`, `_internal_to_anthropic`, `_anthropic_error`,
    `_anthropic_sse`, ...);
  * wire tests that drive the REAL handler over the REAL server, reusing the
    house harness from `tests/test_openai_endpoint.py` (ThreadingHTTPServer +
    a fake council + an outbound-socket guard) so no test makes a network call.

Compatibility claim bound (W3-I7/W3-A7): responses and streaming events are
validated against the **real `anthropic` SDK types** — this is SDK-type
verification, NOT real-client verification. No third-party client is driven
over a socket here.
"""

from __future__ import annotations

import inspect
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import anthropic                                  # REQUIRED dependency (W3-A3)
from anthropic.types import (Message, RawContentBlockDeltaEvent,
                             RawContentBlockStartEvent,
                             RawContentBlockStopEvent, RawMessageDeltaEvent,
                             RawMessageStartEvent, RawMessageStopEvent)

from olympus import orchestrator, streamguard, usage, web


CANNED = "The council has spoken: hello from Olympus."


# --- harness (same shape as tests/test_openai_endpoint.py) -------------------

class _FakeBot:
    """A council stand-in: canned answer, never touches the network."""

    last_run_id = "run-anthropic-test"

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def ask(self, message: str) -> str:
        return CANNED

    def ask_stream(self, message: str):
        yield "The council has spoken: "
        yield "hello from Olympus."


@pytest.fixture(autouse=True)
def no_outbound_sockets(monkeypatch):
    real_connect = socket.socket.connect

    def guard(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"unexpected outbound socket to {address!r}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guard)
    yield


@pytest.fixture()
def server(monkeypatch):
    monkeypatch.setattr(orchestrator, "Olympus", _FakeBot)
    monkeypatch.setattr(web.orchestrator, "Olympus", _FakeBot)
    monkeypatch.setattr(web, "_SESSIONS", {})
    monkeypatch.setattr(web, "_HITS", {})
    monkeypatch.setenv("OLYMPUS_API_KEYS", "devkey")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _request(url, method="POST", payload=None, headers=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method)
    return urllib.request.urlopen(req)


def _key(name="x-api-key", key="devkey"):
    return {name: key}


def _post(base, payload, headers=None, path="/v1/messages"):
    return _request(base + path, payload=payload,
                    headers={**_key(), **(headers or {})})


def _msg(text="Say hello.", **extra):
    body = {"model": "olympus-council", "max_tokens": 1024,
            "messages": [{"role": "user", "content": text}]}
    body.update(extra)
    return body


def _err_body(exc: urllib.error.HTTPError) -> dict:
    return json.loads(exc.read())


def _frames(raw: str) -> list[tuple[str, dict]]:
    """Parse an SSE response into (event-name, data) pairs."""
    out = []
    for block in raw.split("\n\n"):
        if not block.strip():
            continue
        lines = block.strip().split("\n")
        assert lines[0].startswith("event: "), block
        assert lines[1].startswith("data: "), block
        out.append((lines[0][len("event: "):], json.loads(lines[1][6:])))
    return out


# --- 1. pure helpers: request translation ------------------------------------

def test_string_system_and_single_user_turn():
    messages, system, opts = web._anthropic_to_internal(
        {"model": "m", "max_tokens": 64, "system": "Be terse.",
         "messages": [{"role": "user", "content": "hi"}]})
    assert system == "Be terse."
    assert messages == [{"role": "user", "content": "hi"}]
    assert opts["model"] == "m" and opts["max_tokens"] == 64
    assert opts["stream"] is False


def test_block_list_system_is_flattened():
    _messages, system, _opts = web._anthropic_to_internal(
        {"max_tokens": 8, "system": [{"type": "text", "text": "one"},
                                     {"type": "text", "text": "two"}],
         "messages": [{"role": "user", "content": "hi"}]})
    assert system == "one\ntwo"


def test_unsupported_system_block_is_refused():
    with pytest.raises(web._AnthropicRefusal) as err:
        web._anthropic_to_internal(
            {"max_tokens": 8,
             "system": [{"type": "image", "source": {}}],
             "messages": [{"role": "user", "content": "hi"}]})
    assert "unsupported content block" in str(err.value)


def test_multi_turn_order_is_preserved_and_collapses_like_the_openai_path():
    messages, _system, _opts = web._anthropic_to_internal(
        {"max_tokens": 8, "messages": [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": [{"type": "text", "text": "second"}]}]})
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[-1]["content"] == "second"
    # The internal list is exactly what the OpenAI dialect already consumes.
    from olympus import openai_server
    prompt = openai_server.messages_to_prompt(messages)
    assert "User: first" in prompt and "Assistant: ok" in prompt
    assert "second" in prompt


def test_tool_use_block_becomes_the_internal_openai_tool_call():
    messages, _system, _opts = web._anthropic_to_internal(
        {"max_tokens": 8, "messages": [
            {"role": "user", "content": "weather?"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "checking"},
                {"type": "tool_use", "id": "toolu_1", "name": "weather",
                 "input": {"city": "Athens"}}]}]})
    call = messages[-1]["tool_calls"][0]
    assert call["id"] == "toolu_1" and call["type"] == "function"
    assert call["function"]["name"] == "weather"
    assert json.loads(call["function"]["arguments"]) == {"city": "Athens"}
    assert messages[-1]["content"] == "checking"


def test_tool_result_block_becomes_the_internal_tool_message():
    messages, _system, _opts = web._anthropic_to_internal(
        {"max_tokens": 8, "messages": [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1",
                 "content": [{"type": "text", "text": "18C"}]},
                {"type": "text", "text": "and tomorrow?"}]}]})
    assert messages[0] == {"role": "tool", "tool_call_id": "toolu_1",
                           "content": "18C"}
    assert messages[1] == {"role": "user", "content": "and tomorrow?"}


def test_unsupported_content_block_is_refused_not_dropped():
    with pytest.raises(web._AnthropicRefusal) as err:
        web._anthropic_to_internal(
            {"max_tokens": 8, "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "data": "x"}}]}]})
    assert err.value.status == 400
    assert err.value.kind == "invalid_request_error"
    assert "image" in str(err.value)


@pytest.mark.parametrize("payload", [
    {"max_tokens": 8, "stop_sequences": ["END"],
     "messages": [{"role": "user", "content": "hi"}]},
    {"max_tokens": 8, "top_k": 5,
     "messages": [{"role": "user", "content": "hi"}]},
])
def test_unsupported_fields_are_refused_by_the_helper(payload):
    with pytest.raises(web._AnthropicRefusal) as err:
        web._anthropic_to_internal(payload)
    assert "silently ignored" in str(err.value)


def test_missing_max_tokens_is_refused_by_the_helper():
    with pytest.raises(web._AnthropicRefusal) as err:
        web._anthropic_to_internal(
            {"messages": [{"role": "user", "content": "hi"}]})
    assert "`max_tokens` is required" in str(err.value)


def test_honoured_optional_knobs_survive_translation():
    _m, _s, opts = web._anthropic_to_internal(
        {"max_tokens": 8, "temperature": 0.2, "top_p": 0.9, "stream": True,
         "tools": [{"name": "t", "input_schema": {}}], "tool_choice": {"type": "auto"},
         "messages": [{"role": "user", "content": "hi"}]})
    assert opts["stream"] is True and opts["temperature"] == 0.2
    assert opts["top_p"] == 0.9 and opts["tools"] and opts["tool_choice"]


def test_bad_bodies_are_refused():
    for bad in ([], {"max_tokens": 8, "messages": []},
                {"max_tokens": 8, "messages": [{"role": "system",
                                                "content": "x"}]},
                {"max_tokens": 0, "messages": [{"role": "user",
                                                "content": "x"}]},
                {"max_tokens": "many", "messages": [{"role": "user",
                                                     "content": "x"}]}):
        with pytest.raises(web._AnthropicRefusal):
            web._anthropic_to_internal(bad)


# --- 2. pure helpers: response rendering -------------------------------------

def test_internal_to_anthropic_shape_and_usage_mapping():
    body = web._internal_to_anthropic(
        "hello", model="olympus-council",
        usage={"prompt_tokens": 7, "completion_tokens": 2})
    assert body["id"].startswith("msg_")
    assert body["type"] == "message" and body["role"] == "assistant"
    assert body["content"] == [{"type": "text", "text": "hello"}]
    assert body["stop_reason"] == "end_turn" and body["stop_sequence"] is None
    assert body["usage"] == {"input_tokens": 7, "output_tokens": 2}


def test_anthropic_error_envelope_is_native_not_openai():
    env = web._anthropic_error("invalid_request_error", "nope")
    assert env == {"type": "error",
                   "error": {"type": "invalid_request_error",
                             "message": "nope"}}
    assert "code" not in env["error"]        # the OpenAI envelope's field


def test_error_kind_mapping_covers_the_status_codes_used():
    assert web._anthropic_error_kind(400) == "invalid_request_error"
    assert web._anthropic_error_kind(401) == "authentication_error"
    assert web._anthropic_error_kind(403) == "permission_error"
    assert web._anthropic_error_kind(429) == "rate_limit_error"
    assert web._anthropic_error_kind(500) == "api_error"
    assert web._anthropic_error_kind(502) == "api_error"


def test_stop_reason_mapping():
    assert web._anthropic_stop_reason() == "end_turn"
    assert web._anthropic_stop_reason(truncated=True) == "max_tokens"
    assert web._anthropic_stop_reason(tool_use=True) == "tool_use"
    # a tool call wins over a length cut (the turn ends to run the tool)
    assert web._anthropic_stop_reason(truncated=True,
                                      tool_use=True) == "tool_use"


def test_max_tokens_cap_truncates_and_reports_it():
    text, truncated = web._anthropic_cap("x" * 100, 5)
    assert truncated is True and len(text) == 20        # ~4 chars/token
    text, truncated = web._anthropic_cap("short", 100)
    assert truncated is False and text == "short"


# --- 3. W3-A3 SDK-type verification (the compatibility evidence) --------------

def test_nonstreaming_body_validates_as_a_real_sdk_message(server):
    resp = _post(server, _msg())
    assert resp.status == 200
    body = json.loads(resp.read())
    message = Message.model_validate(body)          # real anthropic.types model
    assert message.type == "message" and message.role == "assistant"
    assert message.content[0].type == "text"
    assert message.content[0].text == CANNED
    assert message.stop_reason == "end_turn"
    assert message.usage.input_tokens > 0
    assert message.id.startswith("msg_")
    # the SDK re-serialises it without loss
    assert Message.model_validate(message.model_dump()).content[0].text == CANNED


def test_sdk_types_are_the_real_dependency_not_a_stub():
    assert anthropic.__name__ == "anthropic"
    assert Message.__module__.startswith("anthropic.types")


def test_streaming_events_validate_as_real_sdk_event_models(server):
    resp = _post(server, _msg(stream=True))
    events = _frames(resp.read().decode())
    models = {
        "message_start": RawMessageStartEvent,
        "content_block_start": RawContentBlockStartEvent,
        "content_block_delta": RawContentBlockDeltaEvent,
        "content_block_stop": RawContentBlockStopEvent,
        "message_delta": RawMessageDeltaEvent,
        "message_stop": RawMessageStopEvent,
    }
    seen = set()
    for name, data in events:
        assert name == data["type"], "event name must match the payload type"
        if name == "ping":
            continue
        models[name].model_validate(data)            # real SDK event models
        seen.add(name)
    assert seen == set(models)


# --- 4. W3-I2 loud refusal over the wire -------------------------------------

@pytest.mark.parametrize("payload,needle", [
    (_msg(stop_sequences=["END"]), "stop_sequences"),
    (_msg(top_k=5), "top_k"),
    ({"model": "m", "max_tokens": 8, "messages": [
        {"role": "user", "content": [{"type": "image", "source": {}}]}],
      }, "image"),
    ({"model": "m", "messages": [{"role": "user", "content": "hi"}]},
     "max_tokens"),
])
def test_unsupported_request_is_a_typed_400_in_the_anthropic_envelope(
        server, payload, needle):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(server, payload)
    assert exc.value.code == 400
    body = _err_body(exc.value)
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"
    assert needle in body["error"]["message"]


def test_malformed_json_body_is_a_400_envelope(server):
    req = urllib.request.Request(
        server + "/v1/messages", data=b"{not json",
        headers={"Content-Type": "application/json", **_key()}, method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req)
    assert exc.value.code == 400
    assert _err_body(exc.value)["error"]["type"] == "invalid_request_error"


def test_empty_user_content_is_refused(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(server, _msg(""))
    assert exc.value.code == 400
    assert "no user message content" in _err_body(exc.value)["error"]["message"]


# --- 5. W3-I5 auth parity -----------------------------------------------------

def test_x_api_key_header_is_accepted(server):
    resp = _request(server + "/v1/messages", payload=_msg(),
                    headers={"x-api-key": "devkey"})
    assert resp.status == 200


def test_authorization_bearer_is_accepted(server):
    resp = _request(server + "/v1/messages", payload=_msg(),
                    headers={"Authorization": "Bearer devkey"})
    assert resp.status == 200


@pytest.mark.parametrize("headers", [{"x-api-key": "wrong"},
                                     {"Authorization": "Bearer wrong"}])
def test_wrong_key_is_401_in_the_anthropic_envelope(server, headers):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _request(server + "/v1/messages", payload=_msg(), headers=headers)
    assert exc.value.code == 401
    body = _err_body(exc.value)
    assert body["type"] == "error"
    assert body["error"]["type"] == "authentication_error"


def test_missing_key_matches_the_chat_completions_status(server):
    """Both dialects refuse an unauthenticated call identically (only the
    envelope differs) — one gate, no second policy."""
    codes = {}
    for path, payload in (("/v1/messages", _msg()),
                          ("/v1/chat/completions",
                           {"messages": [{"role": "user", "content": "hi"}]})):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _request(server + path, payload=payload)
        codes[path] = exc.value.code
        body = _err_body(exc.value)
        if path == "/v1/messages":
            assert body["type"] == "error"
        else:
            assert "error" in body and "type" not in body   # OpenAI envelope
    assert codes["/v1/messages"] == codes["/v1/chat/completions"] == 401


def test_one_constant_time_comparison_serves_both_dialects():
    """W3-I5: the key check is not duplicated — `_v1_credential` only extracts,
    and `_v1_authorized` holds the single `hmac.compare_digest` call.

    Scoped to the two /v1 methods rather than counting across the whole
    Handler. The original assertion was `getsource(Handler).count(...) == 1`,
    which was a PROXY for that property and only equalled it while /v1 was the
    sole credential on the class. W2-1a added a separate operator credential
    (`_admin_authorized`, `X-Olympus-Operator`) whose comparisons are a
    different secret on a different surface, so the class-wide count became
    meaningless — it would now forbid any future credential check anywhere in
    the Handler, which is not what this test is about. Asserting per method
    tests the actual invariant instead of a coincidence.
    """
    v1_auth = inspect.getsource(web.Handler._v1_authorized)
    v1_cred = inspect.getsource(web.Handler._v1_credential)
    assert v1_auth.count("compare_digest") == 1, (
        "the /v1 key check must be exactly one constant-time comparison "
        "serving both dialects")
    assert v1_cred.count("compare_digest") == 0, (
        "_v1_credential must only EXTRACT the credential; comparing there "
        "would duplicate the check it exists to feed")
    assert "self._v1_credential()" in inspect.getsource(
        web.Handler._v1_authorized)
    assert "x-api-key" in inspect.getsource(web.Handler._v1_credential)
    assert "compare_digest" not in inspect.getsource(web.Handler._v1_credential)


# --- 6. W3-I6 streaming fidelity ---------------------------------------------

EXPECTED_ORDER = ["message_start", "content_block_start", "content_block_delta",
                  "content_block_stop", "message_delta", "message_stop"]


def test_streaming_event_names_and_order(server):
    resp = _post(server, _msg(stream=True))
    assert resp.headers.get("Content-Type") == "text/event-stream"
    events = _frames(resp.read().decode())
    names = [n for n, _ in events]
    assert "ping" in names                       # protocol keepalive
    spine = [n for n in names if n != "ping"]
    assert spine[0] == "message_start"
    assert spine[1] == "content_block_start"
    assert spine[-3] == "content_block_stop"
    assert spine[-2] == "message_delta"
    assert spine[-1] == "message_stop"
    assert set(spine[2:-3]) == {"content_block_delta"}
    assert [n for n in EXPECTED_ORDER if n in spine] == sorted(
        set(spine), key=spine.index)


def test_streaming_text_reassembles_to_the_council_answer(server):
    events = _frames(_post(server, _msg(stream=True)).read().decode())
    text = "".join(d["delta"]["text"] for n, d in events
                   if n == "content_block_delta")
    assert text == CANNED
    delta = [d for n, d in events if n == "message_delta"][0]
    assert delta["delta"]["stop_reason"] == "end_turn"
    assert delta["usage"]["output_tokens"] > 0
    start = [d for n, d in events if n == "message_start"][0]
    assert start["message"]["id"].startswith("msg_")
    assert start["message"]["usage"]["input_tokens"] > 0


def test_streaming_deltas_are_text_delta_typed(server):
    events = _frames(_post(server, _msg(stream=True)).read().decode())
    for name, data in events:
        if name == "content_block_delta":
            assert data["delta"]["type"] == "text_delta"


def test_streaming_respects_max_tokens_and_says_so(server):
    events = _frames(_post(server, _msg(stream=True, max_tokens=2)).read()
                     .decode())
    text = "".join(d["delta"]["text"] for n, d in events
                   if n == "content_block_delta")
    assert len(text) <= 8                       # 2 tokens * ~4 chars
    delta = [d for n, d in events if n == "message_delta"][0]
    assert delta["delta"]["stop_reason"] == "max_tokens"
    assert [n for n, _ in events][-1] == "message_stop"   # envelope still closed


def test_nonstreaming_length_limit_reports_max_tokens(server):
    body = json.loads(_post(server, _msg(max_tokens=2)).read())
    assert Message.model_validate(body).stop_reason == "max_tokens"
    assert body["content"][0]["text"] == CANNED[:8]


def test_streamguard_trip_yields_a_disclosure_and_closes_the_envelope(
        server, monkeypatch):
    """W2-I8.3 through the Anthropic dialect: a tripping stream must end with a
    disclosure delta, never a silent truncation — and the SSE envelope is still
    closed by content_block_stop / message_delta / message_stop."""

    class _Tripping:
        enabled = True
        provider = "olympus-web"
        model = "m"
        max_tokens = None
        tripped = None

        def __init__(self):
            self.n = 0

        def feed(self, delta):
            self.n += 1
            if self.n > 1:
                raise streamguard.StreamPathology("token_loop", "synthetic")

        def stats(self):
            return {"chunks": self.n}

        def safe_tool_calls(self):
            return []

    monkeypatch.setattr(streamguard, "monitor", lambda **kw: _Tripping())
    events = _frames(_post(server, _msg(stream=True)).read().decode())
    names = [n for n, _ in events]
    text = "".join(d["delta"]["text"] for n, d in events
                   if n == "content_block_delta")
    assert "Response incomplete" in text and "token_loop" in text
    assert "unfinished" in text
    assert names[-3:] == ["content_block_stop", "message_delta",
                          "message_stop"]
    # every event still validates as a real SDK event model
    for name, data in events:
        if name != "ping":
            assert data["type"] == name


def test_guarded_pieces_is_the_only_source_of_streamed_text():
    """W3-I1/W2-PR14: the Anthropic stream must feed through the committed
    streamguard wrapper, not around it."""
    src = inspect.getsource(web.Handler._stream_v1_messages)
    assert "self._guarded_pieces(bot.ask_stream(prompt)" in src
    assert "_anthropic_sse(" in src


# --- 7. W3-A5 parity: admission, council entry, no second server -------------

def test_admission_refusal_is_429_with_retry_after_in_the_anthropic_envelope(
        server, monkeypatch):
    def refuse(self, message):
        raise usage.AdmissionRefused("queue_full", retry_after_s=2.5,
                                     detail="too many in flight")

    monkeypatch.setattr(_FakeBot, "ask", refuse)
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(server, _msg())
    assert exc.value.code == 429
    assert exc.value.headers.get("Retry-After") == "3"
    assert exc.value.headers.get("X-Olympus-Admission") == "queue_full"
    body = _err_body(exc.value)
    assert body["type"] == "error"
    assert body["error"]["type"] == "rate_limit_error"
    assert "queue_full" in body["error"]["message"]


def test_admission_status_and_headers_match_the_openai_dialect(
        server, monkeypatch):
    def refuse(self, message):
        raise usage.AdmissionRefused("queue_full", retry_after_s=2.5)

    monkeypatch.setattr(_FakeBot, "ask", refuse)
    seen = {}
    for path, payload in (("/v1/messages", _msg()),
                          ("/v1/chat/completions",
                           {"messages": [{"role": "user", "content": "hi"}]})):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(server, payload, path=path)
        seen[path] = (exc.value.code, exc.value.headers.get("Retry-After"),
                      exc.value.headers.get("X-Olympus-Admission"))
    assert seen["/v1/messages"] == seen["/v1/chat/completions"]
    assert seen["/v1/messages"] == (429, "3", "queue_full")


def test_sovereignty_refusal_is_403_in_the_anthropic_envelope(
        server, monkeypatch):
    from olympus import security

    def refuse(self, message):
        raise security.SovereigntyError("sovereign mode: no local member")

    monkeypatch.setattr(_FakeBot, "ask", refuse)
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(server, _msg())
    assert exc.value.code == 403
    assert _err_body(exc.value)["error"]["type"] == "permission_error"


def test_internal_error_is_500_with_the_envelope_and_no_stack_trace(
        server, monkeypatch):
    def boom(self, message):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(_FakeBot, "ask", boom)
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(server, _msg())
    assert exc.value.code == 500
    body = _err_body(exc.value)
    assert body["error"]["type"] == "api_error"
    assert "Traceback" not in body["error"]["message"]


def test_audit_headers_are_present_on_both_dialects(server):
    resp = _post(server, _msg())
    assert resp.headers.get("X-Olympus-Audit", "").startswith("signed-")
    assert resp.headers.get("X-Olympus-Run-Id") == "run-anthropic-test"


def test_same_council_entry_as_chat_completions(server, monkeypatch):
    """W3-A1 (runtime): both dialects build the bot through the SAME factory
    and call the SAME council method."""
    calls = []

    class _Recording(_FakeBot):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            calls.append(("build", kw.get("user")))

        def ask(self, message):
            calls.append(("ask", message))
            return CANNED

    monkeypatch.setattr(web.orchestrator, "Olympus", _Recording)
    _post(server, _msg("anthropic side"))
    _post(server, {"messages": [{"role": "user", "content": "openai side"}]},
          path="/v1/chat/completions")
    assert [c[0] for c in calls] == ["build", "ask", "build", "ask"]
    # Same factory ⇒ same principal derivation. Both requests present the same
    # `devkey`, so both land on the same derived namespace. What W3-A1 asserts
    # is that the two dialects AGREE; the per-key split (F2) lives inside
    # `_v1_principal`, not in either dialect.
    expected = web.Handler._v1_principal(
        type("S", (), {"_v1_credential": lambda self: "devkey"})())
    assert calls[0][1] == calls[2][1] == expected
    assert expected != "api-v1"          # the old shared-principal literal


def test_source_asserts_one_generation_path():
    """W3-A1 (source): the Anthropic handler routes through the shared seams —
    `_v1_authorized`, `_v1_bot`, `_v1_run`, `bot.ask`/`ask_stream` — exactly as
    `_handle_v1_chat` does. No duplicated policy block."""
    a = inspect.getsource(web.Handler._handle_v1_messages)
    o = inspect.getsource(web.Handler._handle_v1_chat)
    for seam in ("self._v1_authorized()", "self._v1_bot()", "self._v1_run(",
                 "bot.ask(prompt)", "openai_server.messages_to_prompt("):
        assert seam in a, seam
        assert seam in o, seam
    # the failure policy exists once, in the shared runner
    run = inspect.getsource(web.Handler._v1_run)
    assert "usage.AdmissionRefused" in run
    assert "SovereigntyError" in run
    for handler in (a, o):
        assert "AdmissionRefused" not in handler
        assert "SovereigntyError" not in handler


def test_no_parallel_server_is_constructed():
    """W3-A1: the capability adds no second HTTP server and no new module."""
    src = open(web.__file__, encoding="utf-8").read()
    assert src.count("ThreadingHTTPServer(") == 1        # only in serve()
    assert "HTTPServer((" not in src.replace("ThreadingHTTPServer((", "")
    assert "def serve(" in src
    import os
    here = os.path.dirname(web.__file__)
    assert not os.path.exists(os.path.join(here, "anthropic_server.py"))
    assert not os.path.exists(os.path.join(here, "messages_api.py"))


# --- 8. exclusions ------------------------------------------------------------

def test_no_v1_complete_route(server):
    """Explicit exclusion: the legacy Anthropic Text Completions API is not
    implemented, so the route must 404 rather than half-exist."""
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(server, {"prompt": "hi", "max_tokens_to_sample": 10},
              path="/v1/complete")
    assert exc.value.code == 404


def test_openai_dialect_is_unchanged(server):
    """Additive route: the OpenAI surface still answers exactly as before."""
    body = json.loads(_post(server, {"messages": [
        {"role": "user", "content": "hi"}]},
        path="/v1/chat/completions").read())
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == CANNED


# --- 9. Step 1E: the refusal reaches the client, and never waits for the body -
#
# `/v1/messages` refuses on exactly the same structural path as
# `/v1/chat/completions`: `_v1_authorized` fails, the envelope is written, and
# the handler returns without ever reading the request body. `Handler` speaks
# HTTP/1.0, so the socket is then closed with those bytes still queued — and
# closing a TCP socket with unread data makes the stack send RST rather than FIN
# (RFC 1122 §4.2.2.13), which can destroy a response the peer has not read yet.
# The Anthropic dialect gets the same two-sided guard as the OpenAI one: the
# refusal must SURVIVE a large unread body, and must be EMITTED BEFORE the body
# finishes arriving. See tests/test_openai_endpoint.py for the full rationale.

_LARGE_BODY_CHARS = 512 * 1024      # materially large, well under _MAX_BODY
_REFUSAL_DEADLINE_S = 5.0


def _recv_headers(sock) -> bytes:
    """Read until the end of the response headers; returns everything read."""
    raw = b""
    while b"\r\n\r\n" not in raw:
        chunk = sock.recv(4096)
        if not chunk:
            break
        raw += chunk
    return raw


def _recv_rest_of_body(sock, head: bytes, body: bytes) -> bytes:
    """Keep reading until the advertised Content-Length has all arrived.

    `\\r\\n\\r\\n` can land in a segment before the body does, so a reader that
    stops at the header terminator sees an empty body and would wrongly report a
    truncated refusal.
    """
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip())
    while len(body) < length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        body += chunk
    assert len(body) == length, (len(body), length, body[:200])
    return body


def test_large_unread_body_still_yields_a_401_to_the_real_anthropic_sdk(server):
    """The real SDK, a wrong key, and real unread-body pressure on the socket.

    `max_retries=0`, so a single connection error is a failure rather than
    something a retry hides.
    """
    filler = "x" * _LARGE_BODY_CHARS
    messages = [{"role": "user", "content": filler}]
    assert len(json.dumps(messages)) < web._MAX_BODY, (
        "this regression must send a PERMITTED body; above _MAX_BODY it would "
        "be testing the oversize guard instead")

    client = anthropic.Anthropic(api_key="wrong-key", base_url=server,
                                 max_retries=0, timeout=30.0)
    try:
        try:
            with pytest.raises(anthropic.AuthenticationError) as caught:
                client.messages.create(model="olympus-council", max_tokens=64,
                                       messages=messages)
        except anthropic.APIConnectionError as err:
            raise AssertionError(
                "the server refused with 401 but the connection died before "
                "the SDK could read it — the HTTP/1.0 close is resetting the "
                "connection with the request body still unread, destroying a "
                f"response that was correctly written: {err!r}") from err
    finally:
        client.close()

    # ...and the envelope on the wire is still the NATIVE Anthropic shape, not
    # the OpenAI one and not something Step 1E reshaped. Asserted from the raw
    # response rather than `err.body`, which the SDK unwraps.
    assert caught.value.status_code == 401
    envelope = caught.value.response.json()
    assert envelope["type"] == "error", envelope
    assert envelope["error"]["type"] == "authentication_error", envelope


def test_messages_refusal_is_written_before_the_client_finishes_its_body(server):
    """A slow unauthenticated sender on `/v1/messages` is refused promptly.

    The mirror of the OpenAI slow-client regression: it fails if anyone ever
    "fixes" the reset by reading the body before answering, which would hand an
    unauthenticated caller a thread-pinning slowloris.
    """
    host, _, port = server.removeprefix("http://").partition(":")
    sliver = b'{"model": "m", "max_tokens": 16, "messages": [{"role": "user"'
    raw = b""
    with socket.create_connection((host, int(port)),
                                  timeout=_REFUSAL_DEADLINE_S) as sock:
        sock.sendall(
            f"POST /v1/messages HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"x-api-key: wrong-key\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {_LARGE_BODY_CHARS}\r\n"
            f"\r\n".encode())
        sock.sendall(sliver)             # a fraction of the advertised body
        owed = _LARGE_BODY_CHARS - len(sliver)

        started = time.perf_counter()
        sock.settimeout(_REFUSAL_DEADLINE_S)
        try:
            raw = _recv_headers(sock)
            # The timing claim is about the REFUSAL becoming readable, so it is
            # measured here — before the body is drained off the socket.
            elapsed = time.perf_counter() - started
            head, _, rest = raw.partition(b"\r\n\r\n")
            rest = _recv_rest_of_body(sock, head, rest)
        except (TimeoutError, OSError) as err:
            raise AssertionError(
                f"no refusal arrived within {_REFUSAL_DEADLINE_S}s while the "
                f"client still owed {owed} body bytes it never sent — the "
                f"server is waiting for the request body before answering, "
                f"which is exactly the unauthenticated slowloris the "
                f"response-first design exists to prevent: {err!r}") from err

    assert b"401" in head.split(b"\r\n")[0], raw[:400]
    assert elapsed < _REFUSAL_DEADLINE_S, (
        f"the 401 took {elapsed:.3f}s to become readable while the body was "
        f"still owed")
    envelope = json.loads(rest)
    assert envelope["type"] == "error", envelope
    assert envelope["error"]["type"] == "authentication_error", envelope

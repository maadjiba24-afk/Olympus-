"""Tests for the inbound OpenAI-compatible endpoint (SPEC-01).

The orchestrator is mocked so no test makes a real network call. A socket spy
fails the test if any outbound TCP connection is attempted, guaranteeing the
council pipeline is never actually run against a provider here.
"""

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import openai            # test-extra dependency, present whenever tests run

from olympus import config, openai_server, orchestrator, web


# --- a fake council: returns a canned answer, never touches the network ------

CANNED = "The council has spoken: hello from Olympus."


class _FakeBot:
    def __init__(self, *args, **kwargs):
        pass

    def ask(self, message: str) -> str:
        return CANNED

    def ask_stream(self, message: str):
        # The real pipeline yields its final answer in one or more pieces.
        yield CANNED


@pytest.fixture(autouse=True)
def no_outbound_sockets(monkeypatch):
    """Fail loudly if any test opens an outbound connection."""
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
    # Every request builds a council bot via orchestrator.Olympus — swap it for
    # the fake so the pipeline never runs for real.
    monkeypatch.setattr(orchestrator, "Olympus", _FakeBot)
    monkeypatch.setattr(web.orchestrator, "Olympus", _FakeBot)
    monkeypatch.setattr(web, "_SESSIONS", {})
    monkeypatch.setattr(web, "_HITS", {})
    monkeypatch.setenv("OLYMPUS_API_KEYS", "devkey")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base
    srv.shutdown()


def _request(url, method="GET", payload=None, headers=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method)
    return urllib.request.urlopen(req)


def _auth(key="devkey"):
    return {"Authorization": f"Bearer {key}"}


# --- non-streaming happy path -------------------------------------------------

def test_chat_completion_happy_path(server):
    resp = _request(
        server + "/v1/chat/completions", method="POST",
        payload={"model": "olympus-council",
                 "messages": [{"role": "user", "content": "Say hello."}]},
        headers=_auth())
    assert resp.status == 200
    body = json.loads(resp.read())
    assert body["object"] == "chat.completion"
    assert body["id"].startswith("chatcmpl-")
    assert body["model"] == "olympus-council"
    assert isinstance(body["created"], int)
    choice = body["choices"][0]
    assert choice["index"] == 0
    assert choice["finish_reason"] == "stop"
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"] == CANNED
    usage = body["usage"]
    assert set(usage) == {"prompt_tokens", "completion_tokens", "total_tokens"}
    assert usage["total_tokens"] == (
        usage["prompt_tokens"] + usage["completion_tokens"])


def test_unsupported_params_are_accepted_and_ignored(server):
    # temperature/max_tokens/tools must not error the request.
    resp = _request(
        server + "/v1/chat/completions", method="POST",
        payload={"model": "anything", "temperature": 0.2, "max_tokens": 50,
                 "tools": [{"type": "function", "function": {"name": "x"}}],
                 "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth())
    body = json.loads(resp.read())
    assert body["choices"][0]["message"]["content"] == CANNED
    # `model` is echoed back even for an arbitrary value (maps to the council).
    assert body["model"] == "anything"


# --- auth ---------------------------------------------------------------------

def test_missing_bearer_is_401(server):
    with pytest.raises(urllib.error.HTTPError) as err:
        _request(server + "/v1/chat/completions", method="POST",
                 payload={"messages": [{"role": "user", "content": "hi"}]})
    assert err.value.code == 401


def test_wrong_bearer_is_401(server):
    with pytest.raises(urllib.error.HTTPError) as err:
        _request(server + "/v1/chat/completions", method="POST",
                 payload={"messages": [{"role": "user", "content": "hi"}]},
                 headers=_auth("wrong-key"))
    assert err.value.code == 401


def test_valid_bearer_is_200(server):
    resp = _request(
        server + "/v1/chat/completions", method="POST",
        payload={"messages": [{"role": "user", "content": "hi"}]},
        headers=_auth())
    assert resp.status == 200


def test_models_requires_auth(server):
    with pytest.raises(urllib.error.HTTPError) as err:
        _request(server + "/v1/models")
    assert err.value.code == 401


# --- models list --------------------------------------------------------------

def test_models_list_shape(server):
    resp = _request(server + "/v1/models", headers=_auth())
    body = json.loads(resp.read())
    assert body["object"] == "list"
    ids = [m["id"] for m in body["data"]]
    assert "olympus-council" in ids
    model = body["data"][0]
    assert model["object"] == "model"
    assert model["owned_by"] == "olympus"
    assert isinstance(model["created"], int)


# --- streaming ----------------------------------------------------------------

def test_streaming_sse_shape(server):
    resp = _request(
        server + "/v1/chat/completions", method="POST",
        payload={"model": "olympus-council", "stream": True,
                 "messages": [{"role": "user", "content": "Count to three."}]},
        headers=_auth())
    assert resp.headers.get("Content-Type") == "text/event-stream"
    raw = resp.read().decode()
    # Split into SSE frames.
    frames = [b for b in raw.split("\n\n") if b.strip()]
    assert frames[-1].strip() == "data: [DONE]"
    chunks = []
    for frame in frames:
        line = frame.strip()
        assert line.startswith("data: ")
        data = line[len("data: "):]
        if data == "[DONE]":
            continue
        obj = json.loads(data)
        assert obj["object"] == "chat.completion.chunk"
        assert obj["choices"][0]["index"] == 0
        chunks.append(obj)
    # First chunk announces the assistant role; some chunk carries the content.
    assert chunks[0]["choices"][0]["delta"].get("role") == "assistant"
    content = "".join(c["choices"][0]["delta"].get("content", "")
                      for c in chunks)
    assert content == CANNED
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_streaming_via_openai_client(server):
    openai = pytest.importorskip("openai")
    client = openai.OpenAI(base_url=server + "/v1", api_key="devkey")
    stream = client.chat.completions.create(
        model="olympus-council", stream=True,
        messages=[{"role": "user", "content": "Count to three."}])
    content = ""
    for chunk in stream:
        delta = chunk.choices[0].delta
        if delta and delta.content:
            content += delta.content
    assert content == CANNED


def test_nonstreaming_via_openai_client(server):
    openai = pytest.importorskip("openai")
    client = openai.OpenAI(base_url=server + "/v1", api_key="devkey")
    completion = client.chat.completions.create(
        model="olympus-council",
        messages=[{"role": "user", "content": "Say hello."}])
    assert completion.choices[0].message.content == CANNED
    assert completion.object == "chat.completion"


# --- loopback-only default (no OLYMPUS_API_KEYS) ------------------------------

def test_loopback_allowed_without_keys(server, monkeypatch):
    # With no keys configured, loopback callers are served (no bearer needed).
    monkeypatch.delenv("OLYMPUS_API_KEYS", raising=False)
    resp = _request(
        server + "/v1/chat/completions", method="POST",
        payload={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status == 200


def test_remote_refused_without_keys(monkeypatch):
    # A non-loopback client with no configured keys must be refused (403),
    # never a silent open relay. Exercised at the handler level with a faked
    # remote peer address.
    monkeypatch.delenv("OLYMPUS_API_KEYS", raising=False)

    class _H(web.Handler):
        def __init__(self):  # bypass BaseHTTPRequestHandler's socket setup
            self.client_address = ("203.0.113.7", 5555)
            self.headers = {}

    h = _H()
    ok, code, _msg = h._v1_authorized()
    assert not ok and code == 403


# --- regression: existing routes still work in the same server ----------------

def test_existing_healthz_still_works(server):
    resp = _request(server + "/healthz")
    body = json.loads(resp.read())
    assert body["status"] == "ok"
    assert "uptime_seconds" in body


def test_existing_api_route_still_works(server):
    # /api/status is an unauthenticated dashboard route — must behave unchanged.
    resp = _request(server + "/api/status?since=0&session=regress")
    body = json.loads(resp.read())
    assert body["events"] == []
    assert body["next"] == 0


# --- translation unit checks --------------------------------------------------

def test_messages_to_prompt_single_user():
    assert openai_server.messages_to_prompt(
        [{"role": "user", "content": "just this"}]) == "just this"


def test_messages_to_prompt_system_and_history():
    prompt = openai_server.messages_to_prompt([
        {"role": "system", "content": "Be terse."},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "second"},
    ])
    assert "Be terse." in prompt
    assert "User: first" in prompt
    assert "Assistant: ok" in prompt
    assert "second" in prompt


def test_messages_to_prompt_content_parts():
    prompt = openai_server.messages_to_prompt(
        [{"role": "user", "content": [
            {"type": "text", "text": "hello"},
            {"type": "text", "text": "world"}]}])
    assert prompt == "hello\nworld"


def test_config_api_keys_parsing(monkeypatch):
    monkeypatch.setenv("OLYMPUS_API_KEYS", " a , b ,, c ")
    assert config.api_keys() == ["a", "b", "c"]
    monkeypatch.delenv("OLYMPUS_API_KEYS")
    assert config.api_keys() == []


# --- Step 1E: a refusal must reach the client, and must not wait for the body -
#
# `Handler` speaks HTTP/1.0, so `close_connection` is true for every request and
# the socket is closed the moment the response is written. The /v1 refusal paths
# answer BEFORE reading the request body, so that close used to happen with the
# body still queued in the receive buffer — and closing a TCP socket with unread
# bytes makes the stack send RST instead of FIN (RFC 1122 §4.2.2.13). An arriving
# reset lets the peer discard data it has not read yet, so a correctly written
# 401 could be destroyed in flight. The real OpenAI SDK reported
# `APIConnectionError: Connection error.` for a wrong-key request the server had
# in fact refused with a well-formed 401.
#
# The two tests below pin the two halves of the fix, and they pull in opposite
# directions on purpose:
#   * the SDK test proves the 401 SURVIVES a large unread body (someone deleting
#     the drain fails it);
#   * the slow-client test proves the 401 is EMITTED BEFORE the body finishes
#     (someone "fixing" it by reading the body first fails it — that would be a
#     slowloris reachable with no credential at all).
# Neither can be satisfied by weakening the other.

# Materially large, and deliberately well under `web._MAX_BODY`, so this is a
# permitted request rather than an oversize-refusal test. A tiny body hides the
# defect: it rides in with the headers and leaves nothing unread on the socket.
_LARGE_BODY_CHARS = 512 * 1024

# Ceiling for "the refusal arrived promptly". Loose enough not to flake on a
# loaded runner, tight enough that a server blocking on the body cannot pass:
# that server would never answer at all, because the test never sends the rest.
_REFUSAL_DEADLINE_S = 5.0


def _hostport(base: str) -> tuple[str, int]:
    host, _, port = base.removeprefix("http://").partition(":")
    return host, int(port)


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


def test_large_unread_body_still_yields_a_401_to_the_real_openai_sdk(server):
    """The real SDK, a wrong key, and real unread-body pressure on the socket.

    `max_retries=0` so nothing papers over a transport failure — a single
    connection error is a failure, which is the whole point. An
    `APIConnectionError` here means the server refused correctly and the
    connection died before the client could read the refusal.
    """
    filler = "x" * _LARGE_BODY_CHARS
    payload = [{"role": "user", "content": filler}]
    assert len(json.dumps(payload)) < web._MAX_BODY, (
        "this regression must send a PERMITTED body; above _MAX_BODY it would "
        "be testing the oversize guard instead")

    client = openai.OpenAI(api_key="wrong-key", base_url=server + "/v1",
                           max_retries=0, timeout=30.0)
    try:
        try:
            with pytest.raises(openai.AuthenticationError) as caught:
                client.chat.completions.create(model="olympus-council",
                                               messages=payload)
        except openai.APIConnectionError as err:
            raise AssertionError(
                "the server refused with 401 but the connection died before "
                "the SDK could read it — the HTTP/1.0 close is resetting the "
                "connection with the request body still unread, destroying a "
                f"response that was correctly written: {err!r}") from err
    finally:
        client.close()

    # ...and the envelope on the wire is still the OpenAI shape, unchanged by
    # Step 1E. Asserted from the raw response rather than `err.body`, which the
    # SDK unwraps to the inner error object.
    assert caught.value.status_code == 401
    envelope = caught.value.response.json()
    assert set(envelope) == {"error"}, envelope
    assert envelope["error"]["type"] == "invalid_request_error", envelope
    assert envelope["error"]["message"], envelope


def test_refusal_is_written_before_the_client_finishes_its_body(server):
    """A slow unauthenticated sender must be refused promptly.

    This exists specifically to stop a later "fix" that reads the whole body
    before answering. Such a server would hand any unauthenticated client a
    thread-pinning slowloris: advertise a large Content-Length, dribble it out,
    and hold a handler for as long as you like. Here the client advertises a
    large body, sends a sliver of it, and then never sends another byte — so a
    server that waits for the body can never answer, and this fails.
    """
    host, port = _hostport(server)
    sliver = b'{"messages": [{"role": "user", "content": "'
    raw = b""
    with socket.create_connection((host, port), timeout=_REFUSAL_DEADLINE_S) \
            as sock:
        sock.sendall(
            f"POST /v1/chat/completions HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Authorization: Bearer wrong-key\r\n"
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
    # ...and it is a complete, OpenAI-shaped envelope, not a truncated dribble.
    envelope = json.loads(rest)
    assert set(envelope) == {"error"}, envelope
    assert envelope["error"]["type"] == "invalid_request_error", envelope

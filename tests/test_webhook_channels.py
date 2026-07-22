"""Webhook channel family: the generic hardened server + the Mattermost and
Google Chat adapters (parse/verify/respond). Pure logic — no sockets."""

import json

import pytest

from olympus import chanbase, gateway, googlechat, mattermost, webhookchan


# --- generic server handler (via a fake request) ----------------------------

class _FakeHandler:
    """Drive make_handler's do_POST without a real socket."""
    def __init__(self, handler_cls, headers, body: bytes, client="1.2.3.4"):
        self._cls = handler_cls
        self.headers = headers
        self._body = body
        self.client_address = (client, 0)
        self.status = None
        self.out = None

    # BaseHTTPRequestHandler API surface do_POST touches:
    def _run(self):
        h = self._cls.__new__(self._cls)
        h.headers = self.headers
        h.client_address = self.client_address
        h.rfile = _Reader(self._body)
        h.wfile = _Writer()
        h.send_response = lambda code: setattr(self, "status", code)
        h.send_header = lambda *a, **k: None
        h.end_headers = lambda: None
        _orig = h.wfile.write
        def _capture(b):
            self.out = json.loads(b.decode())
            _orig(b)
        h.wfile.write = _capture
        h.do_POST()
        return self


class _Reader:
    def __init__(self, b): self._b = b
    def read(self, n): return self._b[:n]


class _Writer:
    def write(self, b): pass


def _handler(channel, prefix, verify, parse, respond, monkeypatch):
    monkeypatch.setattr(gateway, "reply_for",
                        lambda bots, key, text, prefix="ol", uid=None: [f"re:{text}"])
    return webhookchan.make_handler(channel, prefix, verify, parse, respond,
                                    bots={})


def test_server_rejects_bad_auth(monkeypatch):
    cls = _handler("t", "t", lambda h, b: False,
                   lambda p: ("u", "x"), lambda r: {"text": r}, monkeypatch)
    req = _FakeHandler(cls, {"Content-Length": "2"}, b"{}")._run()
    assert req.status == 401


def test_server_routes_and_responds(monkeypatch):
    monkeypatch.setenv("OLYMPUS_T_DM_POLICY", "open")
    cls = _handler("t", "t", lambda h, b: True,
                   lambda p: (p.get("u"), p.get("text")),
                   lambda r: {"text": r}, monkeypatch)
    body = json.dumps({"u": "alice", "text": "hi"}).encode()
    req = _FakeHandler(cls, {"Content-Length": str(len(body))}, body)._run()
    assert req.status == 200 and req.out == {"text": "re:hi"}


def test_server_ignores_unparseable_event(monkeypatch):
    cls = _handler("t", "t", lambda h, b: True, lambda p: None,
                   lambda r: {"text": r} if r else {}, monkeypatch)
    body = b'{"noise": 1}'
    req = _FakeHandler(cls, {"Content-Length": str(len(body))}, body)._run()
    assert req.status == 200 and req.out == {}


def test_server_never_leaks_stack_trace(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret internal detail")
    monkeypatch.setattr(chanbase, "collect_reply", boom)
    cls = webhookchan.make_handler("t", "t", lambda h, b: True,
                                   lambda p: ("u", "x"),
                                   lambda r: {"text": r}, bots={})
    body = b'{"x":1}'
    req = _FakeHandler(cls, {"Content-Length": str(len(body))}, body)._run()
    assert req.status == 200
    assert "error:" in req.out["text"] and "Traceback" not in req.out["text"]


# --- Mattermost adapter -----------------------------------------------------

def test_mattermost_verify_constant_time(monkeypatch):
    monkeypatch.setenv("MATTERMOST_OUTGOING_TOKEN", "tok123")
    ok = mattermost._verify({"Content-Type": "application/json"},
                            json.dumps({"token": "tok123"}).encode())
    bad = mattermost._verify({"Content-Type": "application/json"},
                             json.dumps({"token": "nope"}).encode())
    assert ok is True and bad is False


def test_mattermost_verify_fails_closed_without_token(monkeypatch):
    monkeypatch.delenv("MATTERMOST_OUTGOING_TOKEN", raising=False)
    assert mattermost._verify({}, b"{}") is False


def test_mattermost_parse_json_and_form():
    assert mattermost._parse({"user_name": "bob", "text": "hey"}) == ("bob", "hey")
    raw = "token=t&user_name=carol&text=hello+there"
    assert mattermost._parse({"_raw": raw}) == ("carol", "hello there")
    assert mattermost._parse({"user_name": "", "text": "x"}) is None


def test_mattermost_respond_shape():
    assert mattermost._respond("hi") == {"text": "hi"}
    assert mattermost._respond("") == {}


def test_mattermost_notify_unconfigured(monkeypatch):
    monkeypatch.delenv("MATTERMOST_WEBHOOK_URL", raising=False)
    assert mattermost.notify("x") is False


# --- Google Chat adapter ----------------------------------------------------

def test_googlechat_verify_bearer(monkeypatch):
    monkeypatch.setenv("GOOGLECHAT_VERIFY_TOKEN", "gtok")
    assert googlechat._verify({"Authorization": "Bearer gtok"}, b"{}") is True
    assert googlechat._verify({"X-Olympus-Token": "gtok"}, b"{}") is True
    assert googlechat._verify({"Authorization": "Bearer no"}, b"{}") is False


def test_googlechat_verify_fails_closed(monkeypatch):
    monkeypatch.delenv("GOOGLECHAT_VERIFY_TOKEN", raising=False)
    assert googlechat._verify({"Authorization": "Bearer x"}, b"{}") is False


def test_googlechat_parse_message_event():
    ev = {"type": "MESSAGE", "message": {"text": "hello",
          "sender": {"name": "users/123"}}}
    assert googlechat._parse(ev) == ("users/123", "hello")
    assert googlechat._parse({"type": "ADDED_TO_SPACE"}) is None
    assert googlechat._parse({"type": "MESSAGE", "message": {"text": ""}}) is None


def test_webhook_channels_untrusted_by_default(monkeypatch):
    monkeypatch.delenv("OLYMPUS_MATTERMOST_DM_POLICY", raising=False)
    monkeypatch.delenv("OLYMPUS_CHANNEL_DM_POLICY", raising=False)
    # collect_reply denies an unknown sender with the pairing hint
    out = chanbase.collect_reply("mattermost", "mm", "stranger", "hi", {})
    assert "private" in out

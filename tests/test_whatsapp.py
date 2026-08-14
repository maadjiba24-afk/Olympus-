"""Tests for the WhatsApp Cloud API gateway (webhook verify, parse, send)."""

import hashlib
import hmac
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from olympus import whatsapp


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "PNID")
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify-secret")
    monkeypatch.delenv("WHATSAPP_ALLOWED_NUMBERS", raising=False)
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)


def _text_msg(sender, body, mid="wamid.1"):
    return {"entry": [{"changes": [{"value": {"messages": [
        {"from": sender, "id": mid, "type": "text", "text": {"body": body}}]}}]}]}


# --- verification handshake ----------------------------------------------

def test_verify_challenge_ok():
    assert whatsapp.verify_challenge("subscribe", "verify-secret", "CH") == "CH"


def test_verify_challenge_wrong_token():
    assert whatsapp.verify_challenge("subscribe", "nope", "CH") is None


def test_verify_challenge_wrong_mode():
    assert whatsapp.verify_challenge("unsubscribe", "verify-secret", "CH") is None


# --- payload parsing -----------------------------------------------------

def test_extract_text_message():
    msgs = whatsapp.extract_messages(_text_msg("15551234567", "hello", "m1"))
    assert msgs == [("15551234567", "hello", "m1")]


def test_extract_ignores_status_callbacks():
    status = {"entry": [{"changes": [{"value": {"statuses": [
        {"id": "wamid.x", "status": "delivered"}]}}]}]}
    assert whatsapp.extract_messages(status) == []


def test_extract_ignores_non_text():
    img = {"entry": [{"changes": [{"value": {"messages": [
        {"from": "1555", "id": "m", "type": "image", "image": {"id": "x"}}]}}]}]}
    assert whatsapp.extract_messages(img) == []


# --- signature verification ----------------------------------------------

def test_signature_refused_without_secret():
    """Fail closed: without WHATSAPP_APP_SECRET no payload can be shown to
    be genuine, so none is accepted. Previously this returned True, so a
    default install trusted any POST that reached the webhook URL and let
    a forged payload spoof any sender number."""
    assert whatsapp.valid_signature(b"body", None) is False
    assert whatsapp.valid_signature(b"body", "sha256=deadbeef") is False


def test_signature_enforced_with_secret(monkeypatch):
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "s3cret")
    body = b'{"a":1}'
    good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert whatsapp.valid_signature(body, good) is True
    assert whatsapp.valid_signature(body, "sha256=deadbeef") is False
    assert whatsapp.valid_signature(body, None) is False


# --- allowlist + message handling ----------------------------------------

def test_allowlist(monkeypatch):
    monkeypatch.setenv("WHATSAPP_ALLOWED_NUMBERS", "111,222")
    assert whatsapp._allowed("111") is True
    assert whatsapp._allowed("333") is False


def test_process_runs_pipeline_and_replies(monkeypatch):
    monkeypatch.setenv("WHATSAPP_ALLOWED_NUMBERS", "1555")  # closed by default
    sent = []
    monkeypatch.setattr(whatsapp, "_send", lambda to, text: sent.append((to, text)))
    monkeypatch.setattr(whatsapp.orchestrator.Olympus, "ask",
                        lambda self, msg: f"answer to: {msg}")
    whatsapp._process({}, "1555", "what's the weather")
    assert sent == [("1555", "answer to: what's the weather")]


def test_process_blocks_disallowed_sender(monkeypatch):
    monkeypatch.setenv("WHATSAPP_ALLOWED_NUMBERS", "999")
    sent = []
    monkeypatch.setattr(whatsapp, "_send", lambda to, text: sent.append((to, text)))
    whatsapp._process({}, "1555", "hi")
    assert sent == [("1555", "This Olympus instance is private.")]


def test_send_calls_graph_api(monkeypatch):
    calls = {}
    class _Resp:
        def read(self): return b"{}"
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_urlopen(req, timeout=0):
        calls["url"] = req.full_url
        calls["auth"] = req.headers.get("Authorization")
        calls["body"] = json.loads(req.data)
        return _Resp()
    monkeypatch.setattr(whatsapp.urllib.request, "urlopen", fake_urlopen)
    whatsapp._send("1555", "hi there")
    assert calls["url"].endswith("/PNID/messages")
    assert calls["auth"] == "Bearer tok"
    assert calls["body"]["to"] == "1555" and calls["body"]["text"]["body"] == "hi there"


# --- the live webhook server ---------------------------------------------

@pytest.fixture()
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), whatsapp.Handler)
    httpd._bots, httpd._workers, httpd._seen = {}, {}, set()
    httpd._lock = threading.Lock()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", httpd
    httpd.shutdown()


def test_webhook_get_verifies(server):
    base, _ = server
    url = (base + "/webhook?hub.mode=subscribe"
           "&hub.verify_token=verify-secret&hub.challenge=12345")
    assert urllib.request.urlopen(url).read().decode() == "12345"


def test_webhook_get_rejects_bad_token(server):
    base, _ = server
    import urllib.error
    url = base + "/webhook?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=1"
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(url)
    assert e.value.code == 403


def test_webhook_post_dispatches_once(server, monkeypatch):
    base, httpd = server
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "")   # signature path covered
    monkeypatch.setattr(whatsapp, "valid_signature", lambda *a: True)
    processed = []
    monkeypatch.setattr(whatsapp, "_process",
                        lambda bots, sender, text: processed.append((sender, text)))
    body = json.dumps(_text_msg("1555", "ping", "dup-id")).encode()
    for _ in range(2):  # send the same webhook twice — must process once
        req = urllib.request.Request(base + "/webhook", data=body,
                                     headers={"Content-Type": "application/json"})
        assert urllib.request.urlopen(req).read() == b"ok"
    import time
    time.sleep(0.3)  # let the background worker run
    assert processed == [("1555", "ping")]

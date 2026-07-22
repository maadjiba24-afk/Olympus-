"""SMS channel (Twilio-compatible): constant-time signature verification,
form parsing, TwiML replies, and the untrusted-by-default access spine."""

import base64
import hashlib
import hmac
import urllib.parse

import pytest

from olympus import chanbase, config, gateway, sms


TOKEN = "twilio-secret-token"
URL = "https://olympus.example/sms"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("OLYMPUS_SMS_PUBLIC_URL", URL)
    monkeypatch.delenv("OLYMPUS_SMS_DM_POLICY", raising=False)
    monkeypatch.delenv("OLYMPUS_CHANNEL_DM_POLICY", raising=False)


def _sign(params: dict, token=TOKEN, url=URL) -> str:
    data = url
    for k in sorted(params):
        data += k + params[k]
    mac = hmac.new(token.encode(), data.encode(), hashlib.sha1)
    return base64.b64encode(mac.digest()).decode()


# --- signature verification ------------------------------------------------

def test_verify_accepts_correct_signature():
    p = {"From": "+15551112222", "Body": "hi"}
    hdrs = {"X-Twilio-Signature": _sign(p)}
    assert sms.verify(hdrs, URL, p) is True


def test_verify_rejects_wrong_signature():
    p = {"From": "+15551112222", "Body": "hi"}
    assert sms.verify({"X-Twilio-Signature": "AAAA"}, URL, p) is False


def test_verify_rejects_missing_header():
    assert sms.verify({}, URL, {"Body": "x"}) is False


def test_verify_fails_closed_without_token(monkeypatch):
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    p = {"Body": "x"}
    assert sms.verify({"X-Twilio-Signature": _sign(p)}, URL, p) is False


# --- parse + twiml ---------------------------------------------------------

def test_parse_extracts_sender_and_body():
    assert sms.parse({"From": "+1999", "Body": "hello"}) == ("+1999", "hello")
    assert sms.parse({"From": "", "Body": "x"}) is None
    assert sms.parse({"From": "+1", "Body": ""}) is None


def test_twiml_escapes_and_empties():
    out = sms.twiml("a & b <c>").decode()
    assert "a &amp; b &lt;c&gt;" in out
    assert out.startswith("<?xml")
    assert sms.twiml("").decode().endswith("<Response></Response>")


# --- handler end-to-end (no socket) ----------------------------------------

class _Reader:
    def __init__(self, b): self._b = b
    def read(self, n): return self._b[:n]


def _post(handler_cls, params, sign=True, client="9.9.9.9"):
    body = urllib.parse.urlencode(params).encode()
    hdrs = {"Content-Length": str(len(body)), "Host": "olympus.example"}
    if sign:
        hdrs["X-Twilio-Signature"] = _sign(params)
    captured = {}
    h = handler_cls.__new__(handler_cls)
    h.headers = hdrs
    h.path = "/sms"
    h.client_address = (client, 0)
    h.rfile = _Reader(body)
    h.send_response = lambda code: captured.__setitem__("status", code)
    h.send_header = lambda *a, **k: None
    h.end_headers = lambda: None

    class _W:
        def write(_self, b): captured["body"] = b
    h.wfile = _W()
    h.do_POST()
    return captured


def test_handler_rejects_unsigned():
    cap = _post(sms.make_handler(bots={}), {"From": "+1", "Body": "hi"},
               sign=False)
    assert cap["status"] == 403


def test_handler_untrusted_by_default():
    # a valid signature proves it's really Twilio, but the SENDER is still
    # untrusted until paired — access is separate from transport auth.
    cap = _post(sms.make_handler(bots={}), {"From": "+15550000", "Body": "hi"})
    assert cap["status"] == 200
    assert "private" in cap["body"].decode().lower()


def test_handler_open_policy_routes(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SMS_DM_POLICY", "open")
    monkeypatch.setattr(gateway, "reply_for",
                        lambda bots, key, text, prefix="ol", uid=None: [f"re:{text}"])
    cap = _post(sms.make_handler(bots={}), {"From": "+15551", "Body": "ping"})
    assert cap["status"] == 200
    assert "<Message>re:ping</Message>" in cap["body"].decode()


def test_handler_never_leaks_stack_trace(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SMS_DM_POLICY", "open")

    def boom(*a, **k):
        raise RuntimeError("secret internal detail")
    monkeypatch.setattr(chanbase, "collect_reply", boom)
    cap = _post(sms.make_handler(bots={}), {"From": "+1", "Body": "x"})
    assert cap["status"] == 200
    txt = cap["body"].decode()
    # generic apology only — never the exception string, path, or a traceback
    assert "went wrong" in txt
    assert "secret internal" not in txt and "Traceback" not in txt


def test_handler_rate_limits(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SMS_DM_POLICY", "open")
    monkeypatch.setattr(gateway, "reply_for",
                        lambda *a, **k: ["ok"])
    lim = chanbase.RateLimiter(per_minute=2)
    cls = sms.make_handler(bots={}, limiter=lim)
    codes = [_post(cls, {"From": "+1", "Body": "hi"})["status"]
             for _ in range(3)]
    assert 429 in codes


# --- notify ----------------------------------------------------------------

def test_notify_unconfigured_is_noop(monkeypatch):
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("TWILIO_FROM", raising=False)
    monkeypatch.delenv("OLYMPUS_SMS_NOTIFY_TO", raising=False)
    assert sms.notify("hello") is False


def test_notify_in_gateway_fanout():
    assert "sms" in gateway.NOTIFY_CHANNELS

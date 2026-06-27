"""Discord / Slack / Signal gateways: shared routing + per-platform transport.

These cover the pure, stdlib-only logic (parsing, formatting, signing, command
routing). Network I/O is monkeypatched; the Ed25519 path (Discord) needs the
cryptography backend and is exercised only structurally."""

import hashlib
import hmac
import json
import time

from olympus import discord, gateway, scheduler, signal as signal_gw, slack


# --- shared gateway routing ------------------------------------------------

def test_reply_for_help_command():
    out = gateway.reply_for({}, "u1", "/help")
    assert any("OLYMPUS" in c for c in out)


def test_reply_for_pipeline(monkeypatch):
    monkeypatch.setattr("olympus.orchestrator.Olympus.ask",
                        lambda self, msg: f"answer:{msg}")
    out = gateway.reply_for({}, "u1", "hello there")
    assert out == ["answer:hello there"]


def test_chunk_splits_long_text():
    parts = gateway.chunk("x" * 8000, size=3500)
    assert len(parts) == 3 and "".join(parts) == "x" * 8000


# --- Slack -----------------------------------------------------------------

def test_slack_url_verification_handshake():
    resp = slack.handle_event({"type": "url_verification", "challenge": "abc"})
    assert resp == {"challenge": "abc"}


def test_slack_ignores_bot_messages():
    resp = slack.handle_event({"event": {"type": "message", "bot_id": "B1",
                                         "text": "loop"}})
    assert resp == {"ok": True}


def test_slack_signature_roundtrip():
    secret = "s3cr3t"
    body = b'{"hello":"world"}'
    ts = str(int(time.time()))
    digest = hmac.new(secret.encode(), f"v0:{ts}:".encode() + body,
                      hashlib.sha256).hexdigest()
    sig = f"v0={digest}"
    assert slack.verify_signature(secret, ts, body, sig) is True
    assert slack.verify_signature(secret, ts, body, "v0=deadbeef") is False
    # stale timestamp is rejected
    old = str(int(time.time()) - 10_000)
    digest2 = hmac.new(secret.encode(), f"v0:{old}:".encode() + body,
                       hashlib.sha256).hexdigest()
    assert slack.verify_signature(secret, old, body, f"v0={digest2}") is False


def test_slack_message_routes_and_sends(monkeypatch):
    sent = []
    monkeypatch.setattr(slack, "send", lambda ch, txt: sent.append((ch, txt)))
    monkeypatch.setattr(gateway, "reply_for",
                        lambda bots, uk, text, prefix="": ["hi back"])
    resp = slack.handle_event({"event": {"type": "message", "text": "hi",
                                         "channel": "C1", "user": "U1"}})
    assert resp == {"ok": True}
    assert sent == [("C1", "hi back")]


# --- Discord ---------------------------------------------------------------

def test_discord_ping_returns_pong():
    assert discord.handle_interaction({"type": 1}) == {"type": 1}


def test_discord_slash_command_routes(monkeypatch):
    monkeypatch.setattr(gateway, "reply_for",
                        lambda bots, uk, text, prefix="": [f"got:{text}"])
    payload = {"type": 2, "member": {"user": {"id": "42"}},
               "data": {"name": "ask",
                        "options": [{"value": "price my product"}]}}
    resp = discord.handle_interaction(payload)
    assert resp["type"] == 4
    assert resp["data"]["content"] == "got:price my product"


def test_discord_bad_signature_rejected():
    # wrong key/sig must not verify (covers the non-crypto failure path)
    assert discord.verify_signature("zz", "zz", "1", b"body") is False


# --- Signal ----------------------------------------------------------------

def test_signal_parse_messages():
    raw = [
        {"envelope": {"source": "+1555", "dataMessage": {"message": "hello"}}},
        {"envelope": {"source": "+1555", "dataMessage": {}}},      # no text
        {"nope": True},                                            # malformed
    ]
    assert signal_gw.parse_messages(raw) == [("+1555", "hello")]


def test_signal_send_requires_number(monkeypatch):
    monkeypatch.delenv("SIGNAL_NUMBER", raising=False)
    assert signal_gw.send("+1999", "hi", number="") is False


# --- scheduler delivery dispatches to the new gateways ---------------------

def test_scheduler_delivers_to_discord(monkeypatch):
    pushed = {}
    monkeypatch.setattr(discord, "notify",
                        lambda msg: pushed.update({"d": msg}) or True)
    job = scheduler.Job(name="j", interval=60, prompt="p", deliver_to="discord")
    scheduler._deliver(job, "the answer")
    assert "the answer" in pushed["d"]

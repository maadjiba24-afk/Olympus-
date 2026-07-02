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


# --- proactive fan-out (heartbeat/backup push to every channel) ------------

def test_notify_all_fans_out_and_reports_delivered(monkeypatch):
    from olympus import discord, signal as signal_gw, slack, telegram
    monkeypatch.setattr(telegram, "notify", lambda t: True)
    monkeypatch.setattr(discord, "notify", lambda t: False)   # not configured
    monkeypatch.setattr(slack, "notify", lambda t: True)
    # a broken channel must not abort the fan-out to the others
    monkeypatch.setattr(signal_gw, "notify",
                        lambda t: (_ for _ in ()).throw(RuntimeError("boom")))
    delivered = gateway.notify_all("heads up")
    assert delivered == ["telegram", "slack"]


# --- background dispatcher (fast-ack + de-dup) -----------------------------

def test_dispatcher_dedups_retries():
    d = gateway.Dispatcher()
    assert d.seen("e1") is False        # first delivery
    assert d.seen("e1") is True         # platform retried the same event
    assert d.seen("e2") is False        # a different event still processes
    assert d.seen(None) is False        # missing id is never a "duplicate"
    assert d.seen(None) is False


def test_dispatcher_eviction_is_fifo_not_full_clear():
    # At the cap, only the OLDEST id is evicted; a recent id is still deduped.
    d = gateway.Dispatcher(max_seen=3)
    for e in ("a", "b", "c"):
        assert d.seen(e) is False
    assert d.seen("d") is False         # 'd' added, oldest 'a' evicted
    assert d.seen("c") is True          # recent id still remembered (not cleared)
    assert d.seen("b") is True          # ditto
    assert d.seen("a") is False         # evicted → looks new (acceptable, bounded)


def test_dispatcher_runs_work_on_a_worker():
    import threading
    d = gateway.Dispatcher()
    done = threading.Event()
    d.submit("u1", lambda: done.set())
    assert done.wait(timeout=5)


# --- Slack -----------------------------------------------------------------

def test_slack_url_verification_handshake():
    resp = slack.handle_event({"type": "url_verification", "challenge": "abc"})
    assert resp == {"challenge": "abc"}


def test_slack_ignores_bot_messages():
    resp = slack.handle_event({"event": {"type": "message", "bot_id": "B1",
                                         "text": "loop"}})
    assert resp == {"ok": True}


def test_slack_process_event_ignores_bot_messages(monkeypatch):
    # A bot's own message must never trigger a reply (would loop forever).
    sent = []
    monkeypatch.setattr(slack, "send", lambda ch, txt: sent.append((ch, txt)))
    slack.process_event({"event": {"type": "message", "bot_id": "B1",
                                   "text": "loop", "channel": "C1"}})
    assert sent == []


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


def test_slack_handle_event_acks_fast_without_pipeline(monkeypatch):
    # The synchronous handler must NOT run the (slow) pipeline — Slack's ~3s
    # deadline would be blown and it would retry into a duplicate reply.
    called = {"n": 0}
    monkeypatch.setattr(gateway, "reply_for",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    resp = slack.handle_event({"type": "event_callback",
                               "event": {"type": "message", "text": "hi",
                                         "channel": "C1", "user": "U1"}})
    assert resp == {"ok": True} and called["n"] == 0


def test_slack_process_event_routes_and_sends(monkeypatch):
    sent = []
    monkeypatch.setattr(slack, "send", lambda ch, txt: sent.append((ch, txt)))
    monkeypatch.setattr(gateway, "reply_for",
                        lambda bots, uk, text, prefix="": ["hi back"])
    slack.process_event({"event": {"type": "message", "text": "hi",
                                   "channel": "C1", "user": "U1"}})
    assert sent == [("C1", "hi back")]


# --- Discord ---------------------------------------------------------------

def test_discord_ping_returns_pong():
    assert discord.sync_response({"type": 1}) == {"type": 1}


def test_discord_slash_command_is_deferred_then_followed_up(monkeypatch):
    # A slash command is acked with a DEFERRED response (type 5) so Discord's
    # 3s deadline is met; the real reply is delivered later via the follow-up.
    payload = {"type": 2, "id": "int1", "application_id": "app1",
               "token": "tok1", "member": {"user": {"id": "42"}},
               "data": {"name": "ask",
                        "options": [{"value": "price my product"}]}}
    assert discord.sync_response(payload) == {"type": 5}

    followups = []
    monkeypatch.setattr(gateway, "reply_for",
                        lambda bots, uk, text, prefix="": [f"got:{text}"])
    monkeypatch.setattr(discord, "_followup",
                        lambda app, tok, text: followups.append((app, tok, text)))
    discord.process_command(payload)
    assert followups == [("app1", "tok1", "got:price my product")]


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


# --- in-flight journal / session auto-resume (Hermes v0.13/v0.18) -----------

def test_inflight_mark_take_clear():
    from olympus import gateway
    gateway.inflight_mark("tg-123", 123, "long question")
    taken = gateway.inflight_take("tg-")
    assert len(taken) == 1
    assert taken[0]["key"] == 123 and taken[0]["text"] == "long question"
    assert gateway.inflight_take("tg-") == []       # popped


def test_inflight_clear_removes_entry():
    from olympus import gateway
    gateway.inflight_mark("wa-555", "555", "q")
    gateway.inflight_clear("wa-555")
    assert gateway.inflight_take("wa-") == []


def test_inflight_second_attempt_is_dropped():
    from olympus import gateway
    # Same message marked twice = it already crashed the gateway once.
    gateway.inflight_mark("tg-9", 9, "poison message")
    gateway.inflight_mark("tg-9", 9, "poison message")
    assert gateway.inflight_take("tg-") == []       # not retried again


def test_inflight_prefix_scoping():
    from olympus import gateway
    gateway.inflight_mark("tg-1", 1, "telegram work")
    gateway.inflight_mark("wa-2", "2", "whatsapp work")
    tg = gateway.inflight_take("tg-")
    assert [e["text"] for e in tg] == ["telegram work"]
    wa = gateway.inflight_take("wa-")
    assert [e["text"] for e in wa] == ["whatsapp work"]

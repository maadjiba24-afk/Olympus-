"""Per-agent heartbeats: cadence, quietness contract, delivery, chat wiring."""

from olympus import agentbeat, gateway, heartbeat


def test_add_and_summary():
    beat = agentbeat.add("u1", "2h", "anything urgent?")
    assert beat.every == 7200
    text = agentbeat.summary("u1")
    assert beat.id in text and "anything urgent?" in text


def test_beats_are_per_user():
    agentbeat.add("u1", 3600, "check a")
    agentbeat.add("u2", 3600, "check b")
    assert len(agentbeat.for_user("u1")) == 1
    assert "check b" not in agentbeat.summary("u1")


def test_not_due_until_cadence_elapses():
    agentbeat.add("u1", 3600, "check", now=1000.0)
    assert agentbeat.run_due(now=1000.0 + 3599) == []
    log = agentbeat.run_due(now=1000.0 + 3601,
                            runner=lambda b: agentbeat.QUIET_TOKEN)
    assert len(log) == 1 and "quiet" in log[0]


def test_quiet_reply_is_not_delivered(monkeypatch):
    agentbeat.add("u1", 60, "check", now=0.0)
    delivered = []
    monkeypatch.setattr(gateway, "notify_all",
                        lambda t: delivered.append(t) or ["telegram"])
    log = agentbeat.run_due(now=61.0, runner=lambda b: "HB_OK")
    assert delivered == []
    assert "quiet" in log[0]


def test_substantive_reply_is_delivered(monkeypatch):
    agentbeat.add("u1", 60, "check", now=0.0)
    delivered = []
    monkeypatch.setattr(gateway, "notify_all",
                        lambda t: delivered.append(t) or ["telegram"])
    log = agentbeat.run_due(now=61.0,
                            runner=lambda b: "Your build broke overnight.")
    assert delivered and "build broke" in delivered[0]
    assert "delivered to telegram" in log[0]


def test_failing_beat_does_not_spin(monkeypatch):
    agentbeat.add("u1", 60, "check", now=0.0)

    def boom(beat):
        raise RuntimeError("provider down")

    log = agentbeat.run_due(now=61.0, runner=boom)
    assert "failed" in log[0]
    # marked as run: not due again immediately
    assert agentbeat.run_due(now=62.0, runner=boom) == []


def test_is_quiet_tolerates_trailing_punctuation():
    assert agentbeat.is_quiet("HB_OK")
    assert agentbeat.is_quiet("hb_ok.")
    assert agentbeat.is_quiet("  HB_OK\n")
    assert not agentbeat.is_quiet("HB_OK but your server is on fire")
    assert not agentbeat.is_quiet("all good, nothing urgent")


def test_remove_and_next_due():
    beat = agentbeat.add("u1", 600, "check", now=100.0)
    assert agentbeat.next_due_in(now=100.0) == 600.0
    assert agentbeat.remove("u1", beat.id) is True
    assert agentbeat.next_due_in(now=100.0) is None
    assert agentbeat.remove("u1", beat.id) is False


def test_heartbeat_tick_runs_beats(monkeypatch):
    agentbeat.add("u1", 60, "check", now=0.0)
    monkeypatch.setattr(agentbeat, "_default_runner",
                        lambda b: agentbeat.QUIET_TOKEN)
    log = heartbeat.tick({}, now=61.0)
    assert any("Heartbeats:" in line and "quiet" in line for line in log)


# --- chat wiring --------------------------------------------------------------

def test_chat_add_list_drop():
    out = gateway.reply_for({}, "u9", "/heartbeat add 30m watch my goals")
    joined = "\n".join(out)
    assert "every 30 minutes" in joined
    beat = agentbeat.for_user("ol-u9")[0]
    assert beat.prompt == "watch my goals"

    out = gateway.reply_for({}, "u9", "/heartbeat list")
    assert any(beat.id in c for c in out)

    out = gateway.reply_for({}, "u9", f"/heartbeat drop {beat.id}")
    assert any("Dropped" in c for c in out)
    assert agentbeat.for_user("ol-u9") == []


def test_chat_add_requires_prompt():
    out = gateway.reply_for({}, "u9", "/heartbeat add 2h")
    assert any("Usage" in c for c in out)

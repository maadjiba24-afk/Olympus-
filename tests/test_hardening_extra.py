"""Hardening coverage for the operator modules: sandbox docker backend +
guards, scheduler job cap, and gateway edge cases."""

from olympus import sandbox, scheduler, slack, discord, gateway


# --- sandbox ---------------------------------------------------------------

def test_docker_cmd_isolated_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    monkeypatch.delenv("OLYMPUS_EXEC_NETWORK", raising=False)
    monkeypatch.setenv("OLYMPUS_EXEC_DOCKER_IMAGE", "python:3.11-slim")
    argv = sandbox._docker_cmd("echo hi", sandbox.workdir(), 60)
    assert argv[0] == "docker" and "run" in argv and "--rm" in argv
    assert "--network" in argv and "none" in argv          # isolated by default
    assert argv[-4:] == ["python:3.11-slim", "sh", "-c", "echo hi"]


def test_docker_cmd_network_opt_in(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    monkeypatch.setenv("OLYMPUS_EXEC_NETWORK", "1")
    argv = sandbox._docker_cmd("echo hi", sandbox.workdir(), 60)
    assert "--network" not in argv                          # network allowed


def test_run_rejects_empty_command(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    r = sandbox.run("   ")
    assert not r.ok and "empty command" in r.output


def test_run_clamps_timeout(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    # absurd timeout is clamped to MAX_TIMEOUT, command still runs
    r = sandbox.run("echo ok", timeout=10**9)
    assert r.ok and "ok" in r.output


def test_unknown_backend_falls_back_to_local(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    r = sandbox.run("echo via-default", be="something-weird")
    assert r.ok and "via-default" in r.output


# --- scheduler -------------------------------------------------------------

def test_scheduler_caps_job_count(monkeypatch):
    monkeypatch.setattr(scheduler, "MAX_JOBS", 5)
    for i in range(8):
        scheduler.add(f"job{i}", "daily", f"task {i}", now=float(i))
    jobs = scheduler.jobs()
    assert len(jobs) == 5
    # the most recently created survive (job3..job7)
    names = {j.name for j in jobs}
    assert "job7" in names and "job0" not in names


# --- gateways --------------------------------------------------------------

def test_slack_handles_app_mention(monkeypatch):
    sent = []
    monkeypatch.setattr(slack, "send", lambda ch, txt: sent.append((ch, txt)))
    monkeypatch.setattr(gateway, "reply_for",
                        lambda bots, uk, text, prefix="": ["mention reply"])
    # app_mention is routed by the background processor (the webhook only acks).
    slack.process_event({"event": {"type": "app_mention", "text": "<@U> hi",
                                    "channel": "C9", "user": "U2"}})
    assert sent == [("C9", "mention reply")]


def test_discord_non_ask_command_maps_to_slash(monkeypatch):
    captured = {}
    monkeypatch.setattr(gateway, "reply_for",
                        lambda bots, uk, text, prefix="": captured.setdefault("t", text) or ["ok"])
    monkeypatch.setattr(discord, "_followup", lambda *a: None)
    payload = {"type": 2, "id": "i", "application_id": "a", "token": "t",
               "member": {"user": {"id": "7"}},
               "data": {"name": "scan", "options": []}}
    discord.process_command(payload)
    assert captured["t"] == "/scan"

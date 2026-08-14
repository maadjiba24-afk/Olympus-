"""Phase 2 (the surveyed gateway reach) — the unified gateway daemon: runs every configured
channel in one process, SUPERVISED so a channel that exits or crashes is
auto-restarted with backoff. Channel `start` callables are injected so tests
exercise selection/launch/restart/health without real credentials or blocking
servers.
"""

import threading
import time

import pytest

from olympus import gateway


def _blocking_start(stop):
    """A healthy channel: blocks like a real server loop until told to stop."""
    def start():
        stop.wait(2.0)
    return start


def _registry(configured=("telegram", "discord"), starts=None):
    """Fake registry: `configured` names are 'configured'; `starts` maps a name
    to its start callable (default: a fast no-op that 'exits')."""
    starts = starts or {}
    def make(name):
        return (lambda n=name: n in configured), starts.get(name, lambda: None)
    return {n: make(n) for n in ("telegram", "discord", "slack", "signal")}


def _fast_sup(report=None, **kw):
    """A supervisor with zero backoff so restart logic runs instantly."""
    return gateway._Supervisor(report=report or (lambda _m: None),
                               base_delay=0.0, max_delay=0.0, **kw)


# --- selection -------------------------------------------------------------

def test_configured_channels_filters():
    reg = _registry(configured=("telegram", "slack"))
    assert set(gateway.configured_channels(reg)) == {"telegram", "slack"}


def test_run_all_starts_each_configured_channel():
    stop = threading.Event()
    reg = _registry(configured=("telegram", "discord"),
                    starts={"telegram": _blocking_start(stop),
                            "discord": _blocking_start(stop)})
    msgs = []
    sup = _fast_sup(report=msgs.append)
    out = gateway.run_all(registry=reg, block=False, report=msgs.append,
                          supervisor=sup)
    assert set(out) == {"telegram", "discord"}
    time.sleep(0.1)
    health = gateway.channel_health(sup)
    assert health["telegram"]["status"] == "running"
    assert any("Gateway daemon live" in m for m in msgs)
    stop.set(); sup.stop()


def test_run_all_only_subset():
    stop = threading.Event()
    reg = _registry(configured=("telegram", "discord", "slack"),
                    starts={n: _blocking_start(stop) for n in
                            ("telegram", "discord", "slack")})
    out = gateway.run_all(only=["discord"], registry=reg, block=False,
                          report=lambda _m: None, supervisor=_fast_sup())
    assert out == ["discord"]
    stop.set()


def test_run_all_no_channels_configured():
    msgs = []
    out = gateway.run_all(registry=_registry(configured=()), block=False,
                          report=msgs.append, supervisor=_fast_sup())
    assert out == [] and any("No channels configured" in m for m in msgs)


def test_run_all_warns_on_unknown_only():
    msgs = []
    gateway.run_all(only=["bogus"], registry=_registry(configured=()),
                    block=False, report=msgs.append, supervisor=_fast_sup())
    assert any("unknown channel 'bogus'" in m for m in msgs)


# --- supervision / auto-restart --------------------------------------------

def test_crashing_channel_is_restarted_then_given_up():
    calls = {"n": 0}
    def crash():
        calls["n"] += 1
        raise RuntimeError("boom")
    reg = _registry(configured=("telegram",), starts={"telegram": crash})
    msgs = []
    sup = _fast_sup(report=msgs.append, max_restarts=3)
    gateway.run_all(registry=reg, block=False, report=msgs.append, supervisor=sup)
    # supervise: initial run + 3 restarts = 4 attempts, then gives up
    for _ in range(200):
        if gateway.channel_health(sup).get("telegram", {}).get("status") == "failed":
            break
        time.sleep(0.01)
    assert calls["n"] == 4                       # 1 initial + 3 restarts
    assert gateway.channel_health(sup)["telegram"]["status"] == "failed"
    assert any("gave up" in m for m in msgs)


def test_channel_that_exits_is_restarted():
    calls = {"n": 0}
    done = threading.Event()
    def flaky():
        calls["n"] += 1
        if calls["n"] >= 2:
            done.set()
            threading.Event().wait(1.0)          # stay 'running' on 2nd start
    reg = _registry(configured=("signal",), starts={"signal": flaky})
    sup = _fast_sup(max_restarts=5)
    gateway.run_all(registry=reg, block=False, report=lambda _m: None,
                    supervisor=sup)
    assert done.wait(2.0)                         # it was restarted at least once
    assert calls["n"] >= 2
    sup.stop()


def test_max_restarts_zero_means_no_restart():
    calls = {"n": 0}
    def crash():
        calls["n"] += 1
        raise RuntimeError("x")
    reg = _registry(configured=("telegram",), starts={"telegram": crash})
    sup = _fast_sup(max_restarts=0)
    gateway.run_all(registry=reg, block=False, report=lambda _m: None,
                    supervisor=sup)
    for _ in range(200):
        if gateway.channel_health(sup).get("telegram", {}).get("status") == "failed":
            break
        time.sleep(0.01)
    assert calls["n"] == 1                        # ran once, never restarted


def test_stop_halts_supervision():
    stop = threading.Event()
    reg = _registry(configured=("telegram",),
                    starts={"telegram": _blocking_start(stop)})
    sup = _fast_sup()
    gateway.run_all(registry=reg, block=False, report=lambda _m: None,
                    supervisor=sup)
    time.sleep(0.05)
    sup.stop(); stop.set()
    time.sleep(0.05)
    assert sup._stop.is_set()


# --- real registry + CLI ---------------------------------------------------

def test_real_registry_shape():
    reg = gateway._channel_registry()
    assert {"telegram", "signal", "email", "discord", "slack", "whatsapp",
            "webhook"} <= set(reg)
    for name, (pred, start) in reg.items():
        assert callable(pred) and callable(start)


def test_cli_gateway_list(monkeypatch, capsys):
    from olympus import cli
    monkeypatch.setattr(gateway, "configured_channels", lambda: ["telegram"])
    cli.main(["gateway", "--list"])
    assert "telegram" in capsys.readouterr().out


# --- cross-process status file ---------------------------------------------

def test_status_file_reports_running_daemon(monkeypatch, tmp_path):
    from olympus import config
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    stop = threading.Event()
    reg = _registry(configured=("telegram",),
                    starts={"telegram": _blocking_start(stop)})
    sup = gateway._Supervisor(report=lambda _m: None, persist=True,
                              base_delay=0.0)
    gateway.run_all(registry=reg, block=False, report=lambda _m: None,
                    supervisor=sup)
    time.sleep(0.1)
    st = gateway.read_status()
    assert st["running"] is True
    assert st["pid"] == __import__("os").getpid()
    assert st["channels"]["telegram"]["status"] == "running"
    stop.set(); sup.stop()


def test_read_status_missing_file(monkeypatch, tmp_path):
    from olympus import config
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    st = gateway.read_status()
    assert st["running"] is False and "no gateway daemon status" in st["reason"]


def test_read_status_detects_stale_heartbeat(monkeypatch, tmp_path):
    from olympus import config
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    import json, os
    (tmp_path / "gateway_status.json").write_text(json.dumps({
        "pid": os.getpid(), "started_at": 0, "heartbeat": 1.0, "channels": {}}))
    st = gateway.read_status(stale_after=10)
    assert st["running"] is False and st["stale"] is True


def test_read_status_detects_dead_pid(monkeypatch, tmp_path):
    from olympus import config
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    import json, time
    (tmp_path / "gateway_status.json").write_text(json.dumps({
        "pid": 2_000_000_000, "started_at": 0, "heartbeat": time.time(),
        "channels": {}}))
    st = gateway.read_status()
    assert st["running"] is False and "not running" in st["reason"]


def test_cli_gateway_status_running(monkeypatch, capsys):
    from olympus import cli
    monkeypatch.setattr(gateway, "read_status", lambda: {
        "running": True, "pid": 123, "age_secs": 2.0,
        "channels": {"telegram": {"status": "running", "restarts": 0,
                                  "last_error": ""}}})
    rc = cli.main(["gateway", "--status"])
    out = capsys.readouterr().out
    assert "RUNNING" in out and "telegram" in out
    assert rc in (0, None)


def test_cli_gateway_status_not_running(monkeypatch, capsys):
    from olympus import cli
    monkeypatch.setattr(gateway, "read_status",
                        lambda: {"running": False, "reason": "no status file"})
    rc = cli.main(["gateway", "--status"])
    assert "not running" in capsys.readouterr().out and rc == 1

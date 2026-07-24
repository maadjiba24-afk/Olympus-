"""The native (subprocess-backed) computer-use actuator.

Drives the OS via native CLI tools (xdotool/scrot on Linux, screencapture/cliclick
on macOS, PowerShell on Windows) with NO Python GUI dependency. Every call is
exercised offline through injected fakes — no real OS control — asserting the
right command is built per platform, that typed text goes over STDIN (never the
process table), that a missing tool degrades to a clear error, and that the
opt-in `activate()` requires both operator switches.
"""

from __future__ import annotations

import pytest

from olympus import (actions, behavioral_contracts as abc, computeruse, config,
                     deltas, witness)

_signing = pytest.mark.skipif(not witness.available(),
                              reason="cryptography backend unavailable")


class _Runner:
    """Records every (argv, stdin) and returns success; writes dummy PNG bytes
    to any `.png` path in argv so screenshot's file-read succeeds."""

    def __init__(self, returncode=0, stderr=""):
        self.calls: list[tuple[list[str], str | None]] = []
        self.returncode = returncode
        self.stderr = stderr

    def run(self, argv, stdin=None):
        self.calls.append((list(argv), stdin))
        for tok in argv:
            if tok.endswith(".png"):
                from pathlib import Path
                Path(tok).write_bytes(b"\x89PNG\r\n\x1a\nDUMMY")
        return computeruse._RunResult(self.returncode, "", self.stderr)


def _actuator(system, tmp_path, runner=None, which=None, spawn=None):
    runner = runner or _Runner()
    return computeruse.NativeActuator(
        system=system, run=runner.run,
        which=which or (lambda t: f"/usr/bin/{t}"),
        spawn=spawn, shot_dir=tmp_path), runner


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.delenv("OLYMPUS_COMPUTER_USE", raising=False)
    monkeypatch.delenv("OLYMPUS_COMPUTER_USE_ACTUATOR", raising=False)
    monkeypatch.setattr(deltas, "_locks", {})
    computeruse.reset_actuator()
    yield
    computeruse.reset_actuator()


# --- per-platform command building -------------------------------------------

def test_linux_move_click_key_commands(tmp_path):
    act, r = _actuator("linux", tmp_path)
    act.move(10, 20)
    act.click(30, 40, "right")
    act.key("ctrl+c")
    assert r.calls[0][0] == ["xdotool", "mousemove", "--sync", "10", "20"]
    assert r.calls[1][0] == ["xdotool", "mousemove", "--sync", "30", "40",
                             "click", "3"]                 # right = button 3
    assert r.calls[2][0] == ["xdotool", "key", "--clearmodifiers", "ctrl+c"]


def test_linux_type_feeds_text_over_stdin_not_argv(tmp_path):
    act, r = _actuator("linux", tmp_path)
    secret_ish = "hello world 12345"
    act.type(secret_ish)
    argv, stdin = r.calls[0]
    assert argv == ["xdotool", "type", "--clearmodifiers", "--file", "-"]
    assert stdin == secret_ish                    # text is on STDIN...
    assert secret_ish not in " ".join(argv)       # ...never in the process args


def test_darwin_commands(tmp_path):
    act, r = _actuator("darwin", tmp_path)
    act.move(5, 6)
    act.click(7, 8, "left")
    act.type("hi")
    assert r.calls[0][0] == ["cliclick", "m:5,6"]
    assert r.calls[1][0] == ["cliclick", "c:7,8"]
    # type routes through osascript reading the script (with the text) from stdin
    assert r.calls[2][0] == ["osascript", "-"]
    assert "keystroke" in r.calls[2][1] and "hi" in r.calls[2][1]


def test_windows_commands_use_powershell(tmp_path):
    act, r = _actuator("windows", tmp_path)
    act.move(1, 2)
    argv = r.calls[0][0]
    assert argv[0] == "powershell" and "-Command" in argv
    assert "Cursor]::Position" in argv[-1]


def test_screenshot_returns_base64_png(tmp_path):
    act, r = _actuator("linux", tmp_path)
    out = act.screenshot()
    assert out["ok"] and out["format"] == "png" and out["bytes"] > 0
    assert out["image"]                                    # base64 payload
    assert out["path"].endswith(".png")
    assert r.calls[0][0][0] == "scrot"                     # first available tool


def test_launch_uses_injected_spawn(tmp_path):
    spawned = []
    act, _ = _actuator("linux", tmp_path,
                       spawn=lambda cmd: spawned.append(cmd) or {"ok": True, "launched": True})
    out = act.launch("xterm")
    assert out["launched"] and spawned == ["xterm"]


# --- honest degradation ------------------------------------------------------

def test_missing_tool_raises_clear_error(tmp_path):
    act, _ = _actuator("linux", tmp_path, which=lambda t: None)
    with pytest.raises(computeruse.ComputerUseError) as ei:
        act.move(1, 1)
    assert "xdotool" in str(ei.value) and "not found" in str(ei.value)


def test_nonzero_exit_raises(tmp_path):
    act, _ = _actuator("linux", tmp_path, runner=_Runner(returncode=1, stderr="boom"))
    with pytest.raises(computeruse.ComputerUseError) as ei:
        act.click(1, 1)
    assert "exit 1" in str(ei.value) and "boom" in str(ei.value)


def test_no_linux_screenshot_tool_is_a_clear_error(tmp_path):
    act, _ = _actuator("linux", tmp_path, which=lambda t: None)
    with pytest.raises(computeruse.ComputerUseError) as ei:
        act.screenshot()
    assert "screenshot tool" in str(ei.value)


# --- activate(): two deliberate switches, fail-safe --------------------------

def test_activate_needs_capability_switch(monkeypatch):
    monkeypatch.setenv("OLYMPUS_COMPUTER_USE_ACTUATOR", "native")
    # capability switch OFF → nothing installed
    assert computeruse.activate() is False
    assert computeruse.actuator_name() == "disabled"


def test_activate_needs_actuator_switch(monkeypatch):
    monkeypatch.setenv("OLYMPUS_COMPUTER_USE", "1")
    # actuator switch unset → capability on but still no real actuator
    assert computeruse.activate() is False
    assert computeruse.actuator_name() == "disabled"


def test_activate_installs_native_with_both_switches(monkeypatch):
    monkeypatch.setenv("OLYMPUS_COMPUTER_USE", "1")
    monkeypatch.setenv("OLYMPUS_COMPUTER_USE_ACTUATOR", "native")
    assert computeruse.activate() is True
    assert computeruse.actuator_name() == "native"


# --- end-to-end through the guarded chokepoint -------------------------------

@_signing
def test_perform_click_through_native_actuator_is_audited(tmp_path, monkeypatch):
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", "cu-native-seed")
    monkeypatch.setenv("OLYMPUS_COMPUTER_USE", "1")
    act, r = _actuator("linux", tmp_path)
    out = computeruse.perform("computer_click", {"x": 12, "y": 34},
                              user="op", actuator=act, approved=True)
    assert out["ok"] and out["x"] == 12
    assert r.calls[0][0][:2] == ["xdotool", "mousemove"]
    # every actuation leaves a signed record
    events = [rec["state"].get("event") for rec in computeruse.audit("op")]
    assert "performed" in events


@_signing
def test_perform_click_refused_without_approval(tmp_path, monkeypatch):
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", "cu-native-seed")
    monkeypatch.setenv("OLYMPUS_COMPUTER_USE", "1")
    act, r = _actuator("linux", tmp_path)
    with pytest.raises((computeruse.ComputerUseError, abc.ContractViolation)):
        computeruse.perform("computer_click", {"x": 1, "y": 1},
                            user="op", actuator=act, approved=False)
    assert r.calls == []                          # never touched the actuator


# --- readiness probe + wiring ------------------------------------------------

def test_missing_tools_per_platform(tmp_path):
    # nothing installed → the platform's required tools are reported missing
    lin, _ = _actuator("linux", tmp_path, which=lambda t: None)
    assert "xdotool" in lin.missing_tools()
    mac, _ = _actuator("darwin", tmp_path, which=lambda t: None)
    assert "screencapture" in mac.missing_tools() and "cliclick" in mac.missing_tools()
    # all present → ready
    ok, _ = _actuator("linux", tmp_path, which=lambda t: f"/usr/bin/{t}")
    assert ok.missing_tools() == []


def test_actuator_ready_reflects_installed_actuator(tmp_path):
    assert computeruse.actuator_ready() == (False, [])          # disabled default
    act, _ = _actuator("linux", tmp_path, which=lambda t: f"/usr/bin/{t}")
    computeruse.register_actuator(act)
    assert computeruse.actuator_ready() == (True, [])


def test_computer_actions_are_reachable_via_prepare_action_path():
    # The runtime prepare_action path imports only actions + builtin_actions;
    # after the wiring fix the 6 computer-use ActionTypes are registered there.
    from olympus import actions, builtin_actions  # noqa: F401
    reg = actions.registered()
    for name in ("computer_screenshot", "computer_click", "computer_type",
                 "computer_key", "computer_move", "computer_launch"):
        assert name in reg

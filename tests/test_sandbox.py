"""Sandbox execution: confinement, command running, file write/undo, and that
run_command / write_file are approval-gated actions."""

import pytest

from olympus import actions, builtin_actions, sandbox  # noqa: F401


def test_run_command_captures_output(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    monkeypatch.setenv("OLYMPUS_EXEC_BACKEND", "local")
    res = sandbox.run("echo hello-olympus")
    assert res.ok and res.code == 0
    assert "hello-olympus" in res.output


def test_run_command_nonzero_exit_is_reported_not_raised(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    res = sandbox.run("exit 3")
    assert not res.ok and res.code == 3


def test_run_command_times_out(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    res = sandbox.run("sleep 5", timeout=1)
    assert not res.ok and res.code == 124


def test_write_read_and_undo_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    r = sandbox.write_file("a/b.txt", "first")
    assert sandbox.read_file("a/b.txt") == "first"
    assert r["existed"] is False
    # overwrite, then undo restores the prior content
    r2 = sandbox.write_file("a/b.txt", "second")
    assert r2["existed"] is True
    assert "restored" in sandbox.undo_write(r2)
    assert sandbox.read_file("a/b.txt") == "first"
    # undo of the original (new) write deletes the file
    assert "deleted" in sandbox.undo_write(r)
    assert sandbox.read_file("a/b.txt").startswith("Error")


def test_path_confinement_blocks_escape(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    with pytest.raises(ValueError):
        sandbox.write_file("../../etc/evil", "x")
    assert sandbox.read_file("../../../etc/passwd").startswith("Error")


def test_list_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    sandbox.write_file("one.txt", "1")
    sandbox.write_file("sub/two.txt", "2")
    listing = sandbox.list_dir(".")
    assert "one.txt" in listing and "sub/" in listing


def test_exec_actions_are_registered_and_gated():
    reg = actions.registered()
    assert "run_command" in reg and "write_file" in reg
    assert reg["run_command"].risk_class == actions.IRREVERSIBLE  # never auto-runs
    assert reg["write_file"].reversible                           # undoable

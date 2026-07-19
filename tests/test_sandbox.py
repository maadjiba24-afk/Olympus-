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


# --- post-write verification (Hermes v0.13/v0.14: delta lint) -------------

def test_write_file_reports_python_syntax_error(tmp_path, monkeypatch):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path))
    res = sandbox.write_file("bad.py", "def broken(:\n    pass\n")
    assert "syntax check FAILED" in res["check"]
    assert "line 1" in res["check"]


def test_write_file_verifies_clean_python(tmp_path, monkeypatch):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path))
    res = sandbox.write_file("good.py", "x = 1\n")
    assert res["check"] == "verified: python syntax OK"


def test_write_file_checks_json_toml(tmp_path, monkeypatch):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path))
    assert sandbox.write_file("a.json", '{"k": 1}')["check"] == "verified: valid JSON"
    assert "FAILED" in sandbox.write_file("b.json", "{nope")["check"]
    # TOML checking uses stdlib tomllib (3.11+) or the tomli backport; like the
    # YAML branch it degrades gracefully when neither parser is present (3.10
    # without tomli), rather than reporting a false failure.
    try:
        import tomllib  # noqa: F401
        _have_toml = True
    except ModuleNotFoundError:
        try:
            import tomli  # noqa: F401
            _have_toml = True
        except ModuleNotFoundError:
            _have_toml = False
    if _have_toml:
        assert sandbox.write_file("c.toml", 'k = 1')["check"] == "verified: valid TOML"
        assert "FAILED" in sandbox.write_file("d.toml", "= broken")["check"]
    else:
        assert (sandbox.write_file("c.toml", 'k = 1')["check"]
                == "verified: written (toml parser unavailable)")


def test_write_file_plain_text_is_verified_written(tmp_path, monkeypatch):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path))
    assert sandbox.write_file("note.txt", "hello")["check"] == "verified: written"


# --- activity-based timeouts + watch patterns (Hermes v0.8/v0.9) ----------

def test_active_command_outlives_its_idle_timeout(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    monkeypatch.setenv("OLYMPUS_EXEC_BACKEND", "local")
    # Emits a line every 0.5s for ~2.5s: silent-idle never exceeds the 1s
    # timeout, so the command completes even though it runs past 1s total.
    res = sandbox.run(
        "for i in 1 2 3 4 5; do echo tick$i; sleep 0.5; done", timeout=1)
    assert res.ok
    assert "tick5" in res.output


def test_timeout_returns_partial_output(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    monkeypatch.setenv("OLYMPUS_EXEC_BACKEND", "local")
    res = sandbox.run("echo started; sleep 5", timeout=1)
    assert not res.ok and res.code == 124
    assert "timed out" in res.output
    assert "started" in res.output          # partial output preserved


def test_watch_pattern_collects_matching_lines(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    monkeypatch.setenv("OLYMPUS_EXEC_BACKEND", "local")
    res = sandbox.run("echo ok; echo 'ERROR: disk full'; echo done",
                      watch=r"ERROR")
    assert res.ok
    assert res.watched == ("ERROR: disk full",)
    assert "[watch] ERROR: disk full" in res.render()


def test_invalid_watch_pattern_is_reported_not_fatal(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    monkeypatch.setenv("OLYMPUS_EXEC_BACKEND", "local")
    res = sandbox.run("echo hi", watch="[unclosed")
    assert res.ok
    assert "invalid watch pattern" in res.watched[0]

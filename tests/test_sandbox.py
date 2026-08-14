"""Sandbox execution: confinement, command running, file write/undo, and that
run_command / write_file are approval-gated actions."""

import os
import shlex
import subprocess
import sys

import pytest


def _python_cmd(code: str) -> str:
    argv = [sys.executable, "-c", code]
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)

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
    res = sandbox.run(
    _python_cmd("import time; time.sleep(5)"),
    timeout=1,
)
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


# --- run_python: Python execution routed through the same gate as run() -----

def test_run_python_captures_stdout(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    monkeypatch.setenv("OLYMPUS_EXEC_BACKEND", "local")
    res = sandbox.run_python("print(6 * 7)")
    assert res.ok and res.code == 0
    assert "42" in res.output


def test_run_python_empty_code_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    res = sandbox.run_python("   ")
    assert not res.ok and res.code == 2


def test_run_python_shell_metachars_in_source_are_inert(monkeypatch, tmp_path):
    # The quoted heredoc means the shell never expands the snippet: a $(...) in
    # a Python string is data, not a command substitution.
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    monkeypatch.setenv("OLYMPUS_EXEC_BACKEND", "local")
    res = sandbox.run_python("print('$(echo pwned)')")
    assert res.ok and "$(echo pwned)" in res.output and "pwned\n" not in res.output


def test_run_python_reserved_marker_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    res = sandbox.run_python(f"x = 1  # {sandbox._PY_HEREDOC}")
    assert not res.ok and res.code == 2


def test_run_python_action_is_registered_irreversible_exec():
    reg = actions.registered()
    assert "run_python" in reg
    rp = reg["run_python"]
    assert rp.risk_class == actions.IRREVERSIBLE   # never auto-runs, even at L4
    assert rp.scope == "exec"                      # shares run_command's scope


def test_run_python_still_passes_the_command_gate(monkeypatch, tmp_path):
    # cmdguard is shell-only, but the heredoc body still transits it: an obvious
    # shell-shaped payload embedded in the "code" is refused, fail-closed.
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    monkeypatch.setenv("OLYMPUS_EXEC_BACKEND", "local")
    res = sandbox.run_python("rm -rf / --no-preserve-root")
    assert not res.ok and res.code == 126        # blocked by the security gate


# --- docker backend hardening: resource caps land in the argv ---------------

def test_docker_cmd_has_default_resource_caps(monkeypatch, tmp_path):
    for var in ("OLYMPUS_EXEC_MEM", "OLYMPUS_EXEC_PIDS", "OLYMPUS_EXEC_CPUS",
                "OLYMPUS_EXEC_USER", "OLYMPUS_EXEC_NETWORK"):
        monkeypatch.delenv(var, raising=False)
    argv = sandbox._docker_cmd("echo hi", tmp_path, 30)
    assert "--network" in argv and "none" in argv       # isolation kept
    assert "--memory" in argv and "512m" in argv        # default mem cap
    assert "--pids-limit" in argv and "256" in argv     # default pid cap
    assert "--cpus" not in argv and "--user" not in argv  # opt-in, off by default


def test_docker_cmd_caps_are_tunable_and_disablable(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_MEM", "off")
    monkeypatch.setenv("OLYMPUS_EXEC_PIDS", "128")
    monkeypatch.setenv("OLYMPUS_EXEC_CPUS", "1.5")
    monkeypatch.setenv("OLYMPUS_EXEC_USER", "1000:1000")
    argv = sandbox._docker_cmd("echo hi", tmp_path, 30)
    assert "--memory" not in argv                       # disabled with off
    assert "--pids-limit" in argv and "128" in argv
    assert "--cpus" in argv and "1.5" in argv
    assert "--user" in argv and "1000:1000" in argv


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
    _python_cmd(
        "import time; "
        "[(print(f'tick{i}', flush=True), time.sleep(0.5)) "
        "for i in range(1, 6)]"
    ),
    timeout=1,
)

    assert res.ok
    assert "tick5" in res.output


def test_timeout_returns_partial_output(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    monkeypatch.setenv("OLYMPUS_EXEC_BACKEND", "local")
    res = sandbox.run(
    _python_cmd(
        "import time; print('started', flush=True); time.sleep(5)"
    ),
    timeout=1,
)
    assert not res.ok and res.code == 124
    assert "timed out" in res.output
    assert "started" in res.output          # partial output preserved


def test_watch_pattern_collects_matching_lines(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    monkeypatch.setenv("OLYMPUS_EXEC_BACKEND", "local")
    res = sandbox.run(
        _python_cmd(
            "print('ok'); print('ERROR: disk full'); print('done')"
        ),
        watch=r"ERROR",
    )
    assert res.ok
    assert res.watched == ("ERROR: disk full",)
    assert "[watch] ERROR: disk full" in res.render()


def test_invalid_watch_pattern_is_reported_not_fatal(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    monkeypatch.setenv("OLYMPUS_EXEC_BACKEND", "local")
    res = sandbox.run("echo hi", watch="[unclosed")
    assert res.ok
    assert "invalid watch pattern" in res.watched[0]


def test_docker_drops_caps_and_blocks_privesc_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("OLYMPUS_EXEC_CAPS", raising=False)
    argv = sandbox._docker_cmd("echo hi", tmp_path, 30)
    assert "--cap-drop" in argv and "ALL" in argv
    assert "no-new-privileges" in argv
    monkeypatch.setenv("OLYMPUS_EXEC_CAPS", "on")
    assert "--cap-drop" not in sandbox._docker_cmd("echo hi", tmp_path, 30)


def test_run_python_rejects_oversized_code(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    res = sandbox.run_python("x=1\n" * 100_000)
    assert not res.ok and res.code == 2 and "too large" in res.output


# --- Wave-0 hardening: secret confinement + fail-closed isolation -----------
# These tests exist because `run()` used to pass `{**os.environ}` to the child,
# handing every provider key, the vault key and the signing seed to any command
# a user approved. They assert the exfiltration path is CLOSED, not merely that
# the scrubbing code runs.

def test_child_env_strips_the_vault_key_and_signing_seed(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    monkeypatch.setenv("OLYMPUS_EXEC_BACKEND", "local")
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "vault-key-must-not-leak")
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", "seed-must-not-leak")
    env = sandbox._child_env()
    assert "OLYMPUS_SECRET_KEY" not in env
    assert "OLYMPUS_SIGNING_SEED" not in env
    assert env["OLYMPUS_IN_SANDBOX"] == "1"


def test_child_env_strips_provider_credentials_by_shape(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-must-not-leak")
    monkeypatch.setenv("SOME_SERVICE_TOKEN", "tok-must-not-leak")
    monkeypatch.setenv("DB_PASSWORD", "pw-must-not-leak")
    monkeypatch.setenv("HARMLESS_SETTING", "keep-me")
    env = sandbox._child_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert "SOME_SERVICE_TOKEN" not in env
    assert "DB_PASSWORD" not in env
    assert env["HARMLESS_SETTING"] == "keep-me"


def test_env_passthrough_allowlist_readmits_named_variables(monkeypatch):
    monkeypatch.setenv("BUILD_TOKEN", "needed-by-the-build")
    monkeypatch.setenv("OTHER_TOKEN", "not-needed")
    monkeypatch.setenv("OLYMPUS_EXEC_ENV_PASSTHROUGH", "BUILD_TOKEN")
    env = sandbox._child_env()
    assert env["BUILD_TOKEN"] == "needed-by-the-build"
    assert "OTHER_TOKEN" not in env


def test_approved_command_cannot_read_a_secret_from_the_environment(
        monkeypatch, tmp_path):
    """End-to-end: the exfiltration primitive from the audit, executed for real."""
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    monkeypatch.setenv("OLYMPUS_EXEC_BACKEND", "local")
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "canary-vault-key-9f3a")
    res = sandbox.run(_python_cmd(
        "import os; print('LEAK:' + os.environ.get('OLYMPUS_SECRET_KEY', 'ABSENT'))"))
    assert res.ok, res.output
    assert "canary-vault-key-9f3a" not in res.output
    assert "LEAK:ABSENT" in res.output


def test_run_python_also_runs_without_the_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    monkeypatch.setenv("OLYMPUS_EXEC_BACKEND", "local")
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "canary-vault-key-7b21")
    res = sandbox.run_python(
        "import os\nprint(os.environ.get('OLYMPUS_SECRET_KEY', 'ABSENT'))")
    assert res.ok, res.output
    assert "canary-vault-key-7b21" not in res.output
    assert "ABSENT" in res.output


def test_sovereign_mode_refuses_the_unisolated_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    monkeypatch.setenv("OLYMPUS_EXEC_BACKEND", "local")
    monkeypatch.setenv("OLYMPUS_SOVEREIGN", "1")
    res = sandbox.run("echo should-not-run")
    assert not res.ok and res.code == 126
    assert "sovereign" in res.output.lower()
    assert "should-not-run" not in res.output


def test_production_mode_refuses_the_unisolated_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    monkeypatch.setenv("OLYMPUS_EXEC_BACKEND", "local")
    monkeypatch.setenv("OLYMPUS_ENV", "production")
    res = sandbox.run("echo should-not-run")
    assert not res.ok and res.code == 126
    assert "production" in res.output.lower()


def test_unisolated_escape_hatch_is_explicit_and_works(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    monkeypatch.setenv("OLYMPUS_EXEC_BACKEND", "local")
    monkeypatch.setenv("OLYMPUS_ENV", "production")
    monkeypatch.setenv("OLYMPUS_EXEC_ALLOW_UNISOLATED", "1")
    res = sandbox.run("echo hatch-open")
    assert res.ok and "hatch-open" in res.output


def test_isolated_backends_are_never_refused(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SOVEREIGN", "1")
    monkeypatch.setenv("OLYMPUS_ENV", "production")
    assert sandbox._isolation_refusal("docker") is None
    assert sandbox._isolation_refusal("daytona") is None
    assert sandbox._isolation_refusal("local") is not None


def test_dev_mode_still_allows_local_without_ceremony(monkeypatch, tmp_path):
    """The hardening must not break an ordinary developer laptop."""
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    monkeypatch.setenv("OLYMPUS_EXEC_BACKEND", "local")
    monkeypatch.delenv("OLYMPUS_SOVEREIGN", raising=False)
    monkeypatch.delenv("OLYMPUS_ENV", raising=False)
    res = sandbox.run("echo dev-ok")
    assert res.ok and "dev-ok" in res.output


def test_auto_backend_resolves_to_docker_when_the_daemon_is_up(monkeypatch):
    monkeypatch.setenv("OLYMPUS_EXEC_BACKEND", "auto")
    monkeypatch.setattr(sandbox, "_AUTO_BACKEND", [None])
    monkeypatch.setattr(sandbox, "_docker_available", lambda: True)
    assert sandbox.backend() == "docker"


def test_auto_backend_degrades_to_local_without_a_daemon(monkeypatch):
    monkeypatch.setenv("OLYMPUS_EXEC_BACKEND", "auto")
    monkeypatch.setattr(sandbox, "_AUTO_BACKEND", [None])
    monkeypatch.setattr(sandbox, "_docker_available", lambda: False)
    assert sandbox.backend() == "local"

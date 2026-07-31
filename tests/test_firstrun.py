"""Tests for the first-run experience: saved keys + the setup wizard."""

import os

import pytest

from olympus import firstrun


_VARS = ("ANTHROPIC_API_KEY", "OLYMPUS_API_KEY", "OPENAI_API_KEY",
         "OLYMPUS_BASE_URL", "OLYMPUS_PROVIDER", "OLYMPUS_MODEL")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    # snapshot/restore explicitly: the code under test writes os.environ
    # directly, which monkeypatch can't undo for vars that didn't exist
    before = {v: os.environ.get(v) for v in _VARS}
    for var in _VARS:
        os.environ.pop(var, None)
    monkeypatch.setattr(firstrun, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(firstrun, "CONFIG_ENV", tmp_path / "config.env")
    yield
    for var, value in before.items():
        if value is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = value


def test_load_env_file_applies_saved_values():
    firstrun.CONFIG_ENV.write_text(
        "# comment\nANTHROPIC_API_KEY=sk-ant-saved\n\nOLYMPUS_MODEL='m'\n")
    assert firstrun.load_env_file() == 2
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-saved"
    assert os.environ["OLYMPUS_MODEL"] == "m"      # quotes stripped


def test_env_vars_win_over_saved_file(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real-env")
    firstrun.CONFIG_ENV.write_text("ANTHROPIC_API_KEY=sk-ant-saved\n")
    firstrun.load_env_file()
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-real-env"


def test_missing_file_is_fine():
    assert firstrun.load_env_file() == 0
    assert not firstrun.configured()


def test_configured_detects_any_key(monkeypatch):
    assert not firstrun.configured()
    monkeypatch.setenv("OLYMPUS_API_KEY", "sk-x")
    assert firstrun.configured()


def test_wizard_anthropic_saves_and_restricts(monkeypatch):
    from olympus import providers
    monkeypatch.setattr(providers, "fetch_models", lambda *a, **k: [])  # no network
    monkeypatch.setattr(firstrun, "_env_detected", lambda: [])
    # Full; Anthropic is the merged entry #1 → auth mode #2 (API key) → model #1
    # → no more → fast n, sandbox n, msg n
    answers = iter(["2", "1", "2", "1", "n", "n", "n", "n"])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
    monkeypatch.setattr(firstrun, "_ask_secret", lambda *_: "sk-ant-wizard")
    assert firstrun.wizard() is True
    text = firstrun.CONFIG_ENV.read_text()
    assert "ANTHROPIC_API_KEY=sk-ant-wizard" in text
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-wizard"
    if os.name != "nt":
        assert (firstrun.CONFIG_ENV.stat().st_mode & 0o777) == 0o600


def test_wizard_empty_key_cancels(monkeypatch):
    from olympus import providers
    monkeypatch.setattr(providers, "fetch_models", lambda *a, **k: [])
    monkeypatch.setattr(firstrun, "_env_detected", lambda: [])
    # Full; provider #2 (openai) but no key → skipped; decline another → none
    answers = iter(["2", "2", "n"])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
    monkeypatch.setattr(firstrun, "_ask_secret", lambda *_: "")
    assert firstrun.wizard() is False
    assert not firstrun.CONFIG_ENV.exists()


def test_ensure_ready_noninteractive_fails_politely(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert firstrun.ensure_ready() is False
    assert "olympus setup" in capsys.readouterr().err


def test_ensure_ready_uses_saved_config(monkeypatch):
    firstrun.CONFIG_ENV.write_text("ANTHROPIC_API_KEY=sk-ant-saved\n")
    assert firstrun.ensure_ready() is True


def test_setup_command_exists():
    from olympus import cli
    # `olympus setup` runs the wizard (patched here to avoid interaction)
    import olympus.firstrun as fr
    called = {}
    orig = fr.wizard
    fr.wizard = lambda: called.setdefault("yes", True)
    try:
        cli.main(["setup"])
    finally:
        fr.wizard = orig
    assert called.get("yes") is True

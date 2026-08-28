"""Tier C: doctor readiness, config show/set (masked), progress modes, dashboard."""

import os

import pytest

from olympus import config, doctor, firstrun


# --- doctor ---------------------------------------------------------------

def test_doctor_reports_missing_provider(monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "OLYMPUS_API_KEY", "OPENAI_API_KEY",
              "OLYMPUS_BASE_URL"):
        monkeypatch.delenv(v, raising=False)
    checks = doctor.run_checks()
    prov = next(c for c in checks if c.name == "provider")
    assert prov.status == doctor.FAIL
    assert not doctor.is_ready(checks)


def test_doctor_ready_with_provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    checks = doctor.run_checks()
    prov = next(c for c in checks if c.name == "provider")
    assert prov.status == doctor.OK
    assert doctor.is_ready(checks)


def test_doctor_flags_disabled_command_gate(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("OLYMPUS_EXEC_SECURITY", "off")
    gate = next(c for c in doctor.run_checks() if c.name == "command gate")
    assert gate.status == doctor.WARN and "DISABLED" in gate.detail


def test_doctor_render_has_summary_and_caps(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    out = doctor.render()
    assert "readiness check" in out
    assert "Capabilities" in out and "specialists" in out


# --- config show/set ------------------------------------------------------

@pytest.fixture
def temp_config(tmp_path, monkeypatch):
    monkeypatch.setattr(firstrun, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(firstrun, "CONFIG_ENV", tmp_path / "config.env")
    return tmp_path


def test_config_set_masks_secret_in_output(temp_config):
    msg = firstrun.config_set("ANTHROPIC_API_KEY", "sk-supersecret-9999")
    assert "9999" in msg and "supersecret" not in msg
    # but the real value is written to the file
    assert "sk-supersecret-9999" in (temp_config / "config.env").read_text()


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
def test_config_is_private_before_secret_bytes_are_published(temp_config):
    firstrun.config_set("ANTHROPIC_API_KEY", "sk-owner-only")
    assert ((temp_config / "config.env").stat().st_mode & 0o777) == 0o600
    assert (temp_config.stat().st_mode & 0o777) == 0o700


def test_config_rejects_newline_injection_without_replacing_file(temp_config):
    firstrun.config_set("OLYMPUS_FAST", "1")
    path = temp_config / "config.env"
    before = path.read_bytes()
    with pytest.raises(firstrun.ConfigFileError, match="one line"):
        firstrun.config_set(
            "SLACK_NOTIFY_CHANNEL",
            "#ops\nOLYMPUS_EXEC_SECURITY=off")
    assert path.read_bytes() == before
    assert "OLYMPUS_EXEC_SECURITY=off" not in path.read_text()


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
def test_loader_refuses_broadly_readable_saved_secrets(temp_config):
    firstrun.config_set("ANTHROPIC_API_KEY", "sk-owner-only")
    path = temp_config / "config.env"
    path.chmod(0o644)
    with pytest.raises(firstrun.ConfigFileError, match="chmod 600"):
        firstrun.load_env_file()


def test_loader_reports_malformed_utf8_as_config_error(temp_config):
    path = temp_config / "config.env"
    path.write_bytes(b"ANTHROPIC_API_KEY=\xff\n")
    path.chmod(0o600)
    with pytest.raises(firstrun.ConfigFileError, match="cannot be read safely"):
        firstrun.load_env_file()


def test_config_set_nonsecret_shows_value(temp_config):
    msg = firstrun.config_set("OLYMPUS_FAST", "1")
    assert "OLYMPUS_FAST=1" in msg


def test_config_show_masks_secrets(temp_config):
    firstrun.config_set("OPENAI_API_KEY", "sk-abcd1234efgh")
    firstrun.config_set("OLYMPUS_MODEL", "gpt-4o")
    shown = firstrun.show_config()
    assert "gpt-4o" in shown
    assert "sk-abcd1234efgh" not in shown and "efgh" in shown


def test_config_show_empty(temp_config):
    assert "No saved config" in firstrun.show_config()


# --- progress verbosity ---------------------------------------------------

def test_progress_mode_default_and_override(monkeypatch):
    monkeypatch.delenv("OLYMPUS_PROGRESS", raising=False)
    assert config.progress_mode() == "all"
    monkeypatch.setenv("OLYMPUS_PROGRESS", "stages")
    assert config.progress_mode() == "stages"
    monkeypatch.setenv("OLYMPUS_PROGRESS", "bogus")
    assert config.progress_mode() == "all"      # invalid → default


def test_progress_allows_by_mode():
    stage_line = "⚡ Zeus delegates → Athena"
    verify_line = "🔍 Aletheia verifies the findings..."
    tool_line = "  fetching a page..."
    assert config.progress_allows(stage_line, "off") is False
    assert config.progress_allows(stage_line, "all") is True
    # stages mode: pipeline markers (incl. verification) show; tool chatter hidden
    assert config.progress_allows(stage_line, "stages") is True
    assert config.progress_allows(verify_line, "stages") is True
    assert config.progress_allows(tool_line, "stages") is False


# --- capability dashboard -------------------------------------------------

def test_capability_summary_lists_council():
    summary = doctor.capability_summary()
    assert "council:" in summary and "Zeus" not in summary  # Zeus isn't a specialist
    assert "Plutus" in summary

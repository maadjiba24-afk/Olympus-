"""Adaptive fast-mode: OLYMPUS_FAST=auto resolves per message, and the /fast
chat command sets on/off/auto. The orchestrator uses the per-turn value so an
'auto' decision is consistent across all pipeline stages."""

import pytest

from olympus import config, gateway


# --- setting normalization -------------------------------------------------

def test_fast_setting_values(monkeypatch):
    monkeypatch.delenv("OLYMPUS_FAST", raising=False)
    assert config.fast_setting() == "off"
    assert config.fast_mode() is False

    for on in ("1", "true", "yes", "on", "ON"):
        monkeypatch.setenv("OLYMPUS_FAST", on)
        assert config.fast_setting() == "on"
        assert config.fast_mode() is True

    monkeypatch.setenv("OLYMPUS_FAST", "auto")
    assert config.fast_setting() == "auto"
    assert config.fast_auto() is True
    # auto is NOT globally truthy — the per-turn heuristic decides.
    assert config.fast_mode() is False


# --- the heuristic ---------------------------------------------------------

def test_estimate_fast_simple_is_fast():
    assert config.estimate_fast("hi") is True
    assert config.estimate_fast("what's the capital of France?") is True
    assert config.estimate_fast("translate 'hello' to Spanish") is True


def test_estimate_fast_heavy_words_go_full():
    assert config.estimate_fast("analyze this codebase for bugs") is False
    assert config.estimate_fast("compare Postgres and MySQL") is False
    assert config.estimate_fast("do a security review of my auth flow") is False
    assert config.estimate_fast("design an architecture for this") is False


def test_estimate_fast_long_or_structured_goes_full():
    assert config.estimate_fast("word " * 60) is False          # long
    assert config.estimate_fast("```\ncode\n```") is False       # code fence
    assert config.estimate_fast("a\nb\nc\nd\ne") is False        # many lines
    assert config.estimate_fast("what? why? how? really?") is False  # many Qs


def test_fast_mode_for_respects_explicit(monkeypatch):
    monkeypatch.setenv("OLYMPUS_FAST", "1")
    assert config.fast_mode_for("analyze everything deeply") is True  # forced on
    monkeypatch.setenv("OLYMPUS_FAST", "0")
    assert config.fast_mode_for("hi") is False                         # forced off
    monkeypatch.setenv("OLYMPUS_FAST", "auto")
    assert config.fast_mode_for("hi") is True
    assert config.fast_mode_for("analyze this thoroughly") is False


# --- orchestrator per-turn resolution --------------------------------------

def test_orchestrator_fast_turn(monkeypatch):
    from olympus import orchestrator
    monkeypatch.setenv("OLYMPUS_PROVIDER", "anthropic")
    monkeypatch.setenv("OLYMPUS_MODEL", "claude-opus-5")
    monkeypatch.setenv("OLYMPUS_API_KEY", "k")
    monkeypatch.setenv("OLYMPUS_FAST", "auto")
    bot = orchestrator.Olympus(user="ftest")
    # No turn resolved yet → falls back to the (auto→off) global.
    assert bot._fast() is False
    bot._fast_turn = True
    assert bot._fast() is True
    bot._fast_turn = False
    assert bot._fast() is False


# --- /fast chat command ----------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    # keep the saved-config write inside the test dir
    monkeypatch.setenv("OLYMPUS_HOME", str(tmp_path))


def test_fast_command_sets_and_reports(monkeypatch):
    monkeypatch.setenv("OLYMPUS_FAST", "0")
    out = " ".join(gateway.reply_for({}, "u1", "/fast auto"))
    assert "AUTO" in out
    assert config.fast_setting() == "auto"

    out = " ".join(gateway.reply_for({}, "u1", "/fast on"))
    assert "ON" in out
    assert config.fast_setting() == "on"

    out = " ".join(gateway.reply_for({}, "u1", "/fast off"))
    assert "OFF" in out
    assert config.fast_setting() == "off"


def test_fast_command_status_and_usage(monkeypatch):
    monkeypatch.setenv("OLYMPUS_FAST", "auto")
    out = " ".join(gateway.reply_for({}, "u2", "/fast"))
    assert "auto" in out.lower()
    out = " ".join(gateway.reply_for({}, "u2", "/fast wat"))
    assert "Usage" in out

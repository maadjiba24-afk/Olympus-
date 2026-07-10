"""Olympus assumes NO model — vendor-neutral by default. The user chooses one
explicitly (OLYMPUS_MODEL, `olympus setup`, or per-request BYOK); a missing
choice fails fast with an actionable message instead of silently running a
vendor's model.
"""

import pytest

from olympus import config


@pytest.fixture(autouse=True)
def no_model_env(monkeypatch):
    monkeypatch.delenv("OLYMPUS_MODEL", raising=False)
    monkeypatch.delenv("OLYMPUS_GATE_MODEL", raising=False)
    monkeypatch.delenv("OLYMPUS_MODELS", raising=False)


def test_from_env_assumes_no_model(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    s = config.Settings.from_env()
    assert s.model == ""


def test_dataclass_default_is_empty():
    assert config.Settings().model == ""


def test_default_model_reads_env_only(monkeypatch):
    assert config.default_model() == ""
    monkeypatch.setenv("OLYMPUS_MODEL", "my-model")
    assert config.default_model() == "my-model"


def test_validate_requires_a_chosen_model():
    err = config.Settings(provider="anthropic").validate()
    assert err and "No model chosen" in err
    # openai must name a model on the settings themselves — an env
    # OLYMPUS_MODEL belongs to the primary provider and could be another
    # vendor's ID, so it never satisfies an explicit openai config.
    err = config.Settings(provider="openai", api_key="k").validate()
    assert err and "model" in err.lower()


def test_env_model_never_excuses_openai(monkeypatch):
    monkeypatch.setenv("OLYMPUS_MODEL", "claude-something")
    assert config.Settings(provider="openai", api_key="k").validate()


def test_env_model_satisfies_validate(monkeypatch):
    monkeypatch.setenv("OLYMPUS_MODEL", "my-model")
    assert config.Settings(provider="anthropic").validate() is None


def test_claude_code_and_moa_delegate_model_choice():
    # claude-code defers to that CLI's own login default; moa rides members.
    assert config.Settings(provider="claude-code").validate() is None
    assert config.Settings(provider="moa").validate() is None


def test_require_model_raises_actionably():
    with pytest.raises(ValueError) as err:
        config.require_model(config.Settings(provider="anthropic"))
    assert "olympus setup" in str(err.value)


def test_require_model_prefers_settings_then_env(monkeypatch):
    assert config.require_model(config.Settings(model="chosen")) == "chosen"
    monkeypatch.setenv("OLYMPUS_MODEL", "env-model")
    assert config.require_model(config.Settings()) == "env-model"


def test_provider_switch_never_invents_a_model():
    base = config.Settings(provider="openai", model="gpt-x", api_key="k")
    switched = base.merged({"provider": "anthropic"})
    assert switched.model == ""          # caller must choose, not inherit


def test_gate_runs_on_configured_model_when_gate_model_unset(monkeypatch):
    # With no OLYMPUS_GATE_MODEL the replay gate must run on the user's own
    # configured model unchanged — not swap in a vendor's cheaper model.
    from olympus import replaygate
    monkeypatch.setattr(config, "GATE_MODEL", "")
    chosen = config.Settings(provider="anthropic", model="my-chosen-model",
                             api_key="k")
    monkeypatch.setattr(config.Settings, "from_env",
                        classmethod(lambda cls: chosen))
    monkeypatch.setattr(config.ModelPool, "from_env",
                        classmethod(lambda cls: config.ModelPool.of(chosen)))
    captured = {}

    class FakeBot:
        def __init__(self, pool=None, user=""):
            captured["model"] = pool.primary().model

    monkeypatch.setattr(replaygate.orchestrator, "Olympus", FakeBot)
    replaygate._gate_bot()
    assert captured["model"] == "my-chosen-model"


def test_doctor_flags_missing_model(monkeypatch):
    from olympus import doctor, firstrun
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(firstrun, "configured", lambda: True)
    checks = doctor._provider_checks()
    prov = next(c for c in checks if c.name == "provider")
    assert prov.status == doctor.FAIL and "no model chosen" in prov.detail


def test_health_reads_missing_model_as_down(monkeypatch):
    from olympus import health
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    c = health._models()
    assert c["status"] == "down"

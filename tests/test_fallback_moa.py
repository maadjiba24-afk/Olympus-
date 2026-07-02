"""Provider failover chains (Hermes v0.6/v0.7) and the MoA ensemble
provider (Hermes v0.18)."""

import json

import pytest

from olympus import backend, config, moa


def _pool_env(monkeypatch, *members):
    """Configure the operator env pool: first member is primary."""
    first = members[0]
    monkeypatch.setenv("OLYMPUS_PROVIDER", first["provider"])
    monkeypatch.setenv("OLYMPUS_MODEL", first.get("model", ""))
    if first.get("api_key"):
        monkeypatch.setenv("OLYMPUS_API_KEY", first["api_key"])
    if first.get("base_url"):
        monkeypatch.setenv("OLYMPUS_BASE_URL", first["base_url"])
    monkeypatch.setenv("OLYMPUS_MODELS", json.dumps(list(members[1:])))


# --- failover ---------------------------------------------------------------

def test_failover_to_next_pool_member(monkeypatch):
    _pool_env(monkeypatch,
              {"provider": "openai", "model": "gpt-5", "api_key": "k1"},
              {"provider": "openai", "model": "deepseek-v3", "api_key": "k2"})
    calls = []

    def fake(settings, system, messages, effort):
        calls.append(settings.model)
        if settings.model == "gpt-5":
            raise RuntimeError("401 unauthorized")
        return "answer from fallback"

    monkeypatch.setattr(backend.openai_compat, "complete_text", fake)
    primary = config.ModelPool.from_env().primary()
    out = backend.complete_text(primary, "sys", [{"role": "user", "content": "q"}])
    assert out == "answer from fallback"
    assert calls == ["gpt-5", "deepseek-v3"]


def test_byok_settings_never_fail_over(monkeypatch):
    _pool_env(monkeypatch,
              {"provider": "openai", "model": "gpt-5", "api_key": "k1"},
              {"provider": "openai", "model": "deepseek-v3", "api_key": "k2"})

    def always_fail(settings, system, messages, effort):
        raise RuntimeError("401 unauthorized")

    monkeypatch.setattr(backend.openai_compat, "complete_text", always_fail)
    visitor = config.Settings(provider="openai", model="gpt-5",
                              api_key="visitor-key")      # not a pool member
    with pytest.raises(RuntimeError):
        backend.complete_text(visitor, "sys", [{"role": "user", "content": "q"}])


def test_refusals_do_not_fail_over(monkeypatch):
    _pool_env(monkeypatch,
              {"provider": "openai", "model": "gpt-5", "api_key": "k1"},
              {"provider": "openai", "model": "deepseek-v3", "api_key": "k2"})
    calls = []

    def refuse(settings, system, messages, schema, effort):
        calls.append(settings.model)
        raise ValueError("model refused the request")

    monkeypatch.setattr(backend.openai_compat, "complete_json", refuse)
    primary = config.ModelPool.from_env().primary()
    with pytest.raises(ValueError):
        backend.complete_json(primary, "sys", [{"role": "user", "content": "q"}],
                              {"type": "object"})
    assert calls == ["gpt-5"]              # no second attempt


def test_fallback_can_be_disabled(monkeypatch):
    _pool_env(monkeypatch,
              {"provider": "openai", "model": "gpt-5", "api_key": "k1"},
              {"provider": "openai", "model": "deepseek-v3", "api_key": "k2"})
    monkeypatch.setenv("OLYMPUS_FALLBACK", "0")
    calls = []

    def fail(settings, system, messages, effort):
        calls.append(settings.model)
        raise RuntimeError("boom")

    monkeypatch.setattr(backend.openai_compat, "complete_text", fail)
    primary = config.ModelPool.from_env().primary()
    with pytest.raises(RuntimeError):
        backend.complete_text(primary, "sys", [{"role": "user", "content": "q"}])
    assert calls == ["gpt-5"]


# --- MoA ensemble -------------------------------------------------------------

def test_moa_fans_out_and_aggregates(monkeypatch):
    _pool_env(monkeypatch,
              {"provider": "openai", "model": "gpt-5", "api_key": "k1"},
              {"provider": "openai", "model": "kimi-k2", "api_key": "k2"})
    seen = []

    def fake(settings, system, messages, effort="high"):
        seen.append((settings.model, messages[-1]["content"][:40]))
        if "aggregator" in system.lower():
            assert any("Draft 1" in m["content"] for m in messages
                       if isinstance(m["content"], str))
            return "final synthesized answer"
        return f"draft from {settings.model}"

    monkeypatch.setattr(backend.openai_compat, "complete_text", fake)
    out = moa.complete_text(config.Settings(provider="moa", model="council"),
                            "sys", [{"role": "user", "content": "question"}])
    assert out == "final synthesized answer"
    models = [m for m, _ in seen]
    assert "gpt-5" in models and "kimi-k2" in models


def test_moa_provider_is_valid_and_dispatches(monkeypatch):
    _pool_env(monkeypatch,
              {"provider": "openai", "model": "gpt-5", "api_key": "k1"})
    s = config.Settings(provider="moa", model="council")
    assert s.validate() is None and s.usable()

    monkeypatch.setattr(backend.openai_compat, "complete_text",
                        lambda settings, system, messages, effort: "solo")
    # Ensemble of one collapses to that member — no aggregation round.
    assert backend.complete_text(s, "sys",
                                 [{"role": "user", "content": "q"}]) == "solo"


def test_moa_agent_runs_route_to_aggregator(monkeypatch):
    _pool_env(monkeypatch,
              {"provider": "openai", "model": "gpt-5", "api_key": "k1"},
              {"provider": "openai", "model": "qwen-max", "api_key": "k2"})
    assert moa.agent_settings().model == "gpt-5"   # strongest reasoning member


def test_moa_survives_one_reference_failure(monkeypatch):
    _pool_env(monkeypatch,
              {"provider": "openai", "model": "gpt-5", "api_key": "k1"},
              {"provider": "openai", "model": "kimi-k2", "api_key": "k2"})

    def flaky(settings, system, messages, effort="high"):
        if settings.model == "kimi-k2" and "aggregator" not in system.lower():
            raise RuntimeError("provider down")
        if "aggregator" in system.lower():
            return "aggregated"
        return "draft"

    monkeypatch.setattr(backend.openai_compat, "complete_text", flaky)
    out = moa.complete_text(config.Settings(provider="moa"), "sys",
                            [{"role": "user", "content": "q"}])
    assert out == "aggregated"

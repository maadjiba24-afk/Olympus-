"""Tests for multi-key ModelPool — using the best of several frontier models."""

from olympus import config, orchestrator
from olympus.config import ModelPool, Settings


def _s(provider, model, key="k"):
    return Settings(provider=provider, model=model, api_key=key)


def test_capability_scores_rank_models():
    # coding: deepseek-coder should beat a generic small model
    assert config.capability_score("deepseek-coder", "coding") > \
        config.capability_score("llama-3-8b", "coding")
    # unknown model gets the neutral default
    assert config.capability_score("some-unknown-model", "reasoning") == 5


def test_single_key_pool_uses_that_model_everywhere():
    pool = ModelPool.of(_s("anthropic", "claude-opus-4-8"))
    assert not pool.is_multi()
    for role in ("reasoning", "coding", "verify"):
        assert pool.for_role(role).model == "claude-opus-4-8"


def test_comparable_models_split_work_both_used():
    # Two comparable frontier models tie on several roles -> ties distribute,
    # so BOTH keys are used rather than one hogging everything.
    opus = _s("anthropic", "claude-opus-4-8")    # 9/9/9
    gpt5 = _s("openai", "gpt-5")                  # 9/9/8
    pool = ModelPool.of(opus, gpt5)
    used = {pool.for_role(r).model for r in ("reasoning", "coding", "verify")}
    assert len(used) == 2, f"both models should be used, got {used}"


def test_strictly_stronger_model_wins_a_role_outright():
    weak_general = _s("openai", "gpt-4o")          # coding 8
    strong_coder = _s("openai", "deepseek-coder")  # coding 9 (strictly higher)
    pool = ModelPool.of(weak_general, strong_coder)
    assert pool.for_specialist("hephaestus").model == "deepseek-coder"


def test_pool_dedupes_and_drops_unusable():
    good = _s("anthropic", "claude-opus-4-8")
    dup = _s("anthropic", "claude-opus-4-8")
    bad = Settings(provider="openai", model="", api_key=None)  # not usable
    pool = ModelPool.of(good, dup, bad)
    assert len(pool.members) == 1


def test_pool_falls_back_when_none_usable():
    bad = Settings(provider="openai", model="", api_key=None)
    pool = ModelPool.of(bad)
    assert len(pool.members) == 1     # keeps the given one rather than empty


def test_from_env_reads_extra_models(monkeypatch):
    monkeypatch.setenv("OLYMPUS_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-key")
    monkeypatch.setenv("OLYMPUS_MODELS",
                       '[{"provider":"openai","model":"gpt-4o","api_key":"oa"}]')
    pool = ModelPool.from_env()
    models = {m.model for m in pool.members}
    assert "gpt-4o" in models and pool.is_multi()


def test_orchestrator_uses_pool_per_role(monkeypatch):
    claude = _s("anthropic", "claude-opus-4-8")
    coder = _s("openai", "deepseek-coder")
    pool = ModelPool.of(claude, coder)
    bot = orchestrator.Olympus(pool=pool)
    # primary is the first member; pool drives per-role selection
    assert bot.settings.model == "claude-opus-4-8"
    assert bot.pool.for_specialist("hephaestus").provider in ("anthropic", "openai")


def test_assignment_is_readable():
    pool = ModelPool.of(_s("anthropic", "claude-opus-4-8"),
                        _s("openai", "gpt-4o"))
    text = pool.assignment()
    assert "reasoning" in text and "coding" in text
    assert "claude-opus-4-8" in text and "gpt-4o" in text

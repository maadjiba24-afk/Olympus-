"""Tier B: cost-aware pool routing, model-fraction context budget, live pricing."""

from olympus import config, providers


def _s(provider, model):
    return config.Settings(provider=provider, model=model, api_key="k",
                           base_url="http://x")


# --- pricing table --------------------------------------------------------

def test_price_per_mtok_known_and_default():
    assert config.price_per_mtok("claude-opus-4-8") == 30.0
    assert config.price_per_mtok("deepseek-chat") == 0.5
    assert config.price_per_mtok("some-unknown-model") == config._DEFAULT_PRICE


def test_live_pricing_overrides_static(monkeypatch):
    config.set_live_pricing({"deepseek-chat": 0.11})
    try:
        assert config.price_per_mtok("deepseek-chat") == 0.11
        # substring match still works
        assert config.price_per_mtok("deepseek-chat-v2") == 0.11
    finally:
        config.set_live_pricing({})


# --- cost-aware tie-break -------------------------------------------------

def test_ties_break_toward_cheaper_model():
    # kimi and moonshot score the same on reasoning (8.0); kimi is cheaper in
    # the static table? Both 1.0 — use two models with equal capability but
    # different price: glm (0.4) vs mistral... pick equal-capability pair.
    # deepseek and qwen both score 8 on coding; deepseek 0.5 vs qwen 0.5 tie.
    # Use a clear case: two models equal on 'reasoning' but different price.
    cheap = _s("openai", "glm-4.6")        # reasoning 8.5, $0.4
    pricey = _s("openai", "kimi-k2.5")     # reasoning 8.0, $1.0
    # glm scores HIGHER on reasoning, so quality wins regardless of price.
    pool = config.ModelPool.of(cheap, pricey)
    assert pool.for_role("reasoning").model == "glm-4.6"


def test_equal_capability_prefers_cheaper():
    # Construct a genuine capability tie: two 'deepseek' models score identically
    # (same substring), but we give one a live-cheaper price.
    a = _s("openai", "deepseek-chat")
    b = _s("openai", "deepseek-reasoner")
    config.set_live_pricing({"deepseek-chat": 0.10, "deepseek-reasoner": 5.0})
    try:
        pool = config.ModelPool.of(a, b)
        # Equal capability (both 'deepseek') → cheaper one wins the role.
        assert pool.for_role("coding").model == "deepseek-chat"
    finally:
        config.set_live_pricing({})


def test_assignment_shows_pricing():
    pool = config.ModelPool.of(_s("anthropic", "claude-opus-4-8"),
                               _s("openai", "deepseek-chat"))
    text = pool.assignment()
    assert "/Mtok" in text


# --- context-fraction budget ---------------------------------------------

def test_context_window_by_model():
    assert config.context_window("gemini-2.5-pro") == 1_000_000
    assert config.context_window("claude-opus-4-8") == 200_000
    assert config.context_window("weird-model") == config._DEFAULT_CONTEXT


def test_history_budget_is_fraction_of_window(monkeypatch):
    monkeypatch.setattr(config, "HISTORY_BUDGET_IS_EXPLICIT", False)
    monkeypatch.setattr(config, "HISTORY_CONTEXT_FRACTION", 0.5)
    # gemini: 1,000,000 * 0.5 = 500,000
    assert config.history_token_budget("gemini-2.5-pro") == 500_000
    # opus: 200,000 * 0.5 = 100,000
    assert config.history_token_budget("claude-opus-4-8") == 100_000


def test_explicit_override_wins(monkeypatch):
    monkeypatch.setattr(config, "HISTORY_BUDGET_IS_EXPLICIT", True)
    monkeypatch.setattr(config, "HISTORY_TOKEN_BUDGET", 3000)
    assert config.history_token_budget("gemini-2.5-pro") == 3000


# --- live pricing fetch (OpenRouter shape) --------------------------------

def test_fetch_pricing_parses_openrouter(monkeypatch):
    p = providers.get("openrouter")
    fake = {"data": [
        {"id": "openai/gpt-4o",
         "pricing": {"prompt": "0.0000025", "completion": "0.00001"}},
        {"id": "free/model", "pricing": {"prompt": "0", "completion": "0"}},
    ]}
    monkeypatch.setattr(providers, "_http_json", lambda *a, **k: fake)
    out = providers.fetch_pricing(p, api_key="k")
    assert "openai/gpt-4o" in out
    # blended = (2.5e-6*0.4 + 1e-5*0.6) * 1e6 = 7.0
    assert abs(out["openai/gpt-4o"] - 7.0) < 0.01
    assert "free/model" not in out         # zero-priced entries skipped


def test_fetch_pricing_non_openrouter_returns_empty():
    assert providers.fetch_pricing(providers.get("deepseek"), "k") == {}

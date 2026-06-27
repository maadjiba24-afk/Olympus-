"""Provider catalog, model auto-fetch, and pool-config assembly."""

from olympus import providers


def test_catalog_has_subscription_and_keyed_providers():
    keys = {p.key for p in providers.CATALOG}
    assert {"claude-code", "anthropic", "openai", "deepseek", "glm", "kimi"} <= keys
    # subscription option is first and needs no API key
    assert providers.CATALOG[0].key == "claude-code"
    assert providers.get("claude-code").auth == "subscription"
    assert providers.get("ollama").auth == "local"


def test_fetch_models_openai_shape(monkeypatch):
    prov = providers.get("kimi")
    monkeypatch.setattr(providers, "_http_json",
                        lambda url, headers, timeout=15:
                        {"data": [{"id": "moonshot-v1-8k"}, {"id": "kimi-k2.5"}]})
    assert providers.fetch_models(prov, "key") == ["moonshot-v1-8k", "kimi-k2.5"]


def test_fetch_models_graceful_on_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("404")
    monkeypatch.setattr(providers, "_http_json", boom)
    assert providers.fetch_models(providers.get("deepseek"), "key") == []


def test_build_pool_config_single_openai():
    cfg = providers.build_pool_config([
        providers.Member("openai", "deepseek-chat", "k1", "https://api.deepseek.com")])
    assert cfg["OLYMPUS_PROVIDER"] == "openai"
    assert cfg["OLYMPUS_MODEL"] == "deepseek-chat"
    assert cfg["OLYMPUS_API_KEY"] == "k1"
    assert cfg["OLYMPUS_BASE_URL"] == "https://api.deepseek.com"
    assert "OLYMPUS_MODELS" not in cfg


def test_build_pool_config_multi_becomes_pool():
    import json
    cfg = providers.build_pool_config([
        providers.Member("openai", "deepseek-chat", "k1", "https://api.deepseek.com"),
        providers.Member("openai", "glm-4.5-flash", "k2", "https://open.bigmodel.cn/api/paas/v4"),
        providers.Member("openai", "moonshot-v1-128k", "k3", "https://api.moonshot.ai/v1"),
    ])
    pool = json.loads(cfg["OLYMPUS_MODELS"])
    assert len(pool) == 2
    assert pool[0]["model"] == "glm-4.5-flash"
    assert pool[1]["base_url"].endswith("moonshot.ai/v1")


def test_build_pool_config_anthropic_uses_anthropic_key():
    cfg = providers.build_pool_config([
        providers.Member("anthropic", "claude-opus-4-8", "sk-ant", "")])
    assert cfg["OLYMPUS_PROVIDER"] == "anthropic"
    assert cfg["ANTHROPIC_API_KEY"] == "sk-ant"
    assert "OLYMPUS_API_KEY" not in cfg


def test_build_pool_config_claude_code_needs_no_key():
    cfg = providers.build_pool_config([
        providers.Member("claude-code", "claude-opus-4-8")])
    assert cfg["OLYMPUS_PROVIDER"] == "claude-code"
    assert "OLYMPUS_API_KEY" not in cfg and "ANTHROPIC_API_KEY" not in cfg

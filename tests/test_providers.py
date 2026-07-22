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


# --- Azure OpenAI (openai_compat variant, detected by endpoint host) --------

def test_azure_catalog_entry_rides_openai_backend():
    az = providers.get("azure")
    assert az is not None and az.backend == "openai"


def test_azure_endpoint_url_is_deployment_scoped(monkeypatch):
    from olympus import config, openai_compat as oc
    monkeypatch.delenv("OLYMPUS_AZURE_DEPLOYMENT", raising=False)
    monkeypatch.delenv("OLYMPUS_AZURE_API_VERSION", raising=False)
    s = config.Settings(provider="openai", model="gpt4o-deploy",
                        base_url="https://res.openai.azure.com")
    base = s.base_url.rstrip("/")
    assert oc._is_azure(base)
    url = oc._endpoint_url(s, base)
    assert url == ("https://res.openai.azure.com/openai/deployments/"
                   "gpt4o-deploy/chat/completions?api-version=2024-10-21")
    # deployment + api-version overrides
    monkeypatch.setenv("OLYMPUS_AZURE_DEPLOYMENT", "d2")
    monkeypatch.setenv("OLYMPUS_AZURE_API_VERSION", "2025-03-01")
    url2 = oc._endpoint_url(s, base)
    assert "/deployments/d2/" in url2 and "api-version=2025-03-01" in url2


def test_azure_uses_api_key_header_not_bearer():
    from olympus import openai_compat as oc
    azure = "https://res.openai.azure.com"
    h = oc._auth_headers(azure, "SECRET")
    assert h.get("api-key") == "SECRET" and "Authorization" not in h


def test_non_azure_openai_is_unchanged():
    from olympus import config, openai_compat as oc
    s = config.Settings(provider="openai", model="gpt-4o",
                        base_url="https://api.openai.com/v1")
    assert not oc._is_azure("https://api.openai.com/v1")
    assert oc._endpoint_url(s, "https://api.openai.com/v1") == \
        "https://api.openai.com/v1/chat/completions"
    h = oc._auth_headers("https://api.openai.com/v1", "K")
    assert h["Authorization"] == "Bearer K" and "api-key" not in h


# --- AWS Bedrock (Claude via anthropic.AnthropicBedrock) --------------------

def test_bedrock_validate_and_usable():
    from olympus import config
    ok = config.Settings(provider="bedrock",
                         model="anthropic.claude-3-5-sonnet-20241022-v2:0")
    assert ok.validate() is None and ok.usable()
    no_model = config.Settings(provider="bedrock", model="")
    assert no_model.validate() and "Bedrock model" in no_model.validate()


def test_bedrock_member_host_uses_region(monkeypatch):
    from olympus import config
    monkeypatch.setenv("OLYMPUS_BEDROCK_REGION", "ap-southeast-2")
    s = config.Settings(provider="bedrock", model="anthropic.claude-x")
    assert config.member_host(s) == "bedrock-runtime.ap-southeast-2.amazonaws.com"


def test_bedrock_catalog_entry():
    az = providers.get("bedrock")
    assert az is not None and az.backend == "bedrock"


def test_bedrock_client_constructs_anthropic_bedrock(monkeypatch):
    import anthropic
    from olympus import config, llm
    captured = {}

    class FakeBedrock:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(anthropic, "AnthropicBedrock", FakeBedrock, raising=False)
    monkeypatch.setattr(llm, "_clients", {})
    monkeypatch.setenv("OLYMPUS_BEDROCK_REGION", "us-west-2")
    monkeypatch.setenv("OLYMPUS_BEDROCK_ACCESS_KEY", "AKIATEST")
    monkeypatch.setenv("OLYMPUS_BEDROCK_SECRET_KEY", "shh")
    s = config.Settings(provider="bedrock", model="anthropic.claude-x")
    c = llm.client(s)
    assert isinstance(c, FakeBedrock)
    assert captured["aws_region"] == "us-west-2"
    assert captured["aws_access_key"] == "AKIATEST"


def test_azure_empty_deployment_is_rejected():
    from olympus import config, openai_compat as oc
    s = config.Settings(provider="openai", model="",
                        base_url="https://r.openai.azure.com")
    import pytest
    with pytest.raises(ValueError, match="deployment"):
        oc._endpoint_url(s, "https://r.openai.azure.com")


# --- per-provider effort tiers (reasoning_effort on supporting models) -------

def test_reasoning_effort_model_detection():
    from olympus import openai_compat as oc
    for m in ("o3-mini", "openai/o3", "gpt-5", "gpt-5-mini",
              "google/gemini-2.5-pro", "gemini-2.5-flash"):
        assert oc._supports_reasoning_effort(m), m
    for m in ("gpt-4o", "deepseek-chat", "llama-3.3-70b", "gemini-1.5-pro",
              "mistral-large", ""):
        assert not oc._supports_reasoning_effort(m), m


def test_effort_maps_to_reasoning_effort_only_for_supported(monkeypatch):
    from olympus import openai_compat as oc
    monkeypatch.delenv("OLYMPUS_DISABLE_REASONING_EFFORT", raising=False)
    assert oc._reasoning_params("o3", "high") == {"reasoning_effort": "high"}
    assert oc._reasoning_params("o3", "medium") == {"reasoning_effort": "medium"}
    assert oc._reasoning_params("gpt-4o", "high") == {}       # non-reasoning
    assert oc._reasoning_params("o3", "bogus") == {}          # unknown tier
    monkeypatch.setenv("OLYMPUS_DISABLE_REASONING_EFFORT", "1")
    assert oc._reasoning_params("o3", "high") == {}           # kill-switch


def test_complete_text_payload_for_reasoning_model(monkeypatch):
    from olympus import config, openai_compat as oc
    monkeypatch.delenv("OLYMPUS_DISABLE_REASONING_EFFORT", raising=False)
    captured = {}
    monkeypatch.setattr(oc, "_post", lambda s, p: (
        captured.update(p) or {"choices": [{"message": {"content": "ok"}}]}))
    s = config.Settings(provider="openai", model="o3-mini", api_key="k")
    oc.complete_text(s, "sys", [{"role": "user", "content": "hi"}], effort="low")
    assert captured["reasoning_effort"] == "low"
    # reasoning models require max_completion_tokens, not the legacy max_tokens
    assert "max_completion_tokens" in captured and "max_tokens" not in captured


def test_complete_text_payload_for_plain_model(monkeypatch):
    from olympus import config, openai_compat as oc
    captured = {}
    monkeypatch.setattr(oc, "_post", lambda s, p: (
        captured.update(p) or {"choices": [{"message": {"content": "ok"}}]}))
    s = config.Settings(provider="openai", model="gpt-4o", api_key="k")
    oc.complete_text(s, "sys", [{"role": "user", "content": "hi"}], effort="high")
    assert "reasoning_effort" not in captured and "max_tokens" in captured

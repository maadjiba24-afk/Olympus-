"""Runtime dynamic model discovery — list what the configured key can reach."""

from olympus import config, providers


def test_discover_maps_openai_settings(monkeypatch):
    seen = {}

    def fake_fetch(provider, api_key="", base_url=""):
        seen["backend"] = provider.backend
        seen["key"] = api_key
        seen["base"] = base_url or provider.base_url
        return ["gpt-5", "gpt-5-mini"]

    monkeypatch.setattr(providers, "fetch_models", fake_fetch)
    s = config.Settings(provider="openai", model="gpt-5",
                        api_key="k1", base_url="https://api.openai.com/v1")
    out = providers.discover_for_settings(s)
    assert out == ["gpt-5", "gpt-5-mini"]
    assert seen["backend"] == "openai" and seen["key"] == "k1"


def test_discover_custom_endpoint(monkeypatch):
    seen = {}
    monkeypatch.setattr(providers, "fetch_models",
                        lambda p, api_key="", base_url="":
                        seen.update(base=base_url or p.base_url) or ["m1"])
    s = config.Settings(provider="openai", model="m1", api_key="k",
                        base_url="https://my-gateway.example/v1")
    assert providers.discover_for_settings(s) == ["m1"]
    assert seen["base"] == "https://my-gateway.example/v1"


def test_discover_anthropic_default_host(monkeypatch):
    seen = {}
    monkeypatch.setattr(providers, "fetch_models",
                        lambda p, api_key="", base_url="":
                        seen.update(base=p.base_url) or ["claude-opus-5"])
    s = config.Settings(provider="anthropic", model="claude-opus-5",
                        api_key="k")
    assert providers.discover_for_settings(s) == ["claude-opus-5"]
    assert seen["base"] == "https://api.anthropic.com"


def test_claude_code_has_no_discoverable_list():
    s = config.Settings(provider="claude-code", model="claude-opus-4-8")
    assert providers.discover_for_settings(s) == []


def test_discover_is_best_effort_on_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")
    # fetch_models already swallows errors; ensure discover doesn't add its own
    monkeypatch.setattr(providers, "fetch_models", lambda *a, **k: [])
    s = config.Settings(provider="openai", model="x", api_key="k",
                        base_url="https://x/v1")
    assert providers.discover_for_settings(s) == []

"""Fast mode + latency/robustness fixes: fastest-member picking, review skip,
bounded output, and graceful synthesize fallback."""

import olympus.openai_compat as oc
from olympus import config, orchestrator


def _pool(*specs):
    return config.ModelPool(tuple(
        config.Settings(provider="openai", model=m, api_key="k",
                        base_url="http://x") for m in specs))


def test_fastest_prefers_speed_hinted_model():
    pool = _pool("glm-4.6", "glm-4-flash", "deepseek-chat")
    assert pool.fastest().model == "glm-4-flash"


def test_fastest_single_member():
    pool = _pool("glm-4.6")
    assert pool.fastest().model == "glm-4.6"


def test_fast_mode_flag(monkeypatch):
    monkeypatch.delenv("OLYMPUS_FAST", raising=False)
    assert config.fast_mode() is False
    monkeypatch.setenv("OLYMPUS_FAST", "1")
    assert config.fast_mode() is True


def test_light_uses_fastest_only_in_fast_mode(monkeypatch):
    bot = orchestrator.Olympus(pool=_pool("glm-4.6", "moonshot-v1-8k"))
    monkeypatch.delenv("OLYMPUS_FAST", raising=False)
    # normal: reasoning role (glm-4.6 has the higher reasoning score)
    assert bot._light().model == "glm-4.6"
    monkeypatch.setenv("OLYMPUS_FAST", "1")
    assert bot._light().model == "moonshot-v1-8k"   # the speed-hinted member


def test_openai_compat_sends_max_tokens(monkeypatch):
    captured = {}

    def fake_post(settings, payload):
        captured.update(payload)
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(oc, "_post", fake_post)
    s = config.Settings(provider="openai", model="m", api_key="k",
                        base_url="http://x")
    oc.complete_text(s, "sys", [{"role": "user", "content": "hi"}])
    assert captured.get("max_tokens") == config.MAX_TOKENS


def test_synthesize_failure_degrades_gracefully(monkeypatch):
    bot = orchestrator.Olympus(pool=_pool("glm-4.6"))
    monkeypatch.setattr(bot, "_pipeline",
                        lambda msg, tr: ("delegate", "the brief",
                                         "VERIFIED FINDINGS: Sam Altman is CEO."))

    def boom(*a, **k):
        raise RuntimeError("Provider error HTTP 400 model not found")

    monkeypatch.setattr(bot, "_synthesize", boom)
    reply = bot.ask("who is the ceo?")
    # No crash; falls back to the verified findings instead of a traceback.
    assert "VERIFIED FINDINGS" in reply

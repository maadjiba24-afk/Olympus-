"""Per-role ordered fallback chains and Anthropic key rotation
(OpenClaw adoption plan #3 — model failover)."""

import json

import anthropic
import httpx
import pytest

from olympus import backend, config, llm


def _pool_env(monkeypatch, *members):
    first = members[0]
    monkeypatch.setenv("OLYMPUS_PROVIDER", first["provider"])
    monkeypatch.setenv("OLYMPUS_MODEL", first.get("model", ""))
    monkeypatch.setenv("OLYMPUS_API_KEY", first.get("api_key", "k0"))
    monkeypatch.setenv("OLYMPUS_MODELS", json.dumps(list(members[1:])))
    monkeypatch.delenv("OLYMPUS_ROLE_FALLBACKS", raising=False)


# --- chain ordering ----------------------------------------------------------

def test_fallbacks_ordered_by_role_capability(monkeypatch):
    # deepseek is the pool's coder (coding 9); when it fails, the chain should
    # try the next-best coder (gpt-5, coding 9 but reasoning-assigned; still
    # ranked above haiku, coding 6).
    _pool_env(monkeypatch,
              {"provider": "openai", "model": "gpt-5", "api_key": "k1"},
              {"provider": "openai", "model": "deepseek-v3", "api_key": "k2"},
              {"provider": "anthropic", "model": "claude-haiku-4-5",
               "api_key": "k3"})
    pool = config.ModelPool.from_env()
    coder = pool.for_role("coding")
    assert coder.model == "deepseek-v3"
    chain = pool.fallbacks_for(coder)
    assert [m.model for m in chain] == ["gpt-5", "claude-haiku-4-5"]


def test_explicit_role_fallback_order_wins(monkeypatch):
    _pool_env(monkeypatch,
              {"provider": "openai", "model": "gpt-5", "api_key": "k1"},
              {"provider": "openai", "model": "deepseek-v3", "api_key": "k2"},
              {"provider": "anthropic", "model": "claude-haiku-4-5",
               "api_key": "k3"})
    monkeypatch.setenv("OLYMPUS_ROLE_FALLBACKS",
                       json.dumps({"coding": ["haiku", "gpt-5"]}))
    pool = config.ModelPool.from_env()
    coder = pool.for_role("coding")
    chain = pool.fallbacks_for(coder)
    assert [m.model for m in chain] == ["claude-haiku-4-5", "gpt-5"]


def test_malformed_role_fallbacks_env_is_ignored(monkeypatch):
    monkeypatch.setenv("OLYMPUS_ROLE_FALLBACKS", "{not json")
    assert config.role_fallback_overrides() == {}
    monkeypatch.setenv("OLYMPUS_ROLE_FALLBACKS", json.dumps(["a", "b"]))
    assert config.role_fallback_overrides() == {}


def test_backend_walks_full_chain_in_order(monkeypatch):
    _pool_env(monkeypatch,
              {"provider": "openai", "model": "gpt-5", "api_key": "k1"},
              {"provider": "openai", "model": "deepseek-v3", "api_key": "k2"},
              {"provider": "openai", "model": "llama-70b", "api_key": "k3"})
    calls = []

    def fake(settings, system, messages, effort):
        calls.append(settings.model)
        if settings.model != "llama-70b":
            raise RuntimeError("503 down")
        return "third time lucky"

    monkeypatch.setattr(backend.openai_compat, "complete_text", fake)
    primary = config.ModelPool.from_env().primary()
    out = backend.complete_text(primary, "sys",
                                [{"role": "user", "content": "q"}])
    assert out == "third time lucky"
    # gpt-5 (failing member) → deepseek (next best for reasoning) → llama
    assert calls == ["gpt-5", "deepseek-v3", "llama-70b"]


# --- Anthropic key rotation ---------------------------------------------------

class _FakeStream:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._message


class _FakeMessage:
    usage = None
    stop_reason = "end_turn"
    content = []

    def to_json(self):
        return "{}"


def _api_error(cls, status, text):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status, request=request, text=text)
    return cls(text, response=response, body=None)


def _fake_client_factory(keys_seen, fail_keys, error):
    class _Endpoint:
        def __init__(self, key):
            self._key = key

        def stream(self, **params):
            keys_seen.append(self._key)
            if self._key in fail_keys:
                raise error
            return _FakeStream(_FakeMessage())

    class _Client:
        def __init__(self, key):
            self.messages = _Endpoint(key)
            class _Beta:  # noqa: N801 - mimic sdk shape
                pass
            self.beta = _Beta()
            self.beta.messages = self.messages

    return _Client


def test_rate_limited_key_rotates_to_next(monkeypatch):
    err = _api_error(anthropic.RateLimitError, 429, "rate limited")
    seen: list = []
    client_cls = _fake_client_factory(seen, {"k1"}, err)
    monkeypatch.setattr(llm, "client",
                        lambda s=None: client_cls(s.api_key if s else None))
    settings = config.Settings(provider="anthropic", model="claude-x",
                               api_key="k1", api_keys=("k1", "k2"))
    message = llm.complete("sys", [{"role": "user", "content": "q"}],
                           settings=settings)
    assert isinstance(message, _FakeMessage)
    assert seen == ["k1", "k2"]           # rotated, no retry burned on k1


def test_credit_exhausted_key_rotates(monkeypatch):
    err = _api_error(anthropic.BadRequestError, 400,
                     "your credit balance is too low")
    seen: list = []
    client_cls = _fake_client_factory(seen, {"k1"}, err)
    monkeypatch.setattr(llm, "client",
                        lambda s=None: client_cls(s.api_key if s else None))
    settings = config.Settings(provider="anthropic", model="claude-x",
                               api_key="k1", api_keys=("k1", "k2"))
    message = llm.complete("sys", [{"role": "user", "content": "q"}],
                           settings=settings)
    assert isinstance(message, _FakeMessage)
    assert seen == ["k1", "k2"]


def test_plain_bad_request_does_not_rotate(monkeypatch):
    err = _api_error(anthropic.BadRequestError, 400, "max_tokens too large")
    seen: list = []
    client_cls = _fake_client_factory(seen, {"k1", "k2"}, err)
    monkeypatch.setattr(llm, "client",
                        lambda s=None: client_cls(s.api_key if s else None))
    settings = config.Settings(provider="anthropic", model="claude-x",
                               api_key="k1", api_keys=("k1", "k2"))
    with pytest.raises(anthropic.BadRequestError):
        llm.complete("sys", [{"role": "user", "content": "q"}],
                     settings=settings)
    assert seen == ["k1"]                 # a real request bug surfaces at once


def test_single_key_rate_limit_still_backs_off(monkeypatch):
    err = _api_error(anthropic.RateLimitError, 429, "rate limited")
    seen: list = []
    client_cls = _fake_client_factory(seen, {"k1"}, err)
    monkeypatch.setattr(llm, "client",
                        lambda s=None: client_cls(s.api_key if s else None))
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    settings = config.Settings(provider="anthropic", model="claude-x",
                               api_key="k1")
    with pytest.raises(anthropic.RateLimitError):
        llm.complete("sys", [{"role": "user", "content": "q"}],
                     settings=settings)
    assert seen == ["k1"] * 4             # retried, never silently switched

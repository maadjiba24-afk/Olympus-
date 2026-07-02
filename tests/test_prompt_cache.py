"""Cross-session prompt caching: the stable request prefix (system prompt +
tool schemas) carries a cache breakpoint whose TTL is configurable. The 1-hour
tier keeps the cache warm between heartbeat cycles and gateway chats and must
ride the beta endpoint with the extended-TTL beta header.
"""

import types

import anthropic

from olympus import config, llm


def _text_message(text="ok"):
    return anthropic.types.Message.model_validate({
        "id": "m1", "type": "message", "role": "assistant", "model": "claude-test",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
    })


def _capturing_client(captured):
    """Fake Anthropic client recording which endpoint got which params."""
    def make_stream(endpoint):
        def stream(**params):
            captured.append((endpoint, params))

            class _S:
                def __enter__(s): return s
                def __exit__(s, *a): return False
                def get_final_message(s): return _text_message()
            return _S()
        return stream
    return types.SimpleNamespace(
        messages=types.SimpleNamespace(stream=make_stream("messages")),
        beta=types.SimpleNamespace(
            messages=types.SimpleNamespace(stream=make_stream("beta"))),
    )


def _complete(monkeypatch, captured, **kwargs):
    monkeypatch.setattr(llm, "client", lambda settings=None: _capturing_client(captured))
    settings = config.Settings(provider="anthropic", model="claude-test")
    return llm.complete("sys", [{"role": "user", "content": "hi"}],
                        settings=settings, **kwargs)


def test_default_ttl_is_five_minutes(monkeypatch):
    monkeypatch.delenv("OLYMPUS_CACHE_TTL", raising=False)
    captured = []
    _complete(monkeypatch, captured)
    endpoint, params = captured[0]
    assert endpoint == "messages"
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "betas" not in params


def test_one_hour_ttl_marks_blocks_and_uses_beta(monkeypatch):
    monkeypatch.setenv("OLYMPUS_CACHE_TTL", "1h")
    captured = []
    tool_defs = [{"name": "a", "description": "", "input_schema": {}},
                 {"name": "b", "description": "", "input_schema": {}}]
    _complete(monkeypatch, captured, tools=tool_defs)
    endpoint, params = captured[0]
    assert endpoint == "beta"
    assert params["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert llm._EXTENDED_TTL_BETA in params["betas"]
    # Breakpoint sits on the LAST tool (caches the whole array) with the TTL.
    assert params["tools"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "cache_control" not in params["tools"][0]


def test_one_hour_ttl_composes_with_mcp_beta(monkeypatch):
    monkeypatch.setenv("OLYMPUS_CACHE_TTL", "1h")
    captured = []
    _complete(monkeypatch, captured,
              mcp_servers=[{"type": "url", "url": "https://mcp.example", "name": "x"}])
    endpoint, params = captured[0]
    assert endpoint == "beta"
    assert "mcp-client-2025-11-20" in params["betas"]
    assert llm._EXTENDED_TTL_BETA in params["betas"]


def test_invalid_ttl_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("OLYMPUS_CACHE_TTL", "2d")
    assert config.prompt_cache_ttl() == "5m"
    monkeypatch.setenv("OLYMPUS_CACHE_TTL", "1H")
    assert config.prompt_cache_ttl() == "1h"

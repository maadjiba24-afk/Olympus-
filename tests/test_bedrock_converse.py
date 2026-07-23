"""Native Bedrock Converse client for non-Claude models (DEFERRED #13).

Claude-on-Bedrock keeps the AnthropicBedrock path; Titan/Llama/Mistral now route
to the Converse API. boto3 stays a lazy optional extra; the request/response
mapping is a pure transform tested with an injected client (no network).
"""

import ast

import pytest

from olympus import backend, bedrock_converse as bc, config


# --- model detection --------------------------------------------------------

@pytest.mark.parametrize("model,converse", [
    ("amazon.titan-text-express-v1", True),
    ("amazon.nova-pro-v1:0", True),
    ("meta.llama3-70b-instruct-v1:0", True),
    ("mistral.mistral-large-2402-v1:0", True),
    ("cohere.command-r-v1:0", True),
    ("anthropic.claude-3-5-sonnet-20240620-v1:0", False),
    ("us.anthropic.claude-3-haiku-20240307-v1:0", False),
    ("", False),
])
def test_is_converse_model(model, converse):
    assert bc.is_converse_model(model) is converse


# --- pure request/response mapping ------------------------------------------

def test_to_converse_merges_roles_and_prepends_user():
    body = bc._to_converse("be terse", [
        {"role": "user", "content": "a"},
        {"role": "user", "content": "b"},            # merged with previous
        {"role": "assistant", "content": [{"text": "ok"}]},
        {"role": "user", "content": "c"},
    ])
    assert body["system"] == [{"text": "be terse"}]
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    assert body["messages"][0]["content"][0]["text"] == "a\nb"
    assert body["inferenceConfig"]["maxTokens"] >= 1


def test_to_converse_prepends_user_when_leading_assistant():
    body = bc._to_converse("", [{"role": "assistant", "content": "hi"}])
    assert body["messages"][0]["role"] == "user"
    assert "system" not in body                       # empty system omitted


def test_text_from_is_tolerant():
    ok = {"output": {"message": {"content": [{"text": "x"}, {"text": "y"}]}}}
    assert bc._text_from(ok) == "x\ny"
    assert bc._text_from({}) == ""
    assert bc._text_from({"output": {"message": {"content": "nope"}}}) == ""


# --- injected-client completions --------------------------------------------

class _Fake:
    def __init__(self, text):
        self.text = text
        self.seen = None

    def converse(self, **kw):
        self.seen = kw
        return {"output": {"message": {"content": [{"text": self.text}]}}}


def _settings():
    return config.Settings(provider="bedrock",
                           model="amazon.titan-text-express-v1")


def test_complete_text_uses_converse():
    fake = _Fake("TITAN")
    out = bc.complete_text(_settings(), "sys", [{"role": "user", "content": "hi"}],
                           client=fake)
    assert out == "TITAN"
    assert fake.seen["modelId"] == "amazon.titan-text-express-v1"


def test_complete_json_tolerant_parse():
    fake = _Fake('Sure: {"ok": true, "n": 5} — done')
    j = bc.complete_json(_settings(), "sys", [{"role": "user", "content": "x"}],
                         {"type": "object"}, client=fake)
    assert j == {"ok": True, "n": 5}


def test_complete_json_raises_when_no_json():
    fake = _Fake("no json here at all")
    with pytest.raises(ValueError):
        bc.complete_json(_settings(), "s", [{"role": "user", "content": "x"}],
                         {}, client=fake)


# --- backend routing --------------------------------------------------------

def test_backend_routes_nonclaude_bedrock_to_converse(monkeypatch):
    fake = _Fake("ROUTED")
    monkeypatch.setattr(bc, "_client", lambda: fake)
    s = config.Settings(provider="bedrock", model="meta.llama3-70b-instruct-v1:0")
    assert backend.complete_text(s, "sys", [{"role": "user", "content": "hi"}]) == "ROUTED"


def test_backend_claude_bedrock_not_routed_to_converse():
    # a Claude-on-Bedrock model must stay on the AnthropicBedrock path
    s = config.Settings(provider="bedrock",
                        model="anthropic.claude-3-5-sonnet-20240620-v1:0")
    assert backend._is_bedrock_converse(s) is False


def test_agent_path_routes_nonclaude_bedrock(monkeypatch):
    fake = _Fake("AGENT-TEXT")
    monkeypatch.setattr(bc, "_client", lambda: fake)
    s = config.Settings(provider="bedrock", model="amazon.titan-text-express-v1")
    text, count = backend.run_agent_counted(s, "sys", "do a thing", tool_defs=None)
    assert text == "AGENT-TEXT" and count is None       # no tool-call count


# --- boto3 stays a LAZY optional extra --------------------------------------

def test_boto3_not_imported_at_module_top():
    src = open("olympus/bedrock_converse.py", encoding="utf-8").read()
    tree = ast.parse(src)
    top = []
    for node in tree.body:                       # module-level imports only
        if isinstance(node, ast.Import):
            top += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            top.append(node.module or "")
    assert not any("boto3" in m for m in top), top   # boto3 only inside _client()

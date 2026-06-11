"""Tests for MCP servers + custom plugins, including security integration."""

import json

import pytest

from olympus import config, connectors, security, tools
from olympus.specialists import SPECIALISTS


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch):
    """Isolate the plugin/MCP registries per test."""
    monkeypatch.setattr(connectors, "_PLUGINS", {})
    monkeypatch.setattr(connectors, "_PLUGINS_LOADED", True)  # skip dir scan
    yield


# --- custom plugins ------------------------------------------------------

def test_plugin_registration_and_handler():
    @connectors.plugin("crm_lookup", "Look up a customer",
                       schema={"type": "object",
                               "properties": {"email": {"type": "string"}},
                               "required": ["email"]},
                       specialists=["plutus"], action=False)
    def crm(email: str) -> str:
        return f"customer:{email}"

    defs = connectors.plugin_tools_for("plutus", allow_action=True)
    assert any(d["name"] == "crm_lookup" for d in defs)
    # not offered to a specialist not in its list
    assert connectors.plugin_tools_for("peitho", allow_action=True) == []
    # resolvable as a handler and executes
    handler = tools.resolve_handler("crm_lookup")
    assert handler(email="a@b.c") == "customer:a@b.c"


def test_action_plugin_hidden_when_no_action_allowed():
    @connectors.plugin("post_tweet", "Post a tweet", action=True,
                       specialists=["iris"])
    def post(text: str) -> str:
        return "posted"

    assert connectors.plugin_tools_for("iris", allow_action=True)
    assert connectors.plugin_tools_for("iris", allow_action=False) == []
    assert "post_tweet" in connectors.plugin_action_names()


def test_data_plugin_output_is_wrapped():
    @connectors.plugin("read_feed", "Read an RSS feed", action=False)
    def feed() -> str:
        return "feed content"

    assert connectors.is_data_plugin("read_feed")
    assert security.should_wrap("read_feed")
    assert not security.should_wrap("post_tweet")  # not registered as data


# --- MCP servers ---------------------------------------------------------

def test_mcp_loaded_from_config(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setenv("OLYMPUS_MCP_ACTION_ALLOWLIST", "github")
    (tmp_path / "connectors.json").write_text(json.dumps({"servers": [
        {"name": "tavily", "url": "https://mcp.tavily.com/mcp/", "type": "data",
         "specialists": ["argus"]},
        {"name": "github", "url": "https://api.githubcopilot.com/mcp/",
         "type": "action", "specialists": ["chronos"]},
    ]}))
    names = {s.name for s in connectors.mcp_servers()}
    assert names == {"tavily", "github"}
    # data server visible to argus; not to peitho
    assert [s.name for s in connectors.mcp_for("argus", allow_action=False)] == ["tavily"]
    assert connectors.mcp_for("peitho", allow_action=False) == []
    # action server only when action allowed
    assert connectors.mcp_for("chronos", allow_action=False) == []
    assert [s.name for s in connectors.mcp_for("chronos", allow_action=True)] == ["github"]


def test_action_mcp_requires_operator_allowlist(monkeypatch, tmp_path):
    """Tier-2 double gate: config alone is NOT enough for an action server."""
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    (tmp_path / "connectors.json").write_text(json.dumps({"servers": [
        {"name": "github", "url": "https://api.githubcopilot.com/mcp/",
         "type": "action", "specialists": ["chronos"]},
    ]}))
    # configured but NOT allowlisted -> inert even when action is allowed
    monkeypatch.delenv("OLYMPUS_MCP_ACTION_ALLOWLIST", raising=False)
    assert connectors.mcp_for("chronos", allow_action=True) == []
    assert "INERT" in connectors.summary()
    # allowlisting a DIFFERENT name doesn't activate it
    monkeypatch.setenv("OLYMPUS_MCP_ACTION_ALLOWLIST", "linear, slack")
    assert connectors.mcp_for("chronos", allow_action=True) == []
    # allowlisting the right name activates it
    monkeypatch.setenv("OLYMPUS_MCP_ACTION_ALLOWLIST", "linear, github")
    assert [s.name for s in connectors.mcp_for("chronos", allow_action=True)] \
        == ["github"]
    assert "ACTIVE" in connectors.summary()
    # data servers never need the allowlist
    (tmp_path / "connectors.json").write_text(json.dumps({"servers": [
        {"name": "notion", "url": "https://mcp.notion.com/mcp", "type": "data"},
    ]}))
    monkeypatch.delenv("OLYMPUS_MCP_ACTION_ALLOWLIST", raising=False)
    assert [s.name for s in connectors.mcp_for("chiron", allow_action=False)] \
        == ["notion"]


def test_mcp_auth_token_from_env(monkeypatch):
    monkeypatch.setenv("MY_MCP_TOKEN", "secret-123")
    s = connectors.MCPServer(name="x", url="https://x/mcp", type="data",
                             auth_env="MY_MCP_TOKEN")
    api = s.to_api()
    assert api["authorization_token"] == "secret-123"
    assert api["type"] == "url" and api["url"] == "https://x/mcp"
    # token absent if env unset -> field omitted (no empty token sent)
    s2 = connectors.MCPServer(name="y", url="https://y/mcp", auth_env="UNSET_X")
    assert "authorization_token" not in s2.to_api()


def test_add_mcp_server_persists(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    connectors.add_mcp_server("linear", "https://mcp.linear.app/mcp",
                              type="data", specialists=["chronos"])
    data = json.loads((tmp_path / "connectors.json").read_text())
    assert data["servers"][0]["name"] == "linear"
    # adding same name replaces, not duplicates
    connectors.add_mcp_server("linear", "https://new/mcp")
    data = json.loads((tmp_path / "connectors.json").read_text())
    assert len([s for s in data["servers"] if s["name"] == "linear"]) == 1


# --- security integration on specialists --------------------------------

def test_data_mcp_makes_specialist_ingest(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    # chronos has NO web and holds action tools (send_email/call_webhook)
    chronos = SPECIALISTS["chronos"]
    before = {d.get("name") for d in chronos.tool_defs("anthropic")}
    assert {"send_email", "call_webhook"} <= before   # actuators present

    # attach a DATA mcp to chronos -> it now ingests -> action tools stripped
    (tmp_path / "connectors.json").write_text(json.dumps({"servers": [
        {"name": "notion", "url": "https://mcp.notion.com/mcp", "type": "data",
         "specialists": ["chronos"]},
    ]}))
    after = {d.get("name") for d in chronos.tool_defs("anthropic")}
    assert "send_email" not in after and "call_webhook" not in after


def test_mcp_only_on_anthropic_backend(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    (tmp_path / "connectors.json").write_text(json.dumps({"servers": [
        {"name": "tavily", "url": "https://mcp.tavily.com/mcp/", "type": "data",
         "specialists": ["argus"]},
    ]}))
    assert SPECIALISTS["argus"].mcp_defs("anthropic")        # present
    assert SPECIALISTS["argus"].mcp_defs("openai") == []     # not on others

"""End-to-end integration test for the native MCP client against a REAL stdio
MCP server (a subprocess speaking the protocol). Opt-in: skipped unless the
optional `mcp` SDK is installed (`pip install olympus-council[mcp]`), so it runs
in any environment that has the extra and is a no-op everywhere else.

Unlike test_mcp_client.py (which monkeypatches the discover/call seam), this
exercises the whole path: spawn → initialize → list_tools → call_tool, plus the
Olympus wiring (namespacing, resolve_handler dispatch, untrusted envelope)."""

import sys
import textwrap

import pytest

pytest.importorskip("mcp", reason="the optional 'mcp' SDK is not installed")

from olympus import connectors, mcp_client, security, tools  # noqa: E402


_SERVER_SRC = textwrap.dedent('''
    import asyncio
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    import mcp.types as types

    server = Server("mini")

    @server.list_tools()
    async def list_tools():
        return [
            types.Tool(name="echo", description="Echo the given text back.",
                       inputSchema={"type": "object",
                                    "properties": {"text": {"type": "string"}},
                                    "required": ["text"]}),
            types.Tool(name="add", description="Add two integers a and b.",
                       inputSchema={"type": "object",
                                    "properties": {"a": {"type": "integer"},
                                                   "b": {"type": "integer"}},
                                    "required": ["a", "b"]}),
        ]

    @server.call_tool()
    async def call_tool(name, arguments):
        if name == "echo":
            return [types.TextContent(type="text", text=str(arguments.get("text", "")))]
        if name == "add":
            total = int(arguments.get("a", 0)) + int(arguments.get("b", 0))
            return [types.TextContent(type="text", text=str(total))]
        return [types.TextContent(type="text", text=f"unknown tool {name}")]

    async def main():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(main())
''')


@pytest.fixture()
def mini_server(tmp_path, monkeypatch):
    path = tmp_path / "mini_mcp_server.py"
    path.write_text(_SERVER_SRC, encoding="utf-8")
    srv = connectors.MCPServer(
        name="mini", transport="stdio", command=sys.executable,
        args=(str(path),), type="data", specialists=("argus",))
    monkeypatch.setattr(connectors, "mcp_servers", lambda: [srv])
    monkeypatch.setattr(mcp_client, "_SCHEMA_CACHE", {})
    monkeypatch.setattr(mcp_client, "_KIND_CACHE", {})
    monkeypatch.setenv("OLYMPUS_MCP_STDIO_ALLOWLIST", "mini")
    return srv


def test_discover_lists_real_server_tools(mini_server):
    schemas = mcp_client.discover(mini_server)
    names = {s["name"] for s in schemas}
    assert names == {"mcp__mini__echo", "mcp__mini__add"}
    add = next(s for s in schemas if s["name"] == "mcp__mini__add")
    assert "integers" in add["description"]
    assert add["input_schema"]["required"] == ["a", "b"]


def test_tool_defs_surface_through_the_connector(mini_server):
    defs = connectors.mcp_client_tools_for("argus", allow_action=False)
    assert sorted(d["name"] for d in defs) == ["mcp__mini__add", "mcp__mini__echo"]


def test_dispatch_calls_the_real_server(mini_server):
    # populate the kind cache / prove discovery first
    mcp_client.discover(mini_server)
    add = tools.resolve_handler("mcp__mini__add")
    echo = tools.resolve_handler("mcp__mini__echo")
    assert add is not None and echo is not None
    assert add(a=20, b=22) == "42"
    assert echo(text="hello from olympus") == "hello from olympus"


def test_real_mcp_output_is_untrusted(mini_server):
    mcp_client.discover(mini_server)
    assert security.should_wrap("mcp__mini__add") is True
    assert mcp_client.kind_of("mcp__mini__add") == "data"


def test_unknown_tool_on_real_server_is_a_clean_error(mini_server):
    out = mcp_client.call("mini", "does_not_exist", {})
    assert isinstance(out, str) and out                    # never raises

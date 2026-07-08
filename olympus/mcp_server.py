"""MCP server mode — expose Olympus to any MCP client over stdio.

Olympus has long been an MCP *client* (connectors.py). This is the other
direction: `olympus mcp-serve` speaks the Model Context Protocol on
stdin/stdout, so Claude Desktop, IDEs, and other agents can use the whole
council as a tool. One entry in an MCP client config:

    {"command": "olympus", "args": ["mcp-serve"]}

Exposed tools:
  ask_olympus              one question through the full Zeus → Athena →
                           Aletheia pipeline (fact-checked before it returns)
  olympus_goals            review the standing-goal board
  olympus_search_documents search the exposed user's documents (read-only)
  olympus_list_todos       list the exposed user's notes/todos/reminders
  olympus_recall_memory    recall facts from the exposed user's memory

The workspace tools are **read-only** and scoped to `OLYMPUS_MCP_USER` (default
the shared namespace): a caller on the other end of the pipe can read what that
user has, and nothing more — no write, no actuator ever crosses this boundary.

Like openai_server.py this module is pure translation — newline-delimited
JSON-RPC in, JSON-RPC out — with the transport loop kept to a few lines so
everything is unit-testable without a subprocess. Session note: all calls
share one conversation ("mcp-default") per server process, mirroring how
the OpenAI-compatible endpoint scopes its API callers.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable

PROTOCOL_VERSION = "2024-11-05"


def _mcp_user() -> str:
    return os.environ.get("OLYMPUS_MCP_USER", "shared")


TOOLS: list[dict[str, Any]] = [
    {
        "name": "ask_olympus",
        "description": (
            "Ask the Olympus council of AI specialists. The question is "
            "routed to the right specialists (finance, marketing, coding, "
            "security, research, ...) and the answer is fact-checked by a "
            "hallucination controller before it returns."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string",
                            "description": "The question or task"},
            },
            "required": ["message"],
        },
    },
    {
        "name": "olympus_goals",
        "description": "List Olympus's standing goals and their status.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "olympus_search_documents",
        "description": ("Search the Olympus user's saved documents for passages "
                        "relevant to a query. Read-only."),
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "olympus_list_todos",
        "description": ("List the Olympus user's notes, todos, and reminders. "
                        "Read-only."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "olympus_recall_memory",
        "description": ("Recall facts relevant to a query from the Olympus "
                        "user's long-term memory. Read-only."),
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
]


def _default_ask(message: str) -> str:
    from . import config, orchestrator
    bot = orchestrator.Olympus(pool=config.ModelPool.from_env(),
                               user="mcp", conversation_id="mcp-default")
    return bot.ask(message)


def _workspace_tool(name: str, args: dict) -> str:
    """Read-only workspace reads, scoped to the exposed user."""
    from . import memory
    user = _mcp_user()
    memory.set_user(user)
    if name == "olympus_search_documents":
        from . import docrag
        return docrag.render_search(user, str(args.get("query", "")))
    if name == "olympus_list_todos":
        from . import todos
        return todos.render_list(user)
    if name == "olympus_recall_memory":
        return memory.search(str(args.get("query", "")), limit=8)
    raise KeyError(name)


def _result(rid, payload: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": payload}


def _error(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": code, "message": message}}


def handle_message(msg: dict, ask: Callable[[str], str] | None = None
                   ) -> dict | None:
    """Handle one JSON-RPC message; return the response dict, or None for
    notifications (which must not be answered). `ask` is injectable."""
    ask = ask or _default_ask
    rid = msg.get("id")
    method = msg.get("method", "")

    if rid is None:                     # notification — no response, ever
        return None

    if method == "initialize":
        from . import __version__
        return _result(rid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "olympus-council",
                           "version": __version__},
        })
    if method == "ping":
        return _result(rid, {})
    if method == "tools/list":
        return _result(rid, {"tools": TOOLS})
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            if name == "ask_olympus":
                message = (args.get("message") or "").strip()
                if not message:
                    return _error(rid, -32602, "message is required")
                text = ask(message)
            elif name == "olympus_goals":
                from . import goals
                text = goals.summary()
            elif name in ("olympus_search_documents", "olympus_list_todos",
                          "olympus_recall_memory"):
                text = _workspace_tool(name, args)
            else:
                return _error(rid, -32602, f"unknown tool '{name}'")
        except Exception as err:
            return _result(rid, {"content": [{"type": "text",
                                              "text": f"Error: {err}"}],
                                 "isError": True})
        return _result(rid, {"content": [{"type": "text", "text": text}]})

    return _error(rid, -32601, f"method not found: {method}")


def serve_stdio() -> None:
    """Newline-delimited JSON-RPC over stdin/stdout until EOF."""
    print("Olympus MCP server on stdio (Ctrl-C to stop).", file=sys.stderr)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            sys.stdout.write(json.dumps(
                _error(None, -32700, "parse error")) + "\n")
            sys.stdout.flush()
            continue
        response = handle_message(msg)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()

"""MCP server mode — expose Olympus to any MCP client over stdio.

Olympus has long been an MCP *client* (connectors.py). This is the other
direction: `olympus mcp-serve` speaks the Model Context Protocol on
stdin/stdout, so Claude Desktop, IDEs, and other agents can use the whole
council as a tool. One entry in an MCP client config:

    {"command": "olympus", "args": ["mcp-serve"]}

Exposed tools:
  ask_olympus     one question through the full Zeus → Athena → Aletheia
                  pipeline (fact-checked before it returns)
  olympus_goals   review the standing-goal board

Like openai_server.py this module is pure translation — newline-delimited
JSON-RPC in, JSON-RPC out — with the transport loop kept to a few lines so
everything is unit-testable without a subprocess. Session note: all calls
share one conversation ("mcp-default") per server process, mirroring how
the OpenAI-compatible endpoint scopes its API callers.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable

PROTOCOL_VERSION = "2024-11-05"

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
]


def _default_ask(message: str) -> str:
    from . import config, orchestrator
    bot = orchestrator.Olympus(pool=config.ModelPool.from_env(),
                               user="mcp", conversation_id="mcp-default")
    return bot.ask(message)


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

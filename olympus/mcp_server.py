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
  olympus_query_codegraph  token-budgeted subgraph of the code graph (read-only)
  olympus_codegraph_impact reverse-dependency analysis for a symbol (read-only)
  olympus_codegraph_report god nodes / communities / surprises (read-only)
  olympus_verify_code_claim CONFIRMED/REFUTED/UNKNOWN for a structural claim
  olympus_assess_report    the ledgered security findings (markdown/json/SARIF)
  olympus_assess_scorecard the Aegis self-benchmark + blast-radius containment proof
  olympus_discover_report  the self-discovery ledger (open gaps + feature proposals)

The workspace tools are **read-only** and scoped to `OLYMPUS_MCP_USER` (default
the shared namespace): a caller on the other end of the pipe can read what that
user has, and nothing more — no write, no actuator ever crosses this boundary.
The Aegis/discovery surfaces are **read/prepare only** by the same rule: they
export already-recorded findings and pure self-scorecards, and never grant a
scope, run a scan, or make a network request from an MCP caller (an assessment
still requires a signed, in-process authorization — it cannot be initiated
across this boundary).

**Authentication.** Stdio-local by default (the OS process boundary is the trust
boundary, so no token). The moment the server is EXPOSED beyond stdio
(`OLYMPUS_MCP_EXPOSED=1`, or any transport an operator puts in front of it) a
bearer token becomes MANDATORY, enforced fail-closed: an exposed server with no
`OLYMPUS_MCP_AUTH_TOKEN[_FILE]` refuses to serve, and every tool call must
present the token. Building the auth is the hardening; exposing the server is
still the operator's deliberate act.

Like openai_server.py this module is pure translation — newline-delimited
JSON-RPC in, JSON-RPC out — with the transport loop kept to a few lines so
everything is unit-testable without a subprocess. Session note: all calls
share one conversation ("mcp-default") per server process, mirroring how
the OpenAI-compatible endpoint scopes its API callers.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from typing import Any, Callable

PROTOCOL_VERSION = "2024-11-05"

# Methods that are always allowed unauthenticated — the handshake and liveness
# probe carry no user data and expose no tool.
_OPEN_METHODS = frozenset({"initialize", "ping"})


class MCPAuthError(RuntimeError):
    """The server is exposed but not safely authenticated (fail-closed)."""


def _mcp_user() -> str:
    return os.environ.get("OLYMPUS_MCP_USER", "shared")


# --- authentication (required before any network exposure) ----------------
#
# The MCP server is stdio-local by default: the OS process boundary is its
# trust boundary, so no token is needed. The moment it is EXPOSED beyond that
# (any network transport an operator puts in front of it), a bearer token is
# MANDATORY and enforced fail-closed — an exposed server with no token refuses
# to serve, and every tool call must present the token.

def _configured_token() -> str | None:
    """The expected bearer token: OLYMPUS_MCP_AUTH_TOKEN_FILE (preferred — the
    token isn't in the process env) or OLYMPUS_MCP_AUTH_TOKEN. A set-but-
    unreadable/empty token file is a hard error, never a silent 'no auth'."""
    path = (os.environ.get("OLYMPUS_MCP_AUTH_TOKEN_FILE") or "").strip()
    if path:
        try:
            tok = open(path, encoding="utf-8").read().strip()
        except OSError as err:
            raise MCPAuthError(
                f"OLYMPUS_MCP_AUTH_TOKEN_FILE {path!r} is unreadable: {err}")
        if not tok:
            raise MCPAuthError(
                f"OLYMPUS_MCP_AUTH_TOKEN_FILE {path!r} is empty")
        return tok
    tok = (os.environ.get("OLYMPUS_MCP_AUTH_TOKEN") or "").strip()
    return tok or None


def exposed() -> bool:
    """Whether the operator has declared this server network-exposed."""
    return (os.environ.get("OLYMPUS_MCP_EXPOSED", "").strip().lower()
            in ("1", "on", "true", "yes"))


def require_auth() -> bool:
    """Auth is enforced when the server is exposed OR a token is configured."""
    return exposed() or _configured_token() is not None


def credential_ok(cred: str | None) -> bool:
    """Constant-time check of a presented credential against the configured
    token. False when no token is configured (an unauthenticated server never
    'accepts' a credential) or the credential is missing/wrong.

    Compared as UTF-8 BYTES: `secrets.compare_digest` on `str` raises TypeError
    if either side contains a non-ASCII character, and this runs on
    attacker-supplied input — so a crafted token with one non-ASCII byte would
    otherwise raise instead of returning False (a crash on the auth surface).
    Bytes comparison is constant-time and never raises on content."""
    expected = _configured_token()
    if not expected or not cred:
        return False
    return secrets.compare_digest(str(cred).encode("utf-8"),
                                  str(expected).encode("utf-8"))


def assert_serveable() -> None:
    """Fail-closed startup guard: an EXPOSED server MUST have a token, or it
    refuses to run — no network exposure without authentication, ever."""
    if exposed() and _configured_token() is None:
        raise MCPAuthError(
            "OLYMPUS_MCP_EXPOSED is set but no OLYMPUS_MCP_AUTH_TOKEN[_FILE] is "
            "configured — refusing to expose the MCP server without "
            "authentication. Set a token (a token FILE is preferred) or unset "
            "OLYMPUS_MCP_EXPOSED to keep it stdio-local.")


def _credential_from(msg: dict, session: dict) -> str | None:
    """A per-message credential: the token from this message's params, else the
    session token captured at initialize."""
    params = msg.get("params") or {}
    return (params.get("authToken") or params.get("_authToken")
            or session.get("token"))


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
    # Code-graph reads. Same trust story as the other workspace tools: pure
    # reads of graph structure, no actuator, scoped to the exposed project.
    {
        "name": "olympus_query_codegraph",
        "description": ("Token-budgeted subgraph of the code knowledge graph "
                        "relevant to a question — symbols, how they connect, "
                        "confidence tags. Read-only."),
        "inputSchema": {
            "type": "object",
            "properties": {"question": {"type": "string"},
                           "budget_tokens": {"type": "integer"}},
            "required": ["question"],
        },
    },
    {
        "name": "olympus_codegraph_impact",
        "description": ("Reverse-dependency analysis from the code graph: "
                        "everything that transitively calls/imports a symbol "
                        "— 'what breaks if this changes'. Read-only."),
        "inputSchema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
    },
    {
        "name": "olympus_codegraph_report",
        "description": ("The full code-graph report: god nodes, communities, "
                        "surprising connections, suggested questions. "
                        "Read-only."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "olympus_verify_code_claim",
        "description": ("Verify a structural claim ('X calls Y', 'Z is "
                        "unused') against the graph's ground-truth edges: "
                        "CONFIRMED, REFUTED, or UNKNOWN. Read-only."),
        "inputSchema": {
            "type": "object",
            "properties": {"claim": {"type": "string"}},
            "required": ["claim"],
        },
    },
    # Aegis / discovery reads. Read/prepare only: exported findings and pure
    # self-scorecards — never a scope grant, a scan, or a network call.
    {
        "name": "olympus_assess_report",
        "description": ("Export the Aegis security assessment's ledgered "
                        "findings as markdown (default), json, or SARIF 2.1.0 "
                        "(GitHub code-scanning compatible). Read-only — reports "
                        "already-recorded findings, never runs a scan."),
        "inputSchema": {
            "type": "object",
            "properties": {"format": {"type": "string",
                                      "enum": ["markdown", "json", "sarif"]}},
        },
    },
    {
        "name": "olympus_assess_scorecard",
        "description": ("The Aegis self-benchmark (precision/recall/F1 over a "
                        "labeled corpus) plus the blast-radius containment proof "
                        "(each of the surveyed agent's damage vectors mapped to an owned "
                        "control). Pure — no scope, network, or scan."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "olympus_discover_report",
        "description": ("The self-discovery ledger: open knowledge/capability "
                        "gaps and filed feature proposals. Read-only."),
        "inputSchema": {"type": "object", "properties": {}},
    },
]

_CG_PROJECT_ENV = "OLYMPUS_MCP_CODEGRAPH_PROJECT"


def _codegraph_tool(name: str, args: dict) -> str:
    """Read-only code-graph tools, scoped to one project (default self)."""
    import os as _os

    from . import codegraph
    project = _os.environ.get(_CG_PROJECT_ENV, "self")
    if name == "olympus_query_codegraph":
        try:
            budget = max(200, min(4000, int(args.get("budget_tokens", 1500))))
        except (TypeError, ValueError):
            budget = 1500
        return (codegraph.subgraph_query(project,
                                         str(args.get("question", "")),
                                         budget_tokens=budget)
                or "Nothing in the graph matches that question.")
    if name == "olympus_codegraph_impact":
        hits = codegraph.find(project, str(args.get("symbol", "")))
        if not hits:
            return "No such symbol in the graph."
        deps = codegraph.impact(project, hits[0]["id"])
        return ("\n".join(f"- {d['label']} ({d['kind']}) — "
                          f"{d.get('path', '?')}" for d in deps)
                or "Nothing depends on it (per the graph).")
    if name == "olympus_codegraph_report":
        from . import codegraph_analysis
        return codegraph_analysis.render_report(project)
    if name == "olympus_verify_code_claim":
        v = codegraph.verify_claim(project, str(args.get("claim", "")))
        return f"{v['verdict']}: {v['detail']}"
    raise KeyError(name)


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


def _governed_tool(name: str, args: dict) -> str:
    """Read/prepare-only Aegis + discovery surfaces, scoped to the exposed user.
    Governed by the same boundary as the workspace tools: export already-recorded
    findings and pure self-scorecards ONLY. It never grants a scope, runs a scan,
    or makes a network call — an assessment still requires a signed, in-process
    authorization that cannot be initiated across the MCP boundary."""
    from . import memory
    user = _mcp_user()
    memory.set_user(user)
    if name == "olympus_assess_report":
        from . import assess
        fmt = str(args.get("format", "markdown")).strip().lower()
        if fmt not in ("markdown", "json", "sarif"):
            fmt = "markdown"
        return assess.export_findings(fmt, user=user)
    if name == "olympus_assess_scorecard":
        from . import assess
        return (assess.bench_scorecard() + "\n\n"
                + assess.containment_scorecard(user=user))
    if name == "olympus_discover_report":
        from . import discovery
        return discovery.report(user=user)
    raise KeyError(name)


def _result(rid, payload: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": payload}


def _error(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": code, "message": message}}


def handle_message(msg: dict, ask: Callable[[str], str] | None = None,
                   *, session: dict | None = None) -> dict | None:
    """Handle one JSON-RPC message; return the response dict, or None for
    notifications (which must not be answered). `ask` is injectable. `session`
    (per-connection) carries the authenticated token captured at initialize.

    When auth is required (the server is exposed or a token is configured),
    every method except the handshake/liveness probe must present the token —
    fail-closed: a missing/wrong credential is refused, no tool runs."""
    ask = ask or _default_ask
    session = session if session is not None else {}
    rid = msg.get("id")
    method = msg.get("method", "")

    if rid is None:                     # notification — no response, ever
        return None

    if method == "initialize":
        from . import __version__
        params = msg.get("params") or {}
        # Capture a presented token for the rest of this connection.
        tok = params.get("authToken") or params.get("_authToken")
        if tok and credential_ok(tok):
            session["token"] = str(tok)
        return _result(rid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "olympus-council",
                           "version": __version__},
            "authRequired": require_auth(),
        })
    if method == "ping":
        return _result(rid, {})

    # Everything below exposes a tool or user data — gate it when auth is on.
    if require_auth() and method not in _OPEN_METHODS:
        if not credential_ok(_credential_from(msg, session)):
            return _error(rid, -32001,
                          "unauthorized: a valid auth token is required "
                          "(this MCP server is exposed/authenticated)")

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
                text = goals.summary(_mcp_user())
            elif name in ("olympus_search_documents", "olympus_list_todos",
                          "olympus_recall_memory"):
                text = _workspace_tool(name, args)
            elif name in ("olympus_query_codegraph", "olympus_codegraph_impact",
                          "olympus_codegraph_report",
                          "olympus_verify_code_claim"):
                text = _codegraph_tool(name, args)
            elif name in ("olympus_assess_report", "olympus_assess_scorecard",
                          "olympus_discover_report"):
                text = _governed_tool(name, args)
            else:
                return _error(rid, -32602, f"unknown tool '{name}'")
        except Exception as err:
            # In-band error text is a deliberate contract (the caller sees why a
            # tool failed). The read-only codegraph tools touch only local graph
            # state, so this carries no more than the existing council tools do.
            return _result(rid, {"content": [{"type": "text",
                                              "text": f"Error: {err}"}],
                                 "isError": True})
        return _result(rid, {"content": [{"type": "text", "text": text}]})

    return _error(rid, -32601, f"method not found: {method}")


def serve_stdio() -> None:
    """Newline-delimited JSON-RPC over stdin/stdout until EOF."""
    assert_serveable()          # fail-closed: no network exposure without auth
    session: dict = {}
    if require_auth():
        print("Olympus MCP server on stdio — AUTHENTICATED (token required for "
              "tool calls).", file=sys.stderr)
    else:
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
        # A well-formed JSON value that isn't an object is not a JSON-RPC
        # message — reject it without touching handle_message (which would
        # AttributeError on a list/number).
        if not isinstance(msg, dict):
            sys.stdout.write(json.dumps(
                _error(None, -32600, "invalid request")) + "\n")
            sys.stdout.flush()
            continue
        # One malformed/hostile message must never take the exposed server down
        # for every other client — turn any handler fault into a JSON-RPC error
        # (class name only; never echo internal detail).
        try:
            response = handle_message(msg, session=session)
        except Exception as err:
            response = _error(msg.get("id"), -32603,
                              f"internal error: {type(err).__name__}")
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()

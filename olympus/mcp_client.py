"""Native, provider-independent MCP client.

Olympus already *serves* MCP (`mcp_server.py`) and can reach *URL* MCP servers
through Anthropic's server-side connector — but that second-class path evaporates
on any non-Anthropic backend and cannot speak stdio (a local MCP process). This
module closes that gap: it connects to external MCP servers over **stdio** or
**SSE** from the client side, discovers their tools, and calls them, so the same
MCP tools are available uniformly on *every* backend (Anthropic and every
OpenAI-compatible provider) and for local tool servers.

Design choices (deliberate, for robustness over raw latency):

* The `mcp` SDK is async-only; Olympus tool handlers are synchronous
  (`handler(**params) -> str`). We bridge with a **thread-confined event loop**
  (`_run_sync`) so an MCP call works whether or not the caller sits inside an
  event loop, and a hung server can never wedge the agent loop — it times out.
* Connections are **ephemeral per operation** rather than a persistent pooled
  session. That trades a little startup cost (a stdio server re-spawns per call)
  for a large reduction in concurrency/lifecycle bugs, and keeps the whole
  module unit-testable without a live server. The `discover`/`call` seam is the
  only place the SDK is touched, so the connector/security wiring on top is
  fully testable by monkeypatching this seam.
* Every failure is contained to a string or an empty list — a missing `mcp`
  package, an unreachable server, or a crashing tool never propagates into the
  agent loop.

SECURITY: the transport and egress are gated in `connectors.mcp_scan_reason`
(https-only + injection checks for SSE, an explicit operator allowlist for
stdio's arbitrary local-process execution). Discovered tool *output* is treated
as untrusted external content by `security.should_wrap` (MCP tools are never on
the trusted allowlist), and data-MCP tools mark a run as ingesting so action
capabilities are stripped from it (capability separation).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

_log = logging.getLogger("olympus.mcp_client")

# tool-name convention: mcp__<server>__<tool>. `__` never appears in a server
# name (validated in connectors.mcp_scan_reason), so the split is unambiguous.
_PREFIX = "mcp__"

DISCOVER_TIMEOUT = 20.0
CALL_TIMEOUT = 120.0

# name -> "data" | "action": the trust kind of each discovered tool, populated
# by discover() and read by the connectors/security classification helpers so
# they never need to reconnect. A miss is treated as untrusted (fail-closed).
_KIND_CACHE: dict[str, str] = {}


def have_sdk() -> bool:
    """True when the optional `mcp` SDK is importable."""
    try:
        import mcp  # noqa: F401
        return True
    except Exception:
        return False


def tool_name(server: str, tool: str) -> str:
    return f"{_PREFIX}{server}{'__'}{tool}"


def split_name(name: str) -> tuple[str, str] | None:
    """`mcp__server__tool` -> (server, tool); None if not an MCP tool name."""
    if not name.startswith(_PREFIX):
        return None
    rest = name[len(_PREFIX):]
    server, sep, tool = rest.partition("__")
    if not sep or not server or not tool:
        return None
    return server, tool


def kind_of(name: str) -> str | None:
    """The trust kind ('data'/'action') of a discovered MCP tool, or None."""
    return _KIND_CACHE.get(name)


# --------------------------------------------------------------------------- #
# The sync<->async bridge (the only place the async SDK is touched).
# --------------------------------------------------------------------------- #

def _run_sync(coro_factory: Callable[[], Any], timeout: float) -> Any:
    """Run an async coroutine to completion on a private, thread-confined event
    loop and return its result synchronously. Works regardless of whether the
    caller already runs inside an event loop; a timeout is enforced so a wedged
    server cannot hang the agent."""
    import asyncio

    box: dict[str, Any] = {}

    def worker() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            box["value"] = loop.run_until_complete(
                asyncio.wait_for(coro_factory(), timeout))
        except BaseException as exc:  # noqa: BLE001 — contained on purpose
            box["error"] = exc
        finally:
            try:
                loop.close()
            except Exception:
                pass

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout + 10)
    if t.is_alive():
        raise TimeoutError("MCP operation did not finish in time")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _env_for(server) -> dict[str, str]:
    """The environment a stdio MCP subprocess is launched with: a minimal base
    plus any operator-named passthrough vars (`env_pass`). We never leak the
    whole environment to a spawned server."""
    import os
    base = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL")
            if k in os.environ}
    for name in getattr(server, "env_pass", ()) or ():
        if name in os.environ:
            base[name] = os.environ[name]
    return base


def _headers_for(server) -> dict[str, str]:
    import os
    headers: dict[str, str] = {}
    auth_env = getattr(server, "auth_env", None)
    if auth_env:
        token = os.environ.get(auth_env)
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


async def _with_session(server, fn):
    """Open the transport + an initialized ClientSession, run `fn(session)`, and
    tear everything down. Import points are defensive across `mcp` SDK layouts."""
    from mcp import ClientSession

    transport = getattr(server, "transport", "url")
    if transport == "stdio":
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client
        params = StdioServerParameters(
            command=server.command, args=list(server.args or []),
            env=_env_for(server))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await fn(session)
    else:  # "sse" (and any http-streamed URL we drive client-side)
        from mcp.client.sse import sse_client
        async with sse_client(url=server.url, headers=_headers_for(server)) \
                as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await fn(session)


# --------------------------------------------------------------------------- #
# Public seam: discover() and call().
# --------------------------------------------------------------------------- #

def discover(server) -> list[dict]:
    """Connect to `server`, list its tools, and return Olympus tool-schema dicts
    (namespaced `mcp__server__tool`). Returns [] on any failure — a broken
    server simply contributes no tools. Also records each tool's trust kind in
    `_KIND_CACHE` so the security classification never needs to reconnect."""
    if not have_sdk():
        _log.warning("the 'mcp' package is not installed; install "
                     "olympus-council[mcp] to use stdio/SSE MCP servers")
        return []

    async def _list(session):
        resp = await session.list_tools()
        return list(resp.tools)

    try:
        raw = _run_sync(lambda: _with_session(server, _list), DISCOVER_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — contained
        _log.warning("discovery of '%s' failed: %s",
                     getattr(server, "name", "?"), exc)
        return []

    kind = (getattr(server, "type", "data") or "data").lower()
    allowed = set(getattr(server, "allowed_tools", ()) or ())
    schemas: list[dict] = []
    for t in raw or []:
        if allowed and t.name not in allowed:
            continue
        full = tool_name(server.name, t.name)
        _KIND_CACHE[full] = kind
        schemas.append({
            "name": full,
            "description": (getattr(t, "description", "") or
                            f"MCP tool '{t.name}' on server '{server.name}'."),
            "input_schema": getattr(t, "inputSchema", None)
            or {"type": "object", "properties": {}},
        })
    return schemas


def _render(result) -> str:
    """Flatten an MCP call result into a string the model can read."""
    parts: list[str] = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(text)
        else:
            parts.append(str(item))
    out = "\n".join(p for p in parts if p)
    if getattr(result, "isError", False):
        return f"MCP tool reported an error:\n{out or '(no detail)'}"
    return out or "(no output)"


def call(server_name: str, tool: str, args: dict | None = None) -> str:
    """Call `tool` on the named MCP server and return its output as a string.
    Never raises: an unconfigured/unreachable server or a failing tool becomes a
    descriptive error string."""
    server = _resolve(server_name)
    if server is None:
        return (f"Error: MCP server '{server_name}' is not configured "
                "(check `olympus connectors`).")
    if not have_sdk():
        return ("Error: the 'mcp' package is not installed — run "
                "`pip install olympus-council[mcp]` to enable stdio/SSE MCP.")

    async def _call(session):
        return await session.call_tool(tool, args or {})

    try:
        result = _run_sync(lambda: _with_session(server, _call), CALL_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — contained
        return f"Error calling MCP tool '{tool}' on '{server_name}': {exc}"
    return _render(result)


def _resolve(server_name: str):
    """Find a configured native (stdio/sse) MCP server by name."""
    from . import connectors  # lazy: connectors is the higher layer
    for s in connectors.mcp_servers():
        if s.name == server_name and getattr(s, "transport", "url") in (
                "stdio", "sse"):
            return s
    return None

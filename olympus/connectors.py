"""Connectors — MCP servers + custom plugins.

Two ways to extend Olympus with external tools and data:

1. **MCP servers**. Connect any of the hundreds of community MCP servers
   (GitHub, Notion, Postgres, Slack, Tavily, …) by adding a definition. Three
   transports:
     - `url`   — Anthropic's server-side connector; tools run on Anthropic's
                 infra (default; Anthropic backend only).
     - `sse`   — the native client (olympus/mcp_client.py) drives the server
                 over HTTP/SSE; works on EVERY backend.
     - `stdio` — the native client spawns a LOCAL MCP process; works on every
                 backend, gated behind OLYMPUS_MCP_STDIO_ALLOWLIST (arbitrary
                 local execution). Native tools are namespaced `mcp__srv__tool`
                 and dispatch through resolve_handler like any builtin.

2. **Custom plugins** (local Python). Drop a `.py` file in the plugins
   directory that registers tool functions with `@plugin(...)`. These run
   locally and work on EVERY backend (Anthropic and OpenAI-compatible) — the
   simplest way to wire Olympus to your own REST API or internal system.

Both are governed by the same security model as built-in tools:
- A connector is `data` (read/ingest) or `action` (acts on the world).
- A specialist run that ingests external content (web, data MCP, data plugin)
  never also holds action capabilities (capability separation).
- Action connectors are attached deliberately, per specialist.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import config

# ─────────────────────────── custom plugins ───────────────────────────

@dataclass(frozen=True)
class Plugin:
    name: str
    description: str
    schema: dict
    handler: Callable[..., str]
    specialists: tuple[str, ...]      # () = available to all specialists
    action: bool                      # action (acts on world) vs data (reads)

    def tool_def(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "input_schema": self.schema}


_PLUGINS: dict[str, Plugin] = {}
_PLUGINS_LOADED = False


def plugin(name: str, description: str, schema: dict | None = None, *,
           specialists: tuple[str, ...] | list[str] = (),
           action: bool = False):
    """Decorator registering a local function as an Olympus tool.

    Example:
        @plugin("crm_lookup", "Look up a customer in our CRM",
                schema={"type": "object",
                        "properties": {"email": {"type": "string"}},
                        "required": ["email"]},
                specialists=["plutus"], action=False)
        def crm_lookup(email: str) -> str:
            ...
    """
    def deco(fn: Callable[..., str]) -> Callable[..., str]:
        _PLUGINS[name] = Plugin(
            name=name, description=description,
            schema=schema or {"type": "object", "properties": {}},
            handler=fn, specialists=tuple(specialists), action=action)
        return fn
    return deco


def _plugins_dir() -> Path | None:
    env = os.environ.get("OLYMPUS_PLUGINS_DIR")
    if env:
        return Path(env)
    default = config.PROJECT_ROOT / "plugins"
    return default if default.is_dir() else None


def load_plugins(force: bool = False) -> None:
    """Import every .py in the plugins directories once, registering its tools.

    Two directories are scanned: the classic dev dir (PROJECT_ROOT/plugins or
    OLYMPUS_PLUGINS_DIR) and the hardened installer's dir (pluginstore). When
    OLYMPUS_PLUGIN_ENFORCE is on, ONLY plugins whose on-disk hash matches the
    install manifest are run — an installer-tampered or unmanifested file is
    skipped, closing the "drop a file, it executes" hole."""
    global _PLUGINS_LOADED
    if _PLUGINS_LOADED and not force:
        return
    _PLUGINS_LOADED = True
    from . import pluginstore
    verified = pluginstore.verified_names()      # None = enforcement off
    dirs = []
    dev = _plugins_dir()
    if dev and dev.is_dir():
        dirs.append(dev)
    store = pluginstore.plugins_dir()
    if store not in dirs:
        dirs.append(store)
    seen: set[str] = set()
    for d in dirs:
        for path in sorted(d.glob("*.py")):
            if path.name.startswith("_") or path.stem in seen:
                continue
            if verified is not None and path.stem not in verified:
                print(f"[connectors] skipping unverified plugin {path.name} "
                      "(OLYMPUS_PLUGIN_ENFORCE is on)")
                continue
            seen.add(path.stem)
            try:
                spec = importlib.util.spec_from_file_location(
                    f"olympus_plugin_{path.stem}", path)
                module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
                spec.loader.exec_module(module)  # type: ignore[union-attr]
            except Exception as err:  # a broken plugin must not crash Olympus
                print(f"[connectors] failed to load plugin {path.name}: {err}")


def plugin_tools_for(specialist_key: str, *, allow_action: bool) -> list[dict]:
    load_plugins()
    out = []
    for p in _PLUGINS.values():
        if p.specialists and specialist_key not in p.specialists:
            continue
        if p.action and not allow_action:
            continue
        out.append(p.tool_def())
    return out


def plugin_handler(name: str) -> Callable[..., str] | None:
    load_plugins()
    p = _PLUGINS.get(name)
    return p.handler if p else None


def plugin_action_names() -> set[str]:
    load_plugins()
    return {p.name for p in _PLUGINS.values() if p.action}


def is_data_plugin(name: str) -> bool:
    load_plugins()
    p = _PLUGINS.get(name)
    return p is not None and not p.action


def plugin_data_names_for(specialist_key: str) -> set[str]:
    load_plugins()
    return {p.name for p in _PLUGINS.values()
            if not p.action and (not p.specialists
                                 or specialist_key in p.specialists)}


# ─────────────────────────── lifecycle hooks ───────────────────────────
#
# Plugins can also observe and (for tools) intercept the agent loop without
# forking Olympus. Events:
#
#   session_start(user, conversation_id)      an Olympus instance came up
#   session_end(user, conversation_id)        the conversation closed
#   run_start(user, message) / run_end(user, reply)   one ask() pipeline
#   pre_compact(user, text)    memory compaction/flush is about to run
#   pre_llm_call(params) / post_llm_call(params, response)   observe-only
#   pre_tool(name, params)     may BLOCK ({"block": reason}) or REWRITE the
#                              input ({"params": {...}}); None = pass through
#   post_tool(name, params, result)   may REWRITE the result by returning a
#                                     string; None = pass through
#
# This mirrors the Claude Code hook surface natively (session_start≈SessionStart,
# session_end≈SessionEnd, run_start≈UserPromptSubmit, run_end≈Stop,
# pre_compact≈PreCompact, pre_tool≈PreToolUse, post_tool≈PostToolUse), so a
# plugin written against those lifecycle points has an Olympus home.
#
# pre/post_llm_call are deliberately observe-only: mutating the request there
# would silently change the replay hash and break byte-identical replay. Tool
# interception runs BEFORE execution, so a block composes with (never
# bypasses) the approval spine — it can only make policy stricter. A raising
# hook is logged and skipped: a broken plugin must not take down the agent.

HOOK_EVENTS = ("session_start", "session_end", "run_start", "run_end",
               "pre_compact", "pre_llm_call", "post_llm_call",
               "pre_tool", "post_tool")

_HOOKS: dict[str, list[Callable]] = {}

# Observe-only events: their args are handed to plugins as deep copies so an
# in-place mutation cannot reach the caller (see emit / I-N2).
_OBSERVE_ONLY_EVENTS = frozenset({"pre_llm_call", "post_llm_call"})


def hook(event: str):
    """Decorator registering a plugin lifecycle hook.

    Example (block a tool against a denylist):
        @hook("pre_tool")
        def no_prod_writes(name, params):
            if name == "call_webhook" and "prod" in str(params):
                return {"block": "prod webhooks are off-limits from plugins"}
    """
    if event not in HOOK_EVENTS:
        raise ValueError(f"unknown hook event '{event}' "
                         f"(expected one of {', '.join(HOOK_EVENTS)})")

    def deco(fn: Callable) -> Callable:
        _HOOKS.setdefault(event, []).append(fn)
        return fn
    return deco


def emit(event: str, *args) -> None:
    """Fire observe-only hooks; exceptions are contained.

    I-N2 (observability non-interference): for the observe-only LLM-call events
    (`pre_llm_call`, `post_llm_call`) the caller passes its LIVE params dict —
    already hashed for replay in llm.complete. A plugin that mutated it in place
    would silently desync record/replay, so each dict arg is deep-copied before
    dispatch; the return value is ignored and the copy is discarded by design.
    All OTHER events dispatch their args unchanged (byte-for-byte), and the
    deliberately mutating tool hooks live in emit_pre_tool/emit_post_tool."""
    load_plugins()
    if event in _OBSERVE_ONLY_EVENTS:
        args = tuple(copy.deepcopy(a) if isinstance(a, dict) else a
                     for a in args)
    for fn in _HOOKS.get(event, ()):
        try:
            fn(*args)
        except Exception as err:
            print(f"[connectors] {event} hook "
                  f"{getattr(fn, '__name__', '?')} failed: {err}")


def emit_pre_tool(name: str, params: dict) -> tuple[dict, str | None]:
    """Run pre_tool hooks. Returns (possibly-rewritten params, block_reason).
    The first hook that blocks wins; rewrites chain in registration order."""
    load_plugins()
    for fn in _HOOKS.get("pre_tool", ()):
        try:
            verdict = fn(name, params)
        except Exception as err:
            print(f"[connectors] pre_tool hook "
                  f"{getattr(fn, '__name__', '?')} failed: {err}")
            continue
        if not isinstance(verdict, dict):
            continue
        if verdict.get("block"):
            return params, str(verdict["block"])
        if isinstance(verdict.get("params"), dict):
            params = verdict["params"]
    return params, None


def emit_post_tool(name: str, params: dict, result: str) -> str:
    """Run post_tool hooks; a hook returning a string replaces the result."""
    load_plugins()
    for fn in _HOOKS.get("post_tool", ()):
        try:
            out = fn(name, params, result)
        except Exception as err:
            print(f"[connectors] post_tool hook "
                  f"{getattr(fn, '__name__', '?')} failed: {err}")
            continue
        if isinstance(out, str):
            result = out
    return result


def clear_hooks() -> None:
    """Testing aid: forget every registered hook."""
    _HOOKS.clear()


# ─────────────────────────── MCP servers ───────────────────────────

@dataclass(frozen=True)
class MCPServer:
    name: str
    url: str = ""                      # required for url/sse; empty for stdio
    type: str = "data"                 # "data" | "action"
    auth_env: str | None = None        # env var holding the bearer token
    specialists: tuple[str, ...] = ()  # () = all specialists
    allowed_tools: tuple[str, ...] = ()  # restrict to these MCP tool names
    # transport:
    #   "url"   — Anthropic server-side connector (default; anthropic backend only)
    #   "sse"   — native client, HTTP/SSE, works on EVERY backend
    #   "stdio" — native client, local subprocess, works on EVERY backend
    transport: str = "url"
    command: str = ""                  # stdio: the program to launch
    args: tuple[str, ...] = ()         # stdio: its arguments
    env_pass: tuple[str, ...] = ()     # stdio: host env var NAMES to pass through

    def is_native(self) -> bool:
        """True for the client-driven transports (sse/stdio) — the ones served
        by olympus.mcp_client on all backends, as opposed to the Anthropic
        server-side url connector."""
        return self.transport in ("sse", "stdio")

    def to_api(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": "url", "name": self.name, "url": self.url}
        if self.auth_env:
            token = os.environ.get(self.auth_env)
            if token:
                d["authorization_token"] = token
        if self.allowed_tools:
            d["tool_configuration"] = {"allowed_tools": list(self.allowed_tools)}
        return d


def _config_path() -> Path:
    return config.MEMORY_DIR / "connectors.json"


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file + os.replace so a crash or a concurrent reader can
    never see a half-written connectors.json (the corruption that used to make
    the next add_mcp_server drop the whole registry)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    from . import atomicio
    atomicio.publish(tmp, path, text)


def _raw_mcp_definitions() -> list[dict]:
    defs: list[dict] = []
    path = _config_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            defs += data.get("servers", [])
        except (json.JSONDecodeError, OSError):
            pass
    env = os.environ.get("OLYMPUS_MCP_SERVERS")
    if env:
        try:
            defs += json.loads(env).get("servers", json.loads(env)) \
                if env.strip().startswith("{") else []
        except json.JSONDecodeError:
            pass
    return defs


def mcp_servers() -> list[MCPServer]:
    servers = []
    for d in _raw_mcp_definitions():
        if not d.get("name"):
            continue
        transport = (d.get("transport") or "url").lower()
        # url/sse need a url; stdio needs a command. Skip malformed entries.
        if transport in ("url", "sse") and not d.get("url"):
            continue
        if transport == "stdio" and not d.get("command"):
            continue
        servers.append(MCPServer(
            name=d["name"], url=d.get("url", ""),
            type=(d.get("type") or "data").lower(),
            auth_env=d.get("auth_env"),
            specialists=tuple(d.get("specialists") or ()),
            allowed_tools=tuple(d.get("allowed_tools") or ()),
            transport=transport,
            command=d.get("command", ""),
            args=tuple(d.get("args") or ()),
            env_pass=tuple(d.get("env_pass") or ()),
        ))
    return servers


def action_allowlist() -> set[str]:
    """Action MCP servers the operator has explicitly enabled by name.

    Mirrors the email/webhook actuator pattern: defining an action server in
    connectors.json is not enough — it stays inert until its name is also in
    OLYMPUS_MCP_ACTION_ALLOWLIST. Two independent gates, both operator-only.
    """
    raw = os.environ.get("OLYMPUS_MCP_ACTION_ALLOWLIST", "")
    return {n.strip() for n in raw.split(",") if n.strip()}


def mcp_for(specialist_key: str, *, allow_action: bool) -> list[MCPServer]:
    """Anthropic server-side (url) MCP servers attached to a specialist. Native
    sse/stdio servers are excluded — they are served client-side by
    olympus.mcp_client (see mcp_client_tools_for)."""
    allowed_actions = action_allowlist()
    out = []
    for s in mcp_servers():
        if s.transport != "url":
            continue  # native transports run through the client, not the API
        if s.specialists and specialist_key not in s.specialists:
            continue
        if s.type == "action":
            if not allow_action:
                continue
            if s.name not in allowed_actions:
                continue  # configured but not operator-allowlisted -> inert
        out.append(s)
    return out


def _native_mcp_for(specialist_key: str, *, allow_action: bool) -> list[MCPServer]:
    """Native (sse/stdio) MCP servers attached to a specialist, after the SAME
    action-allowlist gate as the server-side path. Used by the client to know
    which servers to discover/dispatch for this run."""
    allowed_actions = action_allowlist()
    out = []
    for s in mcp_servers():
        if not s.is_native():
            continue
        if s.specialists and specialist_key not in s.specialists:
            continue
        if s.type == "action":
            if not allow_action or s.name not in allowed_actions:
                continue  # action servers are inert unless operator-allowlisted
        out.append(s)
    return out


def mcp_client_tools_for(specialist_key: str, *, allow_action: bool) -> list[dict]:
    """Tool schemas from the native (client-driven) MCP servers attached to this
    specialist, namespaced `mcp__server__tool`. Mirrors plugin_tools_for: action
    servers are excluded unless allowed, so a run that ingests untrusted content
    never receives an MCP actuator (capability separation)."""
    from . import mcp_client
    out: list[dict] = []
    for s in _native_mcp_for(specialist_key, allow_action=allow_action):
        out += mcp_client.discover(s)
    return out


def mcp_client_handler(name: str) -> Callable[..., str] | None:
    """Dispatch for a namespaced native MCP tool (`mcp__server__tool`)."""
    from . import mcp_client
    parts = mcp_client.split_name(name)
    if not parts:
        return None
    server, tool = parts
    if mcp_client._resolve(server) is None:
        return None
    return lambda **kwargs: mcp_client.call(server, tool, kwargs)


def mcp_client_action_names() -> set[str]:
    """Discovered native MCP tool names whose server is an ACTION server — the
    set capability-separation strips from any ingesting run."""
    from . import mcp_client
    return {n for n, kind in mcp_client._KIND_CACHE.items() if kind == "action"}


def mcp_client_data_names() -> set[str]:
    """Discovered native MCP tool names whose server is a DATA server — treated
    as ingestion (their output is untrusted external content)."""
    from . import mcp_client
    return {n for n, kind in mcp_client._KIND_CACHE.items() if kind == "data"}


def specialist_has_data_mcp(specialist_key: str) -> bool:
    return any(s.type == "data" and (not s.specialists
                                     or specialist_key in s.specialists)
               for s in mcp_servers())


_ENV_NAME = None      # compiled lazily


def stdio_allowlist() -> set[str]:
    """Native stdio MCP servers the operator has explicitly enabled by name.

    A stdio server is an arbitrary LOCAL process Olympus will spawn — strictly
    more dangerous than a URL connector — so it stays inert until its name is in
    OLYMPUS_MCP_STDIO_ALLOWLIST, mirroring the action-allowlist pattern."""
    raw = os.environ.get("OLYMPUS_MCP_STDIO_ALLOWLIST", "")
    return {n.strip() for n in raw.split(",") if n.strip()}


def mcp_scan_reason(name: str, url: str, auth_env: str | None = None, *,
                    transport: str = "url", command: str = "") -> str | None:
    """Security scan for an MCP server definition; reason string when it must
    be refused, None when clean. Persisted connector config is a durable
    attack surface — a poisoned entry exfiltrates every future request's
    context — so definitions are validated on the way IN, not trusted later."""
    global _ENV_NAME
    import re
    from . import security
    if _ENV_NAME is None:
        _ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    transport = (transport or "url").lower()
    if security.looks_like_injection(name):
        return "the server name contains prompt-injection markers"
    if transport in ("sse", "stdio") and "__" in name:
        # The name namespaces every tool as mcp__<name>__<tool>; a '__' inside
        # it would make the tool-name split ambiguous.
        return "a native MCP server name may not contain '__'"

    if transport == "stdio":
        if not command:
            return "a stdio MCP server needs a 'command' to launch"
        # Arbitrary local execution — require the explicit operator allowlist.
        if name not in stdio_allowlist():
            return ("stdio MCP servers execute a local process, so they are "
                    "inert until the operator adds the name to "
                    "OLYMPUS_MCP_STDIO_ALLOWLIST")
    else:  # url / sse both ride HTTP and carry tokens on every request
        if not url.lower().startswith("https://"):
            return "MCP server URLs must be https (tokens ride on every request)"
        if security._URL_CRED.search(url):
            return "the URL embeds credentials — use auth_env instead"
        reason = security.url_block_reason(url)
        if reason:
            return f"the URL is blocked ({reason})"

    if auth_env and not _ENV_NAME.match(auth_env):
        return ("auth_env must be an environment variable NAME (e.g. "
                "MY_MCP_TOKEN), never a literal token — tokens don't belong "
                "in the config file")
    if auth_env and security._KEYISH.search(auth_env):
        return ("auth_env looks like a literal credential — set the token in "
                "the environment and pass its variable name instead")
    return None


def add_mcp_server(name: str, url: str, type: str = "data",
                   auth_env: str | None = None,
                   specialists: list[str] | None = None) -> str:
    """Persist a new MCP server definition to memory/connectors.json.
    Definitions are security-scanned and refused on a hit."""
    reason = mcp_scan_reason(name, url, auth_env)
    if reason:
        return f"Error: refused to save MCP server '{name}' — {reason}."
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"servers": []}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # FAIL CLOSED: a corrupt/truncated file must not be silently reset
            # to empty and overwritten — that would DROP every previously saved
            # MCP server. Refuse, exactly as remove_mcp_server does.
            return ("Error: connectors.json is unreadable — refusing to "
                    "overwrite it (that would drop every saved MCP server). "
                    "Fix or remove the file, then retry.")
    if not isinstance(data, dict):
        return "Error: connectors.json is malformed (not a JSON object)."
    servers = data.get("servers")
    data["servers"] = servers if isinstance(servers, list) else []
    data["servers"] = [s for s in data["servers"] if s.get("name") != name]
    entry = {"name": name, "url": url, "type": type}
    if auth_env:
        entry["auth_env"] = auth_env
    if specialists:
        entry["specialists"] = specialists
    data["servers"].append(entry)
    _atomic_write(path, json.dumps(data, indent=2))
    return f"MCP server '{name}' saved ({type})."


def remove_mcp_server(name: str) -> str:
    """Delete an MCP server definition from memory/connectors.json. Live
    everywhere immediately — definitions are re-read from the file per call."""
    path = _config_path()
    if not path.exists():
        return f"Error: no MCP server named '{name}'."
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "Error: connectors.json is unreadable."
    servers = data.get("servers", [])
    kept = [s for s in servers if s.get("name") != name]
    if len(kept) == len(servers):
        return f"Error: no MCP server named '{name}'."
    data["servers"] = kept
    _atomic_write(path, json.dumps(data, indent=2))
    return f"MCP server '{name}' removed."


def summary() -> str:
    load_plugins()
    lines = ["MCP servers:"]
    servers = mcp_servers()
    allowed = action_allowlist()
    stdio_ok = stdio_allowlist()
    if servers:
        for s in servers:
            who = ", ".join(s.specialists) or "all"
            auth = f" auth:{s.auth_env}" if s.auth_env else ""
            state = ""
            if s.type == "action":
                state = (" [ACTIVE: allowlisted]" if s.name in allowed
                         else " [INERT: add to OLYMPUS_MCP_ACTION_ALLOWLIST]")
            if s.transport == "stdio" and s.name not in stdio_ok:
                state += " [INERT: add to OLYMPUS_MCP_STDIO_ALLOWLIST]"
            reach = ("server-side/anthropic" if s.transport == "url"
                     else "native/all-backends")
            target = s.url if s.transport in ("url", "sse") else \
                f"{s.command} {' '.join(s.args)}".strip()
            lines.append(f"  - {s.name} ({s.type}, {s.transport}:{reach})"
                         f"{state} → {who}{auth}  {target}")
    else:
        lines.append("  (none configured)")
    lines.append("Custom plugins:")
    if _PLUGINS:
        for p in _PLUGINS.values():
            who = ", ".join(p.specialists) or "all"
            kind = "action" if p.action else "data"
            lines.append(f"  - {p.name} ({kind}) → {who}: {p.description[:60]}")
    else:
        lines.append("  (none loaded)")
    return "\n".join(lines)

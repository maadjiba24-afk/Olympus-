"""Connectors — MCP servers + custom plugins.

Two ways to extend Olympus with external tools and data:

1. **MCP servers** (remote, URL-based). Olympus speaks the Model Context
   Protocol natively on the Anthropic backend — connect any of the hundreds of
   community MCP servers (GitHub, Notion, Postgres, Slack, Tavily, …) by adding
   a definition. Tools run server-side on Anthropic's infra.

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
    """Import every .py in the plugins directory once, registering its tools."""
    global _PLUGINS_LOADED
    if _PLUGINS_LOADED and not force:
        return
    _PLUGINS_LOADED = True
    d = _plugins_dir()
    if not d or not d.is_dir():
        return
    for path in sorted(d.glob("*.py")):
        if path.name.startswith("_"):
            continue
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


# ─────────────────────────── MCP servers ───────────────────────────

@dataclass(frozen=True)
class MCPServer:
    name: str
    url: str
    type: str = "data"                 # "data" | "action"
    auth_env: str | None = None        # env var holding the bearer token
    specialists: tuple[str, ...] = ()  # () = all specialists
    allowed_tools: tuple[str, ...] = ()  # restrict to these MCP tool names

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
        if not d.get("name") or not d.get("url"):
            continue
        servers.append(MCPServer(
            name=d["name"], url=d["url"],
            type=(d.get("type") or "data").lower(),
            auth_env=d.get("auth_env"),
            specialists=tuple(d.get("specialists") or ()),
            allowed_tools=tuple(d.get("allowed_tools") or ()),
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
    allowed_actions = action_allowlist()
    out = []
    for s in mcp_servers():
        if s.specialists and specialist_key not in s.specialists:
            continue
        if s.type == "action":
            if not allow_action:
                continue
            if s.name not in allowed_actions:
                continue  # configured but not operator-allowlisted -> inert
        out.append(s)
    return out


def specialist_has_data_mcp(specialist_key: str) -> bool:
    return any(s.type == "data" and (not s.specialists
                                     or specialist_key in s.specialists)
               for s in mcp_servers())


def add_mcp_server(name: str, url: str, type: str = "data",
                   auth_env: str | None = None,
                   specialists: list[str] | None = None) -> str:
    """Persist a new MCP server definition to memory/connectors.json."""
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"servers": []}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    data.setdefault("servers", [])
    data["servers"] = [s for s in data["servers"] if s.get("name") != name]
    entry = {"name": name, "url": url, "type": type}
    if auth_env:
        entry["auth_env"] = auth_env
    if specialists:
        entry["specialists"] = specialists
    data["servers"].append(entry)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return f"MCP server '{name}' saved ({type})."


def summary() -> str:
    load_plugins()
    lines = ["MCP servers:"]
    servers = mcp_servers()
    allowed = action_allowlist()
    if servers:
        for s in servers:
            who = ", ".join(s.specialists) or "all"
            auth = f" auth:{s.auth_env}" if s.auth_env else ""
            state = ""
            if s.type == "action":
                state = (" [ACTIVE: allowlisted]" if s.name in allowed
                         else " [INERT: add to OLYMPUS_MCP_ACTION_ALLOWLIST]")
            lines.append(f"  - {s.name} ({s.type}){state} → {who}{auth}  {s.url}")
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

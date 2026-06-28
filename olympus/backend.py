"""Provider dispatch — one neutral surface over Anthropic and OpenAI-compatible
backends, so the orchestrator and specialists never care which model runs.
"""

from __future__ import annotations

from typing import Any

from . import agent, claude_code, config, llm, openai_compat


def complete_text(settings: config.Settings, system: str,
                  messages: list[dict[str, Any]], effort: str = "high") -> str:
    if settings.provider == "anthropic":
        response = llm.complete(system, messages, settings=settings, effort=effort)
        if response.stop_reason == "refusal":
            return "[The model declined this request for safety reasons.]"
        return llm.text_of(response)
    if settings.provider == "claude-code":
        return claude_code.complete_text(settings, system, messages, effort)
    return openai_compat.complete_text(settings, system, messages, effort)


def complete_json(settings: config.Settings, system: str,
                  messages: list[dict[str, Any]], schema: dict[str, Any],
                  effort: str = "high") -> dict[str, Any]:
    if settings.provider == "anthropic":
        response = llm.complete(system, messages, settings=settings,
                                effort=effort, output_schema=schema)
        if response.stop_reason == "refusal":
            raise ValueError("model refused the request")
        return llm.json_of(response)
    if settings.provider == "claude-code":
        return claude_code.complete_json(settings, system, messages, schema, effort)
    return openai_compat.complete_json(settings, system, messages, schema, effort)


def run_agent(settings: config.Settings, system: str, task: str,
              tool_defs: list[dict[str, Any]] | None,
              mcp_servers: list[dict[str, Any]] | None = None,
              effort: str = "high") -> str:
    text, _ = run_agent_counted(settings, system, task, tool_defs,
                                mcp_servers=mcp_servers, effort=effort)
    return text


def run_agent_counted(settings: config.Settings, system: str, task: str,
                      tool_defs: list[dict[str, Any]] | None,
                      mcp_servers: list[dict[str, Any]] | None = None,
                      effort: str = "high") -> tuple[str, int | None]:
    """Like `run_agent`, but also returns the client-side tool-call count for
    the output-contract tool-call cap. Only the Anthropic path reports a count;
    other providers return None (the cap simply doesn't fire — see
    contracts.check)."""
    if settings.provider == "anthropic":
        return agent.run_agent_counted(system, task, settings=settings,
                                       tool_defs=tool_defs,
                                       mcp_servers=mcp_servers, effort=effort)
    if settings.provider == "claude-code":
        return claude_code.run_agent(settings, system, task, tool_defs, effort), None
    # MCP runs server-side on Anthropic only; other providers still get the
    # local plugin tools (which are included in tool_defs).
    return openai_compat.run_agent(settings, system, task, tool_defs, effort), None

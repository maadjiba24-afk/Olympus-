"""Provider dispatch — one neutral surface over Anthropic and OpenAI-compatible
backends, so the orchestrator and specialists never care which model runs.
"""

from __future__ import annotations

from typing import Any

from . import agent, config, llm, openai_compat


def complete_text(settings: config.Settings, system: str,
                  messages: list[dict[str, Any]], effort: str = "high") -> str:
    if settings.provider == "anthropic":
        response = llm.complete(system, messages, settings=settings, effort=effort)
        if response.stop_reason == "refusal":
            return "[The model declined this request for safety reasons.]"
        return llm.text_of(response)
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
    return openai_compat.complete_json(settings, system, messages, schema, effort)


def run_agent(settings: config.Settings, system: str, task: str,
              tool_defs: list[dict[str, Any]] | None,
              effort: str = "high") -> str:
    if settings.provider == "anthropic":
        return agent.run_agent(system, task, settings=settings,
                               tool_defs=tool_defs, effort=effort)
    return openai_compat.run_agent(settings, system, task, tool_defs, effort)

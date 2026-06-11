"""The agentic loop shared by every Olympus agent.

Handles client-side tool execution, server-side tool continuation
(`pause_turn`), and refusal stop reasons.
"""

from __future__ import annotations

from typing import Any

from . import config, llm, security, tools


def load_prompt(stem: str) -> str:
    return (config.PROMPTS_DIR / f"{stem}.md").read_text(encoding="utf-8")


def run_agent(
    system: str,
    task: str,
    *,
    settings: config.Settings | None = None,
    tool_defs: list[dict[str, Any]] | None = None,
    mcp_servers: list[dict[str, Any]] | None = None,
    effort: str = "high",
    max_iterations: int = config.MAX_AGENT_ITERATIONS,
) -> str:
    """Run one agent to completion on a task; return its final text output."""
    messages: list[dict[str, Any]] = [{"role": "user", "content": task}]

    for _ in range(max_iterations):
        response = llm.complete(system, messages, settings=settings,
                                tools=tool_defs, mcp_servers=mcp_servers,
                                effort=effort)

        if response.stop_reason == "refusal":
            return "[The model declined this request for safety reasons.]"

        # Server-side tool loop (web_search/web_fetch) hit its iteration cap —
        # append the assistant turn and re-send; the server resumes.
        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue

        if response.stop_reason != "tool_use":
            return llm.text_of(response)

        # Client-side tool calls.
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            handler = tools.resolve_handler(block.name)
            if handler is None:
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": f"Error: no handler for tool '{block.name}'",
                    "is_error": True,
                })
                continue
            try:
                output = handler(**(block.input or {}))
            except Exception as err:
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": f"Error: {err}",
                    "is_error": True,
                })
            else:
                text = str(output)
                if security.should_wrap(block.name):
                    text = security.wrap_untrusted(text, source=block.name)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": text,
                })
        messages.append({"role": "user", "content": results})

    return (
        "[Agent stopped: tool-use iteration limit reached. Partial work above "
        "may be incomplete.]"
    )

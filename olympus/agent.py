"""The agentic loop shared by every Olympus agent.

Handles client-side tool execution, server-side tool continuation
(`pause_turn`), and refusal stop reasons.
"""

from __future__ import annotations

import json
from typing import Any

from . import config, llm, replaystore, security, tools


def _assistant_turn(response) -> dict[str, Any]:
    """An assistant message whose content is the SDK's canonical JSON dicts.

    Re-sending raw SDK block objects makes a recorded run and its replay build
    *different* follow-up requests: a live response object and the same response
    reloaded from the frozen JSON don't serialize identically. Round-tripping
    through `to_json` (which is idempotent after one pass) yields byte-identical
    content in both record and replay, so the request hashes match."""
    return {"role": "assistant",
            "content": json.loads(response.to_json())["content"]}


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
    container: str | None = None

    for _ in range(max_iterations):
        response = llm.complete(system, messages, settings=settings,
                                tools=tool_defs, mcp_servers=mcp_servers,
                                container=container, effort=effort)

        # Preserve any server-side container (created by web search's dynamic
        # filtering) so the next turn can resume it.
        resp_container = getattr(response, "container", None)
        if resp_container is not None:
            container = getattr(resp_container, "id", None) or container

        if response.stop_reason == "refusal":
            return "[The model declined this request for safety reasons.]"

        # Server-side tool loop (web_search/web_fetch) hit its iteration cap —
        # append the assistant turn and re-send; the server resumes.
        if response.stop_reason == "pause_turn":
            messages.append(_assistant_turn(response))
            continue

        if response.stop_reason != "tool_use":
            return llm.text_of(response)

        # Client-side tool calls.
        messages.append(_assistant_turn(response))
        results = [_tool_result(block) for block in response.content
                   if block.type == "tool_use"]
        messages.append({"role": "user", "content": results})

    return (
        "[Agent stopped: tool-use iteration limit reached. Partial work above "
        "may be incomplete.]"
    )


def _tool_result(block) -> dict[str, Any]:
    """Run one client-side tool and return its tool_result block.

    For re-executable replay, the result is frozen by tool_use id in record
    mode and returned verbatim in replay mode — never re-executing, so a
    nondeterministic tool (e.g. `current_time`) or state that changed since the
    recorded run can't break a byte-identical replay. A tool call with no frozen
    result in replay mode means the orchestration diverged."""
    def block_result(content: str, is_error: bool) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "tool_result", "tool_use_id": block.id,
                               "content": content}
        if is_error:
            out["is_error"] = True
        return out

    if replaystore.replaying():
        frozen = replaystore.get_tool(block.id)
        if frozen is None:
            raise replaystore.ReplayDivergence(
                block.id, {"model": f"tool:{block.name}"})
        return block_result(frozen["content"], frozen.get("is_error", False))

    handler = tools.resolve_handler(block.name)
    if handler is None:
        content, is_error = f"Error: no handler for tool '{block.name}'", True
    else:
        try:
            output = handler(**(block.input or {}))
        except Exception as err:
            content, is_error = f"Error: {err}", True
        else:
            content = str(output)
            if security.should_wrap(block.name):
                content = security.wrap_untrusted(content, source=block.name)
            is_error = False
    replaystore.put_tool(block.id, content, is_error)
    return block_result(content, is_error)

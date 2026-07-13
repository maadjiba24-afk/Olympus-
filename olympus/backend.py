"""Provider dispatch — one neutral surface over Anthropic and OpenAI-compatible
backends, so the orchestrator and specialists never care which model runs.

Also the failover seam: when a call fails with a provider-side error (auth,
rate limit, outage) and the credentials belong to the operator's own pool,
the call retries on the other pool members in order instead of surfacing the
error. BYOK / per-request credentials NEVER fail over — silently switching a
visitor's request onto the operator's key (or another endpoint) would leak
spend and context across trust boundaries. OLYMPUS_FALLBACK=0 disables.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from . import agent, claude_code, config, llm, openai_compat


def _fallback_enabled() -> bool:
    return os.environ.get("OLYMPUS_FALLBACK", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _fingerprint(s: config.Settings) -> tuple:
    return (s.provider, s.model, s.api_key, s.base_url)


def _fallback_chain(settings: config.Settings) -> list[config.Settings]:
    """Alternate members to try when `settings` fails, strongest-for-the-
    failing-member's-role first (or the explicit OLYMPUS_ROLE_FALLBACKS
    order). Only operator-pool members are eligible, and only when `settings`
    itself IS one of them."""
    if not _fallback_enabled():
        return []
    try:
        pool = config.ModelPool.from_env()
    except Exception:
        return []
    fps = {_fingerprint(m) for m in pool.members}
    if _fingerprint(settings) not in fps:
        return []          # per-request/BYOK creds: never silently switch
    return [m for m in pool.fallbacks_for(settings) if m.provider != "moa"]


def _should_failover(err: Exception) -> bool:
    """Provider-side failures fail over; semantic outcomes don't. A refusal
    (ValueError), bad JSON, or a replay divergence must surface unchanged."""
    from . import replaystore
    if isinstance(err, replaystore.ReplayDivergence):
        return False
    if isinstance(err, ValueError):        # refusals, schema violations
        return False
    import anthropic
    return isinstance(err, (anthropic.APIError, RuntimeError,
                            ConnectionError, TimeoutError, OSError))


def _with_failover(settings: config.Settings,
                   call: Callable[[config.Settings], Any]) -> Any:
    try:
        return call(settings)
    except Exception as err:
        if not _should_failover(err):
            raise
        last = err
        for alt in _fallback_chain(settings):
            try:
                return call(alt)
            except Exception as err2:
                if not _should_failover(err2):
                    raise
                last = err2
        raise last


def _dispatch_text(s: config.Settings, system: str,
                   messages: list[dict[str, Any]], effort: str) -> str:
    if s.provider == "moa":
        from . import moa
        return moa.complete_text(s, system, messages, effort)
    if s.provider == "anthropic":
        response = llm.complete(system, messages, settings=s, effort=effort)
        if response.stop_reason == "refusal":
            return "[The model declined this request for safety reasons.]"
        return llm.text_of(response)
    if s.provider == "claude-code":
        return claude_code.complete_text(s, system, messages, effort)
    return openai_compat.complete_text(s, system, messages, effort)


def complete_text(settings: config.Settings, system: str,
                  messages: list[dict[str, Any]], effort: str = "high") -> str:
    return _with_failover(
        settings,
        lambda s: _dispatch_text(s, system, messages, effort))


def complete_text_once(settings: config.Settings, system: str,
                       messages: list[dict[str, Any]],
                       effort: str = "high") -> str:
    """Run exactly this model — NO cross-member failover. Used by blind compare,
    where substituting another model's answer for a failing one would corrupt
    the very thing being measured."""
    return _dispatch_text(settings, system, messages, effort)


def complete_json(settings: config.Settings, system: str,
                  messages: list[dict[str, Any]], schema: dict[str, Any],
                  effort: str = "high") -> dict[str, Any]:
    def call(s: config.Settings) -> dict[str, Any]:
        if s.provider == "moa":
            from . import moa
            return moa.complete_json(s, system, messages, schema, effort)
        if s.provider == "anthropic":
            response = llm.complete(system, messages, settings=s,
                                    effort=effort, output_schema=schema)
            if response.stop_reason == "refusal":
                raise ValueError("model refused the request")
            return llm.json_of(response)
        if s.provider == "claude-code":
            return claude_code.complete_json(s, system, messages, schema, effort)
        return openai_compat.complete_json(s, system, messages, schema, effort)
    return _with_failover(settings, call)


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
    def call(s: config.Settings) -> tuple[str, int | None]:
        if s.provider == "moa":
            # Tool loops can't be averaged across models — run them on the
            # ensemble's aggregator member (see olympus/moa.py).
            from . import moa
            s = moa.agent_settings()
        # Bind these credentials as the active run settings so inline
        # delegations (spawn_subagent) inherit them instead of the operator's
        # env key.
        token = config.use_active_settings(s)
        try:
            if s.provider == "anthropic":
                return agent.run_agent_counted(system, task, settings=s,
                                               tool_defs=tool_defs,
                                               mcp_servers=mcp_servers,
                                               effort=effort)
            if s.provider == "claude-code":
                return claude_code.run_agent(s, system, task, tool_defs,
                                             effort), None
            # MCP runs server-side on Anthropic only; other providers still get
            # the local plugin tools (which are included in tool_defs).
            return openai_compat.run_agent(s, system, task, tool_defs,
                                           effort), None
        finally:
            config.clear_active_settings(token)
    return _with_failover(settings, call)

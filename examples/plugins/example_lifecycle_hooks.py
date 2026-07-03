"""Example plugin: lifecycle hooks.

Drop this file in your plugins directory (OLYMPUS_PLUGINS_DIR) to see the
hook surface in action. Hooks let a plugin observe the agent loop and
intercept tool calls without forking Olympus:

  session_start / run_start / run_end      observe the conversation
  pre_llm_call / post_llm_call             observe model requests (read-only)
  pre_tool                                 block or rewrite a tool call
  post_tool                                rewrite a tool result

Blocking runs BEFORE the tool executes, so it composes with the approval
spine — a plugin can only make policy stricter, never bypass it. A hook
that raises is logged and skipped; it can't take down the agent.
"""

from olympus.connectors import hook


@hook("run_start")
def log_runs(user: str, message: str) -> None:
    print(f"[hooks-example] run for {user}: {message[:60]!r}")


@hook("pre_tool")
def deny_prod_webhooks(name: str, params: dict):
    """Block any webhook call whose payload mentions prod."""
    if name == "call_webhook" and "prod" in str(params).lower():
        return {"block": "prod webhooks are disabled by the hooks-example plugin"}


@hook("post_tool")
def tag_web_results(name: str, params: dict, result: str):
    """Stamp web-search results so you can see post_tool at work."""
    if name == "web_search":
        return result + "\n[stamped by hooks-example plugin]"

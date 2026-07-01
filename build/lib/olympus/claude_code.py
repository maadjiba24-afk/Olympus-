"""Claude Code provider — run Olympus on a Claude Pro/Max subscription.

Instead of metered API credits, this drives the official `claude` CLI (Claude
Code), which is authenticated by your subscription. Each model call shells out
to `claude -p "<prompt>" --output-format json` and reads the `result` field.

Why it's safe and predictable:
- **One turn only** (`--max-turns 1`). Claude Code is normally an agent that can
  read/write files and run bash. Capping it at a single turn makes it a pure
  *completion* engine — it produces one assistant message and cannot perform any
  tool round-trip, so it never touches your filesystem during Olympus's
  pipeline. The trade-off: specialists on this provider have no live web_search
  (that needs a tool turn) — they reason from the model's own knowledge. For
  web-heavy work, use an API provider (DeepSeek/GLM/etc.) instead.
- Runs from a neutral temp directory so it doesn't pick up a project `CLAUDE.md`
  as hidden context.

Scope: intended for a SINGLE user running their own Olympus on their own
subscription (personal use). It is not for serving a multi-user public instance
— that would breach the subscription terms and hit its rate limits. The `claude`
CLI must be installed and logged in on the machine where Olympus runs.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from typing import Any

from . import config, openai_compat

_BIN = os.environ.get("OLYMPUS_CLAUDE_BIN", "claude")
_TIMEOUT = int(os.environ.get("OLYMPUS_CLAUDE_TIMEOUT", "300"))


class ClaudeCodeError(RuntimeError):
    pass


def available() -> bool:
    """True if the `claude` CLI is on PATH (or pointed at by OLYMPUS_CLAUDE_BIN)."""
    from shutil import which
    return which(_BIN) is not None or os.path.isfile(_BIN)


def _model(settings: config.Settings) -> str:
    """The --model value, or '' to let Claude Code use its subscription default
    (Opus on Max). OLYMPUS_MODEL can pin a cheaper tier, e.g. 'sonnet'."""
    return (settings.model or "").strip()


def _flatten(messages: list[dict[str, Any]]) -> str:
    """Flatten the (role, content) turns into a single prompt. Most calls carry
    one user message; chat history is preserved with light role markers since
    each CLI call is a fresh, stateless session."""
    parts: list[str] = []
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, list):                # SDK-style block content
            content = " ".join(
                str(b.get("text", "")) for b in content if isinstance(b, dict))
        content = str(content).strip()
        if not content:
            continue
        if m.get("role") == "assistant":
            parts.append(f"[Earlier assistant reply]\n{content}")
        else:
            parts.append(content)
    return "\n\n".join(parts) or "(no content)"


def _run(prompt: str, system: str = "", model: str = "") -> str:
    """One stateless, single-turn completion through the Claude Code CLI."""
    cmd = [_BIN, "-p", prompt, "--output-format", "json", "--max-turns", "1"]
    if system:
        cmd += ["--append-system-prompt", system]
    if model:
        cmd += ["--model", model]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=_TIMEOUT, cwd=tempfile.gettempdir())
    except FileNotFoundError:
        raise ClaudeCodeError(
            f"the '{_BIN}' CLI is not installed or not on PATH. Install Claude "
            "Code and log in (claude login), or set OLYMPUS_CLAUDE_BIN.")
    except subprocess.TimeoutExpired:
        raise ClaudeCodeError(f"claude CLI timed out after {_TIMEOUT}s")
    if proc.returncode != 0:
        raise ClaudeCodeError(
            f"claude CLI failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '').strip()[:300]}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise ClaudeCodeError(
            f"claude CLI returned non-JSON output: {proc.stdout[:200]}")
    if data.get("is_error") or data.get("subtype") not in (None, "success"):
        raise ClaudeCodeError(
            f"claude CLI reported an error: "
            f"{data.get('result') or data.get('subtype') or 'unknown'}")
    return (data.get("result") or "").strip()


# --- the backend surface (mirrors openai_compat) -------------------------

def complete_text(settings: config.Settings, system: str,
                  messages: list[dict[str, Any]], effort: str = "high") -> str:
    return _run(_flatten(messages), system=system, model=_model(settings))


def complete_json(settings: config.Settings, system: str,
                  messages: list[dict[str, Any]], schema: dict[str, Any],
                  effort: str = "high") -> dict[str, Any]:
    instruction = (
        "\n\nRespond with ONLY a single JSON object matching this JSON schema "
        "— no prose, no code fences:\n" + json.dumps(schema))
    text = _run(_flatten(messages), system=system + instruction,
                model=_model(settings))
    return openai_compat.extract_json(text)


def run_agent(settings: config.Settings, system: str, task: str,
              tool_defs: list[dict[str, Any]] | None = None,
              effort: str = "high") -> str:
    """A single Claude Code completion. Olympus's client-side tools are NOT wired
    through the CLI (one-turn safety), so the specialist reasons from the model's
    own knowledge — no web_search/recall on this provider. Strong on knowledge
    tasks; for live-web tasks prefer an API provider."""
    return _run(task, system=system, model=_model(settings))

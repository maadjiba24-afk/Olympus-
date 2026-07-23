"""Refusal-safe tool-call repair for weak / OpenAI-compatible / Bedrock models.

Weaker and self-hosted models (Ollama, LM Studio, vLLM, Mistral, Llama, Titan,
DeepSeek, some Gemini/OpenRouter routings) frequently emit *malformed* tool
calls: the arguments double-JSON-encoded, wrapped in a ```json fence, buried in
prose, or — worst — emitted as a JSON object in `content` with an empty
`tool_calls` array, so a naive loop treats an intended action as a final text
answer and stalls.

alibaba/page-agent (see `docs/PAGE_AGENT_TRACKING.md`, watchlist §3.1) salvages
these shapes in its `autoFixer.normalizeResponse`. Olympus absorbs that
capability natively — and inverts its one structural weakness: page-agent's
fixer will happily reconstruct a tool call from *any* content JSON, which can
mask a model's refusal ("I can't do that") or turn a legitimate final answer
into a phantom action. This module is **refusal-safe by construction**:

  * `recover_tool_call` reconstructs a call ONLY when the content parses to a
    JSON object that names a tool the model was *actually offered* (the caller
    passes the known tool names). A refusal or a plain answer names no real
    tool, so it is returned untouched as text — never laundered into an action.
  * It is provider-agnostic and pure (no I/O, no logging side effects), so it is
    exhaustively unit-testable and adds nothing to the import footprint.

The functions never raise: an unrecoverable input degrades to `{}` / `None`, so
the caller's existing error path runs exactly as before (no regression — repair
can only *add* a successful recovery, never remove a working one).
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "extract_json_object",
    "repair_arguments",
    "recover_tool_call",
]


def _find_balanced_object(text: str) -> str | None:
    """Return the first top-level `{...}` substring whose braces balance, honoring
    JSON string literals and escapes. Beats a naive ``find('{')..rfind('}')``,
    which breaks when prose or a second object trails the real one."""
    depth = 0
    start = -1
    in_str = False
    escaped = False
    for i, ch in enumerate(text):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    return text[start : i + 1]
    return None


def extract_json_object(text: Any) -> dict[str, Any] | None:
    """Best-effort recovery of a single JSON object from a model string.

    Tolerates code fences and surrounding prose; uses a brace-balanced scan so a
    trailing sentence after the object does not defeat parsing. Returns the dict,
    or ``None`` when no JSON object can be recovered (never raises). A non-object
    JSON value (bare array/number/string) is treated as "no object" → ``None``.
    """
    if isinstance(text, dict):
        return text
    if not isinstance(text, str):
        return None
    s = text.strip()
    if not s or "{" not in s:
        return None
    # Fast path: the whole string is the object.
    try:
        val = json.loads(s)
        return val if isinstance(val, dict) else None
    except (ValueError, TypeError):
        pass
    candidate = _find_balanced_object(s)
    if candidate is None:
        return None
    try:
        val = json.loads(candidate)
    except (ValueError, TypeError):
        return None
    return val if isinstance(val, dict) else None


def repair_arguments(raw: Any) -> dict[str, Any]:
    """Salvage a tool call's `arguments` into a plain dict.

    Handles: already-a-dict; a valid JSON string; a double-encoded JSON string
    (a JSON string whose decoded value is itself JSON — a very common weak-model
    bug); a ```json-fenced or prose-wrapped object. Anything unrecoverable
    degrades to ``{}`` so the caller's handler errors on missing params exactly
    as it would have without repair — no new failure mode.
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    s = raw.strip()
    if not s:
        return {}
    try:
        val = json.loads(s)
    except (ValueError, TypeError):
        obj = extract_json_object(s)
        return obj if obj is not None else {}
    # Double-encoded: the string decoded to *another* JSON string. Unwrap once.
    if isinstance(val, str):
        obj = extract_json_object(val)
        return obj if obj is not None else {}
    return val if isinstance(val, dict) else {}


# Keys a model might use to name the tool / carry its arguments, across the
# malformed shapes seen in the wild.
_NAME_KEYS = ("name", "tool", "tool_name", "action", "function")
_ARG_KEYS = ("arguments", "args", "parameters", "params", "input", "action_input")


def _as_args(value: Any) -> dict[str, Any] | None:
    """Coerce a candidate arguments value into a dict, or None if it can't be."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return extract_json_object(value)
    return None


def recover_tool_call(
    content: Any, known_names: set[str] | frozenset[str] | None
) -> dict[str, Any] | None:
    """Reconstruct an OpenAI-shaped ``tool_call`` from a model's ``content`` when
    ``tool_calls`` came back empty — but ONLY when the content names a tool the
    model was actually offered (``known_names``). Refusal-safe: prose, refusals,
    and plain final answers name no known tool and return ``None``.

    Returns a dict shaped like an OpenAI tool call
    (``{"id","type","function":{"name","arguments"}}`` with ``arguments`` a JSON
    string), or ``None``. Never raises.
    """
    if not known_names:
        return None
    obj = extract_json_object(content)
    if obj is None:
        return None

    name: str | None = None
    args: dict[str, Any] | None = None

    # Shape A: nested OpenAI-ish {"function": {"name": ..., "arguments": ...}}
    fn = obj.get("function")
    if isinstance(fn, dict) and isinstance(fn.get("name"), str):
        name = fn["name"]
        args = _as_args(fn.get("arguments"))

    # Shape B: flat {"name"/"tool"/...: "<tool>", "arguments"/...: {...}}
    if name is None:
        for nk in _NAME_KEYS:
            cand = obj.get(nk)
            if isinstance(cand, str) and cand in known_names:
                name = cand
                break
        if name is not None:
            for ak in _ARG_KEYS:
                if ak in obj:
                    args = _as_args(obj[ak])
                    break

    # Shape C: page-agent action shape — a single-key object {"<tool>": {...}}
    # where the key is a known tool. Guarded to exactly one key so a wrapper like
    # {"answer": "..."} (which names no tool) can never match.
    if name is None and len(obj) == 1:
        (only_key, only_val), = obj.items()
        if only_key in known_names:
            name = only_key
            args = _as_args(only_val)
            if args is None and only_val is None:
                args = {}

    if name is None or name not in known_names:
        return None
    if args is None:
        args = {}

    return {
        "id": f"repaired_{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }

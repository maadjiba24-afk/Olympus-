"""Refusal-safe tool-call repair for weak / OpenAI-compatible / Bedrock models.

Weaker and self-hosted models (Ollama, LM Studio, vLLM, Mistral, Llama, Titan,
DeepSeek, some Gemini/OpenRouter routings) frequently emit *malformed* tool
calls: the arguments double-JSON-encoded, wrapped in a ```json fence, buried in
prose, or — worst — emitted as a JSON object in `content` with an empty
`tool_calls` array, so a naive loop treats an intended action as a final text
answer and stalls.

alibaba/page-agent salvages
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
import os
import re
import time
from typing import Any

__all__ = [
    "extract_json_object",
    "repair_arguments",
    "recover_tool_call",
    # C8 ladder extension (WAVE1_IMPLEMENTATION_SPEC §C8)
    "validate_arguments",
    "coerce_arguments",
    "close_truncated",
    "salvage_arguments",
    "salvage_enabled",
    "validate_enabled",
    "record_repair",
    "repair_rate",
    "repair_warn_rate",
    "RUNG_STRICT",
    "RUNG_VALIDATED",
    "RUNG_NORMALIZED",
    "RUNG_REPAIRED",
    "RUNG_REGENERATED",
    "RUNG_REFUSED",
]

# Rung numbering is STABLE (I-T4 — telemetry comparability across releases):
#   1 strict parse · 2 schema validation · 3 safe syntactic normalization ·
#   4 constrained repair (coerce / close-truncated / salvage) · 5 regenerate ·
#   6 refuse.
RUNG_STRICT = 1
RUNG_VALIDATED = 2
RUNG_NORMALIZED = 3
RUNG_REPAIRED = 4
RUNG_REGENERATED = 5
RUNG_REFUSED = 6


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
    raw_args: Any = None               # the pre-coercion value, for salvage

    # Shape A: nested OpenAI-ish {"function": {"name": ..., "arguments": ...}}
    fn = obj.get("function")
    if isinstance(fn, dict) and isinstance(fn.get("name"), str):
        name = fn["name"]
        raw_args = fn.get("arguments")
        args = _as_args(raw_args)

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
                    raw_args = obj[ak]
                    args = _as_args(raw_args)
                    break

    # Shape C: page-agent action shape — a single-key object {"<tool>": {...}}
    # where the key is a known tool. Guarded to exactly one key so a wrapper like
    # {"answer": "..."} (which names no tool) can never match.
    if name is None and len(obj) == 1:
        (only_key, only_val), = obj.items()
        if only_key in known_names:
            name = only_key
            raw_args = only_val
            args = _as_args(only_val)
            if args is None and only_val is None:
                args = {}

    if name is None or name not in known_names:
        return None

    if args is None:
        # Preserve an unrecoverable STRING payload (JSON-encoded, so the
        # `arguments` field stays a JSON string as the OpenAI shape requires).
        # Downstream `repair_arguments` still degrades it to {} exactly as
        # before; the only consumer of the preserved text is the default-off
        # salvage tier (OLYMPUS_TOOL_SALVAGE), which maps a lone payload onto a
        # schema's single required parameter.
        if isinstance(raw_args, str) and raw_args.strip():
            return {
                "id": f"repaired_{name}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(raw_args)},
            }
        args = {}

    return {
        "id": f"repaired_{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


# =============================================================================
# C8 — tool-call recovery extension (WAVE1_IMPLEMENTATION_SPEC §C8).
# Everything below is pure except record_repair/repair_rate (telemetry file).
# =============================================================================

# JSON-schema primitive type checks. `bool` is excluded from integer/number
# because Python's bool subclasses int — a schema-typed integer param must not
# silently accept True/False.
_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


def validate_arguments(args: Any, input_schema: Any) -> tuple[list[str], list[str]]:
    """Validate `args` against a tool's `input_schema` (rung 2).

    Returns ``(errors, warnings)``. `errors` are HARD failures (missing
    required key, primitive type mismatch, non-dict args) — a call with any
    error must never execute (I-T1). `warnings` are advisory only: unknown
    keys are allowed-but-flagged, so they never block execution. Empty errors
    ⇒ valid. Pure; never raises.
    """
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(args, dict):
        return ([f"arguments is not an object (got {type(args).__name__})"], [])
    schema = input_schema if isinstance(input_schema, dict) else {}
    props = schema.get("properties")
    props = props if isinstance(props, dict) else {}
    required = schema.get("required")
    required = required if isinstance(required, (list, tuple)) else []
    for key in required:
        if key not in args:
            errors.append(f"missing required key: {key}")
    for key, value in args.items():
        spec = props.get(key)
        if not isinstance(spec, dict):
            if props:  # only meaningful when the schema declares properties
                warnings.append(f"unknown key: {key}")
            continue
        declared = spec.get("type")
        types = (declared if isinstance(declared, list)
                 else [declared] if isinstance(declared, str) else [])
        checks = [_TYPE_CHECKS[t] for t in types if t in _TYPE_CHECKS]
        if checks and not any(c(value) for c in checks):
            errors.append(
                f"key '{key}' expected {'/'.join(str(t) for t in types)}, "
                f"got {type(value).__name__}")
    return errors, warnings


_INT_RE = re.compile(r"[+-]?\d+")
_NUM_RE = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


def _coerce_value(value: Any, declared: Any) -> Any:
    """Schema-typed coercion of ONE value, applied only on mismatch."""
    types = (declared if isinstance(declared, list)
             else [declared] if isinstance(declared, str) else [])
    for t in types:
        check = _TYPE_CHECKS.get(t)
        if check and check(value):
            return value               # already matches — never touch it
    for t in types:
        if t == "integer" and isinstance(value, str):
            s = value.strip()
            if _INT_RE.fullmatch(s):
                return int(s)
        elif t == "number" and isinstance(value, str):
            s = value.strip()
            if _NUM_RE.fullmatch(s):
                try:
                    return float(s)
                except ValueError:
                    pass
        elif t == "boolean" and isinstance(value, str):
            s = value.strip().lower()
            if s == "true":
                return True
            if s == "false":
                return False
        elif (t == "string" and isinstance(value, (int, float))
              and not isinstance(value, bool)):
            # number→str ONLY when the schema says string. (The inverse —
            # digits in a string-typed param — is protected above: a str value
            # for a string param already matches and is returned untouched.)
            return str(value)
    return value


def coerce_arguments(args: Any, input_schema: Any) -> dict[str, Any]:
    """Constrained repair (rung 4): coerce values to their schema-declared
    primitive types, ONLY where the type mismatches. Never invents keys, never
    drops keys, never touches keys the schema doesn't type. Pure."""
    if not isinstance(args, dict):
        return {}
    schema = input_schema if isinstance(input_schema, dict) else {}
    props = schema.get("properties")
    props = props if isinstance(props, dict) else {}
    out: dict[str, Any] = {}
    for key, value in args.items():
        spec = props.get(key)
        out[key] = (_coerce_value(value, spec.get("type"))
                    if isinstance(spec, dict) else value)
    return out


# Hard cap on appended closers — beyond this the tail is too mangled to call
# "unambiguous" (and a legitimate tool call never nests that deep).
_MAX_CLOSERS = 8


def close_truncated(text: Any) -> str | None:
    """Close an UNAMBIGUOUSLY truncated JSON tail (rung 4).

    Appends the missing closers (`"`, `}`, `]`) when — and only when — the
    text ends mid-object with the open containers fully determined:

      * refuse if EOF lands inside a string in KEY position (a partial key —
        completing it would invent structure);
      * refuse if the last significant character is ':' or ',' (a dangling
        separator — the next token is unknowable);
      * refuse when more than 8 closers would be needed, when the braces are
        malformed rather than truncated, or when the closed candidate still
        fails to parse to a JSON object.

    Returns the input with closers appended, or None. Pure; never raises.
    """
    if not isinstance(text, str):
        return None
    start = text.find("{")
    if start == -1:
        return None
    stack: list[str] = []          # pending closers, in open order
    expect_key: list[bool] = []    # parallel; meaningful for '{' frames only
    in_str = False
    escaped = False
    for ch in text[start:]:
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
            stack.append("}")
            expect_key.append(True)
        elif ch == "[":
            stack.append("]")
            expect_key.append(False)
        elif ch in "}]":
            if not stack or stack[-1] != ch:
                return None        # malformed, not merely truncated
            stack.pop()
            expect_key.pop()
            if not stack:
                return None        # first object closed — nothing to repair
        elif ch == ":":
            if stack and stack[-1] == "}":
                expect_key[-1] = False
        elif ch == ",":
            if stack and stack[-1] == "}":
                expect_key[-1] = True
    if not stack:
        return None
    closers = ""
    if in_str:
        if stack[-1] == "}" and expect_key[-1]:
            return None            # open string is a partial KEY — ambiguous
        closers += '"'
    else:
        tail = text.rstrip()
        if tail and tail[-1] in ":,":
            return None            # dangling separator — ambiguous
    if len(stack) > _MAX_CLOSERS:
        return None
    closers += "".join(reversed(stack))
    try:
        val = json.loads(text[start:] + closers)
    except (ValueError, TypeError):
        return None
    if not isinstance(val, dict):
        return None
    return text + closers


def salvage_enabled() -> bool:
    """OLYMPUS_TOOL_SALVAGE — the Colibri-analog salvage tier. Default OFF
    (A14 fail-safe default); read per call so it is runtime-flippable."""
    return os.environ.get("OLYMPUS_TOOL_SALVAGE", "").strip().lower() in (
        "1", "true", "on", "yes")


def validate_enabled() -> bool:
    """OLYMPUS_TOOL_VALIDATE — the rung-2 execution precondition. Default ON;
    'off' (or 0/false) reverts to the legacy pass-through for one release."""
    return os.environ.get("OLYMPUS_TOOL_VALIDATE", "on").strip().lower() not in (
        "off", "0", "false", "no")


def salvage_arguments(payload: Any, input_schema: Any) -> dict[str, Any] | None:
    """Salvage tier (rung 4, behind OLYMPUS_TOOL_SALVAGE — the caller gates on
    `salvage_enabled()`): when a tool name was recovered but its arguments were
    unrecoverable AND the schema has exactly ONE required parameter (string- or
    un-typed), map the lone string payload onto it. Anything else → None (never
    invents structure for multi-parameter schemas). Pure."""
    if not isinstance(payload, str):
        return None
    s = payload.strip()
    if not s:
        return None
    schema = input_schema if isinstance(input_schema, dict) else {}
    required = schema.get("required")
    if not isinstance(required, (list, tuple)) or len(required) != 1:
        return None
    key = required[0]
    if not isinstance(key, str):
        return None
    props = schema.get("properties")
    props = props if isinstance(props, dict) else {}
    spec = props.get(key)
    declared = spec.get("type") if isinstance(spec, dict) else None
    if declared not in (None, "string"):
        return None
    return {key: s}


# --- telemetry (the ONLY impure surface in this module) ----------------------
# Day-bucketed repair counters: MEMORY_DIR/repair_stats.json, schema
# {"v":1,"days":{"<YYYY-MM-DD>":{"<provider>/<model>/<tool>":{"<rung>:<outcome>":n}}}}.
# Telemetry failure never affects the call (I-T3): record_repair swallows
# everything; a corrupt stats file is ephemeral telemetry, so it is moved aside
# (sanitize-and-continue) and counting restarts fresh.

def _stats_path():
    # MEMORY_DIR read lazily (deferred import + attribute read per call) so
    # test isolation (monkeypatched config.MEMORY_DIR) and runtime relocation
    # both work.
    from . import config
    return config.MEMORY_DIR / "repair_stats.json"


def _load_stats(path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("days"), dict):
            return data
    except FileNotFoundError:
        return {"v": 1, "days": {}}
    except (OSError, ValueError):
        pass
    # Corrupt (or wrong-shaped) telemetry: move aside, restart fresh.
    try:
        aside = path.with_name(f"repair_stats.corrupt.{int(time.time())}.json")
        os.replace(path, aside)
    except OSError:
        pass
    return {"v": 1, "days": {}}


def record_repair(provider: str, model: str, tool: str, rung: int,
                  outcome: str) -> None:
    """Count one ladder outcome. Best-effort, never raises (I-T3)."""
    try:
        from . import proclock
        path = _stats_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        day = time.strftime("%Y-%m-%d")
        key = f"{provider}/{model}/{tool}"
        cell = f"{int(rung)}:{outcome}"
        with proclock.lock("repair-stats", timeout=5.0):
            data = _load_stats(path)
            bucket = data["days"].setdefault(day, {}).setdefault(key, {})
            bucket[cell] = int(bucket.get(cell, 0)) + 1
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, path)
    except Exception as err:                       # noqa: BLE001 — I-T3
        try:
            from . import errors
            errors.capture("toolcall_repair", err, context="record_repair")
        except Exception:
            pass


def repair_rate(days: int = 7) -> dict:
    """Aggregate the last `days` day-buckets. Returns
    {"days","total","rung_ge3","rate","by_rung"} — `rate` is the rung≥3 share
    (0.0 when no calls). Read-only; never raises."""
    by_rung: dict[int, int] = {}
    total = 0
    try:
        data = json.loads(_stats_path().read_text(encoding="utf-8"))
        day_map = data.get("days") if isinstance(data, dict) else {}
        if not isinstance(day_map, dict):
            day_map = {}
        import datetime as _dt
        today = _dt.date.today()
        window = {(today - _dt.timedelta(days=i)).isoformat()
                  for i in range(max(1, int(days)))}
        for day, tools_map in day_map.items():
            if day not in window or not isinstance(tools_map, dict):
                continue
            for cells in tools_map.values():
                if not isinstance(cells, dict):
                    continue
                for cell, n in cells.items():
                    try:
                        rung = int(str(cell).split(":", 1)[0])
                        n = int(n)
                    except (ValueError, TypeError):
                        continue
                    by_rung[rung] = by_rung.get(rung, 0) + n
                    total += n
    except (OSError, ValueError):
        pass
    rung_ge3 = sum(n for r, n in by_rung.items() if r >= 3)
    return {
        "days": int(days),
        "total": total,
        "rung_ge3": rung_ge3,
        "rate": (rung_ge3 / total) if total else 0.0,
        "by_rung": {str(r): n for r, n in sorted(by_rung.items())},
    }


def repair_warn_rate() -> float:
    """OLYMPUS_REPAIR_WARN_RATE — doctor's 7-day rung≥3 share WARN threshold
    (default 0.15). Read per call."""
    try:
        return float(os.environ.get("OLYMPUS_REPAIR_WARN_RATE", "0.15"))
    except ValueError:
        return 0.15

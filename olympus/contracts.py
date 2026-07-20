"""Hard output contracts: a specialist's final output must satisfy its
contract before the orchestrator accepts it. A violation fails CLOSED.

Enforcement is gated by config.contracts_enabled() and is ON by default
(ADR 0005 hardening: enforcement mechanisms never ship dormant — the shipped
contracts encode already-true output invariants, so enabling changes no
happy-path behavior, and a violation degrades to the same typed "treat as
missing" marker a crashed specialist produces). `OLYMPUS_CONTRACTS=off` is the
kill switch. It records each check as a `contract` decision in the existing
Trace (trace.py) — never a separate log.

This module is PURE: no I/O, no config reads, no logging. The orchestrator
decides whether to call it (the config gate) and what to do with the result
(record the decision + fail closed). That separation keeps the testable core
free of environment and I/O, the same discipline security.py keeps.
"""
from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class OutputContract:
    max_chars: int | None = None
    must_be_json: bool = False
    json_schema: dict | None = None
    max_tool_calls: int | None = None

    def is_noop(self) -> bool:
        return (self.max_chars is None and not self.must_be_json
                and self.json_schema is None and self.max_tool_calls is None)


@dataclass(frozen=True)
class ContractResult:
    ok: bool
    violations: tuple[str, ...] = ()      # human-readable, for the trace + report


def _check_schema(obj, schema: dict) -> list[str]:
    """Minimal, dependency-free structural check: required keys + top-level
    JSON-type of named properties. NOT full JSON Schema — deliberately small.
    """
    problems: list[str] = []
    if schema.get("type") == "object" and not isinstance(obj, dict):
        return [f"expected a JSON object, got {type(obj).__name__}"]
    for key in schema.get("required", []):
        if not isinstance(obj, dict) or key not in obj:
            problems.append(f"missing required key: {key!r}")
    _PY = {"string": str, "number": (int, float), "integer": int,
           "boolean": bool, "object": dict, "array": list}
    for key, spec in (schema.get("properties") or {}).items():
        if isinstance(obj, dict) and key in obj and "type" in spec:
            want = _PY.get(spec["type"])
            if want and not isinstance(obj[key], want):
                problems.append(
                    f"key {key!r} should be {spec['type']}, "
                    f"got {type(obj[key]).__name__}")
    return problems


def check(output: str, contract: OutputContract | None,
          *, tool_calls: int | None = None) -> ContractResult:
    """Evaluate `output` against `contract`. Pure; no side effects."""
    if contract is None or contract.is_noop():
        return ContractResult(ok=True)

    violations: list[str] = []

    if contract.max_chars is not None and len(output) > contract.max_chars:
        violations.append(
            f"output is {len(output)} chars, limit is {contract.max_chars}")

    if contract.must_be_json or contract.json_schema is not None:
        try:
            parsed = json.loads(output)
        except (json.JSONDecodeError, TypeError):
            violations.append("output is not valid JSON")
        else:
            if contract.json_schema is not None:
                violations.extend(_check_schema(parsed, contract.json_schema))

    if (contract.max_tool_calls is not None and tool_calls is not None
            and tool_calls > contract.max_tool_calls):
        violations.append(
            f"made {tool_calls} tool calls, limit is {contract.max_tool_calls}")

    return ContractResult(ok=not violations, violations=tuple(violations))

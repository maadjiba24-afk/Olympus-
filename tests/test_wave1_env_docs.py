"""Wave-1 config-drift guard (acceptance gate A13).

Every user-facing OLYMPUS_* knob introduced by the Colibri-absorption Wave-1
capabilities must be documented in .env.example, so operators can discover it
and so the knob set cannot silently drift from the spec. This mirrors the
capabilities drift gate's intent for the env surface.
"""
from __future__ import annotations

from pathlib import Path

WAVE1_KNOBS = [
    # C1 sealed session journal
    "OLYMPUS_SESSION_JOURNAL",
    "OLYMPUS_SESSION_FSYNC",
    "OLYMPUS_SESSION_JOURNAL_MAX_MB",
    # C4 calibrated context budgeting
    "OLYMPUS_CTX_BUDGET",
    "OLYMPUS_CTX_OUTPUT_RESERVE_FRACTION",
    "OLYMPUS_CTX_REPAIR_RESERVE",
    # C5 prompt-cache telemetry
    "OLYMPUS_CACHE_READ_MULT",
    "OLYMPUS_CACHE_WRITE_MULT",
    # C6 provider-drift tripwire
    "OLYMPUS_DRIFT_GATE_EVERY",
    "OLYMPUS_DRIFT_BUDGET_USD",
    # C8 tool-call recovery ladder
    "OLYMPUS_TOOL_VALIDATE",
    "OLYMPUS_TOOL_SALVAGE",
    "OLYMPUS_REPAIR_WARN_RATE",
]


def _env_example() -> str:
    return (Path(__file__).resolve().parent.parent / ".env.example").read_text()


def test_wave1_knobs_documented():
    text = _env_example()
    missing = [k for k in WAVE1_KNOBS if k not in text]
    assert not missing, f"undocumented Wave-1 knobs in .env.example: {missing}"

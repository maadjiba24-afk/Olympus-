"""Absorption config-drift guard (acceptance gates A13 / W2-A14).

Every user-facing OLYMPUS_* knob introduced by the Colibri-absorption waves must
be documented in .env.example, so operators can discover it and so the knob set
cannot silently drift from the spec. This mirrors the capabilities drift gate's
intent for the env surface.

Two layers, deliberately:

* the STATIC lists below pin the spec-enumerated knobs of each wave; and
* `test_absorption_modules_have_no_undocumented_knobs` DERIVES the knob set by
  scanning the absorption modules themselves, so a knob added to one of those
  modules in future is caught without anyone remembering to extend a list. The
  static list alone was the very silent-drift pattern this guard exists to
  prevent (it missed OLYMPUS_ADMISSION until a Wave-1 audit test caught it).
"""
from __future__ import annotations

import re
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


WAVE2_KNOBS = [
    # C1 measured model qualification
    "OLYMPUS_MODELGRADE",
    "OLYMPUS_GRADE_MIN_N",
    "OLYMPUS_GRADE_FLOOR",
    "OLYMPUS_GRADE_TTL_DAYS",
    "OLYMPUS_GRADE_HALFLIFE_DAYS",
    "OLYMPUS_POLICY_VERSION",
    # C2 context & skill heat
    "OLYMPUS_CTXHEAT",
    "OLYMPUS_PIN_BUDGET_TOKENS",
    # C3 routing substitution
    "OLYMPUS_ROUTESUB",
    "OLYMPUS_ROUTESUB_VERIFIER_FLOOR",
    # C5 ingestion gate
    "OLYMPUS_INGESTGATE",
    # C6 watchdog
    "OLYMPUS_WATCHDOG",
    "OLYMPUS_WATCHDOG_STALL_SECS",
    # C7 admission
    "OLYMPUS_ADMISSION",
    # C8 degenerate-stream defense
    "OLYMPUS_STREAMGUARD",
]

# The absorption-owned modules whose knobs must all be documented. Derived
# scanning (below) covers knobs nobody remembered to add to the lists above.
ABSORPTION_MODULES = [
    # Wave 1
    "sessionlog.py", "ctxbudget.py", "modelgate.py", "coupling.py",
    # Wave 2
    "modelgrade.py", "ctxheat.py", "routesub.py", "experiments.py",
    "ingestgate.py", "watchdog.py", "streamguard.py",
]

_KNOB_RE = re.compile(r'OLYMPUS_[A-Z0-9_]+')
# Prefix-style lookups (e.g. OLYMPUS_GRADE_FLOOR_<TASK_CLASS>) are documented by
# their base knob; a bare trailing underscore is the scan artefact of an f-string.
_DERIVED_EXEMPT = {"OLYMPUS_GRADE_FLOOR_"}


def _repo() -> Path:
    return Path(__file__).resolve().parent.parent


def _env_example() -> str:
    return (_repo() / ".env.example").read_text()


def test_wave1_knobs_documented():
    text = _env_example()
    missing = [k for k in WAVE1_KNOBS if k not in text]
    assert not missing, f"undocumented Wave-1 knobs in .env.example: {missing}"


def test_wave2_knobs_documented():
    text = _env_example()
    missing = [k for k in WAVE2_KNOBS if k not in text]
    assert not missing, f"undocumented Wave-2 knobs in .env.example: {missing}"


def test_absorption_modules_have_no_undocumented_knobs():
    """Derived guard: scan the absorption modules for OLYMPUS_* reads and require
    each to appear in .env.example. This catches knobs added later that nobody
    added to the static lists — the drift the static lists alone cannot see."""
    text = _env_example()
    olympus = _repo() / "olympus"
    undocumented: dict[str, list[str]] = {}
    for name in ABSORPTION_MODULES:
        path = olympus / name
        if not path.exists():          # module not yet landed — skip, not fail
            continue
        for knob in sorted(set(_KNOB_RE.findall(path.read_text()))):
            if knob in _DERIVED_EXEMPT or knob in text:
                continue
            undocumented.setdefault(name, []).append(knob)
    assert not undocumented, (
        "absorption modules read OLYMPUS_* knobs absent from .env.example: "
        f"{undocumented}")

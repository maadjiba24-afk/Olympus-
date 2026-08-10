#!/usr/bin/env python3
"""Absolute `sessionlog.sync` latency — telemetry, not a PR gate.

WHY THIS FILE EXISTS
--------------------
`tests/test_val_performance.py` asserted absolute wall-clock bounds on
`sessionlog.sync` inside the REQUIRED pytest suite, which runs on uncontrolled
GitHub-hosted runners. On 2026-08-09 run 31377790256 that gate failed the
Windows Python 3.12 leg with p99 284.429 ms against a 250 ms bound, on a commit
whose failing test function and benchmark are byte-identical to the base commit
(SHA-256 `86dcc0bdce5e69df` and `0c348439ff53962b` at both), whose diff touched
no file under `olympus/`, and whose local Windows full suite passed. Linux
3.10-3.13 passed the same commit.

The measured series DECAYED — first decile 74.406 ms, last decile 9.273 ms,
growth ratio 0.1246, slope -1.206 ms/turn — while the cached path is supposed
to be roughly flat in depth. That is consistent with uncontrolled runner
variance or an early-run transient. The precise external cause is NOT proven,
and this file does not claim one.

WHAT MOVED, AND WHAT DID NOT
----------------------------
The bounds are preserved EXACTLY: turns=60, cache=True, p50 < 60 ms,
p99 < 250 ms. Nothing was raised, scaled, or deleted. What changed is only what
a breach blocks. This runs on a schedule and on demand, goes red honestly, and
does not hold an unrelated pull request shut.

`sessionlog.sync` IS the production per-turn path (orchestrator ->
memory.save_conversation -> sync), so relocating a timing bound is only
defensible because the deterministic integrity contract that replaced it is
strictly stronger: routing, one record per extending turn, dense sequences, a
verified hash chain, replayed-history equality, exact scan/fsync/byte
accounting, and fail-closed handling of partial work. See
tests/test_val_performance.py.

PUBLICATION
-----------
The artifact carries numbers and fixed identifiers only. On failure it carries
a CLOSED STAGE NAME and an error COUNT — never an exception type, never a
message, never a repr, never a path, conversation id, history, journal content,
environment value, credential, or rejected value.

Usage:  python scripts/sessionlog_sync_telemetry.py [--out PATH]
Exit:   0 only if the preserved contract passed; non-zero otherwise.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

SCHEMA_VERSION = 1

# PRESERVED VERBATIM from tests/test_val_performance.py as it stood at
# f9b4b13. `turns` is part of the contract: per-turn cost is a function of
# session depth, so a different turn count is a different measurement and the
# thresholds would no longer mean what they meant.
CONTRACT = {
    "benchmark": "sessionlog.sync",
    "turns": 60,
    "cache": True,
    "p50_max_ms": 60.0,
    "p99_max_ms": 250.0,
}

# Closed vocabulary of stage names. A failure publishes one of these and a
# count — never anything derived from the failure itself. `type(name, (), {})`
# makes `__name__` caller-controlled, and an exception message can carry a path
# or a credential, so neither is ever published.
STAGES = ("setup", "benchmark", "evaluation", "publication", "cleanup",
          "serialization")

# Closed platform identifiers, built from integers and a fixed map rather than
# from `platform.system()/release()/machine()` free-form strings.
_PLATFORM_IDS = {"linux": "linux", "win32": "windows", "darwin": "darwin"}


def _platform_id() -> str:
    return _PLATFORM_IDS.get(sys.platform, "other")


def _python_id() -> str:
    return f"{int(sys.version_info[0])}.{int(sys.version_info[1])}"

# Duration-like fields: strictly positive. A latency at or below zero is not a
# fast measurement, it is an impossible one, and `-1 < 250` would otherwise
# satisfy the tail bound.
_POSITIVE_MEASUREMENTS = ("p50", "p90", "p99", "max", "mean",
                          "first_decile_mean", "last_decile_mean",
                          "growth_ratio")
# Finite but free to be negative — a decaying series has a negative slope, and
# that is exactly the shape worth reporting rather than rejecting.
_SIGNED_MEASUREMENTS = ("slope_ms_per_turn",)
# Projections are clamped at the benchmark (`head + max(0.0, slope) * N`), so
# they can never be below the first-decile mean.
_PROJECTIONS = ("projected_ms_at_1k", "projected_ms_at_10k")
_COUNT_FIELDS = ("n", "turns", "records_verified", "journal_bytes")
_FLAG_FIELDS = ("cache", "seqs_dense", "chain_verified",
                "replayed_history_matches")

# Derived-identity tolerance. Strict: these are recomputations of the
# benchmark's own arithmetic, not measurements, so only float noise is allowed.
_REL_TOL = 1e-9


def _finite(value) -> bool:
    """Exactly a built-in int or float, and finite. NEVER raises.

    `type(...) is`, not `isinstance`. A numeric SUBCLASS is not a safe number:
    it can override `__format__`, `__str__`, `__repr__`, `__eq__` and every
    comparison, so it can both defeat a bound check and smuggle arbitrary text
    into any message that formats it. `bool` is excluded automatically, since
    `type(True) is bool`.

    Python ints are arbitrary precision, so `math.isfinite(10**400)` raises
    OverflowError rather than returning False — a validator that crashes is not
    fail-closed, it is an unhandled traceback carrying a value no sanitizer
    inspected. The int branch converts inside a guard and returns False on any
    conversion failure.
    """
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is int:
        try:
            return math.isfinite(float(value))
        except (OverflowError, ValueError, TypeError):
            return False
    return False


def _count(value) -> bool:
    """Exactly a built-in non-negative int within the platform integer range.

    `bool` and subclasses are out by the type check. The upper bound is
    `sys.maxsize`, which is the principled ceiling rather than an invented one:
    every count here is either a Python container length (`n`, `turns`,
    `records_verified`) or a filesystem size (`journal_bytes`), and neither can
    exceed the process's addressable integer range. An arbitrary-precision int
    like `10**400` is a perfectly valid non-negative `int`, so the
    type-and-sign test alone accepted it and only `journal_bytes <= 0` stood
    between it and publication.
    """
    return type(value) is int and 0 <= value <= sys.maxsize


def _positive(value) -> bool:
    """Strictly positive. Runs `float()` only after `_finite` has cleared it."""
    return _finite(value) and float(value) > 0.0


def _journal_ok(value) -> bool:
    """Exactly the built-in string "ok".

    A bare `value == "ok"` asks the VALUE whether it is ok, and a `str`
    subclass overriding `__eq__`/`__ne__` answers yes to anything. The type
    check runs first, so the comparison that follows is between two genuine
    built-in strings.
    """
    return type(value) is str and value == "ok"


def structural_violations(result, contract) -> list[str]:
    """Everything that makes a result unusable, before any bound is applied.

    Reasons name the FIELD and nothing else. A rejected value contributes no
    text — not its repr, not its type name — because these strings reach the
    console, and a caller-controlled value must never ride out on one.
    """
    out: list[str] = []
    if not isinstance(result, dict):
        return ["result is not a mapping"]

    for key in _COUNT_FIELDS:
        if key not in result:
            out.append(f"missing {key}")
        elif not _count(result[key]):
            out.append(f"{key} is not a valid count (value withheld)")
    for key in _POSITIVE_MEASUREMENTS + _PROJECTIONS:
        if key not in result:
            out.append(f"missing {key}")
        elif not _positive(result[key]):
            out.append(f"{key} is not a valid measurement (value withheld)")
    for key in _SIGNED_MEASUREMENTS:
        if key not in result:
            out.append(f"missing {key}")
        elif not _finite(result[key]):
            out.append(f"{key} is not a finite number (value withheld)")
    for key in _FLAG_FIELDS:
        if key not in result:
            out.append(f"missing {key}")
        elif result[key] is not True:
            out.append(f"{key} is not True (value withheld)")
    if "journal_status" not in result:
        out.append("missing journal_status")
    elif not _journal_ok(result["journal_status"]):
        out.append("journal_status is not 'ok' (value withheld)")
    if out:
        return out                  # nothing below is meaningful yet

    # Past this gate every value is exactly a built-in int/float or True. All
    # comparisons below use NORMALISED built-ins, and every reason is a
    # CONSTANT naming fields only — no measurement is interpolated, so no
    # measurement can carry text into the console or the artifact.
    turns = contract["turns"]
    num = {key: float(result[key])
           for key in _POSITIVE_MEASUREMENTS + _SIGNED_MEASUREMENTS
           + _PROJECTIONS}
    cnt = {key: int(result[key]) for key in _COUNT_FIELDS}

    if cnt["turns"] != turns:
        out.append("turns does not equal the contracted turn count")
    if cnt["n"] != turns:
        out.append("n does not equal turns — the sample count must equal the "
                   "number of syncs performed")
    if cnt["records_verified"] != turns:
        out.append("records_verified does not equal turns — one sealed record "
                   "per extending turn")
    if cnt["journal_bytes"] <= 0:
        out.append("journal_bytes is not positive — nothing was journaled")

    # Percentile ordering and containment: arithmetic identities the benchmark's
    # own output must satisfy. An inverted set is a broken measurement, not a
    # fast one, and every value in it would otherwise sit under its bound.
    for lo, hi in (("p50", "p90"), ("p90", "p99"), ("p99", "max")):
        if num[lo] > num[hi]:
            out.append(f"{lo} exceeds {hi} — percentiles cannot decrease")
    if num["mean"] > num["max"]:
        out.append("mean exceeds max")
    for key in ("p50", "p90", "p99", "mean", "first_decile_mean",
                "last_decile_mean"):
        if num[key] > num["max"]:
            out.append(f"{key} exceeds max")

    # Derived-field identities, recomputed from the benchmark's own formulas
    # (perf_validation.bench_sessionlog_sync).
    head, tail = num["first_decile_mean"], num["last_decile_mean"]
    k = max(5, turns // 10)
    span = max(1.0, float(turns) - k)
    if not math.isclose(num["growth_ratio"], tail / head, rel_tol=_REL_TOL):
        out.append("growth_ratio does not equal last/first decile mean")
    if not math.isclose(num["slope_ms_per_turn"], (tail - head) / span,
                        rel_tol=_REL_TOL, abs_tol=1e-12):
        out.append("slope_ms_per_turn does not equal the fitted decile slope")
    growth = max(0.0, num["slope_ms_per_turn"])
    for key, horizon in (("projected_ms_at_1k", 1000),
                         ("projected_ms_at_10k", 10000)):
        if not math.isclose(num[key], head + growth * horizon,
                            rel_tol=_REL_TOL):
            out.append(f"{key} does not equal head + max(0, slope) * {horizon}")
    if num["projected_ms_at_10k"] < num["projected_ms_at_1k"]:
        out.append("the 10k projection is below the 1k projection")
    return out


def threshold_violations(result, contract) -> list[str]:
    """The two preserved bounds. Applied ONLY after structural validation.

    Both are collected — a p99 breach must never be masked by a p50 that
    happened to pass.
    """
    out: list[str] = []
    # Normalised built-ins: structural validation has already proven these are
    # exactly `int`/`float`, so `float()` is a no-op that documents the intent
    # and makes the format specifier provably safe.
    p50, p99 = float(result["p50"]), float(result["p99"])
    if not p50 < contract["p50_max_ms"]:
        out.append(f"p50 {p50:.3f} ms >= {contract['p50_max_ms']} ms")
    if not p99 < contract["p99_max_ms"]:
        out.append(f"p99 {p99:.3f} ms >= {contract['p99_max_ms']} ms")
    return out


def evaluate(result, contract=None) -> list[str]:
    """Every reason this result fails; empty means it passed.

    STAGED, and the stages differ. Structural validation runs first and returns
    early: comparing `nan` against 250 ms produces noise, not information. Once
    the result is usable, both thresholds are collected together.
    """
    contract = contract or CONTRACT
    structural = structural_violations(result, contract)
    if structural:
        return structural
    return threshold_violations(result, contract)


def _published(result, contract=None):
    """The sanitized measurement, or `None` if the result is not a measurement.

    STRUCTURAL VALIDITY IS RE-CHECKED HERE, not assumed from the caller. An
    earlier revision published a field-by-field sanitized view of every result,
    so a structurally invalid row still produced a `measurement` object — some
    fields `None`, others copied verbatim — which reads as a partial
    measurement when in fact nothing about it can be trusted. A result that
    fails structural validation is not a slow measurement or a partial one; it
    is not a measurement, and it publishes as `null`.

    A THRESHOLD BREACH IS DIFFERENT and still publishes in full. `p99 = 284.429`
    against a 250 ms bound is a valid measurement that exceeded a limit, and
    the whole point of the artifact is that an operator can read the number.

    Within a publishable result the allowlist is still only a floor: a key
    allowlist controls which NAMES are copied, not what is copied under them,
    so every value is re-checked against the validator for its kind.
    `conversation_id`, the `journal_status` string, history and journal
    contents are never published at all.
    """
    contract = contract or CONTRACT
    if not isinstance(result, dict):
        return None
    if structural_violations(result, contract):
        return None

    out: dict = {}
    for key in _COUNT_FIELDS:
        value = result.get(key)
        out[key] = int(value) if _count(value) else None
    for key in _POSITIVE_MEASUREMENTS + _PROJECTIONS:
        value = result.get(key)
        out[key] = float(value) if _positive(value) else None
    for key in _SIGNED_MEASUREMENTS:
        value = result.get(key)
        out[key] = float(value) if _finite(value) else None
    for key in _FLAG_FIELDS:
        out[key] = result.get(key) is True
    # The same helper the validator uses, so publication cannot disagree with
    # evaluation about what "ok" means. The status STRING is never published —
    # only this boolean — so a canary in it cannot reach the artifact.
    out["journal_status_ok"] = _journal_ok(result.get("journal_status"))
    return out


def _load_benchmark():
    """Import the benchmark harness.

    A named seam, so an import/setup failure can be exercised deterministically
    without replacing Python's import machinery, and so it happens INSIDE the
    protected path. Importing at module scope would kill the process before any
    artifact existed, and `if: always()` would have nothing to upload.
    """
    import perf_validation as pv
    return pv


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="sessionlog-sync-telemetry.json",
                        help="path for the JSON artifact")
    args = parser.parse_args(argv)

    # Built BEFORE anything that can fail, so every operational failure below
    # still has an artifact to be recorded in.
    payload = {
        "schema_version": SCHEMA_VERSION,
        "benchmark": CONTRACT["benchmark"],
        # Closed identifiers only: built from integers and a fixed map, never
        # from free-form platform strings.
        "python_version": _python_id(),
        "platform": _platform_id(),
        "contract": {k: v for k, v in CONTRACT.items()},
        "measurement": None,
        "failed_stages": [],
        "verdict": "unknown",
        "failure_reasons": [],
    }
    reasons: list[str] = []
    stages: list[dict] = []

    def _stage_failed(stage: str, count: int = 1) -> None:
        """Record a CLOSED stage name and a count. Nothing derived from the
        failure itself is published — not its type, not its message."""
        assert stage in STAGES, stage
        stages.append({"stage": stage, "error_count": count})
        reasons.append(f"stage '{stage}' failed ({count} error(s)); details "
                       f"withheld from the artifact by design")

    pv = None
    try:
        pv = _load_benchmark()
    except Exception:                                           # noqa: BLE001
        _stage_failed("setup")

    if pv is not None:
        result = None
        try:
            # Measured ONCE. Never retried, never resampled, and no sample is
            # selected or discarded.
            result = pv.bench_sessionlog_sync(turns=CONTRACT["turns"],
                                              cache=CONTRACT["cache"])
        except Exception:                                       # noqa: BLE001
            _stage_failed("benchmark")

        if result is not None:
            found = None
            try:
                found = evaluate(result, CONTRACT)
            except Exception:                                   # noqa: BLE001
                _stage_failed("evaluation")
            if found is not None:
                reasons.extend(found)
                # Publication is protected on its own: `_published` walks
                # caller-supplied values, and a hostile `__eq__`/`__float__`
                # can raise from inside it. That must produce a red artifact
                # with a closed stage name, not an unhandled traceback that
                # would print an exception message no validator ever saw.
                try:
                    payload["measurement"] = _published(result)
                except Exception:                               # noqa: BLE001
                    _stage_failed("publication")

        # Cleanup is protected SEPARATELY: failing to remove a scratch tree
        # must not discard a measurement already taken, nor escape before the
        # artifact is written.
        try:
            pv.cleanup()
        except Exception:                                       # noqa: BLE001
            _stage_failed("cleanup")

    payload["failed_stages"] = stages
    payload["failure_reasons"] = reasons
    payload["verdict"] = "pass" if not reasons else "fail"

    # ONE sanitized string for both the artifact and the console, so the two
    # cannot disagree. `allow_nan=False` is a backstop: the validators should
    # already have replaced every non-finite value, and if one still reaches
    # here the serializer refuses rather than emitting bare `NaN`, which is not
    # valid JSON.
    def _serialize(obj) -> str:
        return json.dumps(obj, indent=2, sort_keys=True, allow_nan=False)

    try:
        blob = _serialize(payload)
    except (TypeError, ValueError):
        # Something unserialisable reached the payload despite the validators.
        # Publish a minimal, provably-serialisable artifact and stay red.
        stages.append({"stage": "serialization", "error_count": 1})
        reasons.append("stage 'serialization' failed (1 error(s)); the "
                       "measurement is withheld because it could not be "
                       "sanitized")
        payload = {
            "schema_version": SCHEMA_VERSION,
            "benchmark": CONTRACT["benchmark"],
            "contract": {k: v for k, v in CONTRACT.items()},
            "measurement": None,
            "failed_stages": stages,
            "failure_reasons": reasons,
            "verdict": "fail",
            "measurement_withheld": True,
        }
        blob = _serialize(payload)

    # Written BEFORE the exit code is returned, so a red run still publishes the
    # evidence that explains it. Deliberately NOT wrapped: if the artifact
    # cannot be written the process must fail loudly rather than exit green
    # having published nothing.
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(blob + "\n", encoding="utf-8")

    # STDOUT IS THE ARTIFACT, byte for byte, and nothing else. Earlier
    # revisions also printed the artifact PATH and one line per failure reason:
    # `--out` is caller-supplied, so echoing it published whatever the caller
    # put in the pathname, and the reasons are already inside the JSON. Nothing
    # is written to stderr on a handled failure either — a red run is reported
    # through the exit code and the artifact, not through a second channel that
    # no sanitizer covers.
    sys.stdout.write(blob + "\n")
    return 0 if not reasons else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Absolute session-journal append latency — telemetry, not a PR gate.

WHY THIS FILE EXISTS
--------------------
`tests/test_val_performance.py` used to assert absolute wall-clock bounds on
`sessionlog.append_turn` inside the REQUIRED pytest suite, which runs on
uncontrolled GitHub-hosted runners. That gate cannot distinguish an Olympus
regression from host load, and on 2026-08-08 it blocked a merge: p50 88.11 ms
against a 60 ms bound, with a first-decile mean of 164.01 ms and a last-decile
mean of 33.90 ms — a series that decayed rather than rising with journal depth.
The failing test function and the benchmark it calls are byte-identical to a
commit whose push job passed. The failure is consistent with uncontrolled
runner variance and was not attributable to the Step 1D diff, but the precise
external cause was not proven.

Two further facts settled where the bound belongs:

  * `append_turn` has NO production caller. The live per-turn path is
    `sessionlog.sync` (orchestrator -> memory.save_conversation -> sync). The
    repository already says so in `perf_validation.DRAFT_SLOS`.
  * There is no self-hosted or otherwise qualified runner in this repository.

So the absolute contract is preserved EXACTLY and moved here, where it runs on
a schedule and on demand and can go red honestly without holding an unrelated
PR shut. What remains required at PR time is deterministic: integrity and
work-accounting, which are host-independent.

WHAT THIS IS NOT
----------------
This is not a relaxation. The limits below are the same numbers the required
test used, unchanged. It is not a warning either: a breach exits non-zero and
the workflow goes red. It simply stops a shared runner's load from blocking
work that has nothing to do with it.

Usage:  python scripts/sessionlog_latency_telemetry.py [--out PATH]
Exit:   0 only if every preserved contract passed; non-zero otherwise.
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

SCHEMA_VERSION = 1

# PRESERVED VERBATIM from tests/test_val_performance.py as it stood at
# 8d3c104. Not raised, not scaled, not softened. `n` is part of the contract:
# per-append cost grows with journal depth, so a different sample count is a
# different measurement and the thresholds would no longer mean what they meant.
CONTRACTS = (
    {"benchmark": "sessionlog.append_turn",
     "fsync": "auto",   "n": 120, "p50_max_ms": 60.0,  "p99_max_ms": 200.0},
    {"benchmark": "sessionlog.append_turn",
     "fsync": "always", "n": 120, "p50_max_ms": 100.0, "p99_max_ms": 400.0},
)


def _finite(value) -> bool:
    """A measurement is usable only if it is a real, finite number.

    NaN and infinity fail CLOSED: `nan < 60.0` is False, but relying on that is
    accidental, and `inf` arithmetic elsewhere in a report would propagate
    silently. An unusable measurement is a failed measurement, never a pass.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(float(value))


def evaluate(result: dict, contract: dict) -> list[str]:
    """Judge ONE benchmark result against ONE preserved contract.

    Returns every reason it failed; empty means it passed.

    Reason collection is STAGED, and the stages behave differently:

      * structural validation runs first and RETURNS EARLY on failure — a
        missing field, a non-finite number or a wrong sample count makes every
        later comparison meaningless, so reporting `p50 < 60` against `nan`
        would be noise, not information;
      * once the result is structurally usable, all threshold and integrity
        reasons are collected together with no short-circuit, so a p99 breach
        can never be hidden by a p50 that happened to pass.

    An earlier docstring claimed every reason was always collected. That was
    false for the structural stage.
    """
    reasons: list[str] = []

    # --- the measurement must be a measurement at all ----------------------
    for key in ("n", "p50", "p99"):
        if key not in result:
            reasons.append(f"missing required field {key!r}")
    if reasons:
        return reasons                      # nothing further is meaningful

    for key in ("n", "p50", "p90", "p99", "max", "mean"):
        if key in result and not _finite(result[key]):
            reasons.append(
                f"{key} is not a finite number ({result[key]!r}) — an "
                f"unusable measurement fails, it never passes")
    if not isinstance(result["n"], int) or isinstance(result["n"], bool):
        reasons.append(f"n must be an int, got {type(result['n']).__name__}")
    elif result["n"] != contract["n"]:
        reasons.append(
            f"sample count {result['n']} != the contracted {contract['n']}; "
            f"per-append cost grows with journal depth, so a different n is a "
            f"different measurement")
    if reasons:
        return reasons

    if not result["p50"] > 0.0:
        reasons.append(f"p50 {result['p50']} is not positive — the benchmark "
                       f"did not measure real work")

    # --- the preserved thresholds ------------------------------------------
    if not result["p50"] < contract["p50_max_ms"]:
        reasons.append(
            f"p50 {result['p50']:.3f} ms >= {contract['p50_max_ms']} ms")
    if not result["p99"] < contract["p99_max_ms"]:
        reasons.append(
            f"p99 {result['p99']:.3f} ms >= {contract['p99_max_ms']} ms")

    # --- the work must actually have happened ------------------------------
    # A fast run that skipped the work is worse than a slow one. These come
    # from the benchmark's own integrity accounting.
    records = result.get("records_verified")
    if records != contract["n"]:
        reasons.append(
            f"journal holds {records!r} verified records, expected "
            f"{contract['n']} — a partial run is not a fast run")
    if result.get("journal_status") != "ok":
        reasons.append(
            f"journal status {result.get('journal_status')!r}, expected 'ok'")

    return reasons


def _sanitized(result: dict) -> dict:
    """Copy only the fields that are safe to publish as a CI artifact.

    An allowlist, not a blocklist: no environment variables, no filesystem
    paths (a temp path can carry a username), no payloads, no credentials.
    Everything here is a number, a fixed mode string, or a benchmark name.
    """
    keys = ("n", "fsync", "p50", "p90", "p99", "max", "mean",
            "first_decile_mean", "last_decile_mean", "growth_ratio",
            "slope_ms_per_record", "projected_ms_at_10k", "projected_ms_at_50k",
            "records_verified", "journal_status", "journal_bytes")
    return {key: result[key] for key in keys if key in result}


def _load_benchmark():
    """Import the benchmark harness.

    A named seam so an import/setup failure can be exercised deterministically
    without replacing Python's import machinery, and so it happens INSIDE the
    protected path rather than at module scope. Importing at the top would kill
    the process before any artifact existed, and GitHub's `if: always()` upload
    would then have nothing to upload — a red run with no evidence.
    """
    import perf_validation as pv
    return pv


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="sessionlog-latency-telemetry.json",
                        help="path for the JSON artifact")
    args = parser.parse_args(argv)

    # The payload exists BEFORE anything that can fail, so every operational
    # failure below still has an artifact to be recorded in.
    payload = {
        "schema_version": SCHEMA_VERSION,
        "benchmark": "sessionlog.append_turn",
        "python_version": platform.python_version(),
        "platform": f"{platform.system()} {platform.release()} "
                    f"({platform.machine()})",
        "measurements": [],
        "verdict": "unknown",
        "failure_reasons": [],
    }
    all_reasons: list[str] = []

    # Every `except` below reports ONLY `type(err).__name__`. An exception
    # message can carry a filesystem path, an environment value or a
    # credential, and this artifact is published to a CI run; the type name
    # tells an operator what broke without publishing what it said.
    pv = None
    try:
        pv = _load_benchmark()
    except Exception as err:                                    # noqa: BLE001
        all_reasons.append(
            f"could not load the benchmark harness ({type(err).__name__}) — "
            f"no measurement was produced, so the contract is unverified")

    if pv is not None:
        try:
            for contract in CONTRACTS:
                # Measured ONCE per contract. Never retried, never resampled,
                # and no sample is selected or discarded.
                result = pv.bench_sessionlog_append(n=contract["n"],
                                                    fsync=contract["fsync"])
                reasons = evaluate(result, contract)
                all_reasons.extend(f"[fsync={contract['fsync']}] {r}"
                                   for r in reasons)
                payload["measurements"].append({
                    "fsync": contract["fsync"],
                    "sample_count": contract["n"],
                    "thresholds": {"p50_max_ms": contract["p50_max_ms"],
                                   "p99_max_ms": contract["p99_max_ms"]},
                    "result": _sanitized(result),
                    "passed": not reasons,
                    "failure_reasons": reasons,
                })
        except Exception as err:                                # noqa: BLE001
            all_reasons.append(
                f"benchmark raised {type(err).__name__} — no measurement was "
                f"produced, so the contract is unverified")

        # Cleanup is protected SEPARATELY: a failure to remove a scratch tree
        # must not discard measurements that were already taken, and must not
        # escape before the artifact is written.
        try:
            pv.cleanup()
        except Exception as err:                                # noqa: BLE001
            all_reasons.append(
                f"benchmark cleanup raised {type(err).__name__} — the run is "
                f"reported red because its environment was not left clean")

    payload["failure_reasons"] = all_reasons
    payload["verdict"] = "pass" if not all_reasons else "fail"

    # Written BEFORE the exit code is returned, so a red run still publishes
    # the evidence that explains it. Deliberately NOT wrapped: if the artifact
    # cannot be written the process must fail loudly rather than exit green
    # having published nothing.
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")

    print(json.dumps(payload, indent=2, sort_keys=True))
    print(f"\nverdict: {payload['verdict']}  artifact: {out}")
    for reason in all_reasons:
        print(f"  FAIL: {reason}")
    return 0 if not all_reasons else 1


if __name__ == "__main__":
    raise SystemExit(main())

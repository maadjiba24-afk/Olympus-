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

PUBLICATION
-----------
The artifact carries numbers and fixed identifiers only. On failure it carries
a CLOSED STAGE NAME, the fsync mode of the contract that failed (a runner
constant), and COUNTS — an error count for a failed stage, a violation count
for a failed evaluation — never an exception type, never a message, never a
repr, never an evaluator-provided string, never a path, conversation id,
journal content, environment value, credential, or rejected value. Published
measurements are re-validated against an exact closed schema and rebuilt from
constant keys before they enter the payload. Stdout is the artifact, byte for
byte, and nothing else.

Usage:  python scripts/sessionlog_latency_telemetry.py [--out PATH]
Exit:   0 only if every preserved contract passed; non-zero otherwise.
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

SCHEMA_VERSION = 2

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

# The immutable expected specification of the two preserved contracts:
# (fsync, n, p50_max_ms, p99_max_ms). Deliberately INDEPENDENT of CONTRACTS —
# a plain tuple of built-in scalars, not derived from the table it validates —
# so a CONTRACTS that is emptied, duplicated, reordered or altered cannot
# vouch for itself. CONTRACTS is checked against this before anything runs,
# and completeness is judged against this, never against CONTRACTS.
EXPECTED_CONTRACT_SPEC = (
    ("auto", 120, 60.0, 200.0),
    ("always", 120, 100.0, 400.0),
)

# Closed fsync vocabulary — the only modes a measurement entry may ever name,
# and the order the runner must execute them in. From the specification,
# never from CONTRACTS.
EXPECTED_MODES = tuple(mode for mode, _n, _p50, _p99 in EXPECTED_CONTRACT_SPEC)
FSYNC_MODES = EXPECTED_MODES

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
# fast measurement, it is an impossible one, and `-1 < 200` would otherwise
# satisfy the tail bound.
_POSITIVE_MEASUREMENTS = ("p50", "p90", "p99", "max", "mean",
                          "first_decile_mean", "last_decile_mean",
                          "growth_ratio")
# Finite but free to be negative — a decaying series has a negative slope, and
# that is exactly the shape worth reporting rather than rejecting.
_SIGNED_MEASUREMENTS = ("slope_ms_per_record",)
# Projections are clamped at the benchmark (`head + max(0.0, slope) * N`), so
# they can never be below the first-decile mean.
_PROJECTIONS = ("projected_ms_at_10k", "projected_ms_at_50k")
_COUNT_FIELDS = ("n", "records_verified", "journal_bytes")

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
    every count here is either a Python container length (`n`,
    `records_verified`) or a filesystem size (`journal_bytes`), and neither can
    exceed the process's addressable integer range. An arbitrary-precision int
    like `10**400` is a perfectly valid non-negative `int`, so the
    type-and-sign test alone would accept it.
    """
    return type(value) is int and 0 <= value <= sys.maxsize


def _positive(value) -> bool:
    """Strictly positive. Runs `float()` only after `_finite` has cleared it."""
    return _finite(value) and float(value) > 0.0


def _exact_str(value, expected: str) -> bool:
    """Exactly the built-in string `expected`.

    A bare `value == expected` asks the VALUE whether it matches, and a `str`
    subclass overriding `__eq__`/`__ne__` answers yes to anything. The type
    check runs first, so the comparison that follows is between two genuine
    built-in strings.
    """
    return type(value) is str and value == expected


def _journal_ok(value) -> bool:
    return _exact_str(value, "ok")


def contract_configuration_violations() -> list[str]:
    """CONTRACTS must equal the immutable specification, exactly.

    The thresholds this runner enforces are PRESERVED numbers; a contract
    table that is empty, duplicated, reordered, altered, or malformed makes
    every verdict meaningless, so the run must refuse to execute rather than
    measure against bounds nobody preserved. Reasons are closed constants —
    a mismatched value contributes no text.
    """
    contracts = CONTRACTS
    if type(contracts) not in (tuple, list):
        return ["the contract table is not a sequence — the run is "
                "unverified"]
    if len(contracts) != len(EXPECTED_CONTRACT_SPEC):
        return ["the contract table does not hold exactly one contract per "
                "specification entry, in order — the run is unverified"]
    out: list[str] = []
    for i, (mode, n, p50_max, p99_max) in enumerate(EXPECTED_CONTRACT_SPEC):
        contract = contracts[i]
        if type(contract) is not dict:
            out.append(f"contract {i} is not a mapping — the run is "
                       f"unverified")
            continue
        keys_ok = all(type(key) is str for key in contract) and \
            set(contract) == {"benchmark", "fsync", "n", "p50_max_ms",
                              "p99_max_ms"}
        matches = (
            keys_ok
            and _exact_str(contract.get("benchmark"),
                           "sessionlog.append_turn")
            and _exact_str(contract.get("fsync"), mode)
            and type(contract.get("n")) is int and contract.get("n") == n
            and type(contract.get("p50_max_ms")) is float
            and contract.get("p50_max_ms") == p50_max
            and type(contract.get("p99_max_ms")) is float
            and contract.get("p99_max_ms") == p99_max
        )
        if not matches:
            out.append(f"contract {i} does not match the preserved "
                       f"specification (values withheld)")
    return out


def structural_violations(result, contract) -> list[str]:
    """Everything that makes a result unusable, before any bound is applied.

    Reasons name the FIELD and nothing else. A rejected value contributes no
    text — not its repr, not its type name — because these strings reach the
    console and the artifact, and a caller-controlled value must never ride
    out on one.
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
    if "fsync" not in result:
        out.append("missing fsync")
    elif not _exact_str(result["fsync"], contract["fsync"]):
        out.append("fsync is not the contracted mode (value withheld)")
    if "journal_status" not in result:
        out.append("missing journal_status")
    elif not _journal_ok(result["journal_status"]):
        out.append("journal_status is not 'ok' (value withheld)")
    if out:
        return out                      # nothing below is meaningful yet

    # Past this gate every value is exactly a built-in int/float or the
    # contracted mode string. All comparisons below use NORMALISED built-ins,
    # and every reason is a CONSTANT naming fields only — no measurement is
    # interpolated, so no measurement can carry text into the console or the
    # artifact.
    n = contract["n"]
    num = {key: float(result[key])
           for key in _POSITIVE_MEASUREMENTS + _SIGNED_MEASUREMENTS
           + _PROJECTIONS}
    cnt = {key: int(result[key]) for key in _COUNT_FIELDS}

    if cnt["n"] != n:
        out.append("n does not equal the contracted sample count — per-append "
                   "cost grows with journal depth, so a different n is a "
                   "different measurement")
    if cnt["records_verified"] != n:
        out.append("records_verified does not equal the contracted sample "
                   "count — a partial run is not a fast run")
    if cnt["journal_bytes"] <= 0:
        out.append("journal_bytes is not positive — nothing was journaled")

    # Percentile ordering and containment: arithmetic identities the
    # benchmark's own output must satisfy. An inverted set is a broken
    # measurement, not a fast one, and every value in it would otherwise sit
    # under its bound.
    for lo, hi in (("p50", "p90"), ("p90", "p99"), ("p99", "max")):
        if num[lo] > num[hi]:
            out.append(f"{lo} exceeds {hi} — percentiles cannot decrease")
    for key in ("p50", "p90", "p99", "mean", "first_decile_mean",
                "last_decile_mean"):
        if num[key] > num["max"]:
            out.append(f"{key} exceeds max")

    # Derived-field identities, recomputed from the benchmark's own formulas
    # (perf_validation.bench_sessionlog_append).
    head, tail = num["first_decile_mean"], num["last_decile_mean"]
    k = max(5, n // 10)
    span = max(1.0, float(n) - k)
    if not math.isclose(num["growth_ratio"], tail / head, rel_tol=_REL_TOL):
        out.append("growth_ratio does not equal last/first decile mean")
    if not math.isclose(num["slope_ms_per_record"], (tail - head) / span,
                        rel_tol=_REL_TOL, abs_tol=1e-12):
        out.append("slope_ms_per_record does not equal the fitted decile "
                   "slope")
    growth = max(0.0, num["slope_ms_per_record"])
    for key, horizon in (("projected_ms_at_10k", 10000),
                         ("projected_ms_at_50k", 50000)):
        if not math.isclose(num[key], head + growth * horizon,
                            rel_tol=_REL_TOL):
            out.append(f"{key} does not equal head + max(0, slope) * "
                       f"{horizon}")
    if num["projected_ms_at_50k"] < num["projected_ms_at_10k"]:
        out.append("the 50k projection is below the 10k projection")
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


def evaluate(result, contract) -> list[str]:
    """Every reason this result fails its contract; empty means it passed.

    STAGED, and the stages differ. Structural validation runs first and
    returns early: comparing `nan` against 200 ms produces noise, not
    information. Once the result is usable, both thresholds are collected
    together, so one breach can never mask the other.
    """
    structural = structural_violations(result, contract)
    if structural:
        return structural
    return threshold_violations(result, contract)


def _checked_evaluation(result, contract):
    """Evaluate, then cross-check the evaluator against the canonical judges.

    The module-global `evaluate` is a seam a fault or a patch can replace,
    and its output is caller-shaped data: a return that is not an exact
    built-in list would crash later processing, its strings could carry
    whatever the fault carried — and its LENGTH alone must not control the
    verdict, because an evaluator returning `[]` for a breaching result would
    suppress the breach. The canonical violation count is therefore
    recomputed here, directly from `structural_violations` and — only when
    the result is structurally valid — `threshold_violations`, and the
    evaluator's count must agree with it exactly.

    Returns the CANONICAL count as an exact built-in int, or `None` on any
    type or count disagreement, which the caller records as ONE closed
    evaluation-stage failure. The evaluator's strings are never returned.
    """
    found = evaluate(result, contract)
    if type(found) is not list:
        return None
    canonical = structural_violations(result, contract)
    if not canonical:
        canonical = threshold_violations(result, contract)
    if len(found) != len(canonical):
        return None
    return len(canonical)


def _published(result, contract):
    """The sanitized measurement, or `None` if the result is not a measurement.

    STRUCTURAL VALIDITY IS RE-CHECKED HERE, not assumed from the caller. A
    field-by-field sanitized view of a structurally invalid row — some fields
    `None`, others copied verbatim — reads as a partial measurement when in
    fact nothing about it can be trusted. A result that fails structural
    validation is not a slow measurement or a partial one; it is not a
    measurement, and it publishes as `null`.

    A THRESHOLD BREACH IS DIFFERENT and still publishes in full. `p50 = 88.11`
    against a 60 ms bound is a valid measurement that exceeded a limit, and
    the whole point of the artifact is that an operator can read the number.

    Within a publishable result the allowlist is still only a floor: a key
    allowlist controls which NAMES are copied, not what is copied under them,
    so every value is re-checked against the validator for its kind. The
    published `fsync` is the CONTRACT's own constant, never the result's
    value, and `conversation_id`, the `journal_status` string and journal
    contents are never published at all.
    """
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
    out["fsync"] = str(contract["fsync"])
    # The same helper the validator uses, so publication cannot disagree with
    # evaluation about what "ok" means. The status STRING is never published —
    # only this boolean — so a canary in it cannot reach the artifact.
    out["journal_status_ok"] = _journal_ok(result.get("journal_status"))
    return out


# The exact closed key set a published measurement may carry.
_PUBLISHED_KEYS = frozenset(
    _COUNT_FIELDS + _POSITIVE_MEASUREMENTS + _SIGNED_MEASUREMENTS
    + _PROJECTIONS + ("fsync", "journal_status_ok"))


def _validated_published(published, result, contract):
    """Check the publisher's output AGAINST THE SOURCE, or return `None`.

    A schema check alone was not enough: a patched or faulty `_published` can
    return a mapping with perfect keys and types whose VALUES are fabricated
    — p50 999.0 on a 60 ms contract — and a schema-only gate would publish it
    on a passing run. Publication is therefore validated in four steps:

      * the SOURCE result is independently re-validated structurally — a
        publisher cannot vouch for a result the validator would reject;
      * the expected publication is normalised directly from the source
        result, INLINE, never through the patchable `_published` seam;
      * the publisher's output must be schema-exact: exactly a built-in
        dict, exactly the closed key set, every key an exact built-in
        string, every value the exact built-in type for its kind;
      * every published value must EQUAL the source-derived value exactly.

    The returned measurement is REBUILT from the source result and constant
    keys, so nothing the publisher created — key objects and values included
    — is ever placed in the artifact. Any disagreement returns `None`.
    """
    # --- the source itself must be a valid measurement ----------------------
    if not isinstance(result, dict):
        return None
    if structural_violations(result, contract):
        return None

    # --- the expected publication, normalised from the source ---------------
    expected: dict = {}
    for key in _COUNT_FIELDS:
        value = result.get(key)
        if not _count(value):
            return None
        expected[key] = int(value)
    for key in _POSITIVE_MEASUREMENTS + _PROJECTIONS:
        value = result.get(key)
        if not _positive(value):
            return None
        expected[key] = float(value)
    for key in _SIGNED_MEASUREMENTS:
        value = result.get(key)
        if not _finite(value):
            return None
        expected[key] = float(value)
    if not _journal_ok(result.get("journal_status")):
        return None
    expected["fsync"] = str(contract["fsync"])
    expected["journal_status_ok"] = True

    # --- the publisher's output must be schema-exact ------------------------
    if type(published) is not dict:
        return None
    for key in published:
        if type(key) is not str:
            return None
    if set(published) != _PUBLISHED_KEYS:
        return None

    # --- ...and must equal the source-derived values exactly ----------------
    for key in _COUNT_FIELDS:
        value = published[key]
        if not _count(value) or int(value) != expected[key]:
            return None
    for key in _POSITIVE_MEASUREMENTS + _PROJECTIONS:
        value = published[key]
        if type(value) is not float or not _positive(value) \
                or float(value) != expected[key]:
            return None
    for key in _SIGNED_MEASUREMENTS:
        value = published[key]
        if type(value) is not float or not _finite(value) \
                or float(value) != expected[key]:
            return None
    if not _exact_str(published["fsync"], contract["fsync"]):
        return None
    if published["journal_status_ok"] is not True:
        return None
    # Rebuilt from the SOURCE, never from the publisher.
    return expected


def measurement_completeness_violations(measurements) -> list[str]:
    """The artifact must hold exactly one entry per preserved mode, in order.

    A run that measured only one contract, measured one twice, or measured
    them out of order is unverified — never a pass. The expected modes come
    from the IMMUTABLE specification, never from CONTRACTS, so a mutated
    contract table cannot vouch for its own run. Entry modes are accepted
    only as exact built-in strings, so a `str` subclass whose `__eq__` answers
    yes to anything cannot impersonate a contract that never ran.
    """
    expected = list(EXPECTED_MODES)
    if not isinstance(measurements, list):
        return ["measurements is not a list — the run is unverified"]
    modes = []
    for entry in measurements:
        mode = entry.get("fsync") if isinstance(entry, dict) else None
        modes.append(mode if type(mode) is str else None)
    if modes != expected:
        return ["the artifact does not hold exactly one measurement entry "
                "per contract, in contract order — a missing or duplicated "
                "contract execution is unverified, never a pass"]
    return []


def _load_benchmark():
    """Import the benchmark harness.

    A named seam so an import/setup failure can be exercised deterministically
    without replacing Python's import machinery, and so it happens INSIDE the
    protected path rather than at module scope. Importing at the top would
    kill the process before any artifact existed, and GitHub's `if: always()`
    upload would then have nothing to upload — a red run with no evidence.
    """
    import perf_validation as pv
    return pv


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="sessionlog-latency-telemetry.json",
                        help="path for the JSON artifact")
    args = parser.parse_args(argv)

    # Built BEFORE anything that can fail, so every operational failure below
    # still has an artifact to be recorded in.
    payload = {
        "schema_version": SCHEMA_VERSION,
        "benchmark": "sessionlog.append_turn",
        # Closed identifiers only: built from integers and a fixed map, never
        # from free-form platform strings.
        "python_version": _python_id(),
        "platform": _platform_id(),
        "measurements": [],
        "failed_stages": [],
        "verdict": "unknown",
        "failure_reasons": [],
    }
    reasons: list[str] = []
    stages: list[dict] = []

    def _stage_failed(stage: str, fsync: str | None = None) -> None:
        """Record a CLOSED stage name, an optional CONTRACT mode, and a count.

        Nothing derived from the failure itself is published — not its type,
        not its message. The fsync mode comes from CONTRACTS, never from the
        failure or the result.
        """
        assert stage in STAGES, stage
        assert fsync is None or fsync in FSYNC_MODES, fsync
        entry = {"stage": stage, "error_count": 1}
        if fsync is not None:
            entry["fsync"] = fsync
        stages.append(entry)
        where = "" if fsync is None else f" for the fsync={fsync} contract"
        reasons.append(f"stage '{stage}' failed{where} (1 error(s)); details "
                       f"withheld from the artifact by design")

    def _guarded(stage, call, fsync=None):
        """Run one stage. Returns `(value, ok)`, recording failure ONCE.

        A stage that RAISES and a stage that silently returns `None` are the
        same kind of failure: neither produced what the next step needs, and
        `if value is not None:` as a success guard would let a `None` return
        record nothing and exit 0 — a false green on setup and benchmark
        alike. `evaluate` returning `[]` is falsy but NOT None, which is
        exactly the distinction this guard draws, so a clean result still
        proceeds.

        Exactly one stage entry is recorded per failure: the `except` branch
        returns immediately, so a raising stage is never also counted as a
        `None` return.
        """
        try:
            value = call()
        except Exception:                                       # noqa: BLE001
            _stage_failed(stage, fsync)
            return None, False
        if value is None:
            _stage_failed(stage, fsync)
            return None, False
        return value, True

    # The contract table is validated against the immutable specification
    # BEFORE anything executes: measuring against an empty, duplicated,
    # reordered or altered table would produce verdicts about bounds nobody
    # preserved, so a mismatch refuses to execute rather than measure.
    config_violations = contract_configuration_violations()
    reasons.extend(config_violations)
    pv, ok = ((None, False) if config_violations
              else _guarded("setup", _load_benchmark))
    if ok:
        for contract in CONTRACTS:
            mode = contract["fsync"]
            entry = {
                "fsync": mode,
                "sample_count": contract["n"],
                "thresholds": {"p50_max_ms": contract["p50_max_ms"],
                               "p99_max_ms": contract["p99_max_ms"]},
                "measurement": None,
                "passed": False,
                "violation_count": None,
            }
            payload["measurements"].append(entry)

            # Measured ONCE per contract. Never retried, never resampled, and
            # no sample is selected or discarded. A failure in one contract
            # does not skip the other: each still runs exactly once and
            # produces its own evidence.
            result, ok = _guarded(
                "benchmark",
                lambda c=contract: pv.bench_sessionlog_append(
                    n=c["n"], fsync=c["fsync"]),
                fsync=mode)
            if not ok:
                continue
            count, ok = _guarded(
                "evaluation",
                lambda c=contract, r=result: _checked_evaluation(r, c),
                fsync=mode)
            if not ok:
                continue
            # `_checked_evaluation` cross-checks the evaluator against the
            # canonical judges and returns the CANONICAL count as an exact
            # int; `None` (any disagreement) was already recorded by the
            # guard. This exact-int check is a backstop against a patched
            # seam — anything else is an evaluation failure, not data. The
            # published reason is a closed constant naming the contract mode
            # and the count, never the evaluator's own text.
            if type(count) is not int or count < 0:
                _stage_failed("evaluation", mode)
                continue
            violation_count = count
            entry["violation_count"] = violation_count
            entry["passed"] = violation_count == 0
            if violation_count:
                reasons.append(
                    f"contract fsync={mode} failed evaluation with "
                    f"{violation_count} violation(s); details withheld from "
                    f"the artifact by design")
            healthy = violation_count == 0
            # Publication is protected on its own: the publisher walks
            # caller-supplied values, and a hostile `__eq__`/`__float__` can
            # raise from inside it. Its OUTPUT is then validated against an
            # exact closed schema AND against the source result itself, and
            # the published measurement is rebuilt from the source — so a
            # faulty publisher can neither crash the run, nor place anything
            # unvalidated in the artifact, nor substitute numbers the
            # benchmark never produced.
            try:
                published = _published(result, contract)
                clean = (None if published is None
                         else _validated_published(published, result,
                                                   contract))
            except Exception:                                   # noqa: BLE001
                entry["passed"] = False
                _stage_failed("publication", mode)
            else:
                if published is None:
                    # `None` from the publisher is CORRECT for a structurally
                    # invalid result and for nothing else. A result the
                    # evaluator found no fault with MUST publish; `None` there
                    # means the publisher and the evaluator disagree about
                    # validity — a defect in this runner, not a property of
                    # the measurement — and it must never read as a pass with
                    # no numbers.
                    if healthy:
                        entry["passed"] = False
                        _stage_failed("publication", mode)
                elif clean is None:
                    # The publisher returned something that is not a
                    # schema-exact measurement, or one that disagrees with
                    # the source result. Nothing of it reaches the artifact.
                    entry["passed"] = False
                    _stage_failed("publication", mode)
                else:
                    entry["measurement"] = clean

        # Cleanup is protected SEPARATELY: failing to remove a scratch tree
        # must not discard measurements already taken, nor escape before the
        # artifact is written. The real `cleanup()` returns None, which is its
        # normal successful result — only a RAISE is a failure here.
        try:
            pv.cleanup()
        except Exception:                                       # noqa: BLE001
            _stage_failed("cleanup")

    # The verdict below is meaningful only if the run really assembled exactly
    # one measurement entry per contract, in contract order — a missing or
    # duplicated execution must fail closed rather than pass by omission.
    reasons.extend(measurement_completeness_violations(payload["measurements"]))

    payload["failed_stages"] = stages
    payload["failure_reasons"] = reasons
    payload["verdict"] = "pass" if not reasons else "fail"

    # ONE sanitized string for both the artifact and the console, so the two
    # cannot disagree. `allow_nan=False` is a backstop: the validators should
    # already have replaced every non-finite value, and if one still reaches
    # here the serializer refuses rather than emitting bare `NaN`, which is
    # not valid JSON.
    def _serialize(obj) -> str:
        return json.dumps(obj, indent=2, sort_keys=True, allow_nan=False)

    try:
        blob = _serialize(payload)
    except Exception:                                           # noqa: BLE001
        # Something unserialisable reached the payload despite the validators.
        # EVERY exception is caught, not just TypeError/ValueError: a hostile
        # object can raise a custom exception type from its own attribute
        # lookups while being serialized, and neither the type nor the
        # message may abort the run or be published. Publish a minimal,
        # provably-serialisable artifact and stay red.
        stages.append({"stage": "serialization", "error_count": 1})
        reasons.append("stage 'serialization' failed (1 error(s)); the "
                       "measurements are withheld because they could not be "
                       "sanitized")
        payload = {
            "schema_version": SCHEMA_VERSION,
            "benchmark": "sessionlog.append_turn",
            # From the immutable specification — never from CONTRACTS, whose
            # contents may be exactly what could not be serialized.
            "contracts": [
                {"fsync": mode, "n": n, "p50_max_ms": p50, "p99_max_ms": p99}
                for mode, n, p50, p99 in EXPECTED_CONTRACT_SPEC],
            "measurements": [],
            "failed_stages": stages,
            "failure_reasons": reasons,
            "verdict": "fail",
            "measurements_withheld": True,
        }
        blob = _serialize(payload)

    # Written BEFORE the exit code is returned, so a red run still publishes
    # the evidence that explains it. Deliberately NOT wrapped: if the artifact
    # cannot be written the process must fail loudly rather than exit green
    # having published nothing.
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(blob + "\n", encoding="utf-8")

    # STDOUT IS THE ARTIFACT, byte for byte, and nothing else. Earlier
    # revisions also printed the artifact PATH and one line per failure
    # reason: `--out` is caller-supplied, so echoing it published whatever the
    # caller put in the pathname, and the reasons are already inside the JSON.
    # Nothing is written to stderr on a handled failure either — a red run is
    # reported through the exit code and the artifact, not through a second
    # channel that no sanitizer covers.
    sys.stdout.write(blob + "\n")
    return 0 if not reasons else 1


if __name__ == "__main__":
    raise SystemExit(main())

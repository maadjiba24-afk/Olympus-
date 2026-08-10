#!/usr/bin/env python3
"""Usage-ledger contention latency — telemetry, not a PR gate.

WHY THIS FILE EXISTS
--------------------
`tests/test_val_performance.py::test_usage_ledger_tail_under_concurrency` used
to assert absolute wall-clock verdicts on uncontrolled GitHub-hosted runners.
On the v0.27.3 release branch it failed the required Linux Python 3.11 leg:
four of five repetitions carried p99 outliers of 865-1188 ms against the
unchanged 500 ms bound.

PROVEN on that run: every deterministic guarantee held. Persisted accounting was
exact, every latency sample was collected, every worker started, the start
barrier tripped correctly, p50 stayed healthy and throughput stayed above
100 calls/s. The release diff itself touched only version metadata and
CHANGELOG.md, and the other Linux versions and Windows passed at the same
commit.

INFERRED, NOT PROVEN: that the outliers came from uncontrolled hosted-runner
contention. The evidence is consistent with it — a scheduler/lock-wait tail that
moved while every count stayed exact, on one matrix leg only, with no code
change that could reach the lock — but the precise external cause was never
established, and this file does not claim it was.

So the wall-clock VERDICTS move here, where they run on a schedule and can go
red honestly without holding an unrelated pull request shut. The MEASUREMENTS
never moved: `perf_validation.bench_usage_contention` still reports every timing
field, and the required suite still runs all five repetitions and still enforces
every deterministic guarantee listed above.

WHAT THIS IS NOT
----------------
Not a relaxation. Every threshold below is the number the required test used,
unchanged, including the 3-of-5 quorum. Not a warning either: a breach exits
non-zero and the workflow goes red.

Usage:  python scripts/usage_contention_telemetry.py [--out PATH]
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
# f9b4b13. Not raised, not scaled, not softened, not reinterpreted.
CONTRACT = {
    "repetitions": 5,            # independent repetitions of the whole sweep
    "quorum": 3,                 # ... of which this many must meet EVERY bound
    "per_thread": 50,            # calls issued by each worker at each level
    "max_start_skew_ms": 100.0,  # workers must actually start together
    "p50_floor_ms": 2.0,         # the `max(2.0, ...)` term of the median bound
    "p50_serialisation_factor": 1.5,   # ... x threads x 1.5
    "p99_max_ms": 500.0,         # tail stays far inside a provider call
    "min_calls_per_s": 100.0,    # the ledger must not become the limiter
    "first_touch_max_ms": 2000.0,      # 1-thread `max`, every repetition
}

# Fields published in the artifact, split by the validator each must pass.
#
# A KEY allowlist is not enough on its own: it controls which names are copied,
# not what is copied under them. A credential-shaped string sitting in `p50`
# would have been published verbatim under an allowlisted key. Publication is
# therefore TYPE-SAFE — a count must satisfy `_count`, a measurement must
# satisfy `_finite`, and anything failing its validator is published as `None`
# rather than copied. Nothing else from a row is ever published, and
# `ledger_read_errors` is reduced to a COUNT because a read error can name a
# path.
_PUBLISHED_COUNT_FIELDS = (
    "threads", "per_thread", "n", "expected_samples",
    "ledger_calls", "expected_calls", "barrier_parties", "workers_started",
)
_PUBLISHED_MEASUREMENT_FIELDS = (
    "p50", "p90", "p99", "max", "mean", "wall_ms", "calls_per_s",
    "start_skew_ms",
)

# A latency, a wall time and a throughput are all strictly positive quantities.
# Finiteness alone was NOT enough: a sweep reporting p50=-1, p90=-2, p99=-3,
# max=-4, mean=-5, wall_ms=-6, start_skew_ms=-7 with calls_per_s=1000 satisfied
# every performance bound — a negative p99 is comfortably "< 500 ms" — and
# evaluated to passed=True on physically impossible data. Domain validity is a
# measurement-INTEGRITY property, so it carries no quorum tolerance.
_POSITIVE_MEASUREMENTS = ("p50", "p90", "p99", "max", "mean", "wall_ms",
                          "calls_per_s")
# Start skew is legitimately 0.0 at one thread (there is one worker, so there is
# nothing to be skewed against), but it can never be negative.
_NON_NEGATIVE_MEASUREMENTS = ("start_skew_ms",)

# Relative tolerance for the throughput identity. Strict: `calls_per_s` is
# computed as n/wall and `wall_ms` as wall*1000, so the round trip costs at most
# a couple of ulps. Anything looser would stop being an accounting check.
_THROUGHPUT_REL_TOL = 1e-6


def _finite(value) -> bool:
    """A measurement is usable only if it is a real, finite number.

    `bool` is rejected explicitly: `True` is an `int` in Python, and a boolean
    slipping into a latency field would compare as 1.0 and silently pass a
    threshold. NaN fails closed too — `nan < 500.0` is False, but relying on
    that accident is not the same as refusing unusable data.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(float(value))


def _count(value) -> bool:
    """A usable non-negative integer count (again, `bool` is not one)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_measurement(key: str, value) -> bool:
    """Is `value` a physically possible reading for the field `key`?

    Domain, not just type: a duration or a rate below zero is not a slow
    measurement, it is an impossible one, and treating it as a number that
    merely happens to satisfy `< 500 ms` is how the evaluator failed open.
    """
    if not _finite(value):
        return False
    if key in _NON_NEGATIVE_MEASUREMENTS:
        return float(value) >= 0.0
    return float(value) > 0.0


def median_bound_ms(single_p50_ms: float, threads: int,
                    contract: dict = CONTRACT) -> float:
    """The contended-median bound: max(2.0, single-thread p50 x threads x 1.5).

    `usage.record` serialises on a cross-process lock by design, so at N threads
    the median rises towards N x the single-thread median. That shape is the
    accepted cost, not a regression, and the bound has to sit above it.
    """
    return max(contract["p50_floor_ms"],
               single_p50_ms * threads * contract["p50_serialisation_factor"])


def row_hard_violations(row, *, threads: int, contract: dict = CONTRACT
                        ) -> list[str]:
    """Deterministic guarantees for ONE level. NO quorum tolerance.

    These are the checks the required pytest suite still owns; they are
    re-verified here so a telemetry artifact can never report a green latency
    verdict for a measurement that did not actually do the work. A dropped
    ledger row is a spend-cap defect regardless of how the machine behaved.
    """
    out: list[str] = []
    if not isinstance(row, dict):
        return [f"{threads}t row is not a mapping"]

    # A REJECTED VALUE CONTRIBUTES NOTHING BUT ITS FIELD NAME. Refusing to
    # publish a bad `p50` while interpolating it into a failure reason leaks it
    # just the same: reasons go into the artifact and onto stdout. The type name
    # is withheld too — `type(name, (), {})` builds a class whose `__name__` is
    # any string the caller likes, so an arbitrary type name is not
    # automatically safe. (Exception TYPES reported elsewhere are different:
    # those classes come from the controlled benchmark code, not from row data.)
    expected = threads * contract["per_thread"]
    for key in ("threads", "per_thread", "n", "expected_samples",
                "ledger_calls", "expected_calls", "barrier_parties",
                "workers_started"):
        if key not in row:
            out.append(f"{threads}t row is missing {key}")
        elif not _count(row[key]):
            out.append(f"{threads}t {key} is not a valid count "
                       f"(value withheld)")
    for key in _POSITIVE_MEASUREMENTS + _NON_NEGATIVE_MEASUREMENTS:
        if key not in row:
            out.append(f"{threads}t row is missing {key}")
        elif not _valid_measurement(key, row[key]):
            out.append(f"{threads}t {key} is not a valid measurement "
                       f"(value withheld)")
    if out:
        return out                      # nothing below is meaningful yet

    # Past this point every value below has been validated as an int or a
    # finite number, so interpolating it cannot carry caller-controlled text.
    if row["threads"] != threads:
        out.append(f"row reports {row['threads']} threads, expected {threads}")
    if row["per_thread"] != contract["per_thread"]:
        out.append(f"{threads}t per_thread is {row['per_thread']}, contract "
                   f"says {contract['per_thread']} — a different call count "
                   f"per worker is a different measurement")
    # --- internal consistency: exact benchmark accounting -------------------
    # Every value below has already been validated as a positive finite number,
    # so interpolating it cannot carry caller-controlled text. These are not new
    # performance thresholds — they are arithmetic identities the benchmark's
    # own output must satisfy. A run reporting p50=1.0, p90=0.8, p99=0.5,
    # max=0.4, mean=0.3 is not a fast run; it is a broken measurement, and it
    # used to evaluate green because every value sat under its bound.
    for lo, hi in (("p50", "p90"), ("p90", "p99"), ("p99", "max")):
        if row[lo] > row[hi]:
            out.append(f"{threads}t {lo} ({row[lo]:.6f} ms) exceeds {hi} "
                       f"({row[hi]:.6f} ms) — percentiles cannot decrease")
    if row["mean"] > row["max"]:
        out.append(f"{threads}t mean ({row['mean']:.6f} ms) exceeds max "
                   f"({row['max']:.6f} ms)")
    if row["max"] > row["wall_ms"]:
        out.append(f"{threads}t max ({row['max']:.6f} ms) exceeds the wall "
                   f"time ({row['wall_ms']:.6f} ms) — a single call cannot "
                   f"outlast the window that contained it")
    if row["start_skew_ms"] > row["wall_ms"]:
        out.append(f"{threads}t start skew ({row['start_skew_ms']:.6f} ms) "
                   f"exceeds the wall time ({row['wall_ms']:.6f} ms)")
    implied = row["n"] / (row["wall_ms"] / 1000.0)
    if not math.isclose(row["calls_per_s"], implied,
                        rel_tol=_THROUGHPUT_REL_TOL, abs_tol=0.0):
        out.append(f"{threads}t throughput ({row['calls_per_s']:.6f} calls/s) "
                   f"does not match n/wall ({implied:.6f} calls/s) — the "
                   f"reported rate is not the rate that was measured")

    errors = row.get("ledger_read_errors")
    if not isinstance(errors, list):
        out.append(f"{threads}t ledger_read_errors is not a list "
                   f"(value withheld)")
    elif errors:
        out.append(f"{threads}t the persisted ledger could not be read "
                   f"({len(errors)} read error(s))")
    if row["expected_calls"] != expected:
        out.append(f"{threads}t benchmark expected {row['expected_calls']} "
                   f"calls, contract says {expected}")
    if row["ledger_calls"] != expected:
        out.append(f"{threads}t persisted ledger recorded "
                   f"{row['ledger_calls']} calls in __all__ but {expected} "
                   f"were issued — accounting is LOSING calls under contention")
    if not row["n"] == row["expected_samples"] == expected:
        out.append(f"{threads}t timed {row['n']} samples "
                   f"(expected_samples {row['expected_samples']}), contract "
                   f"says {expected} — the percentiles describe less work "
                   f"than they claim")
    if row["barrier_parties"] != threads:
        out.append(f"{threads}t start barrier held {row['barrier_parties']} "
                   f"parties")
    if row["workers_started"] != threads:
        out.append(f"{threads}t only {row['workers_started']} workers cleared "
                   f"the start barrier, so less contention was measured than "
                   f"reported")
    return out


def repetition_hard_violations(rows, levels, contract: dict = CONTRACT
                               ) -> list[str]:
    """Structure, accounting and the first-touch bound for ONE repetition.

    The first-touch bound is here rather than in the quorum on purpose: a first
    ledger write into a fresh scratch tree that costs seconds is a defect in any
    single run, not scheduler noise to be voted out.
    """
    out: list[str] = []
    if not isinstance(rows, list) or len(rows) != len(levels):
        return [f"repetition has {len(rows) if isinstance(rows, list) else '?'}"
                f" levels, expected {len(levels)}"]
    observed = [r.get("threads") if isinstance(r, dict) else None
                for r in rows]
    if observed != list(levels):
        # The observed thread values are caller data and are withheld; the
        # contract levels are this module's own integers and are safe to name.
        out.append(f"repetition did not measure the requested levels "
                   f"{list(levels)} (observed levels withheld)")
        return out
    for row, threads in zip(rows, levels):
        out.extend(row_hard_violations(row, threads=threads,
                                       contract=contract))
    if out:
        return out

    first_touch = rows[0]
    if first_touch["threads"] != 1:
        out.append("the first row must be the uncontended 1-thread baseline")
    elif first_touch["max"] >= contract["first_touch_max_ms"]:
        out.append(
            f"the slowest uncontended usage.record took "
            f"{first_touch['max']:.3f} ms, at or above the "
            f"{contract['first_touch_max_ms']:.0f} ms first-touch bound")
    return out


def repetition_performance_failures(rows, contract: dict = CONTRACT
                                    ) -> list[str]:
    """Every quorum-eligible bound ONE repetition misses (empty == it held).

    Judged as a SET: a repetition counts toward the quorum only when all of
    them hold together, so no guarantee can be read off a repetition selected
    for a different statistic. Contention bounds apply to CONTENDED rows only —
    the 1-thread row's tail and throughput are dominated by first-touch cost,
    which has its own hard bound above.
    """
    single = rows[0]
    out: list[str] = []
    for row in rows:
        threads = row["threads"]
        # Contention is only real if every worker was in flight together.
        if row["start_skew_ms"] > contract["max_start_skew_ms"]:
            out.append(
                f"{threads}t worker start skew {row['start_skew_ms']:.3f} ms "
                f"> {contract['max_start_skew_ms']:.0f} ms — the workers did "
                f"not start together, so this repetition did not measure "
                f"contention")
        if threads == 1:
            continue
        bound = median_bound_ms(single["p50"], threads, contract)
        if row["p50"] >= bound:
            out.append(
                f"{threads}t median {row['p50']:.3f} ms >= bound "
                f"{bound:.3f} ms = max({contract['p50_floor_ms']} ms, "
                f"single-thread p50 {single['p50']:.3f} ms x {threads} x "
                f"{contract['p50_serialisation_factor']})")
        if row["p99"] >= contract["p99_max_ms"]:
            out.append(f"{threads}t p99 {row['p99']:.3f} ms >= "
                       f"{contract['p99_max_ms']:.0f} ms")
        if row["calls_per_s"] <= contract["min_calls_per_s"]:
            out.append(f"{threads}t throughput {row['calls_per_s']:.1f} "
                       f"calls/s <= {contract['min_calls_per_s']:.0f} calls/s")
    return out


def evaluate(reps, levels, contract: dict = CONTRACT) -> dict:
    """Judge a whole sweep. Returns a verdict plus every reason behind it.

    Two classes, deliberately not interchangeable:

      * HARD violations — structure, accounting, workers, barrier, samples, and
        the first-touch bound. No quorum, no tolerance, 5/5 or the run is red.
      * PERFORMANCE failures — start skew, contended p50/p99/throughput. These
        carry the preserved 3-of-5 quorum, which is a NOISE TOLERANCE and is
        honest about its cost: a regression firing on fewer than three
        repetitions in five can pass.
    """
    hard: list[str] = []
    per_rep: list[list[str]] = []

    if not isinstance(reps, list) or len(reps) != contract["repetitions"]:
        hard.append(
            f"expected {contract['repetitions']} repetitions, got "
            f"{len(reps) if isinstance(reps, list) else '?'}")
        return {"hard_violations": hard, "performance_failures": [],
                "clean_repetitions": [], "quorum_met": False, "passed": False}

    for index, rows in enumerate(reps, start=1):
        rep_hard = repetition_hard_violations(rows, levels, contract)
        hard.extend(f"repetition {index}: {reason}" for reason in rep_hard)
        # Performance bounds are only meaningful on a structurally sound
        # repetition; an unusable one cannot count toward the quorum either.
        # Formatting a performance reason from an unvalidated row would also be
        # a leak path, so it is never attempted on one.
        if rep_hard:
            per_rep.append(["structurally unusable"])
        else:
            per_rep.append(repetition_performance_failures(rows, contract))

    clean = [i for i, failures in enumerate(per_rep, start=1) if not failures]
    quorum_met = len(clean) >= contract["quorum"]
    return {
        "hard_violations": hard,
        "performance_failures": per_rep,
        "clean_repetitions": clean,
        "quorum_met": quorum_met,
        "passed": not hard and quorum_met,
    }


def _published_rows(rows) -> list[dict]:
    """Type-validated numeric view of one repetition, safe to publish.

    Every value is re-checked against the validator for its kind and replaced
    with `None` if it fails. A key allowlist alone would have published a
    credential-shaped string sitting under `p50`; this cannot, because the only
    things that survive are ints that satisfy `_count` and floats that satisfy
    `_finite`.
    """
    out: list[dict] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            out.append({"unpublishable_row": True})
            continue
        item: dict = {}
        for key in _PUBLISHED_COUNT_FIELDS:
            value = row.get(key)
            item[key] = int(value) if _count(value) else None
        for key in _PUBLISHED_MEASUREMENT_FIELDS:
            value = row.get(key)
            # The hardened validator, not bare finiteness: a negative duration
            # is not a publishable measurement.
            item[key] = float(value) if _valid_measurement(key, value) else None
        errors = row.get("ledger_read_errors")
        # The COUNT, never the messages: a read error can name a path.
        item["ledger_read_error_count"] = (
            len(errors) if isinstance(errors, list) else None)
        out.append(item)
    return out


def _published_levels(levels) -> list:
    """The measured levels, validated as counts before publication."""
    if not isinstance(levels, (list, tuple)):
        return []
    return [int(v) if _count(v) else None for v in levels]


def _load_benchmark():
    """Import the benchmark harness.

    A named seam so an import/setup failure can be exercised deterministically
    without replacing Python's import machinery, and so it happens INSIDE the
    protected path. Importing at module scope would kill the process before any
    artifact existed, and the workflow's `if: always()` upload would then have
    nothing to upload — a red run with no evidence.
    """
    import perf_validation as pv
    return pv


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="usage-contention-telemetry.json",
                        help="path for the JSON artifact")
    args = parser.parse_args(argv)

    # Built BEFORE anything that can fail, so every operational failure below
    # still has an artifact to be recorded in.
    payload = {
        "schema_version": SCHEMA_VERSION,
        "benchmark": "usage.record contention",
        "python_version": platform.python_version(),
        "platform": f"{platform.system()} {platform.release()} "
                    f"({platform.machine()})",
        "contract": dict(CONTRACT),
        "levels": [],
        "repetitions": [],
        "clean_repetitions": [],
        "verdict": "unknown",
        "failure_reasons": [],
    }
    reasons: list[str] = []

    # Every `except` reports ONLY `type(err).__name__`. An exception message can
    # carry a filesystem path, an environment value or a credential, and this
    # artifact is published to a CI run.
    pv = None
    try:
        pv = _load_benchmark()
    except Exception as err:                                    # noqa: BLE001
        reasons.append(
            f"could not load the benchmark harness ({type(err).__name__}) — "
            f"no measurement was produced, so the contract is unverified")

    if pv is not None:
        levels: tuple = ()
        reps: list = []
        try:
            levels = tuple(pv.contention_levels())
            payload["levels"] = _published_levels(levels)
            # Five INDEPENDENT repetitions, measured ONCE each. Never retried,
            # never resampled, and no repetition is selected or discarded.
            for _ in range(CONTRACT["repetitions"]):
                reps.append(pv.bench_usage_contention(
                    levels=levels, per_thread=CONTRACT["per_thread"]))
        except Exception as err:                                # noqa: BLE001
            reasons.append(
                f"benchmark raised {type(err).__name__} — no complete sweep "
                f"was produced, so the contract is unverified")
        else:
            verdict = evaluate(reps, levels)
            payload["repetitions"] = [
                {"index": i, "rows": _published_rows(rows),
                 "performance_failures": verdict["performance_failures"][i - 1]}
                for i, rows in enumerate(reps, start=1)]
            payload["clean_repetitions"] = verdict["clean_repetitions"]
            reasons.extend(verdict["hard_violations"])
            if not verdict["quorum_met"]:
                reasons.append(
                    f"only {len(verdict['clean_repetitions'])}/"
                    f"{CONTRACT['repetitions']} repetitions met every "
                    f"concurrency guarantee (need {CONTRACT['quorum']})")

        # Cleanup is protected SEPARATELY: a failure to remove a scratch tree
        # must not discard measurements already taken, nor escape before the
        # artifact is written.
        try:
            pv.cleanup()
        except Exception as err:                                # noqa: BLE001
            reasons.append(
                f"benchmark cleanup raised {type(err).__name__} — the run is "
                f"reported red because its environment was not left clean")

    payload["failure_reasons"] = reasons
    payload["verdict"] = "pass" if not reasons else "fail"

    # ONE sanitized string, used for both the artifact and the console, so the
    # two can never disagree about what was published. `allow_nan=False` is a
    # defensive backstop: the validators above should already have replaced
    # every non-finite value, and if one still reaches here the serializer
    # refuses rather than emitting bare `NaN`/`Infinity`, which is not valid
    # JSON and which downstream readers parse inconsistently.
    def _serialize(obj) -> str:
        return json.dumps(obj, indent=2, sort_keys=True, allow_nan=False)

    try:
        blob = _serialize(payload)
    except (TypeError, ValueError) as err:
        # Something unserialisable or non-finite reached the payload despite
        # the validators. Publish a minimal, provably-serialisable artifact
        # rather than crashing with nothing written — and stay red.
        reasons.append(
            f"artifact serialisation failed ({type(err).__name__}) — the "
            f"measurements are withheld because they could not be sanitized")
        payload = {
            "schema_version": SCHEMA_VERSION,
            "benchmark": "usage.record contention",
            "contract": dict(CONTRACT),
            "levels": [],
            "repetitions": [],
            "clean_repetitions": [],
            "verdict": "fail",
            "failure_reasons": reasons,
            "measurements_withheld": True,
        }
        blob = _serialize(payload)

    # Written BEFORE the exit code is returned, so a red run still publishes the
    # evidence that explains it. Deliberately NOT wrapped: if the artifact
    # cannot be written the process must fail loudly rather than exit green
    # having published nothing.
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(blob + "\n", encoding="utf-8")

    print(blob)
    print(f"\nverdict: {payload['verdict']}  artifact: {out}")
    for reason in reasons:
        print(f"  FAIL: {reason}")
    return 0 if not reasons else 1


if __name__ == "__main__":
    raise SystemExit(main())

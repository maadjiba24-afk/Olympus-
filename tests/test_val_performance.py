"""Phase 4 Stage D — performance & cost validation (CI regression guard).

These tests re-run the measurements in ``scripts/perf_validation.py`` at reduced
sample sizes and assert **deliberately loose** bounds. The bounds are sized to
catch a ~10x regression (someone puts an fsync in a loop, makes the classifier
ladder quadratic, turns a pure function into an I/O call), NOT to police
machine-to-machine variance. Every bound below is annotated with the value
actually measured on the reference run, so the headroom is visible.

They also assert the HONESTY properties of the report itself:

* every provider-dependent metric is in the UNMEASURABLE register with a reason
  and a way to measure it — it is never given a number;
* every service objective that needs real traffic is labelled a PROPOSAL.

Nothing here needs a provider, a key, or a network.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "scripts"))

import perf_validation as pv  # noqa: E402

from olympus import config  # noqa: E402


def test_perf_validation_imports_with_platform_memory_backend():
    """Import collection must select a real backend on every supported OS."""
    assert pv.memory_backend() in {
        "windows-psapi",
        "unix-getrusage+/proc",
        "unix-getrusage+ps",
    }
    if sys.platform == "win32":
        assert pv.memory_backend() == "windows-psapi"


def test_rss_measurements_are_non_negative_integers():
    for sample in (pv.rss_kb(), pv.current_rss_kb()):
        assert isinstance(sample, int)
        assert sample >= 0


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="requires the Windows PSAPI process-memory backend",
)
def test_windows_memory_backend_is_a_live_process_measurement():
    """A fresh child process must report live PSAPI working-set growth."""
    probe = textwrap.dedent(
        """
        import json
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path.cwd() / "scripts"))
        import perf_validation as pv

        if pv.memory_backend() != "windows-psapi":
            raise AssertionError(f"unexpected backend: {pv.memory_backend()}")

        peak_before, current_before = pv._windows_process_memory_kb()
        blocks = []
        peak_after, current_after = peak_before, current_before

        for _ in range(8):
            block = bytearray(8 * 1024 * 1024)
            for offset in range(0, len(block), 4096):
                block[offset] = 1
            blocks.append(block)
            peak_after, current_after = pv._windows_process_memory_kb()
            if peak_after > peak_before and current_after > current_before:
                break

        print(json.dumps({
            "backend": pv.memory_backend(),
            "peak_before_kb": peak_before,
            "current_before_kb": current_before,
            "peak_after_kb": peak_after,
            "current_after_kb": current_after,
            "allocated_mib": len(blocks) * 8,
            "touch_checksum": sum(block[0] for block in blocks),
        }))
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=_REPO,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, (
        "isolated Windows PSAPI probe failed:\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )

    sample = json.loads(completed.stdout)
    assert sample["backend"] == "windows-psapi"
    assert sample["peak_before_kb"] > 0
    assert sample["current_before_kb"] > 0
    assert sample["peak_before_kb"] >= sample["current_before_kb"]
    assert sample["peak_after_kb"] >= sample["current_after_kb"]
    assert sample["peak_after_kb"] > sample["peak_before_kb"], sample
    assert sample["current_after_kb"] > sample["current_before_kb"], sample
    assert 8 <= sample["allocated_mib"] <= 64
    assert sample["touch_checksum"] == sample["allocated_mib"] // 8


# --- 1. sealed session journal ---------------------------------------------

def test_sessionlog_append_latency_within_loose_bounds():
    """Reference: p50 5.98 ms / p99 13.5 ms over 500 appends (fsync=auto)."""
    r = pv.bench_sessionlog_append(n=120, fsync="auto")
    assert r["n"] == 120
    assert r["p50"] > 0.0
    assert r["p50"] < 60.0, f"append p50 regressed: {r}"
    assert r["p99"] < 200.0, f"append p99 regressed: {r}"


def test_sessionlog_append_fsync_always_within_loose_bounds():
    """Reference: p50 6.36 ms / p99 14.5 ms over 500 appends (fsync=always)."""
    r = pv.bench_sessionlog_append(n=120, fsync="always")
    assert r["p50"] < 100.0, f"fsync=always append p50 regressed: {r}"
    assert r["p99"] < 400.0, f"fsync=always append p99 regressed: {r}"


def test_sessionlog_sync_is_the_live_per_turn_path_and_is_bounded():
    """`memory.save_conversation` calls `sessionlog.sync`, not `append_turn`.

    Reference (post-D1-fix): p50 1.47 ms over 400 turns, flat in depth.
    """
    r = pv.bench_sessionlog_sync(turns=60)
    assert r["turns"] == 60 and r["cache"] is True
    assert r["p50"] < 60.0, f"sync p50 regressed: {r}"
    assert r["p99"] < 250.0, f"sync p99 regressed: {r}"


def test_sync_depth_scaling_is_sharply_reduced_but_not_eliminated():
    """Stage-D DEFECT D1, fixed — stated at its true strength.

    `sync` re-scanned, re-verified and re-replayed the WHOLE journal on every
    turn, so a conversation's per-turn journaling cost grew with its own depth.
    `_cache_get` removes the dominant O(journal bytes) parse-and-sha256 term.

    It does NOT make the path flat, and an earlier version of this test claimed
    it did. An O(history length) term remains — `sync` must still compare the
    caller's history against the journal's replayed prefix to classify the
    delta as an extension rather than a rewrite, and `_cache_put` takes a
    defensive copy so a caller mutating a message in place cannot silently
    de-sync the cache. Both are inherent to the contract, not oversights.

    Measured (medians of syncs taken AT depth, 100 -> 3000 turns):

        depth   cached    uncached
          100    1.68 ms     4.36 ms
          500    1.73 ms    14.56 ms
         1500    2.53 ms    38.31 ms
         3000    6.61 ms    82.20 ms

        depth-scaling coefficient: 1.70 us/turn cached, 26.84 us/turn
        uncached — a 15.8x reduction, not an elimination.

    The previous assertion fitted a slope from the first/last decile of one
    growing run, which conflates the signal with warm-up and page-cache
    effects; at 150 turns the noise swamped it and the test flaked in a full
    suite run. This measures the coefficient directly at two depths.

    METHODOLOGY CORRECTION — why the absolute claim is now made on a paired
    statistic. The benchmark used to measure the arms in separate sequential
    phases. The SLOPE assertions tolerate that, because each arm is compared
    against itself; the absolute ON-versus-OFF ratio does not, because the two
    arms then sit minutes apart in wall-clock and any ambient load landing on
    one phase goes straight into the ratio. A Windows PR runner reported cache
    ON as 29.3105 ms at depth 100 and 25.3359 ms at depth 900 — cheaper at
    greater depth — giving on_slope -4.97 us/turn against off_slope
    +39.75 us/turn. A negative depth slope is inconsistent with the scaling
    shape this code produces under stable measurement conditions, and the
    pattern is evidence consistent with phase-separated ambient-load
    contamination; which runner activity produced it cannot be determined from
    those timings alone, so the reading that the ON phase absorbed load the
    later OFF phase did not is an inference, not an established cause. What the
    run does show without inference: both scaling invariants passed, and only
    the 2x absolute assertion failed, at 1.5547x, on a comparison between two
    phases measured minutes apart.

    The bound was NOT lowered to fit that number. The measurement was fixed:
    both arms now run as independent sessions in one environment, interleaved
    sample by sample with alternating ON-first/OFF-first order, and the
    absolute claim is asserted on the median of the PER-PAIR OFF/ON ratios.
    Pairing makes the two observations in each ratio time-local — microseconds
    apart rather than minutes — which removes the multi-minute
    phase-separation bias; alternating order balances first/second-position
    effects between the arms; and the median tolerates isolated outlying pairs.
    None of that makes the statistic immune to scheduler or load stalls: a
    stall spanning both members of a pair still perturbs that ratio, and enough
    disturbed pairs will still move the median. The systematic error mode was
    removed; the residual random one was reduced, not eliminated.

    Three independent invariants, each able to fail alone:

      * the uncached arm must reproduce D1 (off_slope > 5 us/turn), else the
        A/B is void and proves nothing;
      * cached scaling must stay at least 5x gentler (on_slope < off_slope/5);
      * the paired operator speedup at maximum depth must exceed 2x.

    Bounds are loose because the point is the ORDER of the effect, not the
    constant, against a measured 15.8x slope reduction."""
    lo, hi = 100, 900
    r = pv.bench_sync_depth_scaling(depths=(lo, hi), samples=12)
    off_slope = r["off_us_per_turn_of_depth"]
    on_slope = r["on_us_per_turn_of_depth"]

    # The measurement must be the paired one, and must have produced usable
    # pairs — a silent fallback to zero pairs would make the third assertion
    # vacuous rather than failing.
    assert r["paired"] is True, f"the A/B is no longer paired: {r}"
    assert r["paired_samples_at_max_depth"] > 0, (
        f"no paired observations at depth {hi}: {r}")

    assert off_slope > 5.0, (
        f"the pre-fix arm did not reproduce the D1 depth scaling — the A/B is "
        f"void and this test proves nothing: {r}")
    assert on_slope < off_slope / 5.0, (
        f"per-turn cost is scaling with depth again: {r}")
    # and the absolute win at the deeper end is what an operator actually feels
    paired = r["paired_speedup_at_max_depth"]
    assert paired is not None and paired > 2.0, (
        f"the cached path is less than 2x cheaper at depth {hi} on the paired "
        f"measurement (median of {r['paired_samples_at_max_depth']} per-pair "
        f"OFF/ON ratios = {paired}); unpaired ratio of medians was "
        f"{r['speedup_at_max_depth']}: {r}")


def test_sync_depth_scaling_measures_the_two_arms_paired_and_order_balanced():
    """The A/B must not drift back to sequential, order-biased phases.

    This is the anti-regression for the methodology fix above, and it is
    deliberately STRUCTURAL rather than timing-based: it observes the actual
    `sessionlog.sync` calls the benchmark makes and the cache regime in force
    at each one. No sleeps, no wall-clock thresholds, nothing that can flake on
    a loaded runner — the properties asserted are true or false regardless of
    how fast the machine is.

    What it pins, and what each would catch:

      * the two arms are DIFFERENT conversations — one shared session would
        make the arms contend on one journal and one lock;
      * within the timed window each arm is sampled equally and no arm ever
        runs more than twice in a row — a sequential implementation produces
        one run of `samples` per arm, which fails this immediately;
      * both ON-first and OFF-first orders actually occur, balanced to within
        one — a fixed order hands the second-position warm-cache advantage to
        the same arm every time;
      * the cache bypass is applied to the OFF timed calls ONLY, and the real
        `_cache_get` is in force for every other call, including after the
        benchmark returns.
    """
    from olympus import sessionlog

    real_cache_get = sessionlog._cache_get
    real_sync = sessionlog.sync
    depth, samples = 4, 4
    calls: list[tuple[str, bool]] = []

    def spy(conversation_id, history):
        # Record the cache regime AT THE MOMENT OF THE CALL — that is the thing
        # the benchmark is supposed to be toggling per sample.
        calls.append((conversation_id,
                      sessionlog._cache_get is not real_cache_get))
        return real_sync(conversation_id, history)

    sessionlog.sync = spy
    try:
        r = pv.bench_sync_depth_scaling(depths=(depth, depth + 2),
                                        samples=samples)
    finally:
        sessionlog.sync = real_sync

    assert sessionlog._cache_get is real_cache_get, (
        "the benchmark left the cache seam patched — a later test would run "
        "against a disabled sessionlog cache")
    assert r["paired"] is True, r

    for d in (depth, depth + 2):
        arms = {"on": f"depth{d}-on", "off": f"depth{d}-off"}
        assert arms["on"] != arms["off"]
        mine = [c for c in calls if c[0] in arms.values()]
        # setup is 2 x depth syncs, measurement is 2 x samples; the timed
        # window is therefore exactly the tail.
        assert len(mine) == 2 * d + 2 * samples, (d, len(mine))
        window = mine[-2 * samples:]

        seq = ["on" if cid == arms["on"] else "off" for cid, _ in window]
        assert seq.count("on") == samples, (d, seq)
        assert seq.count("off") == samples, (d, seq)

        # No arm may run more than twice consecutively. Sequential phases give
        # a run of `samples`; a fixed ON-then-OFF order gives runs of 2 but
        # would fail the order-count check below.
        longest, run = 1, 1
        for prev, cur in zip(seq, seq[1:]):
            run = run + 1 if cur == prev else 1
            longest = max(longest, run)
        assert longest <= 2, (
            f"depth {d}: the arms are measured in blocks of {longest}, not "
            f"interleaved — this is the sequential, order-biased A/B the "
            f"paired design replaced: {seq}")

        # Order alternates: the first element of each pair must not always be
        # the same arm.
        firsts = seq[0::2]
        assert firsts.count("on") > 0 and firsts.count("off") > 0, (
            f"depth {d}: only {set(firsts)} ever ran first, so one arm always "
            f"pays first-position cost: {seq}")
        assert abs(firsts.count("on") - firsts.count("off")) <= 1, (
            f"depth {d}: measurement order is unbalanced: {seq}")

        # The cache bypass is per-sample and per-arm: every OFF timed call ran
        # with `_cache_get` replaced, every ON timed call did not, and no setup
        # call did either.
        for cid, bypassed in window:
            assert bypassed == (cid == arms["off"]), (d, cid, bypassed)
        assert not any(bypassed for _cid, bypassed in mine[:-2 * samples]), (
            f"depth {d}: setup syncs ran with the cache bypassed; setup is "
            f"untimed and must not be part of the A/B")

    # The reported metadata must agree with what actually happened.
    orders = r["order_counts_at_max_depth"]
    assert orders["on_first"] + orders["off_first"] == samples, orders
    assert orders["on_first"] > 0 and orders["off_first"] > 0, orders
    assert abs(orders["on_first"] - orders["off_first"]) <= 1, orders
    assert r["paired_samples_at_max_depth"] == samples, r
    deep = r["per_depth"][depth + 2]
    assert deep["on_samples"] == deep["off_samples"] == samples, deep


def test_journal_recovery_scales_linearly_and_stays_bounded():
    """Reference: 17 us/record; 90.8 ms at 5000 records."""
    rows = pv.bench_journal_recovery(sizes=(100, 500), repeats=3)
    small, large = rows[0], rows[1]
    assert small["records"] == 100 and large["records"] == 500
    # a full verified scan is inherently linear — a deeper journal costs more
    assert large["p50_ms"] > small["p50_ms"]
    for row in rows:
        assert row["us_per_record"] < 400.0, f"recovery regressed: {row}"
    assert large["p50_ms"] < 2000.0, f"recovery at 500 records too slow: {large}"


# --- 2. observability non-interference cost ---------------------------------

def test_observability_overhead_absolute_cost_bounded():
    """Reference: +2.10 ms/run absolute on a fake pipeline (+100.9% of a
    denominator containing ~0 ms of provider time).

    The assertion is on the ABSOLUTE cost, which is the portable number: the
    percentage is meaningless when the baseline run has no inference latency.
    """
    r = pv.bench_observability_overhead(iters=6)
    assert r["on_p50_ms"] > 0.0 and r["off_p50_ms"] > 0.0
    assert r["abs_overhead_ms"] < 25.0, f"observability cost regressed: {r}"
    # every named component must be accounted for
    assert set(r["components_ms"]) == {"usage", "ctx", "otel", "metrics",
                                       "guard"}


# --- 3. context budgeting ----------------------------------------------------

def test_ctxbudget_call_costs_bounded():
    """Reference: estimate_tokens 13.3 us/call, plan 15.0 us/call."""
    r = pv.bench_ctxbudget(calls=300)
    assert r["estimate_tokens_us"] < 300.0, f"estimate_tokens regressed: {r}"
    assert r["plan_us"] < 300.0, f"plan regressed: {r}"
    # the live seam: flag ON delegates to the calibrated estimator, so it is
    # expected to cost more than legacy chars//4 — but not 10x more.
    assert r["seam_on_us"] < 300.0, f"orchestrator ctx seam regressed: {r}"


# --- 4. degenerate-stream defence -------------------------------------------

def test_streamguard_per_delta_cost_bounded_and_off_is_free():
    """Reference: 21.9 us/delta ON, 0.03 us/delta OFF (NullMonitor)."""
    r = pv.bench_streamguard(deltas=400, repeats=3)
    assert r["off_per_delta_us"] < r["on_per_delta_us"], \
        f"the NullMonitor must be cheaper than the real monitor: {r}"
    assert r["off_per_delta_us"] < 5.0, f"flag-off streamguard regressed: {r}"
    assert r["on_per_delta_us"] < 250.0, f"streamguard regressed: {r}"


# --- 5. progress watchdog ----------------------------------------------------

def test_watchdog_call_costs_bounded_and_off_short_circuits():
    """Reference: beat 2.1 us; check 1.9 us (off) vs 27.6 us (observe)."""
    r = pv.bench_watchdog_calls(calls=800)
    assert r["off_check_us"] < r["observe_check_us"], \
        f"mode=off must short-circuit classification: {r}"
    assert r["off_beat_us"] < 60.0, f"lease.beat regressed: {r}"
    assert r["observe_beat_us"] < 60.0, f"lease.beat regressed: {r}"
    assert r["observe_check_us"] < 300.0, f"lease.check regressed: {r}"


# --- 6. admission ------------------------------------------------------------

def test_admission_overhead_bounded_uncontended_and_contended():
    """Reference: slot acquire+release 0.060 ms off / 0.128 ms on
    (uncontended); 4.7 ms off / 8.5 ms on with 8 threads contending."""
    r = pv.bench_admission(calls=40, threads=8, per_thread=5)
    assert r["off_uncontended_p50_ms"] < 5.0, f"slot primitive regressed: {r}"
    assert r["on_uncontended_p50_ms"] < 10.0, f"admission policy regressed: {r}"
    assert r["on_contended_p50_ms"] < 200.0, f"contended admission regressed: {r}"


def test_concurrency_limit_matches_configured_capacity():
    """Reference: 6 concurrent slots with admission OFF (== MAX_CONCURRENT_CALLS,
    and there is no refusal path — it blocks); 5 with admission ON, the sixth
    refused with `no_capacity` because 1 slot is reserved for `critical`."""
    r = pv.bench_concurrency_limit()
    assert r["admission_off_max_concurrent"] == config.MAX_CONCURRENT_CALLS
    assert r["admission_off_behaviour"].startswith("blocks")
    assert 1 <= r["admission_on_max_concurrent"] <= config.MAX_CONCURRENT_CALLS
    assert r["admission_on_max_concurrent"] <= r["admission_off_max_concurrent"]
    assert r["admission_on_refusal_reason"] not in ("", "none-within-probe"), \
        f"admission must REFUSE (never silently downgrade) at capacity: {r}"


# --- 7. replay ---------------------------------------------------------------

def test_replay_is_faster_than_record_and_touches_no_provider():
    """Reference: record 4.68 ms -> replay 0.43 ms (11.0x faster), 0 calls."""
    r = pv.bench_replay(iters=4)
    assert r["provider_calls_during_replay"] == 0, \
        f"replay must make ZERO provider calls: {r}"
    assert r["speedup_x"] > 1.0, \
        f"replay must be FASTER than record, not slower: {r}"


# --- 8. tool-call ladder -----------------------------------------------------

def test_toolcall_ladder_per_call_cost():
    """Wave 1 published 0.016 ms/call against a 0.5 ms target; re-measured
    here at 0.0147 ms/call over the same corpus."""
    r = pv.bench_ladder(passes=8)
    assert r["corpus_cases"] >= 20
    assert r["per_call_ms"] < 0.5, f"ladder overhead regressed: {r}"


# --- 9. memory & storage -----------------------------------------------------

def test_memory_growth_bounded():
    """Reference: 3.2 MiB peak-RSS delta for 3x1000 records."""
    r = pv.bench_memory_growth(n=200)
    for key in (
        "journal_peak_kb", "journal_cur_kb", "heat_peak_kb", "heat_cur_kb",
        "usage_peak_kb", "usage_cur_kb", "total_peak_kb", "total_cur_kb",
    ):
        assert isinstance(r[key], int)
    assert r["total_peak_kb"] >= 0
    assert r["total_peak_kb"] < 200_000, f"RSS growth regressed: {r}"
    assert r["disk_bytes"] > 0


def test_storage_per_turn_bounded_and_extrapolated():
    """Reference: 4.4 KiB/turn -> ~43 MiB at 10k turns."""
    r = pv.bench_storage_per_turn(turns=6)
    assert r["per_turn_bytes"] > 0
    assert r["per_turn_bytes"] < 200 * 1024, f"storage per turn regressed: {r}"
    assert r["extrapolated_10k_bytes"] == pytest.approx(
        r["per_turn_bytes"] * 10000)


# --- 10. HONESTY properties of the report itself ----------------------------

_MUST_BE_UNMEASURABLE = [
    "request latency", "time to first token", "council completion latency",
    "tool latency", "verifier", "queue time", "model-routing savings",
    "prompt-cache savings", "cost per successful task", "recovery cost",
]


def test_every_provider_dependent_metric_is_declared_unmeasurable():
    """The register is the anti-fabrication guard: a metric that needs a real
    provider must appear here rather than as a number in the measured table."""
    blob = " ".join(name.lower() for name, _why, _how in pv.UNMEASURABLE)
    for needle in _MUST_BE_UNMEASURABLE:
        assert needle in blob, f"{needle!r} missing from the UNMEASURABLE register"


def test_every_unmeasurable_entry_states_why_and_how():
    for name, why, how in pv.UNMEASURABLE:
        assert name and why and how, f"incomplete UNMEASURABLE entry: {name}"
        assert len(why) > 30, f"UNMEASURABLE reason too thin: {name}"
        assert "Phase 5" in how or "shadow" in how, \
            f"UNMEASURABLE entry must name what would measure it: {name}"


def test_provider_dependent_objectives_are_labelled_proposals():
    """No objective that needs real traffic may read as measured."""
    needs_traffic = ("Request latency", "Time to first token", "Availability",
                     "Verified-answer rate", "Admission refusal rate",
                     "Cost per successful task")
    seen = set()
    for name, _target, basis in pv.DRAFT_SLOS:
        for key in needs_traffic:
            if name.startswith(key):
                seen.add(key)
                assert ("CANNOT BE VALIDATED UNTIL SHADOW TRAFFIC" in basis
                        or "PROPOSAL WITHHELD" in basis), \
                    f"{name} must be labelled a proposal, got: {basis}"
    assert seen == set(needs_traffic), f"missing objectives: {set(needs_traffic) - seen}"


def test_every_error_budget_is_a_proposal():
    assert pv.ERROR_BUDGETS
    for name, budget, basis in pv.ERROR_BUDGETS:
        assert name and budget
        assert "PROPOSAL" in basis, \
            f"error budget {name} must be labelled a proposal, got: {basis}"


def test_offline_measured_objectives_are_marked_measured_not_proposed():
    """The converse guard: the handful of objectives that DO have an offline
    basis must say so, so a reader can tell the two classes apart."""
    measured = [b for _n, _t, b in pv.DRAFT_SLOS if b.startswith("MEASURED")]
    assert len(measured) >= 4


# --- 11. usage-ledger accounting & latency under concurrency ----------------

_CONTENTION_REPS = 5           # independent repetitions of the whole sweep
_CONTENTION_QUORUM = 3         # ... of which this many must meet EVERY bound
_CONTENTION_PER_THREAD = 50    # calls issued by each worker at each level

# The 1-thread row is measured with no warm-up and no ledger wipe, so its `max`
# is the cost of the first `usage.record` against a FRESH SCRATCH TREE:
# directory creation, the OS's first touch of a new tree, and — only if this
# happens to be the process's first ledger write — the one-time proclock
# `fcntl` capture on Windows. Measured at 592-710 ms when the process was cold.
#
# It is NOT a guaranteed process-cold-start measurement, and this file no longer
# claims otherwise. Run under the full suite, earlier tests will already have
# imported and exercised `usage.record`/`proclock`, so the one-time costs are
# gone before this test starts. The authoritative process-restart guarantee is
# `test_first_usage_record_after_process_start_is_bounded`, which spawns a fresh
# interpreter. This bound still holds the scratch-tree first touch to the same
# 2000 ms ceiling, on EVERY repetition and outside the performance quorum,
# because a first write that takes multiple seconds is a real defect either way.
_FIRST_TOUCH_MAX_MS = 2000.0

# A repetition whose workers did not actually start together did not measure
# contention, so it cannot be evidence that the contention bounds hold. It is
# discarded from the performance quorum rather than asserted on: the barrier
# already guarantees ordering, and this only rejects a run where the OS
# scheduler stretched the release itself.
_MAX_START_SKEW_MS = 100.0


def _contention_dump(reps: list[list[dict]]) -> str:
    """Every measurement from every repetition, verbatim, for failure output.

    A concurrency bound that fails must print the whole matrix. Printing the
    one row that tripped tells a reader nothing about whether the run was noisy
    or the ledger lock genuinely regressed — which is the only question worth
    asking when this test goes red. `max` on the 1-thread row is the
    fresh-scratch-tree first touch; it dwarfs the other numbers when the process
    happened to be cold and is unremarkable when it was not, so its size is
    diagnostic context rather than a signal on its own.
    """
    lines: list[str] = []
    for i, rows in enumerate(reps, start=1):
        single = rows[0]
        lines.append(f"  repetition {i}/{len(reps)}:")
        for row in rows:
            bound = max(2.0, single["p50"] * row["threads"] * 1.5)
            lines.append(
                f"    threads={row['threads']:>3}"
                f"  samples={row['n']}/{row['expected_samples']}"
                f"  ledger={row['ledger_calls']}/{row['expected_calls']}"
                f"  p50={row['p50']:8.3f}ms (bound {bound:8.3f})"
                f"  p90={row['p90']:8.3f}ms"
                f"  p99={row['p99']:9.3f}ms"
                f"  max={row['max']:9.3f}ms"
                f"{'  <- cold first touch' if row['threads'] == 1 else ''}"
                f"  mean={row['mean']:8.3f}ms"
                f"  wall={row['wall_ms']:9.1f}ms"
                f"  throughput={row['calls_per_s']:8.1f}/s"
                f"  started={row['workers_started']}/{row['barrier_parties']}"
                f"  start_skew={row['start_skew_ms']:.3f}ms"
                f"  ledger_errors={row['ledger_read_errors'] or 'none'}")
    return "\n".join(lines)


def _contention_failures(rows: list[dict]) -> list[str]:
    """Every performance bound ONE repetition misses (empty == it held).

    Judged as a set, not one bound at a time. An earlier revision took three
    samples and picked the best one *per guarantee* — the best-median run for
    the median assertion, the best-tail run for the tail. That is not a
    guarantee about any run that ever happened: no single repetition had to
    satisfy both. A repetition counts here only if it meets all of them
    together.

    The contention bounds are applied to CONTENDED rows only. The 1-thread row
    is measured cold and its tail and throughput are dominated by first-touch
    cost, not by the lock; asserting a contention tail against it would either
    fail honestly-cold runs or force the warm-up back in. It gets its own
    cold-start guard in the test body instead.
    """
    single = rows[0]
    out: list[str] = []
    for row in rows:
        threads = row["threads"]
        # Contention is only real if every worker was in flight together. A
        # stretched barrier release means the level measured something else,
        # so the repetition is not evidence either way.
        if row["start_skew_ms"] > _MAX_START_SKEW_MS:
            out.append(
                f"{threads}t worker start skew {row['start_skew_ms']:.3f} ms "
                f"> {_MAX_START_SKEW_MS:.0f} ms — the workers did not start "
                f"together, so this repetition did not measure contention")
        if threads == 1:
            continue
        # The median is what every ordinary call pays. `usage.record`
        # serialises on a cross-process lock by design, so at N threads the
        # median rises towards N x the single-thread median — that shape is the
        # accepted cost, not a regression, and the bound has to sit above it.
        # Windows file locking makes one call several times more expensive than
        # Linux, which lifts the whole curve clear of the 2 ms floor, so half
        # again on top of the serialisation factor is what separates "the lock,
        # as designed" from "worse than the lock explains".
        bound = max(2.0, single["p50"] * threads * 1.5)
        if row["p50"] >= bound:
            out.append(
                f"{threads}t median {row['p50']:.3f} ms >= bound "
                f"{bound:.3f} ms = max(2 ms, single-thread p50 "
                f"{single['p50']:.3f} ms x {threads} x 1.5)")
        # The tail must stay far inside a provider call's own latency (~1-5 s).
        if row["p99"] >= 500.0:
            out.append(f"{threads}t p99 {row['p99']:.3f} ms >= 500 ms")
        # And the ledger must not become the limiter at these levels.
        if row["calls_per_s"] <= 100.0:
            out.append(f"{threads}t throughput {row['calls_per_s']:.1f} "
                       f"calls/s <= 100 calls/s")
    return out


def test_usage_ledger_tail_under_concurrency():
    """Stage-D F1, re-characterised. `usage.record` takes a cross-process lock
    around a read-modify-write of the day ledger after EVERY provider call, so
    the council's parallel fan-out serialises there. This is ACCEPTED, not
    fixed — batching would make spend non-durable between flushes and the spend
    cap is a safety property, not a metric.

    CORRECTION to what this test used to claim. It described the measured shape
    as "p50 flat at ~0.20 ms" with only the tail and the ceiling moving. The
    median is NOT flat and never was: serialising N callers behind one lock
    makes the median caller wait out roughly N-1 other holders, so the median
    rises about linearly in the thread count (measured 0.63 -> 3.47 -> 9.28 ms
    at 1 / 6 / 16 threads). The bound asserted below — single-thread p50 x
    threads x 1.5 — has always encoded exactly that linear growth, so the
    docstring contradicted the assertion underneath it.

    Four things must stay true for the acceptance to hold, measured at 1
    thread, at the cap the process enforces (`config.MAX_CONCURRENT_CALLS`),
    and at the 16-concurrent-call operating bound F1 publishes:

    * the MEDIAN tracks the serialisation curve and no worse, so an ordinary
      call pays the lock and nothing beyond it;
    * the TAIL stays far inside a provider call's own latency;
    * the FIRST TOUCH stays bounded. There is no warm-up and the ledger is never
      wiped, so the 1-thread row's `max` is the genuine cost of the first
      `usage.record` against a fresh scratch tree — 592-710 ms when the process
      was also cold, guarded here at 2000 ms. An earlier revision warmed that
      away, which made the row tidy and deleted a cost operators actually pay.
      This row is NOT a guaranteed process-cold-start measurement: under the
      full suite, earlier tests have already exercised `usage.record` and
      `proclock`, so the one-time costs are long gone.
      `test_first_usage_record_after_process_start_is_bounded` owns that
      guarantee by spawning a fresh interpreter;
    * accounting stays EXACT. `usage.record` swallows a ledger-lock timeout by
      design (losing one row beats re-billing the user), so contention could in
      principle buy its latency by dropping spend — which would silently break
      the budget cap this whole design exists to keep correct. Every call is
      counted out of the persisted ledger, not inferred from the timings.

    WHAT THE 3/5 QUORUM DOES AND DOES NOT BUY. The contention bounds are met by
    at least 3 of 5 independent repetitions. That is a NOISE TOLERANCE, and it
    is honest about its cost: a regression that fires on fewer than three
    repetitions in five — an intermittent stall, a lock path that degrades only
    under a particular interleaving — can pass this gate. It is chosen because
    p99 over these sample counts is roughly one scheduler stall wide on a shared
    runner, and the alternative (raise the bound until CI stops complaining)
    discards the guarantee rather than the noise. Accounting correctness carries
    NO quorum and NO tolerance: it must hold 5/5, because a lost ledger row is a
    spend-cap defect regardless of how the machine was behaving that second.
    """
    levels = pv.contention_levels()
    assert 1 in levels, f"need an uncontended baseline: {levels}"
    assert config.MAX_CONCURRENT_CALLS in levels, (
        f"the enforced cap must be one of the measured levels: {levels}")
    assert max(levels) >= 16, (
        f"the published 16-concurrent-call operating bound must be measured: "
        f"{levels}")
    # The published operating policy itself, enforced rather than assumed: F1
    # commits to council fan-out staying at or below 16 concurrent provider
    # calls per host, and every bound below is characterised at that ceiling.
    # A config change that raised the cap past it would leave the documented
    # bound unmeasured, so it fails here instead of passing quietly.
    assert config.MAX_CONCURRENT_CALLS <= pv.CONTENTION_OPERATING_BOUND, (
        f"MAX_CONCURRENT_CALLS={config.MAX_CONCURRENT_CALLS} exceeds the "
        f"published operating bound of {pv.CONTENTION_OPERATING_BOUND} "
        f"concurrent provider calls per host (finding F1) — either the cap or "
        f"the published bound has to change, deliberately")

    # Five INDEPENDENT repetitions. Each level within each repetition gets its
    # own scratch tree, so no repetition inherits another's ledger state.
    # Process-level warmth is whatever it happens to be — under the full suite
    # every repetition here is warm, which is exactly why the process-restart
    # guarantee lives in its own subprocess test rather than in repetition 1.
    reps = [pv.bench_usage_contention(levels=levels,
                                      per_thread=_CONTENTION_PER_THREAD)
            for _ in range(_CONTENTION_REPS)]
    dump = "\n" + _contention_dump(reps)

    for i, rows in enumerate(reps, start=1):
        assert [r["threads"] for r in rows] == list(levels), (
            f"repetition {i} did not measure the requested levels {levels}:"
            f"{dump}")

    # --- accounting correctness: 5/5, no quorum, no tolerance ---------------
    # A dropped ledger row is a spend-cap defect, not a slow run, so noise is
    # not an excuse for one: every repetition at every level must account for
    # every call.
    for i, rows in enumerate(reps, start=1):
        for row in rows:
            threads = row["threads"]
            expected = threads * _CONTENTION_PER_THREAD
            assert row["ledger_read_errors"] == [], (
                f"repetition {i}, {threads} threads: the persisted ledger "
                f"could not be read — {row['ledger_read_errors']}:{dump}")
            assert row["expected_calls"] == expected, (
                f"repetition {i}, {threads} threads: benchmark expected the "
                f"wrong call count:{dump}")
            assert row["ledger_calls"] == expected, (
                f"repetition {i}, {threads} threads: the persisted ledger "
                f"recorded {row['ledger_calls']} calls in __all__ but "
                f"{expected} were issued — accounting is LOSING calls under "
                f"contention, which silently under-counts spend against the "
                f"budget cap:{dump}")
            assert row["n"] == row["expected_samples"] == expected, (
                f"repetition {i}, {threads} threads: timed "
                f"{row['n']} samples, expected {expected} "
                f"({threads} threads x {_CONTENTION_PER_THREAD} calls) — the "
                f"latency percentiles below describe less work than they "
                f"claim:{dump}")
            # Contention is only real if every worker was actually in flight.
            assert row["barrier_parties"] == threads, (
                f"repetition {i}: start barrier held {row['barrier_parties']} "
                f"parties for {threads} workers:{dump}")
            assert row["workers_started"] == threads, (
                f"repetition {i}, {threads} threads: only "
                f"{row['workers_started']} workers cleared the start barrier, "
                f"so the level measured less contention than it reports:{dump}")

    # --- fresh-scratch-tree first touch: its own guard, every repetition ----
    # Nothing is warmed away, so each repetition's 1-thread `max` is the first
    # `usage.record` against a brand-new scratch tree. Guarded separately from
    # the contention bounds because it is a different property: not "does the
    # lock scale" but "does writing into a fresh tree cost seconds". No quorum —
    # a multi-second first write is a defect in any single run, not scheduler
    # noise to be voted out. The PROCESS-restart case is not measurable from
    # inside a shared interpreter and is guaranteed by
    # `test_first_usage_record_after_process_start_is_bounded` instead.
    for i, rows in enumerate(reps, start=1):
        first_touch = rows[0]
        assert first_touch["threads"] == 1, (
            f"repetition {i}: the first row must be the uncontended "
            f"baseline:{dump}")
        assert first_touch["max"] < _FIRST_TOUCH_MAX_MS, (
            f"repetition {i}: the slowest uncontended usage.record took "
            f"{first_touch['max']:.3f} ms, at or above the "
            f"{_FIRST_TOUCH_MAX_MS:.0f} ms first-touch bound (p50 "
            f"{first_touch['p50']:.3f} ms, so this is a first-touch cost, not a "
            f"per-call one) — the first ledger write into a fresh tree now "
            f"costs seconds:{dump}")

    # --- performance guarantees: at least 3 of 5 repetitions ----------------
    # Every bound is judged on each repetition as a SET, and a repetition
    # counts only if it meets all of them — including the requirement that its
    # workers actually started together. The quorum is a NOISE TOLERANCE, and
    # what it costs is real: a regression that fires on fewer than three
    # repetitions in five can still pass. It buys tolerance for a scheduler
    # stall (p99 over these sample counts is one stall wide) without letting a
    # guarantee be read off a run selected for a different statistic, and no
    # bound has been loosened to buy that headroom.
    verdicts = [_contention_failures(rows) for rows in reps]
    clean = [i for i, failures in enumerate(verdicts, start=1) if not failures]
    detail = "\n".join(
        f"  repetition {i}: " + ("all bounds held" if not failures
                                 else "; ".join(failures))
        for i, failures in enumerate(verdicts, start=1))
    assert len(clean) >= _CONTENTION_QUORUM, (
        f"only {len(clean)}/{_CONTENTION_REPS} repetitions met every "
        f"concurrency guarantee (need {_CONTENTION_QUORUM}); clean "
        f"repetitions: {clean or 'none'}\n{detail}{dump}")


class _SimulatedLedgerFault(Exception):
    """Deliberately NOT a RuntimeError, so `pytest.raises(RuntimeError)` below
    can only match the benchmark's own VOID signal — never the injected fault
    escaping the worker unchanged."""


def test_worker_exception_voids_the_contention_measurement():
    """A failing worker must destroy the measurement, never speed it up.

    This is the load-bearing property of every bound in
    `test_usage_ledger_tail_under_concurrency`: the cheapest way to look fast
    under contention is to not do the work. If a worker could raise part-way
    through and have the benchmark quietly summarise whatever samples the
    survivors produced, the p50/p99/throughput rows would improve — fewer
    threads contending, fewer calls timed — and every guarantee above would
    read as PASSING on a run that measured almost nothing.

    Both shapes are covered, because they fail in different places: a fault in
    the FIRST level (before any level has produced a row) and a fault in a
    CONTENDED level (after an earlier level has already succeeded, where a
    partial result is most tempting to keep).
    """
    from olympus import usage

    # --- fault in the uncontended level -------------------------------------
    def always_raises(*_args, **_kwargs):
        raise _SimulatedLedgerFault("simulated ledger fault")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(usage, "record", always_raises)
        with pytest.raises(RuntimeError) as caught:
            pv.bench_usage_contention(levels=(1, 4), per_thread=5)
    message = str(caught.value)
    assert "VOID" in message, (
        f"a worker exception must be reported as a VOID measurement, not as a "
        f"slow one: {message}")
    assert "_SimulatedLedgerFault" in message, (
        f"the failure must name what actually went wrong: {message}")

    # --- fault in the contended level, after a level has already passed ------
    seen = {"calls": 0}
    guard = threading.Lock()
    real_record = usage.record

    def raises_once_contended(*args, **kwargs):
        with guard:
            seen["calls"] += 1
            n = seen["calls"]
        if n > 5:                      # the 1-thread level's 5 calls succeed
            raise _SimulatedLedgerFault("simulated ledger fault under load")
        return real_record(*args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(usage, "record", raises_once_contended)
        with pytest.raises(RuntimeError) as caught:
            pv.bench_usage_contention(levels=(1, 4), per_thread=5)
    message = str(caught.value)
    assert "VOID" in message and "4-thread" in message, (
        f"a contended level whose workers failed must be voided by that level, "
        f"not salvaged from the level that passed: {message}")
    assert seen["calls"] > 5, (
        "the contended level never ran, so this asserted nothing")


# --- 12. process cold start: the authoritative first-call guarantee ---------

# Runs in a FRESH interpreter, which is the only place the claim can honestly be
# made. In-process the first `usage.record` of a pytest session has long since
# happened by the time this file runs, so the contention benchmark's 1-thread
# row can only speak for a fresh scratch tree, never for a process restart.
#
# The child sockets itself shut before importing anything from `olympus`, so
# "no provider call, no network" is enforced rather than asserted in prose: any
# attempt to open a connection raises inside the child and the parent fails with
# the child's stderr attached. MEMORY_DIR arrives via OLYMPUS_MEMORY_DIR, so the
# fresh temp tree is in force from the first import of `config` — nothing is
# monkeypatched after the fact.
_COLD_START_PROBE = textwrap.dedent(
    """
    import json
    import socket
    import sys
    import time
    from pathlib import Path

    def _no_network(*_a, **_k):
        raise AssertionError(
            "cold-start probe attempted a network connection; it must touch "
            "no provider and no socket")

    # Block CONNECTING, not the socket type itself. Replacing `socket.socket`
    # outright breaks `ssl.SSLSocket`, which subclasses it -- and `ssl` is
    # imported lazily on this very path (proclock -> errors -> telegram ->
    # urllib.request -> http.client -> ssl), so a stub that broke it would
    # abort the measurement rather than guard it. Patching the connect
    # entry points leaves every import working and still makes an outbound
    # call impossible.
    socket.create_connection = _no_network
    socket.socket.connect = _no_network
    socket.socket.connect_ex = _no_network

    sys.path.insert(0, sys.argv[1])

    from olympus import config, memory

    memory.set_user("coldstart-probe")

    from olympus import usage

    # Exactly one call, and the clock covers that call and nothing else.
    t0 = time.perf_counter()
    usage.record("claude-opus-4-8", 100, 50)
    first_call_ms = (time.perf_counter() - t0) * 1000.0

    ledger_dir = config.MEMORY_DIR / "usage"
    files = sorted(p.name for p in ledger_dir.glob("*.json"))
    calls = 0
    for name in files:
        blob = json.loads((ledger_dir / name).read_text(encoding="utf-8"))
        calls += int(blob.get("__all__", {}).get("calls", 0) or 0)

    print(json.dumps({
        "executable": sys.executable,
        "memory_dir": str(config.MEMORY_DIR),
        "record_calls_issued": 1,
        "first_call_ms": first_call_ms,
        "ledger_calls": calls,
        "ledger_files": files,
    }))
    """
)


def test_first_usage_record_after_process_start_is_bounded():
    """The authoritative process-cold-start / restart guarantee.

    `usage.record` runs after every provider call, and the FIRST one in a
    process pays costs no later call does: importing the module graph is already
    done by then, but the ledger directory does not exist, the proclock lock
    tree does not exist, and on Windows the one-time "fcntl unavailable" capture
    has not fired. An operator restarting the service pays that on their first
    reply — so it needs a bound, and the bound needs to be measured somewhere
    the claim is actually true.

    In-process it is not true. By the time any test in this file runs, a full
    pytest session has already imported and exercised `usage.record` and
    `proclock` many times over, so the contention benchmark's 1-thread row
    measures a fresh SCRATCH TREE, not a fresh PROCESS. This test spawns a real
    interpreter (`sys.executable`) with a fresh `OLYMPUS_MEMORY_DIR`, has it
    make exactly one `usage.record` call, and times only that call.

    It also proves the call is accounted for: a first write that is fast because
    it silently failed is worse than a slow one, so the child reads its own
    persisted ledger back and reports `__all__.calls`, which must be exactly 1.

    No network, no provider, no credentials, no extra dependency — before
    importing `olympus` the child points `socket.create_connection`,
    `socket.socket.connect` and `socket.socket.connect_ex` at a raising network
    guard, so a provider call would abort it rather than pass unnoticed. The
    `socket.socket` CLASS is deliberately left intact: `ssl.SSLSocket`
    subclasses it, and `ssl` is imported lazily on this very path (proclock ->
    errors -> telegram -> urllib.request -> http.client -> ssl), so replacing
    the type would abort the measurement instead of guarding it. Blocking the
    connect entry points leaves every import working and still makes an
    outbound call impossible.
    """
    memory_dir = tempfile.mkdtemp(prefix="olymp-coldstart-")
    try:
        # mkdtemp guarantees a new empty directory; assert the ledger really is
        # absent so "1 call persisted" cannot be satisfied by a pre-existing one.
        assert not (Path(memory_dir) / "usage").exists(), (
            f"the probe's MEMORY_DIR must start empty: {memory_dir}")

        child_env = dict(os.environ)
        child_env["OLYMPUS_MEMORY_DIR"] = memory_dir
        completed = subprocess.run(
            [sys.executable, "-c", _COLD_START_PROBE, str(_REPO)],
            cwd=_REPO,
            env=child_env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert completed.returncode == 0, (
            f"fresh-interpreter cold-start probe failed "
            f"(exit {completed.returncode}):\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}")

        payloads = [line for line in completed.stdout.splitlines()
                    if line.strip().startswith("{")]
        assert payloads, (
            "the cold-start probe printed no JSON payload:\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}")
        try:
            sample = json.loads(payloads[-1])
        except json.JSONDecodeError as err:
            raise AssertionError(
                f"the cold-start probe's output is not JSON ({err}):\n"
                f"stdout:\n{completed.stdout}\n"
                f"stderr:\n{completed.stderr}") from err

        # It really was a fresh interpreter, against a fresh MEMORY_DIR.
        assert sample["executable"] == sys.executable, (
            f"the probe ran under {sample['executable']!r}, not this test's "
            f"interpreter {sys.executable!r}:\n{sample}")
        assert Path(sample["memory_dir"]) == Path(memory_dir), (
            f"the probe used MEMORY_DIR {sample['memory_dir']!r} instead of the "
            f"fresh temp tree {memory_dir!r}:\n{sample}")

        # Exactly one call, and it is in the ledger.
        assert sample["record_calls_issued"] == 1, sample
        assert sample["ledger_files"] and len(sample["ledger_files"]) == 1, (
            f"one usage.record call must persist exactly one day ledger:\n"
            f"{sample}")
        assert sample["ledger_calls"] == 1, (
            f"the first usage.record of a fresh process persisted "
            f"{sample['ledger_calls']} calls in __all__, expected exactly 1 — "
            f"a first write that is fast because it was dropped is worse than a "
            f"slow one:\n{sample}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}")

        # And it is bounded.
        assert sample["first_call_ms"] > 0.0, (
            f"the probe reported a non-positive first-call latency:\n{sample}")
        assert sample["first_call_ms"] < _FIRST_TOUCH_MAX_MS, (
            f"the FIRST usage.record after a process restart took "
            f"{sample['first_call_ms']:.3f} ms, at or above the "
            f"{_FIRST_TOUCH_MAX_MS:.0f} ms bound — every operator pays this on "
            f"their first reply after a deploy:\n{sample}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}")
    finally:
        shutil.rmtree(memory_dir, ignore_errors=True)


# --- 13. the published report curve may not shrink --------------------------

def test_full_report_concurrency_curve_is_never_reduced():
    """`run_all` publishes a CURVE; the focused gate checks three points.

    These are different jobs and the narrower one must not quietly replace the
    wider one. Hardening the gate down to 1 / cap / 16 was correct — a gate
    answers "does the guarantee still hold" — but if the report had been
    narrowed with it, the scaling shape the F1 finding is argued from would
    have silently disappeared from the harness output. This pins the historical
    levels at the source so that cannot happen by omission.
    """
    assert pv.REPORT_LEVELS == (1, 2, 4, 8, 16), (
        f"the historical reporting curve was changed at the source: "
        f"{pv.REPORT_LEVELS}")

    levels = pv.report_levels()
    for historical in (1, 2, 4, 8, 16):
        assert historical in levels, (
            f"the full report dropped the {historical}-thread level, so its "
            f"concurrency curve no longer covers the published shape: {levels}")
    assert config.MAX_CONCURRENT_CALLS in levels, (
        f"the cap this process enforces ({config.MAX_CONCURRENT_CALLS}) must be "
        f"one of the reported levels: {levels}")
    assert list(levels) == sorted(set(levels)), (
        f"report levels must be sorted and unique: {levels}")

    # The gate is allowed to be narrower — but only a subset, never a different
    # set of points than the report is drawn from.
    assert set(pv.contention_levels()) <= set(levels), (
        f"the focused gate measures levels the full report does not: "
        f"gate {pv.contention_levels()} vs report {levels}")

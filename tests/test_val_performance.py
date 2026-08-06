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

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "scripts"))

import perf_validation as pv  # noqa: E402

from olympus import config  # noqa: E402


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

    Bounds are loose because the point is the ORDER of the effect, not the
    constant: the uncached arm must reproduce the defect (else the A/B is void
    and proves nothing), and the cached arm must scale at least 5x more gently
    against a measured 15.8x."""
    r = pv.bench_sync_depth_scaling(depths=(100, 900), samples=12)
    lo, hi = 100, 900
    off_slope = r["off_us_per_turn_of_depth"]
    on_slope = r["on_us_per_turn_of_depth"]

    assert off_slope > 5.0, (
        f"the pre-fix arm did not reproduce the D1 depth scaling — the A/B is "
        f"void and this test proves nothing: {r}")
    assert on_slope < off_slope / 5.0, (
        f"per-turn cost is scaling with depth again: {r}")
    # and the absolute win at the deeper end is what an operator actually feels
    assert r[f"on_ms_at_{hi}"] < r[f"off_ms_at_{hi}"] / 2.0, (
        f"the cached path is no cheaper at depth: {r}")


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
    assert r["total_peak_kb"] >= 0
    assert r["total_peak_kb"] < 200_000, f"RSS growth regressed: {r}"


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


def test_usage_ledger_tail_under_concurrency():
    """Stage-D F1, re-characterised. `usage.record` takes a cross-process lock
    around a read-modify-write of the day ledger after EVERY provider call, so
    the council's parallel fan-out serialises there.

    The measured shape (1 -> 16 threads): p50 flat at ~0.20 ms, p99 0.63 ->
    122.9 ms, throughput ceiling ~2000 calls/s. This is ACCEPTED, not fixed —
    batching would make spend non-durable between flushes and the spend cap is
    a safety property, not a metric.

    Two things must stay true for that acceptance to hold, and both are
    asserted here: the MEDIAN must not degrade beyond what serialising on that
    lock already explains (so ordinary calls pay the lock and nothing more),
    and the tail at the documented operating bound of 16 concurrent calls must
    stay well inside a provider call's own latency."""
    # Best of three samples, and the bound is unchanged at 500 ms.
    #
    # `p99` over 200 observations is the *second worst* one, and the healthy
    # shape here is p50 0.18 ms / p90 0.37 ms — so the statistic is one
    # scheduler stall wide. On a shared CI runner it tripped at 605 ms and
    # 664 ms on runs where nothing touching `usage.record` or `proclock` had
    # changed, while p50 and p90 stayed flat. That is a measurement artefact,
    # not a regression, and it fails the build on whichever leg happens to be
    # unlucky.
    #
    # Sampling three times and asserting on the best keeps the guarantee
    # intact: a real tail regression regresses every sample, so it still
    # fails. A single stall no longer does. The alternative — raising the
    # bound until CI stops complaining — would discard the guarantee instead
    # of the noise, which is the wrong one to give up.
    attempts = [pv.bench_usage_contention(levels=(1, 8), per_thread=25)
                for _ in range(3)]
    for rows in attempts:
        assert rows[0]["threads"] == 1 and rows[1]["threads"] == 8

    # Each guarantee is judged against its own best sample. Selecting one
    # sample by `p99` and then reading its `p50` left the median assertion
    # riding on a statistic nothing had selected for, so the best-of-three
    # protected the tail and not the median.
    median = min(attempts, key=lambda r: r[1]["p50"] / max(r[0]["p50"], 1e-9))
    tail = min(attempts, key=lambda r: r[1]["p99"])

    # The median is what every ordinary call pays. `usage.record` serialises on
    # a cross-process lock by design, so at N threads the median rises towards
    # N x the single-thread median — that shape is the accepted cost, not a
    # regression, and the bound has to sit above it. Windows file locking makes
    # one call ~6x more expensive than Linux (p50 1.38 ms vs 0.24 ms), which
    # lifts the whole curve clear of the 2 ms floor and left the old
    # `single["p50"] * 8` bound sitting exactly on the fully serialised value:
    # the same commit failed it by 0.7% on one CI run and passed on another.
    # Half again on top of the serialisation factor still catches a median
    # degrading faster than the lock explains.
    single, many = median[0], median[1]
    bound = max(2.0, single["p50"] * many["threads"] * 1.5)
    assert many["p50"] < bound, (
        f"median accounting cost now degrades with concurrency beyond what "
        f"serialising on the ledger lock explains (bound {bound:.3f} ms): "
        f"{median}")
    # and the tail stays far below a provider call (~1-5 s)
    assert tail[1]["p99"] < 500.0, (
        f"ledger tail regressed across all {len(attempts)} samples; best was "
        f"{tail[1]}, all p99s {[a[1]['p99'] for a in attempts]}")
    assert many["calls_per_s"] > 100.0, f"throughput collapsed: {many}"

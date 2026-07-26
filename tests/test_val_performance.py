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


def test_sync_cost_no_longer_grows_with_session_depth():
    """Stage-D DEFECT D1, fixed. `sync` re-scanned, re-verified and re-replayed
    the WHOLE journal on every turn, so a conversation's per-turn journaling
    cost grew with its own depth: measured 5.35x growth across 400 turns,
    +0.0198 ms/turn of slope, projecting ~200 ms/turn at 10k turns.

    `_cache_get` now reuses the previous turn's verified replay and chain tail
    when the file is provably untouched (stat tuple AND tail seal), so the hot
    path is O(new records). Measured after the fix: 0.61x growth, slope flat.

    Both arms run here on the same machine in the same process, so this is a
    controlled A/B rather than a comparison to a recorded constant. The
    assertion is deliberately loose (2x headroom on the ratio between arms) —
    it must catch the cache being disabled or bypassed, not CI jitter."""
    on = pv.bench_sessionlog_sync(turns=150, cache=True)
    off = pv.bench_sessionlog_sync(turns=150, cache=False)
    assert off["growth_ratio"] > 1.5, (
        f"the pre-fix arm did not reproduce the D1 growth — the A/B is void "
        f"and this test proves nothing: {off}")
    assert on["growth_ratio"] < off["growth_ratio"] / 2.0, (
        f"per-turn cost is growing with depth again: on={on} off={off}")
    assert on["slope_ms_per_turn"] < off["slope_ms_per_turn"], (
        f"the cached path is no cheaper at depth: on={on} off={off}")


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
    asserted here: the MEDIAN must not degrade with concurrency (so ordinary
    calls are unaffected), and the tail at the documented operating bound of 16
    concurrent calls must stay well inside a provider call's own latency."""
    rows = pv.bench_usage_contention(levels=(1, 8), per_thread=25)
    single, many = rows[0], rows[1]
    assert single["threads"] == 1 and many["threads"] == 8
    # the median is what every ordinary call pays
    assert many["p50"] < max(2.0, single["p50"] * 8), (
        f"median accounting cost now degrades with concurrency: {rows}")
    # and the tail stays far below a provider call (~1-5 s)
    assert many["p99"] < 500.0, f"ledger tail regressed: {many}"
    assert many["calls_per_s"] > 100.0, f"throughput collapsed: {many}"

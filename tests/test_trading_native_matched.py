"""The matched-evaluation harness, probed where it could flatter Olympus.

> Do not begin with the assumption that Olympus is better.

Every test here is aimed at a way this harness could reach that conclusion
without earning it:

* **answering a different question in the same words.** Five arms beating each
  other is not evidence about the external reference, so `verdict` returns
  `INSUFFICIENT_EVIDENCE` whenever the reference arm did not run — and
  `test_no_arrangement_of_the_other_arms_produces_a_verdict_without_the_reference`
  builds the most favourable possible arrangement and asserts it still does.
* **comparing arms measured differently.** A cost model or a split that
  differs between arms is invisible in the resulting numbers, so
  `compare_arms` raises on a contract mismatch rather than noting it.
* **crediting data as architecture.** A cross-input-class comparison raises
  unless the caller says so, and records the advantage when it does.
* **counting the same move several times.** Stride-1 windows overlap;
  `non_overlapping` is what makes a Sharpe ratio mean anything, and the test
  shows the inflation it removes.
* **scoring a decline as a correct answer.** Accuracy metrics read answered
  rows only.
* **finding significance by searching.** Holm adjustment over the family, and
  `significant` reads the adjusted p-value.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from olympus.trading.contracts import Regime
from olympus.trading.errors import ConfigurationError
from olympus.trading.native import matched as M

START = datetime(2027, 6, 1, tzinfo=timezone.utc)


def contract(**overrides) -> M.FairnessContract:
    kwargs = dict(
        instruments=("SIM:A",), first_ts=START,
        last_ts=START + timedelta(days=10),
        prediction_timestamps_hash="abc123", n_prediction_timestamps=200,
        horizon=3, lookback=33,
        input_availability_rule="bars closing at or before the instant",
        data_quality_rule="OHLC coherence enforced at construction",
        split_scheme="embargoed three-way temporal split", n_folds=1,
        embargo_bars=3, cost_model={"round_trip_bps": 4.0},
        risk_limits={"max_position": 1},
        strategy_rules="long above the cost threshold, short below, else flat",
        execution_assumptions="fill at the anchor close")
    kwargs.update(overrides)
    return M.FairnessContract(**kwargs)


def records(n: int = 60, *, error: float = 0.001, seed: int = 1,
            instrument: str = "SIM:A", abstain_every: int = 0,
            band: float = 0.004, start: datetime = START):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        realised = rng.gauss(0.0, 0.003)
        declined = abstain_every and i % abstain_every == 0
        predicted = None if declined else realised + rng.gauss(0.0, error)
        position = 0 if predicted is None else (1 if predicted > 0 else -1)
        out.append(M.WindowRecord(
            key=f"{instrument}@{i}", instrument=instrument,
            ts=start + timedelta(hours=i),
            regime=Regime.RANGING.value if i % 2 else Regime.TRENDING_UP.value,
            realised=realised, predicted=predicted,
            quantiles={} if declined else {"p10": predicted - band,
                                           "p90": predicted + band},
            nominal_coverage=None if declined else 0.8,
            abstained=declined, position=position,
            net_return=None if predicted is None else position * realised - 0.0004,
            cost_paid=0.0 if predicted is None else 0.0004,
            latency_ms=1.0 + i * 0.01))
    return out


def arm(kind: M.ArmKind, name: str, *, available: bool = True,
        input_class: M.InputClass = M.InputClass.CANDLES_ONLY,
        rows=None, fingerprint: str | None = None,
        contract_obj: M.FairnessContract | None = None) -> M.ArmResult:
    base = contract_obj or contract()
    if not available:
        return M.ArmResult(
            spec=M.unavailable(kind, name, reason="not obtainable here",
                               blockers=("B2",), input_class=input_class),
            contract_fingerprint=fingerprint or base.fingerprint())
    rows = records() if rows is None else rows
    return M.ArmResult(
        spec=M.ArmSpec(kind=kind, name=name, input_class=input_class,
                       availability=M.ArmAvailability(available=True)),
        contract_fingerprint=fingerprint or base.fingerprint(),
        records=tuple(rows), forecast=M.forecast_metrics(rows),
        trading=M.trading_metrics(rows, horizon=3))


# ===========================================================================
# 1. the six arms, and the ones that did not run
# ===========================================================================

def test_all_six_required_systems_are_named():
    assert len(M.REQUIRED_ARMS) == 6
    assert {a.value for a in M.ArmKind} == {
        "persistence", "statistical", "machine_learning", "external_reference",
        "olympus_native", "ensemble"}
    # The incumbent is unnamed in the native package on purpose — gate G2. Its
    # identity comes from the adapter that owns it.
    from olympus.trading.kronos_adapter import matched_reference_label
    assert matched_reference_label()


def test_an_unavailable_arm_must_say_why():
    with pytest.raises(ConfigurationError):
        M.ArmAvailability(available=False, reason="   ")
    spec = M.unavailable(M.ArmKind.EXTERNAL_REFERENCE, "kronos",
                         reason="the checkpoint is unreachable",
                         blockers=("B2", "B4"))
    assert spec.available is False
    assert spec.availability.blockers == ("B2", "B4")


def test_an_available_arm_with_no_records_is_refused():
    """The shape a silently-failed arm would take. Marking it unavailable with
    a reason is the only honest alternative."""
    with pytest.raises(ConfigurationError):
        M.ArmResult(
            spec=M.ArmSpec(kind=M.ArmKind.EXTERNAL_REFERENCE, name="k",
                           input_class=M.InputClass.CANDLES_ONLY,
                           availability=M.ArmAvailability(available=True)),
            contract_fingerprint=contract().fingerprint())


def test_missing_arms_are_carried_in_every_rendering():
    report = M.MatchedReport(
        contract=contract(),
        arms=(arm(M.ArmKind.PERSISTENCE, "persistence"),
              arm(M.ArmKind.EXTERNAL_REFERENCE, "reference", available=False)))
    missing = report.missing_arms
    assert set(missing) >= {"external_reference", "statistical",
                            "machine_learning", "olympus_native", "ensemble"}
    assert "not obtainable here" in missing["external_reference"]
    assert "missing_arms" in report.to_dict()


# ===========================================================================
# 2. the fairness contract
# ===========================================================================

def test_a_contract_must_state_every_rule():
    for field in ("input_availability_rule", "data_quality_rule",
                  "split_scheme", "strategy_rules", "execution_assumptions"):
        with pytest.raises(ConfigurationError):
            contract(**{field: "  "})
    with pytest.raises(ConfigurationError):
        contract(cost_model={})
    with pytest.raises(ConfigurationError):
        contract(instruments=())


def test_the_fingerprint_changes_with_anything_that_changes_a_comparison():
    base = contract()
    for change in ({"cost_model": {"round_trip_bps": 8.0}},
                   {"horizon": 5}, {"split_scheme": "random"},
                   {"prediction_timestamps_hash": "different"},
                   {"risk_limits": {"max_position": 3}},
                   {"execution_assumptions": "mid-price fills"}):
        assert contract(**change).fingerprint() != base.fingerprint(), change


def test_comparing_arms_measured_differently_raises():
    """The single most effective way to manufacture an improvement, and it is
    not detectable from the numbers afterwards."""
    cheap = contract()
    realistic = contract(cost_model={"round_trip_bps": 20.0})
    left = arm(M.ArmKind.OLYMPUS_NATIVE, "olympus", contract_obj=cheap)
    right = arm(M.ArmKind.PERSISTENCE, "persistence", contract_obj=realistic)
    with pytest.raises(ConfigurationError) as excinfo:
        M.compare_arms(left, right)
    assert "same contract" in str(excinfo.value)
    assert cheap.differences(realistic)


def test_a_report_refuses_an_arm_from_a_different_contract():
    with pytest.raises(ConfigurationError):
        M.MatchedReport(
            contract=contract(),
            arms=(arm(M.ArmKind.PERSISTENCE, "p", fingerprint="wrong"),))


# ===========================================================================
# 3. input advantage
# ===========================================================================

def test_a_cross_input_class_comparison_is_refused_by_default():
    full = arm(M.ArmKind.OLYMPUS_NATIVE, "olympus (full)",
               input_class=M.InputClass.FULL_FEATURE)
    plain = arm(M.ArmKind.PERSISTENCE, "persistence")
    with pytest.raises(ConfigurationError) as excinfo:
        M.compare_arms(full, plain)
    assert "data advantage" in str(excinfo.value)


def test_an_allowed_cross_class_comparison_records_the_advantage():
    full = arm(M.ArmKind.OLYMPUS_NATIVE, "olympus (full)",
               input_class=M.InputClass.FULL_FEATURE)
    plain = arm(M.ArmKind.PERSISTENCE, "persistence")
    comparisons = M.compare_arms(full, plain, allow_input_advantage=True)
    assert comparisons
    assert all("full_feature" in c.input_advantage for c in comparisons)


def test_every_input_class_declares_what_it_means():
    assert set(M.INPUT_CLASS_NOTES) == set(M.InputClass)
    assert "architecture" in M.INPUT_CLASS_NOTES[M.InputClass.CANDLES_ONLY]


# ===========================================================================
# 4. metrics
# ===========================================================================

def test_a_metric_an_arm_does_not_produce_is_none_not_zero():
    metrics = M.forecast_metrics(records())
    assert metrics.brier is None
    assert metrics.regime_accuracy is None
    assert metrics.volatility_error is None
    assert "brier" in metrics.unmeasured
    assert metrics.mae is not None


def test_accuracy_reads_answered_rows_only():
    rows = records(60, abstain_every=2)
    metrics = M.forecast_metrics(rows)
    assert metrics.n == 60
    assert metrics.n_answered == 30
    # The declines are not scored as zero-error answers.
    answered_only = M.forecast_metrics([r for r in rows if r.answered])
    assert metrics.mae == pytest.approx(answered_only.mae)


def test_abstention_quality_is_none_for_an_arm_that_never_declines():
    """Not 1.0 and not 0.0 — an arm with no abstention is not in the ranking."""
    precision, recall = M._abstention_quality(records(60))
    assert precision is None and recall is None
    precision, recall = M._abstention_quality(records(60, abstain_every=3))
    assert precision is not None and 0.0 <= precision <= 1.0


def test_reliability_counts_failures_separately_from_abstentions():
    rows = records(40)
    rows[0] = M.WindowRecord(key="f", instrument="SIM:A", ts=START,
                             regime="ranging", realised=0.001, failed=True)
    metrics = M.forecast_metrics(rows)
    assert metrics.reliability == pytest.approx(1.0 - 1 / 40)
    assert M.forecast_metrics(records(40, abstain_every=2)).reliability == 1.0


def test_rmse_is_at_least_mae():
    metrics = M.forecast_metrics(records(80))
    assert metrics.rmse >= metrics.mae


def test_every_metric_declares_which_direction_is_better():
    for name in M._FORECAST_FIELDS:
        if name == "coverage":
            continue          # closeness to nominal, carried by calibration
        assert name in M.FORECAST_DIRECTION, name
    for name in M._TRADING_FIELDS:
        assert name in M.TRADING_DIRECTION, name


# ===========================================================================
# 5. overlapping windows
# ===========================================================================

def test_overlapping_windows_are_dropped_before_a_sharpe_is_computed():
    """Stride-1 windows share their horizons. Counting them as independent
    periods multiplies Sharpe by roughly the square root of the overlap, and
    nothing raises — the first version of this harness reported +38."""
    rows = records(120)
    kept, dropped = M.non_overlapping(rows, horizon=3)
    assert len(kept) == 40 and dropped == 80

    honest = M.trading_metrics(rows, horizon=3)
    inflated = M.trading_metrics(rows, horizon=1)
    assert honest.n_periods == 40
    assert honest.n_dropped_overlapping == 80
    assert inflated.n_periods == 120
    assert abs(inflated.sharpe) > abs(honest.sharpe), (
        "treating overlapping windows as independent did not inflate Sharpe, "
        "so this test is not measuring what it claims")


def test_non_overlapping_samples_per_instrument():
    rows = records(30, instrument="SIM:A") + records(30, instrument="SIM:B")
    kept, _ = M.non_overlapping(rows, horizon=3)
    assert len({r.instrument for r in kept}) == 2
    assert sum(1 for r in kept if r.instrument == "SIM:A") == 10


def test_annualisation_divides_by_the_horizon():
    rows = records(120)
    metrics = M.trading_metrics(rows, horizon=3, bars_per_year=8760)
    periods = [r.net_return for r in M.non_overlapping(rows, horizon=3)[0]]
    import statistics
    expected = statistics.fmean(periods) * (8760 / 3)
    assert metrics.annualised_return == pytest.approx(expected)


def test_a_tail_loss_needs_enough_trades():
    assert M.trading_metrics(records(12), horizon=3).tail_loss is None
    assert M.trading_metrics(records(200), horizon=1).tail_loss is not None


# ===========================================================================
# 6. statistics
# ===========================================================================

def test_holm_is_step_down_and_monotone():
    adjusted = M.holm_bonferroni([0.001, 0.02, 0.04, 0.3])
    assert adjusted == sorted(adjusted)
    assert adjusted[0] == pytest.approx(0.004)
    assert all(a >= r for a, r in zip(adjusted, [0.001, 0.02, 0.04, 0.3]))
    assert M.holm_bonferroni([]) == []
    assert all(v <= 1.0 for v in M.holm_bonferroni([0.5, 0.6, 0.9]))


def test_significance_reads_the_adjusted_p_value():
    raw = M.Comparison(arm="a", reference="b", metric="mae", scope="all",
                       n_paired=100, gain=0.001, ci_low=0.0005, ci_high=0.0015,
                       p_value=0.01, p_adjusted=0.01)
    assert raw.significant is True
    corrected = M.Comparison(arm="a", reference="b", metric="mae", scope="all",
                             n_paired=100, gain=0.001, ci_low=0.0005,
                             ci_high=0.0015, p_value=0.01, p_adjusted=0.4)
    assert corrected.significant is False, (
        "a result that survived only before correction was called significant")


def test_an_interval_straddling_zero_is_not_significant():
    comparison = M.Comparison(arm="a", reference="b", metric="mae", scope="all",
                              n_paired=100, gain=0.001, ci_low=-0.0005,
                              ci_high=0.002, p_value=0.01, p_adjusted=0.01)
    assert comparison.significant is False


def test_a_thin_comparison_is_not_usable():
    comparison = M.Comparison(arm="a", reference="b", metric="mae", scope="all",
                              n_paired=5, gain=0.001, ci_low=0.0005,
                              ci_high=0.0015, p_value=0.001, p_adjusted=0.001)
    assert comparison.usable is False and comparison.significant is False


def test_the_family_correction_covers_every_test():
    left = arm(M.ArmKind.OLYMPUS_NATIVE, "olympus")
    right = arm(M.ArmKind.PERSISTENCE, "persistence")
    raw = M.compare_arms(left, right) + M.compare_arms(right, left)
    adjusted = M.adjust_family(raw)
    assert len(adjusted) == len(raw)
    assert all(c.p_adjusted is not None for c in adjusted if c.p_value is not None)
    assert all(c.p_adjusted >= c.p_value for c in adjusted
               if c.p_value is not None)


def test_arms_are_paired_on_the_instant_not_by_position():
    """Two arms with different abstention behaviour produce different-length
    series; zipping them would pair different windows."""
    left_rows = records(60)
    right_rows = [r for i, r in enumerate(records(60)) if i % 2 == 0]
    left = arm(M.ArmKind.OLYMPUS_NATIVE, "olympus", rows=left_rows)
    right = arm(M.ArmKind.PERSISTENCE, "persistence", rows=right_rows)
    comparisons = M.compare_arms(left, right, metrics=("mae",))
    assert comparisons[0].n_paired == 30


# ===========================================================================
# 7. the verdict — the central structural guarantee
# ===========================================================================

def winning_comparison(arm_name: str, reference: str, metric: str
                       ) -> M.Comparison:
    return M.Comparison(arm=arm_name, reference=reference, metric=metric,
                        scope="all", n_paired=500, gain=0.01, ci_low=0.008,
                        ci_high=0.012, p_value=0.0, p_adjusted=0.0)


def test_no_arrangement_of_the_other_arms_produces_a_verdict_without_the_reference():
    """The most favourable arrangement that can be built: Olympus available,
    every other arm available, and Olympus winning every comparison against
    every one of them by a wide, significant margin. The verdict is still
    `INSUFFICIENT_EVIDENCE`, because the question is about the reference."""
    base = contract()
    arms = [arm(M.ArmKind.PERSISTENCE, "persistence"),
            arm(M.ArmKind.STATISTICAL, "autoregression"),
            arm(M.ArmKind.MACHINE_LEARNING, "gbt"),
            arm(M.ArmKind.OLYMPUS_NATIVE, "olympus"),
            arm(M.ArmKind.EXTERNAL_REFERENCE, "reference", available=False),
            arm(M.ArmKind.ENSEMBLE, "ensemble", available=False)]
    comparisons = [winning_comparison("olympus", reference, metric)
                   for reference in ("persistence", "autoregression", "gbt",
                                     "reference")
                   for metric in ("mae", "quantile_loss", "net_return")]
    report = M.MatchedReport(contract=base, arms=tuple(arms),
                             comparisons=tuple(comparisons))
    assert report.reference_ran is False
    assert report.verdict is M.Verdict.INSUFFICIENT_EVIDENCE
    assert report.promotion is M.Promotion.NO_DECISION


def test_there_is_no_argument_that_overrides_the_verdict():
    import dataclasses
    fields = {f.name for f in dataclasses.fields(M.MatchedReport)}
    assert "verdict" not in fields
    assert "promotion" not in fields
    with pytest.raises(TypeError):
        M.MatchedReport(contract=contract(), arms=(),
                        verdict=M.Verdict.OLYMPUS_CLEARLY_SUPERIOR)


def test_the_six_decision_categories_are_the_only_ones():
    assert len(M.Verdict) == 6
    assert {v.value for v in M.Verdict} == {
        "olympus_clearly_superior",
        "olympus_superior_only_in_specific_regimes",
        "olympus_complementary_to_reference",
        "statistically_indistinguishable",
        "external_reference_superior", "insufficient_evidence"}


def _with_reference(comparisons, *, olympus_rows=None):
    base = contract()
    arms = [arm(M.ArmKind.PERSISTENCE, "persistence"),
            arm(M.ArmKind.EXTERNAL_REFERENCE, "reference"),
            arm(M.ArmKind.OLYMPUS_NATIVE, "olympus",
                rows=olympus_rows or records())]
    return M.MatchedReport(contract=base, arms=tuple(arms),
                           comparisons=tuple(comparisons))


def test_the_reference_is_superior_when_olympus_loses_every_usable_comparison():
    losing = [M.Comparison(arm="olympus", reference="reference", metric=metric,
                           scope="all", n_paired=500, gain=-0.01,
                           ci_low=-0.012, ci_high=-0.008, p_value=0.0,
                           p_adjusted=0.0)
              for metric in ("mae", "net_return")]
    assert _with_reference(losing).verdict is M.Verdict.REFERENCE_SUPERIOR
    assert (_with_reference(losing).promotion
            is M.Promotion.REFERENCE_REMAINS_CHAMPION)


def test_indistinguishable_when_nothing_reaches_significance():
    flat = [M.Comparison(arm="olympus", reference="reference", metric=metric,
                         scope="all", n_paired=500, gain=0.0001,
                         ci_low=-0.001, ci_high=0.001, p_value=0.6,
                         p_adjusted=0.9)
            for metric in ("mae", "net_return")]
    assert _with_reference(flat).verdict is M.Verdict.INDISTINGUISHABLE


def test_complementary_when_olympus_wins_some_and_loses_others():
    mixed = [
        winning_comparison("olympus", "reference", "mae"),
        M.Comparison(arm="olympus", reference="reference", metric="net_return",
                     scope="all", n_paired=500, gain=-0.01, ci_low=-0.012,
                     ci_high=-0.008, p_value=0.0, p_adjusted=0.0)]
    report = _with_reference(mixed)
    assert report.verdict is M.Verdict.OLYMPUS_COMPLEMENTARY
    assert report.promotion is M.Promotion.ENSEMBLE


def test_clearly_superior_needs_wins_and_no_losses():
    winning = [winning_comparison("olympus", "reference", metric)
               for metric in ("mae", "quantile_loss", "net_return")]
    report = _with_reference(winning)
    assert report.verdict is M.Verdict.OLYMPUS_CLEARLY_SUPERIOR
    assert report.promotion is M.Promotion.OLYMPUS_REPLACES_REFERENCE


def test_superior_in_regimes_when_only_a_regime_comparison_wins():
    flat = [M.Comparison(arm="olympus", reference="reference", metric=metric,
                         scope="all", n_paired=500, gain=0.0001,
                         ci_low=-0.001, ci_high=0.001, p_value=0.6,
                         p_adjusted=0.9)
            for metric in ("mae", "net_return")]
    regime = [M.Comparison(arm="olympus", reference="reference", metric="mae",
                           scope="regime=crisis", n_paired=200, gain=0.01,
                           ci_low=0.008, ci_high=0.012, p_value=0.0,
                           p_adjusted=0.0)]
    base = contract()
    report = M.MatchedReport(
        contract=base,
        arms=(arm(M.ArmKind.EXTERNAL_REFERENCE, "reference"),
              arm(M.ArmKind.OLYMPUS_NATIVE, "olympus")),
        comparisons=tuple(flat), regime_comparisons=tuple(regime))
    assert report.verdict is M.Verdict.OLYMPUS_SUPERIOR_IN_REGIMES
    assert report.promotion is M.Promotion.OLYMPUS_ROUTES_SELECTED_REGIMES


def test_too_few_usable_comparisons_is_insufficient_even_with_the_reference():
    single = [winning_comparison("olympus", "reference", "mae")]
    assert _with_reference(single).verdict is M.Verdict.INSUFFICIENT_EVIDENCE


# ===========================================================================
# 8. the baseline sub-question
# ===========================================================================

def test_the_baseline_question_is_reported_separately_from_the_reference_one():
    losses = [M.Comparison(arm="olympus", reference=reference, metric=metric,
                           scope="all", n_paired=600, gain=-0.01,
                           ci_low=-0.012, ci_high=-0.008, p_value=0.0,
                           p_adjusted=0.0)
              for reference in ("persistence", "autoregression", "gbt")
              for metric in ("mae", "quantile_loss", "net_return")]
    report = M.MatchedReport(
        contract=contract(),
        arms=(arm(M.ArmKind.PERSISTENCE, "persistence"),
              arm(M.ArmKind.STATISTICAL, "autoregression"),
              arm(M.ArmKind.MACHINE_LEARNING, "gbt"),
              arm(M.ArmKind.OLYMPUS_NATIVE, "olympus"),
              arm(M.ArmKind.EXTERNAL_REFERENCE, "reference", available=False)),
        comparisons=tuple(losses))
    # The reference question stays unanswered ...
    assert report.verdict is M.Verdict.INSUFFICIENT_EVIDENCE
    # ... and the baseline question is answered, in the negative.
    baseline = report.baseline_verdict
    assert baseline["answerable"] is True
    assert baseline["n_comparisons"] == 9
    assert len(baseline["significant_losses"]) == 9
    assert baseline["significant_wins"] == []
    assert "does not beat" in baseline["conclusion"]


def test_the_baseline_question_is_unanswerable_without_an_olympus_arm():
    report = M.MatchedReport(
        contract=contract(),
        arms=(arm(M.ArmKind.PERSISTENCE, "persistence"),))
    assert report.baseline_verdict["answerable"] is False


# ===========================================================================
# 9. the published run
# ===========================================================================

def test_the_published_evaluation_reproduces_its_headline_findings():
    """The script is the artefact; if it drifts from this file it stops being
    evidence. Kept small so it runs inside the suite."""
    import importlib.util
    import re
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "matched_evaluation.py"
    assert script.is_file()
    completed = subprocess.run(
        [sys.executable, str(script), "--bars", "700"],
        capture_output=True, text=True, timeout=1800)
    assert completed.returncode == 0, completed.stderr[-3000:]
    out = completed.stdout
    # The script sits outside the independence boundary and names the
    # reference plainly, which is the point of the split.
    assert "NOT BUILDABLE" in out
    assert "Kronos" in out
    assert "VERDICT              INSUFFICIENT_EVIDENCE" in out
    assert "PROMOTION            NO_PROMOTION_DECISION_POSSIBLE" in out
    # How many arms run depends on the host: the Olympus arm needs torch, and
    # the base install deliberately does not have it. Asserting a fixed 5 made
    # this test a statement about the machine rather than about the script, and
    # it failed on the CI leg whose whole purpose is to have no torch. What is
    # invariant is that the Kronos arm is the one that could not be built, and
    # that whatever did run was measured rather than skipped.
    ran = int(re.search(r"arms that ran\s+(\d+)", out).group(1))
    required = int(re.search(r"arms required\s+(\d+)", out).group(1))
    assert 4 <= ran < required, f"{ran} of {required} arms ran"
    try:
        torch_present = importlib.util.find_spec("torch") is not None
    except (ImportError, ValueError):
        # `find_spec` returns None for a missing module but *raises* when a
        # meta-path hook refuses it, which is how the no-torch CI simulation
        # and some vendored environments express absence.
        torch_present = False
    assert ran == (5 if torch_present else 4), (
        f"{ran} arms ran with torch {'present' if torch_present else 'absent'}")
    # The arms that did run are still measured and compared.
    assert "gradient-boosted trees" in out
    assert "Holm-adjusted at alpha=0.05" in out

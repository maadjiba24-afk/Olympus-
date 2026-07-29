"""The learning loop, challenger generation, the twelve-stage gate, and the
two demonstrations Phase 4 requires end to end.

The failure modes this file is arranged around are the ones a self-improving
system produces on its own, without anyone deciding to cheat:

* **abstaining its way to a perfect record.** A model that declines every
  window has no errors. So an abstention is evidence, `abstention_quality` is
  selectivity rather than rate, and a model that declines everything scores
  `None` — not a high number.
* **maturing a forecast early.** Reading the outcome of a horizon that has not
  finished. `PendingForecast.mature()` refuses, and there is no `force`.
* **skipping the leakage test.** `advance()` accepts only the next stage.
* **promoting itself.** `promote()` calls `governance.authorise` first, and an
  autonomous actor has no route to becoming an operator.
* **losing a failure.** A rejection is terminal, `StageResult` has no mutator,
  and `concealment_check()` re-reads the stored objects independently.
* **improving by generating more work.** `measure()` raises on a volume metric
  rather than dropping it.

The two demonstrations are at the bottom, written as the specification words
them, so a reader can check the sequence against the requirement line by line.
"""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from olympus.trading.contracts import DataQuality, Regime
from olympus.trading.errors import ConfigurationError, GovernanceViolation
from olympus.trading.governance import Actor
from olympus.trading.native import challengers as CH
from olympus.trading.native import evidence as EV
from olympus.trading.native import improvement as IM
from olympus.trading.native import promotion as PR

START = datetime(2028, 5, 1, tzinfo=timezone.utc)
KEY = "SIM:E"


def pending(index: int = 0, **overrides) -> EV.PendingForecast:
    created = START + timedelta(hours=index)
    declined = overrides.pop("abstained", False)
    predicted = overrides.pop("predicted_return",
                              None if declined else 0.001)
    band = overrides.pop("band", 0.004)
    kwargs = dict(
        forecast_id=f"f{index:04d}", instrument_key=KEY, timeframe="1h",
        created_at=created, input_end=created, horizon=3,
        dataset_version="ds-1", feature_schema_version="olympus.market.v1",
        model_version="native-mt-1", checkpoint_hash="a" * 16,
        predicted_return=predicted,
        quantiles={} if predicted is None else {"p10": predicted - band,
                                                "p90": predicted + band},
        nominal_coverage=None if predicted is None else 0.8,
        uncertainty=None if predicted is None else 0.3,
        abstained=declined,
        abstention_reasons=("excessive_dispersion",) if declined else (),
        regime=Regime.RANGING.value)
    kwargs.update(overrides)
    return EV.PendingForecast(**kwargs)


#: Half-width of an 80% interval for a unit normal. The fixtures use it so a
#: "healthy" journal really is calibrated — the first version used a round
#: number and the calibration detector correctly found +16 coverage points in
#: a journal the test called healthy.
Z80 = 1.2816


def journal_of(n: int = 120, *, seed: int = 3, noisy_regime: str = "",
               noise: float = 0.002) -> EV.EvidenceJournal:
    rng = random.Random(seed)
    journal = EV.EvidenceJournal()
    for i in range(n):
        regime = (noisy_regime if noisy_regime and i % 2 == 0
                  else Regime.TRENDING_UP.value)
        scale = noise * (3.0 if regime == noisy_regime else 1.0)
        record = pending(i, regime=regime, band=Z80 * scale,
                         predicted_return=rng.gauss(0.0, 0.0005))
        journal.record(record)
        journal.mature(record.forecast_id,
                       realised_return=rng.gauss(0.0, scale),
                       now=record.matures_at,
                       modelled_cost_bps=2.0, realised_cost_bps=2.1)
    return journal


# ===========================================================================
# 1. the record
# ===========================================================================

def test_a_forecast_cannot_mature_before_its_horizon_has_elapsed():
    forecast = pending()
    with pytest.raises(ConfigurationError):
        forecast.mature(realised_return=0.001,
                        now=forecast.matures_at - timedelta(seconds=1))
    evidence = forecast.mature(realised_return=0.001, now=forecast.matures_at)
    assert evidence.matured_at == forecast.matures_at


def test_there_is_no_argument_that_forces_an_early_maturation():
    import inspect
    signature = inspect.signature(EV.PendingForecast.mature)
    assert not any(name in signature.parameters
                   for name in ("force", "ignore_horizon", "backfill"))


def test_maturity_is_computed_from_the_clock_and_the_timeframe():
    forecast = pending(horizon=4, timeframe="1h")
    assert forecast.matures_at == forecast.input_end + timedelta(hours=4)
    assert forecast.is_mature(forecast.matures_at) is True
    assert forecast.is_mature(forecast.matures_at - timedelta(minutes=1)) is False


def test_every_field_phase_four_names_is_on_the_record():
    forecast = pending()
    evidence = forecast.mature(
        realised_return=0.002, now=forecast.matures_at,
        strategy_id="momentum-1", strategy_used=True,
        modelled_cost_bps=2.0, realised_cost_bps=3.5,
        trade_outcome=EV.TradeOutcome.WIN, trade_return=0.0015)
    payload = evidence.to_dict()
    for field in ("dataset_version", "feature_schema_version", "model_version",
                  "predicted_return", "uncertainty", "abstained",
                  "abstention_reasons", "regime", "instrument", "timeframe"):
        assert field in payload["forecast"], field
    for field in ("realised_return", "signed_error", "absolute_error",
                  "interval_covered", "strategy_used", "strategy_id",
                  "realised_cost_bps", "trade_outcome", "trade_return"):
        assert field in payload, field


def test_an_abstaining_forecast_carries_no_prediction_and_no_zero_error():
    with pytest.raises(ConfigurationError):
        pending(abstained=True, predicted_return=0.001)
    forecast = pending(abstained=True)
    evidence = forecast.mature(realised_return=0.02, now=forecast.matures_at)
    assert evidence.absolute_error is None, (
        "a declined forecast was scored as an answer")
    assert evidence.signed_error is None
    assert evidence.interval_covered is None
    assert evidence.directionally_correct is None
    # But the outcome is still recorded, which is what makes unnecessary
    # abstention measurable at all.
    assert evidence.realised_return == 0.02


def test_an_abstention_must_state_a_reason():
    with pytest.raises(ConfigurationError):
        pending(abstained=True, abstention_reasons=())


def test_a_single_forecast_has_no_calibration_error_only_coverage():
    forecast = pending()
    evidence = forecast.mature(realised_return=0.0015, now=forecast.matures_at)
    assert evidence.interval_covered is True
    assert not hasattr(evidence, "calibration_error")
    # The error is a property of a set, and carries its own sample size.
    assert EV.calibration_error([evidence]) is not None


def test_calibration_error_is_none_rather_than_zero_when_unmeasurable():
    forecast = pending(abstained=True)
    evidence = forecast.mature(realised_return=0.001, now=forecast.matures_at)
    assert EV.calibration_error([evidence]) is None
    assert EV.mean_absolute_error([evidence]) is None


def test_a_cost_overrun_is_none_when_either_side_is_unrecorded():
    forecast = pending()
    partial = forecast.mature(realised_return=0.001, now=forecast.matures_at,
                              realised_cost_bps=4.0)
    assert partial.cost_overrun_bps is None, (
        "'we did not measure it' became 'it matched'")
    full = pending(1).mature(realised_return=0.001,
                             now=pending(1).matures_at,
                             modelled_cost_bps=2.0, realised_cost_bps=4.0)
    assert full.cost_overrun_bps == pytest.approx(2.0)


def test_a_trade_outcome_without_strategy_usage_is_refused():
    forecast = pending()
    with pytest.raises(ConfigurationError):
        forecast.mature(realised_return=0.001, now=forecast.matures_at,
                        trade_outcome=EV.TradeOutcome.WIN)


def test_a_record_names_what_it_could_not_supply():
    forecast = pending()
    evidence = forecast.mature(realised_return=0.001, now=forecast.matures_at)
    missing = evidence.unmeasured_fields
    assert any("modelled_cost_bps" in m for m in missing)
    assert any("strategy usage" in m for m in missing)


def test_a_forecast_cannot_be_scored_twice():
    journal = EV.EvidenceJournal()
    forecast = pending()
    journal.record(forecast)
    journal.mature(forecast.forecast_id, realised_return=0.001,
                   now=forecast.matures_at)
    with pytest.raises(ConfigurationError):
        journal.record(forecast)


def test_the_journal_reports_what_is_due():
    journal = EV.EvidenceJournal()
    early, late = pending(0), pending(100)
    journal.record(early)
    journal.record(late)
    due = journal.due(early.matures_at)
    assert [f.forecast_id for f in due] == [early.forecast_id]


# ===========================================================================
# 2. the ten weaknesses
# ===========================================================================

def test_every_weakness_kind_is_described():
    assert set(EV.WEAKNESS_NOTES) == set(EV.WeaknessKind)
    assert len(EV.WeaknessKind) == 10
    assert all(note.strip() for note in EV.WEAKNESS_NOTES.values())


def test_a_regime_specific_weakness_is_found_with_its_measurement():
    journal = journal_of(160, noisy_regime=Regime.RANGING.value)
    findings = journal.weaknesses()
    regime = [f for f in findings if f.kind is EV.WeaknessKind.REGIME_WEAKNESS]
    assert regime, [f.kind.value for f in findings]
    finding = regime[0]
    assert finding.scope == f"regime={Regime.RANGING.value}"
    assert finding.measured > finding.threshold
    assert finding.n >= EV.MIN_SAMPLE and not finding.provisional
    assert finding.forecast_ids


def test_a_finding_below_the_sample_threshold_is_provisional_not_hidden():
    journal = EV.EvidenceJournal()
    for i in range(8):
        regime = Regime.CRISIS.value if i % 2 else Regime.TRENDING_UP.value
        record = pending(i, regime=regime, predicted_return=0.0)
        journal.record(record)
        journal.mature(record.forecast_id,
                       realised_return=(0.05 if i % 2 else 0.0001),
                       now=record.matures_at)
    findings = journal.weaknesses()
    assert findings, "a real difference on a small sample was suppressed"
    assert all(f.provisional for f in findings)
    report = journal.report()
    assert report["actionable"] == []
    assert report["provisional"]


def test_deterioration_compares_the_recent_half_with_the_earlier_half():
    journal = EV.EvidenceJournal()
    for i in range(80):
        record = pending(i, predicted_return=0.0)
        journal.record(record)
        journal.mature(record.forecast_id,
                       realised_return=0.0005 if i < 40 else 0.004,
                       now=record.matures_at)
    findings = [f for f in journal.weaknesses()
                if f.kind is EV.WeaknessKind.MODEL_DETERIORATION]
    assert findings
    assert findings[0].measured > 1.25
    assert findings[0].scope == "model=native-mt-1"


def test_unnecessary_abstention_is_measurable_only_because_declines_are_kept():
    journal = EV.EvidenceJournal()
    for i in range(60):
        declined = i % 2 == 0
        record = pending(i, abstained=declined,
                         predicted_return=None if declined else 0.0)
        journal.record(record)
        # Every decline is followed by a *smaller* move than the answered ones:
        # the model refused questions it could have answered.
        journal.mature(record.forecast_id,
                       realised_return=0.0001 if declined else 0.004,
                       now=record.matures_at)
    findings = [f for f in journal.weaknesses()
                if f.kind is EV.WeaknessKind.UNNECESSARY_ABSTENTION]
    assert findings, "declining before ordinary moves was not detected"
    assert findings[0].n == 30


def test_insufficient_abstention_looks_at_answered_rows_with_large_errors():
    journal = EV.EvidenceJournal()
    for i in range(60):
        record = pending(i, predicted_return=0.0)
        journal.record(record)
        journal.mature(record.forecast_id,
                       realised_return=0.05 if i % 3 == 0 else 0.0002,
                       now=record.matures_at)
    findings = [f for f in journal.weaknesses()
                if f.kind is EV.WeaknessKind.INSUFFICIENT_ABSTENTION]
    assert findings
    assert findings[0].measured > 0.2


def test_a_data_quality_failure_separates_degraded_inputs_from_clean_ones():
    journal = EV.EvidenceJournal()
    for i in range(80):
        degraded = i % 2 == 0
        record = pending(i, predicted_return=0.0,
                         data_quality=(DataQuality.DEGRADED if degraded
                                       else DataQuality.OK))
        journal.record(record)
        journal.mature(record.forecast_id,
                       realised_return=0.006 if degraded else 0.001,
                       now=record.matures_at)
    findings = [f for f in journal.weaknesses()
                if f.kind is EV.WeaknessKind.DATA_QUALITY_FAILURE]
    assert findings and findings[0].scope == "degraded_inputs"


def test_an_execution_model_error_is_found_from_cost_and_from_fills():
    journal = EV.EvidenceJournal()
    for i in range(60):
        record = pending(i, predicted_return=0.0)
        journal.record(record)
        journal.mature(record.forecast_id, realised_return=0.001,
                       now=record.matures_at, strategy_id="s1",
                       strategy_used=True, modelled_cost_bps=2.0,
                       realised_cost_bps=9.0,
                       trade_outcome=(EV.TradeOutcome.UNFILLED if i % 3 == 0
                                      else EV.TradeOutcome.WIN),
                       trade_return=0.001)
    findings = [f for f in journal.weaknesses()
                if f.kind is EV.WeaknessKind.EXECUTION_MODEL_ERROR]
    scopes = {f.scope for f in findings}
    assert scopes == {"costs", "fills"}


def test_a_healthy_journal_produces_no_actionable_weakness():
    """The counter-case. A detector that fires on everything is not a detector."""
    journal = journal_of(160, seed=9)
    actionable = [f for f in journal.weaknesses() if not f.provisional]
    assert actionable == [], [f.to_dict() for f in actionable]


def test_the_strata_report_covers_regime_instrument_and_timeframe():
    journal = journal_of(120, noisy_regime=Regime.RANGING.value)
    strata = journal.strata()
    assert "all" in strata
    assert any(k.startswith("regime=") for k in strata)
    assert any(k.startswith("instrument=") for k in strata)
    assert any(k.startswith("timeframe=") for k in strata)


# ===========================================================================
# 3. challenger proposals
# ===========================================================================

def a_finding(**overrides) -> EV.WeaknessFinding:
    kwargs = dict(kind=EV.WeaknessKind.REGIME_WEAKNESS, scope="regime=ranging",
                  detail="MAE 0.004 against 0.003 overall (1.4x)",
                  measured=1.4, threshold=1.3, n=120,
                  model_version="native-mt-1")
    kwargs.update(overrides)
    return EV.WeaknessFinding(**kwargs)


def a_proposal(**overrides) -> CH.ChallengerProposal:
    kwargs = dict(
        proposal_id="p1", kind=CH.ChallengerKind.CALIBRATION, created_at=START,
        hypothesis="a conformal step corrects coverage",
        supporting_evidence=("coverage error +13 points",),
        contradicting_evidence=(CH.NO_CONTRADICTION,),
        required_data=("ds-1",),
        experiment=CH.ExperimentDesign(description="fit and score",
                                       arms=("conformal",),
                                       baselines=("champion",)),
        success_criteria=("coverage error falls below 5 points",),
        failure_criteria=("coverage error does not fall below 5 points",),
        compute_budget=CH.ComputeBudget(),
        security_impact=CH.SecurityImpact.NONE,
        operational_impact=CH.OperationalImpact.RETRAIN_ONLY,
        rollback_plan="restore the prior checkpoint",
        rollback_target="native-mt-1")
    kwargs.update(overrides)
    return CH.ChallengerProposal(**kwargs)


def test_all_ten_challenger_kinds_exist_and_are_described():
    assert len(CH.ChallengerKind) == 10
    assert set(CH.KIND_HYPOTHESIS) == set(CH.ChallengerKind)
    assert set(CH.EXPERIMENT_KIND) == set(CH.ChallengerKind)


@pytest.mark.parametrize("field", [
    "supporting_evidence", "contradicting_evidence", "required_data",
    "success_criteria", "failure_criteria"])
def test_every_required_field_is_required(field):
    with pytest.raises(ConfigurationError):
        a_proposal(**{field: ()})


def test_a_proposal_with_no_rollback_plan_does_not_exist():
    with pytest.raises(ConfigurationError):
        a_proposal(rollback_plan="  ")


def test_a_proposal_with_operational_impact_must_name_a_restore_target():
    with pytest.raises(ConfigurationError):
        a_proposal(rollback_target="")
    # And one with no operational impact does not have to.
    assert a_proposal(operational_impact=CH.OperationalImpact.NONE,
                      rollback_target="").rollback_target == ""


def test_success_and_failure_criteria_must_be_distinguishable():
    with pytest.raises(ConfigurationError):
        a_proposal(success_criteria=("it works",), failure_criteria=("It  works",))


def test_a_challenger_may_not_carry_kernel_impact():
    with pytest.raises(ConfigurationError):
        a_proposal(security_impact=CH.SecurityImpact.KERNEL)


def test_the_compute_budget_is_a_set_of_hard_limits():
    with pytest.raises(ConfigurationError):
        CH.ComputeBudget(cpu_seconds=0)
    with pytest.raises(ConfigurationError):
        CH.ComputeBudget(cpu_seconds=60, wall_clock_seconds=30)
    budget = CH.ComputeBudget(cpu_seconds=30, wall_clock_seconds=90)
    assert CH.ComputeBudget.from_dict(budget.to_dict()) == budget


def test_an_experiment_design_needs_a_baseline_and_a_seed():
    with pytest.raises(ConfigurationError):
        CH.ExperimentDesign(description="d", arms=("a",), baselines=())
    with pytest.raises(ConfigurationError):
        CH.ExperimentDesign(description="d", arms=("a",), baselines=("a",))
    with pytest.raises(ConfigurationError):
        CH.ExperimentDesign(description="d", arms=("a",), baselines=("b",),
                            seeds=())


def test_a_generated_proposal_cites_the_measurement_it_came_from():
    proposal = CH.from_weakness(a_finding(), created_at=START)
    assert proposal.kind is CH.ChallengerKind.REGIME_SPECIALIST
    assert any("1.4" in item for item in proposal.supporting_evidence)
    assert proposal.triggered_by == ("regime_weakness:regime=ranging",)
    assert proposal.regimes == ("ranging",)
    assert proposal.rollback_target == "native-mt-1"


def test_a_provisional_finding_becomes_its_own_contradicting_evidence():
    proposal = CH.from_weakness(a_finding(n=5), created_at=START)
    assert any("below the" in item for item in proposal.contradicting_evidence)
    assert CH.NO_CONTRADICTION not in proposal.contradicting_evidence


def test_every_complexity_adding_challenger_is_paired_with_a_simplification():
    proposals = CH.generate([a_finding()], created_at=START)
    kinds = [p.kind for p in proposals]
    assert CH.ChallengerKind.REGIME_SPECIALIST in kinds
    assert CH.ChallengerKind.SIMPLER_MODEL in kinds
    simpler = next(p for p in proposals
                   if p.kind is CH.ChallengerKind.SIMPLER_MODEL)
    assert simpler.adds_complexity is False
    assert proposals[0].proposal_id in simpler.triggered_by


def test_generation_skips_provisional_findings_by_default():
    assert CH.generate([a_finding(n=4)], created_at=START) == []
    assert CH.generate([a_finding(n=4)], created_at=START,
                       include_provisional=True)


def test_a_data_source_challenger_needs_human_review():
    proposal = CH.from_weakness(
        a_finding(kind=EV.WeaknessKind.EXECUTION_MODEL_ERROR, scope="costs"),
        created_at=START)
    assert proposal.kind is CH.ChallengerKind.DATA_SOURCE
    assert proposal.security_impact is CH.SecurityImpact.NEW_EXTERNAL_INPUT
    assert proposal.autonomously_proposable is False
    assert proposal.needs_human_review is True


def test_a_proposal_round_trips_and_enters_the_framework_backlog():
    proposal = a_proposal()
    assert CH.ChallengerProposal.from_dict(proposal.to_dict()) == proposal
    research = proposal.to_research_proposal()
    assert research.proposal_id == proposal.proposal_id
    assert research.testable
    # The four native-only fields travel rather than being dropped.
    for fragment in ("compute", "security", "operational", "rollback"):
        assert fragment in research.notes


def test_two_identical_proposals_have_the_same_fingerprint():
    assert a_proposal().fingerprint() == a_proposal().fingerprint()
    assert a_proposal().fingerprint() != a_proposal(
        hypothesis="something else entirely").fingerprint()


# ===========================================================================
# 4. the twelve-stage gate
# ===========================================================================

def passing_checks(**overrides):
    checks = {stage: (lambda s=stage: (True, {"stage": s.value, "n": 100}))
              for stage in PR.AUTONOMOUS_STAGES}
    checks.update(overrides)
    return checks


def entered(ledger: PR.GateLedger, challenger_id: str = "c1"):
    return ledger.enter(challenger_id=challenger_id, proposal_id="p1",
                        created_at=START)


def test_there_are_twelve_stages_in_the_specified_order():
    assert len(PR.STAGE_ORDER) == 12
    assert [s.value for s in PR.STAGE_ORDER] == [
        "static_validation", "unit_tests", "leakage_tests",
        "historical_evaluation", "walk_forward_evaluation",
        "baseline_comparison", "robustness_tests", "calibration_tests",
        "paper_trading", "shadow_mode", "human_review",
        "restricted_promotion"]
    assert set(PR.STAGE_NOTES) == set(PR.GateStage)
    assert PR.AUTONOMOUS_STAGES.isdisjoint(PR.HUMAN_STAGES)


def test_a_stage_cannot_be_skipped():
    ledger = PR.GateLedger()
    entered(ledger)
    with pytest.raises(PR.PromotionRefused):
        ledger.advance("c1", PR.StageResult(
            stage=PR.GateStage.HISTORICAL_EVALUATION, passed=True, at=START,
            evidence={"n": 100}))


def test_a_stage_result_must_carry_evidence():
    with pytest.raises(ConfigurationError):
        PR.StageResult(stage=PR.GateStage.UNIT_TESTS, passed=True, at=START,
                       evidence={})


def test_a_missing_check_fails_the_stage_rather_than_passing_it():
    ledger = PR.GateLedger()
    entered(ledger)
    run = PR.run_autonomous_stages(ledger, "c1", checks={}, at=START)
    assert run.outcome is PR.GateOutcome.REJECTED
    assert run.failed_at.stage is PR.GateStage.STATIC_VALIDATION
    assert "nobody ran" in run.failed_at.evidence["reason"]


def test_a_challenger_that_passes_ten_stages_waits_for_a_human():
    ledger = PR.GateLedger()
    entered(ledger)
    run = PR.run_autonomous_stages(ledger, "c1", checks=passing_checks(),
                                   at=START)
    assert run.outcome is PR.GateOutcome.AWAITING_REVIEW
    assert run.next_stage is PR.GateStage.HUMAN_REVIEW
    assert len(run.passed_stages) == 10
    assert ledger.awaiting_review == ("c1",)


def test_olympus_cannot_advance_a_human_stage():
    ledger = PR.GateLedger()
    entered(ledger)
    PR.run_autonomous_stages(ledger, "c1", checks=passing_checks(), at=START)
    for actor in (None, Actor.autonomous("engine"), Actor.system("cron")):
        with pytest.raises((PR.PromotionRefused, GovernanceViolation)):
            ledger.advance("c1", PR.StageResult(
                stage=PR.GateStage.HUMAN_REVIEW, passed=True, at=START,
                evidence={"reviewed": True}), actor=actor)


#: A promotion now needs every field a reviewer must have answered, and a
#: durable place to write the record. Both are the point of the fail-closed
#: commit, so the helpers are named rather than inlined at each call site.
REVIEW_EVIDENCE = {
    "model_version": "native-0.1.0+test",
    "evaluation_report": "docs/OLYMPUS_VS_KRONOS.md#matched",
    "reviewed_by": "alice",
    "review_notes": "read the matched evaluation and the robustness suite",
    "rollback_plan": "revert to the incumbent and rerun shadow mode",
}


def durable_ledger(tmp_path, **kwargs):
    """A gate whose positive decisions have somewhere durable to land."""
    return PR.GateLedger(audit_log=PR.AuditLog(tmp_path / "audit.jsonl"),
                         state=PR.StateStore(tmp_path / "state.json"),
                         **kwargs)


def test_olympus_cannot_promote_and_there_is_no_force(tmp_path):
    import inspect
    ledger = durable_ledger(tmp_path)
    entered(ledger)
    PR.run_autonomous_stages(ledger, "c1", checks=passing_checks(), at=START)
    operator = Actor.operator("alice", token="tok")
    ledger.advance("c1", PR.StageResult(
        stage=PR.GateStage.HUMAN_REVIEW, passed=True, at=START,
        evidence={"reviewer": "alice"}, actor="alice"), actor=operator)

    restriction = PR.default_restriction(instruments=(KEY,), max_notional=1000.0,
                                         at=START)
    with pytest.raises(GovernanceViolation):
        ledger.promote("c1", actor=Actor.autonomous("engine"), at=START,
                       restriction=restriction, evidence=REVIEW_EVIDENCE)
    signature = inspect.signature(ledger.promote)
    assert not any(name in signature.parameters
                   for name in ("force", "skip_review", "override"))
    promoted = ledger.promote("c1", actor=operator, at=START,
                              restriction=restriction, evidence=REVIEW_EVIDENCE)
    assert promoted.outcome is PR.GateOutcome.PROMOTED


def test_a_promotion_is_restricted_and_expires(tmp_path):
    ledger = durable_ledger(tmp_path)
    entered(ledger)
    PR.run_autonomous_stages(ledger, "c1", checks=passing_checks(), at=START)
    operator = Actor.operator("alice", token="tok")
    ledger.advance("c1", PR.StageResult(
        stage=PR.GateStage.HUMAN_REVIEW, passed=True, at=START,
        evidence={"reviewer": "alice"}, actor="alice"), actor=operator)
    run = ledger.promote("c1", actor=operator, at=START,
                         restriction=PR.default_restriction(
                             instruments=(KEY,), max_notional=500.0, at=START),
                         evidence=REVIEW_EVIDENCE)
    assert run.active_at(START) is True
    assert run.active_at(START + timedelta(days=31)) is False, (
        "an approval outlived its expiry")
    assert run.restriction.covers(KEY)
    assert not run.restriction.covers("SIM:OTHER")


def test_a_restriction_must_be_a_restriction():
    with pytest.raises(ConfigurationError):
        PR.Restriction(instruments=(), max_notional=1.0,
                       expires_at=START + timedelta(days=1),
                       review_at=START, widening_criteria=("x",))
    with pytest.raises(ConfigurationError):
        PR.Restriction(instruments=(KEY,), max_notional=1.0,
                       expires_at=START + timedelta(days=1),
                       review_at=START, widening_criteria=())
    with pytest.raises(ConfigurationError):
        PR.Restriction(instruments=(KEY,), max_notional=1.0,
                       expires_at=START + timedelta(days=1),
                       review_at=START + timedelta(days=2),
                       widening_criteria=("x",))


def test_a_rejection_is_terminal():
    ledger = PR.GateLedger()
    entered(ledger)
    checks = passing_checks(**{PR.GateStage.LEAKAGE_TESTS: lambda: (
        False, {"reason": "the target is inside the input window"})})
    run = PR.run_autonomous_stages(ledger, "c1", checks=checks, at=START)
    assert run.outcome is PR.GateOutcome.REJECTED
    assert run.terminal
    with pytest.raises(PR.PromotionRefused):
        ledger.advance("c1", PR.StageResult(
            stage=PR.GateStage.HISTORICAL_EVALUATION, passed=True, at=START,
            evidence={"n": 1}))


def test_a_challenger_cannot_re_enter_for_a_second_first_attempt():
    ledger = PR.GateLedger()
    entered(ledger)
    with pytest.raises(PR.PromotionRefused):
        entered(ledger)


def test_a_failed_stage_cannot_be_removed_from_the_record():
    ledger = PR.GateLedger()
    entered(ledger)
    checks = passing_checks(**{PR.GateStage.UNIT_TESTS: lambda: (
        False, {"reason": "two adversarial tests fail"})})
    run = PR.run_autonomous_stages(ledger, "c1", checks=checks, at=START)
    assert run.failed_at is not None
    assert not hasattr(run, "results_setter")
    # A run reconstructed with the failure removed does not describe this gate.
    with pytest.raises(ConfigurationError):
        PR.ChallengerRun(challenger_id="c1", proposal_id="p1",
                         created_at=START,
                         results=(run.results[0], run.results[0]))
    assert ledger.concealment_check() == ()


def test_the_concealment_check_reads_the_stored_objects_independently():
    ledger = PR.GateLedger()
    entered(ledger)
    run = PR.run_autonomous_stages(ledger, "c1", checks=passing_checks(),
                                   at=START)
    assert ledger.concealment_check() == ()
    # A non-prefix shape is refused at construction, so the only malformed
    # ledger that can exist is one with more than one failure — which is what
    # a removed *intermediate* failure would leave behind. The two checks are
    # deliberately redundant: construction catches it when the object is built
    # here, and `concealment_check` catches it when the object arrived from
    # storage that this process did not write.
    with pytest.raises(ConfigurationError):
        PR.ChallengerRun(
            challenger_id="c1", proposal_id="p1", created_at=START,
            results=(run.results[0], run.results[2]))     # a stage removed

    two_failures = run.results[:2] + tuple(
        PR.StageResult(stage=stage, passed=False, at=START,
                       evidence={"reason": "failed"})
        for stage in (PR.GateStage.LEAKAGE_TESTS,
                      PR.GateStage.HISTORICAL_EVALUATION))
    ledger._runs["c1"] = PR.ChallengerRun(
        challenger_id="c1", proposal_id="p1", created_at=START,
        results=two_failures)
    findings = ledger.concealment_check()
    assert findings and any("failure is terminal" in f for f in findings)


def test_the_gate_report_names_who_may_do_what():
    ledger = PR.GateLedger()
    report = ledger.report()
    assert len(report["autonomous_stages"]) == 10
    assert report["human_stages"] == ["human_review", "restricted_promotion"]
    assert report["concealment_findings"] == []


# ===========================================================================
# 5. improvement metrics
# ===========================================================================

def test_all_twelve_metrics_have_a_direction_and_a_definition():
    assert len(IM.NativeMetric) == 12
    assert set(IM.METRIC_DIRECTION) == set(IM.NativeMetric)
    assert set(IM.METRIC_NOTES) == set(IM.NativeMetric)


@pytest.mark.parametrize("name", sorted(IM.FORBIDDEN_PROGRESS_METRICS))
def test_a_volume_metric_is_refused_rather_than_ignored(name):
    with pytest.raises(ConfigurationError):
        IM.assert_not_volume_metric(name)
    with pytest.raises(ConfigurationError):
        IM.measure({name: 1}, {name: 100})


def test_the_verdict_starts_unproven():
    assert IM.measure({}, {}).verdict is IM.Verdict.UNPROVEN
    assert IM.ImprovementReport().verdict is IM.Verdict.UNPROVEN


def test_one_regression_prevents_an_improving_verdict():
    counts = {m.value: (100, 100) for m in IM.NativeMetric}
    before = {"forecast_error": 0.005, "calibration_gap": 12.0,
              "execution_cost": 4.0, "failure_rate": 0.02}
    better = {"forecast_error": 0.004, "calibration_gap": 8.0,
              "execution_cost": 3.0, "failure_rate": 0.01}
    assert IM.measure(before, better, counts=counts).verdict is IM.Verdict.IMPROVING
    mixed = {**better, "failure_rate": 0.09}
    report = IM.measure(before, mixed, counts=counts)
    assert report.verdict is IM.Verdict.NOT_IMPROVING
    assert report.regressed == ("failure_rate",)


def test_a_thin_sample_leaves_the_verdict_unproven():
    counts = {m.value: (5, 5) for m in IM.NativeMetric}
    report = IM.measure({"forecast_error": 0.005}, {"forecast_error": 0.001},
                        counts=counts)
    assert report.improved == ("forecast_error",)
    assert report.verdict is IM.Verdict.UNPROVEN


def test_unmeasured_metrics_are_listed_not_dropped():
    report = IM.measure({"forecast_error": 0.005}, {"forecast_error": 0.004})
    assert "robustness_score" in report.unmeasured
    assert "rollback_seconds" in report.unmeasured
    assert len(report.unmeasured) == 11


def test_abstention_quality_is_selectivity_not_rate():
    """A model that declines everything must not score well."""
    selective = EV.EvidenceJournal()
    indiscriminate = EV.EvidenceJournal()
    for i in range(40):
        big = i % 2 == 0
        record = pending(i, abstained=big, predicted_return=None if big else 0.0)
        selective.record(record)
        selective.mature(record.forecast_id,
                         realised_return=0.05 if big else 0.001,
                         now=record.matures_at)
    for i in range(40):
        record = pending(i, abstained=True)
        indiscriminate.record(record)
        indiscriminate.mature(record.forecast_id, realised_return=0.001,
                              now=record.matures_at)

    assert IM.abstention_quality(selective.matured) > 5.0
    assert IM.abstention_quality(indiscriminate.matured) is None, (
        "declining everything produced a score instead of no score")


def test_drawdown_and_risk_adjusted_return_are_none_when_nothing_traded():
    journal = journal_of(60)
    assert IM.max_drawdown(journal.matured) is None
    assert IM.risk_adjusted_return(journal.matured) is None


def test_unseen_generalisation_reports_both_halves():
    journal = EV.EvidenceJournal()
    for i in range(60):
        new_instrument = i % 3 == 0
        record = pending(i, predicted_return=0.0,
                         instrument_key="SIM:NEW" if new_instrument else KEY,
                         regime=Regime.TRENDING_UP.value)
        journal.record(record)
        journal.mature(record.forecast_id,
                       realised_return=0.006 if new_instrument else 0.001,
                       now=record.matures_at)
    ratio, detail = IM.unseen_generalisation(
        journal.matured, trained_instruments=(KEY,),
        trained_regimes=(Regime.TRENDING_UP.value,))
    assert ratio is not None and ratio < 1.0
    assert detail["n_unseen_instrument"] == 20
    assert detail["unseen_instrument_mae"] > detail["seen_mae"]
    assert "read the halves" in detail["note"]


def test_native_metrics_translate_into_the_framework_names():
    counts = {m.value: (100, 100) for m in IM.NativeMetric}
    report = IM.measure({"forecast_error": 0.005, "execution_cost": 4.0},
                        {"forecast_error": 0.004, "execution_cost": 3.0},
                        counts=counts)
    translated = IM.to_framework_metrics(report)
    from olympus.trading.evolution import IMPROVEMENT_METRICS
    for name in translated["before"]:
        assert name in IMPROVEMENT_METRICS
    assert "slippage" in translated["after"]
    assert "simpler_at_equal_performance" in translated["native_only"]


# ===========================================================================
# 6. the two required demonstrations
# ===========================================================================

def test_demonstration_a_evidence_to_weakness_to_hypothesis_to_rejection():
    """new evidence -> weakness detected -> hypothesis created -> experiment
    isolated -> challenger trained -> challenger evaluated -> weak challenger
    rejected."""
    from olympus.trading.native import isolation as ISO

    # -- new evidence ---------------------------------------------------
    journal = journal_of(200, noisy_regime=Regime.RANGING.value)
    assert len(journal) == 200

    # -- weakness detected ----------------------------------------------
    findings = [f for f in journal.weaknesses() if not f.provisional]
    assert findings, "no actionable weakness in a journal built to contain one"

    # -- hypothesis created ---------------------------------------------
    proposals = CH.generate(findings, created_at=START, dataset_ids=("ds-1",))
    assert proposals
    proposal = proposals[0]
    assert proposal.falsifiable
    assert proposal.contradicting_evidence

    # -- experiment isolated, challenger trained ------------------------
    source = """
import statistics
def run(inputs):
    values = [float(v) for v in inputs['series']]
    # A deliberately useless challenger: it predicts the mean of the series,
    # which on a zero-mean series is what persistence already does.
    mean = statistics.fmean(values)
    challenger = [abs(mean - v) for v in values]
    persistence = [abs(0.0 - v) for v in values]
    return {'challenger_mae': statistics.fmean(challenger),
            'persistence_mae': statistics.fmean(persistence),
            'parameters': 1, 'baseline_parameters': 0,
            'n': len(values)}
"""
    rng = random.Random(4)
    spec = ISO.ExperimentSpec(
        experiment_id=proposal.proposal_id, source=source,
        inputs={"series": [rng.gauss(0.0, 0.003) for _ in range(200)]},
        budget=CH.ComputeBudget(cpu_seconds=10, wall_clock_seconds=30,
                                memory_mb=384))
    manifest = ISO.run_isolated(spec)
    assert manifest.verdict is ISO.Verdict.COMPLETED
    assert manifest.trustworthy
    assert manifest.confinement.adequate_for_generated_code
    assert manifest.destruction["workdir_removed"]

    # -- challenger evaluated -------------------------------------------
    ledger = PR.GateLedger()
    ledger.enter(challenger_id="ch-demo", proposal_id=proposal.proposal_id,
                 created_at=START)
    result = manifest.result
    beat = result["challenger_mae"] < result["persistence_mae"] * 0.95
    checks = passing_checks(**{PR.GateStage.BASELINE_COMPARISON: lambda: (
        beat, {"reason": f"challenger MAE {result['challenger_mae']:.6g} "
                         f"against persistence {result['persistence_mae']:.6g}; "
                         f"{result['parameters']} parameters against "
                         f"{result['baseline_parameters']}",
               "challenger_mae": result["challenger_mae"],
               "persistence_mae": result["persistence_mae"],
               "n": result["n"]})})
    run = PR.run_autonomous_stages(ledger, "ch-demo", checks=checks, at=START)

    # -- weak challenger rejected ---------------------------------------
    assert run.outcome is PR.GateOutcome.REJECTED
    assert run.failed_at.stage is PR.GateStage.BASELINE_COMPARISON
    assert run.terminal
    assert "persistence" in run.rejection_reason
    # Rejecting is autonomous: no operator was involved anywhere above.
    assert ledger.promoted == ()
    assert ledger.concealment_check() == ()


def test_demonstration_b_deterioration_to_restriction_to_restore_to_audit():
    """deterioration detected -> active model restricted -> previously approved
    model restored -> audit trail verified."""
    from olympus.trading import drift as DR
    from olympus.trading import rollback as RB
    from olympus.trading.audit import AuditTrail
    from olympus.trading.clock import FixedClock

    run_id = uuid.uuid4().hex[:8]
    clock = FixedClock(START)
    audit = AuditTrail(f"phase4-demo-{run_id}", clock=clock, ledger_backed=True)
    operator = Actor.operator("alice", token="tok")

    ledger = RB.DeploymentLedger(clock=clock, audit=audit,
                                 namespace=f"t.{run_id}.deployments")
    common = dict(capability_id="native-forecaster", actor=operator,
                  config={"lookback": 33, "horizon": 3},
                  dependency_versions={"python": "3.11"},
                  evidence_refs=("eval-1",),
                  rollback_procedure="restore the prior checkpoint id and "
                                     "re-run the evaluation")
    ledger.deploy(deployment_id=f"{run_id}-1", version="native-mt-1", **common)
    clock.set(START + timedelta(days=1))
    ledger.deploy(deployment_id=f"{run_id}-2", version="native-mt-2", **common)
    assert ledger.active("native-forecaster").version == "native-mt-2"

    # -- deterioration detected, from measurement -----------------------
    reference = [0.002 + 0.0001 * (i % 7) for i in range(60)]
    current = [0.008 + 0.0002 * (i % 7) for i in range(60)]
    signal = DR.detect_metric_regression(
        reference, current, kind=DR.DriftKind.MODEL,
        subject="native-forecaster", metric="forecast_error",
        lower_is_better=True)
    assert signal.drifted and signal.severity is DR.Severity.CRITICAL

    monitor = DR.DeteriorationMonitor(clock=clock, audit=audit,
                                      namespace=f"t.{run_id}.drift")
    health = monitor.evaluate("native-forecaster", kind="model",
                              signals=[signal])
    assert health.state is not DR.ComponentState.HEALTHY

    # -- active model restricted, autonomously --------------------------
    restricted = monitor.restrict("native-forecaster", kind="model",
                                  to=DR.ComponentState.DISABLED,
                                  reason="forecast error quadrupled")
    assert restricted.state is DR.ComponentState.DISABLED
    assert restricted.changed_by == "drift-monitor"

    # ... and cannot un-restrict itself.
    with pytest.raises(GovernanceViolation):
        monitor.reinstate("native-forecaster", kind="model",
                          to=DR.ComponentState.HEALTHY, reason="looks better",
                          actor=Actor.autonomous("evolution-engine"))

    # -- previously approved model restored -----------------------------
    metrics = {"forecast_error_increase": 3.0, "max_drawdown": 0.22}
    triggers = RB.evaluate_triggers(metrics)
    assert triggers
    unmeasured = RB.unmeasured_triggers(metrics)
    assert unmeasured, "a partial rollback decision did not know it was partial"

    rolled = ledger.rollback("native-forecaster",
                             reason="forecast error quadrupled",
                             triggers=triggers)
    assert rolled.version == "native-mt-1"
    assert ledger.active("native-forecaster").version == "native-mt-1"
    # The destination is a version a human deployed, which is what makes the
    # rollback safe to do autonomously.
    assert rolled.deployed_by == "alice"

    # -- audit trail verified -------------------------------------------
    verification = audit.verify()
    assert verification["ok"] is True
    assert verification["verified"] == verification["count"] > 0
    assert verification["problems"] == []
    kinds = {event.type.value for event in audit.events()}
    assert "rollback" in kinds
    assert "drift_detected" in kinds
    history = ledger.history("native-forecaster")
    assert [r.state.value for r in history] == ["active", "rolled_back"]


def test_the_demonstration_script_runs_both_end_to_end():
    """The script is the artefact a reader runs; if it drifts from the tests it
    stops being evidence."""
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "native_evolution_demo.py"
    assert script.is_file()
    completed = subprocess.run([sys.executable, str(script)],
                               capture_output=True, text=True, timeout=600)
    assert completed.returncode == 0, completed.stderr[-3000:]
    assert "weak challenger rejected      True" in completed.stdout
    assert "audit chain verified          True" in completed.stdout
    assert "No real money was used" in completed.stdout

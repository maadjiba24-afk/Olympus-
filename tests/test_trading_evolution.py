"""The evolution loop, its audit trail and its metrics — completion gate 12.

Gate 12: produce a complete audit trail for every evolutionary change.

The seven questions the specification requires an answer to are tested one by
one. Two of them are tested for *refusing* to answer — "did it improve
performance" must return `None` before there is post-change measurement, and an
`ImprovementReport` must sit at UNPROVEN until enough trading metrics were
measured in both periods.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from olympus.trading import drift as D
from olympus.trading import evolution as E
from olympus.trading import hypotheses as H
from olympus.trading import knowledge as KB
from olympus.trading import outcomes as O
from olympus.trading.clock import FixedClock
from olympus.trading.contracts import Regime, Side
from olympus.trading.errors import ConfigurationError

T0 = datetime(2026, 12, 1, tzinfo=timezone.utc)
KEY = "BINANCE:BTCUSDT"


@pytest.fixture()
def ledger():
    return E.EvolutionLedger(clock=FixedClock(T0))


def event(event_id="e1", **kwargs):
    kwargs.setdefault("stage", E.EvolutionStage.RESTRICTION)
    return E.EvolutionEvent(event_id=event_id, at=T0, **kwargs)


# --- gate 12: the eleven recorded fields ------------------------------------

def test_an_event_records_every_field_the_specification_requires():
    data = event(observed="drift in kronos outputs",
                 inferred="the checkpoint or the market changed",
                 proposed="refit against Q4 data",
                 evidence=("drift:model:kronos:mean",),
                 experiment="exp-1", results={"psi": 0.4},
                 reviewed_by="alice", decision=E.ReviewDecision.ACCEPTED,
                 code_version="abc123", model_versions={"kronos": "rev-9"},
                 config_version="cfg-3", deployment_id="d2",
                 deployment_state="active", rollback_target="d1").to_dict()
    for field in ("observed", "inferred", "proposed", "evidence", "experiment",
                  "results", "reviewed_by", "decision", "code_version",
                  "model_versions", "config_version", "deployment_id",
                  "deployment_state", "rollback_target"):
        assert field in data, field


def test_a_promotion_event_must_name_its_reviewer():
    """An unattributed promotion cannot be audited."""
    with pytest.raises(ConfigurationError):
        event(stage=E.EvolutionStage.PROMOTION, reviewed_by="",
              decision=E.ReviewDecision.ACCEPTED)
    with pytest.raises(ConfigurationError):
        event(stage=E.EvolutionStage.DEPLOYMENT, reviewed_by="alice",
              decision=E.ReviewDecision.PENDING)


def test_a_restriction_event_needs_no_reviewer():
    """Demoting a deteriorating component is autonomous by design."""
    assert event(stage=E.EvolutionStage.RESTRICTION).reviewed_by == ""


def test_the_ledger_is_append_only(ledger):
    """A self-evolving system that could revise its own history of changing
    could not be audited at all."""
    ledger.record(event())
    with pytest.raises(ConfigurationError) as exc:
        ledger.record(event(observed="a different story"))
    assert "append a correcting event" in str(exc.value)


def test_the_ledger_has_no_update_or_delete(ledger):
    for banned in ("update", "delete", "remove", "purge", "amend", "truncate"):
        assert not hasattr(ledger, banned), banned


def test_events_are_ordered_and_filterable(ledger):
    clock = FixedClock(T0)
    ledger = E.EvolutionLedger(clock=clock)
    ledger.record(event("e1", stage=E.EvolutionStage.OBSERVATION, subject="a"))
    ledger.record(E.EvolutionEvent(event_id="e2", at=T0 + timedelta(hours=1),
                                   stage=E.EvolutionStage.HYPOTHESIS,
                                   subject="b"))
    assert [e.event_id for e in ledger.events()] == ["e1", "e2"]
    assert [e.event_id for e in
            ledger.events(stage=E.EvolutionStage.HYPOTHESIS)] == ["e2"]
    assert [e.event_id for e in ledger.events(subject="a")] == ["e1"]


def test_unattributed_promotions_are_findable_even_if_impossible(ledger):
    """A record restored from a backup should still be caught."""
    assert ledger.unattributed_promotions() == []


def test_events_persist_across_ledger_instances(ledger):
    ledger.record(event())
    fresh = E.EvolutionLedger(clock=FixedClock(T0))
    assert fresh.get("e1") is not None


# --- the seven questions ----------------------------------------------------

def test_explain_answers_why_what_evidence_and_who(ledger):
    ledger.record(event(observed="forecast error doubled on BTCUSDT",
                        evidence=("outcome-journal:instrument:BINANCE:BTCUSDT",),
                        reviewed_by="alice", code_version="abc123",
                        rollback_target="d1"))
    explanation = ledger.explain("e1")
    assert "forecast error doubled" in explanation.why
    assert explanation.evidence == ("outcome-journal:instrument:BINANCE:BTCUSDT",)
    assert explanation.authorised_by == "alice"
    assert explanation.active_version == "abc123"


def test_explain_answers_whether_the_previous_version_can_be_restored(ledger):
    ledger.record(event("with-target", rollback_target="d1"))
    ledger.record(event("without-target"))
    assert ledger.explain("with-target").restorable is True
    assert ledger.explain("without-target").restorable is False


def test_an_autonomous_event_reports_no_human_authoriser(ledger):
    ledger.record(event(actor="evolution-cycle", actor_kind="autonomous"))
    assert ledger.explain("e1").authorised_by == ""


def test_improvement_is_unanswerable_before_there_is_measurement(ledger):
    """Inventing an answer here is how a self-evolution report becomes a press
    release."""
    ledger.record(event())
    explanation = ledger.explain("e1")
    assert explanation.improved_performance is None
    assert "not yet known" in explanation.performance_detail


def test_improvement_is_judged_only_from_supplied_measurements(ledger):
    """The event's own record of how well it did is not evidence about how well
    it did."""
    ledger.record(event(results={"risk_adjusted_return": 99.0}))
    explanation = ledger.explain(
        "e1",
        performance_before={"risk_adjusted_return": 1.0, "max_drawdown": 0.2},
        performance_after={"risk_adjusted_return": 1.4, "max_drawdown": 0.1},
        risk_dimensions=())
    assert explanation.improved_performance is True
    assert "2 of 2" in explanation.performance_detail
    assert explanation.new_risks == ()


def test_a_falling_drawdown_is_an_improvement_not_a_regression(ledger):
    """Direction is read from the metric table, never assumed: a comparison
    that treated every metric as higher-is-better would report the wrong sign
    for half of them."""
    ledger.record(event())
    explanation = ledger.explain(
        "e1", performance_before={"max_drawdown": 0.4, "slippage": 8.0},
        performance_after={"max_drawdown": 0.1, "slippage": 5.0},
        risk_dimensions=())
    assert explanation.improved_performance is True
    assert explanation.new_risks == ()


def test_a_regression_is_reported_as_a_new_risk(ledger):
    ledger.record(event())
    explanation = ledger.explain(
        "e1",
        performance_before={"risk_adjusted_return": 1.0, "max_drawdown": 0.1,
                            "slippage": 5.0},
        performance_after={"risk_adjusted_return": 1.4, "max_drawdown": 0.3,
                           "slippage": 4.0},
        risk_dimensions=())
    assert explanation.improved_performance is True
    assert explanation.new_risks == ("regression in max_drawdown",)


def test_a_metric_with_no_declared_direction_is_not_judged(ledger):
    """Silently picking a direction for an unknown metric is how a report
    invents a verdict."""
    ledger.record(event())
    explanation = ledger.explain("e1", performance_before={"mystery": 1.0},
                                 performance_after={"mystery": 2.0},
                                 risk_dimensions=())
    assert explanation.improved_performance is None
    assert "not decidable" in explanation.performance_detail
    assert "mystery" in explanation.performance_detail


def test_unmeasured_risk_dimensions_are_listed_as_new_risks(ledger):
    """The risks a change created are not knowable from the change alone."""
    ledger.record(event())
    explanation = ledger.explain(
        "e1", performance_before={"risk_adjusted_return": 1.0},
        performance_after={"risk_adjusted_return": 1.2})
    assert any("leverage unmeasured" in r for r in explanation.new_risks)
    assert any("concentration unmeasured" in r for r in explanation.new_risks)


def test_incomparable_measurements_are_reported_as_such(ledger):
    ledger.record(event())
    explanation = ledger.explain("e1", performance_before={"a": 1.0},
                                 performance_after={"b": 2.0})
    assert explanation.improved_performance is None
    assert "share no metric" in explanation.performance_detail


def test_explaining_an_unknown_event_is_refused(ledger):
    with pytest.raises(ConfigurationError):
        ledger.explain("nope")


# --- is it actually improving? ----------------------------------------------

def test_the_fifteen_metrics_are_all_named():
    assert len(E.IMPROVEMENT_METRICS) == 15
    for metric in ("forecast_error", "calibration_gap", "abstention_quality",
                   "max_drawdown", "risk_adjusted_return", "slippage",
                   "operational_failures", "incident_detection_seconds",
                   "recovery_seconds", "false_signal_rate", "data_quality",
                   "unseen_regime_performance", "validated_capabilities_added",
                   "unsafe_proposals_rejected",
                   "deteriorating_models_restricted"):
        assert metric in E.IMPROVEMENT_METRICS


def test_with_no_measurement_the_verdict_is_unproven():
    report = E.measure_improvement({}, {})
    assert report.verdict is E.ImprovementVerdict.UNPROVEN
    assert len(report.unmeasured) == 15


def test_too_few_measured_metrics_stay_unproven():
    before = {"forecast_error": 1.0, "slippage": 5.0}
    after = {"forecast_error": 0.5, "slippage": 3.0}
    report = E.measure_improvement(before, after, min_metrics=5)
    assert report.verdict is E.ImprovementVerdict.UNPROVEN
    assert "at least 5" in report.rationale


def test_learning_more_is_not_improvement():
    """Governance counters alone can never produce an IMPROVING verdict — else
    Olympus could look better by generating more bad ideas to reject."""
    before = {"validated_capabilities_added": 0, "unsafe_proposals_rejected": 0,
              "deteriorating_models_restricted": 0}
    after = {"validated_capabilities_added": 9, "unsafe_proposals_rejected": 40,
             "deteriorating_models_restricted": 7}
    report = E.measure_improvement(before, after)
    assert report.verdict is E.ImprovementVerdict.UNPROVEN
    assert report.trading_changes == ()


def test_genuine_improvement_across_the_trading_metrics_is_recognised():
    before = {"forecast_error": 1.0, "calibration_gap": 0.3, "slippage": 8.0,
              "max_drawdown": 0.25, "risk_adjusted_return": 0.4,
              "false_signal_rate": 0.5}
    after = {"forecast_error": 0.7, "calibration_gap": 0.1, "slippage": 5.0,
             "max_drawdown": 0.18, "risk_adjusted_return": 0.8,
             "false_signal_rate": 0.3}
    report = E.measure_improvement(before, after)
    assert report.verdict is E.ImprovementVerdict.IMPROVING
    assert set(report.regressed) == set()


def test_mixed_results_below_the_threshold_are_not_improving():
    before = {"forecast_error": 1.0, "calibration_gap": 0.1, "slippage": 5.0,
              "max_drawdown": 0.1, "risk_adjusted_return": 0.8,
              "false_signal_rate": 0.2}
    after = {"forecast_error": 0.9, "calibration_gap": 0.3, "slippage": 9.0,
             "max_drawdown": 0.3, "risk_adjusted_return": 0.4,
             "false_signal_rate": 0.5}
    report = E.measure_improvement(before, after)
    assert report.verdict is E.ImprovementVerdict.NOT_IMPROVING
    assert len(report.regressed) >= 4
    assert "learning more is not improvement" in report.rationale


def test_an_unchanged_metric_does_not_count_as_improved():
    change = E.MetricChange(name="slippage", before=5.0, after=5.0,
                            direction="lower")
    assert change.improved is False


def test_direction_is_respected_per_metric():
    lower = E.MetricChange(name="slippage", before=5.0, after=3.0,
                           direction="lower")
    higher = E.MetricChange(name="data_quality", before=0.9, after=0.95,
                            direction="higher")
    assert lower.improved is True and higher.improved is True


def test_the_report_lists_what_it_could_not_measure():
    report = E.measure_improvement({"forecast_error": 1.0},
                                   {"forecast_error": 0.5})
    assert "slippage" in report.unmeasured
    assert "forecast_error" not in report.unmeasured


# --- the cycle --------------------------------------------------------------

def _resolved_journal(clock, n=40):
    journal = O.OutcomeJournal(clock=clock)
    for i in range(n):
        journal.open(O.DecisionRecord(
            decision_id=f"d{i}", ts=T0, instrument_key=KEY, timeframe="1h",
            strategy_id="momentum", side=Side.BUY, authorised_quantity="1",
            entry_reference_price=100.0, stop_price=98.0,
            regime=Regime.TRENDING_UP,
            forecast_paths=((101.0, 102.0), (101.5, 103.0))))
        journal.resolve(O.Outcome(
            decision_id=f"d{i}", resolved_at=T0 + timedelta(hours=2),
            realised_path=(99.0, 97.0), entry_price=100.0, exit_price=97.0,
            filled_quantity="1", gross_pnl=-3.0, mfe=0.0, mae=3.0))
    return journal


def test_a_full_cycle_scores_learns_restricts_and_asks(ledger):
    clock = FixedClock(T0)
    journal = _resolved_journal(clock)
    engine = H.HypothesisEngine(clock=clock)
    monitor = D.DeteriorationMonitor(clock=clock)
    base = KB.KnowledgeBase(clock=clock)
    signal = D.DriftSignal(kind=D.DriftKind.MODEL, subject="kronos",
                           metric="mean", severity=D.Severity.CRITICAL,
                           detail="PSI 0.9")

    cycle = E.EvolutionCycle(clock=clock, journal=journal, monitor=monitor,
                             hypothesis_engine=engine, knowledge_base=base,
                             ledger=E.EvolutionLedger(clock=clock))
    report = cycle.run(drift_signals=[signal],
                       component_kinds={"kronos": "model"})

    assert report.resolved_decisions == 40
    assert report.weaknesses, "a systematically wrong strategy must be noticed"
    assert report.hypotheses, "a weakness must become a question"
    assert any("kronos" in r for r in report.restricted)
    assert report.knowledge_recorded
    assert report.errors == ()


def test_the_cycle_restricts_but_never_promotes():
    """The strongest action a cycle takes is demoting a component."""
    clock = FixedClock(T0)
    monitor = D.DeteriorationMonitor(clock=clock)
    cycle = E.EvolutionCycle(clock=clock, monitor=monitor,
                             ledger=E.EvolutionLedger(clock=clock))
    signal = D.DriftSignal(kind=D.DriftKind.MODEL, subject="kronos",
                           metric="mean", severity=D.Severity.CRITICAL,
                           detail="d")
    cycle.run(drift_signals=[signal])
    assert monitor.health("kronos").state is D.ComponentState.SHADOW
    for banned in ("promote", "deploy", "enable_live", "install"):
        assert not hasattr(cycle, banned)


def test_a_failing_subsystem_is_collected_not_raised():
    """A scheduler that simply retries a crash learns nothing; an operator
    reading `errors` does."""
    class Exploding:
        def resolved(self):
            raise RuntimeError("store on fire")

        def weaknesses(self):
            raise RuntimeError("store on fire")

    clock = FixedClock(T0)
    cycle = E.EvolutionCycle(clock=clock, journal=Exploding(),
                             ledger=E.EvolutionLedger(clock=clock))
    report = cycle.run()
    assert report.errors and "outcome scoring failed" in report.errors[0]


def test_a_cycle_with_nothing_configured_still_runs():
    clock = FixedClock(T0)
    report = E.EvolutionCycle(clock=clock,
                              ledger=E.EvolutionLedger(clock=clock)).run()
    assert report.resolved_decisions == 0
    assert report.acted is False


def test_the_cycle_writes_its_own_observation_to_the_ledger():
    clock = FixedClock(T0)
    ledger = E.EvolutionLedger(clock=clock)
    E.EvolutionCycle(clock=clock, ledger=ledger).run()
    events = ledger.events(stage=E.EvolutionStage.OBSERVATION)
    assert events and "evolution cycle" in events[0].observed


def test_knowledge_written_by_the_cycle_carries_its_provenance():
    clock = FixedClock(T0)
    base = KB.KnowledgeBase(clock=clock)
    cycle = E.EvolutionCycle(clock=clock, journal=_resolved_journal(clock),
                             knowledge_base=base,
                             ledger=E.EvolutionLedger(clock=clock))
    report = cycle.run()
    record = base.head(report.knowledge_recorded[0])
    assert record.source == "outcomes.detect_weaknesses"
    assert record.source_type is KB.SourceType.STRATEGY_REPORT
    assert record.supporting and record.supporting[0].origin == "outcome-journal"


def test_running_the_cycle_twice_does_not_duplicate_knowledge():
    clock = FixedClock(T0)
    base = KB.KnowledgeBase(clock=clock)
    cycle = E.EvolutionCycle(clock=clock, journal=_resolved_journal(clock),
                             knowledge_base=base,
                             ledger=E.EvolutionLedger(clock=clock))
    first = cycle.run()
    clock.advance(timedelta(minutes=1))
    second = cycle.run()
    assert first.knowledge_recorded
    assert second.knowledge_recorded == ()
    assert second.errors == ()

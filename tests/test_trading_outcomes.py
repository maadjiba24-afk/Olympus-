"""The outcome-learning loop — Phase 10 completion gate 1.

Gate 1: record forecasts and compare them with actual outcomes.

The tests that carry weight are the ones about *not* learning the wrong thing:
an axis that cannot be judged must grade UNKNOWN rather than passing, a decision
record must be unamendable after the outcome is known, and abstentions must be
scored as well as trades.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from olympus.trading import outcomes as O
from olympus.trading.clock import FixedClock
from olympus.trading.contracts import Regime, Side
from olympus.trading.errors import ConfigurationError, KnowledgeError

T0 = datetime(2026, 4, 1, tzinfo=timezone.utc)
KEY = "BINANCE:BTCUSDT"


def decision(decision_id="d1", **kwargs):
    kwargs.setdefault("forecast_paths", ((101.0, 102.0), (100.5, 103.0),
                                         (101.5, 101.0)))
    kwargs.setdefault("entry_reference_price", 100.0)
    kwargs.setdefault("side", Side.BUY)
    kwargs.setdefault("authorised_quantity", "1")
    kwargs.setdefault("stop_price", 98.0)
    kwargs.setdefault("regime", Regime.TRENDING_UP)
    return O.DecisionRecord(decision_id=decision_id, ts=T0, instrument_key=KEY,
                            timeframe="1h", strategy_id="momentum",
                            forecast_horizon=2, **kwargs)


def outcome(decision_id="d1", **kwargs):
    kwargs.setdefault("realised_path", (101.0, 103.0))
    kwargs.setdefault("entry_price", 100.0)
    kwargs.setdefault("exit_price", 103.0)
    kwargs.setdefault("filled_quantity", "1")
    kwargs.setdefault("gross_pnl", 3.0)
    kwargs.setdefault("mfe", 3.0)
    kwargs.setdefault("mae", 0.5)
    return O.Outcome(decision_id=decision_id, resolved_at=T0 + timedelta(hours=2),
                     **kwargs)


@pytest.fixture()
def journal():
    return O.OutcomeJournal(clock=FixedClock(T0))


# --- gate 1: record, then compare -------------------------------------------

def test_a_decision_preserves_every_required_input(journal):
    record = decision(features={"rsi": 55.0}, data_version="sha:abc",
                      model_versions={"kronos": "rev-1"},
                      model_outputs={"sentiment": 0.2},
                      expected_return=0.02, risk_verdict="approved")
    journal.open(record)
    data = journal.decision("d1").to_dict()
    for field in ("market_state", "data_version", "features", "forecast_paths",
                  "model_outputs", "strategy_id", "risk_verdict", "regime",
                  "model_versions", "expected_return", "stop_price"):
        assert field in data, field
    assert data["forecast_paths"] == [[101.0, 102.0], [100.5, 103.0], [101.5, 101.0]]


def test_the_forecast_ensemble_is_kept_whole_and_the_mean_is_a_view():
    """The teardown found upstream Kronos averaging paths away inside inference
    (§12.7). Dispersion across paths is most of the information."""
    record = decision()
    assert len(record.forecast_paths) == 3
    assert record.forecast_mean == pytest.approx((101.0, 102.0))
    assert record.forecast_quantile(0.0) == (100.5, 101.0)
    assert record.forecast_quantile(1.0) == (101.5, 103.0)


def test_resolving_scores_the_decision_against_what_happened(journal):
    journal.open(decision())
    evaluation = journal.resolve(outcome())
    assert evaluation.directional_correct is True
    assert evaluation.forecast_error == pytest.approx(0.5)
    assert evaluation.net_performance == pytest.approx(3.0)


def test_a_wrong_direction_is_recorded_as_an_observation(journal):
    journal.open(decision())
    evaluation = journal.resolve(outcome(realised_path=(99.0, 97.0),
                                         exit_price=97.0, gross_pnl=-3.0,
                                         mfe=0.0, mae=3.0))
    assert evaluation.directional_correct is False
    assert any("direction wrong" in o for o in evaluation.observations)


def test_an_outcome_without_its_decision_is_refused(journal):
    """An outcome with no record of the state that produced it teaches nothing."""
    with pytest.raises(KnowledgeError):
        journal.resolve(outcome())


def test_a_decision_cannot_be_amended_after_the_fact(journal):
    """Amending the input after the outcome is known would make every score
    that follows unfalsifiable."""
    journal.open(decision())
    with pytest.raises(KnowledgeError):
        journal.open(decision(expected_return=0.99))


def test_resolving_twice_is_refused(journal):
    journal.open(decision())
    journal.resolve(outcome())
    with pytest.raises(KnowledgeError):
        journal.resolve(outcome())


def test_pending_lists_decisions_awaiting_an_outcome(journal):
    journal.open(decision("d1"))
    journal.open(decision("d2"))
    journal.resolve(outcome("d1"))
    assert journal.pending() == ["d2"]


def test_evaluations_are_recomputed_not_read_back(journal, monkeypatch):
    """If `evaluate` changes, history is rescored by the new logic rather than
    frozen under the old — a stored score can never drift from the code."""
    journal.open(decision())
    journal.resolve(outcome())
    calls = []
    real = O.evaluate

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(O, "evaluate", counting)
    journal.resolved()
    assert calls, "resolved() must recompute rather than load a stored score"


# --- unmeasurable axes stay unknown -----------------------------------------

def test_an_axis_with_no_input_grades_unknown_not_good():
    """A report showing six good axes out of six, two of which were
    unmeasurable, is a misleading report."""
    bare = O.DecisionRecord(decision_id="d", ts=T0, instrument_key=KEY,
                            timeframe="1h", abstained=True)
    evaluation = O.evaluate(bare, O.Outcome(decision_id="d", resolved_at=T0))
    assert evaluation.forecast_calibration is O.Grade.UNKNOWN
    assert evaluation.stop_quality is O.Grade.UNKNOWN
    assert "stop_quality" in evaluation.unknown_axes
    assert evaluation.poor_axes == ()


def test_a_mismatched_decision_and_outcome_are_refused():
    with pytest.raises(ConfigurationError):
        O.evaluate(decision("a"), outcome("b"))


# --- the thirteen axes ------------------------------------------------------

def test_a_stop_tighter_than_the_favourable_excursion_grades_poor():
    """Stopped out of a trade that had already moved further in favour than the
    stop was wide: the stop was too tight, not the thesis wrong."""
    record = decision(stop_price=99.0)
    result = outcome(exit_reason=O.ExitReason.STOP, mfe=5.0, mae=1.0,
                     exit_price=99.0, gross_pnl=-1.0)
    evaluation = O.evaluate(record, result)
    assert evaluation.stop_quality is O.Grade.POOR
    assert any("tighter than" in o for o in evaluation.observations)


def test_costs_consuming_most_of_gross_pnl_grade_execution_poor():
    evaluation = O.evaluate(decision(), outcome(gross_pnl=1.0, fees=0.4,
                                                slippage=0.2))
    assert evaluation.execution_quality is O.Grade.POOR
    assert any("costs consumed" in o for o in evaluation.observations)


def test_paying_costs_on_a_trade_with_no_gross_pnl_is_poor():
    evaluation = O.evaluate(decision(), outcome(gross_pnl=0.0, fees=0.5))
    assert evaluation.execution_quality is O.Grade.POOR


def test_a_high_confidence_wrong_forecast_is_flagged_specifically():
    record = decision(forecast_confidence=0.9)
    result = outcome(realised_path=(99.0, 97.0), exit_price=97.0, gross_pnl=-3.0,
                     mfe=0.0, mae=3.0)
    evaluation = O.evaluate(record, result)
    assert evaluation.confidence_calibration is O.Grade.POOR
    assert any("high-confidence" in o for o in evaluation.observations)


def test_a_narrow_uncertainty_band_is_detected():
    record = decision(forecast_paths=((101.0, 102.0), (101.0, 102.01),
                                      (101.0, 102.0)))
    evaluation = O.evaluate(record, outcome(realised_path=(150.0, 160.0)))
    assert any("too narrow" in o for o in evaluation.observations)


def test_a_losing_trade_says_abstaining_would_have_been_better():
    """A system that only scores the trades it took cannot learn to take fewer."""
    evaluation = O.evaluate(decision(), outcome(gross_pnl=-2.0))
    assert evaluation.abstention_would_have_been_better is True
    assert any("abstaining would have been better" in o
               for o in evaluation.observations)


def test_a_costly_win_still_counts_as_a_loss_after_costs():
    """Net, not gross: a 0.1 gain paying 0.5 of costs is a losing decision."""
    evaluation = O.evaluate(decision(), outcome(gross_pnl=0.1, fees=0.5))
    assert evaluation.net_performance == pytest.approx(-0.4)
    assert evaluation.abstention_would_have_been_better is True


def test_abstentions_are_scored_too():
    record = O.DecisionRecord(decision_id="a1", ts=T0, instrument_key=KEY,
                              timeframe="1h", abstained=True,
                              strategy_id="momentum")
    evaluation = O.evaluate(record, O.Outcome(decision_id="a1", resolved_at=T0,
                                              gross_pnl=5.0))
    assert evaluation.abstention_would_have_been_better is False
    assert any("forgone" in o for o in evaluation.observations)


def test_underperforming_a_baseline_is_recorded():
    evaluation = O.evaluate(decision(), outcome(gross_pnl=1.0, baseline_pnl=4.0))
    assert evaluation.versus_baseline == pytest.approx(-3.0)
    assert any("underperformed the baseline" in o
               for o in evaluation.observations)


# --- contract invariants ----------------------------------------------------

def test_an_abstained_decision_cannot_carry_a_side():
    with pytest.raises(ConfigurationError):
        O.DecisionRecord(decision_id="x", ts=T0, instrument_key=KEY,
                         timeframe="1h", abstained=True, side=Side.BUY)


def test_a_negative_favourable_excursion_is_refused():
    with pytest.raises(ConfigurationError):
        O.Outcome(decision_id="x", resolved_at=T0, mfe=-1.0)


def test_adverse_excursion_is_recorded_as_a_positive_magnitude():
    with pytest.raises(ConfigurationError):
        O.Outcome(decision_id="x", resolved_at=T0, mae=-1.0)


def test_negative_fees_are_refused():
    with pytest.raises(ConfigurationError):
        O.Outcome(decision_id="x", resolved_at=T0, fees=-0.1)


def test_a_decision_must_name_its_instrument():
    with pytest.raises(ConfigurationError):
        O.DecisionRecord(decision_id="x", ts=T0, instrument_key=" ",
                         timeframe="1h")


# --- systematic weakness ----------------------------------------------------

def _many(n, *, wrong: bool):
    pairs = []
    for i in range(n):
        record = decision(f"d{i}")
        result = (outcome(f"d{i}", realised_path=(99.0, 97.0), exit_price=97.0,
                          gross_pnl=-3.0, mfe=0.0, mae=3.0) if wrong
                  else outcome(f"d{i}"))
        pairs.append((record, O.evaluate(record, result)))
    return pairs


def test_a_small_sample_never_produces_a_weakness_finding():
    """A finding over eight decisions is a story, not a weakness."""
    assert O.detect_weaknesses(_many(8, wrong=True)) == []


def test_a_systematically_wrong_direction_is_reported_as_high_severity():
    findings = O.detect_weaknesses(_many(40, wrong=True))
    directional = [f for f in findings if f.axis == "directional_accuracy"]
    assert directional
    assert directional[0].severity == "high"
    assert directional[0].sample >= 30
    assert "worse than chance" in directional[0].detail


def test_over_trading_is_detected_separately_from_bad_forecasting():
    findings = O.detect_weaknesses(_many(40, wrong=True))
    assert any(f.axis == "over_trading" for f in findings)


def test_a_healthy_sample_produces_no_findings():
    assert O.detect_weaknesses(_many(40, wrong=False)) == []


def test_weaknesses_are_grouped_by_instrument_regime_timeframe_and_strategy():
    findings = O.detect_weaknesses(_many(40, wrong=True))
    scopes = {f.scope for f in findings}
    assert f"instrument:{KEY}" in scopes
    assert "regime:trending_up" in scopes
    assert "timeframe:1h" in scopes
    assert "strategy:momentum" in scopes


def test_the_journal_summarises_what_it_knows(journal):
    for i in range(3):
        journal.open(decision(f"d{i}"))
        journal.resolve(outcome(f"d{i}"))
    summary = journal.summary()
    assert summary["resolved"] == 3
    assert summary["directional_accuracy"] == pytest.approx(1.0)
    assert summary["pending"] == 0


def test_an_empty_journal_summarises_honestly(journal):
    assert journal.summary() == {"resolved": 0, "pending": 0}

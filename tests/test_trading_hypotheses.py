"""Structured research proposals — Phase 10 completion gate 3.

Gate 3: generate structured research hypotheses.

The constructor invariant is what makes this more than a note-taking system: a
proposal that cannot state what would refute it is refused. Everything else
follows from that.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from olympus.trading import hypotheses as H
from olympus.trading import drift as D
from olympus.trading import outcomes as O
from olympus.trading.clock import FixedClock
from olympus.trading.errors import ConfigurationError
from olympus.trading.governance import Actor

T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)


def plan(**kwargs):
    kwargs.setdefault("kind", H.ExperimentKind.ABLATION)
    kwargs.setdefault("description", "remove the feature and re-measure")
    kwargs.setdefault("baselines", ("always flat",))
    return H.ExperimentPlan(**kwargs)


def proposal(proposal_id="p1", **kwargs):
    kwargs.setdefault("hypothesis", "Feature X improves directional accuracy.")
    kwargs.setdefault("motivation", "It is cheap and plausibly informative.")
    kwargs.setdefault("experiment", plan())
    kwargs.setdefault("success_criteria", ("accuracy rises above 0.55",))
    kwargs.setdefault("failure_criteria", ("accuracy stays at or below 0.50",))
    return H.ResearchProposal(proposal_id=proposal_id, created_at=T0, **kwargs)


@pytest.fixture()
def engine():
    return H.HypothesisEngine(clock=FixedClock(T0))


# --- gate 3: the required structure -----------------------------------------

def test_a_proposal_carries_every_required_field():
    data = proposal(supporting_observations=("obs",),
                    contradicting_evidence=("counter",)).to_dict()
    for field in ("hypothesis", "motivation", "supporting_observations",
                  "contradicting_evidence", "experiment", "success_criteria",
                  "failure_criteria", "risk_level", "estimated_cost_units",
                  "production_impact"):
        assert field in data, field
    assert data["experiment"]["required_data"] == []


def test_a_proposal_with_no_failure_condition_is_refused():
    """A hypothesis with no failure condition cannot be tested, only agreed with."""
    with pytest.raises(ConfigurationError) as exc:
        proposal(failure_criteria=())
    assert "refute" in str(exc.value)


def test_a_proposal_with_no_success_condition_is_refused():
    with pytest.raises(ConfigurationError):
        proposal(success_criteria=())


def test_success_and_failure_criteria_may_not_overlap():
    """If no result could distinguish them, the proposal is unfalsifiable."""
    with pytest.raises(ConfigurationError) as exc:
        proposal(success_criteria=("the model  improves",),
                 failure_criteria=("The model improves",))
    assert "no result could distinguish" in str(exc.value)


def test_an_experiment_plan_must_name_a_baseline():
    """A result with nothing to beat cannot show improvement."""
    with pytest.raises(ConfigurationError):
        H.ExperimentPlan(kind=H.ExperimentKind.ABLATION, description="x",
                         baselines=())


def test_a_risk_policy_proposal_is_high_risk_by_definition():
    with pytest.raises(ConfigurationError):
        proposal(production_impact=H.ProductionImpact.RISK_POLICY,
                 risk_level=H.RiskLevel.LOW)


def test_proposals_touching_execution_or_risk_need_human_review():
    assert proposal(production_impact=H.ProductionImpact.RISK_POLICY,
                    risk_level=H.RiskLevel.HIGH).needs_human_review is True
    assert proposal(production_impact=H.ProductionImpact.REPORTING_ONLY,
                    risk_level=H.RiskLevel.LOW).needs_human_review is False


def test_a_proposal_round_trips_through_json():
    original = proposal(instruments=("BINANCE:BTCUSDT",))
    assert H.ResearchProposal.from_dict(original.to_dict()) == original


# --- generation from measured evidence --------------------------------------

def weakness(axis, scope="instrument:BINANCE:BTCUSDT", sample=50, rate=0.7):
    return O.WeaknessFinding(axis=axis, scope=scope, detail="measured",
                             sample=sample, rate=rate)


@pytest.mark.parametrize("axis", ["directional_accuracy", "over_trading",
                                  "stop_quality", "execution_quality",
                                  "confidence_calibration", "versus_baseline"])
def test_each_known_weakness_produces_a_testable_hypothesis(axis):
    generated = H.from_weakness(weakness(axis), clock=FixedClock(T0))
    assert generated is not None
    assert generated.testable
    assert generated.experiment.baselines
    assert generated.triggered_by, "the chain back to the evidence must be recorded"


def test_a_weakness_hypothesis_proposes_the_simplest_mechanism():
    """Accuracy below chance is usually a sign error, not a weak model."""
    generated = H.from_weakness(weakness("directional_accuracy"),
                                clock=FixedClock(T0))
    assert "inverted" in generated.hypothesis
    assert "signal_inverted" in generated.experiment.variants


def test_an_unknown_weakness_axis_generates_nothing():
    """Silence beats a generic hypothesis nobody can act on."""
    assert H.from_weakness(weakness("mystery"), clock=FixedClock(T0)) is None


def test_the_sample_size_is_carried_into_the_proposal():
    generated = H.from_weakness(weakness("over_trading", sample=120),
                                clock=FixedClock(T0))
    assert any("120" in o for o in generated.supporting_observations)


def test_only_major_and_critical_drift_generate_proposals():
    """Minor drift is normal; generating research from it buries the rest."""
    minor = D.DriftSignal(kind=D.DriftKind.DATA, subject="s", metric="m",
                          severity=D.Severity.MINOR, detail="d")
    major = D.DriftSignal(kind=D.DriftKind.DATA, subject="s", metric="m",
                          severity=D.Severity.MAJOR, detail="d")
    assert H.from_drift(minor, clock=FixedClock(T0)) is None
    assert H.from_drift(major, clock=FixedClock(T0)) is not None


def test_a_drift_hypothesis_distinguishes_a_regime_change_from_an_episode():
    signal = D.DriftSignal(kind=D.DriftKind.MODEL, subject="kronos",
                           metric="mean", severity=D.Severity.CRITICAL,
                           detail="PSI 0.9")
    generated = H.from_drift(signal, clock=FixedClock(T0))
    assert "sub-period" in generated.experiment.description
    assert generated.failure_criteria


# --- the standing model-value hypothesis ------------------------------------

def test_the_standing_hypothesis_names_whichever_model_it_is_about():
    """Model-agnostic: the same question, asked of anything."""
    for name in ("Kronos", "olympus-native", "some-vendor-model"):
        standing = H.model_conditional_value(name, instrument="BINANCE:BTCUSDT",
                                             clock=FixedClock(T0))
        assert name in standing.hypothesis
        assert set(standing.experiment.variants) == {f"with {name}",
                                                     f"without {name}"}


def test_the_standing_hypothesis_needs_a_model_name():
    with pytest.raises(ConfigurationError):
        H.model_conditional_value("   ")


def test_the_experiment_holds_everything_but_the_model_fixed():
    standing = H.model_conditional_value("any-model", clock=FixedClock(T0))
    for control in ("identical sizing code", "identical cost model",
                    "identical risk limits"):
        assert control in standing.experiment.controls


def test_the_standing_hypothesis_can_fail():
    standing = H.model_conditional_value("any-model", clock=FixedClock(T0))
    assert any("no tercile" in c for c in standing.failure_criteria)


def test_a_standing_hypothesis_always_carries_a_case_against():
    """A proposal with only the case *for* a model is an advocacy document.
    When the caller supplies no counter-evidence, the absence of evidence is
    recorded as counter-evidence."""
    bare = H.model_conditional_value("unknown-model", clock=FixedClock(T0))
    assert bare.contradicting_evidence
    assert any("no out-of-sample evidence" in e
               for e in bare.contradicting_evidence)


def test_the_kronos_binding_carries_the_known_evidence_against_kronos():
    """The Kronos-specific evidence lives with Kronos, not in an Olympus
    module — see tests/test_trading_independence.py for why."""
    from olympus.trading.kronos_adapter import kronos_value_hypothesis
    standing = kronos_value_hypothesis(instrument="BINANCE:BTCUSDT",
                                       clock=FixedClock(T0))
    assert any("#354" in e for e in standing.contradicting_evidence)
    assert any("no out-of-sample evidence" in e
               for e in standing.contradicting_evidence)
    assert "Kronos" in standing.hypothesis


# --- the engine -------------------------------------------------------------

def test_generating_stores_the_proposals(engine):
    generated = engine.generate(weaknesses=[weakness("over_trading")])
    assert len(generated) == 1
    assert engine.get(generated[0].proposal_id) is not None


def test_the_same_weakness_twice_does_not_duplicate_the_backlog(engine):
    """A backlog with fifty copies of one hypothesis is a backlog nobody triages."""
    engine.generate(weaknesses=[weakness("over_trading")])
    engine.generate(weaknesses=[weakness("over_trading")])
    assert len(engine.all()) == 1


def test_a_refuted_proposal_cannot_be_reopened(engine):
    generated = engine.generate(weaknesses=[weakness("over_trading")])[0]
    engine.set_status(generated.proposal_id, H.ProposalStatus.REFUTED,
                      notes="no threshold beat flat")
    with pytest.raises(ConfigurationError) as exc:
        engine.set_status(generated.proposal_id, H.ProposalStatus.QUEUED)
    assert "propose a new hypothesis" in str(exc.value)


def test_a_refuted_proposal_stays_on_the_record(engine):
    """A system that could tidy away failed hypotheses could present a research
    record that never fails."""
    generated = engine.generate(weaknesses=[weakness("over_trading")])[0]
    engine.set_status(generated.proposal_id, H.ProposalStatus.REFUTED)
    assert engine.get(generated.proposal_id).status is H.ProposalStatus.REFUTED
    assert engine.stats()["by_status"]["refuted"] == 1


def test_the_backlog_is_ordered_cheapest_first(engine):
    engine.submit(proposal("cheap", experiment=plan(estimated_cost_units=1.0)))
    engine.submit(proposal("dear", experiment=plan(estimated_cost_units=99.0),
                           hypothesis="Something else entirely."))
    assert [p.proposal_id for p in engine.backlog()] == ["cheap", "dear"]


def test_a_refuted_proposal_leaves_the_backlog(engine):
    engine.submit(proposal("p"))
    engine.set_status("p", H.ProposalStatus.REFUTED)
    assert engine.backlog() == []


def test_generation_is_an_autonomous_action(engine):
    """Filling a research backlog risks nothing and needs no approval."""
    generated = engine.generate(weaknesses=[weakness("stop_quality")],
                                actor=Actor.autonomous("olympus"))
    assert generated


def test_proposals_survive_a_new_engine_instance(engine):
    engine.submit(proposal("persist"))
    fresh = H.HypothesisEngine(clock=FixedClock(T0))
    assert fresh.get("persist") is not None


def test_generating_from_both_sources_deduplicates_by_id(engine):
    signal = D.DriftSignal(kind=D.DriftKind.MODEL, subject="k", metric="m",
                           severity=D.Severity.MAJOR, detail="d")
    generated = engine.generate(weaknesses=[weakness("over_trading")],
                                drift_signals=[signal, signal])
    assert len({p.proposal_id for p in generated}) == len(generated)

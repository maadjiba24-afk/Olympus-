"""The research sandbox — Phase 10 completion gates 4 and 6.

Gate 4: run isolated experiments without production access.
Gate 6: reject improvements that do not outperform baselines.

The isolation test is the important one, and it is deliberately written as an
experiment that *tries* to escape: the sandbox must refuse it, record the
attempt, and let the error reach the caller.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone

import pytest

from olympus.trading import hypotheses as H
from olympus.trading import lab as L
from olympus.trading.clock import FixedClock
from olympus.trading.errors import ConfigurationError, ExperimentError

T0 = datetime(2026, 7, 1, tzinfo=timezone.utc)


def proposal(proposal_id="p1"):
    return H.ResearchProposal(
        proposal_id=proposal_id, hypothesis="Variant beats baseline.",
        motivation="measured weakness", created_at=T0,
        experiment=H.ExperimentPlan(kind=H.ExperimentKind.ABLATION,
                                    description="compare", baselines=("flat",)),
        success_criteria=("variant beats every baseline significantly",),
        failure_criteria=("variant does not beat the baseline",))


def constant(value, n=200):
    return lambda sandbox: [value] * n


def noisy(mean, n=200, seed=1, spread=0.001):
    def arm(sandbox):
        rng = random.Random(seed)
        return [rng.gauss(mean, spread) for _ in range(n)]
    return arm


@pytest.fixture()
def research_lab():
    return L.ResearchLab(clock=FixedClock(T0))


# --- gate 4: isolation ------------------------------------------------------

@pytest.mark.parametrize("resource", sorted(L.DENIED_RESOURCES))
def test_the_sandbox_holds_no_reference_to_any_denied_resource(resource):
    sandbox = L.ResearchSandbox()
    with pytest.raises(ExperimentError) as exc:
        sandbox.request(resource)
    assert resource in str(exc.value)
    assert sandbox.denied_requests == (resource,)


def test_the_sandbox_fails_closed_on_an_unknown_resource():
    """Adding a capability must be a deliberate edit, not an accident."""
    with pytest.raises(ExperimentError) as exc:
        L.ResearchSandbox().request("shell_access")
    assert "fails closed" in str(exc.value)


def test_the_sandbox_has_no_attribute_for_a_denied_resource():
    """Enforcement by absence, not by inspection: there is nothing to return."""
    sandbox = L.ResearchSandbox()
    for denied in L.DENIED_RESOURCES:
        assert not hasattr(sandbox, denied)
        assert sandbox.has(denied) is False


def test_an_experiment_that_reaches_for_credentials_is_refused_and_recorded(research_lab):
    """The attempt must leave a permanent trace whether or not the caller
    handles the error."""
    def escaping(sandbox):
        sandbox.request("live_broker_credentials")
        return [1.0] * 100

    experiment = L.AblationExperiment(variants={"escape": escaping},
                                      baselines={"flat": constant(0.0)})
    with pytest.raises(ExperimentError):
        research_lab.run(proposal(), experiment)

    results = research_lab.results(proposal_id="p1")
    assert results and results[0].verdict is L.Verdict.ERRORED
    assert "live_broker_credentials" in results[0].sandbox["denied_requests"]
    assert research_lab.stats()["sandbox_breach_attempts"] == 1


def test_an_errored_experiment_infers_nothing_in_either_direction(research_lab):
    def broken(sandbox):
        raise ValueError("bad feature")

    result = research_lab.run(proposal(),
                              L.AblationExperiment(variants={"v": broken},
                                                   baselines={"flat": constant(0.0)}))
    assert result.verdict is L.Verdict.ERRORED
    assert result.variants == ()


def test_the_sandbox_records_every_request_granted_or_not():
    sandbox = L.ResearchSandbox(historical_data=[1, 2, 3])
    sandbox.request("historical_data")
    with pytest.raises(ExperimentError):
        sandbox.request("production_secrets")
    assert set(sandbox.requests) == {"historical_data", "production_secrets"}


def test_experiments_are_deterministic_under_a_seed(research_lab):
    def seeded(sandbox):
        rng = sandbox.request("random")
        return [rng.random() for _ in range(100)]

    experiment = L.AblationExperiment(variants={"v": seeded},
                                      baselines={"flat": constant(0.0)})
    first = research_lab.run(proposal("a"), experiment, seed=42)
    second = research_lab.run(proposal("b"), experiment, seed=42)
    assert first.best_variant.net_mean == second.best_variant.net_mean


# --- gate 6: reject what does not beat the baseline -------------------------

def test_a_variant_that_loses_to_the_baseline_is_refuted(research_lab):
    experiment = L.AblationExperiment(variants={"v": constant(0.001)},
                                      baselines={"flat": constant(0.01)})
    result = research_lab.run(proposal(), experiment)
    assert result.verdict is L.Verdict.REFUTED
    assert result.beat_every_baseline is False


def test_beating_the_weakest_baseline_is_not_enough(research_lab):
    """A result that beats a poor baseline and loses to `always flat` has shown
    nothing."""
    experiment = L.AblationExperiment(
        variants={"v": constant(0.001)},
        baselines={"poor": constant(-0.01), "flat": constant(0.02)})
    result = research_lab.run(proposal(), experiment)
    assert result.verdict is L.Verdict.REFUTED


def test_a_small_sample_is_inconclusive_not_supported(research_lab):
    experiment = L.AblationExperiment(variants={"v": constant(0.05, n=5)},
                                      baselines={"flat": constant(0.0, n=5)},
                                      min_observations=30)
    result = research_lab.run(proposal(), experiment)
    assert result.verdict is L.Verdict.INCONCLUSIVE
    assert "below the pre-registered minimum" in result.rationale


def test_a_genuine_edge_is_supported(research_lab):
    experiment = L.AblationExperiment(variants={"v": noisy(0.01, seed=1)},
                                      baselines={"flat": noisy(0.0, seed=2)})
    result = research_lab.run(proposal(), experiment)
    assert result.verdict is L.Verdict.SUPPORTED
    assert result.significance["paired_bootstrap"]["significant"] is True


def test_a_margin_below_the_pre_registered_minimum_is_inconclusive(research_lab):
    experiment = L.AblationExperiment(variants={"v": noisy(0.01, seed=1)},
                                      baselines={"flat": noisy(0.0, seed=2)},
                                      min_improvement=1.0)
    result = research_lab.run(proposal(), experiment)
    assert result.verdict is L.Verdict.INCONCLUSIVE


def test_an_experiment_needs_a_baseline_to_exist_at_all():
    with pytest.raises(ConfigurationError):
        L.AblationExperiment(variants={"v": constant(1.0)}, baselines={})


def test_costs_are_applied_identically_to_every_arm(research_lab):
    """The easiest way to manufacture an improvement is to charge the variant
    less; the cost model lives on the sandbox, not the experiment."""
    experiment = L.AblationExperiment(variants={"v": constant(0.0)},
                                      baselines={"flat": constant(0.0)})
    result = research_lab.run(proposal(), experiment)
    variant = result.variants[0]
    baseline = result.baselines[0]
    assert variant.net_mean == baseline.net_mean
    assert variant.net_mean < 0, "costs must actually be charged"


def test_the_default_cost_model_is_pessimistic():
    """Under-charging costs produces results that look good in research and
    lose money in production."""
    model = L.CostModel()
    assert model.round_trip_bps > 0
    assert model.apply([0.0, 0.0]) == pytest.approx([-model.round_trip_bps / 1e4] * 2)


def test_turnover_must_align_with_returns():
    with pytest.raises(ConfigurationError):
        L.CostModel().apply([0.1, 0.2], turnover=[1.0])


def test_negative_costs_are_refused():
    with pytest.raises(ConfigurationError):
        L.CostModel(fee_bps=-1.0)


# --- experiment kinds -------------------------------------------------------

def test_a_parameter_sweep_corrects_for_multiple_comparisons():
    """A sweep of thirty settings finds a p<0.05 winner from noise most of the
    time; without the correction a sweep is a false-discovery machine."""
    variants = {f"p{i}": constant(0.0) for i in range(20)}
    sweep = L.ParameterSweepExperiment(variants=variants,
                                       baselines={"flat": constant(0.0)},
                                       alpha=0.05)
    assert sweep.alpha == pytest.approx(0.05 / 20)


def test_cost_sensitivity_reports_the_multiple_an_edge_survives(research_lab):
    experiment = L.CostSensitivityExperiment(
        variants={"v": constant(0.01)}, baselines={"flat": constant(0.0)},
        cost_multipliers=(1.0, 2.0, 10.0))
    result = research_lab.run(proposal(), experiment)
    survives = result.variants[0].extra["survives_cost_multiple"]
    assert survives is not None and survives >= 1.0


def test_robustness_reports_the_spread_across_seeds(research_lab):
    def unstable(sandbox):
        rng = sandbox.request("random")
        return [rng.gauss(0.01, 0.5) for _ in range(100)]

    experiment = L.RobustnessExperiment(variants={"v": unstable},
                                        baselines={"flat": constant(0.0)},
                                        trials=4)
    result = research_lab.run(proposal(), experiment)
    assert result.variants[0].extra["across_seed_stdev"] is not None


def test_a_falsification_that_fails_to_kill_the_claim_says_so(research_lab):
    experiment = L.FalsificationExperiment(variants={"v": noisy(0.01, seed=1)},
                                           baselines={"flat": noisy(0.0, seed=2)})
    result = research_lab.run(proposal(), experiment)
    assert result.verdict is L.Verdict.SUPPORTED
    assert "survived a deliberate attempt" in result.rationale


def test_a_successful_falsification_is_reported_as_such(research_lab):
    experiment = L.FalsificationExperiment(variants={"v": constant(0.0)},
                                           baselines={"flat": constant(0.01)})
    result = research_lab.run(proposal(), experiment)
    assert result.verdict is L.Verdict.REFUTED
    assert "falsification attempt succeeded" in result.rationale


# --- results ----------------------------------------------------------------

def test_a_flat_series_has_no_sharpe_ratio():
    """Reporting 0.0 for an undefined ratio is a lie that survives into a table."""
    summary = L.summarise("flat", [0.0] * 10, [0.0] * 10)
    assert summary.sharpe is None
    assert summary.volatility is None


def test_the_result_records_which_verdict_and_why(research_lab):
    result = research_lab.run(proposal(),
                              L.AblationExperiment(variants={"v": constant(0.0)},
                                                   baselines={"flat": constant(0.01)}))
    assert result.rationale
    assert L.ExperimentResult.from_dict(result.to_dict()).verdict is result.verdict


def test_running_updates_the_proposal_status_including_a_refutation():
    engine = H.HypothesisEngine(clock=FixedClock(T0))
    stored = engine.submit(proposal("linked"))
    research_lab = L.ResearchLab(clock=FixedClock(T0), engine=engine)
    research_lab.run(stored,
                     L.AblationExperiment(variants={"v": constant(0.0)},
                                          baselines={"flat": constant(0.01)}))
    assert engine.get("linked").status is H.ProposalStatus.REFUTED


def test_results_persist_across_lab_instances(research_lab):
    research_lab.run(proposal(),
                     L.AblationExperiment(variants={"v": constant(0.0)},
                                          baselines={"flat": constant(0.0)}))
    fresh = L.ResearchLab(clock=FixedClock(T0))
    assert fresh.results(proposal_id="p1")


def test_there_is_no_way_to_delete_a_result(research_lab):
    """A system that could discard inconvenient results could present a
    research history in which it was always right."""
    assert not hasattr(research_lab, "delete")
    assert not hasattr(research_lab, "purge")
    assert not hasattr(research_lab, "discard")

"""Versioned deployment and rollback — Phase 10 completion gate 11.

Gate 11: roll back a deployed component safely.

"Safely" is doing the work in that sentence. A rollback that leaves open orders
and open positions from the previous version behind has traded a software
problem for a position problem, so the checklist is part of the gate.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from olympus.trading import rollback as R
from olympus.trading.clock import FixedClock
from olympus.trading.errors import (ConfigurationError, GovernanceViolation,
                                    PromotionDenied)
from olympus.trading.governance import Actor

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
OPERATOR = Actor.operator("alice", token="tok")
OLYMPUS = Actor.autonomous("rollback-controller")

DEPS = {"python": "3.12.2", "olympus": "0.9.1"}


@pytest.fixture()
def ledger():
    return R.DeploymentLedger(clock=FixedClock(T0))


def deploy(ledger, deployment_id, version, *, config=None, actor=OPERATOR,
           capability_id="momentum"):
    return ledger.deploy(deployment_id=deployment_id,
                         capability_id=capability_id, version=version,
                         actor=actor, config=config or {"stop_atr": 2.0},
                         dependency_versions=DEPS,
                         evidence_refs=(f"exp-{version}",),
                         rollback_procedure="restore the previous deployment id")


# --- deployment preconditions -----------------------------------------------

def test_deploying_requires_an_operator(ledger):
    with pytest.raises(GovernanceViolation):
        deploy(ledger, "d1", "1.0", actor=OLYMPUS)


@pytest.mark.parametrize("missing", ["config", "dependency_versions",
                                     "evidence_refs", "rollback_procedure"])
def test_a_deployment_without_the_means_to_reverse_it_is_refused(ledger, missing):
    """Each of these is unobtainable once an incident has started."""
    kwargs = dict(deployment_id="d1", capability_id="momentum", version="1.0",
                  actor=OPERATOR, config={"a": 1}, dependency_versions=DEPS,
                  evidence_refs=("exp-1",), rollback_procedure="restore")
    kwargs[missing] = {} if missing in ("config", "dependency_versions") else (
        () if missing == "evidence_refs" else "  ")
    with pytest.raises(ConfigurationError):
        ledger.deploy(**kwargs)


def test_a_deployment_records_everything_needed_to_reproduce_it(ledger):
    record = deploy(ledger, "d1", "1.0")
    data = record.to_dict()
    for field in ("config", "dependency_versions", "evidence_refs",
                  "rollback_procedure", "previous_deployment_id", "signature"):
        assert field in data, field
    assert len(data["signature"]) == 64


def test_the_signature_changes_when_the_configuration_changes(ledger):
    first = deploy(ledger, "d1", "1.0", config={"stop_atr": 2.0})
    second = deploy(ledger, "d2", "1.1", config={"stop_atr": 3.0})
    assert first.signature != second.signature


def test_deploying_supersedes_the_previous_active_version(ledger):
    deploy(ledger, "d1", "1.0")
    deploy(ledger, "d2", "1.1")
    assert ledger.get("d1").state is R.DeploymentState.SUPERSEDED
    assert ledger.active("momentum").deployment_id == "d2"
    assert ledger.get("d2").previous_deployment_id == "d1"


def test_a_first_deployment_has_nothing_to_roll_back_to(ledger):
    assert deploy(ledger, "d1", "1.0").reversible is False
    assert ledger.report("momentum")["active_without_a_rollback_target"] == ["d1"]


def test_a_duplicate_deployment_id_is_refused(ledger):
    deploy(ledger, "d1", "1.0")
    with pytest.raises(ConfigurationError):
        deploy(ledger, "d1", "1.1")


# --- gate 11: rollback ------------------------------------------------------

def test_rolling_back_is_autonomous_because_it_only_retreats(ledger):
    """Olympus can always return to a version a human already deployed; it can
    never advance to one they did not."""
    deploy(ledger, "d1", "1.0")
    deploy(ledger, "d2", "1.1")
    restored = ledger.rollback("momentum", reason="drawdown breach", actor=OLYMPUS)
    assert restored.deployment_id == "d1"
    assert restored.state is R.DeploymentState.ACTIVE
    assert ledger.get("d2").state is R.DeploymentState.ROLLED_BACK
    assert ledger.active("momentum").deployment_id == "d1"


def test_a_rollback_is_distinguished_from_an_ordinary_supersession(ledger):
    """'We moved on' and 'we retreated' are different facts."""
    deploy(ledger, "d1", "1.0")
    deploy(ledger, "d2", "1.1")
    ledger.rollback("momentum", reason="bad", actor=OLYMPUS)
    assert ledger.get("d2").state is R.DeploymentState.ROLLED_BACK
    assert ledger.get("d2").rollback_reason == "bad"


def test_rolling_back_a_first_deployment_is_refused_with_advice(ledger):
    """Rolling 'back' to something nobody deployed would be a deployment."""
    deploy(ledger, "d1", "1.0")
    with pytest.raises(PromotionDenied) as exc:
        ledger.rollback("momentum", reason="bad", actor=OLYMPUS)
    assert "Suspend the capability instead" in str(exc.value)


def test_a_rollback_must_record_why(ledger):
    deploy(ledger, "d1", "1.0")
    deploy(ledger, "d2", "1.1")
    with pytest.raises(ConfigurationError):
        ledger.rollback("momentum", reason="  ", actor=OLYMPUS)


def test_rolling_back_with_no_active_deployment_is_refused(ledger):
    with pytest.raises(ConfigurationError):
        ledger.rollback("nothing-here", reason="x", actor=OLYMPUS)


# --- triggers ---------------------------------------------------------------

@pytest.mark.parametrize("metrics,expected", [
    ({"max_drawdown": 0.4}, R.RollbackTrigger.EXCESSIVE_DRAWDOWN),
    ({"forecast_error_increase": 1.2}, R.RollbackTrigger.FORECAST_DEGRADATION),
    ({"order_reject_rate": 0.5}, R.RollbackTrigger.ABNORMAL_ORDER_BEHAVIOUR),
    ({"data_completeness": 0.5}, R.RollbackTrigger.DATA_QUALITY_DETERIORATION),
    ({"execution_failure_rate": 0.5}, R.RollbackTrigger.EXECUTION_FAILURES),
    ({"audit_failures": 1}, R.RollbackTrigger.AUDIT_FAILURE),
    ({"reconciliation_failures": 1}, R.RollbackTrigger.RECONCILIATION_FAILURE),
    ({"unexpected_capability_access": "lab requested live credentials"},
     R.RollbackTrigger.UNEXPECTED_CAPABILITY_ACCESS),
    ({"paper_divergence_in_vol": 5.0}, R.RollbackTrigger.PAPER_DIVERGENCE),
])
def test_each_specified_trigger_fires(metrics, expected):
    fired = R.evaluate_triggers(metrics)
    assert expected in {t.trigger for t in fired}


def test_a_single_audit_failure_is_enough():
    """There is no acceptable rate of an unverifiable audit chain."""
    assert R.evaluate_triggers({"audit_failures": 1})
    assert R.evaluate_triggers({"audit_failures": 0}) == []


def test_healthy_metrics_fire_nothing():
    assert R.evaluate_triggers({
        "max_drawdown": 0.05, "forecast_error_increase": 0.0,
        "order_reject_rate": 0.0, "data_completeness": 1.0,
        "execution_failure_rate": 0.0, "audit_failures": 0,
        "reconciliation_failures": 0, "paper_divergence_in_vol": 0.2}) == []


def test_an_unmeasured_metric_does_not_fire_but_is_reported():
    """An unmeasured metric is not evidence of a problem, and not evidence of
    its absence either."""
    assert R.evaluate_triggers({}) == []
    assert set(R.unmeasured_triggers({})) == set(R.TRIGGER_METRICS)
    assert "max_drawdown" not in R.unmeasured_triggers({"max_drawdown": 0.1})


def test_a_fired_trigger_cites_its_measurement_and_threshold():
    fired = R.evaluate_triggers({"max_drawdown": 0.4})[0]
    assert fired.measured == 0.4
    assert fired.threshold == R.TriggerThresholds().max_drawdown
    assert "40" in fired.detail


def test_check_and_rollback_acts_when_a_trigger_fires(ledger):
    deploy(ledger, "d1", "1.0")
    deploy(ledger, "d2", "1.1")
    fired, restored = ledger.check_and_rollback("momentum", {"max_drawdown": 0.5})
    assert fired and restored.deployment_id == "d1"
    assert ledger.get("d2").triggers_fired


def test_check_and_rollback_still_reports_triggers_when_it_cannot_retreat(ledger):
    """Swallowing them would turn 'we cannot roll back' into 'nothing was wrong'."""
    deploy(ledger, "d1", "1.0")
    fired, restored = ledger.check_and_rollback("momentum", {"max_drawdown": 0.5})
    assert fired and restored is None


def test_check_and_rollback_does_nothing_when_healthy(ledger):
    deploy(ledger, "d1", "1.0")
    deploy(ledger, "d2", "1.1")
    fired, restored = ledger.check_and_rollback("momentum", {"max_drawdown": 0.01})
    assert fired == [] and restored is None
    assert ledger.active("momentum").deployment_id == "d2"


# --- post-rollback reconciliation -------------------------------------------

def test_the_checklist_names_orders_and_positions_left_behind(ledger):
    deploy(ledger, "d1", "1.0")
    deploy(ledger, "d2", "1.1")
    ledger.rollback("momentum", reason="bad", actor=OLYMPUS)
    tasks = ledger.reconcile_after("momentum", open_orders=2, open_positions=1)
    areas = {t.area for t in tasks}
    assert "orders" in areas and "positions" in areas
    assert all(t.blocking for t in tasks if t.area in ("orders", "positions"))


def test_the_checklist_reports_configuration_differences(ledger):
    deploy(ledger, "d1", "1.0", config={"stop_atr": 2.0})
    deploy(ledger, "d2", "1.1", config={"stop_atr": 3.0})
    ledger.rollback("momentum", reason="bad", actor=OLYMPUS)
    tasks = ledger.reconcile_after("momentum")
    config_task = next(t for t in tasks if t.area == "configuration")
    assert "stop_atr" in config_task.description


def test_the_checklist_flags_changed_dependency_versions():
    rolled = R.DeploymentRecord(
        deployment_id="d2", capability_id="c", version="1.1",
        deployed_at=T0, deployed_by="alice", config={"a": 1},
        dependency_versions={"numpy": "2.0"})
    restored = R.DeploymentRecord(
        deployment_id="d1", capability_id="c", version="1.0",
        deployed_at=T0 - timedelta(days=1), deployed_by="alice",
        config={"a": 1}, dependency_versions={"numpy": "1.26"})
    tasks = R.reconcile_after(rolled, restored)
    dependency_task = next(t for t in tasks if t.area == "dependencies")
    assert "numpy=1.26" in dependency_task.description


def test_knowledge_written_by_the_rolled_back_version_is_flagged_non_blocking(ledger):
    deploy(ledger, "d1", "1.0")
    deploy(ledger, "d2", "1.1")
    ledger.rollback("momentum", reason="bad", actor=OLYMPUS)
    tasks = ledger.reconcile_after("momentum", knowledge_records=5)
    knowledge_task = next(t for t in tasks if t.area == "knowledge")
    assert knowledge_task.blocking is False


def test_reconciling_with_nothing_rolled_back_returns_nothing(ledger):
    deploy(ledger, "d1", "1.0")
    assert ledger.reconcile_after("momentum") == []


# --- history ----------------------------------------------------------------

def test_history_is_ordered_oldest_first(ledger):
    clock = FixedClock(T0)
    ledger = R.DeploymentLedger(clock=clock)
    deploy(ledger, "d1", "1.0")
    clock.advance(timedelta(days=1))
    deploy(ledger, "d2", "1.1")
    assert [r.deployment_id for r in ledger.history("momentum")] == ["d1", "d2"]


def test_history_is_scoped_to_one_capability(ledger):
    deploy(ledger, "d1", "1.0")
    deploy(ledger, "x1", "1.0", capability_id="other")
    assert [r.deployment_id for r in ledger.history("momentum")] == ["d1"]


def test_deployments_persist_across_ledger_instances(ledger):
    deploy(ledger, "d1", "1.0")
    fresh = R.DeploymentLedger(clock=FixedClock(T0))
    assert fresh.active("momentum").deployment_id == "d1"


def test_a_record_round_trips_through_json(ledger):
    record = deploy(ledger, "d1", "1.0")
    assert R.DeploymentRecord.from_dict(record.to_dict()).signature == record.signature

"""The capability registry — Phase 10 completion gate 10.

Gate 10: promote capabilities only through authorised gates.

Three refusals carry the gate: a capability cannot promote itself, cannot skip
a rung, and cannot substitute a rationale for a recorded result.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from olympus.trading import capabilities as C
from olympus.trading.clock import FixedClock
from olympus.trading.errors import (ConfigurationError, GovernanceViolation,
                                    PromotionDenied)
from olympus.trading.governance import Actor

T0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
OPERATOR = Actor.operator("alice", token="tok")
OLYMPUS = Actor.autonomous("evolution-cycle")


@pytest.fixture()
def registry():
    return C.CapabilityRegistry(clock=FixedClock(T0))


def declare(registry, capability_id="cap-1", **kwargs):
    kwargs.setdefault("name", "Momentum strategy")
    kwargs.setdefault("description", "A volatility-targeted momentum strategy.")
    kwargs.setdefault("version", "1.0.0")
    kwargs.setdefault("owner", "alice")
    kwargs.setdefault("source", "olympus.trading.strategy")
    return registry.declare(capability_id, **kwargs)


def evidence_for(registry, capability_id, target):
    for stage in C.REQUIRED_EVIDENCE[target]:
        registry.record_evidence(capability_id, stage,
                                 reference=f"artefact-{stage.value}")


def climb_to(registry, capability_id, target):
    """Walk the ladder properly, recording evidence at each rung."""
    while True:
        current = registry.get(capability_id).state
        if current is target:
            return
        nxt = C.next_state(current)
        assert nxt is not None
        evidence_for(registry, capability_id, nxt)
        registry.promote(capability_id, actor=OPERATOR, reason=f"to {nxt.value}")


# --- the ten states ---------------------------------------------------------

def test_the_registry_declares_all_ten_states():
    assert len(C.CapabilityState) == 10
    for name in ("proposed", "researching", "prototype", "backtest_only",
                 "paper_approved", "shadow_approved",
                 "restricted_live_approved", "production", "suspended",
                 "deprecated"):
        assert C.CapabilityState(name)


def test_suspended_and_deprecated_are_off_the_promotion_ladder():
    assert C.CapabilityState.SUSPENDED not in C.PROMOTION_ORDER
    assert C.CapabilityState.DEPRECATED not in C.PROMOTION_ORDER


def test_the_lifecycle_has_all_fourteen_stages():
    assert len(C.LifecycleStage) == 14


def test_a_capability_records_every_required_field(registry):
    capability = declare(registry, required_permissions=("market_data",),
                         required_data=("1h OHLCV",),
                         dependencies=("olympus.trading.ta",),
                         supported_markets=("crypto",),
                         supported_instruments=("BINANCE:BTCUSDT",),
                         known_limitations=("untested below 1h",),
                         risk_class=C.RiskClass.DECISION_AFFECTING)
    data = capability.to_dict()
    for field in ("name", "description", "version", "owner", "source",
                  "required_permissions", "required_data", "dependencies",
                  "supported_markets", "supported_instruments", "risk_class",
                  "state", "known_limitations", "last_validated_at",
                  "performance_history", "deprecated"):
        assert field in data, field


# --- gate 10: promotion is gated --------------------------------------------

def test_declaration_always_lands_in_proposed(registry):
    """Registration confers nothing, however well tested its author believes."""
    assert declare(registry).state is C.CapabilityState.PROPOSED


def test_a_capability_cannot_promote_itself(registry):
    """The load-bearing refusal. No argument or evidence bundle changes it."""
    declare(registry)
    evidence_for(registry, "cap-1", C.CapabilityState.RESEARCHING)
    with pytest.raises(GovernanceViolation):
        registry.promote("cap-1", actor=OLYMPUS, reason="my metrics are good")
    assert registry.get("cap-1").state is C.CapabilityState.PROPOSED


def test_promotion_advances_one_rung_at_a_time(registry):
    declare(registry)
    evidence_for(registry, "cap-1", C.CapabilityState.RESEARCHING)
    with pytest.raises(PromotionDenied) as exc:
        registry.promote("cap-1", actor=OPERATOR, to=C.CapabilityState.PRODUCTION,
                         reason="it is obviously ready")
    assert "one state at a time" in str(exc.value)
    assert exc.value.details["next_allowed"] == "researching"


def test_missing_evidence_names_exactly_what_is_absent(registry):
    """A persuasive rationale with no walk-forward result is refused identically
    to no rationale at all."""
    declare(registry)
    climb_to(registry, "cap-1", C.CapabilityState.BACKTEST_ONLY)
    with pytest.raises(PromotionDenied) as exc:
        registry.promote("cap-1", actor=OPERATOR,
                         reason="the Sharpe is excellent and the logic is sound")
    missing = set(exc.value.details["missing"])
    assert missing == {"walk_forward", "baseline_comparison", "robustness"}


def test_evidence_recording_is_autonomous_but_promotion_is_not(registry):
    """A walk-forward run should write its own result; what it cannot do is act
    on it."""
    declare(registry)
    updated = registry.record_evidence("cap-1", C.LifecycleStage.PROPOSAL,
                                       reference="hyp-1", recorded_by="olympus")
    assert C.LifecycleStage.PROPOSAL in updated.stages_passed
    with pytest.raises(GovernanceViolation):
        registry.promote("cap-1", actor=OLYMPUS, reason="evidence recorded")


def test_evidence_must_reference_a_checkable_artefact(registry):
    declare(registry)
    with pytest.raises(ConfigurationError):
        registry.record_evidence("cap-1", C.LifecycleStage.UNIT_TESTING,
                                 reference="  ")


def test_a_failed_stage_does_not_count_as_passed(registry):
    declare(registry)
    registry.record_evidence("cap-1", C.LifecycleStage.PROPOSAL,
                             reference="hyp-1", passed=False)
    assert C.LifecycleStage.PROPOSAL not in registry.get("cap-1").stages_passed
    assert C.LifecycleStage.PROPOSAL in registry.get("cap-1").stages_failed
    with pytest.raises(PromotionDenied):
        registry.promote("cap-1", actor=OPERATOR, reason="proposed")


def test_a_later_pass_supersedes_an_earlier_failure_without_erasing_it(registry):
    declare(registry)
    registry.record_evidence("cap-1", C.LifecycleStage.UNIT_TESTING,
                             reference="run-1", passed=False)
    registry.record_evidence("cap-1", C.LifecycleStage.UNIT_TESTING,
                             reference="run-2", passed=True)
    capability = registry.get("cap-1")
    assert C.LifecycleStage.UNIT_TESTING in capability.stages_passed
    assert len(capability.evidence) == 2, "the failed run is still on record"


@pytest.mark.parametrize("verdict", [None, 0, 1, "", "false", [], {}])
def test_evidence_verdict_must_be_an_explicit_boolean(registry, verdict):
    """False-looking strings and numbers must not become passing evidence."""
    declare(registry)
    with pytest.raises(ConfigurationError, match="explicit boolean"):
        registry.record_evidence(
            "cap-1", C.LifecycleStage.PROPOSAL,
            reference="proposal-1", passed=verdict)
    assert registry.get("cap-1").evidence == ()


@pytest.mark.parametrize("verdict", [True, False])
def test_explicit_persisted_evidence_verdict_round_trips(registry, verdict):
    declare(registry)
    registry.record_evidence(
        "cap-1", C.LifecycleStage.PROPOSAL,
        reference="proposal-1", passed=verdict)
    restored = C.CapabilityRegistry(clock=FixedClock(T0)).get("cap-1")
    assert restored.evidence[0].passed is verdict


@pytest.mark.parametrize("verdict", [pytest.param(None, id="missing"),
                                      pytest.param("false", id="string"),
                                      pytest.param(0, id="integer")])
def test_malformed_persisted_evidence_cannot_authorise_promotion(
        registry, verdict):
    """A damaged store record must stop at deserialization, not earn a rung."""
    capability = declare(registry)
    payload = capability.to_dict()
    evidence = {
        "stage": C.LifecycleStage.PROPOSAL.value,
        "recorded_at": T0.isoformat(),
        "reference": "proposal-1",
        "summary": "",
        "recorded_by": "system",
    }
    if verdict is not None:
        evidence["passed"] = verdict
    payload["evidence"] = [evidence]
    registry._store().put(("cap-1",), payload)

    with pytest.raises(ConfigurationError, match="explicit boolean"):
        registry.promote("cap-1", actor=OPERATOR, reason="to researching")
    assert registry._store().get(("cap-1",))["state"] == "proposed"


def test_promotion_requires_a_reason(registry):
    declare(registry)
    evidence_for(registry, "cap-1", C.CapabilityState.RESEARCHING)
    with pytest.raises(ConfigurationError):
        registry.promote("cap-1", actor=OPERATOR, reason="   ")


def test_a_full_climb_to_production_is_possible_with_evidence(registry):
    declare(registry)
    climb_to(registry, "cap-1", C.CapabilityState.PRODUCTION)
    capability = registry.get("cap-1")
    assert capability.state is C.CapabilityState.PRODUCTION
    assert capability.live_capable is True
    assert capability.state_changed_by == "alice"


def test_a_safety_relevant_capability_needs_a_recorded_human_review(registry):
    """Checked independently of the ladder's own evidence requirements."""
    declare(registry, "cap-safety", risk_class=C.RiskClass.SAFETY_RELEVANT)
    climb_to(registry, "cap-safety", C.CapabilityState.SHADOW_APPROVED)
    registry.record_evidence("cap-safety", C.LifecycleStage.SHADOW_TRADING,
                             reference="shadow-1")
    with pytest.raises(PromotionDenied) as exc:
        registry.promote("cap-safety", actor=OPERATOR, reason="ready")
    assert "human_review" in str(exc.value)


# --- suspension and deprecation ---------------------------------------------

def test_suspension_is_autonomous(registry):
    """Requiring approval to halt means a misbehaving capability keeps running
    while a human is found."""
    declare(registry)
    capability = registry.suspend("cap-1", reason="drift critical", actor=OLYMPUS)
    assert capability.state is C.CapabilityState.SUSPENDED


def test_a_suspended_capability_cannot_be_promoted(registry):
    declare(registry)
    climb_to(registry, "cap-1", C.CapabilityState.PROTOTYPE)
    registry.suspend("cap-1", reason="halt", actor=OLYMPUS)
    with pytest.raises(PromotionDenied) as exc:
        registry.promote("cap-1", actor=OPERATOR, reason="fixed")
    assert "reinstated" in str(exc.value)


def test_reinstatement_is_operator_only_and_still_needs_the_evidence(registry):
    declare(registry)
    climb_to(registry, "cap-1", C.CapabilityState.BACKTEST_ONLY)
    registry.suspend("cap-1", reason="halt", actor=OLYMPUS)
    with pytest.raises(GovernanceViolation):
        registry.reinstate("cap-1", to=C.CapabilityState.BACKTEST_ONLY,
                           actor=OLYMPUS, reason="better")
    with pytest.raises(PromotionDenied):
        registry.reinstate("cap-1", to=C.CapabilityState.PAPER_APPROVED,
                           actor=OPERATOR, reason="skip ahead")
    restored = registry.reinstate("cap-1", to=C.CapabilityState.BACKTEST_ONLY,
                                  actor=OPERATOR, reason="root cause fixed")
    assert restored.state is C.CapabilityState.BACKTEST_ONLY


def test_deprecation_is_autonomous_and_permanent(registry):
    declare(registry)
    registry.deprecate("cap-1", reason="superseded", actor=OLYMPUS)
    capability = registry.get("cap-1")
    assert capability.deprecated is True
    with pytest.raises(PromotionDenied):
        registry.promote("cap-1", actor=OPERATOR, reason="bring it back")


# --- point of use -----------------------------------------------------------

def test_a_non_live_capability_is_refused_for_live_use(registry):
    declare(registry)
    climb_to(registry, "cap-1", C.CapabilityState.SHADOW_APPROVED)
    registry.assert_usable("cap-1", live=False)
    with pytest.raises(PromotionDenied) as exc:
        registry.assert_usable("cap-1", live=True)
    assert exc.value.details["state"] == "shadow_approved"


def test_a_suspended_capability_is_refused_everywhere(registry):
    declare(registry)
    registry.suspend("cap-1", reason="halt", actor=OLYMPUS)
    with pytest.raises(PromotionDenied):
        registry.assert_usable("cap-1", live=False)


# --- records ----------------------------------------------------------------

def test_performance_history_is_append_only(registry):
    declare(registry)
    registry.record_performance("cap-1", metrics={"sharpe": 1.2}, sample=100)
    registry.record_performance("cap-1", metrics={"sharpe": 0.3}, sample=100)
    capability = registry.get("cap-1")
    assert len(capability.performance_history) == 2
    assert capability.latest_performance().metrics["sharpe"] == 0.3


def test_redeclaring_an_id_is_refused(registry):
    declare(registry)
    with pytest.raises(ConfigurationError):
        declare(registry)


def test_a_capability_must_name_its_owner_and_source(registry):
    with pytest.raises(ConfigurationError):
        registry.declare("x", name="n", description="d", version="1",
                         owner="", source="s")


def test_records_survive_a_new_registry_instance(registry):
    declare(registry)
    fresh = C.CapabilityRegistry(clock=FixedClock(T0))
    assert fresh.get("cap-1") is not None


def test_the_report_lists_live_capable_capabilities(registry):
    declare(registry)
    climb_to(registry, "cap-1", C.CapabilityState.PRODUCTION)
    declare(registry, "cap-2")
    report = registry.report()
    assert report["live_capable"] == ["cap-1"]
    assert report["by_state"]["proposed"] == 1

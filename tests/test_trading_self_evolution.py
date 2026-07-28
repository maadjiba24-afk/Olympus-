"""The thirteen Phase 10 completion gates, demonstrated end to end.

The per-module suites test each mechanism in isolation. This file exists to
show them working *together* on one story, and to make the gate list
mechanically checkable: `GATES` below is the specification's list, and
`test_every_gate_has_a_demonstration` fails if a gate loses its test.

The story: a strategy that used to work stops working. Olympus notices from its
own recorded outcomes, writes down what it learned, restricts the deteriorating
model on its own authority, forms a testable hypothesis, tests it in a sandbox
that has no production access, refuses the result because it does not beat the
baseline, and produces a code proposal it cannot merge. A human does every step
that increases risk. Every step lands in the audit trail.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from olympus.trading import capabilities as C
from olympus.trading import champion as CH
from olympus.trading import drift as D
from olympus.trading import evolution as E
from olympus.trading import governance as G
from olympus.trading import hypotheses as H
from olympus.trading import kernel as K
from olympus.trading import knowledge as KB
from olympus.trading import lab as L
from olympus.trading import outcomes as O
from olympus.trading import proposals as P
from olympus.trading import rollback as R
from olympus.trading.clock import FixedClock
from olympus.trading.contracts import Regime, Side
from olympus.trading.errors import (GovernanceViolation, PromotionDenied,
                                    SafetyKernelViolation)

T0 = datetime(2027, 1, 4, tzinfo=timezone.utc)
KEY = "BINANCE:BTCUSDT"
OPERATOR = G.Actor.operator("alice", token="tok-1")
OLYMPUS = G.Actor.autonomous("evolution-cycle")

#: The gate list, verbatim from the specification. Each maps to the test that
#: demonstrates it.
GATES: dict[int, str] = {
    1: "record forecasts and compare them with actual outcomes",
    2: "detect model and strategy deterioration",
    3: "generate structured research hypotheses",
    4: "run isolated experiments without production access",
    5: "compare challenger and champion versions",
    6: "reject improvements that do not outperform baselines",
    7: "preserve knowledge provenance and confidence",
    8: "identify and deprecate outdated knowledge",
    9: "produce code proposals without autonomously deploying them",
    10: "promote capabilities only through authorised gates",
    11: "roll back a deployed component safely",
    12: "produce a complete audit trail for every evolutionary change",
    13: "prove that no self-evolution mechanism can alter the safety kernel",
}


@pytest.fixture()
def clock():
    return FixedClock(T0)


@pytest.fixture()
def world(clock):
    """Everything wired together, as an operator would wire it."""
    return {
        "journal": O.OutcomeJournal(clock=clock),
        "monitor": D.DeteriorationMonitor(clock=clock),
        "engine": H.HypothesisEngine(clock=clock),
        "knowledge": KB.KnowledgeBase(clock=clock),
        "lab": L.ResearchLab(clock=clock),
        "capabilities": C.CapabilityRegistry(clock=clock),
        "proposals": P.ProposalRegister(clock=clock),
        "deployments": R.DeploymentLedger(clock=clock),
        "ledger": E.EvolutionLedger(clock=clock),
    }


def losing_decisions(journal, n=40):
    """A strategy that was deployed on good evidence and is now losing."""
    for i in range(n):
        journal.open(O.DecisionRecord(
            decision_id=f"d{i}", ts=T0 + timedelta(hours=i),
            instrument_key=KEY, timeframe="1h", strategy_id="momentum-v1",
            strategy_version="1.0", side=Side.BUY, authorised_quantity="1",
            entry_reference_price=100.0, stop_price=98.0,
            forecast_confidence=0.85, regime=Regime.RANGING,
            model_versions={"kronos": "rev-9"},
            forecast_paths=((102.0, 104.0), (101.0, 103.0))))
        journal.resolve(O.Outcome(
            decision_id=f"d{i}", resolved_at=T0 + timedelta(hours=i + 2),
            realised_path=(99.0, 97.0), entry_price=100.0, exit_price=97.0,
            filled_quantity="1", gross_pnl=-3.0, fees=0.1, mfe=0.0, mae=3.0,
            exit_reason=O.ExitReason.STOP, baseline_pnl=0.5))


# --- the whole story --------------------------------------------------------

def test_the_full_loop_from_a_losing_strategy_to_a_reviewed_proposal(world, clock):
    journal, monitor, engine = world["journal"], world["monitor"], world["engine"]
    knowledge, ledger = world["knowledge"], world["ledger"]

    # 1. Olympus records its decisions and compares them with what happened.
    losing_decisions(journal)
    assert journal.summary()["resolved"] == 40
    assert journal.summary()["directional_accuracy"] == 0.0

    # It notices the failure is systematic, not a bad week.
    weaknesses = journal.weaknesses()
    assert weaknesses
    axes = {w.axis for w in weaknesses}
    assert "directional_accuracy" in axes
    assert "over_trading" in axes
    assert "versus_baseline" in axes

    # 2. And that the model behind it has deteriorated.
    signal = D.DriftSignal(kind=D.DriftKind.CALIBRATION, subject="kronos",
                           metric="confidence_gap", severity=D.Severity.CRITICAL,
                           detail="stated confidence no longer predicts accuracy")
    cycle = E.EvolutionCycle(clock=clock, journal=journal, monitor=monitor,
                             hypothesis_engine=engine, knowledge_base=knowledge,
                             ledger=ledger)
    report = cycle.run(drift_signals=[signal],
                       component_kinds={"kronos": "model"})

    assert monitor.health("kronos").state is D.ComponentState.SHADOW
    assert monitor.health("kronos").operating is False
    assert report.errors == ()

    # 7. What it learned is stored with provenance, not as a bare number.
    assert report.knowledge_recorded
    learned = knowledge.head(report.knowledge_recorded[0])
    assert learned.source == "outcomes.detect_weaknesses"
    assert learned.supporting[0].origin == "outcome-journal"

    # 3. The weakness becomes a hypothesis that can fail.
    assert report.hypotheses
    proposal = engine.get(report.hypotheses[0])
    assert proposal.testable
    assert proposal.failure_criteria
    assert proposal.experiment.baselines

    # 12. Every step of that is in the evolution ledger.
    stages = {e.stage for e in ledger.events()}
    assert E.EvolutionStage.RESTRICTION in stages
    assert E.EvolutionStage.HYPOTHESIS in stages
    assert E.EvolutionStage.OBSERVATION in stages


# --- gate by gate -----------------------------------------------------------

def test_gate_1_forecasts_are_compared_with_outcomes(world):
    losing_decisions(world["journal"], n=5)
    pairs = world["journal"].resolved()
    assert len(pairs) == 5
    _, evaluation = pairs[0]
    assert evaluation.directional_correct is False
    assert evaluation.net_performance is not None
    assert evaluation.abstention_would_have_been_better is True


def test_gate_2_deterioration_is_detected_and_acted_on(world):
    monitor = world["monitor"]
    health = monitor.evaluate(
        "momentum-v1", kind="strategy",
        signals=[D.DriftSignal(kind=D.DriftKind.MODEL, subject="momentum-v1",
                               metric="edge", severity=D.Severity.MAJOR,
                               detail="edge halved")],
        performance=D.PerformanceSnapshot(directional_accuracy=0.30,
                                          max_drawdown=0.05, failure_rate=0.0,
                                          sample=200))
    assert health.state is D.ComponentState.DISABLED
    assert any("hard threshold crossed" in r for r in health.reasons)


def test_gate_3_hypotheses_are_structured_and_falsifiable(world):
    losing_decisions(world["journal"])
    generated = world["engine"].generate(weaknesses=world["journal"].weaknesses())
    assert generated
    for proposal in generated:
        assert proposal.hypothesis and proposal.motivation
        assert proposal.success_criteria and proposal.failure_criteria
        assert proposal.experiment.baselines
        assert proposal.risk_level and proposal.production_impact
        assert proposal.experiment.estimated_cost_units > 0


def test_gate_4_experiments_cannot_reach_production(world):
    proposal = H.kronos_conditional_value(instrument=KEY, clock=FixedClock(T0))

    def greedy(sandbox):
        sandbox.request("production_order_submission")
        return [0.1] * 100

    with pytest.raises(L.ExperimentError):
        world["lab"].run(proposal,
                         L.AblationExperiment(variants={"v": greedy},
                                              baselines={"flat": lambda s: [0.0] * 100}))
    assert world["lab"].stats()["sandbox_breach_attempts"] == 1


def test_gate_5_champion_and_challenger_are_compared_on_matched_terms(clock):
    board = CH.ChampionChallengerBoard("btc-momentum", clock=clock)
    harness = CH.EvaluationHarness(dataset_id="btc-1h", dataset_hash="sha:a",
                                   cost_fingerprint="fee10", risk_limits_hash="v3",
                                   start=T0, end=T0 + timedelta(days=90))
    import random

    def arm(name, mean, complexity, seed):
        rng = random.Random(seed)
        return CH.ArmResult(
            contender=CH.Contender(contender_id=name, kind="strategy",
                                   version="1", complexity=complexity),
            harness=harness,
            net_returns=[rng.gauss(mean, 0.01) for _ in range(200)])

    board.set_champion(CH.Contender(contender_id="champ", kind="strategy",
                                    version="1", complexity=40),
                       actor=OPERATOR, reason="incumbent")
    board.add_challenger(CH.Contender(contender_id="chall", kind="strategy",
                                      version="2", complexity=6), actor=OLYMPUS)
    outcome = board.run_contest(arm("champ", 0.0, 40, 1),
                                arm("chall", 0.01, 6, 2))
    assert outcome.decision is CH.Decision.REPLACE
    # Installing is still a human act.
    with pytest.raises(GovernanceViolation):
        board.install("chall", actor=OLYMPUS, reason="it won")
    board.install("chall", actor=OPERATOR, reason="reviewed the contest")
    assert board.champion().contender_id == "chall"


def test_gate_6_a_result_that_does_not_beat_the_baseline_is_rejected(world):
    proposal = H.kronos_conditional_value(clock=FixedClock(T0))
    result = world["lab"].run(
        proposal,
        L.ModelComparisonExperiment(
            variants={"with kronos": lambda s: [0.001] * 200},
            baselines={"without kronos": lambda s: [0.004] * 200,
                       "always flat": lambda s: [0.0] * 200}))
    assert result.verdict is L.Verdict.REFUTED
    assert result.beat_every_baseline is False


def test_gate_7_provenance_and_confidence_are_preserved(world):
    base = world["knowledge"]
    base.record("btc-overnight", statement="Overnight liquidity is thin.",
                source="internal-observation",
                source_type=KB.SourceType.LIVE_OBSERVATION,
                event_at=T0, instruments=[KEY], market="crypto",
                confidence=0.4)
    for i in range(8):
        base.corroborate("btc-overnight",
                         KB.Evidence(ref=f"blog-{i}", origin="one-press-release"))
    record = base.head("btc-overnight")
    assert len(record.supporting) == 1, "one origin cannot vote eight times"
    assert record.source and record.event_at and record.collected_at


def test_gate_8_outdated_knowledge_is_deprecated_without_being_erased(clock, world):
    base = world["knowledge"]
    base.record("fee-holiday", statement="Maker fees waived until 8 January.",
                source="exchange-notice",
                source_type=KB.SourceType.EXCHANGE_DOCUMENTATION,
                event_at=T0, expires_at=T0 + timedelta(days=4))
    clock.advance(timedelta(days=5))
    assert [r.knowledge_id for r in base.expire_stale()] == ["fee-holiday"]
    assert base.head("fee-holiday").validation is KB.Validation.EXPIRED
    assert base.for_decision() == []
    # The belief that was acted on last week is still readable.
    assert "waived" in base.get("fee-holiday", version=1).statement


def test_gate_9_code_proposals_are_produced_and_never_deployed(world):
    register = world["proposals"]
    defect = P.DefectReport(
        defect_id="def-1", module="olympus/trading/ta.py",
        severity=P.DefectSeverity.CORRECTNESS,
        summary="the ATR warm-up region is one bar short", detected_at=T0)
    proposal = register.submit_code_change(P.CodeChangeProposal(
        proposal_id="fix-atr", title="Fix the ATR warm-up off-by-one",
        created_at=T0, target_files=("olympus/trading/ta.py",),
        diff="--- a/olympus/trading/ta.py\n+++ b/olympus/trading/ta.py\n+ fixed\n",
        rationale=defect.summary,
        expected_impact="ATR shifts by one bar during warm-up only",
        rollback_plan="revert the commit",
        tests_added=("tests/test_trading_ta.py::test_atr_warmup",),
        fixes_defect="def-1"), actor=OLYMPUS)
    register.mark_ci_result("fix-atr", status=P.CIStatus.PASSED)

    assert register.code_change("fix-atr").ready_for_review is True
    assert register.report()["applied"] == 0
    # Only a human can even record acceptance, and that still enacts nothing.
    with pytest.raises(GovernanceViolation):
        register.record_review("fix-atr", kind="code", actor=OLYMPUS,
                               accepted=True)
    reviewed = register.record_review("fix-atr", kind="code", actor=OPERATOR,
                                      accepted=True, notes="correct fix")
    assert reviewed["state"] == "accepted"
    assert "outside olympus.trading" in reviewed["note"]


def test_gate_10_capabilities_are_promoted_only_through_authorised_gates(world):
    registry = world["capabilities"]
    registry.declare("momentum-v2", name="Momentum v2",
                     description="revised momentum strategy", version="2.0",
                     owner="alice", source="olympus.trading.strategy",
                     risk_class=C.RiskClass.DECISION_AFFECTING)
    registry.record_evidence("momentum-v2", C.LifecycleStage.PROPOSAL,
                             reference="hyp-1", recorded_by="olympus")
    # Olympus recorded its own evidence and still cannot promote itself.
    with pytest.raises(GovernanceViolation):
        registry.promote("momentum-v2", actor=OLYMPUS, reason="evidence is in")
    registry.promote("momentum-v2", actor=OPERATOR, reason="begin research")
    assert registry.get("momentum-v2").state is C.CapabilityState.RESEARCHING
    # And it cannot jump the queue even with an operator.
    with pytest.raises(PromotionDenied):
        registry.promote("momentum-v2", actor=OPERATOR,
                         to=C.CapabilityState.PRODUCTION, reason="looks great")


def test_gate_11_a_deployed_component_rolls_back_safely(world):
    ledger = world["deployments"]
    common = dict(capability_id="momentum", actor=OPERATOR,
                  dependency_versions={"python": "3.12.2"},
                  rollback_procedure="restore the previous deployment id")
    ledger.deploy(deployment_id="dep-1", version="1.0",
                  config={"stop_atr": 2.0}, evidence_refs=("exp-1",), **common)
    ledger.deploy(deployment_id="dep-2", version="2.0",
                  config={"stop_atr": 4.0}, evidence_refs=("exp-2",), **common)

    fired, restored = ledger.check_and_rollback(
        "momentum", {"max_drawdown": 0.4, "audit_failures": 0})
    assert R.RollbackTrigger.EXCESSIVE_DRAWDOWN in {t.trigger for t in fired}
    assert restored.deployment_id == "dep-1"

    tasks = ledger.reconcile_after("momentum", open_orders=1, open_positions=1)
    assert {t.area for t in tasks} >= {"orders", "positions", "configuration"}
    assert any(t.blocking for t in tasks)


def test_gate_12_every_evolutionary_change_is_explainable(world):
    ledger = world["ledger"]
    ledger.record(E.EvolutionEvent(
        event_id="promo-1", stage=E.EvolutionStage.DEPLOYMENT, at=T0,
        observed="challenger beat the champion out of sample",
        inferred="the simpler model generalises better",
        proposed="install challenger as champion",
        evidence=("contest:btc-momentum:chall",), experiment="exp-7",
        results={"margin": 0.01}, reviewed_by="alice",
        decision=E.ReviewDecision.ACCEPTED, code_version="abc123",
        model_versions={"momentum": "2.0"}, deployment_id="dep-3",
        deployment_state="active", rollback_target="dep-2",
        subject="momentum", actor="alice", actor_kind="operator"))

    explanation = ledger.explain("promo-1")
    assert explanation.why
    assert explanation.evidence
    assert explanation.authorised_by == "alice"
    assert explanation.active_version == "abc123"
    assert explanation.restorable is True
    assert explanation.rollback_target == "dep-2"
    assert explanation.improved_performance is None       # not yet measurable
    assert explanation.new_risks                           # unmeasured dimensions


def test_gate_13_no_self_evolution_mechanism_can_alter_the_safety_kernel():
    # Structural: no evolution module has a route in.
    assert K.audit_evolution_modules() == []
    # A generated patch cannot even name a kernel file.
    with pytest.raises(SafetyKernelViolation):
        P.CodeChangeProposal(
            proposal_id="widen", title="Widen the daily loss limit",
            created_at=T0, target_files=("olympus/trading/risk.py",),
            diff="+ MAX_DAILY_LOSS = 1.0", rationale="we keep hitting it",
            expected_impact="fewer rejections", rollback_plan="revert",
            tests_added=("tests/test_trading_risk.py",))
    # Nor reach the kernel through a patch body in an innocent file.
    with pytest.raises(SafetyKernelViolation):
        P.CodeChangeProposal(
            proposal_id="sneak", title="Improve reporting", created_at=T0,
            target_files=("olympus/trading/perf.py",),
            diff="--- a/x\n+++ b/x\n+    switches.disengage(scope='global')\n",
            rationale="nicer output", expected_impact="none",
            rollback_plan="revert", tests_added=("tests/test_trading_perf.py",))
    # And the governance layer refuses the actions outright.
    for action in (G.Action.MODIFY_SAFETY_KERNEL, G.Action.MODIFY_RISK_LIMITS,
                   G.Action.ENABLE_LIVE_TRADING, G.Action.CLEAR_KILL_SWITCH,
                   G.Action.EXPAND_PERMISSIONS):
        with pytest.raises(GovernanceViolation):
            G.authorise(action, OLYMPUS)
    # The kernel seal detects any change that happened anyway.
    assert K.verify_seal(K.seal()).intact is True


# --- the gate list itself ---------------------------------------------------

def test_every_gate_has_a_demonstration():
    """A gate that loses its test should fail here rather than quietly stop
    being demonstrated."""
    import inspect
    source = inspect.getsource(__import__(__name__, fromlist=["x"]))
    for number in GATES:
        assert f"def test_gate_{number}_" in source, f"gate {number} has no test"


def test_live_trading_is_still_disabled_by_default():
    """Phase 10 adds no path to live trading, and this is the check that would
    notice if it had."""
    from olympus.trading.contracts import Mode
    from olympus.trading.modes import ModeController
    assert ModeController(clock=FixedClock(T0)).current() is Mode.PAPER
    assert ModeController(clock=FixedClock(T0)).permits_live_orders() is False


def test_the_autonomous_powers_are_exactly_the_specified_ones():
    """The list Olympus may act on without asking. Enumerated so that widening
    it is a visible diff rather than an emergent behaviour."""
    assert {a.value for a in G.AUTONOMOUS_ACTIONS} == {
        "observe", "learn", "evaluate", "research", "generate_hypothesis",
        "build_prototype", "generate_tests", "propose_improvement",
        "recommend_deprecation", "restrict_component", "emergency_shutdown"}

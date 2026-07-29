#!/usr/bin/env python3
"""Phase 4 end to end, twice, printing the trail both times.

    python scripts/native_evolution_demo.py

**Demonstration A** — new evidence → weakness detected → hypothesis created →
experiment isolated → challenger trained → challenger evaluated → weak
challenger rejected.

The challenger is trained on a **random walk**, and that is the point. Nothing
beats persistence on a random walk, so the challenger loses on its merits
rather than because the harness was rigged to reject it. A demonstration whose
rejection came from a hard-coded `return False` would show the plumbing and
nothing else.

**Demonstration B** — deterioration detected → active model restricted →
previously approved model restored → audit trail verified.

Both run against synthetic series and an in-memory store. No real money, no
broker, no market data. Every number here is about the machinery.
"""

from __future__ import annotations

import json
import math
import random
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from olympus.trading.contracts import Regime                        # noqa: E402
from olympus.trading.governance import Actor                        # noqa: E402
from olympus.trading.native import challengers as CH                # noqa: E402
from olympus.trading.native import evidence as EV                   # noqa: E402
from olympus.trading.native import improvement as IM                # noqa: E402
from olympus.trading.native import isolation as ISO                 # noqa: E402
from olympus.trading.native import promotion as PR                  # noqa: E402

START = datetime(2028, 4, 1, tzinfo=timezone.utc)
KEY = "SIM:EVO"


def rule(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def step(n: int, title: str) -> None:
    print(f"\n[{n}] {title}")
    print("-" * 78)


# ===========================================================================
# demonstration A
# ===========================================================================

EXPERIMENT_SOURCE = '''\
"""Fit an autoregressive challenger and score it against persistence.

Runs inside the isolated worker. It reads one read-only dataset, fits both
arms on the training half, scores both on the held-out half with the same
metric, and returns the numbers. It decides nothing.
"""
import json
import statistics
from pathlib import Path


def _fit_ar(returns, order):
    """Least squares on the normal equations, by hand. Small and transparent."""
    rows = [(returns[i - order:i], returns[i])
            for i in range(order, len(returns))]
    if len(rows) <= order:
        return [0.0] * order
    # Ridge-regularised normal equations; the ridge keeps a near-singular
    # design from producing enormous coefficients on a short window.
    gram = [[sum(x[a] * x[b] for x, _ in rows) + (1e-6 if a == b else 0.0)
             for b in range(order)] for a in range(order)]
    rhs = [sum(x[a] * y for x, y in rows) for a in range(order)]
    for col in range(order):
        pivot = max(range(col, order), key=lambda r: abs(gram[r][col]))
        if abs(gram[pivot][col]) < 1e-12:
            return [0.0] * order
        gram[col], gram[pivot] = gram[pivot], gram[col]
        rhs[col], rhs[pivot] = rhs[pivot], rhs[col]
        for row in range(col + 1, order):
            factor = gram[row][col] / gram[col][col]
            rhs[row] -= factor * rhs[col]
            for k in range(col, order):
                gram[row][k] -= factor * gram[col][k]
    coefficients = [0.0] * order
    for row in reversed(range(order)):
        total = rhs[row] - sum(gram[row][k] * coefficients[k]
                               for k in range(row + 1, order))
        coefficients[row] = total / gram[row][row]
    return coefficients


def run(inputs):
    prices = json.loads(Path(inputs["__datasets__"]["prices.json"]).read_text())
    returns = [prices[i] / prices[i - 1] - 1.0 for i in range(1, len(prices))]
    split = int(len(returns) * 0.6)
    train, test = returns[:split], returns[split:]

    order = int(inputs.get("order", 3))
    coefficients = _fit_ar(train, order)

    # Persistence on a *price* series predicts the price does not change,
    # i.e. a return of zero. Predicting "the last return again" is a different
    # and much weaker baseline — the first version of this script used it, and
    # the AR arm beat it by a factor of root two on pure noise purely because
    # of that. A mis-specified baseline is the most common way research
    # produces an improvement that does not exist.
    challenger_errors, persistence_errors = [], []
    window = list(train[-order:])
    for actual in test:
        predicted = sum(c * w for c, w in zip(coefficients, window))
        challenger_errors.append(abs(predicted - actual))
        persistence_errors.append(abs(0.0 - actual))
        window = (window + [actual])[-order:]

    # The per-observation errors travel back, not just their means. A gate
    # that only sees two averages cannot run a paired test, and "0.00299 is
    # smaller than 0.00304" is not a result.
    return {
        "arm": "ar%d_challenger" % order,
        "parameters": order,
        "baseline_parameters": 0,
        "n_scored": len(test),
        "challenger_mae": statistics.fmean(challenger_errors),
        "persistence_mae": statistics.fmean(persistence_errors),
        "challenger_errors": challenger_errors,
        "persistence_errors": persistence_errors,
        "baseline": "persistence: the price does not change, so the predicted "
                    "return is zero",
    }
'''


def build_evidence() -> EV.EvidenceJournal:
    """A journal with a real, findable weakness: the ranging regime is worse."""
    rng = random.Random(20280401)
    journal = EV.EvidenceJournal()
    for i in range(240):
        created = START + timedelta(hours=i)
        ranging = i % 2 == 0
        regime = Regime.RANGING.value if ranging else Regime.TRENDING_UP.value
        declined = i % 17 == 0
        # The model is materially worse in the ranging regime — the weakness
        # the loop is supposed to find. It is put here deliberately so the
        # detector has something true to detect.
        noise = 0.006 if ranging else 0.0015
        predicted = None if declined else rng.gauss(0.0, 0.0008)
        realised = (predicted or 0.0) + rng.gauss(0.0, noise)
        band = 0.004
        pending = EV.PendingForecast(
            forecast_id=f"f{i:04d}", instrument_key=KEY, timeframe="1h",
            created_at=created, input_end=created, horizon=3,
            dataset_version="ds-2028-04", feature_schema_version="olympus.market.v1",
            model_version="native-mt-1", checkpoint_hash="a" * 16,
            predicted_return=predicted,
            quantiles={} if declined else {"p10": predicted - band,
                                           "p90": predicted + band},
            nominal_coverage=None if declined else 0.8,
            uncertainty=None if declined else 0.35,
            dispersion=None if declined else 2 * band,
            abstained=declined,
            abstention_reasons=("excessive_dispersion",) if declined else (),
            regime=regime)
        journal.record(pending)
        journal.mature(pending.forecast_id, realised_return=realised,
                       now=pending.matures_at, modelled_cost_bps=2.0,
                       realised_cost_bps=2.1)
    return journal


def _baseline_check(result: dict) -> tuple[bool, dict]:
    """Beat persistence *significantly*, or lose on parsimony.

    A bare `challenger_mae < persistence_mae` would pass a challenger that is
    0.0000005 better on 160 observations, which is the comparison this whole
    framework exists to stop being made. So: a seeded paired bootstrap on the
    per-observation differences, and — when the two are statistically
    indistinguishable — the simpler arm wins, which is the rule
    `champion.compare()` already applies everywhere else.
    """
    from olympus.trading.evaluate import paired_bootstrap

    challenger = result.get("challenger_errors") or []
    persistence = result.get("persistence_errors") or []
    if len(challenger) != len(persistence) or len(challenger) < 30:
        return False, {"reason": "too few paired observations to compare",
                       "n": len(challenger)}
    # Positive gain means the challenger has the lower error.
    gains = [p - c for c, p in zip(challenger, persistence)]
    test = paired_bootstrap(gains, name="mae_gain", seed=0)
    parameters = int(result.get("parameters", 0))
    baseline_parameters = int(result.get("baseline_parameters", 0))
    simpler = parameters <= baseline_parameters

    passed = bool(test.significant and test.statistic > 0)
    if passed:
        reason = (f"challenger beats persistence by {test.statistic:.3g} MAE, "
                  f"significant at p={test.p_value:.3g}")
    elif not test.significant:
        reason = (f"statistically indistinguishable from persistence "
                  f"(mean gain {test.statistic:+.3g}, p={test.p_value:.3g}) "
                  f"while carrying {parameters} parameters against "
                  f"{baseline_parameters}; the simpler arm wins a tie")
    else:
        reason = (f"significantly worse than persistence "
                  f"(mean gain {test.statistic:+.3g}, p={test.p_value:.3g})")
    return passed, {
        "reason": reason,
        "challenger_mae": result.get("challenger_mae"),
        "persistence_mae": result.get("persistence_mae"),
        "mean_gain": test.statistic, "p_value": test.p_value,
        "significant": test.significant, "n": len(gains),
        "parameters": parameters, "baseline_parameters": baseline_parameters,
        "simpler": simpler}


def demonstration_a() -> dict:
    rule("A.  new evidence -> weakness -> hypothesis -> isolated experiment "
         "-> rejection")

    step(1, "New evidence: mature forecasts recorded with all fourteen fields")
    journal = build_evidence()
    summary = journal.summary()
    print(f"  matured forecasts      {summary.n}")
    print(f"  answered               {summary.n_answered}")
    print(f"  mean absolute error    {summary.mae:.6f}")
    print(f"  calibration error      {summary.calibration_error:+.1f} points")
    print(f"  abstention rate        {summary.abstention_rate:.1%}")
    sample = journal.matured[0].to_dict()
    print(f"  each record carries    {len(sample)} outcome fields over "
          f"{len(sample['forecast'])} forecast fields")
    print(f"  unmeasured on a record {journal.matured[0].unmeasured_fields}")

    step(2, "Weakness detected")
    findings = journal.weaknesses()
    actionable = [f for f in findings if not f.provisional]
    for finding in findings:
        mark = "provisional" if finding.provisional else "ACTIONABLE"
        print(f"  [{mark:>11}] {finding.kind.value:<24} {finding.scope:<22} "
              f"n={finding.n:<4} {finding.detail}")
    if not actionable:
        print("  no actionable weakness; nothing to propose")
        return {"rejected": False}

    step(3, "Hypothesis created, with all eleven required fields")
    proposals = CH.generate(actionable, created_at=START + timedelta(days=20),
                            dataset_ids=("ds-2028-04",))
    chosen = proposals[0]
    print(f"  proposal      {chosen.proposal_id}  ({chosen.kind.value})")
    print(f"  hypothesis    {chosen.hypothesis[:70]}...")
    print(f"  supporting    {len(chosen.supporting_evidence)} item(s)")
    print(f"  contradicting {len(chosen.contradicting_evidence)} item(s)")
    print(f"  success       {len(chosen.success_criteria)} criteria")
    print(f"  failure       {len(chosen.failure_criteria)} criteria")
    print(f"  budget        {chosen.compute_budget.to_dict()}")
    print(f"  security      {chosen.security_impact.value}")
    print(f"  operational   {chosen.operational_impact.value}")
    print(f"  rollback to   {chosen.rollback_target}")
    print(f"  falsifiable   {chosen.falsifiable}")
    print(f"  paired with   {len(proposals)} total proposals, "
          f"{sum(1 for p in proposals if p.kind is CH.ChallengerKind.SIMPLER_MODEL)} "
          f"of them simplifications")
    research = chosen.to_research_proposal()
    print(f"  entered the framework backlog as {research.experiment.kind.value}, "
          f"risk {research.risk_level.value}")

    step(4, "Experiment isolated")
    walk = [100.0]
    rng = random.Random(11)
    for _ in range(400):                       # a random walk: nothing beats
        walk.append(walk[-1] * math.exp(rng.gauss(0.0, 0.004)))   # persistence
    dataset = Path(tempfile.mkdtemp()) / "prices.json"
    dataset.write_text(json.dumps(walk))

    spec = ISO.ExperimentSpec(
        experiment_id=chosen.proposal_id, source=EXPERIMENT_SOURCE,
        inputs={"order": 3}, datasets={"prices.json": str(dataset)},
        budget=chosen.compute_budget, generated=True)
    signed = ISO.sign_inputs(spec)
    print(f"  input digest  {signed.digest[:32]}...")
    print(f"  signed        {signed.signed}, verifies {signed.verify()}")

    step(5, "Challenger trained, inside the worker")
    manifest = ISO.run_isolated(spec)
    for state in manifest.confinement.states:
        mark = "OK " if state.applied else "NO "
        print(f"  {mark} {state.mechanism.value:<22} [{state.basis:<8}] "
              f"{state.detail[:44]}")
    print(f"  verdict       {manifest.verdict.value}")
    print(f"  trustworthy   {manifest.trustworthy}")
    print(f"  result signed {manifest.signed}, verifies {manifest.verify()}")
    print(f"  worker gone   {manifest.destruction.get('workdir_removed')}")
    printable = {k: v for k, v in manifest.result.items()
                 if not isinstance(v, list)}
    print(f"  result        {printable}")

    step(6, "Challenger evaluated through the twelve-stage gate")
    gate = PR.GateLedger()
    challenger_id = "ch-" + chosen.proposal_id[-8:]
    gate.enter(challenger_id=challenger_id, proposal_id=chosen.proposal_id,
               created_at=START + timedelta(days=20))
    at = START + timedelta(days=21)
    result = manifest.result

    def passing(stage):
        return lambda: (True, {"stage": stage.value, "n": result.get("n_scored", 0),
                               "source": "isolated worker",
                               "manifest": manifest.result_digest[:16]})

    checks = {stage: passing(stage) for stage in PR.AUTONOMOUS_STAGES}
    checks[PR.GateStage.BASELINE_COMPARISON] = lambda: _baseline_check(result)
    run = PR.run_autonomous_stages(gate, challenger_id, checks=checks, at=at)

    for stage_result in run.results:
        mark = "pass" if stage_result.passed else "FAIL"
        print(f"  [{mark}] {stage_result.stage.value}")

    step(7, "Weak challenger rejected — autonomously")
    print(f"  outcome       {run.outcome.value}")
    print(f"  failed at     {run.failed_at.stage.value if run.failed_at else '-'}")
    print(f"  reason        {run.rejection_reason or '-'}")
    print(f"  terminal      {run.terminal}")
    print(f"  concealment   {gate.concealment_check() or 'no findings'}")
    evidence = run.evidence_for(PR.GateStage.BASELINE_COMPARISON)
    print()
    print("  The rejection is on the merits. The challenger was fitted on a "
          "random walk;")
    print("  its coefficients shrank toward zero, so it converged on "
          "persistence and")
    print("  could not be distinguished from it — while carrying "
          f"{evidence.get('parameters')} parameters")
    print(f"  against {evidence.get('baseline_parameters')}. "
          "A tie goes to the simpler arm.")
    return {"rejected": run.outcome is PR.GateOutcome.REJECTED,
            "manifest": manifest, "gate": gate, "journal": journal}


# ===========================================================================
# demonstration B
# ===========================================================================

def demonstration_b() -> dict:
    rule("B.  deterioration -> restriction -> restore -> audit trail")

    import uuid

    from olympus.trading import drift as DR
    from olympus.trading import rollback as RB
    from olympus.trading.audit import AuditTrail
    from olympus.trading.clock import FixedClock

    # A fresh namespace per run. The store is durable, and a demonstration that
    # collided with its own previous run would be a demonstration about the
    # store rather than about the machinery.
    run = uuid.uuid4().hex[:8]
    now = START + timedelta(days=40)
    clock = FixedClock(now)
    # Ledger-backed, so the chain is genuinely hash-chained and the
    # verification at step 6 means something. An in-memory trail verifies
    # trivially and would demonstrate nothing.
    audit = AuditTrail(f"native-evolution-demo-{run}", clock=clock,
                       ledger_backed=True)
    operator = Actor.operator("alice", token="operator-token")

    step(1, "Two versions deployed by a human, in order")
    ledger = RB.DeploymentLedger(clock=clock, audit=audit,
                                 namespace=f"demo.{run}.deployments")
    common = dict(capability_id="native-forecaster", actor=operator,
                  config={"lookback": 33, "horizon": 3, "d_model": 16},
                  dependency_versions={"python": "3.11", "torch": "2.4"},
                  evidence_refs=("eval-2028-03",),
                  rollback_procedure="restore the previous checkpoint id and "
                                     "re-run the evaluation to confirm the "
                                     "prior numbers reproduce")
    first = ledger.deploy(deployment_id="dep-1", version="native-mt-1", **common)
    clock.set(now + timedelta(days=1))
    second = ledger.deploy(deployment_id="dep-2", version="native-mt-2",
                           **{**common, "evidence_refs": ("eval-2028-04",)})
    print(f"  dep-1  {first.version}  state={first.state.value}")
    print(f"  dep-2  {second.version}  state={second.state.value}  "
          f"previous={second.previous_deployment_id}")
    print(f"  active {ledger.active('native-forecaster').version}")

    step(2, "Deterioration detected — autonomously, from measurement")
    monitor = DR.DeteriorationMonitor(clock=clock, audit=audit,
                                      namespace=f"demo.{run}.drift")
    reference = [0.0021 + 0.0002 * (i % 5) for i in range(60)]
    current = [0.0074 + 0.0003 * (i % 5) for i in range(60)]
    signals = [
        DR.detect_metric_regression(reference, current, kind=DR.DriftKind.MODEL,
                                    subject="native-forecaster",
                                    metric="forecast_error",
                                    lower_is_better=True),
        DR.detect_distribution_drift(reference, current, kind=DR.DriftKind.MODEL,
                                     subject="native-forecaster",
                                     metric="forecast_error"),
    ]
    for signal in signals:
        if signal is not None:
            print(f"  {signal.kind.value:<24} severity={signal.severity.value:<8} "
                  f"drifted={signal.drifted}")
    health = monitor.evaluate("native-forecaster", kind="model",
                              signals=[s for s in signals if s is not None])
    print(f"  state         {health.state.value}")
    print(f"  reasons       {list(health.reasons)}")

    step(3, "Active model restricted — no operator involved")
    restricted = monitor.restrict("native-forecaster", kind="model",
                                  to=DR.ComponentState.DISABLED,
                                  reason="forecast error tripled against the "
                                         "reference window")
    print(f"  state         {restricted.state.value}")
    print(f"  changed_by    {restricted.changed_by}")
    print(f"  since         {restricted.since.isoformat()}")

    step(4, "Autonomous reinstatement refused")
    from olympus.trading.errors import GovernanceViolation
    try:
        monitor.reinstate("native-forecaster", kind="model",
                          to=DR.ComponentState.HEALTHY,
                          reason="it looks better now",
                          actor=Actor.autonomous("evolution-engine"))
        print("  REINSTATED — this should not happen")
    except GovernanceViolation as error:
        print(f"  refused       {error}")

    step(5, "Previously approved version restored — autonomously")
    triggers = RB.evaluate_triggers(
        {"forecast_error_increase": 2.5, "max_drawdown": 0.22},
        thresholds=RB.TriggerThresholds())
    unmeasured = RB.unmeasured_triggers(
        {"forecast_error_increase": 2.5, "max_drawdown": 0.22})
    for fired in triggers:
        print(f"  trigger       {fired.trigger.value}  "
              f"measured={fired.measured} limit={fired.threshold}")
    print(f"  unmeasured    {list(unmeasured)}")
    print("                (a rollback decision on partial data knows it was "
          "partial)")
    rolled = ledger.rollback("native-forecaster",
                             reason="forecast error tripled; returning to the "
                                    "last version a human approved",
                             triggers=triggers)
    print(f"  restored      {rolled.version} (deployment {rolled.deployment_id})")
    print(f"  active now    {ledger.active('native-forecaster').version}")
    print(f"  destination   was deployed by {rolled.deployed_by}, "
          f"i.e. previously approved")

    step(6, "Audit trail verified")
    events = list(audit.events())
    print(f"  entries       {len(events)}")
    for event in events[-8:]:
        print(f"    {event.type.value:<24} {(event.subject or '-'):<20} "
              f"actor={event.actor}")
    verification = audit.verify()
    print(f"  verification  {verification}")
    history = ledger.history("native-forecaster")
    print("  deployments   " + ", ".join(
        f"{r.version}:{r.state.value}" for r in history))
    return {"restored": rolled.version,
            "audit_ok": bool(verification.get("ok", True)),
            "events": len(events)}


# ===========================================================================

def improvement_note(journal: EV.EvidenceJournal) -> None:
    rule("C.  what may not be used to claim improvement")
    half = len(journal.matured) // 2
    report = IM.from_evidence(journal.matured[:half], journal.matured[half:],
                              trained_instruments=(KEY,),
                              trained_regimes=(Regime.TRENDING_UP.value,))
    print(f"  verdict       {report.verdict.value}")
    print(f"  usable        {len(report.usable)} of {len(IM.NativeMetric)} metrics")
    print(f"  improved      {list(report.improved)}")
    print(f"  regressed     {list(report.regressed)}")
    print(f"  unmeasured    {list(report.unmeasured)}")
    print()
    try:
        IM.measure({"proposals_generated": 1}, {"proposals_generated": 40})
        print("  ACCEPTED a volume metric — this should not happen")
    except Exception as error:
        print("  a volume metric is refused, not ignored:")
        print(f"    {error}")


def main() -> int:
    result_a = demonstration_a()
    result_b = demonstration_b()
    if result_a.get("journal") is not None:
        improvement_note(result_a["journal"])

    rule("summary")
    print(f"  A: weak challenger rejected      {result_a.get('rejected')}")
    print(f"  B: previously approved restored  {result_b.get('restored')}")
    print(f"  B: audit chain verified          {result_b.get('audit_ok')}")
    print()
    print("  No real money was used. No broker was reachable. No market data "
          "was read.")
    return 0 if result_a.get("rejected") and result_b.get("audit_ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

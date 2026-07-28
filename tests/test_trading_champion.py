"""Champion and challengers — Phase 10 completion gate 5.

Gate 5: compare challenger and champion versions.

The test that matters most is the one about a *mismatched* comparison: an arm
measured under different costs, different data or different risk limits is not
comparable, and the framework raises rather than quietly reporting a difference
that is really a difference in conditions.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from olympus.trading import champion as CH
from olympus.trading.clock import FixedClock
from olympus.trading.errors import (ConfigurationError, GovernanceViolation,
                                    PromotionDenied)
from olympus.trading.governance import Actor

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
T1 = T0 + timedelta(days=90)
OPERATOR = Actor.operator("alice", token="tok")
OLYMPUS = Actor.autonomous("board")


def harness(**kwargs):
    kwargs.setdefault("dataset_id", "btc-1h")
    kwargs.setdefault("dataset_hash", "sha:abc")
    kwargs.setdefault("cost_fingerprint", "fee10-slip5")
    kwargs.setdefault("risk_limits_hash", "limits:v3")
    kwargs.setdefault("start", T0)
    kwargs.setdefault("end", T1)
    return CH.EvaluationHarness(**kwargs)


def contender(contender_id, complexity=10, cost=0.0):
    return CH.Contender(contender_id=contender_id, kind="strategy",
                        version="1.0", complexity=complexity,
                        operating_cost_units=cost)


def series(mean, n=200, seed=1, spread=0.01):
    rng = random.Random(seed)
    return [rng.gauss(mean, spread) for _ in range(n)]


def arm(name, mean, *, complexity=10, cost=0.0, h=None, n=200, seed=1,
        by_regime=None):
    return CH.ArmResult(contender=contender(name, complexity, cost),
                        harness=h or harness(),
                        net_returns=series(mean, n, seed),
                        by_regime=by_regime or {})


@pytest.fixture()
def board():
    return CH.ChampionChallengerBoard("btc-momentum", clock=FixedClock(T0))


# --- gate 5: matched comparison ---------------------------------------------

def test_a_mismatched_cost_model_raises_rather_than_reporting():
    """Comparing a challenger measured on cheaper costs against a champion
    measured on realistic ones is the classic manufactured improvement."""
    champion = arm("champ", 0.001)
    challenger = arm("chall", 0.002, h=harness(cost_fingerprint="fee0-slip0"))
    with pytest.raises(ConfigurationError) as exc:
        CH.compare(champion, challenger)
    assert any("cost_fingerprint" in d for d in exc.value.details["differences"])


def test_a_mismatched_risk_limits_hash_raises():
    """A challenger evaluated under looser limits shows a better return and a
    worse risk profile; adopting it would adopt the looser limits silently."""
    with pytest.raises(ConfigurationError) as exc:
        CH.compare(arm("champ", 0.001),
                   arm("chall", 0.002, h=harness(risk_limits_hash="limits:v9")))
    assert any("risk_limits_hash" in d for d in exc.value.details["differences"])


def test_a_mismatched_dataset_raises():
    with pytest.raises(ConfigurationError):
        CH.compare(arm("champ", 0.001),
                   arm("chall", 0.002, h=harness(dataset_hash="sha:different")))


def test_an_in_sample_harness_can_never_justify_a_replacement():
    outcome = CH.compare(arm("champ", 0.001, h=harness(in_sample=True)),
                         arm("chall", 0.05, h=harness(in_sample=True)))
    assert outcome.decision is CH.Decision.INVALID
    assert "in-sample" in outcome.reasons[0]


def test_a_short_sample_is_inconclusive():
    outcome = CH.compare(arm("champ", 0.001, n=10), arm("chall", 0.05, n=10))
    assert outcome.decision is CH.Decision.INCONCLUSIVE


def test_a_genuine_out_of_sample_improvement_replaces_the_champion():
    outcome = CH.compare(arm("champ", 0.000, seed=1),
                         arm("chall", 0.01, seed=2))
    assert outcome.decision is CH.Decision.REPLACE
    assert outcome.margin > 0
    assert outcome.significance["paired_bootstrap"]["significant"] is True


def test_a_challenger_that_underperforms_keeps_the_champion():
    outcome = CH.compare(arm("champ", 0.01, seed=1), arm("chall", 0.0, seed=2))
    assert outcome.decision is CH.Decision.KEEP_CHAMPION


# --- parsimony --------------------------------------------------------------

def test_a_simpler_challenger_wins_a_statistical_tie():
    """The branch that lets Olympus *remove* complexity. Without it a framework
    ratchets complexity upward forever."""
    outcome = CH.compare(arm("complex", 0.001, complexity=50, seed=1),
                         arm("simple", 0.001, complexity=5, seed=1))
    assert outcome.decision is CH.Decision.REPLACE
    assert any("simpler" in r for r in outcome.reasons)


def test_a_more_complex_challenger_loses_a_statistical_tie():
    outcome = CH.compare(arm("simple", 0.001, complexity=5, seed=1),
                         arm("complex", 0.001, complexity=50, seed=1))
    assert outcome.decision is CH.Decision.KEEP_CHAMPION
    assert any("not simpler" in r for r in outcome.reasons)


def test_a_simpler_but_costlier_challenger_does_not_win_a_tie():
    outcome = CH.compare(arm("champ", 0.001, complexity=50, cost=1.0, seed=1),
                         arm("chall", 0.001, complexity=5, cost=99.0, seed=1))
    assert outcome.decision is CH.Decision.KEEP_CHAMPION


def test_a_winning_but_more_complex_challenger_is_noted_as_carrying_that_cost():
    outcome = CH.compare(arm("champ", 0.0, complexity=5, seed=1),
                         arm("chall", 0.01, complexity=90, seed=2))
    assert outcome.decision is CH.Decision.REPLACE
    assert any("more complex" in r for r in outcome.reasons)


# --- risk and regime --------------------------------------------------------

def test_a_challenger_that_worsens_drawdown_is_refused():
    rng = random.Random(3)
    champion = CH.ArmResult(contender=contender("champ"), harness=harness(),
                            net_returns=[0.001] * 200)
    # Better mean, but with a deep hole in the middle.
    returns = [0.01] * 100 + [-0.30] + [0.01] * 99
    challenger = CH.ArmResult(contender=contender("chall"), harness=harness(),
                              net_returns=returns)
    outcome = CH.compare(champion, challenger)
    assert outcome.decision is CH.Decision.KEEP_CHAMPION
    assert any("drawdown" in r for r in outcome.reasons)


def test_a_challenger_materially_worse_in_one_regime_is_refused():
    """An average that hides a regime where the new component loses money is
    not an improvement."""
    champion = arm("champ", 0.0, seed=1,
                   by_regime={"ranging": series(0.01, 60, 4),
                              "trending_up": series(0.0, 60, 5)})
    challenger = arm("chall", 0.01, seed=2,
                     by_regime={"ranging": series(-0.02, 60, 6),
                                "trending_up": series(0.02, 60, 7)})
    outcome = CH.compare(champion, challenger)
    assert outcome.decision is CH.Decision.KEEP_CHAMPION
    assert "ranging" in outcome.regime_analysis
    assert outcome.regime_analysis["ranging"]["verdict"] == "challenger materially worse"


def test_a_thin_regime_slice_reports_insufficient_sample_not_a_verdict():
    champion = arm("champ", 0.0, seed=1, by_regime={"crisis": [0.01, 0.02]})
    challenger = arm("chall", 0.01, seed=2, by_regime={"crisis": [0.0, 0.0]})
    outcome = CH.compare(champion, challenger)
    assert outcome.regime_analysis["crisis"]["verdict"] == "insufficient sample"


def test_a_significant_but_tiny_margin_is_inconclusive():
    """Statistical significance is not the same as materiality. A consistent
    one-basis-point edge is unarguably real and not worth switching for."""
    base = series(0.0, 200, seed=1)
    champion = CH.ArmResult(contender=contender("champ"), harness=harness(),
                            net_returns=base)
    challenger = CH.ArmResult(contender=contender("chall"), harness=harness(),
                              net_returns=[r + 0.0001 for r in base])
    outcome = CH.compare(champion, challenger, min_edge_in_vol=5.0)
    assert outcome.significance["paired_bootstrap"]["significant"] is True
    assert outcome.decision is CH.Decision.INCONCLUSIVE
    assert any("required edge" in r for r in outcome.reasons)


# --- the board --------------------------------------------------------------

def test_setting_the_champion_requires_an_operator(board):
    with pytest.raises(GovernanceViolation):
        board.set_champion(contender("champ"), actor=OLYMPUS, reason="initial")
    board.set_champion(contender("champ"), actor=OPERATOR, reason="initial")
    assert board.champion().contender_id == "champ"


def test_entering_a_challenger_is_autonomous(board):
    """Proposing a candidate risks nothing."""
    board.set_champion(contender("champ"), actor=OPERATOR, reason="initial")
    board.add_challenger(contender("chall"), actor=OLYMPUS)
    assert [c.contender_id for c in board.challengers()] == ["chall"]


def test_the_champion_cannot_challenge_itself(board):
    board.set_champion(contender("champ"), actor=OPERATOR, reason="initial")
    with pytest.raises(ConfigurationError):
        board.add_challenger(contender("champ"), actor=OLYMPUS)


def test_running_a_contest_is_autonomous_but_installing_is_not(board):
    board.set_champion(contender("champ"), actor=OPERATOR, reason="initial")
    board.add_challenger(contender("chall"), actor=OLYMPUS)
    outcome = board.run_contest(arm("champ", 0.0, seed=1),
                                arm("chall", 0.01, seed=2), actor=OLYMPUS)
    assert outcome.decision is CH.Decision.REPLACE
    with pytest.raises(GovernanceViolation):
        board.install("chall", actor=OLYMPUS, reason="it won")
    board.install("chall", actor=OPERATOR, reason="reviewed and accepted")
    assert board.champion().contender_id == "chall"


def test_an_operator_cannot_install_a_challenger_that_never_won(board):
    """Human authority decides whether to act on evidence, not whether evidence
    is required."""
    board.set_champion(contender("champ"), actor=OPERATOR, reason="initial")
    board.add_challenger(contender("chall"), actor=OLYMPUS)
    with pytest.raises(PromotionDenied) as exc:
        board.install("chall", actor=OPERATOR, reason="I like it")
    assert "no recorded contest" in str(exc.value)


def test_an_operator_cannot_install_after_a_losing_contest(board):
    board.set_champion(contender("champ"), actor=OPERATOR, reason="initial")
    board.add_challenger(contender("chall"), actor=OLYMPUS)
    board.run_contest(arm("champ", 0.01, seed=1), arm("chall", 0.0, seed=2))
    with pytest.raises(PromotionDenied):
        board.install("chall", actor=OPERATOR, reason="try anyway")


def test_installing_records_the_previous_champion(board):
    board.set_champion(contender("champ"), actor=OPERATOR, reason="initial")
    board.add_challenger(contender("chall"), actor=OLYMPUS)
    board.run_contest(arm("champ", 0.0, seed=1), arm("chall", 0.01, seed=2))
    board.install("chall", actor=OPERATOR, reason="accepted")
    stored = board._get("champion")
    assert stored["previous_champion"] == "champ"


def test_contest_outcomes_persist_and_are_queryable(board):
    board.set_champion(contender("champ"), actor=OPERATOR, reason="initial")
    board.add_challenger(contender("chall"), actor=OLYMPUS)
    board.run_contest(arm("champ", 0.0, seed=1), arm("chall", 0.01, seed=2))
    fresh = CH.ChampionChallengerBoard("btc-momentum", clock=FixedClock(T0))
    outcomes = fresh.outcomes(challenger_id="chall")
    assert len(outcomes) == 1 and outcomes[0].replaces


def test_the_report_summarises_the_arena(board):
    board.set_champion(contender("champ"), actor=OPERATOR, reason="initial")
    board.add_challenger(contender("chall"), actor=OLYMPUS)
    board.run_contest(arm("champ", 0.01, seed=1), arm("chall", 0.0, seed=2))
    report = board.report()
    assert report["champion"] == "champ"
    assert report["by_decision"]["keep_champion"] == 1


# --- contracts --------------------------------------------------------------

def test_a_harness_without_a_risk_limits_hash_is_refused():
    with pytest.raises(ConfigurationError):
        harness(risk_limits_hash="")


def test_an_empty_harness_window_is_refused():
    with pytest.raises(ConfigurationError):
        harness(start=T1, end=T0)


def test_two_identical_harnesses_share_a_fingerprint():
    assert harness().fingerprint == harness().fingerprint
    assert harness().fingerprint != harness(dataset_id="eth-1h").fingerprint


def test_complexity_must_be_declared_and_non_negative():
    with pytest.raises(ConfigurationError):
        CH.Contender(contender_id="x", kind="model", version="1", complexity=-1)

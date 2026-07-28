"""The autonomous / human-only split.

The Phase 10 governance rule is two lists. These tests check that the lists are
complete, disjoint, and — the part that matters — that an autonomous actor
attempting a human-only action gets an exception rather than a `False` it could
ignore.
"""

from __future__ import annotations

import pytest

from olympus.trading import governance as G
from olympus.trading.errors import ConfigurationError, GovernanceViolation


OPERATOR = G.Actor.operator("alice", token="tok-123")
OLYMPUS = G.Actor.autonomous("evolution-cycle")


# --- the lists --------------------------------------------------------------

def test_every_action_is_classified():
    """An unclassified action is refused, but the right fix is to classify it —
    so an unclassified action existing at all is a bug."""
    classified = G.AUTONOMOUS_ACTIONS | G.HUMAN_ONLY_ACTIONS | G.PROHIBITED_ACTIONS
    unclassified = set(G.Action) - classified
    assert unclassified == set(), f"unclassified actions: {unclassified}"


def test_the_three_classes_are_disjoint():
    assert not (G.AUTONOMOUS_ACTIONS & G.HUMAN_ONLY_ACTIONS)
    assert not (G.AUTONOMOUS_ACTIONS & G.PROHIBITED_ACTIONS)
    assert not (G.HUMAN_ONLY_ACTIONS & G.PROHIBITED_ACTIONS)


@pytest.mark.parametrize("action", [
    G.Action.ENABLE_LIVE_TRADING, G.Action.MODIFY_RISK_LIMITS,
    G.Action.CLEAR_KILL_SWITCH, G.Action.EXPAND_PERMISSIONS,
    G.Action.MODIFY_SAFETY_KERNEL, G.Action.RISK_REAL_CAPITAL,
    G.Action.DEPLOY_CODE, G.Action.MERGE_CODE, G.Action.ROTATE_SECRETS,
    G.Action.DISABLE_MONITORING, G.Action.DISABLE_AUDIT,
])
def test_the_dangerous_actions_are_human_only(action):
    assert action in G.HUMAN_ONLY_ACTIONS
    with pytest.raises(GovernanceViolation):
        G.authorise(action, OLYMPUS)


@pytest.mark.parametrize("action", [
    G.Action.OBSERVE, G.Action.LEARN, G.Action.EVALUATE, G.Action.RESEARCH,
    G.Action.GENERATE_HYPOTHESIS, G.Action.BUILD_PROTOTYPE,
    G.Action.PROPOSE_IMPROVEMENT, G.Action.RECOMMEND_DEPRECATION,
    G.Action.RESTRICT_COMPONENT,
])
def test_the_analysis_actions_are_autonomous(action):
    assert G.authorise(action, OLYMPUS).granted is True


def test_stopping_is_autonomous_but_starting_is_not():
    """The asymmetry the whole phase rests on."""
    assert G.may(G.Action.EMERGENCY_SHUTDOWN, OLYMPUS) is True
    assert G.may(G.Action.CLEAR_KILL_SWITCH, OLYMPUS) is False


def test_concealing_a_result_is_available_to_nobody():
    for actor in (OLYMPUS, OPERATOR, G.Actor.system()):
        with pytest.raises(GovernanceViolation):
            G.authorise(G.Action.CONCEAL_RESULT, actor)
        with pytest.raises(GovernanceViolation):
            G.authorise(G.Action.REWRITE_AUDIT_HISTORY, actor)


# --- actors -----------------------------------------------------------------

def test_an_operator_without_a_token_cannot_be_constructed():
    """Otherwise 'a human approved this' is assertable by the code itself."""
    with pytest.raises(ConfigurationError):
        G.Actor(name="mallory", kind=G.ActorKind.OPERATOR)
    with pytest.raises(ConfigurationError):
        G.Actor.operator("mallory", token="   ")


def test_an_actor_must_be_named():
    with pytest.raises(ConfigurationError):
        G.Actor.autonomous("")


def test_serialising_an_actor_never_carries_the_token():
    data = OPERATOR.to_dict()
    assert data == {"name": "alice", "kind": "operator",
                    "operator_token_present": True}
    assert "tok-123" not in repr(OPERATOR)
    assert "tok-123" not in str(OPERATOR)
    assert "tok-123" not in str(data)


def test_a_system_actor_is_not_an_operator():
    with pytest.raises(GovernanceViolation):
        G.authorise(G.Action.DEPLOY_CODE, G.Actor.system("ci"))


# --- the refusal shape ------------------------------------------------------

def test_refusal_raises_rather_than_returning_false():
    """An autonomous caller can ignore a return value. It cannot ignore this."""
    with pytest.raises(GovernanceViolation) as exc:
        G.authorise(G.Action.PROMOTE_TO_LIVE, OLYMPUS, subject="strategy-a")
    assert exc.value.details["actor_kind"] == "autonomous"
    assert exc.value.details["subject"] == "strategy-a"


def test_may_is_the_non_raising_form():
    assert G.may(G.Action.PROMOTE_TO_LIVE, OPERATOR) is True
    assert G.may(G.Action.PROMOTE_TO_LIVE, OLYMPUS) is False


def test_require_operator_refuses_an_autonomous_actor():
    with pytest.raises(GovernanceViolation):
        G.require_operator(OLYMPUS, what="a promotion")
    assert G.require_operator(OPERATOR, what="a promotion") is OPERATOR


def test_an_attempt_is_recorded_even_when_it_is_refused():
    """A blocked attempt is exactly the event an operator wants to see later."""
    class Recorder:
        def __init__(self):
            self.events = []

        def record(self, event_type, payload, **kwargs):
            self.events.append((event_type, payload))

    audit = Recorder()
    with pytest.raises(GovernanceViolation):
        G.authorise(G.Action.MODIFY_RISK_LIMITS, OLYMPUS, audit=audit)
    assert audit.events, "a refused attempt must still reach the audit trail"
    assert audit.events[0][1]["granted"] is False


def test_an_unknown_action_string_is_refused_not_invented():
    with pytest.raises(ValueError):
        G.authorise("make_me_a_sandwich", OPERATOR)


def test_describe_governance_covers_every_action():
    text = G.describe_governance()
    for action in G.Action:
        assert action.value in text

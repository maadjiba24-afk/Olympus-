"""Feature and code-change proposals — Phase 10 completion gate 9.

Gate 9: produce code proposals without autonomously deploying them.

Two absences are asserted structurally rather than by convention: no function
in the module applies a change, and no proposal can name a safety-kernel file.
Both are parsed from the AST so a future refactor that adds one fails the build.
"""

from __future__ import annotations

import ast
import inspect
from datetime import datetime, timezone

import pytest

from olympus.trading import proposals as P
from olympus.trading.clock import FixedClock
from olympus.trading.errors import (ConfigurationError, GovernanceViolation,
                                    SafetyKernelViolation)
from olympus.trading.governance import Actor

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
OPERATOR = Actor.operator("alice", token="tok")
OLYMPUS = Actor.autonomous("evolution-cycle")

DIFF = "--- a/olympus/trading/ta.py\n+++ b/olympus/trading/ta.py\n+    return out\n"


@pytest.fixture()
def register():
    return P.ProposalRegister(clock=FixedClock(T0))


def feature(proposal_id="f1", **kwargs):
    kwargs.setdefault("title", "Add a Kyle-lambda liquidity feature")
    kwargs.setdefault("kind", P.FeatureKind.LIQUIDITY_MEASURE)
    kwargs.setdefault("problem", "Sizing ignores how much depth is available.")
    kwargs.setdefault("expected_value", "Fewer oversized orders in thin books.")
    kwargs.setdefault("implementation_plan", "Compute from trade and quote data.")
    kwargs.setdefault("test_plan", "Unit tests plus a walk-forward ablation.")
    kwargs.setdefault("rollback_plan", "Remove the feature from the feature set.")
    kwargs.setdefault("acceptance_criteria", ("slippage falls out of sample",))
    return P.FeatureProposal(proposal_id=proposal_id, created_at=T0, **kwargs)


def code_change(proposal_id="c1", **kwargs):
    kwargs.setdefault("title", "Fix an off-by-one in the ATR warm-up")
    kwargs.setdefault("target_files", ("olympus/trading/ta.py",))
    kwargs.setdefault("diff", DIFF)
    kwargs.setdefault("rationale", "The warm-up region was one bar short.")
    kwargs.setdefault("expected_impact", "ATR values shift by one bar at the start.")
    kwargs.setdefault("rollback_plan", "Revert the commit.")
    kwargs.setdefault("tests_added", ("tests/test_trading_ta.py::test_atr_warmup",))
    return P.CodeChangeProposal(proposal_id=proposal_id, created_at=T0, **kwargs)


# --- gate 9: nothing here deploys -------------------------------------------

def test_the_module_has_no_function_that_applies_a_change():
    """Asserted on the AST so a future refactor adding one fails the build."""
    tree = ast.parse(inspect.getsource(P))
    names = {node.name for node in ast.walk(tree)
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    forbidden = {"apply", "merge", "deploy", "commit", "push", "apply_patch",
                 "merge_pull_request", "enact"}
    assert not (names & forbidden), f"proposals.py must not define {names & forbidden}"


def test_the_register_reports_zero_applied_structurally(register):
    register.submit_code_change(code_change())
    report = register.report()
    assert report["applied"] == 0
    assert "no code path that applies" in report["note"]


def test_a_serialised_proposal_says_it_was_not_applied():
    assert code_change().to_dict()["applied"] is False


def test_reviewing_records_a_decision_without_enacting_it(register):
    register.submit_code_change(code_change())
    data = register.record_review("c1", kind="code", actor=OPERATOR,
                                  accepted=True, notes="looks right")
    assert data["state"] == "accepted"
    assert data["reviewer"] == "alice"
    assert "outside olympus.trading" in data["note"]


def test_an_autonomous_actor_cannot_record_its_own_review(register):
    """A review step the system can perform on itself is ceremonial."""
    register.submit_code_change(code_change())
    with pytest.raises(GovernanceViolation):
        register.record_review("c1", kind="code", actor=OLYMPUS, accepted=True)


def test_submitting_a_code_change_is_autonomous(register):
    submitted = register.submit_code_change(code_change(), actor=OLYMPUS)
    assert submitted.state is P.ReviewState.SUBMITTED


# --- the kernel refusal -----------------------------------------------------

@pytest.mark.parametrize("path", ["olympus/trading/risk.py",
                                  "olympus/trading/killswitch.py",
                                  "olympus/trading/modes.py",
                                  "olympus/vault.py",
                                  "olympus/trading/execution.py",
                                  "olympus/trading/brokers/binance_testnet.py"])
def test_a_patch_targeting_the_safety_kernel_cannot_be_constructed(path):
    """It raises before review, before CI, before anyone can be persuaded that
    this particular limit change is fine."""
    with pytest.raises(SafetyKernelViolation):
        code_change(target_files=(path,))


def test_a_patch_body_reaching_into_the_kernel_is_refused():
    """A change to a non-kernel file that imports the vault is still a kernel
    change."""
    with pytest.raises(SafetyKernelViolation) as exc:
        code_change(diff="--- a/x\n+++ b/x\n+from olympus import vault\n")
    assert "vault" in str(exc.value)


@pytest.mark.parametrize("line,fragment", [
    ("+    store = LimitsStore()", "LimitsStore"),
    ("+    switches.disengage(scope='global')", "kill switch"),
    ("+    controller.enable_live(operator='x')", "live trading"),
    ("+    if mode is Mode.LIVE:", "live mode"),
])
def test_specific_kernel_reaching_patch_lines_are_refused(line, fragment):
    with pytest.raises(SafetyKernelViolation) as exc:
        code_change(diff=f"--- a/x\n+++ b/x\n{line}\n")
    assert fragment in str(exc.value)


def test_an_ordinary_patch_is_accepted():
    proposal = code_change()
    assert proposal.touched_modules == ("olympus.trading.ta",)


def test_a_defect_report_against_the_kernel_is_allowed():
    """Only a *change* is refused. Olympus finding a bug in the risk engine and
    being unable to say so would be strictly worse."""
    report = P.DefectReport(defect_id="d1", module="olympus/trading/risk.py",
                            severity=P.DefectSeverity.CORRECTNESS,
                            summary="the daily-loss check uses the wrong sign",
                            detected_at=T0)
    assert report.in_safety_kernel is True
    assert report.to_dict()["severity"] == "correctness"


# --- required completeness --------------------------------------------------

@pytest.mark.parametrize("field", ["problem", "expected_value",
                                   "implementation_plan", "test_plan",
                                   "rollback_plan"])
def test_a_feature_proposal_requires_every_named_field(field):
    with pytest.raises(ConfigurationError) as exc:
        feature(**{field: "   "})
    assert field in str(exc.value)


def test_a_feature_proposal_requires_acceptance_criteria():
    """Without it there is no way to tell afterwards whether the feature did
    what it promised."""
    with pytest.raises(ConfigurationError):
        feature(acceptance_criteria=())


def test_security_implications_must_be_stated_even_when_none():
    """The absence of an answer is not the same as 'no impact'."""
    with pytest.raises(ConfigurationError):
        feature(security_implications=())
    assert feature().needs_security_review is False


def test_a_kernel_touching_feature_proposal_must_explain_itself():
    with pytest.raises(ConfigurationError):
        feature(security_implications=(P.SecurityImpact.TOUCHES_SAFETY_KERNEL,),
                security_notes="")
    allowed = feature(
        security_implications=(P.SecurityImpact.TOUCHES_SAFETY_KERNEL,),
        security_notes="recommends a new per-venue cap; for human review only")
    assert allowed.needs_security_review is True


def test_a_code_change_must_add_a_test():
    """An untested repair is how a defect comes back."""
    with pytest.raises(ConfigurationError) as exc:
        code_change(tests_added=())
    assert "test" in str(exc.value)


def test_a_code_change_must_name_the_files_it_edits():
    with pytest.raises(ConfigurationError):
        code_change(target_files=())


def test_a_code_change_must_state_how_to_reverse_it():
    with pytest.raises(ConfigurationError):
        code_change(rollback_plan="  ")


# --- both meanings of "feature" ---------------------------------------------

def test_market_features_and_system_capabilities_are_distinguished():
    assert feature(kind=P.FeatureKind.INDICATOR).is_market_feature is True
    assert feature(kind=P.FeatureKind.BROKER_INTEGRATION).is_market_feature is False


def test_the_kinds_cover_both_lists_from_the_specification():
    values = {k.value for k in P.FeatureKind}
    for market in ("indicator", "volatility_measure", "liquidity_measure",
                   "regime_indicator", "cross_asset_signal",
                   "sentiment_variable", "fundamental_variable",
                   "calendar_effect", "execution_quality_feature"):
        assert market in values
    for system in ("broker_integration", "data_provider", "monitoring",
                   "reconciliation", "visualisation", "incident_detection",
                   "research_tool", "evaluation_report", "operator_control",
                   "new_market"):
        assert system in values


# --- CI ---------------------------------------------------------------------

def test_ci_results_are_recorded_including_failures(register):
    register.submit_code_change(code_change())
    failed = register.mark_ci_result("c1", status=P.CIStatus.FAILED,
                                     detail="3 tests failed")
    assert failed.ci_status is P.CIStatus.FAILED
    assert failed.ready_for_review is False
    assert register.report()["ci_failed"] == 1


def test_a_passing_run_makes_a_proposal_ready_for_review_not_mergeable(register):
    register.submit_code_change(code_change())
    passed = register.mark_ci_result("c1", status=P.CIStatus.PASSED)
    assert passed.ready_for_review is True
    assert passed.to_dict()["applied"] is False


def test_marking_ci_on_an_unknown_proposal_is_refused(register):
    with pytest.raises(ConfigurationError):
        register.mark_ci_result("nope", status=P.CIStatus.PASSED)


# --- persistence ------------------------------------------------------------

def test_proposals_persist_across_register_instances(register):
    register.submit_feature(feature())
    register.submit_code_change(code_change())
    fresh = P.ProposalRegister(clock=FixedClock(T0))
    assert fresh.feature("f1") is not None
    assert fresh.code_change("c1") is not None


def test_features_and_code_changes_do_not_collide(register):
    register.submit_feature(feature("same-id"))
    register.submit_code_change(code_change("same-id"))
    assert register.feature("same-id").title.startswith("Add a Kyle")
    assert register.code_change("same-id").title.startswith("Fix an off-by-one")


def test_the_report_counts_what_is_awaiting_review(register):
    register.submit_feature(feature())
    register.submit_code_change(code_change())
    assert register.report()["awaiting_review"] == 2
